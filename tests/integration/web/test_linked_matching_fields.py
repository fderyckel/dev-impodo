"""Select an Odoo key on a linked model through the authoring workflow."""

from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from impodo.adapters.duckdb.unit_of_work import _read_databases
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
from impodo.domain.workspace.supporting_review import (
    SupportingChoice,
    SupportingChoiceAction,
)
from impodo.domain.workspace.workbench import OdooConnectionMode
from impodo.web.target_credentials import (
    TargetCredentialRole,
    local_read_credential_binding_hash,
    store_target_credential,
)
from tests.support.browser_scenarios import POST_HEADERS, _wait_for_preparation
from tests.support.hierarchy_correction import MappingForm
from tests.support.mapping_journey import MappingJourneyFixture


class LinkedMatchingFieldTests(MappingJourneyFixture):
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

    def configure_link(self, major, mode, parent, edge, related, key, label):
        workspace_id = self.freeze_files(
            {"records": b"Reference,ItemID\nR-1,ITEM-42\n"}
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
                            "many2one",
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
