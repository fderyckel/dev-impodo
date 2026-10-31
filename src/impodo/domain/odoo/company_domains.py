"""Interpret only Odoo's closed company-domain expressions, without eval.

See docs/developer/contracts/preflight.md.
"""

from __future__ import annotations

import ast


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
