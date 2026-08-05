"""One-time retirement of the superseded field-list mapping draft."""

from __future__ import annotations

from datetime import datetime, timezone
import json

import duckdb

from ....mapping_semantics import (
    DatasetMapping,
    MappingDefinition,
    ScalarFieldMapping,
)
from ....workspace_contracts import MappingWorkingDraft, SourceSelection
from ....workspace_serialization import content_hash


def retire_mapping_draft(connection: duckdb.DuckDBPyConnection) -> None:
    """Archive old JSON and recover compatible work as a working draft."""

    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS retired_evidence (
            evidence_type VARCHAR NOT NULL,
            evidence_key VARCHAR NOT NULL,
            retired_at VARCHAR NOT NULL,
            retirement_reason VARCHAR NOT NULL,
            payload_json VARCHAR NOT NULL,
            PRIMARY KEY (evidence_type, evidence_key)
        )
        """
    )
    tables = {
        str(row[0])
        for row in connection.execute("SHOW TABLES").fetchall()
    }
    if "mapping_draft" not in tables:
        return
    row = connection.execute(
        """
        SELECT draft_json
          FROM mapping_draft
         WHERE singleton_id = 1
        """
    ).fetchone()
    if row is not None:
        raw_json = str(row[0])
        reason = _recover_as_working_draft(connection, raw_json)
        connection.execute(
            """
            INSERT OR REPLACE INTO retired_evidence
            VALUES ('FIELD_LIST_MAPPING_DRAFT', 'singleton', ?, ?, ?)
            """,
            [
                datetime.now(timezone.utc).isoformat(),
                reason,
                raw_json,
            ],
        )
    connection.execute("DROP TABLE mapping_draft")


def _recover_as_working_draft(
    connection: duckdb.DuckDBPyConnection,
    raw_json: str,
) -> str:
    if connection.execute(
        "SELECT COUNT(*) FROM mapping_working_draft"
    ).fetchone()[0]:
        return "SUPERSEDED_BY_WORKING_DRAFT"
    if connection.execute(
        "SELECT COUNT(*) FROM mapping_current"
    ).fetchone()[0]:
        return "SUPERSEDED_BY_MAPPING_REVISION"
    selection_row = connection.execute(
        "SELECT selection_json FROM source_selection WHERE singleton_id = 1"
    ).fetchone()
    schema_row = connection.execute(
        "SELECT catalog_json FROM odoo_schema_catalog WHERE singleton_id = 1"
    ).fetchone()
    if selection_row is None or schema_row is None:
        return "ARCHIVED_WITHOUT_CURRENT_EVIDENCE"
    try:
        payload = json.loads(raw_json)
        selection = SourceSelection.from_json(str(selection_row[0]))
        if payload.get("source_selection_hash") != selection.content_hash:
            return "ARCHIVED_STALE_SOURCE_SELECTION"
        governance_row = connection.execute(
            """
            SELECT revision.content_hash
              FROM schema_governance_current AS current
              JOIN schema_governance_revision AS revision
                ON revision.governance_id = current.governance_id
               AND revision.version = current.version
             WHERE current.singleton_id = 1
            """
        ).fetchone()
        expected_schema_hash = (
            str(governance_row[0])
            if governance_row is not None
            else str(json.loads(str(schema_row[0]))["content_hash"])
        )
        if payload.get("schema_hash") != expected_schema_hash:
            return "ARCHIVED_STALE_SCHEMA"
        if payload.get("content_hash") != _old_draft_content_hash(payload):
            return "ARCHIVED_INVALID_CONTENT_HASH"
        datasets = _convert_datasets(selection, payload.get("entries", ()))
        definition = MappingDefinition(
            mapping_id=str(payload["mapping_id"]),
            source_selection_hash=selection.content_hash,
            schema_hash=expected_schema_hash,
            datasets=datasets,
        )
        draft = MappingWorkingDraft(
            mapping_id=definition.mapping_id,
            version=1,
            project_id=selection.project_id,
            base_mapping_version=None,
            definition=definition,
            updated_at=datetime.fromisoformat(str(payload["updated_at"])),
            updated_by=str(payload["updated_by"]),
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return "ARCHIVED_UNREPRESENTABLE"
    connection.execute(
        """
        INSERT INTO mapping_working_draft
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            1,
            draft.mapping_id,
            draft.version,
            draft.definition.source_selection_hash,
            draft.definition.schema_hash,
            draft.content_hash,
            draft.updated_at.isoformat(),
            draft.to_json(),
        ],
    )
    return "CONVERTED_TO_WORKING_DRAFT"


def _convert_datasets(
    selection: SourceSelection,
    raw_entries: object,
) -> tuple[DatasetMapping, ...]:
    if not isinstance(raw_entries, list):
        raise ValueError("Old mapping entries are invalid")
    source_by_name = {dataset.name: dataset for dataset in selection.datasets}
    if len(source_by_name) != len(selection.datasets):
        raise ValueError("Source dataset names are ambiguous")
    grouped: dict[str, list[dict[str, object]]] = {}
    for item in raw_entries:
        if not isinstance(item, dict):
            raise ValueError("Old mapping entry is invalid")
        dataset_name = str(item["dataset_name"])
        grouped.setdefault(dataset_name, []).append(item)
    converted: list[DatasetMapping] = []
    for dataset_name, entries in sorted(grouped.items()):
        dataset = source_by_name.get(dataset_name)
        if dataset is None:
            raise ValueError("Old mapping dataset is unavailable")
        target_models = {str(item["target_model"]) for item in entries}
        if len(target_models) != 1:
            raise ValueError("Old mapping dataset has multiple target models")
        columns = {column.source_name: column for column in dataset.columns}
        fields = tuple(
            ScalarFieldMapping(
                target_field=str(item["target_field"]),
                source_column_key=columns[str(item["source_column"])].stable_key,
            )
            for item in sorted(
                entries,
                key=lambda entry: (
                    str(entry["target_field"]),
                    str(entry["source_column"]),
                ),
            )
        )
        converted.append(
            DatasetMapping(
                dataset_id=dataset.dataset_id,
                target_model=target_models.pop(),
                fields=fields,
            )
        )
    return tuple(converted)


def _old_draft_content_hash(payload: dict[str, object]) -> str:
    return content_hash(
        {
            "mapping_id": payload["mapping_id"],
            "version": int(payload["version"]),
            "status": str(payload["status"]),
            "source_selection_hash": payload["source_selection_hash"],
            "schema_hash": payload["schema_hash"],
            "entries": payload["entries"],
        }
    )
