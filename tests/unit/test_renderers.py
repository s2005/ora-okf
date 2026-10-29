"""Tests for the OKF Markdown renderers, driven by the shared sample model."""

from __future__ import annotations

import dataclasses

from ora_okf.model import Column, SchemaModel, Table
from ora_okf.okf.concept import parse_frontmatter
from ora_okf.okf.renderers import RenderConfig, render_bundle
from tests.conftest import build_sample_model

SCHEMA_LABEL = "APP"


def _render(model=None, **config_kwargs):
    """Render the sample model (or ``model``) and return {path: content}."""
    model = model if model is not None else build_sample_model()
    config = RenderConfig(schema_label=SCHEMA_LABEL, **config_kwargs)
    return dict(render_bundle(model, config))


class TestDeterminism:
    def test_render_bundle_is_deterministic(self):
        model = build_sample_model()
        config = RenderConfig(schema_label=SCHEMA_LABEL)
        first = render_bundle(model, config)
        second = render_bundle(model, config)
        assert first == second

    def test_reserved_files_are_present(self):
        files = _render()
        assert "index.md" in files
        assert "log.md" in files
        assert "schema.md" in files

    def test_results_are_sorted_by_path(self):
        model = build_sample_model()
        config = RenderConfig(schema_label=SCHEMA_LABEL)
        paths = [path for path, _content in render_bundle(model, config)]
        assert paths == sorted(paths)


class TestConceptFileConformance:
    def test_every_concept_file_has_a_non_empty_type(self):
        files = _render()
        for path, content in files.items():
            if path in ("index.md", "log.md"):
                continue
            frontmatter = parse_frontmatter(content)
            assert frontmatter is not None, f"{path} has no parseable frontmatter"
            assert frontmatter.get("type"), f"{path} has an empty type"


class TestResourceQualification:
    def test_qualified_resource_includes_schema_label(self):
        files = _render(qualify_resources=True)
        frontmatter = parse_frontmatter(files["tables/orders.md"])
        assert frontmatter["resource"] == f"{SCHEMA_LABEL}.ORDERS"

    def test_unqualified_resource_is_bare(self):
        files = _render(qualify_resources=False)
        frontmatter = parse_frontmatter(files["tables/orders.md"])
        assert frontmatter["resource"] == "ORDERS"

    def test_schema_label_never_appears_in_any_resource_when_unqualified(self):
        files = _render(qualify_resources=False)
        for path, content in files.items():
            if path in ("index.md", "log.md"):
                continue
            frontmatter = parse_frontmatter(content)
            resource = frontmatter.get("resource", "")
            assert not str(resource).startswith(f"{SCHEMA_LABEL}."), f"{path} leaked the schema label"


class TestTimestamp:
    def test_timestamp_is_present_by_default(self):
        files = _render()
        assert parse_frontmatter(files["tables/orders.md"])["timestamp"]
        assert parse_frontmatter(files["schema.md"])["timestamp"]
        assert "| timestamp |" in files["log.md"]

    def test_no_timestamp_removes_it_from_every_file(self):
        files = _render(include_timestamp=False)
        for path, content in files.items():
            assert "timestamp" not in content, f"{path} still carries a timestamp"

    def test_only_the_timestamp_differs_between_two_extractions(self):
        """Two runs of an unchanged schema render identically without timestamps."""
        first = _render(include_timestamp=False)
        later = _render(
            model=dataclasses.replace(build_sample_model(), generated_at="2030-06-15 12:34:56 UTC"),
            include_timestamp=False,
        )
        assert first == later

    def test_concepts_still_conform_without_a_timestamp(self):
        files = _render(include_timestamp=False)
        for path, content in files.items():
            if path in ("index.md", "log.md"):
                continue
            frontmatter = parse_frontmatter(content)
            assert frontmatter is not None, f"{path} has no parseable frontmatter"
            assert frontmatter.get("type"), f"{path} has an empty type"


class TestTableConcept:
    def test_primary_key_lists_columns_in_order(self):
        files = _render()
        frontmatter = parse_frontmatter(files["tables/orders.md"])
        assert frontmatter["primary_key"] == ["ID"]

    def test_foreign_key_links_to_the_referenced_tables_actual_path(self):
        files = _render()
        content = files["tables/orders.md"]
        assert "](/tables/customers.md)" in content

    def test_comment_column_present_when_a_column_has_a_comment(self):
        files = _render()
        content = files["tables/orders.md"]
        assert "| Column | Type | Nullable | Default | Comment |" in content

    def test_comment_column_absent_when_no_column_has_a_comment(self):
        table = Table(
            name="PLAIN",
            columns=(Column(name="ID", data_type="NUMBER(10)", nullable=False, position=1),),
        )
        model = SchemaModel(schema_name="APP_OWNER", generated_at="2026-01-01 00:00:00 UTC", tables=(table,))
        files = _render(model=model)
        content = files["tables/plain.md"]
        assert "| Column | Type | Nullable | Default |" in content
        assert "Comment" not in content

    def test_comments_section_reconstructs_comment_on_statements(self):
        files = _render()
        content = files["tables/orders.md"]
        assert "COMMENT ON TABLE APP.ORDERS IS 'Customer orders.';" in content
        assert "COMMENT ON COLUMN APP.ORDERS.AMOUNT IS 'Order amount.';" in content

    def test_single_quote_in_comment_is_doubled(self):
        model = build_sample_model()
        orders = model.tables[0]
        quoted_orders = dataclasses.replace(orders, comment="Customer's orders.")
        model = dataclasses.replace(model, tables=(quoted_orders, model.tables[1]))
        files = _render(model=model)
        content = files["tables/orders.md"]
        assert "Customer''s orders." in content

    def test_include_data_false_omits_row_count_and_examples(self):
        files = _render(include_data=False)
        frontmatter = parse_frontmatter(files["tables/orders.md"])
        assert "row_count" not in frontmatter
        assert "## Examples" not in files["tables/orders.md"]

    def test_include_data_true_includes_row_count_and_examples(self):
        files = _render(include_data=True)
        frontmatter = parse_frontmatter(files["tables/orders.md"])
        assert frontmatter["row_count"] == 2
        assert "## Examples" in files["tables/orders.md"]

    def test_gtt_carries_the_global_temporary_table_tag(self):
        files = _render()
        frontmatter = parse_frontmatter(files["tables/customers.md"])
        assert frontmatter["tags"] == ["global-temporary-table"]

    def test_gtt_carries_on_commit_field(self):
        files = _render()
        frontmatter = parse_frontmatter(files["tables/customers.md"])
        assert frontmatter["on_commit"] == "ON COMMIT DELETE ROWS"


class TestHeadingLevels:
    def test_no_concept_body_line_is_an_h1(self):
        files = _render()
        for path, content in files.items():
            if path in ("index.md", "log.md"):
                continue
            body = content.split("---\n", 2)[-1]
            for line in body.splitlines():
                assert not line.startswith("# "), f"{path} has a stray H1 line: {line!r}"

    def test_section_headings_are_h2(self):
        files = _render()
        content = files["tables/orders.md"]
        assert "## Schema" in content
        assert "### Schema" not in content


def _with_statistics(**overrides):
    """Return the sample model with optimizer statistics set on the ORDERS table."""
    model = build_sample_model()
    statistics = {
        "num_rows": 1500,
        "avg_row_len": 42,
        "blocks": 12,
        "sample_size": 1500,
        "last_analyzed": "2026-10-01T12:00:05",
    }
    statistics.update(overrides)
    orders = dataclasses.replace(model.tables[0], **statistics)
    return dataclasses.replace(model, tables=(orders, *model.tables[1:]))


class TestTableStatisticsFrontmatter:
    def test_include_data_renders_the_five_keys_with_exact_values(self):
        files = _render(_with_statistics(), include_data=True)
        frontmatter = parse_frontmatter(files["tables/orders.md"])
        assert frontmatter["num_rows"] == 1500
        assert frontmatter["avg_row_len"] == 42
        assert frontmatter["blocks"] == 12
        assert frontmatter["sample_size"] == 1500
        assert frontmatter["last_analyzed"] == "2026-10-01T12:00:05"

    def test_without_include_data_no_statistics_are_rendered(self):
        files = _render(_with_statistics(), include_data=False)
        frontmatter = parse_frontmatter(files["tables/orders.md"])
        for key in ("num_rows", "avg_row_len", "blocks", "sample_size", "last_analyzed"):
            assert key not in frontmatter

    def test_a_none_field_is_omitted(self):
        files = _render(_with_statistics(sample_size=None), include_data=True)
        frontmatter = parse_frontmatter(files["tables/orders.md"])
        assert "sample_size" not in frontmatter
        assert frontmatter["num_rows"] == 1500

    def test_none_num_rows_renders_no_statistics_keys(self):
        files = _render(_with_statistics(num_rows=None), include_data=True)
        frontmatter = parse_frontmatter(files["tables/orders.md"])
        for key in ("num_rows", "avg_row_len", "blocks", "sample_size", "last_analyzed"):
            assert key not in frontmatter

    def test_row_count_is_unchanged_and_precedes_num_rows(self):
        files = _render(_with_statistics(), include_data=True)
        frontmatter = parse_frontmatter(files["tables/orders.md"])
        assert frontmatter["row_count"] == 2
        keys = list(frontmatter)
        assert keys.index("row_count") < keys.index("num_rows")
