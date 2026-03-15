"""Command dispatch and auth gate for the Telegram bot.

Calling spec:
  Inputs:  Config, Store, ValidatedBackend (pre-scoped)
  Outputs: async handler callback for python-telegram-bot v21
  Side effects: sends Telegram messages, modifies connection state,
                reads/writes aliases.json, reads/writes store

Factory:
  create_handler(config, store, backend) -> async handler(Update, Context)

Dispatch:  COMMANDS dict maps ".cmd" -> handler function
Flow:  owner check -> TOTP gate -> callback -> commands -> .N -> mgr -> keys -> list
Guarding:  auth first, non-owner silent drop, all state under threading.Lock
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from telegram import Update
from telegram.ext import ContextTypes

from onecmd.auth.owner import check_owner
from onecmd.auth.totp import STORE_KEY as TOTP_SECRET_KEY
from onecmd.auth.totp import is_timed_out, totp_verify
from onecmd.bot.api import answer_callback, html_escape, send_chat_action, send_message
from onecmd.emoji import parse as emoji_parse
from onecmd.manager.router import ManagerRouter
from onecmd.texts import HELP_TEXT, build_welcome_message
from onecmd.terminal.display import (
    TrackedMessages,
    delete_tracked_messages,
    send_terminal_display,
)

if TYPE_CHECKING:
    from telegram import Bot
    from onecmd.config import Config
    from onecmd.store import Store
    from onecmd.terminal.backend import ValidatedBackend
    from onecmd.terminal.backend import TermInfo

logger = logging.getLogger(__name__)

ALIASES_PATH = ".onecmd/aliases.json"
DOT_N_RE = re.compile(r"^\.(\d+)$")
REFRESH_CALLBACK = "refresh"
_START_TIME = time.time()

# -- State -----------------------------------------------------------------

@dataclass
class _State:
    connected: bool = False
    term_id: str = ""
    term_name: str = ""
    term_title: str = ""
    mgr_mode: bool = False
    authenticated: bool = False
    last_auth_time: float = 0.0
    tracked_msgs: TrackedMessages = field(default_factory=TrackedMessages)


def _disconnect(s: _State) -> None:
    s.connected = False
    s.term_id = s.term_name = s.term_title = ""


# -- Aliases ---------------------------------------------------------------

def _load_aliases() -> dict[str, str]:
    try:
        return json.loads(Path(ALIASES_PATH).read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _save_alias(term_id: str, name: str) -> None:
    os.makedirs(".onecmd", exist_ok=True)
    aliases = _load_aliases()
    aliases[term_id] = name
    Path(ALIASES_PATH).write_text(json.dumps(aliases, indent=2))


# -- Helpers ---------------------------------------------------------------

def _build_list_text(terminals: list[TermInfo], connected_id: str = "") -> str:
    if not terminals:
        import sys
        if sys.platform != "darwin":
            return (
                "No terminal sessions found.\n\n"
                "On Linux, onecmd controls tmux sessions.\n"
                "Start one with: <code>tmux new -s dev</code>"
            )
        return "No terminal sessions found."
    lines = [f"<b>Terminals</b> ({len(terminals)})"]
    aliases = _load_aliases()
    for i, t in enumerate(terminals, 1):
        alias = aliases.get(t.id)
        n = html_escape(t.name)
        ti = html_escape(t.title) if t.title else ""
        marker = " \u25c0" if t.id == connected_id else ""
        if alias:
            a = html_escape(alias)
            label = f"<code>.{i}</code> [{a}] {n} — {ti}" if ti else f"<code>.{i}</code> [{a}] {n}"
        else:
            label = f"<code>.{i}</code> {n} — {ti}" if ti else f"<code>.{i}</code> {n}"
        lines.append(label + marker)
    return "\n".join(lines)


async def _show_terminal(bot, chat_id, term_id, s, backend, config):
    captured = backend.capture(term_id)
    if captured:
        await send_terminal_display(
            bot, chat_id, captured, s.tracked_msgs,
            config.split_messages, config.visible_lines,
        )


async def _show_closed(bot, chat_id, s, backend):
    _disconnect(s)
    terminals = backend.list()
    await send_message(bot, chat_id, "Window closed.\n\n" + _build_list_text(terminals, ""))


# -- Keystroke sending -----------------------------------------------------

def _action_to_raw(action) -> str:
    """Convert a KeyAction to raw character(s) for literal-mode send_keys.

    Encodes modifiers as control codes:
      Ctrl+<letter> -> chr(1)..chr(26)
      Alt+<char>    -> ESC + char
      Enter/Tab/Escape -> \\n / \\t / \\x1b
    Cmd modifier cannot be represented in a raw byte stream and is dropped
    (handled separately via _send_keystrokes).
    """
    if action.kind == "key":
        base = {"Enter": "\n", "Tab": "\t", "Escape": "\x1b"}.get(
            action.value, action.value
        )
        if action.ctrl and len(base) == 1 and base.isalpha():
            base = chr(ord(base.upper()) - 64)
        if action.alt and len(base) == 1:
            return f"\x1b{base}"
        return base
    ch = action.value
    if action.ctrl and ch.isalpha():
        ch = chr(ord(ch.upper()) - 64)
    if action.alt:
        return f"\x1b{ch}"
    return ch


def _flush_literal(backend, term_id: str, buf: list[str]) -> None:
    """Send accumulated literal characters in one call, then clear the buffer."""
    if buf:
        backend.send_keys(term_id, "".join(buf), literal=True)
        buf.clear()


def _send_keystrokes(backend, term_id: str, text: str) -> None:
    """Parse emoji-encoded text into KeyActions and send to backend.

    Plain characters are batched and sent as literal text for efficiency.
    Keys with modifiers (Ctrl, Alt, Cmd) or special keys (Enter, Tab,
    Escape) flush the buffer and are sent individually so both tmux
    (literal mode passes raw bytes) and macOS (CGEvent interprets control
    codes) handle them correctly.
    """
    actions, suppress_newline = emoji_parse(text)
    if not suppress_newline:
        from onecmd.emoji import KeyAction as _KA
        actions.append(_KA("key", "Enter"))

    buf: list[str] = []
    for action in actions:
        has_modifier = action.ctrl or action.alt or action.cmd
        is_special = action.kind == "key"
        if not has_modifier and not is_special:
            # Plain character — accumulate for batch send
            buf.append(action.value)
        else:
            _flush_literal(backend, term_id, buf)
            raw = _action_to_raw(action)
            backend.send_keys(term_id, raw, literal=True)
    _flush_literal(backend, term_id, buf)


# -- Command type alias ----------------------------------------------------

CmdFn = Callable  # async (bot, chat_id, text, state, backend, store, config) -> None


# -- Command handlers ------------------------------------------------------

async def _cmd_list(bot, chat_id, _text, s, backend, _store, _config):
    await delete_tracked_messages(bot, chat_id, s.tracked_msgs)
    _disconnect(s)
    await send_message(bot, chat_id, _build_list_text(backend.list()))


async def _cmd_mgr(bot, chat_id, _text, s, _backend, _store, _config, router=None):
    if not s.mgr_mode:
        msg = router.activate() if router else "Manager not available."
        if not router or not router.active:
            await send_message(bot, chat_id, msg)
            return
        s.mgr_mode = True
        await send_message(bot, chat_id,
                     "\U0001f916 Manager mode on.\nDot commands still work: .list .1 .exit")
    else:
        s.mgr_mode = False
        if router:
            router.deactivate()
        await send_message(bot, chat_id, "Manager mode off.")


async def _cmd_debug(bot, chat_id, _text, s, _backend, _store, _config, router=None):
    if router:
        router.debug = not router.debug
        state = "ON" if router.debug else "OFF"
        await send_message(bot, chat_id, f"Debug mode {state}.")
    else:
        await send_message(bot, chat_id, "Manager not available.")


async def _cmd_ceo(bot, chat_id, _text, s, _backend, _store, _config, router=None):
    if not s.mgr_mode or not getattr(s, '_ceo_mode', False):
        msg = router.activate_ceo() if router else "CEO not available."
        if not router or not router.ceo_active:
            await send_message(bot, chat_id, msg)
            return
        s.mgr_mode = True
        s._ceo_mode = True
        await send_message(bot, chat_id,
                     "\U0001f3e2 CEO mode on.\n"
                     "Describe what you want to build.\n"
                     "Dot commands still work: .list .1 .exit")
    else:
        s.mgr_mode = False
        s._ceo_mode = False
        if router:
            router.deactivate_ceo()
        await send_message(bot, chat_id, "CEO mode off.")


async def _cmd_exit(bot, chat_id, _text, s, _backend, _store, _config, router=None):
    if getattr(s, '_ceo_mode', False):
        s._ceo_mode = False
        s.mgr_mode = False
        if router:
            router.deactivate_ceo()
        await send_message(bot, chat_id, "CEO mode off.")
    elif s.mgr_mode:
        s.mgr_mode = False
        if router:
            router.deactivate()
        await send_message(bot, chat_id, "Manager mode off.")
    else:
        await send_message(bot, chat_id, "Not in manager or CEO mode.")


async def _cmd_help(bot, chat_id, _text, _s, _backend, _store, _config):
    await send_message(bot, chat_id, HELP_TEXT)


async def _cmd_health(bot, chat_id, _text, s, backend, _store, config, router=None):
    up = int(time.time() - _START_TIME)
    h, r = divmod(up, 3600)
    m, sec = divmod(r, 60)
    conn = f"{html_escape(s.term_name)} ({s.term_id})" if s.connected else "None"
    # Detect provider info
    llm_status = "not configured"
    if config.has_llm_key:
        try:
            from onecmd.manager.llm import detect_provider
            provider = detect_provider()
            _labels = {
                "anthropic": "Anthropic (API key)",
                "anthropic-oauth": "Claude (OAuth)",
                "google": "Gemini (API key)",
                "openai-codex": "Codex (OAuth)",
            }
            llm_status = _labels.get(provider, provider or "configured")
        except Exception:
            llm_status = "configured"

    lines = [
        "<b>Health Report</b>",
        f"Uptime: {h}h {m}m {sec}s",
        f"Connected: {conn}",
        f"Manager: {'CEO' if getattr(s, '_ceo_mode', False) else 'on' if s.mgr_mode else 'off'}",
        f"LLM: {llm_status}",
        f"Terminals: {len(backend.list())}",
    ]
    if router:
        rh = router.health()
        lines.append(f"Router: active={rh.get('active')}, agent={rh.get('agent_initialized')}")
    await send_message(bot, chat_id, "\n".join(lines))


async def _cmd_new(bot, chat_id, _text, s, backend, _store, _config):
    result = backend.create()
    if result is None:
        await send_message(bot, chat_id, "Failed to create terminal.")
        return
    await asyncio.sleep(1.0)
    terminals = backend.list()
    await send_message(bot, chat_id,
        "\u2705 Terminal created.\n\n" + _build_list_text(terminals))


async def _cmd_rename(bot, chat_id, text, _s, backend, _store, _config):
    parts = text.split(None, 2)
    if len(parts) < 3:
        await send_message(bot, chat_id, "Usage: .rename N name")
        return
    try:
        n = int(parts[1])
    except ValueError:
        await send_message(bot, chat_id, "Usage: .rename N name")
        return
    name = parts[2].strip()
    if not name:
        await send_message(bot, chat_id, "Usage: .rename N name")
        return
    terminals = backend.list()
    if n < 1 or n > len(terminals):
        await send_message(bot, chat_id, "Invalid window number.")
        return
    _save_alias(terminals[n - 1].id, name)
    await send_message(bot, chat_id, f"Terminal {n} renamed to [{html_escape(name)}].")


async def _cmd_otptimeout(bot, chat_id, text, _s, _backend, store, config):
    parts = text.split(None, 1)
    arg = parts[1].strip() if len(parts) > 1 else ""
    try:
        secs = int(arg)
    except ValueError:
        secs = config.otp_timeout
    secs = max(30, min(28800, secs))
    store.set("otp_timeout", str(secs))
    await send_message(bot, chat_id, f"OTP timeout set to {secs} seconds.")


_PROVIDERS = {
    "gemini": ("google", "GOOGLE_API_KEY", "ai.google.dev"),
    "claude": ("anthropic", "ANTHROPIC_API_KEY", "console.anthropic.com"),
    "codex": ("openai-codex", None, None),
}


def _save_env_key(var: str, value: str) -> None:
    """Set env var at runtime and persist to .env."""
    os.environ[var] = value
    try:
        env_file = Path(".env")
        content = env_file.read_text() if env_file.exists() else ""
        import re as _re
        if _re.search(rf'^{var}=', content, _re.MULTILINE):
            content = _re.sub(rf'^{var}=.*$', f'{var}={value}', content, flags=_re.MULTILINE)
        else:
            content = content.rstrip() + f'\n{var}={value}\n'
        env_file.write_text(content)
        os.chmod(str(env_file), 0o600)
    except Exception as e:
        logger.warning("Failed to persist %s to .env: %s", var, e)


async def _cmd_model(bot, chat_id, text, _s, _backend, _store, _config, router=None):
    if not router:
        await send_message(bot, chat_id, "Manager not available.")
        return

    args = text.split(None, 2)  # .model [name] [key]
    name = args[1].lower() if len(args) > 1 else ""
    key = args[2].strip() if len(args) > 2 else ""

    # .model — show status
    if not name:
        info = router.get_model_info()
        from onecmd.auth.codex import has_codex_credentials
        lines = [f"Current: <code>{html_escape(info)}</code>", ""]
        for label, (_, env_var, _) in _PROVIDERS.items():
            if env_var:
                ready = bool(os.environ.get(env_var))
            else:
                ready = has_codex_credentials()
            mark = "\u2705" if ready else "\u274c"
            lines.append(f"{mark} <code>.model {label}</code>")
        await send_message(bot, chat_id, "\n".join(lines))
        return

    provider_info = _PROVIDERS.get(name)
    if not provider_info:
        await send_message(bot, chat_id,
            f"Unknown: {html_escape(name)}\nUse: gemini, claude, or codex")
        return

    provider_key, env_var, signup_url = provider_info

    # .model claude sk-ant-xxx — save key and switch
    if key and env_var:
        _save_env_key(env_var, key)

    # Check credentials
    if env_var:
        has_creds = bool(os.environ.get(env_var))
    else:
        from onecmd.auth.codex import has_codex_credentials
        has_creds = has_codex_credentials()

    if not has_creds:
        if env_var:
            await send_message(bot, chat_id,
                f"Paste your key:\n<code>.model {html_escape(name)} YOUR_KEY</code>"
                f"\n\nGet one at {signup_url}")
        else:
            await send_message(bot, chat_id,
                "Run <code>codex</code> on the server to login,"
                " then <code>./setup.sh</code> to import")
        return

    try:
        result = router.set_model(provider_key, None)
        await send_message(bot, chat_id, f"Switched: <code>{html_escape(result)}</code>")
    except Exception as e:
        await send_message(bot, chat_id, f"Error: {html_escape(str(e))}")


COMMANDS: dict[str, CmdFn] = {
    ".list": _cmd_list,
    ".new": _cmd_new,
    ".mgr": _cmd_mgr,
    ".ceo": _cmd_ceo,
    ".debug": _cmd_debug,
    ".exit": _cmd_exit,
    ".help": _cmd_help,
    ".health": _cmd_health,
    ".rename": _cmd_rename,
    ".model": _cmd_model,
    ".otptimeout": _cmd_otptimeout,
}


# -- Factory ---------------------------------------------------------------

def create_handler(config: Config, store: Store, backend: ValidatedBackend):
    """Return an async handler callback for python-telegram-bot v21."""
    s = _State()
    lock = threading.Lock()

    async def _notify_async(chat_id: int, text: str) -> None:
        """Async notify — used when already in the event loop."""
        await send_message(_notify_async._bot, chat_id, text)

    _notify_async._bot = None  # type: ignore[attr-defined]

    def _notify_sync(chat_id: int, text: str) -> None:
        """Sync notify — safe to call from background threads (queue callbacks).

        Schedules the async send_message on the main event loop via
        run_coroutine_threadsafe.  Text is HTML-escaped because callers
        send raw terminal output that can contain <, >, & characters
        which break Telegram's HTML parser.
        """
        bot = _notify_async._bot
        if bot is None:
            logger.warning("_notify_sync: bot is None, dropping notification for chat %s", chat_id)
            return
        loop = _notify_sync._loop
        if loop is None or loop.is_closed():
            logger.warning("_notify_sync: event loop unavailable, dropping notification for chat %s", chat_id)
            return
        import asyncio

        async def _send_with_fallback() -> None:
            escaped = html_escape(text)
            result = await send_message(bot, chat_id, escaped)
            if result is None:
                # HTML parse failed — retry as plain text
                result = await send_message(bot, chat_id, text, parse_mode=None)
            if result is None:
                logger.error("_notify_sync: notification delivery failed for chat %s", chat_id)

        fut = asyncio.run_coroutine_threadsafe(_send_with_fallback(), loop)
        fut.add_done_callback(
            lambda f: logger.error("_notify_sync error: %s", f.exception())
            if f.exception() else None)

    _notify_sync._loop = None  # type: ignore[attr-defined]

    router = ManagerRouter(backend, config, _notify_sync)

    _ROUTER_CMDS = {".mgr", ".ceo", ".exit", ".health", ".debug", ".model"}

    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        bot = context.bot
        _notify_async._bot = bot  # type: ignore[attr-defined]
        if _notify_sync._loop is None:
            _notify_sync._loop = asyncio.get_running_loop()  # type: ignore[attr-defined]
        is_cb = update.callback_query is not None

        if is_cb:
            user = update.callback_query.from_user
            chat_id = (update.callback_query.message.chat_id
                       if update.callback_query.message else None)
            text = ""
        else:
            if update.effective_message is None:
                return
            user = update.effective_user
            chat_id = update.effective_chat.id if update.effective_chat else None
            text = update.effective_message.text or ""

        if user is None or chat_id is None:
            return

        with lock:
            # 1. Owner check
            is_owner, just_reg = check_owner(store, user.id)
            if just_reg:
                logger.info("Registered owner: %d (%s)", user.id, user.username or "")

        if just_reg:
            terminals = backend.list()
            welcome = build_welcome_message(
                len(terminals), _build_list_text(terminals))
            await send_message(bot, chat_id, welcome)
            # Fall through so the first message still executes as a command.
            # Skip if it's /start or plain text (welcome already shows the list).
            if text.startswith("/start") or not text.startswith("."):
                return
        if not is_owner:
            return

        # Handle /start for existing owner — show terminal list
        if text.startswith("/start"):
            terminals = backend.list()
            await send_message(bot, chat_id,
                "\U0001f4bb <b>OneCmd</b>\n\n" + _build_list_text(terminals))
            return

        # 2. TOTP auth -- HARD GATE
        if not config.weak_security:
            with lock:
                secret_hex = store.get(TOTP_SECRET_KEY)
                if secret_hex:
                    timeout_str = store.get("otp_timeout")
                    otp_timeout = config.otp_timeout
                    if timeout_str:
                        try:
                            otp_timeout = int(timeout_str)
                        except ValueError:
                            pass
                    if not s.authenticated or is_timed_out(s.last_auth_time, otp_timeout):
                        s.authenticated = False
                        if is_cb:
                            await answer_callback(bot, update.callback_query.id)
                            return
                        if len(text) == 6 and text.isdigit() and totp_verify(text, secret_hex):
                            s.authenticated = True
                            s.last_auth_time = time.time()
                            await send_message(bot, chat_id, "Authenticated.")
                        else:
                            await send_message(bot, chat_id, "Enter OTP code.")
                        return
                    s.last_auth_time = time.time()

        # 3. Callback queries (refresh button)
        if is_cb:
            await answer_callback(bot, update.callback_query.id)
            with lock:
                if (update.callback_query.data or "") == REFRESH_CALLBACK and s.connected:
                    if backend.connected(s.term_id):
                        await _show_terminal(bot, chat_id, s.term_id, s, backend, config)
                    else:
                        await _show_closed(bot, chat_id, s, backend)
            return

        # 4. Dot-commands via COMMANDS dict
        cmd_key = text.lower().split()[0] if text.strip() else ""
        cmd_fn = COMMANDS.get(cmd_key)
        if cmd_fn is not None:
            if cmd_key in _ROUTER_CMDS:
                await cmd_fn(bot, chat_id, text, s, backend, store, config, router=router)
            else:
                await cmd_fn(bot, chat_id, text, s, backend, store, config)
            return

        # 5. .N pattern (connect to terminal)
        m = DOT_N_RE.match(text.strip())
        if m:
            n = int(m.group(1))
            terminals = backend.list()
            if n < 1 or n > len(terminals):
                await send_message(bot, chat_id, "Invalid window number.")
                return
            with lock:
                was_mgr = s.mgr_mode
                if s.mgr_mode:
                    s.mgr_mode = False
                    router.deactivate()
            t = terminals[n - 1]
            with lock:
                s.connected, s.term_id = True, t.id
                s.term_name, s.term_title = t.name, t.title
            alias = _load_aliases().get(t.id)
            name = f"[{html_escape(alias)}] " if alias else ""
            title_part = f" — {html_escape(t.title)}" if t.title else ""
            msg = f"\U0001f5a5 Connected to {name}{html_escape(t.name)}{title_part}"
            if was_mgr:
                msg += "\nManager mode off."
            msg += "\n\nType to send keystrokes. <code>.list</code> to disconnect."
            await send_message(bot, chat_id, msg)
            await _show_terminal(bot, chat_id, t.id, s, backend, config)
            return

        # 6. CEO / Manager mode — route to LLM agent
        if s.mgr_mode and not text.startswith("."):
            if not text.strip():
                return
            await send_chat_action(bot, chat_id)
            ceo = getattr(s, '_ceo_mode', False)
            handle_fn = router.handle_ceo if ceo else router.handle
            loop = asyncio.get_event_loop()
            task = asyncio.ensure_future(
                loop.run_in_executor(None, handle_fn, chat_id, text))
            while not task.done():
                await asyncio.sleep(4.0)
                if not task.done():
                    await send_chat_action(bot, chat_id)
            response = task.result()
            if response:
                await send_message(bot, chat_id, response)
            return

        # 7. Connected mode: send keystrokes
        need_display = False
        with lock:
            if s.connected:
                if not backend.connected(s.term_id):
                    pass  # handled below
                else:
                    try:
                        _send_keystrokes(backend, s.term_id, text)
                        need_display = True
                    except (ValueError, RuntimeError) as exc:
                        logger.warning("send_keys failed: %s", exc)
                        await send_message(bot, chat_id, f"Error: {exc}")
                        return

        if s.connected and not backend.connected(s.term_id):
            await _show_closed(bot, chat_id, s, backend)
            return

        # 8. Default: show terminal list
        if not need_display:
            await send_message(bot, chat_id, _build_list_text(backend.list()))
            return

        # Post-keystroke: sleep then show terminal output
        await asyncio.sleep(0.5)
        with lock:
            if s.connected:
                if backend.connected(s.term_id):
                    await _show_terminal(bot, chat_id, s.term_id, s, backend, config)
                else:
                    await _show_closed(bot, chat_id, s, backend)

    return handler
