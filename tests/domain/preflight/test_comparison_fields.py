"""Keep comparison field projection exact, deterministic, and value-free on failure."""

from __future__ import annotations

import unittest
from dataclasses import replace

from impodo.domain.preflight.comparison_fields import (
    COMPARISON_RELATIONSHIP_CODES,
    ComparisonFieldCoverage,
    comparison_relationship_findings,
)
from impodo.domain.preflight.fields_set_aside import (
    FieldSetAsideEvidence,
    FieldSetAsideReason,
)
from impodo.domain.shared.models import PreparedRecord

_HASH = "sha256:" + "1" * 64
_OTHER_HASH = "sha256:" + "2" * 64
_TRACE = "sha256:" + "a" * 64
_OTHER_TRACE = "sha256:" + "b" * 64
_KEY = "column:region"


def _fields() -> FieldSetAsideEvidence:
    return FieldSetAsideEvidence(
        findings=(),
        source_labels={},
        source_keys={
            ("Customers", "parent_id"): (_KEY,),
            ("Customers", "category_id"): (_KEY,),
        },
        null_omissions={},
        upsert_datasets=frozenset(),
        target_models={"Customers": "res.partner"},
        coverage_gaps=(),
    )


def _decision(
    code: str = "REFERENCE_NOT_FOUND",
    *,
    row: int = 19,
    target: str | None = "parent_id",
) -> dict:
    return {
        "classification": "BLOCKED",
        "dataset": "Customers",
        "source_row": row,
        "source_trace_id": _TRACE,
        "issues": [
            {"code": code, "field": target, "message": f"Saved {code} outcome."}
        ],
    }


def _record() -> PreparedRecord:
    return PreparedRecord(
        "Customers", 19, "res.partner", ("C01",), ("C01",), (), {}, {}, _TRACE
    )


class ComparisonFieldCoverageTests(unittest.TestCase):
    def test_coverage_binds_both_exact_sha256_hashes(self) -> None:
        coverage = ComparisonFieldCoverage(_HASH, _OTHER_HASH)
        self.assertEqual(
            (coverage.manifest_hash, coverage.result_hash), (_HASH, _OTHER_HASH)
        )

    def test_malformed_hashes_are_rejected_without_echoing_evidence(self) -> None:
        for invalid in (
            "",
            "Private customer",
            "1" * 64,
            "sha256:" + "z" * 64,
            "sha256:" + "A" * 64,
            _HASH + "\n",
        ):
            for hashes in ((invalid, _HASH), (_HASH, invalid)):
                with (
                    self.subTest(hashes=hashes),
                    self.assertRaisesRegex(ValueError, "hashes are invalid") as raised,
                ):
                    ComparisonFieldCoverage(*hashes)
                self.assertNotIn("Private customer", str(raised.exception))


class ComparisonRelationshipFindingTests(unittest.TestCase):
    def test_saved_blocked_codes_preserve_target_coordinates_and_reasons(self) -> None:
        decisions = tuple(
            _decision(code) for code in sorted(COMPARISON_RELATIONSHIP_CODES)
        )
        findings = comparison_relationship_findings(
            decisions, _fields(), records={_TRACE: _record()}
        )
        self.assertEqual(len(findings), 3)
        self.assertEqual(
            {finding.reason for finding in findings},
            {FieldSetAsideReason.UNRECOGNIZED, FieldSetAsideReason.AMBIGUOUS},
        )
        for finding in findings:
            self.assertEqual(
                (
                    finding.dataset,
                    finding.source_row,
                    finding.source_keys,
                    finding.target_field,
                ),
                ("Customers", 19, (_KEY,), "parent_id"),
            )
            self.assertTrue(finding.comparison_issue)
            self.assertEqual(finding.source_inputs, ())

    def test_other_classifications_and_issue_codes_do_not_become_findings(self) -> None:
        for classification in ("CREATE", "UPDATE", "UNCHANGED", "AMBIGUOUS"):
            decision = {
                **_decision(),
                "classification": classification,
                "source_row": "not a row",
            }
            self.assertEqual(
                comparison_relationship_findings((decision,), _fields(), records={}), ()
            )
        for code in (
            "REQUIRED_ON_CREATE_MISSING",
            "SOURCE_TYPE_INVALID",
            "REFERENCE_BLOCKED_BY_DEPENDENCY",
            "REFERENCE_TARGET_PRECEDENCE_REUSE",
        ):
            self.assertEqual(
                comparison_relationship_findings(
                    (_decision(code),), _fields(), records={}
                ),
                (),
            )

    def test_fieldless_identity_case_issue_is_not_a_relationship_finding(self) -> None:
        for target in (None, ""):
            decision = _decision(
                "REFERENCE_CASE_MISMATCH_REVIEW_REQUIRED", target=target
            )
            decision.pop("source_trace_id")
            self.assertEqual(
                comparison_relationship_findings((decision,), _fields(), records={}), ()
            )

    def test_standalone_findings_do_not_require_a_trace_or_invent_source_keys(
        self,
    ) -> None:
        decision = _decision()
        decision.pop("source_trace_id")
        findings = comparison_relationship_findings((decision,), None)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].source_keys, ())
        self.assertTrue(findings[0].comparison_issue)
        self.assertEqual(findings[0].source_inputs, ())

    def test_selected_decision_requires_exact_prepared_record_binding(self) -> None:
        invalid_decisions = (
            {**_decision(), "source_trace_id": "missing-trace"},
            {**_decision(), "source_trace_id": ""},
            {
                key: value
                for key, value in _decision().items()
                if key != "source_trace_id"
            },
            {**_decision(), "dataset": "Private customer"},
            {**_decision(), "source_row": 20},
        )
        for decision in invalid_decisions:
            with (
                self.subTest(decision=decision),
                self.assertRaises(ValueError) as raised,
            ):
                comparison_relationship_findings(
                    (decision,), _fields(), records={_TRACE: _record()}
                )
            self.assertNotIn("Private customer", str(raised.exception))
        with self.assertRaises(ValueError):
            comparison_relationship_findings(
                (_decision(),),
                _fields(),
                records={_TRACE: replace(_record(), source_trace_id=_OTHER_TRACE)},
            )

    def test_selected_source_rows_are_positive_strict_integers(self) -> None:
        for row in (None, "19", 19.0, True, 0, -1):
            with self.subTest(row=row), self.assertRaises(ValueError):
                comparison_relationship_findings(
                    ({**_decision(), "source_row": row},), _fields()
                )

    def test_selected_issue_evidence_and_optional_coordinates_must_be_valid(
        self,
    ) -> None:
        original = _decision()["issues"][0]
        invalid_issues = (
            {**original, "field": []},
            {**original, "field": 42},
            {**original, "field": "  "},
            {**original, "message": None},
            {**original, "message": []},
            {**original, "message": ""},
            {key: value for key, value in original.items() if key != "message"},
            {**original, "dataset": "Private customer"},
            {**original, "row": 20},
            {**original, "row": True},
        )
        for issue in invalid_issues:
            with self.subTest(issue=issue), self.assertRaises(ValueError) as raised:
                comparison_relationship_findings(
                    ({**_decision(), "issues": [issue]},), _fields()
                )
            self.assertNotIn("Private customer", str(raised.exception))
        valid = {**original, "dataset": "Customers", "row": 19}
        self.assertEqual(
            len(
                comparison_relationship_findings(
                    ({**_decision(), "issues": [valid]},), _fields()
                )
            ),
            1,
        )

    def test_malformed_blocked_issue_collections_raise_a_generic_error(self) -> None:
        for issues in (None, "Private customer", ["Private customer"]):
            with self.subTest(issues=issues), self.assertRaises(ValueError) as raised:
                comparison_relationship_findings(
                    ({**_decision(), "issues": issues},), _fields()
                )
            self.assertNotIn("Private customer", str(raised.exception))

    def test_findings_include_deferred_decisions_and_distinct_targets_sharing_a_cell(
        self,
    ) -> None:
        decisions = (_decision(), {**_decision(target="category_id"), "deferred": True})
        findings = comparison_relationship_findings(
            decisions, _fields(), records={_TRACE: _record()}
        )
        self.assertEqual(
            {finding.target_field for finding in findings}, {"parent_id", "category_id"}
        )
        self.assertEqual({finding.source_keys for finding in findings}, {(_KEY,)})

    def test_identical_findings_are_deduplicated_and_order_does_not_change_projection(
        self,
    ) -> None:
        decisions = (
            _decision(),
            _decision("REFERENCE_AMBIGUOUS", target="category_id"),
            _decision(),
        )
        findings = comparison_relationship_findings(decisions, _fields())
        self.assertEqual(len(findings), 2)
        self.assertEqual(
            findings,
            comparison_relationship_findings(tuple(reversed(decisions)), _fields()),
        )


if __name__ == "__main__":
    unittest.main()
