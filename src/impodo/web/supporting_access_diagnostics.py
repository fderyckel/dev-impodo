"""Describe supporting-choice recovery with closed codes and no business values."""

from fastapi import Request

from impodo.domain.odoo.compatibility import OdooOperation, assess_odoo_operation
from impodo.domain.workspace.contracts import OdooSchemaCatalog
from impodo.domain.workspace.supporting_review import SupportingReview

_TRIGGERS = {"COMPANY_REFRESH", "CHECK_MATCHES", "LINKED_VALUE_LOOKUP"}
_CHANGES = {
    "CREDENTIAL_VERSION",
    "COMPANY_CONTEXT",
    "CHECKING_USER",
    "PERMISSIONS",
    "FIELDS",
    "TARGET",
    "ODOO_VERSION",
    "REFERENCE_POLICY",
}
_OUTCOMES = {
    "STARTED",
    "UNCHANGED",
    "REVALIDATED",
    "REVIEW_REQUIRED",
    "NOT_REQUIRED",
    "SCHEMA_REVIEW_REQUIRED",
    "REFRESH_FAILED",
}


def sanitize_supporting_access(value: object) -> dict[str, object] | None:
    """Allowlist a bounded transition at recording and bundle export boundaries."""
    if not isinstance(value, dict):
        return None
    trigger, outcome = value.get("trigger"), value.get("outcome")
    if not isinstance(trigger, str) or trigger not in _TRIGGERS:
        return None
    if not isinstance(outcome, str) or outcome not in _OUTCOMES:
        return None
    changes = value.get("changes")
    if not isinstance(changes, (list, tuple)):
        return None
    result: dict[str, object] = {
        "trigger": trigger,
        "outcome": outcome,
        "changes": sorted(
            {item for item in changes[:8] if isinstance(item, str) and item in _CHANGES}
        ),
    }
    for field in ("previous_review_version", "review_version", "choice_count"):
        number = value.get(field)
        if type(number) is int and 0 <= number <= 1_000_000:
            result[field] = number
    major = value.get("odoo_major")
    if type(major) is int and major in {19, 20}:
        result["odoo_major"] = major
    return result


def record_supporting_access(
    request: Request | None,
    *,
    trigger: str,
    previous: OdooSchemaCatalog,
    current: OdooSchemaCatalog | None = None,
    before: SupportingReview | None = None,
    after: SupportingReview | None = None,
    outcome: str,
) -> None:
    """Compare already loaded inputs; never record their values or open a store."""
    if request is None:
        return
    changes = []
    candidate = (
        current.pending_refresh if current and current.pending_refresh else current
    )
    if candidate is not None:
        for name, reason in (
            ("read_credential_binding_hash", "CREDENTIAL_VERSION"),
            ("read_context_hash", "COMPANY_CONTEXT"),
            ("read_principal_hash", "CHECKING_USER"),
            ("read_permission_hash", "PERMISSIONS"),
            ("models", "FIELDS"),
            ("connection_target_hash", "TARGET"),
            ("odoo_version", "ODOO_VERSION"),
            ("policy_hash", "REFERENCE_POLICY"),
        ):
            if getattr(previous, name) != getattr(candidate, name):
                changes.append(reason)
    major = assess_odoo_operation(
        previous.odoo_version, OdooOperation.CAPTURE_SCHEMA
    ).version.major
    request.state.diagnostic_supporting_access = sanitize_supporting_access(
        {
            "trigger": trigger,
            "outcome": outcome,
            "changes": changes,
            "odoo_major": major,
            "previous_review_version": before.version if before else 0,
            "review_version": after.version
            if after
            else before.version
            if before
            else 0,
            "choice_count": len(after.choices)
            if after
            else len(before.choices)
            if before
            else 0,
        }
    )
