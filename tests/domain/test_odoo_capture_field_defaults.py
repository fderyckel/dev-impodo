"""Keep sensible capture defaults separate from deliberate backup choices."""

import unittest
from dataclasses import replace
from datetime import UTC, datetime

from impodo.domain.guided_odoo_capture import (
    GuidedOdooCaptureIssueCode,
    propose_guided_odoo_capture_plans,
    recommended_odoo_capture_field_names,
)
from impodo.domain.odoo_capture import OdooCaptureFilterPolicy, OdooCaptureSelection
from impodo.domain.odoo_relationship_scope import (
    OdooRelationshipCaptureAction,
    OdooRelationshipScope,
    OdooRelationshipScopeDecision,
)
from impodo.domain.odoo_source_capture import (
    is_odoo_capture_value_field,
    plan_odoo_source_capture,
)
from impodo.domain.odoo_source_policy import odoo_source_policy_hash
from impodo.domain.odoo_source_scope import RelatedDataHandling
from impodo.domain.workspace.contracts import (
    OdooSchemaCatalog,
    SchemaField,
    SchemaModel,
    SchemaOrigin,
)

WORKSPACE_ID = "00000000-0000-0000-0000-000000000001"
HASH = "sha256:" + "a" * 64
NOW = datetime(2026, 11, 13, tzinfo=UTC)


def _field(name, field_type="char", *, readonly=False):
    return SchemaField(
        name=name,
        label=name,
        type=field_type,
        required=False,
        readonly=readonly,
        relation=None,
        relation_field=None,
        selection=(),
        stored=True,
        computed=False,
        related=False,
        company_dependent=False,
        exportable=True,
    )


def _schema(major, mode):
    policy_hash = odoo_source_policy_hash(major)
    assert policy_hash is not None
    return OdooSchemaCatalog(
        workspace_id=WORKSPACE_ID,
        policy_hash=policy_hash,
        captured_at=NOW,
        captured_by="Data Manager",
        connection_mode=mode,
        database="source",
        odoo_version=f"{major}.0",
        models=(
            SchemaModel(
                name="x.business",
                label="Business records",
                fields=(
                    _field("id", "integer", readonly=True),
                    _field("write_date", "datetime", readonly=True),
                    _field("create_date", "datetime", readonly=True),
                    _field("name"),
                    _field("created_at", "datetime", readonly=True),
                    replace(
                        _field("amount_total", "float", readonly=True), computed=True
                    ),
                    _field("image", "binary"),
                ),
            ),
        ),
        content_hash=HASH,
        origin=SchemaOrigin.LIVE_API,
        read_credential_binding_hash=HASH,
        read_principal_hash=HASH,
        read_permission_hash=HASH,
        read_context_hash=HASH,
        connection_target_hash=HASH,
    )


def _scope(*, decisions=()):
    return OdooRelationshipScope.create(
        scope_id="00000000-0000-0000-0000-000000000002",
        version=1,
        root_models=("x.business",),
        decisions=decisions,
        recorded_at=NOW,
        recorded_by="Data Manager",
    )


class OdooCaptureFieldDefaultTests(unittest.TestCase):
    def test_both_majors_default_to_business_values_in_each_access_mode(self):
        for major in (19, 20):
            for mode in ("LOCAL", "REMOTE"):
                with self.subTest(major=major, mode=mode):
                    schema = _schema(major, mode)
                    model = schema.models[0]
                    self.assertEqual(
                        recommended_odoo_capture_field_names(model),
                        ("amount_total", "created_at", "name"),
                    )
                    proposal = propose_guided_odoo_capture_plans(schema, _scope())
                    self.assertTrue(proposal.complete)
                    self.assertEqual(
                        proposal.drafts[0].field_names,
                        ("amount_total", "created_at", "name"),
                    )
                    created_on = next(
                        field for field in model.fields if field.name == "create_date"
                    )
                    self.assertTrue(is_odoo_capture_value_field(created_on))

    def test_saved_creation_timestamp_remains_selected_and_capturable(self):
        for major in (19, 20):
            with self.subTest(major=major):
                schema = _schema(major, "REMOTE")
                selection = OdooCaptureSelection.create(
                    selection_id="00000000-0000-0000-0000-000000000003",
                    version=1,
                    data_version_id="00000000-0000-0000-0000-000000000004",
                    dataset_name="business_backup",
                    model="x.business",
                    field_names=("create_date", "name"),
                    filter_policy=OdooCaptureFilterPolicy.ALL_MATCHING_RECORDS,
                    max_rows=10_000,
                    policy_hash=schema.policy_hash,
                    connection_target_hash=HASH,
                    schema_scope_hash=HASH,
                    read_principal_hash=HASH,
                    read_permission_hash=HASH,
                    context_hash=HASH,
                    created_at=NOW,
                    created_by="Data Manager",
                )
                proposal = propose_guided_odoo_capture_plans(
                    schema,
                    _scope(),
                    current_selections=(selection,),
                )
                self.assertTrue(proposal.complete)
                self.assertEqual(proposal.drafts, ())
                self.assertEqual(proposal.skipped_models, ("x.business",))
                request = plan_odoo_source_capture(selection, schema)
                self.assertEqual(
                    tuple(field.name for field in request.projection),
                    ("create_date", "name"),
                )

    def test_missing_matching_rule_names_the_available_field_review(self):
        schema = _schema(20, "REMOTE")
        leaf = SchemaModel(
            name="x.ambiguous",
            label="Ambiguous records",
            fields=(_field("name"), _field("reference")),
        )
        schema = replace(schema, models=(*schema.models, leaf))
        decision = OdooRelationshipScopeDecision(
            source_model="x.business",
            field_name="linked_id",
            relation_model=leaf.name,
            required=False,
            handling=RelatedDataHandling.REUSE_DESTINATION,
            action=OdooRelationshipCaptureAction.MATCH_EXISTING,
            relationship_type="many2one",
        )
        proposal = propose_guided_odoo_capture_plans(
            schema,
            _scope(decisions=(decision,)),
        )
        issue = next(issue for issue in proposal.issues if issue.model == leaf.name)
        self.assertEqual(issue.code, GuidedOdooCaptureIssueCode.NO_SAFE_IDENTITY)
        self.assertIn("Review the fields to keep for Ambiguous records", issue.reason)
        self.assertIn("review its matching rule in Match data", issue.reason)


if __name__ == "__main__":
    unittest.main()
