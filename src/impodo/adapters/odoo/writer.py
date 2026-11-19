"""Closed JSON-2 writer for one version-qualified execution preview.

This module is deliberately separate from the read connector.  Callers can
only resolve an exact business key, import bounded creates with reviewed
external IDs, create bounded rows for the disposable-local path, or update one
known record within a per-preview capability derived from captured schema and
the confirmed mapping.  There is no global model/field allowlist and no
caller-controlled method name, context, delete, SQL, ``sudo``, or generic RPC
surface.
"""

from __future__ import annotations

import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from dataclasses import field as dataclass_field
from datetime import UTC, datetime
from functools import partial
from http.client import HTTPException
from typing import Any
from urllib.error import URLError
from urllib.parse import quote

from impodo.adapters.odoo.connectors import (
    Json2Config,
    Transport,
    _urllib_transport,
    target_record_read_context,
)
from impodo.adapters.odoo.external_identity import (
    external_id_bindings,
    external_id_domain,
    external_lookup_values,
)
from impodo.adapters.odoo.lookup_values import bounded_lookup_query, lookup_values_equal
from impodo.domain.execution.models import MAX_CREATE_BATCH_ROWS
from impodo.domain.execution.odoo_scope import OdooApiScope
from impodo.domain.execution.odoo_write import (
    MAX_IDENTITY_LOOKUP_KEYS,
    MAX_PROJECTED_RECEIPT_IDS,
    OdooIdentityLookupUnavailable,
    OdooRequestDeferred,
    OdooWriteOutcomeUnknown,
    OdooWriteRejected,
)
from impodo.domain.execution.operator_errors import capture_operator_error
from impodo.domain.execution.write_diagnostics import (
    OdooWriteDiagnostic,
    OdooWriteErrorType,
    UncertainWriteReason,
    WriteFailureCode,
)
from impodo.domain.mapping.source_record_identity import SOURCE_EXTERNAL_IDS_FIELD
from impodo.domain.odoo.contracts import ConnectorError, ConnectorIncompleteResultError
from impodo.domain.shared.models import canonical_json_bytes, target_identity_hash

from .request_pacing import (
    REQUEST_WAIT_BUDGET_SECONDS,
    SHARED_JSON2_REQUEST_PACER,
    Json2RequestPacer,
    retry_after_seconds,
)
from .write_diagnostics import (
    import_rejection_diagnostic,
    response_failure_reason,
    uncertain_write,
)

MAX_WRITE_BODY_BYTES = 1024 * 1024
_EXTERNAL_ID = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z0-9_.-]+")
_WRITE_TRANSPORT = partial(_urllib_transport, capture_write_error_type=True)


@dataclass(slots=True)
class Json2WriteExecutor:
    """Native JSON-2 load/create/write adapter with no automatic write retry."""

    config: Json2Config
    scope: OdooApiScope
    transport: Transport = _WRITE_TRANSPORT
    request_pacer: Json2RequestPacer = dataclass_field(default_factory=lambda: SHARED_JSON2_REQUEST_PACER)
    _retry_after: float | None = dataclass_field(default=None, init=False, repr=False)
    _write_admitted: bool = dataclass_field(default=False, init=False, repr=False)
    _operator_message: str = dataclass_field(default="", init=False, repr=False)
    _response_status: int | None = dataclass_field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        """Retain bounded metadata from the production transport."""
        if self.transport is _WRITE_TRANSPORT:
            self.transport = partial(
                _urllib_transport, capture_write_error_type=True,
                observe_retry_after=self._observe_retry_after,
                observe_operator_error=self._observe_operator_error,
                observe_response_status=self._observe_response_status,
            )

    def _observe_retry_after(self, value: str | None) -> None:
        self._retry_after = retry_after_seconds(value, now=datetime.now(UTC))

    def _observe_operator_error(self, message: str) -> None:
        self._operator_message = message

    def _observe_response_status(self, status: int) -> None:
        self._response_status = status

    def _rejection(self, diagnostic: OdooWriteDiagnostic, messages: object, values: Sequence[Mapping[str, Any]]) -> OdooWriteRejected:
        """Keep private capture failure independent of a known Odoo rejection."""
        try:
            detail = capture_operator_error(messages, values, secrets=(self.config.api_key,))
        except (ValueError, TypeError, UnicodeError, RecursionError):
            return OdooWriteRejected(diagnostic.summary, diagnostic=replace(diagnostic, operator_detail_status="CAPTURE_FAILED"))
        status = "WITHHELD" if detail.withheld else "TRUNCATED" if detail.truncated else "AVAILABLE" if detail.messages else "UNAVAILABLE"
        return OdooWriteRejected(diagnostic.summary, diagnostic=replace(diagnostic, operator_detail_status=status), operator_detail=detail)

    def admit_write(self) -> None:
        """Wait before journalling a write; a refused admission sent nothing."""
        if not self.request_pacer.wait(
            self.target_hash,
            deadline=self.request_pacer.clock() + REQUEST_WAIT_BUDGET_SECONDS,
            minimum_interval=self.config.minimum_request_interval_seconds,
        ):
            raise OdooRequestDeferred(
                "Odoo's request cooldown exceeds this waiting window. "
                "The next request was not sent. Check recovery before resuming."
            )
        self._write_admitted = True

    @property
    def target_hash(self) -> str:
        return target_identity_hash(
            connection_mode=self.config.connection_mode,
            base_url=self.config.base_url,
            database=self.config.database,
        )

    @property
    def scope_hash(self) -> str:
        return self.scope.semantic_hash

    def find_ids(
        self,
        model: str,
        domain: Sequence[tuple[str, str, Any]],
    ) -> tuple[int, ...]:
        """Return at most two IDs for one exact service-generated key."""

        if any(field == SOURCE_EXTERNAL_IDS_FIELD for field, _, _ in domain):
            return self.find_ids_many(model, (domain,))[0]

        permitted = self.scope.lookup_fields(model)
        if not permitted:
            raise OdooWriteRejected("Odoo lookup model is outside the reviewed preview")
        normalized = []
        if not domain:
            raise OdooWriteRejected("An exact Odoo business key is required")
        for field, operator, value in domain:
            if field not in permitted or operator != "=":
                raise OdooWriteRejected(
                    "Odoo business-key lookup is outside the reviewed preview"
                )
            normalized.append([field, "=", value])
        response = self._post(
            model,
            "search_read",
            {
                "domain": normalized,
                "fields": ["id"],
                "limit": 2,
                "order": "id asc",
                "context": target_record_read_context(self.config.context),
            },
            write=False,
        )
        if not isinstance(response, list):
            raise OdooWriteRejected("Odoo returned an invalid lookup result")
        try:
            raw_identifiers = tuple(item["id"] for item in response)
        except (KeyError, TypeError, ValueError) as error:
            raise OdooWriteRejected("Odoo returned an invalid lookup result") from error
        if any(type(item) is not int for item in raw_identifiers):
            raise OdooWriteRejected("Odoo returned an invalid lookup result")
        identifiers = tuple(raw_identifiers)
        if any(identifier <= 0 for identifier in identifiers):
            raise OdooWriteRejected("Odoo returned an invalid lookup result")
        return identifiers

    def find_ids_many(
        self,
        model: str,
        domains: Sequence[Sequence[tuple[str, str, Any]]],
    ) -> tuple[tuple[int, ...], ...]:
        """Resolve a bounded page of exact keys with one read request."""

        permitted = self.scope.lookup_fields(model)
        domain_page = tuple(tuple(domain) for domain in domains)
        if not permitted:
            raise OdooWriteRejected("Odoo lookup model is outside the reviewed preview")
        if not domain_page or len(domain_page) > MAX_IDENTITY_LOOKUP_KEYS:
            raise OdooWriteRejected(
                "Odoo business-key lookup page is outside the safe bound"
            )
        if any(field == SOURCE_EXTERNAL_IDS_FIELD for domain in domain_page for field, _, _ in domain):
            if SOURCE_EXTERNAL_IDS_FIELD not in permitted:
                raise OdooWriteRejected("Source record identity is outside the reviewed preview")
            try:
                keys = external_lookup_values(domain_page, SOURCE_EXTERNAL_IDS_FIELD)
                bindings = external_id_bindings(model, keys, self._post(
                    "ir.model.data", "search_read", {
                        "domain": external_id_domain(keys),
                        "fields": ["module", "name", "model", "res_id"],
                        "limit": len(set(keys)) + 1, "order": "id asc",
                        "context": target_record_read_context(self.config.context),
                    }, write=False, identity_model=model,
                ))
            except ConnectorError as error:
                raise OdooWriteRejected(str(error)) from error
            if bindings:
                records = self._post(model, "search_read", {
                    "domain": [["id", "in", sorted(bindings)]], "fields": ["id"],
                    "limit": len(bindings) + 1, "order": "id asc",
                    "context": target_record_read_context(self.config.context),
                }, write=False)
                if not isinstance(records, list) or {row.get("id") for row in records if isinstance(row, Mapping)} != set(bindings):
                    raise OdooWriteRejected("A source record External ID points to a missing or inaccessible record")
            by_key = {key: identifier for identifier, aliases in bindings.items() for key in aliases}
            return tuple((by_key[key],) if key in by_key else () for key in keys)
        normalized: list[tuple[tuple[str, str, Any], ...]] = []
        requested_fields: set[str] = set()
        for domain in domain_page:
            if not domain:
                raise OdooWriteRejected("An exact Odoo business key is required")
            conditions = []
            for field, operator, value in domain:
                if field not in permitted or operator != "=":
                    raise OdooWriteRejected(
                        "Odoo business-key lookup is outside the reviewed preview"
                    )
                requested_fields.add(field)
                conditions.append((field, "=", value))
            normalized.append(tuple(conditions))

        query_domain, candidate_limit = bounded_lookup_query(model, normalized)
        fields = tuple(sorted(requested_fields))
        response = self._post(
            model,
            "search_read",
            {
                "domain": query_domain,
                "fields": ["id", *fields],
                "limit": candidate_limit + 1,
                "order": "id asc",
                "context": target_record_read_context(self.config.context),
            },
            write=False,
        )
        if not isinstance(response, list) or len(response) > candidate_limit:
            raise OdooWriteRejected("Odoo returned an invalid bulk lookup result")
        results: list[list[int]] = [[] for _domain in normalized]
        for item in response:
            if not isinstance(item, Mapping):
                raise OdooWriteRejected("Odoo returned an invalid bulk lookup result")
            identifier = item.get("id")
            if type(identifier) is not int or identifier <= 0:
                raise OdooWriteRejected("Odoo returned an invalid bulk lookup result")
            for index, domain in enumerate(normalized):
                if all(
                    lookup_values_equal(item.get(field), value)
                    for field, _operator, value in domain
                ):
                    results[index].append(identifier)
        return tuple(tuple(dict.fromkeys(items)) for items in results)

    def create_rows(
        self,
        model: str,
        values: Sequence[Mapping[str, Any]],
    ) -> tuple[int, ...]:
        """Create one bounded multi-create transaction and return exact IDs."""

        rows = tuple(dict(item) for item in values)
        if not rows or len(rows) > MAX_CREATE_BATCH_ROWS:
            raise OdooWriteRejected("Odoo create batch is outside the safe bound")
        for row in rows:
            self._validate_values(model, row)
        response = self._post(
            model,
            "create",
            {
                "vals_list": list(rows),
                "context": dict(self.config.context),
            },
            write=True,
            batch_size=len(rows),
        )
        if isinstance(response, int) and not isinstance(response, bool):
            identifiers = (response,)
        elif isinstance(response, list) and all(type(item) is int for item in response):
            identifiers = tuple(response)
        else:
            raise uncertain_write(
                model, "create", len(rows), UncertainWriteReason.INVALID_RECEIPT, http_status=200,
            )
        if len(identifiers) != len(rows):
            raise uncertain_write(
                model, "create", len(rows), UncertainWriteReason.RECEIPT_COUNT_MISMATCH,
                http_status=200, returned_id_count=len(identifiers),
            )
        if any(item <= 0 for item in identifiers):
            raise uncertain_write(
                model, "create", len(rows), UncertainWriteReason.INVALID_RECEIPT, http_status=200,
            )
        return identifiers

    def load_create_rows(
        self,
        model: str,
        values: Sequence[Mapping[str, Any]],
        external_ids: Sequence[str],
    ) -> tuple[int, ...]:
        """Import one scalar/many2one create batch with stable External IDs."""

        rows = tuple(dict(item) for item in values)
        xml_ids = tuple(str(item) for item in external_ids)
        if not rows or len(rows) > MAX_CREATE_BATCH_ROWS or len(xml_ids) != len(rows):
            raise OdooWriteRejected("Odoo import batch is outside the safe bound")
        if any(
            not item or len(item) > 255 or _EXTERNAL_ID.fullmatch(item) is None
            for item in xml_ids
        ) or len(set(xml_ids)) != len(xml_ids):
            raise OdooWriteRejected("Odoo External IDs are invalid or duplicated")
        for row in rows:
            self._validate_import_values(model, row)
        field_names = tuple(sorted(rows[0]))
        if not field_names or any(tuple(sorted(row)) != field_names for row in rows):
            raise OdooWriteRejected(
                "Odoo import rows must share the same reviewed field set"
            )
        response = self._post(
            model,
            "load",
            {
                "fields": ["id", *field_names],
                "data": [
                    [xml_id, *(row[field] for field in field_names)]
                    for xml_id, row in zip(xml_ids, rows, strict=True)
                ],
                "context": {
                    **dict(self.config.context),
                    "import_file": True,
                },
            },
            write=True,
            batch_size=len(rows),
        )
        if not isinstance(response, Mapping):
            raise uncertain_write(
                model, "load", len(rows), UncertainWriteReason.INVALID_RECEIPT, http_status=200,
            )
        identifiers = response.get("ids")
        messages = response.get("messages", [])
        if identifiers is False:
            diagnostic = import_rejection_diagnostic(
                messages, model=model, fields=field_names, batch_size=len(rows)
            )
            raise self._rejection(diagnostic, messages, rows)
        if (
            not isinstance(identifiers, list)
            or any(type(item) is not int or item <= 0 for item in identifiers)
            or not isinstance(messages, list)
        ):
            raise uncertain_write(
                model, "load", len(rows), UncertainWriteReason.INVALID_RECEIPT, http_status=200,
            )
        if any(
            isinstance(message, Mapping) and message.get("type") == "error"
            for message in messages
        ):
            raise uncertain_write(
                model, "load", len(rows), UncertainWriteReason.RECEIPT_WITH_ERRORS, http_status=200,
            )
        if len(identifiers) != len(rows):
            raise uncertain_write(
                model, "load", len(rows), UncertainWriteReason.RECEIPT_COUNT_MISMATCH,
                http_status=200, returned_id_count=len(identifiers),
            )
        return tuple(identifiers)

    def update_row(
        self,
        model: str,
        record_id: int,
        values: Mapping[str, Any],
    ) -> None:
        """Update one uniquely re-matched record without retrying the write."""

        self.update_rows(model, (record_id,), values)

    def update_rows(
        self,
        model: str,
        record_ids: Sequence[int],
        values: Mapping[str, Any],
    ) -> None:
        """Update a bounded set of exact IDs sharing one reviewed payload."""

        identifiers = tuple(record_ids)
        if (
            not identifiers
            or len(identifiers) > MAX_CREATE_BATCH_ROWS
            or len(set(identifiers)) != len(identifiers)
            or any(type(item) is not int or item <= 0 for item in identifiers)
        ):
            raise OdooWriteRejected("Odoo update identities are invalid")
        payload = dict(values)
        self._validate_values(model, payload)
        response = self._post(
            model,
            "write",
            {
                "ids": list(identifiers),
                "vals": payload,
                "context": dict(self.config.context),
            },
            write=True,
            batch_size=len(identifiers),
        )
        if response is not True:
            raise uncertain_write(
                model, "write", len(identifiers), UncertainWriteReason.INVALID_RECEIPT, http_status=200,
            )

    def read_projected_ids(
        self,
        model: str,
        identifiers: Sequence[int],
        projection_field: str,
        target_model: str,
    ) -> tuple[int, ...]:
        """Read one reviewed generated many-to-one receipt for exact IDs."""

        requested = tuple(identifiers)
        if (
            not requested
            or len(requested) > MAX_PROJECTED_RECEIPT_IDS
            or len(set(requested)) != len(requested)
            or any(type(item) is not int or item <= 0 for item in requested)
            or projection_field not in self.scope.read_fields(model)
            or self.scope.model(target_model) is None
        ):
            raise OdooWriteRejected(
                "Odoo generated-record read-back is outside the reviewed preview"
            )
        response = self._post(
            model,
            "search_read",
            {
                "domain": [["id", "in", list(requested)]],
                "fields": ["id", projection_field],
                "limit": len(requested),
                "order": "id asc",
                "context": target_record_read_context(self.config.context),
            },
            write=False,
        )
        if not isinstance(response, list) or len(response) != len(requested):
            raise OdooWriteRejected(
                "Odoo generated-record read-back did not cover every created record"
            )
        projected_by_id: dict[int, int] = {}
        for item in response:
            if not isinstance(item, Mapping):
                raise OdooWriteRejected(
                    "Odoo returned an invalid generated-record receipt"
                )
            source_id = item.get("id")
            raw_target = item.get(projection_field)
            target_id = (
                raw_target[0]
                if isinstance(raw_target, (list, tuple)) and raw_target
                else raw_target
            )
            if (
                type(source_id) is not int
                or source_id not in requested
                or source_id in projected_by_id
                or type(target_id) is not int
                or target_id <= 0
            ):
                raise OdooWriteRejected(
                    "Odoo returned an invalid generated-record receipt"
                )
            projected_by_id[source_id] = target_id
        if set(projected_by_id) != set(requested):
            raise OdooWriteRejected(
                "Odoo generated-record read-back changed the exact source IDs"
            )
        return tuple(projected_by_id[item] for item in requested)

    def _validate_values(self, model: str, values: Mapping[str, Any]) -> None:
        permitted = self.scope.write_fields(model)
        if not values or not set(values).issubset(permitted):
            raise OdooWriteRejected(
                "Odoo model or field is outside the reviewed load preview"
            )

    def _validate_import_values(
        self,
        model: str,
        values: Mapping[str, Any],
    ) -> None:
        """Allow reviewed scalar fields and exact many2one identity paths."""

        permitted = self.scope.write_fields(model)
        if not values:
            raise OdooWriteRejected(
                "Odoo model or field is outside the reviewed load preview"
            )
        for field, value in values.items():
            if not isinstance(value, str):
                raise OdooWriteRejected(
                    "Odoo import values must use the reviewed text format"
                )
            if field.endswith("/.id"):
                base_field = field.removesuffix("/.id")
                if not base_field or "/" in base_field:
                    raise OdooWriteRejected(
                        "Odoo import relationship path is outside the reviewed preview"
                    )
            elif field.endswith("/id"):
                base_field = field.removesuffix("/id")
                if not base_field or "/" in base_field:
                    raise OdooWriteRejected(
                        "Odoo import relationship path is outside the reviewed preview"
                    )
            elif "/" in field:
                raise OdooWriteRejected(
                    "Odoo import relationship path is outside the reviewed preview"
                )
            else:
                base_field = field
            if base_field not in permitted:
                raise OdooWriteRejected(
                    "Odoo model or field is outside the reviewed load preview"
                )

    def _post(
        self,
        model: str,
        method: str,
        payload: Mapping[str, Any],
        *,
        write: bool,
        batch_size: int = 1,
        identity_model: str | None = None,
    ) -> object:
        permitted_methods = {"create", "load", "write"} if write else {"search_read"}
        permitted_fields = (
            self.scope.write_fields(model) if write else self.scope.lookup_fields(model)
        )
        if not write and model == "ir.model.data" and identity_model is not None and SOURCE_EXTERNAL_IDS_FIELD in self.scope.lookup_fields(identity_model):
            permitted_fields = frozenset({"module", "name", "model", "res_id"})
        if method not in permitted_methods or not permitted_fields:
            raise OdooWriteRejected(
                "Odoo operation is outside the reviewed load preview"
            )
        body = canonical_json_bytes(dict(payload))
        if len(body) > MAX_WRITE_BODY_BYTES:
            raise OdooWriteRejected("Odoo request is outside the safe size bound")
        url = (
            f"{self.config.base_url}/json/2/"
            f"{quote(model, safe='.')}/{quote(method, safe='')}"
        )
        headers = {
            "Authorization": f"bearer {self.config.api_key}",
            "Content-Type": "application/json; charset=utf-8",
            "X-Odoo-Database": self.config.database,
            "User-Agent": "impodo",
        }
        # A search_read is safe to repeat after a lost response. A write is
        # not: Odoo might have committed it before the connection failed.
        attempts = 1 if write else max(1, self.config.retries + 1)
        status: int | None = None
        response: object = None
        deadline = self.request_pacer.clock() + REQUEST_WAIT_BUDGET_SECONDS
        for attempt in range(attempts):
            if write:
                if not self._write_admitted:
                    self.admit_write()
                self._write_admitted = False
            elif not self.request_pacer.wait(
                self.target_hash, deadline=deadline,
                minimum_interval=self.config.minimum_request_interval_seconds,
            ):
                raise OdooRequestDeferred(
                    "Odoo's request cooldown exceeds this waiting window. "
                    "The next lookup was not sent. Check recovery before resuming."
                )
            try:
                self._retry_after = None
                self._operator_message = ""
                self._response_status = None
                status, response = self.transport(
                    url,
                    headers,
                    body,
                    self.config.timeout_seconds,
                    "POST",
                )
            except (HTTPException, OSError, ValueError, ConnectorIncompleteResultError, RecursionError) as error:
                if write:
                    raise uncertain_write(
                        model, method, batch_size, response_failure_reason(error),
                        http_status=self._response_status,
                    ) from error
                if isinstance(error, (ConnectorIncompleteResultError, RecursionError)):
                    raise
                if attempt + 1 == attempts:
                    reason = error.reason if isinstance(error, URLError) else error
                    if isinstance(reason, TimeoutError):
                        message = "Odoo timed out while checking existing records"
                    elif isinstance(reason, HTTPException | ConnectionError):
                        message = "The connection to Odoo was interrupted while checking existing records"
                    elif isinstance(reason, ValueError):
                        message = "Odoo returned an unreadable response while checking existing records"
                    else:
                        message = "Odoo could not be reached while checking existing records"
                    label = "attempt" if attempts == 1 else "attempts"
                    raise OdooIdentityLookupUnavailable(
                        f"{message} after {attempts} {label}."
                    ) from error
                time.sleep(2**attempt)
                continue
            if status == 429:
                self.request_pacer.defer(
                    self.target_hash, attempt=attempt, retry_after=self._retry_after,
                )
                if not write:
                    if attempt + 1 < attempts:
                        continue
                    raise OdooRequestDeferred(
                        "Odoo is limiting lookup requests. No write was sent for "
                        "this lookup. Check recovery after the cooldown."
                    )
            if (
                not write
                and status in {408, 425, 500, 502, 503, 504}
                and attempt + 1 < attempts
            ):
                time.sleep(2**attempt)
                continue
            break
        if status is None:
            raise OdooWriteRejected("Odoo did not return a response")
        if status == 200:
            return response
        if write:
            unknown = status >= 500 or status in {408, 425, 429}
            error_type = response if isinstance(response, OdooWriteErrorType) else None
            if unknown:
                code = WriteFailureCode.UNKNOWN
            elif status in {401, 403}:
                code = WriteFailureCode.ACCESS_DENIED
            elif status in {400, 422} and error_type is OdooWriteErrorType.VALIDATION:
                code = WriteFailureCode.VALIDATION
            else:
                code = WriteFailureCode.REJECTED
            diagnostic = OdooWriteDiagnostic(
                code, model, method, batch_size=batch_size,
                http_status=status if 100 <= status <= 599 else None,
                error_type=error_type,
            )
            if unknown:
                raise OdooWriteOutcomeUnknown(diagnostic.summary, diagnostic=diagnostic)
            if self._operator_message:
                if method == "load":
                    names, data = payload.get("fields", []), payload.get("data", [])
                    raw_values = tuple({str(k): v for k, v in zip(names, row, strict=True) if k != "id"} for row in data) if isinstance(names, list) and isinstance(data, list) else ({},)
                else:
                    raw_values = payload.get("vals_list") if method == "create" else (payload.get("vals", {}),) * batch_size
                values = tuple(row for row in raw_values if isinstance(row, Mapping)) if isinstance(raw_values, (list, tuple)) else ({},)
                raise self._rejection(diagnostic, [{"type": "error", "message": self._operator_message}], values or ({},))
            raise OdooWriteRejected(diagnostic.summary, diagnostic=diagnostic)
        if status in {401, 403}:
            raise OdooWriteRejected("Odoo did not authorize this load")
        raise OdooWriteRejected(f"Odoo rejected the load request (HTTP {status})")
