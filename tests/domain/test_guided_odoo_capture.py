from __future__ import annotations

from datetime import datetime, timezone
import unittest

from impodo.domain.guided_odoo_capture import (
    GuidedOdooCaptureIssueCode,
    GuidedOdooCaptureMode,
    propose_guided_odoo_capture_plans,
)
from impodo.domain.odoo_capture import (
    MAX_ODOO_CAPTURE_DATASETS,
    MAX_ODOO_CAPTURE_FIELDS,
    OdooCaptureFilterPolicy,
    OdooCaptureRole,
    OdooCaptureSelection,
)
from impodo.domain.odoo_relationship_scope import (
    OdooRelationshipCaptureAction,
    OdooRelationshipScope,
    OdooRelationshipScopeDecision,
)
from impodo.domain.odoo_source_policy import ODOO_SOURCE_POLICY_HASH
from impodo.domain.odoo_source_scope import RelatedDataHandling
from impodo.domain.shared.models import UniqueConstraintMetadata
from impodo.domain.workspace.contracts import (
    OdooSchemaCatalog,
    SchemaField,
    SchemaModel,
    SchemaOrigin,
)


HASH = "sha256:" + "a" * 64
WORKSPACE_ID = "00000000-0000-0000-0000-000000000001"
DATA_VERSION_ID = "00000000-0000-0000-0000-000000000002"


class GuidedOdooCapturePlanTests(unittest.TestCase):
    def test_full_root_plan_captures_every_eligible_scalar_field(self) -> None:
        model = _model(
            "x.asset",
            (
                _field("name", "Name", required=True),
                _field("created_at", "Created at", readonly=True),
                _field("write_date", "Last updated", readonly=True),
                _field("owner_id", "Owner", field_type="many2one", relation="x.owner"),
            ),
        )

        proposal = propose_guided_odoo_capture_plans(
            _schema(model),
            _scope(root_models=(model.name,)),
        )

        self.assertTrue(proposal.complete)
        self.assertEqual(len(proposal.drafts), 1)
        draft = proposal.drafts[0]
        self.assertIs(draft.mode, GuidedOdooCaptureMode.FULL)
        self.assertIs(draft.capture_role, OdooCaptureRole.ROOT)
        self.assertEqual(draft.field_names, ("created_at", "name"))

    def test_modes_keep_company_identity_only_and_minimum_create_small(self) -> None:
        root = _model("x.root", (_field("name", "Name"),))
        full_child = _model(
            "x.full.child",
            (
                _field("name", "Name"),
                _field("description", "Description"),
            ),
        )
        company = _model(
            "res.company",
            (
                _field("name", "Name", required=True),
                _field("vat", "Tax ID"),
            ),
        )
        product = _model(
            "product.template",
            (
                _field("default_code", "Internal Reference"),
                _field("name", "Name"),
                _field("required_note", "Required note", required=True),
                _field("optional_note", "Optional note"),
                _field(
                    "readonly_required",
                    "Readonly required",
                    required=True,
                    readonly=True,
                ),
                _field(
                    "product_variant_ids",
                    "Variants",
                    field_type="one2many",
                    relation="product.product",
                    required=True,
                ),
            ),
        )
        scope = _scope(
            root_models=(root.name,),
            decisions=(
                _decision(
                    root.name,
                    "full_child_id",
                    full_child.name,
                    OdooRelationshipCaptureAction.CAPTURE_LINKED,
                ),
                _decision(
                    root.name,
                    "company_id",
                    company.name,
                    OdooRelationshipCaptureAction.MATCH_EXISTING,
                    handling=RelatedDataHandling.REUSE_DESTINATION,
                ),
                _decision(
                    root.name,
                    "product_id",
                    product.name,
                    OdooRelationshipCaptureAction.PRESERVE_LINKED,
                ),
            ),
        )

        proposal = propose_guided_odoo_capture_plans(
            _schema(root, full_child, company, product),
            scope,
        )

        self.assertTrue(proposal.complete)
        by_model = {draft.model: draft for draft in proposal.drafts}
        self.assertIs(by_model[full_child.name].mode, GuidedOdooCaptureMode.FULL)
        self.assertIs(
            by_model[full_child.name].capture_role,
            OdooCaptureRole.LINKED_ONLY,
        )
        self.assertEqual(by_model[company.name].field_names, ("name",))
        self.assertIs(
            by_model[company.name].mode,
            GuidedOdooCaptureMode.IDENTITY_ONLY,
        )
        self.assertEqual(
            by_model[product.name].field_names,
            ("default_code", "name", "required_note"),
        )
        self.assertIs(
            by_model[product.name].mode,
            GuidedOdooCaptureMode.MINIMUM_CREATE,
        )

    def test_relationship_scoped_identity_generates_each_safe_plan_mode(self) -> None:
        category = _model(
            "uom.category",
            (_field("name", "Name", required=True),),
        )
        unit = _model(
            "uom.uom",
            (
                _field("name", "Name", required=True),
                _field(
                    "category_id",
                    "Category",
                    field_type="many2one",
                    relation=category.name,
                    required=True,
                ),
            ),
        )
        root = _model("x.root", (_field("name", "Name"),))
        schema = _schema(root, unit, category)
        cases = (
            (
                "full root",
                _scope(root_models=(unit.name,)),
                GuidedOdooCaptureMode.FULL,
                OdooCaptureRole.ROOT,
            ),
            (
                "existing-only leaf",
                _scope(
                    root_models=(root.name,),
                    decisions=(
                        _decision(
                            root.name,
                            "uom_id",
                            unit.name,
                            OdooRelationshipCaptureAction.MATCH_EXISTING,
                        ),
                    ),
                ),
                GuidedOdooCaptureMode.IDENTITY_ONLY,
                OdooCaptureRole.LINKED_ONLY,
            ),
            (
                "create-if-missing leaf",
                _scope(
                    root_models=(root.name,),
                    decisions=(
                        _decision(
                            root.name,
                            "uom_id",
                            unit.name,
                            OdooRelationshipCaptureAction.PRESERVE_LINKED,
                        ),
                    ),
                ),
                GuidedOdooCaptureMode.MINIMUM_CREATE,
                OdooCaptureRole.LINKED_ONLY,
            ),
        )

        for label, scope, expected_mode, expected_role in cases:
            with self.subTest(label):
                proposal = propose_guided_odoo_capture_plans(schema, scope)

                self.assertTrue(proposal.complete)
                self.assertFalse(proposal.issues)
                draft = next(
                    item for item in proposal.drafts if item.model == unit.name
                )
                self.assertIs(draft.mode, expected_mode)
                self.assertIs(draft.capture_role, expected_role)
                self.assertEqual(draft.field_names, ("name",))

    def test_identity_leaf_without_one_safe_key_is_a_named_exception(self) -> None:
        owner = _model(
            "x.owner",
            (
                _field("name", "Name"),
                _field("reference", "Reference"),
            ),
        )
        scope = _scope(
            root_models=("x.root",),
            decisions=(
                _decision(
                    "x.root",
                    "owner_id",
                    owner.name,
                    OdooRelationshipCaptureAction.MATCH_EXISTING,
                ),
            ),
        )

        proposal = propose_guided_odoo_capture_plans(
            _schema(_model("x.root", (_field("name", "Name"),)), owner),
            scope,
        )

        self.assertIn(
            GuidedOdooCaptureIssueCode.NO_SAFE_IDENTITY,
            {issue.code for issue in proposal.issues},
        )
        self.assertNotIn(owner.name, {draft.model for draft in proposal.drafts})

    def test_current_plan_is_retained_instead_of_replaced(self) -> None:
        root = _model("x.root", (_field("name", "Name"),))
        schema = _schema(root)
        current = _selection(schema, root.name, dataset_name="manager_chosen_name")

        proposal = propose_guided_odoo_capture_plans(
            schema,
            _scope(root_models=(root.name,)),
            current_selections=(current,),
        )

        self.assertTrue(proposal.complete)
        self.assertFalse(proposal.drafts)
        self.assertEqual(proposal.skipped_models, (root.name,))

    def test_dataset_limit_blocks_the_whole_automatic_set(self) -> None:
        models = tuple(
            _model(f"x.model_{index}", (_field("name", "Name"),))
            for index in range(MAX_ODOO_CAPTURE_DATASETS + 1)
        )

        proposal = propose_guided_odoo_capture_plans(
            _schema(*models),
            _scope(root_models=tuple(model.name for model in models)),
        )

        self.assertFalse(proposal.drafts)
        self.assertEqual(
            proposal.issues[0].code,
            GuidedOdooCaptureIssueCode.DATASET_LIMIT,
        )

    def test_field_limit_is_not_silently_truncated(self) -> None:
        model = _model(
            "x.wide",
            tuple(
                _field(f"field_{index:02d}", f"Field {index}")
                for index in range(MAX_ODOO_CAPTURE_FIELDS + 1)
            ),
        )

        proposal = propose_guided_odoo_capture_plans(
            _schema(model),
            _scope(root_models=(model.name,)),
        )

        self.assertFalse(proposal.drafts)
        self.assertEqual(
            proposal.issues[0].code,
            GuidedOdooCaptureIssueCode.FIELD_LIMIT,
        )
        self.assertIn(str(MAX_ODOO_CAPTURE_FIELDS + 1), proposal.issues[0].reason)

    def test_truncated_automatic_dataset_names_do_not_collide_silently(self) -> None:
        shared = "x." + "a" * 70
        first = _model(shared + "one", (_field("name", "Name"),))
        second = _model(shared + "two", (_field("name", "Name"),))

        proposal = propose_guided_odoo_capture_plans(
            _schema(first, second),
            _scope(root_models=(first.name, second.name)),
        )

        self.assertFalse(proposal.drafts)
        self.assertEqual(
            {issue.code for issue in proposal.issues},
            {GuidedOdooCaptureIssueCode.DATASET_NAME_CONFLICT},
        )
        self.assertEqual({issue.model for issue in proposal.issues}, {first.name, second.name})

    def test_missing_schema_model_is_reported_by_name(self) -> None:
        proposal = propose_guided_odoo_capture_plans(
            _schema(),
            _scope(root_models=("x.missing",)),
        )

        self.assertEqual(
            proposal.issues[0].code,
            GuidedOdooCaptureIssueCode.MODEL_NOT_AVAILABLE,
        )
        self.assertEqual(proposal.issues[0].model, "x.missing")


def _field(
    name: str,
    label: str,
    *,
    field_type: str = "char",
    required: bool = False,
    readonly: bool = False,
    relation: str | None = None,
) -> SchemaField:
    return SchemaField(
        name=name,
        label=label,
        type=field_type,
        required=required,
        readonly=readonly,
        relation=relation,
        relation_field=None,
        selection=(),
        stored=True,
        computed=False,
        has_inverse=False,
        related=False,
        translated=False,
        company_dependent=False,
        searchable=True,
        sortable=True,
        exportable=True,
    )


def _model(
    name: str,
    fields: tuple[SchemaField, ...],
    *,
    constraints: tuple[UniqueConstraintMetadata, ...] = (),
) -> SchemaModel:
    return SchemaModel(
        name=name,
        label=name.replace(".", " ").title(),
        fields=fields,
        unique_constraints=constraints,
    )


def _schema(*models: SchemaModel) -> OdooSchemaCatalog:
    return OdooSchemaCatalog(
        workspace_id=WORKSPACE_ID,
        policy_hash=ODOO_SOURCE_POLICY_HASH,
        captured_at=datetime.now(timezone.utc),
        captured_by="Data Manager",
        connection_mode="REMOTE",
        database="source",
        odoo_version="20.0",
        models=models,
        content_hash="sha256:" + "1" * 64,
        origin=SchemaOrigin.LIVE_API,
        read_credential_binding_hash="sha256:" + "2" * 64,
        read_principal_hash="sha256:" + "3" * 64,
        read_permission_hash="sha256:" + "4" * 64,
        read_context_hash="sha256:" + "5" * 64,
        connection_target_hash=HASH,
    )


def _decision(
    source_model: str,
    field_name: str,
    relation_model: str,
    action: OdooRelationshipCaptureAction,
    *,
    handling: RelatedDataHandling = RelatedDataHandling.INCLUDE_SUPPORTING,
) -> OdooRelationshipScopeDecision:
    return OdooRelationshipScopeDecision(
        source_model=source_model,
        field_name=field_name,
        relation_model=relation_model,
        required=False,
        handling=handling,
        action=action,
    )


def _scope(
    *,
    root_models: tuple[str, ...],
    decisions: tuple[OdooRelationshipScopeDecision, ...] = (),
) -> OdooRelationshipScope:
    return OdooRelationshipScope.create(
        scope_id="00000000-0000-0000-0000-000000000003",
        version=1,
        decisions=tuple(
            sorted(
                decisions,
                key=lambda item: (
                    item.source_model,
                    item.field_name,
                    item.relation_model,
                ),
            )
        ),
        root_models=root_models,
        recorded_at=datetime.now(timezone.utc),
        recorded_by="Data Manager",
    )


def _selection(
    schema: OdooSchemaCatalog,
    model: str,
    *,
    dataset_name: str,
) -> OdooCaptureSelection:
    return OdooCaptureSelection.create(
        selection_id="00000000-0000-0000-0000-000000000004",
        version=1,
        data_version_id=DATA_VERSION_ID,
        dataset_name=dataset_name,
        model=model,
        field_names=("name",),
        filter_policy=OdooCaptureFilterPolicy.ALL_MATCHING_RECORDS,
        max_rows=10_000,
        capture_role=OdooCaptureRole.ROOT,
        connection_target_hash=schema.connection_target_hash,
        schema_scope_hash=schema.content_hash,
        read_principal_hash=schema.read_principal_hash,
        read_permission_hash=schema.read_permission_hash,
        context_hash=schema.read_context_hash,
        created_at=datetime.now(timezone.utc),
        created_by="Data Manager",
    )


if __name__ == "__main__":
    unittest.main()
