"""Unified message handler that bridges connectors with bot logic.

This module provides the glue between the platform-agnostic Connector
interface and the existing bot handler logic.  It translates connector
callbacks into the same operations the handler.py module performs, but
using the connector's send/edit/delete methods instead of direct
Telegram API calls.

Calling spec:
  Inputs:  Config, Store, ValidatedBackend, list of Connectors
  Outputs: message_handler and callback_handler callables
  Side effects: sends messages via connectors, modifies connection state
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
from typing import TYPE_CHECKING, Any

from onecmd.auth.owner import check_owner
from onecmd.auth.totp import STORE_KEY as TOTP_SECRET_KEY
from onecmd.auth.totp import is_timed_out, totp_verify
from onecmd.connectors.base import Connector
from onecmd.emoji import parse as emoji_parse
from onecmd.manager.router import ManagerRouter

if TYPE_CHECKING:
    from onecmd.config import Config
    from onecmd.store import Store
    from onecmd.terminal.backend import ValidatedBackend, TermInfo

logger = logging.getLogger(__name__)

ALIASES_PATH = ".onecmd/aliases.json"
DOT_N_RE = re.compile(r"^\.(\d+)$")
REFRESH_CALLBACK = "refresh"
_START_TIME = time.time()

HELP_TEXT = (
    "<b>Commands</b>\n"
    "<code>.list</code> — show terminal windows\n"
    "<code>.new</code> — open a new terminal\n"
    "<code>.1</code> <code>.2</code> ... — connect to a terminal\n"
    "<code>.rename N name</code> — name a terminal\n"
    "<code>.mgr</code> — AI manager mode\n"
    "<code>.exit</code> — leave manager mode\n"
    "<code>.health</code> — health report\n"
    "<code>.help</code> — this help\n\n"
    "<b>Once connected</b>, text is sent as keystrokes.\n"
    "Enter is auto-appended; end with \U0001f49c to suppress.\n\n"
    "<b>Modifiers</b>\n"
    "<code>\u2764\ufe0f</code> Ctrl  <code>\U0001f499</code> Alt  "
    "<code>\U0001f49a</code> Cmd  <code>\U0001f49b</code> ESC  "
    "<code>\U0001f9e1</code> Enter\n\n"
    "Escape: <code>\\n</code>=Enter  <code>\\t</code>=Tab"
)


def html_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


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
    tracked_msgs: list = field(default_factory=list)  # list of (msg_id_str,)


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


async def _show_terminal(connector: Connector, chat_id: str, term_id: str,
                         s: _State, backend, config):
    captured = backend.capture(term_id)
    if captured:
        await _send_terminal_display(connector, chat_id, captured, s, config)


async def _send_terminal_display(connector: Connector, chat_id: str,
                                 text: str, s: _State, config):
    """Send terminal output as pre-formatted text, deleting old tracked messages."""
    # Delete old tracked messages
    for msg_id in reversed(s.tracked_msgs):
        try:
            await connector.delete_message(chat_id, msg_id)
        except Exception:
            pass
    s.tracked_msgs.clear()

    # Format output
    from onecmd.terminal.display import last_n_lines, format_chunks
    tail = last_n_lines(text, config.visible_lines)
    escaped = html_escape(tail)
    chunks = format_chunks(escaped, config.split_messages)

    for chunk in chunks:
        msg_id = await connector.send_message(chat_id, chunk)
        if msg_id:
            s.tracked_msgs.append(msg_id)


async def _show_closed(connector: Connector, chat_id: str, s: _State, backend):
    _disconnect(s)
    terminals = backend.list()
    await connector.send_message(
        chat_id, "Window closed.\n\n" + _build_list_text(terminals, ""))


# -- Keystroke sending -----------------------------------------------------

def _action_to_raw(action) -> str:
    if action.kind == "key":
        base = {"Enter": "\n", "Tab": "\t", "Escape": "\x1b"}.get(
            action.value, action.value)
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
    if buf:
        backend.send_keys(term_id, "".join(buf), literal=True)
        buf.clear()


def _send_keystrokes(backend, term_id: str, text: str) -> None:
    actions, suppress_newline = emoji_parse(text)
    if not suppress_newline:
        from onecmd.emoji import KeyAction as _KA
        actions.append(_KA("key", "Enter"))
    buf: list[str] = []
    for action in actions:
        has_modifier = action.ctrl or action.alt or action.cmd
        is_special = action.kind == "key"
        if not has_modifier and not is_special:
            buf.append(action.value)
        else:
            _flush_literal(backend, term_id, buf)
            raw = _action_to_raw(action)
            backend.send_keys(term_id, raw, literal=True)
    _flush_literal(backend, term_id, buf)


# -- Factory ---------------------------------------------------------------

def create_connector_handler(config: Config, store: Store,
                             backend: ValidatedBackend):
    """Return message_handler and callback_handler for use with any Connector.

    These handlers have the same logic as the original Telegram-specific
    handler, but communicate through the Connector interface.
    """
    # Per-platform, per-user state.  Key = (platform_name, chat_id)
    states: dict[tuple[str, str], _State] = {}
    lock = threading.Lock()

    def _get_state(platform: str, chat_id: str) -> _State:
        key = (platform, chat_id)
        with lock:
            if key not in states:
                states[key] = _State()
            return states[key]

    # Notification functions for the manager router
    _main_loop: asyncio.AbstractEventLoop | None = None
    _connectors: dict[str, Connector] = {}

    def _register_connector(c: Connector) -> None:
        _connectors[c.platform_name] = c

    def _notify_sync(chat_id_str: str, text: str,
                     platform: str = "telegram") -> None:
        """Sync notify — safe to call from background threads."""
        connector = _connectors.get(platform)
        if connector is None:
            # Try first available connector
            for c in _connectors.values():
                connector = c
                break
        if connector is None or _main_loop is None:
            return
        asyncio.run_coroutine_threadsafe(
            connector.send_message(chat_id_str, text), _main_loop)

    # For backward compatibility with the router's notify_fn(chat_id: int, text: str)
    def _notify_compat(chat_id: int, text: str) -> None:
        _notify_sync(str(chat_id), text)

    router = ManagerRouter(backend, config, _notify_compat)

    _ROUTER_CMDS = {".mgr", ".exit", ".health"}

    async def message_handler(connector: Connector, chat_id: str,
                              user_id: str, text: str,
                              raw_event: Any) -> None:
        nonlocal _main_loop
        if _main_loop is None:
            _main_loop = asyncio.get_running_loop()
        _register_connector(connector)

        platform = connector.platform_name
        s = _get_state(platform, chat_id)

        # 1. Owner check
        with lock:
            is_owner, just_reg = check_owner(store, int(user_id))
            if just_reg:
                logger.info("Registered owner: %s (%s)",
                            user_id, platform)

        if just_reg:
            terminals = backend.list()
            term_count = len(terminals)
            welcome = (
                "\U0001f44b <b>Welcome to OneCmd!</b>\n"
                f"You're now the owner. {term_count} terminal"
                f"{'s' if term_count != 1 else ''} found.\n\n"
            )
            if term_count > 0:
                welcome += _build_list_text(terminals) + "\n\n"
            welcome += (
                "Quick start:\n"
                "<code>.list</code> — show terminals\n"
                "<code>.1</code> — connect to first terminal\n"
                "<code>.mgr</code> — AI manager mode\n"
                "<code>.help</code> — all commands"
            )
            await connector.send_message(chat_id, welcome)
            return

        if not is_owner:
            return

        # Handle /start
        if text.startswith("/start"):
            terminals = backend.list()
            await connector.send_message(
                chat_id,
                "\U0001f4bb <b>OneCmd</b>\n\n" + _build_list_text(terminals))
            return

        # 2. TOTP gate
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
                    if not s.authenticated or is_timed_out(
                            s.last_auth_time, otp_timeout):
                        s.authenticated = False
                        if (len(text) == 6 and text.isdigit()
                                and totp_verify(text, secret_hex)):
                            s.authenticated = True
                            s.last_auth_time = time.time()
                            await connector.send_message(
                                chat_id, "Authenticated.")
                        else:
                            await connector.send_message(
                                chat_id, "Enter OTP code.")
                        return
                    s.last_auth_time = time.time()

        # 4. Dot-commands
        cmd_key = text.lower().split()[0] if text.strip() else ""

        # Handle commands
        if cmd_key == ".list":
            for msg_id in reversed(s.tracked_msgs):
                try:
                    await connector.delete_message(chat_id, msg_id)
                except Exception:
                    pass
            s.tracked_msgs.clear()
            _disconnect(s)
            await connector.send_message(
                chat_id, _build_list_text(backend.list()))
            return

        if cmd_key == ".help":
            await connector.send_message(chat_id, HELP_TEXT)
            return

        if cmd_key == ".new":
            result = backend.create()
            if result is None:
                await connector.send_message(
                    chat_id, "Failed to create terminal.")
                return
            await asyncio.sleep(1.0)
            terminals = backend.list()
            await connector.send_message(
                chat_id,
                "\u2705 Terminal created.\n\n" + _build_list_text(terminals))
            return

        if cmd_key == ".rename":
            parts = text.split(None, 2)
            if len(parts) < 3:
                await connector.send_message(
                    chat_id, "Usage: .rename N name")
                return
            try:
                n = int(parts[1])
            except ValueError:
                await connector.send_message(
                    chat_id, "Usage: .rename N name")
                return
            name = parts[2].strip()
            if not name:
                await connector.send_message(
                    chat_id, "Usage: .rename N name")
                return
            terminals = backend.list()
            if n < 1 or n > len(terminals):
                await connector.send_message(
                    chat_id, "Invalid window number.")
                return
            _save_alias(terminals[n - 1].id, name)
            await connector.send_message(
                chat_id,
                f"Terminal {n} renamed to [{html_escape(name)}].")
            return

        if cmd_key == ".otptimeout":
            parts = text.split(None, 1)
            arg = parts[1].strip() if len(parts) > 1 else ""
            try:
                secs = int(arg)
            except ValueError:
                secs = config.otp_timeout
            secs = max(30, min(28800, secs))
            store.set("otp_timeout", str(secs))
            await connector.send_message(
                chat_id, f"OTP timeout set to {secs} seconds.")
            return

        if cmd_key == ".mgr":
            if not s.mgr_mode:
                msg = router.activate()
                if not router.active:
                    await connector.send_message(chat_id, msg)
                    return
                s.mgr_mode = True
                await connector.send_message(
                    chat_id,
                    "\U0001f916 Manager mode on.\n"
                    "Dot commands still work: .list .1 .exit")
            else:
                s.mgr_mode = False
                router.deactivate()
                await connector.send_message(
                    chat_id, "Manager mode off.")
            return

        if cmd_key == ".exit":
            if s.mgr_mode:
                s.mgr_mode = False
                router.deactivate()
                await connector.send_message(
                    chat_id, "Manager mode off.")
            else:
                await connector.send_message(
                    chat_id, "Not in manager mode.")
            return

        if cmd_key == ".health":
            up = int(time.time() - _START_TIME)
            h, r = divmod(up, 3600)
            m, sec = divmod(r, 60)
            conn = (f"{html_escape(s.term_name)} ({s.term_id})"
                    if s.connected else "None")
            lines = [
                "<b>Health Report</b>",
                f"Uptime: {h}h {m}m {sec}s",
                f"Connected: {conn}",
                f"Manager: {'on' if s.mgr_mode else 'off'}",
                f"LLM: {'configured' if config.has_llm_key else 'not configured'}",
                f"Terminals: {len(backend.list())}",
                f"Platform: {platform}",
                f"Connectors: {', '.join(_connectors.keys())}",
            ]
            rh = router.health()
            lines.append(
                f"Router: active={rh.get('active')}, "
                f"agent={rh.get('agent_initialized')}")
            await connector.send_message(chat_id, "\n".join(lines))
            return

        # 5. .N pattern (connect to terminal)
        m = DOT_N_RE.match(text.strip())
        if m:
            n = int(m.group(1))
            terminals = backend.list()
            if n < 1 or n > len(terminals):
                await connector.send_message(
                    chat_id, "Invalid window number.")
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
            msg = (f"\U0001f5a5 Connected to "
                   f"{name}{html_escape(t.name)}{title_part}")
            if was_mgr:
                msg += "\nManager mode off."
            msg += ("\n\nType to send keystrokes. "
                    "<code>.list</code> to disconnect.")
            await connector.send_message(chat_id, msg)
            await _show_terminal(connector, chat_id, t.id, s, backend, config)
            return

        # 6. Manager mode
        if s.mgr_mode and not text.startswith("."):
            if not text.strip():
                return
            await connector.send_chat_action(chat_id)
            loop = asyncio.get_event_loop()
            # Route to LLM agent
            # Use int chat_id for backward compat with router
            try:
                chat_id_int = int(chat_id)
            except ValueError:
                chat_id_int = hash(chat_id)
            task = asyncio.ensure_future(
                loop.run_in_executor(
                    None, router.handle, chat_id_int, text))
            while not task.done():
                await asyncio.sleep(4.0)
                if not task.done():
                    await connector.send_chat_action(chat_id)
            response = task.result()
            if response:
                await connector.send_message(chat_id, response)
            return

        # 7. Connected mode: send keystrokes
        need_display = False
        with lock:
            if s.connected:
                if not backend.connected(s.term_id):
                    pass
                else:
                    try:
                        _send_keystrokes(backend, s.term_id, text)
                        need_display = True
                    except (ValueError, RuntimeError) as exc:
                        logger.warning("send_keys failed: %s", exc)
                        await connector.send_message(
                            chat_id, f"Error: {exc}")
                        return

        if s.connected and not backend.connected(s.term_id):
            await _show_closed(connector, chat_id, s, backend)
            return

        # 8. Default: show terminal list
        if not need_display:
            await connector.send_message(
                chat_id, _build_list_text(backend.list()))
            return

        # Post-keystroke display
        await asyncio.sleep(0.5)
        with lock:
            if s.connected:
                if backend.connected(s.term_id):
                    await _show_terminal(
                        connector, chat_id, s.term_id, s, backend, config)
                else:
                    await _show_closed(connector, chat_id, s, backend)

    async def callback_handler(connector: Connector, chat_id: str,
                               user_id: str, callback_data: str,
                               callback_id: str, raw_event: Any) -> None:
        nonlocal _main_loop
        if _main_loop is None:
            _main_loop = asyncio.get_running_loop()
        _register_connector(connector)

        platform = connector.platform_name
        s = _get_state(platform, chat_id)

        await connector.answer_callback(callback_id)

        with lock:
            is_owner, _ = check_owner(store, int(user_id))
        if not is_owner:
            return

        with lock:
            if callback_data == REFRESH_CALLBACK and s.connected:
                if backend.connected(s.term_id):
                    await _show_terminal(
                        connector, chat_id, s.term_id, s, backend, config)
                else:
                    await _show_closed(connector, chat_id, s, backend)

    return message_handler, callback_handler, _register_connector
