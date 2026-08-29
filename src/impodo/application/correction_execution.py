"""Confirm, execute, and verify sparse completed-load corrections."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, Protocol
from uuid import uuid4

from impodo.application.correction_orchestration import (
    CorrectionBinding,
    CorrectionBindingRepository,
    CorrectionProtectedStore,
)
from impodo.application.correction_service import (
    CorrectionPlanService,
    odoo_correction_values_equal,
)
from impodo.domain.correction import (
    CorrectionConfirmation,
    CorrectionPlan,
    CorrectionPlanError,
)
from impodo.domain.correction_execution import CorrectionExecutionSnapshot
from impodo.domain.correction_origin import ProtectedCorrectionArtifactReference
from impodo.domain.execution.models import (
    MAX_CREATE_BATCH_ROWS,
    ExecutionRowAttempt,
    ExecutionRowStatus,
    ExecutionRun,
    ExecutionRunStatus,
)
from impodo.domain.execution.odoo_readback import MAX_READBACK_IDS, OdooReadbackReader
from impodo.domain.execution.odoo_scope import OdooApiScope, OdooModelScope
from impodo.domain.execution.odoo_write import (
    OdooWriteExecutor,
    OdooWriteOutcomeUnknown,
    OdooWriteRejected,
)
from impodo.domain.reconciliation import (
    ReconciliationRow,
    ReconciliationRowStatus,
    ReconciliationRun,
    ReconciliationRunStatus,
)
from impodo.domain.serialization import canonical_json
from impodo.domain.shared.access import Actor, AuthorizationPolicy, Capability
from impodo.domain.shared.models import OdooWriteIdentity, portable_value
from impodo.domain.workspace.errors import WorkspaceError


class CorrectionExecutionRepository(Protocol):
    def start_run(
        self,
        workspace_id: str,
        run: ExecutionRun,
        *,
        actor: Actor,
        correction_plan_hash: str = "",
    ) -> None: ...

    def record_batch_started(
        self, workspace_id: str, run_id: str, rows: Sequence[ExecutionRowAttempt]
    ) -> None: ...

    def record_outcomes(
        self, workspace_id: str, run_id: str, rows: Sequence[ExecutionRowAttempt]
    ) -> None: ...

    def finish_run(
        self,
        workspace_id: str,
        run_id: str,
        status: ExecutionRunStatus,
        *,
        actor: Actor,
    ) -> ExecutionRun: ...


class CorrectionReconciliationRepository(Protocol):
    def publish(
        self, workspace_id: str, report: ReconciliationRun, *, actor: Actor
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class CorrectionExecutionResult:
    execution: ExecutionRun
    reconciliation: ReconciliationRun
    binding: CorrectionBinding


@dataclass(slots=True)
class CorrectionExecutionService:
    """Use exact protected IDs and bounded Odoo calls for confirmed corrections."""

    bindings: CorrectionBindingRepository
    protected_store: CorrectionProtectedStore
    execution: CorrectionExecutionRepository
    reconciliations: CorrectionReconciliationRepository
    authorization: AuthorizationPolicy

    def confirm(
        self,
        completed_workspace_id: str,
        plan: CorrectionPlan,
        *,
        confirmation_id: str,
        write_credential_binding_hash: str,
        write_identity: OdooWriteIdentity,
        actor: Actor,
    ) -> tuple[CorrectionConfirmation, CorrectionBinding]:
        self.authorization.require(
            actor, Capability.EXPORT_PLAN_APPROVE, project_id=plan.project_id
        )
        current = self._require_plan(completed_workspace_id, plan)
        confirmation = CorrectionPlanService.confirm(
            plan,
            confirmation_id=confirmation_id,
            write_credential_binding_hash=write_credential_binding_hash,
            write_identity=write_identity,
            confirmed_by=actor.identity,
            confirmed_at=datetime.now(UTC),
        )
        stored = self.protected_store.put_confirmation(plan, confirmation)
        reference = ProtectedCorrectionArtifactReference(
            artifact_id=stored.confirmation_id,
            logical_hash=stored.confirmation_hash,
            storage_key=stored.storage_key,
            artifact_hash=stored.artifact_hash,
        )
        published = self.bindings.publish_confirmation(
            completed_workspace_id,
            successor_workspace_id=plan.workspace_id,
            plan_id=plan.plan_id,
            plan_hash=plan.plan_hash,
            confirmation=reference,
            expected_revision=current.optimistic_revision,
            actor=actor,
        )
        return confirmation, published

    def execute(
        self,
        completed_workspace_id: str,
        plan: CorrectionPlan,
        confirmation: CorrectionConfirmation,
        *,
        target_database: str,
        write_credential_binding_hash: str,
        write_identity: OdooWriteIdentity,
        reader: OdooReadbackReader,
        writer: OdooWriteExecutor,
        actor: Actor,
    ) -> CorrectionExecutionResult:
        self.authorization.require(
            actor, Capability.EXPORT_PLAN_EXECUTE, project_id=plan.project_id
        )
        binding = self._require_confirmation(
            completed_workspace_id, plan, confirmation
        )
        confirmation.assert_current(
            plan,
            write_credential_binding_hash=write_credential_binding_hash,
            write_identity=write_identity,
        )
        snapshot = CorrectionExecutionSnapshot.create(
            plan, confirmation, target_database=target_database
        )
        scope = correction_api_scope(snapshot)
        if (
            reader.target_hash != snapshot.target_hash
            or writer.target_hash != snapshot.target_hash
            or reader.scope_hash != scope.semantic_hash
            or writer.scope_hash != scope.semantic_hash
        ):
            raise CorrectionPlanError(
                "Correction connection is not bound to the confirmed scope"
            )
        before = self._read_exact(snapshot, reader)
        if not self._confirmed_values_are_current(snapshot, before):
            self.bindings.invalidate_plan(
                completed_workspace_id,
                current_mapping_hash=binding.current_mapping_hash,
                current_prepared_hash=binding.current_prepared_hash,
                expected_revision=binding.optimistic_revision,
                actor=actor,
            )
            raise CorrectionPlanError(
                "Odoo changed after confirmation; review the correction again"
            )

        run = self._new_run(snapshot, actor)
        self.execution.start_run(
            snapshot.workspace_id,
            run,
            actor=actor,
            correction_plan_hash=plan.plan_hash,
        )
        rows = list(run.rows)
        stop_status: ExecutionRowStatus | None = None
        for batch_number, indexes in enumerate(self._write_batches(snapshot)):
            if stop_status is not None:
                break
            in_flight = tuple(
                replace(
                    rows[index],
                    status=ExecutionRowStatus.IN_FLIGHT,
                    attempt=rows[index].attempt + 1,
                    transport_page=batch_number,
                    transport_batch=batch_number,
                    transport_phase="UPDATE",
                )
                for index in indexes
            )
            self.execution.record_batch_started(
                snapshot.workspace_id, run.run_id, in_flight
            )
            for index, item in zip(indexes, in_flight, strict=True):
                rows[index] = item
            record = snapshot.records[indexes[0]]
            values = {
                field.target_field: _odoo_value(
                    field.value_kind,
                    field.corrected,
                )
                for field in record.fields
            }
            try:
                writer.update_rows(
                    record.target_model,
                    tuple(snapshot.records[index].odoo_id for index in indexes),
                    values,
                )
                outcome = ExecutionRowStatus.COMMITTED
                safe_error = ""
            except OdooWriteRejected:
                outcome = ExecutionRowStatus.FAILED
                safe_error = "Odoo rejected the correction batch"
                stop_status = outcome
            except OdooWriteOutcomeUnknown:
                outcome = ExecutionRowStatus.OUTCOME_UNKNOWN
                safe_error = "Odoo correction outcome is unknown"
                stop_status = outcome
            completed = tuple(
                replace(rows[index], status=outcome, safe_error=safe_error)
                for index in indexes
            )
            self.execution.record_outcomes(
                snapshot.workspace_id, run.run_id, completed
            )
            for index, item in zip(indexes, completed, strict=True):
                rows[index] = item

        remaining = tuple(
            replace(
                item,
                status=ExecutionRowStatus.BLOCKED,
                safe_error="Stopped after an earlier correction batch failed",
            )
            for item in rows
            if item.status is ExecutionRowStatus.PLANNED
        )
        if remaining:
            self.execution.record_outcomes(snapshot.workspace_id, run.run_id, remaining)
            by_id = {item.row_id: item for item in remaining}
            rows = [by_id.get(item.row_id, item) for item in rows]
        final_status = (
            ExecutionRunStatus.OUTCOME_UNKNOWN
            if any(item.status is ExecutionRowStatus.OUTCOME_UNKNOWN for item in rows)
            else (
                ExecutionRunStatus.COMPLETED_WITH_ERRORS
                if any(
                    item.status in {ExecutionRowStatus.FAILED, ExecutionRowStatus.BLOCKED}
                    for item in rows
                )
                else ExecutionRunStatus.COMPLETED
            )
        )
        finished = self.execution.finish_run(
            snapshot.workspace_id, run.run_id, final_status, actor=actor
        )
        report = self._reconcile(snapshot, finished, reader, actor)
        self.reconciliations.publish(snapshot.workspace_id, report, actor=actor)
        current = binding
        if report.status is ReconciliationRunStatus.VERIFIED:
            current = self.bindings.complete_verified_successor(
                completed_workspace_id,
                successor_migration_run_id=plan.successor_migration_run_id,
                successor_workspace_id=plan.workspace_id,
                execution_run_id=finished.run_id,
                reconciliation_id=report.reconciliation_id,
                reconciliation_hash=report.semantic_hash,
                expected_revision=binding.optimistic_revision,
                actor=actor,
            )
        return CorrectionExecutionResult(finished, report, current)

    def _require_plan(
        self, completed_workspace_id: str, plan: CorrectionPlan
    ) -> CorrectionBinding:
        current = self.bindings.get_for_completed_workspace(completed_workspace_id)
        if (
            current is None
            or current.successor_workspace_id != plan.workspace_id
            or current.successor_migration_run_id != plan.successor_migration_run_id
            or current.current_plan is None
            or current.current_plan.artifact_id != plan.plan_id
            or current.current_plan.logical_hash != plan.plan_hash
        ):
            raise CorrectionPlanError("Correction plan is no longer current")
        return current

    def _require_confirmation(
        self,
        completed_workspace_id: str,
        plan: CorrectionPlan,
        confirmation: CorrectionConfirmation,
    ) -> CorrectionBinding:
        current = self._require_plan(completed_workspace_id, plan)
        if (
            current.current_confirmation is None
            or current.current_confirmation.artifact_id
            != confirmation.confirmation_id
            or current.current_confirmation.logical_hash
            != confirmation.confirmation_hash
        ):
            raise CorrectionPlanError("Correction is not explicitly confirmed")
        return current

    @staticmethod
    def _read_exact(snapshot, reader):
        found: dict[tuple[str, int], Mapping[str, Any]] = {}
        by_model: dict[str, list[int]] = {}
        fields: dict[str, set[str]] = {}
        for record in snapshot.records:
            by_model.setdefault(record.target_model, []).append(record.odoo_id)
            fields.setdefault(record.target_model, set()).update(
                item.target_field for item in record.fields
            )
        for model in sorted(by_model):
            identifiers = by_model[model]
            for start in range(0, len(identifiers), MAX_READBACK_IDS):
                page = identifiers[start : start + MAX_READBACK_IDS]
                for item in reader.read_ids(model, page, tuple(sorted(fields[model]))):
                    found[(model, item.odoo_id)] = item.values
        return found

    @staticmethod
    def _confirmed_values_are_current(snapshot, found) -> bool:
        for record in snapshot.records:
            values = found.get((record.target_model, record.odoo_id))
            if values is None or any(
                field.target_field not in values
                or not odoo_correction_values_equal(
                    field.value_kind,
                    field.confirmed_current,
                    values[field.target_field],
                )
                for field in record.fields
            ):
                return False
        return True

    @staticmethod
    def _new_run(snapshot, actor) -> ExecutionRun:
        model_order = {
            model: index
            for index, model in enumerate(
                sorted({item.target_model for item in snapshot.records})
            )
        }
        rows = tuple(
            ExecutionRowAttempt(
                row_id=item.row_id,
                dataset=item.dataset,
                source_row=item.source_row,
                target_model=item.target_model,
                operation="UPDATE",
                field_names=tuple(field.target_field for field in item.fields),
                proposed_external_id="",
                odoo_id=item.odoo_id,
                schedule_component=model_order[item.target_model],
            )
            for item in snapshot.records
        )
        return ExecutionRun(
            run_id=str(uuid4()),
            workspace_id=snapshot.workspace_id,
            snapshot_hash=snapshot.semantic_hash,
            snapshot_root_hash=snapshot.root_hash,
            preflight_run_id=snapshot.snapshot_id,
            target_hash=snapshot.target_hash,
            target_database=snapshot.target_database,
            batch_rows=MAX_CREATE_BATCH_ROWS,
            status=ExecutionRunStatus.RUNNING,
            started_at=datetime.now(UTC),
            started_by=actor.identity.display_name,
            completed_at=None,
            rows=rows,
            write_credential_binding_hash=snapshot.write_credential_binding_hash,
            write_principal_hash=snapshot.write_principal_hash,
            write_permission_hash=snapshot.write_permission_hash,
            write_context_hash=snapshot.write_context_hash,
        )

    @staticmethod
    def _write_batches(snapshot):
        grouped: dict[tuple[str, str], list[int]] = {}
        for index, record in enumerate(snapshot.records):
            payload = {
                field.target_field: portable_value(field.corrected)
                for field in record.fields
            }
            grouped.setdefault(
                (record.target_model, canonical_json(payload)), []
            ).append(index)
        for key in sorted(grouped):
            indexes = grouped[key]
            for start in range(0, len(indexes), MAX_CREATE_BATCH_ROWS):
                yield tuple(indexes[start : start + MAX_CREATE_BATCH_ROWS])

    def _reconcile(self, snapshot, run, reader, actor):
        current = self._read_exact(snapshot, reader)
        attempts = {item.row_id: item for item in run.rows}
        rows = []
        for record in snapshot.records:
            attempt = attempts[record.row_id]
            values = current.get((record.target_model, record.odoo_id))
            differing = tuple(
                field.target_field
                for field in record.fields
                if values is None
                or field.target_field not in values
                or not odoo_correction_values_equal(
                    field.value_kind,
                    field.corrected,
                    values[field.target_field],
                )
            )
            if values is None:
                status = ReconciliationRowStatus.MISSING
            elif not differing:
                status = ReconciliationRowStatus.VERIFIED
            elif attempt.status is ExecutionRowStatus.FAILED:
                status = ReconciliationRowStatus.NOT_APPLIED
            elif attempt.status is ExecutionRowStatus.BLOCKED:
                status = ReconciliationRowStatus.NOT_WRITTEN
            elif attempt.status is ExecutionRowStatus.OUTCOME_UNKNOWN:
                unchanged = all(
                    field.target_field in values
                    and odoo_correction_values_equal(
                        field.value_kind,
                        field.confirmed_current,
                        values[field.target_field],
                    )
                    for field in record.fields
                )
                status = (
                    ReconciliationRowStatus.NOT_APPLIED
                    if unchanged
                    else ReconciliationRowStatus.OUTCOME_UNKNOWN
                )
            else:
                status = ReconciliationRowStatus.DIFFERENT
            rows.append(
                ReconciliationRow(
                    row_id=record.row_id,
                    dataset=record.dataset,
                    source_row=record.source_row,
                    target_model=record.target_model,
                    operation="UPDATE",
                    execution_status=attempt.status.value,
                    status=status,
                    odoo_id=record.odoo_id,
                    differing_fields=differing,
                    message=("Correction verified" if not differing else "Correction requires review"),
                    retry_safe=(
                        status is ReconciliationRowStatus.NOT_APPLIED
                    ),
                )
            )
        unknown = any(
            item.status is ReconciliationRowStatus.OUTCOME_UNKNOWN for item in rows
        )
        fallout = any(item.status is not ReconciliationRowStatus.VERIFIED for item in rows)
        return ReconciliationRun(
            reconciliation_id=str(uuid4()),
            workspace_id=snapshot.workspace_id,
            execution_run_id=run.run_id,
            snapshot_hash=snapshot.semantic_hash,
            target_hash=snapshot.target_hash,
            target_database=snapshot.target_database,
            status=(
                ReconciliationRunStatus.OUTCOME_UNKNOWN
                if unknown
                else ReconciliationRunStatus.FALLOUT
                if fallout
                else ReconciliationRunStatus.VERIFIED
            ),
            verified_at=datetime.now(UTC),
            verified_by=actor.identity.display_name,
            unchanged_count=0,
            rows=tuple(rows),
            verification_credential_binding_hash=(
                snapshot.write_credential_binding_hash
            ),
            verification_principal_hash=snapshot.write_principal_hash,
            verification_permission_hash=snapshot.write_permission_hash,
            verification_context_hash=snapshot.write_context_hash,
        )


def correction_api_scope(snapshot: CorrectionExecutionSnapshot) -> OdooApiScope:
    fields: dict[str, set[str]] = {}
    for record in snapshot.records:
        fields.setdefault(record.target_model, set()).update(
            item.target_field for item in record.fields
        )
    return OdooApiScope(
        preview_hash=snapshot.semantic_hash,
        models=tuple(
            OdooModelScope(
                model=model,
                write_fields=tuple(sorted(names)),
                read_fields=tuple(sorted(names)),
                lookup_fields=(),
            )
            for model, names in sorted(fields.items())
        ),
    )


def _odoo_value(kind, value: Any) -> Any:
    if kind.value == "MANY2ONE":
        if value is None:
            return False
        if type(value) is int and value > 0:
            return value
        raise WorkspaceError("A correction relationship identity is invalid")
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(UTC).replace(tzinfo=None)
        return value.isoformat(sep=" ")
    if isinstance(value, date):
        return value.isoformat()
    if value is None or type(value) in {str, int, bool, float}:
        return value
    raise WorkspaceError("A correction value is not supported by the Odoo API")


__all__ = [
    "CorrectionExecutionResult",
    "CorrectionExecutionService",
    "correction_api_scope",
]
