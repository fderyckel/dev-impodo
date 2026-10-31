"""Qualify real row-filtered transfers between disposable local and remote Odoo 20.

Run as a module from the checkout. Explicit --allow-write authorizes unique
synthetic Contacts only. Every case uses real capture, matching, review,
preflight, journalled load, and read-back. Interrupted cases stop after the
first accepted Model.load reply, reopen application storage with the same
private vault, and recover the original journal through the HTTP action.
Receipts contain no credentials, business values, database names, or Odoo IDs.
"""

import argparse
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile, TemporaryDirectory
from uuid import uuid4

from fastapi.testclient import TestClient

from impodo.adapters.odoo.connectors import Json2Config, Json2ReadConnector
from impodo.adapters.odoo.writer import Json2WriteExecutor
from impodo.adapters.protected_evidence.credential_vault import MemorySecretStore
from impodo.application.destination_matching_service import (
    DestinationMatchingService,
    DestinationMatchKeyChoice,
)
from impodo.application.shared.build_contract import PROCESS_BUILD_CONTRACT
from impodo.domain.execution.odoo_scope import OdooApiScope, OdooModelScope
from impodo.domain.odoo.contracts import ConnectorError, RecordRequest
from impodo.domain.odoo_source_scope import (
    propose_related_odoo_data,
    related_model_can_be_selected,
)
from impodo.domain.shared.models import canonical_json_bytes
from impodo.domain.workspace.errors import WorkspaceError
from impodo.domain.workspace.workbench import transfer_destination_workspace
from impodo.web.app import create_local_app
from impodo.web.composition.odoo_company import read_company_access
from impodo.web.target_credentials import TargetCredentialRole, get_target_credential
from tests.support.browser_scenarios import (
    ProjectWorkspaceBuilder,
    _csrf,
    _wait_for_load,
    _wait_for_odoo_capture,
)
from tests.support.identity import resolve_local_actor

FIELDS = ("city", "email", "name", "ref")
SEED_SCOPE = OdooApiScope(
    preview_hash="sha256:" + "7" * 64,
    models=(
        OdooModelScope(
            "res.partner",
            write_fields=FIELDS,
            read_fields=FIELDS,
            lookup_fields=("ref",),
        ),
    ),
)
CASES = (
    "local-to-remote-normal",
    "local-to-remote-interrupted",
    "remote-to-local-normal",
    "remote-to-local-interrupted",
)


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


@dataclass(frozen=True)
class Connection:
    url: str
    database: str
    mode: str
    key: str = field(repr=False)

    def config(self):
        return Json2Config(
            self.url,
            self.database,
            self.key,
            connection_mode=self.mode,
            retries=0,
            context={
                "mail_create_nosubscribe": True,
                "mail_notrack": True,
                "tracking_disable": True,
            },
        )

    def records(self, refs):
        return (
            Json2ReadConnector(self.config())
            .get_records(
                (
                    RecordRequest(
                        "res.partner",
                        FIELDS,
                        domain=(("ref", "in", tuple(refs)),),
                        limit=20,
                    ),
                )
            )
            .records["res.partner"]
        )


def cleanup(connection, refs):
    """Remove only exact prechecked synthetic references; verify absence."""

    for attempt in range(3):
        try:
            _cleanup_once(connection, refs)
            return
        except (ConnectorError, OSError):
            # Resolve the exact namespace again before any further delete.
            # A lost unlink reply may already have removed every own record.
            if attempt == 2:
                raise


def _cleanup_once(connection, refs):
    records = connection.records(refs)
    require(
        len(records) < 20 and all(row.values["ref"] in refs for row in records),
        "Cleanup namespace is inconsistent",
    )
    if records:
        config = connection.config()
        writer = Json2WriteExecutor(config, SEED_SCOPE)
        status, result = writer.transport(
            config.base_url + "/json/2/res.partner/unlink",
            {
                "Authorization": "bearer " + config.api_key,
                "Content-Type": "application/json",
                "X-Odoo-Database": config.database,
            },
            canonical_json_bytes(
                {
                    "ids": [row.odoo_id for row in records],
                    "context": dict(config.context),
                }
            ),
            config.timeout_seconds,
            "POST",
        )
        require(status == 200 and result is True, "Synthetic cleanup failed")
    require(not connection.records(refs), "Synthetic cleanup absence check failed")


class InjectedInterruption(RuntimeError):
    """Stop client processing after a real accepted write, before journal receipt."""


def observe_loads(context, counters, *, interrupt):
    """Instrument real transport without changing requests, receipts, or retries."""

    factory = context.write_executor_factory

    def executor_factory(*args):
        executor = factory(*args)
        transport = executor.transport

        def observed(url, headers, body, timeout, method):
            status, payload = transport(url, headers, body, timeout, method)
            if (
                url.endswith("/res.partner/load")
                and status == 200
                and isinstance(payload, dict)
                and payload.get("ids")
            ):
                counters["accepted_batches"] += 1
                counters["accepted_rows"] += len(payload["ids"])
                if interrupt and not counters["interrupted"]:
                    counters["interrupted"] = True
                    raise InjectedInterruption(
                        "Controlled client interruption after accepted create"
                    )
            return status, payload

        executor.transport = observed
        return executor

    context.write_executor_factory = executor_factory


def stop_context(context):
    for name in (
        "load_jobs",
        "odoo_capture_jobs",
        "preparation_jobs",
        "recipe_run_jobs",
    ):
        manager = getattr(context, name, None)
        if manager is not None:
            manager.shutdown()


def run_case(source, destination, case, storage):
    """Run one unique source population; never reuse a failed write namespace."""

    marker = "impodo-row-transfer-" + uuid4().hex
    refs = tuple(marker + suffix for suffix in ("-DROP", "-KEEP1", "-KEEP2"))
    source_values = tuple(
        {
            "city": marker,
            "email": marker + ("-drop" if i == 0 else "-keep") + "@example.invalid",
            "name": f"Fictional transfer Contact {i}",
            "ref": ref,
        }
        for i, ref in enumerate(refs)
    )
    require(
        not source.records(refs) and not destination.records(refs),
        "Synthetic namespace already exists",
    )
    with NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        prefix="impodo-transfer-cleanup-",
        suffix=".json",
        delete=False,
    ) as manifest:
        json.dump({"case": case, "references": refs}, manifest)
    cleanup_manifest = Path(manifest.name)
    evidence = {
        "case": case,
        "status": "RUNNING",
        "source_rows": 3,
        "included": 2,
        "excluded": 1,
    }
    counters = {"accepted_batches": 0, "accepted_rows": 0, "interrupted": False}
    vault, token = MemorySecretStore(), uuid4().hex
    client = context = None
    phase = "seed"

    def reopen():
        app = create_local_app(
            storage, secret_store=vault, launch_token=token, session_secret=token
        )
        client = TestClient(app)
        require(
            client.get("/launch?token=" + token, follow_redirects=False).status_code
            == 303,
            "Local authentication failed",
        )
        return client, app.state.context

    def post(path, data, *, expected=303):
        csrf = _csrf(client.get("/projects").text)
        response = client.post(
            path,
            data={"csrf_token": csrf, **data},
            headers={"Origin": "http://testserver"},
            follow_redirects=False,
        )
        require(
            response.status_code == expected,
            f"{phase} returned HTTP {response.status_code}",
        )
        return response

    try:
        Json2WriteExecutor(source.config(), SEED_SCOPE).create_rows(
            "res.partner", source_values
        )
        client, context = reopen()
        actor = resolve_local_actor(context)
        state = ProjectWorkspaceBuilder(context).create(
            name="Row inclusion transfer qualification",
            source_system="Synthetic Odoo 20",
            source_mode="ODOO",
        )
        state = context.workspace_states.update_target(
            state.workspace_id,
            actor=actor,
            expected_revision=state.revision,
            odoo_connection_mode=source.mode,
            odoo_base_url=source.url,
            odoo_database=source.database,
            intended_applications=(),
            intended_models=("res.partner",),
        )
        state = context.workspace_states.register(
            state.workspace_id, actor=actor, expected_revision=state.revision
        )
        workspace_id, base = state.workspace_id, f"/workspaces/{state.workspace_id}"
        phase = "source_schema"
        print(case + ": capturing live source", flush=True)
        post(base + "/sources/odoo-read-credential", {"read_api_key": source.key})
        post(base + "/schema/models/refresh", {})
        post(base + "/schema/capture", {"return_to_sources": "1"})
        phase = "relationship_review"
        source_schema = context.queries.get_odoo_schema_catalog(workspace_id)
        related = propose_related_odoo_data(source_schema.models, include_selected=True)
        state = context.queries.get(workspace_id)
        post(
            base + "/sources/odoo-related-data",
            {
                "revision": str(state.revision),
                "related_actions": [
                    name + "::DO_NOT_CAPTURE"
                    for name in sorted(
                        {
                            item.relation_model
                            for item in related
                            if related_model_can_be_selected(item.handling)
                        }
                    )
                ],
            },
        )
        phase = "source_selection"
        post(
            base + "/sources/odoo-selection",
            {
                "dataset_name": "synthetic_contacts",
                "model": "res.partner",
                "field_names": list(FIELDS),
                "page_size": "10",
                "filter_field": "city",
                "filter_value": marker,
                "include_archived": "1",
            },
        )
        selections = context.queries.get_current_odoo_capture_selections(workspace_id)
        require(len(selections) == 1, "Source selection is incomplete")
        choice = {
            "selection_id": selections[0].selection_id,
            "selection_hash": selections[0].content_hash,
        }
        post(base + "/sources/odoo-assessment", choice, expected=200)
        phase = "source_capture"
        started = post(
            base + "/sources/odoo-capture", {**choice, "confirm_capture": "1"}
        )
        job = _wait_for_odoo_capture(client, started.headers["location"], timeout=120)
        require(job.get("status") == "SUCCEEDED", "Source capture failed")
        selection = context.queries.get_source_selection(workspace_id)
        schema = context.queries.get_odoo_schema_catalog(workspace_id)
        require(
            selection is not None
            and schema is not None
            and selection.datasets[0].row_count == 3,
            "Frozen source count changed",
        )
        dataset = selection.datasets[0]
        keys = {column.source_name: column.stable_key for column in dataset.columns}

        phase = "destination_connection"

        def connect():
            state = context.queries.get(workspace_id)
            post(
                base + "/transfer-destination",
                {
                    "revision": str(state.revision),
                    "odoo_connection_mode": destination.mode,
                    "odoo_base_url": destination.url,
                    "odoo_database": destination.database,
                    "read_api_key": destination.key,
                    "read_api_key_storage": "session",
                },
            )

        connect()
        state = context.queries.get(workspace_id)
        access = read_company_access(
            transfer_destination_workspace(state), destination.key
        )
        companies = tuple(
            item for item in access.choices if item.name == access.default_company_name
        )
        require(len(companies) == 1, "Destination current company is ambiguous")
        post(
            base + "/transfer-destination/company",
            {
                "revision": str(state.revision),
                "company_id": str(companies[0].company_id),
            },
        )
        connect()
        phase = "row_matching"
        print(case + ": checking and confirming selected rows", flush=True)
        state = context.queries.get(workspace_id)
        post(
            base + "/destination-matching",
            {
                "revision": str(state.revision),
                "match_key": dataset.dataset_id + "::" + keys["ref"],
                "destination_handling": dataset.dataset_id + "::transfer",
                "row_inclusion_mode_0": "matching_rows",
                "row_inclusion_join_0": "all",
                "row_inclusion_condition_id_0_0": str(uuid4()),
                "row_inclusion_source_0_0": keys["email"],
                "row_inclusion_operator_0_0": "equals",
                "row_inclusion_value_0_0": source_values[1]["email"],
                "row_inclusion_type_0_0": "string",
            },
        )
        state = context.queries.get(workspace_id)
        require(state.destination_match_plan is not None, "Matching plan was not saved")
        pending = state.destination_match_plan.pending_create_field_decisions
        if pending:
            post(
                base + "/destination-matching/defaults",
                {
                    "revision": str(state.revision),
                    "match_plan_hash": state.destination_match_plan.content_hash,
                    "default_field": [
                        item.model + "::" + item.field_name for item in pending
                    ],
                },
            )
            state = context.queries.get(workspace_id)
        require(
            len(
                state.destination_match_plan.model_matches[
                    0
                ].rule_excluded_source_row_numbers
            )
            == 1,
            "Rule did not exclude one row",
        )
        post(
            base + "/destination-matching/rows-to-use/confirm",
            {
                "revision": str(state.revision),
                "match_plan_hash": state.destination_match_plan.content_hash,
                "confirmation": "confirm",
            },
        )
        state = context.queries.get(workspace_id)
        require(
            state.destination_match_plan.ready,
            "Confirmed destination matching is blocked",
        )
        evidence["reviewed_defaults"] = len(pending)
        phase = "review"
        post(base + "/transfer-order", {"revision": str(state.revision)})
        state = context.queries.get(workspace_id)
        post(
            base + "/transfer-review/build",
            {
                "revision": str(state.revision),
                "policy_" + dataset.dataset_id: "create_if_missing",
            },
        )
        state = context.queries.get(workspace_id)
        post(
            base + "/transfer-review/approve",
            {
                "revision": str(state.revision),
                "confirmation": "approve",
                "reason": "Disposable row inclusion acceptance",
            },
        )
        phase = "preflight"
        state = context.queries.get(workspace_id)
        post(base + "/transfer-preflight", {"revision": str(state.revision)})
        state = context.queries.get(workspace_id)
        require(
            state.transfer_preflight_report.ready, "Fresh transfer preflight is blocked"
        )
        phase = "prepare_load"
        post(
            base + "/transfer-load/prepare",
            {
                "revision": str(state.revision),
                "preflight_hash": state.transfer_preflight_report.content_hash,
            },
        )
        state = context.queries.get(workspace_id)
        snapshot = context.transfer_execution.current_snapshot(state, selection, schema)
        require(
            snapshot is not None
            and snapshot.write_count == 2
            and len(snapshot.rows) == 2,
            "Execution snapshot includes an excluded row",
        )
        snapshot_hash = snapshot.semantic_hash
        phase = "load"
        interrupt = case.endswith("interrupted")
        observe_loads(context, counters, interrupt=interrupt)
        print(case + ": loading through the normal journal", flush=True)
        for attempt in range(3):
            started = post(
                base + "/transfer-load",
                {
                    "revision": str(state.revision),
                    "snapshot_hash": snapshot_hash,
                    "preflight_hash": state.transfer_preflight_report.content_hash,
                    "batch_rows": "1",
                },
            )
            job = _wait_for_load(client, started.headers["location"], timeout=180)
            if (
                job["status"] != "FAILED"
                or counters["interrupted"]
                or context.execution.current_transfer_run(workspace_id) is not None
                or attempt == 2
            ):
                break
            require(
                not destination.records(refs),
                "Unjournalled destination changes prevent retry",
            )
            current = context.transfer_execution.current_snapshot(
                context.queries.get(workspace_id), selection, schema
            )
            require(
                current is not None and current.semantic_hash == snapshot_hash,
                "Pre-write retry snapshot changed",
            )
            evidence["prewrite_probe_retries"] = attempt + 1
            print(
                case
                + ": retrying a failed pre-write probe; no journal or destination records",
                flush=True,
            )
        if interrupt:
            phase = "interruption"
            require(
                counters["interrupted"] and job["status"] == "FAILED",
                "Controlled interruption did not stop loading",
            )
            run = context.execution.current_transfer_run(workspace_id)
            require(
                run is not None
                and run.status.value == "RUNNING"
                and run.in_flight_count == 1,
                "Interrupted journal is not recoverable",
            )
            require(
                len(destination.records(refs)) == 1
                and not destination.records(refs[:1]),
                "Interrupted population is incorrect",
            )
            run_id = run.run_id
            client.close()
            stop_context(context)
            client, context = reopen()
            observe_loads(context, counters, interrupt=False)
            state = context.queries.get(workspace_id)
            restored = context.transfer_execution.current_snapshot(
                state, selection, schema
            )
            require(
                restored is not None and restored.semantic_hash == snapshot_hash,
                "Reopened snapshot changed",
            )
            phase = "recover"
            print(case + ": recovering the original persisted journal", flush=True)
            started = post(
                base + "/transfer-load/recover",
                {
                    "revision": str(state.revision),
                    "execution_run_id": run_id,
                    "snapshot_hash": snapshot_hash,
                    "preflight_hash": state.transfer_preflight_report.content_hash,
                },
            )
            job = _wait_for_load(client, started.headers["location"], timeout=180)
            run = context.execution.current_transfer_run(workspace_id)
            require(
                run.run_id == run_id and run.snapshot_hash == snapshot_hash,
                "Recovery replaced the original journal or snapshot",
            )
            evidence["original_journal_recovered"] = True
        phase = "verify"
        evidence["load_job_status"] = job["status"]
        evidence["automatic_readback_verified"] = bool(job.get("verification_complete"))
        if job["status"] == "SUCCEEDED" and not job.get("verification_complete"):
            run = context.execution.current_transfer_run(workspace_id)
            post(base + "/transfer-load/reconcile", {"execution_run_id": run.run_id})
            checked = context.reconciliation.current(workspace_id)
            job["verification_complete"] = (
                checked is not None
                and checked.verified_count == 2
                and not checked.fallout_count
                and not checked.unknown_count
            )
            evidence["explicit_readback_retry"] = True
        require(
            job["status"] == "SUCCEEDED" and job.get("verification_complete"),
            "Journalled transfer did not finish verified",
        )
        run = context.execution.current_transfer_run(workspace_id)
        reconciliation = context.reconciliation.current(workspace_id)
        require(
            run.committed_count == 2
            and reconciliation.verified_count == 2
            and not reconciliation.fallout_count
            and not reconciliation.unknown_count,
            "Persisted verification is incomplete",
        )
        actual = {
            row.values["ref"]: {key: row.values[key] for key in FIELDS}
            for row in destination.records(refs)
        }
        require(
            actual == {row["ref"]: row for row in source_values[1:]},
            "Read-back differs or an excluded row was created",
        )
        require(
            counters["accepted_rows"] == 2 and counters["accepted_batches"] == 2,
            "A create batch was replayed",
        )
        state = context.queries.get(workspace_id)
        credential = get_target_credential(
            context.secret_store, state, TargetCredentialRole.DESTINATION_TRANSFER
        )
        target = transfer_destination_workspace(state)
        identity = context.read_identity_probe(
            target, destination.key, ("res.partner",)
        )
        origins = context.odoo_provenance.read_current_origins(
            workspace_id, actor=actor, dataset_id=dataset.dataset_id
        )
        repeat = DestinationMatchingService(context.categorical_coverage).check(
            state,
            selection,
            schema,
            tuple(
                DestinationMatchKeyChoice(
                    dataset_id=item.dataset_id,
                    source_column_key=item.source_column_key,
                    row_inclusion=item.row_inclusion,
                )
                for item in state.destination_match_plan.model_matches
            ),
            api_key=destination.key,
            credential_binding_hash=credential.binding_hash,
            read_identity=identity,
            reader=context.destination_match_reader,
            recorded_by=actor.identity.display_name,
            source_origins={dataset.dataset_id: origins[1]},
            row_inclusion_confirmation_hash=state.destination_match_plan.row_inclusion_confirmation_hash,
        )
        require(
            repeat.model_matches[0].destination_existing_key_count == 2
            and repeat.model_matches[0].destination_create_key_count == 0,
            "Repeat matching would create duplicate Contacts",
        )
        evidence.update(
            status="PASSED",
            committed=2,
            verified=2,
            excluded_records_created=0,
            accepted_create_batches=2,
            accepted_create_rows=2,
            snapshot_preserved=True,
            repeat_existing=2,
            repeat_create=0,
        )
    except (OSError, RuntimeError, ValueError, ConnectorError, WorkspaceError) as error:
        evidence.update(status="FAILED", phase=phase, error_type=type(error).__name__)
        if type(error) is RuntimeError:
            evidence["error"] = str(error)
    finally:
        if client is not None:
            client.close()
        if context is not None:
            stop_context(context)
        for label, connection in (("destination", destination), ("source", source)):
            try:
                cleanup(connection, refs)
                evidence[label + "_cleanup_verified"] = True
            except (OSError, RuntimeError, ValueError, ConnectorError):
                evidence[label + "_cleanup_verified"] = False
                evidence["status"] = "CLEANUP_FAILED"
        if evidence.get("source_cleanup_verified") and evidence.get(
            "destination_cleanup_verified"
        ):
            cleanup_manifest.unlink()
        else:
            evidence["private_cleanup_manifest"] = str(cleanup_manifest)
    return evidence


def qualify(args):
    require(args.allow_write, "Use --allow-write before reading credentials")
    connections = {}
    for label, mode in (("local", "LOCAL"), ("remote", "REMOTE")):
        key = getattr(args, label + "_api_key_file").read_text(encoding="utf-8").strip()
        require(
            key and not any(char.isspace() for char in key),
            "A private API key file is invalid",
        )
        connections[label] = Connection(
            getattr(args, label + "_base_url"),
            getattr(args, label + "_database"),
            mode,
            key,
        )
    fingerprints = {
        label: Json2ReadConnector(connection.config()).get_target_fingerprint()
        for label, connection in connections.items()
    }
    require(
        fingerprints["local"].target_hash != fingerprints["remote"].target_hash,
        "Use distinct Odoo targets",
    )
    from impodo.domain.odoo.compatibility import (
        OdooOperation,
        assess_odoo_operation,
        recognize_odoo_version,
    )

    for fingerprint in fingerprints.values():
        require(
            recognize_odoo_version(fingerprint.odoo_version).major == 20
            and assess_odoo_operation(
                fingerprint.odoo_version, OdooOperation.RECOVER
            ).allowed,
            "Both targets must be final Odoo 20",
        )
    report = {
        "application_build_id": PROCESS_BUILD_CONTRACT.application_build_id,
        "versions": {label: item.odoo_version for label, item in fingerprints.items()},
        "cases": [],
    }
    args.output_directory.mkdir(parents=True, exist_ok=True)
    for case in (args.case,) if args.case else CASES:
        source, destination = (
            (connections["local"], connections["remote"])
            if case.startswith("local")
            else (connections["remote"], connections["local"])
        )
        with TemporaryDirectory(prefix="impodo-row-transfer-") as storage:
            result = run_case(source, destination, case, Path(storage))
        report["cases"].append(result)
        report["checked_at"] = datetime.now(UTC).isoformat()
        report["status"] = (
            "PASSED"
            if all(item["status"] == "PASSED" for item in report["cases"])
            else "FAILED"
        )
        (args.output_directory / "result.json").write_text(
            json.dumps(report, indent=2) + "\n", encoding="utf-8"
        )
        print(case + ": " + result["status"], flush=True)
        if result["status"] != "PASSED":
            break
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for label in ("local", "remote"):
        parser.add_argument("--" + label + "-base-url", required=True)
        parser.add_argument("--" + label + "-database", required=True)
        parser.add_argument("--" + label + "-api-key-file", type=Path, required=True)
    parser.add_argument("--output-directory", type=Path, required=True)
    parser.add_argument("--case", choices=CASES)
    parser.add_argument("--allow-write", action="store_true", required=True)
    args = parser.parse_args()
    try:
        return 0 if qualify(args)["status"] == "PASSED" else 1
    except (OSError, RuntimeError, ValueError, ConnectorError, WorkspaceError) as error:
        print("Qualification stopped: " + type(error).__name__, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
