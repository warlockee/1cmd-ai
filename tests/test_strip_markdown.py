"""Tests for strip_markdown — ensures Telegram-safe plain text output."""

from onecmd.manager.agent import strip_markdown


class TestStripMarkdown:
    def test_bold(self):
        assert strip_markdown("**bold text**") == "bold text"

    def test_italic_star(self):
        assert strip_markdown("*italic*") == "italic"

    def test_italic_underscore(self):
        assert strip_markdown("_italic_") == "italic"

    def test_inline_code(self):
        assert strip_markdown("`code`") == "code"

    def test_code_block(self):
        assert strip_markdown("```python\nprint('hi')\n```") == "python\nprint('hi')\n"

    def test_code_block_no_lang(self):
        assert strip_markdown("```\nprint('hi')\n```") == "print('hi')\n"

    def test_heading(self):
        assert strip_markdown("## Heading") == "Heading"

    def test_heading_h1(self):
        assert strip_markdown("# Title") == "Title"

    def test_mixed(self):
        text = "**Terminal 0** — running `npm start`"
        result = strip_markdown(text)
        assert "**" not in result
        assert "`" not in result
        assert "Terminal 0" in result
        assert "npm start" in result

    def test_preserves_plain_text(self):
        text = "No terminals found."
        assert strip_markdown(text) == text

    def test_preserves_underscores_in_words(self):
        # file_name_here should NOT be treated as italic
        assert strip_markdown("my_file_name") == "my_file_name"

    def test_nested_bold_italic(self):
        assert strip_markdown("***bold italic***") == "bold italic"

    def test_multiline_headings(self):
        text = "## Summary\nsome content\n### Details\nmore content"
        result = strip_markdown(text)
        assert "#" not in result
        assert "Summary" in result
        assert "Details" in result

    def test_empty_string(self):
        assert strip_markdown("") == ""

    def test_multiple_code_blocks(self):
        text = "first ```a``` and ```b``` end"
        result = strip_markdown(text)
        assert "```" not in result
