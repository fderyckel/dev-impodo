from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
import unittest
from uuid import uuid4

from jinja2 import ChoiceLoader, DictLoader, Environment, FileSystemLoader

from impodo.domain.workspace.contracts import (
    SchemaField,
    SchemaModel,
    SourceDataset,
    SourceDatasetColumn,
)
from impodo.domain.odoo_relationship_scope import (
    OdooRelationshipCaptureAction,
    OdooRelationshipScope,
    OdooRelationshipScopeDecision,
)
from impodo.domain.odoo_source_scope import RelatedDataHandling
from impodo.web.routers.destination_matching import _matching_rows
from tests.application.workspace.test_destination_matching import (
    _binding,
    _selection,
    _source_schema,
    _workspace,
)
from tests.application.workspace.test_transfer_order import _model
from tests.support.paths import REPOSITORY_ROOT


class DestinationMatchingPresenterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.templates = Environment(
            loader=ChoiceLoader(
                [
                    DictLoader(
                        {"base.html": "{% block content %}{% endblock %}"}
                    ),
                    FileSystemLoader(
                        REPOSITORY_ROOT / "src/impodo/web/templates"
                    ),
                ]
            ),
            autoescape=True,
        )
        self.templates.globals.update(
            app_icon=lambda _name: "",
            url_for=lambda *_args, **_kwargs: "",
        )

    def test_destination_setup_defaults_to_reference_only_per_record_type(self) -> None:
        now = datetime.now(UTC)
        workspace = _workspace(now)
        selection = _selection(now)
        schema = _source_schema(workspace, now)
        company = SourceDataset(
            dataset_id=str(uuid4()),
            name="Companies",
            source=_binding("res.company"),
            row_count=1,
            columns=(
                SourceDatasetColumn(1, "name", "company-name", "TEXT"),
            ),
        )
        company_relation = SchemaField(
            name="company_id",
            label="Company",
            type="many2one",
            required=False,
            readonly=False,
            relation="res.company",
            relation_field=None,
            selection=(),
            related=False,
            company_dependent=False,
            exportable=True,
        )
        company_name = SchemaField(
            name="name",
            label="Company Name",
            type="char",
            required=True,
            readonly=False,
            relation=None,
            relation_field=None,
            selection=(),
            stored=True,
            related=True,
            company_dependent=False,
            exportable=True,
        )
        selection = replace(
            selection,
            datasets=(selection.datasets[0], company),
        )
        schema = replace(
            schema,
            models=(
                replace(
                    schema.models[0],
                    fields=(*schema.models[0].fields, company_relation),
                ),
                SchemaModel("res.company", "Company", (company_name,)),
            ),
        )

        scope = OdooRelationshipScope.create(
            scope_id=str(uuid4()),
            version=1,
            decisions=(
                OdooRelationshipScopeDecision(
                    source_model="product.template",
                    field_name="company_id",
                    relation_model="res.company",
                    required=False,
                    handling=RelatedDataHandling.REUSE_DESTINATION,
                    action=OdooRelationshipCaptureAction.MATCH_EXISTING,
                ),
            ),
            recorded_at=now,
            recorded_by="Data manager",
        )
        rows = {
            row["model"]: row
            for row in _matching_rows(
                workspace,
                selection,
                schema,
                scope,
            )
        }

        self.assertEqual(rows["product.template"]["destination_handling"], "transfer")
        self.assertEqual(
            rows["res.company"]["destination_handling"],
            "reference_only",
        )
        self.assertEqual(rows["res.company"]["selected_keys"], ("company-name",))
        self.assertTrue(rows["res.company"]["destination_handling_locked"])

    def test_preserve_linked_is_locked_to_reuse_or_create_and_rendered(self) -> None:
        now = datetime.now(UTC)
        workspace = _workspace(now)
        selection = _selection(now)
        schema = _source_schema(workspace, now)
        scope = OdooRelationshipScope.create(
            scope_id=str(uuid4()),
            version=1,
            decisions=(
                OdooRelationshipScopeDecision(
                    source_model="product.template",
                    field_name="uom_id",
                    relation_model="uom.uom",
                    required=False,
                    handling=RelatedDataHandling.INCLUDE_SUPPORTING,
                    action=OdooRelationshipCaptureAction.PRESERVE_LINKED,
                ),
            ),
            root_models=("product.template",),
            recorded_at=now,
            recorded_by="Data manager",
        )
        rows = _matching_rows(workspace, selection, schema, scope)
        product = next(
            row for row in rows if row["model"] == "product.template"
        )
        uom = next(row for row in rows if row["model"] == "uom.uom")

        self.assertEqual(uom["destination_handling"], "reuse_or_create")
        self.assertTrue(uom["destination_handling_locked"])
        self.assertEqual(
            uom["destination_handling_locked_reason"],
            "preserve_linked",
        )

        html = self.templates.get_template(
            "workspace_destination_matching.html"
        ).render(
            migration_project=SimpleNamespace(display_name="Test project"),
            match_plan_create_defaults_pending=False,
            match_plan_ready=False,
            match_plan=None,
            match_plan_stale=False,
            matching_rows=(product, uom),
            destination_credential_status=SimpleNamespace(
                available=True,
                label="Available",
            ),
            workspace_id="workspace-1",
            workspace_state=SimpleNamespace(revision=7),
            csrf_token="csrf",
            matching_can_check=True,
        )

        handling_value = (
            f'{uom["dataset"].dataset_id}::reuse_or_create'
        )
        self.assertIn(
            f'name="destination_handling" value="{handling_value}"',
            html,
        )
        self.assertIn("Related-data outcome", html)
        self.assertIn("Missing-record policy", html)
        self.assertIn(
            '<p class="matching-fixed-outcome" role="status"><strong>Keep linked value</strong></p>',
            html,
        )
        self.assertNotIn(
            f'<select id="destination-handling-{2}" disabled',
            html,
        )
        self.assertNotIn(f'value="{handling_value}" selected', html)
        self.assertIn(
            "Reuse existing; create identity and required values only",
            html,
        )
        self.assertIn("Transfer missing records using selected fields", html)
        self.assertNotIn("Transfer full records", html)
        self.assertIn("Fixed by the Stage 2", html)
        self.assertIn(
            "A matching destination record is reused unchanged",
            html,
        )
        self.assertIn(
            "unsafe or incomplete required values remain a named decision",
            html,
        )
        self.assertIn("<h3>Product</h3>", html)
        self.assertIn("<h3>Unit of Measure</h3>", html)
        self.assertIn(
            f"Source dataset <code>{uom['dataset'].name}</code>. Odoo model <code>uom.uom</code>.",
            html,
        )

    def test_write_blocker_is_not_presented_as_checked(self) -> None:
        now = datetime.now(UTC)
        workspace = _workspace(now)
        selection = _selection(now)
        schema = _source_schema(workspace, now)
        rows = _matching_rows(workspace, selection, schema)
        product = next(
            row for row in rows if row["model"] == "product.template"
        )
        uom = next(row for row in rows if row["model"] == "uom.uom")
        blocked_result = replace(
            _model("uom.uom", "Unit of Measure", create=2),
            dataset_id=uom["dataset"].dataset_id,
            dataset_name=uom["dataset"].name,
            destination_handling="reuse_or_create",
            unresolved_create_fields=("company_id",),
        )
        self.assertTrue(blocked_result.ready)
        self.assertTrue(blocked_result.write_blocking_reasons)

        html = self.templates.get_template(
            "workspace_destination_matching.html"
        ).render(
            migration_project=SimpleNamespace(display_name="Test project"),
            match_plan_create_defaults_pending=False,
            match_plan_ready=False,
            match_plan=None,
            match_plan_stale=False,
            matching_rows=(product, {**uom, "result": blocked_result}),
            destination_credential_status=SimpleNamespace(
                available=True,
                label="Available",
            ),
            workspace_id="workspace-1",
            workspace_state=SimpleNamespace(revision=7),
            csrf_token="csrf",
            matching_can_check=True,
        )

        self.assertIn("Needs review", html)
        self.assertNotIn(">Checked<", html)


if __name__ == "__main__":
    unittest.main()
