"""Reusable real-store mixed-write load worker for structural comparisons."""

import json
from contextlib import contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import patch

import duckdb

from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from impodo.adapters.duckdb.unit_of_work import (
    DuckDbConnectionFactory,
    _read_databases,
)
from impodo.adapters.odoo.connectors import Json2Config
from impodo.adapters.odoo.readback import Json2ReadbackReader
from impodo.adapters.odoo.writer import Json2WriteExecutor
from impodo.application.workspace.access import bind_workspace_access_context
from impodo.application.workspace.execution.load_observation import (
    LoadObservation,
    bind_load_observation,
    observe_load_stage,
)
from impodo.domain.serialization import content_hash
from impodo.domain.shared.models import OdooReadIdentity, OdooWriteIdentity
from impodo.web.load_performance import collect_load_stage_metrics
from impodo.web.target_credentials import TargetCredentialRole, store_target_credential
from tests.support.database_probe import count_duckdb_statements
from tests.support.hierarchy_odoo import HierarchyOdoo


class MixedHierarchyOdoo(HierarchyOdoo):
    """Seed one existing product and execute real JSON-2 updates as well as creates."""

    def __init__(self, schema):
        super().__init__(schema)
        self.records[("product.template", 501)] = {
            "default_code": "PX101",
            "name": "Old",
            "categ_id": 104,
        }
        self.updates = []
        self.remote_entry = lambda *_: None

    def update_row(self, model, identifier, values):
        self.remote_entry("update_row", model)
        assert (model, identifier) in self.records
        for field, value in values.items():
            relation = self.relations.get((model, field))
            if relation and value not in (False, None):
                assert (relation, int(value)) in self.records
        self.records[(model, identifier)].update(values)
        self.updates.append((model, identifier, dict(values)))
        self.writes.append((model, (dict(values),), (identifier,)))

    def transport(self, url, headers, body, timeout, method):
        model, operation = url.split("/json/2/", 1)[1].rsplit("/", 1)
        self.remote_entry(operation, model)
        if operation == "write":
            assert method == "POST"
            payload = json.loads(body)
            self.wire_calls.append((model, operation))
            for identifier in payload["ids"]:
                self.update_row(model, identifier, payload["vals"])
            return 200, True
        return super().transport(url, headers, body, timeout, method)


def worker_fixture(fixture, *, major=19, mode="LOCAL"):
    """Use identical generated source bytes and mapping for every comparison."""

    workspace_id, _ = fixture.hierarchy(major=major, mode=mode)
    fixture.prepare(workspace_id)
    fixture.approve(workspace_id)
    fixture.expect_business_writes(workspace_id)
    context, actor = fixture.context, fixture.actor
    target = MixedHierarchyOdoo(context.queries.get_odoo_schema_catalog(workspace_id))
    report = context.preflight.compare(workspace_id, reader=target.compare, actor=actor)
    fixture.assertEqual(report.status, "READY", report)
    fixture.assertEqual(
        (report.create_count, report.update_count, report.unchanged_count), (12, 1, 5)
    )
    preview = context.execution.current_preview(workspace_id, actor=actor)
    fixture.assertIsNotNone(preview)
    fixture.assertTrue(preview.can_load, preview.scope_error)
    snapshot = preview.snapshot
    target.scope_hash = preview.api_scope.semantic_hash
    observed = datetime.now(UTC).isoformat()
    read = OdooReadIdentity(
        snapshot.target_hash,
        snapshot.read_principal_hash,
        snapshot.read_permission_hash,
        snapshot.read_context_hash,
        snapshot.readable_models,
        observed,
    )
    write = OdooWriteIdentity(
        target_hash=snapshot.target_hash,
        principal_hash=content_hash("worker writer"),
        permission_hash=content_hash("worker write permission"),
        context_hash=snapshot.read_context_hash,
        readable_models=snapshot.readable_models,
        writable_models=snapshot.readable_models,
        observed_at=observed,
    )
    state = context.queries.get(workspace_id)
    owner = context.target_credential_workspace(
        workspace_id, workspace_state=state, actor=actor
    )
    credential = store_target_credential(
        context.secret_store,
        owner,
        TargetCredentialRole.WRITE,
        "fictional-write-key",
        persistent=False,
    )
    return SimpleNamespace(
        workspace_id=workspace_id,
        preview=preview,
        snapshot=snapshot,
        target=target,
        state=state,
        identities={
            "read_identity": read,
            "read_credential_binding_hash": snapshot.read_credential_binding_hash,
            "write_identity": write if mode == "REMOTE" else None,
            "write_credential_binding_hash": credential.binding_hash
            if mode == "REMOTE"
            else "",
        },
        form={
            "csrf_token": fixture.csrf,
            "snapshot_hash": snapshot.semantic_hash,
            "intent_assessment_hash": preview.business_intent_assessment_hash,
            "batch_rows": "10",
        },
    )


def queue_worker(fixture, packet):
    """Capture the actual governed route closure without starting a worker thread."""

    with patch.object(
        fixture.context.load_jobs,
        "enqueue",
        return_value=SimpleNamespace(job_id="isolated-worker"),
    ) as enqueue:
        response = fixture._post(f"/workspaces/{packet.workspace_id}/load", packet.form)
    fixture.assertEqual(response.status_code, 303, response.text)
    enqueue.assert_called_once()
    fixture.assertTrue(
        response.headers["location"].endswith("/load/progress/isolated-worker")
    )
    return SimpleNamespace(**enqueue.call_args.kwargs)


@contextmanager
def worker_boundaries(fixture, packet):
    """Observe committed receipts and assert physical owners close before all RPCs."""

    owners, ledger, committed = [], [], set()
    existing = set(packet.target.records)
    prior_before_row = packet.target.before_row
    connect = DuckDbConnectionFactory._connect_with_lock_wait
    journal = fixture.context.execution.journal

    def tracked_connect(factory, *args, **kwargs):
        owner = connect(factory, *args, **kwargs)
        owners.append(owner)
        return owner

    def remote_entry(operation, model=""):
        fixture.assertIsNone(getattr(_read_databases, "connections", None))
        for owner in owners:
            with fixture.assertRaises(duckdb.ConnectionException):
                owner.execute("SELECT 1")
        ledger.append(("remote", operation, model))

    def receipt(method, label):
        original = getattr(journal, method)

        def record(workspace_id, run_id, rows, *args, **kwargs):
            result = original(workspace_id, run_id, rows, *args, **kwargs)
            ledger.append((label, run_id, tuple(rows)))
            committed.update(
                (row.target_model, row.odoo_id)
                for row in rows
                if row.status.value == "COMMITTED"
            )
            return result

        return record

    def before_row(model, values):
        for field, value in values.items():
            relation = packet.target.relations.get((model, field))
            if relation and value not in (False, None):
                identity = (relation, int(value))
                fixture.assertIn(
                    identity,
                    existing | committed,
                    "A dependent write preceded its parent's durable receipt",
                )
        if prior_before_row is not None:
            prior_before_row(model, values)

    packet.target.remote_entry = remote_entry
    packet.target.before_row = before_row
    try:
        with (
            patch.object(
                DuckDbConnectionFactory, "_connect_with_lock_wait", tracked_connect
            ),
            patch.object(
                journal,
                "record_batch_started",
                side_effect=receipt("record_batch_started", "before"),
            ),
            patch.object(
                journal,
                "record_outcomes",
                side_effect=receipt("record_outcomes", "after"),
            ),
            patch.object(
                journal,
                "record_outcomes_with_detail",
                side_effect=receipt("record_outcomes_with_detail", "after"),
            ),
        ):
            yield SimpleNamespace(remote_entry=remote_entry, ledger=ledger)
    finally:
        try:
            remote_entry("worker_finished")
        finally:
            packet.target.remote_entry = lambda *_: None
            packet.target.before_row = prior_before_row


@contextmanager
def target_dependencies(fixture, packet, boundary):
    """Inject only network boundaries; retain production execution and readback."""

    def identity(kind, *_):
        boundary.remote_entry(kind)
        return packet.identities[kind]

    def adapter(adapter_type, state, key, scope):
        boundary.remote_entry(adapter_type.__name__)
        fixture.assertEqual(scope.semantic_hash, packet.preview.api_scope.semantic_hash)
        config = Json2Config(
            state.odoo_base_url,
            state.odoo_database,
            key,
            connection_mode=state.odoo_connection_mode.value,
            retries=0,
        )
        return adapter_type(config, scope, transport=packet.target.transport)

    with (
        patch.object(
            fixture.context,
            "read_identity_probe",
            side_effect=lambda *args: identity("read_identity", *args),
        ),
        patch.object(
            fixture.context,
            "write_identity_probe",
            side_effect=lambda *args: identity("write_identity", *args),
        ),
        patch.object(
            fixture.context,
            "write_executor_factory",
            side_effect=lambda *args: adapter(Json2WriteExecutor, *args),
        ),
        patch.object(
            fixture.context,
            "readback_reader_factory",
            side_effect=lambda *args: adapter(Json2ReadbackReader, *args),
        ),
    ):
        yield


def measure_worker(fixture, packet, *, queued=None):
    """Run the real queued job, or direct execute, with separate structural costs."""

    events, progress = [], []
    with count_duckdb_statements() as statements:

        @contextmanager
        def stage_metrics(sample_memory=True):
            before = len(statements)
            with collect_load_stage_metrics(sample_memory) as metrics:
                yield metrics
            metrics["sql_statement_count"] = len(statements) - before

        observer = LoadObservation(
            "synthetic-worker",
            lambda event, facts: events.append((event, facts)),
            stage_metrics=stage_metrics,
        )
        with (
            collect_duckdb_request_timings() as timings,
            worker_boundaries(fixture, packet) as boundary,
            target_dependencies(fixture, packet, boundary),
            bind_load_observation(observer),
        ):
            if queued is None:
                writer = fixture.context.write_executor_factory(
                    packet.state, "fictional-write-key", packet.preview.api_scope
                )
                with observe_load_stage("direct_execute"):
                    result = fixture.context.execution.execute(
                        packet.workspace_id,
                        expected_snapshot_hash=packet.snapshot.semantic_hash,
                        executor=writer,
                        actor=fixture.actor,
                        progress=progress.append,
                        **packet.identities,
                    )
            else:
                with bind_workspace_access_context(queued.access_context):
                    result = queued.work(
                        queued.access_context, progress.append, lambda: None
                    )
    return SimpleNamespace(
        result=result,
        timings=timings,
        sql_count=len(statements),
        local_phases=observer.local_phases,
        events=events,
        progress=progress,
        ledger=boundary.ledger,
    )
