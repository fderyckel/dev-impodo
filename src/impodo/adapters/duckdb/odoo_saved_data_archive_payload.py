"""Validate the closed semantic payload inside a password-protected archive."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from impodo.application.data_version.source_snapshots import (
    validate_snapshot_for_dataset,
)
from impodo.domain.odoo.company import OdooCompanySelection
from impodo.domain.odoo.compatibility import OdooOperation, assess_odoo_operation
from impodo.domain.odoo_capture import OdooCaptureFilterClause, OdooCaptureSelection
from impodo.domain.odoo_provenance import (
    OdooCaptureManifest,
    OdooCaptureOriginHeader,
    OdooOriginBatch,
    OdooRelationshipOriginColumn,
)
from impodo.domain.odoo_relationship_scope import OdooRelationshipScope
from impodo.domain.odoo_source_capture import validate_odoo_capture_selection_reference
from impodo.domain.run.setup import OdooConnectionMode, validate_odoo_base_url
from impodo.domain.serialization import canonical_json, content_hash
from impodo.domain.shared.models import target_identity_hash
from impodo.domain.source_snapshot import SourceSnapshot
from impodo.domain.workspace.contracts import (
    WORKSPACE_EVIDENCE_IDENTITY_CONTRACT_VERSION,
    OdooSchemaCatalog,
    SourceSelection,
)
from impodo.domain.workspace.errors import WorkspaceError


@dataclass(frozen=True, slots=True)
class OdooArchiveDataset:
    """Carry one validated capture and the verified transport members."""

    selection: OdooCaptureSelection
    snapshot: SourceSnapshot
    manifest: OdooCaptureManifest
    filters: tuple[OdooCaptureFilterClause, ...]
    header: OdooCaptureOriginHeader
    origins: tuple[OdooOriginBatch, ...]
    data_member: str
    path: Path


@dataclass(frozen=True, slots=True)
class OdooArchivePayload:
    """Keep historical source provenance separate from new local ownership."""

    label: str
    all_fields: bool
    source_system: str
    source_mode: OdooConnectionMode
    source_url: str
    source_database: str
    company: OdooCompanySelection
    schema: OdooSchemaCatalog
    scope: OdooRelationshipScope | None
    source_selection: SourceSelection
    datasets: tuple[OdooArchiveDataset, ...]


def encode_origins(
    header: OdooCaptureOriginHeader, batches: tuple[OdooOriginBatch, ...]
) -> bytes:
    """Serialize origin columns only for an encrypted transport member."""
    return canonical_json(
        {
            "high_water_id": header.high_water_id,
            "batches": [
                {
                    "first_row_ordinal": batch.first_row_ordinal,
                    "odoo_ids": batch.odoo_ids,
                    "write_dates": [
                        value.isoformat() if value is not None else None
                        for value in batch.write_dates
                    ],
                    "relationships": [
                        {
                            "field_name": field.field_name,
                            "kind": field.kind,
                            "relation_model": field.relation_model,
                            "values": field.values,
                        }
                        for field in batch.relationships
                    ],
                }
                for batch in batches
            ],
        }
    ).encode("utf-8")


def decode_origins(
    path: Path, row_count: int
) -> tuple[OdooCaptureOriginHeader, tuple[OdooOriginBatch, ...]]:
    """Check bounded domain columns and complete row coverage after decryption."""
    payload = _object(
        json.loads(path.read_bytes(), object_pairs_hook=_unique),
        {"high_water_id", "batches"},
    )
    header = OdooCaptureOriginHeader(payload["high_water_id"])
    batches = []
    expected_ordinal = 1
    previous_id = 0
    for raw in _list(payload["batches"]):
        item = _object(
            raw, {"first_row_ordinal", "odoo_ids", "write_dates", "relationships"}
        )
        relationships = []
        for raw_field in _list(item["relationships"]):
            field = _object(
                raw_field, {"field_name", "kind", "relation_model", "values"}
            )
            relationships.append(
                OdooRelationshipOriginColumn(
                    field["field_name"],
                    field["kind"],
                    field["relation_model"],
                    tuple(tuple(values) for values in _list(field["values"])),
                )
            )
        batch = OdooOriginBatch(
            item["first_row_ordinal"],
            tuple(_list(item["odoo_ids"])),
            tuple(
                datetime.fromisoformat(value) if value is not None else None
                for value in _list(item["write_dates"])
            ),
            tuple(relationships),
        )
        if (
            batch.first_row_ordinal != expected_ordinal
            or batch.odoo_ids[0] <= previous_id
            or batch.odoo_ids[-1] > header.high_water_id
        ):
            raise WorkspaceError("Saved Odoo origin row coverage is inconsistent")
        expected_ordinal += batch.row_count
        previous_id = batch.odoo_ids[-1]
        batches.append(batch)
    if expected_ordinal - 1 != row_count:
        raise WorkspaceError("Saved Odoo origins do not cover the captured rows")
    return header, tuple(batches)


def decode_payload(
    metadata: bytes, member_paths: dict[str, Path]
) -> OdooArchivePayload:
    """Validate all semantic model coverage before creating imported owners."""
    if len(metadata) > 8 * 1024 * 1024:
        raise WorkspaceError("Saved Odoo metadata exceeds its supported size")
    payload = _object(
        json.loads(metadata, object_pairs_hook=_unique),
        {
            "format_version",
            "label",
            "all_fields",
            "source_system",
            "source_mode",
            "source_url",
            "source_database",
            "company",
            "schema",
            "scope",
            "source_selection",
            "datasets",
        },
    )
    if payload["format_version"] != 1 or type(payload["all_fields"]) is not bool:
        raise WorkspaceError("Saved Odoo archive metadata version is invalid")
    schema = OdooSchemaCatalog.from_json(payload["schema"])
    company = OdooCompanySelection.from_json(payload["company"])
    source = SourceSelection.from_json(payload["source_selection"])
    scope = (
        OdooRelationshipScope.from_json(payload["scope"])
        if payload["scope"] is not None
        else None
    )
    source_mode = OdooConnectionMode(payload["source_mode"])
    source_url = validate_odoo_base_url(payload["source_url"], source_mode)
    source_target = target_identity_hash(
        connection_mode=source_mode.value,
        base_url=source_url,
        database=payload["source_database"],
    )
    if (
        not assess_odoo_operation(
            schema.odoo_version, OdooOperation.CAPTURE_SOURCE
        ).allowed
        or schema.pending_refresh is not None
        or company.target_hash != schema.connection_target_hash
        or source_target != schema.connection_target_hash
        or schema.read_context_hash != company.context_hash
    ):
        raise WorkspaceError("Saved Odoo archive source or company evidence differs")
    rows = _list(payload["datasets"])
    if not rows or len(rows) > 100:
        raise WorkspaceError("Saved Odoo archive model coverage is invalid")
    datasets = []
    expected_members = {"metadata/source.json"}
    source_by_id = {item.dataset_id: item for item in source.datasets}
    expected_hash = content_hash(
        {
            "contract_version": WORKSPACE_EVIDENCE_IDENTITY_CONTRACT_VERSION,
            "data_version_id": source.data_version_id,
            "version": source.version,
            "datasets": [item.to_dict() for item in source.datasets],
        }
    )
    if expected_hash != source.content_hash or len(source_by_id) != len(
        source.datasets
    ):
        raise WorkspaceError("Saved Odoo source selection integrity differs")
    for raw in rows:
        item = _object(
            raw,
            {
                "selection",
                "snapshot",
                "manifest",
                "filters",
                "data_member",
                "origin_member",
            },
        )
        capture = OdooCaptureSelection.from_json(item["selection"])
        snapshot = SourceSnapshot.from_json(item["snapshot"])
        manifest = OdooCaptureManifest.from_json(item["manifest"])
        validate_odoo_capture_selection_reference(capture, schema)
        dataset = source_by_id.get(capture.dataset_id)
        if dataset is None:
            raise WorkspaceError("Saved Odoo archive has an unselected dataset")
        validate_snapshot_for_dataset(source, dataset, snapshot)
        if (
            capture.data_version_id != source.data_version_id
            or not manifest.binds_selection(capture)
            or manifest.data_version_id != source.data_version_id
            or dataset.source != capture.source_binding
            or manifest.data_sha256 != snapshot.parquet_sha256
            or manifest.data_logical_hash != snapshot.data_logical_hash
            or manifest.row_count != snapshot.row_count
        ):
            raise WorkspaceError("Saved Odoo archive model evidence differs")
        filters = tuple(
            OdooCaptureFilterClause.from_dict(value) for value in _list(item["filters"])
        )
        if bool(filters) != bool(
            capture.filter_clauses or capture.protected_filter_artifact_hash
        ) or (capture.filter_clauses and filters != capture.filter_clauses):
            raise WorkspaceError("Saved Odoo archive filter evidence is incomplete")
        for name in (item["data_member"], item["origin_member"]):
            if (
                not isinstance(name, str)
                or name not in member_paths
                or name in expected_members
            ):
                raise WorkspaceError("Saved Odoo archive artifacts are incomplete")
            expected_members.add(name)
        header, origins = decode_origins(
            member_paths[item["origin_member"]], manifest.row_count
        )
        datasets.append(
            OdooArchiveDataset(
                capture,
                snapshot,
                manifest,
                filters,
                header,
                origins,
                item["data_member"],
                member_paths[item["data_member"]],
            )
        )
    if (
        set(member_paths) != expected_members
        or len({item.selection.model for item in datasets}) != len(datasets)
        or {item.selection.model for item in datasets}
        != {item.name for item in schema.models}
        or {item.selection.dataset_id for item in datasets} != set(source_by_id)
    ):
        raise WorkspaceError("Saved Odoo archive has unexpected or missing models")
    _relationships(tuple(datasets))
    if (
        not isinstance(payload["label"], str)
        or not payload["label"].strip()
        or len(payload["label"]) > 300
        or not isinstance(payload["source_system"], str)
        or not payload["source_system"].strip()
        or len(payload["source_system"]) > 200
    ):
        raise WorkspaceError("Saved Odoo archive labels are invalid")
    return OdooArchivePayload(
        payload["label"],
        payload["all_fields"],
        payload["source_system"],
        source_mode,
        source_url,
        payload["source_database"],
        company,
        schema,
        scope,
        source,
        tuple(datasets),
    )


def _relationships(datasets: tuple[OdooArchiveDataset, ...]) -> None:
    ids = {
        item.selection.model: {
            identifier for batch in item.origins for identifier in batch.odoo_ids
        }
        for item in datasets
    }
    for item in datasets:
        for batch in item.origins:
            for field in batch.relationships:
                available = ids.get(field.relation_model, set())
                if any(
                    identifier not in available
                    for values in field.values
                    for identifier in values
                ):
                    raise WorkspaceError(
                        "Saved Odoo archive relationship coverage is incomplete"
                    )


def _object(value: object, expected: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != expected:
        raise WorkspaceError("Saved Odoo archive metadata is invalid")
    return value


def _list(value: object) -> list[Any]:
    if not isinstance(value, list):
        raise WorkspaceError("Saved Odoo archive collection is invalid")
    return value


def _unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise WorkspaceError("Saved Odoo archive contains duplicate metadata")
        result[key] = value
    return result
