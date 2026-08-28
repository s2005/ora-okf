"""Tests for CLI argument parsing and error handling, without a database."""

from __future__ import annotations

import pytest

from ora_okf.cli import _options_from_args, build_parser, main


def write(tmp_path, name, text):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def _valid_env_file(tmp_path):
    """Write a credentials file whose values are all placeholders."""
    settings = (
        "DB_USER=APP_OWNER",
        "DB_PASSWORD=placeholder",
        "DB_DSN=host:1521/SVC",
    )
    return write(tmp_path, "oracle.env", "".join(f"{line}\n" for line in settings))


def _valid_mapping_file(tmp_path):
    return write(tmp_path, "map.yaml", "schemas:\n  APP_PROD: APP\n")


class TestBuildParser:
    def test_accepts_a_minimal_valid_command(self, tmp_path):
        parser = build_parser()
        args = parser.parse_args(["--okf-dir", str(tmp_path / "out")])
        assert args.okf_dir == tmp_path / "out"

    def test_okf_dir_is_required(self):
        parser = build_parser()
        with pytest.raises(SystemExit) as exc_info:
            parser.parse_args([])
        assert exc_info.value.code == 2


class TestMainArgumentErrors:
    def test_negative_sample_rows_exits_2(self, tmp_path):
        with pytest.raises(SystemExit) as exc_info:
            main(["--okf-dir", str(tmp_path / "out"), "--sample-rows", "-1"])
        assert exc_info.value.code == 2

    def test_dry_run_with_validate_only_exits_2(self, tmp_path):
        with pytest.raises(SystemExit) as exc_info:
            main(["--okf-dir", str(tmp_path / "out"), "--dry-run", "--validate-only"])
        assert exc_info.value.code == 2

    def test_version_exits_0(self):
        with pytest.raises(SystemExit) as exc_info:
            main(["--version"])
        assert exc_info.value.code == 0


class TestMainValidateOnly:
    def test_valid_env_and_mapping_returns_0_and_prints_pairs(self, tmp_path, capsys):
        env_file = _valid_env_file(tmp_path)
        mapping_file = _valid_mapping_file(tmp_path)
        exit_code = main(
            [
                "--env-file",
                str(env_file),
                "--okf-dir",
                str(tmp_path / "out"),
                "--mapping",
                str(mapping_file),
                "--validate-only",
            ]
        )
        assert exit_code == 0
        captured = capsys.readouterr()
        assert "APP_PROD -> APP" in captured.out

    def test_missing_env_file_returns_1_and_reports_to_stderr(self, tmp_path, capsys):
        exit_code = main(
            [
                "--env-file",
                str(tmp_path / "absent.env"),
                "--okf-dir",
                str(tmp_path / "out"),
                "--validate-only",
            ]
        )
        assert exit_code == 1
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "error:" in captured.err

    def test_invalid_mapping_file_returns_1_and_reports_to_stderr(self, tmp_path, capsys):
        mapping_file = write(tmp_path, "map.yaml", "schemas: {}\n")
        exit_code = main(
            [
                "--okf-dir",
                str(tmp_path / "out"),
                "--mapping",
                str(mapping_file),
                "--validate-only",
            ]
        )
        assert exit_code == 1
        captured = capsys.readouterr()
        assert "error:" in captured.err


class TestOptionsFromArgs:
    def test_no_schema_qualifier_maps_to_qualify_resources_false(self, tmp_path):
        parser = build_parser()
        args = parser.parse_args(["--okf-dir", str(tmp_path), "--no-schema-qualifier"])
        options = _options_from_args(args)
        assert options.qualify_resources is False

    def test_no_fail_on_leak_maps_to_fail_on_leak_false(self, tmp_path):
        parser = build_parser()
        args = parser.parse_args(["--okf-dir", str(tmp_path), "--no-fail-on-leak"])
        options = _options_from_args(args)
        assert options.fail_on_leak is False

    def test_no_timestamp_maps_to_include_timestamp_false(self, tmp_path):
        parser = build_parser()
        args = parser.parse_args(["--okf-dir", str(tmp_path), "--no-timestamp"])
        options = _options_from_args(args)
        assert options.include_timestamp is False

    def test_defaults_are_true(self, tmp_path):
        parser = build_parser()
        args = parser.parse_args(["--okf-dir", str(tmp_path)])
        options = _options_from_args(args)
        assert options.qualify_resources is True
        assert options.fail_on_leak is True
        assert options.include_timestamp is True
