"""Focused browser evidence for one Impodo capability."""

from __future__ import annotations

from tests.support.browser_scenarios import (
    OdooConnectionMode,
    OdooWriteIdentity,
    POST_HEADERS,
    Path,
    ProjectSetupBrowserTestCase,
    ReadbackRecord,
    TargetCredentialRole,
    WorkspaceStatus,
    _browser_schema,
    _created_workspace_id,
    _source_value_choices,
    _wait_for_load,
    _wait_for_preparation,
    _workbook_bytes,
    asdict,
    canonical_json_text,
    create_local_app,
    get_target_credential,
    json,
    parse_qs,
    patch,
    re,
    replace,
    time,
    unescape,
    urlsplit,
)


class ProjectSetupJourneyTests(ProjectSetupBrowserTestCase):
    def test_complete_project_setup_registration_without_yaml(self) -> None:
        created = self._post(
            "/projects/new",
            {
                "csrf_token": self.csrf,
                "display_name": "Customer migration",
                "source_mode": "FILE",
                "source_system_identity": "Dynamics AX 2012",
            },
        )
        self.assertEqual(created.status_code, 303)
        workspace_id = _created_workspace_id(self.app, created)
        data_project_id = self.app.state.context.migration_workspaces.get(
            workspace_id,
            actor=self.app.state.context.actor,
        ).project_id
        self.assertEqual(
            created.headers["location"],
            f"/projects/{data_project_id}",
        )

        uploaded = self.client.post(
            f"/workspaces/{workspace_id}/files",
            data={"csrf_token": self.csrf, "revision": "1"},
            files=[
                (
                    "source_file",
                    (
                    "customers.csv",
                    b"code,name\nC001,Example\n",
                    "text/csv",
                    ),
                ),
                (
                    "source_file",
                    (
                    "products.xlsx",
                    _workbook_bytes(),
                    "application/vnd.openxmlformats-officedocument."
                    "spreadsheetml.sheet",
                    ),
                ),
            ],
            headers=POST_HEADERS,
            follow_redirects=False,
        )
        self.assertEqual(uploaded.status_code, 303)

        registered = self._post(
            f"/workspaces/{workspace_id}/register",
            {"csrf_token": self.csrf, "revision": "3"},
        )
        self.assertEqual(registered.status_code, 303)
        self.assertEqual(
            registered.headers["location"],
            f"/workspaces/{workspace_id}/sources#source-files",
        )
        summary = self.client.get(f"/workspaces/{workspace_id}/overview")
        self.assertIn("Data version overview", summary.text)
        self.assertIn("Ready for source check", summary.text)
        self.assertIn("Check source data", summary.text)
        self.assertIn(
            '<div class="overview-stage-number" aria-hidden="true">1</div>',
            summary.text,
        )
        self.assertIn(
            '<div class="overview-stage-number" aria-hidden="true">6</div>',
            summary.text,
        )
        self.assertIn("Source data", summary.text)
        self.assertIn("Load into Odoo", summary.text)
        self.assertIn(
            f'href="/workspaces/{workspace_id}/sources"',
            summary.text,
        )
        workspace_state = self.app.state.context.workspace_states.repository.get(workspace_id)
        self.assertEqual(workspace_state.status, WorkspaceStatus.REGISTERED)
        self.assertIsNone(workspace_state.odoo_connection_mode)
        self.assertEqual(workspace_state.mapping_version, None)
        manifest = (
            self.app.state.context.workspace_states.repository.workspace_directory(workspace_id)
            / "audit"
            / f"workspace-registration-r{workspace_state.revision}.json"
        )
        self.assertTrue(manifest.is_file())
        manifest_text = manifest.read_text()
        self.assertIn('"contract_version":6', manifest_text)
        self.assertIn('"odoo_connection_mode":null', manifest_text)
        for removed_field in (
            "business_unit",
            "data_manager",
            "description",
            "export_date",
            "export_status",
            "functional_owner",
            "support_access",
        ):
            self.assertNotIn(f'"{removed_field}"', manifest_text)

        source_discovery = self.client.get(registered.headers["location"])
        self.assertEqual(source_discovery.status_code, 200)
        self.assertIn("Stage 1 of 6", source_discovery.text)
        self.assertIn("Source data", source_discovery.text)
        self.assertIn('aria-current="step"', source_discovery.text)
        self.assertIn('aria-current="page"', source_discovery.text)
        self.assertIn("2/2 checked", source_discovery.text)
        self.assertIn("Check files again", source_discovery.text)
        self.assertNotIn("Your files have not been checked yet", source_discovery.text)
        self.assertIn("data-source-review-page", source_discovery.text)
        self.assertIn("data-source-review-form", source_discovery.text)
        inspection_page = source_discovery
        self.assertIn("customers.csv", inspection_page.text)
        self.assertIn("C001", inspection_page.text)
        self.assertIn("products.xlsx", inspection_page.text)
        self.assertIn("ProductTable", inspection_page.text)
        self.assertIn('class="source-table-summary"', inspection_page.text)
        self.assertIn('class="source-table-title"', inspection_page.text)
        self.assertIn("covers the same data", inspection_page.text)
        self.assertNotIn("Use separate Excel tables instead", inspection_page.text)
        self.assertIn("Likely content", inspection_page.text)
        self.assertIn("data-source-review-card", inspection_page.text)
        catalogs = self.app.state.context.sources.sources.get_source_catalogs(workspace_id)
        self.assertEqual(len(catalogs), 2)
        catalogs_by_name = {catalog.display_name: catalog for catalog in catalogs}
        customer_catalog = catalogs_by_name["customers.csv"]
        product_catalog = catalogs_by_name["products.xlsx"]
        source_files_by_name = {
            source.display_name: source for source in workspace_state.source_files
        }
        self.assertEqual(
            customer_catalog.source_sha256,
            source_files_by_name["customers.csv"].sha256,
        )
        self.assertIn("Data in customers.csv", inspection_page.text)
        self.assertNotIn(
            f"<h3>{customer_catalog.tables[0].name}</h3>",
            inspection_page.text,
        )

        configured = self.client.post(
            f"/workspaces/{workspace_id}/sources/{customer_catalog.file_id}/configure",
            data={
                "csrf_token": self.csrf,
                "action": "confirm",
                "encoding": "utf-8",
                "delimiter": ",",
                "header_row_0": "1",
                "selected_0": "1",
            },
            headers=POST_HEADERS,
            follow_redirects=False,
        )
        self.assertEqual(configured.status_code, 303)
        self.assertEqual(
            configured.headers["location"],
            f"/workspaces/{workspace_id}/sources#source-{customer_catalog.file_id}",
        )
        configured_page = self.client.get(configured.headers["location"])
        self.assertIn("Confirmed customers.csv", configured_page.text)

        workbook_configured = self.client.post(
            f"/workspaces/{workspace_id}/sources/{product_catalog.file_id}/configure",
            data={
                "csrf_token": self.csrf,
                "action": "confirm",
                "encoding": "",
                "delimiter": "",
                "header_row_0": "1",
                "selected_0": "1",
            },
            headers=POST_HEADERS,
            follow_redirects=False,
        )
        self.assertEqual(workbook_configured.status_code, 303)
        self.assertEqual(
            workbook_configured.headers["location"],
            f"/workspaces/{workspace_id}/sources#source-{product_catalog.file_id}",
        )
        configured_page = self.client.get(workbook_configured.headers["location"])
        self.assertIn("Confirmed products.xlsx", configured_page.text)
        self.assertIn("Save the tables for this data version", configured_page.text)
        self.assertIn(
            f'action="/workspaces/{workspace_id}/datasets/freeze"',
            configured_page.text,
        )
        self.assertIn('name="dataset_name_0"', configured_page.text)
        self.assertIn('name="dataset_name_1"', configured_page.text)
        self.assertIn(
            "source files cannot be added or removed from this data version",
            configured_page.text,
        )

        datasets = self.client.get(
            f"/workspaces/{workspace_id}/datasets",
            follow_redirects=False,
        )
        self.assertEqual(datasets.status_code, 303)
        self.assertEqual(
            datasets.headers["location"],
            f"/workspaces/{workspace_id}/sources#table-choices",
        )
        dataset_names: dict[str, str] = {}
        for configuration in (
            self.app.state.context.queries.get_source_configurations(workspace_id)
        ):
            for _table_key in configuration.selected_table_keys:
                dataset_names[f"dataset_name_{len(dataset_names)}"] = (
                    "customers"
                    if configuration.file_id == customer_catalog.file_id
                    else "products"
                )
        frozen = self.client.post(
            f"/workspaces/{workspace_id}/datasets/freeze",
            data={
                "csrf_token": self.csrf,
                **dataset_names,
            },
            headers=POST_HEADERS,
            follow_redirects=False,
        )
        self.assertEqual(frozen.status_code, 303)
        self.assertEqual(
            frozen.headers["location"],
            f"/workspaces/{workspace_id}/datasets#tables-ready",
        )
        saved_datasets = self.client.get(frozen.headers["location"])
        self.assertIn("Saved source tables", saved_datasets.text)
        self.assertIn('id="tables-ready"', saved_datasets.text)
        self.assertIn("Tables ready for the next step", saved_datasets.text)
        self.assertIn("Choose Odoo data", saved_datasets.text)
        self.assertNotIn('name="dataset_name_0"', saved_datasets.text)
        self.assertNotIn("Save tables for this data version", saved_datasets.text)

        odoo_data = self.client.get(
            f"/workspaces/{workspace_id}/schema",
            follow_redirects=False,
        )
        self.assertEqual(odoo_data.status_code, 303)
        self.assertEqual(
            odoo_data.headers["location"],
            f"/workspaces/{workspace_id}/target",
        )
        target_page = self.client.get(odoo_data.headers["location"])
        self.assertIn("Connect the Odoo destination", target_page.text)
        self.assertIn("It does not discover models or fields", target_page.text)

        workspace_state = self.app.state.context.queries.get(workspace_id)
        tested_target = self.client.post(
            f"/workspaces/{workspace_id}/target",
            data={
                "csrf_token": self.csrf,
                "revision": str(workspace_state.revision),
                "odoo_connection_mode": "REMOTE",
                "odoo_base_url": "https://odoo.example.test",
                "odoo_database": "odoo19_target",
                "read_api_key": "super-secret-token",
                "action": "test",
            },
            headers=POST_HEADERS,
            follow_redirects=False,
        )
        self.assertEqual(tested_target.status_code, 303)
        self.assertEqual(
            self.connection_calls,
            [(workspace_id, "super-secret-token", OdooConnectionMode.REMOTE)],
        )
        workspace_state = self.app.state.context.queries.get(workspace_id)
        saved_target = self._post(
            f"/workspaces/{workspace_id}/target",
            {
                "csrf_token": self.csrf,
                "revision": str(workspace_state.revision),
                "odoo_connection_mode": "REMOTE",
                "odoo_base_url": "https://odoo.example.test",
                "odoo_database": "odoo19_target",
                "action": "save",
            },
        )
        self.assertEqual(
            saved_target.headers["location"],
            f"/workspaces/{workspace_id}/schema",
        )
        workspace_state = self.app.state.context.queries.get(workspace_id)
        self.assertEqual(workspace_state.status, WorkspaceStatus.REGISTERED)
        self.assertEqual(workspace_state.odoo_connection_mode, OdooConnectionMode.REMOTE)
        self.assertNotIn(
            b"super-secret-token",
            (
                self.app.state.context.workspace_states.repository.workspace_directory(workspace_id)
                / "workspace-engine.duckdb"
            ).read_bytes(),
        )

        derived_page = self.client.get(
            f"/workspaces/{workspace_id}/derived-entities"
        )
        self.assertIn("Separate combined information", derived_page.text)
        self.assertIn("Stage 1 of 6", derived_page.text)
        self.assertIn("Optional source organization", derived_page.text)
        self.assertIn("You are viewing Source data", derived_page.text)
        self.assertIn("Current data-version work:", derived_page.text)
        self.assertIn("Stage 2", derived_page.text)
        self.assertIn("Odoo data", derived_page.text)
        self.assertIn(
            "Saved rules are repeated consistently for every row",
            derived_page.text,
        )
        self.assertIn("Create two related tables", derived_page.text)
        self.assertIn("Which field groups rows together?", derived_page.text)
        self.assertIn(
            "Which field identifies each row within its group?",
            derived_page.text,
        )
        self.assertNotIn("BOMId", derived_page.text)
        self.assertNotIn("dataAreaId", derived_page.text)
        self.assertNotIn("LineNum", derived_page.text)
        self.assertIn("Show available Odoo record types", derived_page.text)
        self.assertNotIn(
            (
                f'action="/workspaces/{workspace_id}/derived-entities/'
                'lookup/preview#lookup-preview"'
            ),
            derived_page.text,
        )
        selection = (
            self.app.state.context.sources.sources.get_source_selection(workspace_id)
        )
        self.assertIsNotNone(selection)
        assert selection is not None
        datasets_by_name = {item.name: item for item in selection.datasets}
        customer_dataset = datasets_by_name["customers"]
        product_dataset = datasets_by_name["products"]
        source_choices = _source_value_choices(
            self.app.state.context,
            workspace_id,
            customer_dataset.dataset_id,
            customer_dataset.columns[0].stable_key,
        )
        self.assertEqual(source_choices, ({"value": "C001", "count": 1},))
        product_name = product_dataset.columns[1]
        product_code = product_dataset.columns[0]
        related_preview = self.client.post(
            f"/workspaces/{workspace_id}/derived-entities/related/preview",
            data={
                "csrf_token": self.csrf,
                "expected_parent_version": "",
                "source_dataset_id": product_dataset.dataset_id,
                "parent_dataset_name": "product_groups",
                "child_dataset_name": "product_rows",
                "parent_key_column_key": product_name.stable_key,
                "scope_column_key": "",
                "child_key_column_key": product_code.stable_key,
                "blank_policy": "block",
            },
            headers=POST_HEADERS,
        )
        self.assertEqual(related_preview.status_code, 200)
        self.assertIn("Review before creating", related_preview.text)
        self.assertIn("Create these separate tables", related_preview.text)
        saved_related = self.client.post(
            f"/workspaces/{workspace_id}/derived-entities/related/save",
            data={
                "csrf_token": self.csrf,
                "expected_parent_version": "",
                "source_dataset_id": product_dataset.dataset_id,
                "parent_dataset_name": "product_groups",
                "child_dataset_name": "product_rows",
                "parent_key_column_key": product_name.stable_key,
                "scope_column_key": "",
                "child_key_column_key": product_code.stable_key,
                "blank_policy": "block",
            },
            headers=POST_HEADERS,
            follow_redirects=False,
        )
        self.assertEqual(saved_related.status_code, 303)
        related_page = self.client.get(saved_related.headers["location"])
        self.assertIn(
            "Created the separate tables product_groups and product_rows",
            related_page.text,
        )
        related_plan = (
            self.app.state.context.derived_entities.derived_entities.get_derived_entity_plan(workspace_id)
        )
        self.assertIsNotNone(related_plan)
        removed_related = self.client.post(
            (
                f"/workspaces/{workspace_id}/derived-entities/"
                f"{related_plan.rules[0].rule_id}/delete"
            ),
            data={
                "csrf_token": self.csrf,
                "expected_parent_version": "1",
            },
            headers=POST_HEADERS,
            follow_redirects=False,
        )
        self.assertEqual(removed_related.status_code, 303)
        derived_rule_data = {
            "csrf_token": self.csrf,
            "expected_parent_version": "2",
            "source_binding": (
                f"{product_dataset.dataset_id}|{product_name.stable_key}"
            ),
            "output_dataset_name": "product_names",
            "target_model": "res.partner",
            "target_name_field": "name",
            "external_id_namespace": "dynamics_ax_2012",
            "parent_separator": "",
            "blank_policy": "block",
        }
        blocked_without_models = self.client.post(
            f"/workspaces/{workspace_id}/derived-entities/lookup/preview",
            data=derived_rule_data,
            headers=POST_HEADERS,
        )
        self.assertEqual(blocked_without_models.status_code, 422)
        self.assertIn(
            "Show the available Odoo record types before choosing one",
            blocked_without_models.text,
        )

        workspace_state = self.app.state.context.workspace_states.repository.get(workspace_id)
        refreshed_lookup_models = self._post(
            f"/workspaces/{workspace_id}/derived-entities/models/refresh",
            {"csrf_token": self.csrf},
        )
        self.assertEqual(refreshed_lookup_models.status_code, 303)
        self.assertEqual(
            refreshed_lookup_models.headers["location"],
            f"/workspaces/{workspace_id}/derived-entities#lookup-extraction",
        )
        self.assertEqual(
            self.model_catalog_calls,
            [(workspace_id, "super-secret-token")],
        )
        lookup_model_page = self.client.get(
            refreshed_lookup_models.headers["location"]
        )
        self.assertIn("Odoo record types are ready", lookup_model_page.text)
        self.assertIn(
            (
                f'action="/workspaces/{workspace_id}/derived-entities/'
                'lookup/preview#lookup-preview"'
            ),
            lookup_model_page.text,
        )
        self.assertIn('value="res.partner" label="Contact"', lookup_model_page.text)
        self.assertIn("Start typing an Odoo record type", lookup_model_page.text)
        self.assertNotIn('placeholder="product_categories"', lookup_model_page.text)
        self.assertNotIn("Article and Service", lookup_model_page.text)

        rejected_lookup_model = self.client.post(
            f"/workspaces/{workspace_id}/derived-entities/lookup/preview",
            data={**derived_rule_data, "target_model": "x.not.available"},
            headers=POST_HEADERS,
        )
        self.assertEqual(rejected_lookup_model.status_code, 422)
        self.assertIn(
            "Choose an existing Odoo record type from the loaded list",
            rejected_lookup_model.text,
        )
        rejected_lookup_save = self.client.post(
            f"/workspaces/{workspace_id}/derived-entities/save",
            data={**derived_rule_data, "target_model": "x.not.available"},
            headers=POST_HEADERS,
        )
        self.assertEqual(rejected_lookup_save.status_code, 422)
        self.assertIn(
            "Choose an existing Odoo record type from the loaded list",
            rejected_lookup_save.text,
        )
        lookup_preview = self.client.post(
            f"/workspaces/{workspace_id}/derived-entities/lookup/preview",
            data=derived_rule_data,
            headers=POST_HEADERS,
        )
        self.assertEqual(lookup_preview.status_code, 200)
        self.assertIn("Review before creating", lookup_preview.text)
        self.assertIn('id="lookup-preview"', lookup_preview.text)
        self.assertIn("Create this related table", lookup_preview.text)
        saved_derived = self.client.post(
            f"/workspaces/{workspace_id}/derived-entities/save",
            data=derived_rule_data,
            headers=POST_HEADERS,
            follow_redirects=False,
        )
        self.assertEqual(saved_derived.status_code, 303)
        saved_plan = (
            self.app.state.context.derived_entities.derived_entities.get_derived_entity_plan(
                workspace_id
            )
        )
        self.assertIsNotNone(saved_plan)
        saved_rule = next(
            rule
            for rule in saved_plan.rules
            if getattr(rule, "output_dataset_name", None) == "product_names"
        )
        self.assertEqual(
            saved_derived.headers["location"],
            (
                f"/workspaces/{workspace_id}/derived-entities"
                f"#lookup-rule-{saved_rule.rule_id}"
            ),
        )
        derived_preview = self.client.get(saved_derived.headers["location"])
        self.assertIn(
            f'id="lookup-rule-{saved_rule.rule_id}"',
            derived_preview.text,
        )
        self.assertIn("Created the related table product_names", derived_preview.text)
        self.assertIn("Example product", derived_preview.text)
        self.assertIn("impodo_dynamics_ax_2012.res_partner_", derived_preview.text)
        self.assertIn(
            "available when you match data beside the original rows",
            derived_preview.text,
        )
        self.assertNotIn("entity:P001", derived_preview.text)

        refreshed_models = self._post(
            f"/workspaces/{workspace_id}/schema/models/refresh",
            {"csrf_token": self.csrf},
        )
        self.assertEqual(refreshed_models.status_code, 303)
        self.assertEqual(
            self.model_catalog_calls,
            [
                (workspace_id, "super-secret-token"),
                (workspace_id, "super-secret-token"),
            ],
        )
        model_page = self.client.get(refreshed_models.headers["location"])
        self.assertIn("Stage 2 of 6", model_page.text)
        self.assertIn("Odoo data", model_page.text)
        self.assertIn('aria-current="step"', model_page.text)
        self.assertIn('aria-current="page"', model_page.text)
        self.assertIn("Choose the Odoo data you need", model_page.text)
        self.assertNotIn("Odoo areas included:", model_page.text)
        self.assertIn("Contact", model_page.text)
        self.assertIn("res.partner", model_page.text)
        self.assertIn(
            "No Odoo area was selected, so all available business records are shown.",
            model_page.text,
        )
        self.assertIn(
            f'action="/workspaces/{workspace_id}/schema"',
            model_page.text,
        )
        self.assertIn('aria-live="polite"', model_page.text)
        self.assertIn(
            'data-model-search-text="product product.template product stock"',
            model_page.text,
        )

        model_picker_script = self.client.get("/static/schema.js")
        self.assertIn("const hasQuery = Boolean(query);", model_picker_script.text)
        self.assertIn(
            "matches && (hasQuery || browseAll || choice.inFocus || selected)",
            model_picker_script.text,
        )
        self.assertIn(
            "Saving choices and loading Odoo data...",
            model_picker_script.text,
        )
        model_picker_styles = self.client.get("/static/workflow-pages.css")
        self.assertIn("label.model-choice[hidden]", model_picker_styles.text)

        rejected_scope = self.client.post(
            f"/workspaces/{workspace_id}/schema",
            data={
                "csrf_token": self.csrf,
                "revision": str(workspace_state.revision),
                "permitted_models": "x.not.available",
            },
            headers=POST_HEADERS,
        )
        self.assertEqual(rejected_scope.status_code, 422)
        self.assertIn(
            "not in the refreshed Odoo model catalogue",
            rejected_scope.text,
        )

        scope = self.client.post(
            f"/workspaces/{workspace_id}/schema",
            data={
                "csrf_token": self.csrf,
                "revision": str(workspace_state.revision),
                "permitted_models": "res.partner",
            },
            headers=POST_HEADERS,
            follow_redirects=False,
        )
        self.assertEqual(scope.status_code, 303)
        workspace_state = self.app.state.context.workspace_states.repository.get(workspace_id)
        self.assertEqual(workspace_state.intended_models, ("res.partner",))
        self.assertEqual(self.schema_calls, [(workspace_id, "super-secret-token")])
        workspace_state = self.app.state.context.workspace_states.repository.get(workspace_id)
        read_credential = get_target_credential(
            self.secrets,
            workspace_state,
            TargetCredentialRole.READ,
        )
        assert read_credential is not None
        model_catalog = (
            self.app.state.context.schema_workspace.schemas.get_odoo_model_catalog(
                workspace_id
            )
        )
        schema_catalog = (
            self.app.state.context.schema_workspace.schemas.get_odoo_schema_catalog(
                workspace_id
            )
        )
        assert model_catalog is not None
        assert schema_catalog is not None
        self.assertEqual(
            model_catalog.read_credential_binding_hash,
            read_credential.binding_hash,
        )
        self.assertEqual(
            schema_catalog.read_credential_binding_hash,
            read_credential.binding_hash,
        )
        self.assertEqual(
            model_catalog.read_principal_hash,
            "sha256:" + "1" * 64,
        )
        self.assertEqual(
            schema_catalog.read_principal_hash,
            model_catalog.read_principal_hash,
        )
        self.assertEqual(
            schema_catalog.read_context_hash,
            model_catalog.read_context_hash,
        )
        self.assertNotEqual(
            schema_catalog.read_permission_hash,
            model_catalog.read_permission_hash,
        )
        schema_page = self.client.get(scope.headers["location"])
        self.assertIn("Tell Impodo how to find existing records", schema_page.text)
        self.assertIn("How should Impodo find an existing Contact?", schema_page.text)
        self.assertNotIn("Reference (ref)", schema_page.text)
        self.assertIn("Search fields", schema_page.text)
        self.assertIn("Show fields that Odoo controls", schema_page.text)
        self.assertIn("Impodo found no single safe recommendation", schema_page.text)
        self.assertIn("Reference", schema_page.text)
        self.assertIn("Support options for combined matching", schema_page.text)
        governed = self.client.post(
            f"/workspaces/{workspace_id}/schema/govern",
            data={
                "csrf_token": self.csrf,
                "primary_key_field_0": "ref",
                "primary_scope_field_0": "",
                "key_fields_0": "",
                "scope_fields_0": "",
                "key_description_0": "Unique contact reference",
            },
            headers=POST_HEADERS,
            follow_redirects=False,
        )
        self.assertEqual(governed.status_code, 303)
        mapping_page = self.client.get(governed.headers["location"])
        self.assertIn("Stage 3 of 6", mapping_page.text)
        self.assertIn("Match data", mapping_page.text)
        self.assertIn('aria-current="step"', mapping_page.text)
        self.assertIn('aria-current="page"', mapping_page.text)
        self.assertIn("Match your data to Odoo", mapping_page.text)
        self.assertIn('<details class="technical-evidence">', mapping_page.text)
        self.assertNotIn("Evidence binding", mapping_page.text)
        self.assertIn("Which column uniquely identifies each row?", mapping_page.text)
        self.assertIn("How should Impodo find the same record in Odoo?", mapping_page.text)
        self.assertIn('class="identity-pair"', mapping_page.text)
        self.assertIn("Use existing Odoo records only", mapping_page.text)
        self.assertIn("Source value, or backup when blank", mapping_page.text)
        self.assertNotIn("Let Odoo choose", mapping_page.text)
        self.assertIn("Choose what goes into each Odoo field", mapping_page.text)
        self.assertNotIn("Scalar target fields", mapping_page.text)
        self.assertIn("Find a field", mapping_page.text)
        self.assertIn("For example: Sales Price or Barcode", mapping_page.text)
        self.assertNotIn("list_price", mapping_page.text)
        self.assertIn(
            "Choose where each Odoo field gets its value",
            mapping_page.text,
        )
        self.assertIn("Connect values to existing Odoo lists", mapping_page.text)
        self.assertIn("such as a product category", mapping_page.text)
        self.assertIn("Find an Odoo list or linked field", mapping_page.text)
        self.assertIn("data-relation-pagination", mapping_page.text)
        self.assertIn("data-scalar-pagination", mapping_page.text)
        self.assertIn("data-scalar-table-scroll-top", mapping_page.text)
        self.assertIn(
            'aria-label="Scroll scalar target fields horizontally"',
            mapping_page.text,
        )
        self.assertIn("data-scalar-table-scroll", mapping_page.text)
        self.assertIn("Preview", mapping_page.text)
        self.assertIn("Prepare and check values", mapping_page.text)
        self.assertIn("Must be exactly", mapping_page.text)
        self.assertIn("The first characters", mapping_page.text)
        self.assertIn("Add cleanup step", mapping_page.text)
        self.assertIn("Remove separators between numbers", mapping_page.text)
        self.assertIn("data-text-step-storage", mapping_page.text)
        self.assertIn("Advanced: custom pattern", mapping_page.text)
        self.assertIn("Advanced: formula or custom calculation", mapping_page.text)
        self.assertIn("Safe formulas only", mapping_page.text)
        self.assertIn('/static/mapping.css', mapping_page.text)
        self.assertIn('/static/mapping-save-recovery.js', mapping_page.text)
        self.assertIn('/static/mapping-editor.js', mapping_page.text)
        self.assertIn('/static/mapping-value-rules.js', mapping_page.text)
        self.assertIn('/static/mapping-catalogs.js', mapping_page.text)
        self.assertIn('/static/mapping.js', mapping_page.text)

        mapping_script = "\n".join(
            self.client.get(asset).text
            for asset in (
                "/static/mapping-save-recovery.js",
                "/static/mapping-editor.js",
                "/static/mapping-value-rules.js",
                "/static/mapping-catalogs.js",
            )
        )
        normalization_script = self.client.get("/static/normalization.js").text
        source_workflow_script = self.client.get("/static/source-workflow.js").text
        mapping_position_script = self.client.get("/static/mapping.js")
        self.assertIn("updateScalarTableScroll", mapping_script)
        self.assertIn(
            "new ResizeObserver(updateScalarTableScroll)",
            mapping_script,
        )
        self.assertIn('window.addEventListener("beforeunload"', mapping_script)
        self.assertIn("impodoMappingPosition?.remember", mapping_script)
        self.assertIn("rememberInteraction", mapping_position_script.text)
        self.assertIn("visibleRow", mapping_position_script.text)
        self.assertIn("const restore = ()", mapping_position_script.text)
        self.assertIn("window.sessionStorage", mapping_position_script.text)
        self.assertIn(
            'mappingForm.addEventListener("pointerdown"',
            mapping_script,
        )
        self.assertIn("preventScroll: true", mapping_position_script.text)
        self.assertIn("rememberNormalizationPosition", normalization_script)
        self.assertIn("restoreNormalizationPosition", normalization_script)
        self.assertIn("data-normalization-reject-form", normalization_script)
        self.assertIn("normalizationApproveDialog", normalization_script)
        self.assertIn("normalizationRejectDialog", normalization_script)
        self.assertIn("rememberSourceReviewPosition", source_workflow_script)
        self.assertIn("restoreSourceReviewPosition", source_workflow_script)
        self.assertIn("data-source-review-form", source_workflow_script)
        self.assertIn("datasetNameViolations", source_workflow_script)
        self.assertIn("Give each table a different name", source_workflow_script)
        self.assertIn("scheduleScalarCatalogSearch", mapping_script)
        self.assertIn("catalogRequestUrl", mapping_script)
        self.assertIn("relationRequestUrl", mapping_script)
        self.assertIn("new AbortController()", mapping_script)
        self.assertIn('searchParams.set("editor_id"', mapping_script)
        self.assertIn('searchParams.set("generation"', mapping_script)
        self.assertIn("response.status === 204", mapping_script)
        self.assertIn("requestGeneration !== scalarSearchGeneration", mapping_script)
        self.assertIn("requestGeneration !== relationSearchGeneration", mapping_script)
        self.assertIn("new DOMParser()", mapping_script)
        self.assertIn("window.history.replaceState", mapping_script)
        self.assertIn("restoreScalarRow(row)", mapping_script)
        self.assertIn("restoreRelationRow(row)", mapping_script)
        self.assertIn(
            "is ordinary text here.",
            mapping_script,
        )
        self.assertIn("internationalPhoneTextSteps", mapping_script)
        self.assertIn('characters: " .-/"', mapping_script)
        self.assertIn("scheduleRelationCatalogSearch", mapping_script)
        self.assertIn("relationDraftRows", mapping_script)
        self.assertNotIn("pendingRedirect", mapping_script)
        self.assertNotIn(
            "mappingForm.requestSubmit(saveProgress)",
            mapping_script,
        )
        self.assertIn(
            "Searching Odoo fields",
            mapping_script,
        )
        self.assertIn(
            'mappingForm.getAttribute("action")',
            mapping_script,
        )
        self.assertNotIn("fetch(mappingForm.action", mapping_script)
        self.assertIn(
            "updateMappingVersionFields(payload)",
            mapping_script,
        )
        self.assertIn("workingVersionUpdated", mapping_script)
        self.assertIn('if (action === "save_progress")', mapping_script)
        self.assertIn("navigateToMappingResult", mapping_script)
        self.assertIn(
            "Your unsaved changes are still on this page",
            mapping_script,
        )
        self.assertIn(
            "Your checked matches remain unchanged on this page",
            mapping_script,
        )
        self.assertIn("hydrateSourceOptions", mapping_script)
        self.assertIn("option.defaultSelected = selected", mapping_script)
        mapping_styles = self.client.get("/static/mapping.css")
        shared_styles = self.client.get("/static/workflow-pages.css")
        self.assertIn(".scalar-table-scroll-top", mapping_styles.text)
        self.assertIn("overflow-x: scroll", mapping_styles.text)
        self.assertIn(".mapping-save-state.unsaved", mapping_styles.text)
        self.assertIn(".source-table-summary", shared_styles.text)
        self.assertIn(".source-table-title", shared_styles.text)

        selection = (
            self.app.state.context.sources.sources.get_source_selection(workspace_id)
        )
        schema_governance = (
            self.app.state.context.schema_workspace.schemas.get_schema_governance(workspace_id)
        )
        self.assertIsNotNone(selection)
        self.assertIsNotNone(schema_governance)
        assert selection is not None
        physical_datasets_by_name = {item.name: item for item in selection.datasets}
        customer = physical_datasets_by_name["customers"]
        product = physical_datasets_by_name["products"]
        customer_code, customer_name = customer.columns
        product_code, product_name = product.columns
        mapping_selection = (
            self.app.state.context.sources.sources.get_mapping_source_selection(
                workspace_id
            )
        )
        self.assertIsNotNone(mapping_selection)
        mapping_datasets = {
            item.name: item for item in mapping_selection.datasets
        }
        mapped_customer = mapping_datasets["customers"]
        product_names = mapping_datasets["product_names"]
        mapped_product = mapping_datasets["products"]
        product_name_key, product_name_value = product_names.columns
        dataset_indices = {
            item.dataset_id: index
            for index, item in enumerate(mapping_selection.datasets)
        }
        customer_index = dataset_indices[mapped_customer.dataset_id]
        product_names_index = dataset_indices[product_names.dataset_id]
        product_index = dataset_indices[mapped_product.dataset_id]
        self.assertEqual(mapped_customer.dataset_id, customer.dataset_id)
        self.assertEqual(mapped_product.dataset_id, product.dataset_id)
        business_key_id = schema_governance.business_keys[0].key_id
        mapping_filter_query = (
            "scalar_page=1&field_query=nam&"
            f"mapping_dataset={customer_index}&relation_page=1"
        )
        saved_progress = self.client.post(
            f"/workspaces/{workspace_id}/mapping/save?{mapping_filter_query}",
            data={
                "csrf_token": self.csrf,
                "action": "save_progress",
                "expected_parent_version": "",
                "expected_working_draft_version": "",
                f"target_model_{customer_index}": "res.partner",
                f"mode_{customer_index}": "upsert",
                f"scalar_value_source_{customer_index}_1": "source",
                f"scalar_type_{customer_index}_1": "string",
                f"scalar_case_{customer_index}_1": "preserve",
                f"scalar_formula_{customer_index}_1": (
                    'coalesce(value, "Unnamed contact")'
                ),
                f"scalar_compare_{customer_index}_1": "1",
                f"scalar_null_{customer_index}_1": "distinct",
                f"target_model_{product_names_index}": "res.partner",
                f"mode_{product_names_index}": "upsert",
                f"target_model_{product_index}": "res.partner",
                f"mode_{product_index}": "upsert",
            },
            headers=POST_HEADERS,
            follow_redirects=False,
        )
        self.assertEqual(saved_progress.status_code, 303)
        self.assertEqual(
            saved_progress.headers["location"],
            (
                f"/workspaces/{workspace_id}/mapping?{mapping_filter_query}"
                f"#mapping-dataset-{customer_index}"
            ),
        )
        saved_progress_page = self.client.get(
            saved_progress.headers["location"]
        )
        self.assertIn(
            f'id="mapping-dataset-{customer_index}"',
            saved_progress_page.text,
        )
        self.assertIn("data-mapping-dataset", saved_progress_page.text)
        self.assertIn(
            "Progress saved. Check matches when ready.",
            saved_progress_page.text,
        )
        saved_draft = (
            self.app.state.context.mapping_workspace.mappings.get_mapping_working_draft(
                workspace_id
            )
        )
        current_mapping_selection = (
            self.app.state.context.queries.get_mapping_source_selection(workspace_id)
        )
        self.assertIsNotNone(saved_draft)
        self.assertIsNotNone(current_mapping_selection)
        self.assertEqual(
            saved_draft.definition.source_selection_hash,
            current_mapping_selection.content_hash,
        )
        current_governance = (
            self.app.state.context.queries.get_schema_governance(workspace_id)
        )
        self.assertIsNotNone(current_governance)
        self.assertEqual(
            saved_draft.definition.schema_hash,
            current_governance.content_hash,
        )
        self.assertIn("Saved changes need checking", saved_progress_page.text)
        self.assertIn("Your saved work is loaded", saved_progress_page.text)
        working_draft = (
            self.app.state.context.mapping_workspace.mappings.get_mapping_working_draft(
                workspace_id
            )
        )
        self.assertEqual(working_draft.version, 1)
        working_by_dataset = {
            item.dataset_id: item
            for item in working_draft.definition.datasets
        }
        self.assertEqual(
            working_by_dataset[customer.dataset_id].fields[0].source_column_key,
            "",
        )
        self.assertIsNone(
            self.app.state.context.mapping_workspace.mappings.get_mapping_revision(workspace_id)
        )
        mapping_data = {
                "csrf_token": self.csrf,
                f"target_model_{customer_index}": "res.partner",
                f"mode_{customer_index}": "upsert",
                f"source_identity_{customer_index}": customer_code.stable_key,
                f"business_key_{customer_index}": business_key_id,
                f"identity_source_{customer_index}_0": customer_code.stable_key,
                f"scalar_value_source_{customer_index}_1": "source_with_fallback",
                f"scalar_source_{customer_index}_1": customer_name.stable_key,
                f"scalar_literal_{customer_index}_1": "Unnamed contact",
                f"scalar_type_{customer_index}_1": "string",
                f"scalar_trim_{customer_index}_1": "1",
                f"scalar_collapse_{customer_index}_1": "1",
                f"scalar_empty_null_{customer_index}_1": "1",
                f"scalar_case_{customer_index}_1": "preserve",
                f"scalar_formula_{customer_index}_1": (
                    'coalesce(value, "Unnamed contact")'
                ),
                f"scalar_compare_{customer_index}_1": "1",
                f"scalar_null_{customer_index}_1": "distinct",
                f"target_model_{product_names_index}": "res.partner",
                f"mode_{product_names_index}": "upsert",
                f"source_identity_{product_names_index}": product_name_key.stable_key,
                f"business_key_{product_names_index}": business_key_id,
                f"identity_source_{product_names_index}_0": product_name_value.stable_key,
                f"scalar_value_source_{product_names_index}_1": "source",
                f"scalar_source_{product_names_index}_1": product_name_value.stable_key,
                f"scalar_type_{product_names_index}_1": "string",
                f"scalar_compare_{product_names_index}_1": "1",
                f"scalar_null_{product_names_index}_1": "distinct",
                f"target_model_{product_index}": "res.partner",
                f"mode_{product_index}": "upsert",
                f"source_identity_{product_index}": product_code.stable_key,
                f"business_key_{product_index}": business_key_id,
                f"identity_source_{product_index}_0": product_code.stable_key,
                f"scalar_value_source_{product_index}_1": "constant",
                f"scalar_literal_{product_index}_1": "Imported product",
                f"scalar_type_{product_index}_1": "string",
                f"scalar_case_{product_index}_1": "sentence",
                f"scalar_text_steps_{product_index}_1": (
                    '[{"kind":"find_replace","search_value":"Imported",'
                    '"replacement_value":"imported","search_mode":"literal",'
                    '"replace_all":true,"characters":""}]'
                ),
                f"scalar_exact_length_{product_index}_1": "16",
                f"scalar_segment_location_{product_index}_1": "first",
                f"scalar_segment_length_{product_index}_1": "1",
                f"scalar_character_class_{product_index}_1": "uppercase",
                f"scalar_pattern_{product_index}_1": "[A-Z][a-z ]{15}",
                f"scalar_compare_{product_index}_1": "1",
                f"scalar_null_{product_index}_1": "distinct",
        }
        checked = self.client.post(
            f"/workspaces/{workspace_id}/mapping/save",
            data={
                **mapping_data,
                "action": "draft",
                "expected_parent_version": "",
                "expected_working_draft_version": "1",
            },
            headers=POST_HEADERS,
            follow_redirects=False,
        )
        self.assertEqual(checked.status_code, 303)
        checked_page = self.client.get(checked.headers["location"])
        self.assertIn("Matches checked and ready to confirm", checked_page.text)
        self.assertIn("Ready to confirm", checked_page.text)
        checked_draft = (
            self.app.state.context.mapping_workspace.mappings.get_mapping_working_draft(
                workspace_id
            )
        )
        self.assertEqual(checked_draft.version, 2)
        checked_revision = (
            self.app.state.context.mapping_workspace.mappings.get_mapping_revision(
                workspace_id
            )
        )
        self.assertEqual(checked_revision.version, 1)

        submitted = self.client.post(
            f"/workspaces/{workspace_id}/mapping/save",
            data={
                **mapping_data,
                "action": "submit",
                "expected_parent_version": "1",
                "expected_working_draft_version": "2",
            },
            headers=POST_HEADERS,
            follow_redirects=False,
        )
        self.assertEqual(submitted.status_code, 303)
        self.assertEqual(
            submitted.headers["location"],
            f"/workspaces/{workspace_id}/prepare",
        )
        submitted_page = self.client.get(submitted.headers["location"])
        self.assertIn("Field matches confirmed", submitted_page.text)
        self.assertIn("Prepare all source rows", submitted_page.text)
        confirmed_mapping_page = self.client.get(
            f"/workspaces/{workspace_id}/mapping"
        )
        self.assertIn("Field matches are confirmed", confirmed_mapping_page.text)
        self.assertIn("Continue to Prepare data", confirmed_mapping_page.text)
        self.assertRegex(
            confirmed_mapping_page.text,
            rf'class="button secondary" href="/workspaces/{workspace_id}/prepare"',
        )
        revision = (
            self.app.state.context.mapping_workspace.mappings.get_mapping_revision(workspace_id)
        )
        self.assertEqual(revision.version, 1)
        self.assertEqual(
            [
                item.version
                for item in self.app.state.context.mapping_workspace.mappings.list_mapping_revisions(
                    workspace_id
                )
            ],
            [1],
        )
        revision_by_dataset = {
            item.dataset_id: item for item in revision.definition.datasets
        }
        self.assertEqual(
            revision_by_dataset[
                customer.dataset_id
            ].fields[0].value_source.value,
            "source_with_fallback",
        )
        self.assertTrue(
            revision_by_dataset[customer.dataset_id].fields[0].transform.trim
        )
        self.assertEqual(
            revision_by_dataset[customer.dataset_id].fields[0].transform.formula,
            'coalesce(value, "Unnamed contact")',
        )
        self.assertEqual(
            revision_by_dataset[
                product.dataset_id
            ].fields[0].value_source.value,
            "constant",
        )
        self.assertEqual(
            revision_by_dataset[product.dataset_id].fields[0].literal_value,
            "Imported product",
        )
        product_field = revision_by_dataset[product.dataset_id].fields[0]
        self.assertEqual(product_field.transform.case_mode, "sentence")
        self.assertEqual(
            product_field.transform.text_steps[0].search_value,
            "Imported",
        )
        self.assertEqual(product_field.validation.exact_length, 16)
        self.assertEqual(product_field.validation.segment_location, "first")
        self.assertEqual(product_field.validation.character_class, "uppercase")

        impact_link = (
            f"/workspaces/{workspace_id}/mapping/transformation-impact"
        )
        self.assertIn("Review rule effects (optional)", confirmed_mapping_page.text)
        impact_page = self.client.get(impact_link)
        self.assertEqual(impact_page.status_code, 200)
        self.assertIn("Review rule effects", impact_page.text)
        self.assertIn("Stage 3 of 6", impact_page.text)
        self.assertIn("Optional rule review", impact_page.text)
        self.assertIn('aria-current="step"', impact_page.text)
        self.assertIn('aria-current="page"', impact_page.text)
        self.assertIn("This review is optional", impact_page.text)
        self.assertIn("Prepare preview", impact_page.text)
        self.assertNotIn("What each rule did", impact_page.text)
        self.assertIn("your checked field rules", impact_page.text)
        self.assertNotIn("data-impact-row", impact_page.text)
        prepared = self.client.post(
            f"{impact_link}/prepare",
            data={"csrf_token": self.csrf},
            headers=POST_HEADERS,
            follow_redirects=False,
        )
        self.assertEqual(prepared.status_code, 303)
        impact_page = self.client.get(prepared.headers["location"])
        self.assertIn("Original", impact_page.text)
        self.assertIn("Prepared", impact_page.text)
        self.assertIn("All Odoo fields", impact_page.text)
        self.assertIn("Download matching rows (.csv)", impact_page.text)
        self.assertIn("Download all affected rows (.csv)", impact_page.text)
        self.assertIn("Your registered Excel or CSV source remains unchanged", impact_page.text)
        self.assertIn("Showing 1", impact_page.text)
        self.assertNotIn("data-impact-row", impact_page.text)
        impact_csv = self.client.post(
            f"{impact_link}.csv",
            data={"csrf_token": self.csrf},
            headers=POST_HEADERS,
        )
        self.assertEqual(impact_csv.status_code, 200)
        self.assertIn("text/csv", impact_csv.headers["content-type"])
        self.assertIn("Raw source", impact_csv.text)
        self.assertIn("Proposed value", impact_csv.text)
        impact_script = self.client.get("/static/transformation-impact.js")
        self.assertNotIn("data-impact-export", impact_script.text)

        prepare_page = self.client.get(f"/workspaces/{workspace_id}/prepare")
        self.assertEqual(prepare_page.status_code, 200)
        self.assertIn("Stage 4 of 6", prepare_page.text)
        self.assertIn("Prepare data", prepare_page.text)
        self.assertIn("Prepare all source rows", prepare_page.text)
        self.assertIn(
            "Impodo prepares from the accepted Data version selected by this workspace",
            prepare_page.text,
        )
        self.assertIn(
            f'action="/workspaces/{workspace_id}/summary/check"',
            prepare_page.text,
        )
        self.assertIn('aria-current="step"', prepare_page.text)
        self.assertIn('aria-current="page"', prepare_page.text)

        summary = self.client.get(f"/workspaces/{workspace_id}/summary")
        self.assertIn("Prepare and review data", summary.text)
        self.assertIn("Uses Impodo’s stored local copy", summary.text)
        checked = self.client.post(
            f"/workspaces/{workspace_id}/summary/check",
            data={"csrf_token": self.csrf},
            headers=POST_HEADERS,
            follow_redirects=False,
        )
        self.assertEqual(checked.status_code, 303)
        self.assertIn("/preparation/", checked.headers["location"])
        progress_page = self.client.get(checked.headers["location"])
        self.assertIn("Stage 4 of 6", progress_page.text)
        self.assertIn("Prepare data", progress_page.text)
        self.assertIn(
            "Impodo is preparing from its stored local copy",
            progress_page.text,
        )
        self.assertIn('aria-current="step"', progress_page.text)
        self.assertIn('aria-current="page"', progress_page.text)
        completed_job = _wait_for_preparation(
            self.client,
            checked.headers["location"],
        )
        self.assertEqual(completed_job["status"], "SUCCEEDED", completed_job)
        manager = self.app.state.context.preparation_jobs
        assert manager is not None
        worker_deadline = time.monotonic() + 2.0
        while (
            manager.worker_alive(str(completed_job["job_id"]))
            and time.monotonic() < worker_deadline
        ):
            time.sleep(0.01)
        self.assertFalse(manager.worker_alive(str(completed_job["job_id"])))
        review_page = self.client.get(str(completed_job["redirect_url"]))
        self.assertIn("Review what Impodo prepared", review_page.text)
        self.assertIn("Nothing is sent to Odoo", review_page.text)
        self.assertIn("data-normalization-review", review_page.text)
        self.assertIn("Approve all prepared data", review_page.text)
        self.assertIn("Send back to fix", review_page.text)
        self.assertNotIn("Accept this change", review_page.text)
        self.assertIn("data-normalization-approve-dialog", review_page.text)
        self.assertIn("data-normalization-reject-dialog", review_page.text)
        self.assertIn("data-normalization-table-scroll", review_page.text)
        self.assertEqual(len(self.readiness_calls), 0)

        context = self.app.state.context
        quality_summary = context.quality.current_summary(workspace_id)
        assert quality_summary is not None
        quality_page = context.queries.get_quality_review_page(
            workspace_id,
            quality_summary.run_id,
            status="",
            dataset="",
            page=1,
            page_size=20,
        )
        self.assertLessEqual(len(quality_page.items), 20)
        prepared_summary_page = self.client.get(
            f"/workspaces/{workspace_id}/summary"
        )
        self.assertIn(
            f"Records 1-{min(20, quality_page.matching_count)} "
            f"of {quality_page.matching_count}",
            prepared_summary_page.text,
        )
        if quality_page.matching_count > 10:
            self.assertIn("Records per page:", prepared_summary_page.text)

        normalization_service = self.app.state.context.normalization
        review = normalization_service.current_review(workspace_id)
        assert review is not None
        normalization, evaluation, dry_run = review
        decision_group = next(
            group for group in evaluation.groups if group.requires_decision
        )
        rejected = self.client.post(
            f"/workspaces/{workspace_id}/normalization/groups/"
            f"{decision_group.group_id}/reject?status=pending&page=2",
            data={
                "csrf_token": self.csrf,
                "run_id": normalization.run_id,
                "lifecycle_version": str(normalization.lifecycle_version),
                "reason": "The prepared value needs another review.",
            },
            headers=POST_HEADERS,
            follow_redirects=False,
        )
        self.assertEqual(rejected.status_code, 303)
        blocked_page = self.client.get(rejected.headers["location"])
        self.assertIn("Fix the change that was sent back", blocked_page.text)
        self.assertIn("The prepared value needs another review", blocked_page.text)
        self.assertIn("Reopen review", blocked_page.text)
        self.assertNotIn("Accept this change", blocked_page.text)
        normalization = normalization_service.current_summary(workspace_id)
        assert normalization is not None
        reopened = self.client.post(
            f"/workspaces/{workspace_id}/normalization/reopen",
            data={
                "csrf_token": self.csrf,
                "run_id": normalization.run_id,
                "lifecycle_version": str(normalization.lifecycle_version),
            },
            headers=POST_HEADERS,
            follow_redirects=False,
        )
        self.assertEqual(reopened.status_code, 303)
        self.assertEqual(
            reopened.headers["location"],
            f"/workspaces/{workspace_id}/normalization?status=pending#review-groups",
        )
        normalization = normalization_service.current_summary(workspace_id)
        assert normalization is not None
        approved = self.client.post(
            f"/workspaces/{workspace_id}/normalization/approve",
            data={
                "csrf_token": self.csrf,
                "run_id": normalization.run_id,
                "lifecycle_version": str(normalization.lifecycle_version),
            },
            headers=POST_HEADERS,
            follow_redirects=False,
        )
        self.assertEqual(approved.status_code, 303)
        normalization = normalization_service.current_summary(workspace_id)
        assert normalization is not None
        self.assertTrue(normalization.frozen)
        frozen_review = normalization_service.current_review(workspace_id)
        assert frozen_review is not None
        self.assertEqual(
            frozen_review[2].approved_groups,
            frozen_review[2].summary.required_group_keys,
        )
        workspace = self.app.state.context.migration_workspaces.get(
            workspace_id,
            actor=self.app.state.context.actor,
        )
        package = (
            self.app.state.context.data_version_source_projection.packages.repository
            .get_source_package(workspace.data_version_id)
        )
        assert package is not None
        source_artifact = (
            Path(self.temporary.name)
            / "artifacts"
            / "dv"
            / workspace.data_version_id
            / "inbox"
            / package.files[0].storage_key
        )
        source_artifact.unlink()
        compared = self.client.post(
            f"/workspaces/{workspace_id}/summary/compare",
            data={"csrf_token": self.csrf},
            headers=POST_HEADERS,
            follow_redirects=False,
        )
        self.assertEqual(compared.status_code, 303, compared.text)
        readiness_page = self.client.get(compared.headers["location"])
        self.assertIn("Included in preparation", readiness_page.text)
        self.assertIn("Set aside", readiness_page.text)
        self.assertIn("Needs correction", readiness_page.text)
        self.assertIn("New in Odoo", readiness_page.text)
        self.assertIn("Different from Odoo", readiness_page.text)
        self.assertIn("Already matches", readiness_page.text)
        self.assertIn("Needs attention", readiness_page.text)
        self.assertIn('id="quality-rows"', readiness_page.text)
        self.assertIn("Ready", readiness_page.text)
        self.assertIn("Needs a decision", readiness_page.text)
        self.assertIn("Needs correction", readiness_page.text)
        self.assertIn("Rows", readiness_page.text)
        self.assertIn("Support details", readiness_page.text)
        self.assertIn("Create review workbook", readiness_page.text)
        self.assertIn("Keep these rules for another data version", readiness_page.text)
        self.assertIn(
            "Your submitted preparation, matching, relationship, and checking "
            "rules are already saved in this workspace.",
            readiness_page.text,
        )
        self.assertIn("Save these rules as a Recipe", readiness_page.text)
        self.assertIn(
            f'href="/projects/{workspace.project_id}#project-recipes-title"',
            readiness_page.text,
        )
        self.assertIn(
            "The review workbook documents this data check. "
            "It does not save the rules for reuse.",
            readiness_page.text,
        )
        self.assertIn("Odoo remains unchanged", readiness_page.text)
        self.assertIn("prepared rows safely saved", readiness_page.text)
        self.assertIn("data-staging-summary", readiness_page.text)
        self.assertIn("<summary>Support details</summary>", readiness_page.text)
        self.assertIn("data-preflight-compare", readiness_page.text)
        self.assertIn(
            "Comparing with Odoo... Keep this page open.",
            readiness_page.text,
        )

        report = self.app.state.context.preflight.current_report(workspace_id)
        assert report is not None
        self.assertEqual(
            report.create_count
            + report.update_count
            + report.unchanged_count
            + report.ambiguous_count
            + report.blocked_count,
            report.total_count,
        )
        staging = self.app.state.context.preflight.current_staging(workspace_id)
        assert staging is not None
        self.assertEqual(report.staging_run_id, staging.run_id)
        self.assertEqual(report.staging_content_hash, staging.content_hash)
        restored_staging = (
            self.app.state.context.preflight.staging.get_canonical_staging_run(
                workspace_id,
                staging.run_id,
            )
        )
        self.assertIsNotNone(restored_staging)
        self.assertEqual(
            restored_staging.content_hash,
            staging.content_hash,
        )
        restart_app = create_local_app(
            self.temporary.name,
            secret_store=self.secrets,
            readiness_reader=lambda *_args: self.fail(
                "Restart retrieval must not contact Odoo"
            ),
        )
        restarted_report = restart_app.state.context.preflight.current_report(
            workspace_id
        )
        self.assertIsNotNone(restarted_report)
        assert restarted_report is not None
        self.assertEqual(restarted_report.run_id, report.run_id)

        sample_row = self.app.state.context.preflight.readiness_rows(
            workspace_id,
            report.run_id,
        ).items[0]
        self.assertIn(
            sample_row.source_trace_id,
            {item.row_id for item in restored_staging.rows},
        )

        database_path = (
            self.app.state.context.workspace_states.repository.workspace_directory(workspace_id)
            / "workspace-engine.duckdb"
        )
        staging_repository = self.app.state.context.preflight.staging
        with staging_repository._connect(database_path) as connection:
            stored_row = connection.execute(
                """
                SELECT ordinal, row_json
                  FROM canonical_staging_row
                 WHERE run_id = ?
                 ORDER BY ordinal
                 LIMIT 1
                """,
                [staging.run_id],
            ).fetchone()
            assert stored_row is not None
            tampered_payload = json.loads(str(stored_row[1]))
            tampered_payload["target_model"] = "x.tampered"
            connection.execute(
                """
                UPDATE canonical_staging_row
                   SET row_json = ?
                 WHERE run_id = ? AND ordinal = ?
                """,
                [
                    canonical_json_text(tampered_payload),
                    staging.run_id,
                    int(stored_row[0]),
                ],
            )
        try:
            rejected_tamper = self.client.post(
                f"/workspaces/{workspace_id}/summary/compare",
                data={"csrf_token": self.csrf},
                headers=POST_HEADERS,
                follow_redirects=False,
            )
        finally:
            with staging_repository._connect(database_path) as connection:
                connection.execute(
                    """
                    UPDATE canonical_staging_row
                       SET row_json = ?
                     WHERE run_id = ? AND ordinal = ?
                    """,
                    [str(stored_row[1]), staging.run_id, int(stored_row[0])],
                )
        self.assertEqual(rejected_tamper.status_code, 422)
        self.assertEqual(len(self.readiness_calls), 1)

        compared_again = self.client.post(
            f"/workspaces/{workspace_id}/summary/compare",
            data={"csrf_token": self.csrf},
            headers=POST_HEADERS,
            follow_redirects=False,
        )
        self.assertEqual(compared_again.status_code, 303, compared_again.text)
        repeated_report = self.app.state.context.preflight.current_report(workspace_id)
        assert repeated_report is not None
        self.assertNotEqual(repeated_report.run_id, report.run_id)
        self.assertEqual(repeated_report.staging_run_id, report.staging_run_id)
        self.assertEqual(repeated_report.quality_run_id, report.quality_run_id)
        self.assertEqual(
            repeated_report.normalization_run_id,
            report.normalization_run_id,
        )
        with staging_repository._connect(database_path) as connection:
            run_count = connection.execute(
                "SELECT COUNT(*) FROM readiness_run"
            ).fetchone()
            current_run = connection.execute(
                "SELECT run_id FROM preflight_current WHERE singleton_id = 1"
            ).fetchone()
            superseded = connection.execute(
                """
                SELECT detail
                  FROM preflight_transition
                 WHERE run_id = ? AND event_type = 'SUPERSEDED'
                """,
                [report.run_id],
            ).fetchone()
        self.assertEqual(run_count, (2,))
        self.assertEqual(current_run, (repeated_report.run_id,))
        self.assertEqual(superseded, (repeated_report.run_id,))
        current_normalization = normalization_service.current_summary(workspace_id)
        assert current_normalization is not None
        self.assertEqual(
            current_normalization.run_id,
            report.normalization_run_id,
        )

        load_preview = self.app.state.context.execution.current_preview(workspace_id)
        assert load_preview is not None
        final_review = self.client.get(f"/workspaces/{workspace_id}/summary")
        self.assertIn("Final review complete", final_review.text)
        self.assertNotIn("Preview the Odoo load", final_review.text)
        self.assertLess(
            final_review.text.index("Checked rows"),
            final_review.text.index("Final review complete"),
        )
        load_landing = self.client.get(
            f"/workspaces/{workspace_id}/load",
            follow_redirects=False,
        )
        self.assertEqual(load_landing.status_code, 303)
        self.assertTrue(load_landing.headers["location"].endswith("/load/review"))
        load_page = self.client.get(load_landing.headers["location"])
        self.assertEqual(load_page.status_code, 200)
        self.assertIn("Check what will change in Odoo", load_page.text)
        self.assertIn("Nothing is written from this page", load_page.text)
        self.assertIn("Reviewed captured Odoo fields only", load_page.text)
        self.assertIn("Continue to confirmation", load_page.text)
        self.assertNotIn('name="write_api_key"', load_page.text)
        outcome_before_load = self.client.get(
            f"/workspaces/{workspace_id}/load/outcome",
            follow_redirects=False,
        )
        self.assertEqual(outcome_before_load.status_code, 303)
        self.assertTrue(
            outcome_before_load.headers["location"].endswith("/load/review")
        )
        confirmation_page = self.client.get(
            f"/workspaces/{workspace_id}/load/confirm"
        )
        self.assertEqual(confirmation_page.status_code, 200)
        self.assertIn("Confirm the Odoo load", confirmation_page.text)
        self.assertIn("bounded load group", confirmation_page.text)
        self.assertIn("in the order shown", confirmation_page.text)
        self.assertIn("will not retry blindly", confirmation_page.text)
        self.assertIn("Advanced settings", confirmation_page.text)
        self.assertIn('name="write_api_key"', confirmation_page.text)
        self.assertIn('name="remember_write_api_key"', confirmation_page.text)
        self.assertIn("checking-only key is not reused here", confirmation_page.text)

        class FakeWriteExecutor:
            target_hash = load_preview.snapshot.target_hash
            scope_hash = load_preview.api_scope.semantic_hash

            def __init__(self):
                self.created = []
                self.updated = []
                self.records = {}
                self.next_id = 100

            def find_ids(self, model, domain):
                del model, domain
                return (42,)

            def find_ids_many(self, model, domains):
                del model
                return tuple((42,) for _domain in domains)

            def create_rows(self, model, values):
                rows = tuple(dict(item) for item in values)
                self.created.append((model, rows))
                identifiers = tuple(
                    range(self.next_id, self.next_id + len(rows))
                )
                self.next_id += len(rows)
                for identifier, row in zip(identifiers, rows, strict=True):
                    self.records[(model, identifier)] = dict(row)
                return identifiers

            def load_create_rows(self, model, values, external_ids):
                del external_ids
                return self.create_rows(model, values)

            def update_row(self, model, record_id, values):
                self.updated.append((model, record_id, dict(values)))
                self.records.setdefault((model, record_id), {}).update(values)

        class FakeReadbackReader:
            target_hash = load_preview.snapshot.target_hash
            scope_hash = load_preview.api_scope.semantic_hash
            imports_external_ids = False

            def __init__(self, writer):
                self.writer = writer

            def read_ids(self, model, identifiers, fields):
                return tuple(
                    ReadbackRecord(
                        identifier,
                        {
                            field: self.writer.records[(model, identifier)][field]
                            for field in fields
                        },
                    )
                    for identifier in identifiers
                    if (model, identifier) in self.writer.records
                )

            def find_records(self, model, domain, fields):
                del domain
                matches = [
                    ReadbackRecord(
                        identifier,
                        {field: values[field] for field in fields},
                    )
                    for (stored_model, identifier), values in self.writer.records.items()
                    if stored_model == model and all(field in values for field in fields)
                ]
                if matches:
                    return tuple(matches[:2])
                return (ReadbackRecord(42, {}),) if not fields else ()

            def find_records_many(self, model, lookups):
                return tuple(
                    self.find_records(model, lookup.domain, lookup.fields)
                    for lookup in lookups
                )

            def read_external_ids(self, external_ids):
                del external_ids
                return ()

        fake_writer = FakeWriteExecutor()
        write_factory_keys = []
        readback_factory_keys = []

        def write_factory(_project, api_key, _scope):
            write_factory_keys.append(api_key)
            return fake_writer

        def readback_factory(_project, api_key, _scope):
            readback_factory_keys.append(api_key)
            return FakeReadbackReader(fake_writer)

        def write_identity_probe(_project, _api_key, scope):
            return OdooWriteIdentity(
                target_hash=load_preview.snapshot.target_hash,
                principal_hash="sha256:" + "5" * 64,
                permission_hash="sha256:" + "6" * 64,
                context_hash=load_preview.snapshot.read_context_hash,
                readable_models=tuple(item.model for item in scope.models),
                writable_models=tuple(
                    item.model for item in scope.models if item.write_fields
                ),
                observed_at="2026-08-21T00:00:00Z",
            )

        self.app.state.context.write_executor_factory = write_factory
        self.app.state.context.readback_reader_factory = readback_factory
        self.app.state.context.write_identity_probe = write_identity_probe
        missing_write_key = self.client.post(
            f"/workspaces/{workspace_id}/load",
            data={
                "csrf_token": self.csrf,
                "snapshot_hash": load_preview.snapshot.semantic_hash,
                "batch_rows": "10",
            },
            headers=POST_HEADERS,
        )
        self.assertEqual(missing_write_key.status_code, 422)
        self.assertIn("Enter an Odoo API key approved for loading", missing_write_key.text)
        self.assertIn("Confirm the Odoo load", missing_write_key.text)
        self.assertEqual(write_factory_keys, [])
        self.assertEqual(readback_factory_keys, [])

        loaded = self.client.post(
            f"/workspaces/{workspace_id}/load",
            data={
                "csrf_token": self.csrf,
                "snapshot_hash": load_preview.snapshot.semantic_hash,
                "write_api_key": "load-secret",
                "batch_rows": "10",
            },
            headers=POST_HEADERS,
            follow_redirects=False,
        )
        self.assertEqual(loaded.status_code, 303, loaded.text)
        progress_url = loaded.headers["location"]
        self.assertIn("/load/progress/", progress_url)
        progress_page = self.client.get(progress_url)
        self.assertEqual(progress_page.status_code, 200, progress_page.text)
        self.assertIn("data-load-job", progress_page.text)
        self.assertIn("odoo.example.test", progress_page.text)
        self.assertIn("migration", progress_page.text)
        finished_load = _wait_for_load(self.client, progress_url)
        self.assertEqual(finished_load["status"], "SUCCEEDED", finished_load)
        self.assertEqual(
            finished_load["completed_rows"],
            repeated_report.create_count + repeated_report.update_count,
        )
        self.assertEqual(finished_load["created_count"], repeated_report.create_count)
        self.assertEqual(finished_load["updated_count"], repeated_report.update_count)
        self.assertEqual(finished_load["attention_count"], 0)
        self.assertTrue(finished_load["verification_complete"])
        self.assertNotIn("load-secret", json.dumps(finished_load))
        self.assertEqual(write_factory_keys, ["load-secret"])
        self.assertEqual(readback_factory_keys, ["load-secret"])
        outcome_page = self.client.get(finished_load["redirect_url"])
        self.assertIn("Odoo read-back complete", outcome_page.text)
        self.assertIn("Verified in Odoo", outcome_page.text)
        self.assertIn("Odoo now matches every field", outcome_page.text)
        self.assertIn("Verify result", outcome_page.text)
        self.assertEqual(
            outcome_page.text.count("data-load-row"),
            repeated_report.create_count + repeated_report.update_count,
        )
        self.assertNotIn("load-secret", outcome_page.text)

        report = repeated_report
        sample_row = self.app.state.context.preflight.readiness_rows(
            workspace_id,
            report.run_id,
        ).items[0]
        paged_rows = tuple(
            replace(
                sample_row,
                source_trace_id=f"sha256:{index:064x}",
                source_row=index,
                status="blocked" if index <= 120 else "ready",
                identity=f"ROW-{index:04d}",
            )
            for index in range(1, 202)
        )
        with staging_repository._connect(database_path) as connection:
            connection.execute(
                "DELETE FROM preflight_decision WHERE run_id = ?",
                [report.run_id],
            )
            connection.executemany(
                """
                INSERT INTO preflight_decision (
                    run_id, ordinal, source_trace_id, dataset,
                    source_row, status, decision_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    [
                        report.run_id,
                        index,
                        item.source_trace_id,
                        item.dataset,
                        item.source_row,
                        item.status,
                        canonical_json_text(asdict(item)),
                    ]
                    for index, item in enumerate(paged_rows)
                ],
            )

        with self.subTest("persisted readiness paging"):
            first_page = self.client.get(
                f"/workspaces/{workspace_id}/summary"
            )
            self.assertEqual(
                first_page.text.count("data-readiness-row"),
                20,
            )
            self.assertIn("Rows 1-20 of 201", first_page.text)
            self.assertIn("Page 1 of 11", first_page.text)
            self.assertIn("Rows per page:", first_page.text)
            for size in (10, 20, 50, 100):
                self.assertIn(f">{size}</a>", first_page.text)
            self.assertIn("ROW-0020", first_page.text)
            self.assertNotIn("ROW-0021", first_page.text)
            next_match = re.search(
                r'href="([^"]+)" data-readiness-next',
                first_page.text,
            )
            assert next_match is not None
            next_query = parse_qs(
                urlsplit(unescape(next_match.group(1))).query
            )
            self.assertEqual(next_query["page"], ["2"])

            second_page = self.client.get(
                f"/workspaces/{workspace_id}/summary?page=2"
            )
            self.assertEqual(
                second_page.text.count("data-readiness-row"),
                20,
            )
            self.assertIn("Rows 21-40 of 201", second_page.text)
            self.assertIn("ROW-0021", second_page.text)
            self.assertIn("ROW-0040", second_page.text)
            self.assertNotIn("ROW-0001", second_page.text)

            clamped_page = self.client.get(
                f"/workspaces/{workspace_id}/summary?page=999"
            )
            self.assertEqual(
                clamped_page.text.count("data-readiness-row"),
                1,
            )
            self.assertIn("Rows 201-201 of 201", clamped_page.text)
            self.assertIn("Page 11 of 11", clamped_page.text)
            self.assertIn("ROW-0201", clamped_page.text)

            filtered_page = self.client.get(
                f"/workspaces/{workspace_id}/summary",
                params={
                    "status": "blocked",
                    "dataset": sample_row.dataset,
                    "page": "3",
                    "page_size": "50",
                },
            )
            self.assertEqual(
                filtered_page.text.count("data-readiness-row"),
                20,
            )
            self.assertIn("Rows 101-120 of 120", filtered_page.text)
            self.assertIn("Page 3 of 3", filtered_page.text)
            self.assertNotIn("data-readiness-next", filtered_page.text)
            previous_match = re.search(
                r'href="([^"]+)" data-readiness-previous',
                filtered_page.text,
            )
            assert previous_match is not None
            previous_query = parse_qs(
                urlsplit(unescape(previous_match.group(1))).query
            )
            self.assertEqual(previous_query["status"], ["blocked"])
            self.assertEqual(
                previous_query["dataset"],
                [sample_row.dataset],
            )
            self.assertEqual(previous_query["page"], ["2"])
            self.assertEqual(previous_query["page_size"], ["50"])

        self.assertEqual(len(self.readiness_calls), 2)
        readiness_requests = self.readiness_calls[-1][2]
        self.assertTrue(readiness_requests)
        self.assertEqual(
            {item.model for item in readiness_requests},
            {"res.partner"},
        )
        self.assertTrue(all(item.domain for item in readiness_requests))
        evidence = self.client.get(
            f"/workspaces/{workspace_id}/summary/manifest"
        )
        self.assertEqual(evidence.status_code, 200)
        self.assertIn(
            "application/json",
            evidence.headers["content-type"],
        )
        with patch("impodo.web.routers.preflight.write_review_workbook") as builder:
            builder.side_effect = lambda _manifest, workbook, **_options: Path(
                workbook
            ).write_bytes(b"review package")
            packaged = self.client.post(
                f"/workspaces/{workspace_id}/summary/package",
                data={"csrf_token": self.csrf},
                headers=POST_HEADERS,
                follow_redirects=False,
            )
            review_evidence = builder.call_args.kwargs["review_evidence"]
            self.assertIsNotNone(review_evidence)
            current_report = self.app.state.context.preflight.current_report(
                workspace_id
            )
            self.assertEqual(
                len(review_evidence.records),
                current_report.total_count,
            )
            self.assertEqual(
                review_evidence.frozen_input_hash,
                current_report.frozen_input_hash,
            )
            self.assertEqual(
                review_evidence.normalization_content_hash,
                current_report.normalization_content_hash,
            )
            review_trace_ids = {
                item.source_trace_id for item in review_evidence.records
            }
            self.assertTrue(
                all(
                    item.source_trace_id in review_trace_ids
                    for item in review_evidence.cell_effects
                )
            )
        self.assertEqual(packaged.status_code, 303)
        packaged_page = self.client.get(packaged.headers["location"])
        self.assertIn("Download review workbook", packaged_page.text)
        self.assertIn("Recreate review workbook", packaged_page.text)
        workbook = self.client.get(
            f"/workspaces/{workspace_id}/summary/workbook"
        )
        self.assertEqual(workbook.status_code, 200)
        self.assertIn(
            "spreadsheetml.sheet",
            workbook.headers["content-type"],
        )

        workspace_state = self.app.state.context.workspace_states.repository.get(workspace_id)

        def company_schema_reader(workspace_state, api_key):
            self.schema_calls.append((workspace_state.workspace_id, api_key))
            snapshot = _browser_schema(workspace_state)
            company = replace(
                snapshot.models["res.partner"],
                model="res.company",
            )
            return replace(snapshot, models={"res.company": company})

        self.app.state.context.schema_reader = company_schema_reader
        changed_scope = self.client.post(
            f"/workspaces/{workspace_id}/schema",
            data={
                "csrf_token": self.csrf,
                "revision": str(workspace_state.revision),
                "permitted_models": "res.company",
            },
            headers=POST_HEADERS,
            follow_redirects=False,
        )
        self.assertEqual(changed_scope.status_code, 303)
        workspace_state = self.app.state.context.workspace_states.repository.get(workspace_id)
        self.assertEqual(workspace_state.intended_models, ("res.company",))
        self.assertIsNone(workspace_state.mapping_version)
        self.assertEqual(workspace_state.approval_status.value, "INVALIDATED")
        refreshed_schema = (
            self.app.state.context.schema_workspace.schemas.get_odoo_schema_catalog(
                workspace_id
            )
        )
        self.assertIsNotNone(refreshed_schema)
        self.assertEqual(
            tuple(model.name for model in refreshed_schema.models),
            ("res.company",),
        )
        self.assertIsNone(
            self.app.state.context.schema_workspace.schemas.get_schema_governance(workspace_id)
        )
        self.assertIsNone(
            self.app.state.context.mapping_workspace.mappings.get_mapping_revision(workspace_id)
        )
        self.assertIsNone(
            self.app.state.context.preflight.current_staging(workspace_id)
        )
        self.assertIsNotNone(
            self.app.state.context.preflight.staging.get_canonical_staging_run(
                workspace_id,
                staging.run_id,
            )
        )
        self.assertIsNotNone(
            self.app.state.context.mapping_workspace.mappings.get_mapping_working_draft(
                workspace_id
            )
        )
        stale_mapping = self.client.get(
            f"/workspaces/{workspace_id}/mapping"
        )
        self.assertIn(
            "Your source tables or Odoo choices changed, so older matching work was not loaded",
            stale_mapping.text,
        )
