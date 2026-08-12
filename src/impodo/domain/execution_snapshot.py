"""Immutable, portable hand-off from read-only preflight to execution.

The snapshot is generated automatically from the exact frozen preparation
input and its target comparison.  It is an internal reliability artifact,
not another user approval.  Every compared row is accounted for, while only
``CREATE`` and ``UPDATE`` rows carry field intentions for the practical writer.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from hashlib import sha256
import json
import re
from typing import Any, Mapping

from .compiler.contracts import CompiledMigrationPlan
from .preflight.frozen_input import FrozenPreflightInput
from ..models import (
    BusinessReference,
    Classification,
    Decision,
    LogicalReference,
    PreflightResult,
    PreparedRecord,
    assert_no_numeric_odoo_ids,
    canonical_json_bytes,
    portable_value,
    restore_portable_value,
)
from ..profile import DatasetSpec, IdentityComponent, ResolveSpec


EXECUTION_SNAPSHOT_VERSION = 2


@dataclass(frozen=True, slots=True)
class ExecutionDataset:
    """One dependency-ordered dataset and its existing-record behavior."""

    dataset: str
    target_model: str
    sequence: int
    dependencies: tuple[str, ...]
    existing_policy: str
    identity_fields: tuple[str, ...]
    scope_fields: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FieldIntent:
    """Explicit field effect consumed by the schema-bound Odoo writer."""

    field: str
    action: str
    value: Any = None
    kind: str = "scalar"
    relation_operation: str = ""
    related_model: str = ""
    related_identity_fields: tuple[str, ...] = ()
    related_scope_fields: tuple[str, ...] = ()
    defer_on_create: bool = False

    def portable_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "field": self.field,
            "action": self.action,
            "kind": self.kind,
        }
        if self.action == "SET_VALUE":
            payload["value"] = portable_value(self.value)
        if self.relation_operation:
            payload["relation_operation"] = self.relation_operation
            payload["related_model"] = self.related_model
            payload["related_identity_fields"] = list(
                self.related_identity_fields
            )
            payload["related_scope_fields"] = list(self.related_scope_fields)
            if self.defer_on_create:
                payload["defer_on_create"] = True
        return payload


@dataclass(frozen=True, slots=True)
class ExecutionRow:
    """Portable disposition and, when actionable, exact write intentions."""

    row_id: str
    dataset: str
    source_row: int
    source_trace_id: str
    source_identity: tuple[Any, ...]
    target_model: str
    business_identity: tuple[Any, ...]
    business_scope: tuple[Any, ...]
    disposition: str
    target_match_count: int
    proposed_external_id: str
    fields: tuple[FieldIntent, ...]
    row_hash: str = ""

    def portable_dict(self, *, include_hash: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "row_id": self.row_id,
            "dataset": self.dataset,
            "source_row": self.source_row,
            "source_trace_id": self.source_trace_id,
            "source_identity": portable_value(self.source_identity),
            "target_model": self.target_model,
            "business_identity": portable_value(self.business_identity),
            "business_scope": portable_value(self.business_scope),
            "disposition": self.disposition,
            "target_match_count": self.target_match_count,
            "proposed_external_id": self.proposed_external_id,
            "fields": [item.portable_dict() for item in self.fields],
        }
        if include_hash:
            payload["row_hash"] = self.row_hash
        return payload


@dataclass(frozen=True, slots=True)
class ExecutionSnapshot:
    """Exact source, target, row-accounting, and write-intent hand-off."""

    project_id: str
    preflight_run_id: str
    mapping_id: str
    mapping_version: int
    mapping_content_hash: str
    compiled_plan_hash: str
    staging_run_id: str
    staging_content_hash: str
    quality_run_id: str
    quality_content_hash: str
    normalization_run_id: str
    normalization_content_hash: str
    normalization_lifecycle_version: int
    eligible_dataset_hash: str
    frozen_input_hash: str
    preflight_result_hash: str
    metadata_snapshot_hash: str
    record_snapshot_hash: str
    target_hash: str
    target_database: str
    target_odoo_version: str
    target_snapshot_at: str
    target_module_versions: Mapping[str, str]
    datasets: tuple[ExecutionDataset, ...]
    counts: Mapping[str, int]
    rows: tuple[ExecutionRow, ...]
    root_hash: str
    contract_version: int = EXECUTION_SNAPSHOT_VERSION

    @property
    def write_count(self) -> int:
        return int(self.counts.get("CREATE", 0)) + int(
            self.counts.get("UPDATE", 0)
        )

    def portable_dict(self, *, include_hash: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "contract_version": self.contract_version,
            "project_id": self.project_id,
            "preflight_run_id": self.preflight_run_id,
            "mapping": {
                "id": self.mapping_id,
                "version": self.mapping_version,
                "content_hash": self.mapping_content_hash,
                "compiled_plan_hash": self.compiled_plan_hash,
            },
            "preparation": {
                "staging_run_id": self.staging_run_id,
                "staging_content_hash": self.staging_content_hash,
                "quality_run_id": self.quality_run_id,
                "quality_content_hash": self.quality_content_hash,
                "normalization_run_id": self.normalization_run_id,
                "normalization_content_hash": self.normalization_content_hash,
                "normalization_lifecycle_version": (
                    self.normalization_lifecycle_version
                ),
                "eligible_dataset_hash": self.eligible_dataset_hash,
                "frozen_input_hash": self.frozen_input_hash,
            },
            "preflight": {
                "result_hash": self.preflight_result_hash,
                "metadata_snapshot_hash": self.metadata_snapshot_hash,
                "record_snapshot_hash": self.record_snapshot_hash,
            },
            "target": {
                "target_hash": self.target_hash,
                "database": self.target_database,
                "odoo_version": self.target_odoo_version,
                "snapshot_at": self.target_snapshot_at,
                "module_versions": dict(
                    sorted(self.target_module_versions.items())
                ),
            },
            "datasets": [
                {
                    "dataset": item.dataset,
                    "target_model": item.target_model,
                    "sequence": item.sequence,
                    "dependencies": list(item.dependencies),
                    "existing_policy": item.existing_policy,
                    "identity_fields": list(item.identity_fields),
                    "scope_fields": list(item.scope_fields),
                }
                for item in self.datasets
            ],
            "counts": dict(sorted(self.counts.items())),
            "rows": [item.portable_dict() for item in self.rows],
            "root_hash": self.root_hash,
        }
        if include_hash:
            payload["semantic_hash"] = self.semantic_hash
        assert_no_numeric_odoo_ids(payload)
        return payload

    @property
    def semantic_hash(self) -> str:
        return "sha256:" + sha256(
            canonical_json_bytes(self.portable_dict(include_hash=False))
        ).hexdigest()

    def to_json(self) -> str:
        return canonical_json_bytes(self.portable_dict()).decode("utf-8")

    @classmethod
    def from_json(cls, value: str) -> "ExecutionSnapshot":
        payload = json.loads(value)
        if int(payload["contract_version"]) != EXECUTION_SNAPSHOT_VERSION:
            raise ValueError("Execution snapshot contract version is unsupported")
        mapping = dict(payload["mapping"])
        preparation = dict(payload["preparation"])
        preflight = dict(payload["preflight"])
        target = dict(payload["target"])
        rows = tuple(_restore_row(item) for item in payload.get("rows", ()))
        snapshot = cls(
            project_id=str(payload["project_id"]),
            preflight_run_id=str(payload["preflight_run_id"]),
            mapping_id=str(mapping["id"]),
            mapping_version=int(mapping["version"]),
            mapping_content_hash=str(mapping["content_hash"]),
            compiled_plan_hash=str(mapping["compiled_plan_hash"]),
            staging_run_id=str(preparation["staging_run_id"]),
            staging_content_hash=str(preparation["staging_content_hash"]),
            quality_run_id=str(preparation["quality_run_id"]),
            quality_content_hash=str(preparation["quality_content_hash"]),
            normalization_run_id=str(preparation["normalization_run_id"]),
            normalization_content_hash=str(
                preparation["normalization_content_hash"]
            ),
            normalization_lifecycle_version=int(
                preparation["normalization_lifecycle_version"]
            ),
            eligible_dataset_hash=str(preparation["eligible_dataset_hash"]),
            frozen_input_hash=str(preparation["frozen_input_hash"]),
            preflight_result_hash=str(preflight["result_hash"]),
            metadata_snapshot_hash=str(preflight["metadata_snapshot_hash"]),
            record_snapshot_hash=str(preflight["record_snapshot_hash"]),
            target_hash=str(target["target_hash"]),
            target_database=str(target["database"]),
            target_odoo_version=str(target["odoo_version"]),
            target_snapshot_at=str(target["snapshot_at"]),
            target_module_versions={
                str(key): str(item)
                for key, item in dict(target.get("module_versions", {})).items()
            },
            datasets=tuple(
                ExecutionDataset(
                    dataset=str(item["dataset"]),
                    target_model=str(item["target_model"]),
                    sequence=int(item["sequence"]),
                    dependencies=tuple(
                        str(value) for value in item["dependencies"]
                    ),
                    existing_policy=str(item["existing_policy"]),
                    identity_fields=tuple(
                        str(value) for value in item["identity_fields"]
                    ),
                    scope_fields=tuple(
                        str(value) for value in item.get("scope_fields", ())
                    ),
                )
                for item in payload.get("datasets", ())
            ),
            counts={
                str(key): int(item)
                for key, item in dict(payload.get("counts", {})).items()
            },
            rows=rows,
            root_hash=str(payload["root_hash"]),
        )
        _validate_rows(rows, snapshot.counts)
        expected_root = _root_hash(rows)
        if snapshot.root_hash != expected_root:
            raise ValueError("Execution snapshot row root hash is invalid")
        if str(payload.get("semantic_hash", "")) != snapshot.semantic_hash:
            raise ValueError("Execution snapshot semantic hash is invalid")
        return snapshot


def build_execution_snapshot(
    *,
    preflight_run_id: str,
    frozen: FrozenPreflightInput,
    result: PreflightResult,
) -> ExecutionSnapshot:
    """Adapt frozen rows and deterministic decisions without re-preparing data."""

    if (
        result.source_hashes != frozen.prepared.source_hashes
        or result.metadata_snapshot_hash is None
        or result.record_snapshot_hash is None
    ):
        raise ValueError("Preflight result does not match the frozen input")
    records = {
        (record.dataset, record.source_row): record
        for record in frozen.prepared.records
    }
    target_resolutions = _target_resolution_index(result)
    rows = []
    for decision in result.decisions:
        record = records.get((decision.dataset, decision.source_row))
        if record is None or (
            decision.source_trace_id
            and record.source_trace_id != decision.source_trace_id
        ):
            raise ValueError("Preflight decision row is missing from frozen input")
        dataset = frozen.plan.dataset(decision.dataset)
        execution_record = (
            _resolved_create_record(record, target_resolutions)
            if decision.classification is Classification.CREATE
            else record
        )
        rows.append(
            _execution_row(
                frozen.project_id,
                frozen.plan,
                dataset,
                execution_record,
                decision,
            )
        )
    row_tuple = tuple(rows)
    counts = result.counts
    if len(row_tuple) != sum(counts.values()):
        raise ValueError("Preflight decision accounting is incomplete")
    datasets = tuple(
        ExecutionDataset(
            dataset=dataset.name,
            target_model=dataset.target.model,
            sequence=sequence,
            dependencies=_dataset_dependencies(dataset),
            existing_policy=_existing_policy(dataset),
            identity_fields=_identity_fields(
                dataset.target_identity.components
            ),
            scope_fields=_identity_fields(dataset.target_identity.scope),
        )
        for sequence, dataset in enumerate(frozen.plan.datasets)
    )
    snapshot = ExecutionSnapshot(
        project_id=frozen.project_id,
        preflight_run_id=preflight_run_id,
        mapping_id=frozen.revision.mapping_id,
        mapping_version=frozen.revision.version,
        mapping_content_hash=frozen.revision.definition.content_hash,
        compiled_plan_hash=frozen.plan.semantic_hash,
        staging_run_id=frozen.staging.run_id,
        staging_content_hash=frozen.staging.content_hash,
        quality_run_id=frozen.quality.run_id,
        quality_content_hash=frozen.quality.content_hash,
        normalization_run_id=frozen.normalization.run_id,
        normalization_content_hash=frozen.normalization.content_hash,
        normalization_lifecycle_version=frozen.normalization.lifecycle_version,
        eligible_dataset_hash=frozen.normalization.eligible_dataset_hash,
        frozen_input_hash=frozen.content_hash,
        preflight_result_hash=result.semantic_hash,
        metadata_snapshot_hash=result.metadata_snapshot_hash,
        record_snapshot_hash=result.record_snapshot_hash,
        target_hash=result.fingerprint.target_hash,
        target_database=result.fingerprint.database,
        target_odoo_version=result.fingerprint.odoo_version,
        target_snapshot_at=result.fingerprint.snapshot_timestamp,
        target_module_versions=dict(
            sorted(result.fingerprint.module_versions.items())
        ),
        datasets=datasets,
        counts=counts,
        rows=row_tuple,
        root_hash=_root_hash(row_tuple),
    )
    _validate_rows(row_tuple, counts)
    snapshot.portable_dict()
    return snapshot


def _target_resolution_index(
    result: PreflightResult,
) -> dict[tuple[str, bytes], tuple[str, int]]:
    """Index reviewed target-reference outcomes without carrying Odoo IDs.

    Resolution evidence is grouped by field for reporting, while the logical
    reference itself already contains the complete lookup shape.  Identical
    references within one dataset must therefore have one consistent outcome.
    """

    outcomes: dict[tuple[str, bytes], tuple[str, int]] = {}
    for evidence in result.reference_resolutions:
        reference = evidence.reference
        if reference.origin != "target":
            continue
        key = (
            evidence.dataset,
            canonical_json_bytes(portable_value(reference)),
        )
        outcome = (evidence.status, evidence.match_count)
        previous = outcomes.setdefault(key, outcome)
        if previous != outcome:
            raise ValueError("Target relationship resolution evidence conflicts")
    return outcomes


def _resolved_create_record(
    record: PreparedRecord,
    target_resolutions: Mapping[tuple[str, bytes], tuple[str, int]],
) -> PreparedRecord:
    """Use reviewed target matches while preserving incoming dependencies.

    The preflight engine resolves both target and incoming logical references
    for comparison.  Execution must keep incoming references symbolic so it
    can use dependency ordering and External IDs, but a uniquely reviewed
    existing-Odoo reference becomes a portable ``BusinessReference``.
    """

    def resolved_value(value: Any) -> Any:
        if isinstance(value, tuple):
            return tuple(resolved_value(item) for item in value)
        if not isinstance(value, LogicalReference) or value.origin != "target":
            return value
        outcome = target_resolutions.get(
            (
                record.dataset,
                canonical_json_bytes(portable_value(value)),
            )
        )
        if outcome is None:
            raise ValueError("Target relationship resolution evidence is incomplete")
        status, match_count = outcome
        if status != "RESOLVED":
            return value
        if match_count != 1 or not value.model:
            raise ValueError("Resolved target relationship evidence is invalid")
        return BusinessReference(
            model=value.model,
            key=value.key,
            scope=value.scope,
        )

    return replace(
        record,
        target_identity=tuple(
            resolved_value(value) for value in record.target_identity
        ),
        target_scope=tuple(resolved_value(value) for value in record.target_scope),
        references={
            field: resolved_value(value)
            for field, value in record.references.items()
        },
    )


def _execution_row(
    project_id: str,
    plan: CompiledMigrationPlan,
    dataset: DatasetSpec,
    record: PreparedRecord,
    decision: Decision,
) -> ExecutionRow:
    operation = decision.classification
    fields: tuple[FieldIntent, ...] = ()
    if operation is Classification.CREATE:
        fields = _create_intents(plan, dataset, record)
    elif operation is Classification.UPDATE:
        fields = _update_intents(plan, dataset, decision)
    row_id = _portable_row_id(project_id, record)
    external_id = (
        _proposed_external_id(
            project_id,
            record.target_model,
            decision.business_identity,
            decision.business_scope,
        )
        if operation is Classification.CREATE
        else ""
    )
    row = ExecutionRow(
        row_id=row_id,
        dataset=record.dataset,
        source_row=record.source_row,
        source_trace_id=record.source_trace_id,
        source_identity=record.source_identity,
        target_model=record.target_model,
        business_identity=decision.business_identity,
        business_scope=decision.business_scope,
        disposition=operation.value,
        target_match_count=decision.target_match_count,
        proposed_external_id=external_id,
        fields=fields,
    )
    digest = "sha256:" + sha256(
        canonical_json_bytes(row.portable_dict(include_hash=False))
    ).hexdigest()
    return ExecutionRow(
        row_id=row.row_id,
        dataset=row.dataset,
        source_row=row.source_row,
        source_trace_id=row.source_trace_id,
        source_identity=row.source_identity,
        target_model=row.target_model,
        business_identity=row.business_identity,
        business_scope=row.business_scope,
        disposition=row.disposition,
        target_match_count=row.target_match_count,
        proposed_external_id=row.proposed_external_id,
        fields=row.fields,
        row_hash=digest,
    )


def _create_intents(
    plan: CompiledMigrationPlan,
    dataset: DatasetSpec,
    record: PreparedRecord,
) -> tuple[FieldIntent, ...]:
    intentions: dict[str, FieldIntent] = {}
    identity_fields = {
        field
        for component in (
            *dataset.target_identity.components,
            *dataset.target_identity.scope,
        )
        for field in component.target_fields
    }
    _add_identity_intents(
        intentions,
        plan,
        dataset.target_identity.components,
        record.target_identity,
    )
    _add_identity_intents(
        intentions,
        plan,
        dataset.target_identity.scope,
        record.target_scope,
    )
    for field, spec in dataset.fields.items():
        if spec.validate_only:
            continue
        intentions[field] = _intent(
            field,
            record.scalar_values.get(field),
            spec.null_policy,
        )
    for field, spec in dataset.relations.items():
        if spec.validate_only:
            continue
        intentions[field] = _intent(
            field,
            record.references.get(field),
            spec.null_policy,
            kind="relation",
            relation_operation=spec.operation,
            defer_on_create=(
                spec.resolve.dataset is not None
                and not spec.required_on_create
                and field not in identity_fields
            ),
            **_relation_shape(plan, spec.resolve),
        )
    return tuple(intentions[field] for field in sorted(intentions))


def _update_intents(
    plan: CompiledMigrationPlan,
    dataset: DatasetSpec,
    decision: Decision,
) -> tuple[FieldIntent, ...]:
    intentions = []
    for difference in decision.differences:
        relation = dataset.relations.get(difference.field)
        intentions.append(
            _intent(
                difference.field,
                difference.proposed,
                (
                    relation.null_policy
                    if relation is not None
                    else dataset.fields[difference.field].null_policy
                ),
                kind="relation" if relation is not None else "scalar",
                relation_operation=(
                    # Engine differences contain the final canonical relation
                    # value after add/remove semantics.  Execution therefore
                    # replaces with that final value instead of applying the
                    # source operation a second time.
                    "replace" if relation is not None else ""
                ),
                **(
                    _relation_shape(plan, relation.resolve)
                    if relation is not None
                    else {}
                ),
            )
        )
    return tuple(sorted(intentions, key=lambda item: item.field))


def _add_identity_intents(
    intentions: dict[str, FieldIntent],
    plan: CompiledMigrationPlan,
    components: tuple[IdentityComponent, ...],
    values: tuple[Any, ...],
) -> None:
    target_fields = tuple(
        field for component in components for field in component.target_fields
    )
    if len(target_fields) != len(values):
        raise ValueError("Prepared identity does not match compiled target fields")
    component_by_field = {
        field: component
        for component in components
        for field in component.target_fields
    }
    for field, value in zip(target_fields, values, strict=True):
        component = component_by_field[field]
        intentions[field] = _intent(
            field,
            value,
            "distinct",
            kind="relation" if component.resolve is not None else "scalar",
            relation_operation="replace" if component.resolve is not None else "",
            **(
                _relation_shape(plan, component.resolve)
                if component.resolve is not None
                else {}
            ),
        )


def _intent(
    field: str,
    value: Any,
    null_policy: str,
    *,
    kind: str = "scalar",
    relation_operation: str = "",
    related_model: str = "",
    related_identity_fields: tuple[str, ...] = (),
    related_scope_fields: tuple[str, ...] = (),
    defer_on_create: bool = False,
) -> FieldIntent:
    empty_relation = kind == "relation" and value == ()
    if (value is None or empty_relation) and null_policy == "ignore_source_null":
        action = "OMIT"
    elif value is None:
        action = "SET_NULL"
    else:
        action = "SET_VALUE"
    return FieldIntent(
        field=field,
        action=action,
        value=value,
        kind=kind,
        relation_operation=relation_operation,
        related_model=related_model,
        related_identity_fields=related_identity_fields,
        related_scope_fields=related_scope_fields,
        defer_on_create=defer_on_create and action == "SET_VALUE",
    )


def _portable_row_id(project_id: str, record: PreparedRecord) -> str:
    return "sha256:" + sha256(
        canonical_json_bytes(
            {
                "project_id": project_id,
                "dataset": record.dataset,
                "source_trace_id": record.source_trace_id,
                "target_model": record.target_model,
            }
        )
    ).hexdigest()


def _proposed_external_id(
    project_id: str,
    target_model: str,
    identity: tuple[Any, ...],
    scope: tuple[Any, ...],
) -> str:
    namespace = sha256(project_id.encode("utf-8")).hexdigest()[:12]
    identity_hash = sha256(
        canonical_json_bytes(
            {
                "target_model": target_model,
                "business_identity": portable_value(identity),
                "business_scope": portable_value(scope),
            }
        )
    ).hexdigest()[:24]
    model_token = re.sub(r"[^a-z0-9_]+", "_", target_model.casefold())
    return f"impodo_{namespace}.{model_token}_{identity_hash}"


def _dataset_dependencies(dataset: DatasetSpec) -> tuple[str, ...]:
    dependencies = {
        spec.resolve.dataset
        for spec in dataset.relations.values()
        if spec.resolve.dataset is not None
    }
    dependencies.update(
        component.resolve.dataset
        for component in (
            *dataset.target_identity.components,
            *dataset.target_identity.scope,
        )
        if component.resolve is not None and component.resolve.dataset is not None
    )
    return tuple(sorted(str(item) for item in dependencies))


def _identity_fields(
    components: tuple[IdentityComponent, ...],
) -> tuple[str, ...]:
    return tuple(
        field for component in components for field in component.target_fields
    )


def _relation_shape(
    plan: CompiledMigrationPlan,
    resolve: ResolveSpec,
) -> dict[str, Any]:
    if resolve.target_model is not None:
        return {
            "related_model": resolve.target_model,
            "related_identity_fields": tuple(resolve.target_fields),
            "related_scope_fields": tuple(resolve.target_scope_fields),
        }
    if resolve.dataset is None:
        raise ValueError("Execution relationship target is incomplete")
    related = plan.dataset(resolve.dataset)
    return {
        "related_model": related.target.model,
        "related_identity_fields": _identity_fields(
            related.target_identity.components
        ),
        "related_scope_fields": _identity_fields(
            related.target_identity.scope
        ),
    }


def _existing_policy(dataset: DatasetSpec) -> str:
    if dataset.target.mode == "upsert":
        return "update"
    if dataset.target.mode == "create":
        return "skip" if dataset.target.on_existing == "unchanged" else "error"
    return "reference"


def _root_hash(rows: tuple[ExecutionRow, ...]) -> str:
    return "sha256:" + sha256(
        canonical_json_bytes([row.row_hash for row in rows])
    ).hexdigest()


def _validate_rows(
    rows: tuple[ExecutionRow, ...],
    counts: Mapping[str, int],
) -> None:
    """Reject incomplete accounting or writer-ambiguous row contracts."""

    expected_keys = {item.value for item in Classification}
    if set(counts) != expected_keys or any(
        int(value) < 0 for value in counts.values()
    ):
        raise ValueError("Execution snapshot row accounting is invalid")
    actual_counts = {
        disposition: sum(row.disposition == disposition for row in rows)
        for disposition in expected_keys
    }
    if dict(counts) != actual_counts:
        raise ValueError("Execution snapshot row accounting is invalid")
    if len({row.row_id for row in rows}) != len(rows):
        raise ValueError("Execution snapshot row identity is duplicated")
    create_external_ids = [
        row.proposed_external_id
        for row in rows
        if row.disposition == Classification.CREATE.value
    ]
    if (
        any(not value for value in create_external_ids)
        or len(set(create_external_ids)) != len(create_external_ids)
    ):
        raise ValueError("Execution snapshot External ID is invalid")
    for row in rows:
        actionable = row.disposition in {
            Classification.CREATE.value,
            Classification.UPDATE.value,
        }
        if actionable != bool(row.fields):
            raise ValueError("Execution snapshot write intentions are incomplete")
        if row.disposition != Classification.CREATE.value and row.proposed_external_id:
            raise ValueError("Execution snapshot External ID is invalid")
        if (
            row.disposition == Classification.CREATE.value
            and row.target_match_count != 0
        ):
            raise ValueError("Execution snapshot target match count is invalid")
        if row.disposition in {
            Classification.UPDATE.value,
            Classification.UNCHANGED.value,
        } and row.target_match_count != 1:
            raise ValueError("Execution snapshot target match count is invalid")
        if (
            row.disposition == Classification.AMBIGUOUS.value
            and row.target_match_count < 2
        ):
            raise ValueError("Execution snapshot target match count is invalid")
        if len({item.field for item in row.fields}) != len(row.fields):
            raise ValueError("Execution snapshot field intention is duplicated")
        for intent in row.fields:
            if intent.action not in {"OMIT", "SET_NULL", "SET_VALUE"}:
                raise ValueError("Execution snapshot field action is invalid")
            if intent.kind not in {"scalar", "relation"}:
                raise ValueError("Execution snapshot field kind is invalid")
            if intent.action == "SET_VALUE" and intent.value is None:
                raise ValueError("Execution snapshot field value is invalid")
            if intent.action != "SET_VALUE" and intent.value is not None:
                raise ValueError("Execution snapshot field value is invalid")
            if intent.kind == "scalar" and intent.relation_operation:
                raise ValueError("Execution snapshot relation operation is invalid")
            if intent.kind == "scalar" and (
                intent.related_model
                or intent.related_identity_fields
                or intent.related_scope_fields
                or intent.defer_on_create
            ):
                raise ValueError("Execution snapshot relation shape is invalid")
            if intent.kind == "relation" and intent.relation_operation not in {
                "replace",
                "add",
                "remove",
            }:
                raise ValueError("Execution snapshot relation operation is invalid")
            if intent.kind == "relation" and (
                not intent.related_model or not intent.related_identity_fields
            ):
                raise ValueError("Execution snapshot relation shape is invalid")
            if intent.defer_on_create and (
                row.disposition != Classification.CREATE.value
                or intent.action != "SET_VALUE"
            ):
                raise ValueError("Execution snapshot deferred relation is invalid")


def _restore_row(payload: Mapping[str, Any]) -> ExecutionRow:
    fields = tuple(
        FieldIntent(
            field=str(item["field"]),
            action=str(item["action"]),
            value=restore_portable_value(item.get("value")),
            kind=str(item.get("kind", "scalar")),
            relation_operation=str(item.get("relation_operation", "")),
            related_model=str(item.get("related_model", "")),
            related_identity_fields=tuple(
                str(value)
                for value in item.get("related_identity_fields", ())
            ),
            related_scope_fields=tuple(
                str(value) for value in item.get("related_scope_fields", ())
            ),
            defer_on_create=bool(item.get("defer_on_create", False)),
        )
        for item in payload.get("fields", ())
    )
    identity = restore_portable_value(payload.get("business_identity", ()))
    scope = restore_portable_value(payload.get("business_scope", ()))
    source_identity = restore_portable_value(payload.get("source_identity", ()))
    if (
        not isinstance(identity, tuple)
        or not isinstance(scope, tuple)
        or not isinstance(source_identity, tuple)
    ):
        raise ValueError("Execution snapshot business identity is invalid")
    row = ExecutionRow(
        row_id=str(payload["row_id"]),
        dataset=str(payload["dataset"]),
        source_row=int(payload["source_row"]),
        source_trace_id=str(payload["source_trace_id"]),
        source_identity=source_identity,
        target_model=str(payload["target_model"]),
        business_identity=identity,
        business_scope=scope,
        disposition=str(payload["disposition"]),
        target_match_count=int(payload["target_match_count"]),
        proposed_external_id=str(payload.get("proposed_external_id", "")),
        fields=fields,
        row_hash=str(payload["row_hash"]),
    )
    expected = "sha256:" + sha256(
        canonical_json_bytes(row.portable_dict(include_hash=False))
    ).hexdigest()
    if row.row_hash != expected:
        raise ValueError("Execution snapshot row hash is invalid")
    return row
