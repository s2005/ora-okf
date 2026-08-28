"""Tests for the post-write leak audit."""

from __future__ import annotations

from ora_okf.okf.audit import audit_bundle, audit_rendered

PHYSICAL = "APP_PROD_OWNER"


class TestAuditRendered:
    def test_finds_a_bare_occurrence(self):
        files = [("tables/orders.md", f"resource: {PHYSICAL}.ORDERS")]
        result = audit_rendered(files, [PHYSICAL])
        assert result.has_leaks()
        assert result.hits[0].path == "tables/orders.md"
        assert result.hits[0].line == 1
        assert result.hits[0].schema == PHYSICAL

    def test_finds_a_quoted_occurrence(self):
        files = [("tables/orders.md", f'resource: "{PHYSICAL}"."ORDERS"')]
        result = audit_rendered(files, [PHYSICAL])
        assert result.has_leaks()
        assert result.hits[0].schema == PHYSICAL

    def test_does_not_match_inside_a_longer_identifier(self):
        files = [("tables/orders.md", f"resource: {PHYSICAL}_ARCHIVE.ORDERS")]
        result = audit_rendered(files, [PHYSICAL])
        assert not result.has_leaks()

    def test_empty_physical_names_returns_clean_without_scanning(self):
        files = [("tables/orders.md", f"{PHYSICAL}.ORDERS")]
        result = audit_rendered(files, [])
        assert not result.has_leaks()
        assert result.files_scanned == 1

    def test_has_leaks_true_when_hits_present(self):
        result = audit_rendered([("a.md", PHYSICAL)], [PHYSICAL])
        assert result.has_leaks() is True

    def test_has_leaks_false_when_clean(self):
        result = audit_rendered([("a.md", "nothing here")], [PHYSICAL])
        assert result.has_leaks() is False

    def test_schemas_found_is_sorted_and_deduplicated(self):
        files = [("a.md", f"{PHYSICAL} {PHYSICAL} REF_DATA")]
        result = audit_rendered(files, [PHYSICAL, "REF_DATA"])
        assert result.schemas_found() == (PHYSICAL, "REF_DATA")

    def test_summary_reports_no_hits(self):
        result = audit_rendered([("a.md", "clean")], [PHYSICAL])
        assert "no physical schema names" in result.summary()

    def test_summary_reports_hits(self):
        result = audit_rendered([("a.md", PHYSICAL)], [PHYSICAL])
        assert PHYSICAL in result.summary()

    def test_report_is_empty_for_a_clean_result(self):
        result = audit_rendered([("a.md", "clean")], [PHYSICAL])
        assert result.report() == ""

    def test_report_lists_hits(self):
        result = audit_rendered([("a.md", PHYSICAL)], [PHYSICAL])
        assert "a.md:1:" in result.report()

    def test_per_schema_cap_limits_recorded_hits(self):
        text = " ".join([PHYSICAL] * 5)
        result = audit_rendered([("a.md", text)], [PHYSICAL], max_hits_per_schema=2)
        assert len(result.hits) == 2
        assert result.truncated == (PHYSICAL,)


class TestAuditBundle:
    def test_missing_directory_returns_clean_result(self, tmp_path):
        result = audit_bundle(tmp_path / "does-not-exist", [PHYSICAL])
        assert not result.has_leaks()
        assert result.files_scanned == 0

    def test_scans_md_files_on_disk(self, tmp_path):
        (tmp_path / "tables").mkdir()
        (tmp_path / "tables" / "orders.md").write_text(f"resource: {PHYSICAL}.ORDERS\n", encoding="utf-8")
        result = audit_bundle(tmp_path, [PHYSICAL])
        assert result.has_leaks()
        assert result.files_scanned == 1
