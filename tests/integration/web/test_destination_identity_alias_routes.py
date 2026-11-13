"""Authorize, check, and protect exact reference mappings through matching POST."""

import asyncio
import unittest
from dataclasses import replace
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fastapi import HTTPException
from fastapi.responses import HTMLResponse

from impodo.adapters.protected_destination_create_fields import (
    ProtectedDestinationCreateFieldStore,
)
from impodo.adapters.protected_destination_identity_aliases import (
    ProtectedDestinationIdentityAliasStore,
)
from impodo.adapters.protected_project_evidence_store import (
    ProtectedProjectEvidenceStore,
)
from impodo.domain.shared.access import (
    AuthorizationError,
    Capability,
    CapabilityAuthorizationPolicy,
)
from impodo.domain.workspace.errors import WorkspaceError
from impodo.domain.workspace.workbench import WorkspaceStateService
from impodo.web.presenters.identity_alias_forms import identity_alias_form_rows
from impodo.web.routers.destination_matching import build_destination_matching_router
from tests.application.workspace.test_aliased_odoo_transfer import (
    AliasedTransferFixture,
)
from tests.application.workspace.test_destination_matching import BINDING_HASH
from tests.application.workspace.test_transfer_review import _WorkspaceRepository
from tests.integration.protected_evidence.test_destination_create_fields import _Secrets
from tests.integration.web.test_transfer_load_routes import (
    _endpoint,
    _Queries,
    _request,
)
from tests.support.identity import MutableLocalRequestIdentityResolver

PROJECT = "00000000-0000-0000-0000-000000000091"


class _Access:
    def __init__(self):
        self.denied = set()
        self.calls = []

    def resolve(self, workspace_id, *, actor, capability):
        self.calls.append(capability)
        if capability in self.denied:
            raise AuthorizationError("Denied protected mapping access")
        return SimpleNamespace(project_id=PROJECT)


class DestinationIdentityAliasRouteTests(unittest.TestCase):
    def _context(self, fixture, directory):
        repository = _WorkspaceRepository(fixture.workspace)
        queries = _Queries(repository, fixture.selection, fixture.schema)
        queries.get_current_odoo_relationship_scope = lambda _: None
        protected = ProtectedProjectEvidenceStore(directory, _Secrets())
        source = Mock(wraps=fixture)
        context = SimpleNamespace(
            identity_resolver=MutableLocalRequestIdentityResolver(),
            queries=queries,
            workspace_states=WorkspaceStateService(
                repository, CapabilityAuthorizationPolicy()
            ),
            categorical_coverage=source,
            secret_store=None,
            workspace_access=_Access(),
            destination_identity_aliases=ProtectedDestinationIdentityAliasStore(
                protected
            ),
            destination_create_fields=ProtectedDestinationCreateFieldStore(protected),
            read_identity_probe=lambda *_: fixture.identity,
            destination_match_reader=fixture.reader,
            odoo_provenance=SimpleNamespace(
                read_current_origins=lambda _, *, dataset_id, **__: (
                    None,
                    fixture.origins[dataset_id],
                )
            ),
        )
        return context, repository, source

    @staticmethod
    def _entries(fixture, revision=1):
        return [
            ("csrf_token", "csrf"),
            ("revision", str(revision)),
            ("match_key", f"{fixture.child.dataset_id}::product-code"),
            ("match_key", f"{fixture.parent.dataset_id}::uom-name"),
            ("destination_handling", f"{fixture.child.dataset_id}::transfer"),
            ("destination_handling", f"{fixture.parent.dataset_id}::reuse_only"),
            ("identity_alias_1_source_0", "Parent A"),
            ("identity_alias_1_target_0", "Reviewed parent A"),
        ]

    def _post(self, context, fixture, entries):
        credential = SimpleNamespace(secret="fixture-key", binding_hash=BINDING_HASH)
        with patch(
            "impodo.web.routers.destination_matching.get_target_credential",
            return_value=credential,
        ):
            return asyncio.run(
                _endpoint(
                    build_destination_matching_router(context),
                    "check_destination_matching",
                )(
                    _request("/destination-matching", entries),
                    fixture.workspace.workspace_id,
                )
            )

    def test_new_mapping_is_checked_and_encrypted_for_both_majors_and_modes(self):
        for major in (19, 20):
            for mode in ("LOCAL", "REMOTE"):
                with (
                    self.subTest(major=major, mode=mode),
                    TemporaryDirectory() as directory,
                ):
                    fixture = AliasedTransferFixture(major, mode)
                    fixture.targets["x.scope_parent"] = (
                        fixture.targets["x.scope_parent"][0],
                        replace(
                            fixture.targets["x.scope_parent"][1],
                            values={"name": "Parent B"},
                        ),
                    )
                    context, repository, _ = self._context(fixture, directory)
                    response = self._post(
                        context,
                        fixture,
                        self._entries(fixture, fixture.workspace.revision),
                    )
                    self.assertEqual(response.status_code, 303)
                    plan = repository.workspace.destination_match_plan
                    self.assertTrue(plan.ready)
                    self.assertIsNone(plan.identity_alias_evidence)
                    self.assertNotIn("Parent A", plan.to_json())
                    self.assertNotIn("Reviewed parent A", plan.to_json())
                    evidence = context.destination_identity_aliases.read(PROJECT, plan)
                    self.assertEqual(
                        evidence.values[0].aliases[0].target_value, "Reviewed parent A"
                    )
                    self.assertIn(
                        Capability.PROTECTED_EVIDENCE_READ,
                        context.workspace_access.calls,
                    )
                    self.assertIn(
                        Capability.PROTECTED_EVIDENCE_MANAGE,
                        context.workspace_access.calls,
                    )
                    saved_hash = plan.content_hash
                    checked_reads = len(fixture.calls)
                    entries = self._entries(fixture, repository.workspace.revision)[:-2]
                    with patch(
                        "impodo.web.routers.destination_matching._render_matching",
                        return_value=HTMLResponse("incomplete form", status_code=422),
                    ) as render:
                        incomplete = self._post(context, fixture, entries)
                    self.assertEqual(incomplete.status_code, 422)
                    self.assertIn("Reload this page", render.call_args.kwargs["error"])
                    self.assertEqual(
                        repository.workspace.destination_match_plan.content_hash,
                        saved_hash,
                    )
                    self.assertEqual(len(fixture.calls), checked_reads)
                    current_form = [
                        *self._entries(fixture, repository.workspace.revision),
                        ("identity_alias_1_source_1", ""),
                        ("identity_alias_1_target_1", ""),
                    ]
                    repeated = self._post(context, fixture, current_form)
                    self.assertEqual(repeated.status_code, 303)
                    self.assertEqual(
                        repository.workspace.destination_match_plan.identity_alias_bindings[
                            0
                        ].binding.alias_count,
                        1,
                    )

    def test_denied_new_mapping_reads_no_source_keys_target_or_protected_artifact(self):
        for denied in (
            Capability.PROTECTED_EVIDENCE_READ,
            Capability.PROTECTED_EVIDENCE_MANAGE,
        ):
            with self.subTest(denied=denied), TemporaryDirectory() as directory:
                fixture = AliasedTransferFixture()
                context, repository, source = self._context(fixture, directory)
                context.workspace_access.denied.add(denied)
                context.destination_identity_aliases.read = Mock(
                    side_effect=AssertionError("No protected read")
                )
                context.destination_identity_aliases.put = Mock(
                    side_effect=AssertionError("No protected write")
                )
                with self.assertRaises(HTTPException) as rejected:
                    self._post(
                        context,
                        fixture,
                        self._entries(fixture, fixture.workspace.revision),
                    )
                self.assertEqual(rejected.exception.status_code, 403)
                source.source_identity_counts.assert_not_called()
                source.source_key_tuples.assert_not_called()
                self.assertEqual(fixture.calls, [])
                self.assertIsNone(repository.workspace.destination_match_plan)

    def test_failed_target_check_preserves_draft_without_replacing_saved_plan(self):
        with TemporaryDirectory() as directory:
            fixture = AliasedTransferFixture()
            fixture.targets["x.scope_parent"] = (
                fixture.targets["x.scope_parent"][0],
                replace(
                    fixture.targets["x.scope_parent"][1], values={"name": "Parent B"}
                ),
            )
            context, repository, _ = self._context(fixture, directory)
            entries = self._entries(fixture, fixture.workspace.revision)
            entries[-1] = ("identity_alias_1_target_0", "Missing destination")
            with patch(
                "impodo.web.routers.destination_matching._render_matching",
                return_value=HTMLResponse("draft", status_code=422),
            ) as render:
                response = self._post(context, fixture, entries)
            self.assertEqual(response.status_code, 422)
            self.assertEqual(
                render.call_args.kwargs["identity_alias_drafts"][
                    fixture.parent.dataset_id
                ][0],
                ("Parent A", "Missing destination"),
            )
            self.assertIsNone(repository.workspace.destination_match_plan)
            drafts = render.call_args.kwargs["identity_alias_drafts"][
                fixture.parent.dataset_id
            ]
            self.assertEqual(len(identity_alias_form_rows(drafts=drafts)), 1)
            # The failed form retains the same bounded input names for retry.
            retried = self._post(
                context, fixture, self._entries(fixture, fixture.workspace.revision)
            )
            self.assertEqual(retried.status_code, 303)

    def test_missing_artifact_can_be_explicitly_replaced_without_old_decryption(self):
        with TemporaryDirectory() as directory:
            fixture = AliasedTransferFixture()
            fixture.targets["x.scope_parent"] = (
                fixture.targets["x.scope_parent"][0],
                replace(
                    fixture.targets["x.scope_parent"][1], values={"name": "Parent B"}
                ),
            )
            context, repository, _ = self._context(fixture, directory)
            first = self._post(
                context, fixture, self._entries(fixture, fixture.workspace.revision)
            )
            self.assertEqual(first.status_code, 303)
            old_hash = repository.workspace.destination_match_plan.content_hash
            entries = self._entries(fixture, repository.workspace.revision)
            with (
                patch.object(
                    context.destination_identity_aliases,
                    "read",
                    side_effect=WorkspaceError("Missing protected artifact"),
                ) as read,
                patch(
                    "impodo.web.routers.destination_matching._render_matching",
                    return_value=HTMLResponse("repair", status_code=422),
                ),
            ):
                rejected = self._post(context, fixture, entries)
                self.assertEqual(rejected.status_code, 422)
                self.assertEqual(
                    repository.workspace.destination_match_plan.content_hash, old_hash
                )
                read.reset_mock()
                repaired = self._post(
                    context, fixture, [*entries, ("replace_identity_aliases", "yes")]
                )
                self.assertEqual(repaired.status_code, 303)
                read.assert_not_called()
            self.assertTrue(repository.workspace.destination_match_plan.ready)

    def test_half_filled_pair_fails_before_source_or_target_checks(self):
        with TemporaryDirectory() as directory:
            fixture = AliasedTransferFixture()
            context, repository, source = self._context(fixture, directory)
            entries = self._entries(fixture, fixture.workspace.revision)
            entries[-1] = ("identity_alias_1_target_0", "")
            with patch(
                "impodo.web.routers.destination_matching._render_matching",
                return_value=HTMLResponse("draft", status_code=422),
            ) as render:
                response = self._post(context, fixture, entries)
            self.assertEqual(response.status_code, 422)
            self.assertEqual(
                render.call_args.kwargs["identity_alias_drafts"][
                    fixture.parent.dataset_id
                ][0],
                ("Parent A", ""),
            )
            source.source_identity_counts.assert_not_called()
            self.assertEqual(fixture.calls, [])
            self.assertIsNone(repository.workspace.destination_match_plan)


if __name__ == "__main__":
    unittest.main()
