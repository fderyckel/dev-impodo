"""Application service for durable quality rules and evaluation."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Protocol

from ..access import Actor
from ..domain.mapping.artifacts import MappingRevision
from ..domain.resolution import EffectiveDataset
from ..domain.staging.preparation_session import StoredCanonicalStagingRun
from ..domain.coverage import ReferenceBundle
from ..workspace_state import WorkspaceState
from ..quality import (
    QualityError,
    QualityRule,
    QualityRuleSet,
    QualityRun,
    QualityRunSummary,
    StoredQualityRun,
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
from .bounded_quality import (
    BoundedQualityUnsupported,
    build_bounded_quality_run,
    materialize_staging_run,
)


class RecipeQualitySeedRepository(Protocol):
    """Read reusable business checks staged for one exact mapping draft."""

    def get_quality_seed(
        self,
        project_id: str,
        mapping_content_hash: str,
    ) -> tuple[QualityRule, ...]: ...


@dataclass(frozen=True, slots=True)
class QualityConfigurationContext:
    """Validated mapping/source scope for editing one dataset's manager rules."""

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
        *,
        recipe_quality: RecipeQualitySeedRepository | None = None,
    ) -> None:
        self.mappings = mappings
        self.sources = sources
        self.quality = quality
        self.recipe_quality = recipe_quality

    def current_ruleset(self, project_id: str) -> QualityRuleSet | None:
        """Return the currently published Stage-F rule contract."""

        return self.quality.get_current_quality_ruleset(project_id)

    def current_summary(self, project_id: str) -> QualityRunSummary | None:
        """Return the lightweight projection of the current quality run."""

        return self.quality.get_current_quality_summary(project_id)

    def current_run(self, project_id: str) -> QualityRun | None:
        """Load the complete current run referenced by its summary."""

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
        """Persist a complete immutable ruleset version."""

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
        """Resolve a safe dataset editing scope from current saved evidence.

        Unsaved mapping changes are rejected so manager rules cannot be bound
        to field names that differ from the published mapping revision.
        """

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
        """Replace one dataset's manager rules while preserving other datasets."""

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
        elif self.recipe_quality is not None:
            combined.extend(
                item
                for item in self.recipe_quality.get_quality_seed(
                    context.project_id,
                    context.revision.definition.content_hash,
                )
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
        if (
            current is not None
            and current.mapping_hash == context.revision.definition.content_hash
            and current.schema_hash == context.revision.definition.schema_hash
        ):
            advanced = tuple(
                item
                for item in current.rules
                if item.source.value == "SCOPE_APPROVED"
            )
            if advanced:
                ruleset = replace(
                    ruleset,
                    rules=tuple(
                        sorted((*ruleset.rules, *advanced), key=lambda item: item.rule_id)
                    ),
                    coverage_scope_hash=current.coverage_scope_hash,
                    reference_bundle_hash=current.reference_bundle_hash,
                )
        return self.publish_ruleset(context.project_id, ruleset, actor=actor)

    def evaluate_and_publish(
        self,
        project: WorkspaceState,
        revision: MappingRevision,
        selection: SourceSelection,
        canonical_run: CanonicalStagingRun | StoredCanonicalStagingRun,
        physical_rows: dict[str, tuple[int, ...]],
        staging: StagingRunSummary,
        effective: EffectiveDataset | None = None,
        effective_dataset_run_id: str | None = None,
        reference_bundle: ReferenceBundle | None = None,
        *,
        actor: Actor,
        allow_materialized_fallback: bool = True,
    ) -> tuple[QualityRun | StoredQualityRun, QualityRunSummary]:
        """Evaluate Stage F and publish its full run plus lifecycle summary.

        A compatible ruleset is reused; otherwise automatic rules are rebuilt
        from the current mapping/schema. The published staging content hash is
        passed into evaluation so downstream evidence binds to durable Stage E.
        """

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
                manager_rules=(
                    self.recipe_quality.get_quality_seed(
                        project.project_id,
                        revision.definition.content_hash,
                    )
                    if self.recipe_quality is not None
                    else ()
                ),
            )
            ruleset = self.quality.publish_quality_ruleset(
                project.project_id,
                ruleset,
                actor=actor,
            )
        try:
            if (
                isinstance(canonical_run, StoredCanonicalStagingRun)
                and effective is None
            ):
                try:
                    quality_run: QualityRun | StoredQualityRun = (
                        build_bounded_quality_run(
                            project=project,
                            staging=canonical_run,
                            physical_rows=physical_rows,
                            ruleset=ruleset,
                            published_staging_content_hash=staging.content_hash,
                        )
                    )
                except BoundedQualityUnsupported as error:
                    if not allow_materialized_fallback:
                        raise ReadinessError(
                            "The data-check route could not stay bounded for "
                            "this project. Whole-run fallback is disabled above "
                            "the materialized safety limit; no fallback was run."
                        ) from error
                    quality_run = evaluate_quality(
                        project=project,
                        staging=materialize_staging_run(canonical_run),
                        physical_rows=physical_rows,
                        ruleset=ruleset,
                        published_staging_content_hash=staging.content_hash,
                        reference_bundle=reference_bundle,
                    )
            else:
                quality_run = evaluate_quality(
                    project=project,
                    staging=canonical_run,
                    physical_rows=physical_rows,
                    ruleset=ruleset,
                    published_staging_content_hash=staging.content_hash,
                    effective=effective,
                    reference_bundle=reference_bundle,
                )
        except QualityError as error:
            raise ReadinessError(str(error)) from error
        summary = self.quality.publish_quality_run(
            project.project_id,
            quality_run,
            staging_run_id=staging.run_id,
            effective_dataset_run_id=effective_dataset_run_id,
            actor=actor,
        )
        if not summary.can_compare:
            raise ReadinessError(
                "Fix the data-check setup shown below, then check all rows again. "
                "Odoo was not contacted."
            )
        if isinstance(quality_run, StoredQualityRun):
            quality_run = quality_run.with_content_hash(summary.content_hash)
        return quality_run, summary

