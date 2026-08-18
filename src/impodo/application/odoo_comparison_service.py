"""Build one bounded, read-only comparison for pinned Odoo source rows."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from hashlib import sha256
from typing import Callable, Iterable

from ..access import Actor
from ..artifacts import ArtifactStore, ArtifactStoreError
from ..connectors import (
    MetadataRequest,
    MetadataSnapshot,
    RecordRequest,
    RecordSnapshot,
    bind_snapshot_hashes,
)
from ..domain.errors import ReadinessError
from ..domain.mapping.contracts import MappingTargetMode
from ..domain.odoo_comparison import (
    OdooComparisonArtifact,
    OdooComparisonError,
    OdooComparisonOutcome,
    OdooComparisonRow,
    OdooFieldComparison,
    OdooFieldComparisonOutcome,
    canonical_odoo_scalar,
    canonical_write_date,
)
from ..domain.odoo_provenance import OdooCaptureManifest, OdooOriginBatch
from ..domain.source_binding import OdooSourceBinding, SourceOriginKind
from ..domain.preflight.frozen_input import FrozenPreflightInput
from ..domain.preflight.reports import (
    ReadinessDataset,
    ReadinessReport,
    ReadinessRow,
)
from ..domain.source_snapshot import SourceSnapshot
from ..models import (
    FieldMetadata,
    TargetRecord,
    assert_no_numeric_odoo_ids,
    canonical_json_bytes,
)
from ..projects import MigrationProject
from ..source_snapshot_io import load_source_snapshot_table, validate_snapshot_for_dataset
from ..workspace_contracts import OdooSchemaCatalog, SchemaField, SourceSelection
from ..workspace_errors import WorkspaceError
from .odoo_provenance_service import (
    OdooProvenanceService,
    ProtectedOdooComparisonCandidate,
)


ODOO_COMPARISON_ARTIFACT_NAME = "impodo_odoo_comparison.protected.json"
ODOO_COMPARISON_CHUNK_SIZE = 500

OdooComparisonReader = Callable[
    [tuple[MetadataRequest, ...], tuple[RecordRequest, ...]],
    tuple[MetadataSnapshot, RecordSnapshot],
]


@dataclass(frozen=True, slots=True)
class OdooComparisonPublication:
    """Portable and protected outputs awaiting one current-pointer publish."""

    report: ReadinessReport
    rows: tuple[ReadinessRow, ...]
    metadata_snapshot: MetadataSnapshot
    redacted_record_snapshot: RecordSnapshot
    portable_manifest: bytes
    protected: ProtectedOdooComparisonCandidate
    artifact: OdooComparisonArtifact


def build_odoo_comparison_publication(
    *,
    project: MigrationProject,
    frozen: FrozenPreflightInput,
    selection: SourceSelection,
    source_snapshots: tuple[SourceSnapshot, ...],
    artifacts: ArtifactStore,
    provenance: OdooProvenanceService,
    reader: OdooComparisonReader,
    actor: Actor,
    run_id: str,
) -> OdooComparisonPublication:
    """Verify protected origins, read exact IDs in chunks, and classify rows."""

    dataset_mapping, dataset, binding = _pinned_context(frozen, selection)
    manifest, origin_batches = _protected_origins(
        project.project_id,
        selection,
        dataset.dataset_id,
        binding,
        source_snapshots,
        provenance,
        actor,
    )
    snapshot = source_snapshots[0]
    baselines = _load_baselines(
        artifacts,
        project.project_id,
        selection,
        dataset,
        snapshot,
    )
    origins = _origin_by_ordinal(origin_batches, expected_count=manifest.row_count)
    prepared = tuple(sorted(frozen.prepared.records, key=lambda item: item.source_row))
    if any(item.dataset != dataset.name for item in prepared):
        raise ReadinessError(
            "The prepared Odoo rows do not match the protected capture. Refresh the capture."
        )
    approved_fields = tuple(dataset_mapping.approved_write_fields)
    fields = tuple(sorted({*approved_fields, "write_date"}))
    record_ids = tuple(_origin_for_record(item.source_row, origins)[0] for item in prepared)
    metadata_requests = (MetadataRequest(binding.model, fields),)
    record_requests = plan_pinned_record_requests(
        binding.model,
        fields,
        record_ids,
    )
    metadata, records = reader(metadata_requests, record_requests)
    metadata, records = bind_snapshot_hashes(metadata, records)
    _validate_live_binding(project, binding, metadata, records, fields)

    captured_fields = _captured_fields(frozen.captured_schema, binding.model)
    live_fields = metadata.models.get(binding.model)
    schema_changed = live_fields is None or any(
        not _same_write_field(captured_fields.get(name), live_fields.fields.get(name))
        for name in approved_fields
    )
    write_date_field = live_fields.fields.get("write_date") if live_fields else None
    schema_changed = schema_changed or not _valid_write_date_field(write_date_field)
    current_by_id = {
        item.odoo_id: item for item in records.records.get(binding.model, ())
    }
    if len(current_by_id) != len(records.records.get(binding.model, ())):
        raise ReadinessError("Odoo returned duplicate protected record identifiers")

    comparison_rows = tuple(
        compare_pinned_odoo_row(
            item,
            origins=origins,
            baseline=baselines.get(item.source_row),
            current=current_by_id.get(_origin_for_record(item.source_row, origins)[0]),
            approved_fields=approved_fields,
            captured_fields=captured_fields,
            schema_changed=schema_changed,
        )
        for item in prepared
    )
    checked_at = _snapshot_time(metadata.fingerprint.snapshot_timestamp)
    artifact = OdooComparisonArtifact.create(
        run_id=run_id,
        project_id=project.project_id,
        capture_manifest_hash=manifest.content_hash,
        frozen_input_hash=frozen.content_hash,
        model=binding.model,
        connection_target_hash=binding.connection_target_hash,
        schema_scope_hash=binding.schema_scope_hash,
        read_principal_hash=binding.read_principal_hash,
        context_hash=binding.context_hash,
        checked_at=checked_at,
        rows=comparison_rows,
    )
    artifact_bytes = artifact.to_json().encode("utf-8") + b"\n"
    protected = provenance.protect_comparison(
        project.project_id,
        run_id,
        manifest.content_hash,
        artifact_bytes,
        actor=actor,
    )
    if protected.logical_hash != "sha256:" + sha256(artifact_bytes).hexdigest():
        raise ReadinessError("Protected Odoo comparison hash is invalid")

    redacted = RecordSnapshot(
        fingerprint=records.fingerprint,
        records={binding.model: ()},
        requested_fields={binding.model: fields},
        complete=True,
    )
    metadata, redacted = bind_snapshot_hashes(metadata, redacted)
    readiness_rows = tuple(
        _readiness_row(item, dataset.name, frozen.dataset_labels.get(dataset.name, dataset.name))
        for item in comparison_rows
    )
    report = _report(
        project,
        frozen,
        run_id,
        binding.model,
        readiness_rows,
        metadata,
        redacted,
        checked_at,
        protected.logical_hash,
        protected.artifact_hash,
        checked_by=actor.identity.display_name,
        dataset_name=dataset.name,
        dataset_label=frozen.dataset_labels.get(dataset.name, dataset.name),
        chunk_count=len(record_requests),
    )
    portable_manifest = canonical_json_bytes(
        {
            "contract": "odoo-pinned-comparison-v1",
            "counts": artifact.counts,
            "preflight_evidence": {
                "capture_manifest_hash": manifest.content_hash,
                "frozen_input_hash": frozen.content_hash,
                "metadata_snapshot_hash": report.metadata_snapshot_hash,
                "protected_artifact_hash": protected.artifact_hash,
                "protected_comparison_hash": artifact.content_hash,
                "protected_logical_hash": protected.logical_hash,
                "record_chunk_count": len(record_requests),
                "record_snapshot_hash": report.record_snapshot_hash,
            },
            "project_id": project.project_id,
            "run_id": run_id,
            "status": report.status,
        }
    ) + b"\n"
    assert_no_numeric_odoo_ids(portable_manifest.decode("utf-8"))
    report = replace(
        report,
        manifest_hash="sha256:" + sha256(portable_manifest).hexdigest(),
    )
    return OdooComparisonPublication(
        report=report,
        rows=readiness_rows,
        metadata_snapshot=metadata,
        redacted_record_snapshot=redacted,
        portable_manifest=portable_manifest,
        protected=protected,
        artifact=artifact,
    )


def _pinned_context(frozen: FrozenPreflightInput, selection: SourceSelection):
    if len(frozen.revision.definition.datasets) != 1 or len(selection.datasets) != 1:
        raise ReadinessError("Pinned Odoo comparison requires one captured record type")
    mapping = frozen.revision.definition.datasets[0]
    dataset = selection.datasets[0]
    binding = dataset.source
    if (
        mapping.mode is not MappingTargetMode.ODOO_PINNED_UPDATE
        or dataset.origin is not SourceOriginKind.ODOO
        or not isinstance(binding, OdooSourceBinding)
        or mapping.dataset_id != dataset.dataset_id
        or mapping.target_model != binding.model
    ):
        raise ReadinessError("The approved field matches are not pinned to this Odoo capture")
    return mapping, dataset, binding


def _protected_origins(
    project_id: str,
    selection: SourceSelection,
    dataset_id: str,
    binding: OdooSourceBinding,
    source_snapshots: tuple[SourceSnapshot, ...],
    provenance: OdooProvenanceService,
    actor: Actor,
) -> tuple[OdooCaptureManifest, tuple[OdooOriginBatch, ...]]:
    invalid = "Refresh the captured Odoo records before comparing."
    if len(source_snapshots) != 1 or source_snapshots[0].dataset_id != dataset_id:
        raise ReadinessError(invalid)
    try:
        manifest = provenance.current_manifest(project_id, actor=actor)
        protected = provenance.read_current_origins(project_id, actor=actor)
    except (ArtifactStoreError, WorkspaceError, ValueError) as error:
        raise ReadinessError(invalid) from error
    if manifest is None or protected is None:
        raise ReadinessError(invalid)
    _header, batches = protected
    snapshot = source_snapshots[0]
    dataset = selection.datasets[0]
    if (
        manifest.project_id != project_id
        or manifest.selection_hash != binding.capture_selection_hash
        or manifest.dataset_id != dataset_id
        or manifest.dataset_name != dataset.name
        or manifest.model != binding.model
        or manifest.field_names
        != tuple(sorted(column.source_name for column in dataset.columns))
        or manifest.column_stable_keys
        != tuple(column.stable_key for column in dataset.columns)
        or manifest.policy_hash != binding.policy_hash
        or manifest.connection_target_hash != binding.connection_target_hash
        or manifest.schema_scope_hash != binding.schema_scope_hash
        or manifest.read_principal_hash != binding.read_principal_hash
        or manifest.read_permission_hash != binding.read_permission_hash
        or manifest.context_hash != binding.context_hash
        or manifest.row_count != dataset.row_count
        or manifest.row_count != snapshot.row_count
        or manifest.data_logical_hash != snapshot.data_logical_hash
        or manifest.data_sha256 != snapshot.parquet_sha256
        or manifest.data_storage_key != snapshot.parquet_storage_key
    ):
        raise ReadinessError(invalid)
    return manifest, batches


def _load_baselines(
    artifacts: ArtifactStore,
    project_id: str,
    selection: SourceSelection,
    dataset,
    snapshot: SourceSnapshot,
) -> dict[int, dict[str, object]]:
    try:
        validate_snapshot_for_dataset(selection, dataset, snapshot)
        with artifacts.materialize_source_snapshot(
            project_id,
            snapshot.parquet_storage_key,
            expected_sha256=snapshot.parquet_sha256,
        ) as path:
            table = load_source_snapshot_table(path, snapshot)
    except (ArtifactStoreError, OSError, ValueError) as error:
        raise ReadinessError(
            "Refresh the captured Odoo records before comparing."
        ) from error
    return {item.number: dict(item.values) for item in table.rows}


def _origin_by_ordinal(
    batches: Iterable[OdooOriginBatch],
    *,
    expected_count: int,
) -> dict[int, tuple[int, datetime | None]]:
    result: dict[int, tuple[int, datetime | None]] = {}
    expected = 1
    for batch in batches:
        if batch.first_row_ordinal != expected:
            raise ReadinessError("Protected Odoo record order is incomplete")
        for offset, (odoo_id, write_date) in enumerate(
            zip(batch.odoo_ids, batch.write_dates)
        ):
            result[expected + offset] = (odoo_id, write_date)
        expected += batch.row_count
    if len(result) != expected_count:
        raise ReadinessError("Protected Odoo record count is incomplete")
    return result


def _origin_for_record(
    source_row: int,
    origins: dict[int, tuple[int, datetime | None]],
) -> tuple[int, datetime | None]:
    value = origins.get(source_row)
    if value is None:
        raise ReadinessError("A prepared row lost its protected Odoo origin")
    return value


def _validate_live_binding(
    project: MigrationProject,
    binding: OdooSourceBinding,
    metadata: MetadataSnapshot,
    records: RecordSnapshot,
    fields: tuple[str, ...],
) -> None:
    if metadata.fingerprint != records.fingerprint:
        raise ReadinessError("Odoo comparison snapshots came from different targets")
    fingerprint = metadata.fingerprint
    if (
        fingerprint.target_hash != binding.connection_target_hash
        or fingerprint.database != project.odoo_database
        or not fingerprint.odoo_version.startswith("19.")
    ):
        raise ReadinessError(
            "The Odoo target changed. Refresh the captured records before comparing."
        )
    if set(metadata.models) != {binding.model}:
        raise ReadinessError("Odoo comparison metadata is incomplete")
    actual_fields = set(metadata.models[binding.model].fields)
    if actual_fields - set(fields):
        raise ReadinessError("Odoo comparison returned unapproved metadata fields")
    if set(records.records) != {binding.model} or set(records.requested_fields) != {
        binding.model
    }:
        raise ReadinessError("Odoo comparison record evidence is incomplete")
    if tuple(records.requested_fields[binding.model]) != fields:
        raise ReadinessError("Odoo comparison returned an unexpected field projection")


def _captured_fields(
    schema: OdooSchemaCatalog | None,
    model: str,
) -> dict[str, SchemaField]:
    if schema is None:
        return {}
    captured = next((item for item in schema.models if item.name == model), None)
    return {item.name: item for item in captured.fields} if captured else {}


def _same_write_field(stored: SchemaField | None, live: FieldMetadata | None) -> bool:
    if stored is None or live is None:
        return False
    return (
        stored.type == live.type
        and stored.required == live.required
        and stored.readonly == live.readonly
        and stored.relation == live.relation
        and stored.relation_field == live.relation_field
        and tuple(stored.selection) == tuple(live.selection)
        and stored.stored == live.stored
        and stored.computed == live.computed
        and stored.related == live.related
        and stored.translated == live.translated
        and stored.company_dependent == live.company_dependent
    )


def _valid_write_date_field(field: FieldMetadata | None) -> bool:
    return bool(field is not None and field.type == "datetime" and field.stored is True)


def compare_pinned_odoo_row(
    prepared,
    *,
    origins: dict[int, tuple[int, datetime | None]],
    baseline: dict[str, object] | None,
    current: TargetRecord | None,
    approved_fields: tuple[str, ...],
    captured_fields: dict[str, SchemaField],
    schema_changed: bool,
) -> OdooComparisonRow:
    odoo_id, captured_write_date = _origin_for_record(prepared.source_row, origins)
    if schema_changed:
        return OdooComparisonRow(
            source_row_ordinal=prepared.source_row,
            source_trace_id=prepared.source_trace_id,
            odoo_id=odoo_id,
            captured_write_date=captured_write_date,
            current_write_date=None,
            outcome=OdooComparisonOutcome.TARGET_SCHEMA_CHANGED,
            fields=tuple(
                OdooFieldComparison(
                    field=name,
                    field_type=(captured_fields[name].type if name in captured_fields else "unknown"),
                    baseline=(baseline or {}).get(name),
                    proposed=prepared.scalar_values.get(name),
                    current=(current.values.get(name) if current else None),
                    outcome=OdooFieldComparisonOutcome.SCHEMA_CHANGED,
                )
                for name in approved_fields
            ),
        )
    if current is None:
        return OdooComparisonRow(
            source_row_ordinal=prepared.source_row,
            source_trace_id=prepared.source_trace_id,
            odoo_id=odoo_id,
            captured_write_date=captured_write_date,
            current_write_date=None,
            outcome=OdooComparisonOutcome.RECORD_REMOVED_OR_INACCESSIBLE,
            fields=(),
        )
    try:
        current_write_date = canonical_write_date(current.values.get("write_date"))
    except OdooComparisonError as error:
        raise ReadinessError("Odoo returned an invalid write timestamp") from error
    comparisons: list[OdooFieldComparison] = []
    missing_baseline = baseline is None
    concurrent = False
    updates = False
    current_intended_change = False
    for name in approved_fields:
        metadata = captured_fields[name]
        if baseline is None or name not in baseline:
            comparisons.append(
                OdooFieldComparison(
                    name,
                    metadata.type,
                    None,
                    prepared.scalar_values.get(name),
                    current.values.get(name),
                    OdooFieldComparisonOutcome.BASELINE_MISSING,
                )
            )
            missing_baseline = True
            continue
        if name not in prepared.scalar_values or name not in current.values:
            raise ReadinessError("Odoo comparison values are incomplete")
        try:
            original = canonical_odoo_scalar(metadata.type, baseline[name])
            proposed = canonical_odoo_scalar(metadata.type, prepared.scalar_values[name])
            live = canonical_odoo_scalar(metadata.type, current.values[name])
        except OdooComparisonError as error:
            raise ReadinessError(
                f"Odoo returned an invalid value for {name}"
            ) from error
        will_write = proposed != original
        live_changed = live != original
        if will_write and live_changed:
            outcome = OdooFieldComparisonOutcome.CONCURRENT_CHANGE
            concurrent = True
            current_intended_change = True
        elif will_write:
            outcome = OdooFieldComparisonOutcome.UPDATE
            updates = True
        elif live_changed:
            outcome = OdooFieldComparisonOutcome.EXTERNAL_CHANGE_NOT_WRITTEN
            current_intended_change = True
        else:
            outcome = OdooFieldComparisonOutcome.UNCHANGED
        comparisons.append(
            OdooFieldComparison(name, metadata.type, original, proposed, live, outcome)
        )
    if missing_baseline:
        row_outcome = OdooComparisonOutcome.BASELINE_NOT_CAPTURED
    elif captured_write_date is None or current_write_date is None:
        row_outcome = OdooComparisonOutcome.TARGET_SCHEMA_CHANGED
    elif concurrent:
        row_outcome = OdooComparisonOutcome.CONCURRENT_FIELD_CHANGE
    elif updates:
        row_outcome = OdooComparisonOutcome.UPDATE
    else:
        row_outcome = OdooComparisonOutcome.UNCHANGED
    unrelated = bool(
        current_write_date != captured_write_date and not current_intended_change
    )
    return OdooComparisonRow(
        source_row_ordinal=prepared.source_row,
        source_trace_id=prepared.source_trace_id,
        odoo_id=odoo_id,
        captured_write_date=captured_write_date,
        current_write_date=current_write_date,
        outcome=row_outcome,
        fields=tuple(comparisons),
        unrelated_current_change=unrelated,
    )


def plan_pinned_record_requests(
    model: str,
    fields: tuple[str, ...],
    record_ids: tuple[int, ...],
) -> tuple[RecordRequest, ...]:
    """Plan deterministic exact-ID chunks without a business-key fallback."""

    if (
        any(value < 1 for value in record_ids)
        or len(set(record_ids)) != len(record_ids)
        or tuple(sorted(record_ids)) != record_ids
    ):
        raise ReadinessError("Protected Odoo record identifiers are invalid")
    return tuple(
        RecordRequest(
            model,
            fields,
            (["id", "in", list(record_ids[start : start + ODOO_COMPARISON_CHUNK_SIZE])],),
        )
        for start in range(0, len(record_ids), ODOO_COMPARISON_CHUNK_SIZE)
    )


def _readiness_row(
    row: OdooComparisonRow,
    dataset: str,
    dataset_label: str,
) -> ReadinessRow:
    blocked = row.outcome not in {
        OdooComparisonOutcome.UNCHANGED,
        OdooComparisonOutcome.UPDATE,
    }
    if row.outcome is OdooComparisonOutcome.UPDATE:
        reason = "The prepared values are ready to update."
        action = "Review the changes. Nothing has been written to Odoo."
        field = next(
            (item.field for item in row.fields if item.outcome is OdooFieldComparisonOutcome.UPDATE),
            "",
        )
    elif row.outcome is OdooComparisonOutcome.UNCHANGED:
        reason = "This record already matches the prepared values."
        action = "No action needed."
        field = ""
    else:
        reason = "This captured Odoo record is no longer safe to update."
        action = "Refresh the captured records, then prepare and compare again."
        field = next(
            (
                item.field
                for item in row.fields
                if item.outcome
                in {
                    OdooFieldComparisonOutcome.CONCURRENT_CHANGE,
                    OdooFieldComparisonOutcome.BASELINE_MISSING,
                    OdooFieldComparisonOutcome.SCHEMA_CHANGED,
                }
            ),
            "",
        )
    return ReadinessRow(
        dataset=dataset,
        dataset_label=dataset_label,
        source_row=row.source_row_ordinal,
        status="blocked" if blocked else "ready",
        classification=("BLOCKED" if blocked else row.outcome.value),
        identity="Captured Odoo record",
        reason=reason,
        field=field,
        recommended_action=action,
        technical_code=(row.outcome.value if blocked else ""),
        issue_count=1 if blocked else 0,
        source_trace_id=row.source_trace_id,
    )


def _report(
    project: MigrationProject,
    frozen: FrozenPreflightInput,
    run_id: str,
    model: str,
    rows: tuple[ReadinessRow, ...],
    metadata: MetadataSnapshot,
    records: RecordSnapshot,
    checked_at: datetime,
    protected_logical_hash: str,
    protected_artifact_hash: str,
    *,
    checked_by: str,
    dataset_name: str,
    dataset_label: str,
    chunk_count: int,
) -> ReadinessReport:
    update_count = sum(item.classification == "UPDATE" for item in rows)
    unchanged_count = sum(item.classification == "UNCHANGED" for item in rows)
    blocked_count = sum(item.status == "blocked" for item in rows)
    requirement_hash = "sha256:" + sha256(
        canonical_json_bytes(
            {
                "chunk_count": chunk_count,
                "fields": list(records.requested_fields.get(model, ())),
                "model": model,
                "record_count": len(rows),
                "protected_origin_set_hash": protected_logical_hash,
            }
        )
    ).hexdigest()
    result_hash = "sha256:" + sha256(
        canonical_json_bytes(
            {
                "counts": {
                    "blocked": blocked_count,
                    "unchanged": unchanged_count,
                    "update": update_count,
                },
                "protected_artifact_hash": protected_artifact_hash,
                "rows": [
                    {
                        "classification": item.classification,
                        "source_trace_id": item.source_trace_id,
                        "technical_code": item.technical_code,
                    }
                    for item in rows
                ],
            }
        )
    ).hexdigest()
    return ReadinessReport(
        run_id=run_id,
        project_id=project.project_id,
        mapping_id=frozen.revision.mapping_id,
        mapping_version=frozen.revision.version,
        mapping_content_hash=frozen.revision.definition.content_hash,
        staging_run_id=frozen.staging.run_id,
        staging_content_hash=frozen.staging.content_hash,
        quality_run_id=frozen.quality.run_id,
        quality_content_hash=frozen.quality.content_hash,
        normalization_run_id=frozen.normalization.run_id,
        normalization_content_hash=frozen.normalization.content_hash,
        normalization_lifecycle_version=frozen.normalization.lifecycle_version,
        eligible_dataset_hash=frozen.normalization.eligible_dataset_hash,
        frozen_input_hash=frozen.content_hash,
        requirement_plan_hash=requirement_hash,
        metadata_snapshot_hash=str(metadata.content_hash),
        record_snapshot_hash=str(records.content_hash),
        result_hash=result_hash,
        manifest_hash="",
        target_hash=metadata.fingerprint.target_hash,
        target_database=metadata.fingerprint.database,
        target_odoo_version=metadata.fingerprint.odoo_version,
        target_snapshot_at=metadata.fingerprint.snapshot_timestamp,
        target_module_versions=dict(metadata.fingerprint.module_versions),
        checked_at=checked_at,
        checked_by=checked_by,
        datasets=(
            ReadinessDataset(
                dataset=dataset_name,
                label=dataset_label,
                target_model=model,
                total=len(rows),
                ready=update_count + unchanged_count,
                needs_review=0,
                blocked=blocked_count,
                create_count=0,
                update_count=update_count,
                unchanged_count=unchanged_count,
                ambiguous_count=0,
            ),
        ),
        rows=rows,
    )


def _snapshot_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ReadinessError("Odoo comparison timestamp is invalid") from error
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
