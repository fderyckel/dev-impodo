"""Immutable Test qualification and rollout-candidate projections."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Mapping

from ..access import ActorIdentity
from ..recipes import require_hash, require_uuid


class RecipeQualificationError(ValueError):
    """Raised when a Recipe cannot be qualified from current Test evidence."""


class RecipeQualificationState(StrEnum):
    UNTESTED = "UNTESTED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    READY = "READY"
    QUALIFIED = "QUALIFIED"
    SELECTED = "SELECTED"


@dataclass(frozen=True, slots=True)
class QualificationExpectedOutcomes:
    """Explicit bounded outcome counts confirmed by the data manager."""

    create_count: int
    update_count: int
    unchanged_count: int
    verified_count: int

    def __post_init__(self) -> None:
        if any(
            not isinstance(value, int) or isinstance(value, bool) or value < 0
            for value in (
                self.create_count,
                self.update_count,
                self.unchanged_count,
                self.verified_count,
            )
        ):
            raise RecipeQualificationError("Qualification outcome counts are invalid")
        if self.verified_count != self.total_count:
            raise RecipeQualificationError(
                "Verified outcomes must equal new, changed, and unchanged records"
            )

    @property
    def total_count(self) -> int:
        return self.create_count + self.update_count + self.unchanged_count

    def to_dict(self) -> dict[str, int]:
        return {
            "create_count": self.create_count,
            "unchanged_count": self.unchanged_count,
            "update_count": self.update_count,
            "verified_count": self.verified_count,
        }

    @classmethod
    def from_mapping(
        cls,
        payload: Mapping[str, object],
    ) -> "QualificationExpectedOutcomes":
        try:
            return cls(
                create_count=int(payload["create_count"]),
                update_count=int(payload["update_count"]),
                unchanged_count=int(payload["unchanged_count"]),
                verified_count=int(payload["verified_count"]),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise RecipeQualificationError(
                "Qualification outcome counts are invalid"
            ) from error


@dataclass(frozen=True, slots=True)
class RecipeQualificationIssue:
    code: str
    message: str
    recovery_action: str
    recovery_href: str = ""


@dataclass(frozen=True, slots=True)
class RecipeQualificationRecord:
    """Registry-safe projection of one protected qualification."""

    qualification_id: str
    recipe_id: str
    recipe_revision: int
    application_id: str
    test_target_binding_hash: str
    status: str
    findings: tuple[Mapping[str, object], ...]
    qualified_by: ActorIdentity
    qualified_at: datetime
    evidence_storage_key: str
    evidence_hash: str

    def __post_init__(self) -> None:
        for value, label in (
            (self.qualification_id, "qualification_id"),
            (self.recipe_id, "recipe_id"),
            (self.application_id, "application_id"),
        ):
            require_uuid(value, label)
        require_hash(self.test_target_binding_hash, "test_target_binding_hash")
        require_hash(self.evidence_hash, "evidence_hash")
        if self.recipe_revision < 1 or self.status != "TEST_QUALIFIED":
            raise RecipeQualificationError("Qualification record is invalid")
        if self.qualified_at.tzinfo is None or self.qualified_at.utcoffset() is None:
            raise RecipeQualificationError("Qualification time must be timezone-aware")


@dataclass(frozen=True, slots=True)
class CutoverCandidateRecord:
    """Exact qualified Recipe revision selected for a later rollout."""

    cutover_candidate_id: str
    recipe_id: str
    recipe_revision: int
    qualification_id: str
    selected_by: ActorIdentity
    selected_at: datetime
    content_hash: str

    def __post_init__(self) -> None:
        for value, label in (
            (self.cutover_candidate_id, "cutover_candidate_id"),
            (self.recipe_id, "recipe_id"),
            (self.qualification_id, "qualification_id"),
        ):
            require_uuid(value, label)
        require_hash(self.content_hash, "content_hash")
        if self.recipe_revision < 1:
            raise RecipeQualificationError("Cutover candidate revision is invalid")
        if self.selected_at.tzinfo is None or self.selected_at.utcoffset() is None:
            raise RecipeQualificationError(
                "Cutover selection time must be timezone-aware"
            )
