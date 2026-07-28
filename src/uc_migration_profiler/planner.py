"""Profile-derived minimal metadata and record request planning."""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterable

from .connectors import MetadataRequest, RecordRequest
from .models import LogicalReference, PreparedRecord
from .profile import DatasetSpec, ProfileDocument, ResolveSpec


def plan_metadata_requests(profile: ProfileDocument) -> tuple[MetadataRequest, ...]:
    fields: dict[str, set[str]] = defaultdict(set)
    for dataset in profile.datasets:
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
    profile: ProfileDocument,
    records: Iterable[PreparedRecord],
) -> tuple[RecordRequest, ...]:
    records_by_dataset: dict[str, list[PreparedRecord]] = defaultdict(list)
    for record in records:
        records_by_dataset[record.dataset].append(record)

    fields: dict[str, set[str]] = defaultdict(set)
    domains: dict[str, list[Any]] = {}
    for dataset in profile.datasets:
        fields[dataset.target.model].update(_dataset_target_fields(dataset))
        dataset_records = records_by_dataset.get(dataset.name, ())
        source_domain = _identity_domain(dataset, dataset_records)
        domains[dataset.target.model] = _combine_domains(
            list(dataset.target_domain),
            source_domain,
        )

        for component, references in _identity_reference_groups(
            dataset, dataset_records
        ):
            resolve = component.resolve
            if resolve is None or resolve.target_model is None:
                continue
            fields[resolve.target_model].update(resolve.target_fields)
            fields[resolve.target_model].update(resolve.target_scope_fields)
            reference_domain = _key_domain(
                resolve.target_fields,
                [reference.key for reference in references],
            )
            domains[resolve.target_model] = _or_domains(
                domains.get(resolve.target_model, []),
                reference_domain,
            )

        for target_field, relation in dataset.relations.items():
            resolve = relation.resolve
            if resolve.target_model is None:
                continue
            fields[resolve.target_model].update(resolve.target_fields)
            fields[resolve.target_model].update(resolve.target_scope_fields)
            keys = [
                reference.key
                for record in dataset_records
                for reference in _references_for_field(
                    record.references.get(target_field)
                )
                if reference.origin == "target"
            ]
            relation_domain = _key_domain(resolve.target_fields, keys)
            domains[resolve.target_model] = _or_domains(
                domains.get(resolve.target_model, []),
                relation_domain,
            )

    return tuple(
        RecordRequest(
            model=model,
            fields=tuple(sorted(model_fields)),
            domain=tuple(domains.get(model, ())),
        )
        for model, model_fields in sorted(fields.items())
    )


def _dataset_target_fields(dataset: DatasetSpec) -> set[str]:
    fields = set(dataset.fields)
    fields.update(dataset.relations)
    for component in (
        *dataset.target_identity.components,
        *dataset.target_identity.scope,
    ):
        fields.update(component.target_fields)
    return fields


def _resolvers(dataset: DatasetSpec) -> list[ResolveSpec]:
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


def _identity_domain(
    dataset: DatasetSpec,
    records: Iterable[PreparedRecord],
) -> list[Any]:
    components = dataset.target_identity.components
    if len(components) != 1:
        return []
    component = components[0]
    if component.resolve is not None or len(component.target_fields) != 1:
        return []
    values = sorted(
        {
            record.target_identity[0]
            for record in records
            if record.target_identity and record.target_identity[0] is not None
        },
        key=str,
    )
    if not values:
        return []
    return [[component.target_fields[0], "in", values]]


def _key_domain(
    fields: tuple[str, ...],
    keys: Iterable[tuple[Any, ...]],
) -> list[Any]:
    unique_keys = sorted(set(keys), key=str)
    if len(fields) != 1 or not unique_keys:
        return []
    return [[fields[0], "in", [key[0] for key in unique_keys]]]


def _combine_domains(left: list[Any], right: list[Any]) -> list[Any]:
    if not left:
        return right
    if not right:
        return left
    # Odoo domains implicitly AND adjacent expressions.
    return [*left, *right]


def _or_domains(left: list[Any], right: list[Any]) -> list[Any]:
    if not left:
        return right
    if not right:
        return left
    return ["|", *_as_expression(left), *_as_expression(right)]


def _as_expression(domain: list[Any]) -> list[Any]:
    if len(domain) <= 1:
        return domain
    if isinstance(domain[0], str) and domain[0] in {"&", "|", "!"}:
        return domain
    return ["&"] * (len(domain) - 1) + domain


def _references_for_field(value: Any) -> tuple[LogicalReference, ...]:
    if isinstance(value, LogicalReference):
        return (value,)
    if isinstance(value, tuple):
        return tuple(item for item in value if isinstance(item, LogicalReference))
    return ()


def _identity_reference_groups(
    dataset: DatasetSpec,
    records: Iterable[PreparedRecord],
) -> list[tuple[Any, tuple[LogicalReference, ...]]]:
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
