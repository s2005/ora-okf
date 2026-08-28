"""Schema mapping: physical Oracle schema name to the name used in the bundle.

The mapping file is the *only* input that drives renaming. It is deliberately
separate from the credentials file: credentials say which database to read,
the mapping says what the exported documentation is allowed to call things.

Accepted shape (YAML shown; the same structure in JSON is accepted)::

    version: 1
    unmapped: keep          # keep | redact | error
    redacted_name: EXTERNAL # substituted when unmapped is "redact"
    schemas:
      APP_PROD_OWNER: APP
      REF_PROD_OWNER: REF

``schemas`` maps a physical schema to its published label. Keys are compared
case-insensitively (Oracle reports conventional identifiers upper case), and
lookups normalize to upper case.

``unmapped`` decides what happens to a schema that is referenced by the
extracted objects but absent from ``schemas``:

* ``keep`` (default) leaves the physical name in place. The post-write audit
  still reports it, so nothing leaks silently.
* ``redact`` replaces it with ``redacted_name``, collapsing every unmapped
  schema onto one placeholder.
* ``error`` refuses to export until every referenced schema has an entry. This
  is the setting to use when a bundle is destined for a public repository.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .errors import MappingError

# Policies for a referenced schema with no entry in ``schemas``.
UNMAPPED_KEEP = "keep"
UNMAPPED_REDACT = "redact"
UNMAPPED_ERROR = "error"
UNMAPPED_POLICIES = (UNMAPPED_KEEP, UNMAPPED_REDACT, UNMAPPED_ERROR)

DEFAULT_REDACTED_NAME = "EXTERNAL"

# A published label is substituted into ``COMMENT ON`` statements, resource
# identifiers, and PL/SQL source, so it has to be a legal unquoted Oracle
# identifier. Rejecting anything else here turns a corrupted bundle into a
# startup error.
_LABEL_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_$#]*$")

_ALLOWED_KEYS = frozenset({"version", "schemas", "unmapped", "redacted_name"})


@dataclass(frozen=True)
class SchemaMapping:
    """A validated physical-to-published schema mapping.

    Attributes:
        entries: Physical schema name (upper case) to published label.
        unmapped: The policy for a referenced schema absent from ``entries``.
        redacted_name: The label substituted under the ``redact`` policy.
        source: The file the mapping was loaded from, for error messages. Empty
            for a mapping built in memory (``SchemaMapping.identity``).
    """

    entries: Mapping[str, str]
    unmapped: str = UNMAPPED_KEEP
    redacted_name: str = DEFAULT_REDACTED_NAME
    source: str = ""

    @staticmethod
    def identity() -> SchemaMapping:
        """Return an empty mapping that renames nothing.

        Used when ``--mapping`` is omitted, so the export path is the same with
        and without a mapping file and needs no None checks.
        """
        return SchemaMapping(entries={}, unmapped=UNMAPPED_KEEP)

    def is_mapped(self, physical: str) -> bool:
        """Return True when ``physical`` has an explicit entry."""
        return _normalize(physical) in self.entries

    def resolve(self, physical: str) -> str:
        """Return the published label for a physical schema name.

        Args:
            physical: The physical schema name, in any case.

        Returns:
            The mapped label, the redaction placeholder, or the name unchanged,
            according to the ``unmapped`` policy. An empty input returns empty.

        Raises:
            MappingError: When the policy is ``error`` and the schema has no entry.
        """
        if not physical:
            return physical
        key = _normalize(physical)
        mapped = self.entries.get(key)
        if mapped is not None:
            return mapped
        if self.unmapped == UNMAPPED_REDACT:
            return self.redacted_name
        if self.unmapped == UNMAPPED_ERROR:
            raise MappingError(
                f"Schema '{physical}' is referenced by the extracted objects but has no entry "
                f"in the mapping{self._source_suffix()}. Add it under 'schemas:', or set "
                f"'unmapped: keep' to leave it as-is."
            )
        return physical

    def missing(self, referenced: Iterable[str]) -> tuple[str, ...]:
        """Return the referenced schemas that have no explicit entry.

        Args:
            referenced: Physical schema names discovered during extraction.

        Returns:
            The unmapped names, upper-cased and sorted, without duplicates.
        """
        unknown = {_normalize(name) for name in referenced if name and not self.is_mapped(name)}
        return tuple(sorted(unknown))

    def physical_names(self) -> tuple[str, ...]:
        """Return every mapped physical name, sorted, for the leak audit."""
        return tuple(sorted(self.entries))

    def _source_suffix(self) -> str:
        return f" ({self.source})" if self.source else ""


def load_mapping(path: Path) -> SchemaMapping:
    """Load and validate a mapping file in YAML or JSON.

    The format is chosen by file extension: ``.json`` is parsed as JSON (for
    precise error positions), anything else as YAML. YAML is a JSON superset, so
    a JSON document with a non-JSON extension still parses.

    Args:
        path: Path to the mapping file.

    Returns:
        The validated mapping.

    Raises:
        MappingError: If the file is missing, unparseable, or fails validation.
    """
    if not path.is_file():
        raise MappingError(f"Mapping file not found: {path}")
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise MappingError(f"Cannot read mapping file {path}: {exc}") from exc

    document = _parse_document(path, text)
    if document is None:
        raise MappingError(f"Mapping file {path} is empty")
    if not isinstance(document, dict):
        raise MappingError(
            f"Mapping file {path} must contain a mapping at the top level, got {type(document).__name__}"
        )

    _reject_unknown_keys(path, document)
    entries = _parse_entries(path, document.get("schemas"))
    unmapped = _parse_unmapped(path, document.get("unmapped", UNMAPPED_KEEP))
    redacted = _parse_redacted_name(path, document.get("redacted_name", DEFAULT_REDACTED_NAME))
    _reject_chained_renames(path, entries)

    return SchemaMapping(entries=entries, unmapped=unmapped, redacted_name=redacted, source=str(path))


def _parse_document(path: Path, text: str) -> Any:
    """Parse mapping text as JSON or YAML, chosen by extension."""
    if path.suffix.lower() == ".json":
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise MappingError(f"Mapping file {path} is not valid JSON: {exc}") from exc
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise MappingError(f"Mapping file {path} is not valid YAML: {exc}") from exc


def _reject_unknown_keys(path: Path, document: dict[str, Any]) -> None:
    """Reject top-level keys the format does not define.

    A typo such as ``schema:`` for ``schemas:`` would otherwise yield a mapping
    that renames nothing, and the failure would only show up as a leak much
    later. Failing at load time points straight at the typo.
    """
    unknown = sorted(set(document) - _ALLOWED_KEYS)
    if unknown:
        allowed = ", ".join(sorted(_ALLOWED_KEYS))
        raise MappingError(f"Mapping file {path} has unknown key(s): {', '.join(unknown)}. Allowed keys: {allowed}")


def _parse_entries(path: Path, raw: Any) -> dict[str, str]:
    """Validate and normalize the ``schemas`` block."""
    if raw is None:
        raise MappingError(f"Mapping file {path} must define a 'schemas' block")
    if not isinstance(raw, dict):
        raise MappingError(f"Mapping file {path}: 'schemas' must be a mapping, got {type(raw).__name__}")
    if not raw:
        raise MappingError(f"Mapping file {path}: 'schemas' must not be empty")

    entries: dict[str, str] = {}
    for physical, published in raw.items():
        key = _validate_physical(path, physical)
        label = _validate_label(path, physical, published)
        if key in entries and entries[key] != label:
            raise MappingError(
                f"Mapping file {path}: schema '{physical}' is mapped twice, to "
                f"'{entries[key]}' and '{label}'. Schema keys are case-insensitive."
            )
        entries[key] = label
    return entries


def _validate_physical(path: Path, physical: Any) -> str:
    """Validate one physical schema key and return its normalized form."""
    if not isinstance(physical, str) or not physical.strip():
        raise MappingError(f"Mapping file {path}: every key under 'schemas' must be a non-empty schema name")
    return _normalize(physical)


def _validate_label(path: Path, physical: Any, published: Any) -> str:
    """Validate one published label and return it unchanged."""
    if not isinstance(published, str) or not published.strip():
        raise MappingError(f"Mapping file {path}: schema '{physical}' must map to a non-empty name")
    label = published.strip()
    if not _LABEL_RE.match(label):
        raise MappingError(
            f"Mapping file {path}: '{label}' (for schema '{physical}') is not a valid identifier. "
            "A published name is substituted into SQL text, so it must start with a letter and "
            "contain only letters, digits, and _ $ #."
        )
    return label


def _parse_unmapped(path: Path, raw: Any) -> str:
    """Validate the ``unmapped`` policy."""
    if not isinstance(raw, str):
        raise MappingError(f"Mapping file {path}: 'unmapped' must be one of {', '.join(UNMAPPED_POLICIES)}")
    policy = raw.strip().lower()
    if policy not in UNMAPPED_POLICIES:
        raise MappingError(
            f"Mapping file {path}: 'unmapped' must be one of {', '.join(UNMAPPED_POLICIES)}, got '{raw}'"
        )
    return policy


def _parse_redacted_name(path: Path, raw: Any) -> str:
    """Validate the ``redacted_name`` placeholder."""
    if not isinstance(raw, str) or not raw.strip():
        raise MappingError(f"Mapping file {path}: 'redacted_name' must be a non-empty name")
    label = raw.strip()
    if not _LABEL_RE.match(label):
        raise MappingError(f"Mapping file {path}: 'redacted_name' value '{label}' is not a valid identifier")
    return label


def _reject_chained_renames(path: Path, entries: dict[str, str]) -> None:
    """Reject a mapping whose output feeds back into its own input.

    Mapping ``A -> B`` alongside ``B -> C`` has no well-defined result: whether
    an ``A`` reference ends up as ``B`` or ``C`` would depend on substitution
    order. Renaming is applied as a single simultaneous pass precisely so order
    cannot matter, and this check keeps a mapping that would make the question
    observable from being accepted at all. An identity entry (``A -> A``) is
    exempt: it is a deliberate "leave this one alone" declaration.
    """
    for physical, published in sorted(entries.items()):
        target_key = _normalize(published)
        if target_key == physical:
            continue
        if target_key in entries:
            raise MappingError(
                f"Mapping file {path}: schema '{physical}' maps to '{published}', but '{published}' is "
                "itself a mapped schema. Chained renames are ambiguous - map each physical schema to a "
                "published name that is not also a physical schema key."
            )


def _normalize(name: str) -> str:
    """Return the case-insensitive lookup key for a schema name."""
    return name.strip().upper()
