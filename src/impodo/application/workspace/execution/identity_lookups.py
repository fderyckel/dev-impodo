"""Resolve reviewed ancestor keys in batches before native flat-field lookups.

Odoo accepts dotted domains but does not return dotted search_read fields.
Resolve each distinct ancestor from the deepest key outward, then send exact
numeric parent domains only inside the current target operation. Portable
snapshots and journals retain their business references and binding hashes.
"""

from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

from impodo.domain.execution.odoo_readback import (
    MAX_READBACK_LOOKUPS,
    OdooReadbackReader,
    ReadbackLookup,
)
from impodo.domain.execution_snapshot import ExecutionDataset, ExecutionRow
from impodo.domain.shared.models import BusinessReference
from impodo.domain.workspace.errors import WorkspaceError

Domain = tuple[tuple[str, str, Any], ...]
Lookup = Callable[[str, Sequence[Domain]], tuple[tuple[int, ...], ...]]


class NestedIdentityLookup:
    """Keep exact related-model paths and ancestor results within one operation."""

    def __init__(
        self,
        rows: Iterable[ExecutionRow],
        metadata: Mapping[str, ExecutionDataset],
        lookup: Lookup,
        *,
        page_size: int,
    ) -> None:
        self.lookup = lookup
        self.page_size = page_size
        self.relations: dict[tuple[str, str], str] = {}
        self.cache: dict[tuple[str, Domain], tuple[int, ...]] = {}
        for row in rows:
            dataset = metadata[row.dataset]
            self._register(
                row.target_model,
                (*dataset.identity_fields, *dataset.scope_fields),
                (*row.business_identity, *row.business_scope),
            )
            for intent in row.fields:
                if intent.kind != "relation":
                    continue
                values = (
                    intent.value if isinstance(intent.value, tuple) else (intent.value,)
                )
                for value in values:
                    if isinstance(value, BusinessReference):
                        self._register(
                            value.model,
                            (
                                *intent.related_identity_fields,
                                *intent.related_scope_fields,
                            ),
                            (*value.key, *value.scope),
                        )

    def _register(
        self, model: str, fields: tuple[str, ...], values: tuple[Any, ...]
    ) -> None:
        for name, value in zip(fields, values, strict=True):
            if not isinstance(value, BusinessReference):
                continue
            previous = self.relations.setdefault((model, name), value.model)
            if previous != value.model:
                raise WorkspaceError("Reviewed parent identity models conflict")
            if value.target_fields:
                self._register(
                    value.model,
                    (*value.target_fields, *value.target_scope_fields),
                    (*value.key, *value.scope),
                )

    def translate(self, model: str, domains: Sequence[Domain]) -> tuple[Domain, ...]:
        """Replace only reviewed ancestor paths with exact, uniquely resolved IDs."""

        split: list[
            tuple[list[tuple[str, str, Any]], dict[str, list[tuple[str, str, Any]]]]
        ] = []
        wanted: dict[str, set[Domain]] = {}
        for domain in domains:
            direct: list[tuple[str, str, Any]] = []
            parents: dict[str, list[tuple[str, str, Any]]] = {}
            for field, operator, value in domain:
                head, separator, remaining = field.partition(".")
                if not separator:
                    direct.append((head, operator, value))
                    continue
                related_model = self.relations.get((model, head))
                if related_model is None:
                    raise WorkspaceError(
                        "An ancestor lookup is outside the reviewed identity"
                    )
                parents.setdefault(head, []).append((remaining, operator, value))
            for head, conditions in parents.items():
                wanted.setdefault(self.relations[(model, head)], set()).add(
                    tuple(conditions)
                )
            split.append((direct, parents))
        for related_model, required in sorted(wanted.items()):
            pending = tuple(
                sorted(
                    (
                        domain
                        for domain in required
                        if (related_model, domain) not in self.cache
                    ),
                    key=repr,
                )
            )
            if not pending:
                continue
            translated = self.translate(related_model, pending)
            for start in range(0, len(pending), self.page_size):
                originals = pending[start : start + self.page_size]
                results = self.lookup(
                    related_model, translated[start : start + self.page_size]
                )
                if len(results) != len(originals):
                    raise WorkspaceError("Odoo returned incomplete ancestor identities")
                for original, identifiers in zip(originals, results, strict=True):
                    if len(identifiers) != 1:
                        raise WorkspaceError(
                            "An ancestor business key is missing or ambiguous; compare again"
                        )
                    self.cache[(related_model, original)] = identifiers
        result = []
        for direct, parents in split:
            for head, conditions in parents.items():
                identifiers = self.cache[
                    (self.relations[(model, head)], tuple(conditions))
                ]
                direct.append((head, "=", identifiers[0]))
            result.append(tuple(direct))
        return tuple(result)


def readback_identity_lookup(
    rows: Iterable[ExecutionRow],
    metadata: Mapping[str, ExecutionDataset],
    reader: OdooReadbackReader,
) -> NestedIdentityLookup:
    """Use the bounded read-back port for temporary ancestor ID resolution."""

    def lookup(model: str, domains: Sequence[Domain]) -> tuple[tuple[int, ...], ...]:
        return tuple(
            tuple(record.odoo_id for record in records)
            for records in reader.find_records_many(
                model, tuple(ReadbackLookup(domain=domain) for domain in domains)
            )
        )

    return NestedIdentityLookup(rows, metadata, lookup, page_size=MAX_READBACK_LOOKUPS)
