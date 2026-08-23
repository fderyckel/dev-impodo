"""Define bounded values used to materialize one Recipe application."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from enum import StrEnum
import re
from typing import Mapping

from ..access import ActorIdentity
from ..migration_foundation import require_uuid
from .serialization import content_hash, portable


RECIPE_CONTROL_VALUES_CONTRACT_VERSION = 1
_TECHNICAL_ID = re.compile(r"[a-z][a-z0-9_.:-]{0,299}\Z")


class RecipeApplicationError(ValueError):
    """Reject invalid Recipe application inputs or materialization."""


class RecipeApplicationIssueLevel(StrEnum):
    BLOCKER = "BLOCKER"
    REVIEW = "REVIEW"
    INFORMATION = "INFORMATION"


@dataclass(frozen=True, slots=True)
class RecipeControlValues:
    """Store fresh expected controls for one exact DataVersion application."""

    data_version_id: str
    values: Mapping[str, str]
    actor: ActorIdentity
    confirmed_at: datetime
    contract_version: int = RECIPE_CONTROL_VALUES_CONTRACT_VERSION

    def __post_init__(self) -> None:
        require_uuid(self.data_version_id, "data_version_id")
        if self.contract_version != RECIPE_CONTROL_VALUES_CONTRACT_VERSION:
            raise RecipeApplicationError("Control-values contract is unsupported")
        if len(self.values) > 300:
            raise RecipeApplicationError("Too many Recipe control values")
        for key, value in self.values.items():
            if _TECHNICAL_ID.fullmatch(str(key)) is None or len(str(value)) > 200:
                raise RecipeApplicationError("Recipe control value is invalid")
        if self.confirmed_at.tzinfo is None or self.confirmed_at.utcoffset() is None:
            raise RecipeApplicationError("Control confirmation time is invalid")

    @property
    def content_hash(self) -> str:
        return content_hash(self.to_dict(include_hash=False))

    def to_dict(self, *, include_hash: bool = True) -> dict[str, object]:
        payload = portable(asdict(self))
        if include_hash:
            payload["content_hash"] = self.content_hash
        return payload


@dataclass(frozen=True, slots=True)
class RecipeApplicationIssue:
    """Describe one bounded source, target, or input compatibility result."""

    code: str
    level: RecipeApplicationIssueLevel
    message: str
    recovery_action: str
    logical_id: str = ""

    def __post_init__(self) -> None:
        if _TECHNICAL_ID.fullmatch(self.code.casefold().replace("_", "-")) is None:
            raise RecipeApplicationError("Application issue code is invalid")
        object.__setattr__(self, "level", RecipeApplicationIssueLevel(self.level))
        if not self.message.strip() or len(self.message) > 1_000:
            raise RecipeApplicationError("Application issue message is invalid")
        if not self.recovery_action.strip() or len(self.recovery_action) > 1_000:
            raise RecipeApplicationError("Application recovery action is invalid")

    @property
    def fingerprint(self) -> str:
        return content_hash(
            {
                "code": self.code,
                "level": self.level.value,
                "logical_id": self.logical_id,
                "recovery_action": self.recovery_action,
            }
        )

    @property
    def blocks(self) -> bool:
        return self.level is RecipeApplicationIssueLevel.BLOCKER
