"""Tests for OkfConcept rendering and frontmatter parsing."""

from __future__ import annotations

import pytest

from ora_okf.okf.concept import OkfConcept, parse_frontmatter


class TestRender:
    def test_emits_fenced_yaml_frontmatter(self):
        concept = OkfConcept(frontmatter={"type": "Oracle Table", "title": "ORDERS"})
        rendered = concept.render()
        lines = rendered.splitlines()
        assert lines[0] == "---"
        assert "---" in lines[1:]

    def test_preserves_frontmatter_key_order(self):
        concept = OkfConcept(frontmatter={"type": "Oracle Table", "b": 1, "a": 2})
        rendered = concept.render()
        yaml_block = rendered.split("---")[1]
        assert yaml_block.index("type:") < yaml_block.index("b:") < yaml_block.index("a:")

    def test_ends_with_exactly_one_newline(self):
        concept = OkfConcept(frontmatter={"type": "Oracle Table"})
        rendered = concept.render()
        assert rendered.endswith("\n")
        assert not rendered.endswith("\n\n")

    def test_missing_type_raises_value_error(self):
        concept = OkfConcept(frontmatter={"title": "X"})
        with pytest.raises(ValueError, match="type"):
            concept.render()

    def test_blank_type_raises_value_error(self):
        concept = OkfConcept(frontmatter={"type": "   "})
        with pytest.raises(ValueError, match="type"):
            concept.render()

    def test_sections_render_as_h2(self):
        concept = OkfConcept(frontmatter={"type": "Oracle Table"})
        concept.add_section("Schema", "some body")
        rendered = concept.render()
        assert "## Schema" in rendered

    def test_none_frontmatter_value_becomes_empty_list(self):
        concept = OkfConcept(frontmatter={"type": "Oracle Table", "tags": None})
        rendered = concept.render()
        assert "tags: []" in rendered

    def test_tuple_frontmatter_value_becomes_yaml_sequence(self):
        concept = OkfConcept(frontmatter={"type": "Oracle Table", "primary_key": ("ID", "CODE")})
        rendered = concept.render()
        assert "primary_key:" in rendered
        assert "- ID" in rendered
        assert "- CODE" in rendered


class TestAddSection:
    def test_ignores_empty_body(self):
        concept = OkfConcept(frontmatter={"type": "Oracle Table"})
        concept.add_section("Schema", "")
        assert concept.sections == []

    def test_ignores_whitespace_only_body(self):
        concept = OkfConcept(frontmatter={"type": "Oracle Table"})
        concept.add_section("Schema", "   \n  ")
        assert concept.sections == []

    def test_keeps_a_non_empty_body(self):
        concept = OkfConcept(frontmatter={"type": "Oracle Table"})
        concept.add_section("Schema", "content")
        assert concept.sections == [("Schema", "content")]


class TestParseFrontmatter:
    def test_round_trips_a_rendered_concept(self):
        concept = OkfConcept(frontmatter={"type": "Oracle Table", "title": "ORDERS"})
        parsed = parse_frontmatter(concept.render())
        assert parsed == {"type": "Oracle Table", "title": "ORDERS"}

    def test_returns_none_when_there_is_no_frontmatter(self):
        assert parse_frontmatter("# Just a heading\n\nbody text\n") is None

    def test_returns_none_for_an_unterminated_block(self):
        assert parse_frontmatter("---\ntype: Oracle Table\nno closing fence\n") is None

    def test_returns_none_when_frontmatter_is_not_a_mapping(self):
        assert parse_frontmatter("---\n- one\n- two\n---\nbody\n") is None

    def test_returns_none_for_empty_content(self):
        assert parse_frontmatter("") is None
