"""Data dictionary SQL for Oracle schema extraction.

Every statement here is a module-level constant built from ``ALL_*`` views
(never ``USER_*`` or ``DBA_*``, since the extracting account may read a schema
it does not own and should never need DBA privileges) and bound with a named
``:schema`` parameter rather than string interpolation. Oracle cannot bind an
object name, so the two row-count/sample-data helpers at the bottom are the
sole exception: they validate the schema and table identifiers against a
strict allowlist pattern before building SQL text, which is why the
per-file ``S608`` (possible SQL injection) lint rule is disabled for this file
in ``pyproject.toml`` rather than fixed with an inline suppression -- there is
no caller-supplied free text reaching either query, only a driver-verified
identifier.
"""

from __future__ import annotations

import re

from ..errors import ExtractionError

# An Oracle unquoted identifier: a letter followed by letters, digits, ``_``,
# ``$`` or ``#``. Anything that fails this is rejected before it can reach a
# SQL string, which is what keeps ``row_count_sql``/``sample_rows_sql`` safe
# despite building their statements by interpolation.
_IDENTIFIER_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_$#]*$")

DATABASE_VERSION = "SELECT banner FROM v$version WHERE ROWNUM = 1"

TABLES = """
SELECT table_name, temporary, duration
FROM all_tables
WHERE owner = :schema
  AND table_name NOT LIKE 'BIN$%'
  AND table_name NOT IN (SELECT mview_name FROM all_mviews WHERE owner = :schema)
  AND nested = 'NO'
  AND iot_type IS NULL
ORDER BY table_name
"""

TABLE_COMMENTS = """
SELECT table_name, comments
FROM all_tab_comments
WHERE owner = :schema
  AND comments IS NOT NULL
"""

COLUMNS = """
SELECT
    tc.table_name,
    tc.column_name,
    tc.data_type,
    tc.data_length,
    tc.data_precision,
    tc.data_scale,
    tc.char_length,
    tc.nullable,
    tc.data_default,
    tc.column_id,
    cc.comments
FROM all_tab_columns tc
LEFT JOIN all_col_comments cc
    ON cc.owner = tc.owner
   AND cc.table_name = tc.table_name
   AND cc.column_name = tc.column_name
WHERE tc.owner = :schema
ORDER BY tc.table_name, tc.column_id
"""

VIEW_COLUMNS = """
SELECT
    tc.table_name,
    tc.column_name,
    tc.data_type,
    tc.data_length,
    tc.data_precision,
    tc.data_scale,
    tc.char_length,
    tc.nullable,
    tc.data_default,
    tc.column_id,
    cc.comments
FROM all_tab_columns tc
LEFT JOIN all_col_comments cc
    ON cc.owner = tc.owner
   AND cc.table_name = tc.table_name
   AND cc.column_name = tc.column_name
WHERE tc.owner = :schema
  AND tc.table_name IN (SELECT view_name FROM all_views WHERE owner = :schema)
ORDER BY tc.table_name, tc.column_id
"""

CONSTRAINTS = """
SELECT
    ac.constraint_name,
    ac.constraint_type,
    ac.table_name,
    ac.search_condition_vc AS search_condition,
    ac.delete_rule,
    acc.column_name,
    acc.position,
    rc.owner AS referenced_owner,
    rc.table_name AS referenced_table,
    rcc.column_name AS referenced_column
FROM all_constraints ac
LEFT JOIN all_cons_columns acc
    ON acc.constraint_name = ac.constraint_name
   AND acc.owner = ac.owner
LEFT JOIN all_constraints rc
    ON rc.constraint_name = ac.r_constraint_name
   AND rc.owner = ac.r_owner
LEFT JOIN all_cons_columns rcc
    ON rcc.constraint_name = rc.constraint_name
   AND rcc.owner = rc.owner
   AND rcc.position = acc.position
WHERE ac.owner = :schema
  AND ac.constraint_type IN ('P', 'U', 'R', 'C')
ORDER BY ac.table_name, ac.constraint_name, acc.position
"""

INDEXES = """
SELECT
    ic.table_name,
    ic.index_name,
    ic.column_name,
    ic.column_position,
    ic.descend,
    ai.uniqueness,
    ai.index_type,
    ie.column_expression
FROM all_ind_columns ic
JOIN all_indexes ai
    ON ai.index_name = ic.index_name
   AND ai.owner = ic.index_owner
LEFT JOIN all_ind_expressions ie
    ON ie.index_owner = ic.index_owner
   AND ie.index_name = ic.index_name
   AND ie.column_position = ic.column_position
WHERE ic.table_owner = :schema
  AND ic.index_name NOT LIKE 'SYS_%'
  AND ai.index_type != 'LOB'
ORDER BY ic.table_name, ic.index_name, ic.column_position
"""

VIEWS = """
SELECT view_name, text_vc
FROM all_views
WHERE owner = :schema
  AND view_name NOT LIKE 'BIN$%'
ORDER BY view_name
"""

VIEW_TEXT_FALLBACK = """
SELECT text
FROM all_views
WHERE owner = :schema
  AND view_name = :view_name
"""

SEQUENCES = """
SELECT sequence_name, min_value, max_value, increment_by, cache_size, cycle_flag, order_flag
FROM all_sequences
WHERE sequence_owner = :schema
ORDER BY sequence_name
"""

PROGRAM_OBJECTS = """
SELECT object_name, object_type, status
FROM all_objects
WHERE owner = :schema
  AND object_type IN ('PROCEDURE', 'FUNCTION', 'PACKAGE', 'PACKAGE BODY')
  AND object_name NOT LIKE 'BIN$%'
ORDER BY object_name, object_type
"""

SOURCE_ALL = """
SELECT name, type, line, text
FROM all_source
WHERE owner = :schema
  AND type IN ('PROCEDURE', 'FUNCTION', 'PACKAGE', 'PACKAGE BODY', 'TYPE', 'TYPE BODY', 'TRIGGER')
ORDER BY name, type, line
"""

TRIGGERS = """
SELECT trigger_name, trigger_type, triggering_event, table_name, status, trigger_body
FROM all_triggers
WHERE owner = :schema
ORDER BY trigger_name
"""

TYPES = """
SELECT type_name, typecode
FROM all_types
WHERE owner = :schema
  AND type_name NOT LIKE 'SYS_%'
ORDER BY type_name
"""

TYPE_ATTRS = """
SELECT type_name, attr_name, attr_type_name, length, precision, scale, attr_no
FROM all_type_attrs
WHERE owner = :schema
ORDER BY type_name, attr_no
"""

SYNONYMS = """
SELECT synonym_name, table_owner, table_name, db_link
FROM all_synonyms
WHERE owner = :schema
ORDER BY synonym_name
"""

DB_LINKS = """
SELECT db_link, username, host
FROM all_db_links
WHERE owner = :schema
ORDER BY db_link
"""

JOBS = """
SELECT job_name, job_type, job_action, schedule_type, repeat_interval, enabled, state, job_class, comments
FROM all_scheduler_jobs
WHERE owner = :schema
  AND job_name NOT LIKE 'SYS_%'
  AND job_name NOT LIKE 'ORA$%'
ORDER BY job_name
"""

MVIEWS = """
SELECT mview_name, query, refresh_method, refresh_mode, build_mode, compile_state
FROM all_mviews
WHERE owner = :schema
  AND mview_name NOT LIKE 'BIN$%'
ORDER BY mview_name
"""

MVIEW_MASTERS = """
SELECT mview_name, detailobj_owner, detailobj_name
FROM all_mview_detail_relations
WHERE owner = :schema
ORDER BY mview_name, detailobj_name
"""

MVIEW_LOGS = """
SELECT log_table, master, log_owner, rowids, primary_key, sequence, include_new_values
FROM all_mview_logs
WHERE log_owner = :schema
ORDER BY log_table
"""


def _validate_identifier(value: str, label: str) -> str:
    """Return ``value`` unchanged after confirming it is a bare Oracle identifier.

    Args:
        value: The candidate schema or table name.
        label: A short noun used in the error message (``"schema"``/``"table"``).

    Returns:
        The validated identifier, unchanged.

    Raises:
        ExtractionError: If ``value`` contains anything other than letters,
            digits, ``_``, ``$`` or ``#``, starting with a letter. This is the
            allowlist that makes the interpolation below safe: nothing that
            reaches the SQL text can be attacker- or caller-controlled free
            text, only a name that already matches Oracle's own identifier
            grammar.
    """
    if not _IDENTIFIER_RE.match(value):
        raise ExtractionError(f"Invalid Oracle {label} identifier: {value!r}")
    return value


def row_count_sql(schema: str, table: str) -> str:
    """Build a ``SELECT COUNT(*)`` statement for one validated table.

    Args:
        schema: The schema that owns the table.
        table: The table name.

    Returns:
        A ready-to-execute SQL statement with no bind parameters.

    Raises:
        ExtractionError: If either identifier fails validation.
    """
    safe_schema = _validate_identifier(schema, "schema")
    safe_table = _validate_identifier(table, "table")
    return f'SELECT COUNT(*) FROM "{safe_schema}"."{safe_table}"'


def sample_rows_sql(schema: str, table: str, limit: int) -> str:
    """Build a bounded ``SELECT *`` statement for one validated table.

    Args:
        schema: The schema that owns the table.
        table: The table name.
        limit: The maximum number of rows to fetch. Must be a positive integer.

    Returns:
        A ready-to-execute SQL statement with no bind parameters.

    Raises:
        ExtractionError: If either identifier fails validation, or ``limit``
            is not a positive integer.
    """
    safe_schema = _validate_identifier(schema, "schema")
    safe_table = _validate_identifier(table, "table")
    if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
        raise ExtractionError(f"Sample row limit must be a positive integer, got {limit!r}")
    return f'SELECT * FROM "{safe_schema}"."{safe_table}" FETCH FIRST {limit} ROWS ONLY'
