"""Write a rendered OKF bundle to disk, safely and repeatably.

Two behaviours here exist to make reruns safe rather than merely convenient:

**Stale files are cleared.** A bundle is regenerated in full, so a concept for
an object dropped or renamed since the last run would otherwise survive on disk
and still pass the conformance check, leaving a bundle that quietly no longer
describes the schema.

**Only an owned directory is cleared.** Because the writer deletes Markdown, it
refuses to touch a pre-existing non-empty directory that lacks its marker file.
A mistyped ``--okf-dir docs`` is then an error message instead of a deleted
documentation tree.
"""

from __future__ import annotations

import logging
from pathlib import Path

from ..errors import BundleError
from ..model import SchemaModel
from .concept import RESERVED_FILENAMES, parse_frontmatter
from .renderers import RenderConfig, build_placements, render_bundle

logger = logging.getLogger(__name__)

# Dropped into every directory the writer owns. Its presence is what authorizes
# clearing stale Markdown on a later run.
BUNDLE_MARKER = ".okf-bundle"

_MARKER_CONTENT = (
    "This directory is an OKF bundle managed by ora-okf.\n"
    "Every .md file under it is regenerated on each export; do not hand-edit them.\n"
)


class BundleWriter:
    """Render a model into a directory and verify what was written."""

    def __init__(self, okf_dir: Path, config: RenderConfig) -> None:
        """Initialize the writer.

        Args:
            okf_dir: The bundle root directory.
            config: The render configuration.
        """
        self.okf_dir = okf_dir
        self.config = config

    def write(self, model: SchemaModel) -> list[str]:
        """Write the complete bundle.

        Args:
            model: The (already renamed) schema model to render.

        Returns:
            The bundle-relative POSIX paths written, sorted.

        Raises:
            BundleError: If the directory is not owned by the exporter, or a
                file cannot be written.
        """
        self._prepare_directory()
        self._clear_stale_markdown()
        self._warn_collisions(model)

        files = render_bundle(model, self.config)
        for relative_path, content in files:
            self._write_file(self.okf_dir / relative_path, content)

        nonconforming = self.conformance_failures()
        if nonconforming:
            logger.warning(
                "OKF bundle has %d non-conformant concept file(s): %s",
                len(nonconforming),
                ", ".join(nonconforming[:5]),
            )

        logger.info("OKF bundle written to %s (%d files)", self.okf_dir, len(files))
        return [relative_path for relative_path, _content in files]

    def conformance_failures(self) -> list[str]:
        """Return relative paths of concept files lacking a usable ``type``.

        The two reserved bundle-root files are exempt because they are metadata,
        not concepts. The exemption matches on the full relative path rather
        than the basename, so a genuine concept at ``tables/log.md`` is still
        checked instead of being silently skipped.

        Returns:
            The sorted failing paths.
        """
        reserved = set(RESERVED_FILENAMES)
        failures: list[str] = []
        for md_file in sorted(self.okf_dir.rglob("*.md")):
            relative = md_file.relative_to(self.okf_dir).as_posix()
            if relative in reserved:
                continue
            if not _has_type(md_file):
                failures.append(relative)
        return failures

    def _prepare_directory(self) -> None:
        """Create the bundle directory after establishing ownership.

        A directory is owned when it is missing, empty, or already carries the
        marker. Anything else is someone's data and is refused.

        Raises:
            BundleError: When the directory exists, is non-empty, and is unowned.
        """
        marker = self.okf_dir / BUNDLE_MARKER
        if self.okf_dir.exists():
            if not self.okf_dir.is_dir():
                raise BundleError(f"--okf-dir points at a file, not a directory: {self.okf_dir}")
            if not marker.exists() and any(self.okf_dir.iterdir()):
                raise BundleError(
                    f"Refusing to write an OKF bundle into the non-empty directory '{self.okf_dir}', "
                    f"which was not created by ora-okf: a rerun deletes every .md file under it. "
                    f"Point --okf-dir at a fresh or previously exported directory."
                )
        try:
            self.okf_dir.mkdir(parents=True, exist_ok=True)
            marker.write_text(_MARKER_CONTENT, encoding="utf-8")
        except OSError as exc:
            raise BundleError(f"Cannot prepare bundle directory {self.okf_dir}: {exc}") from exc

    def _clear_stale_markdown(self) -> None:
        """Delete every ``.md`` file from a previous run."""
        removed = 0
        for md_file in self.okf_dir.rglob("*.md"):
            try:
                md_file.unlink()
                removed += 1
            except OSError as exc:
                raise BundleError(f"Cannot remove stale file {md_file}: {exc}") from exc
        if removed:
            logger.debug("Cleared %d stale Markdown file(s) from %s", removed, self.okf_dir)

    @staticmethod
    def _warn_collisions(model: SchemaModel) -> None:
        """Log every concept filename that had to be disambiguated."""
        allocator, _placements = build_placements(model)
        for category, name, path in allocator.collisions():
            logger.warning("OKF filename collision disambiguated: %s/%s -> %s", category, name, path)

    @staticmethod
    def _write_file(path: Path, content: str) -> None:
        """Write one file, creating parent directories as needed."""
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            # newline="\n" keeps bundle bytes identical across platforms, so a
            # bundle generated on Windows and one generated in CI compare equal.
            with open(path, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(content)
        except OSError as exc:
            raise BundleError(f"Cannot write {path}: {exc}") from exc


def _has_type(path: Path) -> bool:
    """Return True when a file has frontmatter with a non-empty ``type``."""
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return False
    frontmatter = parse_frontmatter(content)
    if frontmatter is None:
        return False
    type_value = frontmatter.get("type")
    return bool(type_value) and bool(str(type_value).strip())
