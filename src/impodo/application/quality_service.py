"""Application service for durable quality rules and evaluation."""

from __future__ import annotations

from dataclasses import dataclass

from ..access import Actor
from ..domain.mapping.artifacts import MappingRevision
from ..projects import MigrationProject
from ..quality import (
    QualityError,
    QualityRule,
    QualityRuleSet,
    QualityRun,
    QualityRunSummary,
    default_quality_ruleset,
    evaluate_quality,
)
from ..staging import StagingRunSummary
from ..staging_contracts import CanonicalStagingRun
from ..workspace_contracts import SourceSelection
from ..workspace_errors import WorkspaceError
from ..domain.errors import ReadinessError
from .readiness_ports import (
    QualityMappingRepository,
    QualityRepository,
    QualitySourceRepository,
)


@dataclass(frozen=True, slots=True)
class QualityConfigurationContext:
    project_id: str
    revision: MappingRevision
    selection: SourceSelection
    dataset_id: str
    dataset_name: str
    allowed_fields: frozenset[str]


class QualityService:
    """Manage the quality contract independently of staging and HTTP."""

    def __init__(
        self,
        mappings: QualityMappingRepository,
        sources: QualitySourceRepository,
        quality: QualityRepository,
    ) -> None:
        self.mappings = mappings
        self.sources = sources
        self.quality = quality

    def current_ruleset(self, project_id: str) -> QualityRuleSet | None:
        return self.quality.get_current_quality_ruleset(project_id)

    def current_summary(self, project_id: str) -> QualityRunSummary | None:
        return self.quality.get_current_quality_summary(project_id)

    def current_run(self, project_id: str) -> QualityRun | None:
        summary = self.current_summary(project_id)
        if summary is None:
            return None
        return self.quality.get_quality_run(project_id, summary.run_id)

    def publish_ruleset(
        self,
        project_id: str,
        ruleset: QualityRuleSet,
        *,
        actor: Actor,
    ) -> QualityRuleSet:
        return self.quality.publish_quality_ruleset(
            project_id,
            ruleset,
            actor=actor,
        )

    def configuration(
        self,
        project_id: str,
        dataset_id: str,
    ) -> QualityConfigurationContext:
        revision = self.mappings.get_mapping_revision(project_id)
        selection = self.sources.get_mapping_source_selection(project_id)
        if revision is None or selection is None:
            raise WorkspaceError(
                "Check and save the field matches before adding data checks"
            )
        working = self.mappings.get_mapping_working_draft(project_id)
        if (
            working is not None
            and working.content_hash != revision.definition.content_hash
        ):
            raise WorkspaceError(
                "Check the latest field-match changes before saving data checks"
            )
        dataset = next(
            (
                item
                for item in revision.definition.datasets
                if item.dataset_id == dataset_id
            ),
            None,
        )
        source_dataset = next(
            (item for item in selection.datasets if item.dataset_id == dataset_id),
            None,
        )
        if dataset is None or source_dataset is None:
            raise WorkspaceError("Choose a current source table")
        return QualityConfigurationContext(
            project_id=project_id,
            revision=revision,
            selection=selection,
            dataset_id=dataset_id,
            dataset_name=source_dataset.name,
            allowed_fields=frozenset(item.target_field for item in dataset.fields),
        )

    def save_manager_rules(
        self,
        context: QualityConfigurationContext,
        manager_rules: tuple[QualityRule, ...],
        *,
        actor: Actor,
    ) -> QualityRuleSet:
        current = self.quality.get_current_quality_ruleset(context.project_id)
        combined = list(manager_rules)
        if (
            current is not None
            and current.mapping_hash == context.revision.definition.content_hash
            and current.schema_hash == context.revision.definition.schema_hash
        ):
            combined.extend(
                item
                for item in current.manager_rules
                if item.dataset != context.dataset_name
            )
        ruleset = default_quality_ruleset(
            project_id=context.project_id,
            mapping_hash=context.revision.definition.content_hash,
            schema_hash=context.revision.definition.schema_hash,
            datasets=(item.name for item in context.selection.datasets),
            version=(current.version + 1 if current is not None else 1),
            parent_version=(current.version if current is not None else None),
            manager_rules=combined,
        )
        return self.publish_ruleset(context.project_id, ruleset, actor=actor)

    def evaluate_and_publish(
        self,
        project: MigrationProject,
        revision: MappingRevision,
        selection: SourceSelection,
        canonical_run: CanonicalStagingRun,
        physical_rows: dict[str, tuple[int, ...]],
        staging: StagingRunSummary,
        *,
        actor: Actor,
    ) -> tuple[QualityRun, QualityRunSummary]:
        ruleset = self.quality.get_current_quality_ruleset(project.project_id)
        if (
            ruleset is None
            or ruleset.mapping_hash != revision.definition.content_hash
            or ruleset.schema_hash != revision.definition.schema_hash
        ):
            ruleset = default_quality_ruleset(
                project_id=project.project_id,
                mapping_hash=revision.definition.content_hash,
                schema_hash=revision.definition.schema_hash,
                datasets=(item.name for item in selection.datasets),
                version=(ruleset.version + 1 if ruleset is not None else 1),
                parent_version=(ruleset.version if ruleset is not None else None),
            )
            ruleset = self.quality.publish_quality_ruleset(
                project.project_id,
                ruleset,
                actor=actor,
            )
        try:
            quality_run = evaluate_quality(
                project=project,
                staging=canonical_run,
                physical_rows=physical_rows,
                ruleset=ruleset,
                published_staging_content_hash=staging.content_hash,
            )
        except QualityError as error:
            raise ReadinessError(str(error)) from error
        summary = self.quality.publish_quality_run(
            project.project_id,
            quality_run,
            staging_run_id=staging.run_id,
            actor=actor,
        )
        if not summary.can_compare:
            raise ReadinessError(
                "Fix the data-check setup shown below, then check all rows again. "
                "Odoo was not contacted."
            )
        return quality_run, summary
