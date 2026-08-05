"""Application coordinator for the prepare-then-compare workflow."""

from __future__ import annotations

from ..access import Actor
from ..domain.preflight.reports import ReadinessReport
from .preflight_service import PreflightService, ReadinessReader
from .preparation_service import PreparationService


class ReadinessWorkflowService:
    """Coordinate preparation and read-only Odoo comparison."""

    def __init__(
        self,
        preparation: PreparationService,
        preflight: PreflightService,
    ) -> None:
        self.preparation = preparation
        self.preflight = preflight

    def compare(
        self,
        project_id: str,
        *,
        reader: ReadinessReader,
        actor: Actor,
    ) -> ReadinessReport:
        prepared = self.preparation.prepare_context(project_id, actor=actor)
        return self.preflight.compare(prepared, reader=reader, actor=actor)
