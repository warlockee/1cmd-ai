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
from onecmd.bot.api import answer_callback, html_escape, send_message
from onecmd.emoji import parse as emoji_parse
from onecmd.manager.router import ManagerRouter
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

HELP_TEXT = (
    "Commands:\n.list - Show terminal windows\n.1 .2 ... - Connect to window\n"
    ".rename N name - Name a terminal\n.mgr - Toggle AI manager mode\n"
    ".exit - Leave manager mode\n.health - Health report\n.help - This help\n\n"
    "Once connected, text is sent as keystrokes.\n"
    "Newline is auto-added; end with \U0001f49c to suppress it.\n\n"
    "Modifiers:\n<code>\u2764\ufe0f</code> Ctrl  <code>\U0001f499</code> Alt  "
    "<code>\U0001f49a</code> Cmd  <code>\U0001f49b</code> ESC  "
    "<code>\U0001f9e1</code> Enter\n\nEscape sequences: \\n=Enter \\t=Tab"
)


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

def _build_list_text(terminals: list[TermInfo]) -> str:
    if not terminals:
        return "No terminal sessions found."
    lines = ["Terminal windows:"]
    for i, t in enumerate(terminals, 1):
        alias = _load_aliases().get(t.id)
        n = html_escape(t.name)
        ti = html_escape(t.title) if t.title else ""
        if alias:
            a = html_escape(alias)
            lines.append(f".{i} [{a}] {n} - {ti}" if ti else f".{i} [{a}] {n}")
        else:
            lines.append(f".{i} {n} - {ti}" if ti else f".{i} {n}")
    return "\n".join(lines)


def _show_terminal(bot, chat_id, term_id, s, backend, config):
    captured = backend.capture(term_id)
    if captured:
        send_terminal_display(
            bot, chat_id, captured, s.tracked_msgs,
            config.split_messages, config.visible_lines,
        )


def _show_closed(bot, chat_id, s, backend):
    _disconnect(s)
    terminals = backend.list()
    send_message(bot, chat_id, "Window closed.\n\n" + _build_list_text(terminals))


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

CmdFn = Callable  # (bot, chat_id, text, state, backend, store, config) -> None


# -- Command handlers ------------------------------------------------------

def _cmd_list(bot, chat_id, _text, s, backend, _store, _config):
    _disconnect(s)
    delete_tracked_messages(bot, chat_id, s.tracked_msgs)
    send_message(bot, chat_id, _build_list_text(backend.list()))


def _cmd_mgr(bot, chat_id, _text, s, _backend, _store, _config, router=None):
    if not s.mgr_mode:
        msg = router.activate() if router else "Manager not available."
        if not router or not router.active:
            send_message(bot, chat_id, msg)
            return
        s.mgr_mode = True
        send_message(bot, chat_id,
                     "\U0001f916 Manager mode on.\nDot commands still work: .list .1 .exit")
    else:
        s.mgr_mode = False
        if router:
            router.deactivate()
        send_message(bot, chat_id, "Manager mode off.")


def _cmd_exit(bot, chat_id, _text, s, _backend, _store, _config, router=None):
    if s.mgr_mode:
        s.mgr_mode = False
        if router:
            router.deactivate()
        send_message(bot, chat_id, "Manager mode off.")
    else:
        send_message(bot, chat_id, "Not in manager mode.")


def _cmd_help(bot, chat_id, _text, _s, _backend, _store, _config):
    send_message(bot, chat_id, HELP_TEXT)


def _cmd_health(bot, chat_id, _text, s, backend, _store, config, router=None):
    up = int(time.time() - _START_TIME)
    h, r = divmod(up, 3600)
    m, sec = divmod(r, 60)
    conn = f"{html_escape(s.term_name)} ({s.term_id})" if s.connected else "None"
    lines = [
        "<b>Health Report</b>",
        f"Uptime: {h}h {m}m {sec}s",
        f"Connected: {conn}",
        f"Manager: {'on' if s.mgr_mode else 'off'}",
        f"LLM: {'configured' if config.has_llm_key else 'not configured'}",
        f"Terminals: {len(backend.list())}",
    ]
    if router:
        rh = router.health()
        lines.append(f"Router: active={rh.get('active')}, agent={rh.get('agent_initialized')}")
    send_message(bot, chat_id, "\n".join(lines))


def _cmd_rename(bot, chat_id, text, _s, backend, _store, _config):
    parts = text.split(None, 2)
    if len(parts) < 3:
        send_message(bot, chat_id, "Usage: .rename N name")
        return
    try:
        n = int(parts[1])
    except ValueError:
        send_message(bot, chat_id, "Usage: .rename N name")
        return
    name = parts[2].strip()
    if not name:
        send_message(bot, chat_id, "Usage: .rename N name")
        return
    terminals = backend.list()
    if n < 1 or n > len(terminals):
        send_message(bot, chat_id, "Invalid window number.")
        return
    _save_alias(terminals[n - 1].id, name)
    send_message(bot, chat_id, f"Terminal {n} renamed to [{html_escape(name)}].")


def _cmd_otptimeout(bot, chat_id, text, _s, _backend, store, config):
    parts = text.split(None, 1)
    arg = parts[1].strip() if len(parts) > 1 else ""
    try:
        secs = int(arg)
    except ValueError:
        secs = config.otp_timeout
    secs = max(30, min(28800, secs))
    store.set("otp_timeout", str(secs))
    send_message(bot, chat_id, f"OTP timeout set to {secs} seconds.")


COMMANDS: dict[str, CmdFn] = {
    ".list": _cmd_list,
    ".mgr": _cmd_mgr,
    ".exit": _cmd_exit,
    ".help": _cmd_help,
    ".health": _cmd_health,
    ".rename": _cmd_rename,
    ".otptimeout": _cmd_otptimeout,
}


# -- Factory ---------------------------------------------------------------

def create_handler(config: Config, store: Store, backend: ValidatedBackend):
    """Return an async handler callback for python-telegram-bot v21."""
    s = _State()
    lock = threading.Lock()

    def _notify(chat_id: int, text: str) -> None:
        # Deferred import to avoid circular; bot is captured from closure at call time.
        send_message(_notify._bot, chat_id, text)

    _notify._bot = None  # type: ignore[attr-defined]

    router = ManagerRouter(backend, config, _notify)

    _ROUTER_CMDS = {".mgr", ".exit", ".health"}

    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        bot = context.bot
        _notify._bot = bot  # type: ignore[attr-defined]
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
                send_message(bot, chat_id,
                             "Welcome to onecmd! You are now the owner.\n\n"
                             "<b>.list</b> \u2014 show terminals\n"
                             "<b>.mgr</b> \u2014 AI manager mode\n"
                             "<b>.help</b> \u2014 all commands")
                return
            if not is_owner:
                return

            # 2. TOTP auth -- HARD GATE
            if not config.weak_security:
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
                            answer_callback(bot, update.callback_query.id)
                            return
                        if len(text) == 6 and text.isdigit() and totp_verify(text, secret_hex):
                            s.authenticated = True
                            s.last_auth_time = time.time()
                            send_message(bot, chat_id, "Authenticated.")
                        else:
                            send_message(bot, chat_id, "Enter OTP code.")
                        return
                    s.last_auth_time = time.time()

            # 3. Callback queries (refresh button)
            if is_cb:
                answer_callback(bot, update.callback_query.id)
                if (update.callback_query.data or "") == REFRESH_CALLBACK and s.connected:
                    if backend.connected(s.term_id):
                        _show_terminal(bot, chat_id, s.term_id, s, backend, config)
                    else:
                        _show_closed(bot, chat_id, s, backend)
                return

            # 4. Dot-commands via COMMANDS dict
            cmd_key = text.lower().split()[0] if text.strip() else ""
            cmd_fn = COMMANDS.get(cmd_key)
            if cmd_fn is not None:
                if cmd_key in _ROUTER_CMDS:
                    cmd_fn(bot, chat_id, text, s, backend, store, config, router=router)
                else:
                    cmd_fn(bot, chat_id, text, s, backend, store, config)
                return

            # 5. .N pattern (connect to terminal)
            m = DOT_N_RE.match(text.strip())
            if m:
                n = int(m.group(1))
                terminals = backend.list()
                if n < 1 or n > len(terminals):
                    send_message(bot, chat_id, "Invalid window number.")
                    return
                if s.mgr_mode:
                    s.mgr_mode = False
                    router.deactivate()
                    send_message(bot, chat_id, "Manager mode off. 1-on-1 mode on.")
                t = terminals[n - 1]
                s.connected, s.term_id = True, t.id
                s.term_name, s.term_title = t.name, t.title
                title_part = f" - {html_escape(t.title)}" if t.title else ""
                send_message(bot, chat_id, f"Connected to {html_escape(t.name)}{title_part}")
                _show_terminal(bot, chat_id, t.id, s, backend, config)
                return

            # 6. Manager mode — route to LLM agent
            if s.mgr_mode and not text.startswith("."):
                if not text.strip():
                    return
                response = router.handle(chat_id, text)
                if response:
                    send_message(bot, chat_id, response)
                return

            # 7. Connected mode: send keystrokes
            if s.connected:
                if not backend.connected(s.term_id):
                    _show_closed(bot, chat_id, s, backend)
                    return
                try:
                    _send_keystrokes(backend, s.term_id, text)
                except (ValueError, RuntimeError) as exc:
                    logger.warning("send_keys failed: %s", exc)
                    send_message(bot, chat_id, f"Error: {exc}")
                    return
                need_display = True
            else:
                need_display = False

            # 8. Default: show terminal list
            if not need_display:
                send_message(bot, chat_id, _build_list_text(backend.list()))
                return

        # Post-keystroke: sleep outside lock, then show terminal output
        await asyncio.sleep(0.5)
        with lock:
            if s.connected:
                if backend.connected(s.term_id):
                    _show_terminal(bot, chat_id, s.term_id, s, backend, config)
                else:
                    _show_closed(bot, chat_id, s, backend)

    return handler
