"""
onecmd mgr — Shared utilities and constants.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

CTL_PATH: str = os.environ.get("ONECMD_CTL", "./onecmd-ctl")

# Provider detection: prefer Gemini, fall back to Anthropic
_anthropic_key: str = os.environ.get("ANTHROPIC_API_KEY", "")
_google_key: str = os.environ.get("GOOGLE_API_KEY", "")

_custom_model: str = os.environ.get("ONECMD_MGR_MODEL", "")

# Build list of available providers
_providers: dict[str, dict[str, str]] = {}
if _google_key:
    _providers["gemini"] = {
        "key": _google_key,
        "model": _custom_model or "gemini-3-flash-preview",
    }
if _anthropic_key:
    _providers["anthropic"] = {
        "key": _anthropic_key,
        "model": _custom_model or "claude-opus-4-6",
    }

# Active provider state (mutable for fallback)
_provider_lock = threading.Lock()
_preferred: str = "gemini" if "gemini" in _providers else "anthropic"

PROVIDER: str = _preferred if _preferred in _providers else next(iter(_providers), "")
API_KEY: str = _providers[PROVIDER]["key"] if PROVIDER else ""
MODEL: str = _providers[PROVIDER]["model"] if PROVIDER else ""

# Fallback state
_fallback_active: bool = False
_fallback_until: float = 0  # timestamp when to retry preferred provider
_FALLBACK_RETRY_INTERVAL: int = 300  # check if preferred is back every 5 min

_clients: dict[str, object] = {}


def _create_client_for(provider: str) -> object:
    """Create a client for a specific provider."""
    if provider in _clients:
        return _clients[provider]
    info = _providers[provider]
    if provider == "anthropic":
        import anthropic
        _clients[provider] = anthropic.Anthropic(api_key=info["key"])
    elif provider == "gemini":
        from google import genai
        _clients[provider] = genai.Client(api_key=info["key"])
    return _clients[provider]


def create_client() -> object:
    """Create and return the LLM client for the active provider."""
    if not PROVIDER:
        raise RuntimeError(
            "No LLM API key found. Set ANTHROPIC_API_KEY or GOOGLE_API_KEY."
        )
    return _create_client_for(PROVIDER)


def is_rate_limit_error(e: Exception) -> bool:
    """Check if an exception is a rate limit / quota exceeded error."""
    msg = str(e).lower()
    if "429" in msg or "rate" in msg or "quota" in msg or "limit" in msg:
        return True
    # Google specific
    if "resource_exhausted" in msg or "resourceexhausted" in msg or "resource exhausted" in msg:
        return True
    # Anthropic specific
    etype = type(e).__name__
    if "RateLimitError" in etype or ("APIStatusError" in etype and "429" in msg):
        return True
    return False


def switch_to_fallback() -> bool:
    """Switch to fallback provider. Returns True if switched, False if no fallback."""
    global PROVIDER, API_KEY, MODEL, _fallback_active, _fallback_until
    with _provider_lock:
        if _fallback_active:
            return False  # Already on fallback
        fallback = "anthropic" if _preferred == "gemini" else "gemini"
        if fallback not in _providers:
            return False
        logger.warning("Switching from %s to %s (rate limited)", PROVIDER, fallback)
        PROVIDER = fallback
        API_KEY = _providers[fallback]["key"]
        MODEL = _providers[fallback]["model"]
        _fallback_active = True
        _fallback_until = time.time() + _FALLBACK_RETRY_INTERVAL
        return True


def maybe_switch_back() -> None:
    """If on fallback, check if it's time to try the preferred provider again."""
    global PROVIDER, API_KEY, MODEL, _fallback_active, _fallback_until
    with _provider_lock:
        if not _fallback_active:
            return
        if time.time() < _fallback_until:
            return
        logger.info("Trying to switch back to preferred provider: %s", _preferred)
        PROVIDER = _preferred
        API_KEY = _providers[_preferred]["key"]
        MODEL = _providers[_preferred]["model"]
        _fallback_active = False


def on_provider_success() -> None:
    """Called after a successful API call. Resets fallback retry timer."""
    global _fallback_until
    if _fallback_active:
        # Still on fallback, keep checking
        pass


def get_active_provider() -> str:
    """Return the name of the currently active provider."""
    with _provider_lock:
        return PROVIDER


def get_active_provider_and_model() -> tuple[str, str]:
    """Return (provider_name, model) atomically."""
    with _provider_lock:
        return PROVIDER, MODEL

MAX_CONVERSATION_TURNS: int = 30   # sliding window (hard cap, safety net)
MAX_TOOL_ROUNDS: int = 15          # max tool-use rounds per request
SUMMARIZE_THRESHOLD: int = 20      # trigger summarization at this many turns (40 msgs)
SUMMARIZE_KEEP_TURNS: int = 10     # keep this many recent turns unsummarized
SUMMARY_MAX_CHARS: int = 2000      # cap summary at ~500 tokens
MAX_TASK_ITERATIONS: int = 100
MAX_TASK_TIMEOUT: int = 3600       # 1 hour
DEFAULT_POLL_INTERVAL: int = 10    # seconds

# Smart task (LLM-judged background tasks)
SMART_TASK_MODEL: str = os.environ.get("ONECMD_SMART_TASK_MODEL", MODEL)
SMART_TASK_HISTORY_LIMIT: int = 10  # max conversation exchanges to keep

# Monitoring
MONITOR_INTERVAL: int = 3600       # health log every hour
MEMORY_LIMIT_MB: int = 500         # auto-restart if RSS exceeds this
TASK_PRUNE_AGE: int = 3600         # prune completed tasks older than 1 hour

# Output stability detection
DEFAULT_STABLE_SECONDS: int = 5    # output must be unchanged for this long
STABILITY_POLL: int = 2            # how often to re-capture while waiting
MAX_STABILITY_WAIT: int = 300      # max seconds to wait for stability (5 min)

# ---------------------------------------------------------------------------
# Output: send JSON lines to onecmd (stdout)
# ---------------------------------------------------------------------------

_output_lock: threading.Lock = threading.Lock()

def send_response(chat_id: int, text: str) -> None:
    """Send a message back to onecmd for delivery to Telegram."""
    if chat_id == 0:
        return  # Drop messages with no target
    msg = json.dumps({"chat_id": chat_id, "text": text}, ensure_ascii=False)
    with _output_lock:
        print(msg, flush=True)

# ---------------------------------------------------------------------------
# Shared ID counter for watchers and tasks
# ---------------------------------------------------------------------------

_task_id_counter: int = 0
_task_id_lock: threading.Lock = threading.Lock()

def _next_task_id() -> int:
    global _task_id_counter
    with _task_id_lock:
        _task_id_counter += 1
        return _task_id_counter

# ---------------------------------------------------------------------------
# Terminal operations via onecmd-ctl
# ---------------------------------------------------------------------------

def ctl_run(args: list[str]) -> tuple[str, str, int]:
    """Run onecmd-ctl with given args, return (stdout, stderr, returncode)."""
    try:
        result = subprocess.run(
            [CTL_PATH] + args,
            capture_output=True, timeout=15
        )
        # Decode with replace to handle binary/invalid UTF-8 in terminal output
        stdout = result.stdout.decode("utf-8", errors="replace").strip()
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        return stdout, stderr, result.returncode
    except FileNotFoundError:
        logger.error("onecmd-ctl not found at %s", CTL_PATH)
        return "", f"onecmd-ctl not found at {CTL_PATH}", 1
    except subprocess.TimeoutExpired:
        logger.warning("onecmd-ctl timed out for args: %s", args)
        return "", "onecmd-ctl timed out", 1
