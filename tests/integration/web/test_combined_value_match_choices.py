"""Check mixed Odoo and extracted-table matches through the real dialog route."""

from collections import Counter
from dataclasses import replace
import json
from unittest import TestCase
from unittest.mock import patch
from uuid import uuid4

from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from impodo.domain.workspace.workbench import OdooConnectionMode
from impodo.domain.mapping.contracts import (
    RowInclusionCondition,
    RowInclusionPolicy,
    RowInclusionMode,
    SelectionConditionOperator,
)
from tests.support.browser_scenarios import POST_HEADERS
from tests.support.database_probe import count_duckdb_statements
from tests.support.value_match_choices import ValueMatchFixture
from tests.support.hierarchy_correction import MappingForm


class CombinedValueMatchChoicesTests(TestCase):
    def test_three_file_join_keeps_extracted_table_matches_in_the_dialog(self):
        from tests.integration.web.test_join_lookup_preparation import (
            JoinLookupPreparationTests,
        )
        from impodo.domain.odoo.contracts import MetadataSnapshot, RecordSnapshot
        from impodo.domain.shared.models import (
            FieldMetadata,
            ModelMetadata,
            TargetFingerprint,
            TargetRecord,
        )

        case = JoinLookupPreparationTests(methodName="runTest")
        case.setUp()
        self.addCleanup(case.doCleanups)
        workspace_id, _ = case._setup((6, 8, 8))
        fixture, context = case.fixture, case.context
        schema = context.queries.get_odoo_schema_catalog(workspace_id)
        state = context.queries.get(workspace_id)
        fingerprint = TargetFingerprint(
            schema.connection_target_hash,
            state.odoo_connection_mode.value,
            state.odoo_database,
            schema.odoo_version,
            schema.captured_at.isoformat(),
        )
        metadata = MetadataSnapshot(
            fingerprint,
            {
                model.name: ModelMetadata(
                    model.name,
                    model.label,
                    {
                        field.name: FieldMetadata(
                            field.name,
                            field.type,
                            label=field.label,
                            required=field.required,
                            relation=field.relation,
                        )
                        for field in model.fields
                    },
                )
                for model in schema.models
            },
        )
        context.readiness_reader = lambda _state, _metadata, requests: (
            metadata,
            RecordSnapshot(
                fingerprint,
                records={
                    request.model: (TargetRecord("x_secondary", 17, {"ref": "b"}),)
                    if request.model == "x_secondary"
                    else ()
                    for request in requests
                },
                requested_fields={
                    request.model: request.fields for request in requests
                },
            ),
        )
        selection = context.queries.get_mapping_source_selection(workspace_id)
        owner = next(
            item for item in selection.datasets if item.name == "records_complete"
        )
        parent = next(
            item for item in selection.datasets if item.name == "generated_labels"
        )
        source = next(
            item.stable_key for item in owner.columns if item.source_name == "Choice"
        )
        response = fixture.client.post(
            f"/workspaces/{workspace_id}/mapping/value-choices",
            data={
                "csrf_token": fixture.csrf,
                "kind": "relationship",
                "dataset_id": owner.dataset_id,
                "source_column_key": source,
                "target_model": "x_record",
                "target_field": "x_generated_id",
                "business_key_id": "x_secondary:ref",
                "resolver_origin": "target_then_dataset",
                "incoming_dataset_id": parent.dataset_id,
            },
            headers=POST_HEADERS,
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["incoming_table"]["checked"], response.json())
        self.assertEqual(
            Counter(
                item["status"] for item in response.json()["relationship_resolutions"]
            ),
            {"target": 1, "incoming": 2},
        )

    def fixture(self, major=19, mode=OdooConnectionMode.LOCAL, related_model=None):
        fixture = ValueMatchFixture(methodName="runTest")
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        if related_model:
            fixture.related_model = related_model
        fixture.configure_matches(major, mode)
        return fixture

    def test_physical_table_and_unrelated_model_use_the_saved_source_identity(self):
        fixture = self.fixture(related_model="x.category")
        table = next(
            item for item in fixture.selection.datasets if item.name == "spare_parts"
        )
        key = next(
            item.stable_key for item in table.columns if item.source_name == "Category"
        )
        parent = replace(
            fixture.mappings[0],
            dataset_id=table.dataset_id,
            source_identity_column_keys=(key,),
            target_identity=(
                replace(
                    fixture.mappings[0].target_identity[0], source_column_keys=(key,)
                ),
            ),
        )
        relation = fixture.mappings[1].relationships[0]
        owner = replace(
            fixture.mappings[1],
            relationships=(
                replace(
                    relation,
                    resolver=replace(relation.resolver, dataset_id=table.dataset_id),
                ),
            ),
        )
        fixture.mappings = (parent, owner)
        fixture.save_mappings()
        response = fixture.client.post(
            f"/workspaces/{fixture.workspace_id}/mapping/value-choices",
            data={**fixture.choices_data(), "incoming_dataset_id": table.dataset_id},
            headers=POST_HEADERS,
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            Counter(
                item["status"] for item in response.json()["relationship_resolutions"]
            ),
            {"target": 11, "incoming": 7},
        )

    def test_mixed_terms_are_checked_on_both_majors_and_access_modes(self):
        for major in (19, 20):
            for mode in (OdooConnectionMode.LOCAL, OdooConnectionMode.REMOTE):
                with self.subTest(major=major, mode=mode):
                    fixture = self.fixture(major, mode)
                    before = fixture.context.queries.get_mapping_working_draft(
                        fixture.workspace_id
                    )
                    with (
                        collect_duckdb_request_timings() as timings,
                        count_duckdb_statements() as statements,
                    ):
                        response = fixture.client.post(
                            f"/workspaces/{fixture.workspace_id}/mapping/value-choices",
                            data=fixture.choices_data(),
                            headers=POST_HEADERS,
                        )
                    self.assertEqual(response.status_code, 200, response.text)
                    payload = response.json()
                    self.assertTrue(payload["incoming_table"]["checked"], payload)
                    self.assertEqual(
                        Counter(
                            item["status"]
                            for item in payload["relationship_resolutions"]
                        ),
                        {"target": 11, "incoming": 7},
                    )
                    self.assertNotIn("odoo_id", response.text)
                    self.assertEqual(
                        fixture.context.queries.get_mapping_working_draft(
                            fixture.workspace_id
                        ),
                        before,
                    )
                    # The cold route retains the existing admission/lookup/capture
                    # scopes and adds one bounded in-memory lookup producer.
                    self.assertLessEqual(timings.connection_count, 12)
                    self.assertLessEqual(timings.schema_check_count, 8)
                    self.assertLessEqual(len(statements), 200)
                    self.assertEqual(
                        sum(
                            "duckdb_columns()" in statement for statement in statements
                        ),
                        timings.schema_check_count,
                    )
                    self.assertEqual(timings.lock_retry_count, 0)

    def test_excluded_parent_term_is_missing_and_reopening_preserves_the_split(self):
        fixture = self.fixture()
        original = fixture.mappings[1].relationships[0]
        fixture.mappings = (
            replace(
                fixture.mappings[0],
                row_inclusion=RowInclusionPolicy(
                    mode=RowInclusionMode.MATCHING_ROWS,
                    conditions=(
                        RowInclusionCondition(
                            str(uuid4()),
                            fixture.link.name_column_key,
                            SelectionConditionOperator.NOT_EQUALS,
                            "TERM18",
                        ),
                    ),
                ),
            ),
            fixture.mappings[1],
        )
        fixture.save_mappings()
        for reopen in (False, True):
            if reopen:
                fixture.reopen(
                    readiness_reader=fixture.snapshots,
                    read_identity_probe=fixture.read_identity,
                )
            response = fixture.client.post(
                f"/workspaces/{fixture.workspace_id}/mapping/value-choices",
                data=fixture.choices_data(),
                headers=POST_HEADERS,
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(
                Counter(
                    item["status"]
                    for item in response.json()["relationship_resolutions"]
                ),
                {"target": 11, "incoming": 6, "missing": 1},
            )
            draft = fixture.context.queries.get_mapping_working_draft(
                fixture.workspace_id
            )
            actual = next(
                item
                for item in draft.definition.datasets
                if item.dataset_id == fixture.owner.dataset_id
            ).relationships[0]
            self.assertEqual(actual, original)

    def test_only_odoo_origin_does_not_scan_the_incoming_table(self):
        fixture = self.fixture()
        data = {**fixture.choices_data(), "resolver_origin": "target_catalog"}
        with patch.object(
            fixture.context.categorical_coverage,
            "source_identity_key_tuples",
            side_effect=AssertionError(
                "Odoo-only matching must not read incoming rows"
            ),
        ):
            response = fixture.client.post(
                f"/workspaces/{fixture.workspace_id}/mapping/value-choices",
                data=data,
                headers=POST_HEADERS,
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn("incoming_table", response.json())

    def test_saved_odoo_override_keeps_the_incoming_resolver_after_form_reopen(self):
        fixture = self.fixture()
        # First capture the same target choices the dialog needs to validate aliases.
        response = fixture.client.post(
            f"/workspaces/{fixture.workspace_id}/mapping/value-choices",
            data=fixture.choices_data(),
            headers=POST_HEADERS,
        )
        self.assertEqual(response.status_code, 200, response.text)
        index = next(
            index
            for index, item in enumerate(fixture.selection.datasets)
            if item.dataset_id == fixture.owner.dataset_id
        )
        url = f"/workspaces/{fixture.workspace_id}/mapping?mapping_dataset={index}"
        page = fixture.client.get(url)
        self.assertEqual(page.status_code, 200, page.text)
        form = MappingForm(page.text)
        matches = next(
            item
            for item in form.entries
            if item[0].startswith("relation_value_matches_") and "TERM01" in item[1]
        )
        matches[1] = json.dumps(
            [
                *json.loads(matches[1]),
                {"source_value": "TERM18", "target_value": "TERM01"},
            ]
        )
        saved = fixture.client.post(
            form.action,
            json={"entries": [*form.entries, ["action", "save_progress"]]},
            headers={**POST_HEADERS, "X-CSRF-Token": fixture.csrf},
        )
        self.assertEqual(saved.status_code, 200, saved.text)
        draft = fixture.context.queries.get_mapping_working_draft(fixture.workspace_id)
        relationship = next(
            item
            for item in draft.definition.datasets
            if item.dataset_id == fixture.owner.dataset_id
        ).relationships[0]
        self.assertEqual(relationship.resolver.origin.value, "target_then_dataset")
        self.assertEqual(relationship.resolver.dataset_id, fixture.parent.dataset_id)
        self.assertEqual(len(relationship.resolver.value_mappings), 12)
        reopened = MappingForm(fixture.client.get(url).text)
        self.assertTrue(
            any(
                '"source_value":"TERM18","target_value":"TERM01"' in value
                for name, value in reopened.entries
                if name.startswith("relation_value_matches_")
            )
        )

    def test_parent_identity_must_be_saved_before_claiming_a_match(self):
        fixture = self.fixture()
        fixture.mappings = (
            replace(fixture.mappings[0], source_identity_column_keys=()),
            fixture.mappings[1],
        )
        fixture.save_mappings()
        response = fixture.client.post(
            f"/workspaces/{fixture.workspace_id}/mapping/value-choices",
            data=fixture.choices_data(),
            headers=POST_HEADERS,
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(response.json()["incoming_table"]["checked"])
        self.assertEqual(
            Counter(
                item["status"] for item in response.json()["relationship_resolutions"]
            ),
            {"target": 11, "unavailable": 7},
        )
