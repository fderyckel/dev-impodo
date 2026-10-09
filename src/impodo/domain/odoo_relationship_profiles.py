"""Recommend common Odoo relationship handling without owning capabilities.

The source-scope engine discovers relationships from captured metadata.  A
profile may recommend familiar business handling for an exact model and field,
but it cannot make an ineligible relationship capturable or remove a safe user
choice.  Profiles are ordered by their caller so an organization profile can
precede Impodo's standard profile without changing the discovery engine.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .workspace.contracts import SchemaField


class OdooRelationshipRecommendation(StrEnum):
    """One profile recommendation understood by Odoo source scope."""

    INCLUDE_SUPPORTING = "INCLUDE_SUPPORTING"
    OPTIONAL_BUSINESS_DATA = "OPTIONAL_BUSINESS_DATA"
    REUSE_DESTINATION = "REUSE_DESTINATION"
    ODOO_MANAGED = "ODOO_MANAGED"
    SEPARATE_PROCESS = "SEPARATE_PROCESS"
    EXCLUDE_HISTORY = "EXCLUDE_HISTORY"


@dataclass(frozen=True, slots=True)
class OdooRelationshipProfile:
    """Versioned recommendations for exact Odoo relationship identities."""

    profile_id: str
    version: int
    destination_configuration_models: frozenset[str] = frozenset()
    history_models: frozenset[str] = frozenset()
    supporting_relationships: frozenset[tuple[str, str]] = frozenset()
    optional_relationships: frozenset[tuple[str, str]] = frozenset()
    generated_relationships: frozenset[tuple[str, str]] = frozenset()
    separate_process_models: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if not self.profile_id.strip() or self.version < 1:
            raise ValueError("Odoo relationship profile identity is invalid")

    def recommend(
        self,
        source_model: str,
        field: SchemaField,
    ) -> OdooRelationshipRecommendation | None:
        """Return this profile's first exact recommendation for one edge."""

        relation = field.relation
        if relation is None:
            return None
        if relation in self.history_models:
            return OdooRelationshipRecommendation.EXCLUDE_HISTORY
        if (source_model, field.name) in self.generated_relationships:
            return OdooRelationshipRecommendation.ODOO_MANAGED
        if relation in self.destination_configuration_models:
            return OdooRelationshipRecommendation.REUSE_DESTINATION
        if (source_model, field.name) in self.supporting_relationships:
            return OdooRelationshipRecommendation.INCLUDE_SUPPORTING
        if relation in self.separate_process_models:
            return OdooRelationshipRecommendation.SEPARATE_PROCESS
        if (source_model, relation) in self.optional_relationships:
            return OdooRelationshipRecommendation.OPTIONAL_BUSINESS_DATA
        return None


STANDARD_ODOO_RELATIONSHIP_PROFILE = OdooRelationshipProfile(
    profile_id="impodo.standard.odoo.relationships",
    version=2,
    destination_configuration_models=frozenset(
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
    ),
    history_models=frozenset(
        {
            "mail.activity",
            "mail.followers",
            "mail.message",
            "rating.rating",
        }
    ),
    supporting_relationships=frozenset(
        {
            ("product.template", "categ_id"),
            ("product.template", "uom_id"),
            ("product.template", "uom_po_id"),
            ("mrp.bom", "bom_line_ids"),
            ("mrp.bom", "operation_ids"),
            ("uom.uom", "category_id"),
        }
    ),
    optional_relationships=frozenset(
        ("product.template", relation)
        for relation in {
            "ir.attachment",
            "product.combo",
            "product.packaging",
            "product.supplierinfo",
            "product.tag",
            "product.template.attribute.line",
        }
    ),
    generated_relationships=frozenset(
        {
            ("product.template", "product_variant_id"),
            ("product.template", "product_variant_ids"),
        }
    ),
    separate_process_models=frozenset(
        {
            "mrp.bom",
            "mrp.bom.line",
            "mrp.eco",
            "planning.slot",
            "product.pricelist.item",
            "project.project",
        }
    ),
)

DEFAULT_ODOO_RELATIONSHIP_PROFILES = (STANDARD_ODOO_RELATIONSHIP_PROFILE,)


__all__ = [
    "DEFAULT_ODOO_RELATIONSHIP_PROFILES",
    "OdooRelationshipProfile",
    "OdooRelationshipRecommendation",
    "STANDARD_ODOO_RELATIONSHIP_PROFILE",
]
