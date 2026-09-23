"""Pure dependency-component ordering shared by authoring and execution.

The graph contains dataset identifiers only.  Callers own the evidence that
created each edge and the meaning of the resulting order.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime
from enum import StrEnum
from heapq import heappop, heappush
import json
from typing import Iterable
from uuid import UUID

from impodo.domain.mapping.contracts import MappingTargetMode
from impodo.domain.serialization import canonical_json, content_hash
from impodo.domain.workspace.errors import WorkspaceError


@dataclass(frozen=True, slots=True)
class DatasetOrderEdge:
    """One dataset that must follow another dataset."""

    owner_dataset: str
    dependency_dataset: str


@dataclass(frozen=True, slots=True)
class DatasetComponentOrder:
    """A stable topological order and its strongly connected groups."""

    ordered_dataset_ids: tuple[str, ...]
    components: tuple[tuple[str, ...], ...]


class MatchingOrderConfidence(StrEnum):
    """How strongly current local evidence supports one ordering fact."""

    CONFIRMED = "confirmed"
    PRELIMINARY = "preliminary"


class MatchingOrderSource(StrEnum):
    """The local evidence that created one matching-order fact."""

    SAVED_MAPPING = "saved_mapping"
    RELATED_TABLE = "related_table"
    DERIVED_TABLE = "derived_table"
    ODOO_SCHEMA = "odoo_schema"


@dataclass(frozen=True, slots=True)
class MatchingOrderFact:
    """One explained local reason to match a dependency before its owner."""

    owner_dataset: str
    dependency_dataset: str
    source: MatchingOrderSource
    confidence: MatchingOrderConfidence
    target_field: str = ""

    @property
    def edge(self) -> DatasetOrderEdge:
        """Return the graph-only projection used by the ordering algorithm."""

        return DatasetOrderEdge(
            owner_dataset=self.owner_dataset,
            dependency_dataset=self.dependency_dataset,
        )


@dataclass(frozen=True, slots=True)
class MatchingOrderRecommendation:
    """A local-only authoring recommendation with its supporting facts."""

    ordered_dataset_ids: tuple[str, ...]
    components: tuple[tuple[str, ...], ...]
    facts: tuple[MatchingOrderFact, ...]

    @property
    def preliminary(self) -> bool:
        """Return whether any ordering fact is only a captured-schema hint."""

        return any(
            fact.confidence is MatchingOrderConfidence.PRELIMINARY
            for fact in self.facts
        )


@dataclass(frozen=True, slots=True)
class MatchingOrderPreference:
    """Store one workspace's non-semantic Stage 3 table display order."""

    workspace_id: str
    version: int
    source_selection_hash: str
    ordered_dataset_ids: tuple[str, ...]
    updated_at: datetime
    actor_issuer: str
    actor_subject: str
    actor_display_name: str

    def __post_init__(self) -> None:
        workspace_id = self.workspace_id.strip()
        if not workspace_id or len(workspace_id) > 200:
            raise WorkspaceError("Matching-order workspace identity is invalid")
        object.__setattr__(self, "workspace_id", workspace_id)
        if self.version < 1:
            raise WorkspaceError("Matching-order preference version is invalid")
        if not (
            len(self.source_selection_hash) == 71
            and self.source_selection_hash.startswith("sha256:")
        ):
            raise WorkspaceError("Matching-order source selection is invalid")
        normalized = tuple(item.strip() for item in self.ordered_dataset_ids)
        if (
            not normalized
            or len(normalized) != len(set(normalized))
            or any(not item or len(item) > 500 for item in normalized)
        ):
            raise WorkspaceError("Matching-order datasets are invalid")
        object.__setattr__(self, "ordered_dataset_ids", normalized)
        if self.updated_at.tzinfo is None:
            raise WorkspaceError("Matching-order update time must include a timezone")
        for value, label, maximum in (
            (self.actor_issuer, "actor issuer", 500),
            (self.actor_subject, "actor subject", 500),
            (self.actor_display_name, "actor display name", 200),
        ):
            if not value.strip() or len(value.strip()) > maximum:
                raise WorkspaceError(f"Matching-order {label} is invalid")
        object.__setattr__(self, "actor_issuer", self.actor_issuer.strip())
        object.__setattr__(self, "actor_subject", self.actor_subject.strip())
        object.__setattr__(
            self,
            "actor_display_name",
            self.actor_display_name.strip(),
        )

    def portable_dict(self) -> dict[str, object]:
        """Return the bounded persistence form of this preference."""

        return {
            "workspace_id": self.workspace_id,
            "version": self.version,
            "source_selection_hash": self.source_selection_hash,
            "ordered_dataset_ids": list(self.ordered_dataset_ids),
            "updated_at": self.updated_at.isoformat(),
            "actor_issuer": self.actor_issuer,
            "actor_subject": self.actor_subject,
            "actor_display_name": self.actor_display_name,
        }

    def to_json(self) -> str:
        """Serialize the preference without adding it to mapping meaning."""

        return canonical_json(self.portable_dict())

    @classmethod
    def from_json(cls, value: str) -> "MatchingOrderPreference":
        """Restore one exact supported preference payload."""

        payload = json.loads(value)
        expected = {
            "workspace_id",
            "version",
            "source_selection_hash",
            "ordered_dataset_ids",
            "updated_at",
            "actor_issuer",
            "actor_subject",
            "actor_display_name",
        }
        if not isinstance(payload, dict) or set(payload) != expected:
            raise WorkspaceError("Stored matching-order preference is invalid")
        dataset_ids = payload["ordered_dataset_ids"]
        if not isinstance(dataset_ids, list) or not all(
            isinstance(item, str) for item in dataset_ids
        ):
            raise WorkspaceError("Stored matching-order datasets are invalid")
        return cls(
            workspace_id=str(payload["workspace_id"]),
            version=int(payload["version"]),
            source_selection_hash=str(payload["source_selection_hash"]),
            ordered_dataset_ids=tuple(dataset_ids),
            updated_at=datetime.fromisoformat(str(payload["updated_at"])),
            actor_issuer=str(payload["actor_issuer"]),
            actor_subject=str(payload["actor_subject"]),
            actor_display_name=str(payload["actor_display_name"]),
        )


class MatchingOrderVersionConflict(WorkspaceError):
    """Reject a stale table-order change while preserving the saved order."""

    code = "MATCHING_ORDER_VERSION_CONFLICT"

    def __init__(
        self,
        *,
        submitted_version: int | None,
        current_version: int | None,
    ) -> None:
        self.submitted_version = submitted_version
        self.current_version = current_version
        super().__init__(
            "This table order is out of date because a newer order was saved. "
            "The latest saved order is shown."
        )


class MatchingOrderCheckStatus(StrEnum):
    """Durable lifecycle states for one explicit read-only check."""

    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    STALE = "STALE"


class MatchingOrderCheckPhase(StrEnum):
    """Safe, aggregate-only phases exposed to the browser."""

    QUEUED = "QUEUED"
    READING = "READING"
    CLASSIFYING = "CLASSIFYING"
    PUBLISHING = "PUBLISHING"
    COMPLETE = "COMPLETE"


class MatchingOrderRelationshipOutcome(StrEnum):
    """How one saved incoming relationship resolves against live Odoo."""

    TARGET = "TARGET"
    INCOMING = "INCOMING"
    MIXED = "MIXED"
    MISSING = "MISSING"
    AMBIGUOUS = "AMBIGUOUS"
    UNCHECKED = "UNCHECKED"


@dataclass(frozen=True, slots=True)
class MatchingOrderRelationshipResult:
    """Aggregate-only result for one checked relationship."""

    owner_dataset_id: str
    dependency_dataset_id: str
    target_field: str
    outcome: MatchingOrderRelationshipOutcome
    target_count: int = 0
    incoming_count: int = 0
    missing_count: int = 0
    ambiguous_count: int = 0

    @property
    def target_satisfied(self) -> bool:
        """Return whether Odoo uniquely satisfies every populated key."""

        return (
            self.target_count > 0
            and self.incoming_count == 0
            and self.missing_count == 0
            and self.ambiguous_count == 0
        )


class MatchingIdentityCheckStatus(StrEnum):
    """Whether current source and target identity values were classified."""

    CHECKED = "CHECKED"
    NOT_CHECKABLE = "NOT_CHECKABLE"


class MatchingIdentityNotCheckableReason(StrEnum):
    """Why one saved identity was not included in the bounded live check."""

    MAPPING_INCOMPLETE = "MAPPING_INCOMPLETE"
    UNCONFIRMED_MATCHING_RULE = "UNCONFIRMED_MATCHING_RULE"
    INDIRECT_IDENTITY = "INDIRECT_IDENTITY"
    UNSUPPORTED_TARGET_FIELD = "UNSUPPORTED_TARGET_FIELD"
    SOURCE_EVIDENCE_UNAVAILABLE = "SOURCE_EVIDENCE_UNAVAILABLE"
    ODOO_PINNED_SELECTION = "ODOO_PINNED_SELECTION"
    ODOO_SCHEMA_CHANGED = "ODOO_SCHEMA_CHANGED"


@dataclass(frozen=True, slots=True)
class MatchingIdentityResult:
    """Aggregate-only health for one saved dataset-to-Odoo identity."""

    dataset_id: str
    target_model: str
    target_fields: tuple[str, ...]
    status: MatchingIdentityCheckStatus
    not_checkable_reason: MatchingIdentityNotCheckableReason | None = None
    source_row_count: int = 0
    source_unique_row_count: int = 0
    source_blank_row_count: int = 0
    source_repeated_row_count: int = 0
    source_repeated_group_count: int = 0
    target_unique_key_count: int = 0
    target_ambiguous_key_count: int = 0
    expected_new_count: int = 0
    expected_existing_count: int = 0
    blocked_count: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", MatchingIdentityCheckStatus(self.status))
        if self.not_checkable_reason is not None:
            object.__setattr__(
                self,
                "not_checkable_reason",
                MatchingIdentityNotCheckableReason(self.not_checkable_reason),
            )
        if not self.dataset_id or not self.target_model:
            raise WorkspaceError("Matching identity result is invalid")
        if len(self.target_fields) != len(set(self.target_fields)):
            raise WorkspaceError("Matching identity target fields are invalid")
        counts = (
            self.source_row_count,
            self.source_unique_row_count,
            self.source_blank_row_count,
            self.source_repeated_row_count,
            self.source_repeated_group_count,
            self.target_unique_key_count,
            self.target_ambiguous_key_count,
            self.expected_new_count,
            self.expected_existing_count,
            self.blocked_count,
        )
        if any(value < 0 for value in counts):
            raise WorkspaceError("Matching identity counts are invalid")
        if self.status is MatchingIdentityCheckStatus.CHECKED:
            if self.not_checkable_reason is not None or not self.target_fields:
                raise WorkspaceError("Checked matching identity evidence is invalid")
            if (
                self.source_unique_row_count
                + self.source_blank_row_count
                + self.source_repeated_row_count
                != self.source_row_count
            ):
                raise WorkspaceError("Matching identity source totals are invalid")
            if (
                self.expected_new_count
                + self.expected_existing_count
                + self.blocked_count
                != self.source_row_count
            ):
                raise WorkspaceError("Matching identity outcome totals are invalid")
        elif self.not_checkable_reason is None or any(counts):
            raise WorkspaceError("Unchecked matching identity evidence is invalid")

    @property
    def checked(self) -> bool:
        """Return whether the result contains current-data counts."""

        return self.status is MatchingIdentityCheckStatus.CHECKED

    def portable_dict(self) -> dict[str, object]:
        """Return aggregate evidence without source keys or target records."""

        return {
            **asdict(self),
            "status": self.status.value,
            "not_checkable_reason": (
                self.not_checkable_reason.value
                if self.not_checkable_reason is not None
                else None
            ),
            "target_fields": list(self.target_fields),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "MatchingIdentityResult":
        """Restore the bounded portable identity-health shape."""

        return cls(
            dataset_id=str(payload["dataset_id"]),
            target_model=str(payload["target_model"]),
            target_fields=tuple(payload.get("target_fields", ())),
            status=MatchingIdentityCheckStatus(str(payload["status"])),
            not_checkable_reason=(
                MatchingIdentityNotCheckableReason(
                    str(payload["not_checkable_reason"])
                )
                if payload.get("not_checkable_reason") is not None
                else None
            ),
            source_row_count=int(payload.get("source_row_count", 0)),
            source_unique_row_count=int(
                payload.get("source_unique_row_count", 0)
            ),
            source_blank_row_count=int(payload.get("source_blank_row_count", 0)),
            source_repeated_row_count=int(
                payload.get("source_repeated_row_count", 0)
            ),
            source_repeated_group_count=int(
                payload.get("source_repeated_group_count", 0)
            ),
            target_unique_key_count=int(
                payload.get("target_unique_key_count", 0)
            ),
            target_ambiguous_key_count=int(
                payload.get("target_ambiguous_key_count", 0)
            ),
            expected_new_count=int(payload.get("expected_new_count", 0)),
            expected_existing_count=int(
                payload.get("expected_existing_count", 0)
            ),
            blocked_count=int(payload.get("blocked_count", 0)),
        )


@dataclass(frozen=True, slots=True)
class MatchingOrderCheck:
    """Immutable aggregate result of one explicit Stage 3 Odoo read.

    Exact source values, Odoo rows, and numeric identifiers intentionally do
    not appear here. They remain in the repository's protected snapshot row.
    """

    check_id: str
    workspace_id: str
    source_selection_hash: str
    schema_hash: str
    governance_hash: str
    working_draft_version: int
    working_draft_hash: str
    target_hash: str
    read_credential_binding_hash: str
    read_principal_hash: str
    read_permission_hash: str
    read_context_hash: str
    relationship_results: tuple[MatchingOrderRelationshipResult, ...]
    unchecked_relationship_count: int
    ordered_dataset_ids: tuple[str, ...]
    recommendation_hash: str
    schema_changed: bool
    captured_at: datetime
    actor_issuer: str
    actor_subject: str
    actor_display_name: str
    identity_results: tuple[MatchingIdentityResult, ...] = ()

    def __post_init__(self) -> None:
        try:
            object.__setattr__(self, "check_id", str(UUID(self.check_id)))
        except (ValueError, TypeError, AttributeError) as error:
            raise WorkspaceError("Matching-order check identity is invalid") from error
        if self.working_draft_version < 1:
            raise WorkspaceError("Matching-order working draft is invalid")
        if self.unchecked_relationship_count < 0:
            raise WorkspaceError("Matching-order unchecked count is invalid")
        for value in (
            self.source_selection_hash,
            self.schema_hash,
            self.governance_hash,
            self.working_draft_hash,
            self.target_hash,
            self.read_credential_binding_hash,
            self.read_principal_hash,
            self.read_permission_hash,
            self.read_context_hash,
            self.recommendation_hash,
        ):
            if len(value) != 71 or not value.startswith("sha256:"):
                raise WorkspaceError("Matching-order check binding is invalid")
        if len(self.ordered_dataset_ids) != len(set(self.ordered_dataset_ids)):
            raise WorkspaceError("Matching-order recommendation is invalid")
        identity_dataset_ids = tuple(
            item.dataset_id for item in self.identity_results
        )
        if len(identity_dataset_ids) != len(set(identity_dataset_ids)):
            raise WorkspaceError("Matching identity results are duplicated")
        if self.captured_at.tzinfo is None:
            raise WorkspaceError("Matching-order capture time needs a timezone")

    @property
    def partial(self) -> bool:
        """Return whether some saved incoming relationships were unchecked."""

        return self.unchecked_relationship_count > 0

    @property
    def counts(self) -> dict[str, int]:
        """Return aggregate reference counts safe for browser display."""

        return {
            "target": sum(item.target_count for item in self.relationship_results),
            "incoming": sum(item.incoming_count for item in self.relationship_results),
            "missing": sum(item.missing_count for item in self.relationship_results),
            "ambiguous": sum(
                item.ambiguous_count for item in self.relationship_results
            ),
        }

    def portable_dict(self) -> dict[str, object]:
        """Return only aggregate, non-target-specific check evidence."""

        return {
            "check_id": self.check_id,
            "workspace_id": self.workspace_id,
            "source_selection_hash": self.source_selection_hash,
            "schema_hash": self.schema_hash,
            "governance_hash": self.governance_hash,
            "working_draft_version": self.working_draft_version,
            "working_draft_hash": self.working_draft_hash,
            "target_hash": self.target_hash,
            "read_credential_binding_hash": self.read_credential_binding_hash,
            "read_principal_hash": self.read_principal_hash,
            "read_permission_hash": self.read_permission_hash,
            "read_context_hash": self.read_context_hash,
            "relationship_results": [
                {**asdict(item), "outcome": item.outcome.value}
                for item in self.relationship_results
            ],
            "unchecked_relationship_count": self.unchecked_relationship_count,
            "ordered_dataset_ids": list(self.ordered_dataset_ids),
            "recommendation_hash": self.recommendation_hash,
            "schema_changed": self.schema_changed,
            "captured_at": self.captured_at.isoformat(),
            "actor_issuer": self.actor_issuer,
            "actor_subject": self.actor_subject,
            "actor_display_name": self.actor_display_name,
            "identity_results": [
                item.portable_dict() for item in self.identity_results
            ],
        }

    def to_json(self) -> str:
        return canonical_json(self.portable_dict())

    @classmethod
    def from_json(cls, value: str) -> "MatchingOrderCheck":
        payload = json.loads(value)
        return cls(
            check_id=str(payload["check_id"]),
            workspace_id=str(payload["workspace_id"]),
            source_selection_hash=str(payload["source_selection_hash"]),
            schema_hash=str(payload["schema_hash"]),
            governance_hash=str(payload["governance_hash"]),
            working_draft_version=int(payload["working_draft_version"]),
            working_draft_hash=str(payload["working_draft_hash"]),
            target_hash=str(payload["target_hash"]),
            read_credential_binding_hash=str(
                payload["read_credential_binding_hash"]
            ),
            read_principal_hash=str(payload["read_principal_hash"]),
            read_permission_hash=str(payload["read_permission_hash"]),
            read_context_hash=str(payload["read_context_hash"]),
            relationship_results=tuple(
                MatchingOrderRelationshipResult(
                    owner_dataset_id=str(item["owner_dataset_id"]),
                    dependency_dataset_id=str(item["dependency_dataset_id"]),
                    target_field=str(item["target_field"]),
                    outcome=MatchingOrderRelationshipOutcome(item["outcome"]),
                    target_count=int(item["target_count"]),
                    incoming_count=int(item["incoming_count"]),
                    missing_count=int(item["missing_count"]),
                    ambiguous_count=int(item["ambiguous_count"]),
                )
                for item in payload["relationship_results"]
            ),
            unchecked_relationship_count=int(
                payload["unchecked_relationship_count"]
            ),
            ordered_dataset_ids=tuple(payload["ordered_dataset_ids"]),
            recommendation_hash=str(payload["recommendation_hash"]),
            schema_changed=bool(payload["schema_changed"]),
            captured_at=datetime.fromisoformat(str(payload["captured_at"])),
            actor_issuer=str(payload["actor_issuer"]),
            actor_subject=str(payload["actor_subject"]),
            actor_display_name=str(payload["actor_display_name"]),
            identity_results=tuple(
                MatchingIdentityResult.from_dict(item)
                for item in payload.get("identity_results", ())
            ),
        )


@dataclass(frozen=True, slots=True)
class MatchingOrderCheckAttempt:
    """Bounded durable control-plane state for one browser check."""

    check_id: str
    workspace_id: str
    status: MatchingOrderCheckStatus
    phase: MatchingOrderCheckPhase
    message: str
    progress_percent: int
    created_at: datetime
    updated_at: datetime
    finished_at: datetime | None = None
    failure_message: str = ""

    @property
    def active(self) -> bool:
        return self.status in {
            MatchingOrderCheckStatus.QUEUED,
            MatchingOrderCheckStatus.RUNNING,
        }

    @property
    def terminal(self) -> bool:
        return not self.active

    def __post_init__(self) -> None:
        try:
            object.__setattr__(self, "check_id", str(UUID(self.check_id)))
        except (ValueError, TypeError, AttributeError) as error:
            raise WorkspaceError("Matching-order check identity is invalid") from error
        object.__setattr__(self, "status", MatchingOrderCheckStatus(self.status))
        object.__setattr__(self, "phase", MatchingOrderCheckPhase(self.phase))
        if not 0 <= self.progress_percent <= 100:
            raise WorkspaceError("Matching-order progress is invalid")


def classify_matching_identity(
    *,
    dataset_id: str,
    target_model: str,
    target_fields: tuple[str, ...],
    source_keys: Iterable[tuple[object | None, ...]],
    target_keys: Iterable[tuple[object | None, ...]],
    mode: MappingTargetMode,
    on_existing: str | None,
) -> MatchingIdentityResult:
    """Classify complete keys without retaining their values in the result."""

    if (
        not target_fields
        or any(not item for item in target_fields)
        or len(target_fields) != len(set(target_fields))
    ):
        raise WorkspaceError("Matching identity target fields are invalid")
    source_rows = tuple(source_keys)
    target_rows = tuple(target_keys)
    if any(len(item) != len(target_fields) for item in (*source_rows, *target_rows)):
        raise WorkspaceError("Matching identity key shape is invalid")
    normalized_source = tuple(
        _normalized_identity_key(item) for item in source_rows
    )
    blank_source_count = sum(
        any(value is None for value in key) for key in normalized_source
    )
    source_counts = Counter(
        key
        for key in normalized_source
        if all(value is not None for value in key)
    )
    normalized_targets = (
        _normalized_identity_key(item) for item in target_rows
    )
    target_counts = Counter(
        key
        for key in normalized_targets
        if all(value is not None for value in key)
    )
    unique_source_keys = {
        key for key, count in source_counts.items() if count == 1
    }
    repeated_source = {
        key: count for key, count in source_counts.items() if count > 1
    }
    expected_new = 0
    expected_existing = 0
    blocked = blank_source_count + sum(repeated_source.values())
    normalized_mode = MappingTargetMode(mode)
    for key in unique_source_keys:
        match_count = target_counts[key]
        if match_count > 1:
            blocked += 1
        elif match_count == 1:
            if (
                normalized_mode is MappingTargetMode.CREATE
                and on_existing != "unchanged"
            ):
                blocked += 1
            else:
                expected_existing += 1
        elif normalized_mode is MappingTargetMode.REFERENCE:
            blocked += 1
        else:
            expected_new += 1
    return MatchingIdentityResult(
        dataset_id=dataset_id,
        target_model=target_model,
        target_fields=target_fields,
        status=MatchingIdentityCheckStatus.CHECKED,
        source_row_count=len(normalized_source),
        source_unique_row_count=len(unique_source_keys),
        source_blank_row_count=blank_source_count,
        source_repeated_row_count=sum(repeated_source.values()),
        source_repeated_group_count=len(repeated_source),
        target_unique_key_count=sum(
            target_counts[key] == 1 for key in source_counts
        ),
        target_ambiguous_key_count=sum(
            target_counts[key] > 1 for key in source_counts
        ),
        expected_new_count=expected_new,
        expected_existing_count=expected_existing,
        blocked_count=blocked,
    )


def _normalized_identity_key(
    key: tuple[object | None, ...],
) -> tuple[str | None, ...]:
    return tuple(
        None
        if value is None or value is False or not str(value).strip()
        else str(value).strip()
        for value in key
    )


def live_matching_order_recommendation(
    local: MatchingOrderRecommendation,
    results: Iterable[MatchingOrderRelationshipResult],
    *,
    tie_break_order: Iterable[str] | None = None,
) -> MatchingOrderRecommendation:
    """Remove an incoming edge only when live Odoo fully satisfies it."""

    satisfied = {
        (
            item.owner_dataset_id,
            item.dependency_dataset_id,
            item.target_field,
        )
        for item in results
        if item.target_satisfied
    }
    facts = tuple(
        fact
        for fact in local.facts
        if (
            fact.owner_dataset,
            fact.dependency_dataset,
            fact.target_field,
        )
        not in satisfied
    )
    return recommend_dataset_matching_order(
        local.ordered_dataset_ids,
        facts,
        tie_break_order=tie_break_order or local.ordered_dataset_ids,
    )


def matching_order_recommendation_hash(
    recommendation: MatchingOrderRecommendation,
) -> str:
    """Hash the aggregate recommendation without target-specific values."""

    return content_hash(
        {
            "ordered_dataset_ids": list(recommendation.ordered_dataset_ids),
            "components": [list(item) for item in recommendation.components],
            "facts": [
                {
                    "owner_dataset": item.owner_dataset,
                    "dependency_dataset": item.dependency_dataset,
                    "source": item.source.value,
                    "confidence": item.confidence.value,
                    "target_field": item.target_field,
                }
                for item in recommendation.facts
            ],
        }
    )


def recommend_dataset_matching_order(
    dataset_ids: Iterable[str],
    facts: Iterable[MatchingOrderFact],
    *,
    tie_break_order: Iterable[str] | None = None,
) -> MatchingOrderRecommendation:
    """Order datasets from typed local facts and preserve their explanations."""

    datasets = tuple(dataset_ids)
    known = set(datasets)
    applicable = tuple(
        sorted(
            {
                fact
                for fact in facts
                if fact.owner_dataset in known
                and fact.dependency_dataset in known
                and fact.owner_dataset != fact.dependency_dataset
            },
            key=lambda fact: (
                fact.owner_dataset,
                fact.dependency_dataset,
                fact.confidence.value,
                fact.source.value,
                fact.target_field,
            ),
        )
    )
    order = order_dataset_dependency_components(
        datasets,
        (fact.edge for fact in applicable),
        tie_break_order=tie_break_order,
    )
    return MatchingOrderRecommendation(
        ordered_dataset_ids=order.ordered_dataset_ids,
        components=order.components,
        facts=applicable,
    )


def order_dataset_dependency_components(
    dataset_ids: Iterable[str],
    edges: Iterable[DatasetOrderEdge],
    *,
    tie_break_order: Iterable[str] | None = None,
) -> DatasetComponentOrder:
    """Order dependency components before consumers without opening I/O.

    Cyclic datasets stay together in tie-break order.  Edges outside the known
    dataset set are ignored so a caller can safely project a larger graph onto
    a selected subset.  A partial tie-break order is allowed; omitted datasets
    retain their input order after the explicitly ranked datasets.
    """

    datasets = tuple(dataset_ids)
    if len(set(datasets)) != len(datasets):
        raise ValueError("dataset dependency graph contains duplicate datasets")
    if not datasets:
        return DatasetComponentOrder(ordered_dataset_ids=(), components=())

    ranked_datasets = _ranked_datasets(datasets, tie_break_order)
    rank = {dataset_id: index for index, dataset_id in enumerate(ranked_datasets)}
    known = set(datasets)
    known_edges = tuple(
        sorted(
            {
                edge
                for edge in edges
                if edge.owner_dataset in known
                and edge.dependency_dataset in known
            },
            key=lambda edge: (
                rank[edge.dependency_dataset],
                rank[edge.owner_dataset],
            ),
        )
    )
    following: dict[str, set[str]] = {dataset_id: set() for dataset_id in datasets}
    for edge in known_edges:
        following[edge.dependency_dataset].add(edge.owner_dataset)

    components = _strong_components(ranked_datasets, following, rank)
    component_by_dataset = {
        dataset_id: component_index
        for component_index, component in enumerate(components)
        for dataset_id in component
    }
    component_following = {index: set() for index in range(len(components))}
    indegree = {index: 0 for index in range(len(components))}
    for edge in known_edges:
        dependency_component = component_by_dataset[edge.dependency_dataset]
        owner_component = component_by_dataset[edge.owner_dataset]
        if dependency_component == owner_component:
            continue
        if owner_component in component_following[dependency_component]:
            continue
        component_following[dependency_component].add(owner_component)
        indegree[owner_component] += 1

    component_rank = {
        index: min(rank[dataset_id] for dataset_id in component)
        for index, component in enumerate(components)
    }
    ready: list[tuple[int, int]] = []
    for component_index, count in indegree.items():
        if count == 0:
            heappush(
                ready,
                (component_rank[component_index], component_index),
            )

    ordered_components: list[tuple[str, ...]] = []
    while ready:
        _rank, component_index = heappop(ready)
        ordered_components.append(components[component_index])
        for follower in sorted(
            component_following[component_index],
            key=component_rank.__getitem__,
        ):
            indegree[follower] -= 1
            if indegree[follower] == 0:
                heappush(ready, (component_rank[follower], follower))

    ordered_ids = tuple(
        dataset_id
        for component in ordered_components
        for dataset_id in component
    )
    if len(ordered_ids) != len(datasets):
        raise ValueError("dataset dependency component order is incomplete")
    return DatasetComponentOrder(
        ordered_dataset_ids=ordered_ids,
        components=tuple(ordered_components),
    )


def _ranked_datasets(
    datasets: tuple[str, ...],
    tie_break_order: Iterable[str] | None,
) -> tuple[str, ...]:
    if tie_break_order is None:
        return datasets
    preferred = tuple(tie_break_order)
    if len(set(preferred)) != len(preferred):
        raise ValueError("dataset tie-break order contains duplicates")
    unknown = set(preferred).difference(datasets)
    if unknown:
        raise ValueError("dataset tie-break order contains unknown datasets")
    preferred_set = set(preferred)
    return (*preferred, *(item for item in datasets if item not in preferred_set))


def _strong_components(
    datasets: tuple[str, ...],
    following: dict[str, set[str]],
    rank: dict[str, int],
) -> tuple[tuple[str, ...], ...]:
    """Return stable SCCs using iterative Kosaraju passes."""

    ordered_following = {
        dataset_id: tuple(sorted(owners, key=rank.__getitem__))
        for dataset_id, owners in following.items()
    }
    reverse: dict[str, list[str]] = {dataset_id: [] for dataset_id in datasets}
    for dependency, owners in ordered_following.items():
        for owner in owners:
            reverse[owner].append(dependency)
    for dependencies in reverse.values():
        dependencies.sort(key=rank.__getitem__)

    visited: set[str] = set()
    finish: list[str] = []
    for start in datasets:
        if start in visited:
            continue
        stack: list[tuple[str, bool]] = [(start, False)]
        while stack:
            dataset_id, expanded = stack.pop()
            if expanded:
                finish.append(dataset_id)
                continue
            if dataset_id in visited:
                continue
            visited.add(dataset_id)
            stack.append((dataset_id, True))
            for owner in reversed(ordered_following[dataset_id]):
                if owner not in visited:
                    stack.append((owner, False))

    assigned: set[str] = set()
    components: list[tuple[str, ...]] = []
    for start in reversed(finish):
        if start in assigned:
            continue
        members: set[str] = set()
        stack = [start]
        assigned.add(start)
        while stack:
            dataset_id = stack.pop()
            members.add(dataset_id)
            for dependency in reversed(reverse[dataset_id]):
                if dependency not in assigned:
                    assigned.add(dependency)
                    stack.append(dependency)
        components.append(tuple(sorted(members, key=rank.__getitem__)))
    return tuple(
        sorted(
            components,
            key=lambda component: min(rank[item] for item in component),
        )
    )
