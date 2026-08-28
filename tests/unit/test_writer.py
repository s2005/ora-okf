"""Tests for BundleWriter's filesystem behavior."""

from __future__ import annotations

import re

import pytest

from ora_okf.errors import BundleError
from ora_okf.okf.renderers import RenderConfig
from ora_okf.okf.writer import BUNDLE_MARKER, BundleWriter
from tests.conftest import build_sample_model

SCHEMA_LABEL = "APP"


def _writer(okf_dir):
    return BundleWriter(okf_dir, RenderConfig(schema_label=SCHEMA_LABEL))


class TestWrite:
    def test_writes_expected_files_and_marker(self, tmp_path):
        okf_dir = tmp_path / "bundle"
        written = _writer(okf_dir).write(build_sample_model())
        assert "index.md" in written
        assert "log.md" in written
        assert "schema.md" in written
        assert (okf_dir / BUNDLE_MARKER).exists()
        for relative_path in written:
            assert (okf_dir / relative_path).exists()

    def test_second_write_removes_a_stale_file(self, tmp_path):
        okf_dir = tmp_path / "bundle"
        _writer(okf_dir).write(build_sample_model())

        stale = okf_dir / "tables" / "gone.md"
        stale.parent.mkdir(parents=True, exist_ok=True)
        stale.write_text("stale content\n", encoding="utf-8")
        assert stale.exists()

        _writer(okf_dir).write(build_sample_model())
        assert not stale.exists()

    def test_refuses_a_non_empty_unowned_directory(self, tmp_path):
        okf_dir = tmp_path / "bundle"
        okf_dir.mkdir()
        (okf_dir / "existing.txt").write_text("not ours\n", encoding="utf-8")
        with pytest.raises(BundleError, match=re.escape(str(okf_dir))):
            _writer(okf_dir).write(build_sample_model())

    def test_accepts_an_existing_empty_directory(self, tmp_path):
        okf_dir = tmp_path / "bundle"
        okf_dir.mkdir()
        written = _writer(okf_dir).write(build_sample_model())
        assert written

    def test_accepts_a_directory_that_already_has_the_marker(self, tmp_path):
        okf_dir = tmp_path / "bundle"
        okf_dir.mkdir()
        (okf_dir / BUNDLE_MARKER).write_text("marker\n", encoding="utf-8")
        written = _writer(okf_dir).write(build_sample_model())
        assert written

    def test_raises_when_okf_dir_is_an_existing_file(self, tmp_path):
        okf_dir = tmp_path / "bundle"
        okf_dir.write_text("i am a file\n", encoding="utf-8")
        with pytest.raises(BundleError):
            _writer(okf_dir).write(build_sample_model())

    def test_files_are_written_with_lf_line_endings(self, tmp_path):
        okf_dir = tmp_path / "bundle"
        written = _writer(okf_dir).write(build_sample_model())
        for relative_path in written:
            raw = (okf_dir / relative_path).read_bytes()
            assert b"\r\n" not in raw


class TestConformanceFailures:
    def test_clean_bundle_has_no_failures(self, tmp_path):
        okf_dir = tmp_path / "bundle"
        writer = _writer(okf_dir)
        writer.write(build_sample_model())
        assert writer.conformance_failures() == []

    def test_reports_a_file_missing_type(self, tmp_path):
        okf_dir = tmp_path / "bundle"
        writer = _writer(okf_dir)
        writer.write(build_sample_model())

        bad_file = okf_dir / "tables" / "broken.md"
        bad_file.write_text("---\ntitle: No Type Here\n---\n\nbody\n", encoding="utf-8")

        failures = writer.conformance_failures()
        assert "tables/broken.md" in failures
