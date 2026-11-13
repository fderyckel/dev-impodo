"""Exercise the real Check matches action on generic company-scoped hierarchies."""

from dataclasses import replace
from unittest import TestCase
from unittest.mock import patch
from uuid import uuid4

from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from impodo.adapters.duckdb.unit_of_work import _read_databases
from impodo.application.hierarchy_parent_lookup import HierarchyParentChoice
from impodo.domain.mapping.contracts import (
    DatasetMapping,
    IdentityComponentMapping,
    IdentityNullPolicy,
    ReferenceKeyMapping,
    RelationshipResolver,
    ResolverOrigin,
)
from impodo.domain.workspace.derived_entities import (
    SourceDatasetUse,
    derived_dataset_links,
)
from impodo.web.composition.page_reads import run_local_operation_sync
from tests.integration.web import test_existing_hierarchy_parents as existing
from tests.support.browser_scenarios import POST_HEADERS
from tests.support.database_probe import count_duckdb_statements
from tests.support.hierarchy_correction import MappingForm


class GeneratedHierarchyCheckTests(TestCase):
    def setUp(self):
        self.fixture = existing.ExistingHierarchyParentWebTests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_odoo19_local_check(self):
        self.check(19, "LOCAL")

    def test_odoo20_local_check(self):
        self.check(20, "LOCAL")

    def test_odoo19_remote_check(self):
        self.check(19, "REMOTE")

    def test_odoo20_remote_check(self):
        self.check(20, "REMOTE")

    def check(self, major, mode):
        fixture = self.fixture
        context, actor = fixture.context, fixture.actor
        workspace_id, form, schema = fixture.configure(major, mode)
        form.update(hierarchy_company_id="7", hierarchy_start="existing")
        prefix = f"/workspaces/{workspace_id}/derived-entities/hierarchy"
        response = fixture._post(prefix + "/parents", form)
        self.assertEqual(response.status_code, 200, response.text[:1000])
        form["existing_parent_choice"] = HierarchyParentChoice(
            ("Physical", "Stock"), (None, "Example")
        ).value
        response = fixture._post(prefix + "/save", form)
        self.assertEqual(response.status_code, 303, response.text[:1000])
        # This synthetic schema was captured before the parent picker selected
        # its company. Represent a completed Stage 2 capture in that context.
        state = context.queries.get(workspace_id)
        schema = replace(
            schema,
            read_context_hash=state.odoo_company.context_hash,
            models=tuple(
                model for model in schema.models if model.name != "res.company"
            ),
        )
        keys = tuple(
            key
            for key in context.queries.get_schema_governance(workspace_id).business_keys
            if key.model != "res.company"
        )
        context.schema_workspace.schemas.save_odoo_schema_catalog(
            workspace_id, schema, actor=actor
        )
        context.schema_workspace.govern(workspace_id, business_keys=keys, actor=actor)
        plan = context.queries.get_derived_entity_plan(workspace_id)
        link = derived_dataset_links(plan)[0]
        run_local_operation_sync(
            context.derived_entities.save_dataset_uses,
            workspace_id,
            dataset_uses={
                plan.rules[0].source_dataset_id: SourceDatasetUse.PREPARATION_ONLY
            },
            expected_parent_version=plan.version,
            actor=actor,
        )
        mapping = DatasetMapping(
            link.derived_dataset_id,
            existing.MODEL,
            source_identity_column_keys=(link.canonical_key_column_key,),
            target_identity=(
                IdentityComponentMapping((link.name_column_key,), ("name",)),
            ),
            target_scope=(
                IdentityComponentMapping(
                    (link.parent_key_column_key,),
                    ("container_id",),
                    resolver=RelationshipResolver(
                        ResolverOrigin.DATASET, dataset_id=link.derived_dataset_id
                    ),
                    null_policy=IdentityNullPolicy.EXPLICIT_SCOPE_NULL,
                ),
                IdentityComponentMapping(
                    (link.company_column_key,),
                    ("company_id",),
                    resolver=RelationshipResolver(
                        ResolverOrigin.TARGET_CATALOG,
                        model="res.company",
                        key_mappings=(
                            ReferenceKeyMapping(link.company_column_key, "name"),
                        ),
                    ),
                    null_policy=IdentityNullPolicy.EXPLICIT_SCOPE_NULL,
                ),
            ),
        )
        current = context.queries.get_mapping_working_draft(workspace_id)
        run_local_operation_sync(
            context.mapping_workspace.save_working_draft,
            workspace_id,
            datasets=(mapping,),
            expected_version=current.version if current else None,
            actor=actor,
        )
        page = fixture.client.get(
            f"/workspaces/{workspace_id}/mapping?step=tables&task=identity"
        )
        self.assertEqual(page.status_code, 200, page.text[:1000])
        browser_form = MappingForm(page.text)
        calls = []
        reader = context.readiness_reader
        original = reader.side_effect if reader is not None else None

        def read(state, metadata_requests, record_requests):
            self.assertIsNone(getattr(_read_databases, "connections", None))
            calls.extend(record_requests)
            return original(state, metadata_requests, record_requests)

        operation_id = str(uuid4())
        entries = [
            entry
            for entry in browser_form.entries
            if entry[0] not in {"operation_id", "action"}
        ]
        entries.extend([["operation_id", operation_id], ["action", "draft"]])
        with (
            patch.object(context, "readiness_reader", side_effect=read)
            if reader is not None
            else patch.object(context, "readiness_reader", None)
        ):
            with (
                count_duckdb_statements() as statements,
                collect_duckdb_request_timings() as timings,
            ):
                checked = fixture.client.post(
                    browser_form.action,
                    json={"entries": entries},
                    headers={**POST_HEADERS, "X-CSRF-Token": fixture.csrf},
                )
        self.assertEqual(checked.status_code, 200, checked.text[:3000])
        self.assertEqual(checked.json()["status"], "committed", checked.text)
        receipt = context.mapping_workspace.get_mutation_receipt(
            workspace_id, operation_id, actor=actor
        )
        self.assertEqual(receipt.state.value, "COMMITTED")
        self.assertEqual(timings.lock_retry_count, 0)
        self.assertTrue(
            any("lookup_records" in sql for sql in statements), checked.text
        )
        self.assertFalse(
            any("list(DISTINCT input.source_row" in sql for sql in statements)
        )
        if mode == "REMOTE":
            company_reads = [
                request for request in calls if request.model == "res.company"
            ]
            self.assertEqual(len(company_reads), 1)
            self.assertIn(("name", "in", ["Example"]), company_reads[0].domain)
        staging, quality = fixture.prepare(workspace_id)
        self.assertEqual((quality.blocked_count, quality.quarantined_count), (0, 0))
        self.assertEqual(len(staging.rows), 6)
