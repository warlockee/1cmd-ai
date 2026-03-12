"""Shared user-facing text constants and builders.

Centralizes help text, welcome messages, and other strings used
by both the Telegram handler and the multi-connector handler.
"""

from __future__ import annotations

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


def build_welcome_message(term_count: int, list_text: str) -> str:
    """Build the welcome message shown to a newly registered owner."""
    welcome = (
        "\U0001f44b <b>Welcome to OneCmd!</b>\n"
        f"You're now the owner. {term_count} terminal"
        f"{'s' if term_count != 1 else ''} found.\n\n"
    )
    if term_count > 0:
        welcome += list_text + "\n\n"
    welcome += (
        "Quick start:\n"
        "<code>.list</code> — show terminals\n"
        "<code>.1</code> — connect to first terminal\n"
        "<code>.mgr</code> — AI manager mode\n"
        "<code>.help</code> — all commands"
    )
    return welcome
