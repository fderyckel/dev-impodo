"""Qualify one synthetic Contact transfer between private disposable Odoo demos.

The runner reads ``https://host: API_KEY`` entries from a private file, keeps
credentials and exact synthetic values out of its output, and uses a unique
exact ``res.partner.ref`` namespace for every run. It exercises authenticated
Impodo HTTP routes from Odoo-source schema capture through verified load,
captures three authenticated browser screenshots, proves a duplicate Company
key blocks before a journal exists, and removes only the exact synthetic Odoo
records created by this run.
"""

from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import UTC, datetime
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from typing import Any, Iterable, Mapping
from urllib.parse import quote
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient

from impodo.adapters.odoo.connectors import Json2Config
from impodo.adapters.odoo.writer import Json2WriteExecutor
from impodo.adapters.protected_evidence.credential_vault import MemorySecretStore
from impodo.application.destination_matching_service import (
    DestinationMatchKeyChoice,
    DestinationMatchingService,
)
from impodo.domain.execution.odoo_scope import OdooApiScope, OdooModelScope
from impodo.domain.shared.access import Capability
from impodo.domain.shared.models import canonical_json_bytes
from impodo.domain.workspace.destination_matching import (
    carry_destination_create_field_reviews,
)
from impodo.domain.workspace.workbench import (
    OdooConnectionMode,
    transfer_destination_workspace,
)
from impodo.web.app import create_local_app
from impodo.web.target_credentials import (
    TargetCredentialRole,
    get_target_credential,
)
from scripts.capture_match_data_recovery_screenshots import (
    VIEWPORT,
    _start_server,
    _stop_server,
)
from scripts.qualify_odoo_to_odoo_matching import _connections
from tests.support.browser_scenarios import (
    POST_HEADERS,
    ProjectWorkspaceBuilder,
    _csrf,
    _replace_run_target_setup,
    _wait_for_load,
    _wait_for_odoo_capture,
)


PARTNER_FIELDS = ("city", "company_type", "email", "name", "parent_id", "ref")
PARTNER_SCOPE = OdooApiScope(
    preview_hash="sha256:" + "8" * 64,
    models=(
        OdooModelScope(
            "res.partner",
            write_fields=PARTNER_FIELDS,
            read_fields=PARTNER_FIELDS,
            lookup_fields=("ref",),
        ),
    ),
)


class QualificationError(RuntimeError):
    """A safe, operator-readable qualification failure."""


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--connections-file", type=Path, required=True)
    parser.add_argument("--source-index", type=int, default=1)
    parser.add_argument("--destination-index", type=int, default=2)
    parser.add_argument(
        "--output-directory",
        type=Path,
        default=ROOT / "build" / "acceptance" / "odoo-to-odoo-contact",
    )
    parser.add_argument("--browser-channel", default="msedge")
    return parser.parse_args()


def _executor(connection: tuple[str, str, str]) -> Json2WriteExecutor:
    base_url, database, api_key = connection
    return Json2WriteExecutor(
        Json2Config(
            base_url,
            database,
            api_key,
            retries=0,
            context={
                "mail_create_nosubscribe": True,
                "mail_notrack": True,
                "tracking_disable": True,
            },
        ),
        PARTNER_SCOPE,
    )


def _post(
    client: TestClient,
    csrf_token: str,
    path: str,
    data: Mapping[str, Any],
    *,
    expected: Iterable[int] = (303,),
):
    submitted = {"csrf_token": csrf_token, **data}
    response = client.post(
        path,
        data=submitted,
        headers=POST_HEADERS,
        follow_redirects=False,
    )
    if response.status_code not in set(expected):
        raise QualificationError(
            f"Impodo route {path} returned HTTP {response.status_code}"
        )
    return response


def _unlink(executor: Json2WriteExecutor, record_ids: Iterable[int]) -> bool:
    identifiers = tuple(dict.fromkeys(int(item) for item in record_ids if item))
    if not identifiers:
        return True
    config = executor.config
    url = f"{config.base_url}/json/2/{quote('res.partner', safe='.')}/unlink"
    headers = {
        "Authorization": f"bearer {config.api_key}",
        "Content-Type": "application/json; charset=utf-8",
        "X-Odoo-Database": config.database,
        "User-Agent": "impodo-contact-qualification",
    }
    try:
        status, response = executor.transport(
            url,
            headers,
            canonical_json_bytes({"ids": list(identifiers), "context": {}}),
            config.timeout_seconds,
            "POST",
        )
    except Exception:
        return False
    return status == 200 and response is True


def _find_ids(executor: Json2WriteExecutor, refs: Iterable[str]) -> tuple[int, ...]:
    found: list[int] = []
    for reference in refs:
        found.extend(executor.find_ids("res.partner", (("ref", "=", reference),)))
    return tuple(dict.fromkeys(found))


def _fresh_match(context, workspace, selection, schema, credential):
    choices = tuple(
        DestinationMatchKeyChoice(
            dataset_id=item.dataset_id,
            source_column_key=item.source_column_key,
            additional_source_column_keys=item.source_column_keys[1:],
        )
        for item in workspace.destination_match_plan.model_matches
    )
    destination = replace(
        transfer_destination_workspace(workspace),
        intended_models=tuple(
            sorted({item.source.model for item in selection.datasets})
        ),
    )
    identity = context.read_identity_probe(
        destination,
        credential.secret,
        destination.intended_models,
    )
    source_origins = {}
    for dataset in selection.datasets:
        protected = context.odoo_provenance.read_current_origins(
            workspace.workspace_id,
            actor=context.actor,
            dataset_id=dataset.dataset_id,
        )
        if protected is not None:
            source_origins[dataset.dataset_id] = protected[1]
    plan = DestinationMatchingService(context.categorical_coverage).check(
        workspace,
        selection,
        schema,
        choices,
        api_key=credential.secret,
        credential_binding_hash=credential.binding_hash,
        read_identity=identity,
        reader=context.destination_match_reader,
        recorded_by=context.actor.identity.display_name,
        source_origins=source_origins,
    )
    protected = None
    approved = workspace.destination_match_plan
    if approved.create_field_evidence_id is not None:
        access = context.workspace_access.resolve(
            workspace.workspace_id,
            actor=context.actor,
            capability=Capability.PROTECTED_EVIDENCE_READ,
        )
        protected = context.destination_create_fields.read(
            access.project_id,
            approved,
        )
    return carry_destination_create_field_reviews(plan, approved, protected)


def _screenshots(
    app,
    session_cookie: str,
    workspace_id: str,
    output_directory: Path,
    browser_channel: str,
) -> tuple[str, ...]:
    from playwright.sync_api import sync_playwright

    output_directory.mkdir(parents=True, exist_ok=True)
    server = None
    thread = None
    paths = (
        output_directory / "stage-5-destination-matching.png",
        output_directory / "stage-7-transfer-review.png",
        output_directory / "stage-8b-verified-load.png",
    )
    try:
        server, thread, port = _start_server(app)
        base_url = f"http://127.0.0.1:{port}"
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                channel=browser_channel,
                headless=True,
            )
            browser_context = browser.new_context(
                viewport=VIEWPORT,
                device_scale_factor=1,
                locale="en-GB",
            )
            browser_context.add_cookies(
                [
                    {
                        "name": "impodo_session",
                        "value": session_cookie,
                        "url": base_url,
                        "httpOnly": True,
                        "sameSite": "Strict",
                    }
                ]
            )
            page = browser_context.new_page()
            routes = (
                f"/workspaces/{workspace_id}/destination-matching",
                f"/workspaces/{workspace_id}/transfer-review",
                f"/workspaces/{workspace_id}/transfer-load/outcome",
            )
            for route, path in zip(routes, paths, strict=True):
                response = page.goto(base_url + route, wait_until="networkidle")
                if response is None or response.status != 200:
                    raise QualificationError(
                        f"Authenticated screenshot route {route} did not load"
                    )
                if route.endswith("/transfer-load/outcome"):
                    body = page.locator("body").inner_text()
                    if "Odoo change · explicit action only" not in body:
                        raise QualificationError(
                            "The verified load page still claims that Odoo stays unchanged"
                        )
                    if "res.partnerres.partner" in body:
                        raise QualificationError(
                            "The verified load page repeats the record type without separation"
                        )
                page.screenshot(path=str(path), full_page=True)
            browser_context.close()
            browser.close()
    finally:
        if server is not None and thread is not None:
            _stop_server(server, thread)
    return tuple(path.name for path in paths)


def qualify(args: argparse.Namespace) -> dict[str, object]:
    connections = _connections(args.connections_file)
    if (
        args.source_index == args.destination_index
        or not 1 <= args.source_index <= len(connections)
        or not 1 <= args.destination_index <= len(connections)
    ):
        raise QualificationError("Choose two distinct private Odoo connections")
    source_connection = connections[args.source_index - 1]
    destination_connection = connections[args.destination_index - 1]
    source_executor = _executor(source_connection)
    destination_executor = _executor(destination_connection)
    if source_executor.target_hash == destination_executor.target_hash:
        raise QualificationError("Source and destination resolve to the same target")

    token = uuid4().hex
    company_ref = f"IMPODO-QA-{token}-COMPANY"
    contact_ref = f"IMPODO-QA-{token}-CONTACT"
    synthetic_city = f"Impodo-QA-{token}"
    company_name = f"Impodo synthetic company {token}"
    contact_name = f"Impodo synthetic contact {token}"
    source_created: list[int] = []
    destination_created: list[int] = []
    cleanup = {"source": False, "destination": False}
    temporary: TemporaryDirectory[str] | None = None
    client: TestClient | None = None
    app = None
    evidence: dict[str, object] = {
        "contract": "impodo-odoo-to-odoo-contact-acceptance-v1",
        "started_at": datetime.now(UTC).isoformat(),
        "source_odoo_major": 19,
        "destination_odoo_major": 19,
        "synthetic": True,
    }
    phase = "seed_source"
    try:
        source_company = source_executor.create_rows(
            "res.partner",
            (
                {
                    "city": synthetic_city,
                    "company_type": "company",
                    "name": company_name,
                    "ref": company_ref,
                },
            ),
        )[0]
        source_created.append(source_company)
        source_contact = source_executor.create_rows(
            "res.partner",
            (
                {
                    "city": synthetic_city,
                    "company_type": "person",
                    "email": f"contact-{token}@example.invalid",
                    "name": contact_name,
                    "parent_id": source_company,
                    "ref": contact_ref,
                },
            ),
        )[0]
        source_created.append(source_contact)

        phase = "seed_destination"
        destination_companies = destination_executor.create_rows(
            "res.partner",
            (
                {
                    "city": synthetic_city,
                    "company_type": "company",
                    "name": company_name,
                    "ref": company_ref,
                },
                {
                    "city": synthetic_city,
                    "company_type": "company",
                    "name": company_name + " duplicate",
                    "ref": company_ref,
                },
            ),
        )
        destination_created.extend(destination_companies)

        phase = "workspace_setup"
        (ROOT / ".tmp").mkdir(exist_ok=True)
        temporary = TemporaryDirectory(dir=ROOT / ".tmp")
        app = create_local_app(
            temporary.name,
            launch_token="contact-qualification-launch",
            session_secret="contact-qualification-session",
            secret_store=MemorySecretStore(),
        )
        context = app.state.context
        created = ProjectWorkspaceBuilder(context).create(
            name="Synthetic Contact transfer qualification",
            source_system="Odoo 19 synthetic data",
            source_mode="ODOO",
        )
        source_url, source_database, source_key = source_connection
        configured = context.workspace_states.update_target(
            created.workspace_id,
            actor=context.actor,
            expected_revision=created.revision,
            odoo_connection_mode=OdooConnectionMode.REMOTE.value,
            odoo_base_url=source_url,
            odoo_database=source_database,
            intended_applications=("contacts",),
            intended_models=("res.partner",),
        )
        _replace_run_target_setup(
            context,
            configured.workspace_id,
            connection_mode=OdooConnectionMode.REMOTE,
            base_url=source_url,
            database=source_database,
            intended_applications=("contacts",),
        )
        registered = context.workspace_states.register(
            configured.workspace_id,
            actor=context.actor,
            expected_revision=configured.revision,
        )

        client = TestClient(app)
        launched = client.get(
            "/launch?token=contact-qualification-launch",
            follow_redirects=False,
        )
        if launched.status_code != 303:
            raise QualificationError("Impodo launch authentication failed")
        csrf_token = _csrf(client.get("/projects").text)
        workspace_id = registered.workspace_id

        phase = "source_schema"
        _post(
            client,
            csrf_token,
            f"/workspaces/{workspace_id}/sources/odoo-read-credential",
            {"read_api_key": source_key},
        )
        _post(
            client,
            csrf_token,
            f"/workspaces/{workspace_id}/schema/capture",
            {"return_to_sources": "1"},
        )
        phase = "source_plan"
        _post(
            client,
            csrf_token,
            f"/workspaces/{workspace_id}/sources/odoo-selection",
            {
                "dataset_name": "synthetic_contacts",
                "model": "res.partner",
                "field_names": ["city", "email", "name", "ref"],
                "page_size": "10",
                "filter_field": "city",
                "filter_value": synthetic_city,
                "include_archived": "1",
            },
        )
        selections = context.queries.get_current_odoo_capture_selections(workspace_id)
        if len(selections) != 1:
            raise QualificationError("The source capture plan was not saved")
        source_plan = selections[0]
        phase = "source_assessment"
        _post(
            client,
            csrf_token,
            f"/workspaces/{workspace_id}/sources/odoo-assessment",
            {
                "selection_id": source_plan.selection_id,
                "selection_hash": source_plan.content_hash,
            },
            expected=(200,),
        )
        phase = "source_capture"
        started = _post(
            client,
            csrf_token,
            f"/workspaces/{workspace_id}/sources/odoo-capture",
            {
                "selection_id": source_plan.selection_id,
                "selection_hash": source_plan.content_hash,
                "confirm_capture": "1",
            },
        )
        capture = _wait_for_odoo_capture(
            client,
            started.headers["location"],
            timeout=60.0,
        )
        if capture.get("status") != "SUCCEEDED":
            raise QualificationError("The bounded Odoo source capture did not succeed")
        selection = context.queries.get_source_selection(workspace_id)
        schema = context.queries.get_odoo_schema_catalog(workspace_id)
        if selection is None or schema is None or selection.datasets[0].row_count != 2:
            raise QualificationError("The frozen source evidence is incomplete")
        dataset = selection.datasets[0]
        ref_column = next(item for item in dataset.columns if item.source_name == "ref")
        evidence["frozen_source_rows"] = dataset.row_count

        phase = "destination_connection"
        destination_url, destination_database, destination_key = destination_connection
        current = context.queries.get(workspace_id)
        _post(
            client,
            csrf_token,
            f"/workspaces/{workspace_id}/transfer-destination",
            {
                "revision": str(current.revision),
                "odoo_connection_mode": OdooConnectionMode.REMOTE.value,
                "odoo_base_url": destination_url,
                "odoo_database": destination_database,
                "read_api_key": destination_key,
                "read_api_key_storage": "session",
            },
        )

        phase = "ambiguous_match"
        current = context.queries.get(workspace_id)
        _post(
            client,
            csrf_token,
            f"/workspaces/{workspace_id}/destination-matching",
            {
                "revision": str(current.revision),
                "match_key": f"{dataset.dataset_id}::{ref_column.stable_key}",
            },
        )
        ambiguous_state = context.queries.get(workspace_id)
        ambiguous_match = ambiguous_state.destination_match_plan.model_matches[0]
        if ambiguous_match.destination_duplicate_key_count != 1:
            raise QualificationError("The duplicate Company did not block matching")
        if context.execution.current_transfer_run(workspace_id) is not None:
            raise QualificationError("A transfer journal existed before ambiguity was resolved")
        evidence["ambiguous_company_blocked_before_journal"] = True

        phase = "ambiguity_cleanup"
        duplicate_company = destination_created.pop()
        if not _unlink(destination_executor, (duplicate_company,)):
            raise QualificationError("The synthetic ambiguous Company could not be removed")

        phase = "ready_match"
        current = context.queries.get(workspace_id)
        _post(
            client,
            csrf_token,
            f"/workspaces/{workspace_id}/destination-matching",
            {
                "revision": str(current.revision),
                "match_key": f"{dataset.dataset_id}::{ref_column.stable_key}",
            },
        )
        current = context.queries.get(workspace_id)
        pending = current.destination_match_plan.pending_create_field_decisions
        if pending:
            _post(
                client,
                csrf_token,
                f"/workspaces/{workspace_id}/destination-matching/defaults",
                {
                    "revision": str(current.revision),
                    "match_plan_hash": current.destination_match_plan.content_hash,
                    "default_field": [
                        f"{item.model}::{item.field_name}" for item in pending
                    ],
                },
            )
            current = context.queries.get(workspace_id)
        if not current.destination_match_plan.ready:
            raise QualificationError("Destination matching remained blocked after review")
        match = current.destination_match_plan.model_matches[0]
        if (
            match.destination_existing_key_count != 1
            or match.destination_create_key_count != 1
        ):
            raise QualificationError("The destination did not classify one reuse and one create")
        evidence["first_match"] = {"existing": 1, "create": 1, "ambiguous": 0}
        evidence["reviewed_required_defaults"] = len(pending)
        evidence["destination_field_check"] = {
            "missing": list(match.missing_fields),
            "incompatible": list(match.incompatible_fields),
            "unresolved_required": list(match.unresolved_create_fields),
            "blockers": list(match.write_blocking_reasons),
        }
        if match.write_blocking_reasons:
            raise QualificationError(
                "Destination write-field check remained blocked after required-default review"
            )

        phase = "transfer_order"
        _post(
            client,
            csrf_token,
            f"/workspaces/{workspace_id}/transfer-order",
            {"revision": str(current.revision)},
        )
        phase = "transfer_review"
        current = context.queries.get(workspace_id)
        _post(
            client,
            csrf_token,
            f"/workspaces/{workspace_id}/transfer-review/build",
            {
                "revision": str(current.revision),
                f"policy_{dataset.dataset_id}": "create_if_missing",
            },
        )
        current = context.queries.get(workspace_id)
        _post(
            client,
            csrf_token,
            f"/workspaces/{workspace_id}/transfer-review/approve",
            {
                "revision": str(current.revision),
                "confirmation": "approve",
                "reason": "Synthetic disposable two-instance qualification",
            },
        )
        phase = "preflight"
        current = context.queries.get(workspace_id)
        _post(
            client,
            csrf_token,
            f"/workspaces/{workspace_id}/transfer-preflight",
            {"revision": str(current.revision)},
        )
        current = context.queries.get(workspace_id)
        if not current.transfer_preflight_report.ready:
            raise QualificationError("Destination preflight was not ready")
        evidence["preflight_ready"] = True

        phase = "prepare_load"
        _post(
            client,
            csrf_token,
            f"/workspaces/{workspace_id}/transfer-load/prepare",
            {
                "revision": str(current.revision),
                "preflight_hash": current.transfer_preflight_report.content_hash,
            },
        )
        current = context.queries.get(workspace_id)
        snapshot = context.transfer_execution.current_snapshot(
            current,
            selection,
            schema,
        )
        if snapshot is None or snapshot.write_count != 1:
            raise QualificationError("The final transfer preview was not one create")
        phase = "execute_load"
        started_load = _post(
            client,
            csrf_token,
            f"/workspaces/{workspace_id}/transfer-load",
            {
                "revision": str(current.revision),
                "snapshot_hash": snapshot.semantic_hash,
                "preflight_hash": current.transfer_preflight_report.content_hash,
                "batch_rows": "50",
            },
        )
        load = _wait_for_load(
            client,
            started_load.headers["location"],
            timeout=90.0,
        )
        evidence["load_job"] = {
            "status": load.get("status"),
            "verification_complete": bool(load.get("verification_complete")),
        }
        if load.get("status") != "SUCCEEDED":
            raise QualificationError("The Contact load did not succeed")
        if not load.get("verification_complete"):
            run = context.execution.current_transfer_run(workspace_id)
            if run is None:
                raise QualificationError(
                    "The completed load did not retain an execution journal"
                )
            _post(
                client,
                csrf_token,
                f"/workspaces/{workspace_id}/transfer-load/reconcile",
                {"execution_run_id": run.run_id},
            )
            reconciliation = context.reconciliation.current(workspace_id)
            if reconciliation is not None:
                evidence["reconciliation"] = {
                    "status": reconciliation.status.value,
                    "verified": reconciliation.verified_count,
                    "fallout": reconciliation.fallout_count,
                    "unknown": reconciliation.unknown_count,
                    "rows": [
                        {
                            "status": item.status.value,
                            "execution_status": item.execution_status,
                            "differing_fields": list(item.differing_fields),
                            "message": item.message,
                        }
                        for item in reconciliation.rows
                    ],
                }
            if (
                reconciliation is None
                or reconciliation.unknown_count
                or reconciliation.fallout_count
            ):
                raise QualificationError(
                    "The Contact load did not finish verified after recovery"
                )
            evidence["manual_verification_recovery"] = True
        loaded_contact_ids = destination_executor.find_ids(
            "res.partner",
            (("ref", "=", contact_ref),),
        )
        if len(loaded_contact_ids) != 1:
            raise QualificationError("Read-back did not find one transferred Contact")
        destination_created.extend(loaded_contact_ids)
        evidence["load"] = {"writes": 1, "verified": True}

        phase = "repeat_preview"
        current = context.queries.get(workspace_id)
        credential = get_target_credential(
            context.secret_store,
            current,
            TargetCredentialRole.DESTINATION_TRANSFER,
        )
        if credential is None:
            raise QualificationError("The destination transfer credential disappeared")
        repeat = _fresh_match(context, current, selection, schema, credential)
        repeat_match = repeat.model_matches[0]
        if (
            repeat_match.destination_create_key_count != 0
            or repeat_match.destination_existing_key_count != 2
            or repeat_match.destination_duplicate_key_count != 0
        ):
            raise QualificationError("The repeat preview did not classify both rows as existing")
        evidence["repeat_match"] = {"existing": 2, "create": 0, "ambiguous": 0}

        phase = "screenshots"
        session_cookie = client.cookies.get("impodo_session")
        if not session_cookie:
            raise QualificationError("The authenticated browser session is missing")
        client.close()
        client = None
        evidence["screenshots"] = list(
            _screenshots(
                app,
                session_cookie,
                workspace_id,
                args.output_directory,
                args.browser_channel,
            )
        )
        evidence["status"] = "PASSED"
        evidence["completed_at"] = datetime.now(UTC).isoformat()
        return evidence
    except Exception as error:
        evidence["status"] = "FAILED"
        evidence["phase"] = phase
        evidence["error_type"] = type(error).__name__
        if isinstance(error, QualificationError):
            evidence["error_detail"] = str(error)
        evidence["completed_at"] = datetime.now(UTC).isoformat()
        return evidence
    finally:
        if client is not None:
            client.close()
        destination_ids = _find_ids(
            destination_executor,
            (contact_ref, company_ref),
        )
        cleanup["destination"] = _unlink(destination_executor, destination_ids)
        source_ids = _find_ids(source_executor, (contact_ref, company_ref))
        cleanup["source"] = _unlink(source_executor, source_ids)
        evidence["cleanup"] = cleanup
        if evidence.get("status") == "PASSED" and not all(cleanup.values()):
            evidence["status"] = "CLEANUP_FAILED"
        if temporary is not None:
            temporary.cleanup()


def main() -> int:
    args = _arguments()
    args.output_directory.mkdir(parents=True, exist_ok=True)
    result_path = args.output_directory / "result.json"
    try:
        result = qualify(args)
    except Exception as error:
        payload = {
            "contract": "impodo-odoo-to-odoo-contact-acceptance-v1",
            "status": "FAILED",
            "error_type": type(error).__name__,
            "completed_at": datetime.now(UTC).isoformat(),
        }
        result_path.write_text(
            json.dumps(payload, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        print(json.dumps(payload, sort_keys=True))
        return 1
    result_path.write_text(
        json.dumps(result, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(result, sort_keys=True))
    return 0 if result.get("status") == "PASSED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
