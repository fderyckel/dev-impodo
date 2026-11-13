"""Replay protected aliases before destination preflight or final preparation."""

import asyncio
from dataclasses import replace
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from starlette.responses import Response

from impodo.adapters.protected_destination_identity_aliases import (
    ProtectedDestinationIdentityAliasStore,
)
from impodo.adapters.protected_evidence.credential_vault import MemorySecretStore
from impodo.adapters.protected_project_evidence_store import (
    ProtectedProjectEvidenceStore,
)
from impodo.application.transfer_order_service import TransferOrderService
from impodo.application.transfer_preflight_service import TransferPreflightService
from impodo.application.transfer_review_service import TransferReviewService
from impodo.domain.shared.access import (
    AuthorizationError,
    Capability,
    CapabilityAuthorizationPolicy,
    LOCAL_ACTOR,
)
from impodo.domain.workspace.transfer_review import TransferReviewApproval
from impodo.domain.workspace.workbench import WorkspaceStateService
from impodo.web.routers.transfer_load import build_transfer_load_router
from impodo.web.routers.transfer_preflight import build_transfer_preflight_router
from tests.application.workspace.test_aliased_odoo_transfer import (
    AliasedTransferFixture,
)
from tests.application.workspace.test_destination_matching import BINDING_HASH
from tests.application.workspace.test_scoped_odoo_transfer import ScopedTransferFixture
from tests.application.workspace.test_transfer_review import _WorkspaceRepository
from tests.integration.web.test_scoped_transfer_load import _ScopedExecution
from tests.integration.web.test_transfer_load_routes import (
    _endpoint,
    _NoExecution,
    _Queries,
    _request,
)
from tests.support.identity import MutableLocalRequestIdentityResolver


def _approve_stored(fixture, store, project_id):
    match = store.put(project_id, fixture.match())
    matched = replace(fixture.workspace, destination_match_plan=match)
    order = TransferOrderService().build(matched, match, recorded_by="Data manager")
    ordered = replace(matched, transfer_order_plan=order)
    package = TransferReviewService().build(
        ordered,
        match,
        order,
        run_id=str(uuid4()),
        data_version_id=fixture.selection.data_version_id,
        built_by=LOCAL_ACTOR.identity,
    )
    approval = TransferReviewApproval.approve(
        package, approval_id=str(uuid4()), actor=LOCAL_ACTOR, approved_at=fixture.now
    )
    approved = replace(
        ordered, transfer_review_package=package, transfer_review_approval=approval
    )
    report = TransferPreflightService().build(
        approved, package, approval, match, match, recorded_by=LOCAL_ACTOR.identity
    )
    fixture.match_plan = match
    fixture.workspace = replace(approved, transfer_preflight_report=report)
    fixture.report = report
    fixture.calls.clear()


class _ProtectedAccess:
    def __init__(self, project_id, *, denied=False):
        self.project_id = project_id
        self.denied = denied
        self.calls = []

    def require(self, actor, capability, *, workspace_id):
        self.calls.append((actor, capability, workspace_id))
        if self.denied:
            raise AuthorizationError("Protected destination mappings are unavailable")
        return SimpleNamespace(project_id=self.project_id)


def _context(fixture, store, project_id, *, denied=False):
    repository = _WorkspaceRepository(fixture.workspace)
    access = _ProtectedAccess(project_id, denied=denied)
    execution = _ScopedExecution(fixture)
    protected = Mock(wraps=store)
    target_calls = []
    origin_calls = []

    def identity(*_args):
        target_calls.append("identity")
        return fixture.identity

    def origins(_workspace, *, dataset_id, **_kwargs):
        origin_calls.append(dataset_id)
        return None, fixture.origins[dataset_id]

    context = SimpleNamespace(
        identity_resolver=MutableLocalRequestIdentityResolver(),
        queries=_Queries(repository, fixture.selection, fixture.schema),
        workspace_states=WorkspaceStateService(
            repository, CapabilityAuthorizationPolicy()
        ),
        workspace_access=access,
        categorical_coverage=fixture,
        secret_store=MemorySecretStore(),
        read_identity_probe=identity,
        destination_match_reader=fixture.reader,
        destination_identity_aliases=protected,
        odoo_provenance=SimpleNamespace(read_current_origins=origins),
        execution=_NoExecution(),
        transfer_execution=execution,
        write_executor_factory=Mock(
            side_effect=AssertionError("No writer may be opened")
        ),
    )
    return context, repository, access, execution, protected, target_calls, origin_calls


class AliasedTransferLoadRouteTests(unittest.TestCase):
    def test_final_prepare_reopens_encrypted_rules_and_compiles_effective_parent(self):
        for major in (19, 20):
            for mode in ("LOCAL", "REMOTE"):
                with (
                    self.subTest(major=major, mode=mode),
                    TemporaryDirectory() as directory,
                ):
                    fixture = AliasedTransferFixture(major, mode, scoped=True)
                    project_id = str(uuid4())
                    store = ProtectedDestinationIdentityAliasStore(
                        ProtectedProjectEvidenceStore(directory, MemorySecretStore())
                    )
                    _approve_stored(fixture, store, project_id)
                    self.assertIsNone(fixture.match_plan.identity_alias_evidence)
                    context, repository, access, execution, protected, _, _ = _context(
                        fixture, store, project_id
                    )
                    with patch(
                        "impodo.web.routers.transfer_load.get_target_credential",
                        return_value=SimpleNamespace(
                            secret="fixture-key", binding_hash=BINDING_HASH
                        ),
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
                    protected.read.assert_called_once_with(
                        project_id, fixture.match_plan
                    )
                    self.assertEqual(
                        access.calls[0][1], Capability.PROTECTED_EVIDENCE_READ
                    )
                    self.assertTrue(
                        repository.workspace.transfer_preflight_report.ready
                    )
                    self.assertEqual(len(fixture.calls), 2)
                    children = tuple(
                        row
                        for row in execution.staged.rows
                        if row.target_model == "x.scope_record"
                    )
                    self.assertEqual(
                        children[1].business_scope[0].key, ("Reviewed parent B",)
                    )
                    original_trace = next(
                        row.source_identity
                        for row in ScopedTransferFixture(major, mode).compile().rows
                        if row.target_model == "x.scope_record" and row.source_row == 2
                    )
                    self.assertEqual(children[1].source_identity, original_trace)
                    parents = tuple(
                        row
                        for row in execution.staged.rows
                        if row.target_model == "x.scope_parent"
                    )
                    self.assertTrue(
                        all(
                            row.disposition == "UNCHANGED" and not row.fields
                            for row in parents
                        )
                    )
                    context.write_executor_factory.assert_not_called()

    def test_denied_protected_access_stops_both_routes_before_source_or_target_io(self):
        routes = (
            (
                build_transfer_load_router,
                "prepare_transfer_load",
                "transfer_load",
                "_render_transfer",
            ),
            (
                build_transfer_preflight_router,
                "run_transfer_preflight",
                "transfer_preflight",
                "_render_preflight",
            ),
        )
        for builder, endpoint, module, renderer in routes:
            with self.subTest(endpoint=endpoint), TemporaryDirectory() as directory:
                fixture = AliasedTransferFixture(scoped=True)
                project_id = str(uuid4())
                store = ProtectedDestinationIdentityAliasStore(
                    ProtectedProjectEvidenceStore(directory, MemorySecretStore())
                )
                _approve_stored(fixture, store, project_id)
                context, _, _, execution, protected, targets, origins = _context(
                    fixture, store, project_id, denied=True
                )
                values = {
                    "csrf_token": "csrf",
                    "revision": str(fixture.workspace.revision),
                }
                if module == "transfer_load":
                    values["preflight_hash"] = fixture.report.content_hash
                with (
                    patch(
                        f"impodo.web.routers.{module}.{renderer}",
                        return_value=Response(status_code=422),
                    ),
                    patch.object(
                        fixture,
                        "source_key_tuples",
                        side_effect=AssertionError("No source scan"),
                    ),
                ):
                    response = asyncio.run(
                        _endpoint(builder(context), endpoint)(
                            _request("/check", values), fixture.workspace.workspace_id
                        )
                    )
                self.assertEqual(response.status_code, 422)
                self.assertEqual(targets, [])
                self.assertEqual(origins, [])
                self.assertEqual(fixture.calls, [])
                protected.read.assert_not_called()
                self.assertIsNone(execution.staged)
                context.write_executor_factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
