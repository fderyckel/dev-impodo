"""Domain errors shared by preparation and preflight workflows."""

from ..workspace import WorkspaceError


class ReadinessError(WorkspaceError):
    """Raised when current project evidence cannot be processed safely."""
