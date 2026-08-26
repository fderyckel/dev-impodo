from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest
from uuid import uuid4

from impodo.adapters.protected_odoo_comparison import (
    decode_odoo_comparison,
    encode_odoo_comparison,
)
from impodo.application.odoo_comparison_service import (
    ODOO_COMPARISON_CHUNK_SIZE,
    compare_pinned_odoo_row,
    plan_pinned_record_requests,
)
from impodo.domain.errors import ReadinessError
from impodo.domain.odoo_comparison import (
    OdooComparisonArtifact,
    OdooComparisonOutcome,
    OdooFieldComparisonOutcome,
)
from impodo.domain.shared.models import TargetRecord
from impodo.domain.workspace.contracts import SchemaField
from impodo.domain.workspace.errors import WorkspaceError


HASH = "sha256:" + "1" * 64
TRACE = "sha256:" + "2" * 64
NOW = datetime(2026, 8, 18, 10, 0, tzinfo=timezone.utc)


class PinnedOdooComparisonTests(unittest.TestCase):
    def setUp(self) -> None:
        self.field = SchemaField(
            name="name",
            label="Name",
            type="char",
            required=True,
            readonly=False,
            relation=None,
            relation_field=None,
            selection=(),
            stored=True,
            computed=False,
            related=False,
            translated=False,
            company_dependent=False,
        )
        self.origins = {1: (41, NOW)}

    def compare(
        self,
        *,
        proposed: str = "Alice",
        baseline: dict[str, object] | None = None,
        current: TargetRecord | None = None,
        schema_changed: bool = False,
    ):
        if baseline is None:
            baseline = {"name": "Alice"}
        if current is None:
            current = TargetRecord(
                "res.partner",
                41,
                {"name": "Alice", "write_date": NOW.isoformat()},
            )
        return compare_pinned_odoo_row(
            SimpleNamespace(
                source_row=1,
                source_trace_id=TRACE,
                scalar_values={"name": proposed},
            ),
            origins=self.origins,
            baseline=baseline,
            current=current,
            approved_fields=("name",),
            captured_fields={"name": self.field},
            schema_changed=schema_changed,
        )

    def test_classifies_unchanged_update_and_concurrent_change(self) -> None:
        unchanged = self.compare()
        update = self.compare(proposed="ALICE")
        concurrent = self.compare(
            proposed="ALICE",
            current=TargetRecord(
                "res.partner",
                41,
                {
                    "name": "Alicia",
                    "write_date": (NOW + timedelta(minutes=1)).isoformat(),
                },
            ),
        )

        self.assertEqual(unchanged.outcome, OdooComparisonOutcome.UNCHANGED)
        self.assertEqual(update.outcome, OdooComparisonOutcome.UPDATE)
        self.assertEqual(
            concurrent.outcome,
            OdooComparisonOutcome.CONCURRENT_FIELD_CHANGE,
        )
        self.assertEqual(
            concurrent.fields[0].outcome,
            OdooFieldComparisonOutcome.CONCURRENT_CHANGE,
        )

    def test_records_unrelated_change_without_overwriting_it(self) -> None:
        result = self.compare(
            proposed="ALICE",
            current=TargetRecord(
                "res.partner",
                41,
                {
                    "name": "Alice",
                    "write_date": (NOW + timedelta(minutes=1)).isoformat(),
                },
            ),
        )

        self.assertEqual(result.outcome, OdooComparisonOutcome.UPDATE)
        self.assertTrue(result.unrelated_current_change)
        self.assertEqual(result.fields[0].outcome, OdooFieldComparisonOutcome.UPDATE)

    def test_records_external_approved_field_change_without_proposing_a_write(self) -> None:
        result = self.compare(
            current=TargetRecord(
                "res.partner",
                41,
                {
                    "name": "Alicia",
                    "write_date": (NOW + timedelta(minutes=1)).isoformat(),
                },
            ),
        )

        self.assertEqual(result.outcome, OdooComparisonOutcome.UNCHANGED)
        self.assertFalse(result.unrelated_current_change)
        self.assertEqual(
            result.fields[0].outcome,
            OdooFieldComparisonOutcome.EXTERNAL_CHANGE_NOT_WRITTEN,
        )

    def test_missing_record_baseline_and_schema_fail_closed(self) -> None:
        removed = compare_pinned_odoo_row(
            SimpleNamespace(
                source_row=1,
                source_trace_id=TRACE,
                scalar_values={"name": "Alice"},
            ),
            origins=self.origins,
            baseline={"name": "Alice"},
            current=None,
            approved_fields=("name",),
            captured_fields={"name": self.field},
            schema_changed=False,
        )
        missing_baseline = self.compare(baseline={})
        schema = self.compare(schema_changed=True)

        self.assertEqual(
            removed.outcome,
            OdooComparisonOutcome.RECORD_REMOVED_OR_INACCESSIBLE,
        )
        self.assertEqual(
            missing_baseline.outcome,
            OdooComparisonOutcome.BASELINE_NOT_CAPTURED,
        )
        self.assertEqual(schema.outcome, OdooComparisonOutcome.TARGET_SCHEMA_CHANGED)

    def test_plans_only_bounded_exact_id_domains(self) -> None:
        identifiers = tuple(range(1, ODOO_COMPARISON_CHUNK_SIZE * 2 + 2))
        requests = plan_pinned_record_requests(
            "res.partner",
            ("name", "write_date"),
            identifiers,
        )

        self.assertEqual(len(requests), 3)
        self.assertEqual(requests[0].domain[0][:2], ["id", "in"])
        self.assertEqual(len(requests[0].domain[0][2]), ODOO_COMPARISON_CHUNK_SIZE)
        self.assertEqual(requests[-1].domain, (["id", "in", [1001]],))
        self.assertNotIn("name", str(requests[0].domain))
        self.assertEqual(
            plan_pinned_record_requests(
                "res.partner",
                ("name", "write_date"),
                (),
            ),
            (),
        )
        with self.assertRaisesRegex(ReadinessError, "identifiers"):
            plan_pinned_record_requests(
                "res.partner",
                ("name",),
                (2, 1),
            )

    def test_protected_artifact_round_trips_and_rejects_tampering(self) -> None:
        row = self.compare(proposed="ALICE")
        artifact = OdooComparisonArtifact.create(
            run_id=str(uuid4()),
            workspace_id=str(uuid4()),
            capture_manifest_hash=HASH,
            frozen_input_hash=HASH,
            model="res.partner",
            connection_target_hash=HASH,
            schema_scope_hash=HASH,
            read_principal_hash=HASH,
            context_hash=HASH,
            checked_at=NOW,
            rows=(row,),
        )
        restored = OdooComparisonArtifact.from_json(artifact.to_json())
        repeated = OdooComparisonArtifact.create(
            run_id=artifact.run_id,
            workspace_id=artifact.workspace_id,
            capture_manifest_hash=HASH,
            frozen_input_hash=HASH,
            model="res.partner",
            connection_target_hash=HASH,
            schema_scope_hash=HASH,
            read_principal_hash=HASH,
            context_hash=HASH,
            checked_at=NOW,
            rows=(row,),
        )
        plaintext = restored.to_json().encode("utf-8")
        binding = b"project/run/capture"
        key = b"k" * 32
        encoded = encode_odoo_comparison(
            plaintext,
            authenticated_binding=binding,
            key=key,
        )

        self.assertEqual(restored, artifact)
        self.assertEqual(repeated.content_hash, artifact.content_hash)
        self.assertEqual(
            decode_odoo_comparison(
                encoded.encrypted_bytes,
                authenticated_binding=binding,
                expected_logical_hash=encoded.logical_hash,
                expected_artifact_hash=encoded.artifact_hash,
                key=key,
            ),
            plaintext,
        )
        tampered = encoded.encrypted_bytes[:-1] + bytes(
            [encoded.encrypted_bytes[-1] ^ 1]
        )
        with self.assertRaisesRegex(WorkspaceError, "verification"):
            decode_odoo_comparison(
                tampered,
                authenticated_binding=binding,
                expected_logical_hash=encoded.logical_hash,
                expected_artifact_hash=encoded.artifact_hash,
                key=key,
            )


if __name__ == "__main__":
    unittest.main()
