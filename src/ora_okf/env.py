"""Oracle credentials loaded from a ``.env``-style file or the process environment.

The file is a plain ``KEY=value`` list, the same shape most database tooling
already uses, so an existing connection file can usually be pointed at directly::

    DB_HOST=db.example.com
    DB_PORT=1521
    DB_SERVICE=ORCLPDB1
    DB_USER=APP_OWNER
    DB_PASSWORD=...
    # DB_DSN wins over host/port/service when both are present
    # DB_DSN=db.example.com:1521/ORCLPDB1
    # SCHEMA defaults to DB_USER
    # SCHEMA=APP_OWNER

Values missing from the file fall back to the process environment, so a secret
can stay out of the file entirely (``DB_PASSWORD`` exported by a CI runner) while
the rest of the connection details stay checked in next to the project.

The password never reaches a log line, an exception message, or a ``repr``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .errors import ConfigError

# Keys read from the file or the process environment.
KEY_HOST = "DB_HOST"
KEY_PORT = "DB_PORT"
KEY_SERVICE = "DB_SERVICE"
KEY_USER = "DB_USER"
KEY_PASSWORD = "DB_PASSWORD"
KEY_DSN = "DB_DSN"
KEY_SCHEMA = "SCHEMA"

DEFAULT_PORT = "1521"


@dataclass(frozen=True)
class OracleCredentials:
    """Connection details for one Oracle schema.

    Attributes:
        user: The account to authenticate as.
        password: The account password. Excluded from ``repr`` so it cannot be
            echoed by a debugger, a log call, or an exception rendering its
            arguments.
        dsn: The Easy Connect descriptor, e.g. ``host:1521/SERVICE``.
        schema: The schema to extract, which may differ from ``user`` when the
            account reads another schema through granted privileges.
    """

    user: str
    password: str
    dsn: str
    schema: str

    def __repr__(self) -> str:
        """Return a representation with the password replaced by a placeholder."""
        return f"OracleCredentials(user={self.user!r}, dsn={self.dsn!r}, schema={self.schema!r}, password=***)"

    def describe(self) -> str:
        """Return a one-line, password-free summary for logs and ``--dry-run``."""
        return f"{self.user}@{self.dsn} (schema {self.schema})"


def load_credentials(env_file: Path | None = None, *, schema_override: str | None = None) -> OracleCredentials:
    """Build credentials from an env file, the process environment, or both.

    Args:
        env_file: Path to a ``KEY=value`` file. When None, only the process
            environment is consulted.
        schema_override: An explicit schema that wins over ``SCHEMA`` and
            ``DB_USER`` from the environment.

    Returns:
        The assembled credentials.

    Raises:
        ConfigError: If the file is missing or a required value is absent.
    """
    values = _read_env_file(env_file) if env_file is not None else {}

    user = _require(values, KEY_USER, env_file)
    password = _require(values, KEY_PASSWORD, env_file)
    dsn = _resolve_dsn(values, env_file)
    schema = (schema_override or _lookup(values, KEY_SCHEMA) or user).strip().upper()

    return OracleCredentials(user=user, password=password, dsn=dsn, schema=schema)


def _resolve_dsn(values: dict[str, str], env_file: Path | None) -> str:
    """Return the Easy Connect DSN, preferring an explicit ``DB_DSN``.

    A JDBC-style prefix is stripped so a connection file shared with a Java tool
    works unchanged; python-oracledb wants the bare ``host:port/service``.
    """
    dsn = _lookup(values, KEY_DSN)
    if dsn:
        return _strip_jdbc_prefix(dsn)

    host = _lookup(values, KEY_HOST)
    service = _lookup(values, KEY_SERVICE)
    if not host or not service:
        raise ConfigError(
            f"Cannot build an Oracle DSN{_origin(env_file)}: set {KEY_DSN}, or both {KEY_HOST} and {KEY_SERVICE}."
        )
    port = _lookup(values, KEY_PORT) or DEFAULT_PORT
    return f"{host}:{port}/{service}"


def _strip_jdbc_prefix(dsn: str) -> str:
    """Return ``dsn`` without a ``jdbc:oracle:thin:@`` prefix."""
    prefix = "jdbc:oracle:thin:@"
    if dsn.lower().startswith(prefix):
        return dsn[len(prefix) :].lstrip("/")
    return dsn


def _read_env_file(env_file: Path) -> dict[str, str]:
    """Parse a ``KEY=value`` file into a dict.

    Blank lines, ``#`` comments, and an optional ``export`` prefix are ignored.
    A value may be wrapped in matching single or double quotes, which are
    stripped. A malformed line is skipped rather than raising, because a
    connection file often carries unrelated shell syntax.

    Args:
        env_file: The file to read.

    Returns:
        The parsed key/value pairs.

    Raises:
        ConfigError: If the file does not exist or cannot be read.
    """
    if not env_file.is_file():
        raise ConfigError(f"Environment file not found: {env_file}")
    try:
        text = env_file.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"Cannot read environment file {env_file}: {exc}") from exc

    values: dict[str, str] = {}
    for line in text.splitlines():
        parsed = _parse_env_line(line)
        if parsed is not None:
            values[parsed[0]] = parsed[1]
    return values


def _parse_env_line(line: str) -> tuple[str, str] | None:
    """Parse one ``KEY=value`` line, or return None when there is nothing to read."""
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or "=" not in stripped:
        return None
    if stripped.startswith("export "):
        stripped = stripped[len("export ") :].lstrip()
    key, _, raw_value = stripped.partition("=")
    key = key.strip()
    if not key:
        return None
    return key, _unquote(raw_value.strip())


def _unquote(value: str) -> str:
    """Strip one layer of matching quotes from a value."""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


def _lookup(values: dict[str, str], key: str) -> str:
    """Return a value from the file, falling back to the process environment."""
    from_file = values.get(key, "").strip()
    if from_file:
        return from_file
    return os.environ.get(key, "").strip()


def _require(values: dict[str, str], key: str, env_file: Path | None) -> str:
    """Return a required value, or raise a message naming where it was looked for."""
    value = _lookup(values, key)
    if not value:
        raise ConfigError(f"Required setting {key} is missing{_origin(env_file)}")
    return value


def _origin(env_file: Path | None) -> str:
    """Describe where settings were read from, for an error message."""
    if env_file is None:
        return " from the environment"
    return f" from {env_file} or the environment"
