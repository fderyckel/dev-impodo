"""Exact comparisons for Odoo JSON-2 search_read business keys."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

_CATEGORY_PATH_SEPARATOR = " / "
_MAX_NESTED_CATEGORY_CANDIDATES = 500


def bounded_lookup_query(
    model: str,
    domains: Sequence[tuple[tuple[str, str, Any], ...]],
) -> tuple[list[Any], int]:
    """Build a bounded candidate search for already authorized exact keys."""

    combined: list[Any] = ["|"] * (len(domains) - 1)
    nested_categories = False
    for domain in domains:
        combined.extend(["&"] * (len(domain) - 1))
        category_key = model == "product.category" and any(
            field == "name" and operator == "=" and isinstance(value, str) and value
            for field, operator, value in domain
        )
        for field, operator, value in domain:
            condition = [field, operator, value]
            if (
                category_key and field == "parent_id" and operator == "="
                and isinstance(value, str) and _CATEGORY_PATH_SEPARATOR in value
            ):
                # Odoo 20 may search a many2one label by its bare name.
                # Any suffix could be the actual name, including a literal
                # separator. Callers still compare the full returned label.
                parts = value.split(_CATEGORY_PATH_SEPARATOR)
                parent_names = [
                    _CATEGORY_PATH_SEPARATOR.join(parts[index:])
                    for index in range(len(parts))
                ]
                combined.extend(("|", condition, ["parent_id.name", "in", parent_names]))
                nested_categories = True
            else:
                combined.append(condition)
    limit = 2 * len(domains)
    if nested_categories:
        limit = max(limit, _MAX_NESTED_CATEGORY_CANDIDATES)
    return combined, limit


def lookup_values_equal(actual: object, expected: object) -> bool:
    """Compare a reviewed scalar with Odoo's scalar or many2one pair."""

    if expected is None and actual is False:
        return True
    if (
        isinstance(actual, (list, tuple))
        and len(actual) == 2
        and type(actual[0]) is int
    ):
        # JSON-2 represents many2one values as [id, display_name].
        if type(expected) is str:
            return actual[1] == expected
        if type(expected) is int:
            return actual[0] == expected
    return actual == expected
