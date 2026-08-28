"""Closed Odoo 19 JSON-2 writer for one reviewed execution preview.

This module is deliberately separate from the read connector.  Callers can
only resolve an exact business key, import bounded creates with reviewed
external IDs, create bounded rows for the disposable-local path, or update one
known record within a per-preview capability derived from captured schema and
the confirmed mapping.  There is no global model/field allowlist and no
caller-controlled method name, context, delete, SQL, ``sudo``, or generic RPC
surface.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
import socket
from typing import Any, Mapping, Sequence
from urllib.error import URLError
from urllib.parse import quote

from impodo.adapters.odoo.connectors import Json2Config, Transport, _urllib_transport
from impodo.domain.execution.models import MAX_CREATE_BATCH_ROWS
from impodo.domain.execution.odoo_write import (
    MAX_IDENTITY_LOOKUP_KEYS,
    MAX_PROJECTED_RECEIPT_IDS,
    OdooWriteOutcomeUnknown,
    OdooWriteRejected,
)
from impodo.domain.shared.models import canonical_json_bytes, target_identity_hash
from impodo.domain.execution.odoo_scope import OdooApiScope


MAX_WRITE_BODY_BYTES = 1024 * 1024
_EXTERNAL_ID = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z0-9_.-]+")
@dataclass(slots=True)
class Json2WriteExecutor:
    """Native JSON-2 load/create/write adapter with no automatic write retry."""

    config: Json2Config
    scope: OdooApiScope
    transport: Transport = _urllib_transport

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
                "context": dict(self.config.context),
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
            raise OdooWriteRejected("Odoo business-key lookup page is outside the safe bound")
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

        query_domain: list[Any] = ["|"] * (len(normalized) - 1)
        for domain in normalized:
            query_domain.extend(["&"] * (len(domain) - 1))
            query_domain.extend([list(condition) for condition in domain])
        fields = tuple(sorted(requested_fields))
        response = self._post(
            model,
            "search_read",
            {
                "domain": query_domain,
                "fields": ["id", *fields],
                "limit": (2 * len(normalized)) + 1,
                "order": "id asc",
                "context": dict(self.config.context),
            },
            write=False,
        )
        if not isinstance(response, list) or len(response) > 2 * len(normalized):
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
                    _lookup_values_equal(item.get(field), value)
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
        )
        if isinstance(response, int) and not isinstance(response, bool):
            identifiers = (response,)
        elif isinstance(response, list):
            if any(type(item) is not int for item in response):
                raise OdooWriteOutcomeUnknown("Odoo create returned an invalid receipt")
            identifiers = tuple(response)
        else:
            raise OdooWriteOutcomeUnknown("Odoo create returned an invalid receipt")
        if len(identifiers) != len(rows) or any(item <= 0 for item in identifiers):
            raise OdooWriteOutcomeUnknown("Odoo create returned an invalid receipt")
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
        )
        if not isinstance(response, Mapping):
            raise OdooWriteOutcomeUnknown("Odoo import returned an invalid receipt")
        identifiers = response.get("ids")
        messages = response.get("messages", [])
        if identifiers is False:
            raise OdooWriteRejected(_safe_import_error(messages))
        if (
            not isinstance(identifiers, list)
            or any(type(item) is not int or item <= 0 for item in identifiers)
            or len(identifiers) != len(rows)
            or not isinstance(messages, list)
        ):
            raise OdooWriteOutcomeUnknown("Odoo import returned an invalid receipt")
        if any(
            isinstance(message, Mapping) and message.get("type") == "error"
            for message in messages
        ):
            raise OdooWriteOutcomeUnknown("Odoo import returned an invalid receipt")
        return tuple(identifiers)

    def update_row(
        self,
        model: str,
        record_id: int,
        values: Mapping[str, Any],
    ) -> None:
        """Update one uniquely re-matched record without retrying the write."""

        if type(record_id) is not int or record_id <= 0:
            raise OdooWriteRejected("Odoo update identity is invalid")
        payload = dict(values)
        self._validate_values(model, payload)
        response = self._post(
            model,
            "write",
            {
                "ids": [record_id],
                "vals": payload,
                "context": dict(self.config.context),
            },
            write=True,
        )
        if response is not True:
            raise OdooWriteOutcomeUnknown("Odoo update returned an invalid receipt")

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
                "context": dict(self.config.context),
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
    ) -> Any:
        permitted_methods = {"create", "load", "write"} if write else {"search_read"}
        permitted_fields = (
            self.scope.write_fields(model) if write else self.scope.lookup_fields(model)
        )
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
        try:
            status, response = self.transport(
                url,
                headers,
                body,
                self.config.timeout_seconds,
                "POST",
            )
        except (TimeoutError, socket.timeout, URLError, ValueError) as error:
            if write:
                raise OdooWriteOutcomeUnknown(
                    "The Odoo response was lost; the outcome is unknown"
                ) from error
            raise OdooWriteRejected(
                "Odoo could not be reached for identity lookup"
            ) from error
        if status == 200:
            return response
        if status in {401, 403}:
            raise OdooWriteRejected("Odoo did not authorize this load")
        if write and (status >= 500 or status in {408, 425, 429}):
            raise OdooWriteOutcomeUnknown(
                "Odoo did not return a definitive write result; the outcome is unknown"
            )
        raise OdooWriteRejected(f"Odoo rejected the load request (HTTP {status})")


def _safe_import_error(_messages: object) -> str:
    """Return a fixed error because import details can contain secrets or data."""

    return "Odoo rejected one or more imported rows"


def _lookup_values_equal(actual: object, expected: object) -> bool:
    """Compare JSON-2 search values with exact generated domain scalars."""

    if expected is None and actual is False:
        return True
    return actual == expected
