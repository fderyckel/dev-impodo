"""Partition read domains by encoded size while retaining every search condition."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from sys import maxsize
from typing import Any

from impodo.domain.odoo.contracts import ConnectorRequestSizeError, RecordRequest
from impodo.domain.shared.models import canonical_json_bytes


@dataclass(frozen=True, slots=True)
class _Expression:
    operator: str = ""
    children: tuple[_Expression, ...] = ()
    term: tuple[Any, ...] = ()


def bounded_record_requests(
    request: RecordRequest,
    *,
    context: Mapping[str, Any],
    page_size: int,
    maximum_bytes: int,
) -> tuple[RecordRequest, ...]:
    """Split positive OR/IN searches, retaining AND scopes and pagination limits.

    External-ID reads keep their separate alias-to-record verification contract.
    Negated searches and a single oversized condition cannot be partitioned.
    Every partition is validated before the first business read is dispatched.
    """

    if request.external_ids:
        return (request,)

    def fits(domain: tuple[Any, ...]) -> bool:
        # Reserve a full machine-sized pagination offset rather than fitting
        # only the first page. JSON encoding measures UTF-8 bytes, not characters.
        payload = {
            "domain": domain,
            "fields": tuple(dict.fromkeys(("id", *request.fields))),
            "offset": maxsize,
            "limit": min(page_size, request.limit) if request.limit else page_size,
            "order": "id asc",
            "context": dict(context),
        }
        return len(canonical_json_bytes(payload)) <= maximum_bytes

    if fits(request.domain):
        return (request,)
    pending = [_parse(request.domain)]
    result = []
    while pending:
        expression = pending.pop()
        domain = _tokens(expression)
        if fits(domain):
            result.append(replace(request, domain=domain))
            continue
        parts = _partition(expression)
        if parts is None:
            raise ConnectorRequestSizeError(
                "The read-only comparison request exceeds the safe size limit "
                "and cannot be split safely."
            )
        pending.extend(reversed(parts))
    return tuple(result)


def _combine(operator: str, children: tuple[_Expression, ...]) -> _Expression:
    """Flatten associative operators without changing operand order."""

    flattened = tuple(
        child
        for item in children
        for child in (item.children if item.operator == operator else (item,))
    )
    return flattened[0] if len(flattened) == 1 else _Expression(operator, flattened)


def _parse(domain: tuple[Any, ...]) -> _Expression:
    """Parse prefix operators and Odoo's implicit top-level conjunction."""

    stack: list[_Expression] = []
    for token in reversed(domain):
        if isinstance(token, str) and token in {"&", "|", "!"}:
            width = 1 if token == "!" else 2
            if len(stack) < width:
                raise ConnectorRequestSizeError(
                    "The oversized read domain cannot be partitioned safely."
                )
            children = tuple(stack.pop() for _ in range(width))
            stack.append(
                _Expression(token, children)
                if token == "!"
                else _combine(token, children)
            )
        elif isinstance(token, (list, tuple)) and len(token) == 3:
            stack.append(_Expression(term=tuple(token)))
        else:
            raise ConnectorRequestSizeError(
                "The oversized read domain cannot be partitioned safely."
            )
    if not stack:
        raise ConnectorRequestSizeError(
            "The read request envelope exceeds the safe size limit."
        )
    return _combine("&", tuple(reversed(stack)))


def _tokens(expression: _Expression) -> tuple[Any, ...]:
    """Serialize iteratively so long prefix domains do not consume Python's stack."""

    result: list[Any] = []
    pending = [expression]
    while pending:
        item = pending.pop()
        if not item.operator:
            result.append(list(item.term))
        else:
            result.extend(
                [item.operator]
                * (1 if item.operator == "!" else len(item.children) - 1)
            )
            pending.extend(reversed(item.children))
    return tuple(result)


def _partition(expression: _Expression) -> tuple[_Expression, _Expression] | None:
    """Distribute one positive alternative while retaining all enclosing scopes."""

    if expression.operator == "|":
        middle = len(expression.children) // 2
        return (
            _combine("|", expression.children[:middle]),
            _combine("|", expression.children[middle:]),
        )
    if expression.operator == "&":
        indexes = sorted(
            range(len(expression.children)),
            key=lambda i: len(canonical_json_bytes(_tokens(expression.children[i]))),
            reverse=True,
        )
        for index in indexes:
            parts = _partition(expression.children[index])
            if parts is not None:
                before, after = (
                    expression.children[:index],
                    expression.children[index + 1 :],
                )
                return (
                    _combine("&", (*before, parts[0], *after)),
                    _combine("&", (*before, parts[1], *after)),
                )
    if not expression.operator and expression.term[1] == "in":
        values = expression.term[2]
        if isinstance(values, (tuple, list)) and len(values) > 1:
            middle = len(values) // 2
            return (
                _Expression(term=(*expression.term[:2], values[:middle])),
                _Expression(term=(*expression.term[:2], values[middle:])),
            )
    return None
