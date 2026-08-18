"""Closed Odoo JSON-2 reader for reviewed post-write verification."""

from __future__ import annotations

from dataclasses import dataclass
import re
import socket
from typing import Any, Mapping, Protocol, Sequence
from urllib.error import URLError
from urllib.parse import quote

from .connectors import (
    Json2Config,
    Transport,
    _urllib_transport,
    target_record_read_context,
)
from .models import canonical_json_bytes, target_identity_hash
from .odoo_scope import OdooApiScope


MAX_READBACK_IDS = 50
MAX_READBACK_LOOKUPS = 20
MAX_READBACK_BODY_BYTES = 1024 * 1024
_EXTERNAL_ID = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z0-9_.-]+")


class OdooReadbackError(RuntimeError):
    """A protected read-back could not produce trustworthy records."""


@dataclass(frozen=True, slots=True)
class ReadbackRecord:
    odoo_id: int
    values: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class ExternalIdBinding:
    external_id: str
    model: str
    odoo_id: int


@dataclass(frozen=True, slots=True)
class ReadbackLookup:
    """One exact reviewed business-key lookup in a bounded batch."""

    domain: tuple[tuple[str, str, Any], ...]
    fields: tuple[str, ...] = ()


class OdooReadbackReader(Protocol):
    @property
    def target_hash(self) -> str: ...

    @property
    def scope_hash(self) -> str: ...

    @property
    def imports_external_ids(self) -> bool: ...

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

    def find_records_many(
        self,
        model: str,
        lookups: Sequence[ReadbackLookup],
    ) -> tuple[tuple[ReadbackRecord, ...], ...]: ...

    def read_external_ids(
        self,
        external_ids: Sequence[str],
    ) -> tuple[ExternalIdBinding, ...]: ...


@dataclass(slots=True)
class Json2ReadbackReader:
    """Read only exact IDs, XML IDs, or service-generated business keys."""

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

    @property
    def imports_external_ids(self) -> bool:
        return self.config.connection_mode == "REMOTE"

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
        return self.find_records_many(
            model,
            (
                ReadbackLookup(
                    domain=tuple(domain),
                    fields=tuple(fields),
                ),
            ),
        )[0]

    def find_records_many(
        self,
        model: str,
        lookups: Sequence[ReadbackLookup],
    ) -> tuple[tuple[ReadbackRecord, ...], ...]:
        """Resolve up to twenty exact keys with one bounded Odoo request."""

        requested = tuple(lookups)
        if not requested or len(requested) > MAX_READBACK_LOOKUPS:
            raise OdooReadbackError(
                "Odoo read-back keys are outside the safe batch bound"
            )
        permitted_lookup_fields = self.scope.lookup_fields(model)
        if not permitted_lookup_fields:
            raise OdooReadbackError("An exact Odoo read-back key is required")

        normalized_domains = []
        requested_fields = []
        query_fields: set[str] = set()
        for lookup in requested:
            if not lookup.domain:
                raise OdooReadbackError("An exact Odoo read-back key is required")
            normalized = []
            for field, operator, value in lookup.domain:
                if field not in permitted_lookup_fields or operator != "=":
                    raise OdooReadbackError(
                        "Odoo read-back key is outside the reviewed load preview"
                    )
                normalized.append((field, operator, value))
                query_fields.add(field)
            clean_fields = self._fields(model, lookup.fields)
            query_fields.update(clean_fields)
            normalized_domains.append(tuple(normalized))
            requested_fields.append(clean_fields)

        permitted_result_fields = (
            self.scope.read_fields(model) | permitted_lookup_fields
        )
        if not query_fields.issubset(permitted_result_fields):
            raise OdooReadbackError(
                "Odoo read-back fields are outside the reviewed load preview"
            )
        records = self._search(
            model,
            _combined_lookup_domain(normalized_domains),
            tuple(sorted(query_fields)),
            limit=(2 * len(requested)) + 1,
            permitted_ids=None,
        )
        if len(records) > 2 * len(requested):
            raise OdooReadbackError(
                "Odoo read-back keys matched too many records safely"
            )

        grouped = []
        for domain, fields in zip(
            normalized_domains,
            requested_fields,
            strict=True,
        ):
            matches = tuple(
                ReadbackRecord(
                    odoo_id=record.odoo_id,
                    values={field: record.values[field] for field in fields},
                )
                for record in records
                if _record_matches_lookup(record, domain)
            )
            grouped.append(matches[:2])
        return tuple(grouped)

    def read_external_ids(
        self,
        external_ids: Sequence[str],
    ) -> tuple[ExternalIdBinding, ...]:
        """Resolve only the exact XML IDs committed by one import batch."""

        requested = tuple(external_ids)
        if (
            not requested
            or len(requested) > MAX_READBACK_IDS
            or len(set(requested)) != len(requested)
            or any(
                not isinstance(item, str)
                or len(item) > 255
                or _EXTERNAL_ID.fullmatch(item) is None
                for item in requested
            )
        ):
            raise OdooReadbackError(
                "Odoo External-ID read-back is outside the safe bound"
            )

        names_by_module: dict[str, list[str]] = {}
        for external_id in requested:
            module, _separator, name = external_id.partition(".")
            names_by_module.setdefault(module, []).append(name)

        permitted = frozenset(requested)
        found: dict[str, ExternalIdBinding] = {}
        for module, names in names_by_module.items():
            records = self._search(
                "ir.model.data",
                [["module", "=", module], ["name", "in", names]],
                ("module", "name", "model", "res_id"),
                limit=len(names),
                permitted_ids=None,
            )
            for record in records:
                values = record.values
                returned_module = values["module"]
                returned_name = values["name"]
                model = values["model"]
                odoo_id = values["res_id"]
                if (
                    not isinstance(returned_module, str)
                    or not isinstance(returned_name, str)
                    or not isinstance(model, str)
                    or not model
                    or type(odoo_id) is not int
                    or odoo_id <= 0
                ):
                    raise OdooReadbackError(
                        "Odoo verification returned an invalid External ID"
                    )
                external_id = f"{returned_module}.{returned_name}"
                if external_id not in permitted or external_id in found:
                    raise OdooReadbackError(
                        "Odoo verification returned inconsistent External IDs"
                    )
                found[external_id] = ExternalIdBinding(
                    external_id=external_id,
                    model=model,
                    odoo_id=odoo_id,
                )
        return tuple(found[item] for item in requested if item in found)

    def _fields(self, model: str, fields: Sequence[str]) -> tuple[str, ...]:
        permitted = self.scope.read_fields(model)
        clean = tuple(dict.fromkeys(fields))
        if not set(clean).issubset(permitted):
            raise OdooReadbackError(
                "Odoo read-back fields are outside the reviewed load preview"
            )
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
            "context": target_record_read_context(self.config.context),
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


def _combined_lookup_domain(
    domains: Sequence[tuple[tuple[str, str, Any], ...]],
) -> list[Any]:
    """Return an Odoo prefix domain for an OR of exact AND keys."""

    combined: list[Any] = ["|"] * (len(domains) - 1)
    for domain in domains:
        combined.extend(["&"] * (len(domain) - 1))
        combined.extend(
            [field, operator, value]
            for field, operator, value in domain
        )
    return combined


def _record_matches_lookup(
    record: ReadbackRecord,
    domain: tuple[tuple[str, str, Any], ...],
) -> bool:
    for field, _operator, expected in domain:
        actual = record.values.get(field)
        if (
            isinstance(actual, (list, tuple))
            and len(actual) == 2
            and type(actual[0]) is int
        ):
            actual = actual[0]
        if actual != expected:
            return False
    return True
