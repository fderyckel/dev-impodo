"""Qualify account company-list review and grouped reads on both Odoo majors."""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from impodo.application.recipe.odoo_requirements import (
    OdooCheckRelationshipRequirement,
    OdooCheckSupportingRequirement,
)
from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from impodo.adapters.duckdb.unit_of_work import _read_databases
from impodo.domain.mapping.contracts import (
    CategoricalCoveragePolicy,
    DatasetMapping,
    IdentityComponentMapping,
    MappingDefinition,
    ReferenceKeyMapping,
    RelationshipMapping,
    RelationshipResolver,
    ResolverOrigin,
)
from impodo.domain.mapping.validation.validator import MappingSemanticValidator
from impodo.domain.odoo.contracts import MetadataSnapshot, RecordSnapshot
from impodo.domain.shared.models import FieldMetadata, ModelMetadata, TargetRecord
from impodo.domain.workspace.contracts import SchemaField, SchemaModel
from impodo.domain.workspace.reference_keys import reference_policy_hash
from impodo.domain.workspace.workbench import OdooConnectionMode
from impodo.web.composition.page_reads import run_local_operation_sync
from impodo.web.composition.target_readers import _capture_recipe_supporting_values
from tests.support.browser_scenarios import (
    POST_HEADERS,
    ProjectSetupBrowserTestCase,
    _browser_model_catalog,
    _browser_schema,
)
from tests.support.identity import resolve_local_actor


class AccountCompanySupportingTests(ProjectSetupBrowserTestCase):
    def account_workspace(self, version, mode):
        workspace_id, dataset, key = self._mapping_ready_workspace(
            scalar_field_count=1,
            target_model="account.account",
            connection_mode=mode,
        )
        context = self.app.state.context
        schema = context.queries.get_odoo_schema_catalog(workspace_id)
        companies = SchemaField(
            "company_ids",
            "Companies",
            "many2many",
            True,
            False,
            "res.company",
            None,
            (),
        )
        schema = replace(
            schema,
            odoo_version=version,
            content_hash="sha256:" + "8" * 64,
            models=(
                replace(
                    schema.models[0],
                    label="Account",
                    fields=(
                        *schema.models[0].fields,
                        companies,
                        replace(
                            companies,
                            name="x_company_id",
                            label="Company",
                            type="many2one",
                            required=False,
                        ),
                    ),
                ),
            ),
        )
        context.schema_workspace.schemas.save_odoo_schema_catalog(
            workspace_id, schema, actor=resolve_local_actor(context)
        )
        catalogue = _browser_model_catalog(context.queries.get(workspace_id))
        context.schema_workspace.discover_models(
            workspace_id,
            replace(
                catalogue,
                fingerprint=replace(catalogue.fingerprint, odoo_version=version),
            ),
            read_credential_binding_hash=schema.read_credential_binding_hash,
            read_identity=SimpleNamespace(
                target_hash=schema.connection_target_hash,
                principal_hash=schema.read_principal_hash,
                permission_hash=schema.read_permission_hash,
                context_hash=schema.read_context_hash,
                readable_models=("ir.model",),
            ),
            actor=resolve_local_actor(context),
        )
        return workspace_id, dataset, key, schema

    def review_form(self, workspace_id):
        page = self.client.get(f"/workspaces/{workspace_id}/schema")
        self.assertEqual(page.status_code, 200, page.text)
        view = page.context["supporting_review"]
        form = {
            "csrf_token": self.csrf,
            "binding_hash": view["binding_hash"],
            "expected_version": str(view["version"]),
            "operation_id": view["operation_id"],
            "choice_count": str(len(view["rows"])),
        }
        for index, row in enumerate(view["rows"]):
            form[f"edge_{index}"] = f"{row['model']}|{row['field']}"
            form[f"action_{index}"] = "REUSE"
        return page, form

    def post(self, workspace_id, action, form):
        return self.client.post(
            f"/workspaces/{workspace_id}/schema/supporting/{action}",
            data=form,
            headers=POST_HEADERS,
            follow_redirects=False,
        )

    def lookup(self, workspace_id, schema, major):
        context = self.app.state.context
        snapshot = context.supporting_lookups.current(
            workspace_id,
            relation_model="res.company",
            key_fields=("name",),
            scope_fields=(),
            display_field="name",
            target_hash=schema.connection_target_hash,
            read_credential_binding_hash=schema.read_credential_binding_hash,
            read_principal_hash=schema.read_principal_hash,
            read_context_hash=schema.read_context_hash,
            actor=resolve_local_actor(context),
            reference_policy_hash=reference_policy_hash(major),
        )
        self.assertIsNotNone(snapshot)
        return snapshot

    def snapshots(self, workspace, schema, *, duplicate=False, wrong_type=False):
        fingerprint = replace(
            _browser_schema(workspace).fingerprint, odoo_version=schema.odoo_version
        )
        field = FieldMetadata(
            "name",
            "integer" if wrong_type else "char",
            label="Company Name",
            required=True,
        )
        records = [TargetRecord("res.company", 105, {"name": "Example"})]
        if duplicate:
            records.append(TargetRecord("res.company", 707, {"name": "Example"}))
        return (
            MetadataSnapshot(
                fingerprint,
                {
                    "res.company": ModelMetadata(
                        "res.company", "Company", {"name": field}
                    )
                },
            ),
            RecordSnapshot(
                fingerprint, {"res.company": tuple(records)}, {"res.company": ("name",)}
            ),
        )

    def test_save_check_and_confirm_keep_companies_outside_write_scope(self):
        context = self.app.state.context
        actor = resolve_local_actor(context)
        for major in (19, 20):
            for mode in (OdooConnectionMode.LOCAL, OdooConnectionMode.REMOTE):
                with self.subTest(major=major, mode=mode):
                    workspace_id, dataset, key, schema = self.account_workspace(
                        f"{major}.0+e", mode
                    )
                    workspace = context.queries.get(workspace_id)
                    with collect_duckdb_request_timings() as page_timings:
                        page, form = self.review_form(workspace_id)
                    self.assertIn("Why matching rules are waiting", page.text)
                    # Request access opens two owners; the bounded local page
                    # or save operation opens four lineage/evidence owners.
                    self.assertLessEqual(page_timings.connection_count, 6)
                    self.assertLessEqual(page_timings.schema_check_count, 4)
                    with patch.object(
                        context,
                        "readiness_reader",
                        side_effect=AssertionError("Save must stay local"),
                    ):
                        with collect_duckdb_request_timings() as save_timings:
                            saved = self.post(workspace_id, "save", form)
                    self.assertEqual(saved.status_code, 303, saved.text)
                    self.assertLessEqual(save_timings.connection_count, 6)
                    self.assertLessEqual(save_timings.schema_check_count, 4)
                    reopened, _form = self.review_form(workspace_id)
                    view = reopened.context["supporting_review"]
                    self.assertTrue(view["current"])
                    payload = {
                        "csrf_token": self.csrf,
                        "binding_hash": view["binding_hash"],
                        "expected_version": str(view["version"]),
                    }
                    metadata, records = self.snapshots(workspace, schema)

                    def assert_released(*_args, **_kwargs):
                        self.assertFalse(
                            getattr(_read_databases, "connections", None),
                            "DuckDB owner crossed the Odoo boundary",
                        )

                    primary = SimpleNamespace(
                        target_hash=schema.connection_target_hash,
                        principal_hash=schema.read_principal_hash,
                        permission_hash=schema.read_permission_hash,
                        context_hash=schema.read_context_hash,
                        readable_models=("account.account",),
                    )
                    supporting = SimpleNamespace(
                        **{
                            **vars(primary),
                            "permission_hash": "sha256:" + "a" * 64,
                            "readable_models": ("res.company",),
                        }
                    )
                    with (
                        patch.object(context, "readiness_reader", None),
                        patch(
                            "impodo.web.composition.target_readers._selected_local_profile",
                            return_value=object(),
                        ),
                        patch.object(
                            context.local_odoo_reader,
                            "get_preflight_snapshots",
                            side_effect=lambda *_args, **_kw: (
                                assert_released(),
                                (metadata, records),
                            )[1],
                        ) as local,
                        patch.object(
                            context,
                            "read_identity_probe",
                            side_effect=lambda _w, _s, models: (
                                assert_released(),
                                primary
                                if models == ("account.account",)
                                else supporting,
                            )[1],
                        ),
                        patch(
                            "impodo.web.composition.target_readers._target_json2_config"
                        ),
                        patch(
                            "impodo.web.composition.target_readers.Json2ReadConnector"
                        ) as connector,
                        collect_duckdb_request_timings() as check_timings,
                    ):
                        connector.return_value.get_model_metadata.side_effect = (
                            lambda *_: (assert_released(), metadata)[1]
                        )
                        connector.return_value.get_records.side_effect = lambda *_: (
                            assert_released(),
                            records,
                        )[1]
                        checked = self.post(workspace_id, "check", payload)
                    self.assertEqual(checked.status_code, 303, checked.text)
                    # Access, preparation and publication open 2 + 4 + 4
                    # owners. Remote reads also load the checking credential.
                    connection_limit = 10 if mode is OdooConnectionMode.LOCAL else 11
                    self.assertLessEqual(
                        check_timings.connection_count, connection_limit
                    )
                    self.assertLessEqual(check_timings.schema_check_count, 6)
                    self.assertEqual(check_timings.lock_retry_count, 0)
                    if mode is OdooConnectionMode.LOCAL:
                        local.assert_called_once()
                        requests = local.call_args.args[2]
                        connector.assert_not_called()
                    else:
                        local.assert_not_called()
                        connector.return_value.get_model_metadata.assert_called_once()
                        connector.return_value.get_records.assert_called_once()
                        requests = (
                            connector.return_value.get_model_metadata.call_args.args[0]
                        )
                    self.assertEqual(
                        [(r.model, r.fields) for r in requests],
                        [("res.company", ("name",))],
                    )
                    review = context.schema_workspace.supporting.current(
                        workspace_id, actor=actor
                    )
                    requirements = context.schema_workspace.supporting.requirements(
                        context.schema_workspace.supporting_review_inputs(
                            workspace_id, actor=actor
                        ),
                        review,
                        actor=actor,
                    )
                    self.assertEqual(len(requirements), 1)
                    self.assertEqual(len(requirements[0].relationships), 2)
                    snapshot = self.lookup(workspace_id, schema, major)
                    self.assertEqual(
                        snapshot.reference_policy_hash, reference_policy_hash(major)
                    )
                    self.assertEqual(
                        [(c.value, c.label) for c in snapshot.choices],
                        [("Example", "Example")],
                    )
                    self.assertNotIn('"105"', snapshot.to_json())
                    run_local_operation_sync(
                        context.schema_workspace.govern_complete,
                        workspace_id,
                        business_keys=(key,),
                        expected_catalog_hash=schema.content_hash,
                        actor=actor,
                    )
                    mapping_page = self.client.get(
                        f"/workspaces/{workspace_id}/mapping"
                    )
                    self.assertEqual(mapping_page.status_code, 200, mapping_page.text)
                    self.assertIn("Company name", mapping_page.text)
                    mapping = DatasetMapping(
                        dataset.dataset_id,
                        "account.account",
                        source_identity_column_keys=(dataset.columns[0].stable_key,),
                        target_identity=(
                            IdentityComponentMapping(
                                (dataset.columns[0].stable_key,), ("ref",)
                            ),
                        ),
                        relationships=tuple(
                            RelationshipMapping(
                                field_name,
                                field_type,
                                (dataset.columns[1].stable_key,),
                                RelationshipResolver(
                                    ResolverOrigin.TARGET_CATALOG,
                                    model="res.company",
                                    key_mappings=(
                                        ReferenceKeyMapping(
                                            dataset.columns[1].stable_key, "name"
                                        ),
                                    ),
                                ),
                                required=field_name == "company_ids",
                                categorical_policy=CategoricalCoveragePolicy.EXACT_BUSINESS_KEY,
                            )
                            for field_name, field_type in (
                                ("company_ids", "many2many"),
                                ("x_company_id", "many2one"),
                            )
                        ),
                    )
                    run_local_operation_sync(
                        context.mapping_workspace.supporting_decisions.require_mapping,
                        workspace_id,
                        (mapping,),
                        schema,
                        None,
                    )
                    governance = context.queries.get_schema_governance(workspace_id)
                    selection = context.queries.get_source_selection(workspace_id)
                    validation = MappingSemanticValidator().validate(
                        MappingDefinition(
                            str(uuid4()),
                            selection.content_hash,
                            governance.content_hash,
                            (mapping,),
                        ),
                        selection,
                        schema,
                        governance,
                        supporting_references=(snapshot,),
                    )
                    self.assertFalse(
                        [i for i in validation.issues if i.severity == "error"],
                        validation.issues,
                    )
                    self.assertEqual(
                        context.queries.get(workspace_id).intended_models,
                        ("account.account",),
                    )

    def test_recipe_refresh_reads_only_verified_company_name_fields(self):
        context = self.app.state.context
        actor = resolve_local_actor(context)
        for major in (19, 20):
            with self.subTest(major=major):
                workspace_id, _dataset, _key, schema = self.account_workspace(
                    f"{major}.0", OdooConnectionMode.REMOTE
                )
                company = SchemaModel(
                    "res.company",
                    "Company",
                    (SchemaField("name", "Name", "char", True, False, None, None, ()),),
                )
                fresh = replace(schema, models=(*schema.models, company))
                workspace = context.queries.get(workspace_id)
                requirement = OdooCheckSupportingRequirement(
                    "res.company",
                    ("name",),
                    (),
                    (
                        OdooCheckRelationshipRequirement(
                            "account.account", "company_ids", "many2many"
                        ),
                    ),
                    ("Accounts",),
                )
                with patch.object(
                    context,
                    "readiness_reader",
                    return_value=self.snapshots(workspace, fresh),
                ) as reader:
                    captured = _capture_recipe_supporting_values(
                        context, workspace, fresh, (requirement,), actor=actor
                    )
                reader.assert_called_once()
                self.assertEqual(
                    [(r.model, r.fields) for r in reader.call_args.args[1]],
                    [("res.company", ("name",))],
                )
                self.assertEqual(len(captured), 1)
                self.assertEqual(
                    captured[0].reference_policy_hash, reference_policy_hash(major)
                )
                self.assertEqual(
                    [choice.value for choice in captured[0].choices], ["Example"]
                )

    def test_duplicate_names_stay_ambiguous_and_incompatible_metadata_is_not_published(
        self,
    ):
        context = self.app.state.context
        for major in (19, 20):
            with self.subTest(major=major):
                workspace_id, _dataset, _key, schema = self.account_workspace(
                    f"{major}.0", OdooConnectionMode.REMOTE
                )
                _page, form = self.review_form(workspace_id)
                self.assertEqual(self.post(workspace_id, "save", form).status_code, 303)
                page, _ = self.review_form(workspace_id)
                view = page.context["supporting_review"]
                payload = {
                    "csrf_token": self.csrf,
                    "binding_hash": view["binding_hash"],
                    "expected_version": str(view["version"]),
                }
                workspace = context.queries.get(workspace_id)
                with patch.object(
                    context,
                    "readiness_reader",
                    return_value=self.snapshots(workspace, schema, duplicate=True),
                ):
                    checked = self.post(workspace_id, "check", payload)
                self.assertEqual(checked.status_code, 303, checked.text)
                snapshot = self.lookup(workspace_id, schema, major)
                self.assertEqual(snapshot.choices, ())
                self.assertEqual(snapshot.ambiguous_values, ("Example",))
                with (
                    patch.object(
                        context,
                        "readiness_reader",
                        return_value=self.snapshots(workspace, schema, wrong_type=True),
                    ),
                    patch.object(context.supporting_lookups, "capture") as capture,
                ):
                    rejected = self.post(workspace_id, "check", payload)
                self.assertEqual(rejected.status_code, 422)
                self.assertIn("incompatible supporting fields", rejected.text)
                capture.assert_not_called()
