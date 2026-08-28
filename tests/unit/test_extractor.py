"""Tests for pure extractor logic: type formatting, query building, and a
bare extraction round trip against a fake connection (no real database).
"""

from __future__ import annotations

import pytest

from ora_okf.errors import ExtractionError
from ora_okf.oracle.extractor import OracleSchemaExtractor, format_data_type
from ora_okf.oracle.queries import row_count_sql, sample_rows_sql


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
