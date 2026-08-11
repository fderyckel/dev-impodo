"""Bounded, user-authored scalar transformations and validation rules.

The browser exposes these policies with plain-language controls.  Formula and
custom-pattern inputs remain deterministic expressions: they cannot import
code, access attributes, read files, use the network, or call Odoo.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from decimal import (
    Decimal,
    ROUND_CEILING,
    ROUND_DOWN,
    ROUND_FLOOR,
    ROUND_HALF_EVEN,
    ROUND_HALF_UP,
    ROUND_UP,
)
from functools import lru_cache
import operator
import re
from typing import Any, Callable, Mapping


MAX_PATTERN_LENGTH = 500
MAX_FORMULA_LENGTH = 1_000
MAX_FORMULA_NODES = 100
MAX_RULE_TEXT_LENGTH = 10_000
MAX_RULE_OUTPUT_LENGTH = 1_000_000
MAX_RULE_SIZE = 10_000
MAX_TEXT_TRANSFORM_STEPS = 20

CASE_MODES = frozenset(
    {"preserve", "uppercase", "lowercase", "sentence", "title"}
)
SEARCH_MODES = frozenset({"literal", "starts_with", "ends_with", "pattern"})
TEXT_TRANSFORM_KINDS = frozenset(
    {"find_replace", "remove_separators_between_digits"}
)
ROUNDING_MODES = frozenset(
    {"half_up", "half_even", "up", "down", "ceiling", "floor"}
)
SEGMENT_LOCATIONS = frozenset({"none", "entire", "first", "last"})
CHARACTER_CLASSES = frozenset({"none", "digits", "uppercase", "lowercase"})

_ROUNDING = {
    "half_up": ROUND_HALF_UP,
    "half_even": ROUND_HALF_EVEN,
    "up": ROUND_UP,
    "down": ROUND_DOWN,
    "ceiling": ROUND_CEILING,
    "floor": ROUND_FLOOR,
}
_RISKY_REPETITION = re.compile(
    r"\((?:[^()\\]|\\.)*[+*](?:[^()\\]|\\.)*\)(?:[+*]|\{)"
)
_RISKY_ALTERNATION = re.compile(
    r"\((?:[^()\\]|\\.)*\|(?:[^()\\]|\\.)*\)(?:[+*]|\{)"
)
_BACK_REFERENCE = re.compile(r"\\[1-9]|\(\?P=|\(\?\(")
_ASCII_CLASSES = {
    "digits": re.compile(r"^[0-9]+$"),
    "uppercase": re.compile(r"^[A-Z]+$"),
    "lowercase": re.compile(r"^[a-z]+$"),
}


@dataclass(frozen=True, slots=True)
class TextTransformStep:
    """One bounded text change in an explicitly ordered sequence."""

    kind: str = "find_replace"
    search_value: str = ""
    replacement_value: str = ""
    search_mode: str = "literal"
    replace_all: bool = True
    characters: str = ""

    def __post_init__(self) -> None:
        if self.kind not in TEXT_TRANSFORM_KINDS:
            raise ValueError("The text-change kind is unsupported")
        if len(self.search_value) > MAX_PATTERN_LENGTH:
            raise ValueError(
                f"Find text is limited to {MAX_PATTERN_LENGTH} characters"
            )
        if len(self.replacement_value) > MAX_RULE_SIZE:
            raise ValueError(
                f"Replacement text is limited to {MAX_RULE_SIZE} characters"
            )
        if len(self.characters) > 50:
            raise ValueError("Separator cleanup is limited to 50 characters")

    @property
    def configured(self) -> bool:
        if self.kind == "remove_separators_between_digits":
            return bool(self.characters)
        return bool(self.search_value)


@dataclass(frozen=True, slots=True)
class ScalarTransformPolicy:
    """Guided transformations applied in one documented fixed order."""

    trim: bool = False
    collapse_whitespace: bool = False
    empty_as_null: bool = False
    case_mode: str = "preserve"
    decimal_locale: str = "invariant"
    date_format: str = "iso"
    timezone: str = "UTC"
    search_value: str = ""
    replacement_value: str = ""
    search_mode: str = "literal"
    replace_all: bool = True
    decimal_places: int | None = None
    rounding_mode: str = "half_up"
    formula: str = ""
    text_steps: tuple[TextTransformStep, ...] = ()

    def __post_init__(self) -> None:
        if len(self.search_value) > MAX_PATTERN_LENGTH:
            raise ValueError(
                f"Find text is limited to {MAX_PATTERN_LENGTH} characters"
            )
        if len(self.replacement_value) > MAX_RULE_SIZE:
            raise ValueError(
                f"Replacement text is limited to {MAX_RULE_SIZE} characters"
            )
        if len(self.formula) > MAX_FORMULA_LENGTH:
            raise ValueError(
                f"Formulas are limited to {MAX_FORMULA_LENGTH} characters"
            )
        object.__setattr__(self, "text_steps", tuple(self.text_steps))
        if len(self.text_steps) > MAX_TEXT_TRANSFORM_STEPS:
            raise ValueError(
                "A field cannot contain more than "
                f"{MAX_TEXT_TRANSFORM_STEPS} text changes"
            )
        if self.text_steps and (
            self.search_value
            or self.replacement_value
            or self.search_mode != "literal"
            or not self.replace_all
        ):
            raise ValueError(
                "Ordered text changes cannot be combined with the legacy "
                "find-and-replace fields"
            )
        if not self.text_steps and (self.search_value or self.replacement_value):
            object.__setattr__(
                self,
                "text_steps",
                (
                    TextTransformStep(
                        search_value=self.search_value,
                        replacement_value=self.replacement_value,
                        search_mode=self.search_mode,
                        replace_all=self.replace_all,
                    ),
                ),
            )
            object.__setattr__(self, "search_value", "")
            object.__setattr__(self, "replacement_value", "")
            object.__setattr__(self, "search_mode", "literal")
            object.__setattr__(self, "replace_all", True)

    @property
    def effective_text_steps(self) -> tuple[TextTransformStep, ...]:
        """Return ordered rules, adapting the legacy single-rule contract."""

        return self.text_steps

    @property
    def configured_text_steps(self) -> tuple[TextTransformStep, ...]:
        """Return only executable steps while retaining authored order."""

        return tuple(
            step for step in self.effective_text_steps if step.configured
        )


@dataclass(frozen=True, slots=True)
class ScalarValidationPolicy:
    """Plain-language checks for the final proposed scalar value."""

    exact_length: int | None = None
    segment_location: str = "none"
    segment_length: int | None = None
    character_class: str = "none"
    pattern: str = ""

    def __post_init__(self) -> None:
        if len(self.pattern) > MAX_PATTERN_LENGTH:
            raise ValueError(
                f"Custom patterns are limited to {MAX_PATTERN_LENGTH} characters"
            )

    @property
    def configured(self) -> bool:
        """Return whether any final-value validation check is enabled."""

        return bool(
            self.exact_length is not None
            or self.segment_location != "none"
            or self.character_class != "none"
            or self.pattern
        )


class ScalarRuleError(ValueError):
    """A row value could not safely satisfy an authored rule."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@lru_cache(maxsize=256)
def validate_pattern(pattern: str) -> re.Pattern[str]:
    """Compile a bounded custom pattern and reject known ReDoS constructs."""

    if len(pattern) > MAX_PATTERN_LENGTH:
        raise ValueError(
            f"Custom patterns are limited to {MAX_PATTERN_LENGTH} characters"
        )
    if _BACK_REFERENCE.search(pattern):
        raise ValueError(
            "Back-references and conditional pattern branches are not supported"
        )
    if _RISKY_REPETITION.search(pattern):
        raise ValueError("Nested repeating pattern groups are not supported")
    if _RISKY_ALTERNATION.search(pattern):
        raise ValueError("Repeating alternative pattern groups are not supported")
    try:
        compiled = re.compile(pattern)
    except re.error as error:
        raise ValueError(f"Custom pattern is invalid: {error}") from error
    if compiled.groups > 50:
        raise ValueError("Custom patterns are limited to 50 capture groups")
    return compiled


def validate_formula(
    expression: str,
    *,
    allowed_names: set[str] | None = None,
) -> ast.Expression:
    """Parse the safe formula language without executing it."""

    return _validate_formula(
        expression,
        tuple(sorted(allowed_names or ())),
    )


@lru_cache(maxsize=256)
def _validate_formula(
    expression: str,
    allowed_names: tuple[str, ...],
) -> ast.Expression:
    """Cache structural validation shared by rows using the same formula."""

    if len(expression) > MAX_FORMULA_LENGTH:
        raise ValueError(
            f"Formulas are limited to {MAX_FORMULA_LENGTH} characters"
        )
    try:
        parsed = ast.parse(expression, mode="eval")
    except SyntaxError as error:
        raise ValueError("Formula syntax is invalid") from error
    nodes = tuple(ast.walk(parsed))
    if len(nodes) > MAX_FORMULA_NODES:
        raise ValueError("Formula is too complex")
    permitted = (
        ast.Expression,
        ast.Constant,
        ast.Name,
        ast.Load,
        ast.BinOp,
        ast.Add,
        ast.Sub,
        ast.Mult,
        ast.Div,
        ast.Mod,
        ast.UnaryOp,
        ast.UAdd,
        ast.USub,
        ast.Not,
        ast.BoolOp,
        ast.And,
        ast.Or,
        ast.Compare,
        ast.Eq,
        ast.NotEq,
        ast.Lt,
        ast.LtE,
        ast.Gt,
        ast.GtE,
        ast.IfExp,
        ast.Call,
    )
    if any(not isinstance(node, permitted) for node in nodes):
        raise ValueError(
            "Formula uses an unsupported operation; use values, arithmetic, "
            "comparisons, or the listed helper functions"
        )
    function_names = frozenset(
        {"concat", "coalesce", "substring", "upper", "lower", "strip", "length", "abs"}
    )
    valid_names = {"value", *function_names}
    valid_names.update(allowed_names)
    unknown = sorted(
        {
            node.id
            for node in nodes
            if isinstance(node, ast.Name) and node.id not in valid_names
        }
    )
    if unknown:
        raise ValueError(f"Formula uses unknown value {unknown[0]!r}")
    for node in nodes:
        if isinstance(node, ast.Call) and (
            not isinstance(node.func, ast.Name) or node.func.id not in function_names
        ):
            raise ValueError("Formula calls an unsupported function")
        if isinstance(node, ast.Call) and node.keywords:
            raise ValueError("Formula helper functions do not accept named arguments")
    return parsed


def prepare_rule_text(
    raw_value: Any,
    policy: ScalarTransformPolicy,
    *,
    formula_context: Mapping[str, Any] | None = None,
    find_replace_observer: Callable[[bool, bool], None] | None = None,
    text_step_observer: Callable[[int, bool, bool], None] | None = None,
) -> str | None:
    """Apply formula, normalization, replacement, and casing to raw input."""

    value = raw_value
    if policy.formula.strip():
        context = dict(formula_context or {})
        context.setdefault("value", raw_value)
        try:
            value = evaluate_formula(policy.formula, context)
        except (ArithmeticError, TypeError, ValueError) as error:
            raise ScalarRuleError(
                "SOURCE_FORMULA_INVALID",
                f"Formula could not calculate this value: {error}",
            ) from error
    if value is None:
        return None
    rendered = str(value)
    if policy.trim:
        rendered = rendered.strip()
    if policy.collapse_whitespace:
        rendered = re.sub(r"\s+", " ", rendered)
    for step_index, step in enumerate(policy.effective_text_steps):
        if not step.configured:
            continue
        if (
            step.kind == "find_replace"
            and step.search_mode == "pattern"
            and len(rendered) > MAX_RULE_TEXT_LENGTH
        ):
            raise ScalarRuleError(
                "SOURCE_PATTERN_INPUT_TOO_LONG",
                "The value is too long for an advanced find pattern",
            )
        before_replacement = rendered
        rendered = _replace_text(rendered, step)
        if before_replacement != "":
            matched = _text_step_matches(before_replacement, step)
            changed = rendered != before_replacement
            if find_replace_observer is not None:
                find_replace_observer(matched, changed)
            if text_step_observer is not None:
                text_step_observer(step_index, matched, changed)
        if len(rendered) > MAX_RULE_OUTPUT_LENGTH:
            raise ScalarRuleError(
                "SOURCE_RULE_OUTPUT_TOO_LONG",
                (
                    "A value rule produced more than "
                    f"{MAX_RULE_OUTPUT_LENGTH} characters"
                ),
            )
    if policy.case_mode == "uppercase":
        rendered = rendered.upper()
    elif policy.case_mode == "lowercase":
        rendered = rendered.lower()
    elif policy.case_mode == "sentence":
        rendered = _sentence_case(rendered)
    elif policy.case_mode == "title":
        rendered = rendered.title()
    if (
        (policy.configured_text_steps or policy.formula)
        and len(rendered) > MAX_RULE_OUTPUT_LENGTH
    ):
        raise ScalarRuleError(
            "SOURCE_RULE_OUTPUT_TOO_LONG",
            (
                "A value rule produced more than "
                f"{MAX_RULE_OUTPUT_LENGTH} characters"
            ),
        )
    if policy.empty_as_null and rendered == "":
        return None
    return rendered


def round_decimal_value(value: Decimal, policy: ScalarTransformPolicy) -> Decimal:
    """Apply explicit base-10 rounding after decimal parsing."""

    if policy.decimal_places is None:
        return value
    quantum = Decimal(1).scaleb(-policy.decimal_places)
    try:
        return value.quantize(quantum, rounding=_ROUNDING[policy.rounding_mode])
    except (ArithmeticError, KeyError) as error:
        raise ScalarRuleError(
            "SOURCE_DECIMAL_ROUNDING_INVALID",
            "Decimal rounding could not be applied to this value",
        ) from error


def validate_scalar_value(
    value: Any,
    policy: ScalarValidationPolicy,
) -> None:
    """Evaluate the final string value without exposing it in error text."""

    if value is None or not policy.configured:
        return
    if not isinstance(value, str):
        raise ScalarRuleError(
            "SOURCE_VALUE_RULE_INVALID",
            "Text validation rules require a string value",
        )
    if policy.exact_length is not None and len(value) != policy.exact_length:
        raise ScalarRuleError(
            "SOURCE_TEXT_LENGTH_INVALID",
            (
                f"Expected exactly {policy.exact_length} characters; "
                f"found {len(value)}"
            ),
        )
    if policy.character_class != "none":
        segment = value
        if policy.segment_location in {"first", "last"}:
            assert policy.segment_length is not None
            if len(value) < policy.segment_length:
                raise ScalarRuleError(
                    "SOURCE_TEXT_SEGMENT_INVALID",
                    (
                        f"Expected at least {policy.segment_length} characters "
                        f"to check the {policy.segment_location} part"
                    ),
                )
            segment = (
                value[: policy.segment_length]
                if policy.segment_location == "first"
                else value[-policy.segment_length :]
            )
        matcher = _ASCII_CLASSES[policy.character_class]
        if matcher.fullmatch(segment) is None:
            label = {
                "digits": "digits 0-9",
                "uppercase": "capital letters A-Z",
                "lowercase": "lowercase letters a-z",
            }[policy.character_class]
            area = {
                "entire": "The whole value",
                "first": f"The first {policy.segment_length} characters",
                "last": f"The last {policy.segment_length} characters",
            }[policy.segment_location]
            raise ScalarRuleError(
                "SOURCE_TEXT_SEGMENT_INVALID",
                f"{area} must contain only {label}",
            )
    if policy.pattern:
        if len(value) > MAX_RULE_TEXT_LENGTH:
            raise ScalarRuleError(
                "SOURCE_PATTERN_INPUT_TOO_LONG",
                "The value is too long for a custom-pattern check",
            )
        matcher = validate_pattern(policy.pattern)
        if matcher.fullmatch(value) is None:
            raise ScalarRuleError(
                "SOURCE_PATTERN_MISMATCH",
                "The value does not match the configured custom pattern",
            )


def evaluate_formula(expression: str, context: Mapping[str, Any]) -> Any:
    """Evaluate a validated expression against one bounded row context."""

    parsed = validate_formula(expression, allowed_names=set(context))
    prepared = {name: _formula_value(value) for name, value in context.items()}
    result = _eval_node(parsed.body, prepared)
    if len(str(result)) > MAX_RULE_TEXT_LENGTH:
        raise ValueError("formula result is too long")
    return result


def _replace_text(value: str, step: TextTransformStep) -> str:
    if step.kind == "remove_separators_between_digits":
        return _remove_separators_between_digits(value, step.characters)
    if step.search_mode == "literal":
        count = -1 if step.replace_all else 1
        return value.replace(step.search_value, step.replacement_value, count)
    if step.search_mode == "starts_with":
        return (
            f"{step.replacement_value}{value[len(step.search_value):]}"
            if value.startswith(step.search_value)
            else value
        )
    if step.search_mode == "ends_with":
        return (
            f"{value[:-len(step.search_value)]}{step.replacement_value}"
            if value.endswith(step.search_value)
            else value
        )
    try:
        matcher = validate_pattern(step.search_value)
        return matcher.sub(
            step.replacement_value,
            value,
            count=0 if step.replace_all else 1,
        )
    except (re.error, ValueError) as error:
        raise ScalarRuleError(
            "SOURCE_REPLACEMENT_INVALID",
            f"Find and replace could not run safely: {error}",
        ) from error


def _text_step_matches(value: str, step: TextTransformStep) -> bool:
    if step.kind == "remove_separators_between_digits":
        return _remove_separators_between_digits(value, step.characters) != value
    if step.search_mode == "literal":
        return step.search_value in value
    if step.search_mode == "starts_with":
        return value.startswith(step.search_value)
    if step.search_mode == "ends_with":
        return value.endswith(step.search_value)
    return validate_pattern(step.search_value).search(value) is not None


def _remove_separators_between_digits(value: str, characters: str) -> str:
    separators = frozenset(characters)
    if not separators:
        return value
    result: list[str] = []
    index = 0
    while index < len(value):
        character = value[index]
        if character in separators and result and result[-1] in "0123456789":
            end = index + 1
            while end < len(value) and value[end] in separators:
                end += 1
            if end < len(value) and value[end] in "0123456789":
                index = end
                continue
        result.append(character)
        index += 1
    return "".join(result)


def _sentence_case(value: str) -> str:
    for index, character in enumerate(value):
        if character.isalpha():
            return f"{value[:index]}{character.upper()}{value[index + 1:]}"
    return value


def _formula_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, Decimal, int)):
        return value
    if isinstance(value, float):
        return Decimal(str(value))
    rendered = str(value)
    if re.fullmatch(r"[+-]?\d+(?:\.\d+)?", rendered.strip()):
        return Decimal(rendered.strip())
    return rendered


def _eval_node(node: ast.AST, context: Mapping[str, Any]) -> Any:
    if isinstance(node, ast.Constant):
        return _formula_value(node.value)
    if isinstance(node, ast.Name):
        return context[node.id]
    if isinstance(node, ast.BinOp):
        left = _eval_node(node.left, context)
        right = _eval_node(node.right, context)
        if isinstance(node.op, ast.Add) and isinstance(left, str) and isinstance(right, str):
            return left + right
        operations = {
            ast.Add: operator.add,
            ast.Sub: operator.sub,
            ast.Mult: operator.mul,
            ast.Div: operator.truediv,
            ast.Mod: operator.mod,
        }
        return operations[type(node.op)](_number(left), _number(right))
    if isinstance(node, ast.UnaryOp):
        value = _eval_node(node.operand, context)
        if isinstance(node.op, ast.Not):
            return not value
        return _number(value) if isinstance(node.op, ast.UAdd) else -_number(value)
    if isinstance(node, ast.BoolOp):
        values = [_eval_node(item, context) for item in node.values]
        return all(values) if isinstance(node.op, ast.And) else any(values)
    if isinstance(node, ast.Compare):
        left = _eval_node(node.left, context)
        for operation_node, comparator in zip(node.ops, node.comparators):
            right = _eval_node(comparator, context)
            comparison = {
                ast.Eq: operator.eq,
                ast.NotEq: operator.ne,
                ast.Lt: operator.lt,
                ast.LtE: operator.le,
                ast.Gt: operator.gt,
                ast.GtE: operator.ge,
            }[type(operation_node)]
            if not comparison(left, right):
                return False
            left = right
        return True
    if isinstance(node, ast.IfExp):
        branch = node.body if _eval_node(node.test, context) else node.orelse
        return _eval_node(branch, context)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        arguments = [_eval_node(item, context) for item in node.args]
        return _call_formula_function(node.func.id, arguments)
    raise ValueError("unsupported formula operation")


def _call_formula_function(name: str, arguments: list[Any]) -> Any:
    if name == "concat":
        return "".join("" if item is None else str(item) for item in arguments)
    if name == "coalesce":
        return next((item for item in arguments if item not in {None, ""}), None)
    if name == "substring":
        if len(arguments) not in {2, 3}:
            raise ValueError("substring expects value, start, and optional length")
        value = str(arguments[0])
        start = int(_number(arguments[1]))
        if len(arguments) == 2:
            return value[start:]
        length = int(_number(arguments[2]))
        if length < 0 or length > MAX_RULE_SIZE:
            raise ValueError("substring length is outside the safe range")
        return value[start : start + length]
    if len(arguments) != 1:
        raise ValueError(f"{name} expects one value")
    value = arguments[0]
    if name == "upper":
        return str(value).upper()
    if name == "lower":
        return str(value).lower()
    if name == "strip":
        return str(value).strip()
    if name == "length":
        return Decimal(len(str(value)))
    if name == "abs":
        return abs(_number(value))
    raise ValueError("unsupported formula helper")


def _number(value: Any) -> Decimal:
    prepared = _formula_value(value)
    if isinstance(prepared, bool) or not isinstance(prepared, (Decimal, int)):
        raise TypeError("arithmetic requires numbers")
    return Decimal(prepared)
