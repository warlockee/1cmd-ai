"""Tests for terminal type detection (AI vs shell vs REPL)."""

import pytest

from onecmd.manager.tools import detect_terminal_type


# ---------------------------------------------------------------------------
# AI tool detection — from process name / title
# ---------------------------------------------------------------------------

class TestAIDetectionByName:
    def test_claude_process(self):
        assert detect_terminal_type("claude", "") == ("ai", "claude")

    def test_claude_in_title(self):
        assert detect_terminal_type("node", "Claude Code") == ("ai", "claude")

    def test_gemini_process(self):
        assert detect_terminal_type("gemini", "") == ("ai", "gemini")

    def test_codex_process(self):
        assert detect_terminal_type("codex", "") == ("ai", "codex")

    def test_aider_process(self):
        assert detect_terminal_type("aider", "") == ("ai", "aider")

    def test_cursor_in_title(self):
        assert detect_terminal_type("node", "Cursor Agent") == ("ai", "cursor")

    def test_copilot_process(self):
        assert detect_terminal_type("copilot", "workspace") == ("ai", "copilot")

    def test_case_insensitive(self):
        assert detect_terminal_type("CLAUDE", "")[0] == "ai"
        assert detect_terminal_type("node", "GEMINI CLI")[0] == "ai"


# ---------------------------------------------------------------------------
# Shell detection
# ---------------------------------------------------------------------------

class TestShellDetection:
    def test_bash(self):
        assert detect_terminal_type("bash", "") == ("shell", "bash")

    def test_zsh(self):
        assert detect_terminal_type("zsh", "") == ("shell", "zsh")

    def test_fish(self):
        assert detect_terminal_type("fish", "") == ("shell", "fish")

    def test_dash(self):
        assert detect_terminal_type("dash", "") == ("shell", "dash")

    def test_shell_with_path(self):
        assert detect_terminal_type("/bin/bash", "")[0] == "shell"

    def test_shell_with_args(self):
        assert detect_terminal_type("bash --login", "")[0] == "shell"


# ---------------------------------------------------------------------------
# REPL detection
# ---------------------------------------------------------------------------

class TestREPLDetection:
    def test_python(self):
        assert detect_terminal_type("python3", "") == ("repl", "python")

    def test_ipython(self):
        assert detect_terminal_type("ipython", "") == ("repl", "python")

    def test_node(self):
        assert detect_terminal_type("node", "") == ("repl", "node")

    def test_psql(self):
        assert detect_terminal_type("psql", "") == ("repl", "psql")

    def test_mysql(self):
        assert detect_terminal_type("mysql", "mydb") == ("repl", "mysql")

    def test_redis_cli(self):
        assert detect_terminal_type("redis-cli", "") == ("repl", "redis")


# ---------------------------------------------------------------------------
# AI detection from terminal output (shell process but AI tool running inside)
# ---------------------------------------------------------------------------

class TestAIDetectionByOutput:
    def test_claude_box_drawing_in_bash(self):
        output = "some stuff\n╭─ some header\n│ content\n╰─ footer"
        ttype, detail = detect_terminal_type("bash", "", output)
        assert ttype == "ai"

    def test_claude_indicator_in_zsh(self):
        output = "loading...\n⏺ Reading file.py"
        ttype, detail = detect_terminal_type("zsh", "", output)
        assert ttype == "ai"

    def test_codex_indicator(self):
        output = "some text\n✦ Working on task..."
        ttype, detail = detect_terminal_type("bash", "", output)
        assert ttype == "ai"

    def test_claude_name_in_output(self):
        # "Claude Code" is specific enough to detect
        output = "Welcome to Claude Code\nType your request..."
        ttype, detail = detect_terminal_type("bash", "", output)
        assert ttype == "ai"
        assert detail == "claude"

    def test_gemini_name_in_output(self):
        # "Gemini CLI" is specific enough to detect
        output = "Gemini CLI v1.0\n> what should I do?"
        ttype, detail = detect_terminal_type("bash", "", output)
        assert ttype == "ai"
        assert detail == "gemini"

    def test_casual_mention_no_false_positive(self):
        # Just mentioning "claude" in a git message shouldn't trigger AI detection
        output = "$ git log\ncommit abc123\n  fix: update claude api key\n$ "
        assert detect_terminal_type("bash", "", output) == ("shell", "bash")

    def test_plain_bash_no_ai_output(self):
        output = "$ ls\nfile1.txt  file2.txt\n$ "
        assert detect_terminal_type("bash", "", output) == ("shell", "bash")

    def test_only_checks_last_2000_chars(self):
        # AI indicators far in the past shouldn't trigger
        old_output = "⏺ old stuff\n" + "x\n" * 2000
        assert detect_terminal_type("bash", "", old_output) == ("shell", "bash")


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_empty_name(self):
        ttype, _ = detect_terminal_type("", "")
        assert ttype == "shell"

    def test_none_output(self):
        # Should not crash with None output
        assert detect_terminal_type("bash", "", None) == ("shell", "bash")

    def test_unknown_process(self):
        ttype, _ = detect_terminal_type("vim", "")
        assert ttype == "shell"

    def test_ai_takes_priority_over_node_repl(self):
        # node could be a REPL, but if title says "Claude" it's AI
        assert detect_terminal_type("node", "Claude Code") == ("ai", "claude")
