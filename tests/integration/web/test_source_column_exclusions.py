"""Exercise saved source exclusions through the existing Match fields form."""

from dataclasses import replace
import unittest
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from fastapi import Request

from starlette.datastructures import FormData

from impodo.domain.mapping.canonicalization import canonicalize_mapping_definition
from impodo.domain.mapping.column_exclusions import validate_source_column_exclusions
from impodo.domain.workspace.contracts import SourceDatasetColumn
from impodo.domain.workspace.errors import WorkspaceError
from impodo.web.presenters.mapping_forms import (
    _mapping_allowed_fields,
    _merge_partial_mapping_datasets,
)
from impodo.web.presenters.mapping_view import _mapping_issue_views
from tests.integration.web import test_mapping_form_roundtrip as roundtrip
from tests.support.browser_scenarios import POST_HEADERS, ProjectSetupBrowserTestCase
from impodo.domain.workspace.workbench import OdooConnectionMode
from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from tests.support.database_probe import count_duckdb_statements


class SourceColumnExclusionFormTests(unittest.TestCase):
    def setUp(self):
        self.fixture = roundtrip.MappingFormRoundTripTests(methodName="runTest")
        self.fixture.setUp()
        self.fixture.selection.datasets = tuple(
            replace(
                source,
                columns=(
                    *source.columns,
                    SourceDatasetColumn(
                        len(source.columns) + 1,
                        "Internal notes",
                        source.dataset_id + ".notes",
                        "string",
                    ),
                ),
            )
            for source in self.fixture.selection.datasets
        )
        sources = {
            source.dataset_id: source for source in self.fixture.selection.datasets
        }
        self.definition = replace(
            self.fixture.definition,
            datasets=tuple(
                replace(
                    mapping,
                    excluded_source_column_keys=(
                        sources[mapping.dataset_id].columns[-1].stable_key,
                    ),
                )
                for mapping in self.fixture.definition.datasets
            ),
        )

    def saved(self, index):
        dataset_id = self.fixture.selection.datasets[index].dataset_id
        return next(
            item for item in self.definition.datasets if item.dataset_id == dataset_id
        )

    def merge(self, form):
        return _merge_partial_mapping_datasets(
            self.fixture.parse(form),
            self.definition,
            form,
            self.fixture.selection,
            self.fixture.schema,
        )

    def test_reopen_and_save_preserves_both_tables_and_hash(self):
        for major in (19, 20):
            self.fixture.schema.odoo_version = f"{major}.0"
            for active in (0, 1):
                with self.subTest(major=major, active=active):
                    form = self.fixture.render_form(self.definition, active)
                    self.assertTrue(
                        set(form).issubset(
                            _mapping_allowed_fields(
                                form,
                                self.fixture.selection,
                                self.fixture.schema,
                            )
                        )
                    )
                    reopened = canonicalize_mapping_definition(
                        replace(
                            self.definition,
                            datasets=self.merge(form),
                        )
                    )
                    self.assertEqual(reopened.datasets, self.definition.datasets)
                    self.assertEqual(
                        reopened.content_hash, self.definition.content_hash
                    )
                    self.assertEqual(
                        form.getlist(f"excluded_source_column_{active}"),
                        list(self.saved(active).excluded_source_column_keys),
                    )
                    self.assertEqual(
                        form[f"excluded_source_columns_present_{active}"], "1"
                    )

    def test_clearing_the_active_choice_preserves_the_other_table(self):
        form = self.fixture.render_form(self.definition, 0)
        cleared = FormData(
            (key, value)
            for key, value in form.multi_items()
            if key != "excluded_source_column_0"
        )
        merged = self.merge(cleared)
        self.assertEqual(merged[0].excluded_source_column_keys, ())
        self.assertEqual(
            merged[1].excluded_source_column_keys,
            self.saved(1).excluded_source_column_keys,
        )
        self.assertNotEqual(
            replace(self.definition, datasets=merged).content_hash,
            self.definition.content_hash,
        )

    def test_save_without_the_control_keeps_the_previous_choice(self):
        form = self.fixture.render_form(self.definition, 0)
        partial = FormData(
            (key, value)
            for key, value in form.multi_items()
            if key
            not in {"excluded_source_column_0", "excluded_source_columns_present_0"}
        )
        self.assertEqual(
            self.merge(partial)[0].excluded_source_column_keys,
            self.saved(0).excluded_source_column_keys,
        )

    def test_a_nonactive_table_cannot_be_changed(self):
        form = self.fixture.render_form(self.definition, 0)
        tampered = FormData(
            (
                *form.multi_items(),
                ("excluded_source_columns_present_1", "1"),
                (
                    "excluded_source_column_1",
                    self.saved(1).excluded_source_column_keys[0],
                ),
            )
        )
        with self.assertRaisesRegex(WorkspaceError, "not being edited"):
            self.merge(tampered)

    def test_unknown_columns_and_unmarked_choices_are_rejected(self):
        form = self.fixture.render_form(self.definition, 0)
        unknown = FormData(
            (*form.multi_items(), ("excluded_source_column_0", "unknown"))
        )
        with self.assertRaisesRegex(WorkspaceError, "current table"):
            self.fixture.parse(unknown)
        unmarked = FormData(
            (key, value)
            for key, value in form.multi_items()
            if key != "excluded_source_columns_present_0"
        )
        with self.assertRaisesRegex(WorkspaceError, "Reopen Match fields"):
            self.fixture.parse(unmarked)

    def test_conflict_names_the_column_and_links_to_its_choice(self):
        source = self.fixture.selection.datasets[0]
        mapping = replace(
            self.saved(0), excluded_source_column_keys=(source.columns[0].stable_key,)
        )
        issues = validate_source_column_exclusions(
            mapping,
            source.columns,
            origin=source.origin,
            path="/datasets/0/excluded_source_column_keys",
        )
        request = Request(
            {
                "type": "http",
                "method": "GET",
                "scheme": "http",
                "server": ("testserver", 80),
                "path": "/workspaces/workspace:test/mapping",
                "query_string": b"step=check",
                "headers": [],
            }
        )
        view = _mapping_issue_views(
            request,
            "workspace:test",
            SimpleNamespace(issues=issues),
            self.fixture.selection,
            self.fixture.schema,
            self.definition,
        )[0]
        self.assertIn(source.columns[0].source_name, view["issue"].message)
        self.assertEqual(view["field_label"], "Source columns set aside")
        target = urlsplit(view["fix_url"])
        self.assertEqual(parse_qs(target.query)["task"], ["fields"])
        self.assertEqual(target.fragment, "source-columns-set-aside-0")


class SavedSourceColumnExclusionRouteTests(ProjectSetupBrowserTestCase):
    def test_saved_choice_roundtrips_without_extra_local_reads(self):
        for major in (19, 20):
            for mode in OdooConnectionMode:
                with self.subTest(major=major, mode=mode):
                    budgets = []
                    for exclude in (False, True):
                        workspace_id, dataset, key = self._mapping_ready_workspace(
                            scalar_field_count=1,
                            odoo_major=major,
                            connection_mode=mode,
                        )
                        self.assertGreater(len(dataset.columns), 1)
                        source_before = (
                            self.app.state.context.queries.get_source_selection(
                                workspace_id
                            )
                        )
                        entries = [
                            ["csrf_token", self.csrf],
                            ["action", "save_progress"],
                            ["operation_id", str(uuid4())],
                            ["editable_dataset_id", dataset.dataset_id],
                            ["target_model_0", "res.partner"],
                            ["mode_0", "upsert"],
                            ["source_identity_0", dataset.columns[0].stable_key],
                            ["business_key_0", key.key_id],
                            ["identity_source_0_0", dataset.columns[0].stable_key],
                            ["excluded_source_columns_present_0", "1"],
                        ]
                        if exclude:
                            entries.append(
                                [
                                    "excluded_source_column_0",
                                    dataset.columns[-1].stable_key,
                                ]
                            )
                        with (
                            count_duckdb_statements() as sql,
                            collect_duckdb_request_timings() as timings,
                        ):
                            saved = self.client.post(
                                f"/workspaces/{workspace_id}/mapping/save?step=tables&task=fields",
                                json={"entries": entries},
                                headers={**POST_HEADERS, "X-CSRF-Token": self.csrf},
                            )
                        self.assertEqual(saved.status_code, 200, saved.text)
                        self.assertEqual(saved.json()["status"], "committed")
                        expected = (dataset.columns[-1].stable_key,) if exclude else ()
                        draft = (
                            self.app.state.context.queries.get_mapping_working_draft(
                                workspace_id
                            )
                        )
                        self.assertEqual(
                            draft.definition.datasets[0].excluded_source_column_keys,
                            expected,
                        )
                        self.assertEqual(
                            self.app.state.context.queries.get_source_selection(
                                workspace_id
                            ),
                            source_before,
                        )
                        reopened = self.client.get(
                            f"/workspaces/{workspace_id}/mapping?step=tables&task=fields"
                        )
                        self.assertEqual(reopened.status_code, 200, reopened.text)
                        controls = roundtrip._RenderedControls()
                        controls.feed(reopened.text)
                        form = FormData(controls.entries)
                        self.assertEqual(
                            tuple(form.getlist("excluded_source_column_0")), expected
                        )
                        budgets.append(
                            (
                                timings.connection_count,
                                timings.schema_check_count,
                                len(sql),
                                sum(
                                    "duckdb_columns()" in statement for statement in sql
                                ),
                            )
                        )
                    self.assertEqual(budgets[1], budgets[0])
