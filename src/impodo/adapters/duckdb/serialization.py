"""Boundary helpers between domain objects and DuckDB row/JSON shapes.

Project reconstruction converts persisted enum/date/source-file fields back
into validated domain objects. Canonical JSON uses stable key ordering and
compact separators. Columnar transposition supports bounded DuckDB batch
ingestion without changing semantic row order.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import (
    date,
    datetime,
)
import json


from ...projects import (
    ApprovalStatus,
    DataClassification,
    ExportStatus,
    MigrationProject,
    OdooConnectionMode,
    ProjectStatus,
    SourceMode,
    SourceFile,
)





def _project_values(project: MigrationProject) -> list[object]:
    """Flatten a validated project into the fixed project-table column order."""

    return [
        project.project_id,
        project.name,
        project.source_system,
        project.source_mode.value,
        project.export_status.value,
        project.export_date.isoformat() if project.export_date else None,
        project.description,
        project.data_manager,
        project.functional_owner,
        project.business_unit,
        project.data_classification.value,
        project.retention_days,
        project.support_access,
        (
            project.odoo_connection_mode.value
            if project.odoo_connection_mode
            else None
        ),
        project.odoo_base_url,
        project.odoo_database,
        json.dumps(project.intended_applications),
        json.dumps(project.intended_models),
        project.status.value,
        project.revision,
        project.created_at.isoformat(),
        project.updated_at.isoformat(),
        project.registered_at.isoformat() if project.registered_at else None,
        project.mapping_version,
        project.current_run_id,
        project.approval_status.value,
    ]

def _project_from_rows(
    data: dict[str, object],
    source_rows: list[tuple[object, ...]],
) -> MigrationProject:
    """Rebuild one project aggregate and its immutable source-file children."""

    export_date = str(data["export_date"]) if data["export_date"] else None
    registered_at = (
        str(data["registered_at"]) if data["registered_at"] else None
    )
    connection_mode = (
        OdooConnectionMode(str(data["odoo_connection_mode"]))
        if data.get("odoo_connection_mode")
        else None
    )
    return MigrationProject(
        project_id=str(data["project_id"]),
        name=str(data["name"]),
        source_system=str(data["source_system"]),
        source_mode=SourceMode(str(data.get("source_mode") or "FILE")),
        export_status=ExportStatus(str(data["export_status"])),
        export_date=date.fromisoformat(export_date) if export_date else None,
        description=str(data["description"]),
        data_manager=str(data["data_manager"]),
        functional_owner=str(data["functional_owner"]),
        business_unit=str(data["business_unit"]),
        data_classification=DataClassification(
            str(data["data_classification"])
        ),
        retention_days=int(data["retention_days"]),
        support_access=bool(data["support_access"]),
        odoo_connection_mode=connection_mode,
        odoo_base_url=str(data["odoo_base_url"]),
        odoo_database=str(data["odoo_database"]),
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
        status=ProjectStatus(str(data["status"])),
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
