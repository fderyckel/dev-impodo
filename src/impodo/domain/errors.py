"""Expected safety-gate failures in preparation and read-only preflight.

``ReadinessError`` means evidence cannot safely progress—often explicitly
before Odoo is contacted. Application services translate lower-level domain
validation errors into this type; browser routes present its message as a
recoverable workflow action rather than an internal server failure.
"""

from ..workspace_errors import WorkspaceError


class ReadinessError(WorkspaceError):
    """Raised when current evidence cannot be prepared or compared safely."""


class NormalizationReviewPolicyError(ReadinessError):
    """Expose an unsupported prepared-change policy as a recoverable failure."""

    failure_code = "NORMALIZATION_REVIEW_POLICY_UNSUPPORTED"
