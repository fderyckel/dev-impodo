"""Exercise persisted comparison, journalled writes, and independent read-back.

The expected counts are reviewed inputs. This module does not prepare or join
source data itself, and it never replaces an unknown write with a retry.
"""

from __future__ import annotations

import csv
import io
import json
from datetime import UTC, datetime

from impodo.adapters.odoo.connectors import Json2Config
from impodo.adapters.odoo.readback import Json2ReadbackReader
from impodo.adapters.odoo.writer import Json2WriteExecutor
from impodo.domain.serialization import content_hash
from impodo.domain.shared.models import OdooReadIdentity, OdooWriteIdentity
from impodo.domain.workspace.business_intent import (
    BusinessIntent,
    BusinessModelIntent,
    ExpectedCount,
)
from impodo.web.composition.page_reads import run_local_operation_sync
from impodo.web.target_credentials import TargetCredentialRole, store_target_credential

COUNTS = {
    "enrichment": (7, 1, 1),
    "headers": (1, 0, 1),
    "schedule": (0, 1, 3),
    "customers": (7, 1, 3),
    "incoming-immediate": (2, 1, 1),
}


def classifications(create, update, unchanged):
    """Keep all comparison classifications explicit, including zero blockers."""
    return {
        "CREATE": create,
        "UPDATE": update,
        "UNCHANGED": unchanged,
        "BLOCKED": 0,
        "AMBIGUOUS": 0,
    }


def immediate_overrides(root, company, *, delivery="2026-04-15"):
    """Materialize the reviewed immediate-term variant without changing NET45."""
    output = {}
    for name in ("customers_core.csv", "payment_terms.csv"):
        with (root / "deliveries" / delivery / name).open(
            newline="", encoding="utf-8"
        ) as stream:
            source = csv.DictReader(stream)
            fields, rows = source.fieldnames, list(source)
        for row in rows:
            if row["CompanyCode"] != company:
                continue
            field = "PaymentTermCode" if name == "customers_core.csv" else "TermCode"
            selected_owner = name == "customers_core.csv" and row["CustomerRef"] in (
                ("MA-N-0001", "MA-N-0003")
                if company == "NORTH"
                else ("MA-S-0001", "MA-S-0003")
            )
            if row[field] == "NET45" or selected_owner:
                row[field] = "DUE0"
                if name == "payment_terms.csv":
                    row["Name"] = "Morrow Immediate"
        stream = io.StringIO(newline="")
        writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
        output[name] = stream.getvalue().encode()
    return output


def controlled_ports(fixture, workspace_id, target, snapshot):
    """Replace only Odoo transport while preserving normal writer and reader."""
    state = fixture.context.queries.get(workspace_id)
    preview = fixture.context.execution.current_preview(
        workspace_id, actor=fixture.actor
    )
    observed = datetime.now(UTC).isoformat()
    read = OdooReadIdentity(
        target_hash=snapshot.target_hash,
        principal_hash=snapshot.read_principal_hash,
        permission_hash=snapshot.read_permission_hash,
        context_hash=snapshot.read_context_hash,
        readable_models=snapshot.readable_models,
        observed_at=observed,
    )
    write = OdooWriteIdentity(
        target_hash=snapshot.target_hash,
        principal_hash=content_hash("practice writer"),
        permission_hash=content_hash("practice write permission"),
        context_hash=snapshot.read_context_hash,
        readable_models=tuple(item.model for item in preview.api_scope.models),
        writable_models=tuple(
            item.model for item in preview.api_scope.models if item.write_fields
        ),
        observed_at=observed,
    )
    owner = fixture.context.target_credential_workspace(
        workspace_id, workspace_state=state, actor=fixture.actor
    )
    credential = store_target_credential(
        fixture.context.secret_store,
        owner,
        TargetCredentialRole.WRITE,
        "fictional-practice-write-key",
        persistent=False,
    )
    config = Json2Config(
        state.odoo_base_url,
        state.odoo_database,
        "fictional-key",
        connection_mode=state.odoo_connection_mode.value,
        retries=0,
        context=state.odoo_operation_context,
    )
    return (
        Json2WriteExecutor(config, preview.api_scope, transport=target.transport),
        Json2ReadbackReader(config, preview.api_scope, transport=target.transport),
        {
            "read_identity": read,
            "read_credential_binding_hash": snapshot.read_credential_binding_hash,
            "write_identity": write
            if state.odoo_connection_mode.value == "REMOTE"
            else None,
            "write_credential_binding_hash": credential.binding_hash
            if state.odoo_connection_mode.value == "REMOTE"
            else "",
        },
    )


def exercise_workspace(
    fixture, workspace_id, *, phase, target=None, expected=None, inspect_snapshot=None
):
    """Reopen the actual saved snapshot, execute, reconcile, and compare again."""
    expected = expected or classifications(*COUNTS[phase])
    if hasattr(fixture, "bind_local"):
        fixture.bind_local(workspace_id)
    fixture.prepare(workspace_id)
    fixture.approve(workspace_id)
    if phase == "schedule":
        fixture.expect_business_writes(
            workspace_id,
            intent=BusinessIntent(
                (
                    BusinessModelIntent(
                        "account.payment.term.line",
                        "update",
                        creates=ExpectedCount(maximum=0),
                    ),
                )
            ),
        )
    else:
        fixture.expect_business_writes(workspace_id)

    def compare():
        if target is None:
            return fixture.compare(workspace_id)
        schema = fixture.context.queries.get_odoo_schema_catalog(workspace_id)
        target.schema, target.target_hash = schema, schema.connection_target_hash
        return fixture.context.preflight.compare(
            workspace_id,
            reader=target.compare,
            actor=fixture.actor,
            local_operation=run_local_operation_sync,
        )

    report = compare()
    fixture.assertEqual(report.status, "READY", report)
    preview = fixture.context.execution.current_preview(
        workspace_id, actor=fixture.actor
    )
    fixture.assertTrue(preview.can_load, preview.scope_error)
    snapshot = preview.snapshot
    fixture.assertEqual(dict(snapshot.counts), expected)
    if inspect_snapshot is not None:
        inspect_snapshot(snapshot)
    if phase == "incoming-immediate":
        parent = next(
            row
            for row in snapshot.rows
            if row.target_model == "account.payment.term"
            and row.disposition == "CREATE"
        )
        owners = [row for row in snapshot.rows if row.target_model == "res.partner"]
        fixture.assertEqual({row.disposition for row in owners}, {"CREATE", "UPDATE"})
        for row in owners:
            link = next(
                field
                for field in row.fields
                if field.field == "property_payment_term_id"
            )
            fixture.assertIn(parent.row_id, link.dependency_row_ids)

    fixture.reopen()
    if hasattr(fixture, "bind_local"):
        fixture.bind_local(workspace_id)
    restored = fixture.context.execution.current_preview(
        workspace_id, actor=fixture.actor
    )
    fixture.assertEqual(restored.snapshot.semantic_hash, snapshot.semantic_hash)
    fixture.assertEqual(restored.snapshot.rows, snapshot.rows)
    writer, reader, identities = (
        controlled_ports(fixture, workspace_id, target, snapshot)
        if target is not None
        else fixture.ports(workspace_id, restored)
    )
    run = fixture.context.execution.execute(
        workspace_id,
        expected_snapshot_hash=snapshot.semantic_hash,
        executor=writer,
        actor=fixture.actor,
        **identities,
    )
    fixture.assertEqual(run.status.value, "COMPLETED", run.rows)
    fixture.assertEqual(run.committed_count, expected["CREATE"] + expected["UPDATE"])
    reconciliation = fixture.context.reconciliation.reconcile(
        workspace_id,
        expected_execution_run_id=run.run_id,
        reader=reader,
        actor=fixture.actor,
        **{key: value for key, value in identities.items() if key.startswith("write_")},
    )
    total = sum(expected.values())
    fixture.assertEqual(reconciliation.status.value, "VERIFIED", reconciliation.rows)
    fixture.assertEqual(reconciliation.verified_count, total)
    repeated = compare()
    fixture.assertEqual(repeated.status, "READY", repeated)
    fixture.assertEqual(
        (repeated.create_count, repeated.update_count, repeated.unchanged_count),
        (0, 0, total),
    )
    return {
        "phase": phase,
        "first_comparison": expected,
        "committed": run.committed_count,
        "verified": reconciliation.verified_count,
        "repeat_unchanged": total,
        "snapshot_hash": snapshot.semantic_hash,
        "reconciliation_hash": reconciliation.semantic_hash,
        "persisted_reopen_verified": True,
    }


def assert_controlled_projection(fixture, target, root, *, company, phase, refs=()):
    """Compare actual target values against the separately authored business oracle."""
    oracle = json.loads((root / "expected-business-results.json").read_text())
    if phase in {"enrichment", "customers", "incoming-immediate"}:
        expected = [
            row
            for row in oracle["baseline"]["customers"]
            if row["company_code"] == company
            and (not refs or row["customer_ref"] in refs)
        ]
        for row in expected:
            matches = [
                actual
                for (model, _), actual in target.records.items()
                if model == "res.partner" and actual.get("ref") == row["customer_ref"]
            ]
            fixture.assertEqual(len(matches), 1)
            actual = matches[0]
            for field in ("name", "email", "phone"):
                fixture.assertEqual(actual.get(field) or "", row[field] or "")
            fixture.assertEqual(actual.get("street"), row["address"]["street"])
            fixture.assertEqual(actual.get("zip"), row["address"]["postcode"])
            fixture.assertEqual(actual.get("city"), row["address"]["city"])
            if phase != "enrichment":
                term = target.records[
                    ("account.payment.term", actual["property_payment_term_id"])
                ]
                code = row["payment_term_key"][1]
                name = (
                    "Morrow Immediate"
                    if phase == "incoming-immediate"
                    else f"Morrow Net {code[3:]}"
                )
                fixture.assertEqual(term["name"], name)
                fixture.assertEqual(term["company_id"], target.company_ids[company])
    if phase in {"headers", "schedule", "incoming-immediate"}:
        expected_names = (
            {"Morrow Net 30", "Morrow Immediate"}
            if phase == "incoming-immediate"
            else {"Morrow Net 30", "Morrow Net 45"}
        )
        observed_names = set()
        for (model, identifier), term in target.records.items():
            if (
                model != "account.payment.term"
                or term["company_id"] != target.company_ids[company]
            ):
                continue
            observed_names.add(term["name"])
            lines = [
                line
                for (kind, _), line in target.records.items()
                if kind == "account.payment.term.line"
                and line["payment_id"] == identifier
            ]
            fixture.assertEqual(len(lines), 1)
            expected_days = (
                0
                if term["name"] == "Morrow Immediate"
                or (phase == "headers" and term["name"] == "Morrow Net 45")
                else 45
                if term["name"] == "Morrow Net 45"
                else 30
            )
            fixture.assertEqual(
                (
                    lines[0]["value"],
                    lines[0]["value_amount"],
                    lines[0]["nb_days"],
                    lines[0]["delay_type"],
                ),
                ("percent", 100, expected_days, "days_after"),
            )
        fixture.assertEqual(observed_names, expected_names)
