"""Keep original values and every contributing file traceable after saved joins."""

from io import BytesIO
from unittest import TestCase
from unittest.mock import patch

from openpyxl import load_workbook

from impodo.adapters.artifacts.attention_sources import read_attention_sources
from impodo.adapters.artifacts.attention_workbook import build_attention_workbook
from impodo.application.workspace.review.attention_export import (
    verify_attention_export_current,
)
from impodo.domain.errors import ReadinessError
from impodo.domain.mapping.contracts import (
    CategoricalCoveragePolicy,
    DatasetMapping,
    IdentityComponentMapping,
    ReferenceKeyMapping,
    RelationshipMapping,
    RelationshipResolver,
    ResolverOrigin,
    ValueMapping,
)
from impodo.domain.mapping.validation.evidence import mapping_issue_fingerprint
from impodo.domain.preflight.attention_projection import project_attention_export
from impodo.web.composition.page_reads import run_local_operation_sync
from tests.integration.web import test_joined_recipe_preparation as joined
from tests.support.browser_scenarios import POST_HEADERS


class AttentionJoinedSourceTests(TestCase):
    def test_joined_original_values_are_available_after_columns_are_renamed(self):
        for major in (19, 20):
            with self.subTest(major=major):
                fixture = joined.JoinedRecipePreparationTests(methodName="runTest")
                fixture.setUp()
                try:
                    self.exercise(fixture, major)
                finally:
                    fixture.doCleanups()

    def exercise(self, fixture, major):
        fixture.odoo_major = major
        fixture.participant_count = 2
        browser = fixture.fixture
        context, actor = fixture.context, fixture.actor
        workspace_id, physical, by_name = browser._frozen_customer_workspace()
        fixture._schema(workspace_id)
        response = browser.client.post(
            f"/workspaces/{workspace_id}/derived-entities/join/save",
            data=browser._join_form(physical, by_name),
            headers=POST_HEADERS,
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303, response.text)
        selection = context.queries.get_mapping_source_selection(workspace_id)
        dataset = selection.datasets[0]
        columns = {column.source_name: column.stable_key for column in dataset.columns}
        mapping = DatasetMapping(
            dataset_id=dataset.dataset_id,
            target_model="x_record",
            source_identity_column_keys=(columns["account_number"],),
            target_identity=(
                IdentityComponentMapping((columns["account_number"],), ("ref",)),
            ),
            relationships=(
                RelationshipMapping(
                    target_field="x_list_id",
                    kind="many2one",
                    source_column_keys=(columns["name"],),
                    categorical_policy=CategoricalCoveragePolicy.EXPLICIT_KEY_MATCH,
                    resolver=RelationshipResolver(
                        origin=ResolverOrigin.TARGET_CATALOG,
                        model="x_list",
                        key_mappings=(ReferenceKeyMapping(columns["name"], "ref"),),
                        value_mappings=(
                            ValueMapping("United Caps", "A"),
                            ValueMapping("Fictional Retail", "B"),
                        ),
                    ),
                ),
            ),
        )
        revision, validation = run_local_operation_sync(
            context.mapping_workspace.check_definition,
            workspace_id,
            datasets=(mapping,),
            expected_parent_version=None,
            expected_working_draft_version=None,
            actor=actor,
        )
        self.assertNotEqual(validation.status.value, "INVALID", validation.issues)
        draft = context.mapping_workspace.mappings.get_mapping_working_draft(
            workspace_id
        )
        run_local_operation_sync(
            context.mapping_workspace.submit_current,
            workspace_id,
            datasets=revision.definition.datasets,
            expected_version=revision.version,
            expected_working_draft_version=draft.version,
            warning_acknowledgements=tuple(
                mapping_issue_fingerprint(issue)
                for issue in validation.issues
                if issue.severity == "warning"
            ),
            actor=actor,
        )
        fixture._prepare(workspace_id)
        staging_summary = context.preparation.staging.get_current_staging_summary(
            workspace_id
        )
        staging = context.preparation.staging.get_canonical_staging_run(
            workspace_id,
            staging_summary.run_id,
            expected_content_hash=staging_summary.content_hash,
        )
        row = next(row for row in staging.rows if row.target_identity == ("C001",))
        with patch.object(
            context,
            "readiness_reader",
            side_effect=AssertionError("Export contacted Odoo"),
        ):
            header = context.preflight.attention_export(workspace_id, actor=actor)
            exported = project_attention_export(
                header,
                staging,
                context.quality.current_run(workspace_id),
                revision.definition,
                selection,
                context.queries.get_odoo_schema_catalog(workspace_id),
                {
                    "decisions": [
                        {
                            "source_trace_id": row.row_id,
                            "dataset": row.dataset,
                            "classification": "BLOCKED",
                            "issues": [
                                {
                                    "code": "REFERENCE_NOT_FOUND",
                                    "severity": "error",
                                    "field": "x_list_id",
                                }
                            ],
                        }
                    ]
                },
            )
            original = read_attention_sources(exported, context.preflight.artifacts)
            content = build_attention_workbook(exported, original)
        workbook = load_workbook(BytesIO(content))
        try:
            finding = next(
                workbook["Needs attention"].iter_rows(min_row=4, values_only=True)
            )
            self.assertIn("United Caps", finding[7])
            self.assertIn("A", finding[8])
            self.assertIn("cust01.csv", finding[5])
            self.assertIn("cust02.csv", finding[5])
            self.assertNotEqual(
                exported.records[0].field_sources["x_list_id"], (columns["name"],)
            )
        finally:
            workbook.close()
        self.assertTrue(header.derived_plan_hash)
        with patch.object(
            context.preflight.derived, "get_derived_entity_plan", return_value=None
        ), self.assertRaisesRegex(ReadinessError, "saved rows or checks changed"):
            verify_attention_export_current(context.preflight, header, actor=actor)
