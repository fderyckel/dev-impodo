"""Application boundary for durable transformation-impact evidence."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol

from ..access import Actor
from ..artifacts import ArtifactStore
from ..derived_entities import DerivedEntityPlan
from ..domain.staging.transformation_impact import (
    TransformationImpactIdentity,
    TransformationImpactReport,
    TransformationImpactRow,
    TransformationImpactSnapshot,
)
from ..inspection import SourceFileCatalog
from ..domain.mapping.artifacts import MappingRevision
from ..domain.mapping.validation.evidence import (
    MappingValidationResult,
    MappingValidationStatus,
)
from ..projects import MigrationProject
from ..workspace_contracts import MappingWorkingDraft, SourceSelection
from ..workspace_errors import WorkspaceError
from .preparation_service import stage_browser_mapping


class TransformationImpactProjectRepository(Protocol):
    def get(self, project_id: str) -> MigrationProject: ...


class TransformationImpactMappingRepository(Protocol):
    def get_mapping_revision(
        self, project_id: str, version: int | None = None
    ) -> MappingRevision | None: ...
    def get_mapping_validation(
        self, project_id: str, version: int
    ) -> MappingValidationResult | None: ...
    def get_mapping_working_draft(
        self, project_id: str
    ) -> MappingWorkingDraft | None: ...


class TransformationImpactSourceRepository(Protocol):
    def get_source_selection(self, project_id: str) -> SourceSelection | None: ...
    def get_mapping_source_selection(
        self, project_id: str
    ) -> SourceSelection | None: ...
    def get_source_catalogs(
        self, project_id: str
    ) -> tuple[SourceFileCatalog, ...]: ...


class TransformationImpactDerivedRepository(Protocol):
    def get_derived_entity_plan(
        self, project_id: str
    ) -> DerivedEntityPlan | None: ...


class TransformationImpactRepository(Protocol):
    def replace_transformation_impact_snapshot(
        self,
        project_id: str,
        identity: TransformationImpactIdentity,
        build: Callable[
            [Callable[[TransformationImpactRow], None]],
            TransformationImpactReport,
        ],
        *,
        actor: Actor,
    ) -> TransformationImpactSnapshot: ...


@dataclass(frozen=True, slots=True)
class TransformationImpactContext:
    project: MigrationProject
    revision: MappingRevision
    physical_selection: SourceSelection
    effective_selection: SourceSelection
    plan: DerivedEntityPlan | None

    @property
    def identity(self) -> TransformationImpactIdentity:
        return TransformationImpactIdentity(
            physical_selection_hash=self.physical_selection.content_hash,
            source_selection_hash=self.effective_selection.content_hash,
            mapping_content_hash=self.revision.definition.content_hash,
            schema_hash=self.revision.definition.schema_hash,
            derived_plan_hash=(self.plan.content_hash if self.plan is not None else None),
        )


class TransformationImpactService:
    """Publish one complete hash-bound impact snapshot."""

    def __init__(
        self,
        projects: TransformationImpactProjectRepository,
        mappings: TransformationImpactMappingRepository,
        sources: TransformationImpactSourceRepository,
        derived_entities: TransformationImpactDerivedRepository,
        impacts: TransformationImpactRepository,
        artifacts: ArtifactStore,
    ) -> None:
        self.projects = projects
        self.mappings = mappings
        self.sources = sources
        self.derived_entities = derived_entities
        self.impacts = impacts
        self.artifacts = artifacts

    def context(self, project_id: str) -> TransformationImpactContext:
        project = self.projects.get(project_id)
        revision = self.mappings.get_mapping_revision(project_id)
        if revision is None:
            raise WorkspaceError(
                "Validate the mapping before reviewing transformations."
            )
        validation = self.mappings.get_mapping_validation(
            project_id, revision.version
        )
        if validation is None or validation.status is MappingValidationStatus.INVALID:
            raise WorkspaceError(
                "Resolve the mapping validation findings before reviewing all "
                "transformed values."
            )
        physical = self.sources.get_source_selection(project_id)
        effective = self.sources.get_mapping_source_selection(project_id)
        if physical is None or effective is None:
            raise WorkspaceError(
                "Freeze the source datasets before reviewing transformations."
            )
        working = self.mappings.get_mapping_working_draft(project_id)
        if (
            working is not None
            and working.definition.source_selection_hash == effective.content_hash
            and working.content_hash != revision.definition.content_hash
        ):
            raise WorkspaceError(
                "Validate the current saved changes before reviewing their "
                "transformation impact."
            )
        return TransformationImpactContext(
            project=project,
            revision=revision,
            physical_selection=physical,
            effective_selection=effective,
            plan=self.derived_entities.get_derived_entity_plan(project_id),
        )

    def prepare_snapshot(
        self,
        project_id: str,
        *,
        actor: Actor,
    ) -> TransformationImpactSnapshot:
        context = self.context(project_id)
        catalogs = self.sources.get_source_catalogs(project_id)

        def evaluate(
            write_impact: Callable[[TransformationImpactRow], None],
        ) -> TransformationImpactReport:
            staged = stage_browser_mapping(
                context.project,
                context.revision.definition,
                context.physical_selection,
                context.effective_selection,
                context.plan,
                catalogs,
                self.artifacts,
                collect_transformation_impact=True,
                transformation_detail_limit=0,
                transformation_impact_sink=write_impact,
            )
            report = staged.transformation_impact
            assert report is not None
            return report

        return self.replace_snapshot(
            project_id,
            context.identity,
            evaluate,
            actor=actor,
        )

    def replace_snapshot(
        self,
        project_id: str,
        identity: TransformationImpactIdentity,
        build: Callable[
            [Callable[[TransformationImpactRow], None]],
            TransformationImpactReport,
        ],
        *,
        actor: Actor,
    ) -> TransformationImpactSnapshot:
        return self.impacts.replace_transformation_impact_snapshot(
            project_id,
            identity,
            build,
            actor=actor,
        )
