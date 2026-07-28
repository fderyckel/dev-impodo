"""Tabular source loading and prepared-record construction."""

from __future__ import annotations

import csv
from dataclasses import dataclass, replace
from hashlib import sha256
from pathlib import Path
from typing import Any, Iterable

from .canonical import ValueParseError, parse_field, parse_value
from .models import (
    Issue,
    LogicalReference,
    PreparedRecord,
    ScalarValue,
    Severity,
)
from .profile import (
    DatasetSpec,
    IdentityComponent,
    NormalizationSpec,
    ProfileDocument,
    RelationSpec,
)


@dataclass(frozen=True, slots=True)
class SourceTable:
    dataset: str
    path: Path
    headers: tuple[str, ...]
    rows: tuple[dict[str, str | None], ...]
    content_hash: str


@dataclass(frozen=True, slots=True)
class PreparedBundle:
    records: tuple[PreparedRecord, ...]
    issues: tuple[Issue, ...]
    source_hashes: dict[str, str]

    def by_dataset(self) -> dict[str, tuple[PreparedRecord, ...]]:
        return {
            dataset: tuple(record for record in self.records if record.dataset == dataset)
            for dataset in dict.fromkeys(record.dataset for record in self.records)
        }


def load_source_tables(
    profile: ProfileDocument,
    input_directory: str | Path,
) -> tuple[SourceTable, ...]:
    root = Path(input_directory)
    tables: list[SourceTable] = []
    for dataset in profile.datasets:
        path = root / dataset.source.file
        data = path.read_bytes()
        with path.open(
            "r",
            encoding=dataset.source.encoding,
            newline="",
        ) as handle:
            reader = csv.DictReader(handle, delimiter=dataset.source.delimiter)
            headers = tuple(reader.fieldnames or ())
            rows = tuple(dict(row) for row in reader)
        tables.append(
            SourceTable(
                dataset=dataset.name,
                path=path,
                headers=headers,
                rows=rows,
                content_hash="sha256:" + sha256(data).hexdigest(),
            )
        )
    return tuple(tables)


def prepare_sources(
    profile: ProfileDocument,
    input_directory: str | Path,
) -> PreparedBundle:
    tables = load_source_tables(profile, input_directory)
    records: list[PreparedRecord] = []
    issues: list[Issue] = []

    for dataset in profile.datasets:
        table = next(item for item in tables if item.dataset == dataset.name)
        missing_headers = sorted(_required_headers(dataset) - set(table.headers))
        if missing_headers:
            issues.append(
                Issue(
                    code="SOURCE_FIELD_MISSING",
                    message=f"missing source headers: {', '.join(missing_headers)}",
                    dataset=dataset.name,
                )
            )
        for row_index, row in enumerate(table.rows, start=2):
            records.append(_prepare_row(dataset, row, row_index, missing_headers))

    records = _mark_duplicate_source_identities(records)
    issues.extend(
        issue
        for record in records
        for issue in record.issues
        if issue.code == "SOURCE_IDENTITY_DUPLICATE"
    )

    return PreparedBundle(
        records=tuple(records),
        issues=tuple(issues),
        source_hashes={
            str(table.path.name): table.content_hash
            for table in sorted(tables, key=lambda item: item.dataset)
        },
    )


def _required_headers(dataset: DatasetSpec) -> set[str]:
    headers = set(dataset.source_identity.fields)
    for component in (
        *dataset.target_identity.components,
        *dataset.target_identity.scope,
    ):
        headers.update(component.source_fields)
    headers.update(field.source for field in dataset.fields.values())
    for relation in dataset.relations.values():
        headers.update(relation.source_fields)
    return headers


def _prepare_row(
    dataset: DatasetSpec,
    row: dict[str, str | None],
    row_index: int,
    missing_headers: Iterable[str],
) -> PreparedRecord:
    row_issues: list[Issue] = []
    for header in missing_headers:
        row_issues.append(
            Issue(
                code="SOURCE_FIELD_MISSING",
                message=f"source field {header!r} is not present",
                dataset=dataset.name,
                row=row_index,
                field=header,
            )
        )

    source_identity: list[ScalarValue] = []
    identity_policy = NormalizationSpec(trim=True, empty_as_null=True)
    for field_name in dataset.source_identity.fields:
        try:
            value = parse_value(
                row.get(field_name),
                "string",
                identity_policy,
                required=True,
            )
        except ValueParseError as exc:
            value = None
            row_issues.append(
                Issue(
                    code="SOURCE_IDENTITY_INVALID",
                    message=str(exc),
                    dataset=dataset.name,
                    row=row_index,
                    field=field_name,
                )
            )
        source_identity.append(value)

    scalar_values: dict[str, ScalarValue] = {}
    for target_field, spec in dataset.fields.items():
        try:
            scalar_values[target_field] = parse_field(row.get(spec.source), spec)
        except ValueParseError as exc:
            scalar_values[target_field] = None
            row_issues.append(
                Issue(
                    code=(
                        "SOURCE_REQUIRED_VALUE_MISSING"
                        if "required value" in str(exc)
                        else "SOURCE_TYPE_INVALID"
                    ),
                    message=str(exc),
                    dataset=dataset.name,
                    row=row_index,
                    field=spec.source,
                )
            )

    target_identity = tuple(
        value
        for component in dataset.target_identity.components
        for value in _prepare_identity_component(
            component, row, dataset.name, row_index, row_issues
        )
    )
    target_scope = tuple(
        value
        for component in dataset.target_identity.scope
        for value in _prepare_identity_component(
            component, row, dataset.name, row_index, row_issues
        )
    )

    references: dict[str, Any] = {}
    for target_field, relation in dataset.relations.items():
        references[target_field] = _prepare_relation(
            relation, row, dataset.name, row_index, target_field, row_issues
        )

    return PreparedRecord(
        dataset=dataset.name,
        source_row=row_index,
        target_model=dataset.target.model,
        source_identity=tuple(source_identity),
        target_identity=target_identity,
        target_scope=target_scope,
        scalar_values=scalar_values,
        references=references,
        issues=tuple(row_issues),
    )


def _prepare_identity_component(
    component: IdentityComponent,
    row: dict[str, str | None],
    dataset_name: str,
    row_index: int,
    issues: list[Issue],
) -> tuple[Any, ...]:
    if component.resolve is not None:
        key = _parse_reference_key(
            component.source_fields,
            row,
            dataset_name,
            row_index,
            component.target_fields[0],
            issues,
            required=True,
        )
        return (
            LogicalReference(
                origin=component.resolve.origin,
                key=key,
                dataset=component.resolve.dataset,
                model=component.resolve.target_model,
                target_fields=component.resolve.target_fields,
            ),
        )

    values: list[ScalarValue] = []
    for source_field in component.source_fields:
        try:
            value = parse_value(
                row.get(source_field),
                component.type,
                component.normalize,
                required=True,
            )
        except ValueParseError as exc:
            value = None
            issues.append(
                Issue(
                    code="SOURCE_IDENTITY_INVALID",
                    message=str(exc),
                    dataset=dataset_name,
                    row=row_index,
                    field=source_field,
                )
            )
        values.append(value)
    return tuple(values)


def _prepare_relation(
    relation: RelationSpec,
    row: dict[str, str | None],
    dataset_name: str,
    row_index: int,
    target_field: str,
    issues: list[Issue],
) -> Any:
    if relation.kind == "many2one":
        key = _parse_reference_key(
            relation.source_fields,
            row,
            dataset_name,
            row_index,
            target_field,
            issues,
            required=relation.required,
        )
        if not key or all(value is None for value in key):
            return None
        return LogicalReference(
            origin=relation.resolve.origin,
            key=key,
            dataset=relation.resolve.dataset,
            model=relation.resolve.target_model,
            target_fields=relation.resolve.target_fields,
        )

    raw = row.get(relation.source_fields[0])
    if raw is None or raw.strip() == "":
        if relation.required:
            issues.append(
                Issue(
                    code="SOURCE_REQUIRED_VALUE_MISSING",
                    message="required many2many value is empty",
                    dataset=dataset_name,
                    row=row_index,
                    field=target_field,
                )
            )
        return ()
    keys = [item.strip() for item in raw.split(relation.separator)]
    if any(item == "" for item in keys):
        issues.append(
            Issue(
                code="SOURCE_TYPE_INVALID",
                message="many2many value contains an empty item",
                dataset=dataset_name,
                row=row_index,
                field=target_field,
            )
        )
        keys = [item for item in keys if item]
    if len(set(keys)) != len(keys):
        issues.append(
            Issue(
                code="SOURCE_REFERENCE_DUPLICATE",
                message="many2many value contains duplicate business keys",
                dataset=dataset_name,
                row=row_index,
                field=target_field,
            )
        )
    return tuple(
        LogicalReference(
            origin=relation.resolve.origin,
            key=(key,),
            dataset=relation.resolve.dataset,
            model=relation.resolve.target_model,
            target_fields=relation.resolve.target_fields,
        )
        for key in dict.fromkeys(keys)
    )


def _parse_reference_key(
    source_fields: Iterable[str],
    row: dict[str, str | None],
    dataset_name: str,
    row_index: int,
    target_field: str,
    issues: list[Issue],
    *,
    required: bool,
) -> tuple[ScalarValue, ...]:
    values: list[ScalarValue] = []
    policy = NormalizationSpec(trim=True, empty_as_null=True)
    for source_field in source_fields:
        try:
            value = parse_value(
                row.get(source_field),
                "string",
                policy,
                required=required,
            )
        except ValueParseError as exc:
            value = None
            issues.append(
                Issue(
                    code="SOURCE_REQUIRED_VALUE_MISSING",
                    message=str(exc),
                    dataset=dataset_name,
                    row=row_index,
                    field=target_field,
                )
            )
        values.append(value)
    return tuple(values)


def _mark_duplicate_source_identities(
    records: list[PreparedRecord],
) -> list[PreparedRecord]:
    indexes: dict[tuple[str, tuple[ScalarValue, ...]], list[int]] = {}
    for index, record in enumerate(records):
        indexes.setdefault((record.dataset, record.source_identity), []).append(index)

    result = list(records)
    for (dataset, identity), record_indexes in indexes.items():
        if len(record_indexes) < 2:
            continue
        for index in record_indexes:
            record = result[index]
            issue = Issue(
                code="SOURCE_IDENTITY_DUPLICATE",
                message=f"source identity {identity!r} occurs {len(record_indexes)} times",
                dataset=dataset,
                row=record.source_row,
                affected_count=len(record_indexes),
            )
            result[index] = replace(record, issues=(*record.issues, issue))
    return result

