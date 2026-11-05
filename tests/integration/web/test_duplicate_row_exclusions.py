"""Exercise current duplicate comparison, exact exclusions and save recovery."""

from dataclasses import replace
from uuid import uuid4
from unittest.mock import patch

from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from impodo.domain.mapping.contracts import (
    RowInclusionCondition,
    RowInclusionJoin,
    RowInclusionMode,
    RowInclusionPolicy,
    SelectionConditionOperator,
)
from impodo.domain.mapping.row_inclusion import row_is_included
from impodo.domain.mapping.mutations import MappingMutationAction, MappingMutationState
from impodo.domain.serialization import content_hash
from impodo.application.workspace.mapping.duplicate_rows import DuplicateRowReview
from impodo.domain.shared.access import (
    Actor,
    ActorIdentity,
    Capability,
    AuthorizationError,
)
from tests.support.hierarchy_workflow import HierarchyWorkflowFixture
from tests.support.identity_group_review import duplicate_identity_workspace
from tests.support.database_probe import count_duckdb_statements


class DuplicateRowExclusionTests(HierarchyWorkflowFixture):
    def test_pending_replay_does_not_reject_the_original_save(self):
        self.setup_rows()
        preview = self.command().json()
        operation_id = str(uuid4())
        fields = {
            "dataset_id": self.dataset.dataset_id,
            "check_id": self.check.check_id,
            "expected_version": "1",
            "position": "1",
            "action": "apply",
            "preview_hash": preview["preview_hash"],
            "operation_id": operation_id,
        }
        self.context.mapping_workspace.begin_mutation(
            self.workspace_id,
            operation_id=operation_id,
            action=MappingMutationAction.SAVE_PROGRESS,
            request_hash=content_hash({"duplicate_exclusions": sorted(fields.items())})[
                7:
            ],
            submitted_working_draft_version=1,
            submitted_mapping_revision_version=None,
            actor=self.actor,
        )
        response = self.command(
            action="apply",
            operation_id=operation_id,
            preview_hash=preview["preview_hash"],
        )
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn("still pending", response.json()["detail"])
        receipt = self.context.mapping_workspace.get_mutation_receipt(
            self.workspace_id, operation_id, actor=self.actor
        )
        self.assertEqual(receipt.state, MappingMutationState.PENDING)
        self.assertEqual(
            self.context.queries.get_mapping_working_draft(self.workspace_id).version, 1
        )

    def setup_rows(self, *, major=19, mode="LOCAL", unique=True, **options):
        self.workspace_id, self.dataset, self.check, self.reads = (
            duplicate_identity_workspace(
                self,
                repeated_count=7,
                unique_source_ids=unique,
                major=major,
                mode=mode,
                **options,
            )
        )
        self.base = f"/workspaces/{self.workspace_id}/mapping"
        self.endpoint = self.base + "/duplicate-rows"
        self.query = {"dataset": self.dataset.dataset_id, "check": self.check.check_id}

    def command(self, *, positions=(1,), action="preview", **extra):
        data = {
            "csrf_token": self.csrf,
            "dataset_id": self.dataset.dataset_id,
            "check_id": self.check.check_id,
            "expected_version": "1",
            "position": list(map(str, positions)),
            "action": action,
            **extra,
        }
        return self.client.post(
            self.endpoint, data=data, headers={"Origin": "http://testserver"}
        )

    def test_comparison_and_exclusion_preserve_the_kept_duplicate_for_both_odoo_majors(
        self,
    ):
        for major, mode in (
            (19, "LOCAL"),
            (19, "REMOTE"),
            (20, "LOCAL"),
            (20, "REMOTE"),
        ):
            with self.subTest(major=major, mode=mode):
                self.setup_rows(major=major, mode=mode)
                with (
                    collect_duckdb_request_timings() as timing,
                    count_duckdb_statements() as comparison_sql,
                ):
                    response = self.client.get(self.endpoint, params=self.query)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.headers["cache-control"], "no-store")
                payload = response.json()
                self.assertEqual(payload["matching_count"], 7)
                self.assertEqual(
                    payload["groups"][0]["rows"][0]["values"],
                    ["ROW-1", "DUP", "BE", "Version 1"],
                )
                self.assertEqual(payload["unavailable"], "")
                compared = self.context.categorical_coverage.source_condition_rows_at_positions(
                    self.workspace_id,
                    self.dataset.dataset_id,
                    tuple(column.stable_key for column in self.dataset.columns),
                    (6, 0, 3),
                )
                self.assertEqual(
                    [row[0] for row in compared], ["ROW-7", "ROW-1", "ROW-4"]
                )
                self.assertLessEqual(timing.connection_count, 6)
                # Two reference-store owners, the Data version store and the
                # workspace engine validate once within the retained operation.
                self.assertLessEqual(timing.schema_check_count, 4)
                self.assertLessEqual(len(comparison_sql), 128)
                self.assertEqual(timing.lock_retry_count, 0)
                self.assertEqual(len(self.reads), 1)
                with (
                    collect_duckdb_request_timings() as preview_timing,
                    count_duckdb_statements() as preview_sql,
                ):
                    preview = self.command(positions=(1, 2, 3, 4, 5, 6))
                self.assertEqual(preview.status_code, 200, preview.text)
                self.assertEqual(preview.json()["excluded_count"], 6)
                self.assertEqual(preview.json()["included_count"], 5)
                self.assertEqual(preview.json()["remaining_groups_on_page"], 0)
                operation_id = str(uuid4())
                with (
                    collect_duckdb_request_timings() as apply_timing,
                    count_duckdb_statements() as apply_sql,
                ):
                    applied = self.command(
                        positions=(1, 2, 3, 4, 5, 6),
                        action="apply",
                        preview_hash=preview.json()["preview_hash"],
                        operation_id=operation_id,
                    )
                self.assertEqual(applied.status_code, 200, applied.text)
                self.assertLessEqual(preview_timing.connection_count, 6)
                self.assertLessEqual(apply_timing.connection_count, 6)
                self.assertLessEqual(len(preview_sql), 128)
                self.assertLessEqual(len(apply_sql), 190)
                draft = self.context.queries.get_mapping_working_draft(
                    self.workspace_id
                )
                self.assertEqual(draft.version, 2)
                mapping = draft.definition.datasets[0]
                source_key = mapping.source_identity_column_keys[0]
                self.assertFalse(
                    row_is_included(mapping.row_inclusion, {source_key: "ROW-1"})
                )
                self.assertTrue(
                    row_is_included(mapping.row_inclusion, {source_key: "ROW-7"})
                )
                self.assertEqual(
                    mapping.row_inclusion.conditions[0].comparison_value,
                    "ROW-1, ROW-2, ROW-3, ROW-4, ROW-5, ROW-6",
                )
                repeated = self.command(
                    positions=(1, 2, 3, 4, 5, 6),
                    action="apply",
                    preview_hash=preview.json()["preview_hash"],
                    operation_id=operation_id,
                )
                self.assertEqual(repeated.status_code, 200, repeated.text)
                self.assertEqual(
                    self.context.queries.get_mapping_working_draft(
                        self.workspace_id
                    ).version,
                    2,
                )
                self.assertEqual(
                    self.client.get(self.endpoint, params=self.query).status_code, 409
                )
                self.assertEqual(len(self.reads), 1)

    def test_stale_preview_tampered_positions_and_missing_csrf_never_save(self):
        self.setup_rows()
        for kwargs in ({"positions": (10,)}, {"positions": (1, 1)}, {"positions": ()}):
            response = self.command(**kwargs)
            self.assertEqual(response.status_code, 409, response.text)
        preview = self.command().json()
        response = self.command(
            action="apply", operation_id=str(uuid4()), preview_hash="old-preview"
        )
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(
            self.context.queries.get_mapping_working_draft(self.workspace_id).version, 1
        )
        csrf = self.client.post(self.endpoint, data={"action": "apply"})
        self.assertIn(csrf.status_code, (400, 403))
        draft = self.context.queries.get_mapping_working_draft(self.workspace_id)
        self.context.mapping_workspace.save_working_draft(
            self.workspace_id,
            datasets=draft.definition.datasets,
            expected_version=draft.version,
            actor=self.actor,
        )
        response = self.command(
            action="apply",
            operation_id=str(uuid4()),
            preview_hash=preview["preview_hash"],
        )
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(
            self.context.queries.get_mapping_working_draft(self.workspace_id).version, 2
        )

    def test_nonunique_source_identity_still_compares_but_cannot_exclude(self):
        self.setup_rows(unique=False)
        response = self.client.get(self.endpoint, params=self.query)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("unique source identifier", response.json()["identifier_message"])
        self.assertEqual(self.command().status_code, 409)
        self.assertEqual(
            self.context.queries.get_mapping_working_draft(self.workspace_id).version, 1
        )

    def test_automatic_identifier_is_proved_beyond_the_displayed_duplicate_rows(self):
        self.setup_rows(
            source_identity_names=("Reference",),
            source_id_values=(
                "ROW-1",
                "ROW-2",
                "ROW-3",
                "ROW-4",
                "ROW-5",
                "ROW-6",
                "ROW-7",
                "ROW-1",
                "ROW-9",
                "ROW-10",
                "ROW-11",
            ),
        )
        columns = {
            column.source_name: column.stable_key for column in self.dataset.columns
        }
        response = self.client.get(self.endpoint, params=self.query)
        self.assertEqual(response.status_code, 200, response.text)
        # SourceId looks unique in the seven displayed rows but also occurs
        # on an eighth row. The complete-population proof must reject it.
        self.assertEqual(
            response.json()["exclusion_source_key"], columns["Description"]
        )
        preview = self.command()
        self.assertEqual(preview.status_code, 200, preview.text)
        self.assertEqual(preview.json()["excluded_count"], 1)
        saved = self.command(
            action="apply",
            operation_id=str(uuid4()),
            preview_hash=preview.json()["preview_hash"],
        )
        self.assertEqual(saved.status_code, 200, saved.text)
        rule = (
            self.context.queries.get_mapping_working_draft(self.workspace_id)
            .definition.datasets[0]
            .row_inclusion
        )
        self.assertFalse(row_is_included(rule, {columns["Description"]: "Version 1"}))
        self.assertTrue(row_is_included(rule, {columns["Description"]: "Version 8"}))

    def test_separate_exclusion_column_preserves_matching_and_surviving_parent_keys(
        self,
    ):
        for major, mode in (
            (19, "LOCAL"),
            (19, "REMOTE"),
            (20, "LOCAL"),
            (20, "REMOTE"),
        ):
            with self.subTest(major=major, mode=mode):
                self.setup_rows(
                    major=major,
                    mode=mode,
                    source_identity_names=("Reference",),
                    children=True,
                    child_parent_values=("DUP", "DUP", "TARGET"),
                    policy_factory=lambda columns: RowInclusionPolicy(
                        mode=RowInclusionMode.MATCHING_ROWS,
                        conditions=(
                            RowInclusionCondition(
                                str(uuid4()),
                                columns["Site"],
                                SelectionConditionOperator.EQUALS,
                                "BE",
                            ),
                        ),
                    ),
                )
                columns = {
                    column.source_name: column.stable_key
                    for column in self.dataset.columns
                }
                initial = self.context.queries.get_mapping_working_draft(
                    self.workspace_id
                )
                with (
                    collect_duckdb_request_timings() as timings,
                    count_duckdb_statements() as statements,
                    patch.object(
                        self.context.categorical_coverage,
                        "source_condition_rows",
                        wraps=self.context.categorical_coverage.source_condition_rows,
                    ) as reads,
                ):
                    response = self.client.get(self.endpoint, params=self.query)
                # Automatic discovery adds one bulk proof, not a read per row
                # or per candidate. Both reads share the six existing owners.
                self.assertEqual(reads.call_count, 2)
                self.assertLessEqual(timings.connection_count, 6)
                self.assertLessEqual(timings.schema_check_count, 4)
                self.assertEqual(timings.lock_retry_count, 0)
                self.assertLessEqual(len(statements), 160)
                self.assertEqual(response.json()["identifier_message"], "")
                self.assertEqual(
                    response.json()["exclusion_source_key"], columns["SourceId"]
                )
                preview = self.command()
                self.assertEqual(preview.status_code, 200, preview.text)
                self.assertEqual(preview.json()["impacts"], [])
                self.assertEqual(preview.json()["exclusion_column"], "SourceId")
                # Removing the last included copy loses the matching parent
                # key; removing one copy while retaining another does not.
                all_copies = self.command(
                    positions=tuple(range(1, 8)),
                    exclusion_source_key=columns["SourceId"],
                )
                self.assertEqual(all_copies.status_code, 200, all_copies.text)
                self.assertEqual(
                    all_copies.json()["impacts"],
                    [
                        {
                            "table": "lines",
                            "field": "parent_id",
                            "count": 2,
                            "follows_parent": False,
                        }
                    ],
                )
                mismatched = self.command(
                    action="apply",
                    operation_id=str(uuid4()),
                    preview_hash=preview.json()["preview_hash"],
                    exclusion_source_key=columns["Description"],
                )
                self.assertEqual(mismatched.status_code, 409, mismatched.text)
                saved = self.command(
                    action="apply",
                    operation_id=str(uuid4()),
                    preview_hash=preview.json()["preview_hash"],
                    exclusion_source_key=columns["SourceId"],
                )
                self.assertEqual(saved.status_code, 200, saved.text)
                after = self.context.queries.get_mapping_working_draft(
                    self.workspace_id
                )
                original = next(
                    item
                    for item in initial.definition.datasets
                    if item.dataset_id == self.dataset.dataset_id
                )
                updated = next(
                    item
                    for item in after.definition.datasets
                    if item.dataset_id == self.dataset.dataset_id
                )
                self.assertEqual(
                    updated.source_identity_column_keys,
                    original.source_identity_column_keys,
                )
                self.assertEqual(updated.target_identity, original.target_identity)
                self.assertEqual(
                    updated.row_inclusion.conditions[0],
                    original.row_inclusion.conditions[0],
                )
                self.assertEqual(
                    updated.row_inclusion.conditions[-1].source_column_key,
                    columns["SourceId"],
                )
                self.assertEqual(
                    updated.row_inclusion.conditions[-1].comparison_value, "ROW-1"
                )
                self.assertEqual(
                    self.command(exclusion_source_key="not-a-column").status_code, 409
                )

    def test_any_rule_is_preserved_instead_of_rewritten_as_all(self):
        self.setup_rows()
        draft = self.context.queries.get_mapping_working_draft(self.workspace_id)
        key = self.dataset.columns[1].stable_key
        policy = RowInclusionPolicy(
            mode=RowInclusionMode.MATCHING_ROWS,
            join=RowInclusionJoin.ANY,
            conditions=(
                RowInclusionCondition(
                    str(uuid4()), key, SelectionConditionOperator.IS_NOT_BLANK
                ),
            ),
        )
        modified = replace(
            draft,
            definition=replace(
                draft.definition,
                datasets=(replace(draft.definition.datasets[0], row_inclusion=policy),),
            ),
        )
        # Bind the isolated check to this rule without making a remote call.
        with (
            patch.object(
                self.context.mapping_workspace.mappings,
                "get_mapping_working_draft",
                return_value=modified,
            ),
            patch.object(
                self.context.matching_order, "check_is_current", return_value=True
            ),
        ):
            response = self.client.get(self.endpoint, params=self.query)
            self.assertIn("any-condition", response.json()["unavailable"])
            self.assertEqual(self.command().status_code, 409)
        self.assertEqual(
            self.context.queries.get_mapping_working_draft(self.workspace_id), draft
        )

    def test_existing_conditions_and_exclusion_list_are_preserved_with_child_impact(
        self,
    ):
        def policy(columns):
            return RowInclusionPolicy(
                mode=RowInclusionMode.MATCHING_ROWS,
                conditions=(
                    RowInclusionCondition(
                        str(uuid4()),
                        columns["Site"],
                        SelectionConditionOperator.IS_NOT_BLANK,
                    ),
                    RowInclusionCondition(
                        str(uuid4()),
                        columns["SourceId"],
                        SelectionConditionOperator.NOT_IN,
                        "ROW-999",
                    ),
                ),
            )

        self.setup_rows(children=True, policy_factory=policy)
        original = self.context.queries.get_mapping_working_draft(self.workspace_id)
        parent = next(
            item
            for item in original.definition.datasets
            if item.dataset_id == self.dataset.dataset_id
        )
        preview = self.command()
        self.assertEqual(preview.status_code, 200, preview.text)
        self.assertEqual(
            preview.json()["impacts"],
            [
                {
                    "table": "lines",
                    "field": "parent_id",
                    "count": 2,
                    "follows_parent": False,
                }
            ],
        )
        saved = self.command(
            action="apply",
            preview_hash=preview.json()["preview_hash"],
            operation_id=str(uuid4()),
        )
        self.assertEqual(saved.status_code, 200, saved.text)
        after = self.context.queries.get_mapping_working_draft(self.workspace_id)
        updated = next(
            item
            for item in after.definition.datasets
            if item.dataset_id == self.dataset.dataset_id
        )
        self.assertEqual(
            updated.row_inclusion.conditions[0], parent.row_inclusion.conditions[0]
        )
        self.assertEqual(
            updated.row_inclusion.conditions[1].condition_id,
            parent.row_inclusion.conditions[1].condition_id,
        )
        self.assertEqual(
            updated.row_inclusion.conditions[1].comparison_value, "ROW-999, ROW-1"
        )
        self.assertEqual(updated.relationships, parent.relationships)
        self.assertEqual(
            tuple(
                item
                for item in after.definition.datasets
                if item.dataset_id != self.dataset.dataset_id
            ),
            tuple(
                item
                for item in original.definition.datasets
                if item.dataset_id != self.dataset.dataset_id
            ),
        )

    def test_excluding_every_included_row_is_rejected(self):
        self.setup_rows(
            policy_factory=lambda columns: RowInclusionPolicy(
                mode=RowInclusionMode.MATCHING_ROWS,
                conditions=(
                    RowInclusionCondition(
                        str(uuid4()),
                        columns["Reference"],
                        SelectionConditionOperator.EQUALS,
                        "DUP",
                    ),
                    RowInclusionCondition(
                        str(uuid4()),
                        columns["Site"],
                        SelectionConditionOperator.EQUALS,
                        "BE",
                    ),
                ),
            )
        )
        response = self.command(positions=tuple(range(1, 8)))
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn("Keep at least one row", response.json()["detail"])
        self.assertEqual(
            self.context.queries.get_mapping_working_draft(self.workspace_id).version, 1
        )

    def test_protected_access_is_required_before_reading_source_values(self):
        self.setup_rows()
        service = DuplicateRowReview(
            self.context.mapping_workspace,
            self.context.matching_order,
            self.context.categorical_coverage,
        )
        actor = Actor(
            ActorIdentity("test", "mapping-only", "Mapping editor"),
            frozenset({Capability.MAPPING_EDIT}),
        )
        with patch.object(
            self.context.categorical_coverage, "source_condition_rows"
        ) as read:
            with self.assertRaises(AuthorizationError):
                service.review(
                    self.workspace_id,
                    self.dataset.dataset_id,
                    self.check.check_id,
                    actor=actor,
                )
            read.assert_not_called()

    def test_restricted_data_and_recipe_runs_do_not_expose_comparison_values(self):
        self.setup_rows()
        owner = self.context.workspace_views.get(self.workspace_id, actor=self.actor)
        restricted = replace(
            owner,
            migration_project=replace(
                owner.migration_project,
                data_classification=type(owner.migration_project.data_classification)(
                    "RESTRICTED"
                ),
            ),
        )
        with (
            patch.object(self.context.workspace_views, "get", return_value=restricted),
            patch.object(
                self.context.categorical_coverage, "source_condition_rows"
            ) as read,
        ):
            response = self.client.get(self.endpoint, params=self.query)
            self.assertEqual(response.status_code, 409)
            self.assertIn("restricted data", response.json()["detail"])
            read.assert_not_called()
        access = self.context.workspace_access.resolve(
            self.workspace_id,
            actor=self.actor,
            capability=Capability.PROTECTED_EVIDENCE_READ,
        )
        with patch.object(
            self.context.workspace_access,
            "resolve",
            return_value=replace(access, recipe_application_id=str(uuid4())),
        ):
            response = self.command()
            self.assertEqual(response.status_code, 409)
            self.assertIn("fixed row rules", response.json()["detail"])
        self.assertEqual(
            self.context.queries.get_mapping_working_draft(self.workspace_id).version, 1
        )
