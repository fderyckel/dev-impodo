"""Closed Odoo 19 JSON-2 write adapter for the practical master-data path.

This module is deliberately separate from the read connector.  Callers can
only resolve an exact business key, create bounded rows, or update one known
record for the initial contact/category/product model and field allowlists.
There is no caller-controlled method name, context, delete, SQL, ``sudo``, or
generic RPC surface.
"""

from __future__ import annotations

from dataclasses import dataclass
import socket
from typing import Any, Mapping, Protocol, Sequence
from urllib.error import URLError
from urllib.parse import quote

from .connectors import Json2Config, Transport, _urllib_transport
from .models import canonical_json_bytes, target_identity_hash


MAX_CREATE_BATCH_ROWS = 50
MAX_WRITE_BODY_BYTES = 1024 * 1024

PRACTICAL_WRITE_FIELDS: Mapping[str, frozenset[str]] = {
    "res.partner": frozenset(
        {
            "active",
            "category_id",
            "city",
            "company_id",
            "company_type",
            "country_id",
            "email",
            "is_company",
            "mobile",
            "name",
            "parent_id",
            "phone",
            "ref",
            "state_id",
            "street",
            "street2",
            "vat",
            "zip",
        }
    ),
    "product.category": frozenset(
        {
            "name",
            "parent_id",
            "property_cost_method",
            "property_valuation",
        }
    ),
    "product.template": frozenset(
        {
            "active",
            "barcode",
            "categ_id",
            "company_id",
            "default_code",
            "list_price",
            "name",
            "purchase_ok",
            "sale_ok",
            "standard_price",
            "supplier_taxes_id",
            "taxes_id",
            "type",
            "uom_id",
            "uom_po_id",
        }
    ),
    "product.product": frozenset(
        {
            "active",
            "barcode",
            "categ_id",
            "company_id",
            "default_code",
            "lst_price",
            "name",
            "product_tmpl_id",
            "standard_price",
            "uom_id",
            "uom_po_id",
        }
    ),
}

PRACTICAL_LOOKUP_FIELDS: Mapping[str, frozenset[str]] = {
    **PRACTICAL_WRITE_FIELDS,
    "res.company": frozenset({"name"}),
    "res.country": frozenset({"code", "name"}),
    "res.country.state": frozenset({"code", "country_id", "name"}),
    "uom.uom": frozenset({"name"}),
}


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
    transport: Transport = _urllib_transport

    @property
    def target_hash(self) -> str:
        return target_identity_hash(
            connection_mode=self.config.connection_mode,
            base_url=self.config.base_url,
            database=self.config.database,
        )

    def find_ids(
        self,
        model: str,
        domain: Sequence[tuple[str, str, Any]],
    ) -> tuple[int, ...]:
        """Return at most two IDs for one exact service-generated key."""

        permitted = PRACTICAL_LOOKUP_FIELDS.get(model)
        if permitted is None:
            raise OdooWriteRejected("Odoo lookup model is outside the load scope")
        normalized = []
        if not domain:
            raise OdooWriteRejected("An exact Odoo business key is required")
        for field, operator, value in domain:
            if field not in permitted or operator != "=":
                raise OdooWriteRejected("Odoo business-key lookup is not allowed")
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
        permitted = self._permitted_fields(model)
        if not values or not set(values).issubset(permitted):
            raise OdooWriteRejected("Odoo model or field is outside the load scope")

    @staticmethod
    def _permitted_fields(model: str) -> frozenset[str]:
        permitted = PRACTICAL_WRITE_FIELDS.get(model)
        if permitted is None:
            raise OdooWriteRejected("Odoo model is outside the practical load scope")
        return permitted

    def _post(
        self,
        model: str,
        method: str,
        payload: Mapping[str, Any],
        *,
        write: bool,
    ) -> Any:
        permitted_methods = {"create", "write"} if write else {"search_read"}
        permitted_models = (
            PRACTICAL_WRITE_FIELDS if write else PRACTICAL_LOOKUP_FIELDS
        )
        if method not in permitted_methods or model not in permitted_models:
            raise OdooWriteRejected("Odoo operation is outside the load scope")
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
