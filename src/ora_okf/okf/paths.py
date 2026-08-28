"""Concept filenames, categories, and collision-safe path allocation.

Two objects can legitimately want the same file. ``A_B`` and ``A$B`` both
sanitize to ``a-b``; a procedure and a trigger named ``AUDIT`` share the
``programs`` category. Silently letting one overwrite the other would drop an
object from the bundle without any signal, so every allocation is unique and
every disambiguation is recorded for the caller to log.
"""

from __future__ import annotations

import re

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
)
from .concept import RESERVED_FILENAMES

# Category subdirectory for each object type. The four PL/SQL program kinds
# share one directory, matching how they are usually read together.
OBJECT_CATEGORIES: dict[str, str] = {
    OBJECT_TYPE_TABLE: "tables",
    OBJECT_TYPE_VIEW: "views",
    OBJECT_TYPE_SEQUENCE: "sequences",
    OBJECT_TYPE_PROCEDURE: "programs",
    OBJECT_TYPE_FUNCTION: "programs",
    OBJECT_TYPE_PACKAGE: "programs",
    OBJECT_TYPE_TRIGGER: "programs",
    OBJECT_TYPE_TYPE: "types",
    OBJECT_TYPE_SYNONYM: "synonyms",
    OBJECT_TYPE_DB_LINK: "db_links",
    OBJECT_TYPE_JOB: "jobs",
    OBJECT_TYPE_MVIEW: "mviews",
    OBJECT_TYPE_MVIEW_LOG: "mview_logs",
}

_NON_SLUG_RE = re.compile(r"[^a-z0-9]+")


def sanitize_name(name: str) -> str:
    """Return a lower-case, hyphen-separated slug for an object name.

    Args:
        name: The raw database object name.

    Returns:
        The slug, or ``object`` when the name contains nothing sluggable (for
        example a name made entirely of punctuation), so a filename is never
        empty.
    """
    slug = _NON_SLUG_RE.sub("-", name.lower()).strip("-")
    return slug or "object"


def concept_filename(name: str) -> str:
    """Return the Markdown filename for an object.

    A stem that would collide with a reserved bundle-root filename (a table
    literally named ``LOG``) gains a ``-concept`` suffix. This is the single
    source of truth for concept filenames, so links and written files cannot
    drift apart.

    Args:
        name: The raw object name.

    Returns:
        A filename such as ``my-table.md``.
    """
    stem = sanitize_name(name)
    if f"{stem}.md" in RESERVED_FILENAMES:
        stem = f"{stem}-concept"
    return f"{stem}.md"


class ConceptPathAllocator:
    """Allocate a unique path per object and resolve cross-links to it.

    Each :meth:`allocate` call reserves a *fresh* path, appending ``-2``,
    ``-3`` ... when the natural filename is taken. The first path allocated for
    a given ``(category, name)`` is remembered as the canonical link target,
    because cross-links (a foreign key naming its parent table) reference an
    object by name only.
    """

    def __init__(self) -> None:
        self._occupied: set[str] = set()
        self._canonical: dict[tuple[str, str], str] = {}
        self._collisions: list[tuple[str, str, str]] = []

    def allocate(self, category: str, name: str) -> str:
        """Reserve and return a unique path for one object occurrence.

        Args:
            category: The category subdirectory, e.g. ``tables``.
            name: The raw object name.

        Returns:
            A relative path such as ``tables/orders.md``.
        """
        natural = f"{category}/{concept_filename(name)}"
        path = natural if natural not in self._occupied else self._next_free(natural)
        self._occupied.add(path)
        self._canonical.setdefault((category, name), path)
        if path != natural:
            self._collisions.append((category, name, path))
        return path

    def path_for(self, category: str, name: str) -> str:
        """Return the canonical path for an object without allocating.

        For a name that was never allocated -- a foreign key pointing outside the
        exported schema -- the natural path is returned as a tolerated dangling
        link, unless that path already belongs to a *different* concept. In that
        case a free path is returned instead, so the link stays dangling rather
        than silently resolving to the wrong file.

        Args:
            category: The category subdirectory.
            name: The object name.

        Returns:
            The canonical or a non-colliding dangling path.
        """
        canonical = self._canonical.get((category, name))
        if canonical is not None:
            return canonical
        natural = f"{category}/{concept_filename(name)}"
        return natural if natural not in self._occupied else self._next_free(natural)

    def link_for(self, category: str, name: str) -> str:
        """Return a bundle-absolute link target such as ``/tables/orders.md``."""
        return "/" + self.path_for(category, name)

    def collisions(self) -> list[tuple[str, str, str]]:
        """Return ``(category, name, path)`` for every disambiguated allocation."""
        return sorted(self._collisions)

    def _next_free(self, natural: str) -> str:
        """Return the first free ``<stem>-N.md`` variant of a taken path."""
        stem = natural[: -len(".md")]
        suffix = 2
        while f"{stem}-{suffix}.md" in self._occupied:
            suffix += 1
        return f"{stem}-{suffix}.md"
