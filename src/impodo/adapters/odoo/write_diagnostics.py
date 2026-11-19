"""Extract bounded, value-free rejection evidence from an Odoo response.

Only fields in the reviewed request and valid batch-relative row ranges are
accepted. Unknown server text is fingerprinted, not persisted or logged.
See docs/developer/contracts/execution-and-reconciliation.md.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from hashlib import sha256
from http.client import IncompleteRead
from urllib.error import URLError

from impodo.domain.execution.load_control import DATA_REJECTION_CODES
from impodo.domain.execution.odoo_write import OdooWriteOutcomeUnknown
from impodo.domain.execution.write_diagnostics import (
    OdooWriteDiagnostic,
    UncertainWriteReason,
    WriteFailureCode,
)
from impodo.domain.odoo.contracts import ConnectorIncompleteResultError


def response_failure_reason(error: Exception) -> UncertainWriteReason:
    """Classify the observed failure without retaining exception text."""

    reason = error.reason if isinstance(error, URLError) else error
    if isinstance(reason, TimeoutError):
        return UncertainWriteReason.TIMEOUT
    if isinstance(reason, IncompleteRead):
        return UncertainWriteReason.INCOMPLETE_RESPONSE
    if isinstance(reason, ConnectorIncompleteResultError):
        return UncertainWriteReason.RESPONSE_TOO_LARGE
    if isinstance(reason, (ValueError, RecursionError)):
        return UncertainWriteReason.UNREADABLE_RESPONSE
    return UncertainWriteReason.CONNECTION_FAILURE


def uncertain_write(
    model: str,
    method: str,
    batch_size: int,
    reason: UncertainWriteReason,
    *,
    http_status: int | None,
    returned_id_count: int | None = None,
) -> OdooWriteOutcomeUnknown:
    """Attach safe response evidence while leaving commit certainty open."""

    diagnostic = OdooWriteDiagnostic(
        WriteFailureCode.UNKNOWN,
        model,
        method,
        batch_size=batch_size,
        http_status=http_status,
        uncertainty_reason=reason,
        returned_id_count=returned_id_count,
    )
    return OdooWriteOutcomeUnknown(diagnostic.summary, diagnostic=diagnostic)


def import_rejection_diagnostic(
    messages: object,
    *,
    model: str,
    fields: Sequence[str],
    batch_size: int,
) -> OdooWriteDiagnostic:
    """Retain evidence even for missing, malformed, or unrecognized errors."""

    items = messages if isinstance(messages, list) else []
    permitted = {field.split("/", 1)[0] for field in fields}
    reported: set[str] = set()
    codes: set[WriteFailureCode] = set()
    ranges: list[tuple[int, int]] = []
    complete_ranges = len(items) <= 20
    if not complete_ranges:
        codes.add(WriteFailureCode.REJECTED)
    digest = sha256()
    for item in items[:20]:
        if not isinstance(item, Mapping):
            complete_ranges = False
            codes.add(WriteFailureCode.REJECTED)
            continue
        message = item.get("message", "")
        text = message[:8192] if isinstance(message, str) else ""
        digest.update(text.encode("utf-8"))
        digest.update(b"\x00")
        severity = item.get("type")
        if severity in ("warning", "info"):
            continue
        if severity != "error":
            codes.add(WriteFailureCode.REJECTED)
            complete_ranges = False
            continue
        field = item.get("field")
        if isinstance(field, (tuple, list)):
            field = field[0] if field else None
        if isinstance(field, str) and field.split("/", 1)[0] in permitted:
            reported.add(field.split("/", 1)[0])
        rows = item.get("rows")
        valid_range = False
        if isinstance(rows, Mapping):
            start, end = rows.get("from"), rows.get("to")
            if (
                type(start) is int
                and type(end) is int
                and 0 <= start <= end < batch_size
            ):
                ranges.append((start, end))
                valid_range = True
        complete_ranges = complete_ranges and valid_range
        if not isinstance(message, str) or len(message) > len(text):
            codes.add(WriteFailureCode.REJECTED)
        probe = text.casefold()
        code = WriteFailureCode.REJECTED
        if (
            "access denied" in probe
            or "accesserror" in probe
            or "not allowed to" in probe
        ):
            code = WriteFailureCode.ACCESS_DENIED
        elif (
            "company inconsistencies" in probe
            or "incompatible companies on records" in probe
            or "belongs to another company" in probe
        ):
            code = WriteFailureCode.COMPANY_MISMATCH
            reported.update(
                name
                for name in re.findall(r"\(([A-Za-z_][A-Za-z0-9_]*):", text)
                if name in permitted
            )
        elif (
            "missing required" in probe
            or "required field" in probe
            or "mandatory field" in probe
        ):
            code = WriteFailureCode.REQUIRED_VALUE
        elif "no matching record" in probe or "no record found" in probe:
            code = WriteFailureCode.RELATED_RECORD
        elif (
            "constraint" in probe
            or "must be unique" in probe
            or "duplicate key" in probe
        ):
            code = WriteFailureCode.CONSTRAINT
        elif (
            "not valid" in probe
            or "invalid" in probe
            or "validationerror" in probe
            or "does not seem to be valid" in probe
        ):
            code = WriteFailureCode.VALIDATION
        codes.add(code)
    if WriteFailureCode.ACCESS_DENIED in codes:
        batch_code = WriteFailureCode.ACCESS_DENIED
    elif len(codes) == 1:
        batch_code = next(iter(codes))
    elif codes and codes <= DATA_REJECTION_CODES:
        batch_code = WriteFailureCode.VALIDATION
    else:
        batch_code = WriteFailureCode.REJECTED
    return OdooWriteDiagnostic(
        code=batch_code,
        model=model,
        method="load",
        fields=tuple(reported),
        batch_size=batch_size,
        row_from=ranges[0][0] if complete_ranges and len(set(ranges)) == 1 else None,
        row_to=ranges[0][1] if complete_ranges and len(set(ranges)) == 1 else None,
        message_count=min(len(items), 1_000_000),
        message_fingerprint=f"sha256:{digest.hexdigest()}",
    )
