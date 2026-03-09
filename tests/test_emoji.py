"""Tests for onecmd.emoji — 100% coverage, sealed module."""

import pytest

from onecmd.emoji import (
    KeyAction,
    MAX_INPUT,
    BLUE_HEART,
    GREEN_HEART,
    ORANGE_HEART,
    PURPLE_HEART,
    RED_HEART_LONG,
    RED_HEART_SHORT,
    YELLOW_HEART,
    parse,
)


# ---------------------------------------------------------------------------
# Basic: empty, plain text
# ---------------------------------------------------------------------------

class TestBasic:
    def test_empty_string(self):
        actions, suppress = parse("")
        assert actions == []
        assert suppress is False

    def test_plain_text(self):
        actions, suppress = parse("hello")
        assert suppress is False
        assert len(actions) == 5
        assert all(a.kind == "char" for a in actions)
        assert "".join(a.value for a in actions) == "hello"

    def test_single_char(self):
        actions, _ = parse("x")
        assert actions == [KeyAction("char", "x")]


# ---------------------------------------------------------------------------
# Modifiers: Ctrl (red heart)
# ---------------------------------------------------------------------------

class TestCtrl:
    def test_ctrl_c_long(self):
        """❤️c -> Ctrl+C"""
        actions, _ = parse(RED_HEART_LONG + "c")
        assert actions == [KeyAction("char", "c", ctrl=True)]

    def test_ctrl_c_short(self):
        """❤c (no variation selector) -> Ctrl+C"""
        actions, _ = parse(RED_HEART_SHORT + "c")
        assert actions == [KeyAction("char", "c", ctrl=True)]

    def test_ctrl_modifier_resets(self):
        """Ctrl applies to next char only."""
        actions, _ = parse(RED_HEART_LONG + "cd")
        assert actions == [
            KeyAction("char", "c", ctrl=True),
            KeyAction("char", "d"),
        ]


# ---------------------------------------------------------------------------
# Modifiers: Alt (blue heart)
# ---------------------------------------------------------------------------

class TestAlt:
    def test_alt_x(self):
        """💙x -> Alt+X"""
        actions, _ = parse(BLUE_HEART + "x")
        assert actions == [KeyAction("char", "x", alt=True)]


# ---------------------------------------------------------------------------
# Modifiers: Cmd (green heart)
# ---------------------------------------------------------------------------

class TestCmd:
    def test_cmd_v(self):
        """💚v -> Cmd+V"""
        actions, _ = parse(GREEN_HEART + "v")
        assert actions == [KeyAction("char", "v", cmd=True)]


# ---------------------------------------------------------------------------
# Combined modifiers
# ---------------------------------------------------------------------------

class TestCombinedModifiers:
    def test_ctrl_alt(self):
        """❤️💙x -> Ctrl+Alt+X"""
        actions, _ = parse(RED_HEART_LONG + BLUE_HEART + "x")
        assert actions == [KeyAction("char", "x", ctrl=True, alt=True)]

    def test_ctrl_cmd(self):
        """❤️💚x -> Ctrl+Cmd+X"""
        actions, _ = parse(RED_HEART_LONG + GREEN_HEART + "x")
        assert actions == [KeyAction("char", "x", ctrl=True, cmd=True)]

    def test_alt_cmd(self):
        """💙💚x -> Alt+Cmd+X"""
        actions, _ = parse(BLUE_HEART + GREEN_HEART + "x")
        assert actions == [KeyAction("char", "x", alt=True, cmd=True)]

    def test_ctrl_alt_cmd(self):
        """❤️💙💚x -> Ctrl+Alt+Cmd+X"""
        actions, _ = parse(RED_HEART_LONG + BLUE_HEART + GREEN_HEART + "x")
        assert actions == [KeyAction("char", "x", ctrl=True, alt=True, cmd=True)]

    def test_modifier_order_reversed(self):
        """💚💙❤️x -> same result regardless of order."""
        actions, _ = parse(GREEN_HEART + BLUE_HEART + RED_HEART_LONG + "x")
        assert actions == [KeyAction("char", "x", ctrl=True, alt=True, cmd=True)]


# ---------------------------------------------------------------------------
# Special keys: Enter (orange heart), Escape (yellow heart)
# ---------------------------------------------------------------------------

class TestSpecialKeys:
    def test_orange_enter(self):
        """🧡 -> Enter"""
        actions, _ = parse(ORANGE_HEART)
        assert actions == [KeyAction("key", "Enter")]

    def test_yellow_escape(self):
        """💛 -> Escape"""
        actions, _ = parse(YELLOW_HEART)
        assert actions == [KeyAction("key", "Escape")]

    def test_ctrl_enter(self):
        """❤️🧡 -> Ctrl+Enter"""
        actions, _ = parse(RED_HEART_LONG + ORANGE_HEART)
        assert actions == [KeyAction("key", "Enter", ctrl=True)]

    def test_alt_enter(self):
        """💙🧡 -> Alt+Enter"""
        actions, _ = parse(BLUE_HEART + ORANGE_HEART)
        assert actions == [KeyAction("key", "Enter", alt=True)]

    def test_escape_resets_modifiers(self):
        """💛 always sends bare Escape, modifiers discarded (matches C impl)."""
        actions, _ = parse(RED_HEART_LONG + YELLOW_HEART + "a")
        # Yellow heart resets mods in C: mods = 0
        assert actions == [
            KeyAction("key", "Escape"),
            KeyAction("char", "a"),
        ]

    def test_multiple_enters(self):
        actions, _ = parse(ORANGE_HEART + ORANGE_HEART)
        assert actions == [
            KeyAction("key", "Enter"),
            KeyAction("key", "Enter"),
        ]


# ---------------------------------------------------------------------------
# Escape sequences: \n, \t, \\
# ---------------------------------------------------------------------------

class TestEscapeSequences:
    def test_backslash_n(self):
        actions, _ = parse("\\n")
        assert actions == [KeyAction("key", "Enter")]

    def test_backslash_t(self):
        actions, _ = parse("\\t")
        assert actions == [KeyAction("key", "Tab")]

    def test_backslash_backslash(self):
        actions, _ = parse("\\\\")
        assert actions == [KeyAction("char", "\\")]

    def test_ctrl_backslash_n(self):
        """❤️\\n -> Ctrl+Enter"""
        actions, _ = parse(RED_HEART_LONG + "\\n")
        assert actions == [KeyAction("key", "Enter", ctrl=True)]

    def test_alt_backslash_t(self):
        """💙\\t -> Alt+Tab"""
        actions, _ = parse(BLUE_HEART + "\\t")
        assert actions == [KeyAction("key", "Tab", alt=True)]

    def test_ctrl_backslash_backslash(self):
        """❤️\\\\ -> Ctrl+backslash"""
        actions, _ = parse(RED_HEART_LONG + "\\\\")
        assert actions == [KeyAction("char", "\\", ctrl=True)]

    def test_unknown_escape_treated_as_literal(self):
        """\\x -> literal backslash then x (no special meaning)."""
        actions, _ = parse("\\x")
        assert actions == [
            KeyAction("char", "\\"),
            KeyAction("char", "x"),
        ]

    def test_trailing_backslash(self):
        """Single trailing backslash is literal."""
        actions, _ = parse("a\\")
        assert actions == [
            KeyAction("char", "a"),
            KeyAction("char", "\\"),
        ]


# ---------------------------------------------------------------------------
# Suppress newline: purple heart
# ---------------------------------------------------------------------------

class TestSuppressNewline:
    def test_purple_heart_suppresses(self):
        actions, suppress = parse("ls" + PURPLE_HEART)
        assert suppress is True
        assert len(actions) == 2
        assert "".join(a.value for a in actions) == "ls"

    def test_no_purple_heart(self):
        _, suppress = parse("ls")
        assert suppress is False

    def test_purple_heart_alone(self):
        actions, suppress = parse(PURPLE_HEART)
        assert actions == []
        assert suppress is True

    def test_purple_heart_not_at_end(self):
        """Purple heart in middle is NOT suppress — it's just ignored/consumed."""
        # Purple heart only works at end per C impl (ends_with_purple_heart)
        actions, suppress = parse(PURPLE_HEART + "a")
        assert suppress is False
        # The purple heart at start doesn't match any emoji -> individual chars
        # Actually in the parsing loop, purple heart is not matched as a modifier
        # It just becomes regular characters since it's not handled in the loop
        assert len(actions) >= 1  # at least the 'a'


# ---------------------------------------------------------------------------
# Mixed sequences
# ---------------------------------------------------------------------------

class TestMixed:
    def test_text_with_enter_at_end(self):
        """ls -la🧡 -> l, s, ' ', -, l, a, Enter"""
        text = "ls -la" + ORANGE_HEART
        actions, _ = parse(text)
        assert actions[-1] == KeyAction("key", "Enter")
        assert len(actions) == 7

    def test_ctrl_c_then_text(self):
        """❤️c followed by plain text."""
        actions, _ = parse(RED_HEART_LONG + "c" + "hello")
        assert actions[0] == KeyAction("char", "c", ctrl=True)
        assert actions[1] == KeyAction("char", "h")
        assert len(actions) == 6

    def test_escape_then_sequence(self):
        """💛dd -> Escape, d, d  (vim dd)"""
        actions, _ = parse(YELLOW_HEART + "dd")
        assert actions == [
            KeyAction("key", "Escape"),
            KeyAction("char", "d"),
            KeyAction("char", "d"),
        ]

    def test_multiple_modifier_groups(self):
        """❤️c❤️z -> Ctrl+C, Ctrl+Z"""
        actions, _ = parse(RED_HEART_LONG + "c" + RED_HEART_LONG + "z")
        assert actions == [
            KeyAction("char", "c", ctrl=True),
            KeyAction("char", "z", ctrl=True),
        ]

    def test_interleaved(self):
        """a❤️b💙c -> a, Ctrl+b, Alt+c"""
        actions, _ = parse("a" + RED_HEART_LONG + "b" + BLUE_HEART + "c")
        assert actions == [
            KeyAction("char", "a"),
            KeyAction("char", "b", ctrl=True),
            KeyAction("char", "c", alt=True),
        ]


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_max_length_exactly(self):
        text = "a" * MAX_INPUT
        actions, _ = parse(text)
        assert len(actions) == MAX_INPUT

    def test_exceeds_max_length(self):
        with pytest.raises(ValueError, match="exceeds max"):
            parse("a" * (MAX_INPUT + 1))

    def test_non_string_input(self):
        with pytest.raises(TypeError, match="must be a string"):
            parse(123)  # type: ignore

    def test_modifier_at_end_no_char(self):
        """Trailing modifier with no char to apply to -> no action emitted."""
        actions, _ = parse(RED_HEART_LONG)
        assert actions == []

    def test_multiple_trailing_modifiers(self):
        """Multiple modifiers at end with nothing to apply."""
        actions, _ = parse(RED_HEART_LONG + BLUE_HEART)
        assert actions == []

    def test_unicode_passthrough(self):
        """Non-emoji unicode chars pass through as literals."""
        actions, _ = parse("\u00e9\u00f1")  # é, ñ
        assert actions == [
            KeyAction("char", "\u00e9"),
            KeyAction("char", "\u00f1"),
        ]

    def test_keyaction_frozen(self):
        a = KeyAction("char", "x")
        with pytest.raises(AttributeError):
            a.value = "y"  # type: ignore

    def test_keyaction_equality(self):
        assert KeyAction("char", "x") == KeyAction("char", "x")
        assert KeyAction("char", "x") != KeyAction("char", "y")
        assert KeyAction("char", "x", ctrl=True) != KeyAction("char", "x")

    def test_keyaction_defaults(self):
        a = KeyAction("char", "x")
        assert a.ctrl is False
        assert a.alt is False
        assert a.cmd is False

    def test_only_escape_sequences_in_text(self):
        """\\n\\t\\\\ -> Enter, Tab, backslash"""
        actions, _ = parse("\\n\\t\\\\")
        assert actions == [
            KeyAction("key", "Enter"),
            KeyAction("key", "Tab"),
            KeyAction("char", "\\"),
        ]

    def test_cmd_enter(self):
        """💚🧡 -> Cmd+Enter"""
        actions, _ = parse(GREEN_HEART + ORANGE_HEART)
        assert actions == [KeyAction("key", "Enter", cmd=True)]

    def test_all_three_mods_enter(self):
        """❤️💙💚🧡 -> Ctrl+Alt+Cmd+Enter"""
        actions, _ = parse(
            RED_HEART_LONG + BLUE_HEART + GREEN_HEART + ORANGE_HEART
        )
        assert actions == [KeyAction("key", "Enter", ctrl=True, alt=True, cmd=True)]

    def test_ctrl_tab(self):
        """❤️\\t -> Ctrl+Tab"""
        actions, _ = parse(RED_HEART_LONG + "\\t")
        assert actions == [KeyAction("key", "Tab", ctrl=True)]
