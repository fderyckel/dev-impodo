"""Extract bounded, value-free rejection evidence from an Odoo response.

Only fields in the reviewed request and valid batch-relative row ranges are
accepted. Unknown server text is fingerprinted, not persisted or logged.
See docs/developer/contracts/execution-and-reconciliation.md.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from hashlib import sha256

from impodo.domain.execution.write_diagnostics import (
    OdooWriteDiagnostic,
    WriteFailureCode,
)


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
    codes: list[WriteFailureCode] = []
    ranges: list[tuple[int, int]] = []
    complete_ranges = len(items) <= 20
    digest = sha256()
    for item in items[:20]:
        if not isinstance(item, Mapping):
            complete_ranges = False
            continue
        text = str(item.get("message", ""))[:8192]
        digest.update(text.encode("utf-8"))
        digest.update(b"\x00")
        if item.get("type") != "error":
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
        probe = text.casefold()
        code = WriteFailureCode.REJECTED
        if (
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
            "access denied" in probe
            or "accesserror" in probe
            or "not allowed to" in probe
        ):
            code = WriteFailureCode.ACCESS_DENIED
        elif (
            "constraint" in probe
            or "must be unique" in probe
            or "duplicate key" in probe
        ):
            code = WriteFailureCode.CONSTRAINT
        elif "not valid" in probe or "invalid" in probe or "validationerror" in probe:
            code = WriteFailureCode.VALIDATION
        codes.append(code)
    specific = tuple(
        dict.fromkeys(code for code in codes if code is not WriteFailureCode.REJECTED)
    )
    return OdooWriteDiagnostic(
        code=specific[0] if len(specific) == 1 else WriteFailureCode.REJECTED,
        model=model,
        method="load",
        fields=tuple(reported),
        batch_size=batch_size,
        row_from=ranges[0][0] if complete_ranges and len(set(ranges)) == 1 else None,
        row_to=ranges[0][1] if complete_ranges and len(set(ranges)) == 1 else None,
        message_count=min(len(items), 1_000_000),
        message_fingerprint=f"sha256:{digest.hexdigest()}",
    )
