"""Command line entry point for ``ora-okf``.

Every option is named. Nothing is positional, so a command reads the same in a
shell history, a Makefile, and a CI job, and adding an option later can never
change the meaning of an existing invocation.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence
from pathlib import Path

from . import __version__
from .errors import OraOkfError, SchemaLeakError
from .export import ExportOptions, ExportResult, load_export_inputs, run_export
from .logging_setup import LOG_LEVELS, configure_logging

logger = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_LEAK = 3

_EPILOG = """\
examples:
  # Export a schema, renaming it through a mapping file
  ora-okf --env-file oracle.env --okf-dir out/okf --mapping schema-map.yaml

  # Check the credentials and mapping without touching the database
  ora-okf --env-file oracle.env --okf-dir out/okf --mapping schema-map.yaml --validate-only

  # See what would be written, including the leak audit, without writing
  ora-okf --env-file oracle.env --okf-dir out/okf --mapping schema-map.yaml --dry-run

  # Include row counts and a bounded sample of rows
  ora-okf --env-file oracle.env --okf-dir out/okf --include-data --sample-rows 10

exit codes:
  0  success
  1  configuration, connection, extraction, or write error
  2  invalid command line
  3  a renamed physical schema name survived into the bundle
"""


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser.

    Returns:
        The configured parser.
    """
    parser = argparse.ArgumentParser(
        prog="ora-okf",
        description=(
            "Export an Oracle schema as an OKF (Open Knowledge Format) Markdown bundle, "
            "rewriting every schema name through a mapping file."
        ),
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    connection = parser.add_argument_group("connection")
    connection.add_argument(
        "--env-file",
        type=Path,
        default=None,
        metavar="PATH",
        help="KEY=value file with DB_USER, DB_PASSWORD and either DB_DSN or DB_HOST/DB_PORT/DB_SERVICE. "
        "Values absent from the file fall back to the process environment.",
    )
    connection.add_argument(
        "--schema",
        type=str,
        default=None,
        metavar="NAME",
        help="Schema to extract (default: SCHEMA, then DB_USER, from the environment).",
    )

    output = parser.add_argument_group("output")
    output.add_argument(
        "--okf-dir",
        type=Path,
        required=True,
        metavar="PATH",
        help="Directory to write the OKF bundle into. Must be empty or a previously exported bundle; "
        "every .md file under it is regenerated.",
    )
    output.add_argument(
        "--no-schema-qualifier",
        action="store_true",
        help="Render 'resource:' values as a bare object name instead of '<SCHEMA>.<OBJECT>'.",
    )

    renaming = parser.add_argument_group("schema renaming")
    renaming.add_argument(
        "--mapping",
        type=Path,
        default=None,
        metavar="PATH",
        help="YAML or JSON file mapping physical schema names to the names used in the bundle. "
        "Without it, no renaming is performed.",
    )
    renaming.add_argument(
        "--no-fail-on-leak",
        action="store_true",
        help="Report, rather than fail, when a renamed physical schema name survives into the bundle.",
    )

    data = parser.add_argument_group("table data")
    data.add_argument(
        "--include-data",
        action="store_true",
        help="Query each table for a row count and a bounded sample of rows. Requires SELECT on the tables.",
    )
    data.add_argument(
        "--sample-rows",
        type=int,
        default=5,
        metavar="N",
        help="Maximum sample rows per table under --include-data (default: 5). 0 keeps row counts only.",
    )

    run = parser.add_argument_group("run control")
    run.add_argument(
        "--dry-run",
        action="store_true",
        help="Render and audit the bundle in memory, then report; write nothing.",
    )
    run.add_argument(
        "--validate-only",
        action="store_true",
        help="Validate the credentials and mapping file, then exit. Does not connect to the database.",
    )
    run.add_argument("--log-level", choices=LOG_LEVELS, default="INFO", help="Console log level (default: INFO).")
    run.add_argument("--log-file", type=Path, default=None, metavar="PATH", help="Write a DEBUG-level log here.")
    run.add_argument("--version", action="version", version=f"ora-okf {__version__}")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI.

    Args:
        argv: Argument list, defaulting to ``sys.argv[1:]``.

    Returns:
        A process exit code.
    """
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.sample_rows < 0:
        parser.error("--sample-rows must be zero or greater")
    if args.dry_run and args.validate_only:
        parser.error("--dry-run and --validate-only are mutually exclusive")

    configure_logging(args.log_level, args.log_file)
    options = _options_from_args(args)

    try:
        if args.validate_only:
            return _run_validate_only(options)
        result = run_export(options)
    except SchemaLeakError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_LEAK
    except OraOkfError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR

    _print_report(result)
    return EXIT_OK


def _options_from_args(args: argparse.Namespace) -> ExportOptions:
    """Translate parsed arguments into export options.

    The two negative flags are inverted exactly once, here, so no code past this
    point has to reason about double negatives.
    """
    return ExportOptions(
        okf_dir=args.okf_dir,
        env_file=args.env_file,
        schema=args.schema,
        mapping_file=args.mapping,
        include_data=args.include_data,
        sample_rows=args.sample_rows,
        qualify_resources=not args.no_schema_qualifier,
        fail_on_leak=not args.no_fail_on_leak,
        dry_run=args.dry_run,
    )


def _run_validate_only(options: ExportOptions) -> int:
    """Validate configuration without connecting, and print what was found."""
    mapping, credentials = load_export_inputs(options)
    lines = [
        "Configuration is valid.",
        f"  connection : {credentials.describe()}",
        f"  bundle dir : {options.okf_dir}",
    ]
    if mapping.entries:
        lines.append(f"  mapping    : {mapping.source or 'in memory'} (unmapped: {mapping.unmapped})")
        lines.extend(f"    {physical} -> {published}" for physical, published in sorted(mapping.entries.items()))
    else:
        lines.append("  mapping    : none (schema names are exported unchanged)")
    print("\n".join(lines))
    return EXIT_OK


def _print_report(result: ExportResult) -> None:
    """Print the post-export summary to stdout."""
    heading = "Dry run complete (nothing written)" if result.dry_run else "OKF bundle written"
    lines: list[str] = [
        heading,
        f"  schema     : {result.physical_schema} exported as {result.schema_label}",
        f"  bundle dir : {result.okf_dir}",
        f"  files      : {len(result.files)} ({result.object_count} object concepts)",
        f"  renaming   : {result.rename.summary()}",
    ]
    if result.residual.has_leaks():
        lines.append(f"  unmapped   : {', '.join(result.residual.schemas_found())} (still present in the bundle)")
    if result.leaks.has_leaks():
        lines.append(f"  LEAKED     : {', '.join(result.leaks.schemas_found())}")
    else:
        lines.append("  leak audit : clean")
    print("\n".join(lines))


if __name__ == "__main__":
    sys.exit(main())
