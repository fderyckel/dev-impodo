"""Errors shared by the governed source, schema, and mapping workspaces."""

from .projects import ProjectError


class WorkspaceError(ProjectError):
    """Raised when a governed workspace transition is invalid."""
