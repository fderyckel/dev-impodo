"""Prove source reset admission preserves the last immutable clean version.

These tests use the saved-data orchestration fixture with explicit immutable
source proofs. Registry integration separately proves atomic generation CAS.
"""

from __future__ import annotations

import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

from impodo.domain.odoo.company import OdooCompanySelection
from impodo.domain.odoo_capture import OdooCaptureRole
from impodo.domain.project.foundation import MigrationConflictError
from impodo.domain.serialization import content_hash
from impodo.domain.shared.access import LOCAL_ACTOR, AuthorizationError
from impodo.domain.shared.models import target_identity_hash
from impodo.domain.workspace.errors import WorkspaceError
from impodo.domain.workspace.workbench import OdooConnectionMode
from tests.application import test_odoo_saved_data_service as fixtures


class SavedOdooSourceRebindTests(unittest.TestCase):
    def _fixture(self, major: int, mode: OdooConnectionMode):
        fixture = fixtures.SavedOdooDataServiceTests(methodName="runTest")
        fixture._setup(major)
        fixture.state_rows[fixture.source_id] = replace(
            fixture.state_rows[fixture.source_id], odoo_connection_mode=mode
        )
        state = fixture.state_rows[fixture.source_id]
        fixture.target = target_identity_hash(
            connection_mode=mode.value,
            base_url=state.odoo_base_url,
            database=state.odoo_database,
        )
        fixture.company = OdooCompanySelection(fixture.target, 1, "Company A")
        fixture.state_rows[fixture.source_id] = replace(
            state, odoo_company=fixture.company
        )
        fixture.schema_rows[fixture.source_id] = fixture._schema(
            fixture.state_rows[fixture.source_id]
        )
        fixture.choice_rows[fixture.source_id] = (fixture._choice(fixture.source_id),)
        definition = fixture._create()
        first = fixture._refresh(definition)
        return fixture, fixture.definitions[definition.saved_data_id], first

    def _fresh_review(self, fixture, *, company_id=1):
        identity = str(uuid4())
        fixture.state_rows[identity] = replace(
            fixture.state_rows[fixture.source_id],
            workspace_id=identity,
            odoo_company=OdooCompanySelection(fixture.target, company_id, "Company A"),
        )
        fixture.owners[identity] = SimpleNamespace(
            project_id=fixture.project, data_version_id=str(uuid4())
        )
        fixture.schema_rows[identity] = fixture._schema(fixture.state_rows[identity])
        fixture.choice_rows[identity] = (fixture._choice(identity),)
        fixture._bind_source(identity)
        return identity

    def test_reset_blocks_refresh_and_retain_but_completed_replay_stays_read_only(self):
        for major in (19, 20):
            for mode in OdooConnectionMode:
                with self.subTest(major=major, mode=mode):
                    fixture, current, first = self._fixture(major, mode)
                    original_hash = first.content_hash
                    fixture.generation_hash = content_hash("recreated database")
                    fixture.factory.reset_mock()
                    fixture.repository.append_version.reset_mock()
                    with self.assertRaisesRegex(WorkspaceError, "generation changed"):
                        fixture._refresh(current)
                    with self.assertRaisesRegex(WorkspaceError, "generation changed"):
                        fixture.service.retain_current(
                            fixture.project,
                            current.saved_data_id,
                            actor=LOCAL_ACTOR,
                            expected_revision=current.revision,
                            operation_id=str(uuid4()),
                        )
                    self.assertEqual(
                        fixture._refresh(current, first.operation_id), first
                    )
                    self.assertEqual(first.content_hash, original_hash)
                    self.assertEqual(
                        fixture.definitions[current.saved_data_id], current
                    )
                    fixture.factory.assert_not_called()
                    fixture.repository.append_version.assert_not_called()

    def test_reviewed_rebind_keeps_historical_meaning_with_reused_company_id(self):
        for major in (19, 20):
            for mode in OdooConnectionMode:
                for company_id in (1, 9):
                    with self.subTest(major=major, mode=mode, company_id=company_id):
                        fixture, current, first = self._fixture(major, mode)
                        initial = fixture.history[(current.saved_data_id, 1)]
                        old_hash = first.content_hash
                        fixture.generation_hash = content_hash("recreated database")
                        identity = self._fresh_review(fixture, company_id=company_id)
                        rebound = fixture.service.rebind_source(
                            fixture.project,
                            current.saved_data_id,
                            identity,
                            actor=LOCAL_ACTOR,
                            expected_revision=current.revision,
                            all_fields=False,
                        )
                        self.assertEqual(rebound.latest_version, 1)
                        self.assertEqual(rebound.configuration_revision, 2)
                        self.assertEqual(
                            fixture.history[(current.saved_data_id, 1)], initial
                        )
                        self.assertEqual(first.content_hash, old_hash)
                        self.assertEqual(
                            fixture.configuration_bindings[(current.saved_data_id, 2)],
                            fixture.binding_rows[identity].content_hash,
                        )
                        second = fixture._refresh(rebound)
                        self.assertEqual(second.version_number, 2)
                        self.assertEqual(second.configuration_revision, 2)
                        self.assertIn(first, fixture.versions.values())
                        with self.assertRaisesRegex(MigrationConflictError, "changed"):
                            fixture.service.rebind_source(
                                fixture.project,
                                current.saved_data_id,
                                identity,
                                actor=LOCAL_ACTOR,
                                expected_revision=current.revision,
                                all_fields=False,
                            )

    def test_reset_during_remote_capture_never_accepts_or_promotes_replacement(self):
        for major in (19, 20):
            for mode in OdooConnectionMode:
                with self.subTest(major=major, mode=mode):
                    fixture, current, first = self._fixture(major, mode)
                    fixture.projections.accept_odoo_capture.reset_mock()
                    fixture.repository.append_version.reset_mock()

                    def reset_after_capture(
                        identity, gateway, *, actor, fixture=fixture
                    ):
                        publication = fixture._publish(identity, gateway, actor=actor)
                        fixture.generation_hash = content_hash("reset during download")
                        return publication

                    fixture.publication.publish.side_effect = reset_after_capture
                    with self.assertRaisesRegex(WorkspaceError, "generation changed"):
                        fixture._refresh(current)
                    fixture.projections.accept_odoo_capture.assert_not_called()
                    fixture.repository.append_version.assert_not_called()
                    self.assertEqual(
                        fixture.definitions[current.saved_data_id], current
                    )
                    self.assertIn(first, fixture.versions.values())

    def test_empty_root_cannot_be_hidden_by_nonempty_reference_artifacts(self):
        for major in (19, 20):
            for mode in OdooConnectionMode:
                with self.subTest(major=major, mode=mode):
                    fixture, current, first = self._fixture(major, mode)
                    fixture.generation_hash = content_hash("recreated database")
                    identity = self._fresh_review(fixture)
                    current = fixture.service.rebind_source(
                        fixture.project,
                        current.saved_data_id,
                        identity,
                        actor=LOCAL_ACTOR,
                        expected_revision=current.revision,
                        all_fields=False,
                    )

                    def empty_root(identity, gateway, *, actor, fixture=fixture):
                        publication = fixture._publish(identity, gateway, actor=actor)
                        root = fixture.manifest_rows[identity][0]
                        root.row_count = 0
                        reference_id = str(uuid4())
                        reference = SimpleNamespace(
                            dataset_id=reference_id,
                            model="x.reference",
                            row_count=3,
                            content_hash=content_hash("nonempty references"),
                            capture_finished_at=root.capture_finished_at,
                        )
                        fixture.choice_rows[identity] += (
                            SimpleNamespace(
                                model="x.reference",
                                capture_role=OdooCaptureRole.LINKED_ONLY,
                            ),
                        )
                        publication.source_selection.datasets += (
                            SimpleNamespace(dataset_id=reference_id),
                        )
                        fixture.manifest_rows[identity] += (reference,)
                        publication.manifests = fixture.manifest_rows[identity]
                        return publication

                    fixture.publication.publish.side_effect = empty_root
                    fixture.repository.append_version.reset_mock()
                    with self.assertRaisesRegex(
                        WorkspaceError, "refreshed source is empty"
                    ):
                        fixture._refresh(current)
                    fixture.repository.append_version.assert_not_called()
                    self.assertEqual(
                        fixture.definitions[current.saved_data_id].latest_version, 1
                    )
                    self.assertIn(first, fixture.versions.values())

    def test_unbound_configuration_and_unreviewed_or_captured_candidates_are_rejected(
        self,
    ):
        fixture, current, _first = self._fixture(20, OdooConnectionMode.REMOTE)
        fixture.configuration_bindings[
            (current.saved_data_id, current.configuration_revision)
        ] = None
        fixture.factory.reset_mock()
        with self.assertRaisesRegex(WorkspaceError, "no database-generation review"):
            fixture._refresh(current)
        fixture.factory.assert_not_called()
        identity = self._fresh_review(fixture)
        del fixture.binding_rows[identity]
        with self.assertRaisesRegex(WorkspaceError, "generation changed"):
            fixture.service.rebind_source(
                fixture.project,
                current.saved_data_id,
                identity,
                actor=LOCAL_ACTOR,
                expected_revision=current.revision,
                all_fields=False,
            )
        fixture._bind_source(identity)
        fixture.captured[identity] = Mock()
        with self.assertRaisesRegex(WorkspaceError, "fresh capture workspace"):
            fixture.service.rebind_source(
                fixture.project,
                current.saved_data_id,
                identity,
                actor=LOCAL_ACTOR,
                expected_revision=current.revision,
                all_fields=False,
            )
        fixture.repository.rebind_source_definition.assert_not_called()

    def test_rebind_cannot_change_endpoint_major_or_project_or_use_ordinary_revision(
        self,
    ):
        for major in (19, 20):
            for mode in OdooConnectionMode:
                with self.subTest(major=major, mode=mode):
                    fixture, current, _first = self._fixture(major, mode)
                    fixture.generation_hash = content_hash("recreated database")
                    identity = self._fresh_review(fixture)
                    with self.assertRaisesRegex(WorkspaceError, "generation changed"):
                        fixture.service.revise(
                            fixture.project,
                            current.saved_data_id,
                            identity,
                            actor=LOCAL_ACTOR,
                            expected_revision=current.revision,
                            label=current.label,
                            all_fields=False,
                        )
                    original = fixture.schema_rows[identity]
                    for changed in (
                        replace(
                            original,
                            connection_target_hash=content_hash("another database"),
                        ),
                        replace(
                            original, odoo_version=f"{20 if major == 19 else 19}.0"
                        ),
                    ):
                        fixture.schema_rows[identity] = changed
                        with self.assertRaisesRegex(
                            WorkspaceError, "database|Odoo major"
                        ):
                            fixture.service.rebind_source(
                                fixture.project,
                                current.saved_data_id,
                                identity,
                                actor=LOCAL_ACTOR,
                                expected_revision=current.revision,
                                all_fields=False,
                            )
                    fixture.schema_rows[identity] = original
                    reviewed_state = fixture.state_rows[identity]
                    fixture.state_rows[identity] = replace(
                        reviewed_state, odoo_base_url="https://different.example.test"
                    )
                    with self.assertRaisesRegex(WorkspaceError, "connection changed"):
                        fixture.service.rebind_source(
                            fixture.project,
                            current.saved_data_id,
                            identity,
                            actor=LOCAL_ACTOR,
                            expected_revision=current.revision,
                            all_fields=False,
                        )
                    fixture.state_rows[identity] = reviewed_state
                    fixture.owners[identity].project_id = str(uuid4())
                    with self.assertRaisesRegex(WorkspaceError, "another project"):
                        fixture.service.rebind_source(
                            fixture.project,
                            current.saved_data_id,
                            identity,
                            actor=LOCAL_ACTOR,
                            expected_revision=current.revision,
                            all_fields=False,
                        )
                    fixture.repository.rebind_source_definition.assert_not_called()

    def test_rebind_authorizes_before_candidate_evidence_or_generation_io(self):
        fixture, current, _first = self._fixture(19, OdooConnectionMode.LOCAL)
        fixture.access.require.side_effect = AuthorizationError(
            "Forbidden source review"
        )
        fixture.evidence.reset_mock()
        fixture.database_bindings.reset_mock()
        with self.assertRaisesRegex(AuthorizationError, "Forbidden"):
            fixture.service.rebind_source(
                fixture.project,
                current.saved_data_id,
                str(uuid4()),
                actor=LOCAL_ACTOR,
                expected_revision=current.revision,
                all_fields=False,
            )
        self.assertEqual(fixture.evidence.mock_calls, [])
        self.assertEqual(fixture.database_bindings.mock_calls, [])
        fixture.repository.rebind_source_definition.assert_not_called()


if __name__ == "__main__":
    unittest.main()
