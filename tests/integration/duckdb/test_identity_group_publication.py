"""Reject partial or relabelled duplicate membership before making it current."""

import json
import unittest
from dataclasses import asdict, replace
from datetime import UTC, datetime
from unittest.mock import patch
from uuid import uuid4

from impodo.application.workspace.mapping.identity_group_review import (
    identity_duplicate_choices,
)
from impodo.domain.mapping.contracts import MappingTargetMode
from impodo.domain.matching_order import (
    MatchingOrderCheck,
    MatchingOrderCheckAttempt,
    MatchingOrderCheckPhase,
    MatchingOrderCheckStatus,
    classify_indexed_matching_identity,
    index_matching_identity_keys,
)
from impodo.domain.shared.access import LOCAL_ACTOR
from impodo.domain.workspace.errors import WorkspaceError
from tests.integration.duckdb import (
    test_matching_order_repository as repository_fixtures,
)


class IdentityGroupPublicationTests(unittest.TestCase):
    def test_incomplete_or_relabelled_group_cannot_publish_a_current_check(self):
        fixture = repository_fixtures.MatchingOrderRepositoryTests("runTest")
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        now = datetime.now(UTC)
        index = index_matching_identity_keys(
            target_fields=("x_code",), source_keys=(("DUP",),) * 3, target_keys=()
        )
        result = classify_indexed_matching_identity(
            index,
            dataset_id="dataset:a",
            target_model="x.model",
            mode=MappingTargetMode.UPSERT,
            on_existing=None,
        )
        choices = identity_duplicate_choices(
            index,
            dataset_id="dataset:a",
            target_model="x.model",
            display_fields=("x_code",),
            display_keys=(("DUP",),) * 3,
        )
        check = MatchingOrderCheck(
            check_id=str(uuid4()),
            workspace_id=fixture.workspace_id,
            source_selection_hash=fixture.selection.content_hash,
            schema_hash=fixture.schema.content_hash,
            governance_hash=fixture.governance.content_hash,
            working_draft_version=fixture.draft.version,
            working_draft_hash=fixture.draft.content_hash,
            target_hash=fixture.schema.connection_target_hash,
            read_credential_binding_hash=fixture.schema.read_credential_binding_hash,
            read_principal_hash=fixture.schema.read_principal_hash,
            read_permission_hash=fixture.schema.read_permission_hash,
            read_context_hash=fixture.schema.read_context_hash,
            relationship_results=(),
            unchecked_relationship_count=0,
            ordered_dataset_ids=("dataset:a", "dataset:b"),
            recommendation_hash="sha256:" + "9" * 64,
            schema_changed=False,
            captured_at=now,
            actor_issuer=LOCAL_ACTOR.identity.issuer,
            actor_subject=LOCAL_ACTOR.identity.subject_id,
            actor_display_name=LOCAL_ACTOR.identity.display_name,
            identity_results=(result,),
            identity_review_version=1,
        )
        fixture.repository.begin_check(
            fixture.workspace_id,
            MatchingOrderCheckAttempt(
                check.check_id,
                fixture.workspace_id,
                MatchingOrderCheckStatus.QUEUED,
                MatchingOrderCheckPhase.QUEUED,
                "Waiting to check Odoo",
                0,
                now,
                now,
            ),
            actor=LOCAL_ACTOR,
        )

        def payload(rows):
            return json.dumps(
                {
                    "identity_review_version": 1,
                    "duplicate_identity_choices": [
                        {
                            name: value
                            for name, value in asdict(row).items()
                            if name != "ordinal"
                        }
                        for row in rows
                    ],
                }
            )

        for invalid in (
            choices[:-1],
            (replace(choices[0], key=("FORGED",)), *choices[1:]),
        ):
            with self.assertRaises(WorkspaceError):
                fixture.repository.publish_check(
                    fixture.workspace_id,
                    check,
                    protected_snapshot_json=payload(invalid),
                    actor=LOCAL_ACTOR,
                )
            self.assertIsNone(
                fixture.repository.get_current_check(fixture.workspace_id)
            )
            with fixture.database._connect(fixture.database_path) as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM matching_order_protected_identity_choice"
                    ).fetchone(),
                    (0,),
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM matching_order_check"
                    ).fetchone(),
                    (0,),
                )
        insert = fixture.repository._insert_identity_choices

        def fail_after_insert(*args):
            insert(*args)
            raise WorkspaceError("injected after membership insertion")

        with patch.object(
            fixture.repository,
            "_insert_identity_choices",
            side_effect=fail_after_insert,
        ):
            with self.assertRaises(WorkspaceError):
                fixture.repository.publish_check(
                    fixture.workspace_id,
                    check,
                    protected_snapshot_json=payload(choices),
                    actor=LOCAL_ACTOR,
                )
        self.assertIsNone(fixture.repository.get_current_check(fixture.workspace_id))
        with fixture.database._connect(fixture.database_path) as connection:
            for table in (
                "matching_order_check",
                "matching_order_protected_snapshot",
                "matching_order_protected_identity_choice",
            ):
                self.assertEqual(
                    connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone(), (0,)
                )
        self.assertEqual(
            fixture.repository.publish_check(
                fixture.workspace_id,
                check,
                protected_snapshot_json=payload(choices),
                actor=LOCAL_ACTOR,
            ),
            MatchingOrderCheckStatus.SUCCEEDED,
        )
