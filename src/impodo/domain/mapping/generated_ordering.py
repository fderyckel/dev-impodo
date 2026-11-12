"""Describe explicitly generated ordering independently of persistent identity."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

OrderingValueType = Literal["string", "integer", "decimal", "date", "datetime"]

GENERATED_ORDER_SOURCE = "__impodo_generated_order"


@dataclass(frozen=True, slots=True)
class GeneratedOrdering:
    """Generate a reviewed integer order with original-row tie-breaking."""

    target_field: str
    group_column_keys: tuple[str, ...] = ()
    sort_column_keys: tuple[str, ...] = ()
    sort_value_types: tuple[OrderingValueType, ...] = ()
    descending: bool = False
    blanks: str = "last"
    start: int = 10
    step: int = 10

    def __post_init__(self) -> None:
        for name in ("group_column_keys", "sort_column_keys", "sort_value_types"):
            object.__setattr__(self, name, tuple(getattr(self, name)))
        if not isinstance(self.target_field, str) or not self.target_field.isidentifier() or self.target_field.startswith("__") or self.target_field == "id":
            raise ValueError("Choose a writable whole-number field for generated ordering")
        for columns in (self.group_column_keys, self.sort_column_keys):
            if len(columns) > 5 or len(set(columns)) != len(columns) or any(not key or len(key) > 500 for key in columns):
                raise ValueError("Choose up to five distinct ordering columns")
        if len(self.sort_column_keys) != len(self.sort_value_types) or any(value not in {"string", "integer", "decimal", "date", "datetime"} for value in self.sort_value_types):
            raise ValueError("Each ordering column needs a supported value type")
        if type(self.descending) is not bool or self.blanks not in {"first", "last"}:
            raise ValueError("Choose an ordering direction and blank placement")
        if type(self.start) is not int or type(self.step) is not int or not 0 <= self.start <= 2_147_483_647 or not 1 <= self.step <= 1_000_000:
            raise ValueError("Generated ordering needs a nonnegative start and a step from 1 to 1000000")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> GeneratedOrdering:
        """Restore only the declared portable ordering policy."""

        if set(value) != {"target_field", "group_column_keys", "sort_column_keys", "sort_value_types", "descending", "blanks", "start", "step"}:
            raise ValueError("Generated ordering settings are invalid")
        return cls(**value)
