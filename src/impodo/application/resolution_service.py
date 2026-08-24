"""Application orchestration for bounded duplicate review and effective data."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Protocol
from uuid import uuid4

from ..access import Actor
from ..domain.errors import ReadinessError
from ..domain.coverage import ReferenceBundle
from ..domain.resolution import (
    EffectiveDataset,
    ResolutionDecision,
    ResolutionDecisionKind,
    ResolutionEvaluation,
    ResolutionPolicy,
    build_effective_dataset,
    evaluate_resolution_candidates,
    resolution_group_id,
)
from ..domain.serialization import content_hash
from ..models import portable_value
from ..staging_contracts import CanonicalRow, CanonicalStagingRun


class ResolutionRepository(Protocol):
    """Persistence boundary used by target-independent preparation."""

    def get_resolution_policy(self, workspace_id: str) -> ResolutionPolicy | None: ...

    def get_validated_reference_bundle(
        self,
        workspace_id: str,
    ) -> ReferenceBundle | None: ...

    def publish_resolution_evaluation(
        self,
        workspace_id: str,
        evaluation: ResolutionEvaluation,
        *,
        staging_run_id: str,
        actor: Actor,
    ) -> "ResolutionSummary": ...

    def get_resolution_decisions(
        self,
        workspace_id: str,
        run_id: str,
    ) -> tuple[ResolutionDecision, ...]: ...

    def freeze_effective_dataset(
        self,
        workspace_id: str,
        run_id: str,
        effective: EffectiveDataset,
        *,
        expected_lifecycle_version: int,
        actor: Actor,
    ) -> "ResolutionSummary": ...

    def get_current_effective_dataset(
        self,
        workspace_id: str,
    ) -> EffectiveDataset | None: ...

    def get_current_resolution_summary(
        self,
        workspace_id: str,
    ) -> "ResolutionSummary | None": ...

    def get_resolution_evaluation(
        self,
        workspace_id: str,
        run_id: str,
    ) -> ResolutionEvaluation | None: ...

    def append_resolution_decision(
        self,
        workspace_id: str,
        run_id: str,
        decision: ResolutionDecision,
        *,
        expected_lifecycle_version: int,
        actor: Actor,
    ) -> "ResolutionSummary": ...


class ResolutionStagingRepository(Protocol):
    def get_canonical_staging_run(
        self,
        workspace_id: str,
        run_id: str,
        *,
        expected_content_hash: str | None = None,
    ) -> CanonicalStagingRun | None: ...


class ResolutionSummary(Protocol):
    run_id: str
    staging_run_id: str
    status: str
    lifecycle_version: int
    effective_content_hash: str | None


@dataclass(frozen=True, slots=True)
class ResolutionCandidateReview:
    candidate_id: str
    dataset: str
    score: str
    left: CanonicalRow
    right: CanonicalRow
    decision: str | None


@dataclass(frozen=True, slots=True)
class SurvivorFieldReview:
    group_id: str
    row_ids: tuple[str, ...]
    field: str
    choices: tuple[tuple[str, object], ...]
    decision: str | None
    correctable: bool


@dataclass(frozen=True, slots=True)
class ResolutionReview:
    summary: ResolutionSummary
    evaluation: ResolutionEvaluation
    candidates: tuple[ResolutionCandidateReview, ...]
    fields: tuple[SurvivorFieldReview, ...]
    decisions: tuple[ResolutionDecision, ...]


class ResolutionService:
    """Publish resolution evidence and return the exact quality input."""

    def __init__(
        self,
        repository: ResolutionRepository,
        staging: ResolutionStagingRepository | None = None,
    ) -> None:
        self.repository = repository
        self.staging = staging

    def current_reference_bundle(self, workspace_id: str) -> ReferenceBundle | None:
        return self.repository.get_validated_reference_bundle(workspace_id)

    def current_summary(self, workspace_id: str) -> ResolutionSummary | None:
        """Return the lightweight current lifecycle state for navigation."""

        return self.repository.get_current_resolution_summary(workspace_id)

    def evaluate_for_preparation(
        self,
        workspace_id: str,
        staging: CanonicalStagingRun,
        *,
        staging_run_id: str,
        staging_content_hash: str,
        actor: Actor,
    ) -> tuple[EffectiveDataset | None, ResolutionSummary | None]:
        """Resolve current prepared rows or pause for explicit duplicate review.

        Projects without an approved resolution policy use the direct
        pass-through path. Once a policy exists, even a zero-candidate result
        is frozen as an immutable effective dataset before quality runs.
        """

        policy = self.repository.get_resolution_policy(workspace_id)
        if policy is None:
            return None, None
        if (
            policy.mapping_hash != staging.mapping_hash
            or policy.schema_hash != staging.schema_hash
        ):
            raise ReadinessError(
                "The duplicate-review policy no longer matches the submitted mapping"
            )
        evaluation = evaluate_resolution_candidates(
            policy=policy,
            staging_content_hash=staging_content_hash,
            rows=staging.rows,
        )
        summary = self.repository.publish_resolution_evaluation(
            workspace_id,
            evaluation,
            staging_run_id=staging_run_id,
            actor=actor,
        )
        if summary.status == "BLOCKED":
            raise ReadinessError(
                "Duplicate checking is blocked by incomplete or overly broad matching fields"
            )
        if summary.status == "FROZEN":
            effective = self.repository.get_current_effective_dataset(workspace_id)
            if effective is None or effective.content_hash != summary.effective_content_hash:
                raise ReadinessError("Resolved data could not be verified")
            return effective, summary
        if evaluation.candidates:
            raise ReadinessError(
                "Review the possible duplicate records before continuing data checks"
            )
        decisions = self.repository.get_resolution_decisions(workspace_id, summary.run_id)
        effective = build_effective_dataset(
            policy=policy,
            evaluation=evaluation,
            rows=staging.rows,
            decisions=decisions,
        )
        frozen = self.repository.freeze_effective_dataset(
            workspace_id,
            summary.run_id,
            effective,
            expected_lifecycle_version=summary.lifecycle_version,
            actor=actor,
        )
        if frozen.effective_content_hash != effective.content_hash:
            raise ReadinessError("Resolved data could not be verified")
        return effective, frozen

    def current_review(self, workspace_id: str) -> ResolutionReview | None:
        if self.staging is None:
            raise ReadinessError("Duplicate review is not available")
        summary = self.repository.get_current_resolution_summary(workspace_id)
        if summary is None:
            return None
        evaluation = self.repository.get_resolution_evaluation(
            workspace_id,
            summary.run_id,
        )
        policy = self.repository.get_resolution_policy(workspace_id)
        staging = self.staging.get_canonical_staging_run(
            workspace_id,
            summary.staging_run_id,
            expected_content_hash=summary.staging_content_hash,
        )
        if evaluation is None or policy is None or staging is None:
            raise ReadinessError("Duplicate-review evidence is incomplete")
        decisions = self.repository.get_resolution_decisions(workspace_id, summary.run_id)
        rows = {item.row_id: item for item in staging.rows}
        pair_decisions = {
            tuple(item.row_ids): item
            for item in decisions
            if item.kind in {
                ResolutionDecisionKind.SAME_RECORD,
                ResolutionDecisionKind.KEEP_SEPARATE,
            }
        }
        candidates = tuple(
            ResolutionCandidateReview(
                candidate_id=item.candidate_id,
                dataset=item.dataset,
                score=item.score,
                left=rows[item.left_row_id],
                right=rows[item.right_row_id],
                decision=(
                    pair_decisions[(item.left_row_id, item.right_row_id)].kind.value
                    if (item.left_row_id, item.right_row_id) in pair_decisions
                    else None
                ),
            )
            for item in evaluation.candidates
        )
        fields = _survivor_field_reviews(policy, rows, decisions)
        return ResolutionReview(
            summary=summary,
            evaluation=evaluation,
            candidates=candidates,
            fields=fields,
            decisions=decisions,
        )

    def decide_pair(
        self,
        workspace_id: str,
        run_id: str,
        candidate_id: str,
        *,
        same_record: bool,
        expected_lifecycle_version: int,
        actor: Actor,
        reason: str,
    ) -> ResolutionSummary:
        evaluation = self.repository.get_resolution_evaluation(workspace_id, run_id)
        if evaluation is None:
            raise ReadinessError("Duplicate review is no longer available")
        candidate = next(
            (item for item in evaluation.candidates if item.candidate_id == candidate_id),
            None,
        )
        if candidate is None:
            raise ReadinessError("Possible-duplicate pair is no longer available")
        decision = ResolutionDecision(
            decision_id=str(uuid4()),
            evaluation_hash=evaluation.content_hash,
            group_id=candidate.candidate_id,
            kind=(
                ResolutionDecisionKind.SAME_RECORD
                if same_record
                else ResolutionDecisionKind.KEEP_SEPARATE
            ),
            row_ids=(candidate.left_row_id, candidate.right_row_id),
            reason=reason,
            actor=actor.identity,
            decided_at=datetime.now(timezone.utc),
            lifecycle_version=expected_lifecycle_version + 1,
        )
        return self.repository.append_resolution_decision(
            workspace_id,
            run_id,
            decision,
            expected_lifecycle_version=expected_lifecycle_version,
            actor=actor,
        )

    def select_survivor_field(
        self,
        workspace_id: str,
        run_id: str,
        group_id: str,
        field: str,
        row_ids: tuple[str, ...],
        *,
        selected_row_id: str,
        expected_lifecycle_version: int,
        actor: Actor,
        reason: str,
    ) -> ResolutionSummary:
        evaluation = self.repository.get_resolution_evaluation(workspace_id, run_id)
        if evaluation is None:
            raise ReadinessError("Duplicate review is no longer available")
        decision = ResolutionDecision(
            decision_id=str(uuid4()),
            evaluation_hash=evaluation.content_hash,
            group_id=group_id,
            kind=ResolutionDecisionKind.SELECT_SOURCE,
            row_ids=tuple(sorted(row_ids)),
            field=field,
            selected_row_id=selected_row_id,
            reason=reason,
            actor=actor.identity,
            decided_at=datetime.now(timezone.utc),
            lifecycle_version=expected_lifecycle_version + 1,
        )
        return self.repository.append_resolution_decision(
            workspace_id,
            run_id,
            decision,
            expected_lifecycle_version=expected_lifecycle_version,
            actor=actor,
        )

    def correct_survivor_field(
        self,
        workspace_id: str,
        run_id: str,
        group_id: str,
        field: str,
        row_ids: tuple[str, ...],
        *,
        replacement_text: str,
        expected_lifecycle_version: int,
        actor: Actor,
        reason: str,
    ) -> ResolutionSummary:
        """Record one typed correction allowed by the approved policy."""

        review = self.current_review(workspace_id)
        if review is None or review.summary.run_id != run_id:
            raise ReadinessError("Duplicate review is no longer current")
        field_review = next(
            (
                item
                for item in review.fields
                if item.group_id == group_id and item.field == field
            ),
            None,
        )
        if field_review is None or not field_review.correctable:
            raise ReadinessError("This field is not approved for reviewer correction")
        if field_review.row_ids != tuple(sorted(row_ids)):
            raise ReadinessError("Correction rows no longer match the review group")
        exemplar = next(
            (value for _, value in field_review.choices if value is not None),
            None,
        )
        replacement = _typed_replacement(exemplar, replacement_text)
        decision = ResolutionDecision(
            decision_id=str(uuid4()),
            evaluation_hash=review.evaluation.content_hash,
            group_id=group_id,
            kind=ResolutionDecisionKind.REVIEWER_CORRECTION,
            row_ids=field_review.row_ids,
            field=field,
            replacement_value=replacement,
            reason=reason,
            actor=actor.identity,
            decided_at=datetime.now(timezone.utc),
            lifecycle_version=expected_lifecycle_version + 1,
        )
        return self.repository.append_resolution_decision(
            workspace_id,
            run_id,
            decision,
            expected_lifecycle_version=expected_lifecycle_version,
            actor=actor,
        )

    def approve(
        self,
        workspace_id: str,
        run_id: str,
        *,
        expected_lifecycle_version: int,
        actor: Actor,
    ) -> ResolutionSummary:
        review = self.current_review(workspace_id)
        policy = self.repository.get_resolution_policy(workspace_id)
        if review is None or policy is None or review.summary.run_id != run_id:
            raise ReadinessError("Duplicate review is no longer current")
        if self.staging is None:
            raise ReadinessError("Duplicate review is not available")
        staging = self.staging.get_canonical_staging_run(
            workspace_id,
            review.summary.staging_run_id,
            expected_content_hash=review.summary.staging_content_hash,
        )
        if staging is None:
            raise ReadinessError("Prepared rows could not be verified")
        try:
            effective = build_effective_dataset(
                policy=policy,
                evaluation=review.evaluation,
                rows=staging.rows,
                decisions=review.decisions,
            )
        except ValueError as error:
            raise ReadinessError(str(error)) from error
        return self.repository.freeze_effective_dataset(
            workspace_id,
            run_id,
            effective,
            expected_lifecycle_version=expected_lifecycle_version,
            actor=actor,
        )


def _survivor_field_reviews(
    policy: ResolutionPolicy,
    rows: dict[str, CanonicalRow],
    decisions: tuple[ResolutionDecision, ...],
) -> tuple[SurvivorFieldReview, ...]:
    parent = {row_id: row_id for row_id in rows}

    def find(row_id: str) -> str:
        while parent[row_id] != row_id:
            parent[row_id] = parent[parent[row_id]]
            row_id = parent[row_id]
        return row_id

    def union(left: str, right: str) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            first, second = sorted((left_root, right_root))
            parent[second] = first

    for decision in decisions:
        if decision.kind is ResolutionDecisionKind.SAME_RECORD:
            union(*decision.row_ids)
    groups: dict[str, list[str]] = {}
    for row_id in rows:
        groups.setdefault(find(row_id), []).append(row_id)
    field_decisions = {
        (item.group_id, item.field): item
        for item in decisions
        if item.field is not None
    }
    reviews: list[SurvivorFieldReview] = []
    for group_rows in groups.values():
        if len(group_rows) < 2:
            continue
        row_ids = tuple(sorted(group_rows))
        group_id = resolution_group_id(policy.content_hash, row_ids)
        rule = next(item for item in policy.rules if item.dataset == rows[row_ids[0]].dataset)
        field_names = (*rule.survivor_fields, "__identity__", "__scope__", "__references__")
        for field in field_names:
            choices = tuple(
                (
                    row_id,
                    rows[row_id].proposed_values.get(field)
                    if not field.startswith("__")
                    else {
                        "__identity__": rows[row_id].target_identity,
                        "__scope__": rows[row_id].target_scope,
                        "__references__": rows[row_id].references,
                    }[field],
                )
                for row_id in row_ids
            )
            if len({content_hash(portable_value(value)) for _, value in choices}) == 1:
                continue
            decision = field_decisions.get((group_id, field))
            reviews.append(
                SurvivorFieldReview(
                    group_id=group_id,
                    row_ids=row_ids,
                    field=field,
                    choices=choices,
                    decision=(decision.kind.value if decision else None),
                    correctable=field in rule.correctable_fields,
                )
            )
    return tuple(sorted(reviews, key=lambda item: (item.group_id, item.field)))


def _typed_replacement(exemplar: object, text: str) -> object:
    """Parse a browser correction without introducing a second rule language."""

    value = text.strip()
    if exemplar is None:
        raise ReadinessError(
            "A blank field needs a typed source value before it can be corrected here"
        )
    if isinstance(exemplar, bool):
        normalized = value.casefold()
        if normalized not in {"true", "false"}:
            raise ReadinessError("Enter true or false for this field")
        return normalized == "true"
    if isinstance(exemplar, int):
        try:
            return int(value)
        except ValueError as error:
            raise ReadinessError("Enter a whole number for this field") from error
    if isinstance(exemplar, Decimal):
        try:
            result = Decimal(value)
        except InvalidOperation as error:
            raise ReadinessError("Enter a decimal number for this field") from error
        if not result.is_finite():
            raise ReadinessError("Enter a finite decimal number for this field")
        return result
    if isinstance(exemplar, datetime):
        try:
            return datetime.fromisoformat(value)
        except ValueError as error:
            raise ReadinessError("Enter an ISO date and time for this field") from error
    if isinstance(exemplar, date):
        try:
            return date.fromisoformat(value)
        except ValueError as error:
            raise ReadinessError("Enter an ISO date for this field") from error
    if isinstance(exemplar, str):
        if not value:
            raise ReadinessError("A corrected text value cannot be blank")
        return value
    raise ReadinessError("This field type cannot be corrected in duplicate review")
