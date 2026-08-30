"""Immutable, portable hand-off from read-only preflight to execution.

The snapshot is generated automatically from the exact frozen preparation
input and its target comparison.  It is an internal reliability artifact,
not another user approval. Every compared row is accounted for, while only
``CREATE`` and ``UPDATE`` rows carry field intentions. The snapshot also owns
the deterministic row schedule and exact optional relationship completions.
"""

from __future__ import annotations

from dataclasses import dataclass, field as dataclass_field, replace
from hashlib import sha256
import json
import re
from typing import Any, Mapping

from .compiler.contracts import CompiledMigrationPlan
from .execution.dependency_scheduler import (
    DependencyEdge,
    DependencyNode,
    ScheduleBlocker,
    schedule_dependencies,
)
from .preflight.frozen_input import FrozenPreflightInput
from impodo.domain.shared.models import (
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
from impodo.domain.recipe.profile import DatasetSpec, IdentityComponent, ResolveSpec
from impodo.domain.relationship_dependencies import (
    DependencyStrength,
    dependency_sets_by_owner,
)


EXECUTION_SNAPSHOT_VERSION = 8
_SHA256 = re.compile(r"sha256:[0-9a-f]{64}")


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
    field_types: tuple[tuple[str, str], ...] = ()


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
    dependency_strength: str = ""
    dependency_row_ids: tuple[str, ...] = ()
    target_binding_hashes: tuple[str, ...] = ()
    incoming_projection_field: str = ""
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
            if self.dependency_strength:
                payload["dependency_strength"] = self.dependency_strength
            if self.dependency_row_ids:
                payload["dependency_row_ids"] = list(self.dependency_row_ids)
            if self.target_binding_hashes:
                payload["target_binding_hashes"] = list(
                    self.target_binding_hashes
                )
            if self.incoming_projection_field:
                payload["incoming_projection_field"] = (
                    self.incoming_projection_field
                )
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
    target_binding_hash: str = ""
    schedule_ordinal: int = -1
    schedule_component: int = -1
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
            "target_binding_hash": self.target_binding_hash,
            "proposed_external_id": self.proposed_external_id,
            "fields": [item.portable_dict() for item in self.fields],
            "schedule_ordinal": self.schedule_ordinal,
            "schedule_component": self.schedule_component,
        }
        if include_hash:
            payload["row_hash"] = self.row_hash
        return payload


@dataclass(frozen=True, slots=True)
class RelationshipCompletion:
    """One owner field omitted during create and written after receipts exist."""

    row_id: str
    field: str
    dependency_row_ids: tuple[str, ...]

    def portable_dict(self) -> dict[str, Any]:
        return {
            "row_id": self.row_id,
            "field": self.field,
            "dependency_row_ids": list(self.dependency_row_ids),
        }


@dataclass(frozen=True, slots=True)
class RelationshipBlocker:
    """One snapshot-time reason a row cannot be safely scheduled."""

    row_id: str
    code: str
    field: str = ""
    dependency_row_id: str = ""

    def portable_dict(self) -> dict[str, str]:
        return {
            "row_id": self.row_id,
            "code": self.code,
            "field": self.field,
            "dependency_row_id": self.dependency_row_id,
        }


@dataclass(frozen=True, slots=True)
class RelationshipComponent:
    """One dependency-independent topological layer safe to batch."""

    sequence: int
    row_ids: tuple[str, ...]

    def portable_dict(self) -> dict[str, Any]:
        return {"sequence": self.sequence, "row_ids": list(self.row_ids)}


@dataclass(frozen=True, slots=True)
class RelationshipPlan:
    """Compact row graph outcome embedded in one execution snapshot."""

    edge_count: int = 0
    components: tuple[RelationshipComponent, ...] = ()
    completions: tuple[RelationshipCompletion, ...] = ()
    blockers: tuple[RelationshipBlocker, ...] = ()
    root_hash: str = ""
    contract_version: int = 1

    @property
    def component_count(self) -> int:
        return len(self.components)

    @property
    def completion_count(self) -> int:
        return len(self.completions)

    @property
    def blocker_count(self) -> int:
        return len(self.blockers)

    def portable_dict(self, *, include_hash: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "contract_version": self.contract_version,
            "edge_count": self.edge_count,
            "component_count": self.component_count,
            "completion_count": self.completion_count,
            "blocker_count": self.blocker_count,
            "components": [item.portable_dict() for item in self.components],
            "completions": [item.portable_dict() for item in self.completions],
            "blockers": [item.portable_dict() for item in self.blockers],
        }
        if include_hash:
            payload["root_hash"] = self.root_hash
        return payload


@dataclass(frozen=True, slots=True)
class ExecutionSnapshot:
    """Exact source, target, row-accounting, and write-intent hand-off."""

    workspace_id: str
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
    relationship_plan: RelationshipPlan = dataclass_field(
        default_factory=RelationshipPlan
    )
    read_credential_binding_hash: str = ""
    read_principal_hash: str = ""
    read_permission_hash: str = ""
    read_context_hash: str = ""
    readable_models: tuple[str, ...] = ()
    contract_version: int = EXECUTION_SNAPSHOT_VERSION

    @property
    def write_count(self) -> int:
        return int(self.counts.get("CREATE", 0)) + int(
            self.counts.get("UPDATE", 0)
        )

    def portable_dict(self, *, include_hash: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "contract_version": self.contract_version,
            "workspace_id": self.workspace_id,
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
                "read_credential_binding_hash": (
                    self.read_credential_binding_hash
                ),
                "read_principal_hash": self.read_principal_hash,
                "read_permission_hash": self.read_permission_hash,
                "read_context_hash": self.read_context_hash,
                "readable_models": list(self.readable_models),
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
                    "field_types": dict(item.field_types),
                }
                for item in self.datasets
            ],
            "counts": dict(sorted(self.counts.items())),
            "rows": [item.portable_dict() for item in self.rows],
            "root_hash": self.root_hash,
            "relationship_plan": self.relationship_plan.portable_dict(),
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
        relationship_plan = _restore_relationship_plan(
            dict(payload.get("relationship_plan", {}))
        )
        snapshot = cls(
            workspace_id=str(payload["workspace_id"]),
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
                    field_types=tuple(
                        sorted(
                            (str(key), str(value))
                            for key, value in dict(
                                item.get("field_types", {})
                            ).items()
                        )
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
            relationship_plan=relationship_plan,
            read_credential_binding_hash=str(
                target["read_credential_binding_hash"]
            ),
            read_principal_hash=str(target["read_principal_hash"]),
            read_permission_hash=str(target["read_permission_hash"]),
            read_context_hash=str(target["read_context_hash"]),
            readable_models=tuple(
                str(item) for item in target["readable_models"]
            ),
        )
        _validate_read_evidence(snapshot)
        _validate_rows(
            rows,
            snapshot.counts,
            relationship_plan,
            snapshot.datasets,
        )
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
    target_bindings = _target_binding_index(frozen.plan, result)
    incoming_resolutions = _incoming_update_resolution_index(
        frozen.plan,
        tuple(frozen.prepared.records),
        result,
        target_resolutions,
    )
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
                frozen.workspace_id,
                frozen.plan,
                dataset,
                execution_record,
                decision,
                target_bindings,
                incoming_resolutions,
            )
        )
    provisional_rows = tuple(rows)
    counts = result.counts
    if len(provisional_rows) != sum(counts.values()):
        raise ValueError("Preflight decision accounting is incomplete")
    dependencies_by_dataset = dependency_sets_by_owner(
        frozen.plan.dependency_edges
    )
    schema = getattr(frozen, "captured_schema", None)
    unordered_datasets = tuple(
        ExecutionDataset(
            dataset=dataset.name,
            target_model=dataset.target.model,
            sequence=sequence,
            dependencies=dependencies_by_dataset.get(dataset.name, ()),
            existing_policy=_existing_policy(dataset),
            identity_fields=_identity_fields(
                dataset.target_identity.components
            ),
            scope_fields=_identity_fields(dataset.target_identity.scope),
            field_types=_execution_field_types(
                dataset.name,
                dataset.target.model,
                provisional_rows,
                schema,
            ),
        )
        for sequence, dataset in enumerate(frozen.plan.datasets)
    )
    datasets = tuple(
        replace(dataset, sequence=sequence)
        for sequence, dataset in enumerate(
            dependency_ordered_execution_datasets(unordered_datasets)
        )
    )
    row_tuple, relationship_plan = plan_execution_rows(
        provisional_rows,
        datasets,
    )
    snapshot = ExecutionSnapshot(
        workspace_id=frozen.workspace_id,
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
        relationship_plan=relationship_plan,
        read_credential_binding_hash=(
            schema.read_credential_binding_hash if schema is not None else ""
        ),
        read_principal_hash=(
            schema.read_principal_hash if schema is not None else ""
        ),
        read_permission_hash=(
            schema.read_permission_hash if schema is not None else ""
        ),
        read_context_hash=(
            schema.read_context_hash if schema is not None else ""
        ),
        readable_models=(
            tuple(sorted(model.name for model in schema.models))
            if schema is not None
            else ()
        ),
    )
    _validate_rows(row_tuple, counts, relationship_plan, datasets)
    _validate_read_evidence(snapshot)
    snapshot.portable_dict()
    return snapshot


def _execution_field_types(
    dataset_name: str,
    target_model: str,
    rows: tuple[ExecutionRow, ...],
    schema: object | None,
) -> tuple[tuple[str, str], ...]:
    """Carry only field types needed to interpret this dataset's read-back."""

    if schema is None:
        return ()
    model = next(
        (
            item
            for item in getattr(schema, "models", ())
            if getattr(item, "name", None) == target_model
        ),
        None,
    )
    if model is None:
        return ()
    used_fields = {
        intent.field
        for row in rows
        if row.dataset == dataset_name
        for intent in row.fields
    }
    available = {
        str(field.name): str(field.type)
        for field in getattr(model, "fields", ())
    }
    return tuple(
        (field, available[field])
        for field in sorted(used_fields)
        if field in available
    )


def _validate_read_evidence(snapshot: ExecutionSnapshot) -> None:
    """Reject malformed safe bindings without requiring local principal probes."""

    hashes = (
        snapshot.read_credential_binding_hash,
        snapshot.read_principal_hash,
        snapshot.read_permission_hash,
        snapshot.read_context_hash,
    )
    if any(value and not _SHA256.fullmatch(value) for value in hashes):
        raise ValueError("Execution snapshot read-identity evidence is invalid")
    if any(hashes[1:]) and not all(hashes):
        raise ValueError("Execution snapshot read-identity evidence is incomplete")
    if len(set(snapshot.readable_models)) != len(snapshot.readable_models):
        raise ValueError("Execution snapshot readable-model evidence is invalid")
    if any(hashes[1:]) and not snapshot.readable_models:
        raise ValueError("Execution snapshot readable-model evidence is incomplete")


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
        if reference.origin not in {"target", "target_then_incoming"}:
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


def _target_binding_index(
    plan: CompiledMigrationPlan,
    result: PreflightResult,
) -> dict[tuple[str, bytes, bytes], str]:
    """Index opaque bindings by portable model, business key, and scope."""

    bindings: dict[tuple[str, bytes, bytes], str] = {}

    def add(model: str, key: tuple[Any, ...], scope: tuple[Any, ...], value: str) -> None:
        if not value:
            return
        index_key = (
            model,
            canonical_json_bytes(portable_value(key)),
            canonical_json_bytes(portable_value(scope)),
        )
        previous = bindings.setdefault(index_key, value)
        if previous != value:
            raise ValueError("Target record binding evidence conflicts")

    for decision in result.decisions:
        add(
            plan.dataset(decision.dataset).target.model,
            decision.business_identity,
            decision.business_scope,
            decision.target_binding_hash,
        )
    for evidence in result.reference_resolutions:
        reference = evidence.reference
        if reference.model:
            add(
                reference.model,
                reference.key,
                reference.scope,
                evidence.target_binding_hash,
            )
    return bindings


def _incoming_update_resolution_index(
    plan: CompiledMigrationPlan,
    records: tuple[PreparedRecord, ...],
    result: PreflightResult,
    target_resolutions: Mapping[tuple[str, bytes], tuple[str, int]],
) -> dict[tuple[str, int, str, bytes], LogicalReference]:
    """Restore incoming provenance erased by comparison business references."""

    records_by_source: dict[tuple[str, bytes], PreparedRecord] = {}
    for record in records:
        key = (
            record.dataset,
            canonical_json_bytes(portable_value(record.source_identity)),
        )
        if key in records_by_source:
            raise ValueError("Incoming execution identity is duplicated")
        records_by_source[key] = record
    decisions = {
        (decision.dataset, decision.source_row): decision
        for decision in result.decisions
    }
    index: dict[tuple[str, int, str, bytes], LogicalReference] = {}
    for owner in records:
        for field, raw_value in owner.references.items():
            values = raw_value if isinstance(raw_value, tuple) else (raw_value,)
            for value in values:
                if not isinstance(value, LogicalReference):
                    continue
                source_key: tuple[Any, ...] | None = None
                if value.origin == "incoming":
                    source_key = value.key
                elif value.origin == "target_then_incoming":
                    outcome = target_resolutions.get(
                        (
                            owner.dataset,
                            canonical_json_bytes(portable_value(value)),
                        )
                    )
                    if outcome == ("RESOLVED_INCOMING", 1):
                        source_key = value.incoming_key
                if source_key is None or value.dataset is None:
                    continue
                referenced = records_by_source.get(
                    (
                        value.dataset,
                        canonical_json_bytes(portable_value(source_key)),
                    )
                )
                if referenced is None:
                    continue
                decision = decisions.get((referenced.dataset, referenced.source_row))
                if decision is None or decision.classification in {
                    Classification.BLOCKED,
                    Classification.AMBIGUOUS,
                }:
                    continue
                business_reference = BusinessReference(
                    model=plan.dataset(referenced.dataset).target.model,
                    key=decision.business_identity,
                    scope=decision.business_scope,
                )
                incoming = LogicalReference(
                    origin="incoming",
                    key=source_key,
                    dataset=value.dataset,
                )
                index_key = (
                    owner.dataset,
                    owner.source_row,
                    field,
                    canonical_json_bytes(portable_value(business_reference)),
                )
                previous = index.setdefault(index_key, incoming)
                if previous != incoming:
                    raise ValueError("Incoming update resolution evidence conflicts")
    return index


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
        if not isinstance(value, LogicalReference) or value.origin not in {
            "target",
            "target_then_incoming",
        }:
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
        if value.origin == "target_then_incoming" and status == "RESOLVED_INCOMING":
            if value.dataset is None or value.incoming_key is None:
                raise ValueError("Resolved incoming fallback evidence is invalid")
            return LogicalReference(
                origin="incoming",
                key=value.incoming_key,
                dataset=value.dataset,
            )
        expected_status = (
            "RESOLVED_TARGET"
            if value.origin == "target_then_incoming"
            else "RESOLVED"
        )
        if status != expected_status:
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
    workspace_id: str,
    plan: CompiledMigrationPlan,
    dataset: DatasetSpec,
    record: PreparedRecord,
    decision: Decision,
    target_bindings: Mapping[tuple[str, bytes, bytes], str],
    incoming_resolutions: Mapping[
        tuple[str, int, str, bytes], LogicalReference
    ],
) -> ExecutionRow:
    operation = decision.classification
    fields: tuple[FieldIntent, ...] = ()
    if operation is Classification.CREATE:
        fields = _create_intents(plan, dataset, record, target_bindings)
    elif operation is Classification.UPDATE:
        fields = _update_intents(
            plan,
            dataset,
            record,
            decision,
            target_bindings,
            incoming_resolutions,
        )
    row_id = _portable_row_id(workspace_id, record)
    external_id = (
        _proposed_external_id(
            workspace_id,
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
        target_binding_hash=decision.target_binding_hash,
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
        target_binding_hash=row.target_binding_hash,
        proposed_external_id=row.proposed_external_id,
        fields=row.fields,
        row_hash=digest,
    )


def _create_intents(
    plan: CompiledMigrationPlan,
    dataset: DatasetSpec,
    record: PreparedRecord,
    target_bindings: Mapping[tuple[str, bytes, bytes], str],
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
        target_bindings,
    )
    _add_identity_intents(
        intentions,
        plan,
        dataset.target_identity.scope,
        record.target_scope,
        target_bindings,
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
            dependency_strength=(
                (
                    DependencyStrength.HARD.value
                    if spec.required_on_create or field in identity_fields
                    else DependencyStrength.DEFERRABLE.value
                )
                if spec.resolve.dataset is not None
                else ""
            ),
            target_binding_hashes=_value_target_bindings(
                record.references.get(field), target_bindings
            ),
            **_relation_shape(plan, spec.resolve),
        )
    return tuple(intentions[field] for field in sorted(intentions))


def _update_intents(
    plan: CompiledMigrationPlan,
    dataset: DatasetSpec,
    record: PreparedRecord,
    decision: Decision,
    target_bindings: Mapping[tuple[str, bytes, bytes], str],
    incoming_resolutions: Mapping[
        tuple[str, int, str, bytes], LogicalReference
    ],
) -> tuple[FieldIntent, ...]:
    intentions = []
    for difference in decision.differences:
        relation = dataset.relations.get(difference.field)
        proposed = (
            _restore_update_incoming_references(
                difference.proposed,
                record,
                difference.field,
                incoming_resolutions,
            )
            if relation is not None
            else difference.proposed
        )
        intentions.append(
            _intent(
                difference.field,
                proposed,
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
                target_binding_hashes=(
                    _value_target_bindings(
                        proposed,
                        target_bindings,
                    )
                    if relation is not None
                    else ()
                ),
                **(
                    {
                        **_relation_shape(plan, relation.resolve),
                        "dependency_strength": (
                            DependencyStrength.HARD.value
                            if relation.required_on_create
                            else DependencyStrength.DEFERRABLE.value
                        )
                        if relation.resolve.dataset is not None
                        else "",
                    }
                    if relation is not None
                    else {}
                ),
            )
        )
    return tuple(sorted(intentions, key=lambda item: item.field))


def _restore_update_incoming_references(
    value: Any,
    owner: PreparedRecord,
    field: str,
    incoming_resolutions: Mapping[
        tuple[str, int, str, bytes], LogicalReference
    ],
) -> Any:
    """Replace only reviewed incoming targets with their frozen source keys."""

    if isinstance(value, tuple):
        return tuple(
            _restore_update_incoming_references(
                item,
                owner,
                field,
                incoming_resolutions,
            )
            for item in value
        )
    if not isinstance(value, BusinessReference):
        return value
    return incoming_resolutions.get(
        (
            owner.dataset,
            owner.source_row,
            field,
            canonical_json_bytes(portable_value(value)),
        ),
        value,
    )


def _add_identity_intents(
    intentions: dict[str, FieldIntent],
    plan: CompiledMigrationPlan,
    components: tuple[IdentityComponent, ...],
    values: tuple[Any, ...],
    target_bindings: Mapping[tuple[str, bytes, bytes], str],
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
            target_binding_hashes=(
                _value_target_bindings(value, target_bindings)
                if component.resolve is not None
                else ()
            ),
            **(
                {
                    **_relation_shape(plan, component.resolve),
                    "dependency_strength": (
                        DependencyStrength.HARD.value
                        if component.resolve.dataset is not None
                        else ""
                    ),
                }
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
    dependency_strength: str = "",
    target_binding_hashes: tuple[str, ...] = (),
    incoming_projection_field: str = "",
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
        dependency_strength=dependency_strength,
        target_binding_hashes=target_binding_hashes,
        incoming_projection_field=incoming_projection_field,
        defer_on_create=defer_on_create and action == "SET_VALUE",
    )


def _value_target_bindings(
    value: Any,
    target_bindings: Mapping[tuple[str, bytes, bytes], str],
) -> tuple[str, ...]:
    """Align opaque existing-target bindings with relation values."""

    values = value if isinstance(value, tuple) else (value,)
    result = []
    for item in values:
        if not isinstance(item, BusinessReference):
            result.append("")
            continue
        result.append(
            target_bindings.get(
                (
                    item.model,
                    canonical_json_bytes(portable_value(item.key)),
                    canonical_json_bytes(portable_value(item.scope)),
                ),
                "",
            )
        )
    return tuple(result) if any(result) else ()


def _portable_row_id(workspace_id: str, record: PreparedRecord) -> str:
    return "sha256:" + sha256(
        canonical_json_bytes(
            {
                "workspace_id": workspace_id,
                "dataset": record.dataset,
                "source_trace_id": record.source_trace_id,
                "target_model": record.target_model,
            }
        )
    ).hexdigest()


def _proposed_external_id(
    workspace_id: str,
    target_model: str,
    identity: tuple[Any, ...],
    scope: tuple[Any, ...],
) -> str:
    namespace = sha256(workspace_id.encode("utf-8")).hexdigest()[:12]
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


def dependency_ordered_execution_datasets(
    datasets: tuple[ExecutionDataset, ...],
) -> tuple[ExecutionDataset, ...]:
    """Place dependency components before their consumers deterministically.

    Optional incoming relationships may contain cycles because execution can
    finish those fields in a second write pass.  Strongly connected datasets
    therefore stay together in their reviewed order, while every acyclic
    dependency component moves before the datasets that consume it.
    """

    if not datasets:
        return ()
    by_name = {dataset.dataset: dataset for dataset in datasets}
    if len(by_name) != len(datasets):
        raise ValueError("execution snapshot contains duplicate datasets")
    rank = {dataset.dataset: index for index, dataset in enumerate(datasets)}
    dependencies: dict[str, tuple[str, ...]] = {}
    for dataset in datasets:
        dependencies[dataset.dataset] = tuple(
            sorted(
                set(dataset.dependencies).intersection(by_name),
                key=rank.__getitem__,
            )
        )

    next_index = 0
    indices: dict[str, int] = {}
    low_links: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    components: list[tuple[str, ...]] = []

    def collect_component(name: str) -> None:
        nonlocal next_index
        indices[name] = next_index
        low_links[name] = next_index
        next_index += 1
        stack.append(name)
        on_stack.add(name)
        for dependency in dependencies[name]:
            if dependency not in indices:
                collect_component(dependency)
                low_links[name] = min(low_links[name], low_links[dependency])
            elif dependency in on_stack:
                low_links[name] = min(low_links[name], indices[dependency])
        if low_links[name] != indices[name]:
            return
        members: list[str] = []
        while True:
            member = stack.pop()
            on_stack.remove(member)
            members.append(member)
            if member == name:
                break
        components.append(tuple(sorted(members, key=rank.__getitem__)))

    for dataset in datasets:
        if dataset.dataset not in indices:
            collect_component(dataset.dataset)

    component_by_name = {
        name: component_index
        for component_index, component in enumerate(components)
        for name in component
    }
    following = {index: set() for index in range(len(components))}
    indegree = {index: 0 for index in range(len(components))}
    for owner, owner_dependencies in dependencies.items():
        owner_component = component_by_name[owner]
        for dependency in owner_dependencies:
            dependency_component = component_by_name[dependency]
            if dependency_component == owner_component:
                continue
            if owner_component not in following[dependency_component]:
                following[dependency_component].add(owner_component)
                indegree[owner_component] += 1

    component_rank = {
        index: min(rank[name] for name in component)
        for index, component in enumerate(components)
    }
    ready = sorted(
        (index for index, count in indegree.items() if count == 0),
        key=component_rank.__getitem__,
    )
    ordered_names: list[str] = []
    while ready:
        component_index = ready.pop(0)
        ordered_names.extend(components[component_index])
        for follower in sorted(
            following[component_index], key=component_rank.__getitem__
        ):
            indegree[follower] -= 1
            if indegree[follower] == 0:
                ready.append(follower)
                ready.sort(key=component_rank.__getitem__)
    if len(ordered_names) != len(datasets):
        raise ValueError("execution snapshot dependency order is incomplete")
    return tuple(by_name[name] for name in ordered_names)


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
            "incoming_projection_field": (
                resolve.incoming_projection_field or ""
            ),
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


def plan_execution_rows(
    rows: tuple[ExecutionRow, ...],
    datasets: tuple[ExecutionDataset, ...],
) -> tuple[tuple[ExecutionRow, ...], RelationshipPlan]:
    """Resolve incoming row edges and freeze one deterministic write schedule."""

    dataset_sequence = {item.dataset: item.sequence for item in datasets}
    if len(dataset_sequence) != len(datasets):
        raise ValueError("execution snapshot contains duplicate datasets")
    missing_datasets = sorted(
        {row.dataset for row in rows}.difference(dataset_sequence)
    )
    if missing_datasets:
        raise ValueError("execution snapshot row dataset is missing")
    stable_rows = tuple(
        sorted(
            rows,
            key=lambda row: (
                dataset_sequence[row.dataset],
                canonical_json_bytes(portable_value(row.business_identity)),
                canonical_json_bytes(portable_value(row.business_scope)),
                canonical_json_bytes(portable_value(row.source_identity)),
                row.row_id,
            ),
        )
    )
    rank = {row.row_id: index for index, row in enumerate(stable_rows)}
    actionable = tuple(
        row
        for row in stable_rows
        if row.disposition in {
            Classification.CREATE.value,
            Classification.UPDATE.value,
        }
    )
    action_rank = {
        row.row_id: index for index, row in enumerate(actionable)
    }
    source_index: dict[tuple[str, bytes], list[ExecutionRow]] = {}
    for row in stable_rows:
        source_index.setdefault(
            (
                row.dataset,
                canonical_json_bytes(portable_value(row.source_identity)),
            ),
            [],
        ).append(row)

    graph_edges: list[DependencyEdge] = []
    blockers: list[ScheduleBlocker] = []
    edge_count = 0
    fields_by_row: dict[str, tuple[FieldIntent, ...]] = {}
    for row in actionable:
        planned_fields: list[FieldIntent] = []
        for intent in row.fields:
            dependency_ids: list[str] = []
            if intent.action == "SET_VALUE" and intent.kind == "relation":
                values = intent.value if isinstance(intent.value, tuple) else (intent.value,)
                incoming = tuple(
                    value
                    for value in values
                    if isinstance(value, LogicalReference)
                    and value.origin == "incoming"
                )
                for reference in incoming:
                    if reference.dataset is None:
                        blockers.append(
                            ScheduleBlocker(
                                row_id=row.row_id,
                                code="INCOMPLETE_INCOMING_REFERENCE",
                                field=intent.field,
                            )
                        )
                        continue
                    matches = source_index.get(
                        (
                            reference.dataset,
                            canonical_json_bytes(portable_value(reference.key)),
                        ),
                        (),
                    )
                    if len(matches) != 1:
                        blockers.append(
                            ScheduleBlocker(
                                row_id=row.row_id,
                                code=(
                                    "MISSING_INCOMING_ROW"
                                    if not matches
                                    else "DUPLICATE_INCOMING_ROW"
                                ),
                                field=intent.field,
                            )
                        )
                        continue
                    dependency = matches[0]
                    if (
                        dependency.target_model != intent.related_model
                        and not intent.incoming_projection_field
                    ):
                        blockers.append(
                            ScheduleBlocker(
                                row_id=row.row_id,
                                code="INCOMING_MODEL_MISMATCH",
                                field=intent.field,
                                dependency_row_id=dependency.row_id,
                            )
                        )
                        continue
                    if (
                        dependency.target_model == intent.related_model
                        and intent.incoming_projection_field
                    ):
                        blockers.append(
                            ScheduleBlocker(
                                row_id=row.row_id,
                                code="UNNEEDED_INCOMING_PROJECTION",
                                field=intent.field,
                                dependency_row_id=dependency.row_id,
                            )
                        )
                        continue
                    if dependency.row_id in dependency_ids:
                        continue
                    dependency_ids.append(dependency.row_id)
                    edge_count += 1
                    if dependency.disposition in {
                        Classification.BLOCKED.value,
                        Classification.AMBIGUOUS.value,
                    }:
                        blockers.append(
                            ScheduleBlocker(
                                row_id=row.row_id,
                                code="UNUSABLE_INCOMING_ROW",
                                field=intent.field,
                                dependency_row_id=dependency.row_id,
                            )
                        )
                    elif dependency.disposition == Classification.CREATE.value:
                        strength = intent.dependency_strength
                        if strength not in {
                            DependencyStrength.HARD.value,
                            DependencyStrength.DEFERRABLE.value,
                        }:
                            blockers.append(
                                ScheduleBlocker(
                                    row_id=row.row_id,
                                    code="MISSING_DEPENDENCY_STRENGTH",
                                    field=intent.field,
                                    dependency_row_id=dependency.row_id,
                                )
                            )
                        else:
                            graph_edges.append(
                                DependencyEdge(
                                    dependency_row_id=dependency.row_id,
                                    owner_row_id=row.row_id,
                                    owner_field=intent.field,
                                    strength=strength,
                                )
                            )
            planned_fields.append(
                replace(
                    intent,
                    dependency_row_ids=tuple(
                        sorted(set(dependency_ids), key=rank.__getitem__)
                    ),
                    defer_on_create=False,
                )
            )
        fields_by_row[row.row_id] = tuple(planned_fields)

    schedule = schedule_dependencies(
        (
            DependencyNode(row_id=row.row_id, rank=action_rank[row.row_id])
            for row in actionable
        ),
        graph_edges,
        blockers,
    )
    deferred_fields = {
        (edge.owner_row_id, edge.owner_field)
        for edge in schedule.deferred_edges
    }
    completions: list[RelationshipCompletion] = []
    for row in actionable:
        updated: list[FieldIntent] = []
        for intent in fields_by_row[row.row_id]:
            deferred = (row.row_id, intent.field) in deferred_fields
            updated_intent = replace(intent, defer_on_create=deferred)
            updated.append(updated_intent)
            if deferred:
                completions.append(
                    RelationshipCompletion(
                        row_id=row.row_id,
                        field=intent.field,
                        dependency_row_ids=intent.dependency_row_ids,
                    )
                )
        fields_by_row[row.row_id] = tuple(updated)

    ordinal = {
        row_id: index for index, row_id in enumerate(schedule.ordered_row_ids)
    }
    component = {
        row_id: index
        for index, row_ids in enumerate(schedule.components)
        for row_id in row_ids
    }
    planned_rows = tuple(
        _rehash_row(
            replace(
                row,
                fields=fields_by_row.get(row.row_id, row.fields),
                schedule_ordinal=ordinal.get(row.row_id, -1),
                schedule_component=component.get(row.row_id, -1),
                row_hash="",
            )
        )
        for row in stable_rows
    )
    planned_rows = tuple(
        sorted(
            planned_rows,
            key=lambda row: (
                row.schedule_ordinal < 0,
                row.schedule_ordinal if row.schedule_ordinal >= 0 else rank[row.row_id],
            ),
        )
    )
    plan = RelationshipPlan(
        edge_count=edge_count,
        components=tuple(
            RelationshipComponent(sequence=index, row_ids=row_ids)
            for index, row_ids in enumerate(schedule.components)
        ),
        completions=tuple(
            sorted(
                completions,
                key=lambda item: (rank[item.row_id], item.field),
            )
        ),
        blockers=tuple(
            RelationshipBlocker(
                row_id=item.row_id,
                code=item.code,
                field=item.field,
                dependency_row_id=item.dependency_row_id,
            )
            for item in schedule.blockers
        ),
    )
    plan = replace(plan, root_hash=_relationship_plan_hash(plan))
    return planned_rows, plan


def _rehash_row(row: ExecutionRow) -> ExecutionRow:
    return replace(
        row,
        row_hash="sha256:"
        + sha256(
            canonical_json_bytes(row.portable_dict(include_hash=False))
        ).hexdigest(),
    )


def _relationship_plan_hash(plan: RelationshipPlan) -> str:
    return "sha256:" + sha256(
        canonical_json_bytes(plan.portable_dict(include_hash=False))
    ).hexdigest()


def _root_hash(rows: tuple[ExecutionRow, ...]) -> str:
    return "sha256:" + sha256(
        canonical_json_bytes([row.row_hash for row in rows])
    ).hexdigest()


def _validate_rows(
    rows: tuple[ExecutionRow, ...],
    counts: Mapping[str, int],
    relationship_plan: RelationshipPlan,
    datasets: tuple[ExecutionDataset, ...],
) -> None:
    """Reject incomplete accounting or writer-ambiguous row contracts."""

    for dataset in datasets:
        if (
            dataset.field_types != tuple(sorted(dataset.field_types))
            or len({field for field, _field_type in dataset.field_types})
            != len(dataset.field_types)
            or any(
                not field or not field_type
                for field, field_type in dataset.field_types
            )
        ):
            raise ValueError("Execution snapshot field-type metadata is invalid")
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
                or intent.dependency_strength
                or intent.dependency_row_ids
                or intent.target_binding_hashes
                or intent.incoming_projection_field
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
            if intent.incoming_projection_field and (
                intent.kind != "relation"
                or not intent.dependency_strength
                or intent.action != "SET_VALUE"
            ):
                raise ValueError("Execution snapshot incoming projection is invalid")
            if intent.defer_on_create and (
                row.disposition != Classification.CREATE.value
                or intent.action != "SET_VALUE"
            ):
                raise ValueError("Execution snapshot deferred relation is invalid")
            if intent.dependency_strength not in {
                "",
                DependencyStrength.HARD.value,
                DependencyStrength.DEFERRABLE.value,
            }:
                raise ValueError("Execution snapshot dependency strength is invalid")
            if len(set(intent.dependency_row_ids)) != len(
                intent.dependency_row_ids
            ):
                raise ValueError("Execution snapshot dependency row is duplicated")
            if any(
                value and not _SHA256.fullmatch(value)
                for value in intent.target_binding_hashes
            ):
                raise ValueError("Execution snapshot target binding is invalid")
            if intent.target_binding_hashes:
                relation_values = (
                    intent.value
                    if isinstance(intent.value, tuple)
                    else (intent.value,)
                )
                if len(intent.target_binding_hashes) != len(relation_values):
                    raise ValueError("Execution snapshot target binding is invalid")
        if row.target_binding_hash and not _SHA256.fullmatch(
            row.target_binding_hash
        ):
            raise ValueError("Execution snapshot target binding is invalid")
        if row.disposition in {
            Classification.UPDATE.value,
            Classification.UNCHANGED.value,
        } and not row.target_binding_hash:
            raise ValueError("Execution snapshot target binding is incomplete")
    if relationship_plan.contract_version != 1 or relationship_plan.root_hash != (
        _relationship_plan_hash(relationship_plan)
    ):
        raise ValueError("Execution snapshot relationship plan hash is invalid")
    _validate_relationship_plan(rows, relationship_plan, datasets)


def _validate_relationship_plan(
    rows: tuple[ExecutionRow, ...],
    plan: RelationshipPlan,
    datasets: tuple[ExecutionDataset, ...],
) -> None:
    """Validate frozen schedule structure without recalculating its graph."""

    row_by_id = {row.row_id: row for row in rows}
    dataset_names = {item.dataset for item in datasets}
    if any(row.dataset not in dataset_names for row in rows):
        raise ValueError("Execution snapshot relationship schedule is invalid")
    if tuple(component.sequence for component in plan.components) != tuple(
        range(plan.component_count)
    ):
        raise ValueError("Execution snapshot relationship schedule is invalid")
    component_row_ids = tuple(
        row_id
        for component in plan.components
        for row_id in component.row_ids
    )
    if (
        len(set(component_row_ids)) != len(component_row_ids)
        or any(row_id not in row_by_id for row_id in component_row_ids)
    ):
        raise ValueError("Execution snapshot relationship schedule is invalid")
    scheduled = tuple(
        sorted(
            (row for row in rows if row.schedule_ordinal >= 0),
            key=lambda row: row.schedule_ordinal,
        )
    )
    blockers = {item.row_id: item for item in plan.blockers}
    if len(blockers) != plan.blocker_count or any(
        row_id not in row_by_id for row_id in blockers
    ):
        raise ValueError("Execution snapshot relationship blocker is invalid")
    actionable = {
        row.row_id
        for row in rows
        if row.disposition in {
            Classification.CREATE.value,
            Classification.UPDATE.value,
        }
    }
    if (
        not set(blockers).issubset(actionable)
        or any(
            item.dependency_row_id
            and item.dependency_row_id not in row_by_id
            for item in plan.blockers
        )
        or
        tuple(row.row_id for row in scheduled) != component_row_ids
        or tuple(row.schedule_ordinal for row in scheduled)
        != tuple(range(len(scheduled)))
        or set(component_row_ids) != actionable.difference(blockers)
    ):
        raise ValueError("Execution snapshot relationship schedule is invalid")
    component_by_row = {
        row_id: component.sequence
        for component in plan.components
        for row_id in component.row_ids
    }
    if any(
        row.schedule_component != component_by_row.get(row.row_id, -1)
        for row in rows
    ):
        raise ValueError("Execution snapshot relationship component is invalid")

    completion_by_field = {
        (item.row_id, item.field): item for item in plan.completions
    }
    if len(completion_by_field) != plan.completion_count:
        raise ValueError("Execution snapshot relationship completion is invalid")
    deferred_by_field = {
        (row.row_id, intent.field): intent
        for row in rows
        for intent in row.fields
        if intent.defer_on_create
    }
    if set(completion_by_field) != set(deferred_by_field):
        raise ValueError("Execution snapshot relationship completion is invalid")
    for key, completion in completion_by_field.items():
        intent = deferred_by_field[key]
        if (
            intent.dependency_strength != DependencyStrength.DEFERRABLE.value
            or completion.dependency_row_ids != intent.dependency_row_ids
        ):
            raise ValueError("Execution snapshot relationship completion is invalid")

    edge_count = 0
    for row in rows:
        for intent in row.fields:
            edge_count += len(intent.dependency_row_ids)
            if any(row_id not in row_by_id for row_id in intent.dependency_row_ids):
                raise ValueError("Execution snapshot dependency row is invalid")
            if row.row_id not in component_by_row or intent.defer_on_create:
                continue
            for dependency_row_id in intent.dependency_row_ids:
                dependency = row_by_id[dependency_row_id]
                if (
                    dependency.disposition == Classification.CREATE.value
                    and dependency.schedule_ordinal >= row.schedule_ordinal
                ):
                    raise ValueError(
                        "Execution snapshot relationship dependency order is invalid"
                    )
    if edge_count != plan.edge_count:
        raise ValueError("Execution snapshot relationship edge count is invalid")


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
            dependency_strength=str(item.get("dependency_strength", "")),
            dependency_row_ids=tuple(
                str(value) for value in item.get("dependency_row_ids", ())
            ),
            target_binding_hashes=tuple(
                str(value) for value in item.get("target_binding_hashes", ())
            ),
            incoming_projection_field=str(
                item.get("incoming_projection_field", "")
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
        target_binding_hash=str(payload.get("target_binding_hash", "")),
        proposed_external_id=str(payload.get("proposed_external_id", "")),
        fields=fields,
        schedule_ordinal=int(payload.get("schedule_ordinal", -1)),
        schedule_component=int(payload.get("schedule_component", -1)),
        row_hash=str(payload["row_hash"]),
    )
    expected = "sha256:" + sha256(
        canonical_json_bytes(row.portable_dict(include_hash=False))
    ).hexdigest()
    if row.row_hash != expected:
        raise ValueError("Execution snapshot row hash is invalid")
    return row


def _restore_relationship_plan(payload: Mapping[str, Any]) -> RelationshipPlan:
    plan = RelationshipPlan(
        edge_count=int(payload.get("edge_count", 0)),
        components=tuple(
            RelationshipComponent(
                sequence=int(item["sequence"]),
                row_ids=tuple(str(value) for value in item.get("row_ids", ())),
            )
            for item in payload.get("components", ())
        ),
        completions=tuple(
            RelationshipCompletion(
                row_id=str(item["row_id"]),
                field=str(item["field"]),
                dependency_row_ids=tuple(
                    str(value)
                    for value in item.get("dependency_row_ids", ())
                ),
            )
            for item in payload.get("completions", ())
        ),
        blockers=tuple(
            RelationshipBlocker(
                row_id=str(item["row_id"]),
                code=str(item["code"]),
                field=str(item.get("field", "")),
                dependency_row_id=str(item.get("dependency_row_id", "")),
            )
            for item in payload.get("blockers", ())
        ),
        root_hash=str(payload.get("root_hash", "")),
        contract_version=int(payload.get("contract_version", 0)),
    )
    if int(payload.get("component_count", -1)) != plan.component_count:
        raise ValueError("Execution snapshot relationship component count is invalid")
    if int(payload.get("completion_count", -1)) != plan.completion_count:
        raise ValueError("Execution snapshot relationship completion count is invalid")
    if int(payload.get("blocker_count", -1)) != plan.blocker_count:
        raise ValueError("Execution snapshot relationship blocker count is invalid")
    return plan
