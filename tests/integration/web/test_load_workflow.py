"""Focused browser evidence for one Impodo capability."""

from __future__ import annotations

from tests.support.browser_scenarios import (
    Capability,
    ExecutionRowStatus,
    ExecutionRunStatus,
    LoadJobResult,
    OdooReadIdentity,
    OdooWriteIdentity,
    POST_HEADERS,
    ProjectSetupBrowserTestCase,
    SimpleNamespace,
    TargetCredentialRole,
    _wait_for_load,
    patch,
    store_target_credential,
)


class LoadWorkflowBrowserTests(ProjectSetupBrowserTestCase):
    def test_load_receipt_rows_offer_twenty_or_fifty_with_pagination(self) -> None:
        context = self.app.state.context
        workspace_state = self.workspaces.create(
            name="Paginated load review",
            source_system="Other",
        )
        workspace_state = context.workspace_states.update_target(
            workspace_state.workspace_id,
            actor=context.actor,
            expected_revision=workspace_state.revision,
            odoo_connection_mode="REMOTE",
            odoo_base_url="https://odoo.example.test",
            odoo_database="migration",
            intended_applications=("Contacts",),
            intended_models=(),
        )
        status = SimpleNamespace(value="COMMITTED")
        rows = tuple(
            SimpleNamespace(
                dataset="contacts",
                source_row=index,
                target_model="res.partner",
                odoo_id=index,
                operation="CREATE",
                status=status,
                safe_error="",
            )
            for index in range(1, 56)
        )
        current_run = SimpleNamespace(
            rows=rows,
            committed_count=55,
            failed_count=0,
            blocked_count=0,
            partially_applied_count=0,
            unknown_count=0,
            run_id="run-1",
        )
        preview = SimpleNamespace(
            snapshot=SimpleNamespace(
                counts={"CREATE": 55, "UPDATE": 0, "UNCHANGED": 0},
                target_database="migration",
                target_odoo_version="19.0",
                semantic_hash="sha256:" + "a" * 64,
                target_hash="sha256:" + "b" * 64,
            ),
            datasets=(),
            current_run=current_run,
            can_load=False,
        )

        with (
            patch.object(
                type(context.execution),
                "current_preview",
                return_value=preview,
            ),
            patch.object(
                type(context.reconciliation),
                "current",
                return_value=None,
            ),
        ):
            page = self.client.get(
                f"/workspaces/{workspace_state.workspace_id}/load"
                "?rows_page=2&rows_per_page=20"
            )

        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.text.count("data-load-row"), 20)
        self.assertIn("Showing 21-40 of 55 records", page.text)
        self.assertIn("Rows per page:", page.text)
        self.assertIn(">20</a>", page.text)
        self.assertIn(">50</a>", page.text)
        self.assertIn("Page 2 of 3", page.text)
        self.assertIn("row 21", page.text)
        self.assertIn("row 40", page.text)
        self.assertNotIn("row 41", page.text)

    def test_load_review_separates_read_only_review_from_confirmation(self) -> None:
        context = self.app.state.context
        workspace_state = self.workspaces.create(
            name="Staged load review",
            source_system="Other",
        )
        workspace_state = context.workspace_states.update_target(
            workspace_state.workspace_id,
            actor=context.actor,
            expected_revision=workspace_state.revision,
            odoo_connection_mode="REMOTE",
            odoo_base_url="https://odoo.example.test",
            odoo_database="migration",
            intended_applications=("Contacts",),
            intended_models=(),
        )
        preview = SimpleNamespace(
            snapshot=SimpleNamespace(
                counts={"CREATE": 2, "UPDATE": 1, "UNCHANGED": 4},
                write_count=3,
                target_database="migration",
                target_odoo_version="19.0",
                semantic_hash="sha256:" + "a" * 64,
                target_hash="sha256:" + "b" * 64,
            ),
            datasets=(),
            current_run=None,
            can_load=True,
            scope_error="",
            deferred_create_count=0,
            dependency_summary=SimpleNamespace(
                groups=(
                    SimpleNamespace(
                        number=1,
                        record_count=2,
                        dataset_labels=("Units", "Products"),
                        omitted_dataset_count=0,
                    ),
                    SimpleNamespace(
                        number=2,
                        record_count=1,
                        dataset_labels=("BOM Lines",),
                        omitted_dataset_count=0,
                    ),
                ),
                total_group_count=2,
                omitted_group_count=0,
                relationship_record_count=0,
                relationship_field_count=0,
                relationship_link_count=2,
            ),
            blocker_summary=SimpleNamespace(
                groups=(),
                total_group_count=0,
                omitted_group_count=0,
            ),
        )

        with (
            patch.object(
                type(context.execution),
                "current_preview",
                return_value=preview,
            ),
            patch.object(
                type(context.reconciliation),
                "current",
                return_value=None,
            ),
        ):
            review = self.client.get(
                f"/workspaces/{workspace_state.workspace_id}/load/review"
            )
            confirm = self.client.get(
                f"/workspaces/{workspace_state.workspace_id}/load/confirm"
            )
            outcome = self.client.get(
                f"/workspaces/{workspace_state.workspace_id}/load/outcome",
                follow_redirects=False,
            )
            preview.can_load = False
            preview.scope_error = "One reviewed field is no longer available."
            preview.blocker_summary = SimpleNamespace(
                groups=(
                    SimpleNamespace(
                        record_count=2,
                        title="A related source record is missing",
                        action="Add the supporting record, then compare again.",
                        dataset_labels=("BOM Lines",),
                        omitted_dataset_count=0,
                    ),
                ),
                total_group_count=1,
                omitted_group_count=0,
            )
            blocked_review = self.client.get(
                f"/workspaces/{workspace_state.workspace_id}/load/review"
            )
            blocked_confirm = self.client.get(
                f"/workspaces/{workspace_state.workspace_id}/load/confirm",
                follow_redirects=False,
            )

        self.assertEqual(review.status_code, 200)
        self.assertIn("Check what will change in Odoo", review.text)
        self.assertIn("Nothing is written from this page", review.text)
        self.assertIn("What Impodo will load first", review.text)
        self.assertIn("Units, Products", review.text)
        self.assertIn("BOM Lines", review.text)
        self.assertNotIn('name="write_api_key"', review.text)
        self.assertEqual(confirm.status_code, 200)
        self.assertIn("Load 3 records into migration", confirm.text)
        self.assertIn('name="write_api_key"', confirm.text)
        self.assertIn("Advanced settings", confirm.text)
        self.assertIn("Load 3 records into Odoo", confirm.text)
        self.assertEqual(outcome.status_code, 303)
        self.assertEqual(
            outcome.headers["location"],
            f"/workspaces/{workspace_state.workspace_id}/load/review",
        )
        self.assertEqual(blocked_confirm.status_code, 303)
        self.assertIn("Why loading is blocked", blocked_review.text)
        self.assertIn("A related source record is missing", blocked_review.text)
        self.assertIn("Add the supporting record", blocked_review.text)
        self.assertEqual(
            blocked_confirm.headers["location"],
            f"/workspaces/{workspace_state.workspace_id}/load/review",
        )

    def test_load_progress_names_target_and_exposes_non_secret_counts(self) -> None:
        context = self.app.state.context
        workspace_state = self.workspaces.create(
            name="Visible load progress",
            source_system="Other",
        )
        workspace_state = context.workspace_states.update_target(
            workspace_state.workspace_id,
            actor=context.actor,
            expected_revision=workspace_state.revision,
            odoo_connection_mode="REMOTE",
            odoo_base_url="https://odoo.example.test",
            odoo_database="migration",
            intended_applications=("Contacts",),
            intended_models=(),
        )
        manager = context.load_jobs
        assert manager is not None
        job = manager.enqueue(
            workspace_state.workspace_id,
            workspace_state.name,
            target_database="migration",
            target_server="odoo.example.test",
            target_environment="Test",
            total_rows=3,
            relationship_total_rows=1,
            load_group_count=2,
            access_context=context.workspace_access.resolve(
                workspace_state.workspace_id,
                actor=context.actor,
                capability=Capability.EXPORT_PLAN_EXECUTE,
            ),
            work=lambda _access, _writing, _verifying: LoadJobResult(
                execution_run_id="run-1",
                verification_complete=False,
            ),
        )
        progress_url = (
            f"/workspaces/{workspace_state.workspace_id}/load/progress/{job.job_id}"
        )

        finished = _wait_for_load(self.client, progress_url)
        page = self.client.get(progress_url)

        self.assertEqual(page.status_code, 200, page.text)
        self.assertIn("data-load-job", page.text)
        self.assertIn(f"Loading {workspace_state.name} into Test Odoo", page.text)
        self.assertIn("odoo.example.test", page.text)
        self.assertIn("migration", page.text)
        self.assertEqual(finished["total_rows"], 3)
        self.assertEqual(finished["created_count"], 0)
        self.assertEqual(finished["updated_count"], 0)
        self.assertEqual(finished["attention_count"], 0)
        self.assertEqual(finished["relationship_total_count"], 1)
        self.assertEqual(finished["load_group_count"], 2)
        self.assertIn("Recorded load group", page.text)
        self.assertNotIn("write_api_key", page.text)

    def test_confirmed_load_runs_in_background_and_redirects_to_progress(self) -> None:
        context = self.app.state.context
        workspace_state = self.workspaces.create(
            name="Background load",
            source_system="Other",
        )
        workspace_state = context.workspace_states.update_target(
            workspace_state.workspace_id,
            actor=context.actor,
            expected_revision=workspace_state.revision,
            odoo_connection_mode="REMOTE",
            odoo_base_url="https://odoo.example.test",
            odoo_database="migration",
            intended_applications=("Contacts",),
            intended_models=(),
        )
        read_credential = store_target_credential(
            context.secret_store,
            workspace_state,
            TargetCredentialRole.READ,
            "read-secret",
            persistent=False,
        )
        store_target_credential(
            context.secret_store,
            workspace_state,
            TargetCredentialRole.WRITE,
            "write-secret",
            persistent=False,
        )
        semantic_hash = "sha256:" + "a" * 64
        target_hash = "sha256:" + "b" * 64
        read_context_hash = "sha256:" + "c" * 64
        snapshot = SimpleNamespace(
            semantic_hash=semantic_hash,
            target_hash=target_hash,
            target_database="migration",
            target_odoo_version="19.0",
            write_count=2,
            readable_models=("res.partner",),
            read_context_hash=read_context_hash,
            read_credential_binding_hash=read_credential.binding_hash,
        )
        preview = SimpleNamespace(
            snapshot=snapshot,
            api_scope=SimpleNamespace(models=()),
            scope_error="",
            current_run=None,
            can_load=True,
        )
        attempts = (
            SimpleNamespace(
                operation="CREATE",
                status=ExecutionRowStatus.COMMITTED,
            ),
            SimpleNamespace(
                operation="UPDATE",
                status=ExecutionRowStatus.COMMITTED,
            ),
        )
        completed_run = SimpleNamespace(
            run_id="11111111-1111-4111-8111-111111111111",
            total_count=2,
            planned_count=0,
            status=ExecutionRunStatus.COMPLETED,
            rows=attempts,
        )

        def execute(_project_id, **kwargs):
            kwargs["progress"](completed_run)
            return completed_run

        read_identity = OdooReadIdentity(
            target_hash=target_hash,
            principal_hash="sha256:" + "d" * 64,
            permission_hash="sha256:" + "e" * 64,
            context_hash=read_context_hash,
            readable_models=("res.partner",),
            observed_at="2026-08-23T00:00:00Z",
        )
        write_identity = OdooWriteIdentity(
            target_hash=target_hash,
            principal_hash="sha256:" + "1" * 64,
            permission_hash="sha256:" + "2" * 64,
            context_hash=read_context_hash,
            readable_models=("res.partner",),
            writable_models=("res.partner",),
            observed_at="2026-08-23T00:00:00Z",
        )

        with (
            patch.object(
                type(context.execution),
                "current_preview",
                return_value=preview,
            ),
            patch.object(type(context.execution), "execute", side_effect=execute),
            patch.object(
                type(context.reconciliation),
                "reconcile",
                return_value=SimpleNamespace(
                    unknown_count=0,
                    fallout_count=0,
                ),
            ),
            patch.object(
                type(context.cutover_plans),
                "assert_application_can_execute",
                return_value=None,
            ),
            patch.object(
                type(context.production_runs),
                "credential_workspace",
                return_value=workspace_state,
            ),
            patch.object(
                type(context.production_runs),
                "assert_execution_authority",
                return_value=None,
            ),
            patch.object(
                type(context.queries),
                "get_odoo_schema_catalog",
                return_value=SimpleNamespace(read_context_hash=read_context_hash),
            ),
            patch.object(context, "read_identity_probe", return_value=read_identity),
            patch.object(context, "write_identity_probe", return_value=write_identity),
            patch.object(context, "write_executor_factory", return_value=object()),
            patch.object(context, "readback_reader_factory", return_value=object()),
        ):
            started = self.client.post(
                f"/workspaces/{workspace_state.workspace_id}/load",
                data={
                    "csrf_token": self.csrf,
                    "snapshot_hash": semantic_hash,
                    "batch_rows": "10",
                },
                headers=POST_HEADERS,
                follow_redirects=False,
            )
            self.assertEqual(started.status_code, 303, started.text)
            self.assertIn("/load/progress/", started.headers["location"])
            finished = _wait_for_load(self.client, started.headers["location"])

        self.assertEqual(finished["status"], "SUCCEEDED", finished)
        self.assertEqual(finished["completed_rows"], 2)
        self.assertEqual(finished["created_count"], 1)
        self.assertEqual(finished["updated_count"], 1)
        self.assertTrue(finished["verification_complete"])
        self.assertEqual(
            finished["redirect_url"],
            f"/workspaces/{workspace_state.workspace_id}/load/outcome",
        )
