"""Read-only two-instance matching rehearsal with ephemeral demo source values.

The connection file contains one ``https://host: API_KEY`` entry per line.
No credential or business value is printed or written by this runner.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from pathlib import Path
import re
from urllib.parse import urlparse
from uuid import uuid4

from impodo.adapters.odoo.connectors import Json2Config, Json2ReadConnector
from impodo.application.destination_matching_service import (
    DestinationMatchKeyChoice,
    DestinationMatchingService,
)
from impodo.domain.odoo.contracts import MetadataRequest, RecordRequest
from impodo.domain.serialization import content_hash
from impodo.domain.source_binding import OdooSourceBinding
from impodo.domain.workspace.contracts import (
    OdooSchemaCatalog,
    SchemaField,
    SchemaModel,
    SchemaOrigin,
    SourceDataset,
    SourceDatasetColumn,
    SourceSelection,
)
from impodo.domain.workspace.portable_identity import portable_identity
from impodo.domain.workspace.workbench import (
    OdooConnectionMode,
    SourceMode,
    WorkspaceState,
    WorkspaceStatus,
)


_ENTRY = re.compile(r"(https://[A-Za-z0-9.-]+):\s*(\S+)")


class _EphemeralValues:
    def __init__(self, fields: tuple[str, ...], values: dict[str, object]) -> None:
        self.fields = fields
        self.values = values

    def source_value_choices(self, _workspace_id, _dataset_id, source_column_key):
        return ({"value": self.values[source_column_key], "count": 1},)

    def source_key_rows(self, _workspace_id, _dataset_id, source_column_key):
        return (self.values[source_column_key],)

    def source_key_tuples(self, _workspace_id, _dataset_id, source_column_keys):
        return (tuple(self.values[key] for key in source_column_keys),)


def _connections(path: Path) -> tuple[tuple[str, str, str], ...]:
    entries = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if not line.strip():
            continue
        match = _ENTRY.fullmatch(line.strip())
        if match is None:
            raise ValueError("Connection file contains an invalid entry")
        url, api_key = match.groups()
        hostname = urlparse(url).hostname
        if hostname is None:
            raise ValueError("Connection file contains an invalid address")
        entries.append((url, hostname.split(".")[0], api_key))
    return tuple(entries)


def qualify(
    connection_file: Path,
    source_index: int,
    destination_index: int,
    model: str,
    key_fields: tuple[str, ...],
) -> str:
    if (
        not 1 <= len(key_fields) <= 3
        or len(set(key_fields)) != len(key_fields)
        or any(re.fullmatch(r"[a-z_][a-z0-9_]*", field) is None for field in key_fields)
    ):
        raise ValueError("Choose one to three distinct scalar key fields")
    entries = _connections(connection_file)
    if (
        source_index == destination_index
        or not 1 <= source_index <= len(entries)
        or not 1 <= destination_index <= len(entries)
    ):
        raise ValueError("Choose two distinct connection entries")
    source_url, source_db, source_key = entries[source_index - 1]
    target_url, target_db, target_key = entries[destination_index - 1]
    source = Json2ReadConnector(
        Json2Config(source_url, source_db, source_key, retries=0)
    )
    destination = Json2ReadConnector(
        Json2Config(target_url, target_db, target_key, retries=0)
    )
    source_fingerprint = source.get_target_fingerprint()
    destination_fingerprint = destination.get_target_fingerprint()
    if source_fingerprint.target_hash == destination_fingerprint.target_hash:
        raise ValueError("Source and destination resolve to the same Odoo target")
    source_metadata = source.get_model_metadata(
        (MetadataRequest(model, key_fields),)
    )
    source_fields = source_metadata.models[model].fields
    if any(field not in source_fields for field in key_fields):
        raise ValueError("A selected source key field is unavailable")
    if source_fields[key_fields[0]].type not in {"char", "text", "selection"}:
        raise ValueError("The first key field must be text")
    if any(
        source_fields[field].type not in {"char", "text", "selection", "integer"}
        for field in key_fields
    ):
        raise ValueError("Every key field must be text or integer")
    source_rows = source.get_records(
        (RecordRequest(model, key_fields, limit=20),)
    ).records.get(model, ())
    chosen = next(
        (
            row for row in source_rows
            if portable_identity(tuple(row.values.get(field) for field in key_fields))
        ),
        None,
    )
    if chosen is None:
        target_metadata = destination.get_model_metadata(
            (MetadataRequest(model, (), all_fields=True),)
        )
        state = target_metadata.models[model].fields.get("state")
        return (
            "No nonblank source identity in the first 20 records; "
            f"model={model}; workflow_handler_required="
            f"{state is not None and state.type == 'selection'}"
        )

    now = datetime.now(UTC)
    workspace_id = str(uuid4())
    dataset_id = str(uuid4())
    evidence_hash = content_hash(
        {"kind": "ephemeral-two-instance-qualification", "model": model}
    )
    credential_hash = content_hash(
        {"kind": "ephemeral-destination-credential", "target": destination_fingerprint.target_hash}
    )
    destination_identity = destination.probe_read_identity((model,))
    workspace = WorkspaceState(
        workspace_id=workspace_id,
        name="Read-only two-instance qualification",
        source_system="Odoo",
        source_mode=SourceMode.ODOO,
        status=WorkspaceStatus.REGISTERED,
        odoo_connection_mode=OdooConnectionMode.REMOTE,
        odoo_base_url=source_url,
        odoo_database=source_db,
        destination_odoo_connection_mode=OdooConnectionMode.REMOTE,
        destination_odoo_base_url=target_url,
        destination_odoo_database=target_db,
        destination_verified_target_hash=destination_fingerprint.target_hash,
        destination_verified_credential_binding_hash=credential_hash,
        destination_verified_read_principal_hash=destination_identity.principal_hash,
        destination_verified_odoo_version=destination_fingerprint.odoo_version,
        destination_verified_at=now,
    )
    binding = OdooSourceBinding(
        capture_selection_hash=evidence_hash,
        model=model,
        policy_hash=evidence_hash,
        connection_target_hash=source_fingerprint.target_hash,
        schema_scope_hash=evidence_hash,
        read_principal_hash=evidence_hash,
        read_permission_hash=evidence_hash,
        context_hash=evidence_hash,
    )
    dataset = SourceDataset(
        dataset_id=dataset_id,
        name="demo_records",
        source=binding,
        row_count=1,
        columns=tuple(
            SourceDatasetColumn(index, field, field, "TEXT")
            for index, field in enumerate(key_fields, 1)
        ),
    )
    selection = SourceSelection(
        selection_id=str(uuid4()),
        version=1,
        data_version_id=str(uuid4()),
        created_at=now,
        created_by="Read-only qualification",
        datasets=(dataset,),
        content_hash=evidence_hash,
    )
    schema = OdooSchemaCatalog(
        workspace_id=workspace_id,
        policy_hash=evidence_hash,
        captured_at=now,
        captured_by="Read-only qualification",
        connection_mode="REMOTE",
        database=source_db,
        odoo_version=source_fingerprint.odoo_version,
        models=(
            SchemaModel(
                model,
                source_metadata.models[model].description or model,
                tuple(
                    SchemaField(
                        name=field,
                        label=source_fields[field].label,
                        type=source_fields[field].type,
                        required=source_fields[field].required,
                        readonly=source_fields[field].readonly,
                        relation=None,
                        relation_field=None,
                        selection=source_fields[field].selection,
                    )
                    for field in key_fields
                ),
            ),
        ),
        content_hash=evidence_hash,
        origin=SchemaOrigin.LIVE_API,
        read_credential_binding_hash=evidence_hash,
        read_principal_hash=evidence_hash,
        read_permission_hash=evidence_hash,
        read_context_hash=evidence_hash,
        connection_target_hash=source_fingerprint.target_hash,
    )

    def destination_reader(_workspace, _api_key, metadata_requests, record_requests):
        return (
            destination.get_model_metadata(metadata_requests),
            destination.get_records(record_requests),
        )

    plan = DestinationMatchingService(
        _EphemeralValues(key_fields, dict(chosen.values))
    ).check(
        workspace,
        selection,
        schema,
        (
            DestinationMatchKeyChoice(
                dataset_id, key_fields[0], key_fields[1:]
            ),
        ),
        api_key="ephemeral-read-only-qualification",
        credential_binding_hash=credential_hash,
        read_identity=destination_identity,
        reader=destination_reader,
        recorded_by="Read-only qualification",
    )
    result = plan.model_matches[0]
    return (
        f"Odoo {source_fingerprint.odoo_version} -> "
        f"{destination_fingerprint.odoo_version}; model={model}; "
        f"fields={','.join(result.key_fields)}; matching_ready={plan.ready}; "
        f"existing={result.destination_existing_key_count}; "
        f"missing={result.destination_create_key_count}; "
        f"matching_blockers={','.join(result.blocking_reasons) or 'none'}; "
        f"write_field_blockers={','.join(result.write_blocking_reasons) or 'none'}; "
        f"missing_fields={','.join(result.missing_fields) or 'none'}; "
        f"incompatible_fields={','.join(result.incompatible_fields) or 'none'}; "
        f"unresolved_create_fields={','.join(result.unresolved_create_fields) or 'none'}; "
        f"workflow_handler_required={result.requires_workflow_handler}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--connections-file", type=Path, required=True)
    parser.add_argument("--source-index", type=int, default=1)
    parser.add_argument("--destination-index", type=int, default=2)
    parser.add_argument("--model", default="res.partner")
    parser.add_argument("--key-field", action="append", dest="key_fields")
    args = parser.parse_args()
    try:
        result = qualify(
            args.connections_file,
            args.source_index,
            args.destination_index,
            args.model,
            tuple(args.key_fields or ("name", "company_type")),
        )
    except Exception as error:
        # Connector errors can include request context. Never print them here.
        print(f"Read-only qualification failed: {type(error).__name__}")
        return 1
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
