"""Define the profiler's read-only boundary to Odoo and snapshot files.

The planner supplies batched :class:`MetadataRequest` and
:class:`RecordRequest` objects.  A connector fulfils those requests either
from Odoo 19's JSON-2 API or from deterministic JSON snapshots.  Both
implementations return the same typed snapshot contracts, so the metadata
validator, catalog, and engine do not depend on transport details.

Only ``fields_get`` and ``search_read`` are available through the live
connector.  This closed method surface is a deliberate safety control: the
profiler can inspect an authorised target but cannot create, update, delete,
or execute an arbitrary Odoo model method.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from io import StringIO
import json
import os
from pathlib import Path
import socket
import time
from typing import Any, Callable, Mapping, Protocol, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .models import (
    FieldMetadata,
    ModelMetadata,
    TargetFingerprint,
    TargetRecord,
    UniqueConstraintMetadata,
    canonical_json_bytes,
    canonical_json_text,
    portable_value,
    target_identity_hash,
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


@dataclass(frozen=True, slots=True)
class MetadataSnapshot:
    """Model metadata plus the exact target identity from which it was read."""

    fingerprint: TargetFingerprint
    models: Mapping[str, ModelMetadata]
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
    for model_index, (model, records) in enumerate(
        sorted(snapshot.records.items())
    ):
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


class ConnectorError(RuntimeError):
    """Base class for failures at the read-only connector boundary."""


class ConnectorConfigurationError(ConnectorError):
    """Raised when connector configuration or snapshot binding is invalid."""


class ConnectorAuthenticationError(ConnectorError):
    """Raised when Odoo rejects the supplied read credentials."""


class ConnectorTransportError(ConnectorError):
    """Raised when a remote read cannot complete safely or successfully."""


class ConnectorIncompleteResultError(ConnectorError):
    """Raised when a response cannot prove that the requested read is complete."""


class SnapshotConnector:
    """Deterministic connector backed by normalized JSON fixture files."""

    def __init__(
        self,
        *,
        metadata_path: str | Path | None = None,
        records_path: str | Path | None = None,
        combined_path: str | Path | None = None,
        expected_profile_id: str | None = None,
        expected_source_hashes: Mapping[str, str] | None = None,
    ) -> None:
        """Load separate or combined snapshots and validate their binding.

        Metadata and record fingerprints must match.  When expected profile
        and source hashes are supplied, the connector also prevents replaying
        a snapshot against a different profile or source package.
        """

        if combined_path is not None:
            combined = _load_json(combined_path)
            self._metadata_data = combined["metadata"]
            self._records_data = combined["records"]
        else:
            if metadata_path is None or records_path is None:
                raise ConnectorConfigurationError(
                    "SnapshotConnector requires combined_path or both snapshot paths"
                )
            self._metadata_data = json.loads(Path(metadata_path).read_bytes())
            self._records_data = json.loads(Path(records_path).read_bytes())
        _validate_snapshot_binding(
            self._metadata_data,
            expected_profile_id,
            None,
        )
        _validate_snapshot_binding(
            self._records_data,
            expected_profile_id,
            expected_source_hashes,
        )
        self._fingerprint = _parse_fingerprint(
            self._metadata_data["fingerprint"]
        )
        record_fingerprint = _parse_fingerprint(
            self._records_data["fingerprint"]
        )
        if record_fingerprint != self._fingerprint:
            raise ConnectorConfigurationError(
                "metadata and record snapshots have different fingerprints"
            )

    def get_target_fingerprint(self) -> TargetFingerprint:
        """Return the common fingerprint validated during construction."""

        return self._fingerprint

    def get_model_metadata(
        self, requests: Sequence[MetadataRequest]
    ) -> MetadataSnapshot:
        """Project stored metadata down to exactly the planned fields."""

        models: dict[str, ModelMetadata] = {}
        available = self._metadata_data.get("models", {})
        for request in requests:
            model_data = available.get(request.model)
            if model_data is None:
                continue
            all_fields = model_data.get("fields", {})
            selected_names = (
                tuple(sorted(all_fields))
                if request.all_fields
                else request.fields
            )
            fields = {
                name: _parse_field_metadata(name, all_fields[name])
                for name in selected_names
                if name in all_fields
            }
            models[request.model] = ModelMetadata(
                model=request.model,
                description=model_data.get("description"),
                fields=fields,
                unique_constraints=(
                    tuple(
                        UniqueConstraintMetadata(
                            name=str(item["name"]),
                            definition=str(item["definition"]),
                        )
                        for item in model_data.get("unique_constraints", ())
                    )
                    if request.include_unique_constraints
                    else ()
                ),
            )
        complete = bool(self._metadata_data.get("complete", True))
        return MetadataSnapshot(
            fingerprint=self._fingerprint,
            models=models,
            complete=complete,
            limitations=tuple(self._metadata_data.get("limitations", ())),
            content_hash=None,
        )

    def get_records(self, requests: Sequence[RecordRequest]) -> RecordSnapshot:
        """Project stored records down to the planned models and fields.

        Snapshot creation has already applied the planned Odoo domains, so
        replay reads the bound record set rather than re-evaluating domains
        locally.
        """

        if not self._records_data.get("complete", True):
            raise ConnectorIncompleteResultError("record snapshot is incomplete")
        available = self._records_data.get("models", {})
        records: dict[str, tuple[TargetRecord, ...]] = {}
        requested_fields: dict[str, tuple[str, ...]] = {}
        for request in requests:
            requested_fields[request.model] = tuple(request.fields)
            model_records = []
            for item in available.get(request.model, {}).get("records", ()):
                values = {
                    field: item.get("values", {}).get(field)
                    for field in request.fields
                    if field in item.get("values", {})
                }
                model_records.append(
                    TargetRecord(
                        model=request.model,
                        odoo_id=int(item["id"]),
                        values=values,
                    )
                )
            records[request.model] = tuple(
                sorted(model_records, key=lambda record: record.odoo_id)
            )
        return RecordSnapshot(
            fingerprint=self._fingerprint,
            records=records,
            requested_fields=requested_fields,
            complete=True,
            content_hash=None,
        )


@dataclass(frozen=True, slots=True)
class Json2Config:
    """Connection and batching settings for the live Odoo JSON-2 adapter.

    The API key is excluded from representations to reduce accidental secret
    disclosure in logs and errors.
    """

    base_url: str
    database: str
    api_key: str = field(repr=False)
    connection_mode: str = "REMOTE"
    timeout_seconds: float = 30.0
    page_size: int = 500
    retries: int = 2
    context: Mapping[str, Any] = field(default_factory=dict)
    relevant_modules: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """Enforce connection-mode URL rules and valid pagination."""

        try:
            parsed_url = urlparse(self.base_url)
            parsed_url.port
        except ValueError as error:
            raise ConnectorConfigurationError(
                "Odoo base URL contains an invalid port"
            ) from error
        if (
            not parsed_url.hostname
            or parsed_url.username
            or parsed_url.password
            or parsed_url.query
            or parsed_url.fragment
        ):
            raise ConnectorConfigurationError(
                "Odoo base URL cannot contain credentials, query parameters, "
                "or fragments"
            )
        hostname = parsed_url.hostname.casefold()
        is_literal_loopback = hostname in {"127.0.0.1", "::1"}
        connection_mode = self.connection_mode.strip().upper()
        if connection_mode not in {"LOCAL", "REMOTE"}:
            raise ConnectorConfigurationError(
                "connection mode must be LOCAL or REMOTE"
            )
        object.__setattr__(self, "connection_mode", connection_mode)
        if connection_mode == "LOCAL":
            if (
                parsed_url.scheme not in {"http", "https"}
                or not is_literal_loopback
                or parsed_url.path not in {"", "/"}
            ):
                raise ConnectorConfigurationError(
                    "insecure local mode permits only a literal loopback "
                    "Odoo URL"
                )
        elif (
            parsed_url.scheme != "https"
            or is_literal_loopback
            or hostname == "localhost"
        ):
            raise ConnectorConfigurationError(
                "remote Odoo must use a non-loopback HTTPS URL"
            )
        if self.page_size < 1:
            raise ConnectorConfigurationError("page_size must be positive")

    @classmethod
    def from_environment(cls) -> "Json2Config":
        """Build configuration from the governed ``IMPODO_ODOO_*`` variables.

        Required variables identify the base URL, database, and API key.
        Connection mode is derived from whether the URL uses a literal
        loopback host. Timeout and page size are optional and receive safe
        defaults.
        """

        missing = [
            name
            for name in (
                "IMPODO_ODOO_BASE_URL",
                "IMPODO_ODOO_DATABASE",
                "IMPODO_ODOO_API_KEY",
            )
            if not os.environ.get(name)
        ]
        if missing:
            raise ConnectorConfigurationError(
                "missing environment variables: " + ", ".join(missing)
            )
        base_url = os.environ["IMPODO_ODOO_BASE_URL"].rstrip("/")
        hostname = (urlparse(base_url).hostname or "").casefold()
        connection_mode = (
            "LOCAL" if hostname in {"127.0.0.1", "::1"} else "REMOTE"
        )
        return cls(
            base_url=base_url,
            database=os.environ["IMPODO_ODOO_DATABASE"],
            api_key=os.environ["IMPODO_ODOO_API_KEY"],
            connection_mode=connection_mode,
            timeout_seconds=float(
                os.environ.get("IMPODO_ODOO_TIMEOUT_SECONDS", "30")
            ),
            page_size=int(os.environ.get("IMPODO_ODOO_PAGE_SIZE", "500")),
        )


Transport = Callable[
    [str, Mapping[str, str], bytes | None, float, str],
    tuple[int, Any],
]


class Json2ReadConnector:
    """Odoo 19 JSON-2 adapter with a deliberately closed read surface."""

    _READ_METHODS = frozenset({"fields_get", "search_read"})

    def __init__(
        self,
        config: Json2Config,
        *,
        transport: Transport | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        """Create an adapter with injectable HTTP and clock dependencies.

        Dependency injection keeps unit tests deterministic while production
        defaults use :func:`_urllib_transport` and the current UTC time.
        """

        self._config = config
        self._transport = transport or _urllib_transport
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._fingerprint: TargetFingerprint | None = None
        self._fingerprint_limitations: tuple[str, ...] = ()

    def get_target_fingerprint(self) -> TargetFingerprint:
        """Read and cache the Odoo version, database, time, and module versions.

        Version endpoints and module visibility can be restricted in Odoo.
        Such gaps are captured as explicit limitations rather than silently
            changing the target identity.
        """

        if self._fingerprint is not None:
            return self._fingerprint
        version = "unknown"
        limitations: list[str] = []
        try:
            status, payload = self._request_url(
                f"{self._config.base_url}/web/version",
                method="GET",
                body=None,
            )
            if status == 200 and isinstance(payload, dict):
                version = str(payload.get("version", "unknown"))
        except ConnectorTransportError:
            limitations.append("Odoo version endpoint unavailable")

        module_versions: dict[str, str] = {}
        if self._config.relevant_modules:
            try:
                module_rows = self._post_read_method(
                    "ir.module.module",
                    "search_read",
                    {
                        "domain": [
                            ["name", "in", list(self._config.relevant_modules)]
                        ],
                        "fields": ["name", "installed_version"],
                        "limit": len(self._config.relevant_modules),
                        "order": "name asc",
                        "context": dict(self._config.context),
                    },
                )
                module_versions = {
                    str(row["name"]): str(row.get("installed_version") or "")
                    for row in module_rows
                }
            except ConnectorError:
                # Module visibility is explicitly non-blocking.
                limitations.append("module version access unavailable")

        self._fingerprint = TargetFingerprint(
            target_hash=target_identity_hash(
                connection_mode=self._config.connection_mode,
                base_url=self._config.base_url,
                database=self._config.database,
            ),
            connection_mode=self._config.connection_mode,
            database=self._config.database,
            odoo_version=version,
            snapshot_timestamp=self._now()
            .astimezone(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
            module_versions=module_versions,
        )
        self._fingerprint_limitations = tuple(limitations)
        return self._fingerprint

    def get_model_metadata(
        self, requests: Sequence[MetadataRequest]
    ) -> MetadataSnapshot:
        """Fetch requested field definitions through batched ``fields_get`` calls.

        There is one call per requested model, not one call per field.  This
        prevents an N+1 pattern as profiles grow.
        """

        fingerprint = self.get_target_fingerprint()
        ordered_requests = tuple(sorted(requests, key=lambda item: item.model))
        constraints: dict[str, tuple[UniqueConstraintMetadata, ...]] = {}
        limitations = list(self._fingerprint_limitations)
        if any(item.include_unique_constraints for item in ordered_requests):
            constraint_models = tuple(
                dict.fromkeys(
                    item.model
                    for item in ordered_requests
                    if item.include_unique_constraints
                )
            )
            try:
                constraints = self._get_unique_constraints(constraint_models)
            except ConnectorError:
                limitations.append("Odoo unique-constraint access unavailable")

        models: dict[str, ModelMetadata] = {}
        for request in ordered_requests:
            response = self._post_read_method(
                request.model,
                "fields_get",
                {
                    "allfields": (
                        []
                        if request.all_fields
                        else list(request.fields)
                    ),
                    "attributes": [
                        "string",
                        "type",
                        "required",
                        "readonly",
                        "relation",
                        "relation_field",
                        "selection",
                    ],
                    "context": dict(self._config.context),
                },
            )
            if not isinstance(response, dict):
                raise ConnectorIncompleteResultError(
                    f"fields_get returned invalid data for {request.model}"
                )
            fields = {
                name: _parse_field_metadata(name, details)
                for name, details in response.items()
                if request.all_fields or name in request.fields
            }
            models[request.model] = ModelMetadata(
                model=request.model,
                description=None,
                fields=fields,
                unique_constraints=constraints.get(request.model, ()),
            )
        return MetadataSnapshot(
            fingerprint=fingerprint,
            models=models,
            limitations=tuple(limitations),
        )

    def _get_unique_constraints(
        self,
        models: tuple[str, ...],
    ) -> dict[str, tuple[UniqueConstraintMetadata, ...]]:
        """Read constraint evidence in two batched queries, never per model."""

        if not models:
            return {}
        model_rows = self._post_read_method(
            "ir.model",
            "search_read",
            {
                "domain": [["model", "in", list(models)]],
                "fields": ["id", "model"],
                "limit": len(models),
                "order": "id asc",
                "context": dict(self._config.context),
            },
        )
        if not isinstance(model_rows, list):
            raise ConnectorIncompleteResultError(
                "search_read returned invalid data for ir.model"
            )
        model_by_id = {
            int(item["id"]): str(item["model"])
            for item in model_rows
            if "id" in item and str(item.get("model") or "") in models
        }
        if set(model_by_id.values()) != set(models):
            raise ConnectorIncompleteResultError(
                "Odoo constraint metadata omitted a permitted model"
            )

        collected: list[Mapping[str, Any]] = []
        offset = 0
        while True:
            page = self._post_read_method(
                "ir.model.constraint",
                "search_read",
                {
                    "domain": [
                        ["model", "in", sorted(model_by_id)],
                        ["type", "=", "u"],
                    ],
                    "fields": ["id", "name", "definition", "model"],
                    "offset": offset,
                    "limit": self._config.page_size,
                    "order": "id asc",
                    "context": dict(self._config.context),
                },
            )
            if not isinstance(page, list):
                raise ConnectorIncompleteResultError(
                    "search_read returned invalid data for ir.model.constraint"
                )
            collected.extend(page)
            if len(page) < self._config.page_size:
                break
            offset += len(page)

        by_model: dict[str, list[UniqueConstraintMetadata]] = {
            model: [] for model in models
        }
        seen_ids: set[int] = set()
        for item in collected:
            try:
                constraint_id = int(item["id"])
                raw_model = item["model"]
                model_id = int(
                    raw_model[0]
                    if isinstance(raw_model, (list, tuple))
                    else raw_model
                )
                name = str(item["name"])
                definition = str(item["definition"])
            except (KeyError, TypeError, ValueError, IndexError) as error:
                raise ConnectorIncompleteResultError(
                    "Odoo returned invalid unique-constraint metadata"
                ) from error
            model = model_by_id.get(model_id)
            if model is None or constraint_id in seen_ids:
                raise ConnectorIncompleteResultError(
                    "Odoo returned inconsistent unique-constraint metadata"
                )
            seen_ids.add(constraint_id)
            by_model[model].append(
                UniqueConstraintMetadata(name=name, definition=definition)
            )
        return {
            model: tuple(sorted(items, key=lambda item: item.name))
            for model, items in by_model.items()
        }

    def get_records(self, requests: Sequence[RecordRequest]) -> RecordSnapshot:
        """Fetch all matching records through deterministic, paginated reads.

        Each model request is ordered by ``id`` and read in configured pages.
        Repeated identifiers across pages are rejected because they make
        completeness uncertain.
        """

        fingerprint = self.get_target_fingerprint()
        records_by_model: dict[str, dict[int, TargetRecord]] = {}
        fields_by_model: dict[str, tuple[str, ...]] = {}
        for request in sorted(
            requests,
            key=lambda item: (item.model, canonical_json_bytes(portable_value(item.domain))),
        ):
            fields = tuple(dict.fromkeys(("id", *request.fields)))
            projected_fields = tuple(
                field for field in fields if field != "id"
            )
            previous_fields = fields_by_model.setdefault(
                request.model, projected_fields
            )
            if previous_fields != projected_fields:
                raise ConnectorConfigurationError(
                    f"record chunks for {request.model} use different fields"
                )
            offset = 0
            collected: list[TargetRecord] = []
            while True:
                response = self._post_read_method(
                    request.model,
                    "search_read",
                    {
                        "domain": list(request.domain),
                        "fields": list(fields),
                        "offset": offset,
                        "limit": self._config.page_size,
                        "order": "id asc",
                        "context": dict(self._config.context),
                    },
                )
                if not isinstance(response, list):
                    raise ConnectorIncompleteResultError(
                        f"search_read returned invalid data for {request.model}"
                    )
                for item in response:
                    if "id" not in item:
                        raise ConnectorIncompleteResultError(
                            f"search_read omitted id for {request.model}"
                        )
                    collected.append(
                        TargetRecord(
                            model=request.model,
                            odoo_id=int(item["id"]),
                            values={
                                field: item.get(field)
                                for field in request.fields
                                if field in item
                            },
                        )
                    )
                if len(response) < self._config.page_size:
                    break
                offset += len(response)
            unique = {record.odoo_id: record for record in collected}
            if len(unique) != len(collected):
                raise ConnectorIncompleteResultError(
                    f"pagination repeated records for {request.model}"
                )
            merged = records_by_model.setdefault(request.model, {})
            for record_id, record in unique.items():
                previous = merged.setdefault(record_id, record)
                if previous != record:
                    raise ConnectorIncompleteResultError(
                        f"record chunks conflict for {request.model}"
                    )
        records = {
            model: tuple(sorted(items.values(), key=lambda record: record.odoo_id))
            for model, items in sorted(records_by_model.items())
        }
        return RecordSnapshot(
            fingerprint=fingerprint,
            records=records,
            requested_fields=fields_by_model,
        )

    def _post_read_method(
        self, model: str, method: str, payload: Mapping[str, Any]
    ) -> Any:
        """POST an allowlisted read method and translate safe status errors.

        Error response bodies are never included in exceptions because Odoo
        may return internal details or business data.
        """

        if method not in self._READ_METHODS:
            raise ConnectorConfigurationError(
                f"method {method!r} is not an approved read operation"
            )
        encoded_model = quote(model, safe=".")
        url = (
            f"{self._config.base_url}/json/2/"
            f"{encoded_model}/{quote(method, safe='')}"
        )
        status, response = self._request_url(
            url,
            method="POST",
            body=canonical_json_bytes(dict(payload)),
        )
        if status != 200:
            if status in {401, 403}:
                raise ConnectorAuthenticationError(
                    f"Odoo JSON-2 authorization failed with HTTP {status}"
                )
            raise ConnectorTransportError(
                f"Odoo JSON-2 read failed with HTTP {status}"
            )
        return response

    def _request_url(
        self,
        url: str,
        *,
        method: str,
        body: bytes | None,
    ) -> tuple[int, Any]:
        """Send one authenticated request with bounded transient retries.

        Authentication/database headers are created only at this boundary.
        Network failures and selected transient HTTP statuses use a short
        exponential backoff; permanent responses return immediately.
        """

        headers = {
            "Authorization": f"bearer {self._config.api_key}",
            "Content-Type": "application/json; charset=utf-8",
            "X-Odoo-Database": self._config.database,
            "User-Agent": "impodo",
        }
        transient_statuses = {429, 502, 503, 504}
        for attempt in range(self._config.retries + 1):
            try:
                status, payload = self._transport(
                    url,
                    headers,
                    body,
                    self._config.timeout_seconds,
                    method,
                )
            except (TimeoutError, socket.timeout, URLError) as exc:
                if attempt >= self._config.retries:
                    raise ConnectorTransportError(
                        "Odoo JSON-2 read timed out or was unreachable"
                    ) from exc
                time.sleep(0.05 * (2**attempt))
                continue
            if status in transient_statuses and attempt < self._config.retries:
                time.sleep(0.05 * (2**attempt))
                continue
            return status, payload
        raise ConnectorTransportError("Odoo JSON-2 read failed")


def write_metadata_snapshot(
    snapshot: MetadataSnapshot,
    output_path: str | Path,
    *,
    profile_id: str,
) -> None:
    """Serialize a replayable metadata snapshot with stable ordering.

    The profile identifier binds the file to the mapping contract that
    requested it.  :func:`_write_json` performs the final atomic replacement.
    """

    payload = {
        "kind": "metadata",
        "profile": {"id": profile_id},
        "fingerprint": snapshot.fingerprint.portable_dict(),
        "complete": snapshot.complete,
        "limitations": list(snapshot.limitations),
        "models": {
            name: {
                "description": model.description,
                "fields": {
                    field_name: {
                        "type": field.type,
                        "string": field.label,
                        "required": field.required,
                        "readonly": field.readonly,
                        "relation": field.relation,
                        "relation_field": field.relation_field,
                        "selection": [list(item) for item in field.selection],
                    }
                    for field_name, field in sorted(model.fields.items())
                },
                "unique_constraints": [
                    {
                        "name": item.name,
                        "definition": item.definition,
                    }
                    for item in model.unique_constraints
                ],
            }
            for name, model in sorted(snapshot.models.items())
        },
    }
    _write_json(output_path, payload)


def write_record_snapshot(
    snapshot: RecordSnapshot,
    output_path: str | Path,
    *,
    profile_id: str,
    source_hashes: Mapping[str, str],
) -> None:
    """Serialize target records bound to a profile and exact source hashes."""

    payload = {
        "kind": "records",
        "profile": {"id": profile_id},
        "fingerprint": snapshot.fingerprint.portable_dict(),
        "source_hashes": dict(sorted(source_hashes.items())),
        "complete": snapshot.complete,
        "models": {
            model: {
                "requested_fields": list(snapshot.requested_fields.get(model, ())),
                "records": [
                    {
                        "id": record.odoo_id,
                        "values": dict(sorted(record.values.items())),
                    }
                    for record in snapshot.records[model]
                ],
            }
            for model in sorted(snapshot.records)
        },
    }
    _write_json(output_path, payload)


def _parse_fingerprint(data: Mapping[str, Any]) -> TargetFingerprint:
    """Convert portable snapshot JSON into an exact target fingerprint."""

    return TargetFingerprint(
        target_hash=str(data["target_hash"]),
        connection_mode=str(data["connection_mode"]),
        database=str(data["database"]),
        odoo_version=str(data.get("odoo_version", "unknown")),
        snapshot_timestamp=str(data["snapshot_timestamp"]),
        module_versions={
            str(key): str(value)
            for key, value in data.get("module_versions", {}).items()
        },
    )


def _parse_field_metadata(name: str, data: Mapping[str, Any]) -> FieldMetadata:
    """Convert one ``fields_get``/snapshot mapping to typed field metadata."""

    selection = data.get("selection") or ()
    return FieldMetadata(
        name=name,
        label=str(data.get("string") or name),
        type=str(data.get("type", "unknown")),
        required=bool(data.get("required", False)),
        readonly=bool(data.get("readonly", False)),
        relation=data.get("relation"),
        relation_field=data.get("relation_field"),
        selection=tuple(tuple(item) for item in selection),
    )


def _load_json(path: str | Path) -> dict[str, Any]:
    """Read a JSON snapshot and expose parse/filesystem failures uniformly."""

    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConnectorConfigurationError(f"cannot read snapshot {path}: {exc}") from exc


def _validate_snapshot_binding(
    data: Mapping[str, Any],
    expected_profile_id: str | None,
    expected_source_hashes: Mapping[str, str] | None,
) -> None:
    """Verify that a snapshot belongs to the selected profile/source package.

    Older snapshots without optional binding fields remain readable.  When a
    binding is present and an expectation is supplied, however, any mismatch
    is a configuration error.
    """

    profile = data.get("profile")
    if expected_profile_id is not None and profile is not None:
        if profile.get("id") != expected_profile_id:
            raise ConnectorConfigurationError(
                "snapshot profile ID does not match selected profile"
            )
    if expected_source_hashes is not None and "source_hashes" in data:
        actual = {
            str(key): str(value)
            for key, value in data.get("source_hashes", {}).items()
        }
        if actual != dict(expected_source_hashes):
            raise ConnectorConfigurationError(
                "record snapshot source hashes do not match current source package"
            )


def _sha256_bytes(value: bytes) -> str:
    """Return the lowercase SHA-256 hex digest used by snapshot hashes."""

    from hashlib import sha256

    return sha256(value).hexdigest()


def _write_json(path: str | Path, payload: Mapping[str, Any]) -> None:
    """Write canonical JSON through a sibling partial file, then replace."""

    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".partial")
    temporary.write_bytes(canonical_json_bytes(payload) + b"\n")
    temporary.replace(destination)


def _urllib_transport(
    url: str,
    headers: Mapping[str, str],
    body: bytes | None,
    timeout: float,
    method: str,
) -> tuple[int, Any]:
    """Execute one JSON request without forwarding credentials on redirects.

    HTTP error bodies are intentionally discarded.  Other network exceptions
    propagate to :meth:`Json2ReadConnector._request_url`, which owns retry and
    public error handling.
    """

    request = Request(
        url=url,
        data=body,
        headers=dict(headers),
        method=method,
    )
    try:
        opener = build_opener(_NoRedirectHandler())
        with opener.open(request, timeout=timeout) as response:
            raw = response.read()
            return response.status, json.loads(raw or b"null")
    except HTTPError as exc:
        # Do not expose response bodies; they can contain data or internals.
        return exc.code, None


class _NoRedirectHandler(HTTPRedirectHandler):
    """Prevent bearer credentials from being forwarded to any redirect."""

    def redirect_request(
        self,
        req,
        fp,
        code,
        msg,
        headers,
        newurl,
    ):
        return None
