"""Deterministic direct-row projection shared by preparation and readers."""

from __future__ import annotations

from hashlib import sha256
from typing import Any, Mapping

from ...models import Issue, canonical_json_bytes, portable_issue, portable_value
from ...staging_contracts import StagingDisposition
from .preparation_session import CanonicalPreparedSessionRow


def canonical_prepared_session_row(
    *,
    dataset: str,
    source_row: int,
    target_model: str,
    source_identity: tuple[Any, ...],
    target_identity: tuple[Any, ...],
    target_scope: tuple[Any, ...],
    scalar_values: Mapping[str, Any],
    issues: tuple[Issue, ...],
    ordinal: int,
    mode: str,
    source_hash: str,
    source_selection_hash: str,
    mapping_hash: str,
    schema_hash: str,
    field_sources: Mapping[str, tuple[str, ...]],
    physical_dataset_id: str,
    encode_payload: bool = True,
) -> CanonicalPreparedSessionRow:
    """Build exact canonical metadata and optionally its compatibility JSON."""

    physical_sources = {physical_dataset_id: (source_row,)}
    lineage = {
        "source_selection_hash": source_selection_hash,
        "source_hash": source_hash,
        "mapping_hash": mapping_hash,
        "schema_hash": schema_hash,
        "derived_plan_hash": None,
        "dataset": dataset,
        "source_row": source_row,
        "physical_dataset_id": physical_dataset_id,
        "physical_source_rows": [source_row],
        "field_sources": {
            field: list(sources)
            for field, sources in sorted(field_sources.items())
        },
    }
    portable_source_identity = portable_value(source_identity)
    row_id = "sha256:" + sha256(
        canonical_json_bytes(
            {
                "lineage": lineage,
                "target_model": target_model,
                "source_identity": portable_source_identity,
            }
        )
    ).hexdigest()
    disposition = (
        StagingDisposition.BLOCKED.value
        if any(item.blocking for item in issues)
        else (
            StagingDisposition.REFERENCE.value
            if mode == "reference"
            else StagingDisposition.CANDIDATE.value
        )
    )
    row_json = ""
    if encode_payload:
        canonical_issues = []
        for issue in issues:
            portable = portable_issue(issue)
            canonical_issues.append(
                {
                    "code": portable["code"],
                    "message": portable["message"],
                    "severity": portable["severity"],
                    "dataset": portable["dataset"],
                    "source_row": portable["row"],
                    "field": portable["field"],
                    "affected_count": portable["affected_count"],
                }
            )
        payload = {
            "row_id": row_id,
            "dataset": dataset,
            "source_row": source_row,
            "target_model": target_model,
            "disposition": disposition,
            "source_identity": portable_source_identity,
            "target_identity": portable_value(target_identity),
            "target_scope": portable_value(target_scope),
            "proposed_values": portable_value(scalar_values),
            "references": {},
            "issues": canonical_issues,
            "lineage": lineage,
        }
        row_json = canonical_json_bytes(payload).decode("utf-8")
    return CanonicalPreparedSessionRow(
        row_id=row_id,
        ordinal=ordinal,
        dataset=dataset,
        source_row=source_row,
        target_model=target_model,
        disposition=StagingDisposition(disposition),
        source_identity=source_identity,
        row_json=row_json,
        physical_sources=physical_sources,
    )
