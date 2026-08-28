"""Tests for concept filename sanitizing and collision-safe path allocation."""

from __future__ import annotations

from ora_okf.okf.paths import ConceptPathAllocator, concept_filename, sanitize_name


class TestSanitizeName:
    def test_lowercases_and_hyphenates(self):
        assert sanitize_name("MY_TABLE NAME") == "my-table-name"

    def test_returns_object_for_unsluggable_name(self):
        assert sanitize_name("!!!") == "object"

    def test_strips_leading_and_trailing_hyphens(self):
        assert sanitize_name("_ORDERS_") == "orders"


class TestConceptFilename:
    def test_appends_concept_suffix_for_index(self):
        assert concept_filename("INDEX") == "index-concept.md"

    def test_appends_concept_suffix_for_log(self):
        assert concept_filename("LOG") == "log-concept.md"

    def test_ordinary_name_is_unaffected(self):
        assert concept_filename("ORDERS") == "orders.md"


class TestConceptPathAllocator:
    def test_distinct_names_sanitizing_alike_get_distinct_paths(self):
        allocator = ConceptPathAllocator()
        first = allocator.allocate("tables", "A_B")
        second = allocator.allocate("tables", "A$B")
        assert first == "tables/a-b.md"
        assert second == "tables/a-b-2.md"

    def test_collisions_reports_the_second_allocation(self):
        allocator = ConceptPathAllocator()
        allocator.allocate("tables", "A_B")
        allocator.allocate("tables", "A$B")
        assert allocator.collisions() == [("tables", "A$B", "tables/a-b-2.md")]

    def test_same_category_and_name_allocated_twice_yields_two_paths(self):
        allocator = ConceptPathAllocator()
        first = allocator.allocate("programs", "AUDIT")
        second = allocator.allocate("programs", "AUDIT")
        assert first == "programs/audit.md"
        assert second == "programs/audit-2.md"

    def test_path_for_returns_the_first_allocated_path(self):
        allocator = ConceptPathAllocator()
        first = allocator.allocate("tables", "ORDERS")
        allocator.allocate("tables", "ORDERS")
        assert allocator.path_for("tables", "ORDERS") == first

    def test_path_for_never_allocated_name_returns_natural_path(self):
        allocator = ConceptPathAllocator()
        assert allocator.path_for("tables", "NEVER_ALLOCATED") == "tables/never-allocated.md"

    def test_path_for_dangling_name_does_not_collide_with_a_different_concept(self):
        allocator = ConceptPathAllocator()
        allocator.allocate("tables", "A_B")
        # "A$B" was never allocated, but sanitizes to the same natural path as "A_B".
        path = allocator.path_for("tables", "A$B")
        assert path == "tables/a-b-2.md"

    def test_link_for_prefixes_with_slash(self):
        allocator = ConceptPathAllocator()
        allocator.allocate("tables", "ORDERS")
        assert allocator.link_for("tables", "ORDERS") == "/tables/orders.md"
