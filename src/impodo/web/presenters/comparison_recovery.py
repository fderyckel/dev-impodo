"""Present one explicit recovery action for a failed Odoo comparison."""

from __future__ import annotations

from dataclasses import dataclass

from ...application.odoo_read_failures import (
    OdooReadFailure,
    OdooReadFailureCode,
    OdooReadRecoveryKind,
)


@dataclass(frozen=True, slots=True)
class ComparisonRecoveryView:
    """Carry safe business wording and one route-owned next action."""

    code: str
    eyebrow: str
    title: str
    message: str
    action_kind: str
    action_label: str
    action_href: str
    credential_label: str
    credential_required: bool
    support_code: str
    support_reference: str

    @property
    def asks_for_credential(self) -> bool:
        """Return whether this failure is specifically repaired with a key."""

        return self.action_kind == "credential"


_COPY: dict[OdooReadFailureCode, tuple[str, str, str]] = {
    OdooReadFailureCode.READ_KEY_MISSING: (
        "Odoo access needed",
        "Enter the Odoo read key",
        "Impodo does not have a read-only key for this Odoo target.",
    ),
    OdooReadFailureCode.READ_KEY_REJECTED: (
        "Odoo access needs attention",
        "Replace the Odoo read key",
        "Odoo rejected the saved read-only key.",
    ),
    OdooReadFailureCode.READ_ACCESS_MISSING: (
        "Odoo access needs attention",
        "Use a key with the required read access",
        "The authenticated Odoo user cannot read all records needed for this comparison.",
    ),
    OdooReadFailureCode.CONNECTION_DETAILS_INVALID: (
        "Odoo connection needs attention",
        "Review the Odoo connection details",
        "The Odoo web address or database name is not valid for this project.",
    ),
    OdooReadFailureCode.TARGET_UNREACHABLE: (
        "Odoo could not be reached",
        "Try the comparison again",
        "Impodo could not reach Odoo. Check the network connection, then retry.",
    ),
    OdooReadFailureCode.RESPONSE_INCOMPLETE: (
        "Odoo returned incomplete data",
        "Try the comparison again",
        "Odoo did not return all information required for the read-only comparison.",
    ),
    OdooReadFailureCode.SCHEMA_EVIDENCE_MISSING: (
        "Odoo data needs attention",
        "Capture the Odoo data structure",
        "Impodo needs current Odoo field evidence before it can compare records.",
    ),
    OdooReadFailureCode.SCHEMA_EVIDENCE_STALE: (
        "Odoo data changed",
        "Refresh the Odoo data structure",
        "The saved Odoo field or access evidence no longer matches this comparison.",
    ),
    OdooReadFailureCode.REFERENCE_POLICY_MISMATCH: (
        "Field matching needs attention",
        "Review the linked field match",
        "A linked Odoo reference no longer matches the reviewed field policy.",
    ),
    OdooReadFailureCode.MAPPING_EVIDENCE_STALE: (
        "Field matching needs attention",
        "Review and submit the field matches",
        "The submitted field matches no longer cover this comparison.",
    ),
    OdooReadFailureCode.PREPARED_EVIDENCE_STALE: (
        "Prepared data needs attention",
        "Prepare and approve the data again",
        "The approved prepared-data evidence is missing or no longer current.",
    ),
    OdooReadFailureCode.LOCAL_PROFILE_REQUIRED: (
        "Local Odoo needs attention",
        "Reconnect local Odoo",
        "Impodo needs the matching local Odoo session before comparing records.",
    ),
    OdooReadFailureCode.COMPARISON_STORAGE_FAILED: (
        "Impodo could not save the comparison",
        "Review support details",
        "Impodo could not safely read or save the local comparison evidence.",
    ),
    OdooReadFailureCode.UNEXPECTED_COMPARISON_FAILURE: (
        "Impodo could not complete the comparison",
        "Review support details",
        "An unexpected problem stopped the read-only comparison.",
    ),
}


def comparison_recovery_view(
    project_id: str,
    failure: OdooReadFailure,
) -> ComparisonRecoveryView:
    """Map a classified failure to its route and safe operator wording."""

    eyebrow, title, message = _COPY[failure.code]
    recovery = failure.recovery
    action_kind = "link"
    action_href = ""
    action_label = title
    credential_label = ""
    credential_required = False
    if recovery in {
        OdooReadRecoveryKind.ENTER_READ_KEY,
        OdooReadRecoveryKind.REPLACE_READ_KEY,
        OdooReadRecoveryKind.USE_KEY_WITH_READ_ACCESS,
    }:
        action_kind = "credential"
        action_href = f"/workspaces/{project_id}/summary/compare"
        credential_required = True
        credential_label = (
            "Read-only Odoo API key"
            if recovery is OdooReadRecoveryKind.ENTER_READ_KEY
            else "Replacement read-only Odoo API key"
        )
        action_label = (
            "Save key and compare"
            if recovery is OdooReadRecoveryKind.ENTER_READ_KEY
            else "Replace key and compare"
        )
    elif recovery is OdooReadRecoveryKind.REVIEW_CONNECTION:
        action_href = f"/workspaces/{project_id}/target"
        action_label = "Review Odoo connection"
    elif recovery is OdooReadRecoveryKind.RETRY_COMPARISON:
        action_kind = "retry"
        action_href = f"/workspaces/{project_id}/summary/compare"
        action_label = "Try comparison again"
    elif recovery in {
        OdooReadRecoveryKind.CAPTURE_ODOO_DATA,
        OdooReadRecoveryKind.REFRESH_ODOO_DATA,
    }:
        action_href = f"/workspaces/{project_id}/schema"
        action_label = (
            "Capture Odoo data"
            if recovery is OdooReadRecoveryKind.CAPTURE_ODOO_DATA
            else "Refresh Odoo data"
        )
    elif recovery is OdooReadRecoveryKind.REVIEW_FIELD_MATCH:
        action_href = f"/workspaces/{project_id}/mapping"
        action_label = "Review field matches"
    elif recovery is OdooReadRecoveryKind.PREPARE_AGAIN:
        action_href = f"/workspaces/{project_id}/prepare"
        action_label = "Prepare data again"
    elif recovery is OdooReadRecoveryKind.RECONNECT_LOCAL_ODOO:
        action_kind = "local"
    else:
        action_kind = "support"
        action_label = ""
    return ComparisonRecoveryView(
        code=failure.code.value,
        eyebrow=eyebrow,
        title=title,
        message=message,
        action_kind=action_kind,
        action_label=action_label,
        action_href=action_href,
        credential_label=credential_label,
        credential_required=credential_required,
        support_code=failure.support_code,
        support_reference=failure.support_reference,
    )
