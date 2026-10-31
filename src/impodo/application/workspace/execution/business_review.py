"""Project expected versus observed effects without retaining customer rows.

Comparison publication owns these bounded facts. Ordinary pages consume the
projection; only an authorized detail query reads field rules and approvals.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace

from impodo.domain.workspace.business_intent import BusinessIntentRevision
from impodo.domain.workspace.business_intent_assessment import (
    BusinessIntentAssessment,
    BusinessWriteObservation,
)

MAX_BUSINESS_REVIEW_MODELS = 20


@dataclass(frozen=True, slots=True)
class BusinessModelReview:
    """Show one record type's count expectations and affected-field totals."""

    model: str
    label: str
    mode: str
    creates: int
    updates: int
    create_min: int | None
    create_max: int | None
    update_min: int | None
    update_max: int | None
    value_field_count: int
    relationship_field_count: int
    deviation_count: int


@dataclass(frozen=True, slots=True)
class BusinessIntentReview:
    """Carry no values or complete field sets into the normal browser journey."""

    state: str
    snapshot_hash: str
    assessment_hash: str
    intent_hash: str
    intent_version: int | None
    model_count: int
    deviation_count: int
    message: str
    models: tuple[BusinessModelReview, ...]
    contract_version: int = 1

    def __post_init__(self) -> None:
        if (
            self.state not in {"READY", "DEVIATION", "MISSING", "STALE"}
            or len(self.models) > MAX_BUSINESS_REVIEW_MODELS
            or self.model_count < len(self.models)
            or self.contract_version != 1
        ):
            raise ValueError("Business review projection is invalid")

    @property
    def ready(self) -> bool:
        return self.state == "READY"

    def stale(self) -> BusinessIntentReview:
        return replace(
            self,
            state="STALE",
            assessment_hash="",
            message="The expectations changed. Review them against the saved comparison.",
        )

    @classmethod
    def from_dict(cls, payload: dict) -> BusinessIntentReview:
        return cls(
            **{
                **payload,
                "models": tuple(
                    BusinessModelReview(**item) for item in payload["models"]
                ),
            }
        )


def _business_model_reviews(
    observation: BusinessWriteObservation,
    revision: BusinessIntentRevision | None,
    assessment: BusinessIntentAssessment,
    *,
    labels: Mapping[str, str],
) -> tuple[BusinessModelReview, ...]:
    """Aggregate all model and field facts once at publication, not at render."""
    found = {item.model: item for item in observation.models}
    rules = {item.model: item for item in revision.intent.models} if revision else {}
    models = []
    for model in sorted(found.keys() | rules.keys()):
        rule, observed = rules.get(model), found.get(model)
        models.append(
            BusinessModelReview(
                model,
                labels.get(model, model),
                rule.mode.value if rule else "none",
                observed.creates if observed else 0,
                observed.updates if observed else 0,
                rule.creates.minimum if rule else None,
                rule.creates.maximum if rule else None,
                rule.updates.minimum if rule else None,
                rule.updates.maximum if rule else None,
                len({*observed.create_fields, *observed.update_fields})
                if observed
                else 0,
                len(
                    {
                        item.field
                        for item in (
                            *observed.create_relationships,
                            *observed.update_relationships,
                        )
                    }
                )
                if observed
                else 0,
                sum(
                    message.startswith(model + ":") for message in assessment.deviations
                ),
            )
        )
    return tuple(models)


def build_business_intent_review(
    observation: BusinessWriteObservation,
    revision: BusinessIntentRevision | None,
    assessment: BusinessIntentAssessment,
    *,
    labels: Mapping[str, str],
) -> BusinessIntentReview:
    """Publish a small first page and exact aggregate state with its receipt hash."""
    models = _business_model_reviews(observation, revision, assessment, labels=labels)
    return BusinessIntentReview(
        "MISSING" if revision is None else "READY" if assessment.ready else "DEVIATION",
        observation.snapshot_hash,
        assessment.content_hash,
        revision.content_hash if revision else "",
        revision.version if revision else None,
        len(models),
        len(assessment.deviations),
        "The proposed changes meet your saved expectations."
        if assessment.ready
        else "Save an explicit choice for each record type before loading."
        if revision is None
        else "The proposed changes differ from your expectations. Review the affected record types.",
        tuple(models[:MAX_BUSINESS_REVIEW_MODELS]),
    )


@dataclass(frozen=True, slots=True)
class BusinessFieldReview:
    """Describe one intended field effect with its authored operation."""

    phase: str
    field: str
    operation: str
    allowed: bool
    proposed: bool = True


@dataclass(frozen=True, slots=True)
class BusinessIntentEvidencePage:
    """Return one bounded model page; the database adapter retains full evidence."""

    review: BusinessIntentReview
    model: BusinessModelReview | None
    fields: tuple[BusinessFieldReview, ...]
    page: int
    page_count: int
    field_count: int
    author: str
    approved_by: str
    run_id: str
    models: tuple[BusinessModelReview, ...]
    specified_rules: tuple[str, ...]


def business_intent_evidence_page(
    observation: BusinessWriteObservation,
    revision: BusinessIntentRevision | None,
    assessment: BusinessIntentAssessment,
    *,
    model: str,
    page: int,
    labels: Mapping[str, str],
    approved_by: str = "",
    run_id: str = "",
) -> BusinessIntentEvidencePage:
    """Page metadata effects; references remain technical names, never record IDs."""
    review = build_business_intent_review(
        observation, revision, assessment, labels=labels
    )
    rule = (
        next((item for item in revision.intent.models if item.model == model), None)
        if revision
        else None
    )
    observed = next((item for item in observation.models if item.model == model), None)
    if model and observed is None and rule is None:
        raise ValueError("Choose a record type from this assessment")
    fields: list[BusinessFieldReview] = []
    specified: list[str] = []
    for phase in ("create", "update"):
        mode_allows = rule is not None and getattr(rule.mode, phase + "s")
        allowed_fields = getattr(rule, phase + "_fields") if rule else ()
        allowed_relations = getattr(rule, phase + "_relationships") if rule else ()
        if rule:
            specified.append(
                f"{phase.title()} value fields: "
                + (
                    "follow the checked matching rules"
                    if allowed_fields is None
                    else f"{len(allowed_fields)} named field(s)"
                )
            )
            specified.append(
                f"{phase.title()} linked fields: "
                + (
                    "follow the checked matching rules"
                    if allowed_relations is None
                    else f"{len(allowed_relations)} named field(s)"
                )
            )
        proposed_fields = (
            set(getattr(observed, phase + "_fields")) if observed else set()
        )
        fields.extend(
            BusinessFieldReview(
                phase,
                name,
                "value",
                mode_allows and (allowed_fields is None or name in allowed_fields),
                name in proposed_fields,
            )
            for name in sorted(proposed_fields | set(allowed_fields or ()))
        )
        allowed_pairs = {
            (item.field, operation)
            for item in allowed_relations or ()
            for operation in item.operations
        }
        proposed_pairs = (
            {
                (item.field, operation)
                for item in getattr(observed, phase + "_relationships")
                for operation in item.operations
            }
            if observed
            else set()
        )
        fields.extend(
            BusinessFieldReview(
                phase,
                field,
                operation.value,
                mode_allows
                and (allowed_relations is None or (field, operation) in allowed_pairs),
                (field, operation) in proposed_pairs,
            )
            for field, operation in sorted(allowed_pairs | proposed_pairs)
        )
    fields.sort(key=lambda item: (item.phase, item.field, item.operation))
    page_count = max(1, (len(fields) + 49) // 50)
    if page < 1 or page > page_count:
        raise ValueError("Choose a current field page")
    all_models = _business_model_reviews(
        observation, revision, assessment, labels=labels
    )
    model_review = next((item for item in all_models if item.model == model), None)
    return BusinessIntentEvidencePage(
        review,
        model_review,
        tuple(fields[(page - 1) * 50 : page * 50]),
        page,
        page_count,
        len(fields),
        revision.author.display_name if revision else "",
        approved_by,
        run_id,
        all_models,
        tuple(specified),
    )
