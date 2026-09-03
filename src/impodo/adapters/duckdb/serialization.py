"""Boundary helpers between domain objects and DuckDB row/JSON shapes.

Workspace reconstruction converts persisted enum/date/source-file fields back
into validated domain objects. Canonical JSON uses stable key ordering and
compact separators. Columnar transposition supports bounded DuckDB batch
ingestion without changing semantic row order.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
import json


from impodo.domain.workspace.workbench import (
    ApprovalStatus,
    DataClassification,
    WorkspaceState,
    OdooConnectionMode,
    WorkspaceStatus,
    SourceMode,
    SourceFile,
)
from impodo.domain.workspace.destination_matching import DestinationMatchPlan
from impodo.domain.workspace.transfer_order import TransferOrderPlan
from impodo.domain.workspace.transfer_review import (
    TransferReviewApproval,
    TransferReviewPackage,
)


def _workspace_values(workspace: WorkspaceState) -> list[object]:
    """Flatten validated workbench state into the fixed workspace column order."""

    return [
        1,
        workspace.name,
        workspace.source_system,
        workspace.source_mode.value,
        workspace.data_classification.value,
        workspace.retention_days,
        (
            workspace.odoo_connection_mode.value
            if workspace.odoo_connection_mode
            else None
        ),
        workspace.odoo_base_url,
        workspace.odoo_database,
        json.dumps(workspace.intended_applications),
        json.dumps(workspace.intended_models),
        workspace.status.value,
        workspace.revision,
        workspace.created_at.isoformat(),
        workspace.updated_at.isoformat(),
        workspace.registered_at.isoformat() if workspace.registered_at else None,
        workspace.mapping_version,
        workspace.current_run_id,
        workspace.approval_status.value,
        (
            workspace.destination_odoo_connection_mode.value
            if workspace.destination_odoo_connection_mode
            else None
        ),
        workspace.destination_odoo_base_url,
        workspace.destination_odoo_database,
        workspace.destination_verified_target_hash,
        workspace.destination_verified_credential_binding_hash,
        workspace.destination_verified_read_principal_hash,
        workspace.destination_verified_odoo_version,
        (
            workspace.destination_verified_at.isoformat()
            if workspace.destination_verified_at
            else None
        ),
        (
            workspace.destination_match_plan.to_json()
            if workspace.destination_match_plan is not None
            else None
        ),
        (
            workspace.transfer_order_plan.to_json()
            if workspace.transfer_order_plan is not None
            else None
        ),
        (
            workspace.transfer_review_package.to_json()
            if workspace.transfer_review_package is not None
            else None
        ),
        (
            workspace.transfer_review_approval.to_json()
            if workspace.transfer_review_approval is not None
            else None
        ),
    ]


def _workspace_from_rows(
    data: dict[str, object],
    source_rows: list[tuple[object, ...]],
    *,
    workspace_id: str,
) -> WorkspaceState:
    """Rebuild one workbench projection and its immutable source-file children."""

    registered_at = (
        str(data["registered_at"]) if data["registered_at"] else None
    )
    connection_mode = (
        OdooConnectionMode(str(data["odoo_connection_mode"]))
        if data.get("odoo_connection_mode")
        else None
    )
    destination_connection_mode = (
        OdooConnectionMode(str(data["destination_odoo_connection_mode"]))
        if data.get("destination_odoo_connection_mode")
        else None
    )
    return WorkspaceState(
        workspace_id=workspace_id,
        name=str(data["name"]),
        source_system=str(data["source_system"]),
        source_mode=SourceMode(str(data.get("source_mode") or "FILE")),
        data_classification=DataClassification(
            str(data["data_classification"])
        ),
        retention_days=int(data["retention_days"]),
        odoo_connection_mode=connection_mode,
        odoo_base_url=str(data["odoo_base_url"]),
        odoo_database=str(data["odoo_database"]),
        destination_odoo_connection_mode=destination_connection_mode,
        destination_odoo_base_url=str(data["destination_odoo_base_url"]),
        destination_odoo_database=str(data["destination_odoo_database"]),
        destination_verified_target_hash=str(
            data["destination_verified_target_hash"]
        ),
        destination_verified_credential_binding_hash=str(
            data["destination_verified_credential_binding_hash"]
        ),
        destination_verified_read_principal_hash=str(
            data["destination_verified_read_principal_hash"]
        ),
        destination_verified_odoo_version=str(
            data["destination_verified_odoo_version"]
        ),
        destination_verified_at=(
            datetime.fromisoformat(str(data["destination_verified_at"]))
            if data.get("destination_verified_at")
            else None
        ),
        destination_match_plan=(
            DestinationMatchPlan.from_json(str(data["destination_match_plan_json"]))
            if data.get("destination_match_plan_json")
            else None
        ),
        transfer_order_plan=(
            TransferOrderPlan.from_json(str(data["transfer_order_plan_json"]))
            if data.get("transfer_order_plan_json")
            else None
        ),
        transfer_review_package=(
            TransferReviewPackage.from_json(
                str(data["transfer_review_package_json"])
            )
            if data.get("transfer_review_package_json")
            else None
        ),
        transfer_review_approval=(
            TransferReviewApproval.from_json(
                str(data["transfer_review_approval_json"])
            )
            if data.get("transfer_review_approval_json")
            else None
        ),
        intended_applications=tuple(json.loads(str(data["intended_applications"]))),
        intended_models=tuple(json.loads(str(data["intended_models"]))),
        source_files=tuple(
            SourceFile(
                file_id=str(row[0]),
                display_name=str(row[1]),
                stored_name=str(row[2]),
                size_bytes=int(row[3]),
                sha256=str(row[4]),
                received_at=datetime.fromisoformat(str(row[5])),
            )
            for row in source_rows
        ),
        status=WorkspaceStatus(str(data["status"])),
        revision=int(data["revision"]),
        created_at=datetime.fromisoformat(str(data["created_at"])),
        updated_at=datetime.fromisoformat(str(data["updated_at"])),
        registered_at=(
            datetime.fromisoformat(registered_at) if registered_at else None
        ),
        mapping_version=(
            str(data["mapping_version"]) if data["mapping_version"] else None
        ),
        current_run_id=(
            str(data["current_run_id"]) if data["current_run_id"] else None
        ),
        approval_status=ApprovalStatus(str(data["approval_status"])),
    )

def _canonical_json(value: object) -> str:
    """Serialize repository evidence with deterministic JSON key ordering."""

    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


@dataclass(frozen=True, slots=True)
class EncodedJsonBatch:
    """One bounded transport envelope and its non-sensitive measurements."""

    payload: str
    row_count: int
    byte_count: int


def iter_encoded_json_batches(
    rows: Iterable[Mapping[str, object]],
    *,
    max_rows: int,
    max_bytes: int,
) -> Iterator[EncodedJsonBatch]:
    """Encode fixed-shape adapter rows with row and UTF-8 byte guardrails."""

    if max_rows < 1:
        raise ValueError("DuckDB JSON batch row limit must be positive")
    if max_bytes < 3:
        raise ValueError("DuckDB JSON batch byte limit is too small")
    encoded_rows: list[str] = []
    payload_bytes = 2  # Opening and closing array brackets.
    for row in rows:
        encoded = json.dumps(
            dict(row),
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
        encoded_bytes = len(encoded.encode("utf-8"))
        if encoded_bytes + 2 > max_bytes:
            raise ValueError(
                "One DuckDB JSON transport row exceeds the byte limit"
            )
        separator_bytes = 1 if encoded_rows else 0
        if (
            encoded_rows
            and (
                len(encoded_rows) >= max_rows
                or payload_bytes + separator_bytes + encoded_bytes > max_bytes
            )
        ):
            yield _encoded_json_batch(encoded_rows, payload_bytes)
            encoded_rows = []
            payload_bytes = 2
            separator_bytes = 0
        encoded_rows.append(encoded)
        payload_bytes += separator_bytes + encoded_bytes
    if encoded_rows:
        yield _encoded_json_batch(encoded_rows, payload_bytes)


def _encoded_json_batch(
    encoded_rows: Sequence[str],
    byte_count: int,
) -> EncodedJsonBatch:
    payload = f"[{','.join(encoded_rows)}]"
    if len(payload.encode("utf-8")) != byte_count:
        raise AssertionError("DuckDB JSON transport byte count is inconsistent")
    return EncodedJsonBatch(
        payload=payload,
        row_count=len(encoded_rows),
        byte_count=byte_count,
    )


def _columnar_parameters(
    rows: Sequence[Sequence[object]],
) -> list[list[object]]:
    """Transpose one bounded row batch for DuckDB parameter-array ingestion."""

    if not rows:
        return []
    width = len(rows[0])
    if not width or any(len(row) != width for row in rows):
        raise ValueError("DuckDB bulk rows must use one non-empty shape")
    return [[row[index] for row in rows] for index in range(width)]
