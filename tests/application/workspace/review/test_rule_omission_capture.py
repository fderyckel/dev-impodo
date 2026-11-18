"""Capture rule and required-value omissions before planning any original reads."""

from __future__ import annotations

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
    ConstantBusinessReference,
    ConstantReferenceComponent,
    RelationshipMapping,
    RelationshipResolver,
    RelationshipValueSource,
    ResolverOrigin,
    ScalarFieldMapping,
)
from impodo.domain.preflight.fields_set_aside import (
    FieldSetAsideEvidence,
    FieldSetAsideReason,
)
from impodo.domain.preparation.staging_contracts import CanonicalLineage
from impodo.domain.shared.access import CapabilityAuthorizationPolicy
from impodo.domain.shared.models import PreparedRecord, canonical_json_bytes
from impodo.domain.workspace.workbench import DataClassification, SourceMode
from tests.application.workspace.review import test_original_values as sources

_HASH = "sha256:" + "a" * 64
_RESULT = "sha256:" + "c" * 64
_TRACE = "sha256:" + "b" * 64
_WORKSPACE = "11111111-1111-4111-8111-111111111111"


class RuleOmissionCaptureTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.artifacts = LocalArtifactStore(Path(temporary.name))
        self.fields = FieldSetAsideEvidence(
            (), {}, {("Customers", "region"): ("column:region",)}, {},
            frozenset({"Customers"}), {"Customers": "res.partner"}, (),
        )
        self.record = PreparedRecord(
            "Customers", 2, "res.partner", ("C01",), ("C01",), (),
            {"region": None}, {}, _TRACE,
        )
        self.frozen = SimpleNamespace(
            content_hash=_HASH, fields_set_aside=self.fields,
            workbook_selection=None, workbook_physical_selection_hash=_HASH,
            workbook_mappings=(), workbook_lineage={}, workbook_derived_plan=None,
            prepared=SimpleNamespace(records=(self.record,)),
            normalization=SimpleNamespace(run_id="normalization", content_hash=_HASH),
            captured_schema=None, dataset_labels={}, workbook_omitted_rows=(),
            workbook_mapping_rules={},
        )
        self.report = SimpleNamespace(
            workspace_id=_WORKSPACE, run_id=str(uuid4()), frozen_input_hash=_HASH,
            result_hash=_RESULT, manifest_hash="", issue_counts=(),
        )
        self.normalization = SimpleNamespace(content_hash=_HASH, groups=(), effects=())
        self.service = PreflightService(
            staging=MagicMock(), quality=MagicMock(), normalization=MagicMock(),
            mappings=MagicMock(), workspaces=MagicMock(), sources=MagicMock(),
            preflight=MagicMock(), artifacts=self.artifacts,
            authorization=CapabilityAuthorizationPolicy(),
        )
        self.service.current_report = MagicMock(return_value=self.report)
        self.service.workspaces.get.return_value = SimpleNamespace(
            source_mode=SourceMode.FILE, data_classification=DataClassification.INTERNAL
        )
        self.service._load_frozen_input = MagicMock(return_value=self.frozen)
        self.service.normalization.get_normalization_evaluation.return_value = self.normalization
        self.manifest = {
            "semantic_hash": _RESULT,
            "preflight_evidence": {
                "frozen_input_hash": _HASH, "normalization_content_hash": _HASH,
            },
            "decisions": [{
                "dataset": "Customers", "source_row": 2, "source_trace_id": _TRACE,
                "classification": "CREATE", "issues": [],
            }],
        }
        self.save()

    def save(self) -> None:
        content = canonical_json_bytes(self.manifest) + b"\n"
        self.artifacts.write_report(_WORKSPACE, self.report.run_id, MANIFEST_NAME, content)
        self.report.manifest_hash = "sha256:" + sha256(content).hexdigest()

    def capture(self):
        def plan(**kwargs):
            return kwargs["fields"], ()

        with patch(
            "impodo.application.preflight_service.plan_workbook_original_values",
            side_effect=plan,
        ) as planner:
            evidence = self.service.review_workbook_evidence(_WORKSPACE, self.report.run_id)
        return evidence, planner

    def assert_captured_before_planning(self, expected_reason) -> None:
        evidence, planner = self.capture()
        findings = planner.call_args.kwargs["fields"].findings
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].reason, expected_reason)
        self.assertEqual(findings[0].source_trace_id, _TRACE)
        self.assertFalse(findings[0].comparison_issue)
        self.assertEqual(evidence.comparison_fields.manifest_hash, self.report.manifest_hash)
        self.assertEqual(evidence.comparison_fields.result_hash, _RESULT)
        self.assertEqual(self.service.sources.mock_calls, [])

    def test_required_on_create_is_captured_before_source_planning(self) -> None:
        self.report.issue_counts = (SimpleNamespace(technical_code="REQUIRED_ON_CREATE_MISSING"),)
        self.manifest["decisions"][0].update(
            classification="BLOCKED",
            issues=[{
                "code": "REQUIRED_ON_CREATE_MISSING", "field": "region",
                "message": "The required region is missing.",
            }],
        )
        self.save()
        self.assert_captured_before_planning(FieldSetAsideReason.MISSING)

    def test_null_policy_is_captured_without_issue_counts(self) -> None:
        self.frozen.fields_set_aside = replace(
            self.fields, null_omissions={"Customers": ("region",)}
        )
        self.assert_captured_before_planning(FieldSetAsideReason.RULE)

    def test_saved_normalization_blanking_is_captured_without_issue_counts(self) -> None:
        self.normalization.groups = (
            SimpleNamespace(group_id="group", name="Clean region", explanation="Saved blanking rule."),
        )
        self.normalization.effects = (
            SimpleNamespace(
                eligible=True, group_id="group", row_id=_TRACE, dataset="Customers",
                source_row=2, target_field="region", before="Original region", after="—",
            ),
        )
        self.assert_captured_before_planning(FieldSetAsideReason.RULE)

    def test_all_generic_omission_paths_create_one_bounded_original_request(self) -> None:
        selection = sources._selection()
        repository = sources._Sources(selection, (sources._snapshot(selection),))
        self.service.sources = repository
        self.frozen.workbook_selection = selection
        self.frozen.workbook_mappings = (
            sources._mapping(selection, ScalarFieldMapping("region", sources._KEY)),
        )
        self.frozen.workbook_lineage = {_TRACE: CanonicalLineage(
            _HASH, _HASH, _HASH, _HASH, None, "Customers", 2,
            selection.datasets[0].dataset_id, (2,), {"region": (sources._KEY,)},
            field_input_keys={"region": (sources._KEY,)},
        )}
        base_fields = replace(
            self.fields, source_keys={("Customers", "region"): (sources._KEY,)}
        )
        for kind in ("required", "null-policy", "normalization"):
            with self.subTest(kind=kind):
                repository.calls.clear()
                self.report.issue_counts = ()
                self.normalization.groups = ()
                self.normalization.effects = ()
                self.frozen.fields_set_aside = base_fields
                decision = self.manifest["decisions"][0]
                decision.update(classification="CREATE", issues=[])
                if kind == "required":
                    self.report.issue_counts = (
                        SimpleNamespace(technical_code="REQUIRED_ON_CREATE_MISSING"),
                    )
                    decision.update(classification="BLOCKED", issues=[{
                        "code": "REQUIRED_ON_CREATE_MISSING", "field": "region",
                        "message": "The required region is missing.",
                    }])
                elif kind == "null-policy":
                    self.frozen.fields_set_aside = replace(
                        base_fields, null_omissions={"Customers": ("region",)}
                    )
                else:
                    self.normalization.groups = (SimpleNamespace(
                        group_id="group", name="Clean region", explanation="Saved blanking rule."
                    ),)
                    self.normalization.effects = (SimpleNamespace(
                        eligible=True, group_id="group", row_id=_TRACE, dataset="Customers",
                        source_row=2, target_field="region", before="Original region", after="",
                    ),)
                self.save()
                evidence = self.service.review_workbook_evidence(_WORKSPACE, self.report.run_id)
                self.assertEqual(len(evidence.original_source_requests), 1)
                request = evidence.original_source_requests[0]
                self.assertEqual(len(request.cells), 1)
                self.assertEqual(request.cells[0].source_row, 2)
                self.assertEqual(request.cells[0].source_key, sources._KEY)
                self.assertEqual(request.targets[0].occurrence.source_trace_id, _TRACE)
                self.assertEqual(
                    repository.calls, [("selection", _WORKSPACE), ("snapshots", _WORKSPACE)]
                )

    def test_failed_constant_relationship_is_known_to_have_no_source_cell(self) -> None:
        selection = sources._selection()
        self.frozen.workbook_selection = selection
        relationship = RelationshipMapping(
            "region", "many2one", (),
            RelationshipResolver(ResolverOrigin.TARGET_CATALOG, model="res.country.state"),
            value_source=RelationshipValueSource.CONSTANT_EXISTING,
            constant_reference=ConstantBusinessReference((ConstantReferenceComponent("code", "WEST"),)),
        )
        self.frozen.workbook_mappings = (
            replace(sources._mapping(selection), fields=(), relationships=(relationship,)),
        )
        self.frozen.fields_set_aside = replace(self.fields, source_keys={("Customers", "region"): ()})
        self.frozen.workbook_lineage = {_TRACE: CanonicalLineage(
            _HASH, _HASH, _HASH, _HASH, None, "Customers", 2,
            selection.datasets[0].dataset_id, (2,), {"region": ()},
        )}
        self.report.issue_counts = (SimpleNamespace(technical_code="REFERENCE_NOT_FOUND"),)
        self.manifest["decisions"][0].update(classification="BLOCKED", issues=[{
            "code": "REFERENCE_NOT_FOUND", "field": "region", "message": "The configured region was not found.",
        }])
        self.save()
        evidence = self.service.review_workbook_evidence(_WORKSPACE, self.report.run_id)
        (finding,) = evidence.fields_set_aside.findings
        self.assertTrue(finding.source_inputs_known)
        self.assertEqual(finding.source_inputs, ())
        self.assertEqual(evidence.original_source_requests, ())
        self.assertEqual(self.service.sources.mock_calls, [])

    def test_no_applicable_saved_omission_does_not_read_manifest(self) -> None:
        with patch.object(
            self.artifacts, "materialize_report", side_effect=AssertionError("unneeded read")
        ):
            evidence, _planner = self.capture()
        self.assertIsNone(evidence.comparison_fields)
        self.assertEqual(evidence.fields_set_aside.findings, ())

    def test_rule_manifest_substitution_fails_before_source_planning(self) -> None:
        self.frozen.fields_set_aside = replace(
            self.fields, null_omissions={"Customers": ("region",)}
        )
        with self.artifacts.materialize_report(_WORKSPACE, self.report.run_id, MANIFEST_NAME) as path:
            path.write_bytes(path.read_bytes() + b" ")
        with (
            patch("impodo.application.preflight_service.plan_workbook_original_values") as planner,
            self.assertRaisesRegex(ReadinessError, "could not be verified"),
        ):
            self.service.review_workbook_evidence(_WORKSPACE, self.report.run_id)
        planner.assert_not_called()
        self.assertEqual(self.service.sources.mock_calls, [])

    def test_normalization_effect_requires_exact_manifest_rule_binding(self) -> None:
        self.manifest["preflight_evidence"]["normalization_content_hash"] = _RESULT
        self.save()
        self.normalization.groups = (
            SimpleNamespace(group_id="group", name="Clean region", explanation="Saved blanking rule."),
        )
        self.normalization.effects = (
            SimpleNamespace(
                eligible=True, group_id="group", row_id=_TRACE, dataset="Customers",
                source_row=2, target_field="region", before="Original region", after="",
            ),
        )
        with (
            patch("impodo.application.preflight_service.plan_workbook_original_values") as planner,
            self.assertRaisesRegex(ReadinessError, "could not be verified"),
        ):
            self.service.review_workbook_evidence(_WORKSPACE, self.report.run_id)
        planner.assert_not_called()
        self.assertEqual(self.service.sources.mock_calls, [])


if __name__ == "__main__":
    unittest.main()
