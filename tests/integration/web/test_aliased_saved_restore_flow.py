"""Run protected saved aliases through assembled capture, restore and read-back.

Both connectors use controlled gateway fixtures. No live Odoo server or ORM
qualification is claimed by these complete local application journeys.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import patch
from uuid import uuid4

from impodo.adapters.duckdb.odoo_saved_source_company import (
    OdooSavedSourceCompanyRepository,
)
from impodo.domain.mapping.contracts import ValueMapping
from impodo.domain.odoo.company import OdooCompanySelection
from impodo.domain.odoo.contracts import MetadataSnapshot, RecordSnapshot
from impodo.domain.odoo_database_binding import OdooDatabaseBindingRole
from impodo.domain.odoo_relationship_scope import (
    OdooRelationshipCaptureAction,
    relationship_scope_decisions,
)
from impodo.domain.odoo_source_capture import (
    OdooCaptureAccounting,
    OdooCapturePage,
    OdooCaptureRelationshipColumn,
    OdooCaptureValueColumn,
)
from impodo.domain.odoo_source_policy import odoo_source_policy_hash
from impodo.domain.odoo_source_scope import propose_related_odoo_data
from impodo.domain.serialization import canonical_json, content_hash
from impodo.domain.shared.access import Capability
from impodo.domain.shared.models import (
    FieldMetadata,
    ModelMetadata,
    OdooWriteIdentity,
    TargetFingerprint,
    TargetRecord,
    target_identity_hash,
)
from impodo.domain.workspace.contracts import OdooModelSummary, SchemaModel
from impodo.domain.workspace.transfer_identity_aliases import (
    DestinationIdentityAliasRule,
)
from impodo.domain.workspace.workbench import OdooConnectionMode
from impodo.web.composition.database_bindings import review_database_binding
from impodo.web.composition.saved_odoo_restore import saved_identity_alias_rule_owner
from tests.integration.web import test_saved_odoo_restore_flow as restore_fixture
from tests.support.browser_scenarios import (
    ProjectSetupBrowserTestCase,
    _BrowserOdooCaptureGateway,
)
from tests.support.identity import resolve_local_actor

REFERENCE = "x.alias_reference"
RELATION = "x_alias_reference_id"
SOURCE_REFERENCE = "Clean source reference"
DESTINATION_REFERENCE = "Existing target reference"


def _metadata(workspace, schema):
    return MetadataSnapshot(
        TargetFingerprint(
            target_hash=target_identity_hash(
                connection_mode=workspace.odoo_connection_mode.value,
                base_url=workspace.odoo_base_url,
                database=workspace.odoo_database,
            ),
            connection_mode=workspace.odoo_connection_mode.value,
            database=workspace.odoo_database,
            odoo_version=schema.odoo_version,
            snapshot_timestamp="2026-11-13T10:00:00Z",
            module_versions={"base": f"{schema.odoo_version}.1.0"},
        ),
        {
            model.name: ModelMetadata(
                model.name,
                model.label,
                {
                    field.name: FieldMetadata(
                        **{
                            name: getattr(field, name)
                            for name in (
                                "name",
                                "type",
                                "label",
                                "required",
                                "readonly",
                                "relation",
                                "relation_field",
                                "selection",
                                "stored",
                                "computed",
                                "has_inverse",
                                "related",
                                "translated",
                                "company_dependent",
                                "searchable",
                                "sortable",
                                "exportable",
                                "digits",
                                "currency_field",
                            )
                        }
                    )
                    for field in model.fields
                },
            )
            for model in schema.models
        },
    )


class _Session:
    def __init__(self, request, now):
        child = request.model == "res.partner"
        ids, names = (
            ((11, 12), ("Alice", "Bob")) if child else ((7,), (SOURCE_REFERENCE,))
        )
        self.page = OdooCapturePage(
            first_row_ordinal=1,
            odoo_ids=ids,
            write_dates=(now,) * len(ids),
            columns=(OdooCaptureValueColumn("name", "char", names),),
            relationships=(
                OdooCaptureRelationshipColumn(
                    RELATION, "many2one", REFERENCE, ((7,), (7,))
                ),
            )
            if child
            else (),
            response_bytes=100,
            normalized_bytes=50,
        )
        self._accounting = OdooCaptureAccounting(
            high_water_id=max(ids),
            row_count=len(ids),
            page_count=1,
            record_request_count=2,
            response_bytes=102,
            normalized_bytes=50,
            capture_started_at=now,
            capture_finished_at=now + timedelta(seconds=1),
            consistency=request.consistency,
            target_instance_assurance=request.target_instance_assurance,
            consistency_limitation="Controlled capture uses a bounded API interval.",
        )

    def pages(self):
        return iter((self.page,))

    @property
    def matching_rows(self):
        return self.page.row_count

    @property
    def accounting(self):
        return self._accounting


class _Gateway(_BrowserOdooCaptureGateway):
    def probe_schema(self, request, context, *, cancellation=None):
        return _metadata(self.workspace_state, self.schema)

    def open_capture(self, request, context, *, cancellation=None):
        return _Session(request, self.now)

    def count_matching(self, request, context, *, limit, cancellation=None):
        return 2 if request.model == "res.partner" else 1

    def scan_origins(self, request, context, *, cancellation=None, observe_page=None):
        page = _Session(request, self.now).page
        if observe_page:
            observe_page(page.row_count, page.response_bytes)
        return (page.origin_batch,)


class _Writer(restore_fixture._RestoreOdoo):
    def create_rows(self, model, values):
        # Odoo's load API accepts /.id columns and persists native relations.
        native = tuple(
            {
                field.removesuffix("/.id"): int(value)
                if field.endswith("/.id")
                else value
                for field, value in row.items()
            }
            for row in values
        )
        return super().create_rows(model, native)

    def find_ids(self, model, domain):
        return tuple(
            identifier
            for (record_model, identifier), values in self.records.items()
            if record_model == model
            and all(
                values.get(field) == value
                for field, operator, value in domain
                if operator == "="
            )
        )


class AliasedSavedRestoreFlowTests(ProjectSetupBrowserTestCase):
    def _capture(self, major, mode):
        context = self.app.state.context
        actor = resolve_local_actor(context)
        state, schema = self._registered_remote_schema_workspace()
        catalog = context.schema_workspace.schemas.get_odoo_model_catalog(
            state.workspace_id
        )
        state = context.workspace_states.update_target(
            state.workspace_id,
            actor=actor,
            expected_revision=context.queries.get(state.workspace_id).revision,
            odoo_connection_mode=mode.value,
            odoo_base_url="https://clean-source.example.test"
            if mode is OdooConnectionMode.REMOTE
            else "http://127.0.0.1:9876",
            odoo_database="clean_source",
            intended_applications=(),
            intended_models=("res.partner", REFERENCE),
        )
        target_hash = target_identity_hash(
            connection_mode=mode.value,
            base_url=state.odoo_base_url,
            database=state.odoo_database,
        )
        company = OdooCompanySelection(target_hash, 1, "Source company")
        OdooSavedSourceCompanyRepository(
            context.sources.sources._database
        ).set_source_company(
            state.workspace_id,
            company,
            actor=actor,
            expected_revision=context.queries.get(state.workspace_id).revision,
        )
        state = context.queries.get(state.workspace_id)
        original = schema.models[0]
        fields = tuple(
            field for field in original.fields if field.name in {"name", "write_date"}
        )
        relation = replace(
            next(field for field in original.fields if field.name == "company_id"),
            name=RELATION,
            label="Reference setup",
            relation=REFERENCE,
            stored=True,
            computed=False,
            has_inverse=False,
            related=False,
            translated=False,
            company_dependent=False,
            exportable=True,
        )
        schema = replace(
            schema,
            connection_mode=mode.value,
            database=state.odoo_database,
            connection_target_hash=target_hash,
            odoo_version=f"{major}.0",
            policy_hash=odoo_source_policy_hash(major),
            read_context_hash=company.context_hash,
            models=(
                SchemaModel("res.partner", "Contacts", (*fields, relation)),
                SchemaModel(REFERENCE, "Reference setup", fields),
            ),
        )
        decisions = relationship_scope_decisions(
            propose_related_odoo_data(schema.models, include_selected=True),
            actions_by_model={REFERENCE: OdooRelationshipCaptureAction.MATCH_EXISTING},
        )
        if catalog is not None:
            catalog = replace(
                catalog,
                connection_mode=mode.value,
                database=state.odoo_database,
                connection_target_hash=target_hash,
                odoo_version=f"{major}.0",
                policy_hash=odoo_source_policy_hash(major),
                read_context_hash=company.context_hash,
                models=(
                    OdooModelSummary("res.partner", "Contacts", ("base",), "base"),
                    OdooModelSummary(REFERENCE, "Reference setup", ("base",), "base"),
                ),
            )
            context.schema_workspace.schemas.save_odoo_model_catalog(
                state.workspace_id,
                catalog,
                actor=actor,
            )
        self.assertIsNotNone(catalog)
        state = context.workspace_states.update_schema_scope(
            state.workspace_id,
            actor=actor,
            expected_revision=state.revision,
            permitted_models=("res.partner", REFERENCE),
            relationship_decisions=decisions,
            relationship_explicit_actions=(
                (REFERENCE, OdooRelationshipCaptureAction.MATCH_EXISTING),
            ),
            relationship_root_models=("res.partner",),
            schema_catalog=schema,
            expected_model_catalog_content_hash=catalog.content_hash,
        )
        for model in ("res.partner", REFERENCE):
            response = self._post(
                f"/workspaces/{state.workspace_id}/sources/odoo-selection",
                {
                    "csrf_token": self.csrf,
                    "dataset_name": "contacts"
                    if model == "res.partner"
                    else "reference_setup",
                    "model": model,
                    "field_names": "name",
                    "include_archived": "",
                    "page_size": "100",
                },
            )
            self.assertEqual(response.status_code, 303, response.text)
        identity = context.identity_resolver.resolve({"authenticated": True})
        review_database_binding(
            context, state, OdooDatabaseBindingRole.SOURCE,
            actor=actor, scope=identity.organization_scope,
            authorization_receipt=identity.authorization,
        )
        publication = context.odoo_capture_publication.publish(
            state.workspace_id, _Gateway(state, schema), actor=actor
        )
        context.data_version_source_projection.accept_odoo_capture(
            state.workspace_id,
            publication.source_selection,
            publication.source_snapshots,
            publication.manifests,
            actor=actor,
        )
        saved = context.saved_odoo_data
        definition = saved.create(
            state.workspace_id, actor=actor, label="Aliased saved clean data"
        )
        definition, version = saved.retain_current(
            definition.project_id,
            definition.saved_data_id,
            actor=actor,
            expected_revision=definition.revision,
            operation_id=str(uuid4()),
        )
        return definition, version, schema

    def _profile(self, definition, mode):
        context = self.app.state.context
        profile = restore_fixture.SavedOdooRestoreFlowTests._profile(
            self, definition, mode
        )
        profile = replace(
            profile,
            settings_json=canonical_json(
                {
                    "keys": {"res.partner": ["name"], REFERENCE: ["name"]},
                    "scopes": {},
                    "rules": {},
                    "defaults": {},
                }
            ),
        )
        context.workspace_access.require(
            resolve_local_actor(context),
            Capability.PROTECTED_EVIDENCE_MANAGE,
            workspace_id=definition.source_workspace_id,
        )
        reference = context.destination_identity_aliases.put_rules(
            definition.project_id,
            owner_key=saved_identity_alias_rule_owner(profile, definition),
            values=(
                DestinationIdentityAliasRule(
                    REFERENCE,
                    ("name",),
                    (ValueMapping(SOURCE_REFERENCE, DESTINATION_REFERENCE),),
                ),
            ),
        )
        profile = replace(
            profile,
            settings_json=canonical_json(
                {
                    **json.loads(profile.settings_json),
                    "identity_alias_rules": reference.to_dict(),
                }
            ),
        )
        return context.saved_odoo_data.repository.save_restore_profile(
            profile, expected_revision=None
        )

    def _destination(self, major, mode, source_schema):
        context = self.app.state.context
        original_identity = context.read_identity_probe
        state = {"adapter": None, "writer_count": 0}
        records = {
            (REFERENCE, 901): {"name": DESTINATION_REFERENCE},
            ("res.partner", 40): {"name": "Target only", RELATION: False},
        }

        def schema(workspace, _key):
            self.assertIsNotNone(
                workspace.saved_restore_generation_id,
                "Restore must not refresh source Odoo",
            )
            return _metadata(workspace, source_schema)

        def identity(workspace, key, models):
            self.assertIsNotNone(
                workspace.saved_restore_generation_id, "Source access is offline"
            )
            return replace(
                original_identity(workspace, key, models),
                context_hash=workspace.odoo_company.context_hash,
            )

        def match(workspace, key, metadata_requests, record_requests):
            metadata = schema(workspace, key)
            requested = {item.model: item.fields for item in record_requests}
            current = state["adapter"].records if state["adapter"] else records
            found = {
                model: tuple(
                    TargetRecord(
                        model, identifier, {name: values.get(name) for name in fields}
                    )
                    for (record_model, identifier), values in current.items()
                    if record_model == model and values["name"] != "Target only"
                )
                for model, fields in requested.items()
            }
            return metadata, RecordSnapshot(metadata.fingerprint, found, requested)

        def writer(workspace, _key, scope):
            current = context.queries.get(workspace.workspace_id)
            snapshot = context.transfer_execution.current_snapshot(
                current,
                context.queries.get_source_selection(current.workspace_id),
                context.queries.get_odoo_schema_catalog(current.workspace_id),
            )
            adapter = _Writer(
                snapshot, scope, imports_external_ids=mode is OdooConnectionMode.REMOTE
            )
            adapter.records.update(
                {key: dict(values) for key, values in records.items()}
            )
            state.update(adapter=adapter, writer_count=state["writer_count"] + 1)
            return adapter

        def write_identity(workspace, _key, scope):
            return OdooWriteIdentity(
                target_hash=target_identity_hash(
                    connection_mode=mode.value,
                    base_url=workspace.odoo_base_url,
                    database=workspace.odoo_database,
                ),
                principal_hash="sha256:" + "1" * 64,
                permission_hash=content_hash("fictional alias permission"),
                context_hash=workspace.odoo_company.context_hash,
                readable_models=tuple(item.model for item in scope.models),
                writable_models=tuple(
                    item.model for item in scope.models if item.write_fields
                ),
                observed_at=datetime.now(UTC).isoformat(),
            )

        return (
            state,
            records,
            {
                "schema_reader": schema,
                "read_identity_probe": identity,
                "destination_match_reader": match,
                "write_executor_factory": writer,
                "write_identity_probe": write_identity,
                "readback_reader_factory": lambda *_args: state["adapter"],
            },
        )

    def test_saved_aliases_restore_offline_and_repeat_without_duplicate_records(self):
        for major in (19, 20):
            for mode in (OdooConnectionMode.LOCAL, OdooConnectionMode.REMOTE):
                with self.subTest(major=major, mode=mode):
                    definition, version, schema = self._capture(major, mode)
                    self.assertEqual(
                        dict(version.row_counts), {"res.partner": 2, REFERENCE: 1}
                    )
                    profile = self._profile(definition, mode)
                    self.assertNotIn(SOURCE_REFERENCE, profile.settings_json)
                    self.assertNotIn(DESTINATION_REFERENCE, profile.settings_json)
                    state, records, adapters = self._destination(major, mode, schema)
                    with patch.object(
                        self.app.state.context,
                        "source_capture_factory",
                        side_effect=AssertionError("Original source is offline"),
                    ):
                        result = restore_fixture.SavedOdooRestoreFlowTests._run(
                            self, definition, version, profile, adapters
                        )
                        self.assertTrue(result.verified, result.issues)
                        current = state["adapter"].records
                        self.assertEqual(len(current), 4)
                        self.assertEqual(
                            current[(REFERENCE, 901)], {"name": DESTINATION_REFERENCE}
                        )
                        imported = [
                            values
                            for (model, _), values in current.items()
                            if model == "res.partner"
                            and values["name"] in {"Alice", "Bob"}
                        ]
                        self.assertEqual(
                            {values[RELATION] for values in imported}, {901}
                        )
                        self.assertTrue(
                            all(
                                model == "res.partner"
                                for model, _rows in state["adapter"].write_batches
                            )
                        )
                        checked = self.app.state.context.queries.get(
                            result.workspace_id
                        ).destination_match_plan
                        self.assertNotIn(SOURCE_REFERENCE, checked.to_json())
                        self.assertNotIn(DESTINATION_REFERENCE, checked.to_json())
                        self.assertIsNotNone(
                            checked.protected_identity_alias_artifact_hash
                        )
                        records.clear()
                        records.update(
                            {key: dict(values) for key, values in current.items()}
                        )
                        profile = self.app.state.context.saved_odoo_data.repository.get_restore_profile(
                            definition.project_id,
                            definition.saved_data_id,
                            result.profile_id,
                        )
                        repeated = restore_fixture.SavedOdooRestoreFlowTests._run(
                            self, definition, version, profile, adapters
                        )
                        self.assertTrue(repeated.verified, repeated.issues)
                        self.assertNotEqual(repeated.workspace_id, result.workspace_id)
                        self.assertEqual(len(state["adapter"].records), 4)
                        self.assertEqual(state["adapter"].records, records)


if __name__ == "__main__":
    import unittest

    unittest.main()
