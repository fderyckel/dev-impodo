"""Orchestrate one confirmed schema-bound load from the current snapshot."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from decimal import Decimal
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
from ..models import (
    BusinessReference,
    LogicalReference,
    canonical_json_text,
    portable_value,
)
from ..odoo_scope import OdooApiScope, OdooModelScope
from ..odoo_writer import (
    MAX_CREATE_BATCH_ROWS,
    OdooWriteExecutor,
    OdooWriteOutcomeUnknown,
    OdooWriteRejected,
)
from ..projects import MigrationProject, OdooConnectionMode
from ..workspace_errors import WorkspaceError
from .preflight_service import PreflightService


class ExecutionProjectRepository(Protocol):
    def get(self, project_id: str) -> MigrationProject: ...


class ExecutionJournalRepository(Protocol):
    def start_run(
        self, project_id: str, run: ExecutionRun, *, actor: Actor
    ) -> None: ...

    def record_outcomes(
        self,
        project_id: str,
        run_id: str,
        rows: Sequence[ExecutionRowAttempt],
    ) -> None: ...

    def finish_run(
        self,
        project_id: str,
        run_id: str,
        status: ExecutionRunStatus,
        *,
        actor: Actor,
    ) -> ExecutionRun: ...

    def get_current_run(
        self,
        project_id: str,
        snapshot_hash: str | None = None,
    ) -> ExecutionRun | None: ...


@dataclass(frozen=True, slots=True)
class ExecutionDatasetPreview:
    dataset: str
    target_model: str
    create_count: int
    update_count: int
    unchanged_count: int


@dataclass(frozen=True, slots=True)
class ExecutionPreview:
    snapshot: ExecutionSnapshot
    datasets: tuple[ExecutionDatasetPreview, ...]
    current_run: ExecutionRun | None
    api_scope: OdooApiScope
    scope_error: str = ""

    @property
    def can_load(self) -> bool:
        return (
            self.snapshot.write_count > 0
            and int(self.snapshot.counts.get("BLOCKED", 0)) == 0
            and int(self.snapshot.counts.get("AMBIGUOUS", 0)) == 0
            and self.current_run is None
            and not self.scope_error
        )


class ExecutionService:
    """Validate, journal, and execute a reviewed disposable-target load."""

    def __init__(
        self,
        projects: ExecutionProjectRepository,
        preflight: PreflightService,
        journal: ExecutionJournalRepository,
        authorization: AuthorizationPolicy,
    ) -> None:
        self.projects = projects
        self.preflight = preflight
        self.journal = journal
        self.authorization = authorization

    def current_preview(self, project_id: str) -> ExecutionPreview | None:
        snapshot = self.preflight.current_execution_snapshot(project_id)
        if snapshot is None:
            return None
        project = self.projects.get(project_id)
        current = self.journal.get_current_run(project_id, snapshot.semantic_hash)
        api_scope = execution_api_scope(snapshot)
        return ExecutionPreview(
            snapshot=snapshot,
            datasets=tuple(
                ExecutionDatasetPreview(
                    dataset=dataset.dataset,
                    target_model=dataset.target_model,
                    create_count=sum(
                        row.dataset == dataset.dataset
                        and row.disposition == "CREATE"
                        for row in snapshot.rows
                    ),
                    update_count=sum(
                        row.dataset == dataset.dataset
                        and row.disposition == "UPDATE"
                        for row in snapshot.rows
                    ),
                    unchanged_count=sum(
                        row.dataset == dataset.dataset
                        and row.disposition == "UNCHANGED"
                        for row in snapshot.rows
                    ),
                )
                for dataset in snapshot.datasets
                if any(row.dataset == dataset.dataset for row in snapshot.rows)
            ),
            current_run=current,
            api_scope=api_scope,
            scope_error=_execution_snapshot_error(project, snapshot),
        )

    def execute(
        self,
        project_id: str,
        *,
        expected_snapshot_hash: str,
        executor: OdooWriteExecutor,
        actor: Actor,
    ) -> ExecutionRun:
        self.authorization.require(
            actor,
            Capability.EXPORT_PLAN_EXECUTE,
            project_id=project_id,
        )
        project = self.projects.get(project_id)
        preview = self.current_preview(project_id)
        if preview is None:
            raise WorkspaceError("Compare the prepared data with Odoo first")
        snapshot = preview.snapshot
        if snapshot.semantic_hash != expected_snapshot_hash:
            raise WorkspaceError("The load preview changed. Review it again.")
        self._validate_execution_scope(project, preview, executor)

        write_rows = tuple(
            row
            for row in snapshot.rows
            if row.disposition in {"CREATE", "UPDATE"}
        )
        attempts = tuple(
            ExecutionRowAttempt(
                row_id=row.row_id,
                dataset=row.dataset,
                source_row=row.source_row,
                target_model=row.target_model,
                operation=row.disposition,
                field_names=tuple(intent.field for intent in row.fields),
                proposed_external_id=row.proposed_external_id,
            )
            for row in write_rows
        )
        started_at = datetime.now(timezone.utc)
        run = ExecutionRun(
            run_id=str(uuid4()),
            project_id=project_id,
            snapshot_hash=snapshot.semantic_hash,
            snapshot_root_hash=snapshot.root_hash,
            preflight_run_id=snapshot.preflight_run_id,
            target_hash=snapshot.target_hash,
            target_database=snapshot.target_database,
            status=ExecutionRunStatus.RUNNING,
            started_at=started_at,
            started_by=actor.identity.display_name,
            completed_at=None,
            rows=attempts,
        )
        self.journal.start_run(project_id, run, actor=actor)

        metadata = {item.dataset: item for item in snapshot.datasets}
        by_source = {
            (row.dataset, _portable_key(row.source_identity)): row
            for row in snapshot.rows
        }
        identity_cache: dict[str, int] = {}
        source_cache: dict[tuple[str, str], int] = {}
        recorded: dict[str, ExecutionRowAttempt] = {
            item.row_id: item for item in attempts
        }
        stop_after_unknown = False

        for dataset in sorted(snapshot.datasets, key=lambda item: item.sequence):
            dataset_rows = tuple(
                row
                for row in write_rows
                if row.dataset == dataset.dataset
                and row.row_id not in {
                    row_id
                    for row_id, attempt in recorded.items()
                    if attempt.status is not ExecutionRowStatus.PLANNED
                }
            )
            if stop_after_unknown:
                self._record_blocked(
                    project_id,
                    run.run_id,
                    dataset_rows,
                    recorded,
                    "Not attempted after an uncertain Odoo response",
                )
                continue
            creates = tuple(row for row in dataset_rows if row.disposition == "CREATE")
            updates = tuple(row for row in dataset_rows if row.disposition == "UPDATE")
            for start in range(0, len(creates), MAX_CREATE_BATCH_ROWS):
                batch = creates[start : start + MAX_CREATE_BATCH_ROWS]
                prepared_rows: list[tuple[ExecutionRow, dict[str, Any]]] = []
                for row in batch:
                    try:
                        values = self._row_values(
                            row,
                            metadata,
                            by_source,
                            source_cache,
                            identity_cache,
                            executor,
                        )
                    except (WorkspaceError, OdooWriteRejected) as error:
                        outcome = replace(
                            recorded[row.row_id],
                            status=ExecutionRowStatus.BLOCKED,
                            safe_error=str(error),
                        )
                        self.journal.record_outcomes(
                            project_id, run.run_id, (outcome,)
                        )
                        recorded[row.row_id] = outcome
                    else:
                        prepared_rows.append((row, values))
                if not prepared_rows:
                    continue
                try:
                    identifiers = executor.create_rows(
                        dataset.target_model,
                        tuple(values for _row, values in prepared_rows),
                    )
                except OdooWriteOutcomeUnknown as error:
                    outcomes = tuple(
                        replace(
                            recorded[row.row_id],
                            status=ExecutionRowStatus.OUTCOME_UNKNOWN,
                            attempt=1,
                            safe_error=str(error),
                        )
                        for row, _values in prepared_rows
                    )
                    self.journal.record_outcomes(project_id, run.run_id, outcomes)
                    recorded.update({item.row_id: item for item in outcomes})
                    stop_after_unknown = True
                    break
                except OdooWriteRejected as error:
                    outcomes = tuple(
                        replace(
                            recorded[row.row_id],
                            status=ExecutionRowStatus.FAILED,
                            attempt=1,
                            safe_error=str(error),
                        )
                        for row, _values in prepared_rows
                    )
                    self.journal.record_outcomes(project_id, run.run_id, outcomes)
                    recorded.update({item.row_id: item for item in outcomes})
                    continue
                outcomes = []
                for (row, _values), identifier in zip(
                    prepared_rows, identifiers, strict=True
                ):
                    outcome = replace(
                        recorded[row.row_id],
                        status=ExecutionRowStatus.COMMITTED,
                        attempt=1,
                        odoo_id=identifier,
                    )
                    outcomes.append(outcome)
                    source_cache[(row.dataset, _portable_key(row.source_identity))] = (
                        identifier
                    )
                    identity_cache[_identity_cache_key(row)] = identifier
                self.journal.record_outcomes(project_id, run.run_id, outcomes)
                recorded.update({item.row_id: item for item in outcomes})

            for row in updates:
                if stop_after_unknown:
                    self._record_blocked(
                        project_id,
                        run.run_id,
                        (row,),
                        recorded,
                        "Not attempted after an uncertain Odoo response",
                    )
                    continue
                try:
                    record_id = self._find_row_id(
                        row,
                        metadata[row.dataset],
                        identity_cache,
                        executor,
                    )
                    values = self._row_values(
                        row,
                        metadata,
                        by_source,
                        source_cache,
                        identity_cache,
                        executor,
                    )
                except (WorkspaceError, OdooWriteRejected) as error:
                    outcome = replace(
                        recorded[row.row_id],
                        status=ExecutionRowStatus.BLOCKED,
                        safe_error=str(error),
                    )
                else:
                    try:
                        executor.update_row(row.target_model, record_id, values)
                    except OdooWriteOutcomeUnknown as error:
                        outcome = replace(
                            recorded[row.row_id],
                            status=ExecutionRowStatus.OUTCOME_UNKNOWN,
                            attempt=1,
                            safe_error=str(error),
                        )
                        stop_after_unknown = True
                    except OdooWriteRejected as error:
                        outcome = replace(
                            recorded[row.row_id],
                            status=ExecutionRowStatus.FAILED,
                            attempt=1,
                            safe_error=str(error),
                        )
                    else:
                        outcome = replace(
                            recorded[row.row_id],
                            status=ExecutionRowStatus.COMMITTED,
                            attempt=1,
                            odoo_id=record_id,
                        )
                self.journal.record_outcomes(project_id, run.run_id, (outcome,))
                recorded[row.row_id] = outcome

        remaining = tuple(
            row
            for row in write_rows
            if recorded[row.row_id].status is ExecutionRowStatus.PLANNED
        )
        if remaining:
            self._record_blocked(
                project_id,
                run.run_id,
                remaining,
                recorded,
                "Not attempted because an earlier dependency did not complete",
            )
        statuses = {item.status for item in recorded.values()}
        final_status = (
            ExecutionRunStatus.OUTCOME_UNKNOWN
            if ExecutionRowStatus.OUTCOME_UNKNOWN in statuses
            else (
                ExecutionRunStatus.COMPLETED_WITH_ERRORS
                if statuses.intersection(
                    {ExecutionRowStatus.FAILED, ExecutionRowStatus.BLOCKED}
                )
                else ExecutionRunStatus.COMPLETED
            )
        )
        return self.journal.finish_run(
            project_id,
            run.run_id,
            final_status,
            actor=actor,
        )

    @staticmethod
    def _validate_execution_scope(
        project: MigrationProject,
        preview: ExecutionPreview,
        executor: OdooWriteExecutor,
    ) -> None:
        snapshot = preview.snapshot
        if project.odoo_connection_mode is not OdooConnectionMode.LOCAL:
            raise WorkspaceError(
                "Loading is currently limited to a disposable local Odoo target"
            )
        if preview.current_run is not None:
            raise WorkspaceError(
                "This preview was already loaded. Compare with Odoo again first."
            )
        if preview.scope_error:
            raise WorkspaceError(preview.scope_error)
        if not preview.can_load:
            raise WorkspaceError(
                "Resolve every blocked or ambiguous row before loading"
            )
        if executor.target_hash != snapshot.target_hash:
            raise WorkspaceError(
                "The load connection points to a different Odoo target"
            )
        if executor.scope_hash != preview.api_scope.semantic_hash:
            raise WorkspaceError(
                "The writer is not bound to this reviewed load preview"
            )

    def _row_values(
        self,
        row: ExecutionRow,
        metadata: Mapping[str, ExecutionDataset],
        by_source: Mapping[tuple[str, str], ExecutionRow],
        source_cache: dict[tuple[str, str], int],
        identity_cache: dict[str, int],
        executor: OdooWriteExecutor,
    ) -> dict[str, Any]:
        values: dict[str, Any] = {}
        for intent in row.fields:
            if intent.action == "OMIT":
                continue
            if intent.action == "SET_NULL":
                values[intent.field] = None
            elif intent.kind == "scalar":
                values[intent.field] = _odoo_scalar(intent.value)
            else:
                values[intent.field] = self._relation_value(
                    intent,
                    metadata,
                    by_source,
                    source_cache,
                    identity_cache,
                    executor,
                )
        if not values:
            raise WorkspaceError("A planned write has no permitted field values")
        return values

    def _relation_value(
        self,
        intent: FieldIntent,
        metadata: Mapping[str, ExecutionDataset],
        by_source: Mapping[tuple[str, str], ExecutionRow],
        source_cache: dict[tuple[str, str], int],
        identity_cache: dict[str, int],
        executor: OdooWriteExecutor,
    ) -> object:
        value = intent.value
        if isinstance(value, tuple):
            identifiers = tuple(
                dict.fromkeys(
                    self._relation_reference_id(
                        item,
                        intent,
                        metadata,
                        by_source,
                        source_cache,
                        identity_cache,
                        executor,
                    )
                    for item in value
                )
            )
            # Update snapshots already contain the final canonical set and use
            # replace.  For creates, add starts from an empty relation and
            # remove therefore also has a deterministic final empty set.
            if intent.relation_operation in {"replace", "add"}:
                return [[6, 0, list(identifiers)]]
            if intent.relation_operation == "remove":
                return [[6, 0, []]]
            raise WorkspaceError(
                f"Relationship field {intent.field} has an unsupported operation"
            )
        if intent.relation_operation != "replace":
            raise WorkspaceError(
                f"Relationship field {intent.field} must use replace"
            )
        return self._relation_reference_id(
            value,
            intent,
            metadata,
            by_source,
            source_cache,
            identity_cache,
            executor,
        )

    def _relation_reference_id(
        self,
        value: object,
        intent: FieldIntent,
        metadata: Mapping[str, ExecutionDataset],
        by_source: Mapping[tuple[str, str], ExecutionRow],
        source_cache: dict[tuple[str, str], int],
        identity_cache: dict[str, int],
        executor: OdooWriteExecutor,
    ) -> int:
        if isinstance(value, LogicalReference) and value.origin == "incoming":
            if value.dataset is None:
                raise WorkspaceError("An incoming relationship is incomplete")
            source_key = (value.dataset, _portable_key(value.key))
            if source_key in source_cache:
                return source_cache[source_key]
            referenced = by_source.get(source_key)
            if referenced is None:
                raise WorkspaceError("A related prepared row could not be found")
            related_id = self._find_row_id(
                referenced,
                metadata[referenced.dataset],
                identity_cache,
                executor,
            )
            source_cache[source_key] = related_id
            return related_id
        if isinstance(value, BusinessReference):
            key, scope = value.key, value.scope
        elif isinstance(value, LogicalReference):
            key, scope = value.key, value.scope
        else:
            raise WorkspaceError("A relationship is not expressed by a business key")
        domain = _identity_domain(
            intent.related_identity_fields,
            key,
            intent.related_scope_fields,
            scope,
        )
        cache_key = _domain_cache_key(intent.related_model, domain)
        return self._find_unique(
            intent.related_model,
            domain,
            cache_key,
            identity_cache,
            executor,
        )

    def _find_row_id(
        self,
        row: ExecutionRow,
        dataset: ExecutionDataset,
        identity_cache: dict[str, int],
        executor: OdooWriteExecutor,
    ) -> int:
        cache_key = _identity_cache_key(row)
        if cache_key in identity_cache:
            return identity_cache[cache_key]
        domain = _identity_domain(
            dataset.identity_fields,
            row.business_identity,
            dataset.scope_fields,
            row.business_scope,
        )
        return self._find_unique(
            row.target_model,
            domain,
            cache_key,
            identity_cache,
            executor,
        )

    @staticmethod
    def _find_unique(
        model: str,
        domain: tuple[tuple[str, str, Any], ...],
        cache_key: str,
        identity_cache: dict[str, int],
        executor: OdooWriteExecutor,
    ) -> int:
        matches = executor.find_ids(model, domain)
        if len(matches) != 1:
            raise WorkspaceError(
                "The Odoo business key no longer matches exactly one record"
            )
        identity_cache[cache_key] = matches[0]
        return matches[0]

    def _record_blocked(
        self,
        project_id: str,
        run_id: str,
        rows: Sequence[ExecutionRow],
        recorded: dict[str, ExecutionRowAttempt],
        reason: str,
    ) -> None:
        outcomes = tuple(
            replace(
                recorded[row.row_id],
                status=ExecutionRowStatus.BLOCKED,
                safe_error=reason,
            )
            for row in rows
            if recorded[row.row_id].status is ExecutionRowStatus.PLANNED
        )
        self.journal.record_outcomes(project_id, run_id, outcomes)
        recorded.update({item.row_id: item for item in outcomes})


def _identity_domain(
    identity_fields: tuple[str, ...],
    identity: tuple[Any, ...],
    scope_fields: tuple[str, ...],
    scope: tuple[Any, ...],
) -> tuple[tuple[str, str, Any], ...]:
    if (
        not identity_fields
        or len(identity_fields) != len(identity)
        or len(scope_fields) != len(scope)
    ):
        raise WorkspaceError("The Odoo business-key shape is incomplete")
    return tuple(
        (field, "=", _odoo_scalar(value))
        for field, value in zip(
            (*identity_fields, *scope_fields),
            (*identity, *scope),
            strict=True,
        )
    )


def execution_api_scope(snapshot: ExecutionSnapshot) -> OdooApiScope:
    """Derive the exact native API capability from one frozen preview."""

    write_fields: dict[str, set[str]] = {}
    read_fields: dict[str, set[str]] = {}
    lookup_fields: dict[str, set[str]] = {}
    for dataset in snapshot.datasets:
        lookup_fields.setdefault(dataset.target_model, set()).update(
            (*dataset.identity_fields, *dataset.scope_fields)
        )
    for row in snapshot.rows:
        if row.disposition not in {"CREATE", "UPDATE"}:
            continue
        for intent in row.fields:
            if intent.action != "OMIT":
                write_fields.setdefault(row.target_model, set()).add(intent.field)
                read_fields.setdefault(row.target_model, set()).add(intent.field)
            if intent.kind == "relation":
                lookup_fields.setdefault(intent.related_model, set()).update(
                    (
                        *intent.related_identity_fields,
                        *intent.related_scope_fields,
                    )
                )
    model_names = sorted(set(write_fields) | set(read_fields) | set(lookup_fields))
    return OdooApiScope(
        preview_hash=snapshot.semantic_hash,
        models=tuple(
            OdooModelScope(
                model=model,
                write_fields=tuple(sorted(write_fields.get(model, set()))),
                read_fields=tuple(sorted(read_fields.get(model, set()))),
                lookup_fields=tuple(sorted(lookup_fields.get(model, set()))),
            )
            for model in model_names
            if write_fields.get(model)
            or read_fields.get(model)
            or lookup_fields.get(model)
        )
    )


def _execution_snapshot_error(
    project: MigrationProject,
    snapshot: ExecutionSnapshot,
) -> str:
    """Explain an execution-shape problem before the user can press Load."""

    if project.odoo_connection_mode is not OdooConnectionMode.LOCAL:
        return "Loading is currently limited to a disposable local Odoo target"
    if not snapshot.target_odoo_version.startswith("19."):
        return "The schema-bound load path requires Odoo 19"
    write_rows = tuple(row for row in snapshot.rows if row.fields)
    if not write_rows:
        return "This preview has no rows to create or update"
    datasets = {item.dataset: item for item in snapshot.datasets}
    for row in write_rows:
        dataset = datasets.get(row.dataset)
        if dataset is None:
            return f"Dataset {row.dataset} is missing from the reviewed load preview"
        for intent in row.fields:
            if intent.kind == "scalar" or intent.action != "SET_VALUE":
                continue
            if isinstance(intent.value, tuple):
                if intent.relation_operation not in {"replace", "add", "remove"}:
                    return (
                        f"{row.dataset}.{intent.field} uses unsupported relationship "
                        f"operation {intent.relation_operation}"
                    )
                if not all(
                    isinstance(item, BusinessReference | LogicalReference)
                    for item in intent.value
                ):
                    return (
                        f"{row.dataset}.{intent.field} contains an unsupported "
                        "relationship value"
                    )
            elif not isinstance(
                intent.value,
                BusinessReference | LogicalReference,
            ):
                return (
                    f"{row.dataset}.{intent.field} is not expressed by a reviewed "
                    "business key"
                )
    return ""


def _odoo_scalar(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        normalized = value
        if normalized.tzinfo is not None:
            normalized = normalized.astimezone(timezone.utc).replace(tzinfo=None)
        return normalized.isoformat(sep=" ")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, BusinessReference | LogicalReference):
        raise WorkspaceError("A relational business key cannot be used as a scalar")
    if value is None or type(value) in {str, int, bool, float}:
        return value
    raise WorkspaceError("A prepared value is not supported by the Odoo API")


def _portable_key(value: tuple[Any, ...]) -> str:
    return canonical_json_text(portable_value(value))


def _identity_cache_key(row: ExecutionRow) -> str:
    return canonical_json_text(
        {
            "model": row.target_model,
            "identity": portable_value(row.business_identity),
            "scope": portable_value(row.business_scope),
        }
    )


def _domain_cache_key(
    model: str,
    domain: tuple[tuple[str, str, Any], ...],
) -> str:
    return canonical_json_text({"model": model, "domain": domain})
