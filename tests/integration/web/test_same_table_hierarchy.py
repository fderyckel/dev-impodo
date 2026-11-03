"""Keep source parent IDs separate from scoped Odoo category names in the editor."""

from dataclasses import replace

from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from impodo.domain.mapping.contracts import IdentityNullPolicy
from impodo.web.presenters.mapping_view import _mapping_dataset_views
from tests.integration.web.test_mapping_form_roundtrip import _RenderedControls
from tests.support.hierarchy_assertions import business_path
from tests.support.hierarchy_workflow import HierarchyWorkflowFixture
from tests.support.hierarchy_odoo import HierarchyOdoo
from tests.support.same_table_hierarchy import CATEGORY_CSV, same_table_workspace


class SameTableHierarchyTests(HierarchyWorkflowFixture):
    def test_author_and_reopen_supplied_hierarchy_for_both_majors_and_modes(self):
        for major in (19, 20):
            for mode in ("LOCAL", "REMOTE"):
                with self.subTest(major=major, mode=mode):
                    workspace_id, source = same_table_workspace(
                        self,
                        major=major,
                        mode=mode,
                    )
                    with collect_duckdb_request_timings() as timings:
                        page = self.client.get(f"/workspaces/{workspace_id}/mapping")
                    self.assertEqual(page.status_code, 200, page.text)
                    self.assertIn("categories (this table)", page.text)
                    self.assertIn("a blank parent means a top-level record", page.text)
                    controls = _RenderedControls()
                    controls.feed(page.text)
                    self.assertIn(
                        ("identity_dataset_0_1", source.dataset_id), controls.entries
                    )
                    # Access, preparation and rendering retain the existing eight-owner
                    # ceiling; offering this table performs no additional database reads.
                    self.assertLessEqual(timings.connection_count, 8)
                    self.assertLessEqual(timings.schema_check_count, 2)
                    self.assertEqual(timings.lock_retry_count, 0)
                    revision = self.context.queries.get_mapping_revision(workspace_id)
                    mapping = revision.definition.datasets[0]
                    columns = {
                        column.source_name: column.stable_key
                        for column in source.columns
                    }
                    self.assertEqual(
                        mapping.source_identity_column_keys, (columns["RecId"],)
                    )
                    self.assertEqual(
                        mapping.target_identity[0].source_column_keys,
                        (columns["Name"],),
                    )
                    self.assertEqual(
                        mapping.target_scope[0].null_policy,
                        IdentityNullPolicy.EXPLICIT_SCOPE_NULL,
                    )
                    self.submit(workspace_id, (mapping,))
                    staging, quality = self.prepare(workspace_id)
                    self.assertEqual(
                        (quality.ready_count, quality.quarantined_count), (4, 0)
                    )
                    self.approve(workspace_id)
                    self.expect_business_writes(workspace_id)
                    target = HierarchyOdoo(
                        self.context.queries.get_odoo_schema_catalog(workspace_id)
                    )
                    report = self.context.preflight.compare(
                        workspace_id, reader=target.compare, actor=self.actor
                    )
                    self.assertEqual(report.status, "READY", report)
                    snapshot = self.context.execution.current_preview(
                        workspace_id, actor=self.actor
                    ).snapshot
                    self.assertEqual(
                        {
                            business_path(row.business_identity, row.business_scope)
                            for row in snapshot.rows
                        },
                        {
                            ("Materials",),
                            ("Materials", "Additive"),
                            ("Materials", "Bag"),
                            ("Materials", "Bag", "Additive"),
                        },
                    )

    def test_populated_unknown_or_circular_parent_does_not_become_a_root(self):
        cases = (
            (
                b"5637144576,Materials,\n",
                b"5637144576,Materials,0\n",
                "INCOMING_RELATIONSHIP_MISSING",
            ),
            (
                b"5637144576,Materials,\n",
                b"5637144576,Materials,5637145328\n",
                "REFERENCE_IDENTITY_CYCLE",
            ),
        )
        for original, replacement, reason in cases:
            with self.subTest(reason=reason):
                workspace_id, _ = same_table_workspace(
                    self,
                    content=CATEGORY_CSV.replace(original, replacement),
                )
                revision = self.context.queries.get_mapping_revision(workspace_id)
                if reason == "INCOMING_RELATIONSHIP_MISSING":
                    validation = self.submit(workspace_id, revision.definition.datasets, check_only=True)
                    self.assertEqual(validation.status.value, "INVALID")
                    self.assertIn("MAPPING_INCOMING_PARENT_MISSING", {issue.code for issue in validation.issues})
                    self.assertIsNone(self.context.preparation.staging.get_current_staging_summary(workspace_id))
                    continue
                self.submit(workspace_id, revision.definition.datasets)
                staging, quality = self.prepare(workspace_id)
                if reason == "REFERENCE_IDENTITY_CYCLE":
                    self.approve(workspace_id)
                    self.expect_business_writes(workspace_id)
                    target = HierarchyOdoo(
                        self.context.queries.get_odoo_schema_catalog(workspace_id)
                    )
                    report = self.context.preflight.compare(
                        workspace_id, reader=target.compare, actor=self.actor
                    )
                    self.assertGreater(report.blocked_count, 0)
                    self.assertEqual(target.writes, [])
                else:
                    self.assertGreater(quality.quarantined_count, 0)
                    issues = self.context.quality.current_run(workspace_id)
                    self.assertIn(
                        reason, {issue.reason_code for issue in issues.issues}
                    )
                self.assertFalse(
                    any(row.target_scope == (None,) for row in staging.rows)
                )

    def test_same_table_is_available_for_a_self_many2one_outside_the_matching_rule(
        self,
    ):
        workspace_id, source = same_table_workspace(self)
        revision = self.context.queries.get_mapping_revision(workspace_id)
        mapping = revision.definition.datasets[0]
        selection = self.context.queries.get_source_selection(workspace_id)
        schema = self.context.queries.get_odoo_schema_catalog(workspace_id)
        governance = self.context.queries.get_schema_governance(workspace_id)
        governance = replace(
            governance,
            business_keys=(replace(governance.business_keys[0], scope_fields=()),),
        )
        # Removing the scope moves Parent Category into the linked-field catalog.
        views = _mapping_dataset_views(
            selection, schema, governance, (replace(mapping, target_scope=()),)
        )
        parent = next(
            row
            for row in views[0]["relation_rows"]
            if row["metadata"].name == "parent_id"
        )
        self.assertIn(source, parent["related_datasets"])
