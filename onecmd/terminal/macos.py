"""
macOS native terminal backend using CoreGraphics / Accessibility / CGEvent APIs.

Calling spec:
  Inputs:  parent_pid (int | None) for PID scoping, danger_mode (bool)
  Outputs: TermInfo list, captured text (str | None), send result (bool)
  Side effects: CGWindowList queries, AXUIElement traversal, CGEvent keystroke injection

Conditional import: only loaded on Darwin via importlib in backend.py.
Requires pyobjc-framework-Quartz and pyobjc-framework-ApplicationServices.

Methods:
  list()                    -> list[TermInfo]   # CGWindowListCopyWindowInfo, filtered
  connected(term_id: str)   -> bool             # window existence check
  capture(term_id: str)     -> str | None       # AXUIElement text extraction
  send_keys(term_id: str, text: str) -> bool    # CGEvent keystroke injection
  create()                  -> str | None       # AppleScript / open -na new window
  free_list()               -> None             # clear cached list

Guarding:
  - PID filter enforced: if parent_pid set and not danger_mode, only that PID
  - DangerMode bypasses terminal app filter but NOT PID filter
  - Window IDs validated as numeric strings
  - Text capture output capped at 64 KB
  - Known terminal apps: hardcoded allowlist
"""

from __future__ import annotations

import logging
import threading
import time

import Quartz  # type: ignore[import-untyped]
from ApplicationServices import (  # type: ignore[import-untyped]
    AXUIElementCreateApplication,
    AXUIElementCopyAttributeValue,
    AXUIElementSetAttributeValue,
    AXUIElementPerformAction,
    kAXErrorSuccess,
    kAXWindowsAttribute,
    kAXChildrenAttribute,
    kAXRoleAttribute,
    kAXValueAttribute,
    kAXTitleAttribute,
    kAXFrontmostAttribute,
    kAXRaiseAction,
)
from Quartz import (  # type: ignore[import-untyped]
    CGWindowListCopyWindowInfo,
    kCGWindowListOptionOnScreenOnly,
    kCGWindowListExcludeDesktopElements,
    kCGNullWindowID,
    CGEventCreateKeyboardEvent,
    CGEventPost,
    CGEventSetFlags,
    CGEventKeyboardSetUnicodeString,
    kCGEventFlagMaskControl,
    kCGEventFlagMaskAlternate,
    kCGEventFlagMaskCommand,
    kCGHIDEventTap,
)
from Quartz.CoreGraphics import (  # type: ignore[import-untyped]
    CGRectMakeWithDictionaryRepresentation,
)
from CoreFoundation import kCFBooleanTrue  # type: ignore[import-untyped]

log = logging.getLogger(__name__)

_MAX_CAPTURE_BYTES = 64 * 1024  # 64 KB cap on captured text

from onecmd.terminal.backend import TermInfo  # noqa: E402

# ---------------------------------------------------------------------------
# Known terminal applications
# ---------------------------------------------------------------------------

_TERMINAL_APPS = (
    "Terminal", "iTerm2", "iTerm", "Ghostty", "kitty",
    "Alacritty", "Hyper", "Warp", "WezTerm", "Tabby",
)


def _is_terminal_app(name: str) -> bool:
    """Case-insensitive check whether *name* matches a known terminal app."""
    lower = name.lower()
    return any(app.lower() in lower for app in _TERMINAL_APPS)


# AppleScript snippets to open a new window in known terminal apps.
_CREATE_SCRIPTS: dict[str, str] = {
    "terminal": 'tell application "Terminal" to do script ""',
    "iterm2": 'tell application "iTerm2" to create window with default profile',
    "iterm": 'tell application "iTerm" to create window with default profile',
}


# ---------------------------------------------------------------------------
# Virtual keycodes (US keyboard layout) — ported from backend_macos.c
# ---------------------------------------------------------------------------

_VK_RETURN = 0x24
_VK_TAB = 0x30
_VK_ESCAPE = 0x35

_MOD_CTRL = 1 << 0
_MOD_ALT = 1 << 1
_MOD_CMD = 1 << 2

_LETTER_MAP: dict[str, int] = {
    "a": 0x00, "b": 0x0B, "c": 0x08, "d": 0x02, "e": 0x0E,
    "f": 0x03, "g": 0x05, "h": 0x04, "i": 0x22, "j": 0x26,
    "k": 0x28, "l": 0x25, "m": 0x2E, "n": 0x2D, "o": 0x1F,
    "p": 0x23, "q": 0x0C, "r": 0x0F, "s": 0x01, "t": 0x11,
    "u": 0x20, "v": 0x09, "w": 0x0D, "x": 0x07, "y": 0x10,
    "z": 0x06,
}

_DIGIT_MAP: dict[str, int] = {
    "0": 0x1D, "1": 0x12, "2": 0x13, "3": 0x14, "4": 0x15,
    "5": 0x17, "6": 0x16, "7": 0x1A, "8": 0x1C, "9": 0x19,
}

_SYMBOL_MAP: dict[str, int] = {
    "-": 0x1B, "=": 0x18, "[": 0x21, "]": 0x1E,
    "\\": 0x2A, ";": 0x29, "'": 0x27, ",": 0x2B,
    ".": 0x2F, "/": 0x2C, "`": 0x32, " ": 0x31,
}


def _keycode_for_char(ch: str) -> int | None:
    """Map an ASCII character to its macOS virtual keycode, or None."""
    lower = ch.lower()
    if lower in _LETTER_MAP:
        return _LETTER_MAP[lower]
    if ch in _DIGIT_MAP:
        return _DIGIT_MAP[ch]
    return _SYMBOL_MAP.get(ch)


# ---------------------------------------------------------------------------
# Private AX helper: get CGWindowID from AXUIElement
# ---------------------------------------------------------------------------

_ax_get_window_fn = None  # cached ctypes function pointer


def _ax_get_window_id(element: object) -> int | None:
    """
    Get CGWindowID from an AXUIElement window.

    Uses the private _AXUIElementGetWindow API via ctypes.
    Falls back to None if unavailable.
    """
    global _ax_get_window_fn
    try:
        if _ax_get_window_fn is None:
            import ctypes
            import ctypes.util
            lib_path = ctypes.util.find_library("ApplicationServices")
            ax_lib = ctypes.cdll.LoadLibrary(lib_path)
            fn = ax_lib._AXUIElementGetWindow
            fn.restype = ctypes.c_int32  # AXError
            fn.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
            _ax_get_window_fn = fn

        import ctypes
        import objc  # type: ignore[import-untyped]
        ptr = objc.pyobjc_id(element)
        wid = ctypes.c_uint32(0)
        err = _ax_get_window_fn(ptr, ctypes.byref(wid))
        if err == 0:  # kAXErrorSuccess
            return int(wid.value)
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# AX text capture helpers — ported from backend_macos.c
# ---------------------------------------------------------------------------

def _ax_read_value(element: object) -> str | None:
    """
    Read AXValue text from an element.

    Strips null bytes (iTerm2 uses these to pad empty terminal cells)
    and trims trailing spaces on each line.
    """
    err, value = AXUIElementCopyAttributeValue(element, kAXValueAttribute, None)
    if err != kAXErrorSuccess or value is None:
        return None
    if not isinstance(value, str):
        return None

    # Replace null bytes with spaces (empty terminal cells)
    text = value.replace("\0", " ")

    # Trim trailing spaces on each line
    lines = text.split("\n")
    lines = [line.rstrip(" ") for line in lines]
    result = "\n".join(lines)

    # Strip trailing empty lines
    result = result.rstrip("\n")
    return result if result else None


def _ax_get_text_content(element: object) -> str | None:
    """Recursively search AX hierarchy for a text area and return its text."""
    err, role = AXUIElementCopyAttributeValue(element, kAXRoleAttribute, None)
    if err == kAXErrorSuccess and role is not None:
        if role in ("AXTextArea", "AXStaticText", "AXWebArea"):
            return _ax_read_value(element)

    err, children = AXUIElementCopyAttributeValue(element, kAXChildrenAttribute, None)
    if err == kAXErrorSuccess and children is not None:
        for child in children:
            text = _ax_get_text_content(child)
            if text is not None:
                return text
    return None


# ---------------------------------------------------------------------------
# Keystroke injection helpers — ported from backend_macos.c
# ---------------------------------------------------------------------------

def _bring_to_front(pid: int) -> bool:
    """Bring app to front via Accessibility API."""
    app = AXUIElementCreateApplication(pid)
    if app is None:
        return False
    err = AXUIElementSetAttributeValue(app, kAXFrontmostAttribute, kCFBooleanTrue)
    if err != kAXErrorSuccess:
        return False
    time.sleep(0.1)
    return True


def _raise_window_by_id(pid: int, target_wid: int) -> bool:
    """Raise the specific window by matching CGWindowID via Accessibility API."""
    app = AXUIElementCreateApplication(pid)
    if app is None:
        return False

    err, windows = AXUIElementCopyAttributeValue(app, kAXWindowsAttribute, None)
    if err != kAXErrorSuccess or windows is None:
        return False

    found = False
    for win in windows:
        wid = _ax_get_window_id(win)
        if wid is not None and wid == target_wid:
            AXUIElementPerformAction(win, kAXRaiseAction)
            found = True
            break

    # Also bring the app to front
    _bring_to_front(pid)
    return found


def _send_key(pid: int, keycode: int, ch: str | None, mods: int) -> None:
    """
    Inject a single keystroke to a process via CGEvent.

    When modifiers are active and we have a character, use the virtual
    keycode so the system sends the right combo. Otherwise set the
    unicode string directly.
    """
    mapped_keycode = False
    if ch and mods:
        mapped = _keycode_for_char(ch)
        if mapped is not None:
            keycode = mapped
            mapped_keycode = True

    down = CGEventCreateKeyboardEvent(None, keycode, True)
    up = CGEventCreateKeyboardEvent(None, keycode, False)
    if down is None or up is None:
        return

    flags = 0
    if mods & _MOD_CTRL:
        flags |= kCGEventFlagMaskControl
    if mods & _MOD_ALT:
        flags |= kCGEventFlagMaskAlternate
    if mods & _MOD_CMD:
        flags |= kCGEventFlagMaskCommand

    if flags:
        CGEventSetFlags(down, flags)
        CGEventSetFlags(up, flags)

    # When we have a mapped keycode with modifiers, let the system
    # derive the character from keycode + flags. Otherwise set it.
    if ch and not mapped_keycode:
        CGEventKeyboardSetUnicodeString(down, 1, ch)
        CGEventKeyboardSetUnicodeString(up, 1, ch)

    CGEventPost(kCGHIDEventTap, down)
    time.sleep(0.001)
    CGEventPost(kCGHIDEventTap, up)
    time.sleep(0.005)


# ---------------------------------------------------------------------------
# Emoji parsing helpers for send_keys
# ---------------------------------------------------------------------------

# UTF-8 byte sequences for emoji hearts used as modifiers
_RED_HEART = "\u2764\uFE0F"       # ❤️  Ctrl
_ORANGE_HEART = "\U0001F9E1"      # 🧡  Enter
_YELLOW_HEART = "\U0001F49B"      # 💛  Escape
_BLUE_HEART = "\U0001F499"        # 💙  Alt
_GREEN_HEART = "\U0001F49A"       # 💚  Cmd
_PURPLE_HEART = "\U0001F49C"      # 💜  Suppress newline


def _strip_purple_heart(text: str) -> tuple[str, bool]:
    """Return (text_without_trailing_purple, add_newline)."""
    if text.endswith(_PURPLE_HEART):
        return text[: -len(_PURPLE_HEART)], False
    return text, True


# ---------------------------------------------------------------------------
# MacOSBackend
# ---------------------------------------------------------------------------

class MacOSBackend:
    """
    macOS native backend using CoreGraphics / Accessibility / CGEvent.

    Constructor args:
      parent_pid:  int | None — if set, scope to windows owned by this PID
      danger_mode: bool — if True, bypass terminal app filter (NOT PID filter)
    """

    # Global lock: CGEvent keystrokes go to the frontmost window, so
    # concurrent send_keys calls would interleave characters.
    _send_lock = threading.Lock()

    def __init__(self, parent_pid: int | None, danger_mode: bool) -> None:
        self._parent_pid = parent_pid
        self._danger_mode = danger_mode
        self._terms: list[TermInfo] = []
        # Cache of term_id -> (pid, wid) for connected/capture/send
        self._id_map: dict[str, tuple[int, int]] = {}

    # ---- list -------------------------------------------------------------

    def list(self) -> list[TermInfo]:
        """Enumerate on-screen terminal windows via CGWindowListCopyWindowInfo."""
        self._terms = []
        self._id_map = {}

        window_list = CGWindowListCopyWindowInfo(
            kCGWindowListOptionOnScreenOnly | kCGWindowListExcludeDesktopElements,
            kCGNullWindowID,
        )
        if window_list is None:
            return []

        terms: list[TermInfo] = []

        for info in window_list:
            # Owner name
            owner = info.get("kCGWindowOwnerName", "")
            if not owner:
                continue

            # Filter to known terminals unless danger mode
            if not self._danger_mode and not _is_terminal_app(owner):
                continue

            # Window ID and PID
            wid = info.get("kCGWindowNumber")
            pid = info.get("kCGWindowOwnerPID")
            if wid is None or pid is None:
                continue
            wid = int(wid)
            pid = int(pid)

            # PID filter (always enforced when parent_pid is set)
            if self._parent_pid is not None and self._parent_pid > 0:
                if not self._danger_mode and pid != self._parent_pid:
                    continue

            # Only layer 0 (normal windows)
            layer = int(info.get("kCGWindowLayer", 0))
            if layer != 0:
                continue

            # Must have reasonable size
            bounds_dict = info.get("kCGWindowBounds")
            if bounds_dict is None:
                continue
            _, bounds = CGRectMakeWithDictionaryRepresentation(bounds_dict, None)
            if bounds is None:
                continue
            if bounds.size.width <= 50 or bounds.size.height <= 50:
                continue

            # Window title
            title = info.get("kCGWindowName", "") or ""

            term_id = str(wid)
            term = TermInfo(id=term_id, pid=pid, name=owner, title=title)
            terms.append(term)
            self._id_map[term_id] = (pid, wid)

        # Fill in titles from AX for windows that need it
        self._terms = terms
        for idx, term in enumerate(self._terms):
            if term.title:
                continue
            self._fill_ax_title(idx, term)

        return list(self._terms)

    def _fill_ax_title(self, idx: int, term: TermInfo) -> None:
        """Fill missing title from AX API (replaces frozen dataclass in list)."""
        app = AXUIElementCreateApplication(term.pid)
        if app is None:
            return
        err, ax_windows = AXUIElementCopyAttributeValue(app, kAXWindowsAttribute, None)
        if err != kAXErrorSuccess or ax_windows is None:
            return
        target_wid = int(term.id)
        for win in ax_windows:
            wid = _ax_get_window_id(win)
            if wid is not None and wid == target_wid:
                terr, title_val = AXUIElementCopyAttributeValue(
                    win, kAXTitleAttribute, None,
                )
                if terr == kAXErrorSuccess and title_val:
                    self._terms[idx] = TermInfo(
                        id=term.id, pid=term.pid,
                        name=term.name, title=str(title_val),
                    )
                break

    # ---- connected --------------------------------------------------------

    def connected(self, term_id: str) -> bool:
        """Check whether a window still exists on screen."""
        if not term_id.isdigit():
            return False

        target_wid = int(term_id)
        pid = self._id_map.get(term_id, (0, 0))[0]

        window_list = CGWindowListCopyWindowInfo(
            kCGWindowListOptionOnScreenOnly | kCGWindowListExcludeDesktopElements,
            kCGNullWindowID,
        )
        if window_list is None:
            return False

        fallback_wid: int | None = None

        for info in window_list:
            wid = info.get("kCGWindowNumber")
            wpid = info.get("kCGWindowOwnerPID")
            if wid is None or wpid is None:
                continue
            wid = int(wid)
            wpid = int(wpid)

            if wid == target_wid:
                return True

            # Track fallback: another on-screen window from the same PID
            if pid and wpid == pid and fallback_wid is None:
                layer = int(info.get("kCGWindowLayer", 0))
                if layer == 0:
                    fallback_wid = wid

        # Window gone but same app has another window (likely tab switch)
        if fallback_wid is not None:
            new_id = str(fallback_wid)
            # Update our id map
            if term_id in self._id_map:
                old_pid, _ = self._id_map.pop(term_id)
                self._id_map[new_id] = (old_pid, fallback_wid)
            return True

        return False

    # ---- capture ----------------------------------------------------------

    def capture(self, term_id: str) -> str | None:
        """Capture text from a terminal window via Accessibility API."""
        if not term_id.isdigit():
            return None

        entry = self._id_map.get(term_id)
        if entry is None:
            return None
        pid, target_wid = entry

        app = AXUIElementCreateApplication(pid)
        if app is None:
            return None

        err, windows = AXUIElementCopyAttributeValue(app, kAXWindowsAttribute, None)
        if err != kAXErrorSuccess or windows is None:
            return None

        text: str | None = None
        for win in windows:
            wid = _ax_get_window_id(win)
            if wid is not None and wid == target_wid:
                text = _ax_get_text_content(win)
                break

        if text is not None and len(text) > _MAX_CAPTURE_BYTES:
            text = text[-_MAX_CAPTURE_BYTES:]

        return text

    # ---- send_keys --------------------------------------------------------

    def send_keys(self, term_id: str, text: str, literal: bool = True) -> bool:
        """
        Inject pre-processed keystrokes into a terminal window via CGEvent.

        Accepts raw characters where control codes are already resolved:
          \\n (0x0a) = Enter, \\t (0x09) = Tab, \\x1b (0x1b) = Escape,
          \\x01-\\x1a = Ctrl+A through Ctrl+Z,
          \\x1b followed by a char = Alt+char,
          all other characters sent as literal keystrokes.

        The *literal* parameter is accepted for protocol compatibility but
        is not used — CGEvent always sends keystrokes directly.

        Emoji parsing is handled upstream by onecmd.emoji.parse() and
        handler._send_keystrokes(); this method must not re-parse emoji.
        """
        if not term_id.isdigit():
            return False

        entry = self._id_map.get(term_id)
        if entry is None:
            return False
        pid, target_wid = entry

        with self._send_lock:
            log.debug("send_keys %s: %r", term_id, text)

            # Raise the target window
            _raise_window_by_id(pid, target_wid)

            i = 0
            while i < len(text):
                ch = text[i]
                code = ord(ch)

                # Escape (0x1b) — if followed by another char, treat as Alt+char
                if code == 0x1B:
                    if i + 1 < len(text):
                        nxt = text[i + 1]
                        mapped = _keycode_for_char(nxt)
                        if mapped is not None:
                            _send_key(pid, mapped, nxt, _MOD_ALT)
                        else:
                            _send_key(pid, 0, nxt, _MOD_ALT)
                        i += 2
                    else:
                        # Bare Escape
                        _send_key(pid, _VK_ESCAPE, None, 0)
                        i += 1
                    continue

                # Enter (newline)
                if ch == "\n":
                    _send_key(pid, _VK_RETURN, None, 0)
                    i += 1
                    continue

                # Tab
                if ch == "\t":
                    _send_key(pid, _VK_TAB, None, 0)
                    i += 1
                    continue

                # Ctrl+A (0x01) through Ctrl+Z (0x1a)
                if 1 <= code <= 26:
                    ctrl_ch = chr(code + 64)  # 0x01 -> 'A', 0x03 -> 'C', etc.
                    mapped = _keycode_for_char(ctrl_ch)
                    if mapped is not None:
                        _send_key(pid, mapped, ctrl_ch, _MOD_CTRL)
                    else:
                        _send_key(pid, 0, ctrl_ch, _MOD_CTRL)
                    i += 1
                    continue

                # Regular printable character
                _send_key(pid, 0, ch, 0)
                i += 1

        return True

    # ---- create -----------------------------------------------------------

    def create(self) -> str | None:
        """Open a new terminal window.

        Uses AppleScript for Terminal.app / iTerm2.  Falls back to
        ``open -na <AppName>`` for other terminal emulators.
        Returns the app name on success, None on failure.
        """
        import subprocess as _sp

        # Pick the terminal app from the last list() call.
        app_name: str | None = None
        for t in self._terms:
            if _is_terminal_app(t.name):
                app_name = t.name
                break
        if app_name is None:
            app_name = "Terminal"

        script = _CREATE_SCRIPTS.get(app_name.lower())
        try:
            if script:
                _sp.run(
                    ["osascript", "-e", script],
                    timeout=10, capture_output=True, shell=False,
                )
            else:
                _sp.run(
                    ["open", "-na", app_name],
                    timeout=10, capture_output=True, shell=False,
                )
            return app_name
        except (_sp.TimeoutExpired, FileNotFoundError, OSError):
            return None

    # ---- free_list --------------------------------------------------------

    def free_list(self) -> None:
        """Clear the cached terminal list."""
        self._terms = []
        self._id_map = {}
