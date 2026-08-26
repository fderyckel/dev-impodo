"""Stream and atomically publish one current Odoo source capture."""

from __future__ import annotations

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
    OdooCapturePage,
    require_not_cancelled,
)
from ..domain.odoo_source_policy import CURRENT_ODOO_SOURCE_POLICY
from ..domain.serialization import content_hash
from impodo.application.workspace.odoo_capture_jobs import OdooCapturePhase, OdooCaptureProgress
from ..domain.source_snapshot import (
    SourceSnapshot,
    SourceSnapshotColumn,
    SourceSnapshotSchema,
)
from impodo.domain.preparation.source import SourceLoadError
from impodo.application.data_version.source_snapshots import SourceSnapshotCandidateWriter
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
    def publish_complete_capture(
        self,
        workspace_id: str,
        manifest: OdooCaptureManifest,
        encrypted_candidate: bytes,
        source_selection: SourceSelection,
        source_snapshot: SourceSnapshot,
        *,
        actor: Actor,
    ) -> None: ...

    def recover_incomplete_publications(self, workspace_id: str) -> int: ...


@dataclass(frozen=True, slots=True)
class OdooCapturePublication:
    """The one manifest and source roots promoted by a complete capture."""

    manifest: OdooCaptureManifest
    source_selection: SourceSelection
    source_snapshot: SourceSnapshot
    page_count: int


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
        """Capture once, encode each value once, then promote all roots together."""

        policy = CURRENT_ODOO_SOURCE_POLICY
        context = self._workspace_access.require(
            actor,
            Capability.SOURCE_CAPTURE,
            workspace_id=workspace_id,
        )
        data_version_id = context.data_version_id
        _report_progress(progress, OdooCapturePhase.VERIFYING, total_rows=0)
        self._publications.recover_incomplete_publications(workspace_id)
        self._artifacts.ensure_source_snapshot_capacity(
            data_version_id,
            required_bytes=policy.max_snapshot_bytes + policy.max_temporary_bytes,
        )
        try:
            with self._artifacts.prepare_source_snapshot(
                data_version_id
            ) as workspace:
                writer: SourceSnapshotCandidateWriter | None = None
                columns: tuple[SourceDatasetColumn, ...] = ()
                schema: SourceSnapshotSchema | None = None
                origins: list[OdooOriginBatch] = []
                maximum_rows = 0
                completed_rows = 0
                page_count = 0
                response_bytes = 0
                normalized_bytes = 0

                def prepare_consumer(request, selection):
                    nonlocal writer, columns, schema, maximum_rows
                    maximum_rows = request.maximum_rows
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
                                selection.column_stable_keys,
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
                        workspace,
                        schema,
                        batch_rows=selection.page_size,
                        maximum_snapshot_bytes=policy.max_snapshot_bytes,
                        maximum_temporary_bytes=policy.max_temporary_bytes,
                    )
                    return consume_page

                def consume_page(page: OdooCapturePage) -> None:
                    nonlocal completed_rows, page_count
                    nonlocal response_bytes, normalized_bytes
                    if writer is None:
                        raise WorkspaceError("Odoo capture writer is not initialized")
                    writer.append_columnar_page(
                        first_row_ordinal=page.first_row_ordinal,
                        values_by_name={
                            column.field_name: column.values for column in page.columns
                        },
                    )
                    origins.append(page.origin_batch)
                    completed_rows += page.row_count
                    page_count += 1
                    response_bytes += page.response_bytes
                    normalized_bytes += page.normalized_bytes
                    _report_progress(
                        progress,
                        OdooCapturePhase.READING,
                        completed_rows=completed_rows,
                        total_rows=maximum_rows,
                        page_count=page_count,
                        response_bytes=response_bytes,
                        normalized_bytes=normalized_bytes,
                    )

                result = self._captures.capture(
                    workspace_id,
                    gateway,
                    consume_page_factory=prepare_consumer,
                    actor=actor,
                    cancellation=cancellation,
                )
                require_not_cancelled(cancellation)
                capture_selection = result.selection
                if capture_selection.data_version_id != data_version_id:
                    raise WorkspaceError(
                        "The Odoo capture belongs to another DataVersion"
                    )
                if writer is None or schema is None:
                    raise WorkspaceError("Odoo capture writer is not initialized")
                candidate = writer.finalize()
                accounting = result.accounting
                _report_progress(
                    progress,
                    OdooCapturePhase.FINALIZING,
                    completed_rows=accounting.row_count,
                    total_rows=result.request.maximum_rows,
                    page_count=accounting.page_count,
                    response_bytes=accounting.response_bytes,
                    normalized_bytes=accounting.normalized_bytes,
                )
                if candidate.row_count != accounting.row_count:
                    raise SourceLoadError(
                        "Odoo values and capture accounting row counts differ"
                    )

                dataset = SourceDataset(
                    dataset_id=capture_selection.dataset_id,
                    name=capture_selection.dataset_name,
                    source=capture_selection.source_binding,
                    row_count=accounting.row_count,
                    columns=columns,
                )
                current_source = self._selections.get_source_selection(workspace_id)
                source_selection = _source_selection(
                    data_version_id,
                    dataset,
                    version=(current_source.version + 1 if current_source else 1),
                    actor=actor,
                    created_at=accounting.capture_finished_at,
                )
                snapshot = SourceSnapshot.create(
                    data_version_id=data_version_id,
                    dataset_id=dataset.dataset_id,
                    dataset_name=dataset.name,
                    source=dataset.source,
                    physical_selection_hash=source_selection.content_hash,
                    schema=schema,
                    row_count=candidate.row_count,
                    data_logical_hash=candidate.data_logical_hash,
                    parquet_sha256=candidate.parquet_sha256,
                    created_at=accounting.capture_finished_at,
                )
                require_not_cancelled(cancellation)
                self._artifacts.publish_source_snapshot(
                    data_version_id,
                    candidate.path,
                    snapshot.parquet_storage_key,
                    expected_sha256=candidate.parquet_sha256,
                )
                protected = self._provenance.prepare_capture_origins(
                    workspace_id,
                    actor=actor,
                    header=OdooCaptureOriginHeader(
                        high_water_id=accounting.high_water_id
                    ),
                    batches=origins,
                    row_count=accounting.row_count,
                    data_logical_hash=candidate.data_logical_hash,
                    data_sha256=candidate.parquet_sha256,
                    data_storage_key=snapshot.parquet_storage_key,
                    data_size_bytes=candidate.size_bytes,
                    capture_started_at=accounting.capture_started_at,
                    capture_finished_at=accounting.capture_finished_at,
                )
                require_not_cancelled(cancellation)
                _report_progress(
                    progress,
                    OdooCapturePhase.PUBLISHING,
                    completed_rows=accounting.row_count,
                    total_rows=result.request.maximum_rows,
                    page_count=accounting.page_count,
                    response_bytes=accounting.response_bytes,
                    normalized_bytes=accounting.normalized_bytes,
                )
                self._publications.publish_complete_capture(
                    workspace_id,
                    protected.manifest,
                    protected.encrypted_bytes,
                    source_selection,
                    snapshot,
                    actor=actor,
                )
                return OdooCapturePublication(
                    manifest=protected.manifest,
                    source_selection=source_selection,
                    source_snapshot=snapshot,
                    page_count=accounting.page_count,
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
    dataset: SourceDataset,
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
        datasets=(dataset,),
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
                "datasets": [dataset.to_dict()],
            }
        ),
    )
