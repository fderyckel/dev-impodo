"""Build minimal, batched read requests from a compiled migration plan.

This module sits between source preparation and connector execution:

* the compiler validates permitted models, fields, identities, and relations;
* `source.py` supplies prepared source identities and reference keys.
* this module groups those requirements per Odoo model;
* `connectors.py` executes one metadata request and paginated record request
  per model, rather than one call per source row.

The planner produces data-only request contracts and never contacts Odoo.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from hashlib import sha256
from typing import Any, Iterable

from .connectors import MetadataRequest, RecordRequest
from .domain.compiler.contracts import CompiledMigrationPlan
from .domain.errors import ReadinessError
from .models import LogicalReference, PreparedRecord, canonical_json_bytes, portable_value
from .profile import DatasetSpec, ResolveSpec


PREFLIGHT_REQUIREMENT_PLAN_VERSION = 1
MAX_KEYS_PER_RECORD_REQUEST = 500


@dataclass(frozen=True, slots=True)
class PreflightRequirementPlan:
    """Versioned, deterministic and bounded target-read requirements."""

    metadata_requests: tuple[MetadataRequest, ...]
    record_requests: tuple[RecordRequest, ...]
    source_record_count: int
    contract_version: int = PREFLIGHT_REQUIREMENT_PLAN_VERSION

    @property
    def semantic_hash(self) -> str:
        payload = {
            "contract_version": self.contract_version,
            "source_record_count": self.source_record_count,
            "metadata": [
                {
                    "model": item.model,
                    "fields": list(item.fields),
                    "all_fields": item.all_fields,
                    "include_unique_constraints": item.include_unique_constraints,
                }
                for item in self.metadata_requests
            ],
            "records": [
                {
                    "model": item.model,
                    "fields": list(item.fields),
                    "domain": portable_value(item.domain),
                }
                for item in self.record_requests
            ],
        }
        return "sha256:" + sha256(canonical_json_bytes(payload)).hexdigest()

    @property
    def model_count(self) -> int:
        return len({item.model for item in self.record_requests})

    @property
    def chunk_count(self) -> int:
        return len(self.record_requests)


def plan_metadata_requests(plan: CompiledMigrationPlan) -> tuple[MetadataRequest, ...]:
    """Return the smallest deterministic metadata field set per Odoo model.

    Target models and target-only reference models are merged so each model
    appears once. The connector later calls `fields_get` for these requests.
    """

    fields: dict[str, set[str]] = defaultdict(set)
    for dataset in plan.datasets:
        fields[dataset.target.model].update(_dataset_target_fields(dataset))
        for resolve in _resolvers(dataset):
            if resolve.target_model is not None:
                fields[resolve.target_model].update(resolve.target_fields)
                fields[resolve.target_model].update(resolve.target_scope_fields)
    return tuple(
        MetadataRequest(model=model, fields=tuple(sorted(model_fields)))
        for model, model_fields in sorted(fields.items())
    )


def plan_record_requests(
    plan: CompiledMigrationPlan,
    records: Iterable[PreparedRecord],
) -> tuple[RecordRequest, ...]:
    """Build batched target-record requests from prepared business keys.

    Simple one-field identities and target-only reference keys become bounded
    `in` domains. Composite or relational identities fall back to the
    plan's declared domain because narrowing them incorrectly could hide a
    legitimate match. Requested fields and models are sorted for deterministic
    snapshots.
    """

    return plan_preflight_requirements(plan, records).record_requests


def plan_preflight_requirements(
    plan: CompiledMigrationPlan,
    records: Iterable[PreparedRecord],
    *,
    maximum_keys_per_request: int = MAX_KEYS_PER_RECORD_REQUEST,
) -> PreflightRequirementPlan:
    """Build one deterministic plan whose record reads are always narrowed."""

    if maximum_keys_per_request < 1:
        raise ValueError("maximum_keys_per_request must be positive")
    prepared_records = tuple(records)
    records_by_dataset: dict[str, list[PreparedRecord]] = defaultdict(list)
    for record in prepared_records:
        records_by_dataset[record.dataset].append(record)

    fields: dict[str, set[str]] = defaultdict(set)
    domain_chunks: dict[str, list[list[Any]]] = defaultdict(list)
    for dataset in plan.datasets:
        dataset_records = records_by_dataset.get(dataset.name, ())
        if dataset_records:
            dataset_domains = _dataset_identity_domain_chunks(
                dataset,
                dataset_records,
                maximum_keys_per_request,
            )
            fields[dataset.target.model].update(_dataset_target_fields(dataset))
            if dataset_domains:
                domain_chunks[dataset.target.model].extend(
                    _combine_domains(list(dataset.target_domain), domain)
                    for domain in dataset_domains
                )
            elif dataset.target_domain:
                domain_chunks[dataset.target.model].append(
                    list(dataset.target_domain)
                )
            else:
                raise ReadinessError(
                    f"Odoo reads for {dataset.name} cannot be narrowed safely"
                )

        for component, references in _identity_reference_groups(
            dataset, dataset_records
        ):
            resolve = component.resolve
            if resolve is None or resolve.target_model is None:
                continue
            fields[resolve.target_model].update(resolve.target_fields)
            fields[resolve.target_model].update(resolve.target_scope_fields)
            reference_domains = _key_domain_chunks(
                (*resolve.target_fields, *resolve.target_scope_fields),
                [(*reference.key, *reference.scope) for reference in references],
                maximum_keys_per_request,
            )
            domain_chunks[resolve.target_model].extend(reference_domains)
            if references and not reference_domains:
                raise ReadinessError(
                    f"Odoo relationship reads for {dataset.name} cannot be narrowed safely"
                )

        for target_field, relation in dataset.relations.items():
            resolve = relation.resolve
            if resolve.target_model is None:
                continue
            references = [
                reference
                for record in dataset_records
                for reference in _references_for_field(
                    record.references.get(target_field)
                )
                if reference.origin == "target"
            ]
            if not references:
                continue
            fields[resolve.target_model].update(resolve.target_fields)
            fields[resolve.target_model].update(resolve.target_scope_fields)
            reference_domains = _key_domain_chunks(
                (*resolve.target_fields, *resolve.target_scope_fields),
                [(*reference.key, *reference.scope) for reference in references],
                maximum_keys_per_request,
            )
            if not reference_domains:
                raise ReadinessError(
                    f"Odoo relationship reads for {dataset.name} cannot be narrowed safely"
                )
            domain_chunks[resolve.target_model].extend(reference_domains)

    requests: list[RecordRequest] = []
    for model, chunks in sorted(domain_chunks.items()):
        unique_chunks = {
            canonical_json_bytes(portable_value(tuple(chunk))): chunk
            for chunk in chunks
            if chunk
        }
        for encoded in sorted(unique_chunks):
            requests.append(
                RecordRequest(
                    model=model,
                    fields=tuple(sorted(fields[model])),
                    domain=tuple(unique_chunks[encoded]),
                )
            )
    return PreflightRequirementPlan(
        metadata_requests=plan_metadata_requests(plan),
        record_requests=tuple(requests),
        source_record_count=len(prepared_records),
    )


def _dataset_identity_domain_chunks(
    dataset: DatasetSpec,
    records: Iterable[PreparedRecord],
    chunk_size: int,
) -> list[list[Any]]:
    fields: list[str] = []
    for component in (
        *dataset.target_identity.components,
        *dataset.target_identity.scope,
    ):
        if component.resolve is None:
            fields.extend(component.target_fields)
            continue
        resolve = component.resolve
        if (
            resolve.target_model is None
            or len(component.target_fields) != 1
        ):
            return []
        relation_field = component.target_fields[0]
        fields.extend(
            f"{relation_field}.{field}"
            for field in (*resolve.target_fields, *resolve.target_scope_fields)
        )
    if not fields:
        return []
    keys = []
    for record in records:
        key = _record_target_key(dataset, record)
        if key is None:
            return []
        keys.append(key)
    return _key_domain_chunks(tuple(fields), keys, chunk_size)


def _record_target_key(
    dataset: DatasetSpec,
    record: PreparedRecord,
) -> tuple[Any, ...] | None:
    result: list[Any] = []
    for components, values in (
        (dataset.target_identity.components, record.target_identity),
        (dataset.target_identity.scope, record.target_scope),
    ):
        cursor = 0
        for component in components:
            if component.resolve is None:
                width = len(component.target_fields)
                selected = values[cursor : cursor + width]
                if len(selected) != width:
                    return None
                result.extend(selected)
                cursor += width
                continue
            if cursor >= len(values):
                return None
            reference = values[cursor]
            cursor += 1
            resolve = component.resolve
            if (
                not isinstance(reference, LogicalReference)
                or reference.origin != "target"
                or resolve.target_model is None
                or len(reference.key) != len(resolve.target_fields)
                or len(reference.scope) != len(resolve.target_scope_fields)
            ):
                return None
            result.extend(reference.key)
            result.extend(reference.scope)
        if cursor != len(values):
            return None
    return tuple(result)


def _key_domain_chunks(
    fields: tuple[str, ...],
    keys: Iterable[tuple[Any, ...]],
    chunk_size: int,
) -> list[list[Any]]:
    unique_keys = sorted(
        {
            tuple(key)
            for key in keys
            if len(key) == len(fields) and all(value is not None for value in key)
        },
        key=lambda item: canonical_json_bytes(portable_value(item)),
    )
    if not fields or not unique_keys:
        return []
    chunks: list[list[Any]] = []
    for start in range(0, len(unique_keys), chunk_size):
        batch = unique_keys[start : start + chunk_size]
        if len(fields) == 1:
            chunks.append([[fields[0], "in", [key[0] for key in batch]]])
            continue
        expressions = [
            _and_terms(
                [[field, "=", value] for field, value in zip(fields, key, strict=True)]
            )
            for key in batch
        ]
        chunks.append(_or_expressions(expressions))
    return chunks


def _and_terms(terms: list[Any]) -> list[Any]:
    if len(terms) <= 1:
        return terms
    return ["&"] * (len(terms) - 1) + terms


def _or_expressions(expressions: list[list[Any]]) -> list[Any]:
    if len(expressions) == 1:
        return expressions[0]
    return ["|"] * (len(expressions) - 1) + [
        item for expression in expressions for item in expression
    ]


def _dataset_target_fields(dataset: DatasetSpec) -> set[str]:
    """Collect target scalar, relation, identity, and scope fields."""

    fields = set(dataset.fields)
    fields.update(dataset.relations)
    for component in (
        *dataset.target_identity.components,
        *dataset.target_identity.scope,
    ):
        fields.update(component.target_fields)
    return fields


def _resolvers(dataset: DatasetSpec) -> list[ResolveSpec]:
    """Collect all identity and field resolvers declared by a dataset."""

    resolvers = [
        component.resolve
        for component in (
            *dataset.target_identity.components,
            *dataset.target_identity.scope,
        )
        if component.resolve is not None
    ]
    resolvers.extend(relation.resolve for relation in dataset.relations.values())
    return resolvers


def _combine_domains(left: list[Any], right: list[Any]) -> list[Any]:
    """Combine two Odoo domain fragments using implicit logical AND."""

    if not left:
        return right
    if not right:
        return left
    # Odoo domains implicitly AND adjacent expressions.
    return [*left, *right]


def _references_for_field(value: Any) -> tuple[LogicalReference, ...]:
    """Return unresolved logical references from one prepared field value."""

    if isinstance(value, LogicalReference):
        return (value,)
    if isinstance(value, tuple):
        return tuple(item for item in value if isinstance(item, LogicalReference))
    return ()


def _identity_reference_groups(
    dataset: DatasetSpec,
    records: Iterable[PreparedRecord],
) -> list[tuple[Any, tuple[LogicalReference, ...]]]:
    """Pair relational identity components with their prepared references.

    Prepared identities are flattened tuples. `cursor` reconstructs which
    flattened value belongs to each compiled component so the planner can batch
    the related-model lookup.
    """

    result = []
    for components, attribute in (
        (dataset.target_identity.components, "target_identity"),
        (dataset.target_identity.scope, "target_scope"),
    ):
        cursor = 0
        for component in components:
            width = 1 if component.resolve is not None else len(component.target_fields)
            if component.resolve is not None:
                references = tuple(
                    value
                    for record in records
                    for value in (getattr(record, attribute)[cursor],)
                    if isinstance(value, LogicalReference)
                )
                result.append((component, references))
            cursor += width
    return result
