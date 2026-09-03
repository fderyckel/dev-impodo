"""Focused browser evidence for one Impodo capability."""

from __future__ import annotations

from io import BytesIO
from openpyxl import load_workbook

from impodo.domain.mapping.contracts import UnsupportedMappingContractError

from tests.support.browser_scenarios import (
    POST_HEADERS,
    CategoricalCoveragePolicy,
    DatasetMapping,
    FieldMetadata,
    IdentityComponentMapping,
    MappingTargetMode,
    MappingValidationStatus,
    MetadataSnapshot,
    ModelMetadata,
    OdooConnectionMode,
    OdooReadCredentialMissingError,
    OdooReadFailureCode,
    OdooReadIdentity,
    ProjectSetupBrowserTestCase,
    RecordSnapshot,
    ReferenceKeyMapping,
    RelationshipMapping,
    RelationshipResolver,
    ResolverOrigin,
    ScalarFieldMapping,
    TargetFieldHandling,
    TargetRecord,
    ValueMapping,
    _browser_schema,
    patch,
    re,
    replace,
    target_identity_hash,
)


class MappingWorkflowBrowserTests(ProjectSetupBrowserTestCase):
    def test_supported_legacy_mapping_opens_with_successor_notice(self) -> None:
        workspace_id, dataset, business_key = self._mapping_ready_workspace(
            scalar_field_count=1,
        )
        context = self.app.state.context
        source_identity = dataset.columns[0]
        revision, _validation = context.mapping_workspace.check_definition(
            workspace_id,
            datasets=(
                DatasetMapping(
                    dataset_id=dataset.dataset_id,
                    target_model="res.partner",
                    source_identity_column_keys=(source_identity.stable_key,),
                    target_identity=(
                        IdentityComponentMapping(
                            source_column_keys=(source_identity.stable_key,),
                            target_fields=business_key.key_fields,
                        ),
                    ),
                ),
            ),
            expected_parent_version=None,
            expected_working_draft_version=None,
            actor=context.actor,
        )
        legacy_revision = replace(
            revision,
            definition=replace(revision.definition, contract_version=12),
        )

        with patch.object(
            context.queries,
            "get_mapping_revision",
            return_value=legacy_revision,
        ):
            page = self.client.get(f"/workspaces/{workspace_id}/mapping")

        self.assertEqual(page.status_code, 200, page.text)
        self.assertIn("created with mapping contract v12", page.text)
        self.assertIn("create a v14 successor revision", page.text)

    def test_unsupported_mapping_has_controlled_stage_and_project_pages(self) -> None:
        workspace_id, _dataset, _business_key = self._mapping_ready_workspace(
            scalar_field_count=1,
        )
        context = self.app.state.context
        error = UnsupportedMappingContractError(11)

        with patch.object(
            context.queries,
            "get_mapping_revision",
            side_effect=error,
        ):
            mapping_page = self.client.get(f"/workspaces/{workspace_id}/mapping")

        self.assertEqual(mapping_page.status_code, 409, mapping_page.text)
        self.assertIn("saved mapping cannot be opened safely", mapping_page.text)
        self.assertIn("Mapping contract v11", mapping_page.text)
        self.assertIn("rest of this project remains available", mapping_page.text)

        workspace = context.migration_workspaces.get(
            workspace_id,
            actor=context.actor,
        )
        with patch.object(
            context.recipe_publication.compiler.mappings,
            "get_mapping_revision",
            side_effect=error,
        ):
            project_page = self.client.get(f"/projects/{workspace.project_id}")

        self.assertEqual(project_page.status_code, 200, project_page.text)
        self.assertIn("mapping contract v11", project_page.text)
        self.assertIn("rest of the project remains available", project_page.text)

    def test_failed_check_can_create_and_download_matching_review_workbook(
        self,
    ) -> None:
        workspace_id, dataset, business_key = self._mapping_ready_workspace(
            scalar_field_count=1,
            required_scalar_indexes=(0,),
            target_model="sale.order",
        )
        source_identity, _source_value = dataset.columns
        context = self.app.state.context
        _revision, validation = context.mapping_workspace.check_definition(
            workspace_id,
            datasets=(
                DatasetMapping(
                    dataset_id=dataset.dataset_id,
                    target_model="sale.order",
                    mode=MappingTargetMode.UPSERT,
                    source_identity_column_keys=(source_identity.stable_key,),
                    target_identity=(
                        IdentityComponentMapping(
                            source_column_keys=(source_identity.stable_key,),
                            target_fields=business_key.key_fields,
                        ),
                    ),
                ),
            ),
            expected_parent_version=None,
            expected_working_draft_version=None,
            actor=context.actor,
        )
        self.assertEqual(validation.status, MappingValidationStatus.INVALID)

        page = self.client.get(f"/workspaces/{workspace_id}/mapping")
        self.assertEqual(page.status_code, 200, page.text)
        self.assertIn("Create matching review workbook", page.text)
        self.assertIn("Includes items to fix", page.text)

        created = self.client.post(
            f"/workspaces/{workspace_id}/mapping/review-workbook",
            data={"csrf_token": self.csrf},
            headers=POST_HEADERS,
            follow_redirects=False,
        )
        self.assertEqual(created.status_code, 303, created.text)
        created_page = self.client.get(created.headers["location"])
        self.assertIn("Download matching review workbook", created_page.text)
        self.assertIn("Recreate matching review workbook", created_page.text)

        downloaded = self.client.get(
            f"/workspaces/{workspace_id}/mapping/review-workbook"
        )
        self.assertEqual(downloaded.status_code, 200, downloaded.text)
        self.assertEqual(
            downloaded.headers["content-type"],
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        workbook = load_workbook(BytesIO(downloaded.content), data_only=True)
        self.assertEqual(
            workbook["Matching overview"]["B4"].value,
            "Cannot confirm matches",
        )
        self.assertEqual(
            workbook["Needs attention"]["A4"].value,
            "Must fix",
        )
        workbook.close()

    def test_active_table_fields_have_a_two_state_disclosure(self) -> None:
        workspace_id, _dataset, _business_key = self._mapping_ready_workspace(
            scalar_field_count=1
        )

        page = self.client.get(f"/workspaces/{workspace_id}/mapping")

        self.assertEqual(page.status_code, 200, page.text)
        self.assertIn("data-table-fields-toggle", page.text)
        self.assertIn('aria-expanded="true"', page.text)
        self.assertIn('aria-controls="mapping-table-fields-0"', page.text)
        self.assertIn("Close this table's fields", page.text)
        self.assertIn('id="mapping-table-fields-0" data-table-fields-panel', page.text)

    def test_invalid_formula_saves_as_attention_but_full_check_rejects_it(
        self,
    ) -> None:
        workspace_id, dataset, business_key = self._mapping_ready_workspace(
            scalar_field_count=1
        )
        context = self.app.state.context
        source_identity, source_value = dataset.columns
        invalid_formula = 'value 1= "UNI"'
        entries = [
            ["csrf_token", self.csrf],
            ["action", "save_progress"],
            ["expected_parent_version", ""],
            ["expected_working_draft_version", ""],
            ["editable_dataset_id", dataset.dataset_id],
            ["target_model_0", "res.partner"],
            ["mode_0", "upsert"],
            ["on_existing_0", "block"],
            ["source_identity_0", source_identity.stable_key],
            ["business_key_0", business_key.key_id],
            ["identity_source_0_0", source_identity.stable_key],
            ["visible_scalar_target_0", "field_0000"],
            ["scalar_value_source_0_1", "source"],
            ["scalar_source_0_1", source_value.stable_key],
            ["scalar_type_0_1", "string"],
            ["scalar_case_0_1", "preserve"],
            ["scalar_formula_0_1", invalid_formula],
            ["scalar_compare_0_1", "1"],
            ["scalar_null_0_1", "distinct"],
        ]

        saved = self.client.post(
            f"/workspaces/{workspace_id}/mapping/save",
            json={"entries": entries},
            headers={**POST_HEADERS, "X-CSRF-Token": self.csrf},
        )

        self.assertEqual(saved.status_code, 200, saved.text)
        payload = saved.json()
        self.assertIn("Saved — needs attention", payload["message"])
        self.assertEqual(len(payload["authoring_issues"]), 1)
        self.assertEqual(
            payload["authoring_issues"][0]["dataset_id"],
            dataset.dataset_id,
        )
        self.assertEqual(
            payload["authoring_issues"][0]["target_field"],
            "field_0000",
        )
        self.assertEqual(
            payload["authoring_issues"][0]["path"],
            "/datasets/0/fields/field_0000/transform/formula",
        )
        self.assertNotIn(invalid_formula, saved.text)
        working = context.mapping_workspace.mappings.get_mapping_working_draft(
            workspace_id
        )
        self.assertEqual(
            working.definition.datasets[0].fields[0].transform.formula,
            invalid_formula,
        )
        self.assertIsNone(
            context.mapping_workspace.mappings.get_mapping_revision(workspace_id)
        )

        page = self.client.get(f"/workspaces/{workspace_id}/mapping")
        self.assertEqual(page.status_code, 200, page.text)
        self.assertIn("Must fix:", page.text)
        self.assertIn("data-go-to-formula-issue", page.text)
        self.assertIn("data-check-mapping", page.text)
        self.assertIn("Correct formula issues before checking matches.", page.text)
        self.assertRegex(page.text, r"data-check-mapping[\s\S]{0,100}disabled")

        checked_entries = []
        for name, value in entries:
            if name == "action":
                value = "draft"
            elif name == "expected_working_draft_version":
                value = "1"
            checked_entries.append([name, value])
        checked = self.client.post(
            f"/workspaces/{workspace_id}/mapping/save",
            json={"entries": checked_entries},
            headers={**POST_HEADERS, "X-CSRF-Token": self.csrf},
        )

        self.assertEqual(checked.status_code, 200, checked.text)
        revision = context.mapping_workspace.mappings.get_mapping_revision(
            workspace_id
        )
        validation = context.mapping_workspace.mappings.get_mapping_validation(
            workspace_id,
            revision.version,
        )
        self.assertEqual(validation.status, MappingValidationStatus.INVALID)
        self.assertIn(
            "MAPPING_FORMULA_INVALID",
            {issue.code for issue in validation.issues},
        )

    def test_formula_validation_script_guards_only_match_checking(self) -> None:
        script = self.client.get("/static/mapping-formula-validation.js")

        self.assertEqual(script.status_code, 200, script.text)
        self.assertIn("formulaValidationDelayMs = 500", script.text)
        self.assertIn("new AbortController()", script.text)
        self.assertIn('event.submitter?.value !== "draft"', script.text)
        self.assertIn("event.stopImmediatePropagation()", script.text)
        self.assertIn("applySaveResult", script.text)
        self.assertIn("formulaApplies", script.text)
        self.assertIn("generations.set(key", script.text)

        recovery = self.client.get("/static/mapping-save-recovery.js")
        self.assertEqual(recovery.status_code, 200, recovery.text)
        self.assertIn("mutationTimeoutMs", recovery.text)
        self.assertIn("fetchWithTimeout", recovery.text)
        self.assertIn("readMutationReceipt", recovery.text)
        self.assertIn("MAPPING_VERSION_CONFLICT", recovery.text)
        self.assertIn("Check save outcome", recovery.text)
        self.assertIn("window.impodoMappingSaveRecovery", recovery.text)

        editor = self.client.get("/static/mapping-editor.js")
        self.assertEqual(editor.status_code, 200, editor.text)
        self.assertIn("window.impodoMappingSaveRecovery.create", editor.text)
        self.assertIn("mappingForm.removeAttribute(\"aria-busy\")", editor.text)
        self.assertIn("finally", editor.text)

        workspace_id, _dataset, _business_key = self._mapping_ready_workspace(
            scalar_field_count=1
        )
        page = self.client.get(f"/workspaces/{workspace_id}/mapping")
        self.assertEqual(page.status_code, 200, page.text)
        self.assertIn('data-mutation-timeout-ms="15000"', page.text)
        self.assertIn("data-mapping-save-outcome", page.text)
        self.assertIn("data-copy-mapping-edits", page.text)
        self.assertIn("data-reload-saved-mapping", page.text)
        self.assertIn("data-check-mapping-outcome", page.text)

    def test_selection_choices_load_and_save_from_the_mapping_dialog(self) -> None:
        workspace_id, dataset, business_key = self._mapping_ready_workspace(
            scalar_field_count=1,
            selection_field=True,
        )
        source_identity, source_value = dataset.columns

        page = self.client.get(f"/workspaces/{workspace_id}/mapping")
        self.assertEqual(page.status_code, 200)
        self.assertIn("data-value-match-dialog", page.text)
        self.assertIn("Match values", page.text)
        self.assertIn("Choice field", page.text)
        self.assertIn("2 choice(s) captured from Odoo", page.text)
        self.assertIn("Match source choices to Odoo", page.text)
        self.assertIn("How must source choices be covered?", page.text)
        self.assertIn("French (France) — fr_FR", page.text)
        self.assertNotIn("datalist", page.text)
        mapping_script = self.client.get("/static/mapping-editor.js")
        self.assertIn(
            'event.target.closest?.("[data-open-value-match]")',
            mapping_script.text,
        )
        self.assertIn(
            'mappingForm.addEventListener("change", (event) => {',
            mapping_script.text,
        )
        self.assertNotIn(
            'for (const trigger of mappingForm.querySelectorAll(\n'
            '      "[data-open-value-match]"',
            mapping_script.text,
        )
        with patch(
            "impodo.web.routers.mapping._source_value_choices",
            return_value=(
                {"value": "French", "count": 12},
                {"value": "German", "count": 4},
            ),
        ):
            choices = self.client.post(
                f"/workspaces/{workspace_id}/mapping/value-choices",
                data={
                    "csrf_token": self.csrf,
                    "kind": "scalar",
                    "dataset_id": dataset.dataset_id,
                    "source_column_key": source_value.stable_key,
                    "target_model": "res.partner",
                    "target_field": "field_0000",
                    "business_key_id": "",
                },
                headers=POST_HEADERS,
            )

        self.assertEqual(choices.status_code, 200)
        self.assertEqual(
            choices.json()["target_choices"],
            [
                {"value": "fr_FR", "label": "French (France)"},
                {"value": "de_DE", "label": "German"},
            ],
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
            ["source_identity_0", source_identity.stable_key],
            ["business_key_0", business_key.key_id],
            ["identity_source_0_0", source_identity.stable_key],
            ["visible_scalar_target_0", "field_0000"],
            ["scalar_value_source_0_1", "source"],
            ["scalar_source_0_1", source_value.stable_key],
            ["scalar_type_0_1", "string"],
            ["scalar_case_0_1", "preserve"],
            ["scalar_compare_0_1", "1"],
            ["scalar_null_0_1", "distinct"],
            [
                "scalar_value_matches_0_1",
                '[{"source_value":"French","target_value":"fr_FR"}]',
            ],
        ]
        saved = self.client.post(
            f"/workspaces/{workspace_id}/mapping/save",
            json={"entries": entries},
            headers={**POST_HEADERS, "X-CSRF-Token": self.csrf},
        )

        self.assertEqual(saved.status_code, 200)
        working = self.app.state.context.mapping_workspace.mappings.get_mapping_working_draft(
            workspace_id
        )
        self.assertEqual(
            working.definition.datasets[0].fields[0].value_mappings,
            (ValueMapping("French", "fr_FR"),),
        )
        self.assertEqual(
            working.definition.datasets[0].fields[0].categorical_policy,
            CategoricalCoveragePolicy.EXPLICIT_VALUE_MATCH,
        )

    def test_relationship_choices_request_the_inline_read_key_recovery(
        self,
    ) -> None:
        workspace_id, dataset, business_key = self._mapping_ready_workspace(
            scalar_field_count=0,
            relationship_field_count=1,
        )
        source_value = dataset.columns[1]
        with (
            patch(
                "impodo.web.routers.mapping._source_value_choices",
                return_value=({"value": "FRA", "count": 3},),
            ),
            patch(
                "impodo.web.routers.mapping._relationship_value_choices",
                side_effect=OdooReadCredentialMissingError(
                    "Enter the read-only Odoo key to load current countries"
                ),
            ),
        ):
            response = self.client.post(
                f"/workspaces/{workspace_id}/mapping/value-choices",
                data={
                    "csrf_token": self.csrf,
                    "kind": "relationship",
                    "dataset_id": dataset.dataset_id,
                    "source_column_key": source_value.stable_key,
                    "target_model": "res.partner",
                    "target_field": "relation_0000",
                    "business_key_id": business_key.key_id,
                },
                headers=POST_HEADERS,
            )

        self.assertEqual(response.status_code, 422, response.text)
        self.assertTrue(
            response.json()["read_credential_required"],
            response.text,
        )
        self.assertEqual(
            response.json()["read_credential_failure_code"],
            OdooReadFailureCode.READ_KEY_MISSING.value,
        )
        self.assertNotIn("Odoo API key", response.text)

        script = self.client.get("/static/mapping-editor.js")
        self.assertIn("payload.read_credential_required === true", script.text)
        self.assertIn('"impodo:read-credential-saved"', script.text)

    def test_relationship_choices_are_read_once_without_exposing_odoo_ids(
        self,
    ) -> None:
        workspace_id, dataset, business_key = self._mapping_ready_workspace(
            scalar_field_count=0,
            relationship_field_count=1,
        )
        source_value = dataset.columns[1]
        context = self.app.state.context
        calls = []

        def readiness_reader(workspace_state, metadata_requests, record_requests):
            calls.append((metadata_requests, record_requests))
            metadata = _browser_schema(workspace_state)
            return metadata, RecordSnapshot(
                fingerprint=metadata.fingerprint,
                records={
                    "res.partner": (
                        TargetRecord("res.partner", 10, {"ref": "FR"}),
                        TargetRecord("res.partner", 11, {"ref": "DE"}),
                        TargetRecord("res.partner", 12, {"ref": "BE"}),
                        TargetRecord("res.partner", 13, {"ref": "BE"}),
                    )
                },
                requested_fields={"res.partner": ("ref",)},
            )

        context.readiness_reader = readiness_reader
        with patch(
            "impodo.web.routers.mapping._source_value_choices",
            return_value=({"value": "FRA", "count": 3},),
        ):
            response = self.client.post(
                f"/workspaces/{workspace_id}/mapping/value-choices",
                data={
                    "csrf_token": self.csrf,
                    "kind": "relationship",
                    "dataset_id": dataset.dataset_id,
                    "source_column_key": source_value.stable_key,
                    "target_model": "res.partner",
                    "target_field": "relation_0000",
                    "business_key_id": business_key.key_id,
                },
                headers=POST_HEADERS,
            )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(calls[0][1]), 1)
        self.assertEqual(
            response.json()["target_choices"],
            [
                {"value": "DE", "label": "DE"},
                {"value": "FR", "label": "FR"},
            ],
        )
        self.assertEqual(response.json()["ambiguous_values"], ["BE"])
        self.assertNotIn("odoo_id", response.text)
        source_identity = dataset.columns[0]
        saved = self.client.post(
            f"/workspaces/{workspace_id}/mapping/save",
            json={
                "entries": [
                    ["csrf_token", self.csrf],
                    ["action", "save_progress"],
                    ["expected_parent_version", ""],
                    ["expected_working_draft_version", ""],
                    ["editable_dataset_id", dataset.dataset_id],
                    ["target_model_0", "res.partner"],
                    ["mode_0", "upsert"],
                    ["on_existing_0", "block"],
                    ["source_identity_0", source_identity.stable_key],
                    ["business_key_0", business_key.key_id],
                    ["identity_source_0_0", source_identity.stable_key],
                    ["visible_relation_target_0", "relation_0000"],
                    ["relation_source_0_0", source_value.stable_key],
                    ["relation_origin_0_0", "target_catalog"],
                    ["relation_key_0_0", business_key.key_id],
                    ["relation_operation_0_0", "add"],
                    ["relation_compare_0_0", "1"],
                    ["relation_missing_0_0", "error"],
                    ["relation_ambiguous_0_0", "error"],
                    ["relation_null_0_0", "distinct"],
                    ["relation_separator_0_0", ";"],
                    [
                        "relation_value_matches_0_0",
                        '[{"source_value":"FRA","target_value":"FR"}]',
                    ],
                ]
            },
            headers={**POST_HEADERS, "X-CSRF-Token": self.csrf},
        )

        self.assertEqual(saved.status_code, 200)
        working = context.mapping_workspace.mappings.get_mapping_working_draft(workspace_id)
        self.assertEqual(
            working.definition.datasets[0]
            .relationships[0]
            .resolver.value_mappings,
            (ValueMapping("FRA", "FR"),),
        )
        self.assertEqual(
            working.definition.datasets[0].relationships[0].categorical_policy,
            CategoricalCoveragePolicy.EXPLICIT_KEY_MATCH,
        )
        self.assertEqual(
            working.definition.datasets[0].relationships[0].operation,
            "replace",
        )

    def test_matching_rule_without_description_uses_odoo_field_label(self) -> None:
        workspace_id, _dataset, _business_key = self._mapping_ready_workspace(
            scalar_field_count=0,
            relationship_field_count=1,
            business_key_description="",
        )

        page = self.client.get(f"/workspaces/{workspace_id}/mapping")

        self.assertEqual(page.status_code, 200)
        self.assertIn(">\n                        Reference\n", page.text)
        self.assertIn(">\n                            Reference\n", page.text)
        self.assertNotIn("Confirmed matching rule", page.text)

    def test_country_matching_uses_reviewed_code_without_schema_recapture(
        self,
    ) -> None:
        workspace_id, dataset, business_key = self._mapping_ready_workspace(
            scalar_field_count=0,
            relationship_field_count=1,
            relationship_model="res.country",
        )
        source_identity, source_country = dataset.columns
        context = self.app.state.context
        calls = []

        def readiness_reader(workspace_state, metadata_requests, record_requests):
            calls.append((metadata_requests, record_requests))
            available = _browser_schema(workspace_state)
            metadata = replace(
                available,
                models={
                    "res.country": ModelMetadata(
                        model="res.country",
                        description="Country",
                        fields={
                            "code": FieldMetadata(
                                name="code",
                                type="char",
                                label="Country Code",
                                required=True,
                            ),
                            "name": FieldMetadata(
                                name="name",
                                type="char",
                                label="Country Name",
                                required=True,
                            ),
                        },
                    )
                },
            )
            return metadata, RecordSnapshot(
                fingerprint=metadata.fingerprint,
                records={
                    "res.country": (
                        TargetRecord(
                            "res.country",
                            1,
                            {"code": "FR", "name": "France"},
                        ),
                        TargetRecord(
                            "res.country",
                            2,
                            {"code": "BE", "name": "Belgium"},
                        ),
                    )
                },
                requested_fields={"res.country": ("code", "name")},
            )

        context.readiness_reader = readiness_reader
        page = self.client.get(f"/workspaces/{workspace_id}/mapping")

        self.assertEqual(page.status_code, 200)
        schema = context.schema_workspace.schemas.get_odoo_schema_catalog(workspace_id)
        self.assertNotEqual(
            schema.content_hash,
            target_identity_hash(
                connection_mode="LOCAL",
                base_url="http://127.0.0.1:8069",
                database="odoo19_local",
            ),
        )
        self.assertIn("Country code — recommended", page.text)
        self.assertIn(
            'value="odoo-standard:res.country:code" selected',
            page.text,
        )
        self.assertIn("data-refresh-value-match", page.text)
        mapping_script = self.client.get("/static/mapping-editor.js")
        self.assertIn("Using saved Odoo values checked", mapping_script.text)
        self.assertIn(
            'body.set("refresh", refresh ? "1" : "0")',
            mapping_script.text,
        )
        with patch(
            "impodo.web.routers.mapping._source_value_choices",
            return_value=({"value": "FRA", "count": 3},),
        ):
            request_data = {
                "csrf_token": self.csrf,
                "kind": "relationship",
                "dataset_id": dataset.dataset_id,
                "source_column_key": source_country.stable_key,
                "target_model": "res.partner",
                "target_field": "relation_0000",
                "business_key_id": "odoo-standard:res.country:code",
            }
            choices = self.client.post(
                f"/workspaces/{workspace_id}/mapping/value-choices",
                data=request_data,
                headers=POST_HEADERS,
            )
            reused = self.client.post(
                f"/workspaces/{workspace_id}/mapping/value-choices",
                data=request_data,
                headers=POST_HEADERS,
            )
            refreshed = self.client.post(
                f"/workspaces/{workspace_id}/mapping/value-choices",
                data={**request_data, "refresh": "1"},
                headers=POST_HEADERS,
            )

        self.assertEqual(choices.status_code, 200, choices.text)
        self.assertEqual(
            choices.json()["target_choices"],
            [
                {"value": "BE", "label": "Belgium (BE)"},
                {"value": "FR", "label": "France (FR)"},
            ],
        )
        self.assertFalse(choices.json()["target_choices_reused"])
        self.assertTrue(choices.json()["target_checked_at"])
        self.assertEqual(reused.status_code, 200)
        self.assertTrue(reused.json()["target_choices_reused"])
        self.assertEqual(refreshed.status_code, 200)
        self.assertFalse(refreshed.json()["target_choices_reused"])
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][0][0].model, "res.country")
        self.assertEqual(calls[0][0][0].fields, ("code", "name"))
        self.assertEqual(calls[0][1][0].model, "res.country")
        self.assertEqual(calls[0][1][0].fields, ("code", "name"))
        self.assertEqual(calls[0][1][0].limit, 2001)
        self.assertNotIn("odoo_id", choices.text)

        saved = self.client.post(
            f"/workspaces/{workspace_id}/mapping/save",
            json={
                "entries": [
                    ["csrf_token", self.csrf],
                    ["action", "save_progress"],
                    ["expected_parent_version", ""],
                    ["expected_working_draft_version", ""],
                    ["editable_dataset_id", dataset.dataset_id],
                    ["target_model_0", "res.partner"],
                    ["mode_0", "upsert"],
                    ["on_existing_0", "block"],
                    ["source_identity_0", source_identity.stable_key],
                    ["business_key_0", business_key.key_id],
                    ["identity_source_0_0", source_identity.stable_key],
                    ["visible_relation_target_0", "relation_0000"],
                    ["relation_source_0_0", source_country.stable_key],
                    ["relation_origin_0_0", "target_catalog"],
                    [
                        "relation_key_0_0",
                        "odoo-standard:res.country:code",
                    ],
                    ["relation_operation_0_0", "replace"],
                    ["relation_compare_0_0", "1"],
                    ["relation_missing_0_0", "error"],
                    ["relation_ambiguous_0_0", "error"],
                    ["relation_null_0_0", "distinct"],
                    ["relation_separator_0_0", ";"],
                    [
                        "relation_value_matches_0_0",
                        '[{"source_value":"FRA","target_value":"FR"}]',
                    ],
                ]
            },
            headers={**POST_HEADERS, "X-CSRF-Token": self.csrf},
        )

        self.assertEqual(saved.status_code, 200)
        working = context.mapping_workspace.mappings.get_mapping_working_draft(workspace_id)
        resolver = working.definition.datasets[0].relationships[0].resolver
        self.assertEqual(
            resolver.key_mappings,
            (ReferenceKeyMapping(source_country.stable_key, "code"),),
        )
        self.assertEqual(
            resolver.value_mappings,
            (ValueMapping("FRA", "FR"),),
        )

    def test_reviewed_reference_matching_is_not_country_specific(self) -> None:
        for related_model, label in (
            ("res.lang", "Language code — recommended"),
            ("res.currency", "Currency code — recommended"),
        ):
            with self.subTest(related_model=related_model):
                workspace_id, _dataset, _business_key = self._mapping_ready_workspace(
                    scalar_field_count=0,
                    relationship_field_count=1,
                    relationship_model=related_model,
                )

                page = self.client.get(f"/workspaces/{workspace_id}/mapping")

                self.assertEqual(page.status_code, 200)
                self.assertIn(label, page.text)
                self.assertNotIn("No matching rule available", page.text)

    def test_uncaptured_many2one_offers_an_explicit_name_matching_rule(
        self,
    ) -> None:
        workspace_id, _dataset, _business_key = self._mapping_ready_workspace(
            scalar_field_count=0,
            relationship_field_count=1,
            relationship_model="res.company",
        )

        page = self.client.get(f"/workspaces/{workspace_id}/mapping")

        self.assertEqual(page.status_code, 200)
        self.assertIn("Odoo record name", page.text)
        self.assertIn("confirm", page.text)
        self.assertIn("Match values", page.text)
        self.assertNotIn('name="relation_key_0_0" disabled', page.text)
        self.assertNotIn("No matching rule available", page.text)

    def test_product_uom_choices_are_fetched_as_bounded_supporting_data(
        self,
    ) -> None:
        workspace_id, dataset, business_key = self._mapping_ready_workspace(
            scalar_field_count=0,
            relationship_field_count=1,
            relationship_model="uom.uom",
            target_model="product.template",
            relationship_field_names=("uom_id",),
        )
        source_identity, source_uom = dataset.columns
        context = self.app.state.context
        calls = []

        def readiness_reader(workspace_state, metadata_requests, record_requests):
            calls.append((metadata_requests, record_requests))
            available = _browser_schema(workspace_state)
            metadata = replace(
                available,
                models={
                    "uom.uom": ModelMetadata(
                        model="uom.uom",
                        description="Unit of Measure",
                        fields={
                            "name": FieldMetadata(
                                name="name",
                                type="char",
                                label="Unit of Measure",
                                required=True,
                            ),
                        },
                    )
                },
            )
            return metadata, RecordSnapshot(
                fingerprint=metadata.fingerprint,
                records={
                    "uom.uom": (
                        TargetRecord("uom.uom", 41, {"name": "Units"}),
                        TargetRecord("uom.uom", 42, {"name": "kg"}),
                        TargetRecord("uom.uom", 43, {"name": "Hours"}),
                    )
                },
                requested_fields={"uom.uom": ("name",)},
            )

        context.readiness_reader = readiness_reader
        page = self.client.get(f"/workspaces/{workspace_id}/mapping")
        self.assertIn("Only existing Odoo records", page.text)
        self.assertIn("Only another incoming table", page.text)
        self.assertIn(
            "Use Odoo first, otherwise use the incoming table",
            page.text,
        )
        self.assertIn(
            "never updates it merely because it won this match",
            page.text,
        )
        candidate = re.search(
            r'<option value="([^"]+)"[^>]*>\s*Odoo record name',
            page.text,
        )
        self.assertIsNotNone(candidate, page.text)
        candidate_key_id = candidate.group(1)
        self.assertNotIn(f'value="{candidate_key_id}" selected', page.text)

        with patch(
            "impodo.web.routers.mapping._source_value_choices",
            return_value=({"value": "Unit", "count": 3},),
        ):
            choices = self.client.post(
                f"/workspaces/{workspace_id}/mapping/value-choices",
                data={
                    "csrf_token": self.csrf,
                    "kind": "relationship",
                    "dataset_id": dataset.dataset_id,
                    "source_column_key": source_uom.stable_key,
                    "target_model": "product.template",
                    "target_field": "uom_id",
                    "business_key_id": candidate_key_id,
                },
                headers=POST_HEADERS,
            )

        self.assertEqual(choices.status_code, 200, choices.text)
        self.assertEqual(
            choices.json()["target_choices"],
            [
                {"value": "Hours", "label": "Hours"},
                {"value": "kg", "label": "kg"},
                {"value": "Units", "label": "Units"},
            ],
        )
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0][0].model, "uom.uom")
        self.assertEqual(calls[0][0][0].fields, ("name",))
        self.assertEqual(calls[0][1][0].fields, ("name",))
        self.assertEqual(calls[0][1][0].limit, 2001)
        self.assertNotIn("odoo_id", choices.text)
        self.assertNotIn("41", choices.text)

        schema = context.queries.get_odoo_schema_catalog(workspace_id)
        lookup = context.supporting_lookups.current(
            workspace_id,
            relation_model="uom.uom",
            key_fields=("name",),
            scope_fields=(),
            display_field="name",
            target_hash=schema.connection_target_hash,
            read_credential_binding_hash=schema.read_credential_binding_hash,
            read_principal_hash=schema.read_principal_hash,
            read_context_hash=schema.read_context_hash,
            actor=context.actor,
        )
        self.assertIsNotNone(lookup)
        context.supporting_lookups.capture(
            workspace_id,
            relation_model=lookup.relation_model,
            key_fields=lookup.key_fields,
            scope_fields=lookup.scope_fields,
            display_field=lookup.display_field,
            field_contracts=lookup.field_contracts,
            target_hash=lookup.target_hash,
            read_credential_binding_hash=lookup.read_credential_binding_hash,
            read_principal_hash=lookup.read_principal_hash,
            read_permission_hash="sha256:" + "9" * 64,
            read_context_hash=lookup.read_context_hash,
            captured_at=lookup.captured_at,
            choices=lookup.choices,
            ambiguous_values=lookup.ambiguous_values,
            actor=context.actor,
        )

        saved = self.client.post(
            f"/workspaces/{workspace_id}/mapping/save",
            json={
                "entries": [
                    ["csrf_token", self.csrf],
                    ["action", "save_progress"],
                    ["expected_parent_version", ""],
                    ["expected_working_draft_version", ""],
                    ["editable_dataset_id", dataset.dataset_id],
                    ["target_model_0", "product.template"],
                    ["mode_0", "upsert"],
                    ["on_existing_0", "block"],
                    ["source_identity_0", source_identity.stable_key],
                    ["business_key_0", business_key.key_id],
                    ["identity_source_0_0", source_identity.stable_key],
                    ["visible_relation_target_0", "uom_id"],
                    ["relation_source_0_0", source_uom.stable_key],
                    ["relation_origin_0_0", "target_catalog"],
                    ["relation_key_0_0", candidate_key_id],
                    ["relation_operation_0_0", "replace"],
                    ["relation_compare_0_0", "1"],
                    ["relation_missing_0_0", "error"],
                    ["relation_ambiguous_0_0", "error"],
                    ["relation_null_0_0", "distinct"],
                    ["relation_separator_0_0", ";"],
                    [
                        "relation_value_matches_0_0",
                        '[{"source_value":"Unit","target_value":"Units"}]',
                    ],
                ]
            },
            headers={**POST_HEADERS, "X-CSRF-Token": self.csrf},
        )

        self.assertEqual(saved.status_code, 200, saved.text)
        working = context.mapping_workspace.mappings.get_mapping_working_draft(
            workspace_id
        )
        self.assertEqual(
            working.definition.datasets[0].relationships[0].operation,
            "replace",
        )
        schema = context.queries.get_odoo_schema_catalog(workspace_id)
        governance = context.queries.get_schema_governance(workspace_id)
        selection = context.queries.get_mapping_source_selection(workspace_id)
        supporting = context.mapping_workspace._current_supporting_references(
            workspace_id,
            working.definition,
            schema,
        )
        validation = context.mapping_workspace.validator.validate(
            working.definition,
            selection,
            schema,
            governance,
            supporting,
        )
        codes = {item.code for item in validation.issues}
        self.assertEqual(len(supporting), 1)
        self.assertEqual(supporting[0].relation_model, "uom.uom")
        self.assertEqual(supporting[0].field_contracts[0].name, "name")
        self.assertNotIn("MAPPING_TARGET_MODEL_UNKNOWN", codes)
        self.assertNotIn("MAPPING_BUSINESS_KEY_NOT_GOVERNED", codes)

    def test_relationship_catalog_is_searchable_and_progressively_disclosed(
        self,
    ) -> None:
        workspace_id, dataset, _business_key = self._mapping_ready_workspace(
            scalar_field_count=1,
            relationship_field_count=51,
        )
        source_identity, source_value = dataset.columns
        context = self.app.state.context
        initial = context.mapping_workspace.save_working_draft(
            workspace_id,
            datasets=(
                DatasetMapping(
                    dataset_id=dataset.dataset_id,
                    target_model="res.partner",
                    mode=MappingTargetMode.UPSERT,
                    source_identity_column_keys=(source_identity.stable_key,),
                    target_identity=(
                        IdentityComponentMapping(
                            source_column_keys=(source_identity.stable_key,),
                            target_fields=("ref",),
                        ),
                    ),
                    relationships=(
                        RelationshipMapping(
                            target_field="relation_0050",
                            kind="many2one",
                            source_column_keys=(source_value.stable_key,),
                            resolver=RelationshipResolver(
                                origin=ResolverOrigin.TARGET_CATALOG,
                                model="res.partner",
                                key_mappings=(
                                    ReferenceKeyMapping(
                                        source_column_key=source_value.stable_key,
                                        target_field="ref",
                                    ),
                                ),
                            ),
                        ),
                    ),
                ),
            ),
            expected_version=None,
            actor=context.actor,
        )
        self.assertEqual(initial.version, 1)

        page = self.client.get(f"/workspaces/{workspace_id}/mapping")
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.text.count("data-relation-field-row"), 3)
        self.assertIn("Showing 3 of 51 linked fields", page.text)
        self.assertIn('data-relation-page-size="3"', page.text)
        self.assertLess(
            page.text.index("relation_0050"),
            page.text.index("relation_0000"),
        )
        self.assertNotIn("relation_0002</code>", page.text)

        expanded = self.client.get(
            f"/workspaces/{workspace_id}/mapping?relation_page_size=20"
        )
        self.assertEqual(expanded.text.count("data-relation-field-row"), 20)
        self.assertIn('data-relation-page-size="20"', expanded.text)

        searched = self.client.get(
            f"/workspaces/{workspace_id}/mapping?relation_query=relation_0049"
        )
        self.assertEqual(searched.status_code, 200)
        self.assertEqual(searched.text.count("data-relation-field-row"), 1)
        self.assertIn("Linked Field 0049", searched.text)
        self.assertIn("Showing 1 of 1 linked fields", searched.text)

        fragment = self.client.get(
            f"/workspaces/{workspace_id}/mapping/field-catalog"
            "?catalog=relation&relation_query=relation_0049"
        )
        self.assertEqual(fragment.status_code, 200)
        self.assertEqual(fragment.text.count("data-relation-field-row"), 1)
        self.assertIn("Linked Field 0049", fragment.text)
        self.assertNotIn("data-scalar-field-catalog", fragment.text)
        self.assertNotIn("<main", fragment.text)
        self.assertIn("projection;dur=", fragment.headers["server-timing"])

        searched_by_model = self.client.get(
            f"/workspaces/{workspace_id}/mapping?relation_query=res.partner"
        )
        self.assertEqual(
            searched_by_model.text.count("data-relation-field-row"),
            3,
        )
        self.assertIn("Showing 3 of 51 linked fields", searched_by_model.text)

        last_page = self.client.get(
            f"/workspaces/{workspace_id}/mapping?relation_page=17"
        )
        self.assertEqual(last_page.text.count("data-relation-field-row"), 3)
        self.assertIn("relation_0049", last_page.text)

        rejected_size = self.client.get(
            f"/workspaces/{workspace_id}/mapping?relation_page_size=100"
        )
        self.assertEqual(
            rejected_size.text.count("data-relation-field-row"),
            3,
        )
        self.assertIn('data-relation-page-size="3"', rejected_size.text)

    def test_readonly_field_matches_are_hidden_and_recovered_as_one_decision(
        self,
    ) -> None:
        workspace_id, dataset, _business_key = self._mapping_ready_workspace(
            scalar_field_count=4,
            readonly_scalar_indexes=(1, 2),
        )
        source_identity, source_value = dataset.columns
        context = self.app.state.context
        mapping = DatasetMapping(
            dataset_id=dataset.dataset_id,
            target_model="res.partner",
            mode=MappingTargetMode.UPSERT,
            source_identity_column_keys=(source_identity.stable_key,),
            target_identity=(
                IdentityComponentMapping(
                    source_column_keys=(source_identity.stable_key,),
                    target_fields=("ref",),
                ),
            ),
            fields=(
                ScalarFieldMapping(
                    target_field="field_0001",
                    source_column_key=source_value.stable_key,
                ),
                ScalarFieldMapping(
                    target_field="field_0002",
                    source_column_key=source_value.stable_key,
                    compare=False,
                    validate_only=True,
                ),
            ),
        )
        _revision, validation = context.mapping_workspace.check_definition(
            workspace_id,
            datasets=(mapping,),
            expected_parent_version=None,
            expected_working_draft_version=None,
            actor=context.actor,
        )
        self.assertEqual(validation.status, MappingValidationStatus.INVALID)
        self.assertEqual(
            [
                item.target_field
                for item in validation.issues
                if item.code == "MAPPING_TARGET_FIELD_READONLY"
            ],
            ["field_0001"],
        )

        page = self.client.get(f"/workspaces/{workspace_id}/mapping")

        self.assertEqual(page.status_code, 200)
        self.assertIn("Odoo manages 1 selected field", page.text)
        self.assertIn("Remove this field match", page.text)
        self.assertIn("1 decision", page.text)
        self.assertNotIn('data-target-field="field_0001"', page.text)
        self.assertIn('data-target-field="field_0002"', page.text)
        self.assertIn('data-target-field="field_0003"', page.text)
        self.assertIn('name="scalar_value_source_0_3"', page.text)
        self.assertEqual(
            page.text.count("MAPPING_TARGET_FIELD_READONLY"),
            1,
        )

        recovered = self.client.post(
            f"/workspaces/{workspace_id}/mapping/save",
            data={
                "csrf_token": self.csrf,
                "action": "remove_readonly",
                "expected_parent_version": "1",
                "expected_working_draft_version": "1",
            },
            headers=POST_HEADERS,
            follow_redirects=False,
        )

        self.assertEqual(recovered.status_code, 303)
        recovered_page = self.client.get(recovered.headers["location"])
        self.assertIn(
            "Removed 1 Odoo-managed field match. Check matches again when ready.",
            recovered_page.text,
        )
        self.assertNotIn("Odoo manages 1 selected field", recovered_page.text)
        working = context.mapping_workspace.mappings.get_mapping_working_draft(
            workspace_id
        )
        self.assertEqual(working.version, 2)
        self.assertEqual(
            [item.target_field for item in working.definition.datasets[0].fields],
            ["field_0002"],
        )

    def test_verified_required_default_is_reviewed_and_confirmed_as_a_group(
        self,
    ) -> None:
        workspace_id, dataset, business_key = self._mapping_ready_workspace(
            scalar_field_count=1,
            required_scalar_indexes=(0,),
            verified_default_scalar_indexes=(0,),
            target_model="sale.order",
        )
        source_identity, _source_value = dataset.columns
        context = self.app.state.context
        revision, validation = context.mapping_workspace.check_definition(
            workspace_id,
            datasets=(
                DatasetMapping(
                    dataset_id=dataset.dataset_id,
                    target_model="sale.order",
                    mode=MappingTargetMode.UPSERT,
                    source_identity_column_keys=(source_identity.stable_key,),
                    target_identity=(
                        IdentityComponentMapping(
                            source_column_keys=(source_identity.stable_key,),
                            target_fields=business_key.key_fields,
                        ),
                    ),
                ),
            ),
            expected_parent_version=None,
            expected_working_draft_version=None,
            actor=context.actor,
        )
        self.assertEqual(validation.status, MappingValidationStatus.INVALID)

        page = self.client.get(
            f"/workspaces/{workspace_id}/mapping?field_query=not-visible"
        )

        self.assertEqual(page.status_code, 200)
        self.assertIn('id="next-step-blockers"', page.text)
        self.assertGreater(
            page.text.index('id="next-step-blockers"'),
            page.text.index('id="semantic-validation"'),
        )
        self.assertLess(
            page.text.index('id="next-step-blockers"'),
            page.text.index('class="actions mapping-actions"'),
        )
        self.assertIn("You cannot continue yet — 1 reason", page.text)
        self.assertIn("Review 1 Odoo default", page.text)
        self.assertIn("Field 0000:", page.text)
        self.assertIn(
            'value="confirm_defaults"',
            page.text,
        )
        self.assertIn("Odoo default 0000", page.text)
        self.assertIn("Use 1 Odoo default", page.text)
        self.assertRegex(
            page.text,
            r'<button class="button primary"[^>]*disabled>'
            r"Confirm field matches</button>",
        )
        self.assertNotIn('data-target-field="field_0000"', page.text)

        decision = self.client.post(
            f"/workspaces/{workspace_id}/mapping/save",
            data={
                "csrf_token": self.csrf,
                "action": "confirm_defaults",
                "expected_parent_version": str(revision.version),
                "expected_working_draft_version": "1",
            },
            headers=POST_HEADERS,
            follow_redirects=False,
        )

        self.assertEqual(decision.status_code, 303)
        working = context.mapping_workspace.mappings.get_mapping_working_draft(
            workspace_id
        )
        self.assertIsNotNone(working)
        assert working is not None
        self.assertEqual(
            working.definition.datasets[0]
            .target_field_dispositions[0]
            .handling,
            TargetFieldHandling.ODOO_DEFAULT,
        )
        decision_page = self.client.get(decision.headers["location"])
        self.assertIn("Odoo will choose this value", decision_page.text)
        self.assertIn(
            'name="target_field_disposition_0"', decision_page.text
        )
        self.assertNotIn('id="next-step-blockers"', decision_page.text)
        self.assertNotIn(
            "Saved changes have not been checked yet",
            decision_page.text,
        )
        self.assertNotIn("Keep working from the last check", decision_page.text)
        self.assertIn(
            "Confirmed 1 Odoo default. Matches checked and ready to confirm.",
            decision_page.text,
        )

        mapping_data = {
            "csrf_token": self.csrf,
            "editable_dataset_id": dataset.dataset_id,
            "target_model_0": "sale.order",
            "mode_0": "upsert",
            "on_existing_0": "block",
            "source_identity_0": source_identity.stable_key,
            "business_key_0": business_key.key_id,
            "identity_source_0_0": source_identity.stable_key,
            "visible_scalar_target_0": "field_0000",
            "target_field_disposition_0": "field_0000:odoo_default",
        }
        current_revision = context.mapping_workspace.mappings.get_mapping_revision(
            workspace_id
        )
        current_working = (
            context.mapping_workspace.mappings.get_mapping_working_draft(workspace_id)
        )
        current_validation = (
            context.mapping_workspace.mappings.get_mapping_validation(
                workspace_id,
                current_revision.version,
            )
        )
        self.assertEqual(current_validation.status, MappingValidationStatus.VALID)
        self.assertEqual(current_validation.issues, ())

        submitted = self.client.post(
            f"/workspaces/{workspace_id}/mapping/save",
            json={
                "entries": [
                    *mapping_data.items(),
                    ["action", "submit"],
                    ["expected_parent_version", str(current_revision.version)],
                    [
                        "expected_working_draft_version",
                        str(current_working.version),
                    ],
                ]
            },
            headers={**POST_HEADERS, "X-CSRF-Token": self.csrf},
        )

        self.assertEqual(submitted.status_code, 200)
        self.assertEqual(
            submitted.json()["redirect_url"],
            f"/workspaces/{workspace_id}/prepare",
        )
        submitted_page = self.client.get(submitted.json()["redirect_url"])
        self.assertIn("Field matches confirmed", submitted_page.text)
        self.assertIn("Prepare all source rows", submitted_page.text)

    def test_individual_let_odoo_choose_rechecks_matches(self) -> None:
        workspace_id, dataset, business_key = self._mapping_ready_workspace(
            scalar_field_count=1,
            required_scalar_indexes=(0,),
            verified_default_scalar_indexes=(0,),
        )
        source_identity, _source_value = dataset.columns
        context = self.app.state.context
        revision, validation = context.mapping_workspace.check_definition(
            workspace_id,
            datasets=(
                DatasetMapping(
                    dataset_id=dataset.dataset_id,
                    target_model="res.partner",
                    mode=MappingTargetMode.UPSERT,
                    source_identity_column_keys=(source_identity.stable_key,),
                    target_identity=(
                        IdentityComponentMapping(
                            source_column_keys=(source_identity.stable_key,),
                            target_fields=business_key.key_fields,
                        ),
                    ),
                ),
            ),
            expected_parent_version=None,
            expected_working_draft_version=None,
            actor=context.actor,
        )
        self.assertEqual(validation.status, MappingValidationStatus.INVALID)
        mapping_script = self.client.get("/static/mapping-editor.js")
        self.assertIn('action === "confirm_defaults"', mapping_script.text)
        self.assertIn('action.endsWith(":odoo_default")', mapping_script.text)
        self.assertIn(
            "Saving the Odoo decision and checking matches...",
            mapping_script.text,
        )

        decision = self.client.post(
            f"/workspaces/{workspace_id}/mapping/save",
            json={
                "entries": [
                    ["csrf_token", self.csrf],
                    [
                        "action",
                        "set_disposition:0:field_0000:odoo_default",
                    ],
                    ["expected_parent_version", str(revision.version)],
                    ["expected_working_draft_version", "1"],
                ]
            },
            headers={**POST_HEADERS, "X-CSRF-Token": self.csrf},
        )

        self.assertEqual(decision.status_code, 200)
        self.assertEqual(decision.json()["expected_working_draft_version"], 3)
        self.assertEqual(
            decision.json()["message"],
            "Odoo will choose this value. Matches checked and ready to confirm.",
        )
        decision_page = self.client.get(decision.json()["redirect_url"])
        self.assertNotIn('id="next-step-blockers"', decision_page.text)
        current_revision = context.mapping_workspace.mappings.get_mapping_revision(
            workspace_id
        )
        current_validation = (
            context.mapping_workspace.mappings.get_mapping_validation(
                workspace_id,
                current_revision.version,
            )
        )
        self.assertEqual(current_validation.status, MappingValidationStatus.VALID)
        self.assertEqual(current_validation.issues, ())

    def test_verified_required_defaults_are_confirmed_in_one_action(
        self,
    ) -> None:
        workspace_id, dataset, business_key = self._mapping_ready_workspace(
            scalar_field_count=2,
            required_scalar_indexes=(0, 1),
            verified_default_scalar_indexes=(0, 1),
        )
        source_identity, _source_value = dataset.columns
        context = self.app.state.context
        revision, validation = context.mapping_workspace.check_definition(
            workspace_id,
            datasets=(
                DatasetMapping(
                    dataset_id=dataset.dataset_id,
                    target_model="res.partner",
                    mode=MappingTargetMode.UPSERT,
                    source_identity_column_keys=(source_identity.stable_key,),
                    target_identity=(
                        IdentityComponentMapping(
                            source_column_keys=(source_identity.stable_key,),
                            target_fields=business_key.key_fields,
                        ),
                    ),
                ),
            ),
            expected_parent_version=None,
            expected_working_draft_version=None,
            actor=context.actor,
        )
        self.assertEqual(validation.status, MappingValidationStatus.INVALID)

        page = self.client.get(f"/workspaces/{workspace_id}/mapping")
        self.assertIn("Review 2 Odoo defaults", page.text)
        self.assertIn("Odoo default 0000", page.text)
        self.assertIn("Odoo default 0001", page.text)
        self.assertEqual(page.text.count('value="confirm_defaults"'), 1)

        decision = self.client.post(
            f"/workspaces/{workspace_id}/mapping/save",
            data={
                "csrf_token": self.csrf,
                "action": "confirm_defaults",
                "expected_parent_version": str(revision.version),
                "expected_working_draft_version": "1",
            },
            headers=POST_HEADERS,
            follow_redirects=False,
        )

        self.assertEqual(decision.status_code, 303)
        working = context.mapping_workspace.mappings.get_mapping_working_draft(
            workspace_id
        )
        self.assertEqual(
            [
                item.target_field
                for item in (
                    working.definition.datasets[0].target_field_dispositions
                )
            ],
            ["field_0000", "field_0001"],
        )
        decision_page = self.client.get(decision.headers["location"])
        self.assertNotIn('id="next-step-blockers"', decision_page.text)
        current_revision = context.mapping_workspace.mappings.get_mapping_revision(
            workspace_id
        )
        current_validation = (
            context.mapping_workspace.mappings.get_mapping_validation(
                workspace_id,
                current_revision.version,
            )
        )
        self.assertEqual(current_validation.status, MappingValidationStatus.VALID)
        self.assertEqual(current_validation.issues, ())

    def test_missing_required_defaults_are_checked_and_confirmed_in_one_action(
        self,
    ) -> None:
        workspace_id, dataset, business_key = self._mapping_ready_workspace(
            scalar_field_count=2,
            required_scalar_indexes=(0, 1),
        )
        source_identity, _source_value = dataset.columns
        context = self.app.state.context
        revision, validation = context.mapping_workspace.check_definition(
            workspace_id,
            datasets=(
                DatasetMapping(
                    dataset_id=dataset.dataset_id,
                    target_model="res.partner",
                    mode=MappingTargetMode.UPSERT,
                    source_identity_column_keys=(source_identity.stable_key,),
                    target_identity=(
                        IdentityComponentMapping(
                            source_column_keys=(source_identity.stable_key,),
                            target_fields=business_key.key_fields,
                        ),
                    ),
                ),
            ),
            expected_parent_version=None,
            expected_working_draft_version=None,
            actor=context.actor,
        )
        self.assertEqual(validation.status, MappingValidationStatus.INVALID)
        schema_before = context.queries.get_odoo_schema_catalog(workspace_id)
        governance_before = context.queries.get_schema_governance(workspace_id)
        calls = []

        def readiness_reader(workspace_state, metadata_requests, record_requests):
            calls.append((metadata_requests, record_requests))
            models = {model.name: model for model in schema_before.models}
            returned_models = {}
            defaults = {}
            for request in metadata_requests:
                model = models[request.model]
                fields = {field.name: field for field in model.fields}
                returned_models[request.model] = ModelMetadata(
                    model=request.model,
                    description=model.label,
                    fields={
                        field_name: FieldMetadata(
                            name=field_name,
                            type=fields[field_name].type,
                            label=fields[field_name].label,
                            required=fields[field_name].required,
                            readonly=fields[field_name].readonly,
                            relation=fields[field_name].relation,
                            relation_field=fields[field_name].relation_field,
                            selection=fields[field_name].selection,
                            stored=fields[field_name].stored,
                            computed=fields[field_name].computed,
                            has_inverse=fields[field_name].has_inverse,
                            related=fields[field_name].related,
                            translated=fields[field_name].translated,
                            company_dependent=fields[field_name].company_dependent,
                            searchable=fields[field_name].searchable,
                            sortable=fields[field_name].sortable,
                            exportable=fields[field_name].exportable,
                            digits=fields[field_name].digits,
                            currency_field=fields[field_name].currency_field,
                        )
                        for field_name in request.fields
                    },
                )
                defaults[request.model] = {
                    field_name: f"Current Odoo default {field_name}"
                    for field_name in request.fields
                }
            metadata = MetadataSnapshot(
                fingerprint=_browser_schema(workspace_state).fingerprint,
                models=returned_models,
                create_defaults=defaults,
            )
            return metadata, RecordSnapshot(
                fingerprint=metadata.fingerprint,
                records={},
                requested_fields={},
            )

        context.readiness_reader = readiness_reader
        page = self.client.get(f"/workspaces/{workspace_id}/mapping")

        self.assertIn("Let Odoo decide for 2 required fields", page.text)
        self.assertIn("read-only for this exact Odoo target", page.text)
        self.assertEqual(page.text.count('value="refresh_defaults"'), 1)

        decision = self.client.post(
            f"/workspaces/{workspace_id}/mapping/save",
            data={
                "csrf_token": self.csrf,
                "action": "refresh_defaults",
                "expected_parent_version": str(revision.version),
                "expected_working_draft_version": "1",
            },
            headers=POST_HEADERS,
            follow_redirects=False,
        )

        self.assertEqual(decision.status_code, 303, decision.text)
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(calls[0][0]), 1)
        self.assertEqual(
            calls[0][0][0].fields,
            ("field_0000", "field_0001"),
        )
        self.assertEqual(calls[0][1], ())
        schema_after = context.queries.get_odoo_schema_catalog(workspace_id)
        self.assertEqual(schema_after.content_hash, schema_before.content_hash)
        self.assertEqual(
            context.queries.get_schema_governance(workspace_id),
            governance_before,
        )
        recovered_fields = {
            field.name: field for field in schema_after.models[0].fields
        }
        self.assertEqual(
            recovered_fields["field_0000"].create_default_value,
            "Current Odoo default field_0000",
        )
        self.assertEqual(
            recovered_fields["field_0001"].create_default_value,
            "Current Odoo default field_0001",
        )
        working = context.mapping_workspace.mappings.get_mapping_working_draft(
            workspace_id
        )
        self.assertEqual(
            [
                item.target_field
                for item in working.definition.datasets[0].target_field_dispositions
            ],
            ["field_0000", "field_0001"],
        )
        decision_page = self.client.get(decision.headers["location"])
        self.assertIn("Odoo will decide 2 required fields", decision_page.text)
        self.assertIn("Odoo will choose this value", decision_page.text)
        self.assertNotIn('id="next-step-blockers"', decision_page.text)
        self.assertIn(
            "Matches checked and ready to confirm.",
            decision_page.text,
        )
        current_revision = context.mapping_workspace.mappings.get_mapping_revision(
            workspace_id
        )
        checked_validation = (
            context.mapping_workspace.mappings.get_mapping_validation(
                workspace_id,
                current_revision.version,
            )
        )
        self.assertEqual(checked_validation.status, MappingValidationStatus.VALID)
        self.assertEqual(checked_validation.issues, ())

    def test_default_recheck_keeps_only_remaining_blockers_visible(self) -> None:
        workspace_id, dataset, business_key = self._mapping_ready_workspace(
            scalar_field_count=2,
            required_scalar_indexes=(0, 1),
            verified_default_scalar_indexes=(0,),
        )
        source_identity, _source_value = dataset.columns
        context = self.app.state.context
        revision, validation = context.mapping_workspace.check_definition(
            workspace_id,
            datasets=(
                DatasetMapping(
                    dataset_id=dataset.dataset_id,
                    target_model="res.partner",
                    mode=MappingTargetMode.UPSERT,
                    source_identity_column_keys=(source_identity.stable_key,),
                    target_identity=(
                        IdentityComponentMapping(
                            source_column_keys=(source_identity.stable_key,),
                            target_fields=business_key.key_fields,
                        ),
                    ),
                ),
            ),
            expected_parent_version=None,
            expected_working_draft_version=None,
            actor=context.actor,
        )
        self.assertEqual(validation.status, MappingValidationStatus.INVALID)

        decision = self.client.post(
            f"/workspaces/{workspace_id}/mapping/save",
            data={
                "csrf_token": self.csrf,
                "action": "confirm_defaults",
                "expected_parent_version": str(revision.version),
                "expected_working_draft_version": "1",
            },
            headers=POST_HEADERS,
            follow_redirects=False,
        )

        self.assertEqual(decision.status_code, 303)
        self.assertTrue(
            decision.headers["location"].endswith("#next-step-blockers")
        )
        decision_page = self.client.get(decision.headers["location"])
        self.assertIn('id="next-step-blockers"', decision_page.text)
        self.assertIn("You cannot continue yet", decision_page.text)
        self.assertIn("1 reason", decision_page.text)
        self.assertNotIn("Review 1 Odoo default", decision_page.text)
        self.assertIn("Let Odoo decide for 1 required field", decision_page.text)
        self.assertIn(
            "Matches checked. Review the remaining items that need attention.",
            decision_page.text,
        )
        current_revision = context.mapping_workspace.mappings.get_mapping_revision(
            workspace_id
        )
        current_validation = (
            context.mapping_workspace.mappings.get_mapping_validation(
                workspace_id,
                current_revision.version,
            )
        )
        self.assertEqual(
            [item.target_field for item in current_validation.issues],
            ["field_0001"],
        )

    def test_default_action_routes_changed_odoo_fields_to_review(self) -> None:
        workspace_id, dataset, business_key = self._mapping_ready_workspace(
            scalar_field_count=2,
            required_scalar_indexes=(0, 1),
            connection_mode=OdooConnectionMode.REMOTE,
        )
        source_identity, _source_value = dataset.columns
        context = self.app.state.context
        revision, validation = context.mapping_workspace.check_definition(
            workspace_id,
            datasets=(
                DatasetMapping(
                    dataset_id=dataset.dataset_id,
                    target_model="res.partner",
                    mode=MappingTargetMode.UPSERT,
                    source_identity_column_keys=(source_identity.stable_key,),
                    target_identity=(
                        IdentityComponentMapping(
                            source_column_keys=(source_identity.stable_key,),
                            target_fields=business_key.key_fields,
                        ),
                    ),
                ),
            ),
            expected_parent_version=None,
            expected_working_draft_version=None,
            actor=context.actor,
        )
        self.assertEqual(validation.status, MappingValidationStatus.INVALID)
        schema_before = context.queries.get_odoo_schema_catalog(workspace_id)
        workspace_state = context.queries.get(workspace_id)
        current_model = schema_before.models[0]

        def metadata(field):
            return FieldMetadata(
                name=field.name,
                type=field.type,
                label=field.label,
                required=field.required,
                readonly=field.readonly,
                relation=field.relation,
                relation_field=field.relation_field,
                selection=field.selection,
                stored=field.stored,
                computed=field.computed,
                has_inverse=field.has_inverse,
                related=field.related,
                translated=field.translated,
                company_dependent=field.company_dependent,
                searchable=field.searchable,
                sortable=field.sortable,
                exportable=field.exportable,
                digits=field.digits,
                currency_field=field.currency_field,
            )

        def changed_identity(_workspace_state, _secret, models):
            normalized = tuple(sorted(models))
            return OdooReadIdentity(
                target_hash=schema_before.connection_target_hash,
                principal_hash=schema_before.read_principal_hash,
                permission_hash="sha256:" + "9" * 64,
                context_hash=schema_before.read_context_hash,
                readable_models=normalized,
                observed_at="2026-08-26T00:00:00Z",
            )

        def changed_schema(_workspace_state, _secret):
            fields = {field.name: metadata(field) for field in current_model.fields}
            fields["x_optional"] = FieldMetadata(
                name="x_optional",
                type="char",
                label="Optional field",
            )
            return MetadataSnapshot(
                fingerprint=_browser_schema(workspace_state).fingerprint,
                models={
                    current_model.name: ModelMetadata(
                        model=current_model.name,
                        description=current_model.label,
                        fields=fields,
                    )
                },
                create_defaults={
                    current_model.name: {
                        "field_0000": "First Odoo default",
                        "field_0001": "Second Odoo default",
                    }
                },
            )

        context.readiness_reader = None
        context.read_identity_probe = changed_identity
        context.schema_reader = changed_schema
        with patch("impodo.web.composition.target_readers.Json2ReadConnector") as connector:
            decision = self.client.post(
                f"/workspaces/{workspace_id}/mapping/save",
                json={
                    "entries": [
                        ["csrf_token", self.csrf],
                        ["action", "refresh_defaults"],
                        ["expected_parent_version", str(revision.version)],
                        ["expected_working_draft_version", "1"],
                    ]
                },
                headers={
                    **POST_HEADERS,
                    "Accept": "application/json",
                    "X-CSRF-Token": self.csrf,
                },
            )

        self.assertEqual(decision.status_code, 200, decision.text)
        self.assertEqual(
            decision.json()["redirect_url"],
            f"/workspaces/{workspace_id}/schema#odoo-details",
        )
        self.assertIn("Review", decision.json()["message"])
        connector.assert_not_called()
        checked_schema = context.queries.get_odoo_schema_catalog(workspace_id)
        self.assertEqual(checked_schema.content_hash, schema_before.content_hash)
        self.assertIsNotNone(checked_schema.pending_refresh)
        self.assertTrue(
            any(
                change.field_name == "x_optional"
                for change in checked_schema.pending_refresh.changes
            )
        )
        working = context.queries.get_mapping_working_draft(workspace_id)
        self.assertEqual(working.version, 1)
        self.assertEqual(
            working.definition.datasets[0].target_field_dispositions,
            (),
        )

    def test_required_managed_relationship_can_be_left_to_odoo(self) -> None:
        workspace_id, dataset, business_key = self._mapping_ready_workspace(
            scalar_field_count=0,
            relationship_field_count=1,
            relationship_field_type="one2many",
            required_relationship_indexes=(0,),
        )
        source_identity = dataset.columns[0]
        context = self.app.state.context
        _revision, validation = context.mapping_workspace.check_definition(
            workspace_id,
            datasets=(
                DatasetMapping(
                    dataset_id=dataset.dataset_id,
                    target_model="res.partner",
                    mode=MappingTargetMode.UPSERT,
                    source_identity_column_keys=(source_identity.stable_key,),
                    target_identity=(
                        IdentityComponentMapping(
                            source_column_keys=(source_identity.stable_key,),
                            target_fields=business_key.key_fields,
                        ),
                    ),
                ),
            ),
            expected_parent_version=None,
            expected_working_draft_version=None,
            actor=context.actor,
        )
        self.assertEqual(validation.status, MappingValidationStatus.INVALID)

        page = self.client.get(f"/workspaces/{workspace_id}/mapping")
        self.assertIn("Linked Field 0000 needs attention", page.text)
        self.assertIn(
            'value="set_disposition:0:relation_0000:odoo_managed"',
            page.text,
        )

        decision = self.client.post(
            f"/workspaces/{workspace_id}/mapping/save",
            data={
                "csrf_token": self.csrf,
                "action": "set_disposition:0:relation_0000:odoo_managed",
                "expected_working_draft_version": "1",
            },
            headers=POST_HEADERS,
            follow_redirects=False,
        )

        self.assertEqual(decision.status_code, 303)
        working = context.mapping_workspace.mappings.get_mapping_working_draft(
            workspace_id
        )
        self.assertEqual(
            working.definition.datasets[0]
            .target_field_dispositions[0]
            .handling,
            TargetFieldHandling.ODOO_MANAGED,
        )
        decision_page = self.client.get(decision.headers["location"])
        self.assertIn("Odoo manages this field", decision_page.text)

    def test_readonly_relationship_fields_are_hidden_without_shifting_indexes(
        self,
    ) -> None:
        workspace_id, _dataset, _business_key = self._mapping_ready_workspace(
            scalar_field_count=1,
            relationship_field_count=3,
            readonly_relationship_indexes=(1,),
        )

        page = self.client.get(f"/workspaces/{workspace_id}/mapping")

        self.assertEqual(page.status_code, 200)
        self.assertIn('data-target-field="relation_0000"', page.text)
        self.assertNotIn('data-target-field="relation_0001"', page.text)
        self.assertIn('data-target-field="relation_0002"', page.text)
        self.assertIn('name="relation_source_0_2"', page.text)
