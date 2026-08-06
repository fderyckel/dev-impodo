"""Closed Odoo JSON-2 reader for practical post-write verification."""

from __future__ import annotations

from dataclasses import dataclass
import socket
from typing import Any, Mapping, Protocol, Sequence
from urllib.error import URLError
from urllib.parse import quote

from .connectors import Json2Config, Transport, _urllib_transport
from .models import canonical_json_bytes, target_identity_hash
from .odoo_writer import PRACTICAL_LOOKUP_FIELDS


MAX_READBACK_IDS = 50
MAX_READBACK_BODY_BYTES = 1024 * 1024


class OdooReadbackError(RuntimeError):
    """A protected read-back could not produce trustworthy records."""


@dataclass(frozen=True, slots=True)
class ReadbackRecord:
    odoo_id: int
    values: Mapping[str, Any]


class OdooReadbackReader(Protocol):
    @property
    def target_hash(self) -> str: ...

    def read_ids(
        self,
        model: str,
        identifiers: Sequence[int],
        fields: Sequence[str],
    ) -> tuple[ReadbackRecord, ...]: ...

    def find_records(
        self,
        model: str,
        domain: Sequence[tuple[str, str, Any]],
        fields: Sequence[str],
    ) -> tuple[ReadbackRecord, ...]: ...


@dataclass(slots=True)
class Json2ReadbackReader:
    """Read only exact IDs or service-generated business keys."""

    config: Json2Config
    transport: Transport = _urllib_transport

    @property
    def target_hash(self) -> str:
        return target_identity_hash(
            connection_mode=self.config.connection_mode,
            base_url=self.config.base_url,
            database=self.config.database,
        )

    def read_ids(
        self,
        model: str,
        identifiers: Sequence[int],
        fields: Sequence[str],
    ) -> tuple[ReadbackRecord, ...]:
        ids = tuple(dict.fromkeys(identifiers))
        if (
            not ids
            or len(ids) > MAX_READBACK_IDS
            or any(type(item) is not int or item <= 0 for item in ids)
        ):
            raise OdooReadbackError("Odoo read-back IDs are outside the safe bound")
        clean_fields = self._fields(model, fields)
        return self._search(
            model,
            [["id", "in", list(ids)]],
            clean_fields,
            limit=len(ids),
            permitted_ids=frozenset(ids),
        )

    def find_records(
        self,
        model: str,
        domain: Sequence[tuple[str, str, Any]],
        fields: Sequence[str],
    ) -> tuple[ReadbackRecord, ...]:
        permitted = PRACTICAL_LOOKUP_FIELDS.get(model)
        if permitted is None or not domain:
            raise OdooReadbackError("An exact Odoo read-back key is required")
        normalized = []
        for field, operator, value in domain:
            if field not in permitted or operator != "=":
                raise OdooReadbackError("Odoo read-back key is outside the load scope")
            normalized.append([field, "=", value])
        return self._search(
            model,
            normalized,
            self._fields(model, fields),
            limit=2,
            permitted_ids=None,
        )

    @staticmethod
    def _fields(model: str, fields: Sequence[str]) -> tuple[str, ...]:
        permitted = PRACTICAL_LOOKUP_FIELDS.get(model)
        clean = tuple(dict.fromkeys(fields))
        if permitted is None or not set(clean).issubset(permitted):
            raise OdooReadbackError("Odoo read-back fields are outside the load scope")
        return clean

    def _search(
        self,
        model: str,
        domain: Sequence[Any],
        fields: tuple[str, ...],
        *,
        limit: int,
        permitted_ids: frozenset[int] | None,
    ) -> tuple[ReadbackRecord, ...]:
        payload = {
            "domain": list(domain),
            "fields": ["id", *fields],
            "limit": limit,
            "order": "id asc",
            "context": dict(self.config.context),
        }
        body = canonical_json_bytes(payload)
        if len(body) > MAX_READBACK_BODY_BYTES:
            raise OdooReadbackError("Odoo read-back request is outside the safe bound")
        url = f"{self.config.base_url}/json/2/{quote(model, safe='.')}/search_read"
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
            raise OdooReadbackError("Odoo could not be reached for verification") from error
        if status in {401, 403}:
            raise OdooReadbackError("Odoo did not authorize verification")
        if status != 200 or not isinstance(response, list):
            raise OdooReadbackError("Odoo verification returned an invalid result")
        records = []
        seen: set[int] = set()
        for item in response:
            if not isinstance(item, dict) or type(item.get("id")) is not int:
                raise OdooReadbackError("Odoo verification returned an invalid record")
            identifier = int(item["id"])
            if (
                identifier <= 0
                or identifier in seen
                or (permitted_ids is not None and identifier not in permitted_ids)
                or any(field not in item for field in fields)
            ):
                raise OdooReadbackError("Odoo verification returned inconsistent records")
            seen.add(identifier)
            records.append(
                ReadbackRecord(
                    odoo_id=identifier,
                    values={field: item[field] for field in fields},
                )
            )
        return tuple(records)
