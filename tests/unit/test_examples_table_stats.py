"""Tests for examples/table_stats/merge_stats.py, run as ``__main__`` with a real command line."""

from __future__ import annotations

import dataclasses
import json
import runpy
import sys
from pathlib import Path
from typing import Any

from ora_okf.model import SchemaModel, Table
from ora_okf.okf.concept import parse_frontmatter
from ora_okf.okf.renderers import RenderConfig, render_bundle
from tests.conftest import build_sample_model

SCRIPT = Path(__file__).resolve().parents[2] / "examples" / "table_stats" / "merge_stats.py"
STAT_KEYS = ("num_rows", "avg_row_len", "blocks", "sample_size", "last_analyzed")
STATISTICS = {
    "num_rows": 1500,
    "avg_row_len": 42,
    "blocks": 12,
    "sample_size": 1500,
    "last_analyzed": "2026-10-01T12:00:05",
}


def _render(model: SchemaModel) -> dict[str, str]:
    """Render the model under --include-data and return {path: content}."""
    return dict(render_bundle(model, RenderConfig(schema_label="APP", include_data=True)))


def _models() -> tuple[SchemaModel, SchemaModel]:
    """Return the sample model without statistics and the same model with them on ORDERS."""
    model_without = build_sample_model()
    orders = dataclasses.replace(model_without.tables[0], **STATISTICS)
    model_with = dataclasses.replace(model_without, tables=(orders, *model_without.tables[1:]))
    return model_without, model_with


def _write_bundle(root: Path, files: dict[str, str]) -> None:
    """Write rendered files under ``root`` keeping LF line endings."""
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content.encode("utf-8"))


def _snapshot(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def _stats_json(tmp_path: Path) -> Path:
    """Write a JSON fixture in the format table_stats.sql emits."""
    document = {
        "metadata": {"schema": "APP", "source": "USER_TABLES", "table_count": 3},
        "tables": [
            {"table_name": "ORDERS", **STATISTICS, "temporary": "N"},
            {
                "table_name": "CUSTOMERS",
                "num_rows": 9,
                "avg_row_len": 1,
                "blocks": 1,
                "sample_size": 9,
                "last_analyzed": "2026-10-01T12:00:00",
                "temporary": "Y",
            },
            {
                "table_name": "NEWT",
                "num_rows": None,
                "avg_row_len": None,
                "blocks": None,
                "sample_size": None,
                "last_analyzed": None,
                "temporary": "N",
            },
        ],
    }
    path = tmp_path / "table_stats.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def _run(monkeypatch, capsys, *args: str) -> tuple[int, str]:
    """Run merge_stats.py as ``__main__`` with ``args``; return its exit code and stdout.

    runpy keeps the script's own argparse and print behaviour but avoids a subprocess call,
    which the project's bandit rule (S603) flags.
    """
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), *args])
    code = 0
    try:
        runpy.run_path(str(SCRIPT), run_name="__main__")
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
    return code, capsys.readouterr().out


def _merge(monkeypatch, capsys, stats_json: Path, okf_dir: Path, *extra: str) -> tuple[int, str]:
    return _run(monkeypatch, capsys, "--stats-json", str(stats_json), "--okf-dir", str(okf_dir), *extra)


def test_help_lists_the_named_options(monkeypatch, capsys):
    code, out = _run(monkeypatch, capsys, "--help")
    assert code == 0
    for option in ("--stats-json", "--okf-dir", "--dry-run"):
        assert option in out


def test_merge_matches_a_render_with_statistics(tmp_path, monkeypatch, capsys):
    model_without, model_with = _models()
    bundle = tmp_path / "bundle"
    _write_bundle(bundle, _render(model_without))

    code, out = _merge(monkeypatch, capsys, _stats_json(tmp_path), bundle)

    assert code == 0
    expected = _render(model_with)["tables/orders.md"]
    merged = (bundle / "tables" / "orders.md").read_bytes()
    assert merged == expected.encode("utf-8")
    merged_front = parse_frontmatter(merged.decode("utf-8"))
    expected_front = parse_frontmatter(expected)
    assert merged_front is not None
    assert merged_front == expected_front
    for key in STAT_KEYS:
        assert merged_front[key] == STATISTICS[key]


def test_temporary_and_never_analysed_tables_are_left_alone(tmp_path, monkeypatch, capsys):
    model_without, _ = _models()
    bundle = tmp_path / "bundle"
    files = _render(model_without)
    # NEWT is not in the sample model; give it a concept so the never-analysed skip is reached
    files["tables/newt.md"] = "---\ntype: Oracle Table\ntitle: NEWT\n---\n"
    _write_bundle(bundle, files)

    code, out = _merge(monkeypatch, capsys, _stats_json(tmp_path), bundle)

    assert code == 0
    assert (bundle / "tables" / "customers.md").read_bytes() == files["tables/customers.md"].encode("utf-8")
    assert (bundle / "tables" / "newt.md").read_bytes() == files["tables/newt.md"].encode("utf-8")
    assert "global temporary: ['CUSTOMERS']" in out
    assert "never analysed: ['NEWT']" in out


def test_second_run_changes_nothing(tmp_path, monkeypatch, capsys):
    model_without, _ = _models()
    bundle = tmp_path / "bundle"
    _write_bundle(bundle, _render(model_without))
    stats_json = _stats_json(tmp_path)

    _merge(monkeypatch, capsys, stats_json, bundle)
    after_first = _snapshot(bundle)
    code, out = _merge(monkeypatch, capsys, stats_json, bundle)

    assert code == 0
    assert _snapshot(bundle) == after_first
    assert "updated: 0" in out


def test_dry_run_writes_nothing(tmp_path, monkeypatch, capsys):
    model_without, _ = _models()
    bundle = tmp_path / "bundle"
    _write_bundle(bundle, _render(model_without))
    before = _snapshot(bundle)

    code, out = _merge(monkeypatch, capsys, _stats_json(tmp_path), bundle, "--dry-run")

    assert code == 0
    assert _snapshot(bundle) == before
    assert "would update: 1" in out


def test_old_nested_statistics_block_is_replaced_by_flat_keys(tmp_path, monkeypatch, capsys):
    model_without, model_with = _models()
    bundle = tmp_path / "bundle"
    files = _render(model_without)
    _write_bundle(bundle, files)
    orders = bundle / "tables" / "orders.md"
    old_block = (
        "statistics:\n"
        "  source: USER_TABLES optimizer statistics (estimate, not an exact count)\n"
        "  num_rows: 1\n"
        "  last_analyzed: '2020-01-01T00:00:00'\n"
    )
    text = orders.read_text(encoding="utf-8")
    head, _, rest = text.partition("\n---\n")
    orders.write_bytes((head + "\n" + old_block + "---\n" + rest).encode("utf-8"))

    code, out = _merge(monkeypatch, capsys, _stats_json(tmp_path), bundle)

    assert code == 0
    merged = orders.read_text(encoding="utf-8")
    assert "statistics:" not in merged
    assert merged == _render(model_with)["tables/orders.md"]


# Names whose concept file names collide with, or differ from, a simple lower-case mapping
_AWKWARD_NAMES = ("A$B", "A_B", "A__C", "A_C", "LOG", "X_", "NO", "A'B")


def _stats_row(index: int) -> dict[str, Any]:
    """Return statistics that differ for every ``index``."""
    return {
        "num_rows": index * 100,
        "avg_row_len": index,
        "blocks": index + 10,
        "sample_size": index * 50,
        "last_analyzed": f"2026-10-0{index}T12:00:00",
    }


def _awkward_models() -> tuple[SchemaModel, SchemaModel]:
    """Return a model without statistics and the same model with different statistics per table."""
    without = SchemaModel(schema_name="APP", tables=tuple(Table(name=n) for n in _AWKWARD_NAMES))
    with_stats = SchemaModel(
        schema_name="APP",
        tables=tuple(Table(name=n, **_stats_row(i)) for i, n in enumerate(_AWKWARD_NAMES, start=1)),
    )
    return without, with_stats


def _awkward_json(tmp_path: Path) -> Path:
    """Write a statistics JSON giving every awkward table its own numbers."""
    tables = [{"table_name": n, **_stats_row(i), "temporary": "N"} for i, n in enumerate(_AWKWARD_NAMES, start=1)]
    path = tmp_path / "awkward.json"
    path.write_text(json.dumps({"metadata": {}, "tables": tables}), encoding="utf-8")
    return path


def test_every_table_gets_its_own_statistics_whatever_its_file_name(tmp_path, monkeypatch, capsys):
    without, with_stats = _awkward_models()
    bundle = tmp_path / "bundle"
    _write_bundle(bundle, _render(without))
    expected = {k: v for k, v in _render(with_stats).items() if k.startswith("tables/")}
    assert len(expected) == len(_AWKWARD_NAMES)

    code, out = _merge(monkeypatch, capsys, _awkward_json(tmp_path), bundle)

    assert code == 0
    actual = {k: v for k, v in _snapshot(bundle).items() if k.startswith("tables/")}
    assert actual == {k: v.encode("utf-8") for k, v in expected.items()}
    assert f"updated: {len(_AWKWARD_NAMES)}" in out
    assert "skipped, no OKF file: 0" in out


def test_files_sharing_a_title_are_left_unchanged_and_reported(tmp_path, monkeypatch, capsys):
    model_without, _ = _models()
    bundle = tmp_path / "bundle"
    files = _render(model_without)
    files["tables/orders-copy.md"] = files["tables/orders.md"]
    _write_bundle(bundle, files)
    before = _snapshot(bundle)

    code, out = _merge(monkeypatch, capsys, _stats_json(tmp_path), bundle)

    assert code == 0
    assert _snapshot(bundle) == before
    assert "updated: 0" in out
    assert "shared by several files: ['ORDERS']" in out
    assert "duplicate title 'ORDERS': ['orders-copy.md', 'orders.md']" in out
