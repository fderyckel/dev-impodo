from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch
from uuid import uuid4

from impodo.access import CapabilityAuthorizationPolicy
from impodo.application.preflight_service import (
    EXECUTION_SNAPSHOT_NAME,
    MANIFEST_NAME,
    PreflightService,
)
from impodo.artifacts import LocalArtifactStore
from impodo.connectors import SnapshotConnector, bind_snapshot_hashes
from impodo.domain.compiler import compile_profile_document
from impodo.domain.execution_snapshot import (
    ExecutionSnapshot,
    build_execution_snapshot,
)
from impodo.engine import PreflightEngine
from impodo.models import BusinessReference, Classification, LogicalReference
from impodo.planner import plan_metadata_requests, plan_record_requests
from impodo.profile import load_profile
from impodo.source import prepare_sources


ROOT = Path(__file__).resolve().parents[1]


def _execution_fixture():
    plan = compile_profile_document(
        load_profile(ROOT / "profiles/examples/golden_slice.yaml")
    )
    prepared = prepare_sources(plan, ROOT / "examples/golden")
    connector = SnapshotConnector(
        combined_path=ROOT / "fixtures/golden/target_snapshot.json"
    )
    metadata = connector.get_model_metadata(plan_metadata_requests(plan))
    records = connector.get_records(plan_record_requests(plan, prepared.records))
    metadata, records = bind_snapshot_hashes(metadata, records)
    result = PreflightEngine().run(plan, prepared, metadata, records)
    project_id = str(uuid4())
    frozen = SimpleNamespace(
        project_id=project_id,
        prepared=prepared,
        plan=plan,
        revision=SimpleNamespace(
            mapping_id=str(uuid4()),
            version=3,
            definition=SimpleNamespace(content_hash="sha256:" + "1" * 64),
        ),
        staging=SimpleNamespace(
            run_id=str(uuid4()),
            content_hash="sha256:" + "2" * 64,
        ),
        quality=SimpleNamespace(
            run_id=str(uuid4()),
            content_hash="sha256:" + "3" * 64,
        ),
        normalization=SimpleNamespace(
            run_id=str(uuid4()),
            content_hash="sha256:" + "4" * 64,
            lifecycle_version=2,
            eligible_dataset_hash="sha256:" + "5" * 64,
        ),
        content_hash="sha256:" + "6" * 64,
    )
    return frozen, result


class ExecutionSnapshotTests(unittest.TestCase):
    def test_snapshot_binds_remote_read_generation_and_probe_evidence(self) -> None:
        frozen, result = _execution_fixture()
        frozen.captured_schema = SimpleNamespace(
            read_credential_binding_hash="sha256:" + "7" * 64,
            read_principal_hash="sha256:" + "8" * 64,
            read_permission_hash="sha256:" + "9" * 64,
            read_context_hash="sha256:" + "a" * 64,
            models=(
                SimpleNamespace(name="product.template"),
                SimpleNamespace(name="res.partner"),
            ),
        )

        snapshot = build_execution_snapshot(
            preflight_run_id=str(uuid4()),
            frozen=frozen,
            result=result,
        )
        restored = ExecutionSnapshot.from_json(snapshot.to_json())

        self.assertEqual(restored.contract_version, 3)
        self.assertEqual(
            restored.read_credential_binding_hash,
            frozen.captured_schema.read_credential_binding_hash,
        )
        self.assertEqual(
            restored.read_permission_hash,
            frozen.captured_schema.read_permission_hash,
        )
        self.assertEqual(
            restored.readable_models,
            ("product.template", "res.partner"),
        )

    def test_snapshot_accounts_for_every_decision_and_only_writes_ready_rows(
        self,
    ) -> None:
        frozen, result = _execution_fixture()
        snapshot = build_execution_snapshot(
            preflight_run_id=str(uuid4()),
            frozen=frozen,
            result=result,
        )

        self.assertEqual(len(snapshot.rows), len(result.decisions))
        self.assertEqual(snapshot.counts, result.counts)
        self.assertEqual(snapshot.write_count, 7)
        for row in snapshot.rows:
            if row.disposition in {"CREATE", "UPDATE"}:
                self.assertTrue(row.fields)
            if row.disposition == "CREATE":
                self.assertRegex(
                    row.proposed_external_id,
                    r"^impodo_[0-9a-f]{12}\.[a-z0-9_]+_[0-9a-f]{24}$",
                )
            else:
                self.assertEqual(row.proposed_external_id, "")
            if row.disposition not in {"CREATE", "UPDATE"}:
                self.assertEqual(row.fields, ())

    def test_create_and_update_intentions_are_explicit_and_portable(self) -> None:
        frozen, result = _execution_fixture()
        snapshot = build_execution_snapshot(
            preflight_run_id=str(uuid4()),
            frozen=frozen,
            result=result,
        )
        create = next(
            row
            for row in snapshot.rows
            if row.dataset == "products"
            and row.business_identity[0] == "P-CREATE"
        )
        update = next(
            row
            for row in snapshot.rows
            if row.dataset == "products"
            and row.business_identity[0] == "P-UPDATE"
        )

        self.assertIn("default_code", {item.field for item in create.fields})
        self.assertEqual(
            {item.field for item in update.fields},
            {"name", "tag_ids"},
        )
        tag_intent = next(item for item in update.fields if item.field == "tag_ids")
        self.assertEqual(tag_intent.kind, "relation")
        self.assertEqual(tag_intent.relation_operation, "replace")
        text = snapshot.to_json()
        self.assertNotIn("odoo_id", text)
        self.assertNotIn("P-CREATE", create.proposed_external_id)

    def test_create_uses_reviewed_target_references_and_keeps_incoming_links(
        self,
    ) -> None:
        frozen, result = _execution_fixture()

        snapshot = build_execution_snapshot(
            preflight_run_id=str(uuid4()),
            frozen=frozen,
            result=result,
        )

        product = next(
            row
            for row in snapshot.rows
            if row.dataset == "products"
            and row.business_identity[0] == "P-CREATE"
        )
        product_intents = {item.field: item for item in product.fields}
        self.assertIsInstance(
            product_intents["company_id"].value,
            BusinessReference,
        )
        self.assertEqual(
            product_intents["company_id"].value,
            BusinessReference("res.company", ("BE",)),
        )
        self.assertEqual(
            product_intents["uom_id"].value,
            BusinessReference("uom.uom", ("UNIT",)),
        )
        self.assertEqual(
            product_intents["tag_ids"].value,
            (BusinessReference("product.tag", ("BLUE",)),),
        )

        child = next(
            row
            for row in snapshot.rows
            if row.dataset == "asset_lines"
            and row.disposition == "CREATE"
        )
        child_intents = {item.field: item for item in child.fields}
        self.assertIsInstance(child_intents["asset_id"].value, LogicalReference)
        self.assertEqual(child_intents["asset_id"].value.origin, "incoming")
        self.assertEqual(
            child_intents["product_tmpl_id"].value,
            BusinessReference("product.template", ("P-SAME",)),
        )

    def test_unresolved_target_reference_remains_fail_closed(self) -> None:
        frozen, result = _execution_fixture()
        decisions = tuple(
            replace(
                decision,
                classification=Classification.CREATE,
                target_match_count=0,
                issues=(),
            )
            if decision.business_identity == ("P-BLOCK",)
            else decision
            for decision in result.decisions
        )

        snapshot = build_execution_snapshot(
            preflight_run_id=str(uuid4()),
            frozen=frozen,
            result=replace(result, decisions=decisions),
        )

        blocked_product = next(
            row
            for row in snapshot.rows
            if row.business_identity == ("P-BLOCK",)
        )
        uom_intent = next(
            item for item in blocked_product.fields if item.field == "uom_id"
        )
        self.assertIsInstance(uom_intent.value, LogicalReference)
        self.assertEqual(uom_intent.value.origin, "target")

    def test_create_rejects_missing_target_resolution_evidence(self) -> None:
        frozen, result = _execution_fixture()
        resolutions = tuple(
            item
            for item in result.reference_resolutions
            if not (
                item.dataset == "products"
                and item.reference.origin == "target"
                and item.reference.key == ("UNIT",)
            )
        )

        with self.assertRaisesRegex(ValueError, "resolution evidence is incomplete"):
            build_execution_snapshot(
                preflight_run_id=str(uuid4()),
                frozen=frozen,
                result=replace(result, reference_resolutions=resolutions),
            )

    def test_round_trip_is_deterministic_and_detects_tampering(self) -> None:
        frozen, result = _execution_fixture()
        snapshot = build_execution_snapshot(
            preflight_run_id=str(uuid4()),
            frozen=frozen,
            result=result,
        )
        restored = ExecutionSnapshot.from_json(snapshot.to_json())
        self.assertEqual(restored.to_json(), snapshot.to_json())

        payload = json.loads(snapshot.to_json())
        payload["rows"][0]["disposition"] = "UPDATE"
        with self.assertRaisesRegex(ValueError, "row hash"):
            ExecutionSnapshot.from_json(json.dumps(payload))

    def test_result_from_other_source_bundle_is_rejected(self) -> None:
        frozen, result = _execution_fixture()
        frozen.prepared = SimpleNamespace(
            source_hashes={"other": "sha256:" + "7" * 64},
            records=frozen.prepared.records,
        )
        with self.assertRaisesRegex(ValueError, "frozen input"):
            build_execution_snapshot(
                preflight_run_id=str(uuid4()),
                frozen=frozen,
                result=result,
            )

    def test_current_snapshot_is_loaded_through_the_report_manifest_binding(
        self,
    ) -> None:
        frozen, result = _execution_fixture()
        run_id = str(uuid4())
        snapshot = build_execution_snapshot(
            preflight_run_id=run_id,
            frozen=frozen,
            result=result,
        )
        manifest_content = json.dumps(
            {
                "preflight_evidence": {
                    "execution_snapshot_hash": snapshot.semantic_hash,
                }
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8") + b"\n"
        report = SimpleNamespace(
            project_id=snapshot.project_id,
            run_id=run_id,
            mapping_id=snapshot.mapping_id,
            mapping_version=snapshot.mapping_version,
            mapping_content_hash=snapshot.mapping_content_hash,
            staging_run_id=snapshot.staging_run_id,
            staging_content_hash=snapshot.staging_content_hash,
            quality_run_id=snapshot.quality_run_id,
            quality_content_hash=snapshot.quality_content_hash,
            normalization_run_id=snapshot.normalization_run_id,
            normalization_content_hash=snapshot.normalization_content_hash,
            normalization_lifecycle_version=(
                snapshot.normalization_lifecycle_version
            ),
            eligible_dataset_hash=snapshot.eligible_dataset_hash,
            frozen_input_hash=snapshot.frozen_input_hash,
            result_hash=snapshot.preflight_result_hash,
            metadata_snapshot_hash=snapshot.metadata_snapshot_hash,
            record_snapshot_hash=snapshot.record_snapshot_hash,
            target_hash=snapshot.target_hash,
            target_database=snapshot.target_database,
            target_odoo_version=snapshot.target_odoo_version,
            target_snapshot_at=snapshot.target_snapshot_at,
            target_module_versions=snapshot.target_module_versions,
            ambiguous_count=snapshot.counts["AMBIGUOUS"],
            blocked_count=snapshot.counts["BLOCKED"],
            create_count=snapshot.counts["CREATE"],
            unchanged_count=snapshot.counts["UNCHANGED"],
            update_count=snapshot.counts["UPDATE"],
            manifest_hash="sha256:" + sha256(manifest_content).hexdigest(),
        )
        with tempfile.TemporaryDirectory() as directory:
            artifacts = LocalArtifactStore(directory)
            (Path(directory) / snapshot.project_id).mkdir()
            artifacts.write_report(
                snapshot.project_id,
                run_id,
                EXECUTION_SNAPSHOT_NAME,
                snapshot.to_json().encode("utf-8") + b"\n",
            )
            artifacts.write_report(
                snapshot.project_id,
                run_id,
                MANIFEST_NAME,
                manifest_content,
            )
            repositories = [MagicMock() for _ in range(7)]
            service = PreflightService(
                staging=repositories[0],
                quality=repositories[1],
                normalization=repositories[2],
                mappings=repositories[3],
                projects=repositories[4],
                sources=repositories[5],
                preflight=repositories[6],
                artifacts=artifacts,
                authorization=CapabilityAuthorizationPolicy(),
            )
            with patch.object(service, "current_report", return_value=report):
                loaded = service.current_execution_snapshot(snapshot.project_id)

        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.semantic_hash, snapshot.semantic_hash)


if __name__ == "__main__":
    unittest.main()
