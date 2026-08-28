"""Oracle connection handling on top of python-oracledb's thin driver.

The thin driver needs no Oracle Client install, which is why
``oracledb.init_oracle_client`` is never called here -- adding it would
silently switch some environments to thick mode and reintroduce the exact
dependency this project avoids. Every failure path is translated into one of
this package's own exception types so callers never need to catch a
third-party exception directly, and no error message here ever includes a
password.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from ..env import OracleCredentials
from ..errors import ConnectionError_, ExtractionError

try:
    import oracledb
except ImportError as exc:  # pragma: no cover - exercised only without the dependency
    raise ConnectionError_(
        "The 'oracledb' package is required to connect to Oracle. Install it with: pip install oracledb"
    ) from exc

_LOGGER = logging.getLogger(__name__)


def connect(credentials: OracleCredentials) -> oracledb.Connection:
    """Open a thin-mode connection to Oracle.

    Args:
        credentials: The user, password, and DSN to connect with.

    Returns:
        An open ``oracledb.Connection``.

    Raises:
        ConnectionError_: If the connection cannot be established. The
            message includes the DSN and user for diagnosis, but never the
            password.
    """
    _LOGGER.info("Connecting to %s", credentials.describe())
    try:
        return oracledb.connect(user=credentials.user, password=credentials.password, dsn=credentials.dsn)
    except oracledb.Error as exc:
        raise ConnectionError_(
            f"Could not connect to Oracle as {credentials.user} at {credentials.dsn}: {exc}"
        ) from exc


@contextmanager
def oracle_connection(credentials: OracleCredentials) -> Iterator[oracledb.Connection]:
    """Yield an open connection and guarantee it is closed afterward.

    Args:
        credentials: The user, password, and DSN to connect with.

    Yields:
        An open ``oracledb.Connection``.

    Raises:
        ConnectionError_: If the connection cannot be established.
    """
    connection = connect(credentials)
    try:
        yield connection
    finally:
        connection.close()


def _coerce_value(value: object) -> object:
    """Return a value safe to hand back from a query, reading LOBs eagerly.

    A LOB column comes back as an ``oracledb.LOB`` bound to the cursor's
    fetch; if it escapes into the caller with the cursor already advanced or
    closed, reading it later raises. Reading it here, while the row is still
    current, avoids that trap entirely.
    """
    if hasattr(value, "read"):
        return value.read()
    return value


def fetch_all(connection: oracledb.Connection, sql: str, **binds: object) -> list[dict[str, Any]]:
    """Run a query and return every row as a dict keyed by lower-case column name.

    Args:
        connection: An open Oracle connection.
        sql: The statement to execute, using named bind placeholders.
        **binds: Values for the statement's named bind placeholders.

    Returns:
        One dict per row, with LOB values already read into ``str``.

    Raises:
        ExtractionError: If the query fails. The message includes the first
            line of the SQL so the failing query is identifiable without
            dumping the whole statement.
    """
    try:
        cursor = connection.cursor()
        try:
            cursor.execute(sql, binds)
            description = cursor.description or []
            columns = [column[0].lower() for column in description]
            return [dict(zip(columns, (_coerce_value(value) for value in row), strict=True)) for row in cursor]
        finally:
            cursor.close()
    except oracledb.Error as exc:
        first_line = sql.strip().splitlines()[0] if sql.strip() else ""
        raise ExtractionError(f"Query failed ({first_line}): {exc}") from exc


def fetch_scalar(connection: oracledb.Connection, sql: str, **binds: object) -> Any:
    """Run a query and return the first column of its first row.

    Args:
        connection: An open Oracle connection.
        sql: The statement to execute, using named bind placeholders.
        **binds: Values for the statement's named bind placeholders.

    Returns:
        The first column of the first row, or None when the query has no rows.

    Raises:
        ExtractionError: If the query fails.
    """
    rows = fetch_all(connection, sql, **binds)
    if not rows:
        return None
    first_row = rows[0]
    first_key = next(iter(first_row))
    return first_row[first_key]
