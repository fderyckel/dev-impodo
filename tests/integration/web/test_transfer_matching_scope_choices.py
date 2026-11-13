"""Keep reviewed parent scopes visible and bound to transfer and restore choices."""

from __future__ import annotations

import json
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

from starlette.datastructures import FormData

from impodo.application.destination_matching_service import DestinationMatchKeyChoice
from impodo.domain.odoo_relationship_scope import (
    OdooRelationshipCaptureAction,
    OdooRelationshipScope,
    OdooRelationshipScopeDecision,
)
from impodo.domain.odoo_source_scope import RelatedDataHandling
from impodo.domain.workspace.contracts import SchemaField
from impodo.domain.workspace.errors import WorkspaceError
from impodo.domain.workspace.workbench import WorkspaceStateError
from impodo.web.routers.destination_matching import _matching_rows, _parse_choices
from impodo.web.routers.saved_odoo_data import (
    _profile_from_form,
    _restore_fields,
    _source_rows,
)
from tests.application.workspace.test_destination_matching import (
    _selection,
    _source_schema,
    _workspace,
)
from tests.integration.web import (
    test_destination_matching_presenter as presenter_fixture,
)


def _fixture(major, mode):
    now = datetime.now(UTC)
    workspace = _workspace(now)
    selection = _selection(now)
    schema = _source_schema(workspace, now)
    field = SchemaField(
        name="x_group_id",
        label="Inventory group",
        type="many2one",
        required=True,
        readonly=False,
        relation="uom.uom",
        relation_field=None,
        selection=(),
        stored=True,
        computed=False,
        related=False,
        company_dependent=False,
        exportable=True,
    )
    schema = replace(
        schema,
        odoo_version=f"{major}.0",
        connection_mode=mode,
        models=(
            replace(schema.models[0], fields=(*schema.models[0].fields, field)),
            schema.models[1],
        ),
    )
    scope = OdooRelationshipScope.create(
        scope_id=str(uuid4()),
        version=1,
        decisions=(
            OdooRelationshipScopeDecision(
                source_model="product.template",
                field_name=field.name,
                relation_model=field.relation,
                required=True,
                handling=RelatedDataHandling.INCLUDE_SUPPORTING,
                action=OdooRelationshipCaptureAction.CAPTURE_LINKED,
                relationship_type="many2one",
            ),
        ),
        recorded_at=now,
        recorded_by="Data manager",
    )
    return workspace, selection, schema, scope


class TransferMatchingScopeChoiceTests(unittest.TestCase):
    def test_post_parser_retains_one_scope_and_rejects_unknown_table_or_repetition(
        self,
    ):
        _, selection, _, _ = _fixture(19, "REMOTE")
        product, unit = selection.datasets
        entries = [
            ("match_key", f"{product.dataset_id}::product-code"),
            ("match_key", f"{unit.dataset_id}::uom-name"),
            ("destination_handling", f"{product.dataset_id}::transfer"),
            ("destination_handling", f"{unit.dataset_id}::reuse_only"),
            ("match_scope", f"{product.dataset_id}::x_group_id"),
        ]
        choices = _parse_choices(FormData(entries), selection)
        self.assertEqual(choices[0].scope_fields, ("x_group_id",))
        self.assertEqual(choices[1].scope_fields, ())
        for extra in (
            ("match_scope", f"{product.dataset_id}::another_parent_id"),
            ("match_scope", f"{uuid4()}::x_group_id"),
        ):
            with self.subTest(extra=extra), self.assertRaises(WorkspaceStateError):
                _parse_choices(FormData([*entries, extra]), selection)

    def test_failed_check_keeps_scoped_choice_for_both_majors_and_access_modes(self):
        presenter = presenter_fixture.DestinationMatchingPresenterTests("runTest")
        presenter.setUp()
        for major in (19, 20):
            for mode in ("LOCAL", "REMOTE"):
                with self.subTest(major=major, mode=mode):
                    workspace, selection, schema, scope = _fixture(major, mode)
                    product = selection.datasets[0]
                    rows = _matching_rows(
                        workspace,
                        selection,
                        schema,
                        scope,
                        submitted_choices=(
                            DestinationMatchKeyChoice(
                                product.dataset_id,
                                "product-code",
                                scope_fields=("x_group_id",),
                            ),
                        ),
                    )
                    self.assertEqual(rows[0]["selected_scopes"], ("x_group_id",))
                    self.assertEqual(
                        rows[0]["scope_candidates"][0]["label"], "Inventory group"
                    )
                    self.assertIsNone(rows[0]["result"])
                    html = presenter.templates.get_template(
                        "workspace_destination_matching.html"
                    ).render(
                        migration_project=SimpleNamespace(display_name="Test project"),
                        match_plan_create_defaults_pending=False,
                        match_plan_ready=False,
                        match_plan=None,
                        match_plan_stale=False,
                        matching_rows=rows,
                        destination_credential_status=SimpleNamespace(
                            available=True, label="Available"
                        ),
                        workspace_id=workspace.workspace_id,
                        workspace_state=workspace,
                        csrf_token="csrf",
                        matching_can_check=True,
                    )
                    self.assertIn("Match within", html)
                    self.assertIn(
                        f'value="{product.dataset_id}::x_group_id" selected', html
                    )
                    self.assertIn("already have one matching destination record", html)

    def test_saved_source_projection_marks_scope_as_identity_without_remote_reads(self):
        for major in (19, 20):
            for mode in ("LOCAL", "REMOTE"):
                with self.subTest(major=major, mode=mode):
                    _, selection, schema, scope = _fixture(major, mode)
                    context = SimpleNamespace(
                        queries=SimpleNamespace(
                            get_odoo_schema_catalog=lambda _, value=schema: value,
                            get_source_selection=lambda _, value=selection: value,
                            get_current_odoo_relationship_scope=lambda _, value=scope: (
                                value
                            ),
                        )
                    )
                    rows, keys, _, _ = _source_rows(
                        context,
                        "workspace",
                        {
                            "keys": {"product.template": ["default_code"]},
                            "scopes": {"product.template": ["x_group_id"]},
                        },
                    )
                    group = next(row for row in rows if row["name"] == "x_group_id")
                    self.assertTrue(group["is_identity"])
                    product = next(
                        row for row in keys if row["model"] == "product.template"
                    )
                    self.assertEqual(product["selected_scope"], "x_group_id")
                    self.assertEqual(
                        product["scope_fields"],
                        ({"name": "x_group_id", "label": "Inventory group"},),
                    )

    def test_restore_profile_remembers_scope_and_protects_its_field_policy(self):
        context = SimpleNamespace(saved_odoo_data=SimpleNamespace(repository=object()))
        values = {
            "key_rows": [
                {
                    "model": "x.line",
                    "selected": ("code",),
                    "fields": ({"name": "code"},),
                    "scope_fields": ({"name": "parent_id"},),
                }
            ],
            "field_rows": [
                {"model": "x.line", "name": "code"},
                {"model": "x.line", "name": "parent_id"},
            ],
            "default_rows": (),
        }
        form = FormData(
            {
                "odoo_connection_mode": "REMOTE",
                "odoo_base_url": "https://destination.example.test",
                "odoo_database": "test",
                "destination_company_name": "Test company",
                "policy_default": "keep_destination",
                "keys__x.line": "code",
                "scope__x.line": "parent_id",
            }
        )
        allowed, repeated = _restore_fields(values)
        self.assertIn("scope__x.line", allowed)
        self.assertNotIn("scope__x.line", repeated)
        profile = _profile_from_form(context, str(uuid4()), str(uuid4()), form, values)
        settings = json.loads(profile.settings_json)
        self.assertEqual(settings["scopes"], {"x.line": ["parent_id"]})
        self.assertEqual(settings["rules"]["x.line"][1]["policy"], "use_saved")
        for changed in (
            {"scope__x.line": "uncaptured_parent_id"},
            {"exclude__x.line__parent_id": "yes"},
            {"target__x.line__parent_id": "other_parent_id"},
        ):
            with self.subTest(changed=changed), self.assertRaises(WorkspaceError):
                _profile_from_form(
                    context,
                    str(uuid4()),
                    str(uuid4()),
                    FormData({**dict(form), **changed}),
                    values,
                )

    def test_saved_scope_captured_through_inverse_stays_in_identity_review(self):
        for major in (19, 20):
            with self.subTest(major=major):
                _, selection, schema, scope = _fixture(major, "REMOTE")
                inverse = replace(
                    schema.models[0].fields[-1],
                    name="x_line_ids",
                    label="Lines",
                    type="one2many",
                    relation="product.template",
                    relation_field="x_group_id",
                )
                schema = replace(
                    schema,
                    models=(
                        schema.models[0],
                        replace(
                            schema.models[1],
                            fields=(*schema.models[1].fields, inverse),
                        ),
                    ),
                )
                scope = OdooRelationshipScope.create(
                    scope_id=str(uuid4()),
                    version=1,
                    decisions=(
                        replace(
                            scope.decisions[0],
                            source_model="uom.uom",
                            field_name="x_line_ids",
                            relation_model="product.template",
                            relationship_type="one2many",
                        ),
                    ),
                    recorded_at=datetime.now(UTC),
                    recorded_by="Data manager",
                )
                context = SimpleNamespace(
                    queries=SimpleNamespace(
                        get_odoo_schema_catalog=lambda _, value=schema: value,
                        get_source_selection=lambda _, value=selection: value,
                        get_current_odoo_relationship_scope=lambda _, value=scope: (
                            value
                        ),
                    )
                )
                rows, _, _, _ = _source_rows(
                    context,
                    "workspace",
                    {
                        "keys": {"product.template": ["default_code"]},
                        "scopes": {"product.template": ["x_group_id"]},
                    },
                )
                group = next(row for row in rows if row["name"] == "x_group_id")
                self.assertTrue(group["is_identity"])


if __name__ == "__main__":
    unittest.main()
