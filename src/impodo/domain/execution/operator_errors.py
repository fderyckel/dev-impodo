"""Bound private Odoo explanations separately from value-free load journals."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from hashlib import sha256
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from .models import ExecutionRowAttempt

MAX_DETAIL_BYTES = 256 * 1024
MAX_RECEIPT_BYTES = 512 * 1024
_UNSAFE = re.compile(
    r"Traceback|\bSELECT\b.+\bFROM\b|\bINSERT\s+INTO\b|\bUPDATE\b.+\bSET\b|"
    r"https?://|/(?:Users|home|var|etc|private|opt|srv|tmp)/|[A-Za-z]:\\|"
    r"(?:^|\n)\s*(?:DETAIL|CONTEXT|STATEMENT|LINE\s+\d+)\s*:|"
    r"(?:authorization|password|api[_ -]?key|token)\s*[:=]",
    re.IGNORECASE | re.DOTALL,
)
_SECRET_FIELD = re.compile(
    r"password|passwd|token|secret|api[_-]?key|credential", re.IGNORECASE
)


def _bounded(value: str, limit: int) -> str:
    return value.encode("utf-8")[:limit].decode("utf-8", errors="ignore")


@dataclass(frozen=True, slots=True, repr=False)
class OperatorErrorMessage:
    """One escaped-at-render user explanation with validated attribution."""

    text: str
    fields: tuple[str, ...] = ()
    row_from: int | None = None
    row_to: int | None = None


@dataclass(frozen=True, slots=True)
class OperatorErrorDetail:
    """Versioned private receipt; repr never contains explanation or row values."""

    messages: tuple[OperatorErrorMessage, ...]
    submitted: tuple[dict[str, str], ...]
    truncated: bool = False
    withheld: bool = False
    contract_version: int = 1

    def __repr__(self) -> str:
        return "OperatorErrorDetail(private=True)"

    def to_json(self) -> str:
        """Serialize only this bounded, current private-detail contract."""
        if (
            self.contract_version != 1
            or type(self.truncated) is not bool
            or type(self.withheld) is not bool
            or not 1 <= len(self.submitted) <= 1000
            or len(self.messages) > 20
            or any(
                not isinstance(k, str) or not isinstance(v, str)
                for row in self.submitted
                for k, v in row.items()
            )
            or any(
                not isinstance(m.text, str)
                or len(m.text.encode()) > 8192
                or any(not isinstance(f, str) or not f for f in m.fields)
                or (
                    (m.row_from is not None or m.row_to is not None)
                    and (
                        type(m.row_from) is not int
                        or type(m.row_to) is not int
                        or not 0 <= m.row_from <= m.row_to < len(self.submitted)
                    )
                )
                for m in self.messages
            )
        ):
            raise ValueError("Private Odoo error detail is invalid")
        value = json.dumps(asdict(self), ensure_ascii=False, sort_keys=True)
        if self.contract_version != 1 or len(value.encode()) > MAX_DETAIL_BYTES:
            raise ValueError("Private Odoo error detail is invalid")
        return value

    @classmethod
    def from_json(cls, value: str) -> OperatorErrorDetail:
        """Reject other contracts rather than interpreting arbitrary saved text."""
        if len(value.encode()) > MAX_DETAIL_BYTES:
            raise ValueError("Private Odoo error detail exceeds its limit")
        payload = json.loads(value)
        if payload.get("contract_version") != 1:
            raise ValueError("Private Odoo error detail requires fresh capture")
        result = cls(
            messages=tuple(
                OperatorErrorMessage(
                    text=item["text"],
                    fields=tuple(item["fields"]),
                    row_from=item["row_from"],
                    row_to=item["row_to"],
                )
                for item in payload["messages"]
            ),
            submitted=tuple(dict(row) for row in payload["submitted"]),
            truncated=payload["truncated"],
            withheld=payload["withheld"],
        )
        if (
            not 1 <= len(result.submitted) <= 1000
            or len(result.messages) > 20
            or any(
                not isinstance(m.text, str) or len(m.text.encode()) > 8192
                for m in result.messages
            )
            or any(
                not isinstance(k, str) or not isinstance(v, str)
                for row in result.submitted
                for k, v in row.items()
            )
        ):
            raise ValueError("Private Odoo error detail is invalid")
        result.to_json()
        return result

    def redact(self, secrets: Sequence[str]) -> OperatorErrorDetail:
        """Hide known protected inputs without copying them into an exception."""
        clipped = False

        def clean(text: str, limit: int) -> str:
            nonlocal clipped
            for secret in secrets:
                if secret:
                    text = text.replace(secret, "[protected]")
            bounded = _bounded(text, limit)
            clipped = clipped or bounded != text
            return bounded

        messages = tuple(replace(m, text=clean(m.text, 8192)) for m in self.messages)
        submitted = tuple(
            {k: clean(v, 2048) for k, v in row.items()} for row in self.submitted
        )
        return replace(
            self,
            messages=messages,
            submitted=submitted,
            truncated=self.truncated or clipped,
        )


def capture_operator_error(
    messages: object,
    values: Sequence[Mapping[str, Any]],
    *,
    secrets: Sequence[str] = (),
) -> OperatorErrorDetail:
    """Keep user messages and actual submitted values, never a raw response body."""
    items = messages if isinstance(messages, list) else []
    protected = tuple(
        str(v)
        for row in values
        for k, v in row.items()
        if _SECRET_FIELD.search(k) and v is not None
    )

    def redact(text: str) -> str:
        for secret in (*secrets, *protected):
            if secret:
                text = text.replace(secret, "[protected]")
        return text

    permitted = {name.split("/", 1)[0] for row in values for name in row}
    captured: list[OperatorErrorMessage] = []
    truncated, withheld = len(items) > 20, False
    for item in items[:20]:
        if not isinstance(item, Mapping) or item.get("type") != "error":
            continue
        text = item.get("message")
        if not isinstance(text, str) or not text:
            continue
        text = redact(text)
        if _UNSAFE.search(text):
            withheld = True
            continue
        bounded = _bounded(text, 8192)
        truncated = truncated or text != bounded
        field = item.get("field")
        if isinstance(field, (tuple, list)):
            field = field[0] if field else None
        fields = (
            (field.split("/", 1)[0],)
            if isinstance(field, str) and field.split("/", 1)[0] in permitted
            else ()
        )
        rows = item.get("rows")
        start, end = (
            (rows.get("from"), rows.get("to"))
            if isinstance(rows, Mapping)
            else (None, None)
        )
        if (
            type(start) is not int
            or type(end) is not int
            or not 0 <= start <= end < len(values)
        ):
            start, end = None, None
        captured.append(OperatorErrorMessage(bounded, fields, start, end))
    submitted: list[dict[str, str]] = []
    budget = 64 * 1024
    for row in values:
        output: dict[str, str] = {}
        for name, value in row.items():
            text = (
                "[protected]"
                if _SECRET_FIELD.search(name)
                else json.dumps(value, ensure_ascii=False)
            )
            if isinstance(value, str) and not _SECRET_FIELD.search(name):
                text = value
            text = redact(text)
            bounded = _bounded(text, min(2048, max(0, budget)))
            truncated = truncated or text != bounded
            if not bounded and text:
                continue
            output[name] = bounded
            budget -= len(name.encode()) + len(bounded.encode()) + 8
        submitted.append(output)
    detail = OperatorErrorDetail(
        tuple(captured), tuple(submitted), truncated, withheld
    ).redact((*secrets, *protected))
    detail.to_json()
    return detail


@dataclass(frozen=True, slots=True, repr=False)
class OperatorErrorReceipt:
    """Bind a private explanation to one exact saved transport membership."""

    snapshot_hash: str
    row_ids: tuple[str, ...]
    detail: OperatorErrorDetail

    def to_json(self) -> str:
        """Retain membership independently of future mapping or source edits."""
        if (
            len(self.row_ids) != len(self.detail.submitted)
            or len(set(self.row_ids)) != len(self.row_ids)
            or any(
                not isinstance(row_id, str) or not 1 <= len(row_id) <= 256
                for row_id in self.row_ids
            )
        ):
            raise ValueError("Private Odoo error membership is invalid")
        value = json.dumps(
            {
                "snapshot_hash": self.snapshot_hash,
                "row_ids": self.row_ids,
                "detail": self.detail.to_json(),
            },
            sort_keys=True,
        )
        if len(value.encode()) > MAX_RECEIPT_BYTES:
            raise ValueError("Private Odoo error receipt exceeds its limit")
        return value

    @property
    def content_hash(self) -> str:
        """Bind private content without including it in immutable execution hashes."""
        return "sha256:" + sha256(self.to_json().encode()).hexdigest()


class OperatorErrorPersistenceError(RuntimeError):
    """Private capture failed; the definitive Odoo outcome still needs saving."""


@runtime_checkable
class OperatorErrorPageRepository(Protocol):
    """Bound row navigation without loading a full execution run."""

    def operator_error_rows(
        self, workspace_id: str, run_id: str | None, *, after: int
    ) -> tuple[str, tuple[tuple[int, ExecutionRowAttempt], ...]] | None: ...


@runtime_checkable
class OperatorErrorRepository(Protocol):
    """Optional private companion to the journal, never a second load tracker."""

    def record_outcomes_with_detail(
        self,
        workspace_id: str,
        run_id: str,
        rows: Sequence[ExecutionRowAttempt],
        detail: OperatorErrorDetail,
    ) -> None: ...

    def operator_error_for_row(
        self, workspace_id: str, run_id: str, row_id: str
    ) -> tuple[ExecutionRowAttempt, OperatorErrorReceipt | None] | None: ...
