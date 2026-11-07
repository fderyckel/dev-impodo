"""Keep source parent IDs separate from scoped Odoo category names in the editor."""

import json
from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from impodo.domain.mapping.contracts import IdentityNullPolicy
from impodo.domain.odoo.contracts import RecordSnapshot
from impodo.domain.shared.models import TargetRecord
from impodo.domain.workspace.reference_keys import (
    captured_reference_field_contracts,
    reference_policy_hash,
)
from impodo.domain.workspace.supporting_lookups import (
    SupportingLookupChoice,
    portable_supporting_value,
)
from impodo.web.presenters.mapping_view import _mapping_dataset_views
from tests.integration.web.test_mapping_form_roundtrip import _RenderedControls
from tests.support.browser_scenarios import POST_HEADERS
from tests.support.hierarchy_assertions import business_path
from tests.support.hierarchy_workflow import HierarchyWorkflowFixture
from tests.support.hierarchy_odoo import HierarchyOdoo
from tests.support.recipe_lifecycle import metadata_for_schema
from tests.support.same_table_hierarchy import CATEGORY_CSV, same_table_workspace


class SameTableHierarchyTests(HierarchyWorkflowFixture):
    def test_bulk_category_choices_keep_the_parent_and_skip_nonunique_names(self):
        for major in (19, 20):
            for mode in ("LOCAL", "REMOTE"):
                with self.subTest(major=major, mode=mode):
                    self._check_bulk_category_choices(major, mode)

    def _check_bulk_category_choices(self, major, mode):
        workspace_id = self.freeze_files({
            "products": b"Code,Category\nP01,Bag\nP02,Seal\n",
        })
        schema = self.schema(workspace_id, major=major, mode=mode)
        dataset = self.context.queries.get_source_selection(workspace_id).datasets[0]
        columns = {column.source_name: column.stable_key for column in dataset.columns}
        model = "product.category"
        fields = ("name", "parent_id")
        calls = []
        metadata = metadata_for_schema(schema, {model: fields})
        bag = portable_supporting_value(("Bag", "Materials"))
        legacy = self.context.supporting_lookups.capture(
            workspace_id, relation_model=model, key_fields=("name",),
            scope_fields=("parent_id",), display_field="name",
            field_contracts=captured_reference_field_contracts(metadata.models[model].fields.values()),
            target_hash=schema.connection_target_hash,
            read_credential_binding_hash=schema.read_credential_binding_hash,
            read_principal_hash=schema.read_principal_hash,
            read_permission_hash=schema.read_permission_hash,
            read_context_hash=schema.read_context_hash,
            captured_at=datetime.now(UTC),
            choices=(SupportingLookupChoice(bag, "Bag (Materials)"),),
            ambiguous_values=(), actor=self.actor, reference_policy_hash=reference_policy_hash(major),
        )
        self.assertEqual(legacy.contract_version, 4)

        def read_choices(_workspace, metadata_requests, record_requests):
            self.assertEqual([(item.model, item.fields) for item in metadata_requests], [(model, fields)])
            self.assertEqual([(item.model, item.fields, item.domain) for item in record_requests], [(model, fields, ())])
            calls.append(record_requests)
            records = tuple(
                TargetRecord(model, identifier, {"name": name, "parent_id": parent})
                for identifier, (name, parent) in enumerate((
                    ("Materials", False), ("Bag", (1, "Materials")),
                    ("Seal", (2, "Materials / Bag")), ("Repeated", (1, "Materials")),
                    ("Repeated", (2, "Materials / Bag")), ("Duplicate", (1, "Materials")),
                    ("Duplicate", (1, "Materials")), ("Mixed", False),
                    ("Mixed", (1, "Materials")),
                ), 1)
            )
            return metadata, RecordSnapshot(
                metadata.fingerprint, {model: records}, {model: fields}
            )

        self.context.readiness_reader = read_choices
        url = f"/workspaces/{workspace_id}/mapping"
        choice_form = {
            "csrf_token": self.csrf,
            "kind": "relationship",
            "dataset_id": dataset.dataset_id,
            "source_column_key": columns["Category"],
            "target_model": "product.template",
            "target_field": "categ_id",
            "business_key_id": "category:name-parent",
        }
        with collect_duckdb_request_timings() as timings:
            response = self.client.post(url + "/value-choices", data=choice_form, headers=POST_HEADERS)
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        suggestions = {
            "Bag": bag,
            "Seal": portable_supporting_value(("Seal", "Materials / Bag")),
        }
        self.assertEqual(result["scoped_suggestions"], suggestions)
        self.assertEqual(result["ambiguous_values"], [portable_supporting_value(("Duplicate", "Materials"))])
        self.assertFalse(result["target_choices_reused"])

        with collect_duckdb_request_timings() as cached_timings:
            cached = self.client.post(url + "/value-choices", data=choice_form, headers=POST_HEADERS)
        self.assertEqual(cached.status_code, 200, cached.text)
        self.assertEqual(cached.json()["scoped_suggestions"], suggestions)
        self.assertTrue(cached.json()["target_choices_reused"])
        self.assertEqual(len(calls), 1)
        self.assertLess(cached_timings.connection_count, timings.connection_count)
        self.assertEqual(cached_timings.lock_retry_count, 0)

        entries = [
            ["csrf_token", self.csrf],
            ["operation_id", str(uuid4())],
            ["action", "save_progress"],
            ["expected_parent_version", ""],
            ["expected_working_draft_version", ""],
            ["editable_dataset_id", dataset.dataset_id],
            ["target_model_0", "product.template"],
            ["business_key_0", "product:reference"],
            ["source_identity_0", columns["Code"]],
            ["identity_source_0_0", columns["Code"]],
            ["visible_relation_target_0", "categ_id"],
            ["relation_value_source_0_0", "source"],
            ["relation_source_0_0", columns["Category"]],
            ["relation_origin_0_0", "target_catalog"],
            ["relation_key_0_0", "category:name-parent"],
            ["relation_categorical_policy_0_0", "EXPLICIT_KEY_MATCH"],
            ["relation_value_matches_0_0", json.dumps([
                {"source_value": name, "target_value": value}
                for name, value in suggestions.items()
            ])],
        ]
        saved = self.client.post(
            url + "/save", json={"entries": entries},
            headers={**POST_HEADERS, "X-CSRF-Token": self.csrf},
        )
        self.assertEqual(saved.status_code, 200, saved.text)
        draft = self.context.queries.get_mapping_working_draft(workspace_id)
        relation = draft.definition.datasets[0].relationships[0]
        self.assertEqual([item.target_field for item in relation.resolver.scope_mappings], ["parent_id"])
        self.assertEqual({item.source_value: item.target_value for item in relation.resolver.value_mappings}, suggestions)
        checked = self.submit(workspace_id, draft.definition.datasets, check_only=True)
        self.assertFalse([issue for issue in checked.issues if issue.target_field == "categ_id"], checked.issues)

    def test_author_and_reopen_supplied_hierarchy_for_both_majors_and_modes(self):
        for major in (19, 20):
            for mode in ("LOCAL", "REMOTE"):
                with self.subTest(major=major, mode=mode):
                    workspace_id, source = same_table_workspace(
                        self,
                        major=major,
                        mode=mode,
                    )
                    with collect_duckdb_request_timings() as timings:
                        page = self.client.get(f"/workspaces/{workspace_id}/mapping")
                    self.assertEqual(page.status_code, 200, page.text)
                    self.assertIn("categories (this table)", page.text)
                    self.assertIn("a blank parent means a top-level record", page.text)
                    controls = _RenderedControls()
                    controls.feed(page.text)
                    self.assertIn(
                        ("identity_dataset_0_1", source.dataset_id), controls.entries
                    )
                    # Access, preparation and rendering retain the existing eight-owner
                    # ceiling; offering this table performs no additional database reads.
                    self.assertLessEqual(timings.connection_count, 8)
                    self.assertLessEqual(timings.schema_check_count, 6)
                    self.assertEqual(timings.lock_retry_count, 0)
                    revision = self.context.queries.get_mapping_revision(workspace_id)
                    mapping = revision.definition.datasets[0]
                    columns = {
                        column.source_name: column.stable_key
                        for column in source.columns
                    }
                    self.assertEqual(
                        mapping.source_identity_column_keys, (columns["RecId"],)
                    )
                    self.assertEqual(
                        mapping.target_identity[0].source_column_keys,
                        (columns["Name"],),
                    )
                    self.assertEqual(
                        mapping.target_scope[0].null_policy,
                        IdentityNullPolicy.EXPLICIT_SCOPE_NULL,
                    )
                    self.submit(workspace_id, (mapping,))
                    staging, quality = self.prepare(workspace_id)
                    self.assertEqual(
                        (quality.ready_count, quality.quarantined_count), (4, 0)
                    )
                    self.approve(workspace_id)
                    self.expect_business_writes(workspace_id)
                    target = HierarchyOdoo(
                        self.context.queries.get_odoo_schema_catalog(workspace_id)
                    )
                    report = self.context.preflight.compare(
                        workspace_id, reader=target.compare, actor=self.actor
                    )
                    self.assertEqual(report.status, "READY", report)
                    snapshot = self.context.execution.current_preview(
                        workspace_id, actor=self.actor
                    ).snapshot
                    self.assertEqual(
                        {
                            business_path(row.business_identity, row.business_scope)
                            for row in snapshot.rows
                        },
                        {
                            ("Materials",),
                            ("Materials", "Additive"),
                            ("Materials", "Bag"),
                            ("Materials", "Bag", "Additive"),
                        },
                    )

    def test_populated_unknown_or_circular_parent_does_not_become_a_root(self):
        cases = (
            (
                b"5637144576,Materials,\n",
                b"5637144576,Materials,0\n",
                "INCOMING_RELATIONSHIP_MISSING",
            ),
            (
                b"5637144576,Materials,\n",
                b"5637144576,Materials,5637145328\n",
                "REFERENCE_IDENTITY_CYCLE",
            ),
        )
        for original, replacement, reason in cases:
            with self.subTest(reason=reason):
                workspace_id, _ = same_table_workspace(
                    self,
                    content=CATEGORY_CSV.replace(original, replacement),
                )
                revision = self.context.queries.get_mapping_revision(workspace_id)
                if reason == "INCOMING_RELATIONSHIP_MISSING":
                    validation = self.submit(workspace_id, revision.definition.datasets, check_only=True)
                    self.assertEqual(validation.status.value, "INVALID")
                    self.assertIn("MAPPING_INCOMING_PARENT_MISSING", {issue.code for issue in validation.issues})
                    self.assertIsNone(self.context.preparation.staging.get_current_staging_summary(workspace_id))
                    continue
                self.submit(workspace_id, revision.definition.datasets)
                staging, quality = self.prepare(workspace_id)
                if reason == "REFERENCE_IDENTITY_CYCLE":
                    self.approve(workspace_id)
                    self.expect_business_writes(workspace_id)
                    target = HierarchyOdoo(
                        self.context.queries.get_odoo_schema_catalog(workspace_id)
                    )
                    report = self.context.preflight.compare(
                        workspace_id, reader=target.compare, actor=self.actor
                    )
                    self.assertGreater(report.blocked_count, 0)
                    self.assertEqual(target.writes, [])
                else:
                    self.assertGreater(quality.quarantined_count, 0)
                    issues = self.context.quality.current_run(workspace_id)
                    self.assertIn(
                        reason, {issue.reason_code for issue in issues.issues}
                    )
                self.assertFalse(
                    any(row.target_scope == (None,) for row in staging.rows)
                )

    def test_same_table_is_available_for_a_self_many2one_outside_the_matching_rule(
        self,
    ):
        workspace_id, source = same_table_workspace(self)
        revision = self.context.queries.get_mapping_revision(workspace_id)
        mapping = revision.definition.datasets[0]
        selection = self.context.queries.get_source_selection(workspace_id)
        schema = self.context.queries.get_odoo_schema_catalog(workspace_id)
        governance = self.context.queries.get_schema_governance(workspace_id)
        governance = replace(
            governance,
            business_keys=(replace(governance.business_keys[0], scope_fields=()),),
        )
        # Removing the scope moves Parent Category into the linked-field catalog.
        views = _mapping_dataset_views(
            selection, schema, governance, (replace(mapping, target_scope=()),)
        )
        parent = next(
            row
            for row in views[0]["relation_rows"]
            if row["metadata"].name == "parent_id"
        )
        self.assertIn(source, parent["related_datasets"])
