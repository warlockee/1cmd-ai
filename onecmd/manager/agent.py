"""LLM agent loop — conversation management and tool execution.

Calling spec:
  Inputs:  chat_id (int), user message text (str)
  Outputs: assistant response text (str)
  Side effects: LLM API calls, tool execution via TOOL_REGISTRY,
                memory reads/writes, conversation state mutation

Constructor:
  Agent(backend, config, notify_fn)
    backend:   ValidatedBackend instance (pre-scoped, rate-limited)
    config:    Config instance (has mgr_model, has_llm_key, etc.)
    notify_fn: Callable[[int, str], None] — sends a message to the user

Main method:
  agent.handle_message(chat_id, text) -> str

Bounds:
  MAX_TOOL_ROUNDS: 15       — max LLM calls per single user message
  MAX_CONVERSATION_TURNS: 30 — hard cap on conversation history (in turns)
  SUMMARIZE_THRESHOLD: 20   — trigger background summarization at this many turns
  SUMMARIZE_KEEP_TURNS: 10  — keep this many recent turns after trimming
  SUMMARY_MAX_CHARS: 2000   — cap rolling summary length

All terminal operations go through ValidatedBackend.
Provider fallback handled by ProviderManager (auto-switches on rate limit).
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable

from onecmd.manager import memory
from onecmd.manager.llm import ProviderManager
from onecmd.manager.skills import ensure_skills
from onecmd.manager.tasks import _next_task_id
from onecmd.manager.tools import TOOL_SCHEMAS, dispatch

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_TOOL_ROUNDS: int = 100
MAX_CONVERSATION_TURNS: int = 30
SUMMARIZE_THRESHOLD: int = 20
SUMMARIZE_KEEP_TURNS: int = 10
SUMMARY_MAX_CHARS: int = 2000
TASK_PRUNE_AGE: float = 3600.0

NotifyFn = Callable[[int, str], None]

# ---------------------------------------------------------------------------
# Markdown stripping — Telegram legacy parser breaks on **, *, _, `
# ---------------------------------------------------------------------------

_CODE_BLOCK_RE = re.compile(r"```[\s\S]*?```")
_INLINE_CODE_RE = re.compile(r"`([^`]+)`")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_ITALIC_STAR_RE = re.compile(r"\*(.+?)\*")
_ITALIC_UNDER_RE = re.compile(r"(?<!\w)_(.+?)_(?!\w)")
_HEADING_RE = re.compile(r"^#{1,6}\s+", re.MULTILINE)


_TRAILING_FILLER_RE = re.compile(
    r"\s*(?:is that all|anything else|need anything|want anything|"
    r"let me know if|do you need|shall i|would you like|can i help"
    r")[\s\S]{0,30}$",
    re.IGNORECASE,
)


def strip_markdown(text: str) -> str:
    """Remove common markdown formatting from LLM output for plain-text display."""
    text = _CODE_BLOCK_RE.sub(lambda m: m.group()[3:].lstrip().rsplit("```", 1)[0], text)
    text = _INLINE_CODE_RE.sub(r"\1", text)
    text = _BOLD_RE.sub(r"\1", text)
    text = _ITALIC_STAR_RE.sub(r"\1", text)
    text = _ITALIC_UNDER_RE.sub(r"\1", text)
    text = _HEADING_RE.sub("", text)
    text = _TRAILING_FILLER_RE.sub("", text)
    return text


# ---------------------------------------------------------------------------
# System prompt — loaded from file with bundled default fallback
# ---------------------------------------------------------------------------

_DEFAULT_PROMPT_FILE = Path(__file__).parent / "default_agent_prompt.md"
_USER_PROMPT_FILE = Path(".onecmd/ai_personality.md")

# mtime-based cache for system prompt
_prompt_cache: dict[str, tuple[float, str]] = {}  # path -> (mtime, content)


def _load_system_prompt() -> str:
    """Load agent system prompt: user override > bundled default.

    Uses mtime-based caching — only re-reads when the file changes.
    Empty files are skipped — always falls back to a working prompt.
    """
    for path in (_USER_PROMPT_FILE, _DEFAULT_PROMPT_FILE):
        if path.exists():
            try:
                mtime = path.stat().st_mtime
                key = str(path)
                cached = _prompt_cache.get(key)
                if cached and cached[0] == mtime:
                    return cached[1]
                text = path.read_text().rstrip()
                if text:
                    _prompt_cache[key] = (mtime, text)
                    return text
            except OSError:
                continue
    logger.warning("Agent prompt files not found, using hardcoded fallback")
    return "You are the onecmd AI manager agent. Help the user monitor and control their terminal sessions."


_MAX_PROMPT_MEMORIES: int = 30  # only include N most recent in system prompt


def _build_system_prompt(
    memories: list[tuple[int, str, str]] | None = None,
    summary: str | None = None,
    skills_context: str = "",
    base_override: str | None = None,
) -> str:
    """Assemble the full system prompt from base + memories + summary + skills."""
    prompt = base_override if base_override else _load_system_prompt()
    if memories:
        total = len(memories)
        # Only include the most recent N memories (list is ORDER BY id ASC)
        included = memories[-_MAX_PROMPT_MEMORIES:] if total > _MAX_PROMPT_MEMORIES else memories
        omitted = total - len(included)
        prompt += "\n\nYOUR MEMORIES FOR THIS USER:"
        if omitted:
            prompt += f"\n[{omitted} older memories stored but omitted — use list_memories tool to see all]"
        for mid, content, category in included:
            prompt += f"\n- [#{mid}] ({category}) {content}"
    if summary:
        prompt += "\n\nCONVERSATION SUMMARY (older context):\n" + summary
    if skills_context:
        prompt += "\n\n" + skills_context
    return prompt


# ---------------------------------------------------------------------------
# Conversation helpers
# ---------------------------------------------------------------------------


def _clean_orphans(conv: list[dict[str, Any]]) -> None:
    """Drop leading orphaned tool_result / tool_use messages after a trim."""
    # Drop leading user messages that are tool_results without a preceding tool_use
    while conv and conv[0].get("role") == "user":
        content = conv[0].get("content")
        if (isinstance(content, list) and content
                and isinstance(content[0], dict)
                and content[0].get("type") == "tool_result"):
            conv.pop(0)
        else:
            break
    # Drop leading assistant messages that are tool_use without a following result
    while conv and conv[0].get("role") == "assistant":
        content = conv[0].get("content")
        has_tool_use = (isinstance(content, list) and any(
            isinstance(b, dict) and b.get("type") == "tool_use" for b in content))
        if has_tool_use:
            conv.pop(0)
            # Also drop the orphaned tool_result that follows
            if conv and conv[0].get("role") == "user":
                c = conv[0].get("content")
                if (isinstance(c, list) and c
                        and isinstance(c[0], dict)
                        and c[0].get("type") == "tool_result"):
                    conv.pop(0)
        else:
            break


def _flatten_for_summary(messages: list[dict[str, Any]],
                         max_chars: int = 8000) -> str:
    """Convert conversation messages to readable text for summarization."""
    lines: list[str] = []
    total = 0
    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content", "")
        if isinstance(content, str):
            label = "User" if role == "user" else "Assistant"
            line = f"{label}: {content[:500]}"
        elif isinstance(content, list):
            parts: list[str] = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type", "")
                if btype == "text" and not block.get("thought"):
                    label = "User" if role == "user" else "Assistant"
                    parts.append(f"{label}: {block['text'][:500]}")
                elif btype == "tool_use":
                    args_str = json.dumps(block.get("input", {}))[:200]
                    parts.append(f"Tool call: {block['name']}({args_str})")
                elif btype == "tool_result":
                    result = (block.get("content") or "")[:300]
                    parts.append(f"Tool result: {result}")
            line = "\n".join(parts)
        else:
            continue
        total += len(line)
        if total > max_chars:
            lines.append("[...older messages truncated...]")
            break
        lines.append(line)
    return "\n".join(lines)


def _is_rate_limit_error(e: Exception) -> bool:
    """Check if an exception is a rate limit / quota error."""
    msg = str(e).lower()
    if "429" in msg or "rate" in msg or "quota" in msg or "limit" in msg:
        return True
    if "resource_exhausted" in msg or "resourceexhausted" in msg:
        return True
    etype = type(e).__name__
    if "RateLimitError" in etype:
        return True
    if "APIStatusError" in etype and "429" in msg:
        return True
    return False


def _is_retriable_error(e: Exception) -> bool:
    """Check if an exception is retriable on the fallback provider.

    Covers: rate limits, timeouts, connection errors, server errors (5xx).
    """
    if _is_rate_limit_error(e):
        return True
    etype = type(e).__name__
    msg = str(e).lower()
    # Timeouts
    if "timeout" in etype.lower() or "timed out" in msg or "timeout" in msg:
        return True
    # Connection errors
    if "connection" in etype.lower() or "connection" in msg:
        return True
    # Server errors (5xx)
    if "500" in msg or "502" in msg or "503" in msg or "overloaded" in msg:
        return True
    if "InternalServerError" in etype or "APIError" in etype:
        return True
    return False


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------


class Agent:
    """LLM agent with tool-use loop and conversation management.

    All terminal operations go through the ValidatedBackend passed at
    construction.  Notifications are sent via *notify_fn(chat_id, text)*.
    """

    def __init__(
        self,
        backend: Any,
        config: Any,
        notify_fn: NotifyFn,
        system_prompt_override: str | None = None,
        tool_schemas: list[dict[str, Any]] | None = None,
        dispatch_fn: Callable[[str, dict[str, Any], dict[str, Any]], str] | None = None,
    ) -> None:
        self._backend = backend
        self._config = config
        self._notify = notify_fn
        self._system_prompt_override = system_prompt_override
        self._tool_schemas = tool_schemas if tool_schemas is not None else TOOL_SCHEMAS
        self._dispatch_fn = dispatch_fn if dispatch_fn is not None else dispatch
        self.debug = False

        # Provider manager handles LLM creation + fallback
        self._providers = ProviderManager(primary=None)

        # Per-chat state
        self._conversations: dict[int, list[dict[str, Any]]] = {}
        self._summaries: dict[int, str] = {}
        self._tasks: dict[int, Any] = {}

        # Concurrency
        self._conv_locks: dict[int, threading.Lock] = {}
        self._global_lock = threading.Lock()
        self._tasks_lock = threading.Lock()

        # Start background task pruner
        threading.Thread(target=self._prune_loop, daemon=True).start()

    # -- public API ---------------------------------------------------------

    def set_model(self, provider_key: str | None, model: str | None) -> str:
        """Switch provider and/or model. Returns status message."""
        if provider_key:
            self._providers.switch_provider(provider_key)
        if model:
            self._model_override = model
        else:
            self._model_override = None

        cur = self._providers.active_name
        cur_model = self._resolve_model()
        return f"{cur} / {cur_model}"

    def get_model_info(self) -> str:
        """Return current provider and model."""
        return f"{self._providers.active_name} / {self._resolve_model()}"

    def handle_message(self, chat_id: int, text: str) -> str:
        """Process a user message and return the assistant's response text.

        Thread-safe: serialized per chat_id to prevent conversation corruption.
        Times out after 180s to prevent one stuck call from blocking follow-ups.
        """
        lock = self._get_chat_lock(chat_id)
        acquired = lock.acquire(timeout=180)
        if not acquired:
            logger.warning("Chat lock timeout for chat %d — previous request still running", chat_id)
            return "Still processing your previous request. Please wait a moment."
        try:
            return strip_markdown(self._handle_locked(chat_id, text))
        finally:
            lock.release()

    def handle_task_result(self, chat_id: int, result: str) -> None:
        """Feed a SmartTask result back into the agent for analysis.

        The agent processes the result and sends its response to the user.
        Called from SmartTask's on_complete callback (background thread).
        """
        lock = self._get_chat_lock(chat_id)
        acquired = lock.acquire(timeout=30)
        if not acquired:
            self._notify(chat_id, result)
            return
        try:
            reply = strip_markdown(self._handle_locked(chat_id, result))
            self._notify(chat_id, reply)
        except Exception as e:
            logger.error("Task result handling error: %s", e)
            self._notify(chat_id, result)
        finally:
            lock.release()

    # -- internals ----------------------------------------------------------

    def _get_chat_lock(self, chat_id: int) -> threading.Lock:
        with self._global_lock:
            if chat_id not in self._conv_locks:
                self._conv_locks[chat_id] = threading.Lock()
            return self._conv_locks[chat_id]

    def _get_conversation(self, chat_id: int) -> list[dict[str, Any]]:
        with self._global_lock:
            if chat_id not in self._conversations:
                self._conversations[chat_id] = []
            conv = self._conversations[chat_id]
            # Hard cap safety net
            if len(conv) > MAX_CONVERSATION_TURNS * 2:
                conv[:] = conv[-(MAX_CONVERSATION_TURNS * 2):]
                _clean_orphans(conv)
            return conv

    def _handle_locked(self, chat_id: int, text: str) -> str:
        """Core agent loop (called with per-chat lock held)."""
        conv = self._get_conversation(chat_id)
        conv.append({"role": "user", "content": text})
        logger.info("Processing message from %d: %s", chat_id, text[:100])

        # Summarize older messages if conversation is getting long
        self._maybe_summarize(chat_id, conv)

        # Build system prompt with memories, summary, skills
        memories = memory.list_for_chat(chat_id)
        chat_summary = self._summaries.get(chat_id)
        skills_ctx = ensure_skills()
        system_prompt = _build_system_prompt(
            memories, chat_summary, skills_ctx,
            base_override=self._system_prompt_override,
        )
        if memories:
            logger.info("Loaded %d memories for chat %d", len(memories), chat_id)

        debug_prompts = self.debug or os.environ.get("ONECMD_DEBUG_PROMPTS", "").lower() in {"1", "true", "yes"}
        if debug_prompts:
            logger.info("[PROMPT][system]\n%s", system_prompt)
            logger.info("[PROMPT][user]\n%s", text)

        # Build tool-execution context
        ctx = self._build_tool_ctx(chat_id)

        try:
            provider = self._providers.active
            prev_name = self._providers.active_name
            model = self._resolve_model()

            for round_num in range(1, MAX_TOOL_ROUNDS + 1):
                # Check if provider changed (cooldown expired)
                cur_name = self._providers.active_name
                if cur_name != prev_name and len(conv) > 1:
                    provider = self._providers.active
                    provider.convert_conversation(conv)
                    logger.info("Provider switched %s -> %s", prev_name, cur_name)
                prev_name = cur_name
                model = self._resolve_model()

                logger.info("API call round %d (%s/%s)", round_num,
                            cur_name, model)

                try:
                    serialized, text_parts, tool_uses, stop = provider.chat(
                        model, system_prompt, self._tool_schemas, conv,
                        max_tokens=provider.default_max_tokens)
                except Exception as api_err:
                    if not _is_retriable_error(api_err):
                        raise
                    is_rate_limit = _is_rate_limit_error(api_err)
                    switched = (self._providers.switch_on_rate_limit()
                                if is_rate_limit
                                else self._providers.switch_on_error())
                    if not switched:
                        raise
                    provider = self._providers.active
                    model = self._resolve_model()
                    prev_name = self._providers.active_name
                    provider.convert_conversation(conv)
                    reason = "rate limit" if is_rate_limit else type(api_err).__name__
                    self._notify(chat_id, f"Switched to {prev_name} ({reason}).")
                    serialized, text_parts, tool_uses, stop = \
                        provider.chat(model, system_prompt,
                                      self._tool_schemas, conv,
                                      max_tokens=provider.default_max_tokens)

                conv.append({"role": "assistant", "content": serialized})

                # No tool calls — return the text response
                if not tool_uses:
                    reply = "\n".join(text_parts) if text_parts else "(no response)"
                    logger.info("Sending reply (%d chars)", len(reply))
                    return reply

                # Execute tools and continue
                logger.info("Executing %d tool(s): %s",
                            len(tool_uses), [tu[1] for tu in tool_uses])
                results: list[tuple[str, str, str]] = []
                for tu_id, tu_name, tu_args in tool_uses:
                    result = self._dispatch_fn(tu_name, tu_args, ctx)
                    results.append((tu_id, tu_name, result))

                conv.append(provider.format_tool_results(results))

            # Exhausted tool rounds
            return ("Reached maximum processing steps. "
                    "Please try a simpler request.")

        except Exception as e:
            err_msg = getattr(e, "message", str(e))
            logger.error("LLM error: %s", e, exc_info=True)
            return f"API error: {err_msg}"

    def _resolve_model(self) -> str:
        """Return the LLM model to use (runtime override > config > provider default)."""
        if getattr(self, '_model_override', None):
            return self._model_override
        if self._config.mgr_model:
            return self._config.mgr_model
        name = self._providers.active_name
        defaults = {
            "google": "gemini-3-flash-preview",
            "anthropic": "claude-sonnet-4-20250514",
            "anthropic-oauth": "claude-sonnet-4-20250514",
            "openai-codex": "gpt-5.3-codex",
        }
        return defaults.get(name, "claude-sonnet-4-20250514")

    def _build_tool_ctx(self, chat_id: int) -> dict[str, Any]:
        """Build the context dict that tool functions receive."""
        from onecmd.manager.queue import TerminalQueue
        provider = self._providers.active
        model = self._resolve_model()

        def chat_fn(system: str, tools: list[dict[str, Any]],
                    messages: list[dict[str, Any]],
                    max_tokens: int) -> tuple:
            return provider.chat(model, system, tools, messages, max_tokens)

        return {
            "backend": self._backend,
            "queue_cls": TerminalQueue,
            "tasks": self._tasks,
            "tasks_lock": self._tasks_lock,
            "chat_id": chat_id,
            "notify": self._notify,
            "debug": self.debug,
            "handle_task_result": self.handle_task_result,
            "llm_client": provider,
            "llm_model": model,
            "chat_fn": chat_fn,
            "format_results_fn": provider.format_tool_results,
            "next_task_id": _next_task_id,
            "dispatch_fn": self._dispatch_fn,
            "skills_enabled": getattr(self._config, "agent_mode", "legacy") == "skills",
            "skills_dir": getattr(self._config, "skills_dir", ".onecmd/skills"),
            "skills_max_steps": getattr(self._config, "skills_max_steps", 20),
        }

    # -- summarization ------------------------------------------------------

    def _maybe_summarize(self, chat_id: int,
                         conv: list[dict[str, Any]]) -> None:
        """Trim + background-summarize when conversation exceeds threshold."""
        if len(conv) <= SUMMARIZE_THRESHOLD * 2:
            return

        keep_from = len(conv) - SUMMARIZE_KEEP_TURNS * 2
        if keep_from <= 0:
            return

        # Don't split tool_use/tool_result pairs
        while keep_from > 1:
            msg = conv[keep_from]
            content = msg.get("content")
            if (msg.get("role") == "user" and isinstance(content, list)
                    and content and isinstance(content[0], dict)
                    and content[0].get("type") == "tool_result"):
                keep_from -= 1
            else:
                break

        to_summarize = conv[:keep_from]
        flat = _flatten_for_summary(to_summarize)

        # Trim immediately — summary will be available next message
        conv[:] = conv[keep_from:]
        _clean_orphans(conv)

        if not flat.strip():
            return

        old_summary = self._summaries.get(chat_id, "")
        threading.Thread(
            target=self._summarize_background,
            args=(chat_id, flat, old_summary),
            daemon=True,
        ).start()

    def _summarize_background(self, chat_id: int, flat: str,
                              old_summary: str) -> None:
        """Run LLM summarization in a background thread."""
        prompt_text = flat
        if old_summary:
            prompt_text = (f"Previous summary:\n{old_summary}\n\n"
                           f"New conversation to incorporate:\n{flat}")

        try:
            provider = self._providers.active
            model = self._resolve_model()
            summary_system = (
                "Summarize this conversation between a user and a terminal "
                "management AI. Focus on: which terminals were discussed "
                "(IDs and names), what commands were run and their outcomes, "
                "the user's current goals, key facts about their environment, "
                "and any pending/unresolved actions. Be concise. Output only "
                "the summary. Plain text, no markdown. Max 400 words.")
            _, text_parts, _, _ = provider.chat(
                model, summary_system, [],
                [{"role": "user", "content": prompt_text}], 600)
            summary = "\n".join(text_parts).strip()
            if summary:
                if len(summary) > SUMMARY_MAX_CHARS:
                    summary = summary[:SUMMARY_MAX_CHARS] + "..."
                self._summaries[chat_id] = summary
                logger.info("Summarized conversation for chat %d (%d chars)",
                            chat_id, len(summary))
        except Exception as e:
            logger.error("Summarization failed for chat %d: %s", chat_id, e)

    # -- task pruning -------------------------------------------------------

    def _prune_loop(self) -> None:
        """Periodically remove completed/failed tasks older than PRUNE_AGE."""
        while True:
            time.sleep(3600)
            try:
                self._prune_tasks()
            except Exception as e:
                logger.error("Task prune error: %s", e)

    def _prune_tasks(self) -> None:
        now = time.time()
        terminal = ("done", "error", "no_change", "completed",
                    "failed", "cancelled")
        to_delete: list[int] = []
        with self._tasks_lock:
            for tid, task in self._tasks.items():
                status = getattr(task, "status", "")
                finished = getattr(task, "finished_at", None)
                if (status in terminal and finished
                        and (now - finished) > TASK_PRUNE_AGE):
                    to_delete.append(tid)
            for tid in to_delete:
                del self._tasks[tid]
        if to_delete:
            logger.info("Pruned %d old tasks.", len(to_delete))
