"""Compile and publish the confirmed Odoo-to-Odoo load input.

The load step turns the final read-only transfer evidence into one exact execution
snapshot.  Staging is still not a write decision.  A separate confirmation
passes the saved snapshot to the shared journalled loader, which rechecks the
destination before it contacts Odoo.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping
from dataclasses import replace
from decimal import Decimal
from hashlib import sha256
from typing import Protocol, cast

from impodo.application.data_version.source_snapshots import (
    load_source_snapshot_table,
    validate_snapshot_for_dataset,
)
from impodo.application.odoo_provenance_service import OdooProvenanceService
from impodo.application.shared.artifacts import GovernedArtifactStores
from impodo.domain.errors import ReadinessError
from impodo.domain.execution.models import ExecutionRun
from impodo.domain.execution.odoo_write import OdooWriteExecutor
from impodo.domain.execution_snapshot import (
    ExecutionDataset,
    ExecutionRow,
    ExecutionSnapshot,
    FieldIntent,
    plan_execution_rows,
    resequence_execution_rows,
)
from impodo.domain.mapping.contracts import RowInclusionMode
from impodo.domain.odoo.contracts import RecordSnapshot, record_snapshot_payload
from impodo.domain.odoo_provenance import OdooOriginBatch
from impodo.domain.odoo_restore import (
    OdooRestorePlan,
    RestoreLinkPolicy,
    RestoreValue,
    RestoreValuePolicy,
    apply_odoo_restore,
    restore_policy_binding,
)
from impodo.domain.preparation.source import SourceRow
from impodo.domain.reconciliation import ReconciliationRun
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
from impodo.domain.workspace.destination_matching import (
    DestinationCreateFieldEvidence,
    DestinationMatchPlan,
)
from impodo.domain.workspace.errors import WorkspaceError
from impodo.domain.workspace.portable_identity import (
    captured_scope_references,
    portable_components,
    portable_identity,
    record_identity,
    scoped_identity,
    scoped_record_identity,
)
from impodo.domain.workspace.transfer_identity_aliases import (
    resolve_transfer_identity_aliases,
)
from impodo.domain.workspace.transfer_preflight import TransferPreflightReport
from impodo.domain.workspace.transfer_review import TransferReviewPackage
from impodo.domain.workspace.workbench import SourceMode, WorkspaceState

from .preflight_service import EXECUTION_SNAPSHOT_NAME
from .transfer_row_inclusion import verify_transfer_row_inclusion
from .workspace.execution.service import ExecutionService

_MODEL_TOKEN = re.compile(r"[^a-z0-9_]+")


class TransferExecutionSourceRepository(Protocol):
    """Structural source-reader contract used by the transfer compiler."""

    def get_current_source_snapshots(
        self, workspace_id: str
    ) -> tuple[SourceSnapshot, ...]: ...


class TransferExecutionService:
    """Build one protected-value snapshot and use the shared transfer loader.

    This service owns the seam between the approved transfer review and normal
    execution.  It verifies that all source, destination, and preflight
    evidence belongs together, but it does not create a writer or perform a
    target write while compiling or staging.
    """

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
        restore_plans: Mapping[str, OdooRestorePlan] | None = None,
    ) -> ExecutionSnapshot:
        """Compile exact frozen values after the final destination read.

        Every input must match the current approved transfer package.  The
        result contains protected source relationship evidence and is suitable
        only for the immediately following load confirmation.
        """

        if workspace.source_mode is not SourceMode.ODOO:
            raise WorkspaceError("The destination load requires a frozen Odoo source")
        try:
            restore_hash = restore_policy_binding(
                restore_plans, workspace.saved_restore_generation_id,
                required_models=(item.model for item in package.datasets if item.model_policy != "reuse_only"),
            )
        except ValueError as error:
            raise WorkspaceError(str(error)) from error
        if package.restore_policy_hash != restore_hash:
            raise WorkspaceError("The saved-data field policies differ from the approved restore review")
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

        # Snapshot bytes and protected Odoo-origin evidence are read separately
        # because neither representation is allowed to stand in for the other.
        snapshots = self.sources.get_current_source_snapshots(workspace.workspace_id)
        snapshot_by_dataset = {item.dataset_id: item for item in snapshots}
        dataset_by_id = {item.dataset_id: item for item in selection.datasets}
        if len(snapshot_by_dataset) != len(snapshots) or set(
            snapshot_by_dataset
        ) != set(dataset_by_id):
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
        manifest_hashes = {item.dataset_id: item.content_hash for item in manifests}
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
            create_field_evidence=fresh_match.create_field_evidence,
            restore_plans=restore_plans,
        )

    def stage(
        self,
        workspace: WorkspaceState,
        selection: SourceSelection,
        schema: OdooSchemaCatalog,
        snapshot: ExecutionSnapshot,
    ) -> ExecutionSnapshot:
        """Publish the exact no-write preview used by final confirmation.

        A staged preview is discarded if no journal is created.  Once a load
        journal exists it remains the recovery authority, even if the browser
        retries this action.
        """

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
        """Return the staged preview only while every approval remains current.

        This query deliberately returns ``None`` for a stale or unreadable
        artifact.  Callers must return to review rather than infer that an old
        preview can be repaired.
        """

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
        protected_values: Mapping[str, object] | None = None,
        progress=None,
    ) -> ExecutionRun:
        """Execute the exact staged snapshot through the shared journalled writer.

        The shared service records intent before transport and verifies the
        destination identity again.  If no journal was created, this method
        removes the staged artifact so it cannot appear as an active preview.
        """

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
                protected_values=protected_values,
                progress=progress,
            )
        except Exception:
            # Retain staging once journalled: it is part of the exact recovery
            # boundary for an interrupted transfer.
            if self.execution.current_transfer_run(workspace.workspace_id) is None:
                self.artifacts.delete_report(
                    workspace.workspace_id,
                    snapshot.preflight_run_id,
                    EXECUTION_SNAPSHOT_NAME,
                )
            raise

    def resume(
        self,
        workspace: WorkspaceState,
        snapshot: ExecutionSnapshot,
        recovery: ReconciliationRun,
        *,
        expected_execution_run_id: str,
        expected_preflight_hash: str,
        executor: OdooWriteExecutor,
        actor: Actor,
        read_identity: OdooReadIdentity,
        credential_binding_hash: str,
        write_identity: OdooWriteIdentity,
        protected_values: Mapping[str, object] | None = None,
        progress=None,
    ) -> ExecutionRun:
        """Resume an interrupted transfer without replacing its journal.

        The caller supplies the recovery read-back tied to this run.  The
        shared execution service decides which rows are safe to retry and
        preserves the original journal and deterministic create identities.
        """

        snapshot = ExecutionSnapshot.from_json(snapshot.to_json())
        return self.execution.resume_transfer(
            workspace.workspace_id,
            expected_execution_run_id=expected_execution_run_id,
            expected_snapshot_hash=snapshot.semantic_hash,
            expected_preflight_hash=expected_preflight_hash,
            snapshot=snapshot,
            recovery=recovery,
            executor=executor,
            actor=actor,
            read_identity=read_identity,
            credential_binding_hash=credential_binding_hash,
            write_identity=write_identity,
            protected_values=protected_values,
            progress=progress,
        )


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
    create_field_evidence: DestinationCreateFieldEvidence | None = None,
    restore_plans: Mapping[str, OdooRestorePlan] | None = None,
) -> ExecutionSnapshot:
    """Turn generic scalar and relation evidence into the shared write contract.

    The function fails closed unless every reviewed dataset has exactly one
    selected snapshot, source-origin set, destination match, and preflight
    entry.  It produces an in-memory snapshot; ``stage`` is responsible for
    making that snapshot the browser's current no-write preview.
    """

    try:
        restore_hash = restore_policy_binding(
            restore_plans, workspace.saved_restore_generation_id,
            required_models=(item.model for item in package.datasets if item.model_policy != "reuse_only"),
        )
    except ValueError as error:
        raise WorkspaceError(str(error)) from error
    if package.restore_policy_hash != restore_hash:
        raise WorkspaceError("The saved-data field policies differ from the approved restore review")
    if not fresh_match.row_inclusion_confirmed or not fresh_match.row_inclusion_confirmable:
        raise WorkspaceError("Check and confirm the current rows to use before loading.")
    selected_by_id = {item.dataset_id: item for item in selection.datasets}
    reviewed_by_id = {item.dataset_id: item for item in package.datasets}
    match_by_id = {item.dataset_id: item for item in fresh_match.model_matches}
    preflight_by_id = {item.dataset_id: item for item in preflight.datasets}
    schema_by_model = {item.name: item for item in schema.models}
    current_values_by_model = {
        model: {record.odoo_id: record.values for record in records}
        for model, records in destination_records.records.items()
    } if restore_plans is not None else {}
    current_reference_catalog = _restore_reference_catalog(
        destination_records, {item.model: item.key_fields for item in package.datasets}
    ) if any(field.link_policy is RestoreLinkPolicy.ADD_MISSING for plan in (restore_plans or {}).values() for field in plan.fields) else {}
    if restore_plans is not None:
        if set(restore_plans) - set(schema_by_model):
            raise WorkspaceError("Restore field plans contain unselected record types")
        schema_by_model = {
            name: replace(model, fields=restore_plans[name].destination_fields)
            if name in restore_plans else model
            for name, model in schema_by_model.items()
        }
    if create_field_evidence is None:
        create_field_evidence = fresh_match.create_field_evidence
    create_decisions_by_dataset: dict[str, list] = {}
    for decision in fresh_match.create_field_decisions:
        create_decisions_by_dataset.setdefault(decision.dataset_id, []).append(decision)
    protected_create_values = (
        {item.key: item for item in create_field_evidence.values}
        if create_field_evidence is not None
        else {}
    )
    if fresh_match.create_field_decisions and (
        create_field_evidence is None
        or create_field_evidence.evidence_id != fresh_match.create_field_evidence_id
        or create_field_evidence.content_hash != fresh_match.create_field_evidence_hash
        or any(
            (protected := protected_create_values.get(decision.key)) is None
            or protected.value_hash != decision.value_hash
            or protected.field_contract_hash != decision.field_contract_hash
            for decision in fresh_match.create_field_decisions
        )
    ):
        raise WorkspaceError("Protected create-only field evidence is unavailable")
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
        raise WorkspaceError("The load source and review datasets do not align")

    target_ids_by_dataset: dict[str, dict[str, int]] = {}
    keys_by_dataset: dict[str, tuple[str, ...]] = {}
    components_by_dataset: dict[str, tuple[tuple[str, ...], ...]] = {}
    bindings_by_dataset: dict[str, dict[str, str]] = {}
    source_id_to_row: dict[str, dict[int, int]] = {}
    included_rows_by_dataset: dict[str, tuple[SourceRow, ...]] = {}
    relationships_by_owner: dict[str, list] = {}
    business_components_by_dataset: dict[str, tuple[tuple[str, ...], ...]] = {}
    alias_destination_by_source: dict[str, dict[str, tuple[str, ...]]] = {}
    alias_source_by_destination: dict[str, dict[str, str]] = {}
    alias_evidence = fresh_match.identity_alias_evidence
    if fresh_match.identity_alias_bindings and (
        alias_evidence is None
        or alias_evidence.evidence_id != fresh_match.identity_alias_evidence_id
        or alias_evidence.content_hash != fresh_match.identity_alias_evidence_hash
        or alias_evidence.public_bindings != fresh_match.identity_alias_bindings
    ):
        raise WorkspaceError("The protected reviewed identity aliases are unavailable or changed")
    alias_values = {item.dataset_id: item for item in alias_evidence.values} if alias_evidence is not None else {}
    for reviewed in package.datasets:
        evidence = alias_values.get(reviewed.dataset_id)
        expected_hash = evidence.binding.content_hash if evidence is not None else ""
        expected_count = len(evidence.aliases) if evidence is not None else 0
        if reviewed.identity_alias_binding_hash != expected_hash or reviewed.identity_alias_count != expected_count:
            raise WorkspaceError("The reviewed identity aliases changed before execution")
        if evidence is None:
            continue
        match = match_by_id[reviewed.dataset_id]
        if reviewed.model_policy != "reuse_only" or reviewed.scope_fields or evidence.model != reviewed.model:
            raise WorkspaceError("Identity aliases cannot authorize writes or scoped reference identities")
        omitted = set(match.omitted_source_row_numbers)
        included = tuple(portable_components(tuple(row.values.get(field) for field in reviewed.key_fields)) for row in source_rows[reviewed.dataset_id] if row.number not in omitted)
        try:
            resolved = resolve_transfer_identity_aliases(included, key_fields=reviewed.key_fields, destination_handling=match.destination_handling, aliases=evidence.aliases)
        except ValueError as error:
            raise WorkspaceError(str(error)) from error
        if resolved.binding != evidence.binding:
            raise WorkspaceError("The source population for the reviewed identity aliases changed")
        alias_destination_by_source[reviewed.dataset_id] = {
            portable_identity(original): destination
            for original, destination in zip(resolved.source_identities, resolved.destination_identities, strict=True)
        }
        business_components_by_dataset[reviewed.dataset_id] = resolved.destination_identities
        alias_source_by_destination[reviewed.dataset_id] = {
            portable_identity(destination): portable_identity(original)
            for original, destination in zip(resolved.source_identities, resolved.destination_identities, strict=True)
        }
    scopes_by_dataset: dict[str, tuple[tuple[BusinessReference, ...], ...]] = {}
    business_scopes_by_dataset: dict[str, tuple[tuple[BusinessReference, ...], ...]] = {}
    scope_targets_by_dataset: dict[str, dict[int, BusinessReference]] = {}
    for reviewed in package.datasets:
        if not reviewed.scope_fields:
            continue
        parent = reviewed_by_id[reviewed.scope_dataset_ids[0]]
        if parent.scope_fields:
            raise WorkspaceError("A matching-scope parent cannot itself use relational scope")
        parent_match = match_by_id[parent.dataset_id]
        match = match_by_id[reviewed.dataset_id]
        scopes = captured_scope_references(
            parent_model=parent.model, parent_fields=parent.key_fields,
            parent_values=tuple(tuple(row.values.get(field) for field in parent.key_fields) for row in source_rows[parent.dataset_id]),
            parent_origins=source_origins[parent.dataset_id], owner_origins=source_origins[reviewed.dataset_id],
            scope_field=reviewed.scope_fields[0], owner_row_count=len(source_rows[reviewed.dataset_id]),
            excluded_parent_rows=parent_match.omitted_source_row_numbers,
            excluded_owner_rows=match.omitted_source_row_numbers,
        )
        targets: dict[str, list[int]] = {}
        for record in destination_records.records.get(parent.model, ()):
            targets.setdefault(record_identity(record.values, parent.key_fields), []).append(record.odoo_id)
        scope_targets: dict[int, BusinessReference] = {}
        for scope in scopes:
            if not scope:
                continue
            destination_key = alias_destination_by_source.get(parent.dataset_id, {}).get(portable_identity(scope[0].key), portable_components(scope[0].key))
            target_ids = targets.get(portable_identity(destination_key), ())
            if len(target_ids) != 1:
                raise WorkspaceError("The matching-scope parent no longer matches one existing destination record")
            scope_targets[target_ids[0]] = scope[0]
        scopes_by_dataset[reviewed.dataset_id] = scopes
        if parent.dataset_id in alias_destination_by_source:
            mapped_references = {
                reference: replace(reference, key=alias_destination_by_source[parent.dataset_id][portable_identity(reference.key)])
                for reference in {reference for scope in scopes for reference in scope}
            }
            business_scopes_by_dataset[reviewed.dataset_id] = tuple(tuple(mapped_references[reference] for reference in scope) for scope in scopes)
        else:
            business_scopes_by_dataset[reviewed.dataset_id] = scopes
        scope_targets_by_dataset[reviewed.dataset_id] = scope_targets
    for relationship in package.relationships:
        relationships_by_owner.setdefault(relationship.owner_dataset_id, []).append(
            relationship
        )

    for reviewed in package.datasets:
        dataset_id = reviewed.dataset_id
        match = match_by_id[dataset_id]
        all_rows = source_rows[dataset_id]
        if tuple(row.number for row in all_rows) != tuple(range(1, len(all_rows) + 1)):
            raise WorkspaceError(f"Frozen row order changed for {reviewed.model_label}")
        if match.row_inclusion.mode is RowInclusionMode.MATCHING_ROWS:
            verify_transfer_row_inclusion(selected_by_id[dataset_id], match, all_rows)
        excluded_rows = set(match.omitted_source_row_numbers)
        if (
            len(all_rows) != match.frozen_source_row_count
            or reviewed.excluded_source_record_count != len(excluded_rows)
        ):
            raise WorkspaceError(f"Frozen row scope changed for {reviewed.model_label}")
        rows = tuple(row for row in all_rows if row.number not in excluded_rows)
        if len(rows) != reviewed.source_row_count:
            raise WorkspaceError(f"Frozen row count changed for {reviewed.model_label}")
        included_rows_by_dataset[dataset_id] = rows
        selected = selected_by_id[dataset_id]
        columns_by_key = {item.stable_key: item for item in selected.columns}
        selected_key_fields = tuple(
            column.source_name if column is not None else None
            for key in match.source_column_keys
            for column in (columns_by_key.get(key),)
        )
        if (
            reviewed.key_fields != match.key_fields
            or reviewed.scope_fields != match.scope_fields
            or reviewed.scope_dataset_ids != match.scope_dataset_ids
            or selected_key_fields != reviewed.key_fields
        ):
            raise WorkspaceError(f"Matching field changed for {reviewed.model_label}")
        components = tuple(
            portable_components(
                tuple(row.values.get(field) for field in reviewed.key_fields)
            )
            for row in rows
        )
        scopes = scopes_by_dataset.get(dataset_id)
        keys = tuple(
            scoped_identity(tuple(row.values.get(field) for field in reviewed.key_fields), scopes[row.number - 1] if scopes else ())
            for row in rows
        )
        if any(not value for value in keys) or len(set(keys)) != len(keys):
            raise WorkspaceError(
                f"Matching values are blank or duplicated for {reviewed.model_label}"
            )
        keys_by_dataset[dataset_id] = keys
        components_by_dataset[dataset_id] = components
        business_components_by_dataset.setdefault(dataset_id, components)

        matches: dict[str, list[int]] = {value: [] for value in keys}
        for record in destination_records.records.get(reviewed.model, ()):
            value = scoped_record_identity(record.values, reviewed.key_fields, reviewed.scope_fields, scope_targets_by_dataset.get(dataset_id, {}))
            if dataset_id in alias_source_by_destination:
                value = alias_source_by_destination[dataset_id].get(value, "")
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
            key_fields=reviewed.key_fields,
            scope_fields=reviewed.scope_fields,
            scope_dataset_ids=reviewed.scope_dataset_ids,
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
            len(all_rows),
            reviewed.model_label,
        )
        source_id_to_row[dataset_id] = {
            identifier: included_ordinal
            for included_ordinal, (row, identifier) in enumerate(
                ((row, origin_ids[row.number - 1]) for row in rows),
                start=1,
            )
        }

    incoming_create_references: dict[
        tuple[str, str],
        tuple[BusinessReference | LogicalReference, tuple[str, ...]],
    ] = {}
    for decision in fresh_match.create_field_decisions:
        if decision.provider_kind != "incoming_reference":
            continue
        protected = protected_create_values.get(decision.key)
        candidate = (
            next(
                (
                    item
                    for item in protected.incoming_reference_candidates
                    if item.choice_hash == protected.incoming_reference_choice_hash
                ),
                None,
            )
            if protected is not None
            else None
        )
        related_dataset_id = decision.source_dataset_id or ""
        related_review = reviewed_by_id.get(related_dataset_id)
        if (
            candidate is None
            or related_review is None
            or candidate.source_dataset_id != related_dataset_id
            or candidate.related_model != decision.related_model
            or candidate.identity_fields != decision.related_identity_fields
            or candidate.identity_fields != related_review.key_fields
            or candidate.requires_create != decision.source_reference_requires_create
        ):
            raise WorkspaceError(
                f"The reviewed source record changed for "
                f"{decision.model}.{decision.field_name}"
            )
        candidate_rows = tuple(
            index
            for index, identity in enumerate(components_by_dataset[related_dataset_id])
            if identity == candidate.identity
        )
        if len(candidate_rows) != 1:
            raise WorkspaceError(
                f"The reviewed source record is no longer unique for "
                f"{decision.model}.{decision.field_name}"
            )
        related_key = keys_by_dataset[related_dataset_id][candidate_rows[0]]
        target_binding = bindings_by_dataset[related_dataset_id].get(
            related_key,
            "",
        )
        if (
            candidate.requires_create != (not target_binding)
            or candidate.target_binding_hash != target_binding
        ):
            raise WorkspaceError(
                f"The destination status changed for the reviewed source record "
                f"used by {decision.model}.{decision.field_name}"
            )
        candidate_key = tuple(
            Decimal(str(value)) if isinstance(value, float) else value
            for value in candidate.identity
        )
        if target_binding:
            candidate_key = business_components_by_dataset[related_dataset_id][candidate_rows[0]]
            reference: BusinessReference | LogicalReference = BusinessReference(
                model=candidate.related_model,
                key=candidate_key,
            )
        else:
            reference = LogicalReference(
                origin="incoming",
                key=candidate_key,
                dataset=related_review.dataset_name,
            )
        incoming_create_references[decision.key] = (
            reference,
            (target_binding,),
        )

    dependencies_by_dataset: dict[str, set[str]] = {
        item.dataset_id: set() for item in package.datasets
    }
    for relationship in package.relationships:
        if relationship.incoming_link_count > 0:
            dependencies_by_dataset[relationship.owner_dataset_id].add(
                reviewed_by_id[relationship.related_dataset_id].dataset_name
            )
    for decision in fresh_match.create_field_decisions:
        incoming = incoming_create_references.get(decision.key)
        if incoming is None or not isinstance(incoming[0], LogicalReference):
            continue
        related = reviewed_by_id.get(decision.source_dataset_id or "")
        if related is None:
            raise WorkspaceError(
                "A reviewed create-only source record is no longer selected"
            )
        dependencies_by_dataset[decision.dataset_id].add(related.dataset_name)

    execution_datasets = tuple(
        ExecutionDataset(
            dataset=item.dataset_name,
            target_model=item.model,
            sequence=index,
            dependencies=tuple(sorted(dependencies_by_dataset[item.dataset_id])),
            existing_policy=(
                "update" if item.model_policy == "upsert" else "reference"
            ),
            identity_fields=item.key_fields,
            scope_fields=item.scope_fields,
            field_types=tuple(
                sorted(
                    {
                        **{
                            field_name: _field_type(
                                schema_by_model, item.model, field_name
                            )
                            for field_name in (
                                *item.scalar_write_fields,
                                *item.relationship_write_fields,
                            )
                        },
                        **{
                            decision.field_name: decision.field_type
                            for decision in create_decisions_by_dataset.get(
                                item.dataset_id, ()
                            )
                        },
                    }.items()
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
        rows = included_rows_by_dataset[dataset_id]
        keys = keys_by_dataset[dataset_id]
        relationship_columns = _relationship_columns(
            source_origins[dataset_id],
            match_by_id[dataset_id].frozen_source_row_count,
        )
        for source_row, key, source_identity, business_identity in zip(
            rows,
            keys,
            components_by_dataset[dataset_id],
            business_components_by_dataset[dataset_id],
            strict=True,
        ):
            target_id = target_ids_by_dataset[dataset_id].get(key)
            if target_id is None and reviewed.model_policy == "reuse_only":
                raise WorkspaceError(
                    f"A destination record is missing for reuse-only {reviewed.model_label}"
                )
            if target_id is None:
                disposition = "CREATE"
            elif reviewed.model_policy == "upsert":
                disposition = "UPDATE"
            else:
                disposition = "UNCHANGED"
            intents = []
            if disposition != "UNCHANGED":
                intents.extend(
                    _scalar_intent(field, source_row.values.get(_restore_source_field((restore_plans or {}).get(reviewed.model), field)))
                    for field in reviewed.scalar_write_fields
                )
            if disposition == "CREATE":
                intents.extend(
                    _create_field_intent(
                        decision,
                        protected_create_values[decision.key],
                        source_row,
                        incoming_create_references.get(decision.key),
                    )
                    for decision in create_decisions_by_dataset.get(dataset_id, ())
                )
            relationships_to_write = (
                relationships_by_owner.get(dataset_id, ())
                if disposition != "UNCHANGED"
                else ()
            )
            for relationship in relationships_to_write:
                members_by_row = relationship_columns.get(_restore_source_field((restore_plans or {}).get(reviewed.model), relationship.field_name))
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
                    related_identity = business_components_by_dataset[relationship.related_dataset_id][related_ordinal - 1]
                    related_binding = bindings_by_dataset[
                        relationship.related_dataset_id
                    ].get(related_key)
                    if related_binding:
                        references.append(
                            BusinessReference(
                                model=relationship.related_model,
                                key=related_identity,
                            )
                        )
                        reference_bindings.append(related_binding)
                    else:
                        references.append(
                            LogicalReference(
                                origin="incoming",
                                key=related_identity,
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
            restore_plan = (restore_plans or {}).get(reviewed.model)
            if restore_plan is not None and disposition != "UNCHANGED":
                current_values = current_values_by_model.get(reviewed.model, {}).get(target_id) if target_id is not None else None
                intents = _restore_field_intents(
                    restore_plan, tuple(intents), source_row.values,
                    current_values, current_reference_catalog,
                )
                if disposition == "UPDATE" and not any(intent.action != "OMIT" for intent in intents):
                    disposition = "UNCHANGED"
                    intents = []
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
                    source_identity=(key,) if reviewed.scope_fields else source_identity,
                    target_model=reviewed.model,
                    business_identity=business_identity,
                    business_scope=business_scopes_by_dataset[dataset_id][source_row.number - 1] if reviewed.scope_fields else (),
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
        if row.disposition in {"CREATE", "UPDATE"}:
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
        counts["UPDATE"] + counts["UNCHANGED"]
        != package.totals.destination_existing_record_count
        or counts["CREATE"] != package.totals.destination_create_record_count
        or len(planned_rows) != package.totals.source_record_count
    ):
        raise WorkspaceError("The load record controls do not reconcile")

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
            **({"restore_policy": restore_hash} if restore_hash is not None else {}),
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
        raise WorkspaceError("The transfer execution snapshot is invalid") from error


def _restore_source_field(plan: OdooRestorePlan | None, name: str) -> str:
    if plan is None:
        return name
    return next((field.source.name for field in plan.fields if field.destination.name == name), name)


def _restore_field_intents(
    plan: OdooRestorePlan,
    intents: tuple[FieldIntent, ...],
    saved_values: Mapping[str, object],
    current_values: Mapping[str, object] | None,
    reference_catalog: Mapping[str, Mapping[int, tuple[BusinessReference, str]]],
) -> list[FieldIntent]:
    """Apply approved policies to ordinary intents without adding a writer."""

    scalar_fields = tuple(field for field in plan.fields if field.destination.type not in {"many2one", "many2many", "one2many"})
    scalar_names = {field.destination.name for field in scalar_fields}
    defaults = tuple(provider for provider in plan.defaults if provider.field_name in scalar_names)
    scalar_plan = replace(
        plan, fields=scalar_fields,
        destination_fields=tuple(field.destination for field in scalar_fields),
        identity_fields=tuple(name for name in plan.identity_fields if any(field.source.name == name for field in scalar_fields)),
        defaults=defaults,
    )
    result = apply_odoo_restore(
        scalar_plan,
        {field.source.name: cast(RestoreValue, saved_values[field.source.name]) for field in scalar_fields if field.source.name in saved_values},
        cast(Mapping[str, RestoreValue], current_values) if current_values is not None else None,
    )
    if not result.ready:
        raise WorkspaceError("The saved-data field policies cannot be applied: " + "; ".join(issue.message for issue in result.issues))
    write_values = dict(result.writes)
    provider_names = {provider.field_name for provider in defaults}
    remaining = [
        intent for intent in intents
        if intent.field not in scalar_names
        or (
            intent.field in provider_names
            and _restore_source_field(plan, intent.field) not in saved_values
            and intent.field not in write_values
        )
    ]
    remaining.extend(_scalar_intent(name, value) for name, value in result.writes)
    if current_values is None:
        return remaining

    relation_policies = {field.destination.name: field for field in plan.fields if field.destination.type in {"many2one", "many2many"}}
    effective: list[FieldIntent] = []
    for intent in remaining:
        policy = relation_policies.get(intent.field)
        if policy is None:
            effective.append(intent)
            continue
        needs_current = policy.policy is not RestoreValuePolicy.USE_SAVED or policy.link_policy is not RestoreLinkPolicy.REPLACE
        if needs_current and intent.field not in current_values:
            raise WorkspaceError(f"Read the current destination relationship {plan.model}.{intent.field} before restoring it")
        current = current_values.get(intent.field)
        empty = current is None or current is False or current == () or current == []
        if (
            policy.policy is RestoreValuePolicy.KEEP_DESTINATION
            or policy.link_policy is RestoreLinkPolicy.KEEP
            or (policy.policy is RestoreValuePolicy.FILL_EMPTY and not empty)
        ):
            effective.append(replace(intent, action="OMIT", value=None, target_binding_hashes=()))
            continue
        if policy.link_policy is not RestoreLinkPolicy.ADD_MISSING:
            effective.append(intent)
            continue
        current_refs, bindings = _restore_current_members(
            policy.destination.relation or "", current, reference_catalog,
        )
        saved_refs = intent.value if isinstance(intent.value, tuple) else ()
        merged: dict[str, tuple[BusinessReference | LogicalReference, str]] = {}
        for reference, binding in zip(current_refs, bindings, strict=True):
            merged[canonical_json_bytes(portable_value(reference.key)).decode("utf-8")] = (reference, binding)
        for index, reference in enumerate(saved_refs):
            if not isinstance(reference, (LogicalReference, BusinessReference)):
                raise WorkspaceError("Saved relationship membership is not portable")
            token = canonical_json_bytes(portable_value(reference.key)).decode("utf-8")
            if token not in merged:
                binding = intent.target_binding_hashes[index] if index < len(intent.target_binding_hashes) else ""
                merged[token] = (reference, binding)
        values = tuple(merged[key] for key in sorted(merged))
        if len(values) == len(current_refs):
            effective.append(replace(intent, action="OMIT", value=None, target_binding_hashes=()))
        else:
            effective.append(replace(
                intent, action="SET_VALUE", value=tuple(value[0] for value in values),
                target_binding_hashes=tuple(value[1] for value in values),
            ))
    return effective


def _restore_current_members(
    model: str,
    value: object,
    reference_catalog: Mapping[str, Mapping[int, tuple[BusinessReference, str]]],
) -> tuple[tuple[BusinessReference, ...], tuple[str, ...]]:
    """Resolve current links only from the fresh, bound destination evidence."""

    if value is None or value is False or value == () or value == []:
        return (), ()
    if not isinstance(value, (tuple, list)) or any(not isinstance(member, int) or isinstance(member, bool) or member <= 0 for member in value):
        raise WorkspaceError("The current destination relationship membership is incomplete")
    by_id = reference_catalog.get(model)
    if by_id is None:
        raise WorkspaceError("Adding saved links requires a reviewed portable identity for the related model")
    references: list[BusinessReference] = []
    bindings: list[str] = []
    for identifier in value:
        matched = by_id.get(identifier)
        if matched is None:
            raise WorkspaceError("Adding saved links needs fresh evidence for every current linked record; no destination links were removed")
        references.append(matched[0])
        bindings.append(matched[1])
    return tuple(references), tuple(bindings)


def _restore_reference_catalog(
    records: RecordSnapshot,
    identity_fields: Mapping[str, tuple[str, ...]],
) -> dict[str, dict[int, tuple[BusinessReference, str]]]:
    """Index each fresh related population once for bounded membership merges."""

    if not records.complete:
        raise WorkspaceError("Adding saved links requires complete destination evidence")
    catalog: dict[str, dict[int, tuple[BusinessReference, str]]] = {}
    for model, fields in identity_fields.items():
        if not fields:
            continue
        rows = records.records.get(model, ())
        key_counts = Counter(record_identity(row.values, fields) for row in rows)
        id_counts = Counter(row.odoo_id for row in rows)
        indexed: dict[int, tuple[BusinessReference, str]] = {}
        for row in rows:
            key = record_identity(row.values, fields)
            components = portable_components(tuple(row.values.get(field) for field in fields))
            if key and key_counts[key] == 1 and id_counts[row.odoo_id] == 1 and len(components) == len(fields):
                indexed[row.odoo_id] = (
                    BusinessReference(model=model, key=components, target_fields=fields),
                    target_record_binding_hash(model, row.odoo_id),
                )
        catalog[model] = indexed
    return catalog


def _scalar_intent(field: str, value: object) -> FieldIntent:
    return FieldIntent(
        field=field,
        action="SET_NULL" if value is None else "SET_VALUE",
        value=None if value is None else value,
    )


def _create_field_intent(
    decision,
    protected,
    source_row: SourceRow,
    incoming_reference: tuple[BusinessReference | LogicalReference, tuple[str, ...]]
    | None = None,
) -> FieldIntent:
    if not decision.reviewed:
        raise WorkspaceError(
            f"Choose how to provide {decision.model}.{decision.field_name}"
        )
    relation_shape = (
        {
            "kind": "relation",
            "relation_operation": "replace",
            "related_model": decision.related_model or "",
            "related_identity_fields": decision.related_identity_fields,
            "dependency_strength": "hard",
        }
        if decision.field_type == "many2one"
        else {}
    )
    if decision.provider_kind == "odoo_default":
        return FieldIntent(
            field=decision.field_name,
            action="EXPECT_PROTECTED",
            protected_value_hash=decision.value_hash,
            **relation_shape,
        )
    if decision.provider_kind == "fixed_value":
        return FieldIntent(
            field=decision.field_name,
            action="SET_PROTECTED",
            protected_value_hash=decision.value_hash,
        )
    if decision.provider_kind == "existing_reference":
        return FieldIntent(
            field=decision.field_name,
            action="SET_PROTECTED",
            protected_value_hash=decision.value_hash,
            **relation_shape,
        )
    if decision.provider_kind == "incoming_reference":
        if incoming_reference is None:
            raise WorkspaceError(
                f"Reviewed source record is unavailable for "
                f"{decision.model}.{decision.field_name}"
            )
        reference, bindings = incoming_reference
        return FieldIntent(
            field=decision.field_name,
            action="SET_VALUE",
            value=reference,
            target_binding_hashes=bindings,
            **relation_shape,
        )
    if decision.provider_kind == "source_field":
        value = source_row.values.get(decision.source_field_name or "")
        if value is None or (isinstance(value, str) and not value.strip()):
            raise WorkspaceError(
                f"Source row {source_row.number} has no value for required "
                f"{decision.model}.{decision.field_name}"
            )
        return _scalar_intent(decision.field_name, value)
    raise WorkspaceError(
        f"Create-only provider changed for {decision.model}.{decision.field_name}"
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
            related_identity_fields=relationship.related_key_fields,
            dependency_strength=("hard" if relationship.required else "deferrable"),
        )
    value = references[0] if relationship.kind == "many2one" else references
    return FieldIntent(
        field=relationship.field_name,
        action="SET_VALUE",
        value=value,
        kind="relation",
        relation_operation="replace",
        related_model=relationship.related_model,
        related_identity_fields=relationship.related_key_fields,
        dependency_strength="hard" if relationship.required else "deferrable",
        target_binding_hashes=bindings,
    )


def _field_type(schema_by_model, model: str, field_name: str) -> str:
    schema_model = schema_by_model.get(model)
    field = (
        next(
            (item for item in schema_model.fields if item.name == field_name),
            None,
        )
        if schema_model is not None
        else None
    )
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
    *,
    key_fields: tuple[str, ...] = (),
    scope_fields: tuple[str, ...] = (),
    scope_dataset_ids: tuple[str, ...] = (),
) -> str:
    return content_hash(
        {
            "model": model,
            "key_field": key_field,
            **({"key_fields": key_fields} if len(key_fields) > 1 else {}),
            **({"scope_fields": scope_fields, "scope_dataset_ids": scope_dataset_ids} if scope_fields else {}),
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


def _row_id(workspace_id: str, dataset_id: str, source_row: int, model: str) -> str:
    return (
        "sha256:"
        + sha256(
            canonical_json_bytes(
                {
                    "workspace_id": workspace_id,
                    "dataset_id": dataset_id,
                    "source_row": source_row,
                    "target_model": model,
                }
            )
        ).hexdigest()
    )


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
