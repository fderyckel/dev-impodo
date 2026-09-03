"""Portable source rows and deterministic preparation semantics.

The filesystem reader lives in ``adapters.artifacts.source_files``. This
module maps supplied rows to scalar values, identities, and unresolved logical
references without opening source artifacts or contacting Odoo.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from hashlib import sha256
from pathlib import PurePath
from typing import Any, Iterable

from impodo.domain.preparation.canonical import ValueParseError, parse_field, parse_value
from impodo.domain.compiler.contracts import CompiledMigrationPlan
from impodo.domain.shared.models import (
    InvalidPreparedValue,
    Issue,
    LogicalReference,
    PreparedRecord,
    ScalarValue,
    canonical_json_bytes,
    portable_value,
)
from impodo.domain.recipe.profile import (
    DatasetSpec,
    IdentityComponent,
    NormalizationSpec,
    RelationSpec,
    ResolveSpec,
)


@dataclass(frozen=True, slots=True)
class SourceTable:
    """One validated input table before compiled mappings are applied.

    ``rows`` preserves physical row numbers for actionable error reporting,
    while ``content_hash`` allows the final report to identify the exact input
    bytes used for the run.
    """

    dataset: str
    path: PurePath
    headers: tuple[str, ...]
    rows: tuple["SourceRow", ...]
    content_hash: str


@dataclass(frozen=True, slots=True)
class SourceRow:
    """A source row paired with its one-based CSV or worksheet row number."""

    number: int
    values: dict[str, Any]


@dataclass(frozen=True, slots=True)
class PreparedBundle:
    """All prepared records, global source issues, and source-file hashes."""

    records: tuple[PreparedRecord, ...]
    issues: tuple[Issue, ...]
    source_hashes: dict[str, str]

    def by_dataset(self) -> dict[str, tuple[PreparedRecord, ...]]:
        """Group records by dataset while preserving plan/record order.

        The engine consumes these groups when it resolves dependencies and
        classifies each dataset independently.
        """

        grouped: dict[str, list[PreparedRecord]] = {}
        for record in self.records:
            grouped.setdefault(record.dataset, []).append(record)
        return {
            dataset: tuple(records)
            for dataset, records in grouped.items()
        }


@dataclass(frozen=True, slots=True)
class CompiledPreparedRowTransformer:
    """Compile row-invariant source preparation for one effective dataset."""

    dataset: DatasetSpec
    missing_headers: tuple[str, ...]

    @classmethod
    def compile(
        cls,
        dataset: DatasetSpec,
        headers: Iterable[str],
    ) -> "CompiledPreparedRowTransformer":
        """Bind one dataset specification to the available staged headers."""

        return cls(
            dataset=dataset,
            missing_headers=tuple(
                sorted(_required_headers(dataset) - set(headers))
            ),
        )

    @property
    def dataset_issue(self) -> Issue | None:
        """Return the existing dataset-level missing-header issue, if any."""

        if not self.missing_headers:
            return None
        return Issue(
            code="SOURCE_FIELD_MISSING",
            message=f"missing source headers: {', '.join(self.missing_headers)}",
            dataset=self.dataset.name,
        )

    def transform(self, row: SourceRow) -> PreparedRecord:
        """Prepare one staged row without retaining it after the call."""

        return _prepare_row(
            self.dataset,
            row.values,
            row.number,
            self.missing_headers,
        )


class SourceLoadError(ValueError):
    """Raised when an input file violates the governed source contract."""


def prepare_source_tables(
    plan: CompiledMigrationPlan,
    tables: Iterable[SourceTable],
    *,
    source_hashes: dict[str, str],
) -> PreparedBundle:
    """Prepare already selected full-row tables for browser or CLI preflight."""

    selected_tables = tuple(tables)
    by_dataset = {table.dataset: table for table in selected_tables}
    if len(by_dataset) != len(selected_tables):
        raise SourceLoadError("Prepared source tables must be unique by dataset")
    if set(by_dataset) != {dataset.name for dataset in plan.datasets}:
        raise SourceLoadError("Prepared source tables do not match the compiled plan")
    records: list[PreparedRecord] = []
    issues: list[Issue] = []

    for dataset in plan.datasets:
        table = by_dataset[dataset.name]
        transformer = CompiledPreparedRowTransformer.compile(
            dataset,
            table.headers,
        )
        if transformer.dataset_issue is not None:
            issues.append(transformer.dataset_issue)
        for row in table.rows:
            records.append(transformer.transform(row))

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
        source_hashes=dict(sorted(source_hashes.items())),
    )


def _required_headers(dataset: DatasetSpec) -> set[str]:
    """Collect every source column used by a dataset's mapping contract."""

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
    row: dict[str, Any],
    row_index: int,
    missing_headers: Iterable[str],
) -> PreparedRecord:
    """Map one raw row to a ``PreparedRecord`` and attach row-local issues.

    This function coordinates scalar conversion, source/target identity
    construction, and relation preparation.  It records validation failures
    instead of aborting the complete dataset, allowing the final report to
    describe every affected row in one run.
    """

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
        raw_value = row.get(spec.source)
        if isinstance(raw_value, InvalidPreparedValue):
            scalar_values[target_field] = None
            row_issues.append(
                Issue(
                    code=raw_value.code,
                    message=raw_value.message,
                    dataset=dataset.name,
                    row=row_index,
                    field=spec.source,
                )
            )
            continue
        try:
            scalar_values[target_field] = parse_field(raw_value, spec)
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
        source_trace_id="sha256:"
        + sha256(
            canonical_json_bytes(
                {
                    "dataset": dataset.name,
                    "source_row": row_index,
                    "target_model": dataset.target.model,
                    "source_identity": portable_value(tuple(source_identity)),
                }
            )
        ).hexdigest(),
        issues=tuple(row_issues),
    )


def _prepare_identity_component(
    component: IdentityComponent,
    row: dict[str, Any],
    dataset_name: str,
    row_index: int,
    issues: list[Issue],
) -> tuple[Any, ...]:
    """Prepare one target-identity component as values or a logical reference.

    Direct components are parsed with their declared type and normalization.
    Resolved components are left symbolic so the engine can use either another
    incoming dataset or the Odoo target catalog.
    """

    if component.resolve is not None:
        incoming_key = _parse_reference_key(
            component.source_fields,
            row,
            dataset_name,
            row_index,
            component.target_fields[0],
            issues,
            required=True,
        )
        key, scope = _reference_parts(component.resolve, incoming_key)
        return (
            LogicalReference(
                origin=component.resolve.origin,
                key=key,
                dataset=component.resolve.dataset,
                model=component.resolve.target_model,
                target_fields=component.resolve.target_fields,
                target_scope_fields=component.resolve.target_scope_fields,
                scope=scope,
                incoming_key=(
                    incoming_key
                    if component.resolve.origin == "target_then_incoming"
                    else None
                ),
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
    row: dict[str, Any],
    dataset_name: str,
    row_index: int,
    target_field: str,
    issues: list[Issue],
) -> Any:
    """Build unresolved many2one or many2many references for one target field.

    Required values, empty list items, and duplicate many2many business keys
    become issues tied to the source row.  No numeric Odoo identifier crosses
    this source-preparation boundary.
    """

    if relation.value_source == "constant_existing":
        return LogicalReference(
            origin="target",
            key=relation.constant_key_values,
            model=relation.resolve.target_model,
            target_fields=relation.resolve.target_fields,
            target_scope_fields=relation.resolve.target_scope_fields,
            scope=relation.constant_scope_values,
        )

    if relation.kind == "many2one":
        incoming_key = _parse_reference_key(
            relation.source_fields,
            row,
            dataset_name,
            row_index,
            target_field,
            issues,
            required=relation.required,
        )
        if not incoming_key or all(value is None for value in incoming_key):
            return None
        key, scope = _reference_parts(relation.resolve, incoming_key)
        return LogicalReference(
            origin=relation.resolve.origin,
            key=key,
            dataset=relation.resolve.dataset,
            model=relation.resolve.target_model,
            target_fields=relation.resolve.target_fields,
            target_scope_fields=relation.resolve.target_scope_fields,
            scope=scope,
            incoming_key=(
                incoming_key
                if relation.resolve.origin == "target_then_incoming"
                else None
            ),
        )

    raw = row.get(relation.source_fields[0])
    if raw is None or str(raw).strip() == "":
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
    keys = [item.strip() for item in str(raw).split(relation.separator)]
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
            key=_target_reference_key(relation.resolve, (key,)),
            dataset=relation.resolve.dataset,
            model=relation.resolve.target_model,
            target_fields=relation.resolve.target_fields,
            target_scope_fields=relation.resolve.target_scope_fields,
            incoming_key=(
                (key,)
                if relation.resolve.origin == "target_then_incoming"
                else None
            ),
        )
        for key in dict.fromkeys(keys)
    )


def _target_reference_key(
    resolve: ResolveSpec,
    incoming_key: tuple[ScalarValue, ...],
) -> tuple[ScalarValue, ...]:
    """Apply exact reviewed aliases only to the hybrid Odoo lookup key."""

    if (
        resolve.origin != "target_then_incoming"
        or not resolve.target_value_mappings
        or len(incoming_key) != 1
        or incoming_key[0] is None
    ):
        return incoming_key
    matches = dict(resolve.target_value_mappings)
    return (matches.get(str(incoming_key[0]), incoming_key[0]),)


def _reference_parts(
    resolve: ResolveSpec,
    incoming_key: tuple[ScalarValue, ...],
) -> tuple[tuple[ScalarValue, ...], tuple[ScalarValue, ...]]:
    """Separate target key/scope while retaining the whole incoming identity."""

    if resolve.target_model is None:
        return incoming_key, ()
    key_width = len(resolve.target_fields)
    target_key = incoming_key[:key_width]
    scope = incoming_key[key_width:]
    return _target_reference_key(resolve, target_key), scope


def _parse_reference_key(
    source_fields: Iterable[str],
    row: dict[str, Any],
    dataset_name: str,
    row_index: int,
    target_field: str,
    issues: list[Issue],
    *,
    required: bool,
) -> tuple[ScalarValue, ...]:
    """Parse the source columns forming one symbolic reference key."""

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
    """Attach an issue to every row sharing an identity within one dataset.

    A single dictionary index performs the grouping in linear time, avoiding
    pairwise row comparisons for large source files.
    """

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
