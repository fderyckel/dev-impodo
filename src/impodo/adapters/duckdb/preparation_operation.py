"""Run one scenario Preparation command behind a durable DuckDB receipt.

The adapter is the first concrete shared-server data-plane command. It uses a
verified repository scenario as deterministic input, writes its prepared rows
to one immutable Parquet artifact, and records the exact artifact identity in
the workspace engine under the PostgreSQL lease fencing token.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import duckdb

from impodo.adapters.artifacts.local_store import LocalArtifactStore
from impodo.adapters.artifacts.profile_loader import load_profile
from impodo.adapters.scenarios.loader import LoadedScenario
from impodo.adapters.scenarios.source_preparation import prepare_scenario_sources
from impodo.application.shared.cross_store_coordinator import (
    BoundaryCheckpoint,
    CrossStoreBoundary,
)
from impodo.domain.compiler import compile_profile_document
from impodo.domain.compiler.contracts import CompiledMigrationPlan
from impodo.domain.preparation.source import PreparedBundle
from impodo.domain.prepared_snapshot import PreparedSnapshot
from impodo.domain.scenarios import FileScenarioSource
from impodo.domain.serialization import content_hash
from impodo.domain.shared.coordination import (
    ArtifactIdentity,
    DataPlaneOperationReceipt,
    OperationRequest,
    ReceiptMismatchError,
    StaleLeaseError,
    WorkspaceLease,
)
from impodo.domain.shared.models import (
    PreparedRecord,
    canonical_json_text,
    portable_issue,
    portable_value,
)
from impodo.domain.shared.tenancy import OrganizationId
from impodo.domain.workspace.errors import WorkspaceError

from .database import DuckDbWorkspaceDatabase

PREPARATION_OPERATION_KIND = "workspace.prepare"
PREPARATION_ARTIFACT_MEDIA_TYPE = "application/vnd.apache.parquet"
_PREPARED_ROW_BATCH_SIZE = 1_000
_PREPARED_ROW_SCHEMA = (
    ("dataset", "VARCHAR"),
    ("source_row", "BIGINT"),
    ("target_model", "VARCHAR"),
    ("source_identity_json", "VARCHAR"),
    ("target_identity_json", "VARCHAR"),
    ("target_scope_json", "VARCHAR"),
    ("scalar_values_json", "VARCHAR"),
    ("references_json", "VARCHAR"),
    ("source_trace_id", "VARCHAR"),
    ("issues_json", "VARCHAR"),
)


class ScenarioPreparationContractError(ValueError):
    """Report that a scenario cannot produce the promised preparation evidence."""


class DuckDbPreparationReceiptError(RuntimeError):
    """Report malformed or conflicting durable preparation receipt evidence."""


class DuckDbPreparationDataPlaneCommand:
    """Prepare a verified scenario and commit one fenced workspace receipt."""

    def __init__(
        self,
        database: DuckDbWorkspaceDatabase,
        artifacts: LocalArtifactStore,
        scenario: LoadedScenario,
        workspace_id: str,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if (
            not isinstance(scenario.definition.source, FileScenarioSource)
            or scenario.fixture_directory is None
            or scenario.profile_path is None
            or scenario.definition.rules.profile_hash is None
        ):
            raise ScenarioPreparationContractError(
                "Cross-store preparation requires one verified file scenario"
            )
        self._database = database
        self._artifacts = artifacts
        self._scenario = scenario
        self._source = scenario.definition.source
        self._workspace_id = workspace_id
        self._clock = clock

    def execute(
        self,
        request: OperationRequest,
        lease: WorkspaceLease,
        checkpoint: BoundaryCheckpoint,
    ) -> DataPlaneOperationReceipt:
        """Stage Parquet, commit its fenced receipt, and publish exact bytes."""

        self._require_request(request, lease)
        database_path = (
            self._database.workspace_directory(lease.workspace_id)
            / "workspace-engine.duckdb"
        )
        if not database_path.is_file():
            raise WorkspaceError("Workspace engine state not found")

        with self._database.connection_factory.connect(
            database_path,
            memory_limit="256MB",
            threads="1",
            preserve_insertion_order=False,
            enable_external_access=True,
        ) as connection:
            self._database._ensure_workspace_database_schema(connection)
            existing = _read_receipt(connection, request.operation_id)
            if existing is not None:
                existing.require_operation(request)
                existing.require_lease(lease)
                return existing

            plan, prepared = self._prepare_scenario()
            with self._artifacts.prepare_prepared_snapshot(
                lease.workspace_id
            ) as work_directory:
                candidate_path = work_directory / "prepared.parquet"
                snapshot = self._write_snapshot(
                    connection,
                    lease.workspace_id,
                    plan,
                    prepared,
                    candidate_path,
                )
                artifact = ArtifactIdentity(
                    artifact_id=str(
                        uuid5(
                            NAMESPACE_URL,
                            f"impodo:prepared-artifact:{snapshot.content_hash}",
                        )
                    ),
                    owner_kind="workspace",
                    owner_id=lease.workspace_id,
                    artifact_kind="prepared_snapshot",
                    storage_key=snapshot.parquet_storage_key,
                    sha256=snapshot.parquet_sha256,
                    size_bytes=candidate_path.stat().st_size,
                    media_type=PREPARATION_ARTIFACT_MEDIA_TYPE,
                )
                checkpoint(CrossStoreBoundary.AFTER_ARTIFACT_STAGING)

                committed_at = self._now()
                lease.require_fencing_token(
                    lease.fencing_token,
                    at=committed_at,
                )
                evidence_hash = content_hash(
                    {
                        "prepared_rows": len(prepared.records),
                        "scenario_hash": self._scenario.scenario_hash,
                        "snapshot": snapshot.to_portable_dict(),
                        "source_hashes": prepared.source_hashes,
                        "source_issues": len(prepared.issues),
                    }
                )
                receipt = DataPlaneOperationReceipt(
                    receipt_id=str(
                        uuid5(
                            NAMESPACE_URL,
                            "impodo:data-plane-receipt:"
                            f"{request.operation_id}:{evidence_hash}:"
                            f"{lease.fencing_token}",
                        )
                    ),
                    operation_id=request.operation_id,
                    organization_id=request.organization_id,
                    project_id=request.project_id,
                    workspace_id=lease.workspace_id,
                    operation_kind=request.operation_kind,
                    semantic_input_hash=request.semantic_input_hash,
                    fencing_token=lease.fencing_token,
                    evidence_hash=evidence_hash,
                    artifacts=(artifact,),
                    committed_at=committed_at,
                )
                _commit_fenced_receipt(connection, receipt, lease)
                checkpoint(CrossStoreBoundary.AFTER_DATA_PLANE_COMMIT)

                self._artifacts.publish_prepared_snapshot(
                    lease.workspace_id,
                    candidate_path,
                    snapshot.parquet_storage_key,
                    expected_sha256=snapshot.parquet_sha256,
                )
                checkpoint(CrossStoreBoundary.AFTER_ARTIFACT_PUBLICATION)
                return receipt

    def find_receipt(
        self,
        request: OperationRequest,
    ) -> DataPlaneOperationReceipt | None:
        """Recover one exact committed receipt without preparing rows again."""

        if request.operation_kind != PREPARATION_OPERATION_KIND:
            raise ReceiptMismatchError("operation kind is not Preparation")
        database_path = (
            self._database.workspace_directory(self._workspace_id)
            / "workspace-engine.duckdb"
        )
        if not database_path.is_file():
            return None
        with self._database.connection_factory.connect(database_path) as connection:
            self._database._ensure_workspace_database_schema(connection)
            receipt = _read_receipt(connection, request.operation_id)
        if receipt is not None:
            receipt.require_operation(request)
        return receipt

    def _prepare_scenario(self) -> tuple[CompiledMigrationPlan, PreparedBundle]:
        profile_path = self._scenario.profile_path
        fixture_directory = self._scenario.fixture_directory
        if profile_path is None or fixture_directory is None:
            raise ScenarioPreparationContractError(
                "Scenario preparation inputs are unavailable"
            )
        plan = compile_profile_document(load_profile(profile_path))
        prepared = prepare_scenario_sources(
            plan,
            fixture_directory,
            self._source.combined_distinct_tables,
        )
        expected = self._scenario.definition.expectations
        if (
            len(prepared.records) != expected.prepared_rows
            or len(prepared.issues) != expected.source_issues
        ):
            raise ScenarioPreparationContractError(
                "Scenario preparation does not match its reviewed expectations"
            )
        return plan, prepared

    def _write_snapshot(
        self,
        connection: duckdb.DuckDBPyConnection,
        workspace_id: str,
        plan: CompiledMigrationPlan,
        prepared: PreparedBundle,
        candidate_path: Path,
    ) -> PreparedSnapshot:
        columns = ", ".join(f"{name} {kind}" for name, kind in _PREPARED_ROW_SCHEMA)
        connection.execute(f"CREATE TEMP TABLE shared_prepared_rows ({columns})")
        placeholders = ", ".join("?" for _ in _PREPARED_ROW_SCHEMA)
        insert_query = f"INSERT INTO shared_prepared_rows VALUES ({placeholders})"
        for batch in _batched(prepared.records, _PREPARED_ROW_BATCH_SIZE):
            connection.executemany(
                insert_query,
                [_prepared_row_values(record) for record in batch],
            )
        connection.execute(
            "COPY shared_prepared_rows TO ? (FORMAT PARQUET, COMPRESSION ZSTD)",
            [str(candidate_path)],
        )
        connection.execute("DROP TABLE shared_prepared_rows")

        parquet_sha256 = _file_hash(candidate_path)
        scenario_hash = self._scenario.scenario_hash
        dataset_id = "dataset:" + sha256(
            self._scenario.definition.scenario_id.encode("utf-8")
        ).hexdigest()[:24]
        profile_hash = self._scenario.definition.rules.profile_hash
        if profile_hash is None:
            raise ScenarioPreparationContractError("Scenario profile hash is missing")
        return PreparedSnapshot.create(
            workspace_id=workspace_id,
            dataset_id=dataset_id,
            dataset_name=self._scenario.definition.scenario_id,
            source_snapshot_hash=self._scenario.fixture_hash,
            mapping_hash=profile_hash,
            schema_hash=plan.semantic_hash,
            transformation_program_hash=content_hash(
                {
                    "compiled_plan": plan.semantic_hash,
                    "scenario": scenario_hash,
                }
            ),
            row_count=len(prepared.records),
            physical_schema_hash=content_hash(
                {
                    "columns": _PREPARED_ROW_SCHEMA,
                    "format": "parquet",
                }
            ),
            parquet_sha256=parquet_sha256,
            created_at=self._now(),
        )

    def _require_request(
        self,
        request: OperationRequest,
        lease: WorkspaceLease,
    ) -> None:
        if (
            request.operation_kind != PREPARATION_OPERATION_KIND
            or request.semantic_input_hash != self._scenario.scenario_hash
            or request.organization_id != lease.organization_id
            or request.project_id != lease.project_id
            or lease.workspace_id != self._workspace_id
        ):
            raise ReceiptMismatchError(
                "Preparation request, scenario, and lease do not match"
            )

    def _now(self) -> datetime:
        current = self._clock()
        if current.utcoffset() is None:
            raise ValueError("Preparation clock must return an aware timestamp")
        return current


def _prepared_row_values(record: PreparedRecord) -> tuple[object, ...]:
    return (
        record.dataset,
        record.source_row,
        record.target_model,
        canonical_json_text(portable_value(record.source_identity)),
        canonical_json_text(portable_value(record.target_identity)),
        canonical_json_text(portable_value(record.target_scope)),
        canonical_json_text(portable_value(record.scalar_values)),
        canonical_json_text(portable_value(record.references)),
        record.source_trace_id,
        canonical_json_text([portable_issue(issue) for issue in record.issues]),
    )


def _batched(
    rows: tuple[PreparedRecord, ...],
    size: int,
) -> Iterable[tuple[PreparedRecord, ...]]:
    for start in range(0, len(rows), size):
        yield rows[start : start + size]


def _commit_fenced_receipt(
    connection: duckdb.DuckDBPyConnection,
    receipt: DataPlaneOperationReceipt,
    lease: WorkspaceLease,
) -> None:
    receipt.require_lease(lease)
    connection.begin()
    try:
        fence = connection.execute(
            """
            SELECT organization_id, project_id, fencing_token, operation_id
              FROM workspace_fence
             WHERE workspace_id = ?
            """,
            [lease.workspace_id],
        ).fetchone()
        if fence is not None:
            same_scope = fence[:2] == (
                str(lease.organization_id),
                lease.project_id,
            )
            if not same_scope or int(fence[2]) >= lease.fencing_token:
                raise StaleLeaseError(
                    "Workspace data-plane write is stale or superseded"
                )
        connection.execute(
            """
            INSERT INTO cross_store_data_receipt VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            """,
            [
                receipt.receipt_id,
                receipt.operation_id,
                str(receipt.organization_id),
                receipt.project_id,
                receipt.workspace_id,
                receipt.operation_kind,
                receipt.semantic_input_hash,
                receipt.fencing_token,
                receipt.evidence_hash,
                _artifacts_json(receipt.artifacts),
                receipt.semantic_hash,
                receipt.committed_at.isoformat(),
            ],
        )
        connection.execute(
            """
            INSERT OR REPLACE INTO workspace_fence VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                lease.workspace_id,
                str(lease.organization_id),
                lease.project_id,
                lease.fencing_token,
                receipt.operation_id,
                receipt.committed_at.isoformat(),
            ],
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def _read_receipt(
    connection: duckdb.DuckDBPyConnection,
    operation_id: str,
) -> DataPlaneOperationReceipt | None:
    row = connection.execute(
        """
        SELECT receipt_id, operation_id, organization_id, project_id,
               workspace_id, operation_kind, semantic_input_hash,
               fencing_token, evidence_hash, artifacts_json, receipt_hash,
               committed_at
          FROM cross_store_data_receipt
         WHERE operation_id = ?
        """,
        [operation_id],
    ).fetchone()
    if row is None:
        return None
    try:
        raw_artifacts = json.loads(str(row[9]))
        if not isinstance(raw_artifacts, list):
            raise TypeError
        artifacts = tuple(
            ArtifactIdentity(
                artifact_id=str(item["artifact_id"]),
                owner_kind=str(item["owner_kind"]),
                owner_id=str(item["owner_id"]),
                artifact_kind=str(item["artifact_kind"]),
                storage_key=str(item["storage_key"]),
                sha256=str(item["sha256"]),
                size_bytes=int(item["size_bytes"]),
                media_type=str(item["media_type"]),
            )
            for item in raw_artifacts
            if isinstance(item, dict)
        )
        if len(artifacts) != len(raw_artifacts):
            raise TypeError
        receipt = DataPlaneOperationReceipt(
            receipt_id=str(row[0]),
            operation_id=str(row[1]),
            organization_id=OrganizationId(str(row[2])),
            project_id=str(row[3]),
            workspace_id=str(row[4]),
            operation_kind=str(row[5]),
            semantic_input_hash=str(row[6]),
            fencing_token=int(row[7]),
            evidence_hash=str(row[8]),
            artifacts=artifacts,
            committed_at=datetime.fromisoformat(str(row[11])),
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise DuckDbPreparationReceiptError(
            "Stored Preparation receipt is invalid"
        ) from error
    if receipt.semantic_hash != str(row[10]):
        raise DuckDbPreparationReceiptError(
            "Stored Preparation receipt hash is invalid"
        )
    return receipt


def _artifacts_json(artifacts: tuple[ArtifactIdentity, ...]) -> str:
    return canonical_json_text(
        [
            {
                "artifact_id": artifact.artifact_id,
                "artifact_kind": artifact.artifact_kind,
                "media_type": artifact.media_type,
                "owner_id": artifact.owner_id,
                "owner_kind": artifact.owner_kind,
                "sha256": artifact.sha256,
                "size_bytes": artifact.size_bytes,
                "storage_key": artifact.storage_key,
            }
            for artifact in artifacts
        ]
    )


def _file_hash(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


__all__ = (
    "PREPARATION_OPERATION_KIND",
    "DuckDbPreparationDataPlaneCommand",
    "DuckDbPreparationReceiptError",
    "ScenarioPreparationContractError",
)
