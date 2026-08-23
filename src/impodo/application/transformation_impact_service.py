"""Application boundary for durable transformation-impact evidence."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol

from ..access import Actor, AuthorizationPolicy, Capability
from ..artifacts import ArtifactStore
from ..derived_entities import DerivedEntityPlan
from ..domain.staging.transformation_impact import (
    TransformationImpactIdentity,
    TransformationImpactReport,
    TransformationImpactRow,
    TransformationImpactSnapshot,
)
from ..domain.source_snapshot import SourceSnapshot
from ..inspection import SourceFileCatalog
from ..domain.mapping.artifacts import MappingRevision
from ..domain.mapping.validation.evidence import (
    MappingValidationResult,
    MappingValidationStatus,
)
from ..workspace_state import WorkspaceState
from ..workspace_contracts import MappingWorkingDraft, SourceSelection
from ..workspace_errors import WorkspaceError
from .preparation_service import stage_browser_mapping


class TransformationImpactProjectRepository(Protocol):
    """Load project context for impact evaluation."""

    def get(self, project_id: str) -> WorkspaceState:
        """Return project policy used for protected display and ownership."""
        ...


class TransformationImpactMappingRepository(Protocol):
    """Read validated mapping evidence and detect unsaved draft drift."""

    def get_mapping_revision(
        self, project_id: str, version: int | None = None
    ) -> MappingRevision | None:
        """Return the current or requested immutable mapping revision."""
        ...
    def get_mapping_validation(
        self, project_id: str, version: int
    ) -> MappingValidationResult | None:
        """Return validation evidence required before impact evaluation."""
        ...
    def get_mapping_working_draft(
        self, project_id: str
    ) -> MappingWorkingDraft | None:
        """Return the draft used to reject unvalidated current edits."""
        ...


class TransformationImpactSourceRepository(Protocol):
    """Read physical/effective selections and materialization catalogs."""

    def get_source_selection(self, project_id: str) -> SourceSelection | None:
        """Return the physical frozen source selection."""
        ...
    def get_mapping_source_selection(
        self, project_id: str
    ) -> SourceSelection | None:
        """Return the effective selection after derived-dataset expansion."""
        ...
    def get_source_catalogs(
        self, project_id: str
    ) -> tuple[SourceFileCatalog, ...]:
        """Return inspected catalogs used to materialize source artifacts."""
        ...
    def get_current_source_snapshots(
        self, project_id: str
    ) -> tuple[SourceSnapshot, ...]:
        """Return current verified physical source snapshots."""
        ...


class TransformationImpactDerivedRepository(Protocol):
    """Read virtual-dataset rules used by impact evaluation."""

    def get_derived_entity_plan(
        self, project_id: str
    ) -> DerivedEntityPlan | None:
        """Return the current virtual-dataset plan, if present."""
        ...


class TransformationImpactRepository(Protocol):
    """Atomically replace the durable, filterable impact snapshot."""

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
    ) -> TransformationImpactSnapshot:
        """Stream a complete replacement and commit it atomically."""
        ...

    def acknowledge_transformation_rule(
        self,
        project_id: str,
        identity: TransformationImpactIdentity,
        rule_fingerprint: str,
        *,
        actor: Actor,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class TransformationImpactContext:
    """All hash-bearing inputs required to re-evaluate prepared values."""

    project: WorkspaceState
    revision: MappingRevision
    physical_selection: SourceSelection
    effective_selection: SourceSelection
    plan: DerivedEntityPlan | None

    @property
    def identity(self) -> TransformationImpactIdentity:
        """Fingerprint the exact source, mapping, schema, and derived inputs."""

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
        authorization: AuthorizationPolicy,
    ) -> None:
        self.projects = projects
        self.mappings = mappings
        self.sources = sources
        self.derived_entities = derived_entities
        self.impacts = impacts
        self.artifacts = artifacts
        self.authorization = authorization

    def context(self, project_id: str) -> TransformationImpactContext:
        """Resolve current inputs and reject invalid or unsaved mapping state."""

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
        """Re-evaluate every mapped value and atomically publish its impacts.

        Impact details are streamed into the repository while the evaluator
        builds complete aggregate counts, avoiding an unbounded in-memory list.
        """

        context = self.context(project_id)
        catalogs = self.sources.get_source_catalogs(project_id)
        source_snapshots = self.sources.get_current_source_snapshots(project_id)

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
                source_snapshots=source_snapshots,
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
        """Delegate one atomic snapshot replacement to the persistence port."""

        return self.impacts.replace_transformation_impact_snapshot(
            project_id,
            identity,
            build,
            actor=actor,
        )

    def acknowledge_rule(
        self,
        project_id: str,
        rule_fingerprint: str,
        *,
        actor: Actor,
    ) -> None:
        """Acknowledge one zero-match or overlap fact for current evidence."""

        context = self.context(project_id)
        self.authorization.require(
            actor,
            Capability.MAPPING_SUBMIT,
            project_id=project_id,
        )
        self.impacts.acknowledge_transformation_rule(
            project_id,
            context.identity,
            rule_fingerprint,
            actor=actor,
        )

