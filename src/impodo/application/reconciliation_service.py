"""Read completed practical loads back from Odoo and classify fallout."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Protocol, Sequence
from uuid import uuid4

from ..access import Actor, AuthorizationPolicy, Capability
from ..domain.execution import (
    ExecutionRowAttempt,
    ExecutionRowStatus,
    ExecutionRun,
    ExecutionRunStatus,
)
from ..domain.execution_snapshot import (
    ExecutionDataset,
    ExecutionRow,
    ExecutionSnapshot,
    FieldIntent,
)
from ..domain.reconciliation import (
    ReconciliationRow,
    ReconciliationRowStatus,
    ReconciliationRun,
    ReconciliationRunStatus,
)
from ..models import BusinessReference, LogicalReference
from ..odoo_readback import MAX_READBACK_IDS, OdooReadbackReader, ReadbackRecord
from ..workspace_errors import WorkspaceError
from .execution_service import _identity_domain, _portable_key
from .preflight_service import PreflightService


class ReconciliationExecutionRepository(Protocol):
    def get_current_run(
        self,
        project_id: str,
        snapshot_hash: str | None = None,
    ) -> ExecutionRun | None: ...

    def get_run(self, project_id: str, run_id: str) -> ExecutionRun | None: ...


class ReconciliationResultRepository(Protocol):
    def get_current(
        self,
        project_id: str,
        execution_run_id: str | None = None,
    ) -> ReconciliationRun | None: ...

    def publish(
        self,
        project_id: str,
        report: ReconciliationRun,
        *,
        actor: Actor,
    ) -> None: ...


@dataclass(slots=True)
class ReconciliationService:
    """Bind a read-back result to one immutable execution journal."""

    preflight: PreflightService
    execution: ReconciliationExecutionRepository
    results: ReconciliationResultRepository
    authorization: AuthorizationPolicy

    def current(self, project_id: str) -> ReconciliationRun | None:
        run = self.execution.get_current_run(project_id)
        if run is None:
            return None
        return self.results.get_current(project_id, run.run_id)

    def reconcile(
        self,
        project_id: str,
        *,
        expected_execution_run_id: str,
        reader: OdooReadbackReader,
        actor: Actor,
    ) -> ReconciliationRun:
        self.authorization.require(
            actor,
            Capability.EXPORT_PLAN_EXECUTE,
            project_id=project_id,
        )
        run = self.execution.get_run(project_id, expected_execution_run_id)
        current = self.execution.get_current_run(project_id)
        if run is None or current is None or current.run_id != expected_execution_run_id:
            raise WorkspaceError("The saved load outcome is no longer current")
        existing = self.results.get_current(project_id, run.run_id)
        if existing is not None:
            return existing
        if run.status is ExecutionRunStatus.RUNNING or run.planned_count:
            raise WorkspaceError("The Odoo load is not finished yet")
        snapshot = self.preflight.execution_snapshot(
            project_id,
            run.preflight_run_id,
        )
        if (
            snapshot.semantic_hash != run.snapshot_hash
            or snapshot.root_hash != run.snapshot_root_hash
            or snapshot.target_hash != run.target_hash
            or snapshot.target_database != run.target_database
            or reader.target_hash != run.target_hash
        ):
            raise WorkspaceError("The verification connection or preview changed")

        report = self._read_back(run, snapshot, reader, actor)
        # Exercise the portable contract before it reaches durable storage.
        report = ReconciliationRun.from_json(report.to_json())
        self.results.publish(project_id, report, actor=actor)
        return report

    def _read_back(
        self,
        run: ExecutionRun,
        snapshot: ExecutionSnapshot,
        reader: OdooReadbackReader,
        actor: Actor,
    ) -> ReconciliationRun:
        rows = {
            row.row_id: row
            for row in snapshot.rows
            if row.disposition in {"CREATE", "UPDATE"}
        }
        attempts = {item.row_id: item for item in run.rows}
        if set(rows) != set(attempts):
            raise WorkspaceError("The saved load rows do not match the preview")
        metadata = {item.dataset: item for item in snapshot.datasets}
        by_source = {
            (row.dataset, _portable_key(row.source_identity)): row
            for row in snapshot.rows
        }

        actual_by_row: dict[str, ReadbackRecord] = {}
        resolved_ids: dict[str, int] = {
            item.row_id: item.odoo_id
            for item in run.rows
            if item.odoo_id is not None
        }
        self._read_committed(rows, attempts, actual_by_row, reader)
        uncertain_matches = self._match_uncertain(
            rows,
            attempts,
            metadata,
            reader,
        )
        for row_id, matches in uncertain_matches.items():
            if len(matches) == 1:
                actual_by_row[row_id] = matches[0]
                resolved_ids[row_id] = matches[0].odoo_id

        identity_cache: dict[tuple[str, tuple[tuple[str, str, Any], ...]], int] = {}
        outcomes = []
        for dataset in sorted(snapshot.datasets, key=lambda item: item.sequence):
            dataset_rows = sorted(
                (row for row in rows.values() if row.dataset == dataset.dataset),
                key=lambda item: item.source_row,
            )
            for row in dataset_rows:
                outcomes.append(
                    self._row_outcome(
                        row,
                        attempts[row.row_id],
                        actual_by_row.get(row.row_id),
                        uncertain_matches.get(row.row_id),
                        metadata,
                        by_source,
                        resolved_ids,
                        identity_cache,
                        reader,
                    )
                )

        outcome_rows = tuple(outcomes)
        status = (
            ReconciliationRunStatus.OUTCOME_UNKNOWN
            if any(
                item.status is ReconciliationRowStatus.OUTCOME_UNKNOWN
                for item in outcome_rows
            )
            else (
                ReconciliationRunStatus.FALLOUT
                if any(
                    item.status is not ReconciliationRowStatus.VERIFIED
                    for item in outcome_rows
                )
                else ReconciliationRunStatus.VERIFIED
            )
        )
        return ReconciliationRun(
            reconciliation_id=str(uuid4()),
            project_id=run.project_id,
            execution_run_id=run.run_id,
            snapshot_hash=run.snapshot_hash,
            target_hash=run.target_hash,
            target_database=run.target_database,
            status=status,
            verified_at=datetime.now(timezone.utc),
            verified_by=actor.identity.display_name,
            unchanged_count=int(snapshot.counts.get("UNCHANGED", 0)),
            rows=outcome_rows,
        )

    @staticmethod
    def _read_committed(
        rows: Mapping[str, ExecutionRow],
        attempts: Mapping[str, ExecutionRowAttempt],
        actual_by_row: dict[str, ReadbackRecord],
        reader: OdooReadbackReader,
    ) -> None:
        by_model: dict[str, list[ExecutionRow]] = {}
        for row_id, attempt in attempts.items():
            if attempt.status is ExecutionRowStatus.COMMITTED:
                if attempt.odoo_id is None:
                    raise WorkspaceError("A committed load row has no Odoo record")
                by_model.setdefault(attempt.target_model, []).append(rows[row_id])
        for model, model_rows in by_model.items():
            identifiers = {
                attempts[row.row_id].odoo_id: row.row_id for row in model_rows
            }
            if len(identifiers) != len(model_rows):
                raise WorkspaceError("Load rows refer to the same Odoo record")
            fields = tuple(
                sorted(
                    {
                        intent.field
                        for row in model_rows
                        for intent in row.fields
                        if intent.action != "OMIT"
                    }
                )
            )
            ids = tuple(identifier for identifier in identifiers if identifier)
            for start in range(0, len(ids), MAX_READBACK_IDS):
                records = reader.read_ids(
                    model,
                    ids[start : start + MAX_READBACK_IDS],
                    fields,
                )
                for record in records:
                    row_id = identifiers.get(record.odoo_id)
                    if row_id is not None:
                        actual_by_row[row_id] = record

    @staticmethod
    def _match_uncertain(
        rows: Mapping[str, ExecutionRow],
        attempts: Mapping[str, ExecutionRowAttempt],
        metadata: Mapping[str, ExecutionDataset],
        reader: OdooReadbackReader,
    ) -> dict[str, tuple[ReadbackRecord, ...]]:
        matches = {}
        for row_id, attempt in attempts.items():
            if attempt.status is not ExecutionRowStatus.OUTCOME_UNKNOWN:
                continue
            row = rows[row_id]
            dataset = metadata[row.dataset]
            domain = _identity_domain(
                dataset.identity_fields,
                row.business_identity,
                dataset.scope_fields,
                row.business_scope,
            )
            fields = tuple(
                intent.field for intent in row.fields if intent.action != "OMIT"
            )
            matches[row_id] = reader.find_records(row.target_model, domain, fields)
        return matches

    def _row_outcome(
        self,
        row: ExecutionRow,
        attempt: ExecutionRowAttempt,
        actual: ReadbackRecord | None,
        uncertain_matches: tuple[ReadbackRecord, ...] | None,
        metadata: Mapping[str, ExecutionDataset],
        by_source: Mapping[tuple[str, str], ExecutionRow],
        resolved_ids: Mapping[str, int],
        identity_cache: dict[tuple[str, tuple[tuple[str, str, Any], ...]], int],
        reader: OdooReadbackReader,
    ) -> ReconciliationRow:
        common = dict(
            row_id=row.row_id,
            dataset=row.dataset,
            source_row=row.source_row,
            target_model=row.target_model,
            operation=row.disposition,
            execution_status=attempt.status.value,
            odoo_id=actual.odoo_id if actual is not None else attempt.odoo_id,
        )
        if attempt.status in {ExecutionRowStatus.FAILED, ExecutionRowStatus.BLOCKED}:
            return ReconciliationRow(
                **common,
                status=ReconciliationRowStatus.NOT_WRITTEN,
                message=attempt.safe_error or "Odoo did not receive this row",
            )
        if attempt.status is ExecutionRowStatus.OUTCOME_UNKNOWN and actual is None:
            if uncertain_matches is not None and len(uncertain_matches) > 1:
                message = "The business key now matches more than one Odoo record"
            elif row.disposition == "CREATE":
                return ReconciliationRow(
                    **common,
                    status=ReconciliationRowStatus.NOT_APPLIED,
                    message=(
                        "No matching Odoo record was found. A fresh comparison "
                        "may safely plan it again."
                    ),
                    retry_safe=True,
                )
            else:
                message = "The uncertain update could not be matched in Odoo"
            return ReconciliationRow(
                **common,
                status=ReconciliationRowStatus.OUTCOME_UNKNOWN,
                message=message,
            )
        if actual is None:
            return ReconciliationRow(
                **common,
                status=ReconciliationRowStatus.MISSING,
                message="Odoo accepted the write but the record is now missing",
            )

        differing = []
        try:
            for intent in row.fields:
                if intent.action == "OMIT":
                    continue
                expected = self._expected_value(
                    intent,
                    metadata,
                    by_source,
                    resolved_ids,
                    identity_cache,
                    reader,
                )
                actual_value = actual.values[intent.field]
                if intent.kind != "scalar" and intent.action == "SET_VALUE":
                    actual_value = _many2one_id(actual_value)
                if not _values_equal(expected, actual_value):
                    differing.append(intent.field)
        except (KeyError, WorkspaceError) as error:
            return ReconciliationRow(
                **common,
                status=ReconciliationRowStatus.OUTCOME_UNKNOWN,
                message=str(error),
            )
        if differing:
            return ReconciliationRow(
                **common,
                status=ReconciliationRowStatus.DIFFERENT,
                differing_fields=tuple(sorted(differing)),
                message="Odoo differs from the confirmed load preview",
            )
        return ReconciliationRow(
            **common,
            status=ReconciliationRowStatus.VERIFIED,
            message=(
                "The uncertain response was resolved by its business key"
                if attempt.status is ExecutionRowStatus.OUTCOME_UNKNOWN
                else "Odoo matches the confirmed load preview"
            ),
        )

    def _expected_value(
        self,
        intent: FieldIntent,
        metadata: Mapping[str, ExecutionDataset],
        by_source: Mapping[tuple[str, str], ExecutionRow],
        resolved_ids: Mapping[str, int],
        identity_cache: dict[tuple[str, tuple[tuple[str, str, Any], ...]], int],
        reader: OdooReadbackReader,
    ) -> Any:
        if intent.action == "SET_NULL":
            return None
        if intent.kind == "scalar":
            return intent.value
        value = intent.value
        if isinstance(value, LogicalReference) and value.origin == "incoming":
            if value.dataset is None:
                raise WorkspaceError("An incoming relationship is incomplete")
            related = by_source.get((value.dataset, _portable_key(value.key)))
            if related is None:
                raise WorkspaceError("A related prepared row could not be found")
            if related.row_id in resolved_ids:
                return resolved_ids[related.row_id]
            dataset = metadata[related.dataset]
            domain = _identity_domain(
                dataset.identity_fields,
                related.business_identity,
                dataset.scope_fields,
                related.business_scope,
            )
            return self._find_unique(
                related.target_model,
                domain,
                identity_cache,
                reader,
            )
        if isinstance(value, BusinessReference | LogicalReference):
            domain = _identity_domain(
                intent.related_identity_fields,
                value.key,
                intent.related_scope_fields,
                value.scope,
            )
            return self._find_unique(
                intent.related_model,
                domain,
                identity_cache,
                reader,
            )
        raise WorkspaceError("A relationship is not expressed by a business key")

    @staticmethod
    def _find_unique(
        model: str,
        domain: tuple[tuple[str, str, Any], ...],
        cache: dict[tuple[str, tuple[tuple[str, str, Any], ...]], int],
        reader: OdooReadbackReader,
    ) -> int:
        cache_key = (model, domain)
        if cache_key not in cache:
            records = reader.find_records(model, domain, ())
            if len(records) != 1:
                raise WorkspaceError(
                    "A related Odoo business key no longer matches one record"
                )
            cache[cache_key] = records[0].odoo_id
        return cache[cache_key]


def _many2one_id(value: Any) -> int | None:
    if value is None or value is False:
        return None
    if type(value) is int and value > 0:
        return value
    if isinstance(value, (tuple, list)) and value and type(value[0]) is int:
        return int(value[0])
    return None


def _values_equal(expected: Any, actual: Any) -> bool:
    if expected is None:
        return actual is None or actual is False or actual == ""
    if type(expected) is bool:
        return actual is expected
    if isinstance(expected, datetime):
        try:
            actual_datetime = (
                actual if isinstance(actual, datetime) else datetime.fromisoformat(str(actual))
            )
        except (TypeError, ValueError):
            return False
        return _utc_naive(expected) == _utc_naive(actual_datetime)
    if isinstance(expected, date):
        try:
            actual_date = actual if isinstance(actual, date) else date.fromisoformat(str(actual))
        except (TypeError, ValueError):
            return False
        return expected == actual_date
    if isinstance(expected, (Decimal, int, float)) and type(expected) is not bool:
        try:
            return Decimal(str(expected)) == Decimal(str(actual))
        except (InvalidOperation, ValueError):
            return False
    if expected == "" and actual is False:
        return True
    return expected == actual


def _utc_naive(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)
