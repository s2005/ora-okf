"""Tests for the schema rename pass.

These are the security-relevant tests of the project: if renaming misses a
reference, a physical schema name reaches a published bundle.
"""

from __future__ import annotations

import pytest

from ora_okf.errors import MappingError
from ora_okf.mapping import UNMAPPED_ERROR, UNMAPPED_REDACT, SchemaMapping
from ora_okf.model import (
    OBJECT_TYPE_PACKAGE,
    Column,
    Constraint,
    ConstraintColumn,
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
from ora_okf.rename import SchemaRenamer, build_identifier_pattern, collect_known_schemas

PHYSICAL = "APP_PROD_OWNER"
OTHER = "REF_DATA_PROD"


def renamer(**kwargs) -> SchemaRenamer:
    """Build a renamer mapping APP_PROD_OWNER -> APP and REF_DATA_PROD -> REF."""
    mapping = SchemaMapping(entries={PHYSICAL: "APP", OTHER: "REF"}, **kwargs)
    return SchemaRenamer(mapping, [PHYSICAL, OTHER])


class TestRenameText:
    def test_replaces_a_bare_qualifier(self):
        assert renamer().rename_text(f"SELECT * FROM {PHYSICAL}.ORDERS") == "SELECT * FROM APP.ORDERS"

    def test_replaces_a_quoted_qualifier_and_keeps_the_quotes(self):
        assert renamer().rename_text(f'FROM "{PHYSICAL}"."ORDERS"') == 'FROM "APP"."ORDERS"'

    def test_matching_is_case_insensitive(self):
        assert renamer().rename_text("select from app_prod_owner.t") == "select from APP.t"

    def test_does_not_match_inside_a_longer_identifier(self):
        """APP_PROD_OWNER must not match inside APP_PROD_OWNER_ARCHIVE."""
        text = f"FROM {PHYSICAL}_ARCHIVE.T"
        assert renamer().rename_text(text) == text

    def test_does_not_match_a_suffix_of_a_longer_identifier(self):
        text = f"FROM X_{PHYSICAL}.T"
        assert renamer().rename_text(text) == text

    def test_replaces_every_occurrence(self):
        text = f"{PHYSICAL}.A, {PHYSICAL}.B, {OTHER}.C"
        assert renamer().rename_text(text) == "APP.A, APP.B, REF.C"

    def test_counts_replacements_per_schema(self):
        instance = renamer()
        instance.rename_text(f"{PHYSICAL}.A {PHYSICAL}.B {OTHER}.C")
        assert instance.report.text_replacements == {PHYSICAL: 2, OTHER: 1}

    def test_substitution_is_simultaneous_not_chained(self):
        """A rename whose output looks like another rename's input is untouched.

        The loader rejects a chained mapping, but the single-pass guarantee is
        what makes ordering irrelevant, so it is asserted directly.
        """
        mapping = SchemaMapping(entries={"AAA": "BBB", "CCC": "AAA"})
        instance = SchemaRenamer(mapping, ["AAA", "CCC"])
        assert instance.rename_text("CCC.T AAA.T") == "AAA.T BBB.T"

    def test_empty_text_is_returned_unchanged(self):
        assert renamer().rename_text("") == ""

    def test_no_substitutions_means_no_rewriting(self):
        instance = SchemaRenamer(SchemaMapping.identity(), ["ANY"])
        assert instance.rename_text("ANY.T") == "ANY.T"


class TestRenameSchema:
    def test_maps_a_known_name(self):
        assert renamer().rename_schema(PHYSICAL) == "APP"

    def test_is_case_insensitive(self):
        assert renamer().rename_schema("app_prod_owner") == "APP"

    def test_preserves_none(self):
        assert renamer().rename_schema(None) is None

    def test_leaves_an_unknown_name_alone(self):
        assert renamer().rename_schema("SOMETHING_ELSE") == "SOMETHING_ELSE"

    def test_redact_policy_collapses_unmapped_names(self):
        mapping = SchemaMapping(entries={PHYSICAL: "APP"}, unmapped=UNMAPPED_REDACT, redacted_name="EXTERNAL")
        instance = SchemaRenamer(mapping, [PHYSICAL, "MYSTERY_SCHEMA"])
        assert instance.rename_schema("MYSTERY_SCHEMA") == "EXTERNAL"

    def test_error_policy_raises_at_construction(self):
        mapping = SchemaMapping(entries={PHYSICAL: "APP"}, unmapped=UNMAPPED_ERROR)
        with pytest.raises(MappingError, match="MYSTERY_SCHEMA"):
            SchemaRenamer(mapping, [PHYSICAL, "MYSTERY_SCHEMA"])


class TestRenameModel:
    def build_model(self) -> SchemaModel:
        """Build a model that puts the physical name in every reachable place."""
        return SchemaModel(
            schema_name=PHYSICAL,
            generated_at="2026-01-01 00:00:00 UTC",
            tables=(
                Table(
                    name="ORDERS",
                    comment=f"Lives in {PHYSICAL}.",
                    columns=(Column("ID", "NUMBER", False, f"{PHYSICAL}.SEQ.NEXTVAL", f"see {PHYSICAL}", 1),),
                    constraints=(
                        Constraint(
                            "FK",
                            "R",
                            "ORDERS",
                            (ConstraintColumn("ID", 1),),
                            referenced_owner=OTHER,
                            referenced_table="CUSTOMERS",
                        ),
                        Constraint(
                            "CK",
                            "C",
                            "ORDERS",
                            search_condition=f"{PHYSICAL}.FN(ID) > 0",
                        ),
                    ),
                    indexes=(Index("IX", "ORDERS", columns=(IndexColumn(f"{PHYSICAL}.FN(ID)", 1),)),),
                    sample=SampleData(columns=("OWNER",), rows=((PHYSICAL,),)),
                ),
            ),
            views=(View("V", f"in {PHYSICAL}", f"SELECT * FROM {PHYSICAL}.ORDERS"),),
            programs=(
                Program(
                    "PKG",
                    OBJECT_TYPE_PACKAGE,
                    spec_source=f"-- {PHYSICAL}",
                    body_source=f"BEGIN {PHYSICAL}.P; END;",
                ),
            ),
            types=(ObjectType("T", source=f"-- {PHYSICAL}"),),
            synonyms=(Synonym("S", target_owner=OTHER, target_name="CUSTOMERS"),),
            jobs=(Job("J", job_action=f"BEGIN {PHYSICAL}.P; END;", comments=f"in {PHYSICAL}"),),
            mviews=(
                MaterializedView(
                    "MV",
                    query=f"SELECT * FROM {PHYSICAL}.ORDERS",
                    master_tables=(f"{PHYSICAL}.ORDERS", f"{OTHER}.CUSTOMERS"),
                ),
            ),
            mview_logs=(MViewLog("MLOG", master_table="ORDERS", master_owner=PHYSICAL),),
            referenced_schemas=(OTHER,),
        )

    def test_no_physical_name_survives_anywhere(self):
        renamed = renamer().rename_model(self.build_model())
        assert PHYSICAL not in repr(renamed)
        assert OTHER not in repr(renamed)

    def test_structured_owners_are_remapped(self):
        renamed = renamer().rename_model(self.build_model())
        assert renamed.schema_name == "APP"
        assert renamed.synonyms[0].target_owner == "REF"
        assert renamed.tables[0].constraints[0].referenced_owner == "REF"
        assert renamed.mview_logs[0].master_owner == "APP"
        assert renamed.mviews[0].master_tables == ("APP.ORDERS", "REF.CUSTOMERS")

    def test_free_text_is_remapped(self):
        renamed = renamer().rename_model(self.build_model())
        table = renamed.tables[0]
        assert table.comment == "Lives in APP."
        assert table.columns[0].default_value == "APP.SEQ.NEXTVAL"
        assert table.columns[0].comment == "see APP"
        assert table.constraints[1].search_condition == "APP.FN(ID) > 0"
        assert table.indexes[0].columns[0].name == "APP.FN(ID)"
        assert renamed.views[0].definition == "SELECT * FROM APP.ORDERS"
        assert renamed.programs[0].body_source == "BEGIN APP.P; END;"
        assert renamed.types[0].source == "-- APP"
        assert renamed.jobs[0].job_action == "BEGIN APP.P; END;"

    def test_sampled_values_are_remapped(self):
        """A table storing its own schema name must not reintroduce it."""
        renamed = renamer().rename_model(self.build_model())
        assert renamed.tables[0].sample is not None
        assert renamed.tables[0].sample.rows == (("APP",),)

    def test_object_names_are_not_touched(self):
        renamed = renamer().rename_model(self.build_model())
        assert renamed.tables[0].name == "ORDERS"
        assert renamed.tables[0].columns[0].name == "ID"

    def test_the_source_model_is_left_unmodified(self):
        """The audit compares against the physical model, so it must survive."""
        model = self.build_model()
        renamer().rename_model(model)
        assert model.schema_name == PHYSICAL
        assert model.views[0].definition == f"SELECT * FROM {PHYSICAL}.ORDERS"

    def test_an_empty_mapping_returns_the_same_object(self):
        model = self.build_model()
        instance = SchemaRenamer(SchemaMapping.identity(), [PHYSICAL])
        assert instance.rename_model(model) is model


class TestCollectKnownSchemas:
    def test_combines_model_and_mapping_names(self):
        model = SchemaModel(schema_name=PHYSICAL, referenced_schemas=(OTHER,))
        mapping = SchemaMapping(entries={"DECLARED_ONLY": "DECL"})
        assert collect_known_schemas(model, mapping) == ("APP_PROD_OWNER", "DECLARED_ONLY", "REF_DATA_PROD")

    def test_deduplicates_case_insensitively(self):
        model = SchemaModel(schema_name="app_prod_owner", referenced_schemas=("APP_PROD_OWNER",))
        assert collect_known_schemas(model, SchemaMapping.identity()) == ("APP_PROD_OWNER",)


class TestBuildIdentifierPattern:
    def test_returns_none_for_no_names(self):
        assert build_identifier_pattern([]) is None
        assert build_identifier_pattern(["", "  "]) is None

    def test_longer_names_win_the_alternation(self):
        pattern = build_identifier_pattern(["APP", "APP_PROD"])
        assert pattern is not None
        match = pattern.search("FROM APP_PROD.T")
        assert match is not None
        assert match.group("bare") == "APP_PROD"
