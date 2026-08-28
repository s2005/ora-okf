"""Exception hierarchy for ora-okf.

Every error the CLI turns into a non-zero exit code derives from
:class:`OraOkfError`, so ``cli.main`` can report a clean message instead of a
traceback. Unexpected exceptions are deliberately not caught: they indicate a
bug and should surface with their traceback intact.
"""

from __future__ import annotations


class OraOkfError(Exception):
    """Base class for every expected, user-facing ora-okf failure."""


class ConfigError(OraOkfError):
    """A credentials file, mapping file, or CLI option combination is invalid."""


class MappingError(ConfigError):
    """The schema mapping file is missing, malformed, or internally inconsistent."""


class ConnectionError_(OraOkfError):
    """The Oracle connection could not be established."""


class ExtractionError(OraOkfError):
    """A data dictionary query failed or returned unusable data."""


class BundleError(OraOkfError):
    """The OKF bundle directory cannot be written."""


class SchemaLeakError(OraOkfError):
    """A physical schema name survived into the written bundle.

    Raised by the post-write audit when a mapped physical schema name is still
    present in the bundle and leak checking is enabled, which is the default.
    """
