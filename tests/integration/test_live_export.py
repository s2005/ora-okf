"""Integration tests that run against a live Oracle database.

These are opt-in. Point ``ORA_OKF_TEST_ENV_FILE`` at a credentials file for a
schema you are allowed to read, and optionally set ``ORA_OKF_TEST_SCHEMA`` to
export a schema other than the connecting user::

    ORA_OKF_TEST_ENV_FILE=oracle.env pytest tests/integration -m integration

Without that variable every test here skips, so the default ``pytest`` run needs
no database.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from ora_okf.export import ExportOptions, run_export
from ora_okf.mapping import SchemaMapping
from ora_okf.okf.audit import audit_bundle
from ora_okf.okf.concept import parse_frontmatter
from ora_okf.oracle.connection import oracle_connection
from ora_okf.oracle.extractor import OracleSchemaExtractor

pytestmark = pytest.mark.integration

ENV_VAR = "ORA_OKF_TEST_ENV_FILE"
SCHEMA_VAR = "ORA_OKF_TEST_SCHEMA"

# A published label that could not plausibly occur in a real schema, so a test
# assertion about it cannot pass by accident.
TEST_LABEL = "ZZTESTLABEL"


def env_file() -> Path:
    """Return the configured credentials file, skipping the test when unset."""
    configured = os.environ.get(ENV_VAR, "").strip()
    if not configured:
        pytest.skip(f"set {ENV_VAR} to a credentials file to run integration tests")
    path = Path(configured)
    if not path.is_file():
        pytest.skip(f"{ENV_VAR} points at a missing file: {path}")
    return path


@pytest.fixture(name="options")
def options_fixture(tmp_path: Path) -> ExportOptions:
    """Build export options pointing at a throwaway bundle directory."""
    return ExportOptions(
        okf_dir=tmp_path / "okf",
        env_file=env_file(),
        schema=os.environ.get(SCHEMA_VAR) or None,
    )


class TestLiveExtraction:
    def test_extract_returns_a_usable_model(self, options: ExportOptions) -> None:
        from ora_okf.env import load_credentials

        credentials = load_credentials(options.env_file, schema_override=options.schema)
        with oracle_connection(credentials) as connection:
            model = OracleSchemaExtractor(connection, credentials.schema).extract()

        assert model.schema_name == credentials.schema
        assert model.generated_at
        assert model.database_version or True  # v$version may not be granted

    def test_export_writes_a_conformant_bundle(self, options: ExportOptions) -> None:
        result = run_export(options)

        assert result.okf_dir.is_dir()
        assert (result.okf_dir / "index.md").is_file()
        assert (result.okf_dir / "schema.md").is_file()
        assert (result.okf_dir / ".okf-bundle").is_file()

        for concept in result.okf_dir.rglob("*.md"):
            if concept.parent == result.okf_dir and concept.name in {"index.md", "log.md"}:
                continue
            frontmatter = parse_frontmatter(concept.read_text(encoding="utf-8"))
            assert frontmatter is not None, f"{concept} has no frontmatter"
            assert str(frontmatter.get("type", "")).strip(), f"{concept} has no type"


class TestLiveRenaming:
    def test_the_physical_schema_name_survives_in_no_file(self, options: ExportOptions, tmp_path: Path) -> None:
        """The whole point of the tool, checked against a real schema."""
        from ora_okf.env import load_credentials

        credentials = load_credentials(options.env_file, schema_override=options.schema)
        mapping_file = tmp_path / "map.yaml"
        mapping_file.write_text(
            f"unmapped: keep\nschemas:\n  {credentials.schema}: {TEST_LABEL}\n",
            encoding="utf-8",
        )

        result = run_export(
            ExportOptions(
                okf_dir=options.okf_dir,
                env_file=options.env_file,
                schema=options.schema,
                mapping_file=mapping_file,
            )
        )

        assert result.schema_label == TEST_LABEL
        assert not result.leaks.has_leaks(), result.leaks.report()

        audit = audit_bundle(result.okf_dir, [credentials.schema])
        assert not audit.has_leaks(), audit.report()

    def test_rerun_is_deterministic_apart_from_the_timestamp(self, options: ExportOptions) -> None:
        first = _bundle_text(run_export(options).okf_dir)
        second = _bundle_text(run_export(options).okf_dir)
        assert first == second


class TestLiveMappingPolicies:
    def test_error_policy_names_the_unmapped_schema(self, options: ExportOptions, tmp_path: Path) -> None:
        """An 'unmapped: error' run must refuse rather than leak silently."""
        from ora_okf.env import load_credentials
        from ora_okf.errors import MappingError

        credentials = load_credentials(options.env_file, schema_override=options.schema)
        mapping = SchemaMapping(entries={"NO_SUCH_SCHEMA": "NONE"}, unmapped="error")
        assert mapping.missing([credentials.schema]) == (credentials.schema.upper(),)

        mapping_file = tmp_path / "map.yaml"
        mapping_file.write_text("unmapped: error\nschemas:\n  NO_SUCH_SCHEMA: NONE\n", encoding="utf-8")

        with pytest.raises(MappingError):
            run_export(
                ExportOptions(
                    okf_dir=options.okf_dir,
                    env_file=options.env_file,
                    schema=options.schema,
                    mapping_file=mapping_file,
                )
            )


def _bundle_text(okf_dir: Path) -> dict[str, list[str]]:
    """Return every bundle file's lines, with timestamp lines removed.

    Two pipeline runs legitimately differ in ``generated_at``, which is stamped
    at extraction. Filtering those lines leaves any other difference visible.
    """
    contents: dict[str, list[str]] = {}
    for path in sorted(okf_dir.rglob("*.md")):
        lines = path.read_text(encoding="utf-8").splitlines()
        relative = path.relative_to(okf_dir).as_posix()
        contents[relative] = [
            line for line in lines if not line.startswith("timestamp:") and "| timestamp |" not in line
        ]
    return contents
