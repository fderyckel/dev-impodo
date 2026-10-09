from __future__ import annotations

from datetime import UTC, datetime
import unittest
from uuid import uuid4

from impodo.domain.mapping.contracts import MappingTargetMode, ResolverOrigin
from impodo.domain.odoo_relationship_scope import (
    OdooRelationshipCaptureAction,
    OdooRelationshipScope,
    OdooRelationshipScopeDecision,
)
from impodo.domain.odoo_source_scope import RelatedDataHandling
from impodo.domain.relationship_dependencies import (
    DependencyStrength,
    extract_dataset_dependency_edges,
)
from impodo.domain.shared.models import target_identity_hash
from impodo.domain.workspace.destination_matching import (
    DestinationMatchPlan,
    DestinationModelMatch,
    DestinationRelationshipMatch,
)
from impodo.domain.workspace.odoo_relationship_compilation import (
    OdooRelationshipCompilationError,
    compile_odoo_relationship_datasets,
)


class OdooRelationshipCompilationTests(unittest.TestCase):
    def test_compiles_transfer_edge_to_target_then_dataset(self) -> None:
        line = _model("mrp.bom.line", ("code-column",), ("x_code",))
        product = _model(
            "product.product",
            ("default-code-column", "company-column"),
            ("default_code", "company_code"),
        )
        relationship = _relationship(
            line,
            product,
            "product_id",
            required=True,
        )

        datasets = compile_odoo_relationship_datasets(
            _plan((line, product), (relationship,)),
            relationship_scope=_scope(
                _decision(line.model, "product_id", product.model)
            ),
        )

        owner = next(item for item in datasets if item.dataset_id == line.dataset_id)
        compiled = owner.relationships[0]
        self.assertEqual(compiled.resolver.origin, ResolverOrigin.TARGET_THEN_DATASET)
        self.assertEqual(compiled.resolver.dataset_id, product.dataset_id)
        self.assertEqual(
            tuple(
                (item.source_column_key, item.target_field)
                for item in compiled.resolver.key_mappings
            ),
            (
                ("default-code-column", "default_code"),
                ("company-column", "company_code"),
            ),
        )
        edges = extract_dataset_dependency_edges(datasets)
        self.assertEqual(len(edges), 1)
        self.assertEqual(edges[0].owner_dataset, line.dataset_id)
        self.assertEqual(edges[0].dependency_dataset, product.dataset_id)
        self.assertEqual(edges[0].strength, DependencyStrength.HARD)

    def test_compiles_reference_only_edge_to_target_catalog(self) -> None:
        bom = _model("mrp.bom", ("code-column",), ("code",))
        company = _model(
            "res.company",
            ("name-column",),
            ("name",),
            destination_handling="reference_only",
        )
        relationship = _relationship(bom, company, "company_id")

        datasets = compile_odoo_relationship_datasets(
            _plan((bom, company), (relationship,)),
            relationship_scope=_scope(
                OdooRelationshipScopeDecision(
                    source_model=bom.model,
                    field_name="company_id",
                    relation_model=company.model,
                    required=False,
                    handling=RelatedDataHandling.REUSE_DESTINATION,
                    action=OdooRelationshipCaptureAction.MATCH_EXISTING,
                )
            ),
        )

        owner = next(item for item in datasets if item.dataset_id == bom.dataset_id)
        resolver = owner.relationships[0].resolver
        self.assertEqual(resolver.origin, ResolverOrigin.TARGET_CATALOG)
        self.assertIsNone(resolver.dataset_id)
        reference = next(
            item for item in datasets if item.dataset_id == company.dataset_id
        )
        self.assertEqual(reference.mode, MappingTargetMode.REFERENCE)
        self.assertFalse(extract_dataset_dependency_edges(datasets))

    def test_preserve_linked_compiles_to_reuse_or_create_minimum(self) -> None:
        line = _model("mrp.bom.line", ("code-column",), ("x_code",))
        product = _model(
            "product.product",
            ("default-code-column",),
            ("default_code",),
            destination_handling="reuse_or_create",
        )
        relationship = _relationship(
            line,
            product,
            "product_id",
            required=True,
        )
        decision = OdooRelationshipScopeDecision(
            source_model=line.model,
            field_name="product_id",
            relation_model=product.model,
            required=True,
            handling=RelatedDataHandling.NEEDS_DECISION,
            action=OdooRelationshipCaptureAction.PRESERVE_LINKED,
        )

        datasets = compile_odoo_relationship_datasets(
            _plan((line, product), (relationship,)),
            relationship_scope=_scope(decision),
        )

        owner = next(item for item in datasets if item.dataset_id == line.dataset_id)
        compiled = owner.relationships[0]
        self.assertEqual(
            compiled.resolver.origin,
            ResolverOrigin.TARGET_THEN_DATASET,
        )
        self.assertEqual(compiled.resolver.dataset_id, product.dataset_id)
        related = next(
            item for item in datasets if item.dataset_id == product.dataset_id
        )
        self.assertEqual(related.mode, MappingTargetMode.CREATE)
        self.assertEqual(related.on_existing, "unchanged")

    def test_match_existing_decision_rejects_transfer_handling(self) -> None:
        bom = _model("mrp.bom", ("code-column",), ("code",))
        company = _model("res.company", ("name-column",), ("name",))
        relationship = _relationship(bom, company, "company_id")
        decision = _decision(
            bom.model,
            "company_id",
            company.model,
            handling=RelatedDataHandling.REUSE_DESTINATION,
        )
        decision = OdooRelationshipScopeDecision(
            source_model=decision.source_model,
            field_name=decision.field_name,
            relation_model=decision.relation_model,
            required=decision.required,
            handling=decision.handling,
            action=OdooRelationshipCaptureAction.MATCH_EXISTING,
        )

        with self.assertRaisesRegex(
            OdooRelationshipCompilationError,
            "destination writes are not allowed",
        ):
            compile_odoo_relationship_datasets(
                _plan((bom, company), (relationship,)),
                relationship_scope=_scope(decision),
            )

    def test_inverse_capture_decision_authorizes_child_owner_edge(self) -> None:
        parent = _model("x.parent", ("name-column",), ("name",))
        child = _model("x.child", ("code-column",), ("code",))
        relationship = _relationship(
            child,
            parent,
            "parent_id",
            inverse_field="child_ids",
        )

        datasets = compile_odoo_relationship_datasets(
            _plan((parent, child), (relationship,)),
            relationship_scope=_scope(
                _decision(parent.model, "child_ids", child.model)
            ),
        )

        owner = next(item for item in datasets if item.dataset_id == child.dataset_id)
        self.assertEqual(owner.relationships[0].target_field, "parent_id")

    def test_rejects_relationship_excluded_by_saved_source_decision(self) -> None:
        owner = _model("x.owner", ("name-column",), ("name",))
        related = _model("x.related", ("code-column",), ("code",))
        relationship = _relationship(owner, related, "related_id")
        decision = _decision(owner.model, "related_id", related.model)
        decision = OdooRelationshipScopeDecision(
            source_model=decision.source_model,
            field_name=decision.field_name,
            relation_model=decision.relation_model,
            required=decision.required,
            handling=decision.handling,
            action=OdooRelationshipCaptureAction.DO_NOT_CAPTURE,
        )

        with self.assertRaisesRegex(
            OdooRelationshipCompilationError,
            "does not include x.owner.related_id",
        ):
            compile_odoo_relationship_datasets(
                _plan((owner, related), (relationship,)),
                relationship_scope=_scope(decision),
            )

    def test_rejects_unreviewed_non_self_relationship(self) -> None:
        owner = _model("x.owner", ("name-column",), ("name",))
        related = _model("x.related", ("code-column",), ("code",))

        with self.assertRaisesRegex(
            OdooRelationshipCompilationError,
            "Review the source relationship scope for x.owner.related_id",
        ):
            compile_odoo_relationship_datasets(
                _plan(
                    (owner, related),
                    (_relationship(owner, related, "related_id"),),
                ),
                relationship_scope=_scope(),
            )


def _model(
    model: str,
    source_columns: tuple[str, ...],
    key_fields: tuple[str, ...],
    *,
    destination_handling: str = "transfer",
) -> DestinationModelMatch:
    return DestinationModelMatch(
        dataset_id=str(uuid4()),
        dataset_name=f"{model} source",
        model=model,
        model_label=model,
        source_column_key=source_columns[0],
        source_column_keys=source_columns,
        key_field=key_fields[0],
        key_fields=key_fields,
        key_field_label=key_fields[0],
        source_row_count=1,
        source_distinct_key_count=1,
        source_blank_row_count=0,
        source_duplicate_key_count=0,
        destination_existing_key_count=0,
        destination_duplicate_key_count=0,
        destination_create_key_count=1,
        destination_key_binding_hash="sha256:" + "0" * 64,
        compatible_fields=(),
        missing_fields=(),
        incompatible_fields=(),
        destination_limit_reached=False,
        destination_handling=destination_handling,
    )


def _relationship(
    owner: DestinationModelMatch,
    related: DestinationModelMatch,
    field_name: str,
    *,
    required: bool = False,
    inverse_field: str | None = None,
) -> DestinationRelationshipMatch:
    return DestinationRelationshipMatch(
        dataset_id=owner.dataset_id,
        dataset_name=owner.dataset_name,
        model=owner.model,
        model_label=owner.model_label,
        field_name=field_name,
        field_label=field_name,
        kind="many2one",
        related_dataset_id=related.dataset_id,
        related_dataset_name=related.dataset_name,
        related_model=related.model,
        related_model_label=related.model_label,
        related_key_field=related.key_field,
        related_key_fields=related.key_fields,
        operation="set",
        inverse_field=inverse_field,
        source_owner_count=1,
        source_link_count=1,
        source_blank_owner_count=0,
        destination_reused_link_count=(
            1 if related.destination_handling != "transfer" else 0
        ),
        incoming_link_count=(
            0 if related.destination_handling != "transfer" else 1
        ),
        missing_related_record_count=0,
        ambiguous_destination_link_count=0,
        source_evidence_available=True,
        required=required,
    )


def _decision(
    source_model: str,
    field_name: str,
    relation_model: str,
    *,
    handling: RelatedDataHandling = RelatedDataHandling.NEEDS_DECISION,
) -> OdooRelationshipScopeDecision:
    return OdooRelationshipScopeDecision(
        source_model=source_model,
        field_name=field_name,
        relation_model=relation_model,
        required=False,
        handling=handling,
        action=OdooRelationshipCaptureAction.CAPTURE_LINKED,
    )


def _scope(
    *decisions: OdooRelationshipScopeDecision,
) -> OdooRelationshipScope:
    return OdooRelationshipScope.create(
        scope_id=str(uuid4()),
        version=1,
        decisions=tuple(
            sorted(decisions, key=lambda item: (*item.identity, item.relation_model))
        ),
        recorded_at=datetime.now(UTC),
        recorded_by="Data manager",
    )


def _plan(
    models: tuple[DestinationModelMatch, ...],
    relationships: tuple[DestinationRelationshipMatch, ...],
) -> DestinationMatchPlan:
    hashes = tuple("sha256:" + character * 64 for character in "abcdef12")
    return DestinationMatchPlan(
        workspace_id=str(uuid4()),
        source_selection_hash=hashes[0],
        source_schema_hash=hashes[1],
        destination_target_hash=target_identity_hash(
            connection_mode="REMOTE",
            base_url="https://destination.example.test",
            database="destination",
        ),
        destination_credential_binding_hash=hashes[2],
        destination_read_principal_hash=hashes[3],
        destination_read_permission_hash=hashes[4],
        destination_read_context_hash=hashes[5],
        destination_schema_snapshot_hash=hashes[6],
        destination_record_snapshot_hash=hashes[7],
        model_matches=tuple(sorted(models, key=lambda item: item.model)),
        relationship_matches=tuple(
            sorted(relationships, key=lambda item: (item.model, item.field_name))
        ),
        recorded_at=datetime.now(UTC),
        recorded_by="Data manager",
    )


if __name__ == "__main__":
    unittest.main()
