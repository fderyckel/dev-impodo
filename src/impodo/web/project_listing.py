"""Deployment-neutral browser projections for Project listing."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

UNAVAILABLE_PROJECT_MESSAGE = (
    "This project uses an older saved-data format that this version of Impodo "
    "cannot safely open. Its saved data is preserved. Create a new project to "
    "continue."
)


@dataclass(frozen=True, slots=True)
class UnavailableProjectSummary:
    """Describe one preserved Project that the current build cannot open."""

    project_id: str
    display_name: str
    previous_status: str
    updated_at: datetime
    message: str = UNAVAILABLE_PROJECT_MESSAGE


__all__ = ["UNAVAILABLE_PROJECT_MESSAGE", "UnavailableProjectSummary"]
