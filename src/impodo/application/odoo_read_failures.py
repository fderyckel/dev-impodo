"""Classify Odoo read failures independently from browser wording."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import re

from ..artifacts import ArtifactStoreError
from ..connectors import (
    ConnectorAuthenticationError,
    ConnectorAuthorizationError,
    ConnectorConfigurationError,
    ConnectorError,
    ConnectorIncompleteResultError,
    ConnectorTransportError,
)
from ..domain.errors import ReadinessError
from ..secrets import SecretStoreError
from ..workspace_errors import WorkspaceDatabaseBusyError


class OdooReadFailureCode(StrEnum):
    """Give each known comparison failure a stable machine identity."""

    READ_KEY_MISSING = "ODOO_READ_KEY_MISSING"
    READ_KEY_REJECTED = "ODOO_READ_KEY_REJECTED"
    READ_ACCESS_MISSING = "ODOO_READ_ACCESS_MISSING"
    CONNECTION_DETAILS_INVALID = "ODOO_CONNECTION_DETAILS_INVALID"
    TARGET_UNREACHABLE = "ODOO_TARGET_UNREACHABLE"
    RESPONSE_INCOMPLETE = "ODOO_RESPONSE_INCOMPLETE"
    SCHEMA_EVIDENCE_MISSING = "ODOO_SCHEMA_EVIDENCE_MISSING"
    SCHEMA_EVIDENCE_STALE = "ODOO_SCHEMA_EVIDENCE_STALE"
    REFERENCE_POLICY_MISMATCH = "REFERENCE_POLICY_MISMATCH"
    MAPPING_EVIDENCE_STALE = "MAPPING_EVIDENCE_STALE"
    PREPARED_EVIDENCE_STALE = "PREPARED_EVIDENCE_STALE"
    LOCAL_PROFILE_REQUIRED = "LOCAL_ODOO_PROFILE_REQUIRED"
    COMPARISON_STORAGE_FAILED = "COMPARISON_STORAGE_FAILED"
    UNEXPECTED_COMPARISON_FAILURE = "UNEXPECTED_COMPARISON_FAILURE"


class RecoveryOwner(StrEnum):
    """Name the workflow stage that owns the next operator action."""

    ODOO_DATA = "ODOO_DATA"
    MATCH_DATA = "MATCH_DATA"
    PREPARE_DATA = "PREPARE_DATA"
    FINAL_REVIEW = "FINAL_REVIEW"
    SUPPORT = "SUPPORT"


class OdooReadRecoveryKind(StrEnum):
    """Describe the action without coupling it to a route or label."""

    ENTER_READ_KEY = "ENTER_READ_KEY"
    REPLACE_READ_KEY = "REPLACE_READ_KEY"
    USE_KEY_WITH_READ_ACCESS = "USE_KEY_WITH_READ_ACCESS"
    REVIEW_CONNECTION = "REVIEW_CONNECTION"
    RETRY_COMPARISON = "RETRY_COMPARISON"
    CAPTURE_ODOO_DATA = "CAPTURE_ODOO_DATA"
    REFRESH_ODOO_DATA = "REFRESH_ODOO_DATA"
    REVIEW_FIELD_MATCH = "REVIEW_FIELD_MATCH"
    PREPARE_AGAIN = "PREPARE_AGAIN"
    RECONNECT_LOCAL_ODOO = "RECONNECT_LOCAL_ODOO"
    VIEW_SUPPORT_DETAILS = "VIEW_SUPPORT_DETAILS"


@dataclass(frozen=True, slots=True)
class OdooReadFailure:
    """Carry safe recovery ownership and affected-object context."""

    code: OdooReadFailureCode
    owner: RecoveryOwner
    recovery: OdooReadRecoveryKind
    support_code: str
    support_reference: str = ""


class OdooReadCredentialMissingError(SecretStoreError):
    """Report an absent comparison credential, not a secret-store failure."""

    failure_code = OdooReadFailureCode.READ_KEY_MISSING.value


class OdooReadWorkflowError(ReadinessError):
    """Raise one stable evidence or workflow failure at its source boundary."""

    def __init__(
        self,
        failure_code: OdooReadFailureCode,
        message: str,
        *,
        support_reference: str = "",
    ) -> None:
        super().__init__(message)
        self.failure_code = failure_code.value
        self.support_reference = support_reference


_RECOVERY = {
    OdooReadFailureCode.READ_KEY_MISSING: (
        RecoveryOwner.FINAL_REVIEW,
        OdooReadRecoveryKind.ENTER_READ_KEY,
    ),
    OdooReadFailureCode.READ_KEY_REJECTED: (
        RecoveryOwner.FINAL_REVIEW,
        OdooReadRecoveryKind.REPLACE_READ_KEY,
    ),
    OdooReadFailureCode.READ_ACCESS_MISSING: (
        RecoveryOwner.ODOO_DATA,
        OdooReadRecoveryKind.USE_KEY_WITH_READ_ACCESS,
    ),
    OdooReadFailureCode.CONNECTION_DETAILS_INVALID: (
        RecoveryOwner.ODOO_DATA,
        OdooReadRecoveryKind.REVIEW_CONNECTION,
    ),
    OdooReadFailureCode.TARGET_UNREACHABLE: (
        RecoveryOwner.FINAL_REVIEW,
        OdooReadRecoveryKind.RETRY_COMPARISON,
    ),
    OdooReadFailureCode.RESPONSE_INCOMPLETE: (
        RecoveryOwner.FINAL_REVIEW,
        OdooReadRecoveryKind.RETRY_COMPARISON,
    ),
    OdooReadFailureCode.SCHEMA_EVIDENCE_MISSING: (
        RecoveryOwner.ODOO_DATA,
        OdooReadRecoveryKind.CAPTURE_ODOO_DATA,
    ),
    OdooReadFailureCode.SCHEMA_EVIDENCE_STALE: (
        RecoveryOwner.ODOO_DATA,
        OdooReadRecoveryKind.REFRESH_ODOO_DATA,
    ),
    OdooReadFailureCode.REFERENCE_POLICY_MISMATCH: (
        RecoveryOwner.MATCH_DATA,
        OdooReadRecoveryKind.REVIEW_FIELD_MATCH,
    ),
    OdooReadFailureCode.MAPPING_EVIDENCE_STALE: (
        RecoveryOwner.MATCH_DATA,
        OdooReadRecoveryKind.REVIEW_FIELD_MATCH,
    ),
    OdooReadFailureCode.PREPARED_EVIDENCE_STALE: (
        RecoveryOwner.PREPARE_DATA,
        OdooReadRecoveryKind.PREPARE_AGAIN,
    ),
    OdooReadFailureCode.LOCAL_PROFILE_REQUIRED: (
        RecoveryOwner.FINAL_REVIEW,
        OdooReadRecoveryKind.RECONNECT_LOCAL_ODOO,
    ),
    OdooReadFailureCode.COMPARISON_STORAGE_FAILED: (
        RecoveryOwner.SUPPORT,
        OdooReadRecoveryKind.VIEW_SUPPORT_DETAILS,
    ),
    OdooReadFailureCode.UNEXPECTED_COMPARISON_FAILURE: (
        RecoveryOwner.SUPPORT,
        OdooReadRecoveryKind.VIEW_SUPPORT_DETAILS,
    ),
}


def classify_odoo_read_failure(error: Exception) -> OdooReadFailure:
    """Map typed exceptions and stable domain codes to one recovery contract."""

    coded = getattr(error, "failure_code", "")
    try:
        code = OdooReadFailureCode(str(coded))
    except ValueError:
        if isinstance(error, ConnectorAuthenticationError):
            code = OdooReadFailureCode.READ_KEY_REJECTED
        elif isinstance(error, ConnectorAuthorizationError):
            code = OdooReadFailureCode.READ_ACCESS_MISSING
        elif isinstance(error, ConnectorConfigurationError):
            code = OdooReadFailureCode.CONNECTION_DETAILS_INVALID
        elif isinstance(error, ConnectorTransportError):
            code = OdooReadFailureCode.TARGET_UNREACHABLE
        elif isinstance(error, ConnectorIncompleteResultError):
            code = OdooReadFailureCode.RESPONSE_INCOMPLETE
        elif isinstance(
            error,
            (
                ArtifactStoreError,
                SecretStoreError,
                WorkspaceDatabaseBusyError,
                OSError,
            ),
        ):
            code = OdooReadFailureCode.COMPARISON_STORAGE_FAILED
        elif isinstance(error, ConnectorError):
            code = OdooReadFailureCode.UNEXPECTED_COMPARISON_FAILURE
        else:
            code = OdooReadFailureCode.UNEXPECTED_COMPARISON_FAILURE
    owner, recovery = _RECOVERY[code]
    support_code = code.value
    if isinstance(error, ConnectorTransportError):
        matched = re.search(r"\bHTTP\s+(\d{3})\b", str(error))
        if matched is not None:
            support_code = f"ODOO_API_HTTP_{matched.group(1)}"
    return OdooReadFailure(
        code=code,
        owner=owner,
        recovery=recovery,
        support_code=support_code,
        support_reference=str(getattr(error, "support_reference", "")),
    )
