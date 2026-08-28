"""Tests for mapping file loading and validation."""

from __future__ import annotations

import json

import pytest

from ora_okf.errors import MappingError
from ora_okf.mapping import (
    UNMAPPED_ERROR,
    UNMAPPED_KEEP,
    UNMAPPED_REDACT,
    SchemaMapping,
    load_mapping,
)


def write(tmp_path, name, text):
    """Write a mapping file and return its path."""
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


class TestLoadMapping:
    def test_loads_yaml(self, tmp_path):
        path = write(tmp_path, "map.yaml", "schemas:\n  APP_PROD: APP\n  REF_PROD: REF\n")
        mapping = load_mapping(path)
        assert mapping.entries == {"APP_PROD": "APP", "REF_PROD": "REF"}
        assert mapping.unmapped == UNMAPPED_KEEP
        assert mapping.source == str(path)

    def test_loads_json(self, tmp_path):
        path = write(tmp_path, "map.json", json.dumps({"schemas": {"APP_PROD": "APP"}, "unmapped": "redact"}))
        mapping = load_mapping(path)
        assert mapping.entries == {"APP_PROD": "APP"}
        assert mapping.unmapped == UNMAPPED_REDACT

    def test_json_content_in_yaml_file_is_accepted(self, tmp_path):
        """YAML is a JSON superset, so the extension need not match the syntax."""
        path = write(tmp_path, "map.yaml", json.dumps({"schemas": {"APP_PROD": "APP"}}))
        assert load_mapping(path).entries == {"APP_PROD": "APP"}

    def test_keys_are_normalized_to_upper_case(self, tmp_path):
        path = write(tmp_path, "map.yaml", "schemas:\n  app_prod: APP\n")
        mapping = load_mapping(path)
        assert mapping.entries == {"APP_PROD": "APP"}
        assert mapping.is_mapped("App_Prod")

    def test_missing_file(self, tmp_path):
        with pytest.raises(MappingError, match="not found"):
            load_mapping(tmp_path / "absent.yaml")

    def test_empty_file(self, tmp_path):
        with pytest.raises(MappingError, match="is empty"):
            load_mapping(write(tmp_path, "map.yaml", ""))

    def test_top_level_must_be_a_mapping(self, tmp_path):
        with pytest.raises(MappingError, match="must contain a mapping"):
            load_mapping(write(tmp_path, "map.yaml", "- one\n- two\n"))

    def test_invalid_yaml(self, tmp_path):
        with pytest.raises(MappingError, match="not valid YAML"):
            load_mapping(write(tmp_path, "map.yaml", "schemas: [unclosed\n"))

    def test_invalid_json(self, tmp_path):
        with pytest.raises(MappingError, match="not valid JSON"):
            load_mapping(write(tmp_path, "map.json", "{nope}"))

    def test_schemas_block_is_required(self, tmp_path):
        with pytest.raises(MappingError, match="must define a 'schemas' block"):
            load_mapping(write(tmp_path, "map.yaml", "version: 1\n"))

    def test_schemas_block_must_not_be_empty(self, tmp_path):
        with pytest.raises(MappingError, match="must not be empty"):
            load_mapping(write(tmp_path, "map.yaml", "schemas: {}\n"))

    def test_unknown_top_level_key_is_rejected(self, tmp_path):
        """A 'schema:' typo must fail loudly, not silently rename nothing."""
        path = write(tmp_path, "map.yaml", "schema:\n  APP_PROD: APP\n")
        with pytest.raises(MappingError, match="unknown key"):
            load_mapping(path)

    def test_published_name_must_be_an_identifier(self, tmp_path):
        path = write(tmp_path, "map.yaml", "schemas:\n  APP_PROD: 'my app'\n")
        with pytest.raises(MappingError, match="not a valid identifier"):
            load_mapping(path)

    def test_published_name_must_not_be_empty(self, tmp_path):
        with pytest.raises(MappingError, match="non-empty name"):
            load_mapping(write(tmp_path, "map.yaml", "schemas:\n  APP_PROD: ''\n"))

    def test_chained_rename_is_rejected(self, tmp_path):
        """A -> B alongside B -> C has no well-defined result."""
        path = write(tmp_path, "map.yaml", "schemas:\n  A_OWNER: B_OWNER\n  B_OWNER: C_OWNER\n")
        with pytest.raises(MappingError, match="Chained renames are ambiguous"):
            load_mapping(path)

    def test_identity_mapping_is_allowed(self, tmp_path):
        """A -> A is a deliberate 'leave this one alone' declaration."""
        path = write(tmp_path, "map.yaml", "schemas:\n  KEEP_ME: KEEP_ME\n  APP_PROD: APP\n")
        assert load_mapping(path).entries["KEEP_ME"] == "KEEP_ME"

    def test_case_insensitive_duplicate_key_is_rejected(self, tmp_path):
        path = write(tmp_path, "map.yaml", "schemas:\n  APP_PROD: APP\n  app_prod: OTHER\n")
        with pytest.raises(MappingError, match="mapped twice"):
            load_mapping(path)

    def test_bad_unmapped_policy(self, tmp_path):
        with pytest.raises(MappingError, match="'unmapped' must be one of"):
            load_mapping(write(tmp_path, "map.yaml", "unmapped: maybe\nschemas:\n  A_OWNER: A\n"))

    def test_bad_redacted_name(self, tmp_path):
        path = write(tmp_path, "map.yaml", "redacted_name: '1BAD'\nschemas:\n  A_OWNER: A\n")
        with pytest.raises(MappingError, match="redacted_name"):
            load_mapping(path)


class TestResolve:
    def test_maps_a_known_schema(self):
        mapping = SchemaMapping(entries={"APP_PROD": "APP"})
        assert mapping.resolve("app_prod") == "APP"

    def test_keep_policy_returns_input_unchanged(self):
        mapping = SchemaMapping(entries={"APP_PROD": "APP"}, unmapped=UNMAPPED_KEEP)
        assert mapping.resolve("OTHER") == "OTHER"

    def test_redact_policy_substitutes_placeholder(self):
        mapping = SchemaMapping(entries={"APP_PROD": "APP"}, unmapped=UNMAPPED_REDACT, redacted_name="EXTERNAL")
        assert mapping.resolve("OTHER") == "EXTERNAL"

    def test_error_policy_raises(self):
        mapping = SchemaMapping(entries={"APP_PROD": "APP"}, unmapped=UNMAPPED_ERROR)
        with pytest.raises(MappingError, match="has no entry in the mapping"):
            mapping.resolve("OTHER")

    def test_empty_input_is_returned_unchanged(self):
        assert SchemaMapping(entries={}, unmapped=UNMAPPED_ERROR).resolve("") == ""

    def test_identity_mapping_renames_nothing(self):
        mapping = SchemaMapping.identity()
        assert mapping.resolve("ANY_SCHEMA") == "ANY_SCHEMA"
        assert mapping.physical_names() == ()

    def test_missing_lists_only_unmapped_names(self):
        mapping = SchemaMapping(entries={"APP_PROD": "APP"})
        assert mapping.missing(["APP_PROD", "other_prod", "APP_PROD", ""]) == ("OTHER_PROD",)
