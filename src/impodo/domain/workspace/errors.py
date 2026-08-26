"""Expected user-correctable failures in governed workspace transitions.

Application services raise ``WorkspaceError`` when current evidence or a
requested transition is invalid rather than when infrastructure unexpectedly
fails. Browser routes catch it and render plain-language validation feedback;
repositories may raise it when optimistic/current-pointer checks fail.
"""

from impodo.domain.workspace.workbench import WorkspaceStateError


class WorkspaceError(WorkspaceStateError):
    """Raised for expected stale, conflicting, or incomplete workspace state."""


class WorkspaceDatabaseBusyError(WorkspaceError):
    """Raised when another Impodo process temporarily owns the project store."""

    failure_code = "WORKSPACE_DATABASE_BUSY"

