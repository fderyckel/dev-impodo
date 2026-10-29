"""Propose bounded supporting dependencies from accepted source descriptors.

This pure query retains each model/field edge and its evidence. Incoming
candidates are proposals, never write scope or permission for an Odoo read.
The older direct-model derivation remains the browser's current contract.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import asdict, dataclass
from enum import StrEnum

from impodo.domain.serialization import content_hash
from impodo.domain.workspace.contracts import SchemaModel
from impodo.domain.workspace.supporting_models import (
    SupportingModelRole,
    supporting_model_role,
)

SUPPORTING_PLAN_VERSION = 1
MAX_SUPPORTING_MODELS = 100
MAX_SUPPORTING_EDGES = 1_000
MAX_SUPPORTING_DEPTH = 5
MAX_SUPPORTING_DATASETS = 1_000
MAX_SUPPORTING_RULES = 200


class SupportingPlanRole(StrEnum):
    """Describe the proposed business treatment of one relationship."""

    INCOMING_CANDIDATE = "INCOMING_CANDIDATE"
    REUSE_EXISTING = "REUSE_EXISTING"
    CHECKED_DEFAULT = "CHECKED_DEFAULT"
    ODOO_MANAGED = "ODOO_MANAGED"
    UNRESOLVED = "UNRESOLVED"


class SupportingPlanGap(StrEnum):
    """Explain why a proposal cannot claim complete dependency coverage."""

    MODEL_LIMIT = "MODEL_LIMIT"
    EDGE_LIMIT = "EDGE_LIMIT"
    DEPTH_LIMIT = "DEPTH_LIMIT"
    INPUT_LIMIT = "INPUT_LIMIT"
    METADATA_UNAVAILABLE = "METADATA_UNAVAILABLE"
    AMBIGUOUS_INCOMING = "AMBIGUOUS_INCOMING"
    INVALID_INTENT = "INVALID_INTENT"
    INCOMING_UNAVAILABLE = "INCOMING_UNAVAILABLE"


@dataclass(frozen=True, slots=True)
class SupportingSourceDescriptor:
    """Describe an accepted mapping candidate without rows or preview values."""

    dataset_id: str
    label: str
    origin: str
    evidence_hash: str
    target_model: str | None = None


@dataclass(frozen=True, slots=True, order=True)
class SupportingDatasetIntent:
    """Explicitly propose a dataset/model binding for these exact inputs."""

    dataset_id: str
    target_model: str


@dataclass(frozen=True, slots=True, order=True)
class SupportingRelationshipIntent:
    """Declare use of an optional edge and optionally select its incoming table."""

    source_model: str
    field_name: str
    incoming_dataset_id: str = ""


@dataclass(frozen=True, slots=True)
class SupportingPlanIssue:
    """Locate a missing decision, unsupported input, or bounded walk limit."""

    reason: SupportingPlanGap
    source_model: str = ""
    field_name: str = ""
    related_model: str = ""


@dataclass(frozen=True, slots=True)
class SupportingPlanEdge:
    """Explain one required, inverse-owned, or explicitly intended edge."""

    source_model: str
    source_label: str
    field_name: str
    field_label: str
    field_type: str
    related_model: str
    inverse_field: str | None
    required: bool
    reason: str
    role: SupportingPlanRole
    candidates: tuple[SupportingSourceDescriptor, ...]
    depth: int


@dataclass(frozen=True, slots=True)
class SupportingDataPlan:
    """Bind a deterministic read-only proposal to its complete input evidence."""

    selected_models: tuple[str, ...]
    source_selection_hash: str
    schema_hash: str
    preparation_hash: str | None
    intent_hash: str
    edges: tuple[SupportingPlanEdge, ...]
    issues: tuple[SupportingPlanIssue, ...]
    contract_version: int = SUPPORTING_PLAN_VERSION

    @property
    def complete(self) -> bool:
        """State whether all justified edges were covered without a gap."""
        return not self.issues

    @property
    def content_hash(self) -> str:
        """Identify the proposal without adding approval or actor semantics."""
        return content_hash(asdict(self))


def plan_supporting_data(
    *,
    selected_models: tuple[str, ...],
    models: tuple[SchemaModel, ...],
    datasets: tuple[SupportingSourceDescriptor, ...],
    source_selection_hash: str,
    schema_hash: str,
    preparation_hash: str | None,
    dataset_intents: tuple[SupportingDatasetIntent, ...] = (),
    relationship_intents: tuple[SupportingRelationshipIntent, ...] = (),
    input_limited: bool = False,
) -> SupportingDataPlan:
    """Walk only selected models and uniquely proposed incoming dependencies.

    Inputs are metadata snapshots, not readers. Model and dataset indexes are
    built once; each reached model and each distinct relationship is visited
    at most once. An ambiguous incoming choice stops expansion at that edge.
    """
    roots = tuple(sorted(set(selected_models)))
    bindings = tuple(sorted(set(dataset_intents)))
    intents = tuple(sorted(set(relationship_intents)))
    intent_hash = content_hash(
        {
            "datasets": [asdict(item) for item in bindings],
            "relationships": [asdict(item) for item in intents],
        }
    )
    issues: set[SupportingPlanIssue] = set()
    edges: list[SupportingPlanEdge] = []
    if (
        input_limited
        or len(datasets) > MAX_SUPPORTING_DATASETS
        or len(bindings) > MAX_SUPPORTING_DATASETS
        or len(intents) > MAX_SUPPORTING_EDGES
    ):
        issues.add(SupportingPlanIssue(SupportingPlanGap.INPUT_LIMIT))
    else:
        _walk(roots, models, datasets, bindings, intents, edges, issues)
    return SupportingDataPlan(
        selected_models=roots,
        source_selection_hash=source_selection_hash,
        schema_hash=schema_hash,
        preparation_hash=preparation_hash,
        intent_hash=intent_hash,
        edges=tuple(
            sorted(edges, key=lambda item: (item.source_model, item.field_name))
        ),
        issues=tuple(
            sorted(
                issues,
                key=lambda item: (
                    item.reason.value,
                    item.source_model,
                    item.field_name,
                    item.related_model,
                ),
            )
        ),
    )


def _walk(
    roots: tuple[str, ...],
    models: tuple[SchemaModel, ...],
    datasets: tuple[SupportingSourceDescriptor, ...],
    bindings: tuple[SupportingDatasetIntent, ...],
    intents: tuple[SupportingRelationshipIntent, ...],
    edges: list[SupportingPlanEdge],
    issues: set[SupportingPlanIssue],
) -> None:
    model_by_name = {model.name: model for model in models}
    dataset_by_id = {item.dataset_id: item for item in datasets}
    if len(model_by_name) != len(models) or len(dataset_by_id) != len(datasets):
        issues.add(SupportingPlanIssue(SupportingPlanGap.INVALID_INTENT))
        return
    bound_models: dict[str, set[str]] = defaultdict(set)
    for item in datasets:
        if item.target_model:
            bound_models[item.dataset_id].add(item.target_model)
    for binding in bindings:
        if binding.dataset_id not in dataset_by_id or not binding.target_model:
            issues.add(SupportingPlanIssue(SupportingPlanGap.INVALID_INTENT))
        else:
            bound_models[binding.dataset_id].add(binding.target_model)
    candidates: dict[str, list[SupportingSourceDescriptor]] = defaultdict(list)
    for dataset_id, names in bound_models.items():
        if len(names) != 1:
            issues.add(SupportingPlanIssue(SupportingPlanGap.INVALID_INTENT))
            continue
        candidates[next(iter(names))].append(dataset_by_id[dataset_id])
    by_edge: dict[tuple[str, str], list[SupportingRelationshipIntent]] = defaultdict(
        list
    )
    for intent in intents:
        by_edge[(intent.source_model, intent.field_name)].append(intent)
    pending = deque((name, 0) for name in roots)
    visited: set[str] = set()
    used_intents: set[tuple[str, str]] = set()
    while pending:
        name, depth = pending.popleft()
        if name in visited:
            continue
        if len(visited) >= MAX_SUPPORTING_MODELS:
            issues.add(SupportingPlanIssue(SupportingPlanGap.MODEL_LIMIT, name))
            break
        visited.add(name)
        model = model_by_name.get(name)
        if model is None:
            issues.add(
                SupportingPlanIssue(SupportingPlanGap.METADATA_UNAVAILABLE, name)
            )
            continue
        for field in sorted(model.fields, key=lambda item: item.name):
            key = (name, field.name)
            explicit = by_edge.get(key, [])
            eligible = (
                field.type in {"many2one", "many2many", "one2many"}
                and field.relation
                and field.exportable is not False
                and field.company_dependent is not True
            )
            if not eligible or not (
                field.required
                or explicit
                or (field.type == "one2many" and field.relation_field)
            ):
                continue
            if len(edges) >= MAX_SUPPORTING_EDGES:
                issues.add(SupportingPlanIssue(SupportingPlanGap.EDGE_LIMIT, name))
                return
            used_intents.add(key)
            related = field.relation
            if related is None:
                continue
            available = tuple(
                sorted(candidates.get(related, ()), key=lambda item: item.dataset_id)
            )
            direct_role = supporting_model_role(field)
            role = (
                SupportingPlanRole.UNRESOLVED
                if direct_role is SupportingModelRole.REVIEW_INCOMING
                else SupportingPlanRole(direct_role.value)
            )
            if role is SupportingPlanRole.ODOO_MANAGED:
                available = ()
            elif explicit and (
                len(explicit) > 1
                or (
                    explicit[0].incoming_dataset_id
                    and not any(
                        item.dataset_id == explicit[0].incoming_dataset_id
                        for item in available
                    )
                )
            ):
                role = SupportingPlanRole.UNRESOLVED
                issues.add(
                    SupportingPlanIssue(
                        SupportingPlanGap.INVALID_INTENT, name, field.name, related
                    )
                )
            else:
                if explicit and explicit[0].incoming_dataset_id:
                    available = tuple(
                        item
                        for item in available
                        if item.dataset_id == explicit[0].incoming_dataset_id
                    )
                if available and (
                    role is not SupportingPlanRole.CHECKED_DEFAULT
                    or (explicit and explicit[0].incoming_dataset_id)
                ):
                    role = (
                        SupportingPlanRole.INCOMING_CANDIDATE
                        if len(available) == 1
                        else SupportingPlanRole.UNRESOLVED
                    )
                    if len(available) > 1:
                        issues.add(
                            SupportingPlanIssue(
                                SupportingPlanGap.AMBIGUOUS_INCOMING,
                                name,
                                field.name,
                                related,
                            )
                        )
                elif direct_role is SupportingModelRole.REVIEW_INCOMING:
                    issues.add(
                        SupportingPlanIssue(
                            SupportingPlanGap.INCOMING_UNAVAILABLE,
                            name,
                            field.name,
                            related,
                        )
                    )
            edges.append(
                SupportingPlanEdge(
                    name,
                    model.label,
                    field.name,
                    field.label,
                    field.type,
                    related,
                    field.relation_field,
                    field.required,
                    "EXPLICIT_RELATIONSHIP"
                    if explicit
                    else "REQUIRED_RELATIONSHIP"
                    if field.required
                    else "INVERSE_CHILD",
                    role,
                    available,
                    depth,
                )
            )
            if role is SupportingPlanRole.INCOMING_CANDIDATE and related not in visited:
                if depth >= MAX_SUPPORTING_DEPTH:
                    issues.add(
                        SupportingPlanIssue(
                            SupportingPlanGap.DEPTH_LIMIT, name, field.name, related
                        )
                    )
                else:
                    pending.append((related, depth + 1))
    for key in by_edge.keys() - used_intents:
        issues.add(SupportingPlanIssue(SupportingPlanGap.INVALID_INTENT, *key))
