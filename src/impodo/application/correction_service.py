"""Review sparse correction intent against exact prior Odoo targets.

This application slice keeps numeric Odoo identifiers in protected in-memory
contracts.  It joins completed-load evidence without business-key fallback,
reads only A/C candidates in bounded exact-ID pages, and delegates the A/B/C
meaning to the pure correction domain.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from itertools import islice
from typing import Iterable, Mapping

from impodo.domain.correction import (
    CorrectionCandidate,
    CorrectionConfirmation,
    CorrectionFieldDecision,
    CorrectionFieldOutcome,
    CorrectionPlan,
    CorrectionPlanError,
    CorrectionPlanField,
    CorrectionValueKind,
    classify_correction_field,
)
from impodo.domain.correction_origin import CorrectionTargetIndexEntry
from impodo.domain.execution.models import (
    ExecutionRowStatus,
    ExecutionRun,
    ExecutionRunStatus,
)
from impodo.domain.execution.odoo_readback import (
    MAX_READBACK_IDS,
    OdooReadbackError,
    OdooReadbackReader,
)
from impodo.domain.execution_snapshot import ExecutionSnapshot
from impodo.domain.odoo.contracts import RecordSnapshot
from impodo.domain.reconciliation import (
    ReconciliationRowStatus,
    ReconciliationRun,
    ReconciliationRunStatus,
)
from impodo.domain.shared.access import ActorIdentity
from impodo.domain.shared.models import (
    OdooReadIdentity,
    OdooWriteIdentity,
    target_record_binding_hash,
)


class CorrectionReviewError(ValueError):
    """Raised when completed or current target evidence is unsafe."""


@dataclass(frozen=True, slots=True)
class CorrectionReviewBlocker:
    """One stable fail-closed reason that prevents all correction writes."""

    code: str
    dataset: str
    source_row: int
    target_model: str
    target_field: str = ""
    message: str = ""


@dataclass(frozen=True, slots=True)
class CorrectionReviewedField:
    """Protected A/B/C result bound to one exact numeric target."""

    target: CorrectionTargetIndexEntry
    decision: CorrectionFieldDecision


@dataclass(frozen=True, slots=True)
class CorrectionReview:
    """Current protected review; any blocker disables the whole apply."""

    target_hash: str
    fields: tuple[CorrectionReviewedField, ...]
    blockers: tuple[CorrectionReviewBlocker, ...]

    @property
    def ready_fields(self) -> tuple[CorrectionReviewedField, ...]:
        return tuple(item for item in self.fields if item.decision.writable)

    @property
    def already_corrected_count(self) -> int:
        return sum(
            item.decision.outcome is CorrectionFieldOutcome.ALREADY_CORRECTED
            for item in self.fields
        )

    @property
    def can_apply(self) -> bool:
        return bool(self.ready_fields) and not self.blockers


def build_completed_load_target_index(
    snapshot: ExecutionSnapshot,
    execution: ExecutionRun,
    reconciliation: ReconciliationRun,
    records: RecordSnapshot,
) -> tuple[CorrectionTargetIndexEntry, ...]:
    """Consolidate unchanged and written exact IDs without another lookup."""

    if (
        execution.status is not ExecutionRunStatus.COMPLETED
        or reconciliation.status is not ReconciliationRunStatus.VERIFIED
        or execution.workspace_id != snapshot.workspace_id
        or execution.snapshot_hash != snapshot.semantic_hash
        or execution.snapshot_root_hash != snapshot.root_hash
        or execution.preflight_run_id != snapshot.preflight_run_id
        or reconciliation.workspace_id != snapshot.workspace_id
        or reconciliation.execution_run_id != execution.run_id
        or reconciliation.snapshot_hash != snapshot.semantic_hash
        or execution.target_hash != snapshot.target_hash
        or reconciliation.target_hash != snapshot.target_hash
        or execution.target_database != snapshot.target_database
        or reconciliation.target_database != snapshot.target_database
        or records.fingerprint.target_hash != snapshot.target_hash
        or records.content_hash != snapshot.record_snapshot_hash
        or not records.complete
    ):
        raise CorrectionReviewError(
            "Completed load evidence is not eligible for correction"
        )
    write_rows = {
        row.row_id: row
        for row in snapshot.rows
        if row.disposition in {"CREATE", "UPDATE"}
    }
    attempts = {row.row_id: row for row in execution.rows}
    reconciled = {row.row_id: row for row in reconciliation.rows}
    if set(write_rows) != set(attempts) or set(write_rows) != set(reconciled):
        raise CorrectionReviewError(
            "Completed load write-row accounting is incomplete"
        )
    record_id_by_binding: dict[tuple[str, str], int] = {}
    for model, model_records in records.records.items():
        for record in model_records:
            binding = target_record_binding_hash(model, record.odoo_id)
            key = (model, binding)
            if key in record_id_by_binding:
                raise CorrectionReviewError(
                    "Completed load target snapshot contains duplicate identities"
                )
            record_id_by_binding[key] = record.odoo_id

    entries: list[CorrectionTargetIndexEntry] = []
    lineage: set[tuple[str, int, str]] = set()
    exact_targets: set[tuple[str, int]] = set()
    for row in snapshot.rows:
        if row.disposition == "UNCHANGED":
            identifier = record_id_by_binding.get(
                (row.target_model, row.target_binding_hash)
            )
            if not row.target_binding_hash or identifier is None:
                raise CorrectionReviewError(
                    "An unchanged row has no exact completed-load target"
                )
        elif row.disposition in {"CREATE", "UPDATE"}:
            attempt = attempts[row.row_id]
            outcome = reconciled[row.row_id]
            if (
                attempt.status is not ExecutionRowStatus.COMMITTED
                or outcome.status is not ReconciliationRowStatus.VERIFIED
                or attempt.odoo_id is None
                or outcome.odoo_id != attempt.odoo_id
                or attempt.dataset != row.dataset
                or attempt.source_row != row.source_row
                or attempt.target_model != row.target_model
                or outcome.dataset != row.dataset
                or outcome.source_row != row.source_row
                or outcome.target_model != row.target_model
            ):
                raise CorrectionReviewError(
                    "A written row has no verified completed-load target"
                )
            identifier = attempt.odoo_id
            if (
                row.disposition == "UPDATE"
                and (
                    not row.target_binding_hash
                    or target_record_binding_hash(row.target_model, identifier)
                    != row.target_binding_hash
                    or record_id_by_binding.get(
                        (row.target_model, row.target_binding_hash)
                    )
                    != identifier
                )
            ):
                raise CorrectionReviewError(
                    "A written row contradicts its reviewed target binding"
                )
        else:
            raise CorrectionReviewError(
                "Completed load contains a row that was not safely delivered"
            )
        entry = CorrectionTargetIndexEntry(
            dataset=row.dataset,
            source_row=row.source_row,
            row_id=row.row_id,
            target_model=row.target_model,
            odoo_id=identifier,
            completed_disposition=row.disposition,
            target_binding_hash=row.target_binding_hash,
        )
        if entry.lineage_key in lineage or (
            entry.target_model,
            entry.odoo_id,
        ) in exact_targets:
            raise CorrectionReviewError(
                "Completed load target index is ambiguous"
            )
        lineage.add(entry.lineage_key)
        exact_targets.add((entry.target_model, entry.odoo_id))
        entries.append(entry)
    if reconciliation.total_count != len(entries):
        raise CorrectionReviewError(
            "Completed load verification does not cover every source row"
        )
    return tuple(entries)


class CorrectionReviewService:
    """Read current Odoo values for sparse candidates by exact protected ID."""

    def review(
        self,
        candidate_batches: Iterable[tuple[CorrectionCandidate, ...]],
        target_index: Iterable[CorrectionTargetIndexEntry],
        *,
        reader: OdooReadbackReader,
        expected_target_hash: str,
        expected_reader_scope_hash: str,
    ) -> CorrectionReview:
        if (
            reader.target_hash != expected_target_hash
            or reader.scope_hash != expected_reader_scope_hash
        ):
            raise CorrectionReviewError(
                "Correction read capability does not match the reviewed target"
            )
        targets = {item.lineage_key: item for item in target_index}
        if len(targets) == 0:
            raise CorrectionReviewError("Correction target index is empty")
        reviewed: list[CorrectionReviewedField] = []
        blockers: list[CorrectionReviewBlocker] = []
        for candidate_batch in candidate_batches:
            scalar_candidates: list[
                tuple[CorrectionCandidate, CorrectionTargetIndexEntry]
            ] = []
            for candidate in candidate_batch:
                target = targets.get(
                    (
                        candidate.dataset,
                        candidate.source_row,
                        candidate.target_model,
                    )
                )
                if target is None:
                    blockers.append(
                        _blocker(
                            "MISSING_EXACT_TARGET",
                            candidate,
                            "The completed load has no exact target for this row",
                        )
                    )
                elif candidate.value_kind is not CorrectionValueKind.SCALAR:
                    blockers.append(
                        _blocker(
                            "RELATIONSHIP_NOT_QUALIFIED",
                            candidate,
                            "Relationship correction requires its separate "
                            "qualification",
                        )
                    )
                else:
                    scalar_candidates.append((candidate, target))
            reviewed_batch, batch_blockers = self._read_scalar_batch(
                scalar_candidates,
                reader,
            )
            reviewed.extend(reviewed_batch)
            blockers.extend(batch_blockers)
        return CorrectionReview(
            target_hash=expected_target_hash,
            fields=tuple(reviewed),
            blockers=tuple(blockers),
        )

    @staticmethod
    def _read_scalar_batch(
        candidates: list[tuple[CorrectionCandidate, CorrectionTargetIndexEntry]],
        reader: OdooReadbackReader,
    ) -> tuple[list[CorrectionReviewedField], list[CorrectionReviewBlocker]]:
        reviewed: list[CorrectionReviewedField] = []
        blockers: list[CorrectionReviewBlocker] = []
        by_model: dict[
            str,
            dict[int, list[tuple[CorrectionCandidate, CorrectionTargetIndexEntry]]],
        ] = {}
        for candidate, target in candidates:
            by_model.setdefault(target.target_model, {}).setdefault(
                target.odoo_id,
                [],
            ).append((candidate, target))
        for model in sorted(by_model):
            by_id = by_model[model]
            identifier_iterator = iter(sorted(by_id))
            while identifiers := tuple(islice(identifier_iterator, MAX_READBACK_IDS)):
                fields = tuple(
                    sorted(
                        {
                            candidate.target_field
                            for identifier in identifiers
                            for candidate, _target in by_id[identifier]
                        }
                    )
                )
                try:
                    current_records = reader.read_ids(model, identifiers, fields)
                except OdooReadbackError as error:
                    raise CorrectionReviewError(
                        "Current correction values could not be read safely"
                    ) from error
                current_by_id = {item.odoo_id: item for item in current_records}
                if (
                    len(current_by_id) != len(current_records)
                    or not set(current_by_id).issubset(identifiers)
                ):
                    raise CorrectionReviewError(
                        "Odoo returned ambiguous correction targets"
                    )
                for identifier in identifiers:
                    entries = by_id[identifier]
                    current_record = current_by_id.get(identifier)
                    if current_record is None:
                        blockers.extend(
                            _blocker(
                                "MISSING_OR_INACCESSIBLE_RECORD",
                                candidate,
                                "The exact completed-load target is unavailable",
                            )
                            for candidate, _target in entries
                        )
                        continue
                    for candidate, target in entries:
                        if candidate.target_field not in current_record.values:
                            blockers.append(
                                _blocker(
                                    "CURRENT_FIELD_UNAVAILABLE",
                                    candidate,
                                    "The affected Odoo field was not returned",
                                )
                            )
                            continue
                        current = current_record.values[candidate.target_field]
                        decision = classify_correction_field(
                            candidate,
                            current,
                            equal=odoo_scalar_values_equal,
                        )
                        reviewed.append(
                            CorrectionReviewedField(target=target, decision=decision)
                        )
                        if decision.outcome is CorrectionFieldOutcome.CONFLICT:
                            blockers.append(
                                _blocker(
                                    "CONCURRENT_FIELD_CHANGE",
                                    candidate,
                                    "Odoo changed independently after the "
                                    "completed load",
                                )
                            )
        return reviewed, blockers


class CorrectionPlanService:
    """Seal one blocker-free review and bind a separate write confirmation."""

    def create_plan(
        self,
        review: CorrectionReview,
        *,
        plan_id: str,
        project_id: str,
        completed_migration_run_id: str,
        successor_migration_run_id: str,
        workspace_id: str,
        origin_evidence_hash: str,
        previous_prepared_hash: str,
        corrected_prepared_hash: str,
        read_credential_binding_hash: str,
        read_identity: OdooReadIdentity,
        created_by: ActorIdentity,
        created_at: datetime,
    ) -> CorrectionPlan:
        """Create one deterministic protected plan without rehashing its inputs."""

        unsafe_outcomes = {
            CorrectionFieldOutcome.CONFLICT,
            CorrectionFieldOutcome.UNCHANGED_INTENT,
        }
        if (
            review.blockers
            or not review.ready_fields
            or review.target_hash != read_identity.target_hash
            or any(
                item.decision.outcome in unsafe_outcomes for item in review.fields
            )
        ):
            raise CorrectionPlanError(
                "Correction review is not eligible for plan publication"
            )
        fields: list[CorrectionPlanField] = []
        for reviewed in review.ready_fields:
            target = reviewed.target
            candidate = reviewed.decision.candidate
            if (
                candidate.dataset != target.dataset
                or candidate.source_row != target.source_row
                or candidate.target_model != target.target_model
                or candidate.value_kind is not CorrectionValueKind.SCALAR
            ):
                raise CorrectionPlanError(
                    "Correction review field does not match its exact target"
                )
            fields.append(
                CorrectionPlanField(
                    dataset=target.dataset,
                    source_row=target.source_row,
                    row_id=target.row_id,
                    target_model=target.target_model,
                    odoo_id=target.odoo_id,
                    completed_disposition=target.completed_disposition,
                    target_binding_hash=target.target_binding_hash,
                    target_field=candidate.target_field,
                    value_kind=candidate.value_kind,
                    previous=candidate.previous,
                    current=reviewed.decision.current,
                    corrected=candidate.corrected,
                )
            )
        return CorrectionPlan.create(
            plan_id=plan_id,
            project_id=project_id,
            completed_migration_run_id=completed_migration_run_id,
            successor_migration_run_id=successor_migration_run_id,
            workspace_id=workspace_id,
            origin_evidence_hash=origin_evidence_hash,
            previous_prepared_hash=previous_prepared_hash,
            corrected_prepared_hash=corrected_prepared_hash,
            read_credential_binding_hash=read_credential_binding_hash,
            read_identity=read_identity,
            fields=fields,
            created_by=created_by,
            created_at=created_at,
        )

    @staticmethod
    def confirm(
        plan: CorrectionPlan,
        *,
        confirmation_id: str,
        write_credential_binding_hash: str,
        write_identity: OdooWriteIdentity,
        confirmed_by: ActorIdentity,
        confirmed_at: datetime,
    ) -> CorrectionConfirmation:
        """Bind explicit confirmation to a freshly probed write capability."""

        return CorrectionConfirmation.create(
            confirmation_id=confirmation_id,
            plan=plan,
            write_credential_binding_hash=write_credential_binding_hash,
            write_identity=write_identity,
            confirmed_by=confirmed_by,
            confirmed_at=confirmed_at,
        )


def _blocker(
    code: str,
    candidate: CorrectionCandidate,
    message: str,
) -> CorrectionReviewBlocker:
    return CorrectionReviewBlocker(
        code=code,
        dataset=candidate.dataset,
        source_row=candidate.source_row,
        target_model=candidate.target_model,
        target_field=candidate.target_field,
        message=message,
    )


def odoo_scalar_values_equal(expected: object, actual: object) -> bool:
    """Compare canonical prepared values with Odoo's documented wire shapes."""

    if expected is None:
        return actual is None or actual is False or actual == ""
    if type(expected) is bool:
        return actual is expected
    if isinstance(expected, datetime):
        try:
            parsed = (
                actual
                if isinstance(actual, datetime)
                else datetime.fromisoformat(str(actual).replace("Z", "+00:00"))
            )
        except (TypeError, ValueError):
            return False
        return _utc_naive(expected) == _utc_naive(parsed)
    if isinstance(expected, date):
        try:
            parsed_date = (
                actual if isinstance(actual, date) else date.fromisoformat(str(actual))
            )
        except (TypeError, ValueError):
            return False
        return expected == parsed_date
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


__all__ = [
    "CorrectionPlanService",
    "CorrectionReview",
    "CorrectionReviewBlocker",
    "CorrectionReviewError",
    "CorrectionReviewService",
    "CorrectionReviewedField",
    "CorrectionTargetIndexEntry",
    "build_completed_load_target_index",
    "odoo_scalar_values_equal",
]
