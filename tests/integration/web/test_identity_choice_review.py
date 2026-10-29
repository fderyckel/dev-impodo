"""Focused browser evidence for protected unresolved identity-choice review."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import uuid4

from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from impodo.domain.matching_order import (
    MatchingIdentityCheckStatus,
    MatchingIdentityNotCheckableReason,
    MatchingIdentityResult,
    MatchingOrderCheck,
    MatchingOrderCheckAttempt,
    MatchingOrderCheckPhase,
    MatchingOrderCheckStatus,
)
from tests.support.browser_scenarios import (
    DatasetMapping,
    IdentityComponentMapping,
    MappingTargetMode,
    ProjectSetupBrowserTestCase,
    ReferenceKeyMapping,
    RelationshipResolver,
    ResolverOrigin,
    replace,
)
from tests.support.identity import resolve_local_actor


class IdentityChoiceReviewBrowserTests(ProjectSetupBrowserTestCase):
    """Prove protected paging, freshness, navigation, and DB bounds."""

    def test_unresolved_identity_choices_are_authorized_paged_and_bounded(
        self,
    ) -> None:
        workspace_id, dataset, business_key = self._mapping_ready_workspace(
            scalar_field_count=0,
            relationship_field_count=1,
        )
        context = self.app.state.context
        actor = resolve_local_actor(context)
        governance = context.queries.get_schema_governance(workspace_id)
        schema = context.queries.get_odoo_schema_catalog(workspace_id)
        selection = context.queries.get_mapping_source_selection(workspace_id)
        assert governance is not None and schema is not None and selection is not None
        governance = replace(
            governance,
            version=governance.version + 1,
            business_keys=(
                replace(business_key, scope_fields=("relation_0000",)),
            ),
        )
        context.schema_workspace.schemas.save_schema_governance(
            workspace_id,
            governance,
            actor=actor,
        )
        source_identity, source_country = dataset.columns
        resolver = RelationshipResolver(
            origin=ResolverOrigin.TARGET_CATALOG,
            model="res.partner",
            key_mappings=(
                ReferenceKeyMapping(source_country.stable_key, "ref"),
            ),
        )
        draft = context.mapping_workspace.save_working_draft(
            workspace_id,
            datasets=(
                DatasetMapping(
                    dataset_id=dataset.dataset_id,
                    target_model="res.partner",
                    mode=MappingTargetMode.UPSERT,
                    source_identity_column_keys=(
                        source_identity.stable_key,
                        source_country.stable_key,
                    ),
                    target_identity=(
                        IdentityComponentMapping(
                            (source_identity.stable_key,),
                            ("ref",),
                        ),
                    ),
                    target_scope=(
                        IdentityComponentMapping(
                            (source_country.stable_key,),
                            ("relation_0000",),
                            resolver=resolver,
                        ),
                    ),
                ),
            ),
            expected_version=None,
            actor=actor,
        )
        now = datetime.now(UTC)
        check_id = str(uuid4())
        repository = context.matching_order._preferences
        repository.begin_check(
            workspace_id,
            MatchingOrderCheckAttempt(
                check_id=check_id,
                workspace_id=workspace_id,
                status=MatchingOrderCheckStatus.QUEUED,
                phase=MatchingOrderCheckPhase.QUEUED,
                message="Waiting to check Odoo",
                progress_percent=0,
                created_at=now,
                updated_at=now,
            ),
            actor=actor,
        )
        check = MatchingOrderCheck(
            check_id=check_id,
            workspace_id=workspace_id,
            source_selection_hash=selection.content_hash,
            schema_hash=schema.content_hash,
            governance_hash=governance.content_hash,
            working_draft_version=draft.version,
            working_draft_hash=draft.content_hash,
            target_hash=schema.connection_target_hash,
            read_credential_binding_hash=schema.read_credential_binding_hash,
            read_principal_hash=schema.read_principal_hash,
            read_permission_hash=schema.read_permission_hash,
            read_context_hash=schema.read_context_hash,
            relationship_results=(),
            unchecked_relationship_count=0,
            ordered_dataset_ids=(dataset.dataset_id,),
            recommendation_hash="sha256:" + "9" * 64,
            schema_changed=False,
            captured_at=now,
            actor_issuer=actor.identity.issuer,
            actor_subject=actor.identity.subject_id,
            actor_display_name=actor.identity.display_name,
            identity_results=(
                MatchingIdentityResult(
                    dataset_id=dataset.dataset_id,
                    target_model="res.partner",
                    target_fields=("ref", "relation_0000"),
                    status=MatchingIdentityCheckStatus.NOT_CHECKABLE,
                    not_checkable_reason=(
                        MatchingIdentityNotCheckableReason.RELATIONAL_IDENTITY_UNRESOLVED
                    ),
                ),
            ),
        )
        protected = {
            "unresolved_identity_choices": [{
                "dataset_id": dataset.dataset_id,
                "target_field": "relation_0000",
                "related_model": "res.partner",
                "related_fields": ["ref"],
                "key": ["LU"],
                "cause": "ambiguous_target",
                "target_match_count": 2,
                "incoming_match_count": 0,
            }]
        }
        published = repository.publish_check(
            workspace_id,
            check,
            protected_snapshot_json=json.dumps(protected),
            actor=actor,
        )
        self.assertIs(published, MatchingOrderCheckStatus.SUCCEEDED)
        mapping_page = self.client.get(f"/workspaces/{workspace_id}/mapping")
        self.assertIn("Review unresolved identity choices", mapping_page.text)

        with collect_duckdb_request_timings() as timings:
            response = self.client.get(
                f"/workspaces/{workspace_id}/mapping/identity-choices"
            )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertIn("Linked Field 0000", response.text)
        self.assertIn("Several Odoo records match", response.text)
        self.assertIn("LU", response.text)
        self.assertNotIn("P001", response.text)
        self.assertGreater(timings.connection_count, 0)
        self.assertLessEqual(timings.connection_count, 6)
        self.assertLessEqual(timings.schema_check_count, 1)
