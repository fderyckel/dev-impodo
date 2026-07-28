"""Read-only Odoo connector port and implementations."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import socket
import time
from typing import Any, Callable, Mapping, Protocol, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from .models import (
    EnvironmentFingerprint,
    FieldMetadata,
    ModelMetadata,
    TargetRecord,
    canonical_json_bytes,
)


@dataclass(frozen=True, slots=True)
class MetadataRequest:
    model: str
    fields: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RecordRequest:
    model: str
    fields: tuple[str, ...]
    domain: tuple[Any, ...] = ()


@dataclass(frozen=True, slots=True)
class MetadataSnapshot:
    fingerprint: EnvironmentFingerprint
    models: Mapping[str, ModelMetadata]
    complete: bool = True
    limitations: tuple[str, ...] = ()
    content_hash: str | None = None


@dataclass(frozen=True, slots=True)
class RecordSnapshot:
    fingerprint: EnvironmentFingerprint
    records: Mapping[str, tuple[TargetRecord, ...]]
    requested_fields: Mapping[str, tuple[str, ...]]
    complete: bool = True
    content_hash: str | None = None


class OdooReadConnector(Protocol):
    def get_environment_fingerprint(self) -> EnvironmentFingerprint: ...

    def get_model_metadata(
        self, requests: Sequence[MetadataRequest]
    ) -> MetadataSnapshot: ...

    def get_records(self, requests: Sequence[RecordRequest]) -> RecordSnapshot: ...


class ConnectorError(RuntimeError):
    pass


class ConnectorConfigurationError(ConnectorError):
    pass


class ConnectorAuthenticationError(ConnectorError):
    pass


class ConnectorTransportError(ConnectorError):
    pass


class ConnectorIncompleteResultError(ConnectorError):
    pass


class SnapshotConnector:
    """Deterministic connector backed by normalized JSON fixture files."""

    def __init__(
        self,
        *,
        metadata_path: str | Path | None = None,
        records_path: str | Path | None = None,
        combined_path: str | Path | None = None,
        expected_profile_id: str | None = None,
        expected_profile_version: str | None = None,
        expected_source_hashes: Mapping[str, str] | None = None,
    ) -> None:
        if combined_path is not None:
            combined = _load_json(combined_path)
            self._metadata_data = combined["metadata"]
            self._records_data = combined["records"]
            self._metadata_hash = "sha256:" + _sha256_bytes(
                canonical_json_bytes(self._metadata_data)
            )
            self._records_hash = "sha256:" + _sha256_bytes(
                canonical_json_bytes(self._records_data)
            )
        else:
            if metadata_path is None or records_path is None:
                raise ConnectorConfigurationError(
                    "SnapshotConnector requires combined_path or both snapshot paths"
                )
            metadata_file = Path(metadata_path)
            records_file = Path(records_path)
            metadata_bytes = metadata_file.read_bytes()
            records_bytes = records_file.read_bytes()
            self._metadata_data = json.loads(metadata_bytes)
            self._records_data = json.loads(records_bytes)
            self._metadata_hash = "sha256:" + _sha256_bytes(metadata_bytes)
            self._records_hash = "sha256:" + _sha256_bytes(records_bytes)
        _validate_snapshot_binding(
            self._metadata_data,
            expected_profile_id,
            expected_profile_version,
            None,
        )
        _validate_snapshot_binding(
            self._records_data,
            expected_profile_id,
            expected_profile_version,
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

    def get_environment_fingerprint(self) -> EnvironmentFingerprint:
        return self._fingerprint

    def get_model_metadata(
        self, requests: Sequence[MetadataRequest]
    ) -> MetadataSnapshot:
        models: dict[str, ModelMetadata] = {}
        available = self._metadata_data.get("models", {})
        for request in requests:
            model_data = available.get(request.model)
            if model_data is None:
                continue
            all_fields = model_data.get("fields", {})
            fields = {
                name: _parse_field_metadata(name, all_fields[name])
                for name in request.fields
                if name in all_fields
            }
            models[request.model] = ModelMetadata(
                model=request.model,
                description=model_data.get("description"),
                fields=fields,
            )
        complete = bool(self._metadata_data.get("complete", True))
        return MetadataSnapshot(
            fingerprint=self._fingerprint,
            models=models,
            complete=complete,
            limitations=tuple(self._metadata_data.get("limitations", ())),
            content_hash=self._metadata_hash,
        )

    def get_records(self, requests: Sequence[RecordRequest]) -> RecordSnapshot:
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
            content_hash=self._records_hash,
        )


@dataclass(frozen=True, slots=True)
class Json2Config:
    base_url: str
    database: str
    api_key: str = field(repr=False)
    environment: str
    timeout_seconds: float = 30.0
    page_size: int = 500
    retries: int = 2
    context: Mapping[str, Any] = field(default_factory=dict)
    relevant_modules: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.base_url.startswith("https://"):
            raise ConnectorConfigurationError(
                "UC_ODOO_BASE_URL must use HTTPS"
            )
        if self.environment.upper() not in {"DEV", "TEST"}:
            raise ConnectorConfigurationError(
                "read-only milestone permits only DEV or TEST"
            )
        if self.page_size < 1:
            raise ConnectorConfigurationError("page_size must be positive")

    @classmethod
    def from_environment(cls) -> "Json2Config":
        missing = [
            name
            for name in (
                "UC_ODOO_BASE_URL",
                "UC_ODOO_DATABASE",
                "UC_ODOO_API_KEY",
                "UC_ODOO_ENVIRONMENT",
            )
            if not os.environ.get(name)
        ]
        if missing:
            raise ConnectorConfigurationError(
                "missing environment variables: " + ", ".join(missing)
            )
        environment = os.environ["UC_ODOO_ENVIRONMENT"].upper()
        if environment not in {"DEV", "TEST"}:
            raise ConnectorConfigurationError(
                "read-only milestone permits only DEV or TEST"
            )
        return cls(
            base_url=os.environ["UC_ODOO_BASE_URL"].rstrip("/"),
            database=os.environ["UC_ODOO_DATABASE"],
            api_key=os.environ["UC_ODOO_API_KEY"],
            environment=environment,
            timeout_seconds=float(
                os.environ.get("UC_ODOO_TIMEOUT_SECONDS", "30")
            ),
            page_size=int(os.environ.get("UC_ODOO_PAGE_SIZE", "500")),
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
        self._config = config
        self._transport = transport or _urllib_transport
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._fingerprint: EnvironmentFingerprint | None = None
        self._fingerprint_limitations: tuple[str, ...] = ()

    def get_environment_fingerprint(self) -> EnvironmentFingerprint:
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

        self._fingerprint = EnvironmentFingerprint(
            environment=self._config.environment,
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
        fingerprint = self.get_environment_fingerprint()
        models: dict[str, ModelMetadata] = {}
        for request in sorted(requests, key=lambda item: item.model):
            response = self._post_read_method(
                request.model,
                "fields_get",
                {
                    "allfields": list(request.fields),
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
                if name in request.fields
            }
            models[request.model] = ModelMetadata(
                model=request.model,
                description=None,
                fields=fields,
            )
        return MetadataSnapshot(
            fingerprint=fingerprint,
            models=models,
            limitations=self._fingerprint_limitations,
        )

    def get_records(self, requests: Sequence[RecordRequest]) -> RecordSnapshot:
        fingerprint = self.get_environment_fingerprint()
        records: dict[str, tuple[TargetRecord, ...]] = {}
        fields_by_model: dict[str, tuple[str, ...]] = {}
        for request in sorted(requests, key=lambda item: item.model):
            fields = tuple(dict.fromkeys(("id", *request.fields)))
            fields_by_model[request.model] = tuple(
                field for field in fields if field != "id"
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
            records[request.model] = tuple(
                sorted(unique.values(), key=lambda record: record.odoo_id)
            )
        return RecordSnapshot(
            fingerprint=fingerprint,
            records=records,
            requested_fields=fields_by_model,
        )

    def _post_read_method(
        self, model: str, method: str, payload: Mapping[str, Any]
    ) -> Any:
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
        headers = {
            "Authorization": f"bearer {self._config.api_key}",
            "Content-Type": "application/json; charset=utf-8",
            "X-Odoo-Database": self._config.database,
            "User-Agent": "uc-migration-profiler/0.2.0",
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
    profile_version: str,
) -> None:
    payload = {
        "contract_version": 1,
        "kind": "metadata",
        "profile": {"id": profile_id, "version": profile_version},
        "fingerprint": snapshot.fingerprint.portable_dict(),
        "complete": snapshot.complete,
        "limitations": list(snapshot.limitations),
        "models": {
            name: {
                "description": model.description,
                "fields": {
                    field_name: {
                        "type": field.type,
                        "required": field.required,
                        "readonly": field.readonly,
                        "relation": field.relation,
                        "relation_field": field.relation_field,
                        "selection": [list(item) for item in field.selection],
                    }
                    for field_name, field in sorted(model.fields.items())
                },
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
    profile_version: str,
    source_hashes: Mapping[str, str],
) -> None:
    payload = {
        "contract_version": 1,
        "kind": "records",
        "profile": {"id": profile_id, "version": profile_version},
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


def _parse_fingerprint(data: Mapping[str, Any]) -> EnvironmentFingerprint:
    return EnvironmentFingerprint(
        environment=str(data["environment"]),
        database=str(data["database"]),
        odoo_version=str(data.get("odoo_version", "unknown")),
        snapshot_timestamp=str(data["snapshot_timestamp"]),
        module_versions={
            str(key): str(value)
            for key, value in data.get("module_versions", {}).items()
        },
    )


def _parse_field_metadata(name: str, data: Mapping[str, Any]) -> FieldMetadata:
    selection = data.get("selection") or ()
    return FieldMetadata(
        name=name,
        type=str(data.get("type", "unknown")),
        required=bool(data.get("required", False)),
        readonly=bool(data.get("readonly", False)),
        relation=data.get("relation"),
        relation_field=data.get("relation_field"),
        selection=tuple(tuple(item) for item in selection),
    )


def _load_json(path: str | Path) -> dict[str, Any]:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConnectorConfigurationError(f"cannot read snapshot {path}: {exc}") from exc


def _validate_snapshot_binding(
    data: Mapping[str, Any],
    expected_profile_id: str | None,
    expected_profile_version: str | None,
    expected_source_hashes: Mapping[str, str] | None,
) -> None:
    profile = data.get("profile")
    if expected_profile_id is not None and profile is not None:
        if profile.get("id") != expected_profile_id:
            raise ConnectorConfigurationError(
                "snapshot profile ID does not match selected profile"
            )
    if expected_profile_version is not None and profile is not None:
        if profile.get("version") != expected_profile_version:
            raise ConnectorConfigurationError(
                "snapshot profile version does not match selected profile"
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
    from hashlib import sha256

    return sha256(value).hexdigest()


def _write_json(path: str | Path, payload: Mapping[str, Any]) -> None:
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
    request = Request(
        url=url,
        data=body,
        headers=dict(headers),
        method=method,
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            if urlparse(response.geturl()).hostname != urlparse(url).hostname:
                raise URLError("redirected to an unexpected host")
            raw = response.read()
            return response.status, json.loads(raw or b"null")
    except HTTPError as exc:
        # Do not expose response bodies; they can contain data or internals.
        return exc.code, None
