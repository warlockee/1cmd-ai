"""
onecmd.manager.llm — LLM provider abstraction for Anthropic, Gemini, and OpenAI Codex.

Calling spec:
  Inputs:  messages (list[dict]), tools (list[dict]), system prompt (str),
           model (str), max_tokens (int)
  Outputs: (content, text_parts, tool_uses, stop_reason)
           content:     list[dict] — serialized blocks for conversation history
           text_parts:  list[str]  — non-thought text from response
           tool_uses:   list[ToolUse] — (id, name, args) tuples
           stop_reason: "end_turn" | "tool_use" | None
  Side effects: HTTP calls to Claude/Gemini API

Provider registry:
  PROVIDERS = {
    "anthropic": AnthropicProvider,
    "google": GeminiProvider,
    "openai-codex": CodexProvider,
  }

Detection:
  - Explicit override: ONECMD_MGR_PROVIDER
  - Then env-backed providers (GOOGLE_API_KEY, ANTHROPIC_API_KEY)
  - Then Codex auth.json / OPENAI_CODEX_* env

Fallback: switches to secondary provider on rate limit (5-minute cooldown)
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any
from urllib import error, request

from onecmd.auth.codex import CodexAuthError, ensure_fresh_codex_credentials, has_codex_credentials

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Normalised response types
# ---------------------------------------------------------------------------
# tool_use: (id, name, args)  — id is str for Anthropic, generated for Gemini
# text: plain string

ToolUse = tuple[str, str, dict[str, Any]]  # (id, name, args)


# ---------------------------------------------------------------------------
# Provider base
# ---------------------------------------------------------------------------

class _Provider:
    """Minimal provider interface — subclasses implement chat()."""

    name: str
    default_max_tokens: int = 65536

    def chat(
        self,
        model: str,
        system: str,
        tools: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        max_tokens: int,
    ) -> tuple[list[dict[str, Any]], list[str], list[ToolUse], str | None]:
        raise NotImplementedError

    def format_tool_results(
        self,
        results: list[tuple[str, str, str]],
    ) -> dict[str, Any]:
        raise NotImplementedError

    def convert_conversation(
        self,
        conv: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Clean conversation history for this provider. Default: no-op."""
        return conv


# ---------------------------------------------------------------------------
# Anthropic provider
# ---------------------------------------------------------------------------

class AnthropicProvider(_Provider):
    name = "anthropic"
    default_max_tokens = 64000  # Sonnet/Haiku: 64K, Opus 4.6: 128K

    def __init__(self) -> None:
        import anthropic
        self._client = anthropic.Anthropic(timeout=120.0)

    def chat(
        self,
        model: str,
        system: str,
        tools: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        max_tokens: int,
    ) -> tuple[list[dict[str, Any]], list[str], list[ToolUse], str | None]:
        response = self._client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            tools=tools,
            messages=messages,
        )

        serialized: list[dict[str, Any]] = []
        text_parts: list[str] = []
        tool_uses: list[ToolUse] = []

        for block in response.content:
            if block.type == "text":
                serialized.append({"type": "text", "text": block.text})
                text_parts.append(block.text)
            elif block.type == "tool_use":
                serialized.append({
                    "type": "tool_use",
                    "id": block.id,
                    "name": block.name,
                    "input": block.input,
                })
                tool_uses.append((block.id, block.name, block.input))

        stop = "tool_use" if tool_uses else "end_turn"
        return serialized, text_parts, tool_uses, stop

    def format_tool_results(
        self,
        results: list[tuple[str, str, str]],
    ) -> dict[str, Any]:
        """Format tool results as an Anthropic user message.

        results: list of (tool_use_id, tool_name, result_text) tuples.
        tool_name is ignored for Anthropic (uses tool_use_id only).
        """
        return {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": tid, "content": text}
                for tid, _, text in results
            ],
        }

    def convert_conversation(
        self,
        conv: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Strip Gemini-specific fields (thought blocks, thought_signature)."""
        id_to_name: dict[str, str] = {}
        for msg in conv:
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    id_to_name[block.get("id", "")] = block.get("name", "unknown")

        to_remove: list[int] = []
        for idx, msg in enumerate(conv):
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            cleaned: list[dict[str, Any]] = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type", "")
                if btype == "text":
                    if block.get("thought"):
                        continue
                    cleaned.append({"type": "text", "text": block["text"]})
                elif btype == "tool_use":
                    cleaned.append({
                        "type": "tool_use",
                        "id": block["id"],
                        "name": block["name"],
                        "input": block.get("input", {}),
                    })
                elif btype == "tool_result":
                    cleaned.append({
                        "type": "tool_result",
                        "tool_use_id": block.get("tool_use_id", ""),
                        "content": block.get("content", ""),
                    })
                else:
                    cleaned.append(block)
            if cleaned:
                msg["content"] = cleaned
            else:
                to_remove.append(idx)

        for idx in reversed(to_remove):
            conv.pop(idx)
        return conv


# ---------------------------------------------------------------------------
# Gemini provider
# ---------------------------------------------------------------------------

class GeminiProvider(_Provider):
    name = "google"
    default_max_tokens = 65536  # Gemini 3.x max output

    def __init__(self) -> None:
        from google import genai
        self._client = genai.Client(
            http_options={"timeout": 120_000},  # 120s timeout
        )

    def chat(
        self,
        model: str,
        system: str,
        tools: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        max_tokens: int,
    ) -> tuple[list[dict[str, Any]], list[str], list[ToolUse], str | None]:
        from google.genai import types

        gemini_tools = _to_gemini_tools(tools)
        config = types.GenerateContentConfig(
            system_instruction=system,
            tools=gemini_tools,
            max_output_tokens=max_tokens,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(
                disable=True,
            ),
        )
        contents = _to_gemini_contents(messages)

        response = self._client.models.generate_content(
            model=model,
            contents=contents,
            config=config,
        )

        serialized: list[dict[str, Any]] = []
        text_parts: list[str] = []
        tool_uses: list[ToolUse] = []

        has_parts = (response.candidates
                     and response.candidates[0].content
                     and response.candidates[0].content.parts)
        if not has_parts:
            reason = getattr(response.candidates[0], "finish_reason", "unknown") if response.candidates else "no_candidates"
            logger.warning("Gemini returned empty response: %s", reason)
        if has_parts:
            for part in response.candidates[0].content.parts:
                thought_sig = (
                    getattr(part, "thought_signature", None)
                    or getattr(part, "thoughtSignature", None)
                )
                if thought_sig is not None and isinstance(
                    thought_sig, (bytes, bytearray)
                ):
                    thought_sig = thought_sig.hex() or None
                is_thought = getattr(part, "thought", None)

                if part.text is not None:
                    entry: dict[str, Any] = {"type": "text", "text": part.text}
                    if thought_sig:
                        entry["thought_signature"] = thought_sig
                    if is_thought:
                        entry["thought"] = True
                    serialized.append(entry)
                    if not is_thought:
                        text_parts.append(part.text)
                elif part.function_call is not None:
                    fc = part.function_call
                    tool_id = f"gemini_{fc.name}_{id(fc)}"
                    args = dict(fc.args) if fc.args else {}
                    entry = {
                        "type": "tool_use",
                        "id": tool_id,
                        "name": fc.name,
                        "input": args,
                    }
                    if thought_sig:
                        entry["thought_signature"] = thought_sig
                    serialized.append(entry)
                    tool_uses.append((tool_id, fc.name, args))

        stop = "tool_use" if tool_uses else "end_turn"
        return serialized, text_parts, tool_uses, stop

    def format_tool_results(
        self,
        results: list[tuple[str, str, str]],
    ) -> dict[str, Any]:
        """Format tool results as a Gemini-compatible message.

        results: list of (tool_use_id, tool_name, result_text) tuples.
        """
        return {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": tid,
                    "tool_name": name,
                    "content": text,
                }
                for tid, name, text in results
            ],
        }

    def convert_conversation(
        self,
        conv: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Ensure tool_result blocks have tool_name (needed for Gemini)."""
        id_to_name: dict[str, str] = {}
        for msg in conv:
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    id_to_name[block.get("id", "")] = block.get("name", "unknown")

        for msg in conv:
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            cleaned: list[dict[str, Any]] = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_result" and not block.get("tool_name"):
                    new_block = dict(block)
                    tid = block.get("tool_use_id", "")
                    new_block["tool_name"] = id_to_name.get(tid, "unknown")
                    cleaned.append(new_block)
                else:
                    cleaned.append(block)
            msg["content"] = cleaned
        return conv


# ---------------------------------------------------------------------------
# OpenAI Codex provider
# ---------------------------------------------------------------------------

class CodexProvider(_Provider):
    name = "openai-codex"
    default_max_tokens = 32768

    def chat(
        self,
        model: str,
        system: str,
        tools: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        max_tokens: int,
    ) -> tuple[list[dict[str, Any]], list[str], list[ToolUse], str | None]:
        creds = ensure_fresh_codex_credentials()
        token = str(creds.get("access_token"))
        account_id = str(creds.get("account_id"))

        payload = {
            "model": model,
            "store": False,
            "stream": True,
            "instructions": system,
            "input": _to_codex_input(messages),
            "text": {"verbosity": os.environ.get("ONECMD_CODEX_VERBOSITY", "medium")},
            "tool_choice": "auto",
            "parallel_tool_calls": True,
        }
        if tools:
            payload["tools"] = _to_codex_tools(tools)

        req = request.Request(
            os.environ.get("ONECMD_CODEX_BASE_URL", "https://chatgpt.com/backend-api/codex/responses"),
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {token}",
                "chatgpt-account-id": account_id,
                "OpenAI-Beta": "responses=experimental",
                "originator": "onecmd",
                "accept": "text/event-stream",
                "content-type": "application/json",
            },
            method="POST",
        )

        serialized: list[dict[str, Any]] = []
        text_parts: list[str] = []
        stream_deltas: list[str] = []
        tool_uses: list[ToolUse] = []

        def _append_output_item(item: dict[str, Any]) -> None:
            typ = item.get("type")
            if typ == "message":
                for c in item.get("content", []) or []:
                    if c.get("type") in ("output_text", "text"):
                        txt = c.get("text", "")
                        if txt:
                            serialized.append({"type": "text", "text": txt})
                            text_parts.append(txt)
            elif typ in ("function_call", "tool_call"):
                tc_id = item.get("call_id") or item.get("id") or f"codex_{len(tool_uses)+1}"
                name = item.get("name", "unknown")
                args_raw = item.get("arguments")
                if isinstance(args_raw, str):
                    try:
                        args = json.loads(args_raw)
                    except Exception:
                        args = {}
                elif isinstance(args_raw, dict):
                    args = args_raw
                else:
                    args = {}
                serialized.append({"type": "tool_use", "id": tc_id, "name": name, "input": args})
                tool_uses.append((tc_id, name, args))

        try:
            with request.urlopen(req, timeout=120) as resp:
                for raw_line in resp:
                    line = raw_line.decode("utf-8", errors="ignore").strip()
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        evt = json.loads(data)
                    except Exception:
                        continue

                    et = evt.get("type", "")
                    if et == "response.output_text.delta":
                        delta = evt.get("delta", "")
                        if delta:
                            stream_deltas.append(delta)
                    elif et == "response.output_item.done":
                        item = evt.get("item") or {}
                        if isinstance(item, dict):
                            _append_output_item(item)
                    elif et == "response.completed":
                        response_obj = evt.get("response") or {}
                        for item in response_obj.get("output", []) or []:
                            if isinstance(item, dict):
                                _append_output_item(item)
        except error.HTTPError as e:
            msg = e.read().decode("utf-8", errors="ignore")
            raise RuntimeError(f"Codex API error ({e.code}): {msg}") from e
        except CodexAuthError as e:
            raise RuntimeError(f"Codex auth error: {e}") from e
        except Exception as e:
            raise RuntimeError(f"Codex API request failed: {e}") from e

        # If stream only delivered deltas, preserve them as a single text block.
        if not serialized and stream_deltas:
            merged = "".join(stream_deltas)
            serialized.append({"type": "text", "text": merged})
            text_parts.append(merged)

        stop = "tool_use" if tool_uses else "end_turn"
        return serialized, text_parts, tool_uses, stop

    def format_tool_results(
        self,
        results: list[tuple[str, str, str]],
    ) -> dict[str, Any]:
        content: list[dict[str, Any]] = []
        for tid, name, text in results:
            content.append({
                "type": "tool_result",
                "tool_use_id": tid,
                "tool_name": name,
                "content": text,
            })
        return {"role": "user", "content": content}


# ---------------------------------------------------------------------------
# Gemini format helpers
# ---------------------------------------------------------------------------

def _to_gemini_tools(tools: list[dict[str, Any]]) -> list[Any]:
    """Convert Anthropic-style tool defs to Gemini function_declarations."""
    from google.genai import types

    declarations = []
    for tool in tools:
        schema = tool.get("input_schema", {})
        declarations.append({
            "name": tool["name"],
            "description": tool.get("description", ""),
            "parameters": schema,
        })
    return [types.Tool(function_declarations=declarations)]


def _to_gemini_contents(messages: list[dict[str, Any]]) -> list[Any]:
    """Convert Anthropic-style messages to Gemini contents format."""
    from google.genai import types

    contents: list[Any] = []
    for msg in messages:
        role = msg["role"]
        gemini_role = "model" if role == "assistant" else "user"
        raw_content = msg.get("content", "")

        if isinstance(raw_content, str):
            contents.append(
                types.Content(
                    role=gemini_role,
                    parts=[types.Part.from_text(text=raw_content)],
                )
            )
        elif isinstance(raw_content, list):
            parts: list[Any] = []
            for block in raw_content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type", "")
                if btype == "text":
                    part = types.Part(text=block["text"])
                    if block.get("thought"):
                        part.thought = True
                    if block.get("thought_signature"):
                        sig = block["thought_signature"]
                        if isinstance(sig, str):
                            sig = bytes.fromhex(sig)
                        part.thought_signature = sig
                    parts.append(part)
                elif btype == "tool_use":
                    part = types.Part.from_function_call(
                        name=block["name"],
                        args=block.get("input", {}),
                    )
                    if block.get("thought_signature"):
                        sig = block["thought_signature"]
                        if isinstance(sig, str):
                            sig = bytes.fromhex(sig)
                        part.thought_signature = sig
                    parts.append(part)
                elif btype == "tool_result":
                    result_text = block.get("content", "")
                    tool_name = block.get("tool_name") or _find_tool_name(
                        contents, block.get("tool_use_id", "")
                    )
                    parts.append(
                        types.Part.from_function_response(
                            name=tool_name,
                            response={"result": result_text},
                        )
                    )
            if parts:
                contents.append(types.Content(role=gemini_role, parts=parts))
    return contents


def _find_tool_name(contents: list[Any], tool_use_id: str) -> str:
    """Find the tool name for a given tool_use_id by searching previous contents."""
    for content in reversed(contents):
        if content.role == "model":
            for part in content.parts:
                if part.function_call is not None:
                    if tool_use_id.startswith(f"gemini_{part.function_call.name}_"):
                        return part.function_call.name
    return "unknown"


def _to_codex_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for t in tools:
        out.append(
            {
                "type": "function",
                "name": t["name"],
                "description": t.get("description", ""),
                "parameters": t.get("input_schema", {}),
            }
        )
    return out


def _to_codex_input(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []

    def _text_type_for_role(role: str) -> str:
        # Codex responses API expects assistant history as output_text,
        # user history as input_text.
        return "output_text" if role == "assistant" else "input_text"

    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        text_type = _text_type_for_role(role)

        if isinstance(content, str):
            out.append(
                {
                    "role": role,
                    "content": [{"type": text_type, "text": content}],
                }
            )
            continue

        if isinstance(content, list):
            parts: list[dict[str, Any]] = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "text":
                    parts.append({"type": text_type, "text": block.get("text", "")})
                elif btype == "tool_result":
                    # tool_result entries are user-originated follow-up context
                    parts.append(
                        {
                            "type": "input_text",
                            "text": block.get("content", ""),
                        }
                    )
            if parts:
                out.append({"role": role, "content": parts})
    return out


# ---------------------------------------------------------------------------
# Provider registry + detection + fallback
# ---------------------------------------------------------------------------

PROVIDERS: dict[str, type[_Provider]] = {
    "anthropic": AnthropicProvider,
    "google": GeminiProvider,
    "openai-codex": CodexProvider,
}

_RATE_LIMIT_COOLDOWN = 300  # 5 minutes


def detect_provider() -> str | None:
    """Return preferred provider key or None.

    Priority:
      1) ONECMD_MGR_PROVIDER explicit override
      2) GOOGLE_API_KEY
      3) ANTHROPIC_API_KEY
      4) Codex credentials (OPENAI_CODEX_TOKEN or auth.json)
    """
    forced = (os.environ.get("ONECMD_MGR_PROVIDER") or "").strip().lower()
    if forced:
        aliases = {"codex": "openai-codex", "openai_codex": "openai-codex"}
        forced = aliases.get(forced, forced)
        if forced in PROVIDERS:
            return forced

    has_google = bool(os.environ.get("GOOGLE_API_KEY"))
    has_anthropic = bool(os.environ.get("ANTHROPIC_API_KEY"))
    if has_google:
        return "google"
    if has_anthropic:
        return "anthropic"
    if has_codex_credentials():
        return "openai-codex"
    return None


class ProviderManager:
    """Manages active provider with automatic fallback on rate limit."""

    def __init__(self, primary: str | None = None) -> None:
        self._primary_key = primary or detect_provider()
        if not self._primary_key:
            raise RuntimeError(
                "No LLM provider credentials found. Set GOOGLE_API_KEY or "
                "ANTHROPIC_API_KEY, or configure Codex auth in ~/.onecmd/auth.json."
            )
        self._providers: dict[str, _Provider] = {}
        self._active_key: str = self._primary_key
        self._cooldown_until: float = 0.0
        self._fallback_key: str | None = self._detect_fallback()

    def _detect_fallback(self) -> str | None:
        """Return the secondary provider key, if available."""
        for key in PROVIDERS:
            if key == self._primary_key:
                continue
            if key == "google" and os.environ.get("GOOGLE_API_KEY"):
                return key
            if key == "anthropic" and os.environ.get("ANTHROPIC_API_KEY"):
                return key
            if key == "openai-codex" and has_codex_credentials():
                return key
        return None

    @property
    def active(self) -> _Provider:
        """Return the active provider instance (lazy-initialized)."""
        if time.time() > self._cooldown_until and self._active_key != self._primary_key:
            logger.info("Cooldown expired, switching back to %s", self._primary_key)
            self._active_key = self._primary_key
        if self._active_key not in self._providers:
            logger.info("Initializing %s provider", self._active_key)
            self._providers[self._active_key] = PROVIDERS[self._active_key]()
        return self._providers[self._active_key]

    @property
    def active_name(self) -> str:
        return self._active_key

    def switch_on_rate_limit(self) -> bool:
        """Switch to fallback provider with cooldown. Returns True if fallback available."""
        return self._switch_to_fallback(
            reason="rate limited",
            cooldown=_RATE_LIMIT_COOLDOWN,
        )

    def switch_on_error(self) -> bool:
        """Switch to fallback provider temporarily (no cooldown). Returns True if fallback available."""
        return self._switch_to_fallback(
            reason="API error",
            cooldown=60,  # Short cooldown — try primary again soon
        )

    def _switch_to_fallback(self, reason: str, cooldown: int) -> bool:
        if not self._fallback_key:
            logger.warning("%s on %s but no fallback provider available",
                           reason, self._active_key)
            return False
        logger.warning(
            "%s on %s, switching to %s for %ds",
            reason, self._active_key, self._fallback_key, cooldown,
        )
        self._active_key = self._fallback_key
        self._cooldown_until = time.time() + cooldown
        if self._active_key not in self._providers:
            self._providers[self._active_key] = PROVIDERS[self._active_key]()
        return True
