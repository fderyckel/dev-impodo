"""Qualify one simple Product transfer from a read-only Odoo 19 source.

The runner selects one existing single-variant Product with a unique Internal
Reference from the source, captures its linked Product Category and Units of
Measure through the normal Impodo browser routes, and transfers only the
Product to a distinct disposable destination. Supporting records are reuse
only. The created Product is verified, matched again, and deleted by its exact
unique qualification key.

Credentials, source values, target numeric identifiers, and protected
relationship evidence are never written to the portable result or printed.
This first Product slice does not qualify multi-variant Products, creation of
missing supporting records, or relational destination identities in general.
"""

from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import UTC, datetime
from html import unescape
import json
from pathlib import Path
import re
import sys
from tempfile import TemporaryDirectory
from typing import Any, Iterable, Mapping
from urllib.parse import quote


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient

from impodo.adapters.odoo.connectors import Json2Config, Json2ReadConnector
from impodo.adapters.odoo.writer import Json2WriteExecutor
from impodo.adapters.protected_evidence.credential_vault import MemorySecretStore
from impodo.application.destination_matching_service import (
    DestinationMatchKeyChoice,
    DestinationMatchingService,
)
from impodo.domain.execution.odoo_scope import OdooApiScope, OdooModelScope
from impodo.domain.odoo.contracts import RecordRequest
from impodo.domain.odoo_capture import odoo_capture_selection_set_hash
from impodo.domain.shared.access import Capability
from impodo.domain.shared.models import canonical_json_bytes
from impodo.domain.source_binding import OdooSourceBinding
from impodo.domain.workspace.destination_matching import (
    carry_destination_create_field_reviews,
)
from impodo.domain.workspace.workbench import (
    OdooConnectionMode,
    transfer_destination_workspace,
)
from impodo.web.app import create_local_app
from impodo.web.target_credentials import TargetCredentialRole, get_target_credential
from scripts.qualify_odoo_to_odoo_matching import _connections
from tests.support.browser_scenarios import (
    POST_HEADERS,
    ProjectWorkspaceBuilder,
    _csrf,
    _wait_for_load,
    _wait_for_odoo_capture,
)


PRODUCT_MODEL = "product.template"
SUPPORT_MODELS = ("product.category", "uom.uom")
SELECTED_MODELS = (PRODUCT_MODEL, *SUPPORT_MODELS)
PRODUCT_SCOPE = OdooApiScope(
    preview_hash="sha256:" + "6" * 64,
    models=(
        OdooModelScope(
            PRODUCT_MODEL,
            write_fields=(
                "categ_id",
                "default_code",
                "name",
                "uom_id",
            ),
            read_fields=(
                "categ_id",
                "default_code",
                "name",
                "uom_id",
            ),
            lookup_fields=("default_code",),
        ),
    ),
)


class QualificationError(RuntimeError):
    """A safe, operator-readable qualification failure."""


def _post(
    client: TestClient,
    csrf_token: str,
    path: str,
    data: Mapping[str, Any],
    *,
    expected: Iterable[int] = (303,),
):
    response = client.post(
        path,
        data={"csrf_token": csrf_token, **data},
        headers=POST_HEADERS,
        follow_redirects=False,
    )
    if response.status_code not in set(expected):
        action_errors = re.findall(
            r"We could not complete that action\.</strong>\s*<span>(.*?)</span>",
            response.text,
            flags=re.DOTALL,
        )
        support_details = re.findall(
            r"<code>(.*?)</code>", response.text, flags=re.DOTALL
        )
        safe_detail = ""
        raw_detail = action_errors[-1] if action_errors else (
            support_details[-1] if support_details else ""
        )
        if raw_detail:
            safe_detail = unescape(re.sub(r"<[^>]+>", "", raw_detail))
            safe_detail = re.sub(
                r"(?<![A-Za-z0-9])[A-Za-z0-9_-]{32,}(?![A-Za-z0-9])",
                "[redacted]",
                safe_detail,
            ).strip()
        raise QualificationError(
            f"Impodo route {path} returned HTTP {response.status_code}"
            + (f": {safe_detail[:500]}" if safe_detail else "")
        )
    return response


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


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--connections-file", type=Path, required=True)
    parser.add_argument("--source-index", type=int, default=3)
    parser.add_argument("--destination-index", type=int, default=1)
    parser.add_argument(
        "--output-directory",
        type=Path,
        default=ROOT / "build" / "acceptance" / "odoo-to-odoo-product",
    )
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
        PRODUCT_SCOPE,
    )


def _unlink(executor: Json2WriteExecutor, identifiers: Iterable[int]) -> bool:
    exact_ids = tuple(dict.fromkeys(int(item) for item in identifiers if item))
    if not exact_ids:
        return True
    config = executor.config
    url = f"{config.base_url}/json/2/{quote(PRODUCT_MODEL, safe='.')}/unlink"
    headers = {
        "Authorization": f"bearer {config.api_key}",
        "Content-Type": "application/json; charset=utf-8",
        "X-Odoo-Database": config.database,
        "User-Agent": "impodo-product-transfer-qualification",
    }
    try:
        status, response = executor.transport(
            url,
            headers,
            canonical_json_bytes({"ids": list(exact_ids), "context": {}}),
            config.timeout_seconds,
            "POST",
        )
    except Exception:
        return False
    return status == 200 and response is True


def _sequence_size(value: object) -> int | None:
    if not isinstance(value, (list, tuple)):
        return None
    return len(value)


def _many2one_label(value: object) -> str | None:
    if (
        isinstance(value, (list, tuple))
        and len(value) == 2
        and isinstance(value[1], str)
        and value[1].strip()
    ):
        return value[1]
    return None


def _value_cardinality(value: object) -> int:
    if value is None or value is False:
        return 0
    if isinstance(value, (list, tuple, set, frozenset)):
        return len(value)
    return 1


def _choose_source_product(
    source: Json2ReadConnector,
    destination: Json2ReadConnector,
) -> tuple[str, str]:
    """Return one protected simple Product identity without logging its value."""

    fields = (
        "attribute_line_ids",
        "categ_id",
        "default_code",
        "name",
        "product_variant_ids",
        "uom_id",
    )
    sampled = source.get_records(
        (RecordRequest(PRODUCT_MODEL, fields, limit=100),)
    ).records.get(PRODUCT_MODEL, ())
    for row in sampled:
        reference = row.values.get("default_code")
        name = row.values.get("name")
        if (
            not isinstance(reference, str)
            or not reference.strip()
            or not isinstance(name, str)
            or not name.strip()
            or not row.values.get("categ_id")
            or not row.values.get("uom_id")
            or _sequence_size(row.values.get("product_variant_ids")) != 1
            or _sequence_size(row.values.get("attribute_line_ids")) != 0
        ):
            continue
        exact_source = source.get_records(
            (
                RecordRequest(
                    PRODUCT_MODEL,
                    fields,
                    domain=(("default_code", "=", reference),),
                    limit=2,
                ),
            )
        ).records.get(PRODUCT_MODEL, ())
        if len(exact_source) != 1:
            continue
        exact_destination = destination.get_records(
            (
                RecordRequest(
                    PRODUCT_MODEL,
                    ("default_code",),
                    domain=(("default_code", "=", reference),),
                    limit=2,
                ),
            )
        ).records.get(PRODUCT_MODEL, ())
        if exact_destination:
            continue
        category_label = _many2one_label(row.values.get("categ_id"))
        uom_label = _many2one_label(row.values.get("uom_id"))
        if category_label is None or uom_label is None:
            continue
        destination_category = destination.get_records(
            (
                RecordRequest(
                    "product.category",
                    ("complete_name",),
                    domain=(("complete_name", "=", category_label),),
                    limit=2,
                ),
            )
        ).records.get("product.category", ())
        destination_uom = destination.get_records(
            (
                RecordRequest(
                    "uom.uom",
                    ("name",),
                    domain=(("name", "=", uom_label),),
                    limit=2,
                ),
            )
        ).records.get("uom.uom", ())
        if len(destination_category) == 1 and len(destination_uom) == 1:
            return reference, name
    raise QualificationError(
        "No unique single-variant source Product with reusable Category and UoM "
        "was absent from the destination"
    )


def _dataset_by_model(selection) -> dict[str, object]:
    result = {}
    for dataset in selection.datasets:
        if not isinstance(dataset.source, OdooSourceBinding):
            raise QualificationError("The frozen source contains a non-Odoo table")
        result[dataset.source.model] = dataset
    if set(result) != set(SELECTED_MODELS):
        raise QualificationError("The frozen Product source scope is incomplete")
    return result


def _column_key(dataset, source_name: str) -> str:
    column = next(
        (item for item in dataset.columns if item.source_name == source_name),
        None,
    )
    if column is None:
        raise QualificationError(
            f"The frozen source is missing the matching field for {source_name}"
        )
    return column.stable_key


def qualify(args: argparse.Namespace) -> dict[str, object]:
    connections = _connections(args.connections_file)
    if (
        args.source_index == args.destination_index
        or not 1 <= args.source_index <= len(connections)
        or not 1 <= args.destination_index <= len(connections)
    ):
        raise QualificationError("Choose two distinct Odoo connections")
    source_connection = connections[args.source_index - 1]
    destination_connection = connections[args.destination_index - 1]
    source_reader = Json2ReadConnector(
        Json2Config(*source_connection, retries=0)
    )
    destination_reader = Json2ReadConnector(
        Json2Config(*destination_connection, retries=0)
    )
    destination_executor = _executor(destination_connection)
    if (
        source_reader.get_target_fingerprint().target_hash
        == destination_executor.target_hash
    ):
        raise QualificationError("Source and destination resolve to the same target")

    product_reference, _product_name = _choose_source_product(
        source_reader, destination_reader
    )
    temporary: TemporaryDirectory[str] | None = None
    client: TestClient | None = None
    evidence: dict[str, object] = {
        "contract": "impodo-odoo-to-odoo-product-acceptance-v1",
        "started_at": datetime.now(UTC).isoformat(),
        "source_read_only": True,
        "destination_disposable": True,
        "source_product_shape": "single_variant",
    }
    cleanup = False
    destination_created: list[int] = []
    write_started = False
    phase = "workspace_setup"
    try:
        (ROOT / ".tmp").mkdir(exist_ok=True)
        temporary = TemporaryDirectory(dir=ROOT / ".tmp")
        app = create_local_app(
            temporary.name,
            launch_token="product-qualification-launch",
            session_secret="product-qualification-session",
            secret_store=MemorySecretStore(),
        )
        context = app.state.context
        created = ProjectWorkspaceBuilder(context).create(
            name="Read-only BetterUnited Product transfer qualification",
            source_system="Odoo 19 Product source",
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
            intended_applications=("products",),
            intended_models=SELECTED_MODELS,
        )
        registered = context.workspace_states.register(
            configured.workspace_id,
            actor=context.actor,
            expected_revision=configured.revision,
        )
        workspace_id = registered.workspace_id
        client = TestClient(app)
        launched = client.get(
            "/launch?token=product-qualification-launch",
            follow_redirects=False,
        )
        if launched.status_code != 303:
            raise QualificationError("Impodo launch authentication failed")
        csrf_token = _csrf(client.get("/projects").text)

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
        selections = (
            (
                "qualification_product",
                PRODUCT_MODEL,
                ("default_code", "name"),
                False,
                "default_code",
                product_reference,
            ),
            (
                "qualification_categories",
                "product.category",
                ("complete_name", "name"),
                True,
                "",
                "",
            ),
            (
                "qualification_uoms",
                "uom.uom",
                ("name",),
                True,
                "",
                "",
            ),
        )
        for dataset_name, model, field_names, linked_only, filter_field, filter_value in selections:
            _post(
                client,
                csrf_token,
                f"/workspaces/{workspace_id}/sources/odoo-selection",
                {
                    "dataset_name": dataset_name,
                    "model": model,
                    "field_names": list(field_names),
                    "page_size": "10",
                    "filter_field": filter_field,
                    "filter_value": filter_value,
                    "include_archived": "1",
                    "linked_only": "1" if linked_only else "",
                },
            )
        plans = context.queries.get_current_odoo_capture_selections(workspace_id)
        if len(plans) != len(SELECTED_MODELS):
            raise QualificationError("The complete Product capture plan was not saved")
        selection_hash = odoo_capture_selection_set_hash(plans)

        phase = "source_assessment"
        _post(
            client,
            csrf_token,
            f"/workspaces/{workspace_id}/sources/odoo-assessment",
            {
                "selection_id": plans[0].selection_id,
                "selection_hash": selection_hash,
                "confirm_linked_relationships": "1",
            },
            expected=(200,),
        )
        phase = "source_capture"
        started = _post(
            client,
            csrf_token,
            f"/workspaces/{workspace_id}/sources/odoo-capture",
            {
                "selection_id": plans[0].selection_id,
                "selection_hash": selection_hash,
                "confirm_capture": "1",
            },
        )
        capture = _wait_for_odoo_capture(
            client, started.headers["location"], timeout=300.0
        )
        if capture.get("status") != "SUCCEEDED":
            raise QualificationError("The bounded Product source capture did not succeed")
        selection = context.queries.get_source_selection(workspace_id)
        schema = context.queries.get_odoo_schema_catalog(workspace_id)
        if selection is None or schema is None:
            raise QualificationError("The frozen Product source evidence is incomplete")
        datasets = _dataset_by_model(selection)
        product_dataset = datasets[PRODUCT_MODEL]
        if product_dataset.row_count != 1:
            raise QualificationError("The Product filter did not freeze exactly one Product")
        evidence["frozen_rows_by_model"] = {
            model: datasets[model].row_count for model in sorted(datasets)
        }

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

        phase = "destination_matching"
        match_keys = []
        for model, field_name in (
            (PRODUCT_MODEL, "default_code"),
            ("product.category", "complete_name"),
            ("uom.uom", "name"),
        ):
            dataset = datasets[model]
            match_keys.append(
                f"{dataset.dataset_id}::{_column_key(dataset, field_name)}"
            )
        current = context.queries.get(workspace_id)
        _post(
            client,
            csrf_token,
            f"/workspaces/{workspace_id}/destination-matching",
            {"revision": str(current.revision), "match_key": match_keys},
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
        plan = current.destination_match_plan
        by_model = {item.model: item for item in plan.model_matches}
        if any(by_model[model].destination_duplicate_key_count for model in SELECTED_MODELS):
            raise QualificationError("A destination supporting identity is ambiguous")
        if by_model[PRODUCT_MODEL].destination_create_key_count != 1:
            raise QualificationError("The destination did not propose one Product create")
        if any(by_model[model].destination_create_key_count for model in SUPPORT_MODELS):
            raise QualificationError("A required supporting record is missing in the destination")
        if (
            not plan.ready
            or any(item.blocking_reasons for item in plan.model_matches)
            or by_model[PRODUCT_MODEL].write_blocking_reasons
        ):
            blocking = sorted(
                {
                    reason
                    for item in plan.model_matches
                    for reason in item.blocking_reasons
                }
                | set(by_model[PRODUCT_MODEL].write_blocking_reasons)
            )
            raise QualificationError(
                "Destination Product matching remained blocked"
                + (f": {','.join(blocking)}" if blocking else "")
            )
        if any(
            not item.source_evidence_available
            or item.missing_related_record_count
            or item.ambiguous_destination_link_count
            for item in plan.relationship_matches
        ):
            raise QualificationError("A Product relationship could not be resolved uniquely")
        evidence["matching"] = {
            "product_create": 1,
            "supporting_reused": sum(
                by_model[model].destination_existing_key_count
                for model in SUPPORT_MODELS
            ),
            "relationship_fields": len(plan.relationship_matches),
            "reviewed_defaults": len(pending),
            "uom_identity": "unique_name_for_bounded_trial",
        }

        phase = "transfer_order"
        _post(
            client,
            csrf_token,
            f"/workspaces/{workspace_id}/transfer-order",
            {"revision": str(current.revision)},
        )
        phase = "transfer_review"
        current = context.queries.get(workspace_id)
        policies: dict[str, Any] = {"revision": str(current.revision)}
        for model, dataset in datasets.items():
            policies[f"policy_{dataset.dataset_id}"] = (
                "create_if_missing" if model == PRODUCT_MODEL else "reuse_only"
            )
        _post(
            client,
            csrf_token,
            f"/workspaces/{workspace_id}/transfer-review/build",
            policies,
        )
        current = context.queries.get(workspace_id)
        _post(
            client,
            csrf_token,
            f"/workspaces/{workspace_id}/transfer-review/approve",
            {
                "revision": str(current.revision),
                "confirmation": "approve",
                "reason": "Disposable BetterUnited Product qualification",
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
            raise QualificationError("Destination Product preflight was not ready")

        phase = "execute_load"
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
            current, selection, schema
        )
        if snapshot is None or snapshot.write_count != 1:
            raise QualificationError("The final Product preview was not one create")
        write_started = True
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
            client, started_load.headers["location"], timeout=120.0
        )
        run = context.execution.current_transfer_run(workspace_id)
        journal_ids = tuple(
            row.odoo_id
            for row in (run.rows if run is not None else ())
            if row.target_model == PRODUCT_MODEL
            and row.operation == "CREATE"
            and row.odoo_id is not None
        )
        if len(journal_ids) != 1:
            raise QualificationError(
                "The Product load journal did not retain one exact created ID"
            )
        destination_created.extend(journal_ids)
        loaded_ids = destination_executor.find_ids(
            PRODUCT_MODEL, (("default_code", "=", product_reference),)
        )
        if loaded_ids != journal_ids:
            raise QualificationError(
                "Read-back did not resolve to the exact journaled Product"
            )
        reconciliation = context.reconciliation.current(workspace_id)
        detail = (
            context.reconciliation.current_detail(
                workspace_id,
                actor=context.actor,
            )
            if reconciliation is not None
            else None
        )
        evidence["load"] = {
            "status": load.get("status"),
            "writes": 1,
            "journaled_product_ids": len(journal_ids),
            "verification_complete": bool(load.get("verification_complete")),
            "failure_reported": bool(load.get("failure_message")),
            "execution_status": run.status.value if run is not None else "missing",
            "reconciliation": (
                {
                    "status": reconciliation.status.value,
                    "verified": reconciliation.verified_write_count,
                    "fallout": reconciliation.fallout_count,
                    "unknown": reconciliation.unknown_count,
                    "differing_fields": sorted(
                        {
                            field
                            for row in reconciliation.rows
                            for field in row.differing_fields
                        }
                    ),
                    "difference_shapes": (
                        [
                            {
                                "field": item.field,
                                "reason": item.reason_code,
                                "expected_members": _value_cardinality(
                                    item.expected_value
                                ),
                                "observed_members": _value_cardinality(
                                    item.observed_value
                                ),
                            }
                            for item in detail.differences
                        ]
                        if detail is not None
                        else []
                    ),
                }
                if reconciliation is not None
                else None
            ),
        }
        if load.get("status") != "SUCCEEDED" or not load.get("verification_complete"):
            raise QualificationError("The Product load did not finish verified")

        phase = "repeat_preview"
        current = context.queries.get(workspace_id)
        credential = get_target_credential(
            context.secret_store,
            current,
            TargetCredentialRole.DESTINATION_TRANSFER,
        )
        if credential is None:
            raise QualificationError("The destination credential disappeared")
        repeat = _fresh_match(context, current, selection, schema, credential)
        repeat_by_model = {item.model: item for item in repeat.model_matches}
        if repeat_by_model[PRODUCT_MODEL].destination_create_key_count != 0:
            raise QualificationError("The repeat preview proposed a duplicate Product")
        if repeat_by_model[PRODUCT_MODEL].destination_existing_key_count != 1:
            raise QualificationError("The repeat preview did not find the Product")
        evidence["repeat_match"] = {
            "product_existing": 1,
            "product_create": 0,
        }
        evidence["status"] = "PASSED"
    except Exception as error:
        evidence["status"] = "FAILED"
        evidence["phase"] = phase
        evidence["error_type"] = type(error).__name__
        if isinstance(error, (AssertionError, QualificationError)):
            evidence["error_detail"] = str(error)
    finally:
        if client is not None:
            client.close()
        try:
            cleanup = (
                _unlink(destination_executor, destination_created)
                if destination_created
                else not write_started
            )
        except Exception:
            cleanup = False
        evidence["cleanup"] = cleanup
        if evidence.get("status") == "PASSED" and not cleanup:
            evidence["status"] = "CLEANUP_FAILED"
        evidence["completed_at"] = datetime.now(UTC).isoformat()
        if temporary is not None:
            temporary.cleanup()
    return evidence


def main() -> int:
    args = _arguments()
    args.output_directory.mkdir(parents=True, exist_ok=True)
    result_path = args.output_directory / "result.json"
    try:
        result = qualify(args)
    except Exception as error:
        result = {
            "contract": "impodo-odoo-to-odoo-product-acceptance-v1",
            "status": "FAILED",
            "error_type": type(error).__name__,
            "completed_at": datetime.now(UTC).isoformat(),
        }
    result_path.write_text(
        json.dumps(result, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(result, sort_keys=True))
    return 0 if result.get("status") == "PASSED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
