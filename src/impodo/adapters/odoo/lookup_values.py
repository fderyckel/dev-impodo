"""Exact comparisons for Odoo JSON-2 search_read business keys."""

from __future__ import annotations


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
