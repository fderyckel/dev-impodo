from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
import unittest
from uuid import uuid4

from impodo.application.transfer_order_service import TransferOrderService
from impodo.domain.workspace.destination_matching import (
    DestinationMatchPlan,
    DestinationModelMatch,
    DestinationRelationshipMatch,
)
from impodo.domain.workspace.transfer_order import TransferOrderPlan
from impodo.domain.shared.models import target_identity_hash
from impodo.domain.workspace.errors import WorkspaceError
from impodo.domain.workspace.workbench import (
    OdooConnectionMode,
    SourceMode,
    WorkspaceState,
    WorkspaceStatus,
)


HASHES = tuple("sha256:" + character * 64 for character in "abcdef123")


class TransferOrderTests(unittest.TestCase):
    def test_required_uom_dependency_runs_before_product(self) -> None:
        product = _model("product.template", "Product", create=2)
        uom = _model("uom.uom", "Unit of Measure", existing=1, create=1)
        relation = _relation(product, uom, "uom_id", required=True)
        match = _match_plan((product, uom), (relation,))

        plan = _build(match)

        self.assertTrue(plan.ready)
        self.assertEqual(
            tuple(
                tuple(
                    next(item.model for item in plan.datasets if item.dataset_id == key)
                    for key in wave.dataset_ids
                )
                for wave in plan.waves
            ),
            (("uom.uom",), ("product.template",)),
        )
        self.assertEqual(plan.dependencies[0].strength, "hard")
        self.assertFalse(plan.dependencies[0].deferred)
        self.assertEqual(TransferOrderPlan.from_json(plan.to_json()), plan)

    def test_optional_cross_model_cycle_defers_only_one_link(self) -> None:
        alpha = _model("x.alpha", "Alpha", create=1)
        beta = _model("x.beta", "Beta", create=1)
        match = _match_plan(
            (alpha, beta),
            (
                _relation(alpha, beta, "beta_id"),
                _relation(beta, alpha, "alpha_id"),
            ),
        )

        plan = _build(match)

        self.assertTrue(plan.ready)
        self.assertEqual(plan.deferred_dependency_count, 1)
        deferred = next(item for item in plan.dependencies if item.deferred)
        self.assertEqual((deferred.owner_model, deferred.field_name), ("x.alpha", "beta_id"))
        self.assertEqual(
            tuple(
                next(item.model for item in plan.datasets if item.dataset_id == wave.dataset_ids[0])
                for wave in plan.waves
            ),
            ("x.alpha", "x.beta"),
        )

    def test_optional_self_relation_uses_post_create_pass(self) -> None:
        location = _model("stock.location", "Location", create=3)
        match = _match_plan(
            (location,),
            (_relation(location, location, "location_id"),),
        )

        plan = _build(match)

        self.assertTrue(plan.ready)
        self.assertEqual(len(plan.waves), 1)
        self.assertTrue(plan.dependencies[0].deferred)
        self.assertTrue(plan.dependencies[0].is_self_reference)

    def test_many2many_uses_the_same_generic_dependency_schedule(self) -> None:
        bundle = _model("x.bundle", "Bundle", create=1)
        item = _model("x.item", "Item", create=2)
        match = _match_plan(
            (bundle, item),
            (_relation(bundle, item, "item_ids", kind="many2many"),),
        )

        plan = _build(match)

        self.assertTrue(plan.ready)
        self.assertEqual(plan.dependencies[0].kind, "many2many")
        self.assertEqual(plan.dependencies[0].strength, "deferrable")
        self.assertFalse(plan.dependencies[0].deferred)
        self.assertEqual(
            tuple(
                next(
                    dataset.model
                    for dataset in plan.datasets
                    if dataset.dataset_id == wave.dataset_ids[0]
                )
                for wave in plan.waves
            ),
            ("x.item", "x.bundle"),
        )

    def test_required_cycle_blocks_cycle_and_downstream_owner(self) -> None:
        alpha = _model("x.alpha", "Alpha", create=1)
        beta = _model("x.beta", "Beta", create=1)
        child = _model("x.child", "Child", create=1)
        match = _match_plan(
            (alpha, beta, child),
            (
                _relation(alpha, beta, "beta_id", required=True),
                _relation(beta, alpha, "alpha_id", required=True),
                _relation(child, alpha, "alpha_id", required=True),
            ),
        )

        plan = _build(match)

        self.assertFalse(plan.ready)
        blockers = {item.dataset_id: item.code for item in plan.blockers}
        self.assertEqual(blockers[alpha.dataset_id], "HARD_DEPENDENCY_CYCLE")
        self.assertEqual(blockers[beta.dataset_id], "HARD_DEPENDENCY_CYCLE")
        self.assertEqual(blockers[child.dataset_id], "BLOCKED_DEPENDENCY")
        self.assertTrue(all(item.wave is None for item in plan.datasets))

    def test_existing_only_relationship_adds_no_ordering_edge(self) -> None:
        product = _model("product.template", "Product", create=1)
        uom = _model("uom.uom", "Unit of Measure", existing=1)
        relation = replace(
            _relation(product, uom, "uom_id", required=True),
            destination_reused_link_count=1,
            incoming_link_count=0,
        )
        match = _match_plan((product, uom), (relation,))

        plan = _build(match)

        self.assertTrue(plan.ready)
        self.assertFalse(plan.dependencies)
        self.assertEqual(len(plan.waves), 1)
        self.assertEqual(len(plan.waves[0].dataset_ids), 2)

    def test_reverified_destination_requires_fresh_matching(self) -> None:
        product = _model("product.template", "Product", create=1)
        match = _match_plan((product,), ())
        workspace = replace(
            _workspace(match),
            destination_verified_at=match.recorded_at + timedelta(seconds=1),
        )

        with self.assertRaisesRegex(
            WorkspaceError,
            "Complete current destination matching first",
        ):
            TransferOrderService().build(
                workspace,
                match,
                recorded_by="Data manager",
            )


def _model(
    model: str,
    label: str,
    *,
    existing: int = 0,
    create: int = 0,
) -> DestinationModelMatch:
    row_count = existing + create
    return DestinationModelMatch(
        dataset_id=str(uuid4()),
        dataset_name=f"{label} source",
        model=model,
        model_label=label,
        source_column_key=f"{model}-key-column",
        key_field="name",
        key_field_label="Name",
        source_row_count=row_count,
        source_distinct_key_count=row_count,
        source_blank_row_count=0,
        source_duplicate_key_count=0,
        destination_existing_key_count=existing,
        destination_duplicate_key_count=0,
        destination_create_key_count=create,
        destination_key_binding_hash="sha256:" + "0" * 64,
        compatible_fields=("name",),
        missing_fields=(),
        incompatible_fields=(),
        destination_limit_reached=False,
    )


def _relation(
    owner: DestinationModelMatch,
    dependency: DestinationModelMatch,
    field_name: str,
    *,
    required: bool = False,
    kind: str = "many2one",
) -> DestinationRelationshipMatch:
    return DestinationRelationshipMatch(
        dataset_id=owner.dataset_id,
        dataset_name=owner.dataset_name,
        model=owner.model,
        model_label=owner.model_label,
        field_name=field_name,
        field_label=field_name.replace("_", " ").title(),
        kind=kind,
        related_dataset_id=dependency.dataset_id,
        related_dataset_name=dependency.dataset_name,
        related_model=dependency.model,
        related_model_label=dependency.model_label,
        related_key_field=dependency.key_field,
        operation="set" if kind == "many2one" else "replace",
        inverse_field=None,
        source_owner_count=max(1, owner.source_row_count),
        source_link_count=1,
        source_blank_owner_count=0,
        destination_reused_link_count=0,
        incoming_link_count=1,
        missing_related_record_count=0,
        ambiguous_destination_link_count=0,
        source_evidence_available=True,
        required=required,
    )


def _match_plan(
    models: tuple[DestinationModelMatch, ...],
    relationships: tuple[DestinationRelationshipMatch, ...],
) -> DestinationMatchPlan:
    return DestinationMatchPlan(
        workspace_id=str(uuid4()),
        source_selection_hash=HASHES[0],
        source_schema_hash=HASHES[1],
        destination_target_hash=target_identity_hash(
            connection_mode="REMOTE",
            base_url="https://destination.example.test",
            database="destination",
        ),
        destination_credential_binding_hash=HASHES[3],
        destination_read_principal_hash=HASHES[4],
        destination_read_permission_hash=HASHES[5],
        destination_read_context_hash=HASHES[6],
        destination_schema_snapshot_hash=HASHES[7],
        destination_record_snapshot_hash=HASHES[8],
        model_matches=tuple(sorted(models, key=lambda item: item.model)),
        relationship_matches=tuple(
            sorted(relationships, key=lambda item: (item.model, item.field_name))
        ),
        recorded_at=datetime.now(UTC),
        recorded_by="Data manager",
    )


def _build(match: DestinationMatchPlan) -> TransferOrderPlan:
    return TransferOrderService().build(
        _workspace(match),
        match,
        recorded_by="Data manager",
    )


def _workspace(match: DestinationMatchPlan) -> WorkspaceState:
    return WorkspaceState(
        workspace_id=match.workspace_id,
        name="Odoo transfer",
        source_system="Odoo",
        source_mode=SourceMode.ODOO,
        status=WorkspaceStatus.REGISTERED,
        destination_odoo_connection_mode=OdooConnectionMode.REMOTE,
        destination_odoo_base_url="https://destination.example.test",
        destination_odoo_database="destination",
        destination_verified_target_hash=match.destination_target_hash,
        destination_verified_credential_binding_hash=(
            match.destination_credential_binding_hash
        ),
        destination_verified_read_principal_hash=(
            match.destination_read_principal_hash
        ),
        destination_verified_odoo_version="19.0",
        destination_verified_at=match.recorded_at,
        destination_match_plan=match,
    )


if __name__ == "__main__":
    unittest.main()
