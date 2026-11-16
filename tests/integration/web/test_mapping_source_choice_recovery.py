"""Save a real partial correction when a generated source choice was added."""

from dataclasses import replace
from unittest import TestCase
from unittest.mock import PropertyMock, patch
from uuid import uuid4

from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from impodo.domain.mapping.contracts import TargetFieldDisposition, TargetFieldHandling
from impodo.domain.workspace.contracts import SchemaField
from impodo.domain.workspace.derived_entities import (
    HierarchyConsumerBinding,
    derived_dataset_links,
)
from impodo.web.presenters.mapping_forms import _active_mapping_definition
from tests.integration.web import test_hierarchy_consumer_preparation as helpers
from tests.support.browser_scenarios import POST_HEADERS
from tests.support.database_probe import count_duckdb_statements
from tests.support.hierarchy_correction import MappingForm


class MappingSourceChoiceRecoveryTests(TestCase):
    def setUp(self):
        self.helper = helpers.HierarchyConsumerPreparationTests(methodName="runTest")
        self.helper.setUp()
        self.addCleanup(self.helper.doCleanups)
        self.fixture = self.helper.fixture

    def correction(self, major, mode):
        fixture = self.fixture
        context, actor = fixture.context, fixture.actor
        with patch.object(
            HierarchyConsumerBinding,
            "company_source_column_key",
            new_callable=PropertyMock,
            return_value=None,
        ):
            workspace_id = self.helper.hierarchy(major, mode)
            schema = context.queries.get_odoo_schema_catalog(workspace_id)
            keys = context.queries.get_schema_governance(workspace_id).business_keys
            schema = replace(
                schema,
                content_hash="sha256:" + "7" * 64,
                read_context_hash=context.queries.get(
                    workspace_id
                ).odoo_company.context_hash,
                models=tuple(
                    replace(
                        model,
                        fields=(
                            *model.fields,
                            SchemaField(
                                "x_kind",
                                "Kind",
                                "char",
                                True,
                                False,
                                None,
                                None,
                                (),
                                create_default_present=True,
                                create_default_value="internal",
                            ),
                        ),
                    )
                    if model.name == helpers.existing.MODEL
                    else model
                    for model in schema.models
                ),
            )
            context.schema_workspace.schemas.save_odoo_schema_catalog(
                workspace_id, schema, actor=actor
            )
            context.schema_workspace.govern(
                workspace_id, business_keys=keys, actor=actor
            )
            datasets = tuple(
                replace(
                    mapping,
                    target_field_dispositions=(
                        TargetFieldDisposition(
                            "x_kind", TargetFieldHandling.ODOO_DEFAULT
                        ),
                    ),
                )
                for mapping in self.helper.mappings(workspace_id, wrong_company=True)
            )
            original = fixture.submit(workspace_id, datasets)
        selection = context.queries.get_mapping_source_selection(workspace_id)
        physical = context.queries.get_source_selection(workspace_id).datasets[0]
        index = next(
            i
            for i, item in enumerate(selection.datasets)
            if item.dataset_id == physical.dataset_id
        )
        page = fixture.client.get(
            f"/workspaces/{workspace_id}/mapping?mapping_dataset={index}&step=tables&task=identity"
        )
        self.assertEqual(page.status_code, 200, page.text[:500])
        form = MappingForm(page.text)
        link = derived_dataset_links(
            context.queries.get_derived_entity_plan(workspace_id)
        )[0]
        key = next(k for k in keys if k.model == helpers.existing.MODEL)
        slot = len(key.key_fields) + key.scope_fields.index("company_id")
        field_name = f"identity_source_{index}_{slot}"
        entries = [
            e
            for e in form.entries
            if e[0] not in ("action", "operation_id", field_name)
        ]
        entries.extend(
            [
                [field_name, link.company_column_key],
                ["action", "save_progress"],
                ["operation_id", str(uuid4())],
            ]
        )
        # Keep the inactive table's required default exactly as rendered.
        hidden_index = next(
            i
            for i, item in enumerate(selection.datasets)
            if item.dataset_id == link.derived_dataset_id
        )
        self.assertIn(
            [f"target_field_disposition_{hidden_index}", "x_kind:odoo_default"], entries
        )
        governance = context.queries.get_schema_governance(workspace_id)
        # An editor baseline does not authorize confirmation of stale rows.
        self.assertIsNone(
            _active_mapping_definition(
                context, workspace_id, selection, schema, governance
            )
        )
        stale_rows = [
            entry for entry in entries if entry[0] not in ("action", "operation_id")
        ]
        stale_rows.extend([["action", "confirm_rows"], ["operation_id", str(uuid4())]])
        stale_confirmation = fixture.client.post(
            form.action,
            json={"entries": stale_rows},
            headers={**POST_HEADERS, "X-CSRF-Token": fixture.csrf},
        )
        self.assertEqual(stale_confirmation.status_code, 422, stale_confirmation.text)
        self.assertIn(
            "Check the current mapping before confirming rows", stale_confirmation.text
        )
        tampered = [
            [
                name,
                "x_kind:odoo_managed"
                if name == f"target_field_disposition_{hidden_index}"
                else value,
            ]
            for name, value in entries
            if name != "operation_id"
        ]
        tampered.append(["operation_id", str(uuid4())])
        rejected = fixture.client.post(
            form.action,
            json={"entries": tampered},
            headers={**POST_HEADERS, "X-CSRF-Token": fixture.csrf},
        )
        self.assertEqual(rejected.status_code, 422, rejected.text)
        self.assertIn("required-field choices", rejected.text)
        with (
            collect_duckdb_request_timings() as timings,
            count_duckdb_statements() as sql,
        ):
            response = fixture.client.post(
                form.action,
                json={"entries": entries},
                headers={**POST_HEADERS, "X-CSRF-Token": fixture.csrf},
            )
        self.assertEqual(response.status_code, 200, response.text[:1500])
        self.assertEqual(response.json()["status"], "committed", response.text)
        saved = context.queries.get_mapping_working_draft(workspace_id).definition
        original_parent = next(
            item
            for item in original.definition.datasets
            if item.dataset_id == link.derived_dataset_id
        )
        saved_parent = next(
            item
            for item in saved.datasets
            if item.dataset_id == link.derived_dataset_id
        )
        self.assertEqual(saved_parent, original_parent)
        child = next(
            item for item in saved.datasets if item.dataset_id == physical.dataset_id
        )
        self.assertEqual(
            child.target_scope[1].source_column_keys, (link.company_column_key,)
        )
        self.assertEqual(saved.source_selection_hash, selection.content_hash)
        # Admission plus capture/publication/render phases each release their
        # local registry/workspace/Data version/engine owners independently.
        self.assertLessEqual(timings.connection_count, 18)
        self.assertLessEqual(len(sql), 600)
        self.assertEqual(timings.lock_retry_count, 0)
        fixture.submit(workspace_id, saved.datasets)
        _staged, quality = fixture.prepare(workspace_id)
        self.assertEqual((quality.blocked_count, quality.quarantined_count), (0, 0))

    def test_odoo19_local(self):
        self.correction(19, "LOCAL")

    def test_odoo20_local(self):
        self.correction(20, "LOCAL")

    def test_odoo19_remote(self):
        self.correction(19, "REMOTE")

    def test_odoo20_remote(self):
        self.correction(20, "REMOTE")
