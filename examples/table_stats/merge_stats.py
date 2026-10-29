"""Merge USER_TABLES optimizer statistics (table_stats.sql JSON) into OKF table concepts.

Reads the JSON document written by table_stats.sql and adds the optimizer statistics
(num_rows, avg_row_len, blocks, sample_size, last_analyzed) as flat top-level frontmatter
keys of the matching tables/<name>.md files in an ora-okf bundle, in the same form
`ora-okf --include-data` writes them. Files are matched by their `title:` frontmatter
key, which holds the exact table name, so concept file names never have to be derived.
If two files carry the same title, neither is changed and both are reported. The script
uses only the Python standard library, so it runs outside the project environment.
Running it twice changes nothing.

Usage:
    python merge_stats.py --stats-json <file> --okf-dir <dir> [--dry-run]

Tables never analysed (null num_rows) and global temporary tables are skipped.
"""

import argparse
import json
import re
from pathlib import Path
from typing import Any

STAT_KEYS = ("num_rows", "avg_row_len", "blocks", "sample_size", "last_analyzed")

# Earlier versions of this script wrote a nested block; it is removed so such bundles are cleaned
_OLD_BLOCK_RE = re.compile(r"^statistics:\n(?:[ \t]+.*\n)*", re.M)
_FRONTMATTER_RE = re.compile(r"\A---\n(.*?\n)---\n", re.S)
_TITLE_RE = re.compile(r"^title:[ \t]*(.*)$", re.M)


def unquote(value: str) -> str:
    """Return the string a YAML scalar written by yaml.safe_dump stands for.

    Args:
        value: The scalar text after "title:".

    Returns:
        The decoded string: single-quoted values have doubled quotes folded, double-quoted
        values are decoded as JSON, plain values are returned as they are.
    """
    value = value.strip()
    if len(value) >= 2 and value[0] == "'" and value[-1] == "'":
        return value[1:-1].replace("''", "'")
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        try:
            decoded = json.loads(value)
        except ValueError:
            return value
        return decoded if isinstance(decoded, str) else value
    return value


def read_title(md_text: str) -> str | None:
    """Return the top-level title of a concept file, or None when it has none.

    Args:
        md_text: The concept file content.

    Returns:
        The decoded title, or None if there is no frontmatter or no title key.
    """
    match = _FRONTMATTER_RE.match(md_text)
    if not match:
        return None
    # Top-level keys start in column 0, so the anchored pattern skips nested ones
    title = _TITLE_RE.search(match.group(1))
    return unquote(title.group(1)) if title else None


def index_by_title(tables_dir: Path) -> tuple[dict[str, Path], dict[str, list[Path]]]:
    """Index the concept files of a directory by title.

    Args:
        tables_dir: The bundle's tables directory (may not exist).

    Returns:
        A pair: titles used by exactly one file mapped to that file, and titles used by
        several files mapped to all of them.
    """
    found: dict[str, list[Path]] = {}
    for path in sorted(tables_dir.glob("*.md")):
        title = read_title(path.read_text(encoding="utf-8"))
        if title is not None:
            found.setdefault(title, []).append(path)
    unique = {t: paths[0] for t, paths in found.items() if len(paths) == 1}
    duplicates = {t: paths for t, paths in found.items() if len(paths) > 1}
    return unique, duplicates


def build_lines(table: dict[str, Any]) -> str:
    """Return the flat frontmatter lines for one table, formatted as yaml.safe_dump writes them.

    Args:
        table: One entry of the JSON "tables" list.

    Returns:
        One "key: value" line per non-null statistic: ints bare, the timestamp single-quoted.
    """
    lines = []
    for key in STAT_KEYS:
        value = table.get(key)
        if value is None:
            continue
        if isinstance(value, str):
            lines.append("{}: '{}'\n".format(key, value.replace("'", "''")))
        else:
            lines.append(f"{key}: {value}\n")
    return "".join(lines)


def merge(md_text: str, lines: str) -> str:
    """Return ``md_text`` with its statistics keys replaced by ``lines`` at the end of the frontmatter.

    Args:
        md_text: The concept file content.
        lines: The flat statistics lines from :func:`build_lines`.

    Returns:
        The updated content. The statistics keys are the last frontmatter keys of a
        non-temporary table, so they are appended just before the closing "---".

    Raises:
        ValueError: If the content has no frontmatter.
    """
    match = _FRONTMATTER_RE.match(md_text)
    if not match:
        raise ValueError("no frontmatter")
    front = _OLD_BLOCK_RE.sub("", match.group(1))
    for key in STAT_KEYS:
        front = re.sub(rf"^{key}:.*\n", "", front, flags=re.M)
    return "---\n" + front + lines + "---\n" + md_text[match.end() :]


def main() -> None:
    """Parse the command line and merge the statistics into the bundle."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--stats-json", required=True, type=Path, help="JSON file written by table_stats.sql")
    parser.add_argument("--okf-dir", required=True, type=Path, help="root directory of the ora-okf bundle")
    parser.add_argument("--dry-run", action="store_true", help="report what would change without writing files")
    args = parser.parse_args()

    doc = json.loads(args.stats_json.read_text(encoding="utf-8"))
    by_title, duplicates = index_by_title(args.okf_dir / "tables")
    updated, unchanged, no_stats, temporary, no_file, ambiguous = [], [], [], [], [], []
    for table in doc["tables"]:
        name = table["table_name"]
        if name in duplicates:
            ambiguous.append(name)
            continue
        path = by_title.get(name)
        if path is None:
            no_file.append(name)
            continue
        # The renderer gives global temporary tables no statistics, so none are merged
        if table.get("temporary") == "Y":
            temporary.append(name)
            continue
        if table.get("num_rows") is None:
            no_stats.append(name)
            continue
        text = path.read_text(encoding="utf-8")
        new_text = merge(text, build_lines(table))
        if new_text == text:
            unchanged.append(name)
            continue
        if not args.dry_run:
            # write_bytes keeps the LF line endings the bundle was written with
            path.write_bytes(new_text.encode("utf-8"))
        updated.append(name)

    in_json = {t["table_name"] for t in doc["tables"]}
    untouched = sorted(
        [p.name for title, p in by_title.items() if title not in in_json]
        + [p.name for title, paths in duplicates.items() if title not in in_json for p in paths]
    )
    print(f"{'would update' if args.dry_run else 'updated'}: {len(updated)}")
    print(f"unchanged: {len(unchanged)}")
    print(f"skipped, never analysed: {no_stats}")
    print(f"skipped, global temporary: {temporary}")
    print(f"skipped, no OKF file: {len(no_file)}")
    print(f"skipped, title shared by several files: {ambiguous}")
    for title, paths in sorted(duplicates.items()):
        print(f"duplicate title {title!r}: {sorted(p.name for p in paths)}")
    print(f"OKF tables absent from JSON: {untouched}")


if __name__ == "__main__":
    main()
