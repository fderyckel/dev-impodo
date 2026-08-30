"""Bounded Odoo 19 JSON-2 adapter for live source capture."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from datetime import date, datetime, timezone
import json
import socket
import time
from typing import Any, Callable, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import HTTPRedirectHandler, Request, build_opener

from impodo.domain.odoo.contracts import ConnectorError, MetadataRequest, MetadataSnapshot
from impodo.adapters.odoo.connectors import (
    Json2CaptureIdentityProbe,
    Json2Config,
    Json2ReadConnector,
    STABLE_ODOO_LANGUAGE,
    STABLE_ODOO_TIMEZONE,
    Transport,
)
from ..domain.odoo_capture import (
    OdooCaptureFilterOperator,
    OdooCaptureFilterPolicy,
)
from ..domain.odoo_source_capture import (
    CancellationProbe,
    CaptureScalar,
    OdooCaptureAccounting,
    OdooCapturePage,
    OdooCaptureSample,
    OdooCaptureValueColumn,
    OdooSourceCaptureConfigurationError,
    OdooSourceCaptureConsistencyError,
    OdooSourceCaptureLimitError,
    OdooSourceCaptureRequest,
    normalize_odoo_datetime,
    require_not_cancelled,
)
from impodo.domain.shared.models import OdooReadIdentity, ProtectedOdooReadContext
from ..domain.serialization import canonical_json


RawCaptureTransport = Callable[
    [str, Mapping[str, str], bytes | None, float, str, int],
    tuple[int, bytes],
]

_MAX_ODOO_ID = 2**63 - 1
_TRANSIENT_STATUSES = frozenset({429, 502, 503, 504})
_CONSISTENCY_LIMITATION = (
    "High-water keyset interval: each JSON-2 page is coherent, but deletes, "
    "record-rule changes, ACL changes, or filter membership changes between "
    "pages can alter final membership."
)


class OdooSourceCaptureSession(Protocol):
    """One single-use bounded stream opened after its high-water read."""

    def pages(self) -> Iterator[OdooCapturePage]: ...

    @property
    def matching_rows(self) -> int: ...

    @property
    def accounting(self) -> OdooCaptureAccounting: ...


class Json2OdooSourceCapture:
    """Closed JSON-2 reader with no generic method/domain/context surface."""

    def __init__(
        self,
        config: Json2Config,
        *,
        transport: RawCaptureTransport | None = None,
        now: Callable[[], datetime] | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        if config.context:
            raise OdooSourceCaptureConfigurationError(
                "Odoo source capture requires an empty base context"
            )
        self._config = config
        self._transport = transport or _bounded_urllib_transport
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._sleep = sleep or time.sleep

    def probe_identity(
        self,
        request: OdooSourceCaptureRequest,
        *,
        cancellation: CancellationProbe | None = None,
    ) -> tuple[OdooReadIdentity, ProtectedOdooReadContext]:
        """Freshly verify connection, principal, permission, and base context."""

        try:
            return self._identity_probe(
                request,
                cancellation=cancellation,
            ).probe_capture_identity(
                request.schema_model_names
            )
        except ConnectorError as error:
            raise OdooSourceCaptureConsistencyError(
                "Odoo capture identity verification failed"
            ) from error

    def probe_schema(
        self,
        request: OdooSourceCaptureRequest,
        context: ProtectedOdooReadContext,
        *,
        cancellation: CancellationProbe | None = None,
    ) -> MetadataSnapshot:
        """Freshly read the whole approved schema scope under one exact context."""

        requests = tuple(
            MetadataRequest(
                model=model,
                fields=(),
                all_fields=True,
                include_unique_constraints=True,
            )
            for model in request.schema_model_names
        )
        try:
            return self._probe_connector(
                request,
                context=_capture_context(context, request.filter_policy),
                cancellation=cancellation,
            ).get_model_metadata(requests)
        except ConnectorError as error:
            raise OdooSourceCaptureConsistencyError(
                "Odoo capture schema verification failed"
            ) from error

    def open_capture(
        self,
        request: OdooSourceCaptureRequest,
        context: ProtectedOdooReadContext,
        *,
        cancellation: CancellationProbe | None = None,
    ) -> OdooSourceCaptureSession:
        """Freeze the matching high-water ID and return a single-use page stream."""

        require_not_cancelled(cancellation)
        started_at = self._now().astimezone(timezone.utc)
        domain = _base_domain(request)
        raw, response_bytes = self._search_read(
            request,
            context,
            domain=domain,
            fields=("id",),
            limit=1,
            order="id desc",
            cancellation=cancellation,
        )
        rows = _require_rows(raw, maximum=1)
        high_water_id = 0
        if rows:
            if set(rows[0]) != {"id"}:
                raise OdooSourceCaptureConsistencyError(
                    "Odoo high-water response projection is invalid"
                )
            high_water_id = _require_id(rows[0]["id"])
        matching_rows = 0
        count_response_bytes = 0
        if high_water_id:
            matching_rows, count_response_bytes = self._search_count(
                request,
                context,
                domain=[*_base_domain(request), ["id", "<=", high_water_id]],
                limit=request.maximum_rows + 1,
                cancellation=cancellation,
            )
            if matching_rows > request.maximum_rows:
                raise OdooSourceCaptureLimitError(
                    f"More than {request.maximum_rows:,} records match this "
                    "capture plan. Narrow the selection before freezing records."
                )
        return _Json2CaptureSession(
            adapter=self,
            request=request,
            context=context,
            high_water_id=high_water_id,
            started_at=started_at,
            matching_rows=matching_rows,
            initial_response_bytes=response_bytes + count_response_bytes,
            initial_request_count=(2 if high_water_id else 1),
            cancellation=cancellation,
        )

    def count_matching(
        self,
        request: OdooSourceCaptureRequest,
        context: ProtectedOdooReadContext,
        *,
        limit: int,
        cancellation: CancellationProbe | None = None,
    ) -> int:
        """Return one bounded Odoo count without fetching record identifiers."""

        count, _ = self._search_count(
            request,
            context,
            domain=_base_domain(request),
            limit=limit,
            cancellation=cancellation,
        )
        return count

    def sample(
        self,
        request: OdooSourceCaptureRequest,
        context: ProtectedOdooReadContext,
        *,
        limit: int,
        cancellation: CancellationProbe | None = None,
    ) -> OdooCaptureSample:
        """Return one bounded preview call, never final membership evidence."""

        if not 1 <= limit <= request.max_sample_rows:
            raise OdooSourceCaptureConfigurationError(
                "Odoo capture sample limit is invalid"
            )
        require_not_cancelled(cancellation)
        raw, response_bytes = self._search_read(
            request,
            context,
            domain=_base_domain(request),
            fields=("id", "write_date", *request.field_names),
            limit=limit,
            order="id asc",
            cancellation=cancellation,
        )
        rows = _require_rows(raw, maximum=limit)
        if not rows:
            return OdooCaptureSample(page=None)
        page = _decode_page(
            request,
            rows,
            first_row_ordinal=1,
            lower_exclusive=0,
            upper_inclusive=_MAX_ODOO_ID,
            response_bytes=response_bytes,
        )
        require_not_cancelled(cancellation)
        return OdooCaptureSample(page=page)

    def _probe_connector(
        self,
        request: OdooSourceCaptureRequest,
        *,
        context: Mapping[str, Any] | None = None,
        cancellation: CancellationProbe | None = None,
    ) -> Json2ReadConnector:
        config, transport = self._probe_parts(
            request,
            context=context,
            cancellation=cancellation,
        )
        return Json2ReadConnector(config, transport=transport, now=self._now)

    def _probe_parts(
        self,
        request: OdooSourceCaptureRequest,
        *,
        context: Mapping[str, Any] | None = None,
        cancellation: CancellationProbe | None = None,
    ) -> tuple[Json2Config, Transport]:
        config = Json2Config(
            base_url=self._config.base_url,
            database=self._config.database,
            api_key=self._config.api_key,
            connection_mode=self._config.connection_mode,
            timeout_seconds=self._config.timeout_seconds,
            page_size=request.page_size,
            # The raw bounded transport below owns the one retry loop.
            retries=0,
            max_request_bytes=request.max_request_bytes,
            max_response_bytes=request.max_response_bytes,
            context=dict(context or {}),
            relevant_modules=self._config.relevant_modules,
        )

        def parsed_transport(
            url: str,
            headers: Mapping[str, str],
            body: bytes | None,
            timeout: float,
            method: str,
        ) -> tuple[int, Any]:
            raw = self._request(
                request,
                url=url,
                headers=headers,
                body=body,
                timeout=timeout,
                method=method,
                cancellation=cancellation,
            )
            status, payload = raw
            if status != 200 or not payload:
                return status, None
            try:
                return status, json.loads(payload)
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise OdooSourceCaptureConsistencyError(
                    "Odoo capture probe returned malformed JSON"
                ) from error

        return config, parsed_transport

    def _identity_probe(
        self,
        request: OdooSourceCaptureRequest,
        *,
        cancellation: CancellationProbe | None,
    ) -> Json2CaptureIdentityProbe:
        config, transport = self._probe_parts(
            request,
            cancellation=cancellation,
        )
        return Json2CaptureIdentityProbe(
            config,
            transport=transport,
            now=self._now,
        )

    def _search_read(
        self,
        request: OdooSourceCaptureRequest,
        context: ProtectedOdooReadContext,
        *,
        domain: list[list[object]],
        fields: tuple[str, ...],
        limit: int,
        order: str,
        cancellation: CancellationProbe | None,
    ) -> tuple[object, int]:
        body = canonical_json(
            {
                "context": _capture_context(context, request.filter_policy),
                "domain": domain,
                "fields": list(fields),
                "limit": limit,
                "order": order,
            }
        ).encode("utf-8")
        if len(body) > request.max_request_bytes:
            raise OdooSourceCaptureLimitError(
                "Odoo capture request exceeds the fixed byte limit"
            )
        url = (
            f"{self._config.base_url}/json/2/"
            f"{quote(request.model, safe='.')}/search_read"
        )
        status, raw = self._request(
            request,
            url=url,
            headers=_headers(self._config),
            body=body,
            timeout=self._config.timeout_seconds,
            method="POST",
            cancellation=cancellation,
        )
        if status in {401, 403}:
            raise OdooSourceCaptureConsistencyError(
                "Odoo capture authorization failed"
            )
        if status != 200:
            raise OdooSourceCaptureConsistencyError(
                f"Odoo capture read failed with HTTP {status}"
            )
        try:
            return json.loads(raw), len(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise OdooSourceCaptureConsistencyError(
                "Odoo capture returned malformed JSON"
            ) from error

    def _search_count(
        self,
        request: OdooSourceCaptureRequest,
        context: ProtectedOdooReadContext,
        *,
        domain: list[list[object]],
        limit: int,
        cancellation: CancellationProbe | None,
    ) -> tuple[int, int]:
        if not 1 <= limit <= request.maximum_rows + 1:
            raise OdooSourceCaptureConfigurationError(
                "Odoo capture count limit is invalid"
            )
        body = canonical_json(
            {
                "context": _capture_context(context, request.filter_policy),
                "domain": domain,
                "limit": limit,
            }
        ).encode("utf-8")
        url = (
            f"{self._config.base_url}/json/2/"
            f"{quote(request.model, safe='.')}/search_count"
        )
        status, raw = self._request(
            request,
            url=url,
            headers=_headers(self._config),
            body=body,
            timeout=self._config.timeout_seconds,
            method="POST",
            cancellation=cancellation,
        )
        if status in {401, 403}:
            raise OdooSourceCaptureConsistencyError(
                "Odoo capture authorization failed"
            )
        if status != 200:
            raise OdooSourceCaptureConsistencyError(
                f"Odoo capture count failed with HTTP {status}"
            )
        try:
            count = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise OdooSourceCaptureConsistencyError(
                "Odoo capture count returned malformed JSON"
            ) from error
        if (
            isinstance(count, bool)
            or not isinstance(count, int)
            or not 0 <= count <= limit
        ):
            raise OdooSourceCaptureConsistencyError(
                "Odoo capture count response is invalid"
            )
        return count, len(raw)

    def _request(
        self,
        request: OdooSourceCaptureRequest,
        *,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout: float,
        method: str,
        cancellation: CancellationProbe | None,
    ) -> tuple[int, bytes]:
        if body is not None and len(body) > request.max_request_bytes:
            raise OdooSourceCaptureLimitError(
                "Odoo capture request exceeds the fixed byte limit"
            )
        for attempt in range(self._config.retries + 1):
            require_not_cancelled(cancellation)
            try:
                status, payload = self._transport(
                    url,
                    headers,
                    body,
                    timeout,
                    method,
                    request.max_response_bytes,
                )
            except OdooSourceCaptureLimitError:
                raise
            except (TimeoutError, socket.timeout, URLError) as error:
                if attempt >= self._config.retries:
                    raise OdooSourceCaptureConsistencyError(
                        "Odoo capture timed out or was unreachable"
                    ) from error
                self._sleep(0.05 * (2**attempt))
                continue
            if len(payload) > request.max_response_bytes:
                raise OdooSourceCaptureLimitError(
                    "Odoo capture response exceeds the fixed byte limit"
                )
            if status in _TRANSIENT_STATUSES and attempt < self._config.retries:
                self._sleep(0.05 * (2**attempt))
                continue
            return status, payload
        raise OdooSourceCaptureConsistencyError("Odoo capture request failed")


class _Json2CaptureSession:
    def __init__(
        self,
        *,
        adapter: Json2OdooSourceCapture,
        request: OdooSourceCaptureRequest,
        context: ProtectedOdooReadContext,
        high_water_id: int,
        started_at: datetime,
        matching_rows: int,
        initial_response_bytes: int,
        initial_request_count: int,
        cancellation: CancellationProbe | None,
    ) -> None:
        self._adapter = adapter
        self._request = request
        self._context = context
        self._high_water_id = high_water_id
        self._started_at = started_at
        self._matching_rows = matching_rows
        self._response_bytes = initial_response_bytes
        self._normalized_bytes = 0
        self._row_count = 0
        self._page_count = 0
        self._record_request_count = initial_request_count
        self._cancellation = cancellation
        self._started = False
        self._finished = False
        self._accounting: OdooCaptureAccounting | None = None

    def pages(self) -> Iterator[OdooCapturePage]:
        if self._started:
            raise OdooSourceCaptureConsistencyError(
                "Odoo capture page stream is single-use"
            )
        self._started = True
        last_id = 0
        if self._high_water_id == 0:
            self._finish()
            return
        while True:
            require_not_cancelled(self._cancellation)
            remaining_probe = self._request.maximum_rows + 1 - self._row_count
            limit = min(self._request.page_size, remaining_probe)
            raw, response_bytes = self._adapter._search_read(
                self._request,
                self._context,
                domain=[
                    *_base_domain(self._request),
                    ["id", ">", last_id],
                    ["id", "<=", self._high_water_id],
                ],
                fields=("id", "write_date", *self._request.field_names),
                limit=limit,
                order="id asc",
                cancellation=self._cancellation,
            )
            self._record_request_count += 1
            self._response_bytes += response_bytes
            rows = _require_rows(raw, maximum=limit)
            if not rows:
                self._finish()
                return

            page = _decode_page(
                self._request,
                rows,
                first_row_ordinal=self._row_count + 1,
                lower_exclusive=last_id,
                upper_inclusive=self._high_water_id,
                response_bytes=response_bytes,
            )
            proposed_count = self._row_count + page.row_count
            proposed_bytes = self._normalized_bytes + page.normalized_bytes
            if proposed_count > self._request.maximum_rows:
                raise OdooSourceCaptureLimitError(
                    "Odoo capture exceeds the selected row limit"
                )
            if proposed_bytes > self._request.max_snapshot_bytes:
                raise OdooSourceCaptureLimitError(
                    "Odoo capture exceeds the fixed snapshot byte limit"
                )
            self._row_count = proposed_count
            self._normalized_bytes = proposed_bytes
            self._page_count += 1
            last_id = page.odoo_ids[-1]
            require_not_cancelled(self._cancellation)
            yield page
            if len(rows) < limit or last_id == self._high_water_id:
                self._finish()
                return

    @property
    def matching_rows(self) -> int:
        return self._matching_rows

    @property
    def accounting(self) -> OdooCaptureAccounting:
        if not self._finished or self._accounting is None:
            raise OdooSourceCaptureConsistencyError(
                "Odoo capture accounting is unavailable before stream completion"
            )
        return self._accounting

    def _finish(self) -> None:
        require_not_cancelled(self._cancellation)
        self._finished = True
        self._accounting = OdooCaptureAccounting(
            high_water_id=self._high_water_id,
            row_count=self._row_count,
            page_count=self._page_count,
            record_request_count=self._record_request_count,
            response_bytes=self._response_bytes,
            normalized_bytes=self._normalized_bytes,
            capture_started_at=self._started_at,
            capture_finished_at=self._adapter._now().astimezone(timezone.utc),
            consistency=self._request.consistency,
            target_instance_assurance=self._request.target_instance_assurance,
            consistency_limitation=_CONSISTENCY_LIMITATION,
        )


def _decode_page(
    request: OdooSourceCaptureRequest,
    rows: list[Mapping[str, Any]],
    *,
    first_row_ordinal: int,
    lower_exclusive: int,
    upper_inclusive: int,
    response_bytes: int,
) -> OdooCapturePage:
    expected = {"id", "write_date", *request.field_names}
    identifiers: list[int] = []
    write_dates: list[datetime | None] = []
    value_columns: dict[str, list[CaptureScalar]] = {
        name: [] for name in request.field_names
    }
    normalized_bytes = 0
    previous = lower_exclusive
    for row in rows:
        if set(row) != expected:
            raise OdooSourceCaptureConsistencyError(
                "Odoo capture response projection is incomplete or unexpected"
            )
        identifier = _require_id(row["id"])
        if not previous < identifier <= upper_inclusive:
            raise OdooSourceCaptureConsistencyError(
                "Odoo capture identifiers are reordered or outside the page bounds"
            )
        previous = identifier
        identifiers.append(identifier)
        write_date, write_size = _decode_write_date(row["write_date"])
        write_dates.append(write_date)
        row_bytes = 8 + write_size
        for projection in request.projection:
            value, value_bytes = _decode_value(
                projection.field_type,
                row[projection.name],
                maximum_bytes=request.max_value_bytes,
            )
            value_columns[projection.name].append(value)
            row_bytes += value_bytes
        if row_bytes > request.max_row_bytes:
            raise OdooSourceCaptureLimitError(
                "Odoo capture row exceeds the fixed byte limit"
            )
        normalized_bytes += row_bytes
        if normalized_bytes > request.max_snapshot_bytes:
            raise OdooSourceCaptureLimitError(
                "Odoo capture page exceeds the fixed snapshot byte limit"
            )
    return OdooCapturePage(
        first_row_ordinal=first_row_ordinal,
        odoo_ids=tuple(identifiers),
        write_dates=tuple(write_dates),
        columns=tuple(
            OdooCaptureValueColumn(
                field_name=projection.name,
                field_type=projection.field_type,
                values=tuple(value_columns[projection.name]),
            )
            for projection in request.projection
        ),
        response_bytes=response_bytes,
        normalized_bytes=normalized_bytes,
    )


def _decode_value(
    field_type: str,
    raw: Any,
    *,
    maximum_bytes: int,
) -> tuple[CaptureScalar, int]:
    if field_type == "boolean":
        if not isinstance(raw, bool):
            raise OdooSourceCaptureConsistencyError(
                "Odoo returned an invalid boolean value"
            )
        return raw, 1
    if raw is False or raw is None:
        return None, 1
    if field_type == "integer":
        if isinstance(raw, bool) or not isinstance(raw, int):
            raise OdooSourceCaptureConsistencyError(
                "Odoo returned an invalid integer value"
            )
        return raw, 8
    if field_type in {"char", "text", "selection"}:
        if not isinstance(raw, str):
            raise OdooSourceCaptureConsistencyError(
                "Odoo returned an invalid text value"
            )
        size = len(raw.encode("utf-8"))
        if size > maximum_bytes:
            raise OdooSourceCaptureLimitError(
                "Odoo capture value exceeds the fixed byte limit"
            )
        return raw, max(1, size)
    if field_type == "date":
        if not isinstance(raw, str):
            raise OdooSourceCaptureConsistencyError(
                "Odoo returned an invalid date value"
            )
        try:
            value = date.fromisoformat(raw)
        except ValueError as error:
            raise OdooSourceCaptureConsistencyError(
                "Odoo returned an invalid date value"
            ) from error
        if value.isoformat() != raw:
            raise OdooSourceCaptureConsistencyError(
                "Odoo returned an invalid date value"
            )
        return value, 4
    if field_type == "datetime":
        return normalize_odoo_datetime(raw), 8
    raise OdooSourceCaptureConsistencyError(
        "Odoo returned an unsupported capture value"
    )


def _decode_write_date(raw: Any) -> tuple[datetime | None, int]:
    if raw is False or raw is None:
        return None, 1
    if not isinstance(raw, str):
        raise OdooSourceCaptureConsistencyError(
            "Odoo returned an invalid write timestamp"
        )
    return normalize_odoo_datetime(raw), 8


def _require_rows(raw: object, *, maximum: int) -> list[Mapping[str, Any]]:
    if not isinstance(raw, list) or len(raw) > maximum:
        raise OdooSourceCaptureConsistencyError(
            "Odoo capture response row shape is invalid"
        )
    if any(not isinstance(item, Mapping) for item in raw):
        raise OdooSourceCaptureConsistencyError(
            "Odoo capture response row shape is invalid"
        )
    return list(raw)


def _require_id(raw: Any) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int) or not 1 <= raw <= _MAX_ODOO_ID:
        raise OdooSourceCaptureConsistencyError(
            "Odoo capture returned an invalid identifier"
        )
    return raw


def _base_domain(request: OdooSourceCaptureRequest) -> list[list[object]]:
    operator_map = {
        OdooCaptureFilterOperator.EQUALS: "=",
        OdooCaptureFilterOperator.IN_SET: "in",
        OdooCaptureFilterOperator.ON_OR_AFTER: ">=",
        OdooCaptureFilterOperator.AFTER: ">",
        OdooCaptureFilterOperator.ON_OR_BEFORE: "<=",
        OdooCaptureFilterOperator.BEFORE: "<",
    }
    domain: list[list[object]] = []
    for clause in request.filter_clauses:
        operand: object = (
            list(clause.values)
            if clause.operator is OdooCaptureFilterOperator.IN_SET
            else clause.values[0]
        )
        domain.append([clause.field_name, operator_map[clause.operator], operand])
    if request.filter_policy is OdooCaptureFilterPolicy.ACTIVE_RECORDS:
        domain.append(["active", "=", True])
    return domain


def _capture_context(
    context: ProtectedOdooReadContext,
    policy: OdooCaptureFilterPolicy,
) -> dict[str, object]:
    return {
        "active_test": policy is OdooCaptureFilterPolicy.ACTIVE_RECORDS,
        "allowed_company_ids": list(context.allowed_company_ids),
        "lang": STABLE_ODOO_LANGUAGE,
        "tz": STABLE_ODOO_TIMEZONE,
    }


def _headers(config: Json2Config) -> dict[str, str]:
    return {
        "Authorization": f"bearer {config.api_key}",
        "Content-Type": "application/json; charset=utf-8",
        "X-Odoo-Database": config.database,
        "User-Agent": "impodo",
    }


def _bounded_urllib_transport(
    url: str,
    headers: Mapping[str, str],
    body: bytes | None,
    timeout: float,
    method: str,
    maximum_bytes: int,
) -> tuple[int, bytes]:
    """Read no more than ``maximum_bytes + 1`` before JSON parsing."""

    request = Request(url=url, data=body, headers=dict(headers), method=method)
    try:
        opener = build_opener(_NoRedirectHandler())
        with opener.open(request, timeout=timeout) as response:
            declared = response.headers.get("Content-Length")
            if declared is not None:
                try:
                    if int(declared) > maximum_bytes:
                        raise OdooSourceCaptureLimitError(
                            "Odoo capture response exceeds the fixed byte limit"
                        )
                except ValueError as error:
                    raise OdooSourceCaptureConsistencyError(
                        "Odoo capture response has an invalid content length"
                    ) from error
            raw = response.read(maximum_bytes + 1)
            if len(raw) > maximum_bytes:
                raise OdooSourceCaptureLimitError(
                    "Odoo capture response exceeds the fixed byte limit"
                )
            return response.status, raw
    except HTTPError as error:
        # Response bodies may contain business values or implementation details.
        return error.code, b""


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None
