"""Save and reopen company-scoped choices, including the earlier malformed shape."""

import json
from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import patch

import polars as pl

from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from impodo.domain.odoo.contracts import RecordSnapshot
from impodo.domain.odoo_source_policy import odoo_source_policy_hash
from impodo.domain.shared.models import FieldMetadata, ModelMetadata, TargetRecord
from impodo.domain.workspace.reference_keys import (
    StandardReferenceFieldContract,
    reference_policy_hash,
)
from impodo.domain.workspace.supporting_lookups import (
    SupportingLookupChoice,
    portable_supporting_value,
)
from impodo.web.presenters.mapping_forms import _related_business_keys
from tests.integration.web import test_mapping_form_roundtrip as form_fixtures
from tests.support.browser_scenarios import (
    POST_HEADERS,
    ProjectSetupBrowserTestCase,
    _browser_schema,
)
from tests.support.identity import resolve_local_actor


class ScopedRelationshipMatchBrowserTests(ProjectSetupBrowserTestCase):
    def _case(self, model, field, *, legacy=False, odoo_version=None):
        workspace_id, dataset, business_key = self._mapping_ready_workspace(
            scalar_field_count=0,
            relationship_field_count=1,
            relationship_model=model,
            relationship_field_names=(field,),
        )
        context = self.app.state.context
        schema = context.queries.get_odoo_schema_catalog(workspace_id)
        if odoo_version is not None and odoo_version != schema.odoo_version:
            governance = context.queries.get_schema_governance(workspace_id)
            schema = replace(
                schema, odoo_version=odoo_version,
                policy_hash=odoo_source_policy_hash(int(odoo_version.split(".", 1)[0])),
            )
            context.schema_workspace.schemas.save_odoo_schema_catalog(
                workspace_id,
                schema,
                actor=resolve_local_actor(context),
            )
            context.schema_workspace.govern(
                workspace_id, business_keys=governance.business_keys,
                actor=resolve_local_actor(context),
            )
        choice = portable_supporting_value(("DOM_S", "United Caps Wiltz"))
        context.supporting_lookups.capture(
            workspace_id,
            relation_model=model,
            key_fields=("name",),
            scope_fields=("company_id",),
            display_field="name",
            field_contracts=(
                StandardReferenceFieldContract("name", "char", True, False),
                StandardReferenceFieldContract(
                    "company_id", "many2one", False, False, "res.company"
                ),
            ),
            target_hash=schema.connection_target_hash,
            read_credential_binding_hash=schema.read_credential_binding_hash,
            read_principal_hash=schema.read_principal_hash,
            read_permission_hash=schema.read_permission_hash,
            read_context_hash=schema.read_context_hash,
            captured_at=datetime.now(UTC),
            choices=(SupportingLookupChoice(choice, "DOM_S (United Caps Wiltz)"),),
            ambiguous_values=(),
            actor=resolve_local_actor(context),
            reference_policy_hash=reference_policy_hash(int(schema.odoo_version.split(".", 1)[0])),
        )
        rule = next(
            key
            for key in _related_business_keys((), model)
            if key.scope_fields == (() if legacy else ("company_id",))
        )
        entries = [
            ["csrf_token", self.csrf],
            ["action", "save_progress"],
            ["expected_parent_version", ""],
            ["expected_working_draft_version", ""],
            ["editable_dataset_id", dataset.dataset_id],
            ["target_model_0", "res.partner"],
            ["mode_0", "upsert"],
            ["on_existing_0", "block"],
            ["source_identity_0", dataset.columns[0].stable_key],
            ["business_key_0", business_key.key_id],
            ["identity_source_0_0", dataset.columns[0].stable_key],
            ["visible_relation_target_0", field],
            ["relation_value_source_0_0", "source"],
            ["relation_source_0_0", dataset.columns[1].stable_key],
            ["relation_origin_0_0", "target_catalog"],
            ["relation_key_0_0", rule.key_id],
            [
                "relation_value_matches_0_0",
                json.dumps([{"source_value": "source", "target_value": choice}]),
            ],
            ["relation_operation_0_0", "replace"],
            ["relation_missing_0_0", "error"],
            ["relation_ambiguous_0_0", "error"],
            ["relation_null_0_0", "distinct"],
            ["relation_categorical_policy_0_0", "EXPLICIT_KEY_MATCH"],
        ]
        return workspace_id, entries, choice

    def _save(self, workspace_id, entries):
        return self.client.post(
            f"/workspaces/{workspace_id}/mapping/save",
            json={"entries": entries},
            headers={**POST_HEADERS, "X-CSRF-Token": self.csrf},
        )

    def test_selected_fiscal_position_and_payment_term_keep_company_on_save_and_reopen(
        self,
    ):
        self._assert_scoped_save_and_reopen("19.0")

    def test_odoo20_selected_fiscal_position_and_payment_term_keep_company(self):
        self._assert_scoped_save_and_reopen("20.0")

    def _assert_scoped_save_and_reopen(self, odoo_version):
        for model, field in (
            ("account.fiscal.position", "property_account_position_id"),
            ("account.payment.term", "property_payment_term_id"),
        ):
            with self.subTest(field=field):
                workspace_id, entries, choice = self._case(model, field, odoo_version=odoo_version)
                with collect_duckdb_request_timings() as timings:
                    response = self._save(workspace_id, entries)
                self.assertEqual(response.status_code, 200, response.text)
                draft = self.app.state.context.queries.get_mapping_working_draft(
                    workspace_id
                )
                relation = draft.definition.datasets[0].relationships[0]
                self.assertEqual(
                    relation.resolver.scope_mappings[0].target_field, "company_id"
                )
                self.assertEqual(
                    relation.resolver.scope_mappings[0].source_column_key,
                    relation.source_column_keys[0],
                )
                self.assertEqual(
                    relation.resolver.value_mappings[0].target_value, choice
                )
                context = self.app.state.context
                schema = context.queries.get_odoo_schema_catalog(workspace_id)
                lookup = context.supporting_lookups.current(
                    workspace_id,
                    relation_model=model,
                    key_fields=("name",),
                    scope_fields=("company_id",),
                    display_field="name",
                    target_hash=schema.connection_target_hash,
                    read_credential_binding_hash=schema.read_credential_binding_hash,
                    read_principal_hash=schema.read_principal_hash,
                    read_context_hash=schema.read_context_hash,
                    actor=resolve_local_actor(context),
                    reference_policy_hash=reference_policy_hash(int(schema.odoo_version.split(".", 1)[0])),
                )
                validation = context.mapping_workspace.validator.validate(
                    draft.definition,
                    context.queries.get_mapping_source_selection(workspace_id),
                    schema,
                    context.queries.get_schema_governance(workspace_id),
                    (lookup,),
                )
                self.assertFalse(
                    [issue for issue in validation.issues if issue.severity == "error"],
                    validation.issues,
                )
                page = self.client.get(f"/workspaces/{workspace_id}/mapping")
                self.assertEqual(page.status_code, 200, page.text)
                controls = form_fixtures._RenderedControls()
                controls.feed(page.text)
                saved_choices = next(
                    value
                    for name, value in controls.entries
                    if name == "relation_value_matches_0_0"
                )
                self.assertEqual(json.loads(saved_choices)[0]["target_value"], choice)
                # Reserve/load/commit/response scopes each own bounded database work.
                self.assertLessEqual(timings.connection_count, 16)
                self.assertLessEqual(timings.schema_check_count, 4)

    def test_legacy_saved_matches_recover_company_from_current_checked_choices(self):
        workspace_id, entries, _ = self._case(
            "account.fiscal.position", "property_account_position_id", legacy=True
        )
        response = self._save(workspace_id, entries)
        self.assertEqual(response.status_code, 200, response.text)
        draft = self.app.state.context.queries.get_mapping_working_draft(workspace_id)
        self.assertEqual(
            draft.definition.datasets[0]
            .relationships[0]
            .resolver.scope_mappings[0]
            .target_field,
            "company_id",
        )

    def test_odoo20_legacy_scope_uses_the_same_policy_as_captured_choices(self):
        workspace_id, entries, choice = self._case(
            "account.fiscal.position",
            "property_account_position_id",
            legacy=True,
            odoo_version="20.0+e",
        )
        response = self._save(workspace_id, entries)
        self.assertEqual(response.status_code, 200, response.text)
        draft = self.app.state.context.queries.get_mapping_working_draft(workspace_id)
        relation = draft.definition.datasets[0].relationships[0]
        self.assertEqual(relation.resolver.scope_mappings[0].target_field, "company_id")
        self.assertEqual(relation.resolver.value_mappings[0].target_value, choice)

    def test_unavailable_cached_choice_can_be_saved_without_changing_its_company(self):
        workspace_id, entries, _choice = self._case(
            "account.payment.term", "property_payment_term_id"
        )
        for entry in entries:
            if entry[0] == "relation_value_matches_0_0":
                entry[1] = entry[1].replace("United Caps Wiltz", "Unknown Company")
        response = self._save(workspace_id, entries)
        self.assertEqual(response.status_code, 200, response.text)
        draft = self.app.state.context.queries.get_mapping_working_draft(workspace_id)
        self.assertIn(
            "Unknown Company",
            draft.definition.datasets[0]
            .relationships[0]
            .resolver.value_mappings[0]
            .target_value,
        )
        self.assertIsNone(
            self.app.state.context.queries.get_mapping_submission(workspace_id)
        )

    def test_missing_lookup_does_not_block_saving_scoped_choices(self):
        workspace_id, entries, choice = self._case(
            "account.fiscal.position", "property_account_position_id"
        )
        context = self.app.state.context
        with patch.object(context.supporting_lookups, "current", return_value=None):
            response = self._save(workspace_id, entries)
        self.assertEqual(response.status_code, 200, response.text)
        draft = context.queries.get_mapping_working_draft(workspace_id)
        self.assertEqual(
            draft.definition.datasets[0]
            .relationships[0]
            .resolver.value_mappings[0]
            .target_value,
            choice,
        )
        self.assertIsNone(context.queries.get_mapping_submission(workspace_id))

    def test_check_matches_refreshes_missing_lookup_and_keeps_selected_company(self):
        model, field = "account.fiscal.position", "property_account_position_id"
        workspace_id, entries, choice = self._case(model, field)
        context = self.app.state.context
        for entry in entries:
            if entry[0] == "action":
                entry[1] = "draft"
            elif entry[0] == "relation_value_matches_0_0":
                entry[1] = entry[1].replace('"source"', '"Example"')
        calls = []

        def read_choices(workspace, metadata_requests, record_requests):
            calls.append(record_requests)
            metadata = replace(
                _browser_schema(workspace),
                models={
                    model: ModelMetadata(
                        model,
                        "Fiscal Position",
                        {
                            "name": FieldMetadata(
                                "name", "char", "Name", required=True
                            ),
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
            return metadata, RecordSnapshot(
                fingerprint=metadata.fingerprint,
                records={
                    model: (
                        TargetRecord(
                            model,
                            17,
                            {"name": "DOM_S", "company_id": [3, "United Caps Wiltz"]},
                        ),
                    )
                },
                requested_fields={model: ("name", "company_id")},
            )

        context.readiness_reader = read_choices
        with (
            patch.object(context.supporting_lookups, "current", return_value=None),
            patch.object(
                context.categorical_coverage,
                "_scan_dataset",
                return_value=pl.DataFrame(
                    {
                        "column:ref": ["P001"],
                        "column:value": ["Example"],
                    }
                ),
            ),
            collect_duckdb_request_timings() as timings,
        ):
            response = self._save(workspace_id, entries)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(calls), 1)
        revision = context.queries.get_mapping_revision(workspace_id)
        self.assertEqual(
            revision.definition.datasets[0]
            .relationships[0]
            .resolver.value_mappings[0]
            .target_value,
            choice,
        )
        validation = context.queries.get_mapping_validation(
            workspace_id, revision.version
        )
        self.assertFalse(
            [issue for issue in validation.issues if issue.severity == "error"],
            validation.issues,
        )
        # Begin/read, supporting capture/publication, checked revision and response
        # each own a bounded local operation; the Odoo reader runs between them.
        self.assertLessEqual(timings.connection_count, 30)
        self.assertLessEqual(timings.schema_check_count, 16)
        self.assertEqual(timings.lock_retry_count, 0)
