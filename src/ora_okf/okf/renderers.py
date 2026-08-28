"""Render an OKF Markdown bundle from a :class:`~ora_okf.model.SchemaModel`.

The bundle is a directory of cross-linked concept files (one per database
object) plus three reserved files: ``index.md``, ``log.md``, and ``schema.md``.
Everything here is pure and deterministic -- no clock, no filesystem, no set
iteration order -- because :func:`render_bundle` must produce byte-identical
output for the same model every time it runs. That property is what lets a
generated bundle be diffed against a previous one in CI.

Object placement and concept rendering are split into two passes
(:func:`build_placements` then per-object rendering) so every cross-link (a
foreign key naming its parent table, for instance) is resolved against a
*fully* populated :class:`~ora_okf.okf.paths.ConceptPathAllocator`. Rendering
while allocating in the same pass would let an early object link to a name
that has not been allocated yet, and the allocator would hand back a
provisional path that a later collision could invalidate.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import NamedTuple, cast

from ..model import (
    OBJECT_TYPE_DB_LINK,
    OBJECT_TYPE_FUNCTION,
    OBJECT_TYPE_JOB,
    OBJECT_TYPE_MVIEW,
    OBJECT_TYPE_MVIEW_LOG,
    OBJECT_TYPE_PACKAGE,
    OBJECT_TYPE_PROCEDURE,
    OBJECT_TYPE_SEQUENCE,
    OBJECT_TYPE_SYNONYM,
    OBJECT_TYPE_TABLE,
    OBJECT_TYPE_TRIGGER,
    OBJECT_TYPE_TYPE,
    OBJECT_TYPE_VIEW,
    Column,
    Constraint,
    DbLink,
    Index,
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
    View,
)
from .concept import OkfConcept
from .markdown import inline_code, join_blocks, render_code_block, render_table
from .paths import OBJECT_CATEGORIES, ConceptPathAllocator

# Human-readable object kind per object-type key. Drives both the frontmatter
# ``type`` value (``"<dbms_label> <kind>"``) and the default description.
_KIND_LABELS: dict[str, str] = {
    OBJECT_TYPE_TABLE: "Table",
    OBJECT_TYPE_VIEW: "View",
    OBJECT_TYPE_SEQUENCE: "Sequence",
    OBJECT_TYPE_PROCEDURE: "Procedure",
    OBJECT_TYPE_FUNCTION: "Function",
    OBJECT_TYPE_PACKAGE: "Package",
    OBJECT_TYPE_TRIGGER: "Trigger",
    OBJECT_TYPE_TYPE: "Type",
    OBJECT_TYPE_SYNONYM: "Synonym",
    OBJECT_TYPE_DB_LINK: "Database Link",
    OBJECT_TYPE_JOB: "Scheduler Job",
    OBJECT_TYPE_MVIEW: "Materialized View",
    OBJECT_TYPE_MVIEW_LOG: "Materialized View Log",
}


@dataclass(frozen=True)
class RenderConfig:
    """Rendering knobs supplied by the caller, never inferred from the model.

    Attributes:
        schema_label: The name shown in ``schema.md`` / ``index.md`` and used
            to qualify ``resource:`` values. Deliberately independent of
            ``model.schema_name`` -- a renamed model's caller decides what
            label the bundle should show.
        qualify_resources: Whether ``resource:`` values are prefixed with
            ``"<schema_label>."``. When False, a bare name is used.
        dbms_label: The prefix used in every concept's frontmatter ``type``
            (``"Oracle Table"``, ``"Oracle Package"``).
        include_data: Whether table row counts and sample rows are rendered.
        sample_rows: The row cap recorded in ``log.md`` for reference.
        generator: The tool name recorded in ``index.md``.
        include_timestamp: Whether the extraction timestamp is written into
            concept frontmatter and ``log.md``. Turning it off makes a bundle
            that only changes when the schema changes, so a committed bundle
            does not show up as modified on every rerun.
    """

    schema_label: str
    qualify_resources: bool = True
    dbms_label: str = "Oracle"
    include_data: bool = False
    sample_rows: int = 5
    generator: str = "ora-okf"
    include_timestamp: bool = True


class ConceptPlacement(NamedTuple):
    """One object's allocated location in the bundle."""

    object_type: str
    category: str
    name: str
    path: str


# A per-object concept builder. Every builder has the same signature so the
# dispatch table in ``_render_concept`` can stay a single lookup rather than a
# long if/elif chain (keeps cyclomatic complexity low).
_ConceptBuilder = Callable[[object, RenderConfig, ConceptPathAllocator, str], OkfConcept]


def _resource(name: str, config: RenderConfig) -> str:
    """Return the frontmatter ``resource`` value for ``name``.

    The caller has already decided the schema label; this never consults any
    other source of truth, so renaming and cross-schema exports stay correct
    without this module knowing about either.
    """
    return f"{config.schema_label}.{name}" if config.qualify_resources else name


def _tag_for(object_type: str, obj: object) -> str:
    """Return the single frontmatter tag for an object.

    A global temporary table gets its own tag (``global-temporary-table``)
    because it is a materially different kind of object to document, even
    though it shares the ``tables`` category and concept shape with a normal
    table.
    """
    if object_type == OBJECT_TYPE_TABLE and getattr(obj, "is_global_temporary", False):
        return "global-temporary-table"
    return _KIND_LABELS[object_type].lower().replace(" ", "-")


def _base_frontmatter(
    object_type: str, obj: object, name: str, description: str, config: RenderConfig, generated_at: str
) -> dict[str, object]:
    """Build the frontmatter fields common to every concept."""
    kind = _KIND_LABELS[object_type]
    frontmatter: dict[str, object] = {
        "type": f"{config.dbms_label} {kind}",
        "title": name,
        "description": description,
        "resource": _resource(name, config),
        "tags": [_tag_for(object_type, obj)],
    }
    if config.include_timestamp:
        frontmatter["timestamp"] = generated_at
    return frontmatter


def _sql_escape(text: str) -> str:
    """Collapse whitespace to one line and double single quotes for SQL text."""
    return " ".join(text.split()).replace("'", "''")


def _collect_items(model: SchemaModel) -> list[tuple[str, str, object]]:
    """Return every object in the model as ``(object_type, name, obj)``.

    Sorted by ``(category, name.lower(), name, object_type)`` so that both
    :func:`build_placements` and :func:`render_bundle` allocate paths in the
    same content-derived order -- the source of the determinism guarantee,
    including which collision suffix a repeated name gets.
    """
    items: list[tuple[str, str, object]] = []
    items.extend((OBJECT_TYPE_TABLE, t.name, t) for t in model.tables)
    items.extend((OBJECT_TYPE_VIEW, v.name, v) for v in model.views)
    items.extend((OBJECT_TYPE_SEQUENCE, s.name, s) for s in model.sequences)
    items.extend((p.program_type, p.name, p) for p in model.programs)
    items.extend((OBJECT_TYPE_TYPE, ty.name, ty) for ty in model.types)
    items.extend((OBJECT_TYPE_SYNONYM, sy.name, sy) for sy in model.synonyms)
    items.extend((OBJECT_TYPE_DB_LINK, dl.name, dl) for dl in model.db_links)
    items.extend((OBJECT_TYPE_JOB, j.name, j) for j in model.jobs)
    items.extend((OBJECT_TYPE_MVIEW, m.name, m) for m in model.mviews)
    items.extend((OBJECT_TYPE_MVIEW_LOG, ml.name, ml) for ml in model.mview_logs)
    items.sort(key=lambda item: (OBJECT_CATEGORIES[item[0]], item[1].lower(), item[1], item[0]))
    return items


def build_placements(model: SchemaModel) -> tuple[ConceptPathAllocator, list[ConceptPlacement]]:
    """Allocate a bundle path for every object in the model.

    Args:
        model: The schema to place.

    Returns:
        The populated allocator (reusable for cross-link resolution) and the
        list of placements in allocation order.
    """
    allocator = ConceptPathAllocator()
    placements: list[ConceptPlacement] = []
    for object_type, name, _obj in _collect_items(model):
        category = OBJECT_CATEGORIES[object_type]
        path = allocator.allocate(category, name)
        placements.append(ConceptPlacement(object_type, category, name, path))
    return allocator, placements


def _category_counts(model: SchemaModel) -> dict[str, int]:
    """Return the number of concepts placed in each category directory."""
    counts: dict[str, int] = {}
    for object_type, _name, _obj in _collect_items(model):
        category = OBJECT_CATEGORIES[object_type]
        counts[category] = counts.get(category, 0) + 1
    return counts


def render_bundle(model: SchemaModel, config: RenderConfig) -> list[tuple[str, str]]:
    """Render every file in the OKF bundle.

    Args:
        model: The schema to render.
        config: Rendering options.

    Returns:
        Every ``(relative_path, content)`` pair, sorted by path.
    """
    items = _collect_items(model)
    allocator = ConceptPathAllocator()
    placed: list[tuple[str, str, object]] = []
    for object_type, name, obj in items:
        category = OBJECT_CATEGORIES[object_type]
        path = allocator.allocate(category, name)
        placed.append((path, object_type, obj))

    results: list[tuple[str, str]] = []
    for path, object_type, obj in placed:
        concept = _render_concept(object_type, obj, config, allocator, model.generated_at)
        results.append((path, concept.render()))

    results.append(("index.md", render_index(model, config)))
    results.append(("log.md", render_log(model, config)))
    results.append(("schema.md", render_schema_overview(model, config).render()))
    results.sort(key=lambda pair: pair[0])
    return results


def render_index(model: SchemaModel, config: RenderConfig) -> str:
    """Render the bundle's root ``index.md``."""
    counts = _category_counts(model)
    rows = [[f"[{category}](/{category}/)", str(count)] for category, count in sorted(counts.items())]
    content = join_blocks(
        f"# {config.schema_label}",
        render_table(["Category", "Count"], rows),
        f"Total concepts: {model.object_count()}",
        f"Generated by: {config.generator}",
    )
    return content.strip() + "\n"


def render_log(model: SchemaModel, config: RenderConfig) -> str:
    """Render the bundle's root ``log.md``."""
    rows = [
        ["dbms", config.dbms_label],
        ["database_version", model.database_version],
        ["concepts", str(model.object_count())],
        ["include_data", "YES" if config.include_data else "NO"],
        ["sample_rows", str(config.sample_rows)],
    ]
    if config.include_timestamp:
        rows.insert(0, ["timestamp", model.generated_at])
    content = join_blocks("# Log", render_table(["Field", "Value"], rows))
    return content.strip() + "\n"


def render_schema_overview(model: SchemaModel, config: RenderConfig) -> OkfConcept:
    """Render the ``schema.md`` concept summarizing the whole schema."""
    frontmatter: dict[str, object] = {
        "type": f"{config.dbms_label} Schema",
        "title": config.schema_label,
        "description": f"Schema {config.schema_label}.",
        "resource": config.schema_label,
        "tags": ["schema"],
    }
    if config.include_timestamp:
        frontmatter["timestamp"] = model.generated_at
    concept = OkfConcept(frontmatter=frontmatter)
    counts = _category_counts(model)
    rows = [[category, str(count)] for category, count in sorted(counts.items())]
    concept.add_section("Contents", render_table(["Category", "Count"], rows) if rows else "")
    return concept


def _render_concept(
    object_type: str, obj: object, config: RenderConfig, allocator: ConceptPathAllocator, generated_at: str
) -> OkfConcept:
    """Dispatch to the concept builder registered for ``object_type``."""
    return _BUILDERS[object_type](obj, config, allocator, generated_at)


# --------------------------------------------------------------------------
# Column tables, shared by Table and View.
# --------------------------------------------------------------------------


def _render_columns_table(columns: tuple[Column, ...]) -> str:
    """Render a Column/Type/Nullable/Default table, adding Comment if used."""
    has_comment = any(column.comment for column in columns)
    headers = ["Column", "Type", "Nullable", "Default"]
    if has_comment:
        headers.append("Comment")
    rows = []
    for column in sorted(columns, key=lambda c: c.position):
        row = [column.name, column.data_type, "YES" if column.nullable else "NO", column.default_value or ""]
        if has_comment:
            row.append(column.comment or "")
        rows.append(row)
    return render_table(headers, rows)


# --------------------------------------------------------------------------
# Table.
# --------------------------------------------------------------------------


def _constraint_columns_text(constraint: Constraint) -> str:
    """Return the constrained columns, comma-joined in position order."""
    return ", ".join(c.name for c in sorted(constraint.columns, key=lambda cc: cc.position))


def _constraint_reference_cell(constraint: Constraint, allocator: ConceptPathAllocator) -> str:
    """Return the Reference cell: a link to the referenced table, or empty."""
    if constraint.constraint_type != "R" or not constraint.referenced_table:
        return ""
    link = allocator.link_for("tables", constraint.referenced_table)
    return f"[{constraint.referenced_table}]({link})"


def _constraint_condition_cell(constraint: Constraint) -> str:
    """Return the Condition cell: the backticked search condition, or empty."""
    if constraint.constraint_type != "C" or not constraint.search_condition:
        return ""
    return inline_code(constraint.search_condition)


def _render_constraints_table(constraints: tuple[Constraint, ...], allocator: ConceptPathAllocator) -> str:
    """Render the Name/Type/Columns/Reference/Condition constraints table."""
    rows = [
        [
            constraint.name,
            constraint.constraint_type,
            _constraint_columns_text(constraint),
            _constraint_reference_cell(constraint, allocator),
            _constraint_condition_cell(constraint),
        ]
        for constraint in sorted(constraints, key=lambda c: c.name)
    ]
    return render_table(["Name", "Type", "Columns", "Reference", "Condition"], rows)


def _render_fk_bullets(
    constraints: tuple[Constraint, ...], allocator: ConceptPathAllocator, config: RenderConfig
) -> str:
    """Render one bullet per foreign key, describing what it references."""
    foreign_keys = [c for c in constraints if c.constraint_type == "R" and c.referenced_table]
    bullets = []
    for fk in sorted(foreign_keys, key=lambda c: c.name):
        columns_text = _constraint_columns_text(fk)
        ref_columns_text = ", ".join(fk.referenced_columns)
        ref_name = fk.referenced_table or ""
        if fk.referenced_owner and fk.referenced_owner != config.schema_label:
            ref_name = f"{fk.referenced_owner}.{ref_name}"
        link = allocator.link_for("tables", fk.referenced_table or "")
        delete_rule = fk.delete_rule or "NO ACTION"
        bullets.append(f"- `{columns_text}` references [{ref_name}]({link}) (`{ref_columns_text}`) ({delete_rule})")
    return "\n".join(bullets)


def _render_constraints_section(
    constraints: tuple[Constraint, ...], allocator: ConceptPathAllocator, config: RenderConfig
) -> str:
    """Render the full Constraints section: table plus foreign-key bullets."""
    if not constraints:
        return ""
    return join_blocks(
        _render_constraints_table(constraints, allocator),
        _render_fk_bullets(constraints, allocator, config),
    )


def _render_indexes_section(indexes: tuple[Index, ...]) -> str:
    """Render the Name/Unique/Columns/Type indexes table."""
    if not indexes:
        return ""
    rows = []
    for index in sorted(indexes, key=lambda i: i.name):
        columns_text = ", ".join(
            f"{c.name}{' DESC' if c.descending else ''}" for c in sorted(index.columns, key=lambda cc: cc.position)
        )
        rows.append([index.name, "YES" if index.unique else "NO", columns_text, index.index_type])
    return render_table(["Name", "Unique", "Columns", "Type"], rows)


def _render_table_comments_section(table: Table, resource: str) -> str:
    """Render reconstructed ``COMMENT ON`` statements, or empty when none."""
    lines = []
    if table.comment:
        lines.append(f"COMMENT ON TABLE {resource} IS '{_sql_escape(table.comment)}';")
    for column in sorted(table.columns, key=lambda c: c.position):
        if column.comment:
            lines.append(f"COMMENT ON COLUMN {resource}.{column.name} IS '{_sql_escape(column.comment)}';")
    if not lines:
        return ""
    return render_code_block("\n".join(lines), "sql")


def _render_table_properties_section(table: Table) -> str:
    """Render the GTT-only Properties table, or empty for an ordinary table."""
    if not table.is_global_temporary:
        return ""
    rows = []
    if table.on_commit:
        rows.append(["On Commit", table.on_commit])
    if table.duration:
        rows.append(["Duration", table.duration])
    rows.append(["Global Temporary", "YES"])
    return render_table(["Property", "Value"], rows)


def _render_examples_section(sample: SampleData | None, config: RenderConfig) -> str:
    """Render the sample-data table, gated on ``include_data`` and content."""
    if not config.include_data or sample is None or not sample.rows:
        return ""
    return render_table(list(sample.columns), [list(row) for row in sample.rows])


def _table_frontmatter(table: Table, config: RenderConfig, generated_at: str) -> dict[str, object]:
    """Build Table-specific frontmatter on top of the common fields."""
    description = table.comment or f"Table {table.name}."
    frontmatter = _base_frontmatter(OBJECT_TYPE_TABLE, table, table.name, description, config, generated_at)
    primary_key = next((c for c in table.constraints if c.constraint_type == "P"), None)
    if primary_key is not None:
        frontmatter["primary_key"] = [c.name for c in sorted(primary_key.columns, key=lambda cc: cc.position)]
    if config.include_data and table.row_count is not None:
        frontmatter["row_count"] = table.row_count
    if table.is_global_temporary:
        if table.on_commit:
            frontmatter["on_commit"] = table.on_commit
        if table.duration:
            frontmatter["duration"] = table.duration
    return frontmatter


def _build_table_concept(
    obj: object, config: RenderConfig, allocator: ConceptPathAllocator, generated_at: str
) -> OkfConcept:
    """Build the concept for one table or global temporary table."""
    table = cast(Table, obj)
    resource = _resource(table.name, config)
    concept = OkfConcept(frontmatter=_table_frontmatter(table, config, generated_at))
    concept.add_section("Schema", _render_columns_table(table.columns))
    concept.add_section("Constraints", _render_constraints_section(table.constraints, allocator, config))
    concept.add_section("Indexes", _render_indexes_section(table.indexes))
    concept.add_section("Comments", _render_table_comments_section(table, resource))
    concept.add_section("Properties", _render_table_properties_section(table))
    concept.add_section("Examples", _render_examples_section(table.sample, config))
    return concept


# --------------------------------------------------------------------------
# View.
# --------------------------------------------------------------------------


def _build_view_concept(
    obj: object, config: RenderConfig, allocator: ConceptPathAllocator, generated_at: str
) -> OkfConcept:
    """Build the concept for one view."""
    del allocator  # views never link out to other concepts
    view = cast(View, obj)
    description = view.comment or f"View {view.name}."
    frontmatter = _base_frontmatter(OBJECT_TYPE_VIEW, view, view.name, description, config, generated_at)
    concept = OkfConcept(frontmatter=frontmatter)
    concept.add_section("Schema", _render_columns_table(view.columns))
    concept.add_section("Definition", render_code_block(view.definition, "sql"))
    return concept


# --------------------------------------------------------------------------
# Sequence.
# --------------------------------------------------------------------------


def _build_sequence_concept(
    obj: object, config: RenderConfig, allocator: ConceptPathAllocator, generated_at: str
) -> OkfConcept:
    """Build the concept for one sequence."""
    del allocator  # sequences never link out to other concepts
    sequence = cast(Sequence, obj)
    description = f"Sequence {sequence.name}."
    frontmatter = _base_frontmatter(OBJECT_TYPE_SEQUENCE, sequence, sequence.name, description, config, generated_at)
    concept = OkfConcept(frontmatter=frontmatter)
    rows = []
    if sequence.min_value is not None:
        rows.append(["Min", sequence.min_value])
    if sequence.max_value is not None:
        rows.append(["Max", sequence.max_value])
    if sequence.increment_by is not None:
        rows.append(["Increment", sequence.increment_by])
    if sequence.cache_size is not None:
        rows.append(["Cache", sequence.cache_size])
    if sequence.cycle is not None:
        rows.append(["Cycle", "YES" if sequence.cycle else "NO"])
    if sequence.ordered is not None:
        rows.append(["Order", "YES" if sequence.ordered else "NO"])
    concept.add_section("Properties", render_table(["Property", "Value"], rows) if rows else "")
    return concept


# --------------------------------------------------------------------------
# Program: procedure, function, package, trigger.
# --------------------------------------------------------------------------


def _render_trigger_properties(program: Program) -> str:
    """Render the trigger-only Type/Event/Table/Status properties table."""
    rows = []
    if program.trigger_type:
        rows.append(["Type", program.trigger_type])
    if program.triggering_event:
        rows.append(["Event", program.triggering_event])
    if program.table_name:
        rows.append(["Table", program.table_name])
    if program.status:
        rows.append(["Status", program.status])
    return render_table(["Property", "Value"], rows) if rows else ""


def _render_simple_program_source(program: Program) -> str:
    """Render the Source body for a procedure, function, or trigger."""
    code = render_code_block(program.source, "sql")
    if program.program_type != OBJECT_TYPE_TRIGGER:
        return code
    return join_blocks(_render_trigger_properties(program), code)


def _render_package_source(program: Program) -> str:
    """Render the Source body for a package as Specification/Body sub-sections.

    Real H3 sub-headings are used rather than bold text, which is what keeps
    the bundle clear of MD036 (no-emphasis-as-heading).
    """
    spec_block = render_code_block(program.spec_source, "sql")
    body_block = render_code_block(program.body_source, "sql")
    spec_part = join_blocks("### Specification", spec_block) if spec_block else ""
    body_part = join_blocks("### Body", body_block) if body_block else ""
    return join_blocks(spec_part, body_part)


def _build_program_concept(
    obj: object, config: RenderConfig, allocator: ConceptPathAllocator, generated_at: str
) -> OkfConcept:
    """Build the concept for one procedure, function, package, or trigger."""
    del allocator  # programs never link out to other concepts
    program = cast(Program, obj)
    object_type = program.program_type
    kind = _KIND_LABELS[object_type]
    description = f"{kind} {program.name}."
    frontmatter = _base_frontmatter(object_type, program, program.name, description, config, generated_at)
    concept = OkfConcept(frontmatter=frontmatter)
    if object_type == OBJECT_TYPE_PACKAGE:
        body = _render_package_source(program)
    else:
        body = _render_simple_program_source(program)
    concept.add_section("Source", body)
    return concept


# --------------------------------------------------------------------------
# Type.
# --------------------------------------------------------------------------


def _build_type_concept(
    obj: object, config: RenderConfig, allocator: ConceptPathAllocator, generated_at: str
) -> OkfConcept:
    """Build the concept for one user-defined object or collection type."""
    del allocator  # types never link out to other concepts
    object_type = cast(ObjectType, obj)
    description = f"Type {object_type.name}."
    frontmatter = _base_frontmatter(OBJECT_TYPE_TYPE, object_type, object_type.name, description, config, generated_at)
    concept = OkfConcept(frontmatter=frontmatter)
    if object_type.attributes:
        rows = [[attribute.name, attribute.data_type] for attribute in object_type.attributes]
        concept.add_section("Attributes", render_table(["Attribute", "Type"], rows))
    else:
        concept.add_section("Source", render_code_block(object_type.source, "sql"))
    return concept


# --------------------------------------------------------------------------
# Synonym.
# --------------------------------------------------------------------------


def _build_synonym_concept(
    obj: object, config: RenderConfig, allocator: ConceptPathAllocator, generated_at: str
) -> OkfConcept:
    """Build the concept for one synonym."""
    del allocator  # the target link is plain text, not a cross-link
    synonym = cast(Synonym, obj)
    description = f"Synonym {synonym.name}."
    frontmatter = _base_frontmatter(OBJECT_TYPE_SYNONYM, synonym, synonym.name, description, config, generated_at)
    concept = OkfConcept(frontmatter=frontmatter)
    target = synonym.target_name
    if synonym.target_owner and synonym.target_owner != config.schema_label:
        target = f"{synonym.target_owner}.{synonym.target_name}"
    lines = [f"Target: {inline_code(target)}"]
    if synonym.db_link:
        lines.append(f"Database link: {inline_code(synonym.db_link)}")
    if synonym.is_public:
        lines.append("Scope: public")
    concept.add_section("Details", join_blocks(*lines))
    return concept


# --------------------------------------------------------------------------
# Database link.
# --------------------------------------------------------------------------


def _build_db_link_concept(
    obj: object, config: RenderConfig, allocator: ConceptPathAllocator, generated_at: str
) -> OkfConcept:
    """Build the concept for one database link. The password is never rendered."""
    del allocator  # db links never link out to other concepts
    db_link = cast(DbLink, obj)
    description = f"Database Link {db_link.name}."
    frontmatter = _base_frontmatter(OBJECT_TYPE_DB_LINK, db_link, db_link.name, description, config, generated_at)
    concept = OkfConcept(frontmatter=frontmatter)
    lines = []
    if db_link.username:
        lines.append(f"Username: {inline_code(db_link.username)}")
    if db_link.host:
        lines.append(f"Host: {inline_code(db_link.host)}")
    concept.add_section("Details", join_blocks(*lines))
    return concept


# --------------------------------------------------------------------------
# Scheduler job.
# --------------------------------------------------------------------------


def _build_job_concept(
    obj: object, config: RenderConfig, allocator: ConceptPathAllocator, generated_at: str
) -> OkfConcept:
    """Build the concept for one DBMS_SCHEDULER job."""
    del allocator  # jobs never link out to other concepts
    job = cast(Job, obj)
    description = job.comments or f"Scheduler Job {job.name}."
    frontmatter = _base_frontmatter(OBJECT_TYPE_JOB, job, job.name, description, config, generated_at)
    concept = OkfConcept(frontmatter=frontmatter)
    rows = []
    if job.job_type:
        rows.append(["Type", job.job_type])
    if job.schedule_type:
        rows.append(["Schedule Type", job.schedule_type])
    if job.repeat_interval:
        rows.append(["Repeat Interval", job.repeat_interval])
    if job.state:
        rows.append(["State", job.state])
    if job.job_class:
        rows.append(["Class", job.job_class])
    if job.enabled is not None:
        rows.append(["Enabled", "YES" if job.enabled else "NO"])
    table = render_table(["Property", "Value"], rows) if rows else ""
    concept.add_section("Details", join_blocks(table, render_code_block(job.job_action, "sql")))
    return concept


# --------------------------------------------------------------------------
# Materialized view.
# --------------------------------------------------------------------------


def _build_mview_concept(
    obj: object, config: RenderConfig, allocator: ConceptPathAllocator, generated_at: str
) -> OkfConcept:
    """Build the concept for one materialized view."""
    del allocator  # master tables are named as plain text, not cross-linked
    mview = cast(MaterializedView, obj)
    description = f"Materialized View {mview.name}."
    frontmatter = _base_frontmatter(OBJECT_TYPE_MVIEW, mview, mview.name, description, config, generated_at)
    concept = OkfConcept(frontmatter=frontmatter)
    rows = []
    if mview.refresh_method:
        rows.append(["Refresh Method", mview.refresh_method])
    if mview.refresh_mode:
        rows.append(["Refresh Mode", mview.refresh_mode])
    if mview.build_mode:
        rows.append(["Build Mode", mview.build_mode])
    if mview.compile_state:
        rows.append(["Compile State", mview.compile_state])
    if mview.master_tables:
        rows.append(["Master Tables", ", ".join(mview.master_tables)])
    table = render_table(["Property", "Value"], rows) if rows else ""
    concept.add_section("Definition", join_blocks(render_code_block(mview.query, "sql"), table))
    return concept


# --------------------------------------------------------------------------
# Materialized view log.
# --------------------------------------------------------------------------


def _build_mview_log_concept(
    obj: object, config: RenderConfig, allocator: ConceptPathAllocator, generated_at: str
) -> OkfConcept:
    """Build the concept for one materialized view log."""
    del allocator  # the master table is named as plain text, not cross-linked
    mview_log = cast(MViewLog, obj)
    description = f"Materialized View Log {mview_log.name}."
    frontmatter = _base_frontmatter(OBJECT_TYPE_MVIEW_LOG, mview_log, mview_log.name, description, config, generated_at)
    concept = OkfConcept(frontmatter=frontmatter)
    master = mview_log.master_table
    if mview_log.master_owner and mview_log.master_owner != config.schema_label:
        master = f"{mview_log.master_owner}.{mview_log.master_table}"
    rows = [["Master Table", master]]
    if mview_log.rowids is not None:
        rows.append(["Rowids", "YES" if mview_log.rowids else "NO"])
    if mview_log.primary_key is not None:
        rows.append(["Primary Key", "YES" if mview_log.primary_key else "NO"])
    if mview_log.sequence is not None:
        rows.append(["Sequence", "YES" if mview_log.sequence else "NO"])
    if mview_log.include_new_values is not None:
        rows.append(["New Values", "YES" if mview_log.include_new_values else "NO"])
    if mview_log.filter_columns:
        rows.append(["Filter Columns", ", ".join(mview_log.filter_columns)])
    concept.add_section("Details", render_table(["Property", "Value"], rows))
    return concept


# --------------------------------------------------------------------------
# Dispatch table.
# --------------------------------------------------------------------------

_BUILDERS: dict[str, _ConceptBuilder] = {
    OBJECT_TYPE_TABLE: _build_table_concept,
    OBJECT_TYPE_VIEW: _build_view_concept,
    OBJECT_TYPE_SEQUENCE: _build_sequence_concept,
    OBJECT_TYPE_PROCEDURE: _build_program_concept,
    OBJECT_TYPE_FUNCTION: _build_program_concept,
    OBJECT_TYPE_PACKAGE: _build_program_concept,
    OBJECT_TYPE_TRIGGER: _build_program_concept,
    OBJECT_TYPE_TYPE: _build_type_concept,
    OBJECT_TYPE_SYNONYM: _build_synonym_concept,
    OBJECT_TYPE_DB_LINK: _build_db_link_concept,
    OBJECT_TYPE_JOB: _build_job_concept,
    OBJECT_TYPE_MVIEW: _build_mview_concept,
    OBJECT_TYPE_MVIEW_LOG: _build_mview_log_concept,
}
