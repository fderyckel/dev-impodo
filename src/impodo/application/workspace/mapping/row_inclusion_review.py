"""Application boundary for Match-data row-inclusion review and confirmation."""

from __future__ import annotations

from typing import Iterable, Protocol

from impodo.application.workspace.mapping.transformation_impact import (
    TransformationImpactService,
)
from impodo.application.workspace.preparation.preparation_service import (
    stage_browser_mapping,
)
from impodo.domain.mapping.contracts import DatasetMapping
from impodo.domain.mapping.mutations import MappingVersionConflict
from impodo.domain.mapping.row_inclusion_review import (
    RowInclusionReviewConfirmation,
    RowInclusionReviewFilter,
    RowInclusionReviewIdentity,
    RowInclusionReviewPage,
    RowInclusionReviewReport,
    RowInclusionReviewSnapshot,
)
from impodo.domain.shared.access import Actor, Capability, WorkspaceAuthorizationPolicy
from impodo.domain.workspace.errors import WorkspaceError


class RowInclusionReviewRepository(Protocol):
    """Persist and page protected source-row decisions."""

    def replace_current_review(
        self,
        workspace_id: str,
        report: RowInclusionReviewReport,
        *,
        actor: Actor,
    ) -> RowInclusionReviewSnapshot: ...

    def get_current_review(
        self,
        workspace_id: str,
        identity: RowInclusionReviewIdentity,
    ) -> RowInclusionReviewSnapshot | None: ...

    def get_confirmation(
        self,
        workspace_id: str,
        snapshot_hash: str,
    ) -> RowInclusionReviewConfirmation | None: ...

    def confirm_current_review(
        self,
        workspace_id: str,
        snapshot: RowInclusionReviewSnapshot,
        *,
        actor: Actor,
        operation_id: str | None = None,
        working_draft_version: int | None = None,
        mapping_revision_version: int | None = None,
    ) -> RowInclusionReviewConfirmation: ...

    def get_review_page(
        self,
        workspace_id: str,
        snapshot_hash: str,
        filters: RowInclusionReviewFilter,
        *,
        page_size: int,
        after: int | None = None,
        before: int | None = None,
    ) -> RowInclusionReviewPage: ...


class RowInclusionReviewService:
    """Check, expose, and confirm exact source-row admission decisions."""

    def __init__(
        self,
        checked_mapping: TransformationImpactService,
        reviews: RowInclusionReviewRepository,
        authorization: WorkspaceAuthorizationPolicy,
    ) -> None:
        self.checked_mapping = checked_mapping
        self.reviews = reviews
        self.authorization = authorization

    def check_current(
        self,
        workspace_id: str,
        *,
        actor: Actor,
    ) -> RowInclusionReviewSnapshot:
        """Evaluate and publish every row governed by a matching policy."""

        self.authorization.require(
            actor,
            Capability.MAPPING_SUBMIT,
            workspace_id=workspace_id,
        )
        context = self.checked_mapping.context(workspace_id)
        staged = stage_browser_mapping(
            context.workspace_state,
            context.revision.definition,
            context.physical_selection,
            context.effective_selection,
            context.plan,
            self.checked_mapping.sources.get_source_catalogs(workspace_id),
            self.checked_mapping.artifacts,
            source_snapshots=(
                self.checked_mapping.sources.get_current_source_snapshots(
                    workspace_id
                )
            ),
        )
        report = staged.row_inclusion_review
        if report is None:
            raise WorkspaceError("The checked mapping does not limit rows")
        return self.reviews.replace_current_review(
            workspace_id,
            report,
            actor=actor,
        )

    def current(
        self,
        workspace_id: str,
        *,
        actor: Actor,
    ) -> tuple[
        RowInclusionReviewSnapshot | None,
        RowInclusionReviewConfirmation | None,
    ]:
        """Return only evidence that matches the current checked revision."""

        self.authorization.require(
            actor,
            Capability.PROTECTED_EVIDENCE_READ,
            workspace_id=workspace_id,
        )
        try:
            context = self.checked_mapping.mapping_review_context(workspace_id)
        except WorkspaceError:
            return None, None
        identity = RowInclusionReviewIdentity(
            physical_selection_hash=context.physical_selection.content_hash,
            source_selection_hash=context.effective_selection.content_hash,
            mapping_content_hash=context.revision.definition.content_hash,
            schema_hash=context.revision.definition.schema_hash,
            derived_plan_hash=(
                context.plan.content_hash if context.plan is not None else None
            ),
        )
        snapshot = self.reviews.get_current_review(workspace_id, identity)
        confirmation = (
            self.reviews.get_confirmation(workspace_id, snapshot.snapshot_hash)
            if snapshot is not None
            else None
        )
        return snapshot, confirmation

    def confirm_current(
        self,
        workspace_id: str,
        *,
        datasets: Iterable[DatasetMapping],
        expected_revision_version: int | None,
        expected_working_draft_version: int | None,
        actor: Actor,
        operation_id: str | None = None,
    ) -> RowInclusionReviewConfirmation:
        """Confirm counts only when the submitted editor state is still exact."""

        self.authorization.require(
            actor,
            Capability.MAPPING_SUBMIT,
            workspace_id=workspace_id,
        )
        context = self.checked_mapping.context(workspace_id)
        working = self.checked_mapping.mappings.get_mapping_working_draft(
            workspace_id
        )
        actual_working_version = working.version if working is not None else None
        if (
            expected_revision_version != context.revision.version
            or expected_working_draft_version != actual_working_version
        ):
            raise MappingVersionConflict(
                submitted_working_draft_version=expected_working_draft_version,
                submitted_mapping_revision_version=expected_revision_version,
                current_working_draft_version=actual_working_version,
                current_mapping_revision_version=context.revision.version,
            )
        if tuple(datasets) != context.revision.definition.datasets:
            raise WorkspaceError(
                "These rows-to-use rules changed after they were checked. "
                "Check matches again before confirming."
            )
        snapshot, _confirmation = self.current(workspace_id, actor=actor)
        if snapshot is None:
            raise WorkspaceError(
                "Check the current rows before confirming which rows to use."
            )
        return self.reviews.confirm_current_review(
            workspace_id,
            snapshot,
            actor=actor,
            operation_id=operation_id,
            working_draft_version=actual_working_version,
            mapping_revision_version=context.revision.version,
        )

    def page(
        self,
        workspace_id: str,
        filters: RowInclusionReviewFilter,
        *,
        page_size: int,
        after: int | None,
        before: int | None,
        actor: Actor,
    ) -> tuple[
        RowInclusionReviewSnapshot,
        RowInclusionReviewFilter,
        RowInclusionReviewPage,
    ]:
        """Return a current bounded page for the authenticated project member."""

        snapshot, _confirmation = self.current(workspace_id, actor=actor)
        if snapshot is None:
            raise WorkspaceError("Check the current rows before reviewing them")
        effective_filters = RowInclusionReviewFilter(
            dataset_id=(
                filters.dataset_id
                if filters.dataset_id
                in {item.dataset_id for item in snapshot.datasets}
                else ""
            ),
            outcome=filters.outcome,
            query=filters.query,
        )
        page = self.reviews.get_review_page(
            workspace_id,
            snapshot.snapshot_hash,
            effective_filters,
            page_size=page_size,
            after=after,
            before=before,
        )
        return snapshot, effective_filters, page
