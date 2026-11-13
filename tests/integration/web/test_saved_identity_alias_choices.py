"""Keep remembered reference choices encrypted, authorized, and repairable."""

import json
import unittest
from dataclasses import replace
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from fastapi import HTTPException
from starlette.datastructures import FormData

from impodo.adapters.protected_destination_identity_aliases import (
    ProtectedDestinationIdentityAliasStore,
)
from impodo.adapters.protected_project_evidence_store import (
    ProtectedProjectEvidenceStore,
)
from impodo.domain.shared.access import LOCAL_ACTOR, AuthorizationError, Capability
from impodo.domain.workspace.errors import WorkspaceError
from impodo.web.composition.saved_odoo_restore import saved_identity_alias_rules
from impodo.web.presenters.identity_alias_forms import identity_alias_form_rows
from impodo.web.routers.saved_odoo_data import _page_data, _profile_from_form
from tests.integration.protected_evidence.test_destination_create_fields import _Secrets
from tests.integration.web.test_destination_identity_alias_routes import _Access
from tests.integration.web.test_transfer_load_routes import _request
from tests.support.identity import MutableLocalRequestIdentityResolver

HASH = "sha256:" + "a" * 64
OTHER_HASH = "sha256:" + "b" * 64


class _Repository:
    def __init__(self):
        self.profile = None

    def get_restore_profile(self, project_id, saved_data_id, profile_id):
        assert self.profile.profile_id == profile_id
        return self.profile


class SavedIdentityAliasChoiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.repository = _Repository()
        self.definition = SimpleNamespace(
            project_id=str(uuid4()),
            saved_data_id=str(uuid4()),
            source_workspace_id=str(uuid4()),
            source_target_hash=HASH,
            source_company_hash=HASH,
            odoo_major=20,
            latest_version=1,
            config_json="{}",
        )
        store = ProtectedDestinationIdentityAliasStore(
            ProtectedProjectEvidenceStore(self.temporary.name, _Secrets())
        )
        self.context = SimpleNamespace(
            saved_odoo_data=SimpleNamespace(repository=self.repository),
            destination_identity_aliases=store,
            workspace_access=_Access(),
            identity_resolver=MutableLocalRequestIdentityResolver(),
        )
        # The route access stub uses this fixed project ownership.
        from tests.integration.web.test_destination_identity_alias_routes import PROJECT

        self.definition.project_id = PROJECT

    def _values(self, rules=(), definition=None, rebind=False):
        aliases = rules[0].aliases if rules else ()
        return {
            "definition": definition or self.definition,
            "_actor": LOCAL_ACTOR,
            "_identity_alias_rules": rules,
            "identity_alias_rebind_required": rebind,
            "key_rows": [
                {
                    "model": "x.reference",
                    "selected": ("name",),
                    "fields": ({"name": "name"},),
                    "scope_fields": (),
                    "identity_alias_prefix": "identity_alias__x.reference",
                    "identity_alias_rows": identity_alias_form_rows(aliases),
                    "identity_alias_eligible": True,
                }
            ],
            "field_rows": [{"model": "x.reference", "name": "name"}],
            "default_rows": (),
        }

    def _form(self, profile=None, **changes):
        return FormData(
            {
                "profile_id": profile.profile_id if profile else "",
                "profile_hash": profile.content_hash if profile else "",
                "odoo_connection_mode": "REMOTE",
                "odoo_base_url": "https://destination.example.test",
                "odoo_database": "test",
                "destination_company_name": "Test company",
                "policy_default": "use_saved",
                "keys__x.reference": "name",
                "identity_alias__x.reference_source_0": "Source reference",
                "identity_alias__x.reference_target_0": "Reviewed destination",
                **changes,
            }
        )

    def _profile(self, form, values):
        return _profile_from_form(
            self.context,
            self.definition.project_id,
            self.definition.saved_data_id,
            form,
            values,
        )

    def test_profile_stores_only_protected_reference_and_reuses_unchanged_rules(self):
        profile = self._profile(self._form(), self._values())
        settings = json.loads(profile.settings_json)
        self.assertIn("identity_alias_rules", settings)
        self.assertNotIn("Source reference", profile.settings_json)
        self.assertNotIn("Reviewed destination", profile.settings_json)
        rules = saved_identity_alias_rules(
            self.context, profile, self.definition, actor=LOCAL_ACTOR
        )
        self.assertEqual(rules[0].aliases[0].target_value, "Reviewed destination")
        self.repository.profile = profile
        with patch.object(
            self.context.destination_identity_aliases,
            "put_rules",
            side_effect=AssertionError("Unchanged rules must reuse the artifact"),
        ):
            repeated = self._profile(
                self._form(
                    profile,
                    **{
                        "identity_alias__x.reference_source_1": "",
                        "identity_alias__x.reference_target_1": "",
                    },
                ),
                self._values(rules),
            )
        self.assertEqual(
            json.loads(repeated.settings_json)["identity_alias_rules"],
            settings["identity_alias_rules"],
        )

    def test_incomplete_saved_mapping_form_requires_a_current_review(self):
        profile = self._profile(self._form(), self._values())
        self.repository.profile = profile
        rules = saved_identity_alias_rules(
            self.context, profile, self.definition, actor=LOCAL_ACTOR
        )
        with (
            patch.object(
                self.context.destination_identity_aliases,
                "put_rules",
                side_effect=AssertionError("No incomplete form publication"),
            ),
            self.assertRaisesRegex(WorkspaceError, "incomplete.*Reload"),
        ):
            self._profile(self._form(profile), self._values(rules))

    def test_stale_profile_is_rejected_before_protected_reads_or_writes(self):
        profile = self._profile(self._form(), self._values())
        self.repository.profile = replace(profile, revision=profile.revision + 1)
        self.context.workspace_access.calls.clear()
        with (
            patch.object(
                self.context.destination_identity_aliases,
                "put_rules",
                side_effect=AssertionError("No stale write"),
            ),
            self.assertRaisesRegex(WorkspaceError, "changed"),
        ):
            self._profile(self._form(profile), self._values())
        self.assertEqual(self.context.workspace_access.calls, [])

    def test_replacement_recovers_from_changed_source_owner_without_old_decryption(
        self,
    ):
        profile = self._profile(self._form(), self._values())
        self.repository.profile = profile
        definition = SimpleNamespace(
            **{**vars(self.definition), "source_company_hash": OTHER_HASH}
        )
        values = self._values(definition=definition, rebind=True)
        with self.assertRaisesRegex(WorkspaceError, "Confirm"):
            self._profile(self._form(profile), values)
        with patch.object(
            self.context.destination_identity_aliases,
            "read_rules",
            side_effect=AssertionError("Old owner must not be read"),
        ):
            changed = self._profile(
                self._form(profile, replace_identity_aliases="yes"), values
            )
        old_ref = json.loads(profile.settings_json)["identity_alias_rules"]
        new_ref = json.loads(changed.settings_json)["identity_alias_rules"]
        self.assertNotEqual(old_ref["owner_key"], new_ref["owner_key"])
        self.assertEqual(
            saved_identity_alias_rules(
                self.context, changed, definition, actor=LOCAL_ACTOR
            )[0].model,
            "x.reference",
        )

    def test_denied_new_alias_cannot_read_or_save_a_protected_rule(self):
        for capability in (
            Capability.PROTECTED_EVIDENCE_READ,
            Capability.PROTECTED_EVIDENCE_MANAGE,
        ):
            with self.subTest(capability=capability):
                self.context.workspace_access.denied = {capability}
                with (
                    patch.object(
                        self.context.destination_identity_aliases,
                        "put_rules",
                        side_effect=AssertionError("No protected write"),
                    ),
                    self.assertRaises(AuthorizationError),
                ):
                    self._profile(self._form(), self._values())

    def test_wrong_saved_set_is_rejected_before_authorization_or_decryption(self):
        profile = self._profile(self._form(), self._values())
        self.context.workspace_access.calls.clear()
        other = SimpleNamespace(
            **{**vars(self.definition), "saved_data_id": str(uuid4())}
        )
        with (
            patch.object(
                self.context.destination_identity_aliases,
                "read_rules",
                side_effect=AssertionError("No cross-set read"),
            ),
            self.assertRaisesRegex(WorkspaceError, "another saved data set"),
        ):
            saved_identity_alias_rules(self.context, profile, other, actor=LOCAL_ACTOR)
        self.assertEqual(self.context.workspace_access.calls, [])

    def _page_context(self, profile):
        version = SimpleNamespace(
            workspace_id=self.definition.source_workspace_id, version_number=1
        )
        repository = self.repository
        repository.profile = profile
        repository.list_definitions = lambda _: (self.definition,)
        repository.list_versions = lambda *_: (version,)
        repository.list_restore_profiles = lambda *_: (profile,)
        repository.get_version = lambda *_: version
        self.context.saved_odoo_data.get = lambda *_args, **_kwargs: self.definition
        return SimpleNamespace(project_id=self.definition.project_id)

    def test_page_rejects_stale_client_hash_before_decrypting_latest_rules(self):
        profile = self._profile(self._form(), self._values())
        project = self._page_context(profile)
        request = _request("/saved-odoo-data", {})
        with (
            patch(
                "impodo.web.routers.saved_odoo_data._visible_project",
                return_value=project,
            ),
            patch.object(
                self.context.destination_identity_aliases,
                "read_rules",
                side_effect=AssertionError("No stale read"),
            ),
            self.assertRaises(HTTPException) as rejected,
        ):
            _page_data(
                request,
                self.context,
                project.project_id,
                selected=self.definition.saved_data_id,
                profile_id=profile.profile_id,
                expected_profile_hash=OTHER_HASH,
                alias_command=True,
            )
        self.assertEqual(rejected.exception.status_code, 409)

    def test_removed_reference_model_offers_explicit_replacement_and_clear(self):
        profile = self._profile(self._form(), self._values())
        project = self._page_context(profile)
        request = _request("/saved-odoo-data", {})
        row = {
            **self._values()["key_rows"][0],
            "model": "x.other",
            "identity_alias_prefix": "identity_alias__x.other",
        }
        with (
            patch(
                "impodo.web.routers.saved_odoo_data._visible_project",
                return_value=project,
            ),
            patch(
                "impodo.web.routers.saved_odoo_data._owned_source",
                return_value=SimpleNamespace(
                    workspace_id=self.definition.source_workspace_id
                ),
            ),
            patch(
                "impodo.web.routers.saved_odoo_data._source_rows",
                return_value=((), (row,), {}, ()),
            ),
        ):
            values = _page_data(
                request,
                self.context,
                project.project_id,
                selected=self.definition.saved_data_id,
                profile_id=profile.profile_id,
            )
        self.assertTrue(values["identity_alias_rebind_required"])
        self.assertIn("record types", values["identity_alias_problem"])
        form = self._form(
            profile,
            replace_identity_aliases="yes",
            **{
                "identity_alias__x.reference_source_0": "",
                "identity_alias__x.reference_target_0": "",
                "identity_alias__x.other_source_0": "",
                "identity_alias__x.other_target_0": "",
            },
        )
        cleared = self._profile(form, values)
        self.assertNotIn("identity_alias_rules", json.loads(cleared.settings_json))


if __name__ == "__main__":
    unittest.main()
