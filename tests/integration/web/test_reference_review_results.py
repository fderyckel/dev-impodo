"""Persist exact generic missing lookups and render bounded checked-row pages."""

from unittest.mock import patch

from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from impodo.domain.mapping.contracts import (
    CategoricalCoveragePolicy,
    DatasetMapping,
    IdentityComponentMapping,
    ReferenceKeyMapping,
    RelationshipMapping,
    RelationshipResolver,
    ResolverOrigin,
)
from impodo.domain.schema.governance import BusinessKeyDefinition, BusinessKeyStatus
from impodo.domain.workspace.contracts import SchemaField, SchemaModel
from impodo.web.composition.page_reads import run_local_operation_sync
from impodo.web.diagnostic_settings import capture_diagnostic_settings
from tests.support.database_probe import count_duckdb_statements
from tests.support.hierarchy_odoo import HierarchyOdoo
from tests.support.hierarchy_workflow import HierarchyWorkflowFixture


class ReferenceReviewResultsTests(HierarchyWorkflowFixture):
    def test_missing_custom_relationship_is_visible_after_reopening_for_both_majors_and_modes(
        self,
    ):
        for major in (19, 20):
            for mode in ("LOCAL", "REMOTE"):
                with self.subTest(major=major, mode=mode):
                    workspace_id = self.configure(major, mode)
                    self.prepare(workspace_id)
                    self.approve(workspace_id)
                    self.expect_business_writes(workspace_id)
                    schema = self.context.queries.get_odoo_schema_catalog(workspace_id)
                    target = HierarchyOdoo(schema)
                    report = run_local_operation_sync(
                        self.context.preflight.compare,
                        workspace_id,
                        reader=target.compare,
                        actor=self.actor,
                    )
                    before = self.context.preflight.readiness_rows(
                        workspace_id, report.run_id
                    )
                    self.assertEqual(before.matching_count, 2)
                    self.assertIn('"000745"', before.items[0].reference_details[0])
                    self.assertIn('"000746"', before.items[1].reference_details[0])
                    settings = run_local_operation_sync(
                        capture_diagnostic_settings,
                        self.context,
                        workspace_id,
                        self.actor,
                    )
                    members, _ = settings.bundle_members()
                    for content in members.values():
                        self.assertNotIn(b'"000745"', content)
                        self.assertNotIn(b'"000746"', content)
                        self.assertNotIn(b"reference_details", content)
                    self.assertTrue(
                        all(
                            "000745" not in issue.reason
                            for issue in report.issue_counts
                        )
                    )
                    self.reopen()
                    with (
                        patch.object(
                            self.context,
                            "readiness_reader",
                            side_effect=AssertionError("Review contacted Odoo"),
                        ),
                        collect_duckdb_request_timings() as timings,
                        count_duckdb_statements() as statements,
                    ):
                        response = self.client.get(
                            f"/workspaces/{workspace_id}/summary?step=results&view=attention"
                            "&status=blocked&dataset=notices&issue=REFERENCE_NOT_FOUND&page_size=10"
                        )
                    self.assertEqual(response.status_code, 200, response.text)
                    rows = response.context["readiness_rows"]
                    self.assertEqual(rows, before.items)
                    self.assertIn(
                        "Contact (contact_id): No related record matched", response.text
                    )
                    self.assertIn("Reference (ref): &#34;000745&#34;", response.text)
                    self.assertIn("Reference (ref): &#34;000746&#34;", response.text)
                    self.assertIn("in Odoo x.contact", response.text)
                    self.assertNotIn("Lookup details were not retained", response.text)
                    self.assertEqual(response.context["readiness_row_total"], 2)
                    self.assertLessEqual(timings.connection_count, 4)
                    self.assertLessEqual(timings.schema_check_count, 3)
                    self.assertLessEqual(len(statements), 240)
                    self.assertEqual(
                        self.context.preflight.current_report(workspace_id).run_id,
                        report.run_id,
                    )
                    self.assertFalse(target.writes)

    def configure(self, major, mode):
        workspace_id = self.freeze_files(
            {
                "notices": b"Code,Contact\nN-01,000745\nN-02,000746\n",
            }
        )

        def field(name, label, kind="char", relation=None):
            return SchemaField(name, label, kind, False, False, relation, None, ())

        self.schema(
            workspace_id,
            major=major,
            mode=mode,
            extra_models=(
                SchemaModel(
                    "x.notice",
                    "Notice",
                    (
                        field("name", "Name"),
                        field("contact_id", "Contact", "many2one", "x.contact"),
                    ),
                ),
                SchemaModel("x.contact", "Contact", (field("ref", "Reference"),)),
            ),
            extra_keys=(
                BusinessKeyDefinition(
                    "notice:name",
                    "x.notice",
                    ("name",),
                    status=BusinessKeyStatus.CONFIRMED,
                ),
                BusinessKeyDefinition(
                    "contact:ref",
                    "x.contact",
                    ("ref",),
                    status=BusinessKeyStatus.CONFIRMED,
                ),
            ),
        )
        (source,) = self.context.queries.get_source_selection(workspace_id).datasets
        columns = {column.source_name: column.stable_key for column in source.columns}
        self.submit(
            workspace_id,
            (
                DatasetMapping(
                    source.dataset_id,
                    "x.notice",
                    source_identity_column_keys=(columns["Code"],),
                    target_identity=(
                        IdentityComponentMapping((columns["Code"],), ("name",)),
                    ),
                    relationships=(
                        RelationshipMapping(
                            "contact_id",
                            "many2one",
                            (columns["Contact"],),
                            resolver=RelationshipResolver(
                                ResolverOrigin.TARGET_CATALOG,
                                model="x.contact",
                                key_mappings=(
                                    ReferenceKeyMapping(columns["Contact"], "ref"),
                                ),
                            ),
                            categorical_policy=CategoricalCoveragePolicy.EXACT_BUSINESS_KEY,
                        ),
                    ),
                ),
            ),
        )
        return workspace_id
