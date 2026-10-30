"""Check current supporting decisions at existing authorized consumer boundaries.

Readers are injected ports. Local callers retain one synchronous database scope;
server callers supply verified worker evidence without opening worker DuckDB.
Historical mappings without a saved review retain their existing meaning.
"""

from __future__ import annotations

from typing import Protocol

from impodo.application.supporting_review_service import (
    SupportingReviewRepository,
    SupportingReviewService,
    supporting_review_packet,
)
from impodo.domain.mapping.contracts import DatasetMapping
from impodo.domain.schema.governance import SchemaGovernance
from impodo.domain.workspace.contracts import OdooSchemaCatalog, SourceSelection
from impodo.domain.workspace.derived_entities import DerivedEntityPlan
from impodo.domain.workspace.errors import WorkspaceError
from impodo.domain.workspace.supporting_closure import (
    close_supporting_plan,
    supporting_mapping_conflicts,
)
from impodo.domain.workspace.supporting_review import SupportingReview
from impodo.domain.workspace.workbench import SourceMode, WorkspaceState


class SupportingWorkspaceReader(Protocol):
    def get(self, workspace_id: str) -> WorkspaceState: ...


class SupportingSourceReader(Protocol):
    def get_source_selection(self, workspace_id: str) -> SourceSelection | None: ...


class SupportingPreparationReader(Protocol):
    def get_derived_entity_plan(
        self, workspace_id: str
    ) -> DerivedEntityPlan | None: ...


class SupportingSchemaReader(Protocol):
    def get_odoo_schema_catalog(
        self, workspace_id: str
    ) -> OdooSchemaCatalog | None: ...

    def get_schema_governance(self, workspace_id: str) -> SchemaGovernance | None: ...


class SupportingPreparationPolicy(Protocol):
    """Recheck review freshness before source-row evaluation or publication."""

    def require_preparation(
        self,
        workspace_id: str,
        datasets: tuple[DatasetMapping, ...],
        schemas: SupportingSchemaReader,
        workspace: WorkspaceState,
        selection: SourceSelection,
        preparation: DerivedEntityPlan | None,
    ) -> None: ...


class SupportingMappingPolicy(Protocol):
    """Consume already-authorized schema and mapping inputs without network I/O."""

    def require_mapping(
        self,
        workspace_id: str,
        datasets: tuple[DatasetMapping, ...],
        schema: OdooSchemaCatalog,
        governance: SchemaGovernance | None,
    ) -> SupportingReview | None: ...


class SupportingMappingGuard:
    """Read a review once and only gather its metadata when one actually exists."""

    def __init__(
        self,
        workspaces: SupportingWorkspaceReader,
        sources: SupportingSourceReader,
        reviews: SupportingReviewRepository,
        preparation: SupportingPreparationReader,
    ) -> None:
        self.workspaces = workspaces
        self.sources = sources
        self.reviews = reviews
        self.preparation = preparation

    def require_mapping(
        self,
        workspace_id: str,
        datasets: tuple[DatasetMapping, ...],
        schema: OdooSchemaCatalog,
        governance: SchemaGovernance | None,
    ) -> SupportingReview | None:
        """Reject stale review or contradictory rules before publishing evidence."""
        review = self.reviews.get_supporting_review(workspace_id)
        if review is None:
            return None
        workspace = self.workspaces.get(workspace_id)
        if workspace.source_mode is SourceMode.ODOO:
            # Existing Odoo relationship scope and destination matching own the
            # full, preservation, reuse and ignored modes for this source path.
            return None
        selection = self.sources.get_source_selection(workspace_id)
        if selection is None:
            raise WorkspaceError(
                "Review supporting choices after accepting source data"
            )
        preparation = self.preparation.get_derived_entity_plan(workspace_id)
        self._require_review(
            review, datasets, workspace, selection, preparation, schema, governance
        )
        return review

    def require_preparation(
        self,
        workspace_id: str,
        datasets: tuple[DatasetMapping, ...],
        schemas: SupportingSchemaReader,
        workspace: WorkspaceState,
        selection: SourceSelection,
        preparation: DerivedEntityPlan | None,
    ) -> None:
        """Reuse the preparation operation's source, workspace and rule metadata."""
        review = self.reviews.get_supporting_review(workspace_id)
        if review is None or workspace.source_mode is SourceMode.ODOO:
            return
        schema = schemas.get_odoo_schema_catalog(workspace_id)
        if schema is None:
            raise WorkspaceError(
                "Refresh Odoo data before rechecking supporting choices"
            )
        governance = schemas.get_schema_governance(workspace_id)
        self._require_review(
            review, datasets, workspace, selection, preparation, schema, governance
        )

    @staticmethod
    def _require_review(
        review: SupportingReview,
        datasets: tuple[DatasetMapping, ...],
        workspace: WorkspaceState,
        selection: SourceSelection,
        preparation: DerivedEntityPlan | None,
        schema: OdooSchemaCatalog,
        governance: SchemaGovernance | None,
    ) -> None:
        packet = supporting_review_packet(
            workspace, selection, schema, preparation, governance
        )
        SupportingReviewService.validate_review(packet, review)
        closure = close_supporting_plan(
            schema=schema,
            datasets=packet.descriptors,
            mappings=datasets,
            choices=review.choices,
            source_hash=selection.content_hash,
            preparation_hash=preparation.content_hash if preparation else None,
        )
        if not closure.proposal.complete:
            raise WorkspaceError(
                "Review the incomplete supporting-data plan in Odoo data before checking Match data"
            )
        conflicts = supporting_mapping_conflicts(review.choices, datasets, schema)
        if conflicts:
            raise WorkspaceError(
                "Review supporting choices in Odoo data and the corresponding Match data rule: "
                + "; ".join(conflicts)
            )
