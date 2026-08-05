"""Application-layer errors presented by the browser workflow."""

from ..workspace import WorkspaceError


class ReadinessError(WorkspaceError):
    """Raised when current project evidence cannot be processed safely."""

