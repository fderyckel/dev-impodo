"""Resolve frozen nested parents with exact read-only API capabilities."""

import unittest

from impodo.adapters.odoo.connectors import Json2Config
from impodo.adapters.odoo.writer import Json2WriteExecutor
from impodo.application.workspace.execution.service import execution_api_scope
from impodo.domain.execution.odoo_write import OdooWriteRejected
from impodo.domain.shared.access import LOCAL_ACTOR
from impodo.domain.workspace.errors import WorkspaceError
from impodo.domain.workspace.workbench import OdooConnectionMode
from tests.application.workspace.execution import test_service as support
from tests.support.practice_reference_cases import (
    compared_reference_case as _comparison,
)
from tests.support.practice_reference_cases import saved_reference_snapshot as _snapshot


class _LookupExecutor(support._Executor):
    """Independent exact identities; mutations are recorded by the shared fake."""

    def __init__(self, scope_hash, *, company_matches=(9,)):
        super().__init__(scope_hash)
        self.company_matches = company_matches

    def find_ids(self, model, domain):
        self.lookups.append((model, tuple(domain)))
        if model == "res.company":
            return self.company_matches
        return {
            "account.payment.term": (10,),
            "account.payment.term.line": (200,),
            "res.partner": (50,),
        }[model]

    def find_ids_many(self, model, domains):
        return tuple(self.find_ids(model, domain) for domain in domains)


class NestedReferenceScopeTests(unittest.TestCase):
    def test_nested_reference_only_scope_grants_exact_parent_reads_without_writes(self):
        frozen, result = _comparison(reference_parent=True, schedule=True)
        self.assertEqual(result.counts["UPDATE"], 1)
        snapshot = _snapshot(frozen, result)
        self.assertEqual({row.dataset for row in snapshot.rows}, {"lines"})
        before = snapshot.to_json()
        scope = execution_api_scope(snapshot)
        self.assertEqual(snapshot.to_json(), before)
        self.assertEqual(scope.lookup_fields("res.company"), frozenset({"name"}))
        self.assertEqual(
            scope.lookup_fields("account.payment.term"),
            frozenset({"name", "company_id"}),
        )
        self.assertEqual(scope.write_fields("res.company"), frozenset())
        self.assertEqual(scope.write_fields("account.payment.term"), frozenset())
        self.assertEqual(scope.read_fields("res.company"), frozenset())
        self.assertEqual(
            scope.write_fields("account.payment.term.line"), frozenset({"nb_days"})
        )
        self.assertEqual(scope.creatable_models, ())
        calls = []

        def transport(url, _headers, _body, _timeout, _method):
            calls.append(url)
            return 200, [{"id": 9}]

        writer = Json2WriteExecutor(
            Json2Config(
                "http://127.0.0.1:8069",
                "disposable",
                "fictional",
                connection_mode="LOCAL",
                retries=0,
            ),
            scope,
            transport=transport,
        )
        self.assertEqual(
            writer.find_ids("res.company", (("name", "=", "North"),)), (9,)
        )
        dispatched = len(calls)
        for action in (
            lambda: writer.create_rows("res.company", ({"name": "Unauthorized"},)),
            lambda: writer.update_row("res.company", 9, {"name": "Unauthorized"}),
            lambda: writer.update_row(
                "account.payment.term", 10, {"name": "Unauthorized"}
            ),
            lambda: writer.find_ids("res.company", (("vat", "=", "UNREVIEWED"),)),
        ):
            with self.assertRaises(OdooWriteRejected):
                action()
            self.assertEqual(len(calls), dispatched)

    def test_existing_schedule_updates_only_line_after_resolving_nested_parents(self):
        frozen, result = _comparison(reference_parent=True, schedule=True)
        snapshot = _snapshot(frozen, result)
        service, journal = support.ExecutionServiceTests()._service(
            snapshot, mode=OdooConnectionMode.LOCAL
        )
        executor = _LookupExecutor(execution_api_scope(snapshot).semantic_hash)
        run = service.execute(
            snapshot.workspace_id,
            expected_snapshot_hash=snapshot.semantic_hash,
            executor=executor,
            actor=LOCAL_ACTOR,
        )
        self.assertEqual(run.status.value, "COMPLETED")
        self.assertEqual(executor.creates, [])
        self.assertEqual(executor.loads, [])
        self.assertEqual(
            executor.updates, [("account.payment.term.line", 200, {"nb_days": 45})]
        )
        self.assertEqual(journal.run.committed_count, 1)

    def test_missing_or_ambiguous_company_stops_before_any_line_write(self):
        for company_matches in ((), (9, 19)):
            with self.subTest(matches=len(company_matches)):
                frozen, result = _comparison(reference_parent=True, schedule=True)
                snapshot = _snapshot(frozen, result)
                service, journal = support.ExecutionServiceTests()._service(
                    snapshot, mode=OdooConnectionMode.LOCAL
                )
                executor = _LookupExecutor(
                    execution_api_scope(snapshot).semantic_hash,
                    company_matches=company_matches,
                )
                with self.assertRaises(WorkspaceError):
                    service.execute(
                        snapshot.workspace_id,
                        expected_snapshot_hash=snapshot.semantic_hash,
                        executor=executor,
                        actor=LOCAL_ACTOR,
                    )
                self.assertEqual(executor.creates, [])
                self.assertEqual(executor.loads, [])
                self.assertEqual(executor.updates, [])
                self.assertIsNone(journal.run)
