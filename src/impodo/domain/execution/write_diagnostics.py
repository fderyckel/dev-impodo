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
    COMPANY_MISMATCH = "ODOO_COMPANY_MISMATCH"
    VALIDATION = "ODOO_VALUE_REJECTED"
    REJECTED = "ODOO_WRITE_REJECTED"
    UNKNOWN = "ODOO_WRITE_OUTCOME_UNKNOWN"


class OdooWriteErrorType(StrEnum):
    """Built-in Odoo exception types safe to retain from JSON-2 errors."""

    ACCESS = "odoo.exceptions.AccessError"
    MISSING = "odoo.exceptions.MissingError"
    USER = "odoo.exceptions.UserError"
    VALIDATION = "odoo.exceptions.ValidationError"


class UncertainWriteReason(StrEnum):
    """Safe observations about a write whose commit could not be confirmed."""

    TIMEOUT = "TIMEOUT"
    CONNECTION_FAILURE = "CONNECTION_FAILURE"
    INCOMPLETE_RESPONSE = "INCOMPLETE_RESPONSE"
    UNREADABLE_RESPONSE = "UNREADABLE_RESPONSE"
    RESPONSE_TOO_LARGE = "RESPONSE_TOO_LARGE"
    INVALID_RECEIPT = "INVALID_RECEIPT"
    RECEIPT_COUNT_MISMATCH = "RECEIPT_COUNT_MISMATCH"
    RECEIPT_WITH_ERRORS = "RECEIPT_WITH_ERRORS"


_UNCERTAIN_MESSAGES = {
    UncertainWriteReason.TIMEOUT: "The request to Odoo timed out",
    UncertainWriteReason.CONNECTION_FAILURE: "The connection to Odoo failed during the write request",
    UncertainWriteReason.INCOMPLETE_RESPONSE: "Impodo received an incomplete response to the write request",
    UncertainWriteReason.UNREADABLE_RESPONSE: "Impodo could not read the response as JSON",
    UncertainWriteReason.RESPONSE_TOO_LARGE: "The response exceeded Impodo's size limit",
    UncertainWriteReason.INVALID_RECEIPT: "The response did not contain a usable write receipt",
    UncertainWriteReason.RECEIPT_WITH_ERRORS: "The receipt contained both record IDs and errors",
}


_MESSAGES = {
    WriteFailureCode.REQUIRED_VALUE: "Odoo requires a value that was not supplied",
    WriteFailureCode.RELATED_RECORD: "Odoo could not resolve a linked record",
    WriteFailureCode.ACCESS_DENIED: "Odoo denied access to this operation",
    WriteFailureCode.CONSTRAINT: "An Odoo constraint rejected the operation",
    WriteFailureCode.COMPANY_MISMATCH: (
        "A linked Odoo record belongs to a different company. "
        "Use the intended Odoo company and compare again"
    ),
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
    http_status: int | None = None
    error_type: OdooWriteErrorType | None = None
    operator_detail_status: str = ""
    uncertainty_reason: UncertainWriteReason | None = None
    returned_id_count: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "code", WriteFailureCode(self.code))
        object.__setattr__(self, "fields", tuple(sorted(set(self.fields))))
        if self.error_type is not None:
            object.__setattr__(self, "error_type", OdooWriteErrorType(self.error_type))
        if self.uncertainty_reason is not None:
            object.__setattr__(self, "uncertainty_reason", UncertainWriteReason(self.uncertainty_reason))
        if (
            _NAME.fullmatch(self.model) is None
            or self.method
            not in {"load", "create", "write", "search_read", "completion"}
            or not 1 <= self.batch_size <= 1000
            or not 0 <= self.message_count <= 1_000_000
            or (
                self.http_status is not None
                and (type(self.http_status) is not int or not 100 <= self.http_status <= 599)
            )
            or len(self.fields) > 200
            or (self.uncertainty_reason is not None and self.code is not WriteFailureCode.UNKNOWN)
            or (
                self.uncertainty_reason is UncertainWriteReason.RECEIPT_COUNT_MISMATCH
                and self.returned_id_count is None
            )
            or (
                self.returned_id_count is not None
                and (
                    self.uncertainty_reason is not UncertainWriteReason.RECEIPT_COUNT_MISMATCH
                    or type(self.returned_id_count) is not int
                    or not 0 <= self.returned_id_count <= 2**31 - 1
                    or self.returned_id_count == self.batch_size
                )
            )
            or self.operator_detail_status not in {"", "AVAILABLE", "UNAVAILABLE", "TRUNCATED", "WITHHELD", "CAPTURE_FAILED"}
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
        status = f" HTTP status {self.http_status}." if self.http_status is not None else ""
        error_type = f" Odoo reported {self.error_type.value.rsplit('.', 1)[-1]}." if self.error_type else ""
        message = (
            "Odoo rejected this operation"
            if self.code is WriteFailureCode.REJECTED and self.error_type
            else _MESSAGES[self.code]
        )
        if self.code is WriteFailureCode.VALIDATION and not self.fields:
            message = "Odoo rejected this operation during validation"
        if self.uncertainty_reason is UncertainWriteReason.RECEIPT_COUNT_MISMATCH:
            message = f"Odoo returned {self.returned_id_count} record IDs for {self.batch_size} submitted records"
        elif self.uncertainty_reason is not None:
            message = _UNCERTAIN_MESSAGES[self.uncertainty_reason]
        if self.uncertainty_reason is not None:
            message += ". Impodo could not confirm what Odoo wrote at the time"
        return f"{message}.{field_text}{rows}{status}{error_type} [{self.code.value}]"

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["code"] = self.code.value
        payload["fields"] = list(self.fields)
        if self.http_status is None:
            del payload["http_status"]
        if self.error_type is None:
            del payload["error_type"]
        if not self.operator_detail_status:
            del payload["operator_detail_status"]
        if self.uncertainty_reason is None:
            del payload["uncertainty_reason"]
        if self.returned_id_count is None:
            del payload["returned_id_count"]
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> OdooWriteDiagnostic:
        fields = payload.get("fields", ())
        if not isinstance(fields, (tuple, list)) or any(
            not isinstance(field, str) for field in fields
        ):
            raise ValueError("Odoo diagnostic fields are invalid")
        start, end = payload.get("row_from"), payload.get("row_to")
        http_status = payload.get("http_status")
        error_type = payload.get("error_type")
        uncertainty_reason = payload.get("uncertainty_reason")
        returned_id_count = payload.get("returned_id_count")
        if http_status is not None and type(http_status) is not int:
            raise ValueError("Odoo diagnostic HTTP status is invalid")
        if returned_id_count is not None and type(returned_id_count) is not int:
            raise ValueError("Odoo diagnostic receipt count is invalid")
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
            http_status=http_status,
            error_type=OdooWriteErrorType(str(error_type)) if error_type is not None else None,
            operator_detail_status=str(payload.get("operator_detail_status", "")),
            uncertainty_reason=(
                UncertainWriteReason(str(uncertainty_reason))
                if uncertainty_reason is not None else None
            ),
            returned_id_count=returned_id_count,
        )
