"""Keep Stage 1 reviewed saves durable before allowing the next stage."""

from __future__ import annotations

import asyncio
from unittest.mock import patch
from uuid import uuid4

from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from impodo.domain.odoo_source_policy import odoo_source_policy_hash
from impodo.domain.workspace.contracts import OdooModelCatalog, OdooModelSummary
from impodo.web.exact_join_forms import exact_join_projection_candidates
from tests.support.browser_scenarios import POST_HEADERS
from tests.support.hierarchy_workflow import HierarchyWorkflowFixture

SOURCE_FILES = {
    "products": b"Id,Group,Subgroup,Name\nP1,Tools,Hand,Hammer\nP2,Tools,Power,Drill\n",
    "details": b"Id,City\nP1,Brussels\nP2,Ghent\n",
}


class SourceJourneySavingTests(HierarchyWorkflowFixture):
    """Exercise existing authoring commands through the guarded HTTP exit."""

    def _source(self, *, major=19, mode="LOCAL", target=False):
        workspace_id = self.freeze_files(SOURCE_FILES)
        if target:
            schema = self.schema(workspace_id, major=major, mode=mode)
            catalog = OdooModelCatalog(
                workspace_id=workspace_id,
                connection_target_hash=schema.connection_target_hash,
                policy_hash=odoo_source_policy_hash(major),
                captured_at=schema.captured_at,
                captured_by=schema.captured_by,
                connection_mode=mode,
                database=schema.database,
                odoo_version=f"{major}.0",
                models=(
                    OdooModelSummary("product.category", "Category", ("base",), "base"),
                ),
                content_hash="sha256:" + "8" * 64,
                read_credential_binding_hash=schema.read_credential_binding_hash,
                read_principal_hash=schema.read_principal_hash,
                read_permission_hash=schema.read_permission_hash,
                read_context_hash=schema.read_context_hash,
            )
            self.context.schema_workspace.schemas.save_odoo_model_catalog(
                workspace_id, catalog, actor=self.actor
            )
        selection = self.context.queries.get_source_selection(workspace_id)
        return workspace_id, selection

    def _form(self, selection, shape):
        products = next(item for item in selection.datasets if item.name == "products")
        details = next(item for item in selection.datasets if item.name == "details")
        columns = {item.source_name: item.stable_key for item in products.columns}
        form = {
            "csrf_token": self.csrf,
            "expected_parent_version": "",
            "return_to": "schema",
        }
        if shape in {"lookup", "hierarchy"}:
            form.update(
                output_dataset_name=f"{shape}_categories",
                target_model="product.category",
                target_name_field="name",
                external_id_namespace="fictional_source",
            )
        if shape == "lookup":
            form.update(
                source_binding=f"{products.dataset_id}|{columns['Group']}",
                blank_policy="block",
                parent_separator="",
            )
            return "lookup/preview", "save", "lookup-extraction", form
        if shape == "hierarchy":
            form.update(
                source_dataset_id=products.dataset_id,
                hierarchy_level_1=columns["Group"],
                hierarchy_level_2=columns["Subgroup"],
                hierarchy_level_3="",
                hierarchy_level_4="",
                hierarchy_level_5="",
                missing_parent_mode="block",
                missing_parent_value="",
                missing_leaf="use_deepest",
                all_blank_mode="emit_null_reference",
                all_blank_value="",
            )
            return "hierarchy/preview", "hierarchy/save", "hierarchy-extraction", form
        if shape == "related":
            form.update(
                source_dataset_id=products.dataset_id,
                parent_dataset_name="source_groups",
                child_dataset_name="source_items",
                parent_key_column_key=columns["Group"],
                scope_column_key="",
                child_key_column_key=columns["Id"],
                blank_policy="block",
                source_dataset_use="map_and_prepare",
            )
            return "related/preview", "related/save", "create-related-datasets", form
        candidates = exact_join_projection_candidates(selection)
        names = [column.source_name for _dataset, column in candidates]
        selected = [
            str(index)
            for index, (dataset, column) in enumerate(candidates)
            if dataset.name == "products" or column.source_name == "City"
        ]
        detail_id = next(
            column.stable_key
            for column in details.columns
            if column.source_name == "Id"
        )
        if shape == "join":
            form.update(
                rule_id=str(uuid4()),
                output_dataset_name="combined_items",
                left_dataset_id=products.dataset_id,
                right_dataset_id=details.dataset_id,
                key_left_1=f"{products.dataset_id}|{columns['Id']}",
                key_right_1=f"{details.dataset_id}|{detail_id}",
                key_type_1="string",
                kind="LEFT",
                left_dataset_use="preparation_only",
                right_dataset_use="preparation_only",
                projection=selected,
                projection_name=names,
            )
            return "join/preview", "join/save", "combine-two-tables", form
        detail_index = selection.datasets.index(details)
        form.update(
            group_id=str(uuid4()),
            output_dataset_name="combined_items",
            primary_dataset_id=products.dataset_id,
            primary_key_field_1=f"{products.dataset_id}|{columns['Id']}",
            primary_key_type_1="string",
            group_addition=[str(detail_index)],
            group_projection=selected,
            group_projection_name=names,
        )
        form[f"group_addition_kind_{detail_index}"] = "LEFT"
        form[f"group_addition_key_{detail_index}_1"] = (
            f"{details.dataset_id}|{detail_id}"
        )
        return "join-group/preview", "join-group/save", "combine-source-tables", form

    def _action(self, workspace_id, suffix, form):
        return self.client.post(
            f"/workspaces/{workspace_id}/derived-entities/{suffix}",
            data=form,
            headers=POST_HEADERS,
            follow_redirects=False,
        )

    def _assert_saved_exit(self, workspace_id, selection, shape):
        preview_action, save_action, _operation, form = self._form(selection, shape)
        preview = self._action(workspace_id, preview_action, form)
        self.assertEqual(preview.status_code, 200, preview.text)
        self.assertIsNone(self.context.queries.get_derived_entity_plan(workspace_id))
        with collect_duckdb_request_timings() as timings:
            saved = self._action(workspace_id, save_action, form)
        self.assertEqual(saved.status_code, 303, saved.text)
        self.assertEqual(
            saved.headers["location"], f"/workspaces/{workspace_id}/schema"
        )
        plan = self.context.queries.get_derived_entity_plan(workspace_id)
        self.assertEqual(plan.version, 1)
        self.assertTrue(plan.rules)
        effective = self.context.queries.get_mapping_source_selection(workspace_id)
        output = "source_groups" if shape == "related" else form["output_dataset_name"]
        self.assertIn(output, {dataset.name for dataset in effective.datasets})
        # Admission has its own owners; the complete command uses one registry
        # and one workspace-engine owner, with no extra owner per service call.
        self.assertLessEqual(timings.connection_count, 8)
        self.assertLessEqual(timings.schema_check_count, 8)
        self.assertEqual(timings.lock_retry_count, 0)
        repeated = self._action(workspace_id, save_action, form)
        self.assertEqual(repeated.status_code, 422, repeated.text)
        self.assertNotIn("location", repeated.headers)
        self.assertEqual(repeated.context["source_form"]["expected_parent_version"], "")
        self.assertEqual(
            self.context.queries.get_derived_entity_plan(workspace_id), plan
        )

    def test_lookup_and_hierarchy_continue_for_both_odoo_versions_and_access_modes(
        self,
    ):
        for major in (19, 20):
            for mode in ("LOCAL", "REMOTE"):
                for shape in ("lookup", "hierarchy"):
                    with self.subTest(major=major, mode=mode, shape=shape):
                        workspace_id, selection = self._source(
                            major=major, mode=mode, target=True
                        )
                        method = (
                            "preview_lookup"
                            if shape == "lookup"
                            else "preview_hierarchy"
                        )
                        original = getattr(self.context.derived_entities, method)

                        def off_loop(*args, **kwargs):
                            with self.assertRaises(RuntimeError):
                                asyncio.get_running_loop()
                            return original(*args, **kwargs)

                        with patch.object(
                            self.context.derived_entities, method, side_effect=off_loop
                        ):
                            self._assert_saved_exit(workspace_id, selection, shape)

    def test_combination_and_split_continue_only_after_persisting_the_result(self):
        for shape in ("join", "join-group", "related"):
            with self.subTest(shape=shape):
                workspace_id, selection = self._source()
                self._assert_saved_exit(workspace_id, selection, shape)

    def test_rejected_preview_and_save_retain_choices_without_advancing(self):
        workspace_id, selection = self._source(target=True)
        for shape in ("lookup", "hierarchy", "join", "join-group", "related"):
            preview_action, save_action, operation, form = self._form(selection, shape)
            name_field = (
                "parent_dataset_name" if shape == "related" else "output_dataset_name"
            )
            form[name_field] = "Wrong Name"
            for action in (preview_action, save_action):
                with self.subTest(shape=shape, action=action):
                    with collect_duckdb_request_timings() as timings:
                        rejected = self._action(workspace_id, action, form)
                    self.assertEqual(rejected.status_code, 422, rejected.text)
                    self.assertNotIn("location", rejected.headers)
                    self.assertEqual(rejected.context["source_operation"], operation)
                    self.assertTrue(rejected.context["source_error"])
                    self.assertEqual(
                        rejected.context["source_form"][name_field], "Wrong Name"
                    )
                    self.assertIn('value="Wrong Name"', rejected.text)
                    self.assertIsNone(
                        self.context.queries.get_derived_entity_plan(workspace_id)
                    )
                    self.assertLessEqual(timings.connection_count, 8)
                    self.assertLessEqual(timings.schema_check_count, 8)

    def test_pair_parse_error_keeps_other_fields_and_selected_output(self):
        workspace_id, selection = self._source()
        preview_action, save_action, _operation, form = self._form(selection, "join")
        form["rule_id"] = "invalid"
        for action in (preview_action, save_action):
            with self.subTest(action=action):
                rejected = self._action(workspace_id, action, form)
                self.assertEqual(rejected.status_code, 422, rejected.text)
                retained = rejected.context["join_form"]
                self.assertEqual(retained.output_dataset_name, "combined_items")
                self.assertEqual(
                    retained.projection_indexes, tuple(map(int, form["projection"]))
                )
                self.assertEqual(
                    retained.projection_names, tuple(form["projection_name"])
                )
                self.assertIsNone(
                    self.context.queries.get_derived_entity_plan(workspace_id)
                )

    def test_next_destination_is_fixed_and_csrf_and_unknown_fields_stay_rejected(self):
        workspace_id, selection = self._source()
        _preview, save_action, _operation, form = self._form(selection, "related")
        bad_csrf = self._action(
            workspace_id, save_action, {**form, "csrf_token": "wrong"}
        )
        self.assertEqual(bad_csrf.status_code, 403)
        unexpected = self._action(
            workspace_id, save_action, {**form, "next_url": "schema"}
        )
        self.assertEqual(unexpected.status_code, 422)
        self.assertIsNone(self.context.queries.get_derived_entity_plan(workspace_id))
        saved = self._action(
            workspace_id, save_action, {**form, "return_to": "https://example.test/"}
        )
        self.assertEqual(saved.status_code, 303, saved.text)
        self.assertTrue(
            saved.headers["location"].startswith(
                f"/workspaces/{workspace_id}/derived-entities#related-rule-"
            )
        )
