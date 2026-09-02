"""Stream and atomically publish one current Odoo source capture set."""

from __future__ import annotations

from contextlib import ExitStack
from dataclasses import dataclass
from datetime import datetime
from threading import RLock
from typing import Callable, Protocol
from uuid import uuid4

from impodo.domain.shared.access import Actor, Capability
from impodo.application.shared.artifacts import DataVersionSourceArtifactStore
from ..domain.odoo_provenance import (
    OdooCaptureManifest,
    OdooCaptureOriginHeader,
    OdooOriginBatch,
)
from ..domain.odoo_source_capture import (
    CancellationProbe,
    OdooCaptureAccounting,
    OdooCapturePage,
    require_not_cancelled,
)
from ..domain.odoo_source_policy import CURRENT_ODOO_SOURCE_POLICY
from ..domain.serialization import content_hash
from impodo.application.workspace.odoo_capture_jobs import (
    OdooCapturePhase,
    OdooCaptureProgress,
)
from ..domain.source_snapshot import (
    SourceSnapshot,
    SourceSnapshotColumn,
    SourceSnapshotSchema,
)
from impodo.domain.preparation.source import SourceLoadError
from impodo.application.data_version.source_snapshots import (
    SourceSnapshotCandidate,
    SourceSnapshotCandidateWriter,
)
from ..domain.odoo_capture import OdooCaptureSelection
from impodo.domain.workspace.contracts import (
    SourceDataset,
    SourceDatasetColumn,
    SourceSelection,
    WORKSPACE_EVIDENCE_IDENTITY_CONTRACT_VERSION,
)
from impodo.domain.workspace.errors import WorkspaceError
from impodo.application.workspace.access import WorkspaceAccessService
from .odoo_provenance_service import OdooProvenanceService
from .odoo_source_capture_service import (
    OdooSourceCapturePort,
    OdooSourceCaptureService,
)


class OdooPublicationSelectionReader(Protocol):
    def get_source_selection(self, workspace_id: str) -> SourceSelection | None: ...


class OdooCapturePublicationStore(Protocol):
    def publish_complete_captures(
        self,
        workspace_id: str,
        protected_candidates: tuple[tuple[OdooCaptureManifest, bytes], ...],
        source_selection: SourceSelection,
        source_snapshots: tuple[SourceSnapshot, ...],
        *,
        actor: Actor,
    ) -> None: ...

    def recover_incomplete_publications(self, workspace_id: str) -> int: ...


@dataclass(frozen=True, slots=True)
class OdooCapturePublication:
    """All manifest and source roots promoted by one complete capture set."""

    manifests: tuple[OdooCaptureManifest, ...]
    source_selection: SourceSelection
    source_snapshots: tuple[SourceSnapshot, ...]
    page_count: int

    @property
    def manifest(self) -> OdooCaptureManifest:
        """Retain the single-model projection used by existing job summaries."""

        return self.manifests[0]

    @property
    def source_snapshot(self) -> SourceSnapshot:
        """Retain the single-model projection used by existing integrations."""

        return self.source_snapshots[0]


@dataclass(frozen=True, slots=True)
class _CapturedDatasetCandidate:
    selection: OdooCaptureSelection
    columns: tuple[SourceDatasetColumn, ...]
    schema: SourceSnapshotSchema
    candidate: SourceSnapshotCandidate
    origins: tuple[OdooOriginBatch, ...]
    accounting: OdooCaptureAccounting
    matching_rows: int


class OdooCapturePublicationService:
    """Own the bounded values/provenance candidate and one pointer promotion."""

    def __init__(
        self,
        captures: OdooSourceCaptureService,
        selections: OdooPublicationSelectionReader,
        provenance: OdooProvenanceService,
        publications: OdooCapturePublicationStore,
        artifacts: DataVersionSourceArtifactStore,
        workspace_access: WorkspaceAccessService,
    ) -> None:
        self._captures = captures
        self._selections = selections
        self._provenance = provenance
        self._publications = publications
        self._artifacts = artifacts
        self._workspace_access = workspace_access
        self._lock = RLock()

    def publish(
        self,
        workspace_id: str,
        gateway: OdooSourceCapturePort,
        *,
        actor: Actor,
        cancellation: CancellationProbe | None = None,
        progress: Callable[[OdooCaptureProgress], None] | None = None,
    ) -> OdooCapturePublication:
        """Serialize project publication so cleanup cannot race another capture."""

        with self._lock:
            return self._publish(
                workspace_id,
                gateway,
                actor=actor,
                cancellation=cancellation,
                progress=progress,
            )

    def _publish(
        self,
        workspace_id: str,
        gateway: OdooSourceCapturePort,
        *,
        actor: Actor,
        cancellation: CancellationProbe | None = None,
        progress: Callable[[OdooCaptureProgress], None] | None = None,
    ) -> OdooCapturePublication:
        """Capture every current model plan and promote the set atomically."""

        policy = CURRENT_ODOO_SOURCE_POLICY
        context = self._workspace_access.require(
            actor,
            Capability.SOURCE_CAPTURE,
            workspace_id=workspace_id,
        )
        data_version_id = context.data_version_id
        selections = self._captures.current_selections(
            workspace_id,
            actor=actor,
            require_complete=True,
        )
        _report_progress(progress, OdooCapturePhase.VERIFYING, total_rows=0)
        self._publications.recover_incomplete_publications(workspace_id)
        self._artifacts.ensure_source_snapshot_capacity(
            data_version_id,
            required_bytes=len(selections)
            * (policy.max_snapshot_bytes + policy.max_temporary_bytes),
        )

        captured: list[_CapturedDatasetCandidate] = []
        completed_rows = 0
        page_count = 0
        response_bytes = 0
        normalized_bytes = 0
        try:
            with ExitStack() as stack:
                workspaces = {
                    selection.model: stack.enter_context(
                        self._artifacts.prepare_source_snapshot(data_version_id)
                    )
                    for selection in selections
                }
                writers: dict[str, SourceSnapshotCandidateWriter] = {}
                columns_by_model: dict[str, tuple[SourceDatasetColumn, ...]] = {}
                schemas_by_model: dict[str, SourceSnapshotSchema] = {}
                origins_by_model: dict[str, list[OdooOriginBatch]] = {
                    selection.model: [] for selection in selections
                }
                matching_by_model: dict[str, int] = {}

                def prepare_consumer(request, current_selection):
                    model = current_selection.model
                    columns = tuple(
                        SourceDatasetColumn(
                            ordinal=index,
                            source_name=projection.name,
                            stable_key=stable_key,
                            candidate_type=_candidate_type(projection.field_type),
                        )
                        for index, (projection, stable_key) in enumerate(
                            zip(
                                request.projection,
                                current_selection.column_stable_keys,
                                strict=True,
                            ),
                            start=1,
                        )
                    )
                    schema = SourceSnapshotSchema.create(
                        SourceSnapshotColumn.create(
                            ordinal=column.ordinal,
                            stable_key=column.stable_key,
                            source_name=column.source_name,
                            candidate_type=column.candidate_type,
                        )
                        for column in columns
                    )
                    writer = SourceSnapshotCandidateWriter(
                        workspaces[model],
                        schema,
                        batch_rows=current_selection.page_size,
                        maximum_snapshot_bytes=policy.max_snapshot_bytes,
                        maximum_temporary_bytes=policy.max_temporary_bytes,
                    )
                    writers[model] = writer
                    columns_by_model[model] = columns
                    schemas_by_model[model] = schema

                    def consume_page(page: OdooCapturePage) -> None:
                        nonlocal completed_rows, page_count
                        nonlocal response_bytes, normalized_bytes
                        writer.append_columnar_page(
                            first_row_ordinal=page.first_row_ordinal,
                            values_by_name={
                                column.field_name: column.values
                                for column in page.columns
                            },
                        )
                        origins_by_model[model].append(page.origin_batch)
                        completed_rows += page.row_count
                        page_count += 1
                        response_bytes += page.response_bytes
                        normalized_bytes += page.normalized_bytes
                        _report_progress(
                            progress,
                            OdooCapturePhase.READING,
                            completed_rows=completed_rows,
                            total_rows=sum(matching_by_model.values()),
                            page_count=page_count,
                            response_bytes=response_bytes,
                            normalized_bytes=normalized_bytes,
                        )

                    return consume_page

                def observe_matching_rows(
                    selection: OdooCaptureSelection,
                    matching_rows: int,
                ) -> None:
                    matching_by_model[selection.model] = matching_rows

                results = self._captures.capture_all(
                    workspace_id,
                    gateway,
                    consume_page_factory=prepare_consumer,
                    actor=actor,
                    cancellation=cancellation,
                    observe_matching_rows=observe_matching_rows,
                )

                for result in results:
                    selection = result.selection
                    writer = writers.get(selection.model)
                    schema = schemas_by_model.get(selection.model)
                    if writer is None or schema is None:
                        raise WorkspaceError(
                            "Odoo capture writer is not initialized"
                        )
                    candidate = writer.finalize()
                    if candidate.row_count != result.accounting.row_count:
                        raise SourceLoadError(
                            "Odoo values and capture accounting row counts differ"
                        )
                    captured.append(
                        _CapturedDatasetCandidate(
                            selection=selection,
                            columns=columns_by_model[selection.model],
                            schema=schema,
                            candidate=candidate,
                            origins=tuple(origins_by_model[selection.model]),
                            accounting=result.accounting,
                            matching_rows=result.matching_rows,
                        )
                    )

                completed_rows = sum(
                    item.accounting.row_count for item in captured
                )
                page_count = sum(
                    item.accounting.page_count for item in captured
                )
                response_bytes = sum(
                    item.accounting.response_bytes for item in captured
                )
                normalized_bytes = sum(
                    item.accounting.normalized_bytes for item in captured
                )
                _report_progress(
                    progress,
                    OdooCapturePhase.FINALIZING,
                    completed_rows=completed_rows,
                    total_rows=sum(item.matching_rows for item in captured),
                    page_count=page_count,
                    response_bytes=response_bytes,
                    normalized_bytes=normalized_bytes,
                )
                datasets = tuple(
                    SourceDataset(
                        dataset_id=item.selection.dataset_id,
                        name=item.selection.dataset_name,
                        source=item.selection.source_binding,
                        row_count=item.accounting.row_count,
                        columns=item.columns,
                    )
                    for item in captured
                )
                current_source = self._selections.get_source_selection(workspace_id)
                source_selection = _source_selection(
                    data_version_id,
                    datasets,
                    version=(current_source.version + 1 if current_source else 1),
                    actor=actor,
                    created_at=max(
                        item.accounting.capture_finished_at for item in captured
                    ),
                )
                snapshots = tuple(
                    SourceSnapshot.create(
                        data_version_id=data_version_id,
                        dataset_id=dataset.dataset_id,
                        dataset_name=dataset.name,
                        source=dataset.source,
                        physical_selection_hash=source_selection.content_hash,
                        schema=item.schema,
                        row_count=item.candidate.row_count,
                        data_logical_hash=item.candidate.data_logical_hash,
                        parquet_sha256=item.candidate.parquet_sha256,
                        created_at=item.accounting.capture_finished_at,
                    )
                    for item, dataset in zip(captured, datasets, strict=True)
                )
                for item, snapshot in zip(captured, snapshots, strict=True):
                    self._artifacts.publish_source_snapshot(
                        data_version_id,
                        item.candidate.path,
                        snapshot.parquet_storage_key,
                        expected_sha256=item.candidate.parquet_sha256,
                    )
                protected = tuple(
                    self._provenance.prepare_capture_origins(
                        workspace_id,
                        actor=actor,
                        selection=item.selection,
                        header=OdooCaptureOriginHeader(
                            high_water_id=item.accounting.high_water_id
                        ),
                        batches=item.origins,
                        row_count=item.accounting.row_count,
                        data_logical_hash=item.candidate.data_logical_hash,
                        data_sha256=item.candidate.parquet_sha256,
                        data_storage_key=snapshot.parquet_storage_key,
                        data_size_bytes=item.candidate.size_bytes,
                        capture_started_at=item.accounting.capture_started_at,
                        capture_finished_at=item.accounting.capture_finished_at,
                    )
                    for item, snapshot in zip(captured, snapshots, strict=True)
                )
                require_not_cancelled(cancellation)
                _report_progress(
                    progress,
                    OdooCapturePhase.PUBLISHING,
                    completed_rows=completed_rows,
                    total_rows=sum(item.matching_rows for item in captured),
                    page_count=page_count,
                    response_bytes=response_bytes,
                    normalized_bytes=normalized_bytes,
                )
                self._publications.publish_complete_captures(
                    workspace_id,
                    tuple(
                        (item.manifest, item.encrypted_bytes)
                        for item in protected
                    ),
                    source_selection,
                    snapshots,
                    actor=actor,
                )
                return OdooCapturePublication(
                    manifests=tuple(item.manifest for item in protected),
                    source_selection=source_selection,
                    source_snapshots=snapshots,
                    page_count=page_count,
                )
        except Exception:
            self._publications.recover_incomplete_publications(workspace_id)
            raise

def _report_progress(
    callback: Callable[[OdooCaptureProgress], None] | None,
    phase: OdooCapturePhase,
    *,
    total_rows: int,
    completed_rows: int = 0,
    page_count: int = 0,
    response_bytes: int = 0,
    normalized_bytes: int = 0,
) -> None:
    """Report counters already produced by the stream; never rescan or rehash."""

    if callback is not None:
        callback(
            OdooCaptureProgress(
                phase=phase,
                completed_rows=completed_rows,
                total_rows=total_rows,
                page_count=page_count,
                response_bytes=response_bytes,
                normalized_bytes=normalized_bytes,
            )
        )


def _candidate_type(field_type: str) -> str:
    return {
        "boolean": "boolean",
        "integer": "integer",
        "date": "date",
        "datetime": "datetime",
        "char": "string",
        "selection": "string",
        "text": "string",
    }[field_type]


def _source_selection(
    data_version_id: str,
    datasets: tuple[SourceDataset, ...],
    *,
    version: int,
    actor: Actor,
    created_at: datetime,
) -> SourceSelection:
    selection = SourceSelection(
        selection_id=str(uuid4()),
        version=version,
        data_version_id=data_version_id,
        created_at=created_at,
        created_by=actor.identity.display_name,
        datasets=datasets,
        content_hash="",
    )
    return SourceSelection(
        selection_id=selection.selection_id,
        version=selection.version,
        data_version_id=selection.data_version_id,
        created_at=selection.created_at,
        created_by=selection.created_by,
        datasets=selection.datasets,
        content_hash=content_hash(
            {
                "contract_version": WORKSPACE_EVIDENCE_IDENTITY_CONTRACT_VERSION,
                "data_version_id": data_version_id,
                "version": version,
                "datasets": [dataset.to_dict() for dataset in datasets],
            }
        ),
    )
