"""Reuse the complete hierarchy Recipe on another accepted, reordered delivery."""

from datetime import UTC, datetime
from uuid import uuid4

from impodo.web.composition.page_reads import run_local_operation_sync
from tests.integration.web.test_fresh_data_controls import field
from tests.support.browser_scenarios import POST_HEADERS
from tests.support.hierarchy_assertions import (
    assert_frozen_hierarchy,
    assert_loaded_hierarchy,
    assert_prepared_hierarchy,
    require_parent_receipts,
)
from tests.support.hierarchy_cases import CATEGORY_ROWS, supplied_files
from tests.support.hierarchy_execution import compare_hierarchy, json2_adapters
from tests.support.hierarchy_workflow import (
    PRODUCT_ROWS,
    HierarchyWorkflowFixture,
    product_csv,
)


class HierarchyRecipeTests(HierarchyWorkflowFixture):
    def test_generated_odoo19_local(self):
        self._reuse(19, "LOCAL", "generated")

    def test_generated_odoo20_local(self):
        self._reuse(20, "LOCAL", "generated")

    def test_generated_odoo19_remote(self):
        self._reuse(19, "REMOTE", "generated")

    def test_generated_odoo20_remote(self):
        self._reuse(20, "REMOTE", "generated")

    def test_supplied_odoo19_local(self):
        self._reuse(19, "LOCAL", "supplied")

    def test_supplied_odoo20_local(self):
        self._reuse(20, "LOCAL", "supplied")

    def test_supplied_odoo19_remote(self):
        self._reuse(19, "REMOTE", "supplied")

    def test_supplied_odoo20_remote(self):
        self._reuse(20, "REMOTE", "supplied")

    def test_supplied_zero_root_odoo19_remote(self):
        self._reuse(19, "REMOTE", "supplied", zero_root=True)

    def test_supplied_zero_root_odoo20_remote(self):
        self._reuse(20, "REMOTE", "supplied", zero_root=True)

    def _reuse(self, major, mode, source_shape, *, zero_root=False):
        context, actor = self.context, self.actor
        workspace_id, original = self.hierarchy(
            major=major, mode=mode, source_shape=source_shape, zero_root=zero_root,
        )
        self.prepare(workspace_id)
        owner = context.migration_workspaces.get(workspace_id, actor=actor)
        draft = context.recipe_publication.draft(
            project_id=owner.project_id,
            data_version_id=owner.data_version_id,
            workspace_id=workspace_id,
            actor=actor,
        )
        self.assertTrue(draft.can_publish, draft.issues)
        publication = run_local_operation_sync(
            context.recipe_publication.publish,
            project_id=owner.project_id,
            data_version_id=owner.data_version_id,
            workspace_id=workspace_id,
            display_name="Fictional workshop hierarchy",
            business_purpose="Keep complete category paths and product links",
            actor=actor,
        )
        setup = context.test_runs.start_setup(
            owner.project_id,
            expected_workspace_revision=context.migration_projects.get(
                owner.project_id, actor=actor
            ).optimistic_revision,
            recipe_revisions=(
                (publication.recipe.recipe_id, publication.revision.version),
            ),
            dependencies=(),
            label="Reordered fictional delivery",
            export_as_of=datetime.now(UTC).date().isoformat(),
            operation_id=str(uuid4()),
            actor=actor,
        )
        url = f"/projects/{owner.project_id}/test-runs/{setup.run.migration_run_id}/fresh-data"
        page = self.client.get(url)
        fresh_rows = tuple(reversed(PRODUCT_ROWS))
        files = (
            supplied_files(fresh_rows, categories=tuple(
                (key, name, parent or "0") if zero_root else (key, name, parent)
                for key, name, parent in reversed(CATEGORY_ROWS)
            ))
            if source_shape == "supplied"
            else {"products": product_csv(fresh_rows)}
        )
        uploaded = self.client.post(
            url + "/files",
            headers=POST_HEADERS,
            follow_redirects=False,
            data={
                "csrf_token": field(page, "csrf_token"),
                "revision": field(page, "revision"),
            },
            files=[
                ("source_file", (name + ".csv", value, "text/csv"))
                for name, value in files.items()
            ],
        )
        self.assertEqual(uploaded.status_code, 303, uploaded.text)
        page = self.client.get(url)
        registered = self.client.post(
            url + "/register",
            headers=POST_HEADERS,
            follow_redirects=False,
            data={
                "csrf_token": field(page, "csrf_token"),
                "revision": field(page, "revision"),
            },
        )
        self.assertEqual(registered.status_code, 303, registered.text)
        page = self.client.get(url)
        accepted = self.client.post(
            url + "/accept",
            headers=POST_HEADERS,
            follow_redirects=False,
            data={
                "csrf_token": field(page, "csrf_token"),
                "parameter_revision": "",
                "warnings_acknowledged": "1",
            },
        )
        self.assertEqual(accepted.status_code, 303, accepted.text)
        schema = self.schema(setup.setup_workspace.workspace_id, major=major, mode=mode)
        activated = run_local_operation_sync(
            context.test_runs.activate,
            owner.project_id,
            setup.run.migration_run_id,
            expected_workspace_revision=context.migration_projects.get(
                owner.project_id, actor=actor
            ).optimistic_revision,
            target_schema=schema,
            target_reference_bundle=None,
            credential_generation=schema.read_credential_binding_hash,
            operation_id=str(uuid4()),
            actor=actor,
        )
        application = activated.applications[0]
        self.assertEqual(
            application.status.value,
            "READY",
            context.run_planning.repository.list_issues(application.application_id),
        )
        rebound = context.mapping_workspace.mappings.get_mapping_revision(
            application.workspace_id
        )
        self.assertNotEqual(
            {mapping.dataset_id for mapping in original.definition.datasets},
            {mapping.dataset_id for mapping in rebound.definition.datasets},
        )
        category = next(
            mapping
            for mapping in rebound.definition.datasets
            if mapping.target_model == "product.category"
        )
        self.assertEqual(
            category.target_scope[0].null_policy.value,
            "zero_as_scope_null" if zero_root else "explicit_scope_null",
        )
        staging, quality = self.prepare(application.workspace_id)
        assert_prepared_hierarchy(self, application.workspace_id, staging, quality)
        self.approve(application.workspace_id)
        self.expect_business_writes(application.workspace_id)
        target, snapshot, identities = compare_hierarchy(self, application.workspace_id)
        assert_frozen_hierarchy(self, snapshot)
        receipts = require_parent_receipts(self, application.workspace_id, target)
        writer, reader = json2_adapters(self, application.workspace_id, target)
        execution = context.execution.execute(
            application.workspace_id,
            expected_snapshot_hash=snapshot.semantic_hash,
            executor=writer,
            actor=actor,
            **identities,
        )
        self.assertEqual(execution.committed_count, 13)
        self.assertGreater(len(receipts), 0)
        assert_loaded_hierarchy(self, target)
        write = {
            key: value for key, value in identities.items() if key.startswith("write_")
        }
        result = context.reconciliation.reconcile(
            application.workspace_id,
            expected_execution_run_id=execution.run_id,
            reader=reader,
            actor=actor,
            **write,
        )
        self.assertEqual(result.status.value, "VERIFIED", result.rows)
        self.assertEqual(result.verified_count, 18)
