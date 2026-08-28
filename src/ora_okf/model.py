"""Immutable schema model produced by extraction and consumed by rendering.

Every record is a frozen dataclass holding tuples rather than lists, which buys
three things the rest of the package relies on:

* **Determinism.** A model is hashable and order-stable, so rendering the same
  model twice produces byte-identical output.
* **Safe renaming.** :mod:`ora_okf.rename` rebuilds a model with
  ``dataclasses.replace`` instead of mutating shared state, so the physical and
  renamed models can coexist (the audit needs both).
* **A single extraction contract.** The extractor is the only place that knows
  Oracle data dictionary column names; everything downstream sees these fields.

Names are stored exactly as Oracle reports them (upper case for conventional
identifiers). Schema-qualified fields such as ``Synonym.target_owner`` keep the
owner separate from the object name so renaming never has to parse a string.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Object type keys used as the stable vocabulary across the renderers, the
# category map, and the log/index summaries.
OBJECT_TYPE_TABLE = "tables"
OBJECT_TYPE_VIEW = "views"
OBJECT_TYPE_SEQUENCE = "sequences"
OBJECT_TYPE_PROCEDURE = "procedures"
OBJECT_TYPE_FUNCTION = "functions"
OBJECT_TYPE_PACKAGE = "packages"
OBJECT_TYPE_TRIGGER = "triggers"
OBJECT_TYPE_TYPE = "types"
OBJECT_TYPE_SYNONYM = "synonyms"
OBJECT_TYPE_DB_LINK = "db_links"
OBJECT_TYPE_JOB = "jobs"
OBJECT_TYPE_MVIEW = "mviews"
OBJECT_TYPE_MVIEW_LOG = "mview_logs"


@dataclass(frozen=True)
class Column:
    """One table or view column.

    Attributes:
        name: The column name.
        data_type: The type with its size/precision modifiers already applied
            (``VARCHAR2(50)``, ``NUMBER(10,2)``), so renderers never reassemble it.
        nullable: True when the column accepts NULL.
        default_value: The column default as written in the dictionary, or None.
        comment: The column comment, or an empty string.
        position: The 1-based ``column_id`` used to preserve declaration order.
    """

    name: str
    data_type: str
    nullable: bool = True
    default_value: str | None = None
    comment: str = ""
    position: int = 0


@dataclass(frozen=True)
class ConstraintColumn:
    """A column participating in a constraint, with its 1-based position."""

    name: str
    position: int = 1


@dataclass(frozen=True)
class Constraint:
    """A primary key, unique, foreign key, or check constraint.

    Attributes:
        name: The constraint name.
        constraint_type: The single-letter Oracle type (``P``, ``U``, ``R``, ``C``).
        table_name: The owning table.
        columns: The constrained columns in position order.
        referenced_owner: For a foreign key, the owner of the referenced table.
            Kept separate from ``referenced_table`` so renaming can rewrite the
            owner without string surgery. None for a same-schema reference that
            Oracle reported without an owner.
        referenced_table: For a foreign key, the referenced table name.
        referenced_columns: For a foreign key, the referenced columns in order.
        delete_rule: For a foreign key, ``CASCADE`` / ``SET NULL`` / ``NO ACTION``.
        search_condition: For a check constraint, its predicate text.
    """

    name: str
    constraint_type: str
    table_name: str
    columns: tuple[ConstraintColumn, ...] = ()
    referenced_owner: str | None = None
    referenced_table: str | None = None
    referenced_columns: tuple[str, ...] = ()
    delete_rule: str | None = None
    search_condition: str | None = None


@dataclass(frozen=True)
class IndexColumn:
    """An indexed column or function-based expression, with sort direction."""

    name: str
    position: int = 1
    descending: bool = False


@dataclass(frozen=True)
class Index:
    """An index owned by a table."""

    name: str
    table_name: str
    unique: bool = False
    index_type: str = ""
    columns: tuple[IndexColumn, ...] = ()


@dataclass(frozen=True)
class SampleData:
    """A bounded sample of table rows, rendered under ``--include-data``.

    Values are pre-stringified by the extractor so the renderer never formats a
    ``datetime`` or ``Decimal`` (which would make output depend on the client's
    locale rather than on the data).
    """

    columns: tuple[str, ...] = ()
    rows: tuple[tuple[str, ...], ...] = ()


@dataclass(frozen=True)
class Table:
    """A table or global temporary table with its constraints and indexes."""

    name: str
    comment: str = ""
    columns: tuple[Column, ...] = ()
    constraints: tuple[Constraint, ...] = ()
    indexes: tuple[Index, ...] = ()
    is_global_temporary: bool = False
    on_commit: str | None = None
    duration: str | None = None
    row_count: int | None = None
    sample: SampleData | None = None


@dataclass(frozen=True)
class View:
    """A view with its projected columns and defining query."""

    name: str
    comment: str = ""
    definition: str = ""
    columns: tuple[Column, ...] = ()


@dataclass(frozen=True)
class Sequence:
    """A sequence and its generation properties."""

    name: str
    min_value: str | None = None
    max_value: str | None = None
    increment_by: str | None = None
    cache_size: str | None = None
    cycle: bool | None = None
    ordered: bool | None = None


@dataclass(frozen=True)
class Program:
    """A stored PL/SQL program: procedure, function, package, or trigger.

    Attributes:
        name: The program name.
        program_type: One of the ``OBJECT_TYPE_*`` program keys, which selects
            both the concept category and the body renderer.
        source: Assembled source for a procedure, function, or trigger.
        spec_source: Package specification source; empty for other kinds.
        body_source: Package body source; empty for other kinds.
        status: The dictionary ``STATUS`` (``VALID`` / ``INVALID``).
        table_name: For a trigger, the table it fires on.
        triggering_event: For a trigger, the DML/DDL event.
        trigger_type: For a trigger, e.g. ``BEFORE EACH ROW``.
    """

    name: str
    program_type: str
    source: str = ""
    spec_source: str = ""
    body_source: str = ""
    status: str = ""
    table_name: str | None = None
    triggering_event: str | None = None
    trigger_type: str | None = None


@dataclass(frozen=True)
class TypeAttribute:
    """One attribute of an object type, with its size/precision modifiers."""

    name: str
    data_type: str


@dataclass(frozen=True)
class ObjectType:
    """A user-defined object or collection type."""

    name: str
    typecode: str = ""
    attributes: tuple[TypeAttribute, ...] = ()
    source: str = ""


@dataclass(frozen=True)
class Synonym:
    """A synonym and the object it points at.

    ``target_owner`` is the physical owner of the target, which may be a schema
    other than the exported one. Renaming rewrites it through the same mapping
    as every other schema reference, which is what keeps a cross-schema synonym
    from leaking a physical name into the bundle.
    """

    name: str
    target_owner: str | None = None
    target_name: str = ""
    db_link: str | None = None
    is_public: bool = False


@dataclass(frozen=True)
class DbLink:
    """A database link. The stored password is never extracted."""

    name: str
    username: str | None = None
    host: str | None = None


@dataclass(frozen=True)
class Job:
    """A DBMS_SCHEDULER job."""

    name: str
    job_type: str = ""
    job_action: str = ""
    schedule_type: str = ""
    repeat_interval: str = ""
    enabled: bool | None = None
    state: str = ""
    job_class: str = ""
    comments: str = ""


@dataclass(frozen=True)
class MaterializedView:
    """A materialized view and its refresh configuration."""

    name: str
    query: str = ""
    refresh_method: str = ""
    refresh_mode: str = ""
    build_mode: str = ""
    compile_state: str = ""
    master_tables: tuple[str, ...] = ()


@dataclass(frozen=True)
class MViewLog:
    """A materialized view log and the master table it records changes for."""

    name: str
    master_table: str = ""
    master_owner: str | None = None
    rowids: bool | None = None
    primary_key: bool | None = None
    sequence: bool | None = None
    include_new_values: bool | None = None
    filter_columns: tuple[str, ...] = ()


@dataclass(frozen=True)
class SchemaModel:
    """The complete extracted schema.

    Attributes:
        schema_name: The physical schema the model was extracted from. Renaming
            replaces this with the mapped label, so a renamed model no longer
            carries the physical name anywhere.
        generated_at: Extraction timestamp, stamped once by the extractor and
            never re-read from a clock during rendering, which is what keeps
            rendering deterministic.
        database_version: The Oracle banner version, for the bundle log.
    """

    schema_name: str
    generated_at: str = ""
    database_version: str = ""
    tables: tuple[Table, ...] = ()
    views: tuple[View, ...] = ()
    sequences: tuple[Sequence, ...] = ()
    programs: tuple[Program, ...] = ()
    types: tuple[ObjectType, ...] = ()
    synonyms: tuple[Synonym, ...] = ()
    db_links: tuple[DbLink, ...] = ()
    jobs: tuple[Job, ...] = ()
    mviews: tuple[MaterializedView, ...] = ()
    mview_logs: tuple[MViewLog, ...] = ()
    # Schemas other than ``schema_name`` referenced from this model (synonym
    # targets, foreign key owners, materialized view masters). Populated by the
    # extractor so the CLI can report exactly which mappings a bundle needs.
    referenced_schemas: tuple[str, ...] = field(default_factory=tuple)

    def object_count(self) -> int:
        """Return the number of rendered concepts this model produces."""
        return (
            len(self.tables)
            + len(self.views)
            + len(self.sequences)
            + len(self.programs)
            + len(self.types)
            + len(self.synonyms)
            + len(self.db_links)
            + len(self.jobs)
            + len(self.mviews)
            + len(self.mview_logs)
        )

    def is_empty(self) -> bool:
        """Return True when the model holds no objects at all."""
        return self.object_count() == 0
