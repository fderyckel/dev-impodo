"""Keep Authoring workspaces and Recipe-run workspaces in separate journeys.

The route policy is intentionally small and deterministic.  It uses only the
verified workspace lineage and the owning run purpose.  It never opens a
workspace database or contacts Odoo.
"""

from __future__ import annotations

from enum import StrEnum

from fastapi import Request
from fastapi.responses import RedirectResponse
from starlette.responses import Response

from ..migration_foundation import MigrationIdentifierConfusionError
from ..domain.run.models import MigrationRunPurpose
from ..workspace_access import WorkspaceAccessContext


class WorkspaceJourney(StrEnum):
    """Name the browser journey allowed for one canonical workspace."""

    AUTHORING = "AUTHORING"
    RECIPE_RUN_SETUP = "RECIPE_RUN_SETUP"
    RECIPE_APPLICATION = "RECIPE_APPLICATION"


_SETUP_AREAS = frozenset(
    {
        "files",
        "register",
        "sources",
        "datasets",
        "derived-entities",
        "schema",
        "target",
        "local-stack",
    }
)
_APPLICATION_AREAS = frozenset(
    {
        "mapping",
        "prepare",
        "preparation",
        "resolution",
        "normalization",
        "summary",
        "load",
    }
)


def classify_workspace_journey(
    run_purpose: MigrationRunPurpose | str,
    recipe_application_id: str | None,
) -> WorkspaceJourney:
    """Return the only browser journey valid for the workspace ownership."""

    purpose = MigrationRunPurpose(run_purpose)
    if purpose is MigrationRunPurpose.AUTHORING:
        if recipe_application_id is not None:
            raise MigrationIdentifierConfusionError(
                "An Authoring workspace cannot belong to a Recipe application"
            )
        return WorkspaceJourney.AUTHORING
    if recipe_application_id is None:
        return WorkspaceJourney.RECIPE_RUN_SETUP
    return WorkspaceJourney.RECIPE_APPLICATION


def workspace_route_is_allowed(
    journey: WorkspaceJourney,
    path: str,
    workspace_id: str,
) -> bool:
    """Return whether a workspace route belongs to its user journey."""

    journey = WorkspaceJourney(journey)
    if journey is WorkspaceJourney.AUTHORING:
        return True
    parts = path.strip("/").split("/")
    if len(parts) < 3 or parts[:2] != ["workspaces", workspace_id]:
        return True
    area = parts[2]
    if journey is WorkspaceJourney.RECIPE_RUN_SETUP:
        return area in _SETUP_AREAS
    return area in _APPLICATION_AREAS


def recipe_run_home(
    access_context: WorkspaceAccessContext,
    run_purpose: MigrationRunPurpose | str,
    journey: WorkspaceJourney,
) -> str:
    """Return the canonical page that owns a non-Authoring workspace."""

    purpose = MigrationRunPurpose(run_purpose)
    journey = WorkspaceJourney(journey)
    if journey is WorkspaceJourney.RECIPE_APPLICATION:
        return (
            f"/projects/{access_context.project_id}/runs/"
            f"{access_context.migration_run_id}"
        )
    if journey is not WorkspaceJourney.RECIPE_RUN_SETUP:
        raise ValueError("An Authoring workspace has no Recipe-run home")
    if purpose is MigrationRunPurpose.TEST:
        return (
            f"/projects/{access_context.project_id}/test-runs/"
            f"{access_context.migration_run_id}/fresh-data"
        )
    return (
        f"/projects/{access_context.project_id}/production-runs/"
        f"{access_context.migration_run_id}/activate"
    )


def enforce_workspace_journey(
    request: Request,
    access_context: WorkspaceAccessContext,
    run_purpose: MigrationRunPurpose | str,
) -> Response | None:
    """Redirect a stale or crafted workspace URL to its owning Recipe run."""

    journey = classify_workspace_journey(
        run_purpose,
        access_context.recipe_application_id,
    )
    if workspace_route_is_allowed(
        journey,
        request.url.path,
        access_context.workspace_id,
    ):
        return None
    if journey is WorkspaceJourney.RECIPE_RUN_SETUP:
        message = (
            "This setup belongs to the Recipe run. Continue with Fresh data or "
            "Check Odoo from the run."
        )
    else:
        message = (
            "This work area uses saved Recipe rules. Continue with Review and "
            "load from the run."
        )
    request.session["flash"] = message
    return RedirectResponse(
        recipe_run_home(access_context, run_purpose, journey),
        status_code=303,
    )
