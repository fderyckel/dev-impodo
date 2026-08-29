"""Transport-neutral Odoo read requests and immutable snapshots."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from hashlib import sha256
from io import StringIO
import json
from typing import Any, Mapping, Protocol, Sequence

from impodo.domain.shared.models import (
    FieldMetadata,
    ModelMetadata,
    OdooReadIdentity,
    OdooWriteIdentity,
    TargetFingerprint,
    TargetRecord,
    canonical_json_bytes,
    canonical_json_text,
    portable_value,
)


@dataclass(frozen=True, slots=True)
class MetadataRequest:
    """Fields whose Odoo metadata must be fetched for one model."""

    model: str
    fields: tuple[str, ...]
    all_fields: bool = False
    include_unique_constraints: bool = False


@dataclass(frozen=True, slots=True)
class RecordRequest:
    """One batched, domain-limited target-record query for an Odoo model."""

    model: str
    fields: tuple[str, ...]
    domain: tuple[Any, ...] = ()
    limit: int | None = None


@dataclass(frozen=True, slots=True)
class MetadataSnapshot:
    """Model metadata plus the exact target identity from which it was read."""

    fingerprint: TargetFingerprint
    models: Mapping[str, ModelMetadata]
    create_defaults: Mapping[str, Mapping[str, object]] = field(default_factory=dict)
    complete: bool = True
    limitations: tuple[str, ...] = ()
    content_hash: str | None = None


@dataclass(frozen=True, slots=True)
class RecordSnapshot:
    """Target records and exact fields returned for each requested model."""

    fingerprint: TargetFingerprint
    records: Mapping[str, tuple[TargetRecord, ...]]
    requested_fields: Mapping[str, tuple[str, ...]]
    complete: bool = True
    content_hash: str | None = None


def bind_snapshot_hashes(
    metadata: MetadataSnapshot,
    records: RecordSnapshot,
) -> tuple[MetadataSnapshot, RecordSnapshot]:
    """Validate one complete capture and attach deterministic content hashes."""

    if metadata.fingerprint != records.fingerprint:
        raise ConnectorIncompleteResultError(
            "metadata and record snapshots have different fingerprints"
        )
    if not metadata.complete or not records.complete:
        raise ConnectorIncompleteResultError("Odoo snapshot is incomplete")
    metadata_hash = "sha256:" + _sha256_bytes(
        canonical_json_bytes(metadata_snapshot_payload(metadata))
    )
    record_hash = "sha256:" + _sha256_bytes(
        canonical_json_bytes(record_snapshot_payload(records))
    )
    if metadata.content_hash not in {None, metadata_hash}:
        raise ConnectorIncompleteResultError("metadata snapshot hash is invalid")
    if records.content_hash not in {None, record_hash}:
        raise ConnectorIncompleteResultError("record snapshot hash is invalid")
    return (
        replace(metadata, content_hash=metadata_hash),
        replace(records, content_hash=record_hash),
    )


def metadata_snapshot_payload(snapshot: MetadataSnapshot) -> dict[str, Any]:
    """Return protected deterministic metadata snapshot evidence."""

    return {
        "fingerprint": snapshot.fingerprint.portable_dict(),
        "complete": snapshot.complete,
        "limitations": list(snapshot.limitations),
        "create_defaults": {
            model: {
                field_name: portable_value(value)
                for field_name, value in sorted(defaults.items())
            }
            for model, defaults in sorted(snapshot.create_defaults.items())
        },
        "models": {
            name: {
                "description": model.description,
                "fields": {
                    field_name: {
                        "type": field.type,
                        "label": field.label,
                        "required": field.required,
                        "readonly": field.readonly,
                        "relation": field.relation,
                        "relation_field": field.relation_field,
                        "selection": [list(item) for item in field.selection],
                        "stored": field.stored,
                        "computed": field.computed,
                        "has_inverse": field.has_inverse,
                        "related": field.related,
                        "translated": field.translated,
                        "company_dependent": field.company_dependent,
                        "searchable": field.searchable,
                        "sortable": field.sortable,
                        "exportable": field.exportable,
                        "digits": list(field.digits) if field.digits else None,
                        "currency_field": field.currency_field,
                    }
                    for field_name, field in sorted(model.fields.items())
                },
                "unique_constraints": [
                    {"name": item.name, "definition": item.definition}
                    for item in model.unique_constraints
                ],
            }
            for name, model in sorted(snapshot.models.items())
        },
    }


def record_snapshot_payload(snapshot: RecordSnapshot) -> dict[str, Any]:
    """Return protected target rows, including environment-local numeric IDs."""

    return {
        "fingerprint": snapshot.fingerprint.portable_dict(),
        "complete": snapshot.complete,
        "requested_fields": {
            model: list(fields)
            for model, fields in sorted(snapshot.requested_fields.items())
        },
        "models": {
            model: [
                {
                    "id": record.odoo_id,
                    "values": portable_value(record.values),
                }
                for record in sorted(items, key=lambda item: item.odoo_id)
            ]
            for model, items in sorted(snapshot.records.items())
        },
    }


def record_snapshot_json(snapshot: RecordSnapshot) -> str:
    """Serialize protected rows without materializing a second record tree."""

    stream = StringIO()
    stream.write('{"complete":')
    stream.write(canonical_json_text(snapshot.complete))
    stream.write(',"fingerprint":')
    stream.write(canonical_json_text(snapshot.fingerprint.portable_dict()))
    stream.write(',"models":{')
    for model_index, (model, records) in enumerate(sorted(snapshot.records.items())):
        if model_index:
            stream.write(",")
        stream.write(canonical_json_text(model))
        stream.write(":[")
        for record_index, record in enumerate(
            sorted(records, key=lambda item: item.odoo_id)
        ):
            if record_index:
                stream.write(",")
            stream.write(
                canonical_json_text(
                    {
                        "id": record.odoo_id,
                        "values": portable_value(record.values),
                    }
                )
            )
        stream.write("]")
    stream.write('},"requested_fields":')
    stream.write(
        canonical_json_text(
            {
                model: list(fields)
                for model, fields in sorted(snapshot.requested_fields.items())
            }
        )
    )
    stream.write("}")
    return stream.getvalue()


def record_snapshot_from_json(value: str) -> RecordSnapshot:
    """Restore one protected target snapshot and verify its stored hash.

    This parser is intentionally paired with :func:`record_snapshot_json` so
    application services do not need to know the protected JSON layout.
    Numeric Odoo identifiers remain inside the protected snapshot contract.
    """

    try:
        payload = json.loads(value)
        fingerprint_payload = payload["fingerprint"]
        fingerprint = TargetFingerprint(
            target_hash=str(fingerprint_payload["target_hash"]),
            connection_mode=str(fingerprint_payload["connection_mode"]),
            database=str(fingerprint_payload["database"]),
            odoo_version=str(fingerprint_payload["odoo_version"]),
            snapshot_timestamp=str(fingerprint_payload["snapshot_timestamp"]),
            module_versions={
                str(name): str(version)
                for name, version in fingerprint_payload.get(
                    "module_versions", {}
                ).items()
            },
        )
        records = {
            str(model): tuple(
                TargetRecord(
                    model=str(model),
                    odoo_id=int(item["id"]),
                    values=dict(item["values"]),
                )
                for item in items
            )
            for model, items in payload["models"].items()
        }
        requested_fields = {
            str(model): tuple(str(field) for field in fields)
            for model, fields in payload["requested_fields"].items()
        }
        snapshot = RecordSnapshot(
            fingerprint=fingerprint,
            records=records,
            requested_fields=requested_fields,
            complete=bool(payload["complete"]),
        )
        _metadata, bound = bind_snapshot_hashes(
            MetadataSnapshot(fingerprint=fingerprint, models={}),
            snapshot,
        )
        return bound
    except ConnectorIncompleteResultError:
        raise
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise ConnectorIncompleteResultError(
            "Stored Odoo record snapshot is invalid"
        ) from error


class OdooReadConnector(Protocol):
    """Transport-independent contract consumed by the preflight workflow."""

    def get_target_fingerprint(self) -> TargetFingerprint:
        """Identify the exact Odoo target used for subsequent reads."""

        ...

    def get_model_metadata(
        self, requests: Sequence[MetadataRequest]
    ) -> MetadataSnapshot:
        """Return metadata for the requested models and fields."""

        ...

    def get_records(self, requests: Sequence[RecordRequest]) -> RecordSnapshot:
        """Return all records matching each planned model request."""

        ...


class OdooReadIdentityProbe(Protocol):
    """Separate closed principal/context/model-permission probe contract."""

    def probe_read_identity(
        self,
        models: Sequence[str],
    ) -> OdooReadIdentity:
        """Identify the authenticated user and observed read capability."""

        ...


class OdooWriteIdentityProbe(Protocol):
    """Separate closed write-principal and read-back permission probe."""

    def probe_write_identity(
        self,
        readable_models: Sequence[str],
        writable_models: Sequence[str],
    ) -> OdooWriteIdentity:
        """Identify the user and its observed reviewed-scope capabilities."""

        ...


class ConnectorError(RuntimeError):
    """Base class for failures at the read-only connector boundary."""


class ConnectorConfigurationError(ConnectorError):
    """Raised when connector configuration or snapshot binding is invalid."""


class ConnectorAuthenticationError(ConnectorError):
    """Raised when Odoo rejects the supplied read credentials."""


class ConnectorAuthorizationError(ConnectorError):
    """Raised when the authenticated Odoo user lacks required read access."""


class ConnectorTransportError(ConnectorError):
    """Raised when a remote read cannot complete safely or successfully."""


class ConnectorIncompleteResultError(ConnectorError):
    """Raised when a response cannot prove that the requested read is complete."""


def _sha256_bytes(value: bytes) -> str:
    return sha256(value).hexdigest()
