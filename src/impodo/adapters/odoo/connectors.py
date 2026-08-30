"""Define the profiler's read-only boundary to Odoo and snapshot files.

The planner supplies batched :class:`MetadataRequest` and
:class:`RecordRequest` objects.  A connector fulfils those requests either
from Odoo 19's JSON-2 API or from deterministic JSON snapshots.  Both
implementations return the same typed snapshot contracts, so the metadata
validator, catalog, and engine do not depend on transport details.

The live connector exposes bounded metadata/record reads plus one fixed
identity probe built from ``context_get``, an exact self-record read, and
model-level ``has_access('read')`` calls. This closed surface is a deliberate
safety control: the profiler cannot create, update, delete, enumerate users,
or execute a caller-selected method. The practical Stage-J writer lives in
:mod:`impodo.odoo_writer` behind a separate port and durable journal; it is not
added to ``OdooReadConnector``. Post-write checks use the separate closed
:mod:`impodo.odoo_readback` adapter.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from io import StringIO
import json
import os
from pathlib import Path
import re
import socket
import time
from typing import Any, Callable, Mapping, Protocol, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from impodo.domain.mapping.create_field_policy import supports_create_default_capture
from impodo.domain.shared.models import (
    FieldMetadata,
    ModelMetadata,
    OdooReadIdentity,
    ProtectedOdooReadContext,
    OdooWriteIdentity,
    TargetFingerprint,
    TargetRecord,
    UniqueConstraintMetadata,
    canonical_json_bytes,
    canonical_json_text,
    portable_value,
    target_identity_hash,
)


_TECHNICAL_MODEL = re.compile(r"^[a-z_][a-z0-9_.]{0,127}$")
_MAX_IDENTITY_MODELS = 100
_MAX_IDENTITY_COMPANIES = 1_000
STABLE_ODOO_LANGUAGE = "en_US"
STABLE_ODOO_TIMEZONE = "UTC"


from impodo.domain.odoo.contracts import (
    ConnectorAuthenticationError,
    ConnectorAuthorizationError,
    ConnectorConfigurationError,
    ConnectorError,
    ConnectorIncompleteResultError,
    ConnectorTransportError,
    MetadataRequest,
    MetadataSnapshot,
    OdooReadConnector,
    OdooReadIdentityProbe,
    OdooWriteIdentityProbe,
    RecordRequest,
    RecordSnapshot,
    bind_snapshot_hashes,
    metadata_snapshot_payload,
    record_snapshot_json,
)


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
        self._fingerprint = _parse_fingerprint(self._metadata_data["fingerprint"])
        record_fingerprint = _parse_fingerprint(self._records_data["fingerprint"])
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
                tuple(sorted(all_fields)) if request.all_fields else request.fields
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
        stored_defaults = self._metadata_data.get("create_defaults", {})
        projected_defaults = {}
        for request in requests:
            model = models.get(request.model)
            if model is None or request.model not in stored_defaults:
                continue
            projected_defaults[request.model] = {
                str(name): portable_value(value)
                for name, value in dict(stored_defaults.get(request.model, {})).items()
                if name in model.fields
            }
        return MetadataSnapshot(
            fingerprint=self._fingerprint,
            models=models,
            create_defaults=projected_defaults,
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
            if request.limit is not None and request.limit <= 0:
                raise ConnectorConfigurationError(
                    "record request limit must be positive"
                )
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
                if request.limit is not None and len(model_records) >= request.limit:
                    break
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
    max_request_bytes: int = 64 * 1024
    max_response_bytes: int = 8 * 1024 * 1024
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
            raise ConnectorConfigurationError("connection mode must be LOCAL or REMOTE")
        object.__setattr__(self, "connection_mode", connection_mode)
        if connection_mode == "LOCAL":
            if (
                parsed_url.scheme not in {"http", "https"}
                or not is_literal_loopback
                or parsed_url.path not in {"", "/"}
            ):
                raise ConnectorConfigurationError(
                    "insecure local mode permits only a literal loopback Odoo URL"
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
        if self.max_request_bytes < 1 or self.max_response_bytes < 1:
            raise ConnectorConfigurationError(
                "JSON-2 request and response limits must be positive"
            )

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
        connection_mode = "LOCAL" if hostname in {"127.0.0.1", "::1"} else "REMOTE"
        return cls(
            base_url=base_url,
            database=os.environ["IMPODO_ODOO_DATABASE"],
            api_key=os.environ["IMPODO_ODOO_API_KEY"],
            connection_mode=connection_mode,
            timeout_seconds=float(os.environ.get("IMPODO_ODOO_TIMEOUT_SECONDS", "30")),
            page_size=int(os.environ.get("IMPODO_ODOO_PAGE_SIZE", "500")),
        )


def target_record_read_context(
    context: Mapping[str, Any],
) -> dict[str, Any]:
    """Return the stable governed context for archived-inclusive target reads."""

    effective = dict(context)
    effective["active_test"] = False
    effective["lang"] = STABLE_ODOO_LANGUAGE
    effective["tz"] = STABLE_ODOO_TIMEZONE
    return effective


def target_record_read_config(config: Json2Config) -> Json2Config:
    """Bind a JSON-2 configuration to archived-inclusive target reads."""

    return replace(
        config,
        context=target_record_read_context(config.context),
    )


Transport = Callable[
    [str, Mapping[str, str], bytes | None, float, str],
    tuple[int, Any],
]


class Json2ReadConnector:
    """Odoo 19 JSON-2 adapter with a deliberately closed read surface."""

    _READ_METHODS = frozenset(
        {
            "context_get",
            "default_get",
            "fields_get",
            "has_access",
            "search_read",
        }
    )

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
        self._transport = transport or (
            lambda url, headers, body, timeout, method: _urllib_transport(
                url,
                headers,
                body,
                timeout,
                method,
                maximum_bytes=config.max_response_bytes,
            )
        )
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
                        "domain": [["name", "in", list(self._config.relevant_modules)]],
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

    def probe_read_identity(
        self,
        models: Sequence[str],
    ) -> OdooReadIdentity:
        """Probe only the API key's own user and explicit model-read access.

        Odoo 19 documents ``res.users/context_get`` as the JSON-2 mechanism
        for retrieving the current user ID from an API key. The returned ID is
        then used in one exact self-record ``search_read``. No broad user,
        group, company, ACL, or rule catalogue is exposed.
        """

        identity, _context = self._probe_capture_identity(models)
        return identity

    def _probe_capture_identity(
        self,
        models: Sequence[str],
    ) -> tuple[OdooReadIdentity, ProtectedOdooReadContext]:
        """Return identity plus the protected context required by live capture."""

        requested_models = tuple(sorted(dict.fromkeys(models)))
        if not requested_models:
            raise ConnectorConfigurationError(
                "read identity probe requires at least one model"
            )
        if len(requested_models) > _MAX_IDENTITY_MODELS:
            raise ConnectorConfigurationError(
                "read identity probe contains too many models"
            )
        if any(_TECHNICAL_MODEL.fullmatch(model) is None for model in requested_models):
            raise ConnectorConfigurationError(
                "read identity probe contains an invalid model"
            )

        raw_context = self._post_read_method(
            "res.users",
            "context_get",
            {"context": dict(self._config.context)},
        )
        if not isinstance(raw_context, Mapping):
            raise ConnectorIncompleteResultError(
                "Odoo read identity context is invalid"
            )
        try:
            user_id = int(raw_context["uid"])
        except (KeyError, TypeError, ValueError) as error:
            raise ConnectorIncompleteResultError(
                "Odoo read identity omitted the current user"
            ) from error
        if user_id <= 0:
            raise ConnectorIncompleteResultError(
                "Odoo read identity contains an invalid current user"
            )

        user_rows = self._post_read_method(
            "res.users",
            "search_read",
            {
                "domain": [["id", "=", user_id]],
                "fields": [
                    "id",
                    "login",
                    "company_id",
                    "group_ids",
                    "share",
                ],
                "limit": 2,
                "order": "id asc",
                "context": dict(self._config.context),
            },
        )
        if not isinstance(user_rows, list) or len(user_rows) != 1:
            raise ConnectorIncompleteResultError(
                "Odoo read identity did not return the exact current user"
            )
        user = user_rows[0]
        if not isinstance(user, Mapping):
            raise ConnectorIncompleteResultError("Odoo read identity user is invalid")
        try:
            returned_user_id = int(user["id"])
            login = str(user["login"]).strip()
            primary_company_id = _many2one_id(user["company_id"])
            group_ids = _positive_ids(user["group_ids"])
            share = bool(user["share"])
        except (KeyError, TypeError, ValueError) as error:
            raise ConnectorIncompleteResultError(
                "Odoo read identity user fields are invalid"
            ) from error
        if returned_user_id != user_id or not login:
            raise ConnectorIncompleteResultError(
                "Odoo read identity does not match the authenticated user"
            )
        if len(login) > 320:
            raise ConnectorIncompleteResultError(
                "Odoo read identity user fields exceed safe limits"
            )

        company_rows = self._post_read_method(
            "res.company",
            "search_read",
            {
                "domain": [
                    ["user_ids", "in", [user_id]],
                    ["active", "=", True],
                ],
                "fields": ["id"],
                "limit": _MAX_IDENTITY_COMPANIES + 1,
                "order": "id asc",
                "context": dict(self._config.context),
            },
        )
        if not isinstance(company_rows, list):
            raise ConnectorIncompleteResultError(
                "Odoo read identity company scope is invalid"
            )
        if len(company_rows) > _MAX_IDENTITY_COMPANIES:
            raise ConnectorIncompleteResultError(
                "Odoo read identity company scope exceeds the safe limit"
            )
        try:
            company_ids = _positive_ids(tuple(item["id"] for item in company_rows))
        except (KeyError, TypeError, ValueError) as error:
            raise ConnectorIncompleteResultError(
                "Odoo read identity company scope is invalid"
            ) from error
        if primary_company_id not in company_ids:
            raise ConnectorIncompleteResultError(
                "Odoo read identity omitted the primary company"
            )
        effective_company_ids = (
            primary_company_id,
            *(item for item in company_ids if item != primary_company_id),
        )

        for model in requested_models:
            allowed = self._post_read_method(
                model,
                "has_access",
                {
                    "ids": [],
                    "operation": "read",
                    "context": dict(self._config.context),
                },
            )
            if allowed is not True:
                raise ConnectorAuthorizationError(
                    f"Odoo read principal cannot access model {model}"
                )

        fingerprint = self.get_target_fingerprint()
        principal_hash = _content_hash(
            {
                "contract_version": 1,
                "kind": "ODOO_USER",
                "target_hash": fingerprint.target_hash,
                "user_id": user_id,
                "login": login,
            }
        )
        permission_hash = _content_hash(
            {
                "contract_version": 1,
                "principal_hash": principal_hash,
                "direct_group_ids": group_ids,
                "readable_models": requested_models,
                "share": share,
            }
        )
        access_context = {
            key: value
            for key, value in self._config.context.items()
            if key not in {"lang", "tz"}
        }
        context_hash = _content_hash(
            {
                "contract_version": 3,
                "kind": "ODOO_ACCESS_SCOPE",
                "primary_company_id": primary_company_id,
                "allowed_company_ids": effective_company_ids,
                "active_test": bool(self._config.context.get("active_test", True)),
                "request_context": portable_value(access_context),
            }
        )
        return (
            OdooReadIdentity(
                target_hash=fingerprint.target_hash,
                principal_hash=principal_hash,
                permission_hash=permission_hash,
                context_hash=context_hash,
                readable_models=requested_models,
                observed_at=fingerprint.snapshot_timestamp,
            ),
            ProtectedOdooReadContext(
                primary_company_id=primary_company_id,
                allowed_company_ids=effective_company_ids,
            ),
        )

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
        create_defaults: dict[str, Mapping[str, object]] = {}
        for request in ordered_requests:
            response = self._post_read_method(
                request.model,
                "fields_get",
                {
                    "allfields": ([] if request.all_fields else list(request.fields)),
                    "attributes": [
                        "string",
                        "type",
                        "required",
                        "readonly",
                        "relation",
                        "relation_field",
                        "selection",
                        "store",
                        "compute",
                        "inverse",
                        "related",
                        "translate",
                        "company_dependent",
                        "searchable",
                        "sortable",
                        "exportable",
                        "digits",
                        "currency_field",
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
            required_scalar_fields = tuple(
                sorted(
                    name
                    for name, field_metadata in fields.items()
                    if _captures_create_default(field_metadata)
                )
            )
            if not required_scalar_fields:
                create_defaults[request.model] = {}
                continue
            try:
                defaults = self._post_read_method(
                    request.model,
                    "default_get",
                    {
                        "fields": list(required_scalar_fields),
                        "context": dict(self._config.context),
                    },
                )
                if not isinstance(defaults, dict) or not set(defaults).issubset(
                    required_scalar_fields
                ):
                    raise ConnectorIncompleteResultError(
                        f"default_get returned invalid data for {request.model}"
                    )
                create_defaults[request.model] = {
                    str(name): portable_value(value) for name, value in defaults.items()
                }
            except ConnectorError:
                limitations.append(
                    f"Odoo create-default access unavailable for {request.model}"
                )
        return MetadataSnapshot(
            fingerprint=fingerprint,
            models=models,
            create_defaults=create_defaults,
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
                    raw_model[0] if isinstance(raw_model, (list, tuple)) else raw_model
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
            key=lambda item: (
                item.model,
                canonical_json_bytes(portable_value(item.domain)),
                item.limit if item.limit is not None else -1,
            ),
        ):
            if request.limit is not None and request.limit <= 0:
                raise ConnectorConfigurationError(
                    "record request limit must be positive"
                )
            fields = tuple(dict.fromkeys(("id", *request.fields)))
            projected_fields = tuple(field for field in fields if field != "id")
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
                remaining = (
                    request.limit - len(collected)
                    if request.limit is not None
                    else self._config.page_size
                )
                if remaining <= 0:
                    break
                page_limit = min(self._config.page_size, remaining)
                response = self._post_read_method(
                    request.model,
                    "search_read",
                    {
                        "domain": list(request.domain),
                        "fields": list(fields),
                        "offset": offset,
                        "limit": page_limit,
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
                if len(response) < page_limit:
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
        url = f"{self._config.base_url}/json/2/{encoded_model}/{quote(method, safe='')}"
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
            raise ConnectorTransportError(f"Odoo JSON-2 read failed with HTTP {status}")
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
        if body is not None and len(body) > self._config.max_request_bytes:
            raise ConnectorConfigurationError("JSON-2 request exceeds the safe limit")
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
                if len(canonical_json_bytes(payload)) > self._config.max_response_bytes:
                    raise ConnectorIncompleteResultError(
                        "JSON-2 response exceeds the safe limit"
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


class Json2CaptureIdentityProbe:
    """Dedicated closed probe returning ephemeral protected capture context."""

    def __init__(
        self,
        config: Json2Config,
        *,
        transport: Transport | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._reader = Json2ReadConnector(
            config,
            transport=transport,
            now=now,
        )

    def probe_capture_identity(
        self,
        models: Sequence[str],
    ) -> tuple[OdooReadIdentity, ProtectedOdooReadContext]:
        return self._reader._probe_capture_identity(models)


class Json2WriteIdentityConnector:
    """Closed JSON-2 probe for a separate write/read-back credential."""

    def __init__(
        self,
        config: Json2Config,
        *,
        transport: Transport | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._reader = Json2ReadConnector(
            config,
            transport=transport,
            now=now,
        )

    def probe_write_identity(
        self,
        readable_models: Sequence[str],
        writable_models: Sequence[str],
    ) -> OdooWriteIdentity:
        """Probe read-back plus write access without making a target change."""

        read_identity = self._reader.probe_read_identity(readable_models)
        requested_writes = tuple(sorted(dict.fromkeys(writable_models)))
        if not requested_writes:
            raise ConnectorConfigurationError(
                "write identity probe requires at least one writable model"
            )
        if len(requested_writes) > _MAX_IDENTITY_MODELS:
            raise ConnectorConfigurationError(
                "write identity probe contains too many writable models"
            )
        if any(_TECHNICAL_MODEL.fullmatch(model) is None for model in requested_writes):
            raise ConnectorConfigurationError(
                "write identity probe contains an invalid writable model"
            )
        if not set(requested_writes).issubset(read_identity.readable_models):
            raise ConnectorConfigurationError(
                "every writable model must also be in the read-back scope"
            )
        for model in requested_writes:
            allowed = self._reader._post_read_method(
                model,
                "has_access",
                {
                    "ids": [],
                    "operation": "write",
                    "context": dict(self._reader._config.context),
                },
            )
            if allowed is not True:
                raise ConnectorAuthorizationError(
                    f"Odoo write principal cannot access model {model}"
                )
        return OdooWriteIdentity(
            target_hash=read_identity.target_hash,
            principal_hash=read_identity.principal_hash,
            permission_hash=_content_hash(
                {
                    "contract_version": 1,
                    "kind": "ODOO_WRITE_AND_READBACK_ACCESS",
                    "principal_hash": read_identity.principal_hash,
                    "read_permission_hash": read_identity.permission_hash,
                    "readable_models": read_identity.readable_models,
                    "writable_models": requested_writes,
                }
            ),
            context_hash=read_identity.context_hash,
            readable_models=read_identity.readable_models,
            writable_models=requested_writes,
            observed_at=read_identity.observed_at,
        )


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
        stored=_metadata_bool(data, "store"),
        computed=_metadata_presence(data, "compute"),
        has_inverse=_metadata_presence(data, "inverse"),
        related=_metadata_presence(data, "related"),
        translated=_metadata_bool(data, "translate"),
        company_dependent=_metadata_bool(data, "company_dependent"),
        searchable=_metadata_bool(data, "searchable"),
        sortable=_metadata_bool(data, "sortable"),
        exportable=_metadata_bool(data, "exportable"),
        digits=_metadata_digits(data.get("digits")),
        currency_field=(
            str(data["currency_field"]) if data.get("currency_field") else None
        ),
    )


def _captures_create_default(field_metadata: FieldMetadata) -> bool:
    """Keep runtime-default reads bounded to required writable scalar fields."""

    return supports_create_default_capture(field_metadata)


def _metadata_bool(data: Mapping[str, Any], key: str) -> bool | None:
    value = data.get(key)
    return value if isinstance(value, bool) else None


def _metadata_presence(data: Mapping[str, Any], key: str) -> bool | None:
    if key not in data:
        return None
    return bool(data[key])


def _metadata_digits(value: Any) -> tuple[int, int] | None:
    if (
        not isinstance(value, (list, tuple))
        or len(value) != 2
        or any(isinstance(item, bool) or not isinstance(item, int) for item in value)
    ):
        return None
    return int(value[0]), int(value[1])


def _load_json(path: str | Path) -> dict[str, Any]:
    """Read a JSON snapshot and expose parse/filesystem failures uniformly."""

    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConnectorConfigurationError(
            f"cannot read snapshot {path}: {exc}"
        ) from exc


def _validate_snapshot_binding(
    data: Mapping[str, Any],
    expected_profile_id: str | None,
    expected_source_hashes: Mapping[str, str] | None,
) -> None:
    """Verify exact profile and source-package snapshot bindings."""

    profile = data.get("profile")
    if expected_profile_id is not None:
        if not isinstance(profile, Mapping) or profile.get("id") != expected_profile_id:
            raise ConnectorConfigurationError(
                "snapshot profile ID does not match selected profile"
            )
    if expected_source_hashes is not None:
        if "source_hashes" not in data:
            raise ConnectorConfigurationError(
                "record snapshot has no source-package binding"
            )
        actual = {
            str(key): str(value) for key, value in data.get("source_hashes", {}).items()
        }
        if actual != dict(expected_source_hashes):
            raise ConnectorConfigurationError(
                "record snapshot source hashes do not match current source package"
            )


def _sha256_bytes(value: bytes) -> str:
    """Return the lowercase SHA-256 hex digest used by snapshot hashes."""

    from hashlib import sha256

    return sha256(value).hexdigest()


def _content_hash(value: Mapping[str, Any]) -> str:
    """Return one canonical non-secret evidence hash."""

    return "sha256:" + _sha256_bytes(canonical_json_bytes(value))


def _many2one_id(value: Any) -> int:
    """Parse only the identifier portion of one JSON-2 many2one value."""

    candidate = value[0] if isinstance(value, (list, tuple)) and value else value
    if isinstance(candidate, bool):
        raise ValueError("boolean is not a valid Odoo identifier")
    identifier = int(candidate)
    if identifier <= 0:
        raise ValueError("Odoo identifier must be positive")
    return identifier


def _positive_ids(value: Any) -> tuple[int, ...]:
    """Validate one bounded many2many identifier projection."""

    if not isinstance(value, (list, tuple)) or len(value) > 10_000:
        raise ValueError("Odoo identifier collection is invalid")
    identifiers: list[int] = []
    for item in value:
        if isinstance(item, bool):
            raise ValueError("boolean is not a valid Odoo identifier")
        identifier = int(item)
        if identifier <= 0:
            raise ValueError("Odoo identifier must be positive")
        identifiers.append(identifier)
    if len(identifiers) != len(set(identifiers)):
        raise ValueError("Odoo identifier collection contains duplicates")
    return tuple(sorted(identifiers))


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
    *,
    maximum_bytes: int = 8 * 1024 * 1024,
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
            raw = response.read(maximum_bytes + 1)
            if len(raw) > maximum_bytes:
                raise ConnectorIncompleteResultError(
                    "JSON-2 response exceeds the safe limit"
                )
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
        """Reject redirects so bearer credentials never reach another URL."""

        return None
