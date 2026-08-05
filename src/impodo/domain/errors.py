"""Domain errors shared by preparation and preflight workflows."""

from ..workspace_errors import WorkspaceError


class ReadinessError(WorkspaceError):
    """Raised when current project evidence cannot be processed safely."""
