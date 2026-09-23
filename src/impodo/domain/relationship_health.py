"""Classify Stage 3 relationship choices without retaining business values.

The application layer owns exact source and Odoo values while a check runs.
This module reduces those protected values to aggregate counts that are safe to
persist with the authoring workspace and present to a data manager.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Iterable, Mapping

from impodo.domain.mapping.contracts import ResolverOrigin
from impodo.domain.workspace.errors import WorkspaceError


class RelationshipHealthStatus(StrEnum):
    """Whether one saved relationship was classified on current data."""

    CHECKED = "CHECKED"
    NOT_CHECKABLE = "NOT_CHECKABLE"


class RelationshipHealthNotCheckableReason(StrEnum):
    """Why a saved relationship could not enter the bounded simulator."""

    MAPPING_INCOMPLETE = "MAPPING_INCOMPLETE"
    UNCONFIRMED_MATCHING_RULE = "UNCONFIRMED_MATCHING_RULE"
    UNSUPPORTED_RELATIONSHIP = "UNSUPPORTED_RELATIONSHIP"
    ONE2MANY_INVERSE_REQUIRED = "ONE2MANY_INVERSE_REQUIRED"
    RELATED_DATASET_UNAVAILABLE = "RELATED_DATASET_UNAVAILABLE"
    RELATED_IDENTITY_UNAVAILABLE = "RELATED_IDENTITY_UNAVAILABLE"
    SOURCE_EVIDENCE_UNAVAILABLE = "SOURCE_EVIDENCE_UNAVAILABLE"
    EVIDENCE_LIMIT_EXCEEDED = "EVIDENCE_LIMIT_EXCEEDED"
    ODOO_SCHEMA_CHANGED = "ODOO_SCHEMA_CHANGED"


@dataclass(frozen=True, slots=True)
class RelationshipHealthResult:
    """Aggregate resolution coverage for one saved relationship field."""

    owner_dataset_id: str
    target_field: str
    relationship_kind: str
    resolver_origin: ResolverOrigin
    related_model: str
    dependency_dataset_id: str | None
    required: bool
    status: RelationshipHealthStatus
    not_checkable_reason: RelationshipHealthNotCheckableReason | None = None
    source_row_count: int = 0
    populated_choice_count: int = 0
    blank_row_count: int = 0
    incomplete_row_count: int = 0
    target_count: int = 0
    incoming_count: int = 0
    missing_count: int = 0
    ambiguous_count: int = 0
    case_mismatch_count: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "resolver_origin", ResolverOrigin(self.resolver_origin))
        object.__setattr__(self, "status", RelationshipHealthStatus(self.status))
        if self.not_checkable_reason is not None:
            object.__setattr__(
                self,
                "not_checkable_reason",
                RelationshipHealthNotCheckableReason(self.not_checkable_reason),
            )
        if (
            not self.owner_dataset_id
            or not self.target_field
            or not self.relationship_kind
            or not self.related_model
        ):
            raise WorkspaceError("Relationship health identity is invalid")
        if (
            self.status is RelationshipHealthStatus.CHECKED
            and self.resolver_origin
            in {ResolverOrigin.DATASET, ResolverOrigin.TARGET_THEN_DATASET}
            and not self.dependency_dataset_id
        ):
            raise WorkspaceError("Relationship health dependency is missing")
        counts = (
            self.source_row_count,
            self.populated_choice_count,
            self.blank_row_count,
            self.incomplete_row_count,
            self.target_count,
            self.incoming_count,
            self.missing_count,
            self.ambiguous_count,
            self.case_mismatch_count,
        )
        if any(value < 0 for value in counts):
            raise WorkspaceError("Relationship health counts are invalid")
        if self.status is RelationshipHealthStatus.CHECKED:
            if self.not_checkable_reason is not None:
                raise WorkspaceError("Checked relationship health has a reason")
            if self.blank_row_count + self.incomplete_row_count > self.source_row_count:
                raise WorkspaceError("Relationship health row totals are invalid")
            if (
                self.target_count
                + self.incoming_count
                + self.missing_count
                + self.ambiguous_count
                + self.case_mismatch_count
                != self.populated_choice_count
            ):
                raise WorkspaceError("Relationship health outcomes are invalid")
        elif self.not_checkable_reason is None or any(counts):
            raise WorkspaceError("Unchecked relationship health is invalid")

    @property
    def checked(self) -> bool:
        """Return whether current-data aggregate counts are available."""

        return self.status is RelationshipHealthStatus.CHECKED

    @property
    def blocked_count(self) -> int:
        """Return choices or required blank rows that need correction."""

        return (
            self.missing_count
            + self.ambiguous_count
            + self.case_mismatch_count
            + self.incomplete_row_count
            + (self.blank_row_count if self.required else 0)
        )

    @property
    def ready(self) -> bool:
        """Return whether every required relationship choice resolves once."""

        return self.checked and self.blocked_count == 0

    def portable_dict(self) -> dict[str, object]:
        """Return aggregate evidence without source keys or Odoo identifiers."""

        return {
            **asdict(self),
            "resolver_origin": self.resolver_origin.value,
            "status": self.status.value,
            "not_checkable_reason": (
                self.not_checkable_reason.value
                if self.not_checkable_reason is not None
                else None
            ),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "RelationshipHealthResult":
        """Restore the bounded portable relationship-health shape."""

        return cls(
            owner_dataset_id=str(payload["owner_dataset_id"]),
            target_field=str(payload["target_field"]),
            relationship_kind=str(payload["relationship_kind"]),
            resolver_origin=ResolverOrigin(str(payload["resolver_origin"])),
            related_model=str(payload["related_model"]),
            dependency_dataset_id=(
                str(payload["dependency_dataset_id"])
                if payload.get("dependency_dataset_id") is not None
                else None
            ),
            required=bool(payload["required"]),
            status=RelationshipHealthStatus(str(payload["status"])),
            not_checkable_reason=(
                RelationshipHealthNotCheckableReason(
                    str(payload["not_checkable_reason"])
                )
                if payload.get("not_checkable_reason") is not None
                else None
            ),
            source_row_count=int(payload.get("source_row_count", 0)),
            populated_choice_count=int(payload.get("populated_choice_count", 0)),
            blank_row_count=int(payload.get("blank_row_count", 0)),
            incomplete_row_count=int(payload.get("incomplete_row_count", 0)),
            target_count=int(payload.get("target_count", 0)),
            incoming_count=int(payload.get("incoming_count", 0)),
            missing_count=int(payload.get("missing_count", 0)),
            ambiguous_count=int(payload.get("ambiguous_count", 0)),
            case_mismatch_count=int(payload.get("case_mismatch_count", 0)),
        )


def classify_relationship_health(
    *,
    owner_dataset_id: str,
    target_field: str,
    relationship_kind: str,
    resolver_origin: ResolverOrigin,
    related_model: str,
    dependency_dataset_id: str | None,
    required: bool,
    source_row_count: int,
    source_key_counts: Mapping[tuple[str, ...], int],
    blank_row_count: int,
    incomplete_row_count: int,
    target_keys: Iterable[tuple[object | None, ...]],
    incoming_key_counts: Mapping[tuple[str, ...], int],
) -> RelationshipHealthResult:
    """Classify exact choices with target-first and case-only safeguards."""

    origin = ResolverOrigin(resolver_origin)
    normalized_source = _normalized_count_map(source_key_counts)
    normalized_incoming = _normalized_count_map(incoming_key_counts)
    arities = {len(key) for key in (*normalized_source, *normalized_incoming)}
    normalized_targets = tuple(
        key
        for raw in target_keys
        if (key := _normalized_key(raw)) is not None
    )
    arities.update(len(key) for key in normalized_targets)
    if len(arities) > 1:
        raise WorkspaceError("Relationship health key shape is invalid")

    target_counts = Counter(normalized_targets)
    target_folded = _folded_counts(target_counts)
    incoming_counts = Counter(normalized_incoming)
    incoming_folded = _folded_counts(incoming_counts)
    target = incoming = missing = ambiguous = case_mismatch = 0

    for key, occurrence_count in normalized_source.items():
        target_outcome = (
            _lookup_outcome(key, target_counts, target_folded)
            if origin in {
                ResolverOrigin.TARGET_CATALOG,
                ResolverOrigin.TARGET_THEN_DATASET,
            }
            else "missing"
        )
        if target_outcome == "exact":
            target += occurrence_count
            continue
        if target_outcome == "ambiguous":
            ambiguous += occurrence_count
            continue
        if target_outcome == "case":
            case_mismatch += occurrence_count
            continue

        incoming_outcome = (
            _lookup_outcome(key, incoming_counts, incoming_folded)
            if origin in {
                ResolverOrigin.DATASET,
                ResolverOrigin.TARGET_THEN_DATASET,
            }
            else "missing"
        )
        if incoming_outcome == "exact":
            incoming += occurrence_count
        elif incoming_outcome == "ambiguous":
            ambiguous += occurrence_count
        elif incoming_outcome == "case":
            case_mismatch += occurrence_count
        else:
            missing += occurrence_count

    return RelationshipHealthResult(
        owner_dataset_id=owner_dataset_id,
        target_field=target_field,
        relationship_kind=relationship_kind,
        resolver_origin=origin,
        related_model=related_model,
        dependency_dataset_id=dependency_dataset_id,
        required=required,
        status=RelationshipHealthStatus.CHECKED,
        source_row_count=source_row_count,
        populated_choice_count=sum(normalized_source.values()),
        blank_row_count=blank_row_count,
        incomplete_row_count=incomplete_row_count,
        target_count=target,
        incoming_count=incoming,
        missing_count=missing,
        ambiguous_count=ambiguous,
        case_mismatch_count=case_mismatch,
    )


def _normalized_count_map(
    values: Mapping[tuple[str, ...], int],
) -> Counter[tuple[str, ...]]:
    result: Counter[tuple[str, ...]] = Counter()
    for raw, count in values.items():
        key = _normalized_key(raw)
        if key is None or count < 1:
            raise WorkspaceError("Relationship health source keys are invalid")
        result[key] += int(count)
    return result


def _normalized_key(
    raw: Iterable[object | None],
) -> tuple[str, ...] | None:
    values: list[str] = []
    for value in raw:
        if value is None or value is False or not str(value).strip():
            return None
        values.append(str(value).strip())
    return tuple(values) if values else None


def _folded_counts(
    counts: Mapping[tuple[str, ...], int],
) -> Counter[tuple[str, ...]]:
    result: Counter[tuple[str, ...]] = Counter()
    for key, count in counts.items():
        result[tuple(value.casefold() for value in key)] += count
    return result


def _lookup_outcome(
    key: tuple[str, ...],
    exact: Mapping[tuple[str, ...], int],
    folded: Mapping[tuple[str, ...], int],
) -> str:
    exact_count = exact.get(key, 0)
    if exact_count == 1:
        return "exact"
    if exact_count > 1:
        return "ambiguous"
    folded_count = folded.get(tuple(value.casefold() for value in key), 0)
    if folded_count == 1:
        return "case"
    if folded_count > 1:
        return "ambiguous"
    return "missing"
