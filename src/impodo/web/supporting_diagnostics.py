"""Allowlist supporting-plan failure metadata at recording and export boundaries.

Only technical schema identifiers and closed decision codes cross this boundary.
Labels, source values, formulas, dataset identities, and exception text cannot.
"""

from __future__ import annotations

import re

from fastapi import Request

from impodo.domain.workspace.supporting_errors import SupportingPlanIncompleteError
from impodo.domain.workspace.supporting_plan import SupportingPlanGap
from impodo.domain.workspace.supporting_review import (
    SupportingChoiceAction,
    SupportingReviewProblem,
    SupportingReviewRequiredError,
)

MAX_SUPPORTING_DIAGNOSTIC_ISSUES = 20
_MODEL = re.compile(r"[a-z_][a-z0-9_]*(?:\.[a-z_][a-z0-9_]*)*")
_FIELD = re.compile(r"[a-z_][a-z0-9_]*")
_REASONS = {item.value for item in SupportingPlanGap}
_CHOICES = {item.value for item in SupportingChoiceAction} | {"UNSET"}
_TRIGGERS = {
    "REQUIRED_RELATIONSHIP",
    "EXPLICIT_RELATIONSHIP",
    "REVIEWED_RELATIONSHIP",
    "INVERSE_CHILD",
    "UNREACHED_METADATA",
}


def sanitize_supporting_failure(value: object) -> dict[str, object] | None:
    """Reject unknown codes and discard all fields outside the bounded schema."""
    if isinstance(value, dict) and isinstance(value.get("failure_code"), str) and value.get("failure_code") in {
        "SUPPORTING_REVIEW_REQUIRED", "SUPPORTING_MAPPING_CONFLICT",
    }:
        problem = value.get("problem")
        if isinstance(problem, str) and problem in {item.value for item in SupportingReviewProblem}:
            return {"failure_code": value["failure_code"], "stage": "supporting_review", "problem": problem}
        return None
    if (
        not isinstance(value, dict)
        or value.get("failure_code") != "SUPPORTING_PLAN_INCOMPLETE"
    ):
        return None
    count = value.get("issue_count")
    raw_issues = value.get("issues")
    if (
        type(count) is not int
        or not 0 <= count <= 1_000_000
        or not isinstance(raw_issues, list)
    ):
        return None
    issues = []
    for raw in raw_issues[:MAX_SUPPORTING_DIAGNOSTIC_ISSUES]:
        if not isinstance(raw, dict):
            continue
        reason = raw.get("reason")
        if not isinstance(reason, str) or reason not in _REASONS:
            continue
        issue: dict[str, object] = {"reason": raw["reason"]}
        for key, pattern in (
            ("source_model", _MODEL),
            ("field", _FIELD),
            ("related_model", _MODEL),
        ):
            candidate = raw.get(key)
            if (
                isinstance(candidate, str)
                and len(candidate) <= 200
                and pattern.fullmatch(candidate)
            ):
                issue[key] = candidate
        for key, allowed in (
            ("field_type", {"many2one", "many2many", "one2many", "UNKNOWN"}),
            ("trigger", _TRIGGERS),
            ("saved_choice", _CHOICES),
        ):
            candidate = raw.get(key)
            if isinstance(candidate, str) and candidate in allowed:
                issue[key] = candidate
        for key in ("required", "used_by_mapping"):
            if isinstance(raw.get(key), bool):
                issue[key] = raw[key]
        candidates = raw.get("candidate_count")
        if type(candidates) is int and 0 <= candidates <= 1_000:
            issue["candidate_count"] = candidates
        issues.append(issue)
    payload: dict[str, object] = {
        "failure_code": "SUPPORTING_PLAN_INCOMPLETE",
        "stage": "supporting_plan",
        "issue_count": max(count, len(issues)),
        "issues": issues,
        "issues_truncated": value.get("issues_truncated") is True
        or count > len(issues) or len(raw_issues) > len(issues),
    }
    if type(value.get("odoo_major")) is int and value["odoo_major"] in {19, 20}:
        payload["odoo_major"] = value["odoo_major"]
    return payload


def set_diagnostic_supporting_failure(
    request: Request, error: SupportingPlanIncompleteError | SupportingReviewRequiredError
) -> None:
    """Attach already-loaded causes to the terminal request record, without I/O."""
    if isinstance(error, SupportingReviewRequiredError):
        request.state.diagnostic_validation_failure = sanitize_supporting_failure({
            "failure_code": error.failure_code, "problem": error.problem.value,
        })
        return
    request.state.diagnostic_validation_failure = sanitize_supporting_failure(
        {
            "failure_code": error.failure_code,
            "odoo_major": error.odoo_major,
            "issue_count": len(error.issues),
            "issues": [
                {
                    "reason": item.reason.value,
                    "source_model": item.source_model,
                    "field": item.field,
                    "related_model": item.related_model,
                    "field_type": item.field_type,
                    "trigger": item.trigger,
                    "required": item.required,
                    "used_by_mapping": item.used_by_mapping,
                    "saved_choice": item.saved_choice,
                    "candidate_count": item.candidate_count,
                }
                for item in error.issues[:MAX_SUPPORTING_DIAGNOSTIC_ISSUES]
            ],
        }
    )
