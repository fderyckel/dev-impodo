"""Preserve repeated business values while matching stable source records."""

import unittest
from pathlib import PurePath

from impodo.domain.compiler.contracts import CompiledMigrationPlan
from impodo.domain.execution.planner import plan_preflight_requirements
from impodo.domain.mapping.source_record_identity import (
    SOURCE_EXTERNAL_IDS_FIELD,
    SourceRecordIdentity,
    source_record_external_id,
)
from impodo.domain.odoo.contracts import MetadataSnapshot, RecordSnapshot
from impodo.domain.preparation.preflight import PreflightEngine
from impodo.domain.preparation.source import (
    SourceRow,
    SourceTable,
    prepare_source_tables,
)
from impodo.domain.recipe.profile import (
    DatasetSpec,
    FieldSpec,
    SourceIdentitySpec,
    SourceSpec,
    TargetIdentitySpec,
    TargetSpec,
)
from impodo.domain.shared.models import (
    FieldMetadata,
    ModelMetadata,
    TargetFingerprint,
    TargetRecord,
)

HASH = "sha256:" + "b" * 64
MODEL = "x.document.line"


def plan(*, allow_new=True, existing=None):
    return CompiledMigrationPlan(
        plan_id="source_identity", origin="profile_document", origin_hash=HASH,
        datasets=(DatasetSpec(
            name="lines", source=SourceSpec(file="lines.csv"), target=TargetSpec(model=MODEL),
            source_identity=SourceIdentitySpec(fields=("key",)),
            target_identity=TargetIdentitySpec(),
            source_record_identity=SourceRecordIdentity("legacy_lines", allow_new, existing),
            fields={"sequence": FieldSpec(source="position", type="integer")},
        ),),
    )


def prepare(current, values):
    return prepare_source_tables(current, (SourceTable(
        "lines", PurePath("lines.csv"), tuple(values[0]),
        tuple(SourceRow(number, dict(value)) for number, value in enumerate(values, 2)), HASH,
    ),), source_hashes={"lines": HASH})


def compare(current, prepared, targets=(), *, major=20, mode="REMOTE"):
    fingerprint = TargetFingerprint(HASH, mode, "fictional", f"{major}.0", "2026-11-12T00:00:00Z")
    return PreflightEngine().run(
        current, prepared,
        MetadataSnapshot(fingerprint, {MODEL: ModelMetadata(MODEL, "Document line", {"sequence": FieldMetadata("sequence", "integer")})}),
        RecordSnapshot(fingerprint, {MODEL: tuple(targets)}, {MODEL: ("sequence", SOURCE_EXTERNAL_IDS_FIELD)}),
    )


class SourceRecordIdentityTests(unittest.TestCase):
    def test_repeated_and_blank_ordinary_values_do_not_become_matching_errors(self):
        current = plan()
        prepared = prepare(current, ({"key": "a", "position": "10"}, {"key": "b", "position": "10"}, {"key": "c", "position": ""}))
        self.assertEqual([row.scalar_values["sequence"] for row in prepared.records], [10, 10, None])
        self.assertEqual(len({row.target_identity for row in prepared.records}), 3)
        for major in (19, 20):
            for mode in ("LOCAL", "REMOTE"):
                with self.subTest(major=major, mode=mode):
                    result = compare(current, prepared, major=major, mode=mode)
                    self.assertEqual([row.classification.value for row in result.decisions], ["CREATE"] * 3)

    def test_reordered_file_and_changed_sequence_keep_the_same_record(self):
        current = plan()
        first = prepare(current, ({"key": "a", "position": "10"}, {"key": "b", "position": "10"}))
        targets = tuple(TargetRecord(MODEL, number, {"sequence": 10, SOURCE_EXTERNAL_IDS_FIELD: row.target_identity}) for number, row in enumerate(first.records, 41))
        again = prepare(current, ({"key": "b", "position": "20"}, {"key": "a", "position": "10"}))
        self.assertEqual(again.records[0].target_identity, first.records[1].target_identity)
        result = compare(current, again, targets)
        self.assertEqual(sorted(row.classification.value for row in result.decisions), ["UNCHANGED", "UPDATE"])

    def test_unmatched_generated_id_requires_a_reviewed_create_decision(self):
        current = plan(allow_new=False)
        result = compare(current, prepare(current, ({"key": "a", "position": "10"},)))
        self.assertEqual(result.decisions[0].classification.value, "BLOCKED")
        self.assertIn("SOURCE_RECORD_CREATE_REVIEW_REQUIRED", {item.code for item in result.decisions[0].issues})

    def test_supplied_existing_id_must_exist_even_when_new_records_are_allowed(self):
        current = plan(existing="existing_id")
        prepared = prepare(current, ({"key": "a", "position": "10", "existing_id": "legacy.existing_41"},))
        result = compare(current, prepared)
        self.assertEqual(result.decisions[0].classification.value, "BLOCKED")
        self.assertIn("EXISTING_SOURCE_EXTERNAL_ID_NOT_FOUND", {item.code for item in result.decisions[0].issues})
        target = TargetRecord(MODEL, 41, {"sequence": 10, SOURCE_EXTERNAL_IDS_FIELD: ("legacy.existing_41",)})
        self.assertEqual(compare(current, prepared, (target,)).decisions[0].classification.value, "UNCHANGED")

    def test_blank_or_repeated_source_keys_remain_blocking(self):
        current = plan()
        prepared = prepare(current, ({"key": "a", "position": "10"}, {"key": "a", "position": "20"}, {"key": "", "position": "30"}))
        self.assertTrue(all(row.blocked for row in prepared.records))

    def test_request_keys_are_batched_and_fields_do_not_include_virtual_columns(self):
        current = plan()
        prepared = prepare(current, tuple({"key": f"key-{number}", "position": "10"} for number in range(1001)))
        requirements = plan_preflight_requirements(current, prepared.records)
        self.assertEqual(len(requirements.record_requests), 3)
        self.assertEqual([len(request.external_ids) for request in requirements.record_requests], [500, 500, 1])
        self.assertTrue(all(request.fields == ("sequence",) for request in requirements.record_requests))
        self.assertEqual(len({key for request in requirements.record_requests for key in request.external_ids}), 1001)

    def test_identity_encoding_handles_composites_unicode_and_delimiters(self):
        cases = (("a/b", "c"), ("a", "b/c"), ("é", "\"\\\n"), ("a", ""))
        identifiers = [source_record_external_id("legacy_lines", MODEL, key) for key in cases[:-1]]
        self.assertEqual(len(set(identifiers)), 3)
        self.assertNotEqual(identifiers[0], source_record_external_id("another_source", MODEL, cases[0]))
        with self.assertRaises(ValueError):
            source_record_external_id("legacy_lines", MODEL, cases[-1])

    def test_compiled_plan_round_trip_retains_the_identity_policy(self):
        current = plan(existing="existing_id")
        self.assertEqual(CompiledMigrationPlan.from_json(current.to_json()), current)

    def test_two_source_records_cannot_claim_one_existing_record(self):
        current = plan(existing="existing_id")
        prepared = prepare(current, ({"key": "a", "position": "10", "existing_id": "legacy.a"}, {"key": "b", "position": "20", "existing_id": "legacy.b"}))
        target = TargetRecord(MODEL, 41, {"sequence": 10, SOURCE_EXTERNAL_IDS_FIELD: ("legacy.a", "legacy.b")})
        self.assertTrue(all(row.classification.value == "BLOCKED" for row in compare(current, prepared, (target,)).decisions))
