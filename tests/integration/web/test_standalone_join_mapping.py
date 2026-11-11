"""Exercise pairwise authoring, source choices, and Match data recovery."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from impodo.domain.workspace.workbench import OdooConnectionMode
from tests.integration.web import test_exact_join_workflow as fixtures
from tests.support.browser_scenarios import POST_HEADERS, ProjectSetupBrowserTestCase
from tests.support.identity import resolve_local_actor


class StandaloneJoinMappingTests(ProjectSetupBrowserTestCase):
    _frozen_customer_workspace = (
        fixtures.ExactJoinBrowserTests._frozen_customer_workspace
    )
    _join_form = fixtures.ExactJoinBrowserTests._join_form
    _column = staticmethod(fixtures.ExactJoinBrowserTests._column)

    def _combined_mapping_workspace(self, major, mode):
        context = self.app.state.context
        template_id, _, key = self._mapping_ready_workspace(
            scalar_field_count=0,
            relationship_field_count=1,
            odoo_major=major,
            connection_mode=mode,
        )
        target = context.queries.get(template_id)
        template_schema = context.queries.get_odoo_schema_catalog(template_id)
        governance = context.queries.get_schema_governance(template_id)
        workspace_id, physical, by_name = self._frozen_customer_workspace()
        state = context.queries.get(workspace_id)
        context.workspace_states.update_target(
            workspace_id,
            actor=resolve_local_actor(context),
            expected_revision=state.revision,
            odoo_connection_mode=mode.value,
            odoo_base_url=target.odoo_base_url,
            odoo_database=target.odoo_database,
            intended_applications=(),
            intended_models=("res.partner",),
        )
        schema = replace(template_schema, workspace_id=workspace_id)
        context.schema_workspace.schemas.save_odoo_schema_catalog(
            workspace_id,
            schema,
            actor=resolve_local_actor(context),
        )
        context.schema_workspace.schemas.save_schema_governance(
            workspace_id,
            replace(governance, workspace_id=workspace_id),
            actor=resolve_local_actor(context),
        )
        form = self._join_form(physical, by_name)
        form["rule_id"] = str(uuid4())
        saved = self.client.post(
            f"/workspaces/{workspace_id}/derived-entities/join/save",
            data=form,
            headers=POST_HEADERS,
            follow_redirects=False,
        )
        self.assertEqual(saved.status_code, 303, saved.text)
        plan = context.queries.get_derived_entity_plan(workspace_id)
        self.assertIsNone(plan.rules[0].group_id)
        effective = context.queries.get_mapping_source_selection(workspace_id)
        dataset = effective.datasets[0]
        columns = {column.source_name: column.stable_key for column in dataset.columns}
        return workspace_id, dataset, columns, key, schema, plan

    def _entries(self, dataset, columns, key):
        return [
            ["csrf_token", self.csrf],
            ["action", "save_progress"],
            ["expected_parent_version", ""],
            ["expected_working_draft_version", ""],
            ["editable_dataset_id", dataset.dataset_id],
            ["target_model_0", "res.partner"],
            ["mode_0", "upsert"],
            ["on_existing_0", "block"],
            ["source_identity_0", columns["account_number"]],
            ["business_key_0", key.key_id],
            ["identity_source_0_0", columns["account_number"]],
            ["visible_relation_target_0", "relation_0000"],
            ["relation_source_0_0", columns["company"]],
            ["relation_origin_0_0", "target_catalog"],
            ["relation_key_0_0", key.key_id],
            ["relation_operation_0_0", "add"],
            ["relation_compare_0_0", "1"],
            ["relation_missing_0_0", "error"],
            ["relation_ambiguous_0_0", "error"],
            ["relation_null_0_0", "distinct"],
            ["relation_separator_0_0", ";"],
            [
                "relation_value_matches_0_0",
                json.dumps(
                    [
                        {"source_value": "United Caps", "target_value": "C001"},
                        {"source_value": "Fictional Retail", "target_value": "C002"},
                    ]
                ),
            ],
        ]

    def _save(self, workspace_id, entries):
        return self.client.post(
            f"/workspaces/{workspace_id}/mapping/save",
            json={"entries": entries},
            headers={**POST_HEADERS, "X-CSRF-Token": self.csrf},
        )

    def test_author_save_check_and_reopen_for_both_majors_and_connection_modes(self):
        for major in (19, 20):
            for mode in (OdooConnectionMode.LOCAL, OdooConnectionMode.REMOTE):
                with self.subTest(major=major, mode=mode):
                    workspace_id, dataset, columns, key, schema, plan = (
                        self._combined_mapping_workspace(major, mode)
                    )
                    with (
                        patch(
                            "impodo.web.routers.mapping._relationship_value_choices",
                            return_value=(
                                (
                                    {"value": "C001", "label": "First"},
                                    {"value": "C002", "label": "Second"},
                                ),
                                (),
                                datetime.now(UTC),
                                False,
                                {},
                            ),
                        ),
                        collect_duckdb_request_timings() as timings,
                    ):
                        choices = self.client.post(
                            f"/workspaces/{workspace_id}/mapping/value-choices",
                            data={
                                "csrf_token": self.csrf,
                                "kind": "relationship",
                                "dataset_id": dataset.dataset_id,
                                "source_column_key": columns["company"],
                                "target_model": "res.partner",
                                "target_field": "relation_0000",
                                "business_key_id": key.key_id,
                            },
                            headers=POST_HEADERS,
                        )
                    self.assertEqual(choices.status_code, 200, choices.text)
                    self.assertEqual(
                        choices.json()["source_choices"],
                        [
                            {"value": "Fictional Retail", "count": 1},
                            {"value": "United Caps", "count": 1},
                        ],
                    )
                    self.assertEqual(timings.lock_retry_count, 0)
                    entries = self._entries(dataset, columns, key)
                    saved = self._save(workspace_id, entries)
                    self.assertEqual(saved.status_code, 200, saved.text)
                    draft = self.app.state.context.queries.get_mapping_working_draft(
                        workspace_id
                    )
                    entries = [
                        [
                            name,
                            "draft"
                            if name == "action"
                            else str(draft.version)
                            if name == "expected_working_draft_version"
                            else value,
                        ]
                        for name, value in entries
                    ]
                    with (
                        patch(
                            "impodo.web.routers.mapping.check_matching_access",
                            new=AsyncMock(return_value=schema),
                        ),
                        patch(
                            "impodo.web.routers.mapping._capture_mapping_supporting_values",
                        ),
                        collect_duckdb_request_timings() as check_timings,
                    ):
                        checked = self._save(workspace_id, entries)
                    self.assertEqual(checked.status_code, 200, checked.text)
                    self.assertEqual(checked.json()["status"], "committed")
                    self.assertEqual(check_timings.lock_retry_count, 0)
                    context = self.app.state.context
                    revision = context.queries.get_mapping_revision(workspace_id)
                    self.assertIsNotNone(revision)
                    self.assertEqual(
                        revision.definition.datasets[0]
                        .relationships[0]
                        .resolver.value_mappings[0]
                        .source_value,
                        "Fictional Retail",
                    )
                    self.assertEqual(
                        context.queries.get_derived_entity_plan(workspace_id).to_json(),
                        plan.to_json(),
                    )
                    page = self.client.get(
                        f"/workspaces/{workspace_id}/mapping?step=check&task=links"
                    )
                    self.assertEqual(page.status_code, 200, page.text)
                    self.assertIn("customer_contacts", page.text)

    def test_rejected_check_classifies_missing_evidence_and_preserves_saved_edits(self):
        workspace_id, dataset, columns, key, schema, _plan = (
            self._combined_mapping_workspace(20, OdooConnectionMode.LOCAL)
        )
        entries = self._entries(dataset, columns, key)
        saved = self._save(workspace_id, entries)
        self.assertEqual(saved.status_code, 200, saved.text)
        context = self.app.state.context
        draft = context.queries.get_mapping_working_draft(workspace_id)
        entries = [
            [
                name,
                "draft"
                if name == "action"
                else str(draft.version)
                if name == "expected_working_draft_version"
                else value,
            ]
            for name, value in entries
        ]
        with (
            patch(
                "impodo.web.routers.mapping.check_matching_access",
                new=AsyncMock(return_value=schema),
            ),
            patch(
                "impodo.web.routers.mapping._capture_mapping_supporting_values",
            ),
            patch.object(
                context.categorical_coverage.logical_values.sources,
                "get_current_source_snapshots",
                return_value=(),
            ),
        ):
            rejected = self._save(workspace_id, entries)
        self.assertEqual(rejected.status_code, 422, rejected.text)
        self.assertEqual(
            rejected.json()["failure_code"], "SOURCE_PREPARATION_EVIDENCE_INCOMPLETE"
        )
        self.assertEqual(
            context.queries.get_mapping_working_draft(workspace_id).content_hash,
            draft.content_hash,
        )
        with (
            patch(
                "impodo.web.routers.mapping.check_matching_access",
                new=AsyncMock(return_value=schema),
            ),
            patch(
                "impodo.web.routers.mapping._capture_mapping_supporting_values",
            ),
        ):
            retried = self._save(workspace_id, entries)
        self.assertEqual(retried.status_code, 200, retried.text)
        self.assertEqual(retried.json()["status"], "committed")
