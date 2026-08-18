"""Read completed practical loads back from Odoo and classify fallout."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Protocol
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
from ..models import BusinessReference, LogicalReference, OdooWriteIdentity
from ..odoo_readback import (
    MAX_READBACK_IDS,
    MAX_READBACK_LOOKUPS,
    OdooReadbackReader,
    ReadbackLookup,
    ReadbackRecord,
)
from ..workspace_errors import WorkspaceError
from .execution_service import _identity_domain, _portable_key, execution_api_scope
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
        write_identity: OdooWriteIdentity | None = None,
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
        _require_matching_write_identity(run, write_identity)
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
        if reader.scope_hash != execution_api_scope(snapshot).semantic_hash:
            raise WorkspaceError(
                "Verification is not bound to this reviewed load preview"
            )
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
        external_id_issues = self._external_id_issues(
            rows,
            attempts,
            resolved_ids,
            reader,
        )

        identity_cache = self._preload_reference_ids(
            rows,
            actual_by_row,
            metadata,
            by_source,
            resolved_ids,
            reader,
        )
        outcomes = []
        for dataset in sorted(snapshot.datasets, key=lambda item: item.sequence):
            dataset_rows = sorted(
                (row for row in rows.values() if row.dataset == dataset.dataset),
                key=lambda item: item.source_row,
            )
            for row in dataset_rows:
                outcome = self._row_outcome(
                    row,
                    attempts[row.row_id],
                    actual_by_row.get(row.row_id),
                    uncertain_matches.get(row.row_id),
                    metadata,
                    by_source,
                    resolved_ids,
                    identity_cache,
                )
                issue = external_id_issues.get(row.row_id)
                if (
                    issue is not None
                    and outcome.status is ReconciliationRowStatus.VERIFIED
                ):
                    outcome = replace(
                        outcome,
                        status=ReconciliationRowStatus.DIFFERENT,
                        differing_fields=("External ID",),
                        message=issue,
                    )
                outcomes.append(outcome)

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
            if (
                attempt.status is ExecutionRowStatus.COMMITTED
                and attempt.odoo_id is None
            ):
                raise WorkspaceError("A committed load row has no Odoo record")
            if attempt.odoo_id is not None:
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
        by_model: dict[str, list[tuple[str, ReadbackLookup]]] = {}
        for row_id, attempt in attempts.items():
            if (
                attempt.status is not ExecutionRowStatus.OUTCOME_UNKNOWN
                or attempt.odoo_id is not None
            ):
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
            by_model.setdefault(row.target_model, []).append(
                (
                    row_id,
                    ReadbackLookup(domain=domain, fields=fields),
                )
            )
        matches = {}
        for model, model_lookups in by_model.items():
            for start in range(0, len(model_lookups), MAX_READBACK_LOOKUPS):
                batch = model_lookups[start : start + MAX_READBACK_LOOKUPS]
                results = reader.find_records_many(
                    model,
                    tuple(lookup for _row_id, lookup in batch),
                )
                if len(results) != len(batch):
                    raise WorkspaceError(
                        "Odoo verification returned incomplete key results"
                    )
                for (row_id, _lookup), row_matches in zip(
                    batch,
                    results,
                    strict=True,
                ):
                    matches[row_id] = row_matches
        return matches

    def _preload_reference_ids(
        self,
        rows: Mapping[str, ExecutionRow],
        actual_by_row: Mapping[str, ReadbackRecord],
        metadata: Mapping[str, ExecutionDataset],
        by_source: Mapping[tuple[str, str], ExecutionRow],
        resolved_ids: Mapping[str, int],
        reader: OdooReadbackReader,
    ) -> dict[tuple[str, tuple[tuple[str, str, Any], ...]], int | None]:
        """Resolve every relationship key in bounded model batches."""

        requested: dict[
            str,
            dict[tuple[tuple[str, str, Any], ...], ReadbackLookup],
        ] = {}
        for row_id, row in rows.items():
            if row_id not in actual_by_row:
                continue
            for intent in row.fields:
                if intent.kind == "scalar" or intent.action != "SET_VALUE":
                    continue
                values = intent.value if isinstance(intent.value, tuple) else (
                    intent.value,
                )
                for value in values:
                    try:
                        lookup = self._reference_lookup(
                            value,
                            intent,
                            metadata,
                            by_source,
                            resolved_ids,
                        )
                    except WorkspaceError:
                        continue
                    if lookup is None:
                        continue
                    model, domain = lookup
                    requested.setdefault(model, {})[domain] = ReadbackLookup(
                        domain=domain
                    )

        cache: dict[
            tuple[str, tuple[tuple[str, str, Any], ...]],
            int | None,
        ] = {}
        for model, lookups_by_domain in requested.items():
            items = tuple(lookups_by_domain.items())
            for start in range(0, len(items), MAX_READBACK_LOOKUPS):
                batch = items[start : start + MAX_READBACK_LOOKUPS]
                results = reader.find_records_many(
                    model,
                    tuple(lookup for _domain, lookup in batch),
                )
                if len(results) != len(batch):
                    raise WorkspaceError(
                        "Odoo verification returned incomplete key results"
                    )
                for (domain, _lookup), matches in zip(
                    batch,
                    results,
                    strict=True,
                ):
                    cache[(model, domain)] = (
                        matches[0].odoo_id if len(matches) == 1 else None
                    )
        return cache

    @staticmethod
    def _reference_lookup(
        value: object,
        intent: FieldIntent,
        metadata: Mapping[str, ExecutionDataset],
        by_source: Mapping[tuple[str, str], ExecutionRow],
        resolved_ids: Mapping[str, int],
    ) -> tuple[str, tuple[tuple[str, str, Any], ...]] | None:
        if isinstance(value, LogicalReference) and value.origin == "incoming":
            if value.dataset is None:
                raise WorkspaceError("An incoming relationship is incomplete")
            related = by_source.get((value.dataset, _portable_key(value.key)))
            if related is None:
                raise WorkspaceError("A related prepared row could not be found")
            if related.row_id in resolved_ids:
                return None
            dataset = metadata[related.dataset]
            return (
                related.target_model,
                _identity_domain(
                    dataset.identity_fields,
                    related.business_identity,
                    dataset.scope_fields,
                    related.business_scope,
                ),
            )
        if isinstance(value, BusinessReference | LogicalReference):
            return (
                intent.related_model,
                _identity_domain(
                    intent.related_identity_fields,
                    value.key,
                    intent.related_scope_fields,
                    value.scope,
                ),
            )
        raise WorkspaceError("A relationship is not expressed by a business key")

    @staticmethod
    def _external_id_issues(
        rows: Mapping[str, ExecutionRow],
        attempts: Mapping[str, ExecutionRowAttempt],
        resolved_ids: Mapping[str, int],
        reader: OdooReadbackReader,
    ) -> dict[str, str]:
        if not reader.imports_external_ids:
            return {}

        expected: dict[str, tuple[str, str, int]] = {}
        for row_id, row in rows.items():
            attempt = attempts[row_id]
            resolved_id = resolved_ids.get(row_id)
            if (
                row.disposition == "CREATE"
                and attempt.status
                in {
                    ExecutionRowStatus.COMMITTED,
                    ExecutionRowStatus.PARTIALLY_APPLIED,
                    ExecutionRowStatus.OUTCOME_UNKNOWN,
                }
                and resolved_id is not None
            ):
                expected[row.proposed_external_id] = (
                    row_id,
                    row.target_model,
                    resolved_id,
                )

        bindings = {}
        external_ids = tuple(expected)
        for start in range(0, len(external_ids), MAX_READBACK_IDS):
            for binding in reader.read_external_ids(
                external_ids[start : start + MAX_READBACK_IDS]
            ):
                bindings[binding.external_id] = binding

        issues = {}
        for external_id, (row_id, model, odoo_id) in expected.items():
            binding = bindings.get(external_id)
            if binding is None:
                issues[row_id] = (
                    "Odoo matches the load preview, but its expected External ID "
                    "is missing"
                )
            elif binding.model != model or binding.odoo_id != odoo_id:
                issues[row_id] = (
                    "Odoo matches the load preview, but its External ID points "
                    "to another model or record"
                )
        return issues

    def _row_outcome(
        self,
        row: ExecutionRow,
        attempt: ExecutionRowAttempt,
        actual: ReadbackRecord | None,
        uncertain_matches: tuple[ReadbackRecord, ...] | None,
        metadata: Mapping[str, ExecutionDataset],
        by_source: Mapping[tuple[str, str], ExecutionRow],
        resolved_ids: Mapping[str, int],
        identity_cache: dict[
            tuple[str, tuple[tuple[str, str, Any], ...]],
            int | None,
        ],
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
        if (
            attempt.status in {ExecutionRowStatus.FAILED, ExecutionRowStatus.BLOCKED}
            and attempt.odoo_id is None
        ):
            return ReconciliationRow(
                **common,
                status=ReconciliationRowStatus.NOT_WRITTEN,
                message=attempt.safe_error or "Odoo did not receive this row",
            )
        if (
            attempt.status is ExecutionRowStatus.OUTCOME_UNKNOWN
            and actual is None
            and attempt.odoo_id is None
        ):
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
                )
                actual_value = actual.values[intent.field]
                if intent.kind != "scalar" and intent.action == "SET_VALUE":
                    actual_value = (
                        _many2many_ids(actual_value)
                        if isinstance(expected, tuple)
                        else _many2one_id(actual_value)
                    )
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
        identity_cache: dict[
            tuple[str, tuple[tuple[str, str, Any], ...]],
            int | None,
        ],
    ) -> Any:
        if intent.action == "SET_NULL":
            return None
        if intent.kind == "scalar":
            return intent.value
        value = intent.value
        if isinstance(value, tuple):
            identifiers = tuple(
                self._expected_reference_id(
                    item,
                    intent,
                    metadata,
                    by_source,
                    resolved_ids,
                    identity_cache,
                )
                for item in value
            )
            return () if intent.relation_operation == "remove" else tuple(
                sorted(set(identifiers))
            )
        return self._expected_reference_id(
            value,
            intent,
            metadata,
            by_source,
            resolved_ids,
            identity_cache,
        )

    def _expected_reference_id(
        self,
        value: object,
        intent: FieldIntent,
        metadata: Mapping[str, ExecutionDataset],
        by_source: Mapping[tuple[str, str], ExecutionRow],
        resolved_ids: Mapping[str, int],
        identity_cache: dict[
            tuple[str, tuple[tuple[str, str, Any], ...]],
            int | None,
        ],
    ) -> int:
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
            )
        raise WorkspaceError("A relationship is not expressed by a business key")

    @staticmethod
    def _find_unique(
        model: str,
        domain: tuple[tuple[str, str, Any], ...],
        cache: dict[
            tuple[str, tuple[tuple[str, str, Any], ...]],
            int | None,
        ],
    ) -> int:
        cache_key = (model, domain)
        identifier = cache.get(cache_key)
        if identifier is None:
            raise WorkspaceError(
                "A related Odoo business key no longer matches one record"
            )
        return identifier


def _require_matching_write_identity(
    run: ExecutionRun,
    identity: OdooWriteIdentity | None,
) -> None:
    """Reject read-back under a changed execution principal or context."""

    expected = (
        run.write_principal_hash,
        run.write_permission_hash,
        run.write_context_hash,
    )
    if not any(expected):
        return
    if identity is None:
        raise WorkspaceError(
            "Re-probe the approved write principal before verification"
        )
    actual = (
        identity.principal_hash,
        identity.permission_hash,
        identity.context_hash,
    )
    if actual != expected or identity.target_hash != run.target_hash:
        raise WorkspaceError(
            "The write principal, permission, or context changed after execution"
        )


def _many2one_id(value: Any) -> int | None:
    if value is None or value is False:
        return None
    if type(value) is int and value > 0:
        return value
    if isinstance(value, (tuple, list)) and value and type(value[0]) is int:
        return int(value[0])
    return None


def _many2many_ids(value: Any) -> tuple[int, ...]:
    if value is None or value is False:
        return ()
    if not isinstance(value, (tuple, list)) or any(
        type(item) is not int or item <= 0 for item in value
    ):
        raise WorkspaceError("Odoo returned an invalid many-to-many value")
    return tuple(sorted(set(value)))


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
