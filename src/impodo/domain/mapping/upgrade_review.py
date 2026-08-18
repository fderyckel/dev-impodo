"""Deterministic focused review boundary for mapping contracts v8-v10."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum

from ...workspace_contracts import OdooSchemaCatalog
from ..serialization import content_hash
from .contracts import MappingDefinition, ScalarValueSource


class MappingUpgradeOutcome(StrEnum):
    """State whether a mapping may cross the v11 recipe boundary."""

    CURRENT = "CURRENT"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    UNSUPPORTED = "UNSUPPORTED"


@dataclass(frozen=True, slots=True)
class LegacyCategoricalReviewItem:
    """One legacy field whose formerly inferred domain needs confirmation."""

    path: str
    dataset_id: str
    target_field: str
    kind: str
    previous_behavior: str
    allowed_policies: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class MappingContractUpgradeReview:
    """Hashable review projection; it never mutates or reinterprets old JSON."""

    mapping_content_hash: str
    source_contract_version: int
    outcome: MappingUpgradeOutcome
    categorical_items: tuple[LegacyCategoricalReviewItem, ...]
    unsupported_reasons: tuple[str, ...] = ()
    contract_version: int = 1

    @property
    def content_hash(self) -> str:
        return content_hash(self.to_dict())

    @property
    def recipe_eligible(self) -> bool:
        return self.outcome is MappingUpgradeOutcome.CURRENT

    def to_dict(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "mapping_content_hash": self.mapping_content_hash,
            "source_contract_version": self.source_contract_version,
            "outcome": self.outcome.value,
            "categorical_items": [
                asdict(item) for item in self.categorical_items
            ],
            "unsupported_reasons": list(self.unsupported_reasons),
        }


def review_mapping_contract_upgrade(
    definition: MappingDefinition,
    schema: OdooSchemaCatalog,
) -> MappingContractUpgradeReview:
    """List every legacy categorical choice without selecting policy for it."""

    if definition.contract_version >= 11:
        return MappingContractUpgradeReview(
            mapping_content_hash=definition.content_hash,
            source_contract_version=definition.contract_version,
            outcome=MappingUpgradeOutcome.CURRENT,
            categorical_items=(),
        )
    schema_fields = {
        (model.name, field.name): field
        for model in schema.models
        for field in model.fields
    }
    items: list[LegacyCategoricalReviewItem] = []
    unsupported: list[str] = []
    for dataset_index, dataset in enumerate(definition.datasets):
        for field_index, field in enumerate(dataset.fields):
            metadata = schema_fields.get((dataset.target_model, field.target_field))
            if (
                metadata is None
                or metadata.type != "selection"
                or field.value_source is ScalarValueSource.ODOO_DEFAULT
            ):
                continue
            items.append(
                LegacyCategoricalReviewItem(
                    path=f"/datasets/{dataset_index}/fields/{field_index}",
                    dataset_id=dataset.dataset_id,
                    target_field=field.target_field,
                    kind="scalar_selection",
                    previous_behavior=(
                        "matched aliases otherwise passed through unchanged"
                        if field.value_mappings
                        else "passed through unchanged"
                    ),
                    allowed_policies=(
                        "EXACT_TARGET_VALUE",
                        "EXPLICIT_VALUE_MATCH",
                    ),
                )
            )
        for relation_index, relation in enumerate(dataset.relationships):
            items.append(
                LegacyCategoricalReviewItem(
                    path=(
                        f"/datasets/{dataset_index}/relationships/{relation_index}"
                    ),
                    dataset_id=dataset.dataset_id,
                    target_field=relation.target_field,
                    kind="relationship_business_key",
                    previous_behavior=(
                        "matched aliases otherwise resolved the original key"
                        if relation.resolver.value_mappings
                        else "resolved the original key"
                    ),
                    allowed_policies=(
                        "EXACT_BUSINESS_KEY",
                        "EXPLICIT_KEY_MATCH",
                    ),
                )
            )
        for component_index, component in enumerate(
            (*dataset.target_identity, *dataset.target_scope)
        ):
            if component.resolver is not None and component.resolver.value_mappings:
                unsupported.append(
                    f"/datasets/{dataset_index}/identity/{component_index}: "
                    "identity resolver value matches need a reviewed converter"
                )
    outcome = (
        MappingUpgradeOutcome.UNSUPPORTED
        if unsupported
        else MappingUpgradeOutcome.REVIEW_REQUIRED
    )
    return MappingContractUpgradeReview(
        mapping_content_hash=definition.content_hash,
        source_contract_version=definition.contract_version,
        outcome=outcome,
        categorical_items=tuple(
            sorted(items, key=lambda item: (item.path, item.target_field))
        ),
        unsupported_reasons=tuple(sorted(unsupported)),
    )
