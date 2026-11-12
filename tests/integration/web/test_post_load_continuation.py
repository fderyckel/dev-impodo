"""Exercise saved loads and repeated exact-target edits with real repositories."""

from dataclasses import asdict, replace
from datetime import UTC, datetime
from io import BytesIO
from unittest.mock import patch
from uuid import uuid4

from openpyxl import load_workbook

from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from impodo.adapters.odoo.connectors import Json2Config
from impodo.adapters.odoo.writer import Json2WriteExecutor
from impodo.domain.correction_origin import CorrectionOriginError
from impodo.domain.mapping.contracts import (
    DatasetMapping,
    IdentityComponentMapping,
    ScalarFieldMapping,
    ScalarValueSource,
)
from impodo.domain.schema.governance import BusinessKeyDefinition, BusinessKeyStatus
from impodo.domain.serialization import content_hash
from impodo.domain.shared.models import OdooReadIdentity, OdooWriteIdentity
from impodo.domain.workspace.contracts import SchemaField, SchemaModel
from impodo.web.target_credentials import TargetCredentialRole, store_target_credential
from tests.support.browser_scenarios import POST_HEADERS
from tests.support.hierarchy_odoo import HierarchyOdoo
from tests.support.hierarchy_workflow import HierarchyWorkflowFixture


class PostLoadContinuationTests(HierarchyWorkflowFixture):
    def _initial_load(self, major, mode, model):
        workspace = self.freeze_files({"records": b"Code,Name\nA1,Alpha\nB1,Beta\n"})
        key = "ref" if model == "res.partner" else "default_code"
        extra = (
            (
                SchemaModel(
                    "res.partner",
                    "Contacts",
                    (
                        SchemaField(
                            "name", "Name", "char", True, False, None, None, ()
                        ),
                        SchemaField(
                            "ref", "Reference", "char", False, False, None, None, ()
                        ),
                    ),
                ),
            )
            if model == "res.partner"
            else ()
        )
        schema = self.schema(
            workspace,
            major=major,
            mode=mode,
            extra_models=extra,
            extra_keys=(
                BusinessKeyDefinition(
                    "contact:ref",
                    "res.partner",
                    ("ref",),
                    status=BusinessKeyStatus.CONFIRMED,
                ),
            )
            if extra
            else (),
        )
        models = tuple(
            replace(
                item,
                fields=(
                    *item.fields,
                    SchemaField(
                        "description",
                        "Description",
                        "text",
                        False,
                        False,
                        None,
                        None,
                        (),
                    ),
                ),
            )
            if item.name == model
            else item
            for item in schema.models
        )
        schema = replace(
            schema,
            models=models,
            content_hash=content_hash([asdict(item) for item in models]),
        )
        governance = self.context.queries.get_schema_governance(workspace)
        self.context.schema_workspace.schemas.save_odoo_schema_catalog(
            workspace, schema, actor=self.actor
        )
        self.context.schema_workspace.govern(
            workspace, business_keys=governance.business_keys, actor=self.actor
        )
        dataset = self.context.queries.get_mapping_source_selection(workspace).datasets[
            0
        ]
        columns = {item.source_name: item.stable_key for item in dataset.columns}
        self.submit(
            workspace,
            (
                DatasetMapping(
                    dataset.dataset_id,
                    model,
                    source_identity_column_keys=(columns["Code"],),
                    target_identity=(
                        IdentityComponentMapping((columns["Code"],), (key,)),
                    ),
                    fields=(
                        ScalarFieldMapping("name", columns["Name"], required=True),
                    ),
                ),
            ),
        )
        self.prepare(workspace)
        self.approve(workspace)
        self.expect_business_writes(workspace)
        target = HierarchyOdoo(schema)
        report = self.context.preflight.compare(
            workspace, reader=target.compare, actor=self.actor
        )
        self.assertEqual(report.status, "READY", report)
        preview = self.context.execution.current_preview(workspace, actor=self.actor)
        snapshot = preview.snapshot
        target.scope_hash = preview.api_scope.semantic_hash
        read_identity = OdooReadIdentity(
            target_hash=snapshot.target_hash,
            principal_hash=snapshot.read_principal_hash,
            permission_hash=snapshot.read_permission_hash,
            context_hash=snapshot.read_context_hash,
            readable_models=snapshot.readable_models,
            observed_at=datetime.now(UTC).isoformat(),
        )
        write_identity = OdooWriteIdentity(
            target_hash=snapshot.target_hash,
            principal_hash=content_hash("writer"),
            permission_hash=content_hash("permission"),
            context_hash=snapshot.read_context_hash,
            readable_models=tuple(item.model for item in preview.api_scope.models),
            writable_models=tuple(item.model for item in preview.api_scope.models if item.write_fields),
            observed_at=datetime.now(UTC).isoformat(),
        )
        state = self.context.queries.get(workspace)
        write_key = store_target_credential(
            self.context.secret_store,
            state,
            TargetCredentialRole.WRITE,
            "fixture-write-key",
            persistent=False,
        )
        identities = {
            "read_identity": read_identity,
            "read_credential_binding_hash": snapshot.read_credential_binding_hash,
            "write_identity": write_identity if mode == "REMOTE" else None,
            "write_credential_binding_hash": write_key.binding_hash if mode == "REMOTE" else "",
        }
        self.context.execution.execute(
            workspace,
            expected_snapshot_hash=snapshot.semantic_hash,
            executor=target,
            actor=self.actor,
            **identities,
        )
        execution = self.context.corrections.executions.get_current_run(workspace)
        reconciliation = self.context.reconciliation.reconcile(
            workspace,
            expected_execution_run_id=execution.run_id,
            reader=target,
            actor=self.actor,
            **{
                key: value
                for key, value in identities.items()
                if key.startswith("write_")
            },
        )
        self.assertEqual(reconciliation.status.value, "VERIFIED")
        self.context.corrections.publish_completed_load(workspace, actor=self.actor)
        return workspace, schema, target

    def _continue(self, workspace, schema):
        async def capture(context, state, *, actor):
            from impodo.web.composition.page_reads import run_local_operation

            await run_local_operation(
                context.schema_workspace.schemas.save_odoo_schema_catalog,
                state.workspace_id,
                replace(
                    schema,
                    workspace_id=state.workspace_id,
                    captured_at=datetime.now(UTC),
                ),
                actor=actor,
            )

        with (
            patch(
                "impodo.web.routers.history._capture_selected_schema",
                side_effect=capture,
            ),
            patch("impodo.web.routers.history._flash") as flashes,
        ):
            project = self.context.migration_workspaces.get(
                workspace, actor=self.actor
            ).project_id
            response = self.client.post(
                f"/projects/{project}/completed-loads/{workspace}/continue",
                data={"csrf_token": self.csrf, "request_id": str(uuid4())},
                headers=POST_HEADERS,
                follow_redirects=False,
            )
        self.assertEqual(response.status_code, 303, response.text)
        self.assertTrue(
            response.headers["location"].endswith("/mapping"), str(flashes.call_args)
        )
        identifier = response.headers["location"].split("/")[2]
        self.assertIsNotNone(self.context.queries.get_odoo_schema_catalog(identifier))
        return identifier

    def _review(self, root, schema, target):
        stage = self.context.corrections.reviewer.pipeline.stages.target
        read = OdooReadIdentity(
            target_hash=schema.connection_target_hash,
            principal_hash=schema.read_principal_hash,
            permission_hash=schema.read_permission_hash,
            context_hash=schema.read_context_hash,
            readable_models=tuple(sorted(item.name for item in schema.models)),
            observed_at=datetime.now(UTC).isoformat(),
        )

        def reader(_state, _key, scope):
            target.scope_hash = scope.semantic_hash
            return target

        with (
            patch.object(stage, "read_identity_probe", return_value=read),
            patch.object(stage, "readback_reader_factory", side_effect=reader),
        ):
            return self.context.corrections.review(
                root,
                actor=self.actor,
                review_request_id=str(uuid4()),
                audit_unchanged=True,
            )

    def test_odoo19_local(self):
        self._version_mode(19, "LOCAL")

    def test_odoo20_local(self):
        self._version_mode(20, "LOCAL")

    def test_odoo19_remote(self):
        self._version_mode(19, "REMOTE")

    def test_odoo20_remote(self):
        self._version_mode(20, "REMOTE")

    def _version_mode(self, major, mode):
        for model in ("product.template", "res.partner"):
            with self.subTest(major=major, mode=mode, model=model):
                self._cycle(major, mode, model)

    def _cycle(self, major, mode, model):
        root, schema, target = self._initial_load(major, mode, model)
        before = dict(target.records)
        with collect_duckdb_request_timings() as timings:
            saved = self.client.get(f"/workspaces/{root}/history/3")
        self.assertLessEqual(timings.connection_count, 6)
        self.assertLessEqual(timings.schema_check_count, 6)
        self.assertEqual(timings.lock_retry_count, 0)
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertIn("Saved field mappings", saved.text)
        self.assertIsNone(
            self.context.corrections.get(root, actor=self.actor).successor_workspace_id
        )
        successor = self._continue(root, schema)
        seed = self.context.queries.get_mapping_working_draft(successor)
        fields = (
            *seed.definition.datasets[0].fields,
            ScalarFieldMapping(
                "description",
                value_source=ScalarValueSource.CONSTANT,
                literal_value="Forgotten field",
            ),
        )
        self.submit(successor, (replace(seed.definition.datasets[0], fields=fields),))
        resumed = self.context.corrections.start(
            root, actor=self.actor, request_id=str(uuid4())
        )
        self.assertEqual(resumed.workspace.workspace_id, successor)
        self.assertEqual(
            self.context.queries.get_mapping_revision(successor)
            .definition.datasets[0]
            .fields,
            tuple(sorted(fields, key=lambda item: item.target_field)),
        )
        root_workspace = self.context.migration_workspaces.get(root, actor=self.actor)
        draft = self.context.recipe_publication.draft(
            project_id=root_workspace.project_id,
            data_version_id=root_workspace.data_version_id,
            workspace_id=root,
            actor=self.actor,
        )
        self.assertTrue(draft.can_publish, draft.issues)
        publication = self.context.recipe_publication.publish(
            project_id=root_workspace.project_id,
            data_version_id=root_workspace.data_version_id,
            workspace_id=root,
            display_name="Successful rules",
            business_purpose="Reuse a completed load",
            operation_id=str(uuid4()),
            actor=self.actor,
        )
        self.assertIsNotNone(publication)
        source_page = self.client.get(
            f"/projects/{root_workspace.project_id}?recipe_workspace={root}"
        )
        self.assertEqual(source_page.status_code, 200, source_page.text)
        self.assertIn(f'name="workspace_id" value="{root}"', source_page.text)
        self.prepare(successor)
        self.approve(successor)
        review, plan, _ = self._review(root, schema, target)
        self.assertFalse(review.blockers)
        self.assertEqual(len(plan.fields), 2)
        self.assertEqual(
            {field.odoo_id for field in plan.fields},
            {key[1] for key in before if key[0] == model},
        )
        for step in ("prepared", "compare", "results", "confirm"):
            page = self.client.get(f"/workspaces/{successor}/continuation/{step}")
            self.assertEqual(page.status_code, 200, page.text)
        stale_export = self.client.get(
            f"/workspaces/{successor}/continuation/workbook?comparison_id=other"
        )
        self.assertEqual(stale_export.status_code, 409)
        write = OdooWriteIdentity(
            target_hash=plan.target_hash,
            principal_hash=content_hash("writer"),
            permission_hash=content_hash("permission"),
            context_hash=plan.read_context_hash,
            readable_models=plan.readable_models,
            writable_models=(model,),
            observed_at=datetime.now(UTC).isoformat(),
        )
        self.context.corrections.confirm(
            root,
            confirmation_id=str(uuid4()),
            write_credential_binding_hash=content_hash("write-key"),
            write_identity=write,
            actor=self.actor,
        )
        from impodo.application.correction_execution import correction_api_scope
        from impodo.domain.correction_execution import CorrectionExecutionSnapshot

        confirmation = self.context.corrections.current_confirmation(root)
        scope = correction_api_scope(
            CorrectionExecutionSnapshot.create(
                plan, confirmation, target_database=schema.database
            )
        )
        target.scope_hash = scope.semantic_hash
        state = self.context.queries.get(successor)

        def transport(url, headers, body, timeout, method):
            if url.endswith("/write"):
                blocked_edit = self.client.post(
                    f"/workspaces/{successor}/mapping",
                    data={"csrf_token": self.csrf},
                    headers=POST_HEADERS,
                    follow_redirects=False,
                )
                self.assertEqual(blocked_edit.status_code, 409)
                import json

                values = json.loads(body)
                for identifier in values["ids"]:
                    target.records[(model, identifier)].update(values["vals"])
                target.writes.append((model, tuple(values["ids"]), values["vals"]))
                return 200, True
            return target.transport(url, headers, body, timeout, method)

        writer = Json2WriteExecutor(
            Json2Config(
                base_url=state.odoo_base_url,
                database=schema.database,
                api_key="fixture-key",
                connection_mode=mode,
                retries=0,
            ),
            scope,
            transport=transport,
        )
        if major == 19 and mode == "LOCAL" and model == "product.template":
            import time

            project = self.context.migration_workspaces.get(
                root, actor=self.actor
            ).project_id

            def bound_reader(_state, _key, bound_scope):
                target.scope_hash = bound_scope.semantic_hash
                return target

            def bound_writer(_state, _key, bound_scope):
                return Json2WriteExecutor(
                    writer.config, bound_scope, transport=transport
                )

            with (
                patch.object(self.context, "write_identity_probe", return_value=write),
                patch.object(
                    self.context, "readback_reader_factory", side_effect=bound_reader
                ),
                patch.object(
                    self.context, "write_executor_factory", side_effect=bound_writer
                ),
            ):
                response = self.client.post(
                    f"/projects/{project}/completed-loads/{root}/apply?continue=1",
                    data={
                        "csrf_token": self.csrf,
                        "confirmation_id": str(uuid4()),
                        "confirm_apply": "yes",
                        "write_api_key": "fixture-write-key",
                    },
                    headers=POST_HEADERS,
                    follow_redirects=False,
                )
                self.assertEqual(response.status_code, 303, response.text)
                job_id = (
                    response.headers["location"].split("/progress/")[1].split("?")[0]
                )
                deadline = time.monotonic() + 20
                job = self.context.correction_jobs.get(root, job_id)
                while not job.terminal and time.monotonic() < deadline:
                    time.sleep(0.02)
                    job = self.context.correction_jobs.get(root, job_id)
                self.assertTrue(job.terminal)
                self.assertEqual(job.status.value, "SUCCEEDED", job.failure_message)
                self.assertTrue(job.result.verified, job.result)
                progress = self.client.get(
                    response.headers["location"], follow_redirects=False
                )
                self.assertEqual(
                    progress.headers["location"],
                    f"/workspaces/{successor}/continuation/outcome",
                )
        elif major == 20 and mode == "REMOTE" and model == "res.partner":
            with (
                self.assertLogs(
                    "impodo.application.correction_workflow", level="WARNING"
                ),
                patch.object(
                    self.context.corrections,
                    "_publish_next_origin",
                    side_effect=OSError("simulated publication interruption"),
                ),
            ):
                result = self.context.corrections.execute(
                    root,
                    target_database=schema.database,
                    write_credential_binding_hash=content_hash("write-key"),
                    write_identity=write,
                    reader=target,
                    writer=writer,
                    actor=self.actor,
                )
            self.assertEqual(result.reconciliation.status.value, "VERIFIED")
            self.assertIsNone(self.context.corrections.get(successor, actor=self.actor))
        else:
            result = self.context.corrections.execute(
                root,
                target_database=schema.database,
                write_credential_binding_hash=content_hash("write-key"),
                write_identity=write,
                reader=target,
                writer=writer,
                actor=self.actor,
            )
            self.assertEqual(result.reconciliation.status.value, "VERIFIED")
        with self.assertRaisesRegex(CorrectionOriginError, "revision is saved"):
            self._review(root, schema, target)
        self.assertEqual(set(target.records), set(before))
        self.assertTrue(
            all(
                values["description"] == "Forgotten field"
                for (name, _), values in target.records.items()
                if name == model
            )
        )
        origin_delayed = major == 20 and mode == "REMOTE" and model == "res.partner"
        if not origin_delayed:
            self.assertIsNotNone(
                self.context.corrections.get(successor, actor=self.actor)
            )
        history = self.client.get(f"/workspaces/{successor}/history/5")
        self.assertEqual(history.status_code, 200, history.text)
        self.assertIn("Saved verified comparison", history.text)
        exported = self.client.get(
            f"/workspaces/{successor}/continuation/workbook?comparison_id={plan.plan_id}"
        )
        self.assertEqual(
            exported.status_code,
            200,
            exported.text[:100] if exported.status_code != 200 else "",
        )
        book = load_workbook(BytesIO(exported.content), read_only=True)
        self.assertIn("Reviewed changes", book.sheetnames)
        book.close()
        next_revision = self._continue(successor, schema)
        seed = self.context.queries.get_mapping_working_draft(next_revision)
        self.submit(next_revision, seed.definition.datasets)
        self.prepare(next_revision)
        self.approve(next_revision)
        writes = len(target.writes)
        changed_record = next(
            values
            for (record_model, _), values in target.records.items()
            if record_model == model and values.get("name") == "Beta"
        )
        changed_record["name"] = "Edited in Odoo"
        review, no_changes, _ = self._review(successor, schema, target)
        self.assertFalse(review.blockers)
        self.assertEqual(no_changes.fields, ())
        self.assertEqual(review.preserved_field_count, 1)
        self.assertEqual(changed_record["name"], "Edited in Odoo")
        self.assertEqual(len(target.writes), writes)
        self.assertTrue(
            self.context.corrections.get(successor, actor=self.actor).completed
        )
        self.assertIsNotNone(
            self.context.corrections.get(next_revision, actor=self.actor)
        )
