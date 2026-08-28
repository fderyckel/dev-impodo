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

from impodo.domain.odoo.contracts import MetadataRequest, RecordRequest
from impodo.domain.compiler.contracts import CompiledMigrationPlan
from impodo.domain.errors import ReadinessError
from impodo.domain.shared.models import (
    LogicalReference,
    PreparedRecord,
    canonical_json_bytes,
    portable_value,
)
from impodo.domain.recipe.profile import DatasetSpec, ResolveSpec
from impodo.domain.relationship_dependencies import DatasetDependencyEdge
from impodo.domain.workspace.reference_keys import REFERENCE_POLICY_HASH


PREFLIGHT_REQUIREMENT_PLAN_VERSION = 3
MAX_KEYS_PER_RECORD_REQUEST = 500


@dataclass(frozen=True, slots=True)
class ReferenceReadRequirement:
    """Preserve why one related Odoo model appears in a bounded read plan."""

    parent_model: str
    relationship_field: str
    relationship_type: str
    relation_model: str
    key_fields: tuple[str, ...]
    scope_fields: tuple[str, ...]
    requested_fields: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PreflightRequirementPlan:
    """Versioned, deterministic and bounded target-read requirements."""

    metadata_requests: tuple[MetadataRequest, ...]
    record_requests: tuple[RecordRequest, ...]
    reference_requirements: tuple[ReferenceReadRequirement, ...]
    source_record_count: int
    dependency_edges: tuple[DatasetDependencyEdge, ...] = ()
    reference_policy_hash: str = REFERENCE_POLICY_HASH
    contract_version: int = PREFLIGHT_REQUIREMENT_PLAN_VERSION

    @property
    def semantic_hash(self) -> str:
        """Fingerprint the exact models, fields, domains, chunks, and row count."""

        payload = {
            "contract_version": self.contract_version,
            "source_record_count": self.source_record_count,
            "reference_policy_hash": self.reference_policy_hash,
            "dependency_edges": [
                edge.portable_dict() for edge in self.dependency_edges
            ],
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
            "references": [
                {
                    "parent_model": item.parent_model,
                    "relationship_field": item.relationship_field,
                    "relationship_type": item.relationship_type,
                    "relation_model": item.relation_model,
                    "key_fields": list(item.key_fields),
                    "scope_fields": list(item.scope_fields),
                    "requested_fields": list(item.requested_fields),
                }
                for item in self.reference_requirements
            ],
        }
        return "sha256:" + sha256(canonical_json_bytes(payload)).hexdigest()

    @property
    def model_count(self) -> int:
        """Count distinct models appearing in bounded record requests."""

        return len({item.model for item in self.record_requests})

    @property
    def chunk_count(self) -> int:
        """Count record requests after business keys are split into safe chunks."""

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

    Direct, composite, and governed relational identities become bounded
    domains. Requested fields and models are sorted for deterministic
    snapshots.
    """

    return plan_preflight_requirements(plan, records).record_requests


def plan_preflight_requirements(
    plan: CompiledMigrationPlan,
    records: Iterable[PreparedRecord],
    *,
    maximum_keys_per_request: int = MAX_KEYS_PER_RECORD_REQUEST,
) -> PreflightRequirementPlan:
    """Build one deterministic plan whose record reads are always narrowed.

    Prepared identities and logical relationship keys become allowlisted Odoo
    domains, split at ``maximum_keys_per_request``. Dataset target domains are
    combined with those keys. A dataset or relationship that has records but
    cannot produce a non-empty narrowing domain raises before connector use.
    """

    if maximum_keys_per_request < 1:
        raise ValueError("maximum_keys_per_request must be positive")
    prepared_records = tuple(records)
    records_by_dataset: dict[str, list[PreparedRecord]] = defaultdict(list)
    for record in prepared_records:
        records_by_dataset[record.dataset].append(record)
    incoming_identity_index = _incoming_identity_index(prepared_records)
    target_key_cache: dict[tuple[str, int], tuple[Any, ...] | None] = {}

    fields: dict[str, set[str]] = defaultdict(set)
    domain_chunks: dict[str, list[list[Any]]] = defaultdict(list)
    for dataset in plan.datasets:
        dataset_records = records_by_dataset.get(dataset.name, ())
        if dataset_records:
            dataset_domains = _dataset_identity_domain_chunks(
                plan,
                dataset,
                dataset_records,
                incoming_identity_index,
                target_key_cache,
                maximum_keys_per_request,
            )
            fields[dataset.target.model].update(_dataset_target_fields(dataset))
            if dataset_domains is None:
                if dataset.target_domain:
                    domain_chunks[dataset.target.model].append(
                        list(dataset.target_domain)
                    )
                else:
                    raise ReadinessError(
                        f"Odoo reads for {dataset.name} cannot be narrowed safely"
                    )
            elif dataset_domains:
                domain_chunks[dataset.target.model].extend(
                    _combine_domains(list(dataset.target_domain), domain)
                    for domain in dataset_domains
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
            reference_domains.extend(
                _casefold_key_domain_chunks(
                    (*resolve.target_fields, *resolve.target_scope_fields),
                    (
                        (*reference.key, *reference.scope)
                        for reference in references
                        if reference.origin == "target_then_incoming"
                    ),
                    maximum_keys_per_request,
                )
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
                if reference.origin in {"target", "target_then_incoming"}
            ]
            if not references:
                continue
            fields[resolve.target_model].update(resolve.target_fields)
            fields[resolve.target_model].update(resolve.target_scope_fields)
            reference_fields = (
                *resolve.target_fields,
                *resolve.target_scope_fields,
            )
            reference_keys = [
                (*reference.key, *reference.scope) for reference in references
            ]
            reference_domains = _key_domain_chunks(
                reference_fields,
                reference_keys,
                maximum_keys_per_request,
            )
            hybrid_domains = _casefold_key_domain_chunks(
                reference_fields,
                (
                    (*reference.key, *reference.scope)
                    for reference in references
                    if reference.origin == "target_then_incoming"
                ),
                maximum_keys_per_request,
            )
            reference_domains.extend(hybrid_domains)
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
        reference_requirements=plan_reference_read_requirements(plan),
        source_record_count=len(prepared_records),
        dependency_edges=plan.dependency_edges,
    )


def plan_reference_read_requirements(
    plan: CompiledMigrationPlan,
) -> tuple[ReferenceReadRequirement, ...]:
    """Retain each parent relation behind a target-only resolver read."""

    requirements: set[ReferenceReadRequirement] = set()
    for dataset in plan.datasets:
        for component in (
            *dataset.target_identity.components,
            *dataset.target_identity.scope,
        ):
            resolve = component.resolve
            if resolve is None or resolve.target_model is None:
                continue
            requirements.add(
                ReferenceReadRequirement(
                    parent_model=dataset.target.model,
                    relationship_field=component.target_fields[0],
                    relationship_type="many2one",
                    relation_model=resolve.target_model,
                    key_fields=resolve.target_fields,
                    scope_fields=resolve.target_scope_fields,
                    requested_fields=tuple(
                        sorted(
                            {
                                *resolve.target_fields,
                                *resolve.target_scope_fields,
                            }
                        )
                    ),
                )
            )
        for relationship_field, relation in dataset.relations.items():
            resolve = relation.resolve
            if resolve.target_model is None:
                continue
            requirements.add(
                ReferenceReadRequirement(
                    parent_model=dataset.target.model,
                    relationship_field=relationship_field,
                    relationship_type=relation.kind,
                    relation_model=resolve.target_model,
                    key_fields=resolve.target_fields,
                    scope_fields=resolve.target_scope_fields,
                    requested_fields=tuple(
                        sorted(
                            {
                                *resolve.target_fields,
                                *resolve.target_scope_fields,
                            }
                        )
                    ),
                )
            )
    return tuple(
        sorted(
            requirements,
            key=lambda item: (
                item.parent_model,
                item.relationship_field,
                item.relation_model,
                item.key_fields,
                item.scope_fields,
            ),
        )
    )


def _dataset_identity_domain_chunks(
    plan: CompiledMigrationPlan,
    dataset: DatasetSpec,
    records: Iterable[PreparedRecord],
    incoming_identity_index: dict[
        tuple[str, bytes], PreparedRecord | None
    ],
    target_key_cache: dict[tuple[str, int], tuple[Any, ...] | None],
    chunk_size: int,
) -> list[list[Any]] | None:
    fields = _dataset_identity_fields(plan, dataset)
    if not fields:
        return None
    keys = []
    for record in records:
        key = _record_target_key(
            plan,
            dataset,
            record,
            incoming_identity_index,
            target_key_cache,
        )
        if key is not None:
            keys.append(key)
    return _key_domain_chunks(fields, keys, chunk_size)


def _dataset_identity_fields(
    plan: CompiledMigrationPlan,
    dataset: DatasetSpec,
    *,
    visiting: frozenset[str] = frozenset(),
) -> tuple[str, ...]:
    """Return safe Odoo domain paths for one governed business identity."""

    if dataset.name in visiting:
        return ()
    visiting = visiting | {dataset.name}
    result: list[str] = []
    for component in (
        *dataset.target_identity.components,
        *dataset.target_identity.scope,
    ):
        if component.resolve is None:
            result.extend(component.target_fields)
            continue
        resolve = component.resolve
        if len(component.target_fields) != 1:
            return ()
        relation_field = component.target_fields[0]
        if resolve.target_model is not None:
            nested_fields = (
                *resolve.target_fields,
                *resolve.target_scope_fields,
            )
        else:
            try:
                referenced_dataset = plan.dataset(str(resolve.dataset))
            except KeyError:
                return ()
            nested_fields = _dataset_identity_fields(
                plan,
                referenced_dataset,
                visiting=visiting,
            )
        if not nested_fields:
            return ()
        result.extend(f"{relation_field}.{field}" for field in nested_fields)
    return tuple(result)


def _record_target_key(
    plan: CompiledMigrationPlan,
    dataset: DatasetSpec,
    record: PreparedRecord,
    incoming_identity_index: dict[
        tuple[str, bytes], PreparedRecord | None
    ],
    target_key_cache: dict[tuple[str, int], tuple[Any, ...] | None],
    *,
    visiting: frozenset[str] = frozenset(),
) -> tuple[Any, ...] | None:
    cache_key = (dataset.name, id(record))
    if cache_key in target_key_cache:
        return target_key_cache[cache_key]
    if dataset.name in visiting:
        return None
    visiting = visiting | {dataset.name}
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
            if not isinstance(reference, LogicalReference):
                return None
            if resolve.target_model is not None:
                if (
                    reference.origin != "target"
                    or len(reference.key) != len(resolve.target_fields)
                    or len(reference.scope) != len(resolve.target_scope_fields)
                ):
                    return None
                result.extend(reference.key)
                result.extend(reference.scope)
                continue
            if reference.origin != "incoming" or resolve.dataset is None:
                return None
            try:
                referenced_dataset = plan.dataset(resolve.dataset)
            except KeyError:
                return None
            match = incoming_identity_index.get(
                (
                    resolve.dataset,
                    canonical_json_bytes(portable_value(reference.key)),
                )
            )
            if match is None:
                return None
            referenced_key = _record_target_key(
                plan,
                referenced_dataset,
                match,
                incoming_identity_index,
                target_key_cache,
                visiting=visiting,
            )
            if referenced_key is None:
                return None
            result.extend(referenced_key)
        if cursor != len(values):
            return None
    resolved = tuple(result)
    target_key_cache[cache_key] = resolved
    return resolved


def _incoming_identity_index(
    records: Iterable[PreparedRecord],
) -> dict[tuple[str, bytes], PreparedRecord | None]:
    """Index unique incoming business keys once; duplicates remain ambiguous."""

    result: dict[tuple[str, bytes], PreparedRecord | None] = {}
    for record in records:
        key = (
            record.dataset,
            canonical_json_bytes(portable_value(record.source_identity)),
        )
        if key in result:
            result[key] = None
        else:
            result[key] = record
    return result


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


def _casefold_key_domain_chunks(
    fields: tuple[str, ...],
    keys: Iterable[tuple[Any, ...]],
    chunk_size: int,
) -> list[list[Any]]:
    """Read exact case-insensitive candidates for explicit hybrid review.

    These domains do not define equality. They only ensure that a case-only
    Odoo candidate is present in the bounded snapshot so the engine can stop
    instead of silently creating a near duplicate.
    """

    unique_keys = sorted(
        {
            tuple(key)
            for key in keys
            if len(key) == len(fields)
            and all(value is not None for value in key)
            and any(isinstance(value, str) for value in key)
        },
        key=lambda item: canonical_json_bytes(portable_value(item)),
    )
    if not fields or not unique_keys:
        return []
    chunks: list[list[Any]] = []
    for start in range(0, len(unique_keys), chunk_size):
        batch = unique_keys[start : start + chunk_size]
        expressions = [
            _and_terms(
                [
                    [field, "=ilike" if isinstance(value, str) else "=", value]
                    for field, value in zip(fields, key, strict=True)
                ]
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
