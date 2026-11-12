"""Interpret only Odoo's closed company-domain expressions, without eval.

See docs/developer/contracts/preflight.md.
"""

from __future__ import annotations

import ast
import re

_LOOKUP_FIELD = re.compile(r"[a-zA-Z][a-zA-Z0-9_]{0,127}")
_LOOKUP_OPERATORS = {"=", "!=", "in", "not in", "parent_of", "child_of", "like", "ilike", "=like", "=ilike", ">", ">=", "<", "<="}


def linked_value_domain(expression: object, company_id: int) -> list[object] | None:
    """Interpret a closed field domain using only the selected company and literals.

    Unlike the company-only compatibility probe, this preserves the complete
    field domain, including a payable/receivable type restriction. Unsupported
    dynamic expressions remain unqualified; callers must not read choices.
    """
    if type(company_id) is not int or company_id <= 0:
        return None
    try:
        if isinstance(expression, str):
            if len(expression) > 8192:
                return None
            root = ast.parse(expression, mode="eval").body
            if sum(1 for _ in ast.walk(root)) > 256:
                return None
            result = _value(root, company_id)
        else:
            result = expression
        if not isinstance(result, (list, tuple)) or len(result) > 256:
            return None
        remaining = 0
        for item in reversed(result):
            if isinstance(item, str):
                arity = {"|": 2, "&": 2, "!": 1}.get(item)
                if arity is None or remaining < arity:
                    return None
                remaining += 1 - arity
            elif (
                isinstance(item, (list, tuple)) and len(item) == 3
                and isinstance(item[0], str) and _LOOKUP_FIELD.fullmatch(item[0])
                and isinstance(item[1], str) and item[1] in _LOOKUP_OPERATORS
                and _lookup_literal(item[2])
            ):
                remaining += 1
            else:
                return None
        return list(result)
    except (ValueError, SyntaxError, RecursionError, TypeError):
        return None


def _lookup_literal(value: object) -> bool:
    if value is None or type(value) in {str, int, bool, float}:
        return True
    return isinstance(value, (list, tuple)) and len(value) <= 1000 and all(
        item is None or type(item) in {str, int, bool, float} for item in value
    )


def operation_company_domain(
    expression: object, company_id: int
) -> list[object] | None:
    """Read the company prefix emitted by fields_get for company-dependent links.

    Unknown expressions remain unqualified. Calls, attributes, arbitrary names
    and non-company predicates are never evaluated or forwarded.
    """
    if not isinstance(expression, str) or len(expression) > 8192:
        return None
    try:
        root = ast.parse(expression, mode="eval").body
        if sum(1 for _ in ast.walk(root)) > 256:
            return None
        prefix = (
            root.left
            if isinstance(root, ast.BinOp) and isinstance(root.op, ast.Add)
            else root
        )
        result = _value(prefix, company_id)
        if not isinstance(result, list) or not result:
            return None
        for item in result:
            if isinstance(item, str) and item in {"|", "&", "!"}:
                continue
            if (
                not isinstance(item, (list, tuple))
                or len(item) != 3
                or item[0] not in {"company_id", "company_ids"}
                or item[1] not in {"=", "!=", "in", "not in", "parent_of", "child_of"}
            ):
                return None
        return result
    except (ValueError, SyntaxError, RecursionError, TypeError):
        return None


def _value(node: ast.AST, company_id: int) -> object:
    if isinstance(node, ast.Constant) and (
        node.value is None or type(node.value) in {str, int, bool}
    ):
        return node.value
    if isinstance(node, (ast.List, ast.Tuple)):
        return [_value(item, company_id) for item in node.elts]
    if (
        isinstance(node, ast.Subscript)
        and isinstance(node.value, ast.Name)
        and node.value.id == "allowed_company_ids"
        and isinstance(node.slice, ast.Constant)
        and type(node.slice.value) is int
        and node.slice.value == 0
    ):
        return company_id
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _value(node.left, company_id), _value(node.right, company_id)
        if isinstance(left, list) and isinstance(right, list):
            return left + right
    raise ValueError("Unsupported company-domain expression")
