"""Markdown emission helpers shared by every renderer.

Centralising table emission here is what keeps the bundle ``markdownlint``-clean
without each renderer having to remember the rules. Two of them bite constantly:

* A raw ``|`` in a cell opens an unintended column, and a raw newline splits the
  row across physical lines. Both are escaped or collapsed by
  :func:`escape_table_cell`.
* ``MD031`` and ``MD058`` want a blank line around fenced blocks and tables,
  while ``MD012`` forbids two. :func:`join_blocks` gives exactly one.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence

_LINE_BREAK_RE = re.compile(r"[\r\n]+")


def escape_table_cell(text: str) -> str:
    """Escape a value so it is safe between two table pipes.

    Runs of CR/LF collapse to a single space so a multi-line value stays on one
    row, and pipes are backslash-escaped so they cannot open a column. A space
    is used rather than an HTML ``<br>`` so the bundle stays plain Markdown for
    text-oriented consumers.

    Args:
        text: The raw cell text.

    Returns:
        The escaped cell text.
    """
    return _LINE_BREAK_RE.sub(" ", str(text)).replace("|", "\\|")


def render_table(headers: Sequence[str], rows: Iterable[Sequence[str]]) -> str:
    """Render a compact, lint-clean Markdown table.

    Cells are escaped here, so callers pass raw text and never pre-escape. A
    non-empty cell is padded with one space on each side; an empty cell renders
    as a single space between pipes, which is what ``MD060``'s compact style
    requires.

    Args:
        headers: Column header labels.
        rows: Rows of cells, each aligned to ``headers``.

    Returns:
        The table with no trailing newline.
    """
    lines = [_render_row(headers), _render_separator(len(headers))]
    lines.extend(_render_row(row) for row in rows)
    return "\n".join(lines)


def _render_row(cells: Sequence[str]) -> str:
    """Render one table row."""
    return "|" + "|".join(_render_cell(cell) for cell in cells) + "|"


def _render_cell(cell: str) -> str:
    """Return a padded cell, or a single space when the value is empty."""
    escaped = escape_table_cell("" if cell is None else str(cell))
    return f" {escaped} " if escaped else " "


def _render_separator(column_count: int) -> str:
    """Render the header separator row."""
    return "|" + "|".join(" --- " for _ in range(column_count)) + "|"


def render_code_block(source: str, language: str = "sql") -> str:
    """Render a fenced code block, or an empty string for empty source.

    The fence is widened past any run of backticks inside the source, so source
    text that itself contains a fence cannot terminate the block early.

    Args:
        source: The code to fence.
        language: The info string for the opening fence.

    Returns:
        The fenced block, or an empty string.
    """
    text = (source or "").strip()
    if not text:
        return ""
    fence = "`" * max(3, _longest_backtick_run(text) + 1)
    return f"{fence}{language}\n{text}\n{fence}"


def _longest_backtick_run(text: str) -> int:
    """Return the length of the longest run of backticks in ``text``."""
    return max((len(run) for run in re.findall(r"`+", text)), default=0)


def join_blocks(*blocks: str) -> str:
    """Join non-empty blocks with exactly one blank line between them.

    Args:
        *blocks: Markdown blocks; empty ones are dropped.

    Returns:
        The joined Markdown.
    """
    cleaned = [block.strip() for block in blocks]
    return "\n\n".join(block for block in cleaned if block)


def inline_code(text: str) -> str:
    """Wrap a value in backticks for inline display, collapsing whitespace."""
    collapsed = " ".join(str(text).split())
    return f"`{collapsed}`" if collapsed else ""
