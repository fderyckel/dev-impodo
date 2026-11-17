"""Verify saved comparison bindings before requesting any original source cells."""

from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

from impodo.adapters.artifacts.local_store import LocalArtifactStore
from impodo.application.preflight_service import MANIFEST_NAME, PreflightService
from impodo.domain.errors import ReadinessError
from impodo.domain.mapping.contracts import (
    DatasetMapping,
    ReferenceKeyMapping,
    RelationshipMapping,
    RelationshipResolver,
    ResolverOrigin,
)
from impodo.domain.preflight.fields_set_aside import FieldSetAsideEvidence
from impodo.domain.preflight.original_values import OriginalSourceTarget
from impodo.domain.shared.access import CapabilityAuthorizationPolicy
from impodo.domain.shared.models import PreparedRecord, canonical_json_bytes
from impodo.domain.workspace.workbench import DataClassification, SourceMode
from tests.application.workspace.review import test_original_values as sources

_HASH = "sha256:" + "a" * 64
_RESULT = "sha256:" + "c" * 64
_TRACE = "sha256:" + "b" * 64
_WORKSPACE = "11111111-1111-4111-8111-111111111111"


class ComparisonWorkbookEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.artifacts = LocalArtifactStore(Path(self.temporary.name))
        self.selection = sources._selection()
        self.sources = sources._Sources(
            self.selection, (sources._snapshot(self.selection),)
        )
        self.record = PreparedRecord(
            "Customers",
            2,
            "res.partner",
            ("C-01",),
            ("C-01",),
            (),
            {"name": "Customer"},
            {"contact_id": None},
            _TRACE,
        )
        mapping = DatasetMapping(
            self.selection.datasets[0].dataset_id,
            "res.partner",
            relationships=(
                RelationshipMapping(
                    "contact_id",
                    "many2one",
                    (sources._KEY,),
                    RelationshipResolver(
                        ResolverOrigin.TARGET_CATALOG,
                        model="res.partner",
                        key_mappings=(ReferenceKeyMapping(sources._KEY, "ref"),),
                    ),
                ),
            ),
        )
        self.fields = FieldSetAsideEvidence(
            (),
            {},
            {("Customers", "contact_id"): (sources._KEY,)},
            {},
            frozenset(),
            {"Customers": "res.partner"},
            (),
        )
        self.frozen = SimpleNamespace(
            content_hash=_HASH,
            fields_set_aside=self.fields,
            workbook_selection=self.selection,
            workbook_physical_selection_hash=_HASH,
            workbook_mappings=(mapping,),
            prepared=SimpleNamespace(records=(self.record,)),
            normalization=SimpleNamespace(run_id="normalization", content_hash=_HASH),
            captured_schema=None,
            dataset_labels={},
            workbook_omitted_rows=(),
            workbook_mapping_rules={},
        )
        self.report = SimpleNamespace(
            workspace_id=_WORKSPACE,
            run_id=str(uuid4()),
            frozen_input_hash=_HASH,
            result_hash=_RESULT,
            manifest_hash="",
            issue_counts=(SimpleNamespace(technical_code="REFERENCE_NOT_FOUND"),),
        )
        self.workspace = SimpleNamespace(
            source_mode=SourceMode.FILE, data_classification=DataClassification.INTERNAL
        )
        self.service = PreflightService(
            staging=MagicMock(),
            quality=MagicMock(),
            normalization=MagicMock(),
            mappings=MagicMock(),
            workspaces=MagicMock(),
            sources=self.sources,
            preflight=MagicMock(),
            artifacts=self.artifacts,
            authorization=CapabilityAuthorizationPolicy(),
        )
        self.service.current_report = MagicMock(return_value=self.report)
        self.service.workspaces.get.return_value = self.workspace
        self.service._load_frozen_input = MagicMock(return_value=self.frozen)
        self.service.normalization.get_normalization_evaluation.return_value = (
            SimpleNamespace(
                content_hash=_HASH,
                groups=(),
                effects=(),
            )
        )
        self.manifest = {
            "semantic_hash": _RESULT,
            "preflight_evidence": {"frozen_input_hash": _HASH},
            "decisions": [
                {
                    "dataset": "Customers",
                    "source_row": 2,
                    "source_trace_id": _TRACE,
                    "classification": "BLOCKED",
                    "issues": [
                        {
                            "code": "REFERENCE_NOT_FOUND",
                            "field": "contact_id",
                            "message": "The related record cannot be found.",
                        }
                    ],
                }
            ],
        }
        self.save()

    def save(self):
        content = canonical_json_bytes(self.manifest) + b"\n"
        self.artifacts.write_report(
            _WORKSPACE, self.report.run_id, MANIFEST_NAME, content
        )
        self.report.manifest_hash = "sha256:" + sha256(content).hexdigest()

    def capture(self):
        return self.service.review_workbook_evidence(_WORKSPACE, self.report.run_id)

    def test_verified_reference_findings_join_one_original_request(self):
        evidence = self.capture()
        self.assertEqual(
            evidence.comparison_fields.manifest_hash, self.report.manifest_hash
        )
        self.assertEqual(evidence.comparison_fields.result_hash, _RESULT)
        self.assertEqual(len(evidence.fields_set_aside.findings), 1)
        self.assertTrue(evidence.fields_set_aside.findings[0].comparison_issue)
        self.assertEqual(
            evidence.original_source_requests[0].targets,
            (OriginalSourceTarget(2, sources._KEY, "contact_id", (sources._KEY,)),),
        )
        self.assertEqual(
            self.sources.calls, [("selection", _WORKSPACE), ("snapshots", _WORKSPACE)]
        )

    def test_irrelevant_restricted_and_unavailable_field_paths_do_not_read_manifest(
        self,
    ):
        for case in ("no-issues", "restricted", "no-fields"):
            with self.subTest(case=case):
                self.report.issue_counts = (
                    ()
                    if case == "no-issues"
                    else (SimpleNamespace(technical_code="REFERENCE_NOT_FOUND"),)
                )
                self.workspace.data_classification = (
                    DataClassification.RESTRICTED
                    if case == "restricted"
                    else DataClassification.INTERNAL
                )
                self.frozen.fields_set_aside = (
                    None if case == "no-fields" else self.fields
                )
                with patch.object(
                    self.artifacts,
                    "materialize_report",
                    side_effect=AssertionError("unneeded manifest read"),
                ):
                    evidence = self.capture()
                self.assertIsNone(evidence.comparison_fields)
                self.assertEqual(evidence.original_source_requests, ())
                self.assertEqual(self.sources.calls, [])

    def test_tampered_manifest_bytes_stop_before_source_reads(self):
        with self.artifacts.materialize_report(
            _WORKSPACE, self.report.run_id, MANIFEST_NAME
        ) as path:
            path.write_bytes(path.read_bytes() + b" ")
        with self.assertRaisesRegex(ReadinessError, "could not be verified"):
            self.capture()
        self.assertEqual(self.sources.calls, [])

    def test_wrong_result_frozen_input_and_decision_coordinates_stop_before_sources(
        self,
    ):
        original = json.dumps(self.manifest)
        for key, value in (
            ("result", "sha256:" + "d" * 64),
            ("input", "sha256:" + "d" * 64),
            ("dataset", "Other"),
            ("source_row", 3),
            ("source_trace_id", "other"),
        ):
            with self.subTest(key=key):
                self.manifest = json.loads(original)
                if key == "result":
                    self.manifest["semantic_hash"] = value
                elif key == "input":
                    self.manifest["preflight_evidence"]["frozen_input_hash"] = value
                else:
                    self.manifest["decisions"][0][key] = value
                self.save()
                with self.assertRaisesRegex(ReadinessError, "could not be verified"):
                    self.capture()
                self.assertEqual(self.sources.calls, [])

    def test_valid_fieldless_identity_case_marks_coverage_without_source_requests(self):
        self.manifest["decisions"][0]["issues"][0].update(
            code="REFERENCE_CASE_MISMATCH_REVIEW_REQUIRED", field=None
        )
        self.save()
        evidence = self.capture()
        self.assertIsNotNone(evidence.comparison_fields)
        self.assertEqual(evidence.fields_set_aside.findings, ())
        self.assertEqual(evidence.original_source_requests, ())
        self.assertEqual(self.sources.calls, [])

    def test_scalar_comparison_target_is_reported_without_authorizing_its_source(self):
        self.frozen.workbook_mappings = (sources._mapping(self.selection),)
        self.frozen.fields_set_aside = replace(
            self.fields, source_keys={("Customers", "name"): (sources._KEY,)}
        )
        self.manifest["decisions"][0]["issues"][0]["field"] = "name"
        self.save()
        evidence = self.capture()
        self.assertEqual(len(evidence.fields_set_aside.findings), 1)
        self.assertEqual(evidence.original_source_requests, ())
        self.assertEqual(self.sources.calls, [])
