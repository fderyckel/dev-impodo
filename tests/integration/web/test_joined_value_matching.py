"""Exercise generic joined Many2one choices and checked mapping publication."""

from dataclasses import replace
from datetime import UTC, datetime
from unittest import TestCase

from starlette.datastructures import FormData

from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from impodo.domain.mapping.contracts import (
    CategoricalCoveragePolicy,
    DatasetMapping,
    IdentityComponentMapping,
    ReferenceKeyMapping,
    RelationshipMapping,
    RelationshipResolver,
    ResolverOrigin,
    ValueMapping,
)
from impodo.domain.odoo.contracts import RecordSnapshot, TargetRecord
from impodo.domain.schema.governance import BusinessKeyDefinition, BusinessKeyStatus
from impodo.domain.shared.models import FieldMetadata, ModelMetadata
from impodo.domain.workspace.contracts import (
    OdooSchemaCatalog,
    SchemaField,
    SchemaModel,
    SchemaOrigin,
)
from impodo.domain.workspace.derived_entities import SourceDatasetUse
from impodo.domain.workspace.supporting_lookups import portable_supporting_value
from impodo.web.composition.page_reads import run_local_operation_sync
from impodo.web.exact_join_forms import (
    parse_exact_join_group_form,
    resolve_exact_join_group_request,
)
from impodo.web.presenters.mapping_forms import _related_business_keys
from tests.integration.web import test_exact_join_workflow as fixtures
from tests.support.browser_scenarios import POST_HEADERS, _browser_schema
from tests.support.identity import resolve_local_actor


class JoinedValueMatchingTests(TestCase):
    def test_joined_custom_many2one_choices_and_mapping_validation(self):
        fixture = fixtures.ExactJoinBrowserTests(methodName="runTest")
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        workspace_id, physical, by_name = fixture._frozen_customer_workspace(
            several_tables=True
        )
        context = fixture.app.state.context
        actor = resolve_local_actor(context)
        state = context.queries.get(workspace_id)
        state = context.workspace_states.update_target(
            workspace_id,
            actor=actor,
            expected_revision=state.revision,
            odoo_connection_mode="LOCAL",
            odoo_base_url="http://127.0.0.1:8069",
            odoo_database="odoo19_local",
            intended_applications=(),
            intended_models=("x_record",),
        )
        form = fixture._join_group_form(physical, by_name)
        items = [
            (name, item)
            for name, value in form.items()
            for item in (value if isinstance(value, list) else [value])
        ]
        request = resolve_exact_join_group_request(
            parse_exact_join_group_form(FormData(items)), physical
        )
        run_local_operation_sync(
            context.derived_entities.save_join_group,
            workspace_id,
            request,
            expected_parent_version=None,
            actor=actor,
        )
        effective = context.queries.get_mapping_source_selection(workspace_id)
        dataset = effective.datasets[0]
        columns = {column.source_name: column.stable_key for column in dataset.columns}

        def field(name, field_type="char", relation=None):
            return SchemaField(name, name, field_type, False, False, relation, None, ())

        schema = OdooSchemaCatalog(
            workspace_id=workspace_id,
            policy_hash="sha256:" + "1" * 64,
            captured_at=datetime.now(UTC),
            captured_by="Fixture",
            connection_mode="LOCAL",
            database="odoo19_local",
            odoo_version="19.0",
            content_hash="sha256:" + "2" * 64,
            models=(
                SchemaModel(
                    "x_record",
                    "Record",
                    (field("ref"), field("x_list_id", "many2one", "x_list")),
                ),
                SchemaModel("x_list", "List", (field("ref"), field("name"))),
            ),
            origin=SchemaOrigin.LIVE_API,
            connection_target_hash=_browser_schema(state).fingerprint.target_hash,
            read_credential_binding_hash="sha256:" + "3" * 64,
            read_principal_hash="sha256:" + "4" * 64,
            read_permission_hash="sha256:" + "5" * 64,
            read_context_hash="sha256:" + "6" * 64,
        )
        context.schema_workspace.schemas.save_odoo_schema_catalog(
            workspace_id, schema, actor=actor
        )
        keys = tuple(
            BusinessKeyDefinition(
                f"{model}:ref", model, ("ref",), status=BusinessKeyStatus.CONFIRMED
            )
            for model in ("x_record", "x_list")
        )
        context.schema_workspace.govern(workspace_id, business_keys=keys, actor=actor)
        calls = []

        def read_choices(workspace, metadata_requests, record_requests):
            calls.append(record_requests)
            metadata = replace(
                _browser_schema(workspace),
                models={
                    "x_list": ModelMetadata(
                        "x_list",
                        None,
                        {
                            "ref": FieldMetadata("ref", "char", "ref"),
                            "name": FieldMetadata("name", "char", "name"),
                        },
                    )
                },
            )
            return metadata, RecordSnapshot(
                fingerprint=metadata.fingerprint,
                records={
                    "x_list": (
                        TargetRecord("x_list", 91001, {"ref": "A", "name": "First"}),
                        TargetRecord("x_list", 91002, {"ref": "B", "name": "Second"}),
                    )
                },
                requested_fields={"x_list": ("ref", "name")},
            )

        context.readiness_reader = read_choices
        with collect_duckdb_request_timings() as timings:
            response = fixture.client.post(
                f"/workspaces/{workspace_id}/mapping/value-choices",
                data={
                    "csrf_token": fixture.csrf,
                    "kind": "relationship",
                    "dataset_id": dataset.dataset_id,
                    "source_column_key": columns["name"],
                    "target_model": "x_record",
                    "target_field": "x_list_id",
                    "business_key_id": "x_list:ref",
                },
                headers=POST_HEADERS,
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            response.json()["source_choices"],
            [
                {"value": "Fictional Retail", "count": 1},
                {"value": "United Caps", "count": 1},
            ],
        )
        self.assertNotIn("Unused Party", response.text)
        self.assertNotIn("91001", response.text)
        self.assertEqual(len(calls), 1)
        # Bound authorization, local source projection, and lookup publication
        # owners; the join adds one in-memory owner, never one per source row.
        self.assertLessEqual(timings.connection_count, 13)
        self.assertLessEqual(timings.schema_check_count, 3)
        relation = RelationshipMapping(
            target_field="x_list_id",
            kind="many2one",
            source_column_keys=(columns["name"],),
            resolver=RelationshipResolver(
                origin=ResolverOrigin.TARGET_CATALOG,
                model="x_list",
                key_mappings=(ReferenceKeyMapping(columns["name"], "ref"),),
                value_mappings=(
                    ValueMapping("United Caps", "A"),
                    ValueMapping("Fictional Retail", "B"),
                ),
            ),
            categorical_policy=CategoricalCoveragePolicy.EXPLICIT_KEY_MATCH,
        )
        mapping = DatasetMapping(
            dataset_id=dataset.dataset_id,
            target_model="x_record",
            source_identity_column_keys=(columns["account_number"],),
            target_identity=(
                IdentityComponentMapping(
                    (columns["account_number"],), ("ref",), "string"
                ),
            ),
            relationships=(relation,),
        )
        revision, validation = run_local_operation_sync(
            context.mapping_workspace.check_definition,
            workspace_id,
            datasets=(mapping,),
            expected_parent_version=None,
            expected_working_draft_version=None,
            actor=actor,
        )
        self.assertNotEqual(
            validation.status.value,
            "INVALID",
            [(item.code, item.message) for item in validation.issues],
        )
        self.assertEqual(
            validation.categorical_coverage.field_results[0].status, "COVERED"
        )
        self.assertEqual(len(validation.categorical_coverage.source_snapshot_hashes), 4)
        self.assertEqual(
            revision.definition.datasets[0].relationships[0].resolver.value_mappings,
            relation.resolver.value_mappings,
        )

        # A second mapping dataset must not disable choices or categorical
        # validation for the existing, independently reviewed combined table.
        plan = context.derived_entities.get_derived_entity_plan(workspace_id)
        run_local_operation_sync(
            context.derived_entities.save_dataset_uses, workspace_id,
            dataset_uses={item.dataset_id: SourceDatasetUse.MAP_AND_PREPARE
                if item.dataset_id == physical.datasets[0].dataset_id else item.use
                for item in plan.dataset_uses},
            expected_parent_version=plan.version, actor=actor,
        )
        effective = context.queries.get_mapping_source_selection(workspace_id)
        self.assertEqual(len(effective.datasets), 2)
        with collect_duckdb_request_timings() as timings:
            multiple = fixture.client.post(
                f"/workspaces/{workspace_id}/mapping/value-choices",
                data={"csrf_token": fixture.csrf, "kind": "relationship", "dataset_id": dataset.dataset_id,
                    "source_column_key": columns["name"], "target_model": "x_record", "target_field": "x_list_id", "business_key_id": "x_list:ref"},
                headers=POST_HEADERS,
            )
        self.assertEqual(multiple.status_code, 200, multiple.text)
        self.assertEqual(multiple.json()["source_choices"], response.json()["source_choices"])
        self.assertLessEqual(timings.connection_count, 13)
        self.assertLessEqual(timings.schema_check_count, 3)
        checked = run_local_operation_sync(context.categorical_coverage.collect, workspace_id,
            replace(revision.definition, source_selection_hash=effective.content_hash), effective, schema)
        self.assertEqual(checked.issues, ())

        # A related model can remain outside the migration scope. Its generic
        # name-within-company candidate must work with the same joined source.
        schema = replace(
            schema, models=(schema.models[0],), content_hash="sha256:" + "7" * 64
        )
        context.schema_workspace.schemas.save_odoo_schema_catalog(
            workspace_id, schema, actor=actor
        )
        governance = context.schema_workspace.govern(
            workspace_id, business_keys=keys[:1], actor=actor
        )
        scoped_key = next(
            key
            for key in _related_business_keys(governance.business_keys, "x_list")
            if key.scope_fields == ("company_id",)
        )

        def read_scoped_choices(workspace, metadata_requests, record_requests):
            metadata = replace(
                _browser_schema(workspace),
                models={
                    "x_list": ModelMetadata(
                        "x_list",
                        None,
                        {
                            "name": FieldMetadata("name", "char", "name"),
                            "company_id": FieldMetadata(
                                "company_id",
                                "many2one",
                                "Company",
                                relation="res.company",
                            ),
                        },
                    )
                },
            )
            records = tuple(
                TargetRecord(
                    "x_list", number, {"name": name, "company_id": (company, label)}
                )
                for number, name, company, label in (
                    (92001, "Standard", 1, "First company"),
                    (92002, "Standard", 2, "Second company"),
                    (92003, "Repeated", 1, "First company"),
                    (92004, "Repeated", 1, "First company"),
                )
            )
            return metadata, RecordSnapshot(
                fingerprint=metadata.fingerprint,
                records={"x_list": records},
                requested_fields={"x_list": ("name", "company_id")},
            )

        context.readiness_reader = read_scoped_choices
        response = fixture.client.post(
            f"/workspaces/{workspace_id}/mapping/value-choices",
            data={
                "csrf_token": fixture.csrf,
                "kind": "relationship",
                "dataset_id": dataset.dataset_id,
                "source_column_key": columns["name"],
                "target_model": "x_record",
                "target_field": "x_list_id",
                "business_key_id": scoped_key.key_id,
            },
            headers=POST_HEADERS,
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            {choice["value"] for choice in response.json()["target_choices"]},
            {
                portable_supporting_value(("Standard", "First company")),
                portable_supporting_value(("Standard", "Second company")),
            },
        )
        self.assertEqual(
            response.json()["ambiguous_values"],
            [portable_supporting_value(("Repeated", "First company"))],
        )
        self.assertNotIn("92001", response.text)
        self.assertEqual(
            context.queries.get(workspace_id).intended_models, ("x_record",)
        )
