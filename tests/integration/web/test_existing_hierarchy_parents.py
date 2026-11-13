"""Exercise Stage 1 parent selection, company scope, and full preparation."""

from dataclasses import replace
from unittest.mock import patch

from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from impodo.adapters.duckdb.unit_of_work import _read_databases
from impodo.adapters.odoo.connectors import Json2ReadConnector
from impodo.application.hierarchy_parent_lookup import HierarchyParentChoice
from impodo.domain.compiler.browser_mapping_compiler import compile_browser_mapping
from impodo.domain.execution.planner import plan_preflight_requirements
from impodo.domain.preflight.frozen_input import _prepared_record
from impodo.domain.preparation.preflight import PreflightEngine
from impodo.domain.preparation.source import PreparedBundle
from impodo.domain.workspace.reference_keys import reference_policy_hash
from impodo.domain.mapping.contracts import (
    DatasetMapping,
    IdentityComponentMapping,
    IdentityNullPolicy,
    ReferenceKeyMapping,
    RelationshipResolver,
    ResolverOrigin,
)
from impodo.domain.odoo.company import OdooCompanyAccess, OdooCompanySelection
from impodo.domain.odoo.contracts import RecordSnapshot
from impodo.domain.schema.governance import BusinessKeyDefinition, BusinessKeyStatus
from impodo.domain.shared.models import OdooReadIdentity, TargetRecord
from impodo.domain.workspace.contracts import SchemaField, SchemaModel
from impodo.domain.workspace.derived_entities import (
    SourceDatasetUse,
    derived_dataset_links,
)
from impodo.web.composition.page_reads import run_local_operation_sync
from impodo.web.target_credentials import TargetCredentialRole, store_target_credential
from tests.support.database_probe import count_duckdb_statements
from tests.support.hierarchy_workflow import HierarchyWorkflowFixture
from tests.support.hierarchy_odoo import HierarchyOdoo
from tests.support.hierarchy_transport import HierarchyParentTransport, odoo_hierarchy_domain
from tests.support.recipe_lifecycle import metadata_for_schema

MODEL = "x.storage.place"


class ExistingHierarchyParentWebTests(HierarchyWorkflowFixture):
    def test_remote_picker_reads_ordinary_parents_through_the_real_json2_adapter(self):
        for major in (19, 20):
            with self.subTest(major=major):
                workspace_id, form, schema = self.configure(major, "REMOTE")
                form["hierarchy_company_id"] = "7"
                form["hierarchy_start"] = "existing"
                wire = HierarchyParentTransport(
                    major, MODEL, "container_id", "company_id",
                    odoo_hierarchy_domain("company_id", "[('kind', '=', 'folder')]"),
                    before_call=lambda: self.assertFalse(getattr(_read_databases, "connections", None)),
                )

                def identity(state, credential, models):
                    self.assertFalse(getattr(_read_databases, "connections", None))
                    return OdooReadIdentity(
                        schema.connection_target_hash, schema.read_principal_hash,
                        schema.read_permission_hash, state.odoo_company.context_hash,
                        tuple(models), "2026-11-13T00:00:00Z",
                    )

                with (
                    patch.object(self.context, "readiness_reader", None),
                    patch.object(self.context, "read_identity_probe", side_effect=identity),
                    patch("impodo.web.composition.target_readers.Json2ReadConnector",
                          side_effect=lambda config: Json2ReadConnector(config, transport=wire)),
                ):
                    prefix = f"/workspaces/{workspace_id}/derived-entities/hierarchy"
                    with collect_duckdb_request_timings() as timings, count_duckdb_statements() as statements:
                        response = self._post(prefix + "/parents", form)
                    self.assertEqual(response.status_code, 200, response.text[:300])
                    self.assertIn("Physical / Stock", response.text)
                    self.assertLessEqual(timings.connection_count, 2 + 4 * 3)
                    self.assertLessEqual(timings.schema_check_count, 10)
                    self.assertLessEqual(len(statements), 220)
                    reads = [(url, payload) for url, payload in wire.calls if url.endswith("/search_read")]
                    self.assertEqual(len(reads), 3)
                    self.assertEqual(reads[0][1]["domain"], [
                        ["name", "ilike", "Stock"], ["active", "=", True],
                        ["company_id", "in", [7, False]], ["kind", "=", "folder"],
                        "|", ["company_id", "=", False], ["company_id", "=", 7],
                    ])
                    self.assertEqual(reads[0][1]["context"]["allowed_company_ids"], [7])
                    self.assertEqual(reads[0][1]["limit"], 51)
                    # Only one full metadata capture in each of the initial
                    # and selected-company contexts; no per-record lookups.
                    self.assertEqual(sum(url.endswith("/fields_get") for url, _ in wire.calls), 2)
                    form["existing_parent_choice"] = HierarchyParentChoice(
                        ("Physical", "Stock"), (None, "Example")
                    ).value
                    preview = self._post(prefix + "/preview", form)
                    self.assertEqual(preview.status_code, 200, preview.text[:300])
                    saved = self._post(prefix + "/save", form)
                    self.assertEqual(saved.status_code, 303, saved.text[:300])
                self.prepare_hierarchy(workspace_id)

    def test_missing_model_discovery_keeps_the_editor_and_inline_recovery(self):
        workspace_id, form, _schema = self.configure(20, "REMOTE")
        form["hierarchy_company_id"] = "7"
        form["hierarchy_start"] = "existing"
        with patch.object(self.context.queries, "get_odoo_model_catalog", return_value=None):
            response = self._post(
                f"/workspaces/{workspace_id}/derived-entities/hierarchy/parents", form
            )
        self.assertEqual(response.status_code, 422)
        self.assertIn("data-hierarchy-editor", response.text)
        self.assertIn("data-source-feedback", response.text)
        self.assertIn('value="location_hierarchy"', response.text)
        self.assertIn('value="Stock"', response.text)
        self.assertEqual(self.calls, [])
        self.assertIsNone(self.context.queries.get_derived_entity_plan(workspace_id))

    def test_starting_parent_is_explicit_and_a_hidden_old_choice_is_ignored(self):
        for major in (19, 20):
            with self.subTest(major=major):
                workspace_id, form, _schema = self.configure(major, "REMOTE")
                prefix = f"/workspaces/{workspace_id}/derived-entities/hierarchy"
                form["hierarchy_start"] = "existing"
                rejected = self._post(prefix + "/preview", form)
                self.assertEqual(rejected.status_code, 422)
                self.assertIn("Choose a Starting parent in Odoo", rejected.text)
                self.assertIn("data-source-feedback", rejected.text)
                self.assertEqual(self.calls, [])
                form["hierarchy_start"] = "source"
                form["existing_parent_choice"] = HierarchyParentChoice(
                    ("An old selection",), ("Example",)
                ).value
                preview = self._post(prefix + "/preview", form)
                self.assertEqual(preview.status_code, 200)
                saved = self._post(prefix + "/save", form)
                self.assertEqual(saved.status_code, 303)
                plan = self.context.queries.get_derived_entity_plan(workspace_id)
                self.assertIsNone(plan.rules[0].existing_parent)
                self.assertEqual(self.calls, [])

    def test_changed_checking_key_rejects_publication_and_keeps_the_form(self):
        workspace_id, form, _schema = self.configure(20, "REMOTE")
        form["hierarchy_company_id"] = "7"
        reader = self.context.readiness_reader
        original = reader.side_effect
        rotated = False

        def rotate_key(state, metadata_requests, record_requests):
            nonlocal rotated
            response = original(state, metadata_requests, record_requests)
            if not rotated:
                rotated = True
                owner = self.context.target_credential_workspace(
                    workspace_id,
                    workspace_state=self.context.queries.get(workspace_id),
                    actor=self.actor,
                )
                store_target_credential(
                    self.context.secret_store,
                    owner,
                    TargetCredentialRole.READ,
                    "replacement-fictional-key",
                    persistent=False,
                )
            return response

        reader.side_effect = rotate_key
        response = self._post(
            f"/workspaces/{workspace_id}/derived-entities/hierarchy/parents", form
        )
        self.assertEqual(response.status_code, 422)
        self.assertIn("checking key changed", response.text)
        self.assertIn('value="location_hierarchy"', response.text)
        self.assertIsNone(self.context.queries.get_derived_entity_plan(workspace_id))
        self.assertIsNone(self.context.queries.get(workspace_id).odoo_company)

    def configure(self, major, mode):
        workspace_id = self.freeze_files({"places": b"Aisle,Bin\nA01,B01\nA02,B02\n"})
        fields = (
            SchemaField("name", "Name", "char", True, False, None, None, ()),
            SchemaField(
                "container_id", "Parent", "many2one", False, False, MODEL, None, ()
            ),
            SchemaField(
                "company_id",
                "Company",
                "many2one",
                False,
                False,
                "res.company",
                None,
                (),
            ),
            SchemaField("active", "Active", "boolean", False, False, None, None, ()),
        )
        schema = self.schema(
            workspace_id,
            major=major,
            mode=mode,
            extra_models=(
                SchemaModel(MODEL, "Place", fields),
                SchemaModel(
                    "res.company",
                    "Company",
                    (SchemaField("name", "Name", "char", True, False, None, None, ()),),
                ),
            ),
            extra_keys=(
                BusinessKeyDefinition(
                    "place:name-parent-company",
                    MODEL,
                    ("name",),
                    scope_fields=("container_id", "company_id"),
                    status=BusinessKeyStatus.CONFIRMED,
                ),
                BusinessKeyDefinition(
                    "company:name",
                    "res.company",
                    ("name",),
                    status=BusinessKeyStatus.CONFIRMED,
                ),
            ),
        )
        metadata = metadata_for_schema(schema)
        catalog = RecordSnapshot(
            metadata.fingerprint,
            {
                "ir.model": (
                    TargetRecord(
                        "ir.model",
                        1,
                        {
                            "name": "Place",
                            "model": MODEL,
                            "abstract": False,
                            "transient": False,
                            "modules": "fictional",
                            "state": "manual",
                        },
                    ),
                )
            },
            {
                "ir.model": (
                    "name",
                    "model",
                    "abstract",
                    "transient",
                    "modules",
                    "state",
                )
            },
        )
        run_local_operation_sync(
            self.context.schema_workspace.discover_models,
            workspace_id,
            catalog,
            read_credential_binding_hash=schema.read_credential_binding_hash,
            read_identity=OdooReadIdentity(
                schema.connection_target_hash,
                schema.read_principal_hash,
                schema.read_permission_hash,
                schema.read_context_hash,
                ("ir.model",),
                "2026-11-12T00:00:00Z",
            )
            if mode == "REMOTE"
            else None,
            actor=self.actor,
        )
        source = self.context.queries.get_source_selection(workspace_id).datasets[0]
        self.calls = []

        def company_access(*args):
            self.assertFalse(getattr(_read_databases, "connections", None))
            return OdooCompanyAccess(
                "fictional.reader",
                "Example",
                (OdooCompanySelection(schema.connection_target_hash, 7, "Example"),),
            )

        def read_metadata(*args):
            self.assertFalse(getattr(_read_databases, "connections", None))
            return metadata

        def read_records(state, metadata_requests, record_requests):
            self.assertFalse(getattr(_read_databases, "connections", None))
            self.assertEqual(state.odoo_company.company_id, 7)
            request = record_requests[0]
            self.calls.append(request)
            if request.model == "res.company":
                records = (TargetRecord("res.company", 7, {"name": "Example"}),)
            elif request.domain[0][0] == "id":
                records = (
                    TargetRecord(
                        MODEL,
                        10,
                        {
                            "name": "Physical",
                            "container_id": False,
                            "company_id": False,
                            "active": True,
                        },
                    ),
                )
            else:
                records = (
                    TargetRecord(
                        MODEL,
                        11,
                        {
                            "name": "Stock",
                            "container_id": 10,
                            "company_id": 7,
                            "active": True,
                        },
                    ),
                )
            return metadata, RecordSnapshot(
                metadata.fingerprint,
                {request.model: records},
                {request.model: request.fields},
            )

        self.enterContext(
            patch(
                "impodo.web.composition.hierarchy_parents._selected_local_profile",
                return_value=None,
            )
        )
        self.enterContext(
            patch(
                "impodo.web.composition.hierarchy_parents.read_company_access",
                side_effect=company_access,
            )
        )
        self.enterContext(
            patch.object(self.context, "schema_reader", side_effect=read_metadata)
        )
        self.enterContext(
            patch.object(self.context, "readiness_reader", side_effect=read_records)
        )
        if mode == "LOCAL":
            self.enterContext(
                patch(
                    "impodo.web.composition.hierarchy_parents._selected_local_profile",
                    return_value=object(),
                )
            )
            self.enterContext(
                patch.object(
                    self.context.local_odoo_reader,
                    "get_model_metadata",
                    side_effect=read_metadata,
                )
            )
            self.enterContext(
                patch.object(
                    self.context.local_odoo_reader,
                    "get_company_access",
                    side_effect=company_access,
                )
            )

            def local_records(
                state, profile, metadata_requests, record_requests, **options
            ):
                if (
                    record_requests[0].model == MODEL
                    and record_requests[0].domain[0][0] != "id"
                ):
                    self.assertEqual(
                        options.get("lookup_relationship"), (MODEL, "container_id")
                    )
                    self.assertEqual(options.get("hierarchy_company_field"), "company_id")
                return read_records(state, metadata_requests, record_requests)

            self.enterContext(
                patch.object(
                    self.context.local_odoo_reader,
                    "get_preflight_snapshots",
                    side_effect=local_records,
                )
            )
            self.enterContext(patch.object(self.context, "readiness_reader", None))
        columns = {column.source_name: column.stable_key for column in source.columns}
        form = {
            "csrf_token": self.csrf,
            "expected_parent_version": "",
            "source_dataset_id": source.dataset_id,
            "hierarchy_level_1": columns["Aisle"],
            "hierarchy_level_2": columns["Bin"],
            "output_dataset_name": "location_hierarchy",
            "target_model": MODEL,
            "target_name_field": "name",
            "external_id_namespace": "fictional",
            "missing_parent_mode": "block",
            "missing_leaf": "use_deepest",
            "all_blank_mode": "emit_null_reference",
            "existing_parent_search": "Stock",
            "existing_source_levels": "0",
            "hierarchy_parent_field": "",
            "hierarchy_company_id": "",
        }
        return workspace_id, form, schema

    def test_large_and_empty_picker_results_keep_the_selected_path(self):
        workspace_id, form, _schema = self.configure(20, "REMOTE")
        form["hierarchy_company_id"] = "7"
        form["existing_parent_search"] = ""
        form["existing_parent_choice"] = HierarchyParentChoice(
            ("Physical", "Stock"), (None, "Example")
        ).value
        reader = self.context.readiness_reader
        original = reader.side_effect
        count = 51

        def picker_records(state, metadata_requests, record_requests):
            metadata, captured = original(state, metadata_requests, record_requests)
            request = record_requests[0]
            if request.model == MODEL and ("active", "=", True) in request.domain:
                captured = replace(
                    captured,
                    records={
                        MODEL: tuple(
                            TargetRecord(
                                MODEL,
                                100 + number,
                                {
                                    "name": f"Place {number}",
                                    "container_id": 10,
                                    "company_id": 7,
                                    "active": True,
                                },
                            )
                            for number in range(count)
                        )
                    },
                )
            return metadata, captured

        reader.side_effect = picker_records
        prefix = f"/workspaces/{workspace_id}/derived-entities/hierarchy"
        large = self._post(prefix + "/parents", form)
        self.assertEqual(large.status_code, 200, large.text[:500])
        self.assertIn("Showing 50 Odoo parents", large.text)
        self.assertIn("Previously selected parent", large.text)
        self.assertIn("Physical / Place 49", large.text)
        self.assertNotIn("Physical / Place 50", large.text)
        self.assertEqual(self.calls[0].limit, 51)
        self.assertEqual(len(self.calls), 3)
        count = 0
        form["existing_parent_search"] = "Typo in the filter"
        empty = self._post(prefix + "/parents", form)
        self.assertEqual(empty.status_code, 200)
        self.assertIn("No Odoo parents match", empty.text)
        self.assertIn("Previously selected parent", empty.text)
        self.assertIsNone(self.context.queries.get_derived_entity_plan(workspace_id))
        # Filter text is never treated as the selected parent or persisted.
        reader.side_effect = original
        preview = self._post(prefix + "/preview", form)
        self.assertEqual(preview.status_code, 200, preview.text[:500])
        self.assertIn("Physical / Stock", preview.text)
        self.assertIn(("name", "=", "Stock"), self.calls[-3].domain)
        self.assertNotIn(("name", "ilike", "Typo in the filter"), self.calls[-3].domain)

    def test_company_selection_existing_second_level_and_preparation_matrix(self):
        for major in (19, 20):
            for mode in ("LOCAL", "REMOTE"):
                with self.subTest(major=major, mode=mode):
                    workspace_id, form, schema = self.configure(major, mode)
                    prefix = f"/workspaces/{workspace_id}/derived-entities/hierarchy"
                    form["existing_parent_search"] = ""
                    form["hierarchy_lookup_action"] = "companies"
                    with (
                        collect_duckdb_request_timings() as company_timings,
                        count_duckdb_statements() as company_statements,
                    ):
                        companies = self._post(prefix + "/parents", form)
                    self.assertEqual(companies.status_code, 200, companies.text[:300])
                    self.assertIn("Example", companies.text)
                    self.assertIn("Load existing Odoo parents", companies.text)
                    self.assertIn("Search parents (optional)", companies.text)
                    self.assertEqual(self.calls, [])
                    self.assertIsNone(
                        self.context.queries.get(workspace_id).odoo_company
                    )
                    self.assertIsNone(
                        self.context.queries.get_derived_entity_plan(workspace_id)
                    )
                    # Same admission and local capture/publication/render owners
                    # as parent browsing, with no record or metadata reads.
                    self.assertLessEqual(company_timings.connection_count, 2 + 4 * 3)
                    self.assertLessEqual(company_timings.schema_check_count, 10)
                    self.assertLessEqual(len(company_statements), 220)
                    form["hierarchy_lookup_action"] = "parents"
                    missing_company = self._post(prefix + "/parents", form)
                    self.assertEqual(
                        missing_company.status_code, 422, missing_company.text[:300]
                    )
                    self.assertIn("Example", missing_company.text)
                    self.assertEqual(self.calls, [])
                    form["hierarchy_company_id"] = "7"
                    with (
                        collect_duckdb_request_timings() as timings,
                        count_duckdb_statements() as statements,
                    ):
                        found = self._post(prefix + "/parents", form)
                    self.assertEqual(found.status_code, 200, found.text[:500])
                    self.assertIn("Physical / Stock", found.text)
                    self.assertIn("1 Odoo parent loaded", found.text)
                    self.assertEqual(len(self.calls), 3)
                    self.assertEqual(self.calls[0].domain, (("active", "=", True),))
                    self.assertEqual(
                        self.context.queries.get(workspace_id).odoo_company.name,
                        "Example",
                    )
                    # Admission opens registry/workspace. Capture, publication,
                    # and render each own the same four distinct databases.
                    self.assertLessEqual(timings.connection_count, 2 + 4 * 3)
                    self.assertLessEqual(timings.schema_check_count, 10)
                    # The measured complete request uses 199 statements. This
                    # catches a repeated source/catalogue read in any phase.
                    self.assertLessEqual(len(statements), 220)
                    form["existing_parent_choice"] = HierarchyParentChoice(
                        ("Physical", "Stock"), (None, "Example")
                    ).value
                    # Refreshing companies keeps the chosen parent and file
                    # fields, but never converts them into a saved hierarchy.
                    form["hierarchy_lookup_action"] = "companies"
                    refreshed = self._post(prefix + "/parents", form)
                    self.assertEqual(refreshed.status_code, 200)
                    self.assertIn("Previously selected parent", refreshed.text)
                    self.assertIn('value="location_hierarchy"', refreshed.text)
                    self.assertIsNone(
                        self.context.queries.get_derived_entity_plan(workspace_id)
                    )
                    del form["hierarchy_lookup_action"]
                    preview = self._post(prefix + "/preview", form)
                    self.assertEqual(preview.status_code, 200, preview.text[:300])
                    self.assertIn("Use existing — no changes", preview.text)
                    saved = self._post(prefix + "/save", form)
                    self.assertEqual(saved.status_code, 303, saved.text[:500])
                    self.assertEqual(
                        self.context.queries.get_derived_entity_plan(workspace_id)
                        .rules[0]
                        .existing_parent.fixed_path,
                        ("Physical", "Stock"),
                    )
                    self.prepare_hierarchy(workspace_id)

    def prepare_hierarchy(self, workspace_id):
        plan = self.context.queries.get_derived_entity_plan(workspace_id)
        link = derived_dataset_links(plan)[0]
        # Only the hierarchy is imported; the original file supplies its rows.
        run_local_operation_sync(
            self.context.derived_entities.save_dataset_uses,
            workspace_id,
            dataset_uses={
                plan.rules[0].source_dataset_id: SourceDatasetUse.PREPARATION_ONLY
            },
            expected_parent_version=plan.version,
            actor=self.actor,
        )
        parent = RelationshipResolver(
            origin=ResolverOrigin.DATASET, dataset_id=link.derived_dataset_id
        )
        company = RelationshipResolver(
            origin=ResolverOrigin.TARGET_CATALOG,
            model="res.company",
            key_mappings=(ReferenceKeyMapping(link.company_column_key, "name"),),
        )
        mapping = DatasetMapping(
            link.derived_dataset_id,
            MODEL,
            source_identity_column_keys=(link.canonical_key_column_key,),
            target_identity=(
                IdentityComponentMapping((link.name_column_key,), ("name",)),
            ),
            target_scope=(
                IdentityComponentMapping(
                    (link.parent_key_column_key,),
                    ("container_id",),
                    resolver=parent,
                    null_policy=IdentityNullPolicy.EXPLICIT_SCOPE_NULL,
                ),
                IdentityComponentMapping(
                    (link.company_column_key,),
                    ("company_id",),
                    resolver=company,
                    null_policy=IdentityNullPolicy.EXPLICIT_SCOPE_NULL,
                ),
            ),
        )
        revision = self.submit(workspace_id, (mapping,))
        staging, quality = self.prepare(workspace_id)
        self.assertEqual((quality.blocked_count, quality.quarantined_count), (0, 0))
        self.assertEqual(len(staging.rows), 6)
        restored = self.context.queries.get_derived_entity_plan(workspace_id)
        self.assertEqual(
            restored.rules[0].existing_parent.fixed_companies, (None, "Example")
        )
        selection = self.context.queries.get_mapping_source_selection(workspace_id)
        compiled = compile_browser_mapping(
            revision.definition, selection, derived_plan_hash=restored.content_hash
        )
        self.assertEqual(compiled.semantic_hash, staging.compiled_plan_hash)
        schema = self.context.queries.get_odoo_schema_catalog(workspace_id)
        target = HierarchyOdoo(schema)
        target.records = {
            (MODEL, 10): {
                "name": "Physical",
                "container_id": False,
                "company_id": False,
                "active": True,
            },
            (MODEL, 11): {
                "name": "Stock",
                "container_id": 10,
                "company_id": 7,
                "active": True,
            },
            ("res.company", 7): {"name": "Example"},
        }
        records = tuple(_prepared_record(row) for row in staging.rows)
        requirements = plan_preflight_requirements(
            compiled,
            records,
            reference_policy_hash=reference_policy_hash(
                int(schema.odoo_version.split(".")[0])
            ),
        )
        metadata, captured = target.compare(requirements)
        comparison = PreflightEngine().run(
            compiled, PreparedBundle(records, (), {}), metadata, captured
        )
        self.assertEqual(
            {row.classification.value for row in comparison.decisions},
            {"CREATE", "UNCHANGED"},
        )
        self.assertEqual(
            sum(
                row.classification.value == "UNCHANGED" for row in comparison.decisions
            ),
            2,
        )
        self.assertTrue(all(not row.differences for row in comparison.decisions))
        owner = self.context.migration_workspaces.get(workspace_id, actor=self.actor)
        draft = run_local_operation_sync(
            self.context.recipe_publication.draft,
            project_id=owner.project_id,
            data_version_id=owner.data_version_id,
            workspace_id=workspace_id,
            actor=self.actor,
        )
        self.assertTrue(draft.can_publish, draft.issues)
        self.assertEqual(
            draft.compiled.recipe["contract_versions"]["source_preparation_recipe"], 2
        )
