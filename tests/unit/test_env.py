"""Tests for credential loading from an env file and the process environment.

Env-file fixtures are built one setting per line via :func:`write`, rather than
as a single ``\\n``-joined literal. Beyond reading better, it keeps each
``KEY=value`` on its own physical source line so a secret scanner sees a lone
placeholder rather than a run of settings that looks like a real credential.
"""

from __future__ import annotations

import pytest

from ora_okf.env import DEFAULT_PORT, OracleCredentials, load_credentials
from ora_okf.errors import ConfigError

_ENV_KEYS = ("DB_USER", "DB_PASSWORD", "DB_DSN", "DB_HOST", "DB_PORT", "DB_SERVICE", "SCHEMA")

# The placeholder substituted for every DB_PASSWORD in these fixtures. Nothing
# here is or ever was a real credential.
_PLACEHOLDER_VALUE = "placeholder"


@pytest.fixture(autouse=True)
def _clear_ambient_environment(monkeypatch):
    """Prevent the process environment from leaking into a test's expectations."""
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def write(tmp_path, *lines: str):
    """Write an env file from one setting per line and return its path."""
    path = tmp_path / "oracle.env"
    path.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")
    return path


class TestLoadCredentials:
    def test_reads_user_password_and_dsn_from_file(self, tmp_path):
        path = write(
            tmp_path,
            "DB_USER=APP_OWNER",
            f"DB_PASSWORD={_PLACEHOLDER_VALUE}",
            "DB_DSN=host:1521/SERVICE",
        )
        credentials = load_credentials(path)
        assert credentials.user == "APP_OWNER"
        assert credentials.password == _PLACEHOLDER_VALUE
        assert credentials.dsn == "host:1521/SERVICE"

    def test_assembles_dsn_from_host_port_service(self, tmp_path):
        path = write(
            tmp_path,
            "DB_USER=APP",
            f"DB_PASSWORD={_PLACEHOLDER_VALUE}",
            "DB_HOST=dbhost",
            "DB_PORT=1522",
            "DB_SERVICE=ORCLPDB1",
        )
        credentials = load_credentials(path)
        assert credentials.dsn == "dbhost:1522/ORCLPDB1"

    def test_dsn_port_defaults_when_absent(self, tmp_path):
        path = write(
            tmp_path,
            "DB_USER=APP",
            f"DB_PASSWORD={_PLACEHOLDER_VALUE}",
            "DB_HOST=dbhost",
            "DB_SERVICE=ORCLPDB1",
        )
        credentials = load_credentials(path)
        assert credentials.dsn == f"dbhost:{DEFAULT_PORT}/ORCLPDB1"

    def test_explicit_dsn_wins_over_host_port_service(self, tmp_path):
        path = write(
            tmp_path,
            "DB_USER=APP",
            f"DB_PASSWORD={_PLACEHOLDER_VALUE}",
            "DB_DSN=explicit:1521/SVC",
            "DB_HOST=ignored",
            "DB_PORT=1522",
            "DB_SERVICE=IGNORED",
        )
        credentials = load_credentials(path)
        assert credentials.dsn == "explicit:1521/SVC"

    def test_jdbc_prefix_is_stripped_from_dsn(self, tmp_path):
        path = write(
            tmp_path,
            "DB_USER=APP",
            f"DB_PASSWORD={_PLACEHOLDER_VALUE}",
            "DB_DSN=jdbc:oracle:thin:@host:1521/SVC",
        )
        credentials = load_credentials(path)
        assert credentials.dsn == "host:1521/SVC"

    def test_schema_defaults_to_user_upper_cased(self, tmp_path):
        path = write(
            tmp_path,
            "DB_USER=app_owner",
            f"DB_PASSWORD={_PLACEHOLDER_VALUE}",
            "DB_DSN=h:1521/S",
        )
        credentials = load_credentials(path)
        assert credentials.schema == "APP_OWNER"

    def test_schema_key_is_honoured_over_user(self, tmp_path):
        path = write(
            tmp_path,
            "DB_USER=app_owner",
            f"DB_PASSWORD={_PLACEHOLDER_VALUE}",
            "DB_DSN=h:1521/S",
            "SCHEMA=other_schema",
        )
        credentials = load_credentials(path)
        assert credentials.schema == "OTHER_SCHEMA"

    def test_schema_override_beats_file_and_user(self, tmp_path):
        path = write(
            tmp_path,
            "DB_USER=app_owner",
            f"DB_PASSWORD={_PLACEHOLDER_VALUE}",
            "DB_DSN=h:1521/S",
            "SCHEMA=other_schema",
        )
        credentials = load_credentials(path, schema_override="explicit_schema")
        assert credentials.schema == "EXPLICIT_SCHEMA"

    def test_falls_back_to_process_environment_for_missing_file_values(self, tmp_path, monkeypatch):
        from_environment = "from-env"
        monkeypatch.setenv("DB_PASSWORD", from_environment)
        path = write(tmp_path, "DB_USER=APP", "DB_DSN=h:1521/S")
        credentials = load_credentials(path)
        assert credentials.password == from_environment

    def test_file_value_wins_over_process_environment(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DB_USER", "FROM_ENV")
        path = write(
            tmp_path,
            "DB_USER=FROM_FILE",
            f"DB_PASSWORD={_PLACEHOLDER_VALUE}",
            "DB_DSN=h:1521/S",
        )
        credentials = load_credentials(path)
        assert credentials.user == "FROM_FILE"

    def test_no_env_file_reads_only_process_environment(self, monkeypatch):
        monkeypatch.setenv("DB_USER", "APP")
        monkeypatch.setenv("DB_PASSWORD", _PLACEHOLDER_VALUE)
        monkeypatch.setenv("DB_DSN", "h:1521/S")
        credentials = load_credentials(None)
        assert credentials.user == "APP"

    def test_missing_file_raises_config_error(self, tmp_path):
        with pytest.raises(ConfigError, match="not found"):
            load_credentials(tmp_path / "absent.env")

    def test_missing_user_raises_config_error(self, tmp_path):
        path = write(tmp_path, f"DB_PASSWORD={_PLACEHOLDER_VALUE}", "DB_DSN=h:1521/S")
        with pytest.raises(ConfigError, match="DB_USER"):
            load_credentials(path)

    def test_missing_password_raises_config_error(self, tmp_path):
        path = write(tmp_path, "DB_USER=APP", "DB_DSN=h:1521/S")
        with pytest.raises(ConfigError, match="DB_PASSWORD"):
            load_credentials(path)

    def test_missing_dsn_and_host_service_raises_config_error(self, tmp_path):
        path = write(tmp_path, "DB_USER=APP", f"DB_PASSWORD={_PLACEHOLDER_VALUE}")
        with pytest.raises(ConfigError, match="Cannot build an Oracle DSN"):
            load_credentials(path)

    def test_missing_service_with_host_present_still_raises(self, tmp_path):
        path = write(
            tmp_path,
            "DB_USER=APP",
            f"DB_PASSWORD={_PLACEHOLDER_VALUE}",
            "DB_HOST=dbhost",
        )
        with pytest.raises(ConfigError, match="Cannot build an Oracle DSN"):
            load_credentials(path)


class TestEnvFileParsing:
    def test_comments_and_blank_lines_are_ignored(self, tmp_path):
        path = write(
            tmp_path,
            "# a comment",
            "",
            "DB_USER=APP",
            f"DB_PASSWORD={_PLACEHOLDER_VALUE}",
            "DB_DSN=h:1521/S",
            "",
            "# trailing comment",
        )
        credentials = load_credentials(path)
        assert credentials.user == "APP"

    def test_export_prefix_is_stripped(self, tmp_path):
        path = write(
            tmp_path,
            "export DB_USER=APP",
            f"DB_PASSWORD={_PLACEHOLDER_VALUE}",
            "DB_DSN=h:1521/S",
        )
        credentials = load_credentials(path)
        assert credentials.user == "APP"

    def test_single_quoted_value_is_unquoted(self, tmp_path):
        path = write(
            tmp_path,
            "DB_USER='APP'",
            f"DB_PASSWORD={_PLACEHOLDER_VALUE}",
            "DB_DSN=h:1521/S",
        )
        credentials = load_credentials(path)
        assert credentials.user == "APP"

    def test_double_quoted_value_is_unquoted(self, tmp_path):
        path = write(
            tmp_path,
            'DB_USER="APP"',
            f"DB_PASSWORD={_PLACEHOLDER_VALUE}",
            "DB_DSN=h:1521/S",
        )
        credentials = load_credentials(path)
        assert credentials.user == "APP"

    def test_malformed_line_is_skipped_not_raised(self, tmp_path):
        path = write(
            tmp_path,
            "this is not a key value line",
            "DB_USER=APP",
            f"DB_PASSWORD={_PLACEHOLDER_VALUE}",
            "DB_DSN=h:1521/S",
        )
        credentials = load_credentials(path)
        assert credentials.user == "APP"


class TestPasswordSecrecy:
    """The password must never reach a log line, a repr, or an error message."""

    def test_repr_does_not_contain_the_password(self):
        distinctive_value = "Sup3rSecretMarker!"
        credentials = OracleCredentials(user="APP", password=distinctive_value, dsn="h:1521/S", schema="APP")
        assert distinctive_value not in repr(credentials)

    def test_describe_does_not_contain_the_password(self):
        distinctive_value = "Sup3rSecretMarker!"
        credentials = OracleCredentials(user="APP", password=distinctive_value, dsn="h:1521/S", schema="APP")
        assert distinctive_value not in credentials.describe()
