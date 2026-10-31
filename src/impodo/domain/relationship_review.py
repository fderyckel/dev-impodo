"""Bounded protected relationship choices, separate from aggregate health.

Only authorized application queries return these values. Publishing or reading
them does not select a resolver, approve a mapping, or grant write authority.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum

from impodo.domain.mapping.contracts import ResolverOrigin
from impodo.domain.workspace.errors import WorkspaceError

MAX_RELATIONSHIP_CHOICE_PAGE_SIZE = 100


class RelationshipChoiceCause(StrEnum):
    """Closed causes agreeing with the saved relationship-health counts."""

    MISSING = "missing"
    AMBIGUOUS = "ambiguous"
    CASE_ONLY = "case_only"
    INCOMPLETE = "incomplete"
    REQUIRED_BLANK = "required_blank"


@dataclass(frozen=True, slots=True)
class RelationshipChoiceResolution:
    """Ephemeral failure from the same lookup that produced aggregate health."""

    key: tuple[str, ...]
    cause: RelationshipChoiceCause
    occurrence_count: int
    target_match_count: int
    incoming_match_count: int
    match_origin: str


@dataclass(frozen=True, slots=True)
class RelationshipChoice:
    """One protected business choice with its complete key, scope, and impact."""

    ordinal: int
    owner_dataset_id: str
    target_field: str
    relationship_kind: str
    resolver_origin: ResolverOrigin
    related_model: str
    dependency_dataset_id: str | None
    key_fields: tuple[str, ...]
    key: tuple[str | None, ...]
    cause: RelationshipChoiceCause
    occurrence_count: int
    affected_row_count: int
    target_match_count: int
    incoming_match_count: int
    match_origin: str

    def __post_init__(self) -> None:
        for value, maximum in (
            (self.owner_dataset_id, 500),
            (self.target_field, 200),
            (self.related_model, 200),
        ):
            if not isinstance(value, str) or not value.strip() or len(value) > maximum:
                raise WorkspaceError("Relationship choice identity is invalid")
        if self.dependency_dataset_id is not None and (
            not isinstance(self.dependency_dataset_id, str)
            or not self.dependency_dataset_id
            or len(self.dependency_dataset_id) > 500
        ):
            raise WorkspaceError("Relationship choice dependency is invalid")
        if self.relationship_kind not in {
            "many2one",
            "many2many",
        } or self.match_origin not in {"target", "incoming", "both", "source"}:
            raise WorkspaceError("Relationship choice origin is invalid")
        if not isinstance(self.key_fields, (list, tuple)) or not isinstance(
            self.key, (list, tuple)
        ):
            raise WorkspaceError("Relationship choice key is invalid")
        if (
            not self.key_fields
            or len(self.key_fields) != len(self.key)
            or any(
                not isinstance(value, str) or not value or len(value) > 200
                for value in self.key_fields
            )
            or len(set(self.key_fields)) != len(self.key_fields)
            or any(
                value is not None and (not isinstance(value, str) or len(value) > 4096)
                for value in self.key
            )
        ):
            raise WorkspaceError("Relationship choice key is invalid")
        counts = (
            self.ordinal,
            self.occurrence_count,
            self.affected_row_count,
            self.target_match_count,
            self.incoming_match_count,
        )
        if (
            any(type(value) is not int or value < 0 for value in counts)
            or not 1 <= self.affected_row_count <= self.occurrence_count
        ):
            raise WorkspaceError("Relationship choice counts are invalid")
        object.__setattr__(
            self, "resolver_origin", ResolverOrigin(self.resolver_origin)
        )
        object.__setattr__(self, "cause", RelationshipChoiceCause(self.cause))
        object.__setattr__(self, "key_fields", tuple(self.key_fields))
        object.__setattr__(self, "key", tuple(self.key))
        if (
            self.resolver_origin is not ResolverOrigin.TARGET_CATALOG
            and not self.dependency_dataset_id
        ):
            raise WorkspaceError("Relationship choice dependency is missing")

    def protected_dict(self) -> dict:
        """Serialize only into protected worker evidence or an authorized result."""
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict) -> RelationshipChoice:
        """Reject unrecognized protected fields before restoring one page row."""
        if not isinstance(payload, dict) or set(payload) != set(
            cls.__dataclass_fields__
        ):
            raise WorkspaceError("Relationship choice fields are invalid")
        return cls(**payload)


@dataclass(frozen=True, slots=True)
class RelationshipChoiceFilter:
    """Allowlisted indexed predicates; cursors remain separate from meaning."""

    owner_dataset_id: str = ""
    target_field: str = ""
    cause: str = ""

    def __post_init__(self) -> None:
        if len(self.owner_dataset_id) > 500 or len(self.target_field) > 200:
            raise WorkspaceError("Relationship choice filter is too long")
        if self.cause:
            RelationshipChoiceCause(self.cause)


@dataclass(frozen=True, slots=True)
class RelationshipChoicePage:
    """One bounded current-check page; totals count choice occurrences."""

    rows: tuple[RelationshipChoice, ...]
    matching_count: int
    occurrence_count: int
    start_position: int
    end_position: int
    previous_before: int | None
    next_after: int | None
    check_id: str
