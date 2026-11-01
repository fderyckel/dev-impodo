"""Drive the accepted hierarchy scenario through the normal local application graph.

REMOTE here names the Odoo transport, not the Impodo Linux deployment profile.
Local persistence callbacks use existing bounded operation scopes. Odoo reads,
writes, and recovery never run inside a retained DuckDB operation.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from time import perf_counter
from uuid import uuid4

from fastapi.testclient import TestClient

from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from impodo.adapters.odoo.local_stack import LocalStackService, probe_local_stack
from impodo.domain.mapping.contracts import TargetFieldDisposition, TargetFieldHandling
from impodo.domain.mapping.create_field_policy import (
    CreateFieldCoverage,
    evaluate_create_field,
)
from impodo.domain.schema.governance import BusinessKeyDefinition, BusinessKeyStatus
from impodo.web.app import create_local_app
from impodo.web.composition.odoo_company import read_company_access
from impodo.web.composition.page_reads import run_local_operation_sync
from impodo.web.composition.target_readers import _read_readiness_snapshots
from impodo.web.routers.schema import (
    _capture_selected_schema_sync,
    _refresh_model_catalog_sync,
)
from impodo.web.target_credentials import (
    TargetCredentialRole,
    get_target_credential,
    store_target_credential,
)
from scripts.hierarchy_qualification_target import (
    MODELS,
    QualificationError,
    require,
    require_runtime,
)
from tests.integration.web.test_fresh_data_controls import field
from tests.support.browser_scenarios import POST_HEADERS, _csrf
from tests.support.hierarchy_assertions import (
    assert_frozen_hierarchy,
    assert_prepared_hierarchy,
)
from tests.support.hierarchy_cases import CATEGORY_ROWS, supplied_files
from tests.support.hierarchy_workflow import (
    PRODUCT_ROWS,
    HierarchyWorkflowFixture,
    product_csv,
)
from tests.support.identity import resolve_local_actor


class InjectedInterruption(RuntimeError):
    """Simulate loss of the client at one named durable/transport boundary."""


class FaultObserver:
    """Observe actual accepted replies and inject once, never replaying a write."""

    def __init__(self, fault):
        self.fault, self.fired = fault, False
        self.accepted_batches = self.accepted_rows = 0

    def transport(self, original):
        def call(url, headers, body, timeout, method):
            write = url.endswith(("/create", "/load"))
            if write and not self.fired and self.fault == "known-rejection":
                self.fired = True
                return 403, {"error": "Injected qualification rejection"}
            status, payload = original(url, headers, body, timeout, method)
            ids = payload.get("ids") if isinstance(payload, dict) else payload
            accepted = (
                write
                and status == 200
                and (
                    type(ids) is int
                    and ids > 0
                    or isinstance(ids, list)
                    and bool(ids)
                    and all(type(item) is int and item > 0 for item in ids)
                )
            )
            if accepted:
                self.accepted_batches += 1
                self.accepted_rows += len(ids) if isinstance(ids, list) else 1
                if not self.fired and self.fault in {"after-commit", "unknown-outcome"}:
                    self.fired = True
                    if self.fault == "unknown-outcome":
                        raise TimeoutError("Injected lost write response")
                    raise InjectedInterruption(
                        "Injected interruption after accepted create"
                    )
            return status, payload

        return call

    def receipt(self, original, boundary):
        def record(*args, **kwargs):
            result = original(*args, **kwargs)
            if not self.fired and self.fault == boundary:
                self.fired = True
                raise InjectedInterruption(
                    "Injected interruption after durable journal boundary"
                )
            return result

        return record


class LiveHierarchyJourney(HierarchyWorkflowFixture):
    """Reuse accepted source and mapping decisions, replacing all fictional Odoo seams."""

    def __init__(self, descriptor, keys, vault, storage):
        super().__init__(methodName="runTest")
        self.descriptor, self.keys, self.secrets, self.storage = (
            descriptor,
            keys,
            vault,
            storage,
        )
        self.metrics = []
        self._open()

    def _open(self):
        descriptor = self.descriptor

        def probe(profile):
            require(
                profile.base_url.rstrip("/") == descriptor.base_url.rstrip("/")
                and profile.database_hint == descriptor.database,
                "LOCAL_PROFILE_TARGET_MISMATCH",
            )
            return probe_local_stack(
                replace(
                    profile,
                    python_path=descriptor.python,
                    odoo_bin_path=descriptor.odoo_bin,
                )
            )

        self.app = create_local_app(
            self.storage,
            launch_token="hierarchy-qualification",
            secret_store=self.secrets,
            local_stack_service=LocalStackService(probe=probe),
        )
        self.context = self.app.state.context
        self.actor = resolve_local_actor(self.context)
        self.client = TestClient(self.app)
        self.assertEqual(
            self.client.get(
                "/launch?token=hierarchy-qualification", follow_redirects=False
            ).status_code,
            303,
        )
        self.csrf = _csrf(self.client.get("/projects").text)

    def close(self):
        self.client.close()
        for name in (
            "load_jobs",
            "preparation_jobs",
            "odoo_capture_jobs",
            "recipe_run_jobs",
            "preflight_jobs",
        ):
            manager = getattr(self.context, name, None)
            if manager is not None:
                manager.shutdown()

    def reopen(self, **target_dependencies):
        require(not target_dependencies, "SCENARIO_INVALID")
        self.close()
        self._open()

    def bind_local(self, workspace_id):
        if self.descriptor.mode == "LOCAL":
            status = self.context.local_stack.select_config(
                workspace_id, self.descriptor.local_config
            )
            require(status.profile is not None, "LOCAL_PROFILE_UNAVAILABLE")

    def schema(self, workspace_id, *, require_api_identity=False, **_unused):
        context, actor, descriptor = self.context, self.actor, self.descriptor
        state = run_local_operation_sync(
            lambda: context.workspace_states.update_target(
                workspace_id,
                actor=actor,
                expected_revision=context.queries.get(workspace_id).revision,
                odoo_connection_mode=descriptor.mode,
                odoo_base_url=descriptor.base_url,
                odoo_database=descriptor.database,
                intended_applications=(),
                intended_models=MODELS,
            )
        )
        self.bind_local(workspace_id)
        owner = run_local_operation_sync(
            context.target_credential_workspace,
            workspace_id,
            workspace_state=state,
            actor=actor,
        )
        # Integrated Recipe activation requires a verified API reader binding.
        # Capture that identity through normal composition even for a LOCAL
        # Recipe target; comparison still selects the native local reader.
        if descriptor.mode == "REMOTE" or require_api_identity:
            store_target_credential(
                self.secrets,
                owner,
                TargetCredentialRole.READ,
                self.keys[0],
                persistent=True,
            )
        if descriptor.mode == "REMOTE":
            access = read_company_access(state, self.keys[0])
        else:
            profile = context.local_stack.get(workspace_id).profile
            access = context.local_odoo_reader.get_company_access(state, profile)
        choices = [
            choice
            for choice in access.choices
            if choice.name == access.default_company_name
        ]
        require(len(choices) == 1, "COMPANY_SCOPE_AMBIGUOUS")
        state = run_local_operation_sync(
            context.workspace_states.select_odoo_company,
            workspace_id,
            actor=actor,
            expected_revision=state.revision,
            selection=choices[0],
        )
        _refresh_model_catalog_sync(context, state, actor=actor)
        schema = _capture_selected_schema_sync(context, state, actor=actor)
        require_runtime(schema.odoo_version, descriptor.major)
        run_local_operation_sync(
            context.schema_workspace.govern,
            workspace_id,
            actor=actor,
            business_keys=(
                BusinessKeyDefinition(
                    "category:name-parent",
                    "product.category",
                    ("name",),
                    scope_fields=("parent_id",),
                    status=BusinessKeyStatus.CONFIRMED,
                ),
                BusinessKeyDefinition(
                    "product:reference",
                    "product.template",
                    ("default_code",),
                    status=BusinessKeyStatus.CONFIRMED,
                ),
            ),
        )
        return schema

    def submit(self, workspace_id, mappings, **kwargs):
        schema = run_local_operation_sync(
            self.context.queries.get_odoo_schema_catalog, workspace_id
        )
        models = {model.name: model for model in schema.models}
        completed = []
        for mapping in mappings:
            provided = {
                item.target_field for item in (*mapping.fields, *mapping.relationships)
            }
            provided.update(
                field
                for item in (*mapping.target_identity, *mapping.target_scope)
                for field in item.target_fields
            )
            defaults = tuple(
                TargetFieldDisposition(field.name, TargetFieldHandling.ODOO_DEFAULT)
                for field in models[mapping.target_model].fields
                if evaluate_create_field(
                    field,
                    provided=field.name in provided,
                    handling=None,
                    target_model=mapping.target_model,
                    odoo_version=schema.odoo_version,
                ).coverage
                is CreateFieldCoverage.DEFAULT_AVAILABLE
            )
            completed.append(
                replace(
                    mapping,
                    target_field_dispositions=(
                        *mapping.target_field_dispositions,
                        *defaults,
                    ),
                    relationships=tuple(
                        replace(
                            item,
                            required_on_create=item.required_on_create
                            or any(
                                field.name == item.target_field and field.required
                                for field in models[mapping.target_model].fields
                            ),
                        )
                        for item in mapping.relationships
                    ),
                )
            )
        return run_local_operation_sync(
            super().submit, workspace_id, completed, **kwargs
        )

    def prepare(self, workspace_id):
        return run_local_operation_sync(super().prepare, workspace_id)

    def approve(self, workspace_id):
        return run_local_operation_sync(super().approve, workspace_id)

    def compare(self, workspace_id):
        state = run_local_operation_sync(self.context.queries.get, workspace_id)
        started = perf_counter()
        with collect_duckdb_request_timings() as timings:
            report = self.context.preflight.compare(
                workspace_id,
                reader=lambda request: _read_readiness_snapshots(
                    self.context, state, request, actor=self.actor
                ),
                actor=self.actor,
                local_operation=run_local_operation_sync,
            )
        self.metrics.append(
            {
                "phase": "comparison",
                "elapsed_ms": round((perf_counter() - started) * 1000, 3),
                "connect_ms": round(timings.connect_ms, 3),
                "schema_ms": round(timings.schema_ms, 3),
                "lock_wait_ms": round(timings.lock_wait_ms, 3),
                "connections": timings.connection_count,
                "schema_checks": timings.schema_check_count,
                "lock_retries": timings.lock_retry_count,
            }
        )
        return report

    def prepare_review(self, workspace_id):
        staging, quality = self.prepare(workspace_id)
        run_local_operation_sync(
            assert_prepared_hierarchy, self, workspace_id, staging, quality
        )
        self.approve(workspace_id)
        self.expect_business_writes(workspace_id)
        report = self.compare(workspace_id)
        self.assertEqual(
            (report.status, report.create_count, report.unchanged_count),
            ("READY", 13, 5),
        )
        preview = run_local_operation_sync(
            self.context.execution.current_preview, workspace_id, actor=self.actor
        )
        self.assertTrue(preview.can_load, preview.scope_error)
        assert_frozen_hierarchy(self, preview.snapshot)
        return preview

    def ports(self, workspace_id, preview):
        state = run_local_operation_sync(self.context.queries.get, workspace_id)
        owner = run_local_operation_sync(
            self.context.target_credential_workspace,
            workspace_id,
            workspace_state=state,
            actor=self.actor,
        )
        credential = get_target_credential(
            self.secrets, owner, TargetCredentialRole.WRITE
        )
        if credential is None:
            credential = store_target_credential(
                self.secrets,
                owner,
                TargetCredentialRole.WRITE,
                self.keys[1],
                persistent=True,
            )
        write = (
            self.context.write_identity_probe(
                state, credential.secret, preview.api_scope
            )
            if self.descriptor.mode == "REMOTE"
            else None
        )
        read = (
            self.context.read_identity_probe(
                state, self.keys[0], preview.snapshot.readable_models
            )
            if self.descriptor.mode == "REMOTE"
            else None
        )
        writer = self.context.write_executor_factory(
            state, credential.secret, preview.api_scope
        )
        reader = self.context.readback_reader_factory(
            state, credential.secret, preview.api_scope
        )
        return (
            writer,
            reader,
            {
                "read_identity": read,
                "read_credential_binding_hash": preview.snapshot.read_credential_binding_hash
                if read
                else "",
                "write_identity": write,
                "write_credential_binding_hash": credential.binding_hash
                if write
                else "",
            },
        )

    def load(self, workspace_id, preview, observer):
        writer, reader, identities = self.ports(workspace_id, preview)
        writer.transport = observer.transport(writer.transport)
        journal = self.context.execution.journal
        method = (
            "record_batch_started"
            if observer.fault == "before-dispatch"
            else "record_outcomes"
        )
        original = getattr(journal, method)
        boundary = (
            "before-dispatch" if method == "record_batch_started" else "after-receipt"
        )
        setattr(journal, method, observer.receipt(original, boundary))
        try:
            run = self.context.execution.execute(
                workspace_id,
                expected_snapshot_hash=preview.snapshot.semantic_hash,
                executor=writer,
                actor=self.actor,
                **identities,
            )
        except InjectedInterruption:
            run = run_local_operation_sync(journal.get_current_run, workspace_id)
            if run is None or run.status.value != "RUNNING":
                raise QualificationError("INTERRUPTION_NOT_DURABLE")
            # Proof comes from independent read-back of actual target state, not
            # from the accepted reply counter or the injected exception.
            write = {
                key: value
                for key, value in identities.items()
                if key.startswith("write_")
            }
            assessment = self.context.reconciliation.assess_recovery(
                workspace_id,
                expected_execution_run_id=run.run_id,
                reader=reader,
                actor=self.actor,
                targeted=True,
                **write,
            )
            self.assertEqual(
                run_local_operation_sync(journal.get_current_run, workspace_id), run
            )
            self.reopen()
            self.bind_local(workspace_id)
            restored = run_local_operation_sync(
                self.context.execution.journal.get_current_run, workspace_id
            )
            self.assertEqual(restored, run)
            writer, reader, identities = self.ports(workspace_id, preview)
            write = {
                key: value
                for key, value in identities.items()
                if key.startswith("write_")
            }
            self.context.execution.review_recovery(
                workspace_id,
                expected_execution_run_id=run.run_id,
                recovery=assessment,
                actor=self.actor,
                **write,
            )
            run = self.context.execution.resume(
                workspace_id,
                expected_execution_run_id=run.run_id,
                recovery=assessment,
                executor=writer,
                actor=self.actor,
                **write,
            )
        finally:
            setattr(journal, method, original)
        require(
            run.status.value == "COMPLETED" and run.committed_count == 13,
            "LOAD_INCOMPLETE",
        )
        write = {
            key: value for key, value in identities.items() if key.startswith("write_")
        }
        result = self.context.reconciliation.reconcile(
            workspace_id,
            expected_execution_run_id=run.run_id,
            reader=reader,
            actor=self.actor,
            **write,
        )
        self.assertEqual((result.status.value, result.verified_count), ("VERIFIED", 18))
        return run

    def publish_recipe(self, workspace_id):
        owner = run_local_operation_sync(
            self.context.migration_workspaces.get, workspace_id, actor=self.actor
        )
        publication = run_local_operation_sync(
            self.context.recipe_publication.publish,
            project_id=owner.project_id,
            data_version_id=owner.data_version_id,
            workspace_id=workspace_id,
            display_name="Hierarchy qualification",
            business_purpose="Complete paths and Product links",
            actor=self.actor,
        )
        return owner.project_id, publication

    def replay_recipe(self, project_id, publication, source_shape):
        context, actor = self.context, self.actor
        setup = run_local_operation_sync(
            lambda: context.test_runs.start_setup(
                project_id,
                expected_workspace_revision=context.migration_projects.get(
                    project_id, actor=actor
                ).optimistic_revision,
                recipe_revisions=(
                    (publication.recipe.recipe_id, publication.revision.version),
                ),
                dependencies=(),
                label="Reordered hierarchy",
                export_as_of=datetime.now(UTC).date().isoformat(),
                operation_id=str(uuid4()),
                actor=actor,
            )
        )
        url = (
            f"/projects/{project_id}/test-runs/{setup.run.migration_run_id}/fresh-data"
        )
        rows = tuple(reversed(PRODUCT_ROWS))
        files = (
            supplied_files(rows, categories=tuple(reversed(CATEGORY_ROWS)))
            if source_shape == "supplied"
            else {"products": product_csv(rows)}
        )
        page = self.client.get(url)
        response = self.client.post(
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
        self.assertEqual(response.status_code, 303)
        page = self.client.get(url)
        self.assertEqual(
            self.client.post(
                url + "/register",
                headers=POST_HEADERS,
                follow_redirects=False,
                data={
                    "csrf_token": field(page, "csrf_token"),
                    "revision": field(page, "revision"),
                },
            ).status_code,
            303,
        )
        page = self.client.get(url)
        self.assertEqual(
            self.client.post(
                url + "/accept",
                headers=POST_HEADERS,
                follow_redirects=False,
                data={
                    "csrf_token": field(page, "csrf_token"),
                    "parameter_revision": "",
                    "warnings_acknowledged": "1",
                },
            ).status_code,
            303,
        )
        schema = self.schema(
            setup.setup_workspace.workspace_id, require_api_identity=True
        )
        activated = run_local_operation_sync(
            lambda: context.test_runs.activate(
                project_id,
                setup.run.migration_run_id,
                expected_workspace_revision=context.migration_projects.get(
                    project_id, actor=actor
                ).optimistic_revision,
                target_schema=schema,
                target_reference_bundle=None,
                credential_generation=schema.read_credential_binding_hash,
                operation_id=str(uuid4()),
                actor=actor,
            )
        )
        application = activated.applications[0]
        self.assertEqual(application.status.value, "READY")
        self.bind_local(application.workspace_id)
        return application.workspace_id
