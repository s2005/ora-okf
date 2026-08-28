"""Tests for the shared Markdown emission helpers."""

from __future__ import annotations

from ora_okf.okf.markdown import escape_table_cell, inline_code, join_blocks, render_code_block, render_table


class TestEscapeTableCell:
    def test_pipe_is_backslash_escaped(self):
        assert escape_table_cell("a|b") == "a\\|b"

    def test_newline_collapses_to_a_space(self):
        assert escape_table_cell("a\nb") == "a b"

    def test_crlf_collapses_to_a_single_space(self):
        assert escape_table_cell("a\r\nb") == "a b"


class TestRenderTable:
    def test_empty_cell_renders_as_one_space(self):
        table = render_table(["A", "B"], [["x", ""]])
        rows = table.splitlines()
        assert rows[2] == "| x | |"

    def test_non_empty_cell_is_padded(self):
        table = render_table(["A"], [["x"]])
        assert table.splitlines()[2] == "| x |"

    def test_separator_row_has_one_dash_group_per_column(self):
        table = render_table(["A", "B", "C"], [])
        separator = table.splitlines()[1]
        assert separator == "| --- | --- | --- |"

    def test_header_row_matches_headers(self):
        table = render_table(["A", "B"], [])
        assert table.splitlines()[0] == "| A | B |"


class TestRenderCodeBlock:
    def test_empty_source_returns_empty_string(self):
        assert render_code_block("") == ""

    def test_whitespace_only_source_returns_empty_string(self):
        assert render_code_block("   \n  ") == ""

    def test_widens_fence_past_a_backtick_run_in_source(self):
        source = "SELECT '```' FROM DUAL"
        block = render_code_block(source, "sql")
        lines = block.splitlines()
        assert lines[0] == "````sql"
        assert lines[-1] == "````"

    def test_default_fence_is_three_backticks(self):
        block = render_code_block("SELECT 1 FROM DUAL", "sql")
        assert block.startswith("```sql\n")
        assert block.endswith("\n```")


class TestJoinBlocks:
    def test_drops_empty_blocks(self):
        assert join_blocks("a", "", "  ", "b") == "a\n\nb"

    def test_exactly_one_blank_line_between_blocks(self):
        joined = join_blocks("first", "second", "third")
        assert joined == "first\n\nsecond\n\nthird"

    def test_all_empty_returns_empty_string(self):
        assert join_blocks("", "  ", "") == ""


class TestInlineCode:
    def test_collapses_whitespace(self):
        assert inline_code("a   b\nc") == "`a b c`"

    def test_empty_input_returns_empty_string(self):
        assert inline_code("") == ""

    def test_whitespace_only_input_returns_empty_string(self):
        assert inline_code("   ") == ""
