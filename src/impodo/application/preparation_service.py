"""Prepare immutable source evidence for quality and normalization review."""

from __future__ import annotations

from dataclasses import dataclass
from contextlib import ExitStack
from typing import Callable, Iterable

from ..access import Actor, AuthorizationPolicy, Capability
from ..artifacts import ArtifactStore
from ..derived_entities import DerivedEntityPlan
from ..domain.contracts import TRANSFORMATION_IMPACT_DETAIL_LIMIT
from ..domain.staging.evaluator import (
    StagedBrowserMapping,
    evaluate_browser_mapping,
)
from ..domain.staging.scale import (
    BROWSER_EVALUATION_ROW_LIMIT,
    BrowserEvaluationScale,
    browser_evaluation_scale,
    require_supported_browser_scale,
)
from ..domain.staging.transformation_impact import TransformationImpactRow
from ..inspection import SourceFileCatalog
from ..mapping_semantics import MappingDefinition, MappingRevision
from ..normalization import NormalizationRunSummary
from ..projects import MigrationProject
from ..quality import QualityRun, QualityRunSummary
from ..source import SourceTable, load_selected_source_table
from ..staging import StagingRunSummary
from ..workspace import SourceSelection
from ..domain.errors import ReadinessError
from .normalization_service import NormalizationService
from .quality_service import QualityService
from .readiness_ports import PreparationRepository


@dataclass(frozen=True, slots=True)
class PreparedReadinessContext:
    project: MigrationProject
    revision: MappingRevision
    staged: StagedBrowserMapping
    staging: StagingRunSummary
    quality_run: QualityRun
    quality: QualityRunSummary
    normalization: NormalizationRunSummary


class PreparationService:
    """Coordinate source loading and target-independent evidence publication."""

    def __init__(
        self,
        repository: PreparationRepository,
        artifacts: ArtifactStore,
        authorization: AuthorizationPolicy,
        quality: QualityService,
        normalization: NormalizationService,
    ) -> None:
        self.repository = repository
        self.artifacts = artifacts
        self.authorization = authorization
        self.quality = quality
        self.normalization = normalization

    def prepare(
        self,
        project_id: str,
        *,
        actor: Actor,
    ) -> NormalizationRunSummary:
        return self.prepare_context(project_id, actor=actor).normalization

    def prepare_context(
        self,
        project_id: str,
        *,
        actor: Actor,
    ) -> PreparedReadinessContext:
        self.authorization.require(
            actor,
            Capability.MAPPING_SUBMIT,
            project_id=project_id,
        )
        project = self.repository.get(project_id)
        revision = self.repository.get_mapping_revision(project_id)
        if revision is None:
            raise ReadinessError("Submit the mapping before checking data")
        submission = self.repository.get_mapping_submission(
            project_id, revision.version
        )
        if (
            submission is None
            or submission.mapping_content_hash != revision.definition.content_hash
        ):
            raise ReadinessError("Submit the current mapping before checking data")
        physical_selection = self.repository.get_source_selection(project_id)
        if physical_selection is None:
            raise ReadinessError("Freeze the source datasets before checking data")
        source_hashes = canonical_source_hashes(physical_selection)
        effective_selection = self.repository.get_mapping_source_selection(project_id)
        if effective_selection is None:
            raise ReadinessError("Freeze the source datasets before checking data")

        impact_rows: list[TransformationImpactRow] = []
        staged = stage_browser_mapping(
            project,
            revision.definition,
            physical_selection,
            effective_selection,
            self.repository.get_derived_entity_plan(project_id),
            self.repository.get_source_catalogs(project_id),
            self.artifacts,
            collect_transformation_impact=True,
            transformation_detail_limit=0,
            transformation_impact_sink=impact_rows.append,
        )
        staging = self.repository.publish_canonical_staging(
            project_id,
            staged.canonical_run,
            mapping_version=revision.version,
            actor=actor,
        )
        quality_run, quality = self.quality.evaluate_and_publish(
            project,
            revision,
            effective_selection,
            staged,
            staging,
            actor=actor,
        )
        normalization = self.normalization.evaluate_and_publish(
            project,
            revision,
            effective_selection,
            staged,
            staging,
            quality_run,
            quality,
            tuple(impact_rows),
            source_hashes,
            actor=actor,
        )
        return PreparedReadinessContext(
            project=project,
            revision=revision,
            staged=staged,
            staging=staging,
            quality_run=quality_run,
            quality=quality,
            normalization=normalization,
        )


def canonical_source_hashes(selection: SourceSelection) -> dict[str, str]:
    """Validate frozen source bindings before publishing derived evidence."""

    invalid_message = (
        "Impodo could not verify the registered source files. "
        "Check the source files, then prepare the data again."
    )
    source_hashes: dict[str, str] = {}
    for dataset in selection.datasets:
        file_id = dataset.file_id
        source_hash = dataset.source_sha256
        if (
            not isinstance(file_id, str)
            or not file_id.strip()
            or file_id != file_id.strip()
            or not isinstance(source_hash, str)
        ):
            raise ReadinessError(invalid_message)
        digest = source_hash.removeprefix("sha256:")
        if len(digest) != 64:
            raise ReadinessError(invalid_message)
        try:
            int(digest, 16)
        except ValueError as error:
            raise ReadinessError(invalid_message) from error
        canonical_hash = f"sha256:{digest.casefold()}"
        existing = source_hashes.setdefault(file_id, canonical_hash)
        if existing != canonical_hash:
            raise ReadinessError(invalid_message)
    if not source_hashes:
        raise ReadinessError(invalid_message)
    return dict(sorted(source_hashes.items()))


def stage_browser_mapping(
    project: MigrationProject,
    definition: MappingDefinition,
    physical_selection: SourceSelection,
    effective_selection: SourceSelection,
    plan: DerivedEntityPlan | None,
    catalogs: Iterable[SourceFileCatalog],
    artifacts: ArtifactStore,
    *,
    collect_transformation_impact: bool = False,
    transformation_detail_limit: int = TRANSFORMATION_IMPACT_DETAIL_LIMIT,
    transformation_impact_sink: Callable[[TransformationImpactRow], None]
    | None = None,
) -> StagedBrowserMapping:
    """Load frozen artifacts, then delegate to the reusable evaluator."""

    require_supported_browser_scale(physical_selection)
    physical_tables = _load_browser_source_tables(
        project,
        physical_selection,
        catalogs,
        artifacts,
    )
    return evaluate_browser_mapping(
        project_id=project.project_id,
        definition=definition,
        physical_selection=physical_selection,
        effective_selection=effective_selection,
        plan=plan,
        loaded_tables=physical_tables,
        collect_transformation_impact=collect_transformation_impact,
        transformation_detail_limit=transformation_detail_limit,
        transformation_impact_sink=transformation_impact_sink,
    )


def _load_browser_source_tables(
    project: MigrationProject,
    physical_selection: SourceSelection,
    catalogs: Iterable[SourceFileCatalog],
    artifacts: ArtifactStore,
) -> dict[str, SourceTable]:
    """Materialize and validate physical tables before pure evaluation."""

    catalog_by_file = {item.file_id: item for item in catalogs}
    source_file_by_id = {item.file_id: item for item in project.source_files}
    loaded: dict[str, SourceTable] = {}
    with ExitStack() as stack:
        for physical in physical_selection.datasets:
            source_file = source_file_by_id.get(physical.file_id)
            catalog = catalog_by_file.get(physical.file_id)
            table_catalog = next(
                (
                    item
                    for item in (catalog.tables if catalog else ())
                    if item.table_key == physical.table_key
                ),
                None,
            )
            if source_file is None or catalog is None or table_catalog is None:
                raise ReadinessError("Frozen source evidence is incomplete")
            path = stack.enter_context(
                artifacts.materialize_source(
                    project.project_id,
                    source_file.stored_name,
                )
            )
            named_range = (
                table_catalog.named_tables[0].cell_range
                if table_catalog.kind == "NAMED_TABLE"
                and table_catalog.named_tables
                else None
            )
            loaded[physical.dataset_id] = load_selected_source_table(
                path,
                dataset=physical.name,
                table_key=physical.table_key,
                encoding=physical.encoding,
                delimiter=physical.delimiter,
                header_row=physical.header_row,
                named_table_range=named_range,
            )
    return loaded

