"""End-to-end export orchestration: connect, extract, rename, render, audit.

This module is the seam between the CLI and everything else. It holds no
argument parsing and no printing, so an embedding application can drive an
export with :func:`run_export` and format the result its own way.

The order of operations matters and is fixed:

1. Load and validate the mapping *before* connecting, so a typo in the mapping
   file costs nothing.
2. Extract the physical model.
3. Rename it. The physical model is kept so the audit knows what to look for.
4. Render, write, then audit the bytes that were actually produced.

The audit is split in two, because two very different things can leave a
physical name in a bundle. A name that was *supposed* to be renamed and is still
there is a defect, and fails the run. A name the mapping deliberately left alone
(the ``keep`` policy) is expected, and is only reported.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from .env import OracleCredentials, load_credentials
from .errors import SchemaLeakError
from .mapping import SchemaMapping, load_mapping
from .model import SchemaModel
from .okf.audit import AuditResult, audit_rendered
from .okf.renderers import RenderConfig, render_bundle
from .okf.writer import BundleWriter
from .oracle.connection import oracle_connection
from .oracle.extractor import OracleSchemaExtractor
from .rename import RenameReport, SchemaRenamer, collect_known_schemas

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ExportOptions:
    """Everything one export run needs.

    Attributes:
        okf_dir: Bundle root to write.
        env_file: Credentials file, or None to read the process environment.
        schema: Schema to extract; defaults to ``SCHEMA`` then ``DB_USER``.
        mapping_file: Schema mapping file, or None for no renaming.
        include_data: Add row counts and bounded row samples.
        sample_rows: Maximum sample rows per table; 0 keeps counts only.
        qualify_resources: Prefix ``resource:`` values with the schema label.
        include_timestamp: Write the extraction timestamp into the bundle.
            Off makes a rerun byte-identical unless the schema itself changed,
            which is what a bundle committed to a repository needs.
        fail_on_leak: Fail when a renamed physical name survives into the bundle.
        dry_run: Render in memory and report, writing nothing.
    """

    okf_dir: Path
    env_file: Path | None = None
    schema: str | None = None
    mapping_file: Path | None = None
    include_data: bool = False
    sample_rows: int = 5
    qualify_resources: bool = True
    include_timestamp: bool = True
    fail_on_leak: bool = True
    dry_run: bool = False


@dataclass
class ExportResult:
    """What an export produced, for the CLI to report.

    Attributes:
        schema_label: The published name the bundle uses for the schema.
        physical_schema: The schema actually read from the database.
        okf_dir: The bundle root (unwritten when ``dry_run``).
        files: Bundle-relative paths rendered.
        object_count: Number of database objects rendered as concepts.
        rename: What the rename pass changed.
        leaks: Occurrences of names that should have been renamed but were not.
        residual: Occurrences of names deliberately left unmapped.
        dry_run: True when nothing was written.
    """

    schema_label: str
    physical_schema: str
    okf_dir: Path
    files: list[str] = field(default_factory=list)
    object_count: int = 0
    rename: RenameReport = field(default_factory=RenameReport)
    leaks: AuditResult = field(default_factory=AuditResult)
    residual: AuditResult = field(default_factory=AuditResult)
    dry_run: bool = False


def load_export_inputs(options: ExportOptions) -> tuple[SchemaMapping, OracleCredentials]:
    """Load and validate the mapping and credentials without connecting.

    Separated from :func:`run_export` so ``--validate-only`` can exercise every
    check that does not need a database.

    Args:
        options: The export options.

    Returns:
        The validated mapping and credentials.

    Raises:
        ConfigError: If credentials are incomplete.
        MappingError: If the mapping file is invalid.
    """
    mapping = load_mapping(options.mapping_file) if options.mapping_file else SchemaMapping.identity()
    credentials = load_credentials(options.env_file, schema_override=options.schema)
    return mapping, credentials


def run_export(options: ExportOptions) -> ExportResult:
    """Run a complete export.

    Args:
        options: The export options.

    Returns:
        The export result.

    Raises:
        OraOkfError: For any expected failure -- bad configuration, an
            unreachable database, an unowned bundle directory, or a surviving
            physical schema name when ``fail_on_leak`` is set.
    """
    mapping, credentials = load_export_inputs(options)
    model = _extract(options, credentials)
    renamed, renamer = _rename(model, mapping)

    config = RenderConfig(
        schema_label=renamed.schema_name,
        qualify_resources=options.qualify_resources,
        include_data=options.include_data,
        sample_rows=options.sample_rows,
        include_timestamp=options.include_timestamp,
    )

    files = render_bundle(renamed, config)
    if not options.dry_run:
        written = BundleWriter(options.okf_dir, config).write(renamed)
    else:
        written = [path for path, _content in files]
        logger.info("Dry run: %d file(s) rendered, nothing written", len(written))

    result = ExportResult(
        schema_label=renamed.schema_name,
        physical_schema=model.schema_name,
        okf_dir=options.okf_dir,
        files=written,
        object_count=renamed.object_count(),
        rename=renamer.report,
        leaks=audit_rendered(files, renamer.report.renamed.keys()),
        residual=audit_rendered(files, _residual_names(model, mapping)),
        dry_run=options.dry_run,
    )
    _report_audits(result, fail_on_leak=options.fail_on_leak)
    return result


def _extract(options: ExportOptions, credentials: OracleCredentials) -> SchemaModel:
    """Connect and extract the physical schema model."""
    logger.info("Connecting to %s", credentials.describe())
    with oracle_connection(credentials) as connection:
        extractor = OracleSchemaExtractor(
            connection,
            credentials.schema,
            include_data=options.include_data,
            sample_rows=options.sample_rows,
        )
        model = extractor.extract()
    logger.info("Extracted %d object(s) from %s", model.object_count(), model.schema_name)
    return model


def _rename(model: SchemaModel, mapping: SchemaMapping) -> tuple[SchemaModel, SchemaRenamer]:
    """Apply the mapping to the extracted model."""
    known = collect_known_schemas(model, mapping)
    renamer = SchemaRenamer(mapping, known)
    renamed = renamer.rename_model(model)
    logger.info("Rename: %s", renamer.report.summary())
    return renamed, renamer


def _residual_names(model: SchemaModel, mapping: SchemaMapping) -> tuple[str, ...]:
    """Return referenced schemas the mapping intentionally left unrenamed.

    These are reported, never failed on: leaving them is what the ``keep``
    policy means. Surfacing them is still worthwhile, because "I did not know
    that schema was referenced" is the usual reason a bundle is not publishable.
    """
    referenced = [model.schema_name, *model.referenced_schemas]
    return mapping.missing(referenced)


def _report_audits(result: ExportResult, *, fail_on_leak: bool) -> None:
    """Log both audits and raise when a renamed name survived.

    Args:
        result: The populated export result.
        fail_on_leak: Whether a surviving renamed name is fatal.

    Raises:
        SchemaLeakError: When ``fail_on_leak`` and the leak audit found hits.
    """
    if result.residual.has_leaks():
        logger.warning(
            "Bundle still contains unmapped schema name(s): %s. Add them under 'schemas:' "
            "in the mapping file, or set 'unmapped: redact'.",
            ", ".join(result.residual.schemas_found()),
        )

    if not result.leaks.has_leaks():
        logger.info("Leak audit clean: %s", result.leaks.summary())
        return

    message = (
        f"Renamed schema name(s) survived into the bundle: {', '.join(result.leaks.schemas_found())}.\n"
        f"{result.leaks.report()}"
    )
    if fail_on_leak:
        raise SchemaLeakError(message)
    logger.warning("%s", message)
