"""Check linked values from two frozen tables against the current Odoo data."""

from collections import Counter
from contextlib import ExitStack
from dataclasses import replace
from unittest.mock import patch

from impodo.adapters.duckdb.unit_of_work import _read_databases
from impodo.domain.mapping.contracts import (
    CategoricalCoveragePolicy,
    DatasetMapping,
    IdentityComponentMapping,
    ReferenceKeyMapping,
    RelationshipMapping,
    RelationshipResolver,
    ResolverOrigin,
    ScalarFieldMapping,
)
from impodo.domain.mapping.reference_preparation import ReferencePreparation
from impodo.domain.workspace.errors import WorkspaceError
from impodo.domain.workspace.reference_keys import reference_policy_hash
from impodo.domain.workspace.workbench import OdooConnectionMode
from impodo.web.composition.page_reads import run_local_operation_sync
from impodo.web.routers import mapping as mapping_routes
from tests.performance.check_matches_runner import CheckFixture, check_once
from tests.support.browser_scenarios import POST_HEADERS
from tests.support.hierarchy_correction import MappingForm


class LinkedCheckTests(CheckFixture):
    def request(self, callback, *, structure, phases=None):
        if phases is None:
            return super().request(callback, structure=structure)

        foundation = self.context.workspace_states.repository.foundation
        read_package = foundation.get_source_package
        package_reads = Counter()

        def count_package_read(data_version_id):
            package_reads[phases.phase] += 1
            return read_package(data_version_id)

        with patch.object(foundation, "get_source_package", count_package_read):
            result = super().request(callback, structure=structure, phases=phases)
        self.assertEqual(package_reads["admission"], 1)
        self.assertGreater(package_reads["publication_and_response"], 0)
        return result

    def snapshots(self, state, metadata_requests, record_requests):
        self.assertIsNone(getattr(_read_databases, "connections", None))
        metadata, records = super().snapshots(state, metadata_requests, record_requests)
        if self.tools_only and "x.category" in records.records:
            remaining = tuple(
                row
                for row in records.records["x.category"]
                if row.values["name"] == "Tools"
            )
            records = replace(records, records={"x.category": remaining})
        return metadata, records

    def exercise_linked_checks(self, major, mode):
        self.tools_only = False
        workspace = self.create_case(major, mode, "linked", rows=4)
        sources = self.context.categorical_coverage.sources
        capture = mapping_routes._capture_mapping_supporting_values

        def observe_capture(*args, **kwargs):
            with ExitStack() as stack:
                reads = [
                    stack.enter_context(
                        patch.object(sources, name, wraps=getattr(sources, name))
                    )
                    for name in (
                        "get_mapping_source_selection",
                        "get_source_selection",
                        "get_current_source_snapshots",
                    )
                ]
                result = capture(*args, **kwargs)
            for read in reads:
                read.assert_called_once_with(workspace)
            return result

        with patch.object(
            mapping_routes, "_capture_mapping_supporting_values", observe_capture
        ):
            for tools_only, expected, sql_limit, opens, schema_checks in (
                (False, ("Parts", "Tools"), 346, 20, 15),
                (True, ("Tools",), 226, 16, 12),
            ):
                self.tools_only = tools_only
                result = check_once(self, workspace, "linked", structure=True)
                post = result["check"]
                self.assertLessEqual(post["sql"], sql_limit)
                self.assertLessEqual(post["connection_count"], opens)
                self.assertLessEqual(post["schema_check_count"], schema_checks)
                self.assertEqual(post["lock_retry_count"], 0)
                for page in (result["result"], result["reopen"]):
                    self.assertLessEqual(page["sql"], 119)
                    self.assertLessEqual(page["connection_count"], 4)
                    self.assertLessEqual(page["schema_check_count"], 3)
                self.assertEqual(self.saved_choices(workspace, major), expected)

    def saved_choices(self, workspace, major):
        schema = self.context.queries.get_odoo_schema_catalog(workspace)
        lookup = self.context.supporting_lookups.current(
            workspace,
            relation_model="x.category",
            key_fields=("name",),
            scope_fields=(),
            display_field="name",
            target_hash=schema.connection_target_hash,
            read_credential_binding_hash=schema.read_credential_binding_hash,
            read_principal_hash=schema.read_principal_hash,
            read_context_hash=schema.read_context_hash,
            reference_policy_hash=reference_policy_hash(major),
            actor=self.actor,
        )
        self.assertIsNotNone(lookup)
        return tuple(choice.value for choice in lookup.choices)

    def test_saved_linked_values_survive_a_later_mapping_failure(self):
        self.tools_only = True
        workspace = self.create_case(20, OdooConnectionMode.REMOTE, "linked", rows=4)
        before = self.context.queries.get_mapping_working_draft(workspace)
        page = self.client.get(f"/workspaces/{workspace}/mapping?step=check")
        self.assertEqual(page.status_code, 200, page.text)
        form = MappingForm(page.text)
        with patch.object(
            self.context.mapping_workspace, "check_definition",
            side_effect=WorkspaceError("Mapping changed during the check"),
        ):
            response = self.client.post(
                form.action, json={"entries": [*form.entries, ["action", "draft"]]},
                headers={**POST_HEADERS, "X-CSRF-Token": self.csrf},
            )
        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(response.json()["detail"], "Mapping changed during the check")
        receipt = self.context.mapping_workspace.get_mutation_receipt(
            workspace, response.json()["operation_id"], actor=self.actor,
        )
        self.assertEqual(receipt.state.value, "REJECTED")
        self.assertEqual(self.saved_choices(workspace, 20), ("Tools",))
        self.assertEqual(self.context.queries.get_mapping_working_draft(workspace), before)
        self.assertIsNone(self.context.queries.get_mapping_revision(workspace))

    def test_each_check_reads_frozen_selections_once_and_refreshes_target_choices(self):
        for major in (19, 20):
            for mode in OdooConnectionMode:
                with self.subTest(major=major, mode=mode):
                    self.exercise_linked_checks(major, mode)

    def test_check_reports_blank_reference_columns_and_recovers_with_a_row_filter(self):
        for major, mode in ((19, OdooConnectionMode.LOCAL), (20, OdooConnectionMode.REMOTE)):
            with self.subTest(major=major, mode=mode):
                self.exercise_blank_reference_check(major, mode)

    def exercise_blank_reference_check(self, major, mode):
        self.tools_only = False
        workspace, selection, _ = self.configure(
            major, mode, category_is_supporting=True, files={"products": (
                b"Reference,Product title,Category prefix,Category suffix,Use\n"
                b"P1,Item one,To,ols,yes\nP2,Item two,Par,ts,yes\n"
                b"P3,Item three,Private prefix,,no\nP4,Item four,Private prefix,,no\n"
                b"P5,Item five,,ols,no\n"
            )},
        )
        dataset, = selection.datasets
        reference, title, prefix, suffix, use = (column.stable_key for column in dataset.columns)
        resolver = RelationshipResolver(
            ResolverOrigin.TARGET_CATALOG,
            model="x.category",
            key_mappings=(ReferenceKeyMapping(prefix, "name", ReferencePreparation(
                formula="concat(value, column_4)", bindings=(("column_4", suffix),),
            )),),
        )
        mapping = DatasetMapping(
            dataset.dataset_id, "x.item",
            source_identity_column_keys=(reference,),
            target_identity=(IdentityComponentMapping((reference,), ("ref",)),),
            fields=(ScalarFieldMapping("name", title),),
            relationships=(RelationshipMapping(
                "category_id", "many2one", (prefix,), resolver,
                categorical_policy=CategoricalCoveragePolicy.EXACT_BUSINESS_KEY,
                selected_matching_field=True,
            ),),
        )
        run_local_operation_sync(
            self.context.mapping_workspace.save_working_draft,
            workspace, datasets=(mapping,), expected_version=None, actor=self.actor,
        )
        before = self.context.queries.get_mapping_working_draft(workspace)
        page = self.client.get(f"/workspaces/{workspace}/mapping?step=check")
        self.assertEqual(page.status_code, 200, page.text)
        form = MappingForm(page.text)
        headers = {**POST_HEADERS, "X-CSRF-Token": self.csrf}
        record_queries = self.reads["record_queries"]
        failed = self.client.post(
            form.action, json={"entries": [*form.entries, ["action", "draft"]]}, headers=headers,
        )
        self.assertEqual(failed.status_code, 422, failed.text)
        detail = failed.json()["detail"]
        self.assertIn("“Category suffix” is blank in 2 included rows.", detail)
        self.assertIn("“Category prefix” is blank in 1 included row.", detail)
        self.assertIn("Rows to use", detail)
        self.assertNotIn("Private prefix", detail)
        self.assertEqual(self.reads["record_queries"], record_queries)
        self.assertEqual(self.context.queries.get_mapping_working_draft(workspace), before)
        receipt = self.context.mapping_workspace.get_mutation_receipt(
            workspace, failed.json()["operation_id"], actor=self.actor,
        )
        self.assertEqual(receipt.failure_detail, detail)

        row_filter = {
            "row_inclusion_mode_0": "matching_rows",
            "row_inclusion_source_0_0": use,
            "row_inclusion_operator_0_0": "equals",
            "row_inclusion_value_0_0": "yes",
        }
        entries = [entry for entry in form.entries if entry[0] not in row_filter]
        checked = self.client.post(
            form.action, json={"entries": [*entries, *row_filter.items(), ["action", "draft"]]},
            headers=headers,
        )
        self.assertEqual(checked.status_code, 200, checked.text)
        self.assertEqual(self.saved_choices(workspace, major), ("Parts", "Tools"))
