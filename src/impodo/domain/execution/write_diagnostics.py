"""Value-free, durable context for a rejected or uncertain Odoo operation.

The execution journal retains this evidence beside each affected attempt.
Server text and submitted business values never enter the diagnostic payload.
See docs/developer/contracts/execution-and-reconciliation.md.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from enum import StrEnum


class WriteFailureCode(StrEnum):
    REQUIRED_VALUE = "ODOO_REQUIRED_VALUE_MISSING"
    RELATED_RECORD = "ODOO_RELATED_RECORD_NOT_FOUND"
    ACCESS_DENIED = "ODOO_ACCESS_DENIED"
    CONSTRAINT = "ODOO_CONSTRAINT_REJECTED"
    VALIDATION = "ODOO_VALUE_REJECTED"
    REJECTED = "ODOO_WRITE_REJECTED"
    UNKNOWN = "ODOO_WRITE_OUTCOME_UNKNOWN"


_MESSAGES = {
    WriteFailureCode.REQUIRED_VALUE: "Odoo requires a value that was not supplied",
    WriteFailureCode.RELATED_RECORD: "Odoo could not resolve a linked record",
    WriteFailureCode.ACCESS_DENIED: "Odoo denied access to this operation",
    WriteFailureCode.CONSTRAINT: "An Odoo constraint rejected the operation",
    WriteFailureCode.VALIDATION: "Odoo rejected a field value",
    WriteFailureCode.REJECTED: "Odoo rejected this operation; no specific reason was available",
    WriteFailureCode.UNKNOWN: "The Odoo write outcome is uncertain; do not retry it",
}
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_.]{0,127}")
_HASH = re.compile(r"sha256:[a-f0-9]{64}")


@dataclass(frozen=True, slots=True)
class OdooWriteDiagnostic:
    """Bounded server-reported context, never an inferred faulty source row."""

    code: WriteFailureCode
    model: str
    method: str
    fields: tuple[str, ...] = ()
    row_from: int | None = None
    row_to: int | None = None
    batch_size: int = 1
    message_count: int = 0
    message_fingerprint: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "code", WriteFailureCode(self.code))
        object.__setattr__(self, "fields", tuple(sorted(set(self.fields))))
        if (
            _NAME.fullmatch(self.model) is None
            or self.method
            not in {"load", "create", "write", "search_read", "completion"}
            or not 1 <= self.batch_size <= 1000
            or not 0 <= self.message_count <= 1_000_000
            or len(self.fields) > 200
            or any(_NAME.fullmatch(field) is None for field in self.fields)
            or (
                self.message_fingerprint
                and _HASH.fullmatch(self.message_fingerprint) is None
            )
            or ((self.row_from is None) != (self.row_to is None))
            or (
                self.row_from is not None
                and (
                    self.row_to is None
                    or not 0 <= self.row_from <= self.row_to < self.batch_size
                )
            )
        ):
            raise ValueError("Odoo write diagnostic is invalid")

    @property
    def summary(self) -> str:
        """Return safe copy suitable for both the journal and browser."""

        field_text = f" Fields: {', '.join(self.fields)}." if self.fields else ""
        rows = (
            f" Odoo reported batch row(s) {self.row_from + 1}–{self.row_to + 1}."
            if self.row_from is not None and self.row_to is not None
            else ""
        )
        return f"{_MESSAGES[self.code]}.{field_text}{rows} [{self.code.value}]"

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["code"] = self.code.value
        payload["fields"] = list(self.fields)
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> OdooWriteDiagnostic:
        fields = payload.get("fields", ())
        if not isinstance(fields, (tuple, list)) or any(
            not isinstance(field, str) for field in fields
        ):
            raise ValueError("Odoo diagnostic fields are invalid")
        start, end = payload.get("row_from"), payload.get("row_to")
        if (start is not None and type(start) is not int) or (
            end is not None and type(end) is not int
        ):
            raise ValueError("Odoo diagnostic row range is invalid")
        return cls(
            code=WriteFailureCode(str(payload["code"])),
            model=str(payload["model"]),
            method=str(payload["method"]),
            fields=tuple(str(field) for field in fields),
            row_from=start,
            row_to=end,
            batch_size=int(str(payload.get("batch_size", 1))),
            message_count=int(str(payload.get("message_count", 0))),
            message_fingerprint=str(payload.get("message_fingerprint", "")),
        )
