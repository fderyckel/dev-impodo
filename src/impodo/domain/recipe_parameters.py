"""Define editable parameter declarations for an authoring Recipe workspace.

Parameter declarations are reusable Recipe meaning. Their values are not:
each Test or Production DataVersion supplies and confirms its own values.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum

from .serialization import canonical_json, content_hash, portable

RECIPE_PARAMETER_DEFINITIONS_CONTRACT_VERSION = 1
MAX_RECIPE_PARAMETER_DEFINITIONS = 50
EXPORT_AS_OF_PARAMETER_ID = "parameter:export_as_of_date"

_PARAMETER_NAME = re.compile(r"[a-z][a-z0-9_]{0,79}\Z")


class RecipeParameterDefinitionError(ValueError):
    """Raised when authoring parameter meaning is not safe or portable."""


class RecipeParameterValueError(ValueError):
    """Raised when a run value does not satisfy its saved Recipe definition."""


class RecipeParameterType(StrEnum):
    STRING = "string"
    DATE = "date"
    INTEGER = "integer"
    DECIMAL = "decimal"


def _definition_mapping(value: object, label: str) -> dict[str, object]:
    """Validate one persisted parameter-definition object."""

    if not isinstance(value, Mapping):
        raise RecipeParameterDefinitionError(f"{label} must be an object")
    result: dict[str, object] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise RecipeParameterDefinitionError(f"{label} keys must be text")
        result[key] = item
    return result


def _definition_sequence(value: object) -> tuple[object, ...]:
    """Validate the persisted parameter-definition collection."""

    if not isinstance(value, (list, tuple)):
        raise RecipeParameterDefinitionError(
            "Recipe parameter definitions must be a sequence"
        )
    return tuple(value)


def _definition_integer(value: object, label: str) -> int:
    """Decode one persisted integer-valued definition field."""

    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise RecipeParameterDefinitionError(f"{label} is invalid")
    try:
        return int(value)
    except ValueError as error:
        raise RecipeParameterDefinitionError(f"{label} is invalid") from error


def _parameter_constraints(value: object) -> dict[str, object]:
    """Validate parameter constraints before interpreting their values."""

    if not isinstance(value, Mapping):
        raise RecipeParameterValueError("Parameter constraints must be an object")
    result: dict[str, object] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise RecipeParameterValueError("Parameter constraint keys must be text")
        result[key] = item
    return result


def _constraint_integer(value: object, label: str) -> int:
    """Decode a validated integer constraint without arbitrary coercion."""

    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise RecipeParameterValueError(f"{label} is invalid")
    try:
        return int(value)
    except ValueError as error:
        raise RecipeParameterValueError(f"{label} is invalid") from error


@dataclass(frozen=True, slots=True)
class RecipeParameterDefinition:
    """One named value that every Recipe application may need to supply."""

    name: str
    label: str
    value_type: RecipeParameterType
    required: bool = True

    def __post_init__(self) -> None:
        normalized_name = self.name.strip().casefold()
        if _PARAMETER_NAME.fullmatch(normalized_name) is None:
            raise RecipeParameterDefinitionError(
                "Parameter name must start with a letter and use only "
                "lowercase letters, numbers, and underscores"
            )
        if f"parameter:{normalized_name}" == EXPORT_AS_OF_PARAMETER_ID:
            raise RecipeParameterDefinitionError(
                "Export as-of date is already provided for file Recipes"
            )
        clean_label = self.label.strip()
        if not clean_label or len(clean_label) > 120:
            raise RecipeParameterDefinitionError("Parameter label is invalid")
        object.__setattr__(self, "name", normalized_name)
        object.__setattr__(self, "label", clean_label)
        object.__setattr__(
            self,
            "value_type",
            RecipeParameterType(self.value_type),
        )

    @property
    def logical_parameter_id(self) -> str:
        return f"parameter:{self.name}"

    def to_recipe_dict(self) -> dict[str, object]:
        return {
            "allowed_use_sites": ["controls", "provenance"],
            "constraints": {},
            "label": self.label,
            "logical_parameter_id": self.logical_parameter_id,
            "required": self.required,
            "type": self.value_type.value,
        }


@dataclass(frozen=True, slots=True)
class RecipeParameterDefinitions:
    """Current custom parameter declarations in one authoring workspace."""

    definitions: tuple[RecipeParameterDefinition, ...] = ()
    contract_version: int = RECIPE_PARAMETER_DEFINITIONS_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if self.contract_version != RECIPE_PARAMETER_DEFINITIONS_CONTRACT_VERSION:
            raise RecipeParameterDefinitionError(
                "Recipe parameter-definition contract is unsupported"
            )
        if len(self.definitions) > MAX_RECIPE_PARAMETER_DEFINITIONS:
            raise RecipeParameterDefinitionError(
                "Recipe has too many parameter definitions"
            )
        ordered = tuple(sorted(self.definitions, key=lambda item: item.name))
        if len({item.name for item in ordered}) != len(ordered):
            raise RecipeParameterDefinitionError(
                "Recipe parameter names must be unique"
            )
        object.__setattr__(self, "definitions", ordered)

    @property
    def content_hash(self) -> str:
        return content_hash(self.to_dict(include_hash=False))

    def to_dict(self, *, include_hash: bool = True) -> dict[str, object]:
        payload = portable(asdict(self))
        if include_hash:
            payload["content_hash"] = self.content_hash
        return payload

    def to_json(self) -> str:
        return canonical_json(self.to_dict())

    @classmethod
    def from_dict(
        cls,
        payload: Mapping[str, object],
    ) -> RecipeParameterDefinitions:
        result = cls(
            definitions=tuple(
                RecipeParameterDefinition(
                    name=str(item["name"]),
                    label=str(item["label"]),
                    value_type=RecipeParameterType(str(item["value_type"])),
                    required=bool(item.get("required", True)),
                )
                for item in (
                    _definition_mapping(value, "Recipe parameter definition")
                    for value in _definition_sequence(payload.get("definitions", ()))
                )
            ),
            contract_version=_definition_integer(
                payload["contract_version"],
                "Recipe parameter-definition contract version",
            ),
        )
        if payload.get("content_hash") != result.content_hash:
            raise RecipeParameterDefinitionError(
                "Recipe parameter-definition content hash is invalid"
            )
        return result

    @classmethod
    def from_json(cls, value: str) -> RecipeParameterDefinitions:
        return cls.from_dict(json.loads(value))


def normalize_recipe_parameter_values(
    definitions: Sequence[Mapping[str, object]],
    supplied: Mapping[str, object],
    *,
    application_date: date | None = None,
) -> dict[str, object]:
    """Validate and normalize values against one exact Recipe revision."""

    expected = {str(item["logical_parameter_id"]): dict(item) for item in definitions}
    unknown = sorted(set(supplied) - set(expected))
    if unknown:
        raise RecipeParameterValueError(
            f"Parameter {unknown[0]} is not declared by this Recipe"
        )
    normalized: dict[str, object] = {}
    comparison_date = application_date or datetime.now(UTC).date()
    for logical_id, definition in expected.items():
        raw = str(supplied.get(logical_id, "")).strip()
        label = str(definition.get("label", logical_id))
        if not raw:
            if bool(definition.get("required")):
                raise RecipeParameterValueError(f"Enter {label}")
            continue
        value_type = str(definition.get("type", "string"))
        constraints = _parameter_constraints(definition.get("constraints", {}))
        if value_type == RecipeParameterType.DATE.value:
            try:
                value = date.fromisoformat(raw)
            except ValueError as error:
                raise RecipeParameterValueError(f"{label} must be a date") from error
            if (
                constraints.get("not_after_application_date")
                and value > comparison_date
            ):
                raise RecipeParameterValueError(f"{label} cannot be in the future")
            normalized[logical_id] = value.isoformat()
        elif value_type == RecipeParameterType.INTEGER.value:
            try:
                normalized[logical_id] = int(raw)
            except ValueError as error:
                raise RecipeParameterValueError(
                    f"{label} must be a whole number"
                ) from error
        elif value_type == RecipeParameterType.DECIMAL.value:
            try:
                normalized[logical_id] = format(Decimal(raw), "f")
            except InvalidOperation as error:
                raise RecipeParameterValueError(f"{label} must be a number") from error
        elif value_type == RecipeParameterType.STRING.value:
            maximum = constraints.get("max_length")
            maximum_length = (
                _constraint_integer(maximum, f"{label} maximum length")
                if maximum is not None
                else None
            )
            if maximum_length is not None and len(raw) > maximum_length:
                raise RecipeParameterValueError(
                    f"{label} must be {maximum_length} characters or fewer"
                )
            normalized[logical_id] = raw
        else:
            raise RecipeParameterValueError(f"{label} has an unsupported value type")
    return normalized
