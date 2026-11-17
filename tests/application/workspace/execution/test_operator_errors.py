"""Carry private rejection detail through execution and value-free workers."""

import json
import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime
from itertools import product
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from impodo.adapters.odoo.writer import Json2WriteExecutor
from impodo.application.workspace.execution.load_jobs import LoadJobResult
from impodo.domain.execution.odoo_scope import execution_api_scope
from impodo.domain.execution.odoo_write import OdooWriteRejected
from impodo.domain.execution.operator_errors import (
    OperatorErrorReceipt,
    capture_operator_error,
)
from impodo.domain.execution.write_diagnostics import (
    OdooWriteDiagnostic,
    OdooWriteErrorType,
    WriteFailureCode,
)
from impodo.domain.shared.access import LOCAL_ACTOR
from impodo.domain.shared.models import (
    OdooReadIdentity,
    OdooWriteIdentity,
    target_identity_hash,
)
from impodo.domain.workspace.workbench import OdooConnectionMode
from impodo.web.diagnostics import LocalDiagnosticRecorder
from tests.application import test_correction_execution as corrections
from tests.application.workspace.execution import test_service as fixtures
from tests.integration.odoo.test_load_request_pacing import config
from tests.integration.web.test_load_diagnostics import _bundle, _run_job
from tests.support.odoo_read_clock import ReadClock


class PrivateJournal(fixtures._Journal):
    def __init__(self):
        super().__init__()
        self.private = {}

    def record_outcomes_with_detail(self, workspace_id, run_id, rows, detail):
        self.record_outcomes(workspace_id, run_id, rows)
        for row in rows:
            self.private[(run_id, row.row_id)] = OperatorErrorReceipt(
                self.run.snapshot_hash, tuple(item.row_id for item in rows), detail
            )

    def operator_error_for_row(self, workspace_id, run_id, row_id):
        row = next(row for row in self.run.rows if row.row_id == row_id)
        return row, self.private.get((run_id, row_id))


class OperatorErrorExecutionTests(unittest.TestCase):
    def test_transfer_redacts_protected_echo_and_keeps_actual_submitted_context(self):
        for major in (19, 20):
            with self.subTest(major=major):
                service, _, snapshot = (
                    fixtures.ExecutionServiceTests()._transfer_service(
                        replace(fixtures._snapshot(), target_odoo_version=f"{major}.0")
                    )
                )
                workspace = replace(
                    service.workspaces.get(snapshot.workspace_id),
                    destination_verified_odoo_version=f"{major}.0",
                )
                service.workspaces = SimpleNamespace(get=lambda _: workspace)
                journal = PrivateJournal()
                service.journal = journal
                scope = execution_api_scope(snapshot)
                writer = fixtures._Executor(
                    scope.semantic_hash,
                    lookup_ids=(),
                    lookup_results={("res.partner", (("ref", "=", "C1"),)): (50,)},
                )
                writer.target_hash = snapshot.target_hash
                observed = datetime.now(UTC).isoformat()
                read = OdooReadIdentity(
                    snapshot.target_hash,
                    fixtures.HASH,
                    fixtures.HASH,
                    fixtures.HASH,
                    snapshot.readable_models,
                    observed,
                )
                write = OdooWriteIdentity(
                    target_hash=snapshot.target_hash,
                    principal_hash=fixtures.HASH,
                    permission_hash=fixtures.HASH,
                    context_hash=fixtures.HASH,
                    readable_models=tuple(m.model for m in scope.models),
                    writable_models=tuple(
                        m.model for m in scope.models if m.write_fields
                    ),
                    observed_at=observed,
                )

                def rejected(model, values, *_):
                    diagnostic = OdooWriteDiagnostic(
                        WriteFailureCode.VALIDATION,
                        model,
                        "load",
                        batch_size=len(values),
                        operator_detail_status="AVAILABLE",
                    )
                    raise OdooWriteRejected(
                        diagnostic.summary,
                        diagnostic=diagnostic,
                        operator_detail=capture_operator_error(
                            [
                                {
                                    "type": "error",
                                    "message": "Fictional rule rejected PRIVATE_ECHO",
                                }
                            ],
                            values,
                        ),
                    )

                with patch.object(
                    writer, "load_create_rows", side_effect=rejected
                ) as load:
                    run = service.execute_transfer(
                        snapshot.workspace_id,
                        expected_snapshot_hash=snapshot.semantic_hash,
                        expected_preflight_hash=fixtures.HASH,
                        snapshot=snapshot,
                        executor=writer,
                        actor=LOCAL_ACTOR,
                        read_identity=read,
                        credential_binding_hash=fixtures.HASH,
                        write_identity=write,
                        protected_values={fixtures.HASH: "PRIVATE_ECHO"},
                    )
                load.assert_called_once()
                self.assertEqual(run.unknown_count, 0)
                detail = next(iter(journal.private.values())).detail
                self.assertNotIn("PRIVATE_ECHO", detail.to_json())
                self.assertIn("[protected]", detail.messages[0].text)
                self.assertEqual(detail.submitted[0]["name"], "Category")

    def test_completed_load_correction_keeps_private_batch_detail(self):
        plan, confirmation, target, _bindings, _journal, _results, service = (
            corrections.CorrectionExecutionServiceTests()._fixture(2)
        )

        class Journal(corrections._Journal):
            def record_outcomes_with_detail(self, workspace_id, run_id, rows, detail):
                self.private = detail
                self.record_outcomes(workspace_id, run_id, rows)

            def operator_error_for_row(self, *_):
                raise AssertionError("Execution must not read private pages")

        journal = Journal()
        service.execution = journal

        def rejected(model, ids, values):
            self.assertEqual(journal.events[-1][0], "before-write")
            diagnostic = OdooWriteDiagnostic(
                WriteFailureCode.VALIDATION,
                model,
                "write",
                batch_size=len(ids),
                operator_detail_status="AVAILABLE",
            )
            raise OdooWriteRejected(
                diagnostic.summary,
                diagnostic=diagnostic,
                operator_detail=capture_operator_error(
                    [
                        {
                            "type": "error",
                            "message": "Fictional correction rule",
                            "field": "active",
                        }
                    ],
                    tuple(dict(values) for _ in ids),
                ),
            )

        with patch.object(target, "update_rows", side_effect=rejected) as write:
            outcome = service.execute(
                corrections.COMPLETED_WORKSPACE_ID,
                plan,
                confirmation,
                target_database="impodo-test",
                write_credential_binding_hash=corrections.HASHES[11],
                write_identity=corrections._write_identity(
                    observed_at="2026-08-28T04:02:00Z"
                ),
                reader=target,
                writer=target,
                actor=LOCAL_ACTOR,
            )
        write.assert_called_once()
        self.assertEqual(outcome.execution.failed_count, 2)
        self.assertEqual(outcome.execution.unknown_count, 0)
        self.assertEqual(journal.private.messages[0].text, "Fictional correction rule")
        self.assertEqual(
            journal.private.submitted, ({"active": "true"}, {"active": "true"})
        )

    def test_authoring_recipe_and_both_versions_keep_private_detail_out_of_zip(self):
        for major, recipe, mode in product(
            (19, 20), (False, True), ("LOCAL", "REMOTE")
        ):
            with (
                self.subTest(major=major, recipe=recipe, mode=mode),
                tempfile.TemporaryDirectory() as directory,
            ):
                cfg = config(major, mode)
                snapshot = replace(
                    fixtures._snapshot(),
                    target_odoo_version=f"{major}.0",
                    target_hash=target_identity_hash(
                        connection_mode=mode,
                        base_url=cfg.base_url,
                        database=cfg.database,
                    ),
                )
                service, _ = fixtures.ExecutionServiceTests()._service(
                    snapshot, mode=OdooConnectionMode(mode)
                )
                journal = PrivateJournal()
                service.journal = journal
                calls = []

                def transport(url, _headers, body, *_):
                    if url.endswith("search_read"):
                        return (
                            (200, [{"id": 42, "ref": "C1"}])
                            if "/res.partner/" in url
                            else (200, [])
                        )
                    calls.append(json.loads(body))
                    if mode == "LOCAL":
                        writer._observe_operator_error(
                            "Fictional private rejection: PREPARED_SENTINEL"
                        )
                        return 400, OdooWriteErrorType.VALIDATION
                    return 200, {
                        "ids": False,
                        "messages": [
                            {
                                "type": "error",
                                "message": "Fictional private rejection: PREPARED_SENTINEL",
                                "field": "name",
                                "rows": {"from": 0, "to": 0},
                            }
                        ],
                    }

                writer = Json2WriteExecutor(
                    cfg,
                    execution_api_scope(snapshot),
                    transport,
                    request_pacer=ReadClock().pacer(),
                )
                with closing(
                    LocalDiagnosticRecorder(Path(directory) / "events.jsonl")
                ) as recorder:

                    def work(*_args):
                        run = service.execute(
                            snapshot.workspace_id,
                            expected_snapshot_hash=snapshot.semantic_hash,
                            executor=writer,
                            actor=LOCAL_ACTOR,
                        )
                        return LoadJobResult(run.run_id, False)

                    finished, _ = _run_job(
                        recorder, snapshot.workspace_id, work, recipe=recipe
                    )
                    members, attempts = _bundle(recorder)
                self.assertTrue(calls, (finished, attempts))
                self.assertTrue(journal.private)
                receipt = next(iter(journal.private.values()))
                self.assertIn("PREPARED_SENTINEL", receipt.detail.messages[0].text)
                self.assertEqual(
                    receipt.detail.submitted[0]["name"],
                    (
                        calls[0]["data"][0][calls[0]["fields"].index("name")]
                        if mode == "REMOTE"
                        else calls[0]["vals_list"][0]["name"]
                    ),
                )
                self.assertNotIn("PREPARED_SENTINEL", str(members))
                self.assertNotIn("PREPARED_SENTINEL", str(attempts))
