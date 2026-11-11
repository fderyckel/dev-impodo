"""Expected user-correctable failures in governed workspace transitions.

Application services raise ``WorkspaceError`` when current evidence or a
requested transition is invalid rather than when infrastructure unexpectedly
fails. Browser routes catch it and render plain-language validation feedback;
repositories may raise it when optimistic/current-pointer checks fail.
"""

from impodo.domain.workspace.workbench import WorkspaceStateError


class WorkspaceError(WorkspaceStateError):
    """Raised for expected stale, conflicting, or incomplete workspace state."""

    def __init__(self, message: str, *, load_failure_code: str = "") -> None:
        super().__init__(message)
        self.load_failure_code = load_failure_code


class OdooModelCatalogRefreshRequired(WorkspaceError):
    """Field capture needs fresh model discovery under the current read access."""


class WorkspaceDatabaseBusyError(WorkspaceError):
    """Raised when another Impodo process temporarily owns the project store."""

    failure_code = "WORKSPACE_DATABASE_BUSY"


class SourcePreparationReadError(WorkspaceError):
    """Distinguish an unavailable reader from stale or missing source evidence."""

    def __init__(self, message: str, *, failure_code: str) -> None:
        super().__init__(message)
        self.failure_code = failure_code

