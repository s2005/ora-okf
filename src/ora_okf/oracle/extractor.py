"""Build a :class:`~ora_okf.model.SchemaModel` from a live Oracle connection.

The extractor is the only place in the package that knows Oracle data
dictionary column names and shapes; everything downstream of it (rename,
render) works exclusively with the immutable model in :mod:`ora_okf.model`.
Queries fetch one category at a time and the extractor groups the flat rows
in Python, which keeps every query in :mod:`ora_okf.oracle.queries` a single
round trip instead of one query per object.
"""

from __future__ import annotations

import dataclasses
import logging
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from ..errors import ExtractionError
from ..model import (
    OBJECT_TYPE_FUNCTION,
    OBJECT_TYPE_PACKAGE,
    OBJECT_TYPE_PROCEDURE,
    OBJECT_TYPE_TRIGGER,
    Column,
    Constraint,
    ConstraintColumn,
    DbLink,
    Index,
    IndexColumn,
    Job,
    MaterializedView,
    MViewLog,
    ObjectType,
    Program,
    SampleData,
    SchemaModel,
    Sequence,
    Synonym,
    Table,
    TypeAttribute,
    View,
)
from . import queries
from .connection import fetch_all, fetch_scalar

_LOGGER = logging.getLogger(__name__)

_TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S UTC"
_MAX_SAMPLE_VALUE_LENGTH = 200
_TRUNCATED_SAMPLE_VALUE_LENGTH = 197

_ON_COMMIT_BY_DURATION = {
    "SYS$SESSION": "ON COMMIT PRESERVE ROWS",
    "SYS$TRANSACTION": "ON COMMIT DELETE ROWS",
}

_PROGRAM_TYPE_BY_OBJECT_TYPE = {
    "PROCEDURE": OBJECT_TYPE_PROCEDURE,
    "FUNCTION": OBJECT_TYPE_FUNCTION,
}


class OracleSchemaExtractor:
    """Extracts one Oracle schema into an immutable :class:`SchemaModel`.

    Attributes:
        connection: An open ``oracledb.Connection``.
        schema: The schema (owner) to extract, upper-cased by the caller.
        include_data: When True, row counts and bounded samples are collected
            for every non-global-temporary table.
        sample_rows: The maximum number of sample rows per table. A value of
            0 still populates row counts but leaves samples unset.
    """

    def __init__(self, connection: Any, schema: str, *, include_data: bool = False, sample_rows: int = 5) -> None:
        """Initialize the extractor.

        Args:
            connection: An open ``oracledb.Connection``.
            schema: The schema (owner) to extract.
            include_data: Whether to collect row counts and sample rows.
            sample_rows: The maximum sample rows per table.
        """
        self._connection = connection
        self._schema = schema
        self._include_data = include_data
        self._sample_rows = sample_rows

    def extract(self) -> SchemaModel:
        """Run every extraction step and assemble the schema model.

        Returns:
            The fully populated, immutable schema model.
        """
        generated_at = datetime.now(timezone.utc).strftime(_TIMESTAMP_FORMAT)
        database_version = self._extract_database_version()

        tables = self._extract_tables()
        views = self._extract_views()
        sequences = self._extract_sequences()
        programs = self._extract_programs()
        types = self._extract_types()
        synonyms = self._extract_synonyms()
        db_links = self._extract_db_links()
        jobs = self._extract_jobs()
        mviews, mview_masters = self._extract_mviews()
        mview_logs = self._extract_mview_logs()

        referenced_schemas = self._collect_referenced_schemas(
            synonyms=synonyms,
            tables=tables,
            mview_masters=mview_masters,
            mview_logs=mview_logs,
        )

        _LOGGER.info(
            "Extracted schema %s: %d tables, %d views, %d sequences, %d programs, %d types",
            self._schema,
            len(tables),
            len(views),
            len(sequences),
            len(programs),
            len(types),
        )

        return SchemaModel(
            schema_name=self._schema,
            generated_at=generated_at,
            database_version=database_version,
            tables=tables,
            views=views,
            sequences=sequences,
            programs=programs,
            types=types,
            synonyms=synonyms,
            db_links=db_links,
            jobs=jobs,
            mviews=mviews,
            mview_logs=mview_logs,
            referenced_schemas=referenced_schemas,
        )

    # -- database version ------------------------------------------------

    def _extract_database_version(self) -> str:
        """Return the Oracle banner, or "" when the account lacks the grant."""
        try:
            value = fetch_scalar(self._connection, queries.DATABASE_VERSION)
        except ExtractionError:
            _LOGGER.debug("Could not read v$version banner; leaving database_version empty", exc_info=True)
            return ""
        return str(value) if value else ""

    # -- tables ------------------------------------------------------------

    def _extract_tables(self) -> tuple[Table, ...]:
        """Return every table, with comments, columns, constraints, and indexes attached."""
        table_rows = fetch_all(self._connection, queries.TABLES, schema=self._schema)
        comments_by_table = self._fetch_table_comments()
        columns_by_table = self._extract_columns_by_table(queries.COLUMNS)
        constraints_by_table = self._extract_constraints_by_table()
        indexes_by_table = self._extract_indexes_by_table()

        tables = []
        for row in table_rows:
            name = row["table_name"]
            is_global_temporary = row["temporary"] == "Y"
            duration = row["duration"]
            tables.append(
                Table(
                    name=name,
                    comment=comments_by_table.get(name, ""),
                    columns=tuple(columns_by_table.get(name, ())),
                    constraints=tuple(constraints_by_table.get(name, ())),
                    indexes=tuple(indexes_by_table.get(name, ())),
                    is_global_temporary=is_global_temporary,
                    on_commit=_ON_COMMIT_BY_DURATION.get(duration) if duration else None,
                    duration=duration,
                )
            )
        _LOGGER.debug("Extracted %d tables for schema %s", len(tables), self._schema)

        if self._include_data:
            tables = [self._attach_table_data(table) for table in tables]
        return tuple(tables)

    def _fetch_table_comments(self) -> dict[str, str]:
        """Return table comments keyed by table name."""
        rows = fetch_all(self._connection, queries.TABLE_COMMENTS, schema=self._schema)
        return {row["table_name"]: row["comments"] for row in rows}

    def _extract_columns_by_table(self, sql: str) -> dict[str, list[Column]]:
        """Return columns grouped by owning table or view name, in declared order."""
        rows = fetch_all(self._connection, sql, schema=self._schema)
        columns_by_table: dict[str, list[Column]] = defaultdict(list)
        for row in rows:
            columns_by_table[row["table_name"]].append(_build_column(row))
        return columns_by_table

    def _attach_table_data(self, table: Table) -> Table:
        """Return ``table`` with a row count and, when requested, a sample attached."""
        if table.is_global_temporary:
            return table
        try:
            row_count_sql = queries.row_count_sql(self._schema, table.name)
            row_count = fetch_scalar(self._connection, row_count_sql)
        except ExtractionError:
            _LOGGER.warning("Could not read row count for table %s; leaving it unset", table.name, exc_info=True)
            return table

        sample = None
        if self._sample_rows > 0:
            sample = self._fetch_sample(table.name)
        return dataclasses.replace(table, row_count=int(row_count) if row_count is not None else None, sample=sample)

    def _fetch_sample(self, table_name: str) -> SampleData | None:
        """Return a bounded, stringified sample of a table's rows, or None on failure."""
        try:
            sql = queries.sample_rows_sql(self._schema, table_name, self._sample_rows)
            rows = fetch_all(self._connection, sql)
        except ExtractionError:
            _LOGGER.warning("Could not read sample rows for table %s; leaving it unset", table_name, exc_info=True)
            return None
        if not rows:
            return SampleData(columns=(), rows=())
        columns = tuple(rows[0].keys())
        stringified_rows = tuple(tuple(_stringify_sample_value(row[column]) for column in columns) for row in rows)
        return SampleData(columns=columns, rows=stringified_rows)

    # -- constraints ---------------------------------------------------------

    def _extract_constraints_by_table(self) -> dict[str, list[Constraint]]:
        """Return constraints grouped by owning table, assembled from flat join rows."""
        rows = fetch_all(self._connection, queries.CONSTRAINTS, schema=self._schema)
        grouped: dict[str, dict[str, Any]] = {}
        for row in rows:
            _accumulate_constraint_row(grouped, row)

        constraints_by_table: dict[str, list[Constraint]] = defaultdict(list)
        for name, entry in grouped.items():
            constraint = _finalize_constraint(name, entry)
            constraints_by_table[constraint.table_name].append(constraint)
        return constraints_by_table

    # -- indexes ---------------------------------------------------------

    def _extract_indexes_by_table(self) -> dict[str, list[Index]]:
        """Return indexes grouped by owning table, assembled from flat join rows."""
        rows = fetch_all(self._connection, queries.INDEXES, schema=self._schema)
        grouped: dict[str, dict[str, Any]] = {}
        for row in rows:
            _accumulate_index_row(grouped, row)

        indexes_by_table: dict[str, list[Index]] = defaultdict(list)
        for name, entry in grouped.items():
            index = _finalize_index(name, entry)
            indexes_by_table[index.table_name].append(index)
        return indexes_by_table

    # -- views ---------------------------------------------------------

    def _extract_views(self) -> tuple[View, ...]:
        """Return every view, with comments, columns, and definitions attached."""
        view_rows = fetch_all(self._connection, queries.VIEWS, schema=self._schema)
        comments_by_view = self._fetch_table_comments()
        columns_by_view = self._extract_columns_by_table(queries.VIEW_COLUMNS)

        views = []
        for row in view_rows:
            name = row["view_name"]
            definition = row["text_vc"] or self._fetch_view_text_fallback(name)
            views.append(
                View(
                    name=name,
                    comment=comments_by_view.get(name, ""),
                    definition=(definition or "").rstrip(),
                    columns=tuple(columns_by_view.get(name, ())),
                )
            )
        _LOGGER.debug("Extracted %d views for schema %s", len(views), self._schema)
        return tuple(views)

    def _fetch_view_text_fallback(self, view_name: str) -> str:
        """Return the LONG ``text`` column when ``text_vc`` overflowed 4000 chars."""
        rows = fetch_all(self._connection, queries.VIEW_TEXT_FALLBACK, schema=self._schema, view_name=view_name)
        return str(rows[0]["text"]) if rows and rows[0]["text"] is not None else ""

    # -- sequences ---------------------------------------------------------

    def _extract_sequences(self) -> tuple[Sequence, ...]:
        """Return every sequence and its generation properties."""
        rows = fetch_all(self._connection, queries.SEQUENCES, schema=self._schema)
        sequences = tuple(
            Sequence(
                name=row["sequence_name"],
                min_value=_stringify_or_none(row["min_value"]),
                max_value=_stringify_or_none(row["max_value"]),
                increment_by=_stringify_or_none(row["increment_by"]),
                cache_size=_stringify_or_none(row["cache_size"]),
                cycle=row["cycle_flag"] == "Y",
                ordered=row["order_flag"] == "Y",
            )
            for row in rows
        )
        _LOGGER.debug("Extracted %d sequences for schema %s", len(sequences), self._schema)
        return sequences

    # -- programs ---------------------------------------------------------

    def _extract_programs(self) -> tuple[Program, ...]:
        """Return procedures, functions, packages, and triggers with source attached."""
        source_by_key = self._fetch_source_by_key()
        program_rows = fetch_all(self._connection, queries.PROGRAM_OBJECTS, schema=self._schema)

        programs = list(self._build_simple_programs(program_rows, source_by_key))
        programs.extend(self._build_package_programs(program_rows, source_by_key))
        programs.extend(self._build_trigger_programs(source_by_key))
        _LOGGER.debug("Extracted %d programs for schema %s", len(programs), self._schema)
        return tuple(programs)

    def _fetch_source_by_key(self) -> dict[tuple[str, str], str]:
        """Return assembled PL/SQL source text keyed by (name, type)."""
        rows = fetch_all(self._connection, queries.SOURCE_ALL, schema=self._schema)
        lines_by_key: dict[tuple[str, str], list[str]] = defaultdict(list)
        for row in rows:
            key = (row["name"], row["type"])
            lines_by_key[key].append(row["text"] or "")
        return {key: "".join(lines).rstrip() for key, lines in lines_by_key.items()}

    def _build_simple_programs(
        self, program_rows: list[dict[str, Any]], source_by_key: dict[tuple[str, str], str]
    ) -> list[Program]:
        """Return one ``Program`` per standalone procedure or function."""
        programs = []
        for row in program_rows:
            object_type = row["object_type"]
            program_type = _PROGRAM_TYPE_BY_OBJECT_TYPE.get(object_type)
            if program_type is None:
                continue
            name = row["object_name"]
            programs.append(
                Program(
                    name=name,
                    program_type=program_type,
                    source=source_by_key.get((name, object_type), ""),
                    status=row["status"] or "",
                )
            )
        return programs

    def _build_package_programs(
        self, program_rows: list[dict[str, Any]], source_by_key: dict[tuple[str, str], str]
    ) -> list[Program]:
        """Return one ``Program`` per package, merging spec and body status/source."""
        package_names = {row["object_name"] for row in program_rows if row["object_type"] == "PACKAGE"}
        status_by_name = {row["object_name"]: row["status"] for row in program_rows if row["object_type"] == "PACKAGE"}
        programs = []
        for name in sorted(package_names):
            programs.append(
                Program(
                    name=name,
                    program_type=OBJECT_TYPE_PACKAGE,
                    spec_source=source_by_key.get((name, "PACKAGE"), ""),
                    body_source=source_by_key.get((name, "PACKAGE BODY"), ""),
                    status=status_by_name.get(name, "") or "",
                )
            )
        return programs

    def _build_trigger_programs(self, source_by_key: dict[tuple[str, str], str]) -> list[Program]:
        """Return one ``Program`` per trigger, preferring assembled source over ``trigger_body``."""
        rows = fetch_all(self._connection, queries.TRIGGERS, schema=self._schema)
        programs = []
        for row in rows:
            name = row["trigger_name"]
            source = source_by_key.get((name, "TRIGGER")) or str(row["trigger_body"] or "").rstrip()
            programs.append(
                Program(
                    name=name,
                    program_type=OBJECT_TYPE_TRIGGER,
                    source=source,
                    status=row["status"] or "",
                    table_name=row["table_name"],
                    triggering_event=row["triggering_event"],
                    trigger_type=row["trigger_type"],
                )
            )
        return programs

    # -- types ---------------------------------------------------------

    def _extract_types(self) -> tuple[ObjectType, ...]:
        """Return every user-defined type, with attributes and source attached."""
        type_rows = fetch_all(self._connection, queries.TYPES, schema=self._schema)
        attrs_by_type = self._extract_type_attrs()
        source_by_key = self._fetch_source_by_key()

        types = tuple(
            ObjectType(
                name=row["type_name"],
                typecode=row["typecode"] or "",
                attributes=tuple(attrs_by_type.get(row["type_name"], ())),
                source=source_by_key.get((row["type_name"], "TYPE"), ""),
            )
            for row in type_rows
        )
        _LOGGER.debug("Extracted %d types for schema %s", len(types), self._schema)
        return types

    def _extract_type_attrs(self) -> dict[str, list[TypeAttribute]]:
        """Return type attributes grouped by owning type, in declared order."""
        rows = fetch_all(self._connection, queries.TYPE_ATTRS, schema=self._schema)
        attrs_by_type: dict[str, list[TypeAttribute]] = defaultdict(list)
        for row in rows:
            attrs_by_type[row["type_name"]].append(
                TypeAttribute(name=row["attr_name"], data_type=_format_attr_data_type(row))
            )
        return attrs_by_type

    # -- synonyms, db links, jobs ---------------------------------------

    def _extract_synonyms(self) -> tuple[Synonym, ...]:
        """Return every synonym owned by the schema."""
        rows = fetch_all(self._connection, queries.SYNONYMS, schema=self._schema)
        synonyms = tuple(
            Synonym(
                name=row["synonym_name"],
                target_owner=row["table_owner"],
                target_name=row["table_name"] or "",
                db_link=row["db_link"],
                is_public=False,
            )
            for row in rows
        )
        _LOGGER.debug("Extracted %d synonyms for schema %s", len(synonyms), self._schema)
        return synonyms

    def _extract_db_links(self) -> tuple[DbLink, ...]:
        """Return every database link owned by the schema. Passwords are never read."""
        rows = fetch_all(self._connection, queries.DB_LINKS, schema=self._schema)
        return tuple(DbLink(name=row["db_link"], username=row["username"], host=row["host"]) for row in rows)

    def _extract_jobs(self) -> tuple[Job, ...]:
        """Return every DBMS_SCHEDULER job owned by the schema."""
        rows = fetch_all(self._connection, queries.JOBS, schema=self._schema)
        return tuple(
            Job(
                name=row["job_name"],
                job_type=row["job_type"] or "",
                job_action=row["job_action"] or "",
                schedule_type=row["schedule_type"] or "",
                repeat_interval=row["repeat_interval"] or "",
                enabled=row["enabled"] == "TRUE" if row["enabled"] is not None else None,
                state=row["state"] or "",
                job_class=row["job_class"] or "",
                comments=row["comments"] or "",
            )
            for row in rows
        )

    # -- materialized views ---------------------------------------------

    def _extract_mviews(self) -> tuple[tuple[MaterializedView, ...], list[dict[str, Any]]]:
        """Return materialized views with their master tables attached.

        Returns:
            A tuple of the materialized views, and the raw master-relation
            rows (needed again by :meth:`_collect_referenced_schemas`).
        """
        rows = fetch_all(self._connection, queries.MVIEWS, schema=self._schema)
        master_rows = fetch_all(self._connection, queries.MVIEW_MASTERS, schema=self._schema)
        masters_by_mview: dict[str, list[str]] = defaultdict(list)
        for master_row in master_rows:
            masters_by_mview[master_row["mview_name"]].append(_qualified_master(master_row))

        mviews = tuple(
            MaterializedView(
                name=row["mview_name"],
                query=str(row["query"] or "").rstrip(),
                refresh_method=row["refresh_method"] or "",
                refresh_mode=row["refresh_mode"] or "",
                build_mode=row["build_mode"] or "",
                compile_state=row["compile_state"] or "",
                master_tables=tuple(masters_by_mview.get(row["mview_name"], ())),
            )
            for row in rows
        )
        _LOGGER.debug("Extracted %d materialized views for schema %s", len(mviews), self._schema)
        return mviews, master_rows

    def _extract_mview_logs(self) -> tuple[MViewLog, ...]:
        """Return every materialized view log owned by the schema."""
        rows = fetch_all(self._connection, queries.MVIEW_LOGS, schema=self._schema)
        return tuple(
            MViewLog(
                name=row["log_table"],
                master_table=row["master"] or "",
                master_owner=None,
                rowids=_yn_to_optional_bool(row["rowids"]),
                primary_key=_yn_to_optional_bool(row["primary_key"]),
                sequence=_yn_to_optional_bool(row["sequence"]),
                include_new_values=_yn_to_optional_bool(row["include_new_values"]),
            )
            for row in rows
        )

    # -- referenced schemas ---------------------------------------------

    def _collect_referenced_schemas(
        self,
        *,
        synonyms: tuple[Synonym, ...],
        tables: tuple[Table, ...],
        mview_masters: list[dict[str, Any]],
        mview_logs: tuple[MViewLog, ...],
    ) -> tuple[str, ...]:
        """Return every other schema referenced by extracted objects, sorted and upper-cased.

        ``mview_logs`` never contributes a candidate: ``MVIEW_LOGS`` is
        queried with ``log_owner = :schema``, so a log's owner is always the
        exported schema itself. The parameter is accepted anyway so the
        signature documents every category the caller considered rather than
        looking incomplete.

        Excludes the exported schema itself, ``PUBLIC``, and any None/empty
        value.
        """
        del mview_logs
        candidates: set[str] = set()
        for synonym in synonyms:
            candidates.add(synonym.target_owner or "")
        for table in tables:
            for constraint in table.constraints:
                candidates.add(constraint.referenced_owner or "")
        for master_row in mview_masters:
            candidates.add(master_row.get("detailobj_owner") or "")

        excluded = {"", "PUBLIC", self._schema.upper()}
        return tuple(sorted({candidate.upper() for candidate in candidates if candidate.upper() not in excluded}))


def _qualified_master(master_row: dict[str, Any]) -> str:
    """Return a materialized view's master table as ``OWNER.NAME``.

    The owner is kept rather than discarded because a master in another schema
    is exactly the kind of cross-schema reference the rename pass exists to
    rewrite: :meth:`ora_okf.rename.SchemaRenamer._rename_qualified` splits this
    string on its first dot and remaps the owner. Dropping the owner would make
    a foreign master read as a local one, and would silently put the reference
    beyond the renamer's reach.

    Args:
        master_row: One ``all_mview_detail_relations`` row.

    Returns:
        ``OWNER.NAME``, or the bare name when the dictionary reports no owner.
    """
    name = str(master_row.get("detailobj_name") or "")
    owner = str(master_row.get("detailobj_owner") or "")
    return f"{owner}.{name}" if owner and name else name


def format_data_type(row: dict[str, Any]) -> str:
    """Assemble a column's full type text, including size or precision modifiers.

    Args:
        row: A raw row from :data:`ora_okf.oracle.queries.COLUMNS` (or
            ``VIEW_COLUMNS``), with keys ``data_type``, ``data_length``,
            ``data_precision``, ``data_scale``, and ``char_length``.

    Returns:
        The type as it would be written in DDL, e.g. ``NUMBER(10,2)`` or
        ``VARCHAR2(50)``. Bare ``data_type`` when no modifier applies.
    """
    data_type = str(row["data_type"])
    precision = row["data_precision"]
    if precision is not None:
        scale = row["data_scale"]
        if scale:
            return f"{data_type}({precision},{scale})"
        return f"{data_type}({precision})"

    char_types = {"VARCHAR2", "NVARCHAR2", "CHAR", "NCHAR", "VARCHAR"}
    if data_type in char_types:
        size = row["char_length"] or row["data_length"]
        if isinstance(size, int) and size > 0:
            return f"{data_type}({size})"
        return data_type

    if data_type == "RAW":
        size = row["data_length"]
        if isinstance(size, int) and size > 0:
            return f"{data_type}({size})"
        return data_type

    return data_type


def _build_column(row: dict[str, Any]) -> Column:
    """Build one :class:`Column` from a raw ``COLUMNS``/``VIEW_COLUMNS`` row."""
    return Column(
        name=row["column_name"],
        data_type=format_data_type(row),
        nullable=row["nullable"] == "Y",
        default_value=_clean_default(row["data_default"]),
        comment=row["comments"] or "",
        position=int(row["column_id"]) if row["column_id"] is not None else 0,
    )


def _clean_default(raw_default: Any) -> str | None:
    """Return a column default with trailing whitespace stripped, or None when empty/NULL."""
    if raw_default is None:
        return None
    text = str(raw_default).rstrip()
    if not text or text.upper() == "NULL":
        return None
    return text


def _accumulate_constraint_row(grouped: dict[str, dict[str, Any]], row: dict[str, Any]) -> None:
    """Fold one flat ``CONSTRAINTS`` join row into the in-progress group for its constraint."""
    name = row["constraint_name"]
    entry = grouped.setdefault(
        name,
        {
            "constraint_type": row["constraint_type"],
            "table_name": row["table_name"],
            "search_condition": row["search_condition"],
            "delete_rule": row["delete_rule"],
            "referenced_owner": row["referenced_owner"],
            "referenced_table": row["referenced_table"],
            "columns": {},
            "referenced_columns": {},
        },
    )
    if row["column_name"] is not None and row["position"] is not None:
        entry["columns"][int(row["position"])] = row["column_name"]
    if row["constraint_type"] == "R" and row["referenced_column"] is not None and row["position"] is not None:
        entry["referenced_columns"][int(row["position"])] = row["referenced_column"]


def _finalize_constraint(name: str, entry: dict[str, Any]) -> Constraint:
    """Build one :class:`Constraint` from an accumulated group, ordering columns by position."""
    columns = tuple(
        ConstraintColumn(name=entry["columns"][position], position=position) for position in sorted(entry["columns"])
    )
    referenced_positions = sorted(entry["referenced_columns"])
    referenced_columns = tuple(entry["referenced_columns"][position] for position in referenced_positions)
    return Constraint(
        name=name,
        constraint_type=entry["constraint_type"],
        table_name=entry["table_name"],
        columns=columns,
        referenced_owner=entry["referenced_owner"],
        referenced_table=entry["referenced_table"],
        referenced_columns=referenced_columns,
        delete_rule=entry["delete_rule"],
        search_condition=entry["search_condition"],
    )


def _accumulate_index_row(grouped: dict[str, dict[str, Any]], row: dict[str, Any]) -> None:
    """Fold one flat ``INDEXES`` join row into the in-progress group for its index."""
    name = row["index_name"]
    entry = grouped.setdefault(
        name,
        {
            "table_name": row["table_name"],
            "unique": row["uniqueness"] == "UNIQUE",
            "index_type": row["index_type"] or "",
            "columns": {},
        },
    )
    position = row["column_position"]
    if position is None:
        return
    column_name = row["column_expression"] or row["column_name"]
    entry["columns"][int(position)] = (column_name, row["descend"] == "DESC")


def _finalize_index(name: str, entry: dict[str, Any]) -> Index:
    """Build one :class:`Index` from an accumulated group, ordering columns by position."""
    columns = tuple(
        IndexColumn(name=column_name, position=position, descending=descending)
        for position, (column_name, descending) in sorted(entry["columns"].items())
    )
    return Index(
        name=name,
        table_name=entry["table_name"],
        unique=entry["unique"],
        index_type=entry["index_type"],
        columns=columns,
    )


def _format_attr_data_type(row: dict[str, Any]) -> str:
    """Assemble a type attribute's full type text from ``TYPE_ATTRS`` columns."""
    data_type = str(row["attr_type_name"])
    precision = row["precision"]
    if precision is not None:
        scale = row["scale"]
        if scale:
            return f"{data_type}({precision},{scale})"
        return f"{data_type}({precision})"
    length = row["length"]
    if isinstance(length, int) and length > 0:
        return f"{data_type}({length})"
    return data_type


def _yn_to_optional_bool(value: Any) -> bool | None:
    """Convert an Oracle ``Y``/``N`` flag to a bool, or None when the column itself is NULL."""
    return value == "Y" if value is not None else None


def _stringify_or_none(value: Any) -> str | None:
    """Return ``str(value)``, or None when the value itself is None."""
    return str(value) if value is not None else None


def _stringify_sample_value(value: Any) -> str:
    """Convert one sample cell to a bounded display string.

    Args:
        value: The raw fetched value: None, a string, a LOB already read to
            ``str`` by :func:`ora_okf.oracle.connection.fetch_all`, a
            ``datetime``, or a numeric type.

    Returns:
        The stringified value, truncated to 200 characters (197 plus an
        ellipsis) when longer.
    """
    if value is None:
        text = ""
    elif hasattr(value, "read"):
        text = str(value.read())
    elif isinstance(value, datetime):
        text = value.isoformat(sep=" ")
    else:
        text = str(value)

    if len(text) > _MAX_SAMPLE_VALUE_LENGTH:
        return text[:_TRUNCATED_SAMPLE_VALUE_LENGTH] + "..."
    return text
