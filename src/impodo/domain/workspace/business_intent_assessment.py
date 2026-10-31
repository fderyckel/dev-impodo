"""Assess business effects without changing execution snapshot meaning.

Observations are produced once from the exact verified snapshot and compiled
relationship rules. They contain counts and technical names, never row values
or target identifiers. Reassessment reuses this immutable bounded evidence.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass

from impodo.domain.execution_snapshot import ExecutionSnapshot
from impodo.domain.serialization import canonical_json, content_hash
from impodo.domain.shared.models import canonical_json_bytes, portable_value

from .business_intent import (
    BusinessIntentError,
    BusinessIntentRevision,
    BusinessRelationshipOperation,
    BusinessRelationshipRule,
)


@dataclass(frozen=True, slots=True)
class ObservedModelWrites:
    """Count business records once per phase, including deferred create fields."""

    model: str
    creates: int
    updates: int
    create_fields: tuple[str, ...]
    update_fields: tuple[str, ...]
    create_relationships: tuple[BusinessRelationshipRule, ...]
    update_relationships: tuple[BusinessRelationshipRule, ...]


@dataclass(frozen=True, slots=True)
class BusinessWriteObservation:
    """Bind reusable aggregate effects to exact comparison and reduced scope."""

    workspace_id: str
    preflight_run_id: str
    snapshot_hash: str
    mapping_hash: str
    prepared_hash: str
    comparison_hash: str
    target_hash: str
    reduced_scope_hash: str
    models: tuple[ObservedModelWrites, ...]
    authored_operations: tuple[tuple[str, str, str], ...]
    contract_version: int = 1

    @property
    def content_hash(self) -> str:
        return content_hash(asdict(self))

    def to_json(self) -> str:
        return canonical_json({**asdict(self), "content_hash": self.content_hash})

    @classmethod
    def from_json(cls, value: str) -> BusinessWriteObservation:
        raw = json.loads(value)
        expected = raw.pop("content_hash")
        models = []
        for item in raw["models"]:
            for name in ("create_fields", "update_fields"):
                item[name] = tuple(item[name])
            for name in ("create_relationships", "update_relationships"):
                item[name] = tuple(
                    BusinessRelationshipRule(**rule) for rule in item[name]
                )
            models.append(ObservedModelWrites(**item))
        raw["models"] = tuple(models)
        raw["authored_operations"] = tuple(
            tuple(item) for item in raw["authored_operations"]
        )
        result = cls(**raw)
        if (
            type(result.contract_version) is not int
            or result.contract_version != 1
            or result.content_hash != expected
        ):
            raise BusinessIntentError(
                "Business write observation hash or version is invalid"
            )
        return result


@dataclass(frozen=True, slots=True)
class BusinessIntentAssessmentSummary:
    """Carry only a verified hash, readiness and one blocker to ordinary pages."""

    content_hash: str
    ready: bool
    deviations: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BusinessIntentAssessment:
    """Describe deviations; acknowledgement cannot convert them into permission."""

    workspace_id: str
    preflight_run_id: str
    snapshot_hash: str
    target_hash: str
    observation_hash: str
    intent_version: int | None
    intent_hash: str | None
    deviations: tuple[str, ...]
    contract_version: int = 1

    @property
    def ready(self) -> bool:
        return self.intent_hash is not None and not self.deviations

    @property
    def content_hash(self) -> str:
        return content_hash(asdict(self))

    def to_json(self) -> str:
        return canonical_json({**asdict(self), "content_hash": self.content_hash})

    @classmethod
    def from_json(cls, value: str) -> BusinessIntentAssessment:
        raw = json.loads(value)
        expected = raw.pop("content_hash")
        raw["deviations"] = tuple(raw["deviations"])
        result = cls(**raw)
        if (
            type(result.contract_version) is not int
            or result.contract_version != 1
            or result.content_hash != expected
        ):
            raise BusinessIntentError(
                "Business intent assessment hash or version is invalid"
            )
        return result


def observe_business_writes(
    snapshot: ExecutionSnapshot,
    *,
    authored_operations: tuple[tuple[str, str, str], ...],
    reduced_scope_hash: str = "",
) -> BusinessWriteObservation:
    """Reduce exact intended effects once; referenced models remain reads only.

    Update relationship payloads already contain their final replacement value.
    The compiled authored operation preserves the add/remove business meaning.
    Deferred completions remain fields of their original create, never updates.
    """
    operations = {
        (dataset, field): operation for dataset, field, operation in authored_operations
    }
    records: dict[tuple[str, str], set[bytes]] = {}
    scalars: dict[tuple[str, str], set[str]] = {}
    relationships: dict[tuple[str, str], dict[str, set[str]]] = {}
    models = {dataset.target_model for dataset in snapshot.datasets}
    for row in snapshot.rows:
        if row.disposition not in {"CREATE", "UPDATE"}:
            continue
        models.add(row.target_model)
        phase = row.disposition.lower()
        key = (row.target_model, phase)
        identity = canonical_json_bytes(
            portable_value((row.business_identity, row.business_scope))
        )
        if not row.business_identity:
            identity = canonical_json_bytes(row.proposed_external_id or row.row_id)
        records.setdefault(key, set()).add(identity)
        for effect in row.fields:
            if effect.action == "OMIT":
                continue
            if effect.action not in {"SET_VALUE", "SET_NULL"}:
                raise BusinessIntentError(
                    "The snapshot contains an unsupported field effect"
                )
            if effect.kind == "scalar":
                scalars.setdefault(key, set()).add(effect.field)
            elif effect.kind == "relation":
                # Relational create identities have no authored mapping rule.
                operation = operations.get((row.dataset, effect.field))
                if operation is None and phase == "create":
                    operation = effect.relation_operation
                if operation is None:
                    raise BusinessIntentError(
                        "The authored relationship operation is missing"
                    )
                relationships.setdefault(key, {}).setdefault(effect.field, set()).add(
                    operation
                )
            else:
                raise BusinessIntentError(
                    "The snapshot contains an unsupported field kind"
                )

    def relation_rules(model: str, phase: str) -> tuple[BusinessRelationshipRule, ...]:
        return tuple(
            BusinessRelationshipRule(
                name,
                tuple(
                    BusinessRelationshipOperation(operation)
                    for operation in sorted(ops)
                ),
            )
            for name, ops in sorted(relationships.get((model, phase), {}).items())
        )

    return BusinessWriteObservation(
        snapshot.workspace_id,
        snapshot.preflight_run_id,
        snapshot.semantic_hash,
        snapshot.mapping_content_hash,
        snapshot.frozen_input_hash,
        snapshot.preflight_result_hash,
        snapshot.target_hash,
        reduced_scope_hash,
        tuple(
            ObservedModelWrites(
                model,
                len(records.get((model, "create"), ())),
                len(records.get((model, "update"), ())),
                tuple(sorted(scalars.get((model, "create"), ()))),
                tuple(sorted(scalars.get((model, "update"), ()))),
                relation_rules(model, "create"),
                relation_rules(model, "update"),
            )
            for model in sorted(models)
        ),
        tuple(sorted(authored_operations)),
    )


def assess_business_intent(
    observation: BusinessWriteObservation,
    revision: BusinessIntentRevision | None,
) -> BusinessIntentAssessment:
    """Apply closed rules and all count limits, including zero observed records."""
    deviations: list[str] = []
    if revision is None:
        deviations.append(
            "Save explicit business expectations before approving this load"
        )
    elif (
        revision.workspace_id != observation.workspace_id
        or revision.binding.mapping_hash != observation.mapping_hash
    ):
        deviations.append(
            "Business expectations belong to another checked mapping; review and save them again"
        )
    else:
        observed = {item.model: item for item in observation.models}
        rules = {item.model: item for item in revision.intent.models}
        for model in sorted(observed.keys() | rules.keys()):
            found, rule = observed.get(model), rules.get(model)
            for phase in ("create", "update"):
                count = getattr(found, phase + "s") if found else 0
                allowed = rule is not None and getattr(rule.mode, phase + "s")
                if count and not allowed:
                    deviations.append(
                        f"{model}: {phase} is forbidden ({count} record(s))"
                    )
                if rule is None:
                    continue
                limits = getattr(rule, phase + "s")
                if limits.minimum is not None and count < limits.minimum:
                    deviations.append(
                        f"{model}: {phase} count {count} is below minimum {limits.minimum}"
                    )
                if limits.maximum is not None and count > limits.maximum:
                    deviations.append(
                        f"{model}: {phase} count {count} exceeds maximum {limits.maximum}"
                    )
                if found is None:
                    continue
                fields, permitted = (
                    getattr(found, phase + "_fields"),
                    getattr(rule, phase + "_fields"),
                )
                if permitted is not None:
                    deviations.extend(
                        f"{model}: {phase} of field {name} is forbidden"
                        for name in sorted(set(fields) - set(permitted))
                    )
                permitted_relations = getattr(rule, phase + "_relationships")
                if permitted_relations is not None:
                    by_field = {
                        item.field: set(item.operations) for item in permitted_relations
                    }
                    for relationship in getattr(found, phase + "_relationships"):
                        for operation in sorted(
                            set(relationship.operations)
                            - by_field.get(relationship.field, set())
                        ):
                            deviations.append(
                                f"{model}: {phase} relationship {relationship.field} operation {operation} is forbidden"
                            )
    return BusinessIntentAssessment(
        observation.workspace_id,
        observation.preflight_run_id,
        observation.snapshot_hash,
        observation.target_hash,
        observation.content_hash,
        revision.version if revision else None,
        revision.content_hash if revision else None,
        tuple(deviations),
    )
