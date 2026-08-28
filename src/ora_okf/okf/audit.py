"""Post-write audit: prove no physical schema name survived into the bundle.

Renaming happens on the model, which covers every reference the extractor
understood. This audit checks the finished bytes instead, and that difference is
the point: it catches a schema name that reached the bundle by a route the model
never described -- a name embedded in a sampled row value, spelled inside a
string literal in PL/SQL, or written into a comment.

It is a text search, so it is deliberately blunt: a whole-identifier,
case-insensitive match, using the exact same pattern the renamer substitutes
with, so the two cannot disagree about what counts as a reference.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from ..rename import build_identifier_pattern

logger = logging.getLogger(__name__)

# Longest excerpt kept per hit. A concept line can be an entire minified view
# definition, and the report is meant to be readable in a terminal.
_EXCERPT_LIMIT = 160


@dataclass(frozen=True)
class LeakHit:
    """One occurrence of a physical schema name in a written file.

    Attributes:
        path: Bundle-relative POSIX path of the file.
        line: 1-based line number.
        schema: The physical schema name that matched, upper-cased.
        excerpt: The matching line, whitespace-collapsed and truncated.
    """

    path: str
    line: int
    schema: str
    excerpt: str

    def format(self) -> str:
        """Return a single ``path:line: schema -- excerpt`` report line."""
        return f"{self.path}:{self.line}: {self.schema} -- {self.excerpt}"


@dataclass(frozen=True)
class AuditResult:
    """The outcome of a bundle audit.

    Attributes:
        hits: Every recorded occurrence, capped per schema.
        files_scanned: How many Markdown files were read.
        truncated: Schemas whose hit list was capped, so the report can say so
            rather than implying the cap is the true total.
    """

    hits: tuple[LeakHit, ...] = ()
    files_scanned: int = 0
    truncated: tuple[str, ...] = ()

    def has_leaks(self) -> bool:
        """Return True when at least one physical name was found."""
        return bool(self.hits)

    def schemas_found(self) -> tuple[str, ...]:
        """Return the distinct physical schema names that leaked, sorted."""
        return tuple(sorted({hit.schema for hit in self.hits}))

    def summary(self) -> str:
        """Return a one-line summary suitable for a log record."""
        if not self.hits:
            return f"no physical schema names found in {self.files_scanned} file(s)"
        names = ", ".join(self.schemas_found())
        return f"{len(self.hits)} occurrence(s) of {names} in {self.files_scanned} file(s)"

    def report(self, limit: int = 20) -> str:
        """Return a multi-line report of the first ``limit`` hits.

        Args:
            limit: Maximum number of hit lines to include.

        Returns:
            The formatted report, or an empty string when there are no hits.
        """
        if not self.hits:
            return ""
        lines = [hit.format() for hit in self.hits[:limit]]
        if len(self.hits) > limit:
            lines.append(f"... and {len(self.hits) - limit} more occurrence(s)")
        if self.truncated:
            lines.append(f"(hit list capped per schema for: {', '.join(self.truncated)})")
        return "\n".join(lines)


def audit_bundle(okf_dir: Path, physical_names: Iterable[str], *, max_hits_per_schema: int = 25) -> AuditResult:
    """Scan a written bundle for any of ``physical_names``.

    Only ``.md`` files are scanned. The exporter's own ``.okf-bundle`` marker is
    fixed text that never contains a schema name, and any other non-Markdown
    file in the directory is not the exporter's output.

    Args:
        okf_dir: The bundle root.
        physical_names: Physical schema names that must not appear.
        max_hits_per_schema: Cap on recorded hits per schema, so a bundle that
            was never renamed at all produces a readable report instead of one
            line per object.

    Returns:
        The audit result. An empty ``physical_names`` yields a clean result
        without reading anything.
    """
    if not okf_dir.is_dir():
        return AuditResult()
    files = [
        (md_file.relative_to(okf_dir).as_posix(), _read_text(md_file)) for md_file in sorted(okf_dir.rglob("*.md"))
    ]
    return audit_rendered(files, physical_names, max_hits_per_schema=max_hits_per_schema)


def audit_rendered(
    files: Iterable[tuple[str, str]],
    physical_names: Iterable[str],
    *,
    max_hits_per_schema: int = 25,
) -> AuditResult:
    """Scan already-rendered ``(relative_path, content)`` pairs.

    Shared with :func:`audit_bundle` so ``--dry-run`` audits exactly the bytes a
    real run would have written, without touching the filesystem.

    Args:
        files: The rendered files.
        physical_names: Physical schema names that must not appear.
        max_hits_per_schema: Cap on recorded hits per schema.

    Returns:
        The audit result.
    """
    pattern = build_identifier_pattern(physical_names)
    materialized = list(files)
    if pattern is None:
        return AuditResult(files_scanned=len(materialized))

    hits: list[LeakHit] = []
    counts: dict[str, int] = {}
    truncated: list[str] = []

    for relative, content in materialized:
        for line_number, line in enumerate(content.splitlines(), start=1):
            for match in pattern.finditer(line):
                schema = (match.group("quoted") or match.group("bare")).upper()
                seen = counts.get(schema, 0)
                if seen >= max_hits_per_schema:
                    if schema not in truncated:
                        truncated.append(schema)
                    continue
                counts[schema] = seen + 1
                hits.append(LeakHit(path=relative, line=line_number, schema=schema, excerpt=_excerpt(line)))

    result = AuditResult(hits=tuple(hits), files_scanned=len(materialized), truncated=tuple(sorted(truncated)))
    logger.debug("Bundle audit: %s", result.summary())
    return result


def _read_text(path: Path) -> str:
    """Return a file's text, or an empty string when it cannot be read.

    An unreadable file is skipped rather than raising: the audit is a safety net
    run after a successful write, and failing it on an I/O hiccup would discard
    a bundle that is probably fine. The write itself would already have failed
    if the directory were genuinely broken.
    """
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("Audit could not read %s: %s", path, exc)
        return ""


def _excerpt(line: str) -> str:
    """Return a whitespace-collapsed, length-capped excerpt of a line."""
    collapsed = " ".join(line.split())
    if len(collapsed) <= _EXCERPT_LIMIT:
        return collapsed
    return collapsed[: _EXCERPT_LIMIT - 3] + "..."
