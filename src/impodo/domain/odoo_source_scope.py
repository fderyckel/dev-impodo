"""Classify related Odoo data without expanding a capture implicitly.

The source workflow starts from business records chosen by the data manager.
This module turns their captured relationship metadata into a bounded proposal;
it does not traverse Odoo's model graph or change the saved model scope.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Iterable

from .workspace.contracts import SchemaField, SchemaModel


class RelatedDataHandling(StrEnum):
    """User decision Impodo needs for one relationship outside the scope."""

    INCLUDE_SUPPORTING = "INCLUDE_SUPPORTING"
    OPTIONAL_BUSINESS_DATA = "OPTIONAL_BUSINESS_DATA"
    REUSE_DESTINATION = "REUSE_DESTINATION"
    ODOO_MANAGED = "ODOO_MANAGED"
    SEPARATE_PROCESS = "SEPARATE_PROCESS"
    EXCLUDE_HISTORY = "EXCLUDE_HISTORY"
    NEEDS_DECISION = "NEEDS_DECISION"


@dataclass(frozen=True, slots=True)
class RelatedDataSuggestion:
    """One eligible source relationship classified for scope review."""

    source_model: str
    source_label: str
    field_name: str
    field_label: str
    relation_model: str
    required: bool
    handling: RelatedDataHandling


_DESTINATION_CONFIGURATION_MODELS = frozenset(
    {
        "account.account",
        "account.account.tag",
        "account.tax",
        "ir.sequence",
        "res.company",
        "res.currency",
        "stock.location",
        "stock.route",
        "stock.warehouse",
    }
)

_HISTORY_MODELS = frozenset(
    {
        "mail.activity",
        "mail.followers",
        "mail.message",
        "rating.rating",
    }
)

_SUPPORTING_RELATIONSHIPS = frozenset(
    {
        ("product.template", "categ_id"),
        ("product.template", "uom_id"),
        ("product.template", "uom_po_id"),
        ("uom.uom", "category_id"),
    }
)

_PRODUCT_OPTIONAL_MODELS = frozenset(
    {
        "ir.attachment",
        "product.combo",
        "product.packaging",
        "product.supplierinfo",
        "product.tag",
        "product.template.attribute.line",
    }
)

_PRODUCT_GENERATED_FIELDS = frozenset(
    {
        "product_variant_id",
        "product_variant_ids",
    }
)

_SEPARATE_PROCESS_MODELS = frozenset(
    {
        "mrp.bom",
        "mrp.bom.line",
        "mrp.eco",
        "planning.slot",
        "product.pricelist.item",
        "project.project",
    }
)


def propose_related_odoo_data(
    models: Iterable[SchemaModel],
) -> tuple[RelatedDataSuggestion, ...]:
    """Return deterministic suggestions for eligible links leaving the scope.

    Only relationships already present in captured schema evidence are
    considered. Unknown and custom relationships fail closed into an explicit
    decision instead of being followed automatically.
    """

    captured_models = tuple(models)
    selected_names = {model.name for model in captured_models}
    suggestions = tuple(
        RelatedDataSuggestion(
            source_model=model.name,
            source_label=model.label,
            field_name=field.name,
            field_label=field.label,
            relation_model=field.relation,
            required=field.required,
            handling=_classify_relationship(model.name, field),
        )
        for model in captured_models
        for field in model.fields
        if _is_scope_candidate(field, selected_names)
        and field.relation is not None
    )
    return tuple(
        sorted(
            suggestions,
            key=lambda item: (
                item.source_label.casefold(),
                item.field_label.casefold(),
                item.source_model,
                item.field_name,
            ),
        )
    )


def _is_scope_candidate(field: SchemaField, selected_names: set[str]) -> bool:
    """Keep the capture engine's existing closed relationship eligibility."""

    return bool(
        field.type in {"many2one", "many2many", "one2many"}
        and field.relation
        and field.relation not in selected_names
        and field.exportable is True
        and field.related is not True
        and field.company_dependent is False
        and (field.type == "one2many" or field.readonly is False)
    )


def _classify_relationship(
    source_model: str,
    field: SchemaField,
) -> RelatedDataHandling:
    """Apply the deliberately small, business-safe first scope policy."""

    assert field.relation is not None
    if field.relation in _HISTORY_MODELS:
        return RelatedDataHandling.EXCLUDE_HISTORY
    if (
        source_model == "product.template"
        and field.name in _PRODUCT_GENERATED_FIELDS
    ):
        return RelatedDataHandling.ODOO_MANAGED
    if field.relation in _DESTINATION_CONFIGURATION_MODELS:
        return RelatedDataHandling.REUSE_DESTINATION
    if field.relation in _SEPARATE_PROCESS_MODELS:
        return RelatedDataHandling.SEPARATE_PROCESS
    if (source_model, field.name) in _SUPPORTING_RELATIONSHIPS:
        return RelatedDataHandling.INCLUDE_SUPPORTING
    if (
        source_model == "product.template"
        and field.relation in _PRODUCT_OPTIONAL_MODELS
    ):
        return RelatedDataHandling.OPTIONAL_BUSINESS_DATA
    return RelatedDataHandling.NEEDS_DECISION
