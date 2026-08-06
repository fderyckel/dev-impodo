"""Closed Odoo 19 JSON-2 writer for one reviewed execution preview.

This module is deliberately separate from the read connector.  Callers can
only resolve an exact business key, create bounded rows, or update one known
record within a per-preview capability derived from captured schema and the
confirmed mapping.  There is no global model/field allowlist and no
caller-controlled method name, context, delete, SQL, ``sudo``, or generic RPC
surface.
"""

from __future__ import annotations

from dataclasses import dataclass
import socket
from typing import Any, Mapping, Protocol, Sequence
from urllib.error import URLError
from urllib.parse import quote

from .connectors import Json2Config, Transport, _urllib_transport
from .models import canonical_json_bytes, target_identity_hash
from .odoo_scope import OdooApiScope


MAX_CREATE_BATCH_ROWS = 50
MAX_WRITE_BODY_BYTES = 1024 * 1024


class OdooWriteError(RuntimeError):
    """Base class for safe Stage-J writer failures."""


class OdooWriteRejected(OdooWriteError):
    """Odoo definitively rejected a request, so it did not commit."""


class OdooWriteOutcomeUnknown(OdooWriteError):
    """The connection failed after send and commit cannot be inferred."""


class OdooWriteExecutor(Protocol):
    """Small application-facing surface implemented by the native adapter."""

    @property
    def target_hash(self) -> str: ...

    @property
    def scope_hash(self) -> str: ...

    def find_ids(
        self,
        model: str,
        domain: Sequence[tuple[str, str, Any]],
    ) -> tuple[int, ...]: ...

    def create_rows(
        self,
        model: str,
        values: Sequence[Mapping[str, Any]],
    ) -> tuple[int, ...]: ...

    def update_row(
        self,
        model: str,
        record_id: int,
        values: Mapping[str, Any],
    ) -> None: ...


@dataclass(slots=True)
class Json2WriteExecutor:
    """Native JSON-2 create/write adapter with no automatic write retry."""

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
            raise OdooWriteRejected(
                "Odoo lookup model is outside the reviewed preview"
            )
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
                raise OdooWriteOutcomeUnknown(
                    "Odoo create returned an invalid receipt"
                )
            identifiers = tuple(response)
        else:
            raise OdooWriteOutcomeUnknown("Odoo create returned an invalid receipt")
        if len(identifiers) != len(rows) or any(item <= 0 for item in identifiers):
            raise OdooWriteOutcomeUnknown("Odoo create returned an invalid receipt")
        return identifiers

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

    def _validate_values(self, model: str, values: Mapping[str, Any]) -> None:
        permitted = self.scope.write_fields(model)
        if not values or not set(values).issubset(permitted):
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
        permitted_methods = {"create", "write"} if write else {"search_read"}
        permitted_fields = (
            self.scope.write_fields(model)
            if write
            else self.scope.lookup_fields(model)
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
            raise OdooWriteRejected("Odoo could not be reached for identity lookup") from error
        if status == 200:
            return response
        if status in {401, 403}:
            raise OdooWriteRejected("Odoo did not authorize this load")
        if write and status >= 500:
            raise OdooWriteOutcomeUnknown(
                "Odoo returned a server error; the outcome is unknown"
            )
        raise OdooWriteRejected(f"Odoo rejected the load request (HTTP {status})")
