"""
Calling spec:
  Inputs:  raw text string with emoji modifiers (max 4096 chars)
  Outputs: list of KeyAction(kind, value, ctrl, alt, cmd, suppress_newline)
  Side effects: None (pure function)

Sealed (deterministic, human-written, 100% test coverage):
  ❤️  = Ctrl modifier       💙 = Alt modifier
  💚  = Cmd modifier         💛 = Escape key
  🧡  = Enter key            💜 = Suppress trailing newline
  \\n = Enter, \\t = Tab, \\\\ = literal backslash
"""

from __future__ import annotations

from dataclasses import dataclass

MAX_INPUT = 4096

# Emoji constants (as they appear in UTF-8 strings)
RED_HEART_LONG = "\u2764\ufe0f"   # ❤️  (with variation selector)
RED_HEART_SHORT = "\u2764"        # ❤  (without variation selector)
BLUE_HEART = "\U0001f499"         # 💙
GREEN_HEART = "\U0001f49a"        # 💚
YELLOW_HEART = "\U0001f49b"       # 💛
ORANGE_HEART = "\U0001f9e1"       # 🧡
PURPLE_HEART = "\U0001f49c"       # 💜


@dataclass(frozen=True, slots=True)
class KeyAction:
    """A single parsed keystroke action.

    kind: 'key' for named keys (Enter/Tab/Escape), 'char' for characters.
    value: the key name or character.
    ctrl: Ctrl modifier active.
    alt: Alt modifier active.
    cmd: Cmd modifier active.
    """
    kind: str       # 'key' | 'char'
    value: str      # 'Enter', 'Tab', 'Escape', or a character
    ctrl: bool = False
    alt: bool = False
    cmd: bool = False


def parse(text: str) -> tuple[list[KeyAction], bool]:
    """Parse emoji-encoded keystroke text.

    Returns (actions, suppress_newline).
    Raises ValueError if input exceeds MAX_INPUT chars.
    """
    if not isinstance(text, str):
        raise TypeError("input must be a string")
    if len(text) > MAX_INPUT:
        raise ValueError(f"input length {len(text)} exceeds max {MAX_INPUT}")
    if not text:
        return [], False

    # Check for trailing purple heart -> suppress newline
    suppress_newline = text.endswith(PURPLE_HEART)
    if suppress_newline:
        text = text[: -len(PURPLE_HEART)]

    actions: list[KeyAction] = []
    ctrl = alt = cmd = False
    i = 0

    while i < len(text):
        ch = text[i]

        # Red heart: Ctrl modifier (with or without variation selector)
        if text[i:].startswith(RED_HEART_LONG):
            ctrl = True
            i += len(RED_HEART_LONG)
            continue
        if text[i:].startswith(RED_HEART_SHORT):
            ctrl = True
            i += len(RED_HEART_SHORT)
            continue

        # Orange heart: Enter key
        if text[i:].startswith(ORANGE_HEART):
            actions.append(KeyAction("key", "Enter", ctrl, alt, cmd))
            ctrl = alt = cmd = False
            i += len(ORANGE_HEART)
            continue

        # Colored hearts: blue=Alt, green=Cmd, yellow=Escape
        if text[i:].startswith(YELLOW_HEART):
            actions.append(KeyAction("key", "Escape"))
            ctrl = alt = cmd = False
            i += len(YELLOW_HEART)
            continue
        if text[i:].startswith(BLUE_HEART):
            alt = True
            i += len(BLUE_HEART)
            continue
        if text[i:].startswith(GREEN_HEART):
            cmd = True
            i += len(GREEN_HEART)
            continue

        # Backslash escape sequences
        if ch == "\\" and i + 1 < len(text):
            nxt = text[i + 1]
            if nxt == "n":
                actions.append(KeyAction("key", "Enter", ctrl, alt, cmd))
                ctrl = alt = cmd = False
                i += 2
                continue
            if nxt == "t":
                actions.append(KeyAction("key", "Tab", ctrl, alt, cmd))
                ctrl = alt = cmd = False
                i += 2
                continue
            if nxt == "\\":
                actions.append(KeyAction("char", "\\", ctrl, alt, cmd))
                ctrl = alt = cmd = False
                i += 2
                continue

        # Regular character
        actions.append(KeyAction("char", ch, ctrl, alt, cmd))
        ctrl = alt = cmd = False
        i += 1

    return actions, suppress_newline
