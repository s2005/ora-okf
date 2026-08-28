"""The OKF concept value object and its Markdown serializer.

An OKF concept file is Markdown with a leading ``---``-delimited YAML
frontmatter block. The only field the format requires is ``type``; everything
else is convention. Body sections are ordered ``(heading, body)`` pairs, and an
empty body is dropped rather than rendered as a bare heading.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import yaml

# Filenames the OKF layout reserves at the bundle root. A concept must never
# occupy one, or it would be read as bundle metadata.
RESERVED_FILENAMES: tuple[str, ...] = ("index.md", "log.md")


@dataclass
class OkfConcept:
    """Frontmatter metadata plus an ordered body.

    Attributes:
        frontmatter: Ordered frontmatter fields. Must carry a non-empty ``type``
            by the time :meth:`render` is called.
        sections: Ordered ``(heading, body)`` pairs. Empty bodies are dropped.
    """

    frontmatter: dict[str, Any] = field(default_factory=dict)
    sections: list[tuple[str, str]] = field(default_factory=list)

    def add_section(self, heading: str, body: str) -> None:
        """Append a section, ignoring one whose body is empty or whitespace."""
        if body and body.strip():
            self.sections.append((heading, body))

    def render(self) -> str:
        """Serialize to Markdown with a YAML frontmatter block.

        Section headings are emitted as H2 so the frontmatter ``title`` remains
        the document's only H1, which is what ``MD025`` requires.

        Returns:
            The rendered Markdown, ending in exactly one newline.

        Raises:
            ValueError: If ``type`` is missing or empty.
        """
        type_value = self.frontmatter.get("type")
        if not type_value or not str(type_value).strip():
            raise ValueError("An OKF concept requires a non-empty 'type' in its frontmatter")

        frontmatter_yaml = yaml.safe_dump(
            _normalize(self.frontmatter),
            sort_keys=False,
            allow_unicode=True,
            default_flow_style=False,
            width=4096,
        ).strip()

        parts: list[str] = ["---", frontmatter_yaml, "---", ""]
        for heading, body in self.sections:
            parts.append(f"## {heading}")
            parts.append("")
            parts.append(body.strip())
            parts.append("")
        return "\n".join(parts).rstrip() + "\n"


def _normalize(frontmatter: dict[str, Any]) -> dict[str, Any]:
    """Return frontmatter with values safe for ``yaml.safe_dump``.

    A ``None`` becomes an empty list so a declared-but-absent field (``tags``,
    ``primary_key``) round-trips as an empty collection instead of a bare
    ``null``, and tuples become lists so YAML renders a sequence rather than a
    Python-specific tag.

    Args:
        frontmatter: The source mapping.

    Returns:
        A new mapping suitable for serialization.
    """
    normalized: dict[str, Any] = {}
    for key, value in frontmatter.items():
        if value is None:
            normalized[key] = []
        elif isinstance(value, tuple):
            normalized[key] = list(value)
        else:
            normalized[key] = value
    return normalized


def parse_frontmatter(content: str) -> dict[str, Any] | None:
    """Parse the leading frontmatter block from Markdown content.

    Used by the bundle conformance self-check, which needs to distinguish "no
    frontmatter" from "frontmatter without a type" without raising.

    Args:
        content: Full file content.

    Returns:
        The parsed mapping, or None when there is no parseable frontmatter block.
    """
    lines = content.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    end_index = next((index for index in range(1, len(lines)) if lines[index].strip() == "---"), None)
    if end_index is None:
        return None
    try:
        parsed = yaml.safe_load("\n".join(lines[1:end_index]))
    except yaml.YAMLError:
        return None
    return parsed if isinstance(parsed, dict) else None
