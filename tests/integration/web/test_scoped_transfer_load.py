"""Preserve both destination read phases through the normal load preview route."""

import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from impodo.adapters.protected_evidence.credential_vault import MemorySecretStore
from impodo.application.transfer_execution_service import (
    compile_transfer_execution_snapshot,
)
from impodo.domain.shared.access import CapabilityAuthorizationPolicy
from impodo.domain.workspace.workbench import WorkspaceStateService
from impodo.web.routers.transfer_load import build_transfer_load_router
from tests.application.workspace.test_destination_matching import BINDING_HASH, HASH
from tests.application.workspace.test_scoped_odoo_transfer import ScopedTransferFixture
from tests.application.workspace.test_transfer_review import _WorkspaceRepository
from tests.integration.web.test_transfer_load_routes import (
    _endpoint,
    _NoExecution,
    _Queries,
    _request,
)
from tests.support.identity import MutableLocalRequestIdentityResolver


class ScopedTransferLoadRouteTests(unittest.TestCase):
    def test_prepare_compiles_combined_parent_and_child_evidence_without_writing(self):
        for major in (19, 20):
            for mode in ("LOCAL", "REMOTE"):
                with self.subTest(major=major, mode=mode):
                    fixture = ScopedTransferFixture(major, mode)
                    fixture.approve()
                    fixture.calls.clear()
                    repository = _WorkspaceRepository(fixture.workspace)
                    execution = _ScopedExecution(fixture)
                    writer_calls = []
                    context = SimpleNamespace(
                        identity_resolver=MutableLocalRequestIdentityResolver(),
                        queries=_Queries(repository, fixture.selection, fixture.schema),
                        workspace_states=WorkspaceStateService(
                            repository, CapabilityAuthorizationPolicy()
                        ),
                        categorical_coverage=fixture,
                        secret_store=MemorySecretStore(),
                        read_identity_probe=lambda *_args: fixture.identity,
                        destination_match_reader=fixture.reader,
                        odoo_provenance=SimpleNamespace(
                            read_current_origins=lambda _workspace, *, dataset_id, **_kwargs: (
                                None,
                                fixture.origins[dataset_id],
                            )
                        ),
                        execution=_NoExecution(),
                        transfer_execution=execution,
                        write_executor_factory=lambda *_args, **_kwargs: (
                            writer_calls.append(True)
                        ),
                    )
                    credential = SimpleNamespace(
                        secret="fixture-key", binding_hash=BINDING_HASH
                    )
                    with patch(
                        "impodo.web.routers.transfer_load.get_target_credential",
                        return_value=credential,
                    ):
                        response = asyncio.run(
                            _endpoint(
                                build_transfer_load_router(context),
                                "prepare_transfer_load",
                            )(
                                _request(
                                    "/transfer-load/prepare",
                                    {
                                        "csrf_token": "csrf",
                                        "revision": str(fixture.workspace.revision),
                                        "preflight_hash": fixture.report.content_hash,
                                    },
                                ),
                                fixture.workspace.workspace_id,
                            )
                        )
                    self.assertEqual(response.status_code, 303)
                    self.assertTrue(
                        response.headers["location"].endswith("/transfer-load/confirm")
                    )
                    self.assertEqual(len(fixture.calls), 2)
                    self.assertEqual(
                        execution.models, {"x.scope_parent", "x.scope_record"}
                    )
                    self.assertIsNotNone(execution.staged)
                    children = tuple(
                        row
                        for row in execution.staged.rows
                        if row.target_model == "x.scope_record"
                    )
                    self.assertEqual(
                        tuple(row.disposition for row in children), ("UPDATE", "CREATE")
                    )
                    self.assertEqual(children[1].business_scope[0].key, ("Parent B",))
                    self.assertEqual(writer_calls, [])


class _ScopedExecution:
    def __init__(self, fixture):
        self.fixture = fixture
        self.models = set()
        self.staged = None

    def compile(
        self, workspace, selection, schema, package, report, match, records, **_kwargs
    ):
        self.models = set(records.records)
        return compile_transfer_execution_snapshot(
            workspace,
            selection,
            schema,
            package,
            report,
            match,
            records,
            source_rows=self.fixture.rows,
            source_origins=self.fixture.origins,
            source_snapshots={
                item.dataset_id: SimpleNamespace(content_hash=HASH)
                for item in selection.datasets
            },
            source_manifest_hashes={
                item.dataset_id: HASH for item in selection.datasets
            },
        )

    def stage(self, _workspace, _selection, _schema, snapshot):
        self.staged = snapshot


if __name__ == "__main__":
    unittest.main()
