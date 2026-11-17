"""Diagnose saved choices without screenshots, source values or remote reads."""

from dataclasses import replace
from datetime import UTC, datetime
from io import BytesIO
import json
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch
from uuid import uuid4
from zipfile import ZipFile

from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from impodo.application.shared.build_contract import PROCESS_BUILD_CONTRACT
from impodo.domain.mapping.artifacts import MappingRevision
from impodo.domain.mapping.contracts import (
    DatasetMapping,
    IdentityComponentMapping,
    IdentityNullPolicy,
    ReferenceKeyMapping,
    RelationshipResolver,
    ResolverOrigin,
    RowInclusionCondition,
    RowInclusionMode,
    RowInclusionPolicy,
    ScalarFieldMapping,
    ScalarValueSource,
    SelectionConditionOperator,
)
from impodo.domain.preparation.staging import StagingRunStatus, StagingRunSummary
from impodo.domain.preparation.staging_contracts import StagingReconciliation
from impodo.domain.recipe.value_rules import ScalarTransformPolicy
from impodo.domain.shared.access import AuthorizationError
from impodo.domain.workspace.workbench import OdooConnectionMode
from impodo.web.composition.page_reads import run_local_operation_sync
from impodo.web.diagnostic_settings import (
    DiagnosticSettingsSnapshot,
    MAX_SETTINGS_ITEMS,
    SettingsProjection,
    SettingsStage,
    capture_diagnostic_settings,
)
from impodo.web.diagnostics import LocalDiagnosticRecorder, create_diagnostic_bundle
from tests.support.database_probe import count_duckdb_statements
from tests.support.mapping_journey import MappingJourneyFixture


class SettingsProjectionTests(TestCase):
    def test_values_formulas_and_unknown_payloads_are_not_exported(self):
        settings = DatasetMapping(
            dataset_id="incoming",
            target_model="x.anything",
            source_identity_column_keys=("article", "location"),
            row_inclusion=RowInclusionPolicy(
                mode=RowInclusionMode.MATCHING_ROWS,
                conditions=(
                    RowInclusionCondition(
                        str(uuid4()),
                        "quantity",
                        SelectionConditionOperator.NOT_EQUALS,
                        "0",
                        "decimal",
                    ),
                ),
            ),
            target_identity=(IdentityComponentMapping(("article",), ("product_id",)),),
            target_scope=(
                IdentityComponentMapping(
                    ("lot",),
                    ("lot_id",),
                    resolver=RelationshipResolver(
                        ResolverOrigin.TARGET_CATALOG,
                        model="x.related",
                        key_mappings=(ReferenceKeyMapping("lot", "name"),),
                    ),
                    null_policy=IdentityNullPolicy.OPTIONAL_SCOPE_NULL,
                    blank_source_column_key="lot",
                ),
            ),
            fields=(
                ScalarFieldMapping(
                    "note",
                    value_source=ScalarValueSource.CONSTANT,
                    literal_value="private-business-value",
                    transform=ScalarTransformPolicy(formula="value * 9876543"),
                ),
            ),
        )
        projection = SettingsProjection()
        result = projection.project(settings)
        encoded = json.dumps(result)
        self.assertNotIn("private-business-value", encoded)
        self.assertNotIn("9876543", encoded)
        self.assertIn("optional_scope_null", encoded)
        self.assertIn('"blank_source_column_key": "lot"', encoded)
        self.assertIn('"structural_value": "zero"', encoded)
        self.assertNotIn("unavailable", encoded)
        self.assertEqual(projection.omissions, {})
        self.assertEqual(
            projection.project({"token": "private-token"})["status"], "unavailable"
        )

    def test_unbounded_settings_are_reported_instead_of_silently_truncated(self):
        projection = SettingsProjection()
        self.assertEqual(
            projection.project(tuple(range(MAX_SETTINGS_ITEMS + 1)))["reason"],
            "SETTING_ITEM_LIMIT",
        )
        self.assertEqual(projection.project("x" * 501)["reason"], "SETTING_TEXT_LIMIT")

    def test_all_six_stage_sections_and_missing_scope_are_explicit(self):
        snapshot = DiagnosticSettingsSnapshot(
            *(str(uuid4()) for _ in range(4)),
            datetime.now(UTC).isoformat(),
            tuple(SettingsStage({"missing_choice": None}) for _ in range(6)),
        )
        members, coverage = snapshot.bundle_members()
        settings = json.loads(members["settings.json"])
        self.assertEqual([s["stage"] for s in settings["stages"]], list(range(1, 7)))
        self.assertEqual(
            settings["stages"][2]["settings"]["missing_choice"], {"status": "not_saved"}
        )
        self.assertEqual(coverage["unsaved_browser_changes"], "unavailable")


class DiagnosticSettingsBrowserTests(MappingJourneyFixture):
    def save_draft(self, selection, workspace_id):
        dataset = selection.datasets[0]
        columns = {c.source_name: c.stable_key for c in dataset.columns}
        mapping = DatasetMapping(
            dataset_id=dataset.dataset_id,
            target_model="x.item",
            source_identity_column_keys=(columns["Reference"],),
            target_identity=(
                IdentityComponentMapping((columns["Reference"],), ("ref",)),
            ),
            fields=(
                ScalarFieldMapping("name", source_column_key=columns["Product title"]),
            ),
        )
        return self.context.mapping_workspace.save_working_draft(
            workspace_id,
            datasets=(mapping,),
            expected_version=None,
            actor=self.actor,
        )

    def bundle(self, workspace_id=None):
        data = {"csrf_token": self.csrf}
        if workspace_id is not None:
            data["workspace_id"] = workspace_id
        return self.client.post(
            "/diagnostics/bundle", data=data, headers={"Origin": "http://testserver"}
        )

    def test_both_majors_and_access_modes_export_actual_saved_choices(self):
        for major in (19, 20):
            for mode in (OdooConnectionMode.LOCAL, OdooConnectionMode.REMOTE):
                with self.subTest(major=major, mode=mode):
                    workspace_id, selection, _ = self.configure(major, mode)
                    draft = self.save_draft(selection, workspace_id)
                    recorder = LocalDiagnosticRecorder(
                        Path(self.temporary.name) / "diagnostics"
                    )
                    self.app.state.diagnostic_recorder = recorder
                    remote_calls = (
                        len(self.schema_calls),
                        len(self.readiness_calls),
                        len(self.read_identity_calls),
                    )
                    with patch.object(
                        self.context.preflight,
                        "current_report",
                        side_effect=AssertionError("diagnostics must not validate"),
                    ):
                        response = self.bundle(workspace_id)
                    recorder.close()
                    self.assertEqual(response.status_code, 200, response.text[:200])
                    with ZipFile(BytesIO(response.content)) as archive:
                        settings = json.loads(archive.read("settings.json"))
                        manifest = json.loads(archive.read("manifest.json"))
                        self.assertFalse(
                            any(
                                name.endswith((".png", ".jpg", ".html"))
                                for name in archive.namelist()
                            )
                        )
                    self.assertEqual(settings["workspace_id"], workspace_id)
                    self.assertEqual(len(settings["stages"]), 6)
                    self.assertTrue(
                        all(s["status"] == "captured" for s in settings["stages"]),
                        settings,
                    )
                    self.assertEqual(manifest["settings_evidence"]["omissions"], {})
                    source = settings["stages"][0]["settings"]["source_selection"]
                    self.assertIn("Product title", json.dumps(source))
                    schema = settings["stages"][1]["settings"]["schema"]
                    self.assertEqual(schema["odoo_version"], f"{major}.0")
                    actual = settings["stages"][2]["settings"]["working_draft"]
                    self.assertEqual(actual["version"], draft.version)
                    self.assertEqual(
                        actual["definition"]["datasets"][0][
                            "source_identity_column_keys"
                        ],
                        list(draft.definition.datasets[0].source_identity_column_keys),
                    )
                    self.assertEqual(
                        actual["definition"]["datasets"][0]["fields"][0][
                            "target_field"
                        ],
                        "name",
                    )
                    encoded = json.dumps(settings)
                    for private in (
                        "Workshop drill",
                        "fictional-read-key",
                        "fictional_odoo",
                        "example.test",
                        "manager@example",
                    ):
                        self.assertNotIn(private, encoded)
                    self.assertEqual(
                        remote_calls,
                        (
                            len(self.schema_calls),
                            len(self.readiness_calls),
                            len(self.read_identity_calls),
                        ),
                    )

    def test_authorization_precedes_all_child_reads_and_rejected_forms(self):
        workspace_id, _, _ = self.configure(20, OdooConnectionMode.LOCAL)
        recorder = LocalDiagnosticRecorder(Path(self.temporary.name) / "diagnostics")
        self.app.state.diagnostic_recorder = recorder
        with (
            patch.object(
                self.context.workspace_access,
                "resolve",
                side_effect=AuthorizationError("denied"),
            ),
            patch.object(self.context.queries, "get") as child,
        ):
            self.assertEqual(self.bundle(workspace_id).status_code, 403)
            child.assert_not_called()
        with patch.object(self.context.workspace_access, "resolve") as access:
            self.assertEqual(self.bundle("bad-workspace").status_code, 422)
            access.assert_not_called()
        response = self.bundle()
        recorder.close()
        with ZipFile(BytesIO(response.content)) as archive:
            self.assertEqual(
                json.loads(archive.read("settings.json"))["reason"],
                "WORKSPACE_NOT_SELECTED",
            )

    def test_stage_capture_failure_is_explicit_without_losing_diagnostics(self):
        workspace_id, _, _ = self.configure(20, OdooConnectionMode.LOCAL)
        with patch.object(
            self.context.queries,
            "get_schema_governance",
            side_effect=ValueError("private-exception-text"),
        ):
            snapshot = run_local_operation_sync(
                capture_diagnostic_settings, self.context, workspace_id, self.actor
            )
        payload = create_diagnostic_bundle(
            Path(self.temporary.name),
            build_contract=PROCESS_BUILD_CONTRACT,
            settings_snapshot=snapshot,
        )
        with ZipFile(BytesIO(payload)) as archive:
            settings = json.loads(archive.read("settings.json"))
        self.assertEqual(settings["stages"][1]["status"], "unavailable")
        self.assertEqual(settings["stages"][1]["failure_type"], "ValueError")
        self.assertNotIn("private-exception-text", json.dumps(settings))

    def test_prepared_mapping_stays_distinct_from_later_saved_draft(self):
        workspace_id, selection, _ = self.configure(19, OdooConnectionMode.LOCAL)
        draft = self.save_draft(selection, workspace_id)
        old = MappingRevision(
            draft.mapping_id,
            2,
            None,
            draft.definition,
            datetime.now(UTC),
            "private-actor",
        )
        current = replace(old, version=3)
        summary = StagingRunSummary(
            str(uuid4()),
            workspace_id,
            "sha256:" + "a" * 64,
            old.mapping_id,
            2,
            1,
            1,
            StagingRunStatus.PUBLISHED,
            datetime.now(UTC),
            "private-actor",
            StagingReconciliation(2, 2, 0, 0),
            (),
        )
        with (
            patch.object(
                self.context.preflight.staging,
                "get_current_staging_summary",
                return_value=summary,
            ),
            patch.object(
                self.context.queries,
                "get_mapping_revision",
                side_effect=lambda _workspace, version=None: (
                    old if version == 2 else current
                ),
            ),
        ):
            snapshot = run_local_operation_sync(
                capture_diagnostic_settings, self.context, workspace_id, self.actor
            )
        settings = json.loads(snapshot.bundle_members()[0]["settings.json"])
        self.assertEqual(
            settings["stages"][2]["settings"]["confirmed_mapping"]["version"], 3
        )
        self.assertEqual(
            settings["stages"][3]["settings"]["mapping_used"]["version"], 2
        )
        self.assertNotIn("private-actor", json.dumps(settings))

    def test_complete_capture_has_bounded_database_owners_and_sql(self):
        workspace_id, selection, _ = self.configure(20, OdooConnectionMode.REMOTE)
        self.save_draft(selection, workspace_id)
        with (
            collect_duckdb_request_timings() as timings,
            count_duckdb_statements() as statements,
        ):
            snapshot = run_local_operation_sync(
                capture_diagnostic_settings, self.context, workspace_id, self.actor
            )
        self.assertTrue(all(stage.status == "captured" for stage in snapshot.stages))
        # Registry, DataVersion and workspace metadata each retain their own
        # database owner. The following exact budget is checked on repeats too.
        self.assertLessEqual(timings.connection_count, 4)
        self.assertLessEqual(len(statements), 250)
        self.assertLessEqual(timings.schema_check_count, 12)
