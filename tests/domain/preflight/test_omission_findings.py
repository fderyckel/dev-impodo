"""Recover only saved, occurrence-bound omissions before original-value planning."""

from __future__ import annotations

import unittest
from dataclasses import replace

from impodo.domain.preflight.fields_set_aside import (
    FieldSetAside,
    FieldSetAsideEvidence,
    FieldSetAsideReason,
)
from impodo.domain.preflight.omission_findings import (
    comparison_omission_findings,
    has_rule_omission_candidates,
)
from impodo.domain.preflight.reports import ReviewWorkbookCellEffect
from impodo.domain.shared.models import PreparedRecord

_TRACE = "sha256:" + "a" * 64
_OTHER_TRACE = "sha256:" + "b" * 64
_KEY = "column:region"


def _fields(*, nulls: bool = False) -> FieldSetAsideEvidence:
    return FieldSetAsideEvidence(
        findings=(), source_labels={},
        source_keys={("Customers", "region"): (_KEY,)},
        null_omissions={"Customers": ("region",)} if nulls else {},
        upsert_datasets=frozenset({"Customers"}),
        target_models={"Customers": "res.partner"}, coverage_gaps=(),
    )


def _record(**changes) -> PreparedRecord:
    return replace(
        PreparedRecord(
            "Customers", 19, "res.partner", ("C01",), ("C01",), (),
            {"region": None}, {}, _TRACE,
        ),
        **changes,
    )


def _decision(classification: str = "CREATE", **changes) -> dict:
    return {
        "classification": classification, "dataset": "Customers", "source_row": 19,
        "source_trace_id": _TRACE, "target_match_count": 1, "issues": [], **changes,
    }


def _issue(code: str, **changes) -> dict:
    return {"code": code, "field": "region", "message": "Saved omission.", **changes}


def _effect(**changes) -> ReviewWorkbookCellEffect:
    return replace(
        ReviewWorkbookCellEffect(
            _TRACE, "Customers", 19, "region", "Original region", "—",
            "Clean region", "The saved rule removed the region.",
        ),
        **changes,
    )


class SavedOmissionFindingTests(unittest.TestCase):
    def test_required_on_create_uses_generic_scalar_authorization(self) -> None:
        findings = comparison_omission_findings(
            (_decision("BLOCKED", issues=[_issue("REQUIRED_ON_CREATE_MISSING")]),),
            _fields(), records={_TRACE: _record()},
        )
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].reason, FieldSetAsideReason.MISSING)
        self.assertEqual(findings[0].source_trace_id, _TRACE)
        self.assertEqual(findings[0].source_keys, (_KEY,))
        self.assertFalse(findings[0].comparison_issue)

    def test_relationship_issues_keep_stricter_relationship_authorization(self) -> None:
        findings = comparison_omission_findings(
            (_decision("BLOCKED", issues=[_issue("REFERENCE_NOT_FOUND")]),),
            _fields(), records={_TRACE: _record()},
        )
        self.assertTrue(findings[0].comparison_issue)

    def test_prepared_null_without_saved_policy_or_effect_does_not_imply_omission(self) -> None:
        self.assertEqual(
            comparison_omission_findings(
                (_decision(),), _fields(), records={_TRACE: _record()}
            ),
            (),
        )

    def test_saved_policy_applies_only_to_explicit_null_scalar_or_reference(self) -> None:
        records = (
            _record(), _record(scalar_values={}, references={"region": None}),
            _record(scalar_values={}), _record(scalar_values={"region": ""}),
            _record(scalar_values={"region": "Europe"}),
        )
        for record, expected in zip(records, (1, 1, 0, 0, 0), strict=True):
            with self.subTest(record=record):
                findings = comparison_omission_findings(
                    (_decision(),), _fields(nulls=True), records={_TRACE: record}
                )
                self.assertEqual(len(findings), expected)
                if findings:
                    self.assertFalse(findings[0].comparison_issue)
                    self.assertEqual(findings[0].source_trace_id, _TRACE)

    def test_null_policy_classification_and_unchanged_match_gates(self) -> None:
        cases = (
            (_decision("CREATE"), True), (_decision("UPDATE"), True),
            (_decision("UNCHANGED"), True), (_decision("BLOCKED"), False),
            (_decision("AMBIGUOUS"), False),
            (_decision("UNCHANGED", target_match_count=0), False),
            (_decision("UNCHANGED", target_match_count=True), False),
            (_decision("UNCHANGED", target_match_count=2), False),
            (_decision("UNCHANGED", issues=[_issue("REFERENCE_TARGET_PRECEDENCE_REUSE")]), False),
        )
        for decision, applies in cases:
            with self.subTest(decision=decision):
                findings = comparison_omission_findings(
                    (decision,), _fields(nulls=True), records={_TRACE: _record()}
                )
                self.assertEqual(bool(findings), applies)
        self.assertEqual(
            comparison_omission_findings(
                (_decision("UNCHANGED"),),
                replace(_fields(nulls=True), upsert_datasets=frozenset()),
                records={_TRACE: _record()},
            ),
            (),
        )

    def test_selected_policy_requires_exact_prepared_occurrence(self) -> None:
        for changes in (
            {"source_trace_id": ""}, {"source_trace_id": _OTHER_TRACE},
            {"source_row": 20}, {"source_row": True},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                comparison_omission_findings(
                    (_decision(**changes),), _fields(nulls=True),
                    records={_TRACE: _record()},
                )

    def test_normalization_null_effect_retains_original_input_coordinate(self) -> None:
        findings = comparison_omission_findings(
            (), _fields(), records={_TRACE: _record()}, cell_effects=(_effect(),)
        )
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].source_trace_id, _TRACE)
        self.assertEqual(findings[0].target_field, "region")
        self.assertEqual(findings[0].detail, _effect().explanation)
        self.assertEqual(findings[0].reason, FieldSetAsideReason.RULE)
        self.assertFalse(findings[0].comparison_issue)

    def test_normalization_requires_saved_blanking_and_explicit_final_null(self) -> None:
        cases = (
            (_effect(before=""), _record()),
            (_effect(before="—"), _record()),
            (_effect(after="Europe"), _record()),
            (_effect(), _record(scalar_values={})),
            (_effect(), _record(scalar_values={"region": "Europe"})),
        )
        for effect, record in cases:
            with self.subTest(effect=effect, record=record):
                self.assertEqual(
                    comparison_omission_findings(
                        (), _fields(), records={_TRACE: record}, cell_effects=(effect,)
                    ),
                    (),
                )

    def test_normalization_requires_exact_saved_trace_and_geometry(self) -> None:
        for effect in (_effect(source_trace_id=_OTHER_TRACE), _effect(source_row=20)):
            with self.subTest(effect=effect), self.assertRaises(ValueError):
                comparison_omission_findings(
                    (), _fields(), records={_TRACE: _record()}, cell_effects=(effect,)
                )

    def test_policy_and_effect_do_not_duplicate_one_occurrence(self) -> None:
        findings = comparison_omission_findings(
            (_decision(),), _fields(nulls=True), records={_TRACE: _record()},
            cell_effects=(_effect(), _effect()),
        )
        self.assertEqual(len(findings), 1)

    def test_existing_rule_finding_suppresses_only_same_occurrence(self) -> None:
        existing = FieldSetAside(
            FieldSetAsideReason.RULE, "Customers", 19, (_KEY,), "region",
            "Existing saved rule.", source_trace_id=_TRACE,
        )
        fields = replace(_fields(), findings=(existing,))
        findings = comparison_omission_findings(
            (), fields,
            records={_TRACE: _record(), _OTHER_TRACE: _record(source_trace_id=_OTHER_TRACE)},
            cell_effects=(_effect(), _effect(source_trace_id=_OTHER_TRACE)),
        )
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].source_trace_id, _OTHER_TRACE)

    def test_two_traces_on_same_logical_row_stay_distinct(self) -> None:
        findings = comparison_omission_findings(
            (_decision(), _decision(source_trace_id=_OTHER_TRACE)), _fields(nulls=True),
            records={_TRACE: _record(), _OTHER_TRACE: _record(source_trace_id=_OTHER_TRACE)},
        )
        self.assertEqual(len(findings), 2)
        self.assertEqual({item.source_trace_id for item in findings}, {_TRACE, _OTHER_TRACE})

    def test_standalone_preparation_issues_keep_raw_source_key_without_inventing_target(self) -> None:
        decision = _decision(
            "BLOCKED", issues=[_issue("SOURCE_PATTERN_MISMATCH", row=None, dataset=None)]
        )
        decision.pop("source_trace_id")
        findings = comparison_omission_findings((decision,), None)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].source_keys, ("region",))
        self.assertEqual(findings[0].target_field, "")
        self.assertEqual(findings[0].source_trace_id, "")

    def test_malformed_selected_evidence_has_value_free_recovery_error(self) -> None:
        for changes in ({"source_row": "Private customer"}, {"source_row": 0}):
            with self.subTest(changes=changes), self.assertRaises(ValueError) as raised:
                comparison_omission_findings(
                    (_decision("BLOCKED", issues=[_issue("REQUIRED_ON_CREATE_MISSING")], **changes),),
                    _fields(), records={_TRACE: _record()},
                )
            self.assertNotIn("Private customer", str(raised.exception))

    def test_rule_candidates_avoid_manifest_read_without_applicable_saved_rule(self) -> None:
        self.assertFalse(has_rule_omission_candidates(_fields()))
        self.assertFalse(has_rule_omission_candidates(_fields(), (_effect(after="Europe"),)))
        self.assertTrue(has_rule_omission_candidates(_fields(nulls=True)))
        self.assertTrue(has_rule_omission_candidates(_fields(), (_effect(),)))


if __name__ == "__main__":
    unittest.main()
