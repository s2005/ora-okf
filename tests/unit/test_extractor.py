"""Tests for pure extractor logic: type formatting, query building, and a
bare extraction round trip against a fake connection (no real database).
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from decimal import Decimal

import oracledb
import pytest

from ora_okf.errors import ExtractionError
from ora_okf.oracle import extractor as extractor_module
from ora_okf.oracle.extractor import OracleSchemaExtractor, format_data_type
from ora_okf.oracle.queries import TABLE_STATISTICS, row_count_sql, sample_rows_sql


def _column_row(data_type, *, precision=None, scale=None, char_length=None, data_length=None):
    return {
        "data_type": data_type,
        "data_precision": precision,
        "data_scale": scale,
        "char_length": char_length,
        "data_length": data_length,
    }


class TestFormatDataType:
    def test_number_with_precision_and_scale(self):
        row = _column_row("NUMBER", precision=10, scale=2)
        assert format_data_type(row) == "NUMBER(10,2)"

    def test_number_with_precision_only(self):
        row = _column_row("NUMBER", precision=10, scale=0)
        assert format_data_type(row) == "NUMBER(10)"

    def test_number_with_precision_and_none_scale(self):
        row = _column_row("NUMBER", precision=10, scale=None)
        assert format_data_type(row) == "NUMBER(10)"

    def test_number_with_neither_precision_nor_scale(self):
        row = _column_row("NUMBER")
        assert format_data_type(row) == "NUMBER"

    def test_varchar2_uses_char_length(self):
        row = _column_row("VARCHAR2", char_length=50, data_length=200)
        assert format_data_type(row) == "VARCHAR2(50)"

    def test_varchar2_falls_back_to_data_length(self):
        row = _column_row("VARCHAR2", char_length=None, data_length=200)
        assert format_data_type(row) == "VARCHAR2(200)"

    def test_varchar2_falls_back_to_data_length_when_char_length_is_zero(self):
        row = _column_row("VARCHAR2", char_length=0, data_length=200)
        assert format_data_type(row) == "VARCHAR2(200)"

    def test_raw_uses_data_length(self):
        row = _column_row("RAW", data_length=16)
        assert format_data_type(row) == "RAW(16)"

    def test_raw_with_no_length_stays_bare(self):
        row = _column_row("RAW", data_length=None)
        assert format_data_type(row) == "RAW"

    def test_date_stays_bare(self):
        row = _column_row("DATE")
        assert format_data_type(row) == "DATE"

    def test_clob_stays_bare(self):
        row = _column_row("CLOB")
        assert format_data_type(row) == "CLOB"


class TestRowCountSql:
    def test_produces_quoted_identifier_sql(self):
        assert row_count_sql("APP_OWNER", "ORDERS") == 'SELECT COUNT(*) FROM "APP_OWNER"."ORDERS"'

    def test_rejects_identifier_with_quote(self):
        with pytest.raises(ExtractionError):
            row_count_sql("APP_OWNER", 'ORDERS"; DROP TABLE X --')

    def test_rejects_identifier_with_semicolon(self):
        with pytest.raises(ExtractionError):
            row_count_sql("APP_OWNER", "ORDERS;DROP TABLE X")

    def test_rejects_identifier_with_space(self):
        with pytest.raises(ExtractionError):
            row_count_sql("APP_OWNER", "ORDERS TABLE")

    def test_rejects_identifier_starting_with_a_digit(self):
        with pytest.raises(ExtractionError):
            row_count_sql("APP_OWNER", "1ORDERS")

    def test_rejects_invalid_schema_identifier(self):
        with pytest.raises(ExtractionError):
            row_count_sql("APP OWNER", "ORDERS")


class TestSampleRowsSql:
    def test_produces_quoted_identifier_sql_with_limit(self):
        sql = sample_rows_sql("APP_OWNER", "ORDERS", 5)
        assert sql == 'SELECT * FROM "APP_OWNER"."ORDERS" FETCH FIRST 5 ROWS ONLY'

    def test_rejects_identifier_with_quote(self):
        with pytest.raises(ExtractionError):
            sample_rows_sql("APP_OWNER", 'ORDERS"', 5)

    def test_rejects_identifier_with_semicolon(self):
        with pytest.raises(ExtractionError):
            sample_rows_sql("APP_OWNER", "ORDERS;DROP TABLE X", 5)

    def test_rejects_identifier_with_space(self):
        with pytest.raises(ExtractionError):
            sample_rows_sql("APP_OWNER", "ORDERS TABLE", 5)

    def test_rejects_identifier_starting_with_a_digit(self):
        with pytest.raises(ExtractionError):
            sample_rows_sql("APP_OWNER", "1ORDERS", 5)

    def test_rejects_zero_limit(self):
        with pytest.raises(ExtractionError):
            sample_rows_sql("APP_OWNER", "ORDERS", 0)

    def test_rejects_negative_limit(self):
        with pytest.raises(ExtractionError):
            sample_rows_sql("APP_OWNER", "ORDERS", -1)

    def test_rejects_non_integer_limit(self):
        with pytest.raises(ExtractionError):
            sample_rows_sql("APP_OWNER", "ORDERS", 1.5)

    def test_rejects_bool_limit(self):
        """bool is a subclass of int in Python, so it needs an explicit reject."""
        with pytest.raises(ExtractionError):
            sample_rows_sql("APP_OWNER", "ORDERS", True)


class _FakeCursor:
    """A cursor that always reports no columns and yields no rows.

    Every query in the extractor is routed through :func:`fetch_all`, which
    only needs ``description`` (for column names) and iteration (for rows).
    An always-empty cursor is therefore enough to drive a full, real
    ``extract()`` call without a database, since every downstream grouping
    step degenerates to an empty dict or list.
    """

    description: tuple = ()

    def execute(self, sql, binds=None):
        del sql, binds

    def close(self):
        pass

    def __iter__(self):
        return iter(())


class _FakeConnection:
    def cursor(self):
        return _FakeCursor()


class TestExtractWithFakeConnection:
    def test_extract_produces_an_empty_but_valid_model(self):
        extractor = OracleSchemaExtractor(_FakeConnection(), "APP_OWNER")
        model = extractor.extract()
        assert model.schema_name == "APP_OWNER"
        assert model.generated_at
        assert model.is_empty()
        assert model.tables == ()
        assert model.views == ()
        assert model.referenced_schemas == ()


class TestTableStatisticsQuery:
    def test_reads_user_tables_guarded_by_the_schema_bind(self):
        assert "user_tables" in TABLE_STATISTICS
        assert "USER = :schema" in TABLE_STATISTICS
        assert "dropped = 'NO'" in TABLE_STATISTICS

    def test_schema_is_the_only_bind_placeholder(self):
        assert set(re.findall(r":\w+", TABLE_STATISTICS)) == {":schema"}


_TABLES_QUERY = "FROM all_tables"
_STATISTICS_QUERY = "FROM user_tables"
_COUNT_QUERY = "COUNT(*)"


class _ScriptedCursor:
    """A cursor that answers by a substring of the SQL text and records every statement."""

    def __init__(self, connection):
        self._connection = connection
        self.description = ()
        self._rows = ()

    def execute(self, sql, binds=None):
        del binds
        self._connection.executed.append(sql)
        self.description, self._rows = (), ()
        for needle, answer in self._connection.answers.items():
            if needle in sql:
                if isinstance(answer, Exception):
                    raise answer
                columns, rows = answer
                self.description = tuple((column,) for column in columns)
                self._rows = tuple(rows)
                return

    def close(self):
        pass

    def __iter__(self):
        return iter(self._rows)


class _ScriptedConnection:
    """A connection whose queries are answered from ``answers``; anything unmapped returns no rows."""

    def __init__(self, answers):
        self.answers = answers
        self.executed = []

    def cursor(self):
        return _ScriptedCursor(self)

    def count(self, needle):
        return sum(1 for sql in self.executed if needle in sql)


_TABLE_COLUMNS = ("TABLE_NAME", "TEMPORARY", "DURATION")
_STATISTICS_COLUMNS = ("TABLE_NAME", "NUM_ROWS", "AVG_ROW_LEN", "BLOCKS", "SAMPLE_SIZE", "LAST_ANALYZED")


def _scripted(table_rows, statistics_rows, **extra):
    answers = {
        _TABLES_QUERY: (_TABLE_COLUMNS, table_rows),
        _STATISTICS_QUERY: (_STATISTICS_COLUMNS, statistics_rows),
        _COUNT_QUERY: (("COUNT",), [(7,)]),
    }
    answers.update(extra)
    return _ScriptedConnection(answers)


_ANALYSED = ("ORDERS", Decimal("1500"), Decimal("42"), Decimal("12"), Decimal("1500"), datetime(2026, 10, 1, 12, 0, 5))


def _tables_by_name(connection, **kwargs):
    model = OracleSchemaExtractor(connection, "APP", **kwargs).extract()
    return {table.name: table for table in model.tables}


def _warnings(caplog):
    return [r for r in caplog.records if r.name == extractor_module.__name__ and r.levelno == logging.WARNING]


class TestTableStatisticsExtraction:
    def test_statistics_are_attached_with_converted_values(self):
        connection = _scripted([("ORDERS", "N", None)], [_ANALYSED])
        table = _tables_by_name(connection, include_data=True)["ORDERS"]
        assert (table.num_rows, table.avg_row_len, table.blocks, table.sample_size) == (1500, 42, 12, 1500)
        assert all(type(value) is int for value in (table.num_rows, table.avg_row_len, table.blocks))
        assert table.last_analyzed == "2026-10-01T12:00:05"
        assert table.row_count == 7
        assert connection.count(_STATISTICS_QUERY) == 1

    def test_statistics_query_never_runs_without_include_data(self):
        connection = _scripted([("ORDERS", "N", None)], [_ANALYSED])
        table = _tables_by_name(connection, include_data=False)["ORDERS"]
        assert connection.count(_STATISTICS_QUERY) == 0
        assert table.num_rows is None
        assert table.last_analyzed is None

    def test_empty_result_warns_once_and_exports_without_statistics(self, caplog):
        connection = _scripted([("ORDERS", "N", None), ("ITEMS", "N", None)], [])
        with caplog.at_level(logging.WARNING, logger=extractor_module.__name__):
            tables = _tables_by_name(connection, include_data=True)
        assert all(table.num_rows is None for table in tables.values())
        warnings = _warnings(caplog)
        assert len(warnings) == 1
        assert "USER_TABLES" in warnings[0].getMessage()

    def test_no_warning_when_the_schema_has_no_tables(self, caplog):
        connection = _scripted([], [])
        with caplog.at_level(logging.WARNING, logger=extractor_module.__name__):
            _tables_by_name(connection, include_data=True)
        assert not _warnings(caplog)

    def test_no_warning_when_every_table_was_never_analysed(self, caplog):
        connection = _scripted([("ORDERS", "N", None)], [("ORDERS", None, None, None, None, None)])
        with caplog.at_level(logging.WARNING, logger=extractor_module.__name__):
            tables = _tables_by_name(connection, include_data=True)
        assert tables["ORDERS"].num_rows is None
        assert not _warnings(caplog)

    def test_global_temporary_table_gets_no_statistics(self):
        gtt = ("TMP", Decimal("9"), Decimal("1"), Decimal("1"), Decimal("9"), datetime(2026, 10, 1, 12, 0, 0))
        connection = _scripted([("TMP", "Y", "SYS$TRANSACTION")], [gtt])
        table = _tables_by_name(connection, include_data=True)["TMP"]
        assert table.is_global_temporary
        assert table.num_rows is None
        assert table.last_analyzed is None

    def test_null_num_rows_table_gets_no_statistics(self):
        connection = _scripted(
            [("ORDERS", "N", None), ("NEWT", "N", None)],
            [_ANALYSED, ("NEWT", None, Decimal("5"), None, None, None)],
        )
        tables = _tables_by_name(connection, include_data=True)
        assert tables["ORDERS"].num_rows == 1500
        assert tables["NEWT"].num_rows is None
        assert tables["NEWT"].avg_row_len is None

    def test_null_optional_fields_stay_none(self):
        connection = _scripted([("ORDERS", "N", None)], [("ORDERS", Decimal("3"), None, None, None, None)])
        table = _tables_by_name(connection, include_data=True)["ORDERS"]
        assert table.num_rows == 3
        assert (table.avg_row_len, table.blocks, table.sample_size, table.last_analyzed) == (None,) * 4

    def test_statistics_are_fetched_with_sample_rows_zero(self):
        connection = _scripted([("ORDERS", "N", None)], [_ANALYSED])
        table = _tables_by_name(connection, include_data=True, sample_rows=0)["ORDERS"]
        assert table.num_rows == 1500
        assert table.sample is None

    def test_failed_row_count_keeps_the_statistics(self):
        connection = _scripted(
            [("ORDERS", "N", None)],
            [_ANALYSED],
            **{_COUNT_QUERY: oracledb.DatabaseError("ORA-00942: table or view does not exist")},
        )
        table = _tables_by_name(connection, include_data=True)["ORDERS"]
        assert table.row_count is None
        assert table.num_rows == 1500
        assert table.last_analyzed == "2026-10-01T12:00:05"
