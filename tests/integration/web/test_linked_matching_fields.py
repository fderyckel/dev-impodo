"""Select an Odoo key on a linked model through the authoring workflow."""

from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from impodo.adapters.duckdb.unit_of_work import _read_databases
from impodo.application.recipe.odoo_requirements import (
    OdooCheckRelationshipRequirement,
    OdooCheckSupportingRequirement,
)
from impodo.domain.odoo.company import OdooCompanyAccess, OdooCompanySelection
from impodo.domain.odoo.contracts import MetadataSnapshot, RecordSnapshot
from impodo.domain.schema.governance import BusinessKeyDefinition, BusinessKeyStatus
from impodo.domain.shared.models import (
    FieldMetadata,
    ModelMetadata,
    TargetFingerprint,
    TargetRecord,
    target_identity_hash,
)
from impodo.domain.workspace.contracts import SchemaField, SchemaModel
from impodo.domain.workspace.supporting_review import (
    SupportingChoice,
    SupportingChoiceAction,
)
from impodo.domain.workspace.workbench import OdooConnectionMode
from impodo.web.composition.target_readers import _capture_recipe_supporting_values
from impodo.web.target_credentials import (
    TargetCredentialRole,
    local_read_credential_binding_hash,
    store_target_credential,
)
from tests.support.browser_scenarios import POST_HEADERS, _wait_for_preparation
from tests.support.hierarchy_correction import MappingForm
from tests.support.mapping_journey import MappingJourneyFixture
from tests.support.database_probe import count_duckdb_statements


class LinkedMatchingFieldTests(MappingJourneyFixture):
    def test_identity_matching_field_odoo19_local(self):
        self.exercise_identity_models(19, OdooConnectionMode.LOCAL)

    def test_identity_matching_field_odoo19_remote(self):
        self.exercise_identity_models(19, OdooConnectionMode.REMOTE)

    def test_identity_matching_field_odoo20_local(self):
        self.exercise_identity_models(20, OdooConnectionMode.LOCAL)

    def test_identity_matching_field_odoo20_remote(self):
        self.exercise_identity_models(20, OdooConnectionMode.REMOTE)

    def exercise_identity_models(self, major, mode):
        for parent, edge, related, key, label in (
            ("mrp.bom", "product_tmpl_id", "product.template", "default_code", "Internal Reference"),
            ("x.work_order", "x_asset_id", "x.asset", "x_asset_code", "Asset Code"),
        ):
            with self.subTest(parent=parent):
                self.exercise_identity_selector(major, mode, parent, edge, related, key, label)

    def exercise_identity_selector(self, major, mode, parent, edge, related, key, label):
        workspace_id, columns = self.configure_link(
            major, mode, parent, edge, related, key, label,
            source_rows=b"Reference,ItemID\nSHARED,ITEM-41\nSHARED,ITEM-42\n",
        )
        base = f"/workspaces/{workspace_id}"
        discovery_data = {"csrf_token": self.csrf, "target_model": parent, "target_field": edge}
        with count_duckdb_statements() as baseline_sql, collect_duckdb_request_timings() as baseline_timings:
            baseline = self.client.post(base + "/mapping/linked-matching-fields", data=discovery_data, headers=POST_HEADERS)
        self.assertEqual(baseline.status_code, 200, baseline.text)
        self.target_rows[related] = tuple(
            TargetRecord(related, number, {"name": "Other display name", key: f"ITEM-{number}"})
            for number in (41, 42)
        )
        self.context.schema_workspace.govern(
            workspace_id, actor=self.actor,
            business_keys=(BusinessKeyDefinition(
                "scoped:reference", parent, ("code" if parent == "mrp.bom" else "x_order",),
                scope_fields=(edge,), status=BusinessKeyStatus.CONFIRMED,
            ),),
        )
        url = base + "/mapping?step=tables&task=identity"
        page = self.client.get(url)
        self.assertEqual(page.status_code, 200, page.text)
        self.assertIn("data-identity-matching-field", page.text)
        self.assertIn("Load Odoo matching fields", page.text)
        with count_duckdb_statements() as statements, collect_duckdb_request_timings() as timings:
            fields = self.client.post(
                base + "/mapping/linked-matching-fields",
                data=discovery_data,
                headers=POST_HEADERS,
            )
        self.assertEqual(fields.status_code, 200, fields.text)
        self.assertIn({"name": key, "label": label}, fields.json()["fields"])
        self.assertLessEqual(timings.connection_count, 6)
        self.assertEqual(timings.lock_retry_count, 0)
        self.assertEqual(timings.connection_count, baseline_timings.connection_count)
        self.assertEqual(timings.schema_check_count, baseline_timings.schema_check_count)
        self.assertEqual(statements, baseline_sql)
        self.discovery_metrics = getattr(self, "discovery_metrics", []) + [{
            "major": major, "mode": mode.value, "parent": parent,
            "connections": timings.connection_count,
            "schema_checks": timings.schema_check_count,
            "statements": len(statements),
        }]
        form = MappingForm(page.text)
        replacements = {
            "source_identity_0": [columns["Reference"], columns["ItemID"]],
            "identity_source_0_0": [columns["Reference"]],
            "identity_source_0_1": [columns["ItemID"]],
            "identity_origin_0_1": ["target_catalog"],
            "identity_resolver_key_0_1": ["odoo-field:" + key],
        }
        form.entries = [item for item in form.entries if item[0] not in replacements]
        form.entries.extend([name, value] for name, values in replacements.items() for value in values)
        original_entries = [list(item) for item in form.entries]
        calls_before = len(self.lookup_calls)
        for forged_field in ("id", "display_name", "nested.code"):
            form.entries = [
                [name, "odoo-field:" + forged_field if name == "identity_resolver_key_0_1" else value]
                for name, value in original_entries
            ]
            rejected = self.send_mapping(form, "save_progress")
            self.assertEqual(rejected.status_code, 422, rejected.text)
            self.assertEqual(len(self.lookup_calls), calls_before)
        form.entries = original_entries
        saved = self.send_mapping(form, "save_progress")
        self.assertEqual(saved.status_code, 200, saved.text)
        reopened = MappingForm(self.client.get(url).text)
        self.assertIn(["identity_resolver_key_0_1", "odoo-field:" + key], reopened.entries)
        checked = self.send_mapping(reopened, "draft")
        self.assertEqual(checked.status_code, 200, checked.text)
        _requests, reads = self.lookup_calls[-1]
        self.assertEqual(len(reads), 1)
        self.assertEqual(reads[0].domain, ((key, "in", ("ITEM-41", "ITEM-42")),))
        self.assertEqual(reads[0].fields, (key,))
        latest = self.context.queries.get_mapping_revision(workspace_id)
        component = latest.definition.datasets[0].target_scope[0]
        self.assertTrue(component.selected_matching_field)
        self.assertEqual(component.resolver.key_mappings[0].target_field, key)
        self.assertEqual(component.resolver.key_mappings[0].source_column_key, columns["ItemID"])
        self.assertEqual(type(latest.definition).from_json(latest.definition.to_json()), latest.definition)
        confirmed = self.send_mapping(MappingForm(self.client.get(url).text), "submit")
        self.assertEqual(confirmed.status_code, 200, confirmed.text)
        staging, quality = self.prepare(workspace_id)
        self.assertEqual(quality.ready_count, 2)
        self.assertEqual(len(staging.rows), 2)
        self.approve(workspace_id)
        self.expect_business_writes(workspace_id)
        owner = self.context.migration_workspaces.get(workspace_id, actor=self.actor)
        draft = self.context.recipe_publication.draft(
            project_id=owner.project_id, data_version_id=owner.data_version_id,
            workspace_id=workspace_id, actor=self.actor,
        )
        self.assertTrue(draft.can_publish, draft.issues)
        portable = draft.compiled.recipe["mapping"]["datasets"][0]["scope"][0]
        self.assertTrue(portable["selected_matching_field"])
        rebound = self.context.run_planning.compiler._identity(
            portable, {column: "fresh-item-id" for column in portable["source_column_ids"]},
        )
        self.assertTrue(rebound.selected_matching_field)
        self.assertEqual(rebound.resolver.key_mappings[0].target_field, key)

    def test_odoo19_local(self):
        self.exercise_models(19, OdooConnectionMode.LOCAL)

    def test_odoo19_remote(self):
        self.exercise_models(19, OdooConnectionMode.REMOTE)

    def test_odoo20_local(self):
        self.exercise_models(20, OdooConnectionMode.LOCAL)

    def test_odoo20_remote(self):
        self.exercise_models(20, OdooConnectionMode.REMOTE)

    def test_discovery_rejects_forged_edge_and_rechecks_saved_field_before_query(self):
        parent, edge, related, key = (
            "x.work_order",
            "x_asset_id",
            "x.asset",
            "x_asset_code",
        )
        workspace_id, columns = self.configure_link(
            20,
            OdooConnectionMode.REMOTE,
            parent,
            edge,
            related,
            key,
            "Asset Code",
        )
        base = f"/workspaces/{workspace_id}"
        forged = self.client.post(
            f"{base}/mapping/linked-matching-fields",
            data={
                "csrf_token": self.csrf,
                "target_model": related,
                "target_field": edge,
            },
            headers=POST_HEADERS,
        )
        self.assertEqual(forged.status_code, 422)
        self.assertFalse(self.lookup_calls)
        url = f"{base}/mapping?step=tables&task=links&relation_query={edge}"
        form = self.select_field(self.client.get(url), columns, key)
        saved = self.send_mapping(form, "save_progress")
        self.assertEqual(saved.status_code, 200, saved.text)
        model = self.metadata.models[related]
        self.metadata = replace(
            self.metadata,
            models={
                **self.metadata.models,
                related: replace(
                    model,
                    fields={
                        **model.fields,
                        key: replace(model.fields[key], searchable=False),
                    },
                ),
            },
        )
        checked = self.send_mapping(MappingForm(self.client.get(url).text), "draft")
        self.assertEqual(checked.status_code, 422, checked.text)
        self.assertIn(
            "cannot be used for exact text matching", checked.json()["detail"]
        )
        self.assertEqual(len(self.lookup_calls), 1)
        self.assertEqual(self.lookup_calls[0][1], ())
        self.assertIn(
            ["relation_key_0_0", "odoo-field:" + key],
            MappingForm(self.client.get(url).text).entries,
        )

    def exercise_models(self, major, mode):
        for parent, edge, related, key, label in (
            (
                "mrp.bom",
                "product_tmpl_id",
                "product.template",
                "default_code",
                "Internal Reference",
            ),
            ("x.work_order", "x_asset_id", "x.asset", "x_asset_code", "Asset Code"),
        ):
            with self.subTest(parent=parent):
                self.exercise_selector(major, mode, parent, edge, related, key, label)

    def configure_link(self, major, mode, parent, edge, related, key, label, *, kind="many2one", source_value="ITEM-42", source_rows=None):
        workspace_id = self.freeze_files(
            {"records": source_rows if source_rows is not None else f"Reference,ItemID\nR-1,{source_value}\n".encode()}
        )
        state = self.context.queries.get(workspace_id)
        state = self.context.workspace_states.update_target(
            workspace_id,
            actor=self.actor,
            expected_revision=state.revision,
            odoo_connection_mode=mode.value,
            odoo_base_url="http://127.0.0.1:8069"
            if mode is OdooConnectionMode.LOCAL
            else "https://fictional.example.test",
            odoo_database=f"fictional_odoo{major}",
            intended_applications=(),
            intended_models=(parent,),
        )
        target = target_identity_hash(
            connection_mode=mode.value,
            base_url=state.odoo_base_url,
            database=state.odoo_database,
        )
        self.access = OdooCompanyAccess(
            "manager@example.test",
            "Workshop",
            (OdooCompanySelection(target, 1, "Workshop"),),
        )
        state = self.context.workspace_states.select_odoo_company(
            workspace_id,
            actor=self.actor,
            expected_revision=state.revision,
            selection=self.access.choices[0],
        )
        identity_field = "code" if parent == "mrp.bom" else "x_order"
        self.metadata = MetadataSnapshot(
            TargetFingerprint(
                target,
                mode.value,
                state.odoo_database,
                f"{major}.0",
                datetime.now(UTC).isoformat(),
                {"base": f"{major}.0.1"},
            ),
            {
                parent: ModelMetadata(
                    parent,
                    "Work order",
                    {
                        identity_field: FieldMetadata(
                            identity_field, "char", "Reference", required=True
                        ),
                        edge: FieldMetadata(
                            edge,
                            kind,
                            "Linked record",
                            required=True,
                            relation=related,
                        ),
                    },
                ),
                related: ModelMetadata(
                    related,
                    "Existing item",
                    {
                        "name": FieldMetadata("name", "char", "Name"),
                        key: FieldMetadata(
                            key,
                            "char",
                            label,
                            stored=True,
                            searchable=True,
                            exportable=True,
                        ),
                        "x_unsearchable": FieldMetadata(
                            "x_unsearchable", "char", searchable=False
                        ),
                        "x_private": FieldMetadata(
                            "x_private", "char", exportable=False
                        ),
                        "x_number": FieldMetadata("x_number", "float"),
                    },
                ),
            },
        )
        self.target_rows = {
            related: (
                TargetRecord(
                    related, 42, {"name": "Different display name", key: "ITEM-42"}
                ),
            )
        }
        self.lookup_calls = []
        self.context.schema_read_session_factory = None
        self.context.schema_reader = lambda *_args: replace(
            self.metadata,
            models={parent: self.metadata.models[parent]},
        )
        self.context.read_identity_probe = self.read_identity
        identity = None
        if mode is OdooConnectionMode.LOCAL:
            self.context.readiness_reader = None
            self._local_profile(state)
            binding = local_read_credential_binding_hash(state)
        else:
            owner = self.context.target_credential_workspace(
                workspace_id, workspace_state=state, actor=self.actor
            )
            binding = store_target_credential(
                self.secrets,
                owner,
                TargetCredentialRole.READ,
                "fictional-read-key",
                persistent=False,
            ).binding_hash
            identity = self.read_identity(state, "fictional-read-key", (parent,))
            self.context.readiness_reader = self.snapshots
        schema = self.context.schema_workspace.capture(
            workspace_id,
            self.context.schema_reader(state, ""),
            read_credential_binding_hash=binding,
            read_identity=identity,
            actor=self.actor,
        )
        self.context.schema_workspace.govern(
            workspace_id,
            actor=self.actor,
            business_keys=(
                BusinessKeyDefinition(
                    "order:reference",
                    parent,
                    (identity_field,),
                    status=BusinessKeyStatus.CONFIRMED,
                ),
            ),
        )
        packet = self.context.schema_workspace.supporting_review_inputs(
            workspace_id, actor=self.actor
        )
        self.context.schema_workspace.supporting.save(
            packet,
            actor=self.actor,
            expected_binding_hash=packet.binding_hash,
            expected_version=0,
            operation_id=str(uuid4()),
            choices=(SupportingChoice(parent, edge, SupportingChoiceAction.REUSE),),
        )
        dataset = self.context.queries.get_mapping_source_selection(
            workspace_id
        ).datasets[0]
        return workspace_id, {
            column.source_name: column.stable_key for column in dataset.columns
        }

    def snapshots(self, state, metadata_requests, record_requests):
        self.assertFalse(getattr(_read_databases, "connections", None))
        self.assertEqual(state.odoo_company.name, "Workshop")
        self.lookup_calls.append((tuple(metadata_requests), tuple(record_requests)))
        models = {
            request.model: replace(
                self.metadata.models[request.model],
                fields={
                    name: field
                    for name, field in self.metadata.models[
                        request.model
                    ].fields.items()
                    if request.all_fields or name in request.fields
                },
            )
            for request in metadata_requests
        }
        rows = {}
        requested_fields = {}
        for request in record_requests:
            requested_fields[request.model] = request.fields
            rows.setdefault(request.model, [])
            for record in self.target_rows.get(request.model, ()):
                if any(
                    (operator == "in" and record.values.get(field) not in value)
                    or (operator == "=" and record.values.get(field) != value)
                    for field, operator, value in request.domain
                ):
                    continue
                rows.setdefault(request.model, []).append(
                    TargetRecord(
                        record.model,
                        record.odoo_id,
                        {field: record.values.get(field) for field in request.fields},
                    )
                )
        return replace(self.metadata, models=models), RecordSnapshot(
            self.metadata.fingerprint,
            {model: tuple(records) for model, records in rows.items()},
            requested_fields,
        )

    def exercise_selector(self, major, mode, parent, edge, related, key, label):
        workspace_id, columns = self.configure_link(
            major, mode, parent, edge, related, key, label
        )
        base = f"/workspaces/{workspace_id}"
        url = f"{base}/mapping?step=tables&task=links&relation_query={edge}"
        page = self.client.get(url)
        self.assertEqual(page.status_code, 200, page.text)
        self.assertIn("Load Odoo matching fields", page.text)
        self.assertFalse(self.lookup_calls)
        with collect_duckdb_request_timings() as timings:
            fields = self.client.post(
                f"{base}/mapping/linked-matching-fields",
                data={
                    "csrf_token": self.csrf,
                    "target_model": parent,
                    "target_field": edge,
                },
                headers=POST_HEADERS,
            )
        self.assertEqual(fields.status_code, 200, fields.text)
        self.assertEqual(
            fields.json()["fields"],
            [{"name": key, "label": label}, {"name": "name", "label": "Name"}]
            if label < "Name"
            else [{"name": "name", "label": "Name"}, {"name": key, "label": label}],
        )
        self.assertEqual(len(self.lookup_calls), 1)
        requests, reads = self.lookup_calls[0]
        self.assertEqual([request.model for request in requests], [related])
        self.assertTrue(requests[0].all_fields)
        self.assertEqual(reads, ())
        self.assertLessEqual(timings.connection_count, 6)
        self.assertEqual(timings.lock_retry_count, 0)

        form = self.select_field(page, columns, key)
        response = self.send_mapping(form, "save_progress")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(self.lookup_calls), 1)
        form = MappingForm(self.client.get(url).text)
        self.assertIn(["relation_key_0_0", "odoo-field:" + key], form.entries)
        checked = self.send_mapping(form, "draft")
        self.assertEqual(checked.status_code, 200, checked.text)
        self.assertEqual(len(self.lookup_calls), 3, self.lookup_calls)
        self.assertEqual(self.lookup_calls[1][1], ())
        self.assertEqual(self.lookup_calls[1][0][0].fields, (key,))
        requests, reads = self.lookup_calls[2]
        self.assertEqual(requests[0].fields, (key,))
        self.assertEqual(len(reads), 1)
        self.assertEqual(reads[0].domain, ((key, "in", ("ITEM-42",)),))
        self.assertEqual(reads[0].fields, (key,))
        self.assertEqual(reads[0].limit, 501)
        latest = self.context.queries.get_mapping_revision(workspace_id)
        self.assertIsNotNone(latest)
        relationship = latest.definition.datasets[0].relationships[0]
        self.assertEqual(relationship.resolver.key_mappings[0].target_field, key)
        self.assertEqual(
            relationship.resolver.key_mappings[0].source_column_key, columns["ItemID"]
        )

        confirmed = self.send_mapping(MappingForm(self.client.get(url).text), "submit")
        self.assertEqual(confirmed.status_code, 200, confirmed.text)
        staging, quality = self.prepare(workspace_id)
        self.assertEqual(quality.ready_count, 1)
        self.assertEqual(staging.rows[0].references[edge].target_fields, (key,))
        self.approve(workspace_id)
        self.expect_business_writes(workspace_id)
        self.assert_recipe_key(workspace_id, edge, key)
        compared = self._post(f"{base}/summary/compare", {"csrf_token": self.csrf})
        self.assertEqual(
            compared.status_code,
            303,
            (
                getattr(compared, "context", {}).get("comparison_recovery"),
                self.lookup_calls[-1],
            ),
        )
        if "/preflight/" in compared.headers["location"]:
            completed = _wait_for_preparation(self.client, compared.headers["location"])
            self.assertEqual(completed["status"], "SUCCEEDED", completed)
        report = self.context.preflight.current_report(workspace_id)
        self.assertIsNotNone(report)
        self.assertEqual(report.blocked_count, 0)
        self.assertEqual(report.create_count, 1)
        self.assertEqual(
            self.context.queries.get(workspace_id).intended_models, (parent,)
        )

    def assert_recipe_key(self, workspace_id, edge, key):
        owner = self.context.migration_workspaces.get(workspace_id, actor=self.actor)
        draft = self.context.recipe_publication.draft(
            project_id=owner.project_id,
            data_version_id=owner.data_version_id,
            workspace_id=workspace_id,
            actor=self.actor,
        )
        self.assertTrue(draft.can_publish, draft.issues)
        dataset = draft.compiled.recipe["mapping"]["datasets"][0]
        relation = next(
            item for item in dataset["relationships"] if item["target_field"] == edge
        )
        self.assertTrue(relation["selected_matching_field"])
        self.assertEqual(relation["target_key_fields"], [key])
        rebound = self.context.run_planning.compiler._relationship(
            relation,
            {column: "fresh-item-id" for column in relation["source_column_ids"]},
        )
        self.assertTrue(rebound.selected_matching_field)
        self.assertEqual(rebound.resolver.key_mappings[0].target_field, key)
        self.assertEqual(
            rebound.resolver.key_mappings[0].source_column_key, "fresh-item-id"
        )

    def select_field(self, page, columns, key):
        form = MappingForm(page.text)
        fields = {
            "source_identity_0": columns["Reference"],
            "identity_source_0_0": columns["Reference"],
            "relation_value_source_0_0": "source",
            "relation_source_0_0": columns["ItemID"],
            "relation_origin_0_0": "target_catalog",
            "relation_key_0_0": "odoo-field:" + key,
            "relation_categorical_policy_0_0": "EXACT_BUSINESS_KEY",
        }
        form.entries = [item for item in form.entries if item[0] not in fields]
        form.entries.extend([name, value] for name, value in fields.items())
        return form

    def send_mapping(self, form, action):
        return self.client.post(
            form.action,
            json={"entries": [*form.entries, ["action", action]]},
            headers={**POST_HEADERS, "X-CSRF-Token": self.csrf},
        )

    def test_reviewed_account_tags_match_each_list_value_without_expanding_scope(self):
        for major in (19, 20):
            for mode in (OdooConnectionMode.LOCAL, OdooConnectionMode.REMOTE):
                with self.subTest(major=major, mode=mode):
                    self.exercise_reviewed_list(major, mode, "account.account.tag", "tag_ids")

    def test_reviewed_company_list_keeps_the_same_lookup_path(self):
        self.exercise_reviewed_list(20, OdooConnectionMode.REMOTE, "res.company", "company_ids")

    def exercise_reviewed_list(self, major, mode, related, edge):
        workspace_id, columns = self.configure_link(
            major, mode, "account.account", edge, related, "name", "Tag Name",
            kind="many2many", source_value="  Operations ; Salaries  ",
        )
        self.target_rows = {related: (
            TargetRecord(related, 42, {"name": "Operations"}),
            TargetRecord(related, 43, {"name": "Salaries"}),
            TargetRecord(related, 44, {"name": "Unrelated"}),
        )}
        base = f"/workspaces/{workspace_id}"
        url = f"{base}/mapping?step=tables&task=links&relation_query={edge}"
        page = self.client.get(url)
        self.assertNotIn("No matching rule available", page.text)
        form = self.select_field(page, columns, "name")
        for entry in form.entries:
            if entry[0] == "relation_key_0_0":
                entry[1] = f"odoo-supporting:{related}:name"
            elif entry[0] == "relation_operation_0_0":
                entry[1] = "add"
        saved = self.send_mapping(form, "save_progress")
        self.assertEqual(saved.status_code, 200, saved.text)
        reopened = MappingForm(self.client.get(url).text)
        self.assertIn(["relation_key_0_0", f"odoo-supporting:{related}:name"], reopened.entries)
        with collect_duckdb_request_timings() as timings:
            checked = self.send_mapping(reopened, "draft")
        self.assertEqual(checked.status_code, 200, checked.text)
        latest = self.context.queries.get_mapping_revision(workspace_id)
        self.assertIsNotNone(latest)
        self.assertNotIn("MAPPING_TARGET_MODEL_UNKNOWN", self.client.get(f"{base}/mapping?step=validate").text)
        requests, reads = self.lookup_calls[-1]
        self.assertEqual(requests[0].fields, ("name",))
        self.assertFalse(requests[0].all_fields)
        self.assertEqual(reads[0].domain, (("name", "in", ("Operations", "Salaries")),))
        self.assertEqual(reads[0].fields, ("name",))
        self.assertEqual(reads[0].limit, 501)
        self.assertEqual(timings.lock_retry_count, 0)
        confirmed = self.send_mapping(MappingForm(self.client.get(url).text), "submit")
        self.assertEqual(confirmed.status_code, 200, confirmed.text)
        staging, quality = self.prepare(workspace_id)
        self.assertEqual(quality.ready_count, 1, staging.rows[0].issues)
        self.assertEqual(
            [(reference.target_fields, reference.key) for reference in staging.rows[0].references[edge]],
            [(("name",), ("Operations",)), (("name",), ("Salaries",))],
        )
        self.approve(workspace_id)
        self.expect_business_writes(workspace_id)
        owner = self.context.migration_workspaces.get(workspace_id, actor=self.actor)
        draft = self.context.recipe_publication.draft(
            project_id=owner.project_id, data_version_id=owner.data_version_id,
            workspace_id=workspace_id, actor=self.actor,
        )
        self.assertTrue(draft.can_publish, draft.issues)
        relation = draft.compiled.recipe["mapping"]["datasets"][0]["relationships"][0]
        self.assertEqual(relation["target_key_fields"], ["name"])
        rebound = self.context.run_planning.compiler._relationship(
            relation, {column: "fresh-tags" for column in relation["source_column_ids"]},
        )
        self.assertEqual(rebound.kind, "many2many")
        self.assertEqual(rebound.separator, ";")
        schema = self.context.queries.get_odoo_schema_catalog(workspace_id)
        fresh = replace(schema, models=(*schema.models, SchemaModel(
            related, "Linked records", (SchemaField("name", "Name", "char", False, False, None, None, ()),),
        )))
        _assessment, issues, _defaults = self.context.run_planning.compiler._target_assessment(
            draft.compiled.recipe, fresh,
        )
        self.assertFalse([issue for issue in issues if issue.blocks], issues)
        captured = _capture_recipe_supporting_values(
            self.context, self.context.queries.get(workspace_id), fresh,
            (OdooCheckSupportingRequirement(
                related, ("name",), (),
                (OdooCheckRelationshipRequirement("account.account", edge, "many2many"),),
                ("Accounts",),
            ),), actor=self.actor,
        )
        self.assertEqual(tuple(choice.value for choice in captured[0].choices), ("Operations", "Salaries", "Unrelated"))
        self.assertEqual(self.lookup_calls[-1][0][0].fields, ("name",))
        compared = self._post(f"{base}/summary/compare", {"csrf_token": self.csrf})
        self.assertEqual(compared.status_code, 303, getattr(compared, "context", {}))
        if "/preflight/" in compared.headers["location"]:
            completed = _wait_for_preparation(self.client, compared.headers["location"])
            self.assertEqual(completed["status"], "SUCCEEDED", completed)
        report = self.context.preflight.current_report(workspace_id)
        self.assertEqual(report.blocked_count, 0, report)
        self.assertEqual(self.context.queries.get(workspace_id).intended_models, ("account.account",))
        self.assertEqual(
            tuple(model.name for model in self.context.queries.get_odoo_schema_catalog(workspace_id).models),
            ("account.account",),
        )

    def test_account_tag_lookups_retain_missing_ambiguous_and_metadata_failures(self):
        for major in (19, 20):
            with self.subTest(major=major):
                related = "account.account.tag"
                workspace_id, columns = self.configure_link(
                    major, OdooConnectionMode.REMOTE, "account.account", "tag_ids",
                    related, "name", "Tag Name", kind="many2many", source_value="Operations;Missing",
                )
                self.target_rows = {related: (
                    TargetRecord(related, 42, {"name": "Operations"}),
                    TargetRecord(related, 43, {"name": "Operations"}),
                )}
                base = f"/workspaces/{workspace_id}"
                url = f"{base}/mapping?step=tables&task=links&relation_query=tag_ids"
                form = self.select_field(self.client.get(url), columns, "name")
                for entry in form.entries:
                    if entry[0] == "relation_key_0_0":
                        entry[1] = f"odoo-supporting:{related}:name"
                checked = self.send_mapping(form, "draft")
                self.assertEqual(checked.status_code, 200, checked.text)
                latest = self.context.queries.get_mapping_revision(workspace_id)
                schema = self.context.queries.get_odoo_schema_catalog(workspace_id)
                supporting = self.context.mapping_workspace._current_supporting_references(
                    workspace_id, latest.definition, schema,
                )
                self.assertEqual(supporting[0].ambiguous_values, ("Operations",))
                self.assertEqual(supporting[0].choices, ())
                self.assertNotIn("MAPPING_TARGET_MODEL_UNKNOWN", self.client.get(f"{base}/mapping?step=check").text)
                confirmed = self.send_mapping(MappingForm(self.client.get(url).text), "submit")
                self.assertEqual(confirmed.status_code, 200, confirmed.text)
                staging, quality = self.prepare(workspace_id)
                self.assertEqual(quality.ready_count, 1)
                self.assertTrue(staging.rows[0].references)
                self.approve(workspace_id)
                self.expect_business_writes(workspace_id)
                compared = self._post(f"{base}/summary/compare", {"csrf_token": self.csrf})
                self.assertEqual(compared.status_code, 303, getattr(compared, "context", {}))
                if "/preflight/" in compared.headers["location"]:
                    completed = _wait_for_preparation(self.client, compared.headers["location"])
                    self.assertEqual(completed["status"], "SUCCEEDED", completed)
                report = self.context.preflight.current_report(workspace_id)
                self.assertGreater(report.blocked_count, 0, report)
                model = self.metadata.models[related]
                self.metadata = replace(self.metadata, models={
                    **self.metadata.models,
                    related: replace(model, fields={"name": replace(model.fields["name"], type="integer")}),
                })
                rejected = self.send_mapping(MappingForm(self.client.get(url).text), "draft")
                self.assertEqual(rejected.status_code, 422, rejected.text)
                self.assertIn("incompatible supporting fields", rejected.json()["detail"])
