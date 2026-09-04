"""Compile and publish the confirmed Odoo-to-Odoo Stage 8B load input."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from hashlib import sha256
import re
from typing import Protocol

from impodo.application.data_version.source_snapshots import (
    load_source_snapshot_table,
    validate_snapshot_for_dataset,
)
from impodo.application.odoo_provenance_service import OdooProvenanceService
from impodo.application.shared.artifacts import GovernedArtifactStores
from impodo.domain.execution.models import ExecutionRun
from impodo.domain.errors import ReadinessError
from impodo.domain.execution.odoo_write import OdooWriteExecutor
from impodo.domain.execution_snapshot import (
    ExecutionDataset,
    ExecutionRow,
    ExecutionSnapshot,
    FieldIntent,
    plan_execution_rows,
    resequence_execution_rows,
)
from impodo.domain.odoo.contracts import RecordSnapshot, record_snapshot_payload
from impodo.domain.odoo_provenance import OdooOriginBatch
from impodo.domain.preparation.source import SourceRow
from impodo.domain.serialization import content_hash
from impodo.domain.shared.access import Actor
from impodo.domain.shared.models import (
    BusinessReference,
    Classification,
    LogicalReference,
    OdooReadIdentity,
    OdooWriteIdentity,
    canonical_json_bytes,
    portable_value,
    target_record_binding_hash,
)
from impodo.domain.source_snapshot import SourceSnapshot
from impodo.domain.workspace.contracts import OdooSchemaCatalog, SourceSelection
from impodo.domain.workspace.destination_matching import DestinationMatchPlan
from impodo.domain.workspace.errors import WorkspaceError
from impodo.domain.workspace.transfer_preflight import TransferPreflightReport
from impodo.domain.workspace.transfer_review import TransferReviewPackage
from impodo.domain.workspace.workbench import SourceMode, WorkspaceState

from .preflight_service import EXECUTION_SNAPSHOT_NAME
from .workspace.execution.service import ExecutionService


_MODEL_TOKEN = re.compile(r"[^a-z0-9_]+")


class TransferExecutionSourceRepository(Protocol):
    """Structural source-reader contract used by the Stage 8B compiler."""

    def get_current_source_snapshots(
        self, workspace_id: str
    ) -> tuple[SourceSnapshot, ...]: ...


class TransferExecutionService:
    """Build one protected-value execution snapshot and invoke the shared loader."""

    def __init__(
        self,
        sources: TransferExecutionSourceRepository,
        artifacts: GovernedArtifactStores,
        provenance: OdooProvenanceService,
        execution: ExecutionService,
    ) -> None:
        self.sources = sources
        self.artifacts = artifacts
        self.provenance = provenance
        self.execution = execution

    def compile(
        self,
        workspace: WorkspaceState,
        selection: SourceSelection,
        schema: OdooSchemaCatalog,
        package: TransferReviewPackage,
        preflight: TransferPreflightReport,
        fresh_match: DestinationMatchPlan,
        destination_records: RecordSnapshot,
        *,
        actor: Actor,
    ) -> ExecutionSnapshot:
        """Compile exact frozen values after the final destination read."""

        if workspace.source_mode is not SourceMode.ODOO:
            raise WorkspaceError("Stage 8B requires a frozen Odoo source")
        if (
            workspace.transfer_review_package != package
            or workspace.transfer_preflight_report != preflight
            or not workspace.transfer_preflight_ready(
                source_selection_hash=selection.content_hash,
                source_schema_hash=schema.content_hash,
            )
            or package.workspace_id != workspace.workspace_id
            or preflight.workspace_id != workspace.workspace_id
            or not preflight.ready
            or preflight.review_package_hash != package.content_hash
            or preflight.fresh_match_plan_hash != fresh_match.content_hash
            or preflight.source_selection_hash != selection.content_hash
            or preflight.source_schema_hash != schema.content_hash
            or destination_records.fingerprint.target_hash
            != preflight.destination_target_hash
            or content_hash(record_snapshot_payload(destination_records))
            != preflight.destination_record_snapshot_hash
        ):
            raise WorkspaceError(
                "The final destination read no longer matches the approved transfer"
            )

        snapshots = self.sources.get_current_source_snapshots(workspace.workspace_id)
        snapshot_by_dataset = {item.dataset_id: item for item in snapshots}
        dataset_by_id = {item.dataset_id: item for item in selection.datasets}
        if (
            len(snapshot_by_dataset) != len(snapshots)
            or set(snapshot_by_dataset) != set(dataset_by_id)
        ):
            raise WorkspaceError("The frozen source snapshots are incomplete")

        source_rows: dict[str, tuple[SourceRow, ...]] = {}
        for dataset_id, dataset in dataset_by_id.items():
            snapshot = snapshot_by_dataset[dataset_id]
            validate_snapshot_for_dataset(selection, dataset, snapshot)
            with self.artifacts.materialize_source_snapshot(
                snapshot.data_version_id,
                snapshot.parquet_storage_key,
                expected_sha256=snapshot.parquet_sha256,
            ) as path:
                source_rows[dataset_id] = load_source_snapshot_table(
                    path, snapshot
                ).rows

        origins: dict[str, tuple[OdooOriginBatch, ...]] = {}
        for dataset_id in sorted(dataset_by_id):
            protected = self.provenance.read_current_origins(
                workspace.workspace_id,
                actor=actor,
                dataset_id=dataset_id,
            )
            if protected is None:
                raise WorkspaceError(
                    "Protected source relationship evidence is unavailable"
                )
            origins[dataset_id] = protected[1]
        manifests = self.provenance.current_manifests(
            workspace.workspace_id,
            actor=actor,
        )
        manifest_hashes = {
            item.dataset_id: item.content_hash for item in manifests
        }
        if set(manifest_hashes) != set(dataset_by_id):
            raise WorkspaceError("Protected source manifests are incomplete")

        return compile_transfer_execution_snapshot(
            workspace,
            selection,
            schema,
            package,
            preflight,
            fresh_match,
            destination_records,
            source_rows=source_rows,
            source_origins=origins,
            source_snapshots=snapshot_by_dataset,
            source_manifest_hashes=manifest_hashes,
        )

    def stage(
        self,
        workspace: WorkspaceState,
        selection: SourceSelection,
        schema: OdooSchemaCatalog,
        snapshot: ExecutionSnapshot,
    ) -> ExecutionSnapshot:
        """Publish the exact no-write preview used by final confirmation."""

        report = workspace.transfer_preflight_report
        package = workspace.transfer_review_package
        if (
            report is None
            or package is None
            or not workspace.transfer_preflight_ready(
                source_selection_hash=selection.content_hash,
                source_schema_hash=schema.content_hash,
            )
            or snapshot.workspace_id != workspace.workspace_id
            or snapshot.preflight_run_id != package.export_plan.run_id
            or snapshot.preflight_result_hash != report.content_hash
            or snapshot.mapping_content_hash != package.destination_match_plan_hash
            or snapshot.compiled_plan_hash != package.transfer_order_plan_hash
        ):
            raise WorkspaceError("The prepared transfer preview is no longer current")
        if self.execution.current_transfer_run(workspace.workspace_id) is not None:
            raise WorkspaceError(
                "This approved transfer already has a load journal. Verify its outcome."
            )
        snapshot = ExecutionSnapshot.from_json(snapshot.to_json())
        self.artifacts.write_report(
            workspace.workspace_id,
            snapshot.preflight_run_id,
            EXECUTION_SNAPSHOT_NAME,
            snapshot.to_json().encode("utf-8"),
        )
        return snapshot

    def current_snapshot(
        self,
        workspace: WorkspaceState,
        selection: SourceSelection,
        schema: OdooSchemaCatalog,
    ) -> ExecutionSnapshot | None:
        """Load a staged 8B preview only while every approval remains current."""

        report = workspace.transfer_preflight_report
        package = workspace.transfer_review_package
        if (
            report is None
            or package is None
            or not workspace.transfer_preflight_ready(
                source_selection_hash=selection.content_hash,
                source_schema_hash=schema.content_hash,
            )
        ):
            return None
        try:
            snapshot = self.execution.preflight.execution_snapshot(
                workspace.workspace_id,
                package.export_plan.run_id,
            )
        except ReadinessError:
            return None
        if (
            snapshot.preflight_result_hash != report.content_hash
            or snapshot.mapping_content_hash != package.destination_match_plan_hash
            or snapshot.compiled_plan_hash != package.transfer_order_plan_hash
            or snapshot.staging_content_hash != selection.content_hash
            or snapshot.target_hash != report.destination_target_hash
        ):
            return None
        return snapshot

    def execute(
        self,
        workspace: WorkspaceState,
        snapshot: ExecutionSnapshot,
        *,
        expected_preflight_hash: str,
        executor: OdooWriteExecutor,
        actor: Actor,
        batch_rows: int,
        read_identity: OdooReadIdentity,
        credential_binding_hash: str,
        write_identity: OdooWriteIdentity,
        progress=None,
    ) -> ExecutionRun:
        """Execute the exact staged snapshot through the shared journalled writer."""

        snapshot = ExecutionSnapshot.from_json(snapshot.to_json())
        try:
            return self.execution.execute_transfer(
                workspace.workspace_id,
                expected_snapshot_hash=snapshot.semantic_hash,
                expected_preflight_hash=expected_preflight_hash,
                snapshot=snapshot,
                executor=executor,
                actor=actor,
                batch_rows=batch_rows,
                read_identity=read_identity,
                credential_binding_hash=credential_binding_hash,
                write_identity=write_identity,
                progress=progress,
            )
        except Exception:
            if self.execution.current_transfer_run(workspace.workspace_id) is None:
                self.artifacts.delete_report(
                    workspace.workspace_id,
                    snapshot.preflight_run_id,
                    EXECUTION_SNAPSHOT_NAME,
                )
            raise


def compile_transfer_execution_snapshot(
    workspace: WorkspaceState,
    selection: SourceSelection,
    schema: OdooSchemaCatalog,
    package: TransferReviewPackage,
    preflight: TransferPreflightReport,
    fresh_match: DestinationMatchPlan,
    destination_records: RecordSnapshot,
    *,
    source_rows: Mapping[str, tuple[SourceRow, ...]],
    source_origins: Mapping[str, tuple[OdooOriginBatch, ...]],
    source_snapshots: Mapping[str, SourceSnapshot],
    source_manifest_hashes: Mapping[str, str],
) -> ExecutionSnapshot:
    """Turn generic scalar and relation evidence into the shared write contract."""

    selected_by_id = {item.dataset_id: item for item in selection.datasets}
    reviewed_by_id = {item.dataset_id: item for item in package.datasets}
    match_by_id = {item.dataset_id: item for item in fresh_match.model_matches}
    preflight_by_id = {item.dataset_id: item for item in preflight.datasets}
    schema_by_model = {item.name: item for item in schema.models}
    expected_ids = set(reviewed_by_id)
    if not expected_ids or any(
        set(items) != expected_ids
        for items in (
            selected_by_id,
            match_by_id,
            preflight_by_id,
            source_rows,
            source_origins,
            source_snapshots,
            source_manifest_hashes,
        )
    ):
        raise WorkspaceError("Stage 8B source and review datasets do not align")

    target_ids_by_dataset: dict[str, dict[str, int]] = {}
    keys_by_dataset: dict[str, tuple[str, ...]] = {}
    bindings_by_dataset: dict[str, dict[str, str]] = {}
    source_id_to_row: dict[str, dict[int, int]] = {}
    relationships_by_owner: dict[str, list] = {}
    for relationship in package.relationships:
        relationships_by_owner.setdefault(relationship.owner_dataset_id, []).append(
            relationship
        )

    for reviewed in package.datasets:
        dataset_id = reviewed.dataset_id
        match = match_by_id[dataset_id]
        rows = source_rows[dataset_id]
        if len(rows) != reviewed.source_row_count:
            raise WorkspaceError(
                f"Frozen row count changed for {reviewed.model_label}"
            )
        selected = selected_by_id[dataset_id]
        key_column = next(
            (
                item
                for item in selected.columns
                if item.stable_key == match.source_column_key
            ),
            None,
        )
        if key_column is None or key_column.source_name != reviewed.key_field:
            raise WorkspaceError(
                f"Matching field changed for {reviewed.model_label}"
            )
        keys = tuple(_match_value(row.values.get(reviewed.key_field)) for row in rows)
        if any(not value for value in keys) or len(set(keys)) != len(keys):
            raise WorkspaceError(
                f"Matching values are blank or duplicated for {reviewed.model_label}"
            )
        keys_by_dataset[dataset_id] = keys

        matches: dict[str, list[int]] = {value: [] for value in keys}
        for record in destination_records.records.get(reviewed.model, ()):
            value = _match_value(record.values.get(reviewed.key_field))
            if value in matches:
                matches[value].append(record.odoo_id)
        if any(len(items) > 1 for items in matches.values()):
            raise WorkspaceError(
                f"Destination matching became ambiguous for {reviewed.model_label}"
            )
        binding_hash = _destination_binding_hash(
            reviewed.model,
            reviewed.key_field,
            matches,
        )
        expected = preflight_by_id[dataset_id]
        existing = sum(bool(items) for items in matches.values())
        if (
            binding_hash != expected.observed_key_binding_hash
            or existing != expected.observed_existing_record_count
            or len(keys) - existing != expected.observed_create_record_count
        ):
            raise WorkspaceError(
                f"Destination identities changed for {reviewed.model_label}"
            )
        target_ids_by_dataset[dataset_id] = {
            key: items[0] for key, items in matches.items() if items
        }
        bindings_by_dataset[dataset_id] = {
            key: target_record_binding_hash(reviewed.model, identifier)
            for key, identifier in target_ids_by_dataset[dataset_id].items()
        }
        origin_ids = _ordered_origin_ids(
            source_origins[dataset_id],
            len(rows),
            reviewed.model_label,
        )
        source_id_to_row[dataset_id] = {
            identifier: ordinal
            for ordinal, identifier in enumerate(origin_ids, start=1)
        }

    execution_datasets = tuple(
        ExecutionDataset(
            dataset=item.dataset_name,
            target_model=item.model,
            sequence=index,
            dependencies=tuple(
                sorted(
                    {
                        reviewed_by_id[relationship.related_dataset_id].dataset_name
                        for relationship in package.relationships
                        if relationship.owner_dataset_id == item.dataset_id
                        and relationship.incoming_link_count > 0
                    }
                )
            ),
            existing_policy="update",
            identity_fields=(item.key_field,),
            scope_fields=(),
            field_types=tuple(
                sorted(
                    (
                        field_name,
                        _field_type(schema_by_model, item.model, field_name),
                    )
                    for field_name in (
                        *item.scalar_write_fields,
                        *item.relationship_write_fields,
                    )
                )
            ),
        )
        for index, item in enumerate(
            sorted(package.datasets, key=lambda current: (current.wave, current.model))
        )
    )

    provisional_rows: list[ExecutionRow] = []
    for reviewed in package.datasets:
        dataset_id = reviewed.dataset_id
        rows = source_rows[dataset_id]
        if tuple(row.number for row in rows) != tuple(range(1, len(rows) + 1)):
            raise WorkspaceError(
                f"Frozen row order changed for {reviewed.model_label}"
            )
        keys = keys_by_dataset[dataset_id]
        relationship_columns = _relationship_columns(
            source_origins[dataset_id],
            len(rows),
        )
        for source_row, key in zip(rows, keys, strict=True):
            target_id = target_ids_by_dataset[dataset_id].get(key)
            disposition = "UPDATE" if target_id is not None else "CREATE"
            intents = [
                _scalar_intent(field, source_row.values.get(field))
                for field in reviewed.scalar_write_fields
            ]
            for relationship in relationships_by_owner.get(dataset_id, ()):
                members_by_row = relationship_columns.get(relationship.field_name)
                if members_by_row is None:
                    raise WorkspaceError(
                        f"Protected relationship evidence is missing for "
                        f"{relationship.owner_model}.{relationship.field_name}"
                    )
                members = members_by_row[source_row.number - 1]
                references = []
                reference_bindings = []
                related_keys = keys_by_dataset[relationship.related_dataset_id]
                related_rows = source_id_to_row[relationship.related_dataset_id]
                for source_id in members:
                    related_ordinal = related_rows.get(source_id)
                    if related_ordinal is None:
                        raise WorkspaceError(
                            f"A related source record is missing for "
                            f"{relationship.owner_model}.{relationship.field_name}"
                        )
                    related_key = related_keys[related_ordinal - 1]
                    related_binding = bindings_by_dataset[
                        relationship.related_dataset_id
                    ].get(related_key)
                    if related_binding:
                        references.append(
                            BusinessReference(
                                model=relationship.related_model,
                                key=(related_key,),
                            )
                        )
                        reference_bindings.append(related_binding)
                    else:
                        references.append(
                            LogicalReference(
                                origin="incoming",
                                key=(related_key,),
                                dataset=reviewed_by_id[
                                    relationship.related_dataset_id
                                ].dataset_name,
                            )
                        )
                        reference_bindings.append("")
                intents.append(
                    _relationship_intent(
                        relationship,
                        tuple(references),
                        tuple(reference_bindings),
                    )
                )
            row_id = _row_id(
                workspace.workspace_id,
                dataset_id,
                source_row.number,
                reviewed.model,
            )
            provisional_rows.append(
                ExecutionRow(
                    row_id=row_id,
                    dataset=reviewed.dataset_name,
                    source_row=source_row.number,
                    source_trace_id=row_id,
                    source_identity=(key,),
                    target_model=reviewed.model,
                    business_identity=(key,),
                    business_scope=(),
                    disposition=disposition,
                    target_match_count=1 if target_id is not None else 0,
                    target_binding_hash=(
                        target_record_binding_hash(reviewed.model, target_id)
                        if target_id is not None
                        else ""
                    ),
                    proposed_external_id=(
                        _external_id(workspace.workspace_id, reviewed.model, key)
                        if target_id is None
                        else ""
                    ),
                    fields=tuple(sorted(intents, key=lambda item: item.field)),
                )
            )

    planned_rows, relationship_plan = plan_execution_rows(
        tuple(provisional_rows), execution_datasets
    )
    if relationship_plan.blockers:
        raise WorkspaceError(
            "The approved relationship order could not be compiled safely"
        )
    rank = {row.row_id: row.schedule_ordinal for row in planned_rows}
    rows_by_dataset = {}
    for row in planned_rows:
        rows_by_dataset.setdefault(row.dataset, []).append(row.row_id)
    components = []
    for wave in sorted({item.wave for item in package.datasets}):
        wave_rows = [
            row_id
            for item in package.datasets
            if item.wave == wave
            for row_id in rows_by_dataset.get(item.dataset_name, ())
        ]
        if wave_rows:
            components.append(tuple(sorted(wave_rows, key=rank.__getitem__)))
    planned_rows, relationship_plan = resequence_execution_rows(
        planned_rows,
        relationship_plan,
        execution_datasets,
        tuple(components),
    )
    counts = {
        item.value: sum(row.disposition == item.value for row in planned_rows)
        for item in Classification
    }
    if (
        counts["UPDATE"] != package.totals.destination_existing_record_count
        or counts["CREATE"] != package.totals.destination_create_record_count
        or len(planned_rows) != package.totals.source_record_count
    ):
        raise WorkspaceError("Stage 8B record controls do not reconcile")

    frozen_input_hash = content_hash(
        {
            "source_selection": selection.content_hash,
            "source_snapshots": {
                dataset_id: source_snapshots[dataset_id].content_hash
                for dataset_id in sorted(source_snapshots)
            },
            "source_manifests": dict(sorted(source_manifest_hashes.items())),
            "review_package": package.content_hash,
            "preflight": preflight.content_hash,
        }
    )
    snapshot = ExecutionSnapshot(
        workspace_id=workspace.workspace_id,
        preflight_run_id=package.export_plan.run_id,
        mapping_id=package.export_plan.plan_id,
        mapping_version=1,
        mapping_content_hash=package.destination_match_plan_hash,
        compiled_plan_hash=package.transfer_order_plan_hash,
        staging_run_id=package.export_plan.run_id,
        staging_content_hash=selection.content_hash,
        quality_run_id=package.export_plan.run_id,
        quality_content_hash=package.content_hash,
        normalization_run_id=package.export_plan.run_id,
        normalization_content_hash=package.export_plan.actions_hash,
        normalization_lifecycle_version=1,
        eligible_dataset_hash=selection.content_hash,
        frozen_input_hash=frozen_input_hash,
        preflight_result_hash=preflight.content_hash,
        metadata_snapshot_hash=preflight.destination_schema_snapshot_hash,
        record_snapshot_hash=preflight.destination_record_snapshot_hash,
        target_hash=preflight.destination_target_hash,
        target_database=destination_records.fingerprint.database,
        target_odoo_version=destination_records.fingerprint.odoo_version,
        target_snapshot_at=destination_records.fingerprint.snapshot_timestamp,
        target_module_versions=dict(
            sorted(destination_records.fingerprint.module_versions.items())
        ),
        datasets=execution_datasets,
        counts=counts,
        rows=planned_rows,
        root_hash="sha256:"
        + sha256(
            canonical_json_bytes([row.row_hash for row in planned_rows])
        ).hexdigest(),
        relationship_plan=relationship_plan,
        read_credential_binding_hash=preflight.destination_credential_binding_hash,
        read_principal_hash=preflight.destination_read_principal_hash,
        read_permission_hash=preflight.observed_permission_hash,
        read_context_hash=preflight.observed_context_hash,
        readable_models=tuple(sorted(item.model for item in package.datasets)),
    )
    try:
        return ExecutionSnapshot.from_json(snapshot.to_json())
    except ValueError as error:
        raise WorkspaceError("The Stage 8B execution snapshot is invalid") from error


def _scalar_intent(field: str, value: object) -> FieldIntent:
    return FieldIntent(
        field=field,
        action="SET_NULL" if value is None else "SET_VALUE",
        value=None if value is None else value,
    )


def _relationship_intent(relationship, references, bindings) -> FieldIntent:
    if not references:
        if relationship.required:
            raise WorkspaceError(
                f"A required relationship is blank for "
                f"{relationship.owner_model}.{relationship.field_name}"
            )
        return FieldIntent(
            field=relationship.field_name,
            action="SET_NULL",
            kind="relation",
            relation_operation="replace",
            related_model=relationship.related_model,
            related_identity_fields=(relationship.related_key_field,),
            dependency_strength=(
                "hard" if relationship.required else "deferrable"
            ),
        )
    value = references[0] if relationship.kind == "many2one" else references
    return FieldIntent(
        field=relationship.field_name,
        action="SET_VALUE",
        value=value,
        kind="relation",
        relation_operation="replace",
        related_model=relationship.related_model,
        related_identity_fields=(relationship.related_key_field,),
        dependency_strength="hard" if relationship.required else "deferrable",
        target_binding_hashes=bindings,
    )


def _field_type(schema_by_model, model: str, field_name: str) -> str:
    schema_model = schema_by_model.get(model)
    field = next(
        (item for item in schema_model.fields if item.name == field_name),
        None,
    ) if schema_model is not None else None
    if field is None:
        raise WorkspaceError(f"Approved field {model}.{field_name} is missing")
    return field.type


def _ordered_origin_ids(
    batches: tuple[OdooOriginBatch, ...],
    expected_rows: int,
    label: str,
) -> tuple[int, ...]:
    identifiers = []
    next_ordinal = 1
    for batch in batches:
        if batch.first_row_ordinal != next_ordinal:
            raise WorkspaceError(f"Protected source rows changed for {label}")
        identifiers.extend(batch.odoo_ids)
        next_ordinal += batch.row_count
    if len(identifiers) != expected_rows:
        raise WorkspaceError(f"Protected source rows changed for {label}")
    return tuple(identifiers)


def _relationship_columns(
    batches: tuple[OdooOriginBatch, ...],
    expected_rows: int,
) -> dict[str, tuple[tuple[int, ...], ...]]:
    columns: dict[str, list[tuple[int, ...]]] = {}
    for batch in batches:
        for column in batch.relationships:
            columns.setdefault(column.field_name, []).extend(column.values)
    result = {field: tuple(values) for field, values in columns.items()}
    if any(len(values) != expected_rows for values in result.values()):
        raise WorkspaceError("Protected relationship rows are inconsistent")
    return result


def _destination_binding_hash(
    model: str,
    key_field: str,
    matches: Mapping[str, list[int]],
) -> str:
    return content_hash(
        {
            "model": model,
            "key_field": key_field,
            "classifications": [
                {
                    "key": key,
                    "target_bindings": sorted(
                        target_record_binding_hash(model, identifier)
                        for identifier in matches[key]
                    ),
                }
                for key in sorted(matches)
            ],
        }
    )


def _match_value(value: object) -> str:
    return "" if value is None or value is False else str(value).strip()


def _row_id(workspace_id: str, dataset_id: str, source_row: int, model: str) -> str:
    return "sha256:" + sha256(
        canonical_json_bytes(
            {
                "workspace_id": workspace_id,
                "dataset_id": dataset_id,
                "source_row": source_row,
                "target_model": model,
            }
        )
    ).hexdigest()


def _external_id(workspace_id: str, model: str, key: str) -> str:
    namespace = sha256(workspace_id.encode("utf-8")).hexdigest()[:12]
    identity = sha256(
        canonical_json_bytes(
            {
                "target_model": model,
                "business_identity": portable_value((key,)),
            }
        )
    ).hexdigest()[:24]
    model_token = _MODEL_TOKEN.sub("_", model.casefold())
    return f"impodo_{namespace}.{model_token}_{identity}"
