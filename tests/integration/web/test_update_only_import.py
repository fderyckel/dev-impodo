"""Update existing file-matched records without create fields or create writes."""

from dataclasses import asdict, replace
from datetime import UTC, datetime

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
from impodo.domain.workspace.workbench import OdooConnectionMode
from impodo.web.target_credentials import (
    TargetCredentialRole,
    local_read_credential_binding_hash,
    store_target_credential,
)
from tests.support.browser_scenarios import POST_HEADERS, _wait_for_preparation
from tests.support.database_probe import count_duckdb_statements
from tests.support.hierarchy_correction import MappingForm
from tests.support.mapping_journey import MappingJourneyFixture


class UpdateOnlyImportTests(MappingJourneyFixture):
    def configure_update(
        self,
        major=20,
        mode=OdooConnectionMode.REMOTE,
        model="account.account",
        *,
        row_count=2,
        identity_field="code",
        change_field="active",
        change_type="boolean",
        new_value=False,
        old_value=True,
    ):
        content = (
            f"Reference,Change\nA,{new_value}\nB,{new_value}\n".encode()
            if row_count == 2
            else (
                "Reference,Change\n"
                + "".join(f"CODE-{index},False\n" for index in range(row_count))
            ).encode()
        )
        workspace_id = self.freeze_files({"records": content})
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
            intended_models=(model,),
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
        self.metadata = MetadataSnapshot(
            TargetFingerprint(
                target,
                mode.value,
                state.odoo_database,
                f"{major}.0",
                datetime.now(UTC).isoformat(),
                {},
            ),
            {
                model: ModelMetadata(
                    model,
                    "Records",
                    {
                        identity_field: FieldMetadata(
                            identity_field, "char", "Reference", required=True
                        ),
                        change_field: FieldMetadata(
                            change_field,
                            "boolean" if change_type == "boolean" else "char",
                            "Value",
                            stored=True,
                        ),
                        "name": FieldMetadata("name", "char", "Name", required=True),
                        "account_type": FieldMetadata(
                            "account_type", "char", "Type", required=True
                        ),
                    },
                ),
            },
        )
        self.target_rows = {
            model: (
                TargetRecord(model, 42, {identity_field: "A", change_field: old_value}),
                TargetRecord(model, 43, {identity_field: "B", change_field: new_value}),
                TargetRecord(
                    model, 44, {identity_field: "UNRELATED", change_field: old_value}
                ),
            )
        }
        self.read_calls = []
        self.context.schema_read_session_factory = None
        self.context.schema_reader = lambda *_: self.metadata
        self.context.read_identity_probe = self.read_identity
        identity = None
        if mode is OdooConnectionMode.LOCAL:
            self._local_profile(state)
            self.context.readiness_reader = None
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
            identity = self.read_identity(state, "fictional-read-key", (model,))
            self.context.readiness_reader = self.snapshots
        schema = self.context.schema_workspace.capture(
            workspace_id,
            self.metadata,
            read_credential_binding_hash=binding,
            read_identity=identity,
            actor=self.actor,
        )
        self.context.schema_workspace.govern(
            workspace_id,
            actor=self.actor,
            business_keys=(
                BusinessKeyDefinition(
                    "records:key",
                    model,
                    (identity_field,),
                    status=BusinessKeyStatus.CONFIRMED,
                ),
            ),
        )
        selection = self.context.queries.get_mapping_source_selection(workspace_id)
        columns = {
            column.source_name: column.stable_key
            for column in selection.datasets[0].columns
        }
        url = f"/workspaces/{workspace_id}/mapping?step=tables&task=scalars&scalar_page_size=50"
        form = MappingForm(self.client.get(url).text)
        scalar_index = next(
            index
            for index, field in enumerate(schema.models[0].fields)
            if field.name == change_field
        )
        values = {
            "mode_0": "update",
            "source_identity_0": columns["Reference"],
            "identity_source_0_0": columns["Reference"],
            "business_key_0": "records:key",
            f"scalar_value_source_0_{scalar_index}": "source",
            f"scalar_source_0_{scalar_index}": columns["Change"],
            f"scalar_compare_0_{scalar_index}": "1",
            f"scalar_type_0_{scalar_index}": change_type,
        }
        form.entries = [entry for entry in form.entries if entry[0] not in values]
        form.entries.extend([name, value] for name, value in values.items())
        return workspace_id, url, form

    def send(self, form, action):
        return self.client.post(
            form.action,
            json={"entries": [*form.entries, ["action", action]]},
            headers={**POST_HEADERS, "X-CSRF-Token": self.csrf},
        )

    def snapshots(self, state, metadata_requests, record_requests):
        self.assertFalse(getattr(_read_databases, "connections", None))
        self.read_calls.append((tuple(metadata_requests), tuple(record_requests)))
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
        for request in record_requests:
            rows.setdefault(request.model, [])
            for record in self.target_rows.get(request.model, ()):
                if any(
                    operator == "in"
                    and record.values.get(field) not in value
                    or operator == "="
                    and record.values.get(field) != value
                    for field, operator, value in request.domain
                ):
                    continue
                rows[request.model].append(
                    TargetRecord(
                        record.model,
                        record.odoo_id,
                        {field: record.values.get(field) for field in request.fields},
                    )
                )
        return replace(self.metadata, models=models), RecordSnapshot(
            self.metadata.fingerprint,
            {model: tuple(records) for model, records in rows.items()},
            {request.model: request.fields for request in record_requests},
        )

    def test_update_only_archive_and_recipe_keep_existing_values(self):
        for major in (19, 20):
            for mode in (OdooConnectionMode.LOCAL, OdooConnectionMode.REMOTE):
                with self.subTest(major=major, mode=mode):
                    self.exercise_update(major, mode)

    def test_custom_model_uses_the_same_update_path(self):
        for major in (19, 20):
            self.exercise_update(
                major,
                OdooConnectionMode.REMOTE,
                "x.asset",
                identity_field="x_reference",
                change_field="x_note",
                change_type="string",
                new_value="Updated",
                old_value="Before",
            )

    def test_switching_a_saved_draft_removes_hidden_create_defaults(self):
        workspace_id, url, form = self.configure_update()
        schema = self.context.queries.get_odoo_schema_catalog(workspace_id)
        name_index = next(
            index
            for index, field in enumerate(schema.models[0].fields)
            if field.name == "name"
        )
        for entry in form.entries:
            if entry[0] == "mode_0":
                entry[1] = "upsert"
        form.entries.append([f"scalar_value_source_0_{name_index}", "odoo_default"])
        self.assertEqual(self.send(form, "save_progress").status_code, 200)
        saved = self.context.queries.get_mapping_working_draft(workspace_id)
        self.assertIn(
            "name",
            {field.target_field for field in saved.definition.datasets[0].fields},
        )
        reopened = MappingForm(self.client.get(url).text)
        # Change mode from Choose rows, where scalar controls are not submitted.
        reopened.entries = [
            entry
            for entry in reopened.entries
            if not entry[0].startswith(("scalar_", "visible_scalar_target_"))
        ]
        for entry in reopened.entries:
            if entry[0] == "mode_0":
                entry[1] = "update"
        self.assertEqual(self.send(reopened, "save_progress").status_code, 200)
        updated = self.context.queries.get_mapping_working_draft(workspace_id)
        self.assertEqual(
            [field.target_field for field in updated.definition.datasets[0].fields],
            ["active"],
        )

    def test_check_io_does_not_grow_with_685_source_rows(self):
        workspace_id, url, form = self.configure_update(row_count=685)
        self.assertEqual(self.send(form, "save_progress").status_code, 200)
        reopened = MappingForm(self.client.get(url).text)
        with (
            collect_duckdb_request_timings() as timings,
            count_duckdb_statements() as sql,
        ):
            checked = self.send(reopened, "draft")
        self.assertEqual(checked.status_code, 200, checked.text)
        self.assertLessEqual(timings.connection_count, 14)
        self.assertLessEqual(timings.schema_check_count, 11)
        self.assertLessEqual(len(sql), 166)
        self.assertIn("ready to confirm", checked.json()["message"], checked.json())

    def exercise_update(self, major, mode, model="account.account", **fields):
        workspace_id, url, form = self.configure_update(major, mode, model, **fields)
        saved = self.send(form, "save_progress")
        self.assertEqual(saved.status_code, 200, saved.text)
        reopened = self.client.get(url)
        self.assertIn(["mode_0", "update"], MappingForm(reopened.text).entries)
        with (
            collect_duckdb_request_timings() as timings,
            count_duckdb_statements() as sql,
        ):
            checked = self.send(MappingForm(reopened.text), "draft")
        self.assertEqual(checked.status_code, 200, checked.text)
        self.measurement = {**asdict(timings), "sql": len(sql)}
        # Admission, mutation intake, mapping preparation, and checked publication
        # own four bounded local phases. The four / four / two / four owners
        # replace the additional four-owner workspace reload before a no-op lookup.
        self.assertLessEqual(timings.connection_count, 14)
        self.assertLessEqual(timings.schema_check_count, 11)
        self.assertLessEqual(len(sql), 166)
        self.assertLessEqual(
            sum("duckdb_columns()" in statement for statement in sql), 11
        )
        self.assertEqual(timings.lock_retry_count, 0)
        self.assertIn("ready to confirm", checked.json()["message"], checked.json())
        confirmed = self.send(MappingForm(self.client.get(url).text), "submit")
        self.assertEqual(confirmed.status_code, 200, confirmed.text)
        staging, quality = self.prepare(workspace_id)
        self.assertEqual(quality.ready_count, 2, staging.rows)
        self.approve(workspace_id)
        self.expect_business_writes(workspace_id)
        owner = self.context.migration_workspaces.get(workspace_id, actor=self.actor)
        draft = self.context.recipe_publication.draft(
            project_id=owner.project_id,
            data_version_id=owner.data_version_id,
            workspace_id=workspace_id,
            actor=self.actor,
        )
        self.assertTrue(draft.can_publish, draft.issues)
        self.assertEqual(
            draft.compiled.recipe["mapping"]["datasets"][0]["mode"], "UPDATE"
        )
        portable = draft.compiled.recipe["mapping"]["datasets"][0]
        selection = self.context.queries.get_mapping_source_selection(workspace_id)
        source = selection.datasets[0]
        by_name = {column.source_name: column.stable_key for column in source.columns}
        bindings = {portable["logical_dataset_id"]: source.dataset_id}
        bindings.update(
            {
                column: by_name["Reference"]
                for column in portable["source_identity_column_ids"]
            }
        )
        bindings.update(
            {
                column: by_name["Change"]
                for column in portable["fields"][0]["provider"]["source_column_ids"]
            }
        )
        rebound = self.context.run_planning.compiler._mapping_datasets(
            draft.compiled.recipe,
            bindings,
            selection,
            (),
            None,
        )[0]
        self.assertEqual(rebound.mode.value, "update")
        self.assertFalse(rebound.mode.creates_records)
        self.assertTrue(rebound.mode.updates_records)
        schema = self.context.queries.get_odoo_schema_catalog(workspace_id)
        _assessment, issues, defaults = (
            self.context.run_planning.compiler._target_assessment(
                draft.compiled.recipe, schema
            )
        )
        self.assertFalse([issue for issue in issues if issue.blocks], issues)
        self.assertFalse(defaults)
        compared = self._post(
            f"/workspaces/{workspace_id}/summary/compare", {"csrf_token": self.csrf}
        )
        self.assertEqual(compared.status_code, 303, getattr(compared, "context", {}))
        if "/preflight/" in compared.headers["location"]:
            result = _wait_for_preparation(self.client, compared.headers["location"])
            self.assertEqual(result["status"], "SUCCEEDED", result)
        report = self.context.preflight.current_report(workspace_id)
        self.assertEqual(
            (
                report.create_count,
                report.update_count,
                report.unchanged_count,
                report.blocked_count,
            ),
            (0, 1, 1, 0),
            report,
        )
        snapshot = self.context.preflight.current_execution_snapshot(workspace_id)
        changed = [row for row in snapshot.rows if row.disposition == "UPDATE"]
        self.assertEqual(len(changed), 1)
        self.assertEqual(
            [field.field for field in changed[0].fields],
            [fields.get("change_field", "active")],
        )
        self.assertEqual(changed[0].fields[0].value, fields.get("new_value", False))
