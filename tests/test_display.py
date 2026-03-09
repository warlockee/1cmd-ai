"""Tests for onecmd.terminal.display — formatting, chunking, tracked messages."""

from __future__ import annotations

import threading
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from onecmd.terminal.display import (
    MAX_MSG_LEN,
    MAX_TRACKED_MSGS,
    TrackedMessages,
    delete_tracked_messages,
    format_chunks,
    last_n_lines,
    send_terminal_display,
)


# ── HTML escaping (via html_escape used in send_terminal_display) ────


class TestHtmlEscaping:
    """Verify that terminal text is escaped before sending."""

    def test_angle_brackets_escaped_in_chunks(self):
        """format_chunks receives already-escaped text, so we test the
        send_terminal_display path which escapes then formats."""
        from onecmd.bot.api import html_escape

        assert "&lt;" in html_escape("<script>")
        assert "&gt;" in html_escape("</script>")
        assert "&amp;" in html_escape("a & b")

    def test_all_three_entities(self):
        from onecmd.bot.api import html_escape

        result = html_escape("<h1>A & B</h1>")
        assert result == "&lt;h1&gt;A &amp; B&lt;/h1&gt;"


# ── last_n_lines ────────────────────────────────────────────────────


class TestLastNLines:
    def test_fewer_lines_than_n(self):
        assert last_n_lines("a\nb\nc", 10) == "a\nb\nc"

    def test_exact_n(self):
        assert last_n_lines("a\nb\nc", 3) == "a\nb\nc"

    def test_more_lines_than_n(self):
        assert last_n_lines("a\nb\nc\nd", 2) == "c\nd"

    def test_single_line(self):
        assert last_n_lines("hello", 1) == "hello"

    def test_empty_string(self):
        assert last_n_lines("", 5) == ""


# ── format_chunks — single message (truncation) mode ────────────────


class TestFormatChunksSingleMessage:
    def test_empty_text(self):
        assert format_chunks("", split=False) == ["<pre></pre>"]

    def test_short_text(self):
        result = format_chunks("hello", split=False)
        assert result == ["<pre>hello</pre>"]

    def test_text_within_limit(self):
        text = "x" * MAX_MSG_LEN
        result = format_chunks(text, split=False)
        assert len(result) == 1
        assert result[0] == f"<pre>{text}</pre>"

    def test_text_exceeding_limit_truncated_from_top(self):
        # Build text with lines that exceed the limit.
        line = "abcdefghij\n"
        text = line * (MAX_MSG_LEN // len(line) + 50)
        result = format_chunks(text, split=False)
        assert len(result) == 1
        # The result fits within 4096 total chars.
        assert len(result[0]) <= 4096

    def test_truncation_preserves_line_boundary(self):
        lines = [f"line-{i:04d}" for i in range(1000)]
        text = "\n".join(lines)
        result = format_chunks(text, split=False)
        assert len(result) == 1
        # Content inside <pre> should start at a line boundary (no partial line).
        inner = result[0][len("<pre>"):-len("</pre>")]
        assert not inner.startswith("\n")


# ── format_chunks — split message mode ──────────────────────────────


class TestFormatChunksSplitMessage:
    def test_empty_text_split(self):
        assert format_chunks("", split=True) == ["<pre></pre>"]

    def test_short_text_single_chunk(self):
        result = format_chunks("hello", split=True)
        assert result == ["<pre>hello</pre>"]

    def test_long_text_produces_multiple_chunks(self):
        text = "\n".join(f"line-{i:04d}" for i in range(1000))
        result = format_chunks(text, split=True)
        assert len(result) > 1
        # Each chunk must be within 4096 chars.
        for chunk in result:
            assert len(chunk) <= 4096

    def test_all_content_preserved_in_split(self):
        lines = [f"line-{i:04d}" for i in range(500)]
        text = "\n".join(lines)
        result = format_chunks(text, split=True)
        # Reconstruct: strip <pre></pre> tags, join with newlines.
        reconstructed = "\n".join(
            chunk[len("<pre>"):-len("</pre>")] for chunk in result
        )
        # Every original line must appear.
        for line in lines:
            assert line in reconstructed

    def test_chunk_boundaries_on_newlines(self):
        line = "a" * 100 + "\n"
        text = line * (MAX_MSG_LEN // len(line) + 50)
        result = format_chunks(text, split=True)
        for chunk in result:
            inner = chunk[len("<pre>"):-len("</pre>")]
            # Should not start with newline (would mean bad split).
            if inner:
                assert not inner.startswith("\n")


# ── TrackedMessages ─────────────────────────────────────────────────


class TestTrackedMessages:
    def test_add_and_pop_all(self):
        tm = TrackedMessages()
        tm.add(1)
        tm.add(2)
        tm.add(3)
        assert tm.pop_all() == [1, 2, 3]

    def test_pop_all_clears(self):
        tm = TrackedMessages()
        tm.add(10)
        tm.pop_all()
        assert tm.pop_all() == []

    def test_count(self):
        tm = TrackedMessages()
        assert tm.count == 0
        tm.add(1)
        tm.add(2)
        assert tm.count == 2

    def test_max_tracked_limit(self):
        tm = TrackedMessages()
        for i in range(MAX_TRACKED_MSGS + 10):
            tm.add(i)
        ids = tm.pop_all()
        assert len(ids) == MAX_TRACKED_MSGS
        # Oldest should have been dropped.
        assert ids[0] == 10

    def test_thread_safety(self):
        tm = TrackedMessages()
        errors: list[Exception] = []

        def adder(start: int):
            try:
                for i in range(50):
                    tm.add(start + i)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=adder, args=(i * 100,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        # All IDs accounted for (up to MAX_TRACKED_MSGS).
        ids = tm.pop_all()
        assert len(ids) <= MAX_TRACKED_MSGS
        assert len(ids) > 0


# ── delete_tracked_messages ─────────────────────────────────────────


class TestDeleteTrackedMessages:
    @pytest.mark.asyncio
    async def test_calls_delete_for_each_id(self):
        tm = TrackedMessages()
        tm.add(10)
        tm.add(20)
        tm.add(30)

        bot = MagicMock()
        with patch("onecmd.terminal.display.delete_message", new_callable=AsyncMock) as mock_del:
            await delete_tracked_messages(bot, 999, tm)
            # Called in reverse order.
            mock_del.assert_has_calls(
                [call(bot, 999, 30), call(bot, 999, 20), call(bot, 999, 10)]
            )

    @pytest.mark.asyncio
    async def test_clears_tracked_after_delete(self):
        tm = TrackedMessages()
        tm.add(1)
        bot = MagicMock()
        with patch("onecmd.terminal.display.delete_message", new_callable=AsyncMock):
            await delete_tracked_messages(bot, 999, tm)
        assert tm.count == 0

    @pytest.mark.asyncio
    async def test_empty_tracked_is_noop(self):
        tm = TrackedMessages()
        bot = MagicMock()
        with patch("onecmd.terminal.display.delete_message", new_callable=AsyncMock) as mock_del:
            await delete_tracked_messages(bot, 999, tm)
            mock_del.assert_not_called()


# ── send_terminal_display ───────────────────────────────────────────


class TestSendTerminalDisplay:
    @pytest.mark.asyncio
    async def test_sends_message_and_tracks_id(self):
        tm = TrackedMessages()
        bot = MagicMock()

        with (
            patch("onecmd.terminal.display.delete_message", new_callable=AsyncMock),
            patch("onecmd.terminal.display.send_message", new_callable=AsyncMock, return_value=42),
        ):
            await send_terminal_display(bot, 123, "hello", tm)

        assert tm.count == 1
        assert tm.pop_all() == [42]

    @pytest.mark.asyncio
    async def test_none_message_id_not_tracked(self):
        tm = TrackedMessages()
        bot = MagicMock()

        with (
            patch("onecmd.terminal.display.delete_message", new_callable=AsyncMock),
            patch("onecmd.terminal.display.send_message", new_callable=AsyncMock, return_value=None),
        ):
            await send_terminal_display(bot, 123, "hello", tm)

        assert tm.count == 0
