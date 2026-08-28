"""Rewrite every schema reference in an extracted model to its published name.

Renaming happens between extraction and rendering, on the immutable
:class:`~ora_okf.model.SchemaModel`, so the renderers never need to know a
mapping exists and the physical model stays available for the post-write audit.

Two kinds of reference are rewritten, and both matter:

**Structured owners.** ``Synonym.target_owner``, ``Constraint.referenced_owner``,
``MViewLog.master_owner`` and the owner half of ``MaterializedView.master_tables``
are stored apart from the object name, so they are remapped by lookup with no
string parsing.

**Free text.** A view's defining query, PL/SQL source, a column default such as
``APP_OWNER.SEQ.NEXTVAL``, a check predicate, a job action, and comments all
routinely spell a schema out in running text. These are rewritten by a single
regular expression pass that matches a schema name as a whole SQL identifier,
bare or double-quoted, case-insensitively.

The pass is *simultaneous*: one :func:`re.sub` with a callback, never a loop of
per-name replacements. Sequential replacement would let an earlier rename's
output be matched by a later rename's pattern; a single pass cannot. (The
mapping loader also rejects chained renames, so the two defences are
independent.)

Sampled row values are rewritten too. A configuration table that stores its own
schema name would otherwise reintroduce the physical name under
``--include-data`` after every structural reference had been cleaned.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from re import Pattern

from .mapping import SchemaMapping
from .model import (
    Column,
    Constraint,
    Index,
    IndexColumn,
    Job,
    MaterializedView,
    MViewLog,
    ObjectType,
    Program,
    SampleData,
    SchemaModel,
    Synonym,
    Table,
    View,
)

# Characters that continue an Oracle identifier. A schema name is only replaced
# when it is not adjacent to one of these, so ``APP`` never matches inside
# ``APP_ARCHIVE``.
_IDENT_CHARS = r"A-Za-z0-9_$#"


@dataclass
class RenameReport:
    """What a rename pass changed, for logging and the ``--dry-run`` summary.

    Attributes:
        renamed: Physical name to published name, for names that actually differ.
        unchanged: Physical names that resolved to themselves (the ``keep``
            policy, or an explicit identity mapping).
        text_replacements: Physical name to the number of free-text occurrences
            rewritten. A zero here alongside a non-empty ``renamed`` entry is
            normal: it just means the schema was only referenced structurally.
    """

    renamed: dict[str, str] = field(default_factory=dict)
    unchanged: list[str] = field(default_factory=list)
    text_replacements: dict[str, int] = field(default_factory=dict)

    def total_text_replacements(self) -> int:
        """Return the total number of free-text substitutions made."""
        return sum(self.text_replacements.values())

    def summary(self) -> str:
        """Return a one-line human summary of the pass."""
        if not self.renamed:
            return "no schema names were rewritten"
        pairs = ", ".join(f"{physical} -> {published}" for physical, published in sorted(self.renamed.items()))
        return f"{pairs} ({self.total_text_replacements()} in-text occurrence(s))"


class SchemaRenamer:
    """Apply a :class:`~ora_okf.mapping.SchemaMapping` to a whole model.

    A renamer is built once per export and is reusable: it holds the compiled
    pattern and the resolved substitutions, and accumulates counts in its
    :class:`RenameReport`.
    """

    def __init__(self, mapping: SchemaMapping, known_schemas: Iterable[str]) -> None:
        """Build a renamer for a fixed set of physical schema names.

        Args:
            mapping: The validated schema mapping.
            known_schemas: Every physical schema that may appear in the model --
                the exported schema plus the schemas it references. Names that
                resolve to themselves are excluded from the text pattern, so the
                ``keep`` policy costs nothing.

        Raises:
            MappingError: When the mapping's ``unmapped`` policy is ``error`` and
                a known schema has no entry.
        """
        self.mapping = mapping
        self.report = RenameReport()
        self._substitutions: dict[str, str] = {}

        for physical in sorted({name.strip().upper() for name in known_schemas if name and name.strip()}):
            published = mapping.resolve(physical)
            if published.upper() == physical:
                self.report.unchanged.append(physical)
                continue
            self._substitutions[physical] = published
            self.report.renamed[physical] = published

        self._pattern = _compile_pattern(self._substitutions)

    def rename_schema(self, name: str | None) -> str | None:
        """Return the published label for one schema name, preserving None."""
        if not name:
            return name
        return self._substitutions.get(name.strip().upper(), name)

    def rename_text(self, text: str) -> str:
        """Rewrite every whole-identifier schema reference in free text.

        Args:
            text: SQL, PL/SQL, comment, or sampled value text.

        Returns:
            The text with each known schema name replaced by its published name.
            Double-quoted references keep their quotes.
        """
        if not text or self._pattern is None:
            return text
        return self._pattern.sub(self._replace_match, text)

    def rename_model(self, model: SchemaModel) -> SchemaModel:
        """Return a copy of ``model`` with every schema reference rewritten.

        Args:
            model: The extracted model, holding physical schema names.

        Returns:
            A new model. The original is left untouched so the audit can compare
            the written bundle against the physical names.
        """
        if not self._substitutions:
            return model
        return replace(
            model,
            schema_name=self.rename_schema(model.schema_name) or model.schema_name,
            tables=tuple(self._rename_table(table) for table in model.tables),
            views=tuple(self._rename_view(view) for view in model.views),
            programs=tuple(self._rename_program(program) for program in model.programs),
            types=tuple(self._rename_type(item) for item in model.types),
            synonyms=tuple(self._rename_synonym(item) for item in model.synonyms),
            jobs=tuple(self._rename_job(item) for item in model.jobs),
            mviews=tuple(self._rename_mview(item) for item in model.mviews),
            mview_logs=tuple(self._rename_mview_log(item) for item in model.mview_logs),
            referenced_schemas=tuple(self.rename_schema(name) or name for name in model.referenced_schemas),
        )

    def _replace_match(self, match: re.Match[str]) -> str:
        """Return the replacement for one matched schema reference."""
        quoted = match.group("quoted")
        matched = quoted if quoted is not None else match.group("bare")
        published = self._substitutions[matched.upper()]
        self.report.text_replacements[matched.upper()] = self.report.text_replacements.get(matched.upper(), 0) + 1
        return f'"{published}"' if quoted is not None else published

    def _rename_table(self, table: Table) -> Table:
        return replace(
            table,
            comment=self.rename_text(table.comment),
            columns=tuple(self._rename_column(column) for column in table.columns),
            constraints=tuple(self._rename_constraint(item) for item in table.constraints),
            indexes=tuple(self._rename_index(item) for item in table.indexes),
            sample=self._rename_sample(table.sample),
        )

    def _rename_column(self, column: Column) -> Column:
        return replace(
            column,
            default_value=self.rename_text(column.default_value) if column.default_value else column.default_value,
            comment=self.rename_text(column.comment),
        )

    def _rename_constraint(self, constraint: Constraint) -> Constraint:
        return replace(
            constraint,
            referenced_owner=self.rename_schema(constraint.referenced_owner),
            search_condition=(
                self.rename_text(constraint.search_condition)
                if constraint.search_condition
                else constraint.search_condition
            ),
        )

    def _rename_index(self, index: Index) -> Index:
        # Function-based index columns hold an expression, which can name a
        # schema-qualified function.
        return replace(
            index,
            columns=tuple(
                IndexColumn(name=self.rename_text(column.name), position=column.position, descending=column.descending)
                for column in index.columns
            ),
        )

    def _rename_sample(self, sample: SampleData | None) -> SampleData | None:
        if sample is None:
            return None
        return SampleData(
            columns=sample.columns,
            rows=tuple(tuple(self.rename_text(value) for value in row) for row in sample.rows),
        )

    def _rename_view(self, view: View) -> View:
        return replace(
            view,
            comment=self.rename_text(view.comment),
            definition=self.rename_text(view.definition),
            columns=tuple(self._rename_column(column) for column in view.columns),
        )

    def _rename_program(self, program: Program) -> Program:
        return replace(
            program,
            source=self.rename_text(program.source),
            spec_source=self.rename_text(program.spec_source),
            body_source=self.rename_text(program.body_source),
        )

    def _rename_type(self, item: ObjectType) -> ObjectType:
        return replace(item, source=self.rename_text(item.source))

    def _rename_synonym(self, item: Synonym) -> Synonym:
        return replace(item, target_owner=self.rename_schema(item.target_owner))

    def _rename_job(self, item: Job) -> Job:
        return replace(
            item,
            job_action=self.rename_text(item.job_action),
            comments=self.rename_text(item.comments),
        )

    def _rename_mview(self, item: MaterializedView) -> MaterializedView:
        return replace(
            item,
            query=self.rename_text(item.query),
            master_tables=tuple(self._rename_qualified(name) for name in item.master_tables),
        )

    def _rename_mview_log(self, item: MViewLog) -> MViewLog:
        return replace(item, master_owner=self.rename_schema(item.master_owner))

    def _rename_qualified(self, qualified: str) -> str:
        """Rewrite the owner half of an ``OWNER.OBJECT`` string."""
        owner, separator, object_name = qualified.partition(".")
        if not separator:
            return qualified
        return f"{self.rename_schema(owner)}{separator}{object_name}"


def build_identifier_pattern(names: Iterable[str]) -> Pattern[str] | None:
    """Compile a pattern matching any of ``names`` as a whole SQL identifier.

    The pattern has two named groups: ``quoted`` for a double-quoted reference
    (``"APP_OWNER"``) and ``bare`` for an unquoted one. Matching is
    case-insensitive, and identifier-character lookaround on both sides stops
    ``APP`` from matching inside ``APP_ARCHIVE``.

    Shared by the renamer (which substitutes) and the audit (which only
    searches), so the two can never disagree about what counts as a reference --
    an audit that matched more loosely than the renamer would report phantom
    leaks, and one that matched more tightly would miss real ones.

    Args:
        names: The identifiers to match.

    Returns:
        The compiled pattern, or None when ``names`` is empty.
    """
    unique = sorted({name.strip() for name in names if name and name.strip()}, key=len, reverse=True)
    if not unique:
        return None
    # Longest first so a name that is a prefix of another cannot win the
    # alternation; the identifier boundaries make this belt-and-braces.
    alternatives = "|".join(re.escape(name) for name in unique)
    return re.compile(
        rf'(?<![{_IDENT_CHARS}])(?:"(?P<quoted>{alternatives})"|(?P<bare>{alternatives}))(?![{_IDENT_CHARS}])',
        re.IGNORECASE,
    )


def _compile_pattern(substitutions: dict[str, str]) -> Pattern[str] | None:
    """Compile the single alternation used for the simultaneous text pass."""
    return build_identifier_pattern(substitutions)


def collect_known_schemas(model: SchemaModel, mapping: SchemaMapping) -> tuple[str, ...]:
    """Return every physical schema a rename pass must consider.

    Combines the exported schema, the schemas the model references, and every
    key in the mapping. Mapping keys are included even when the model never
    references them so that an operator can pre-declare a schema they know
    appears only inside PL/SQL text, which structured extraction cannot see.

    Args:
        model: The extracted model.
        mapping: The validated mapping.

    Returns:
        The sorted, de-duplicated, upper-cased schema names.
    """
    names = {model.schema_name.upper()} if model.schema_name else set()
    names.update(name.upper() for name in model.referenced_schemas if name)
    names.update(mapping.physical_names())
    return tuple(sorted(names))
