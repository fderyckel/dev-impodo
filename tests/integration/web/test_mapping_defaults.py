"""Focused browser evidence for one Impodo capability."""

from __future__ import annotations

from io import BytesIO
from openpyxl import load_workbook

from impodo.domain.mapping.contracts import (
    RelationshipValueSource,
    UnsupportedMappingContractError,
)

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


class MappingDefaultsBrowserTests(ProjectSetupBrowserTestCase):
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
