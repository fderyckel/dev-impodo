"""Orchestrate target-independent preparation and review evidence.

Migration stages: E–G — normalize/validate, canonical staging, and symbolic
relationship preparation. Layer: application service.

``PreparationService.prepare`` is called by the preparation browser route. It
loads a submitted mapping and frozen source selection, delegates pure row
evaluation to :mod:`impodo.domain.staging.evaluator`, then publishes canonical
staging, quality, and normalization evidence in that order. This module may
read registered source artifacts but never contacts Odoo.

See ``docs/architecture/python-code-map.md``,
``docs/contracts/03-canonical-staging.md``, and ``tests/test_readiness.py``.
"""

from __future__ import annotations

from contextlib import ExitStack
from typing import Callable, Iterable

from ..access import Actor, AuthorizationPolicy, Capability
from ..artifacts import ArtifactStore
from ..derived_entities import DerivedEntityPlan
from ..domain.contracts import TRANSFORMATION_IMPACT_DETAIL_LIMIT
from ..domain.coverage import ReferenceBundle
from ..domain.staging.evaluator import (
    StagedBrowserMapping,
    evaluate_browser_mapping,
)
from ..domain.staging.scale import (
    require_supported_browser_scale,
)
from ..domain.staging.transformation_impact import TransformationImpactRow
from ..inspection import SourceFileCatalog
from ..domain.mapping.contracts import MappingDefinition
from ..normalization import NormalizationRunSummary
from ..projects import MigrationProject
from ..source import SourceTable, load_selected_source_table
from ..workspace_contracts import SourceSelection
from ..domain.errors import ReadinessError
from .bounded_preparation import (
    prepare_bounded_direct_session,
    supports_bounded_direct_preparation,
)
from .normalization_service import NormalizationService
from .quality_service import QualityService
from .resolution_service import ResolutionService
from .readiness_ports import (
    PreparationDerivedRepository,
    PreparationMappingRepository,
    PreparationProjectRepository,
    PreparationSourceRepository,
    PreparationStagingRepository,
    PreparationSessionRepository,
)


class PreparationService:
    """Coordinate source loading and target-independent evidence publication.

    The service owns the fixed transition from a current submitted mapping to
    canonical staging, quality, and normalization review evidence. Repositories
    supply current governed inputs and durable publication; the domain
    evaluator owns row semantics. The service deliberately has no target
    reader, which keeps preparation independent from Odoo availability.
    """

    def __init__(
        self,
        projects: PreparationProjectRepository,
        sources: PreparationSourceRepository,
        derived_entities: PreparationDerivedRepository,
        mappings: PreparationMappingRepository,
        staging: PreparationStagingRepository,
        sessions: PreparationSessionRepository,
        artifacts: ArtifactStore,
        authorization: AuthorizationPolicy,
        quality: QualityService,
        normalization: NormalizationService,
        resolution: ResolutionService | None = None,
    ) -> None:
        self.projects = projects
        self.sources = sources
        self.derived_entities = derived_entities
        self.mappings = mappings
        self.staging = staging
        self.sessions = sessions
        self.artifacts = artifacts
        self.authorization = authorization
        self.quality = quality
        self.normalization = normalization
        self.resolution = resolution

    def prepare(
        self,
        project_id: str,
        *,
        actor: Actor,
    ) -> NormalizationRunSummary:
        """Prepare every frozen row for review without contacting Odoo.

        The current mapping must be submitted against the current frozen source
        selection. The method authorizes the action, evaluates the physical and
        derived datasets, publishes canonical staging, evaluates quality, and
        creates normalization review evidence. Each downstream publication is
        bound to the exact result returned by the previous step.

        Returns:
            The current normalization run summary that the browser uses to
            direct the data manager to review and explicit approval.

        Raises:
            ReadinessError: If the mapping, submission, source selection, or
                source bindings are absent, stale, or inconsistent.
        """

        self.authorization.require(
            actor,
            Capability.MAPPING_SUBMIT,
            project_id=project_id,
        )
        project = self.projects.get(project_id)
        revision = self.mappings.get_mapping_revision(project_id)
        if revision is None:
            raise ReadinessError("Submit the mapping before checking data")
        submission = self.mappings.get_mapping_submission(
            project_id, revision.version
        )
        if (
            submission is None
            or submission.mapping_content_hash != revision.definition.content_hash
        ):
            raise ReadinessError("Submit the current mapping before checking data")
        physical_selection = self.sources.get_source_selection(project_id)
        if physical_selection is None:
            raise ReadinessError("Freeze the source datasets before checking data")
        source_hashes = canonical_source_hashes(physical_selection)
        effective_selection = self.sources.get_mapping_source_selection(project_id)
        if effective_selection is None:
            raise ReadinessError("Freeze the source datasets before checking data")
        reference_bundle = (
            self.resolution.current_reference_bundle(project_id)
            if self.resolution is not None
            else None
        )

        derived_plan = self.derived_entities.get_derived_entity_plan(project_id)
        bounded_session_id: str | None = None
        impact_rows: Iterable[TransformationImpactRow]
        if supports_bounded_direct_preparation(
            physical_selection,
            effective_selection,
            derived_plan,
        ):
            require_supported_browser_scale(physical_selection)
            bounded = prepare_bounded_direct_session(
                project,
                revision.definition,
                revision.version,
                physical_selection,
                effective_selection,
                self.sources.get_source_catalogs(project_id),
                self.artifacts,
                reference_bundle,
                self.sessions,
            )
            bounded_session_id = bounded.session_id
            staging_input = bounded.run
            try:
                staging = self.staging.publish_canonical_staging(
                    project_id,
                    staging_input,
                    mapping_version=revision.version,
                    actor=actor,
                )
            except Exception:
                self.sessions.fail_session(
                    project_id,
                    bounded_session_id,
                    "BOUNDED_PUBLICATION_FAILED",
                )
                raise
            del staging_input, bounded
            physical_rows = self.sessions.physical_rows(
                project_id,
                bounded_session_id,
            )
            impact_rows = self.sessions.iter_impacts(
                project_id,
                bounded_session_id,
            )
        else:
            materialized_impacts: list[TransformationImpactRow] = []
            staged = stage_browser_mapping(
                project,
                revision.definition,
                physical_selection,
                effective_selection,
                derived_plan,
                self.sources.get_source_catalogs(project_id),
                self.artifacts,
                collect_transformation_impact=True,
                transformation_detail_limit=0,
                transformation_impact_sink=materialized_impacts.append,
            )
            staging = self.staging.publish_canonical_staging(
                project_id,
                staged.canonical_run,
                mapping_version=revision.version,
                actor=actor,
            )
            physical_rows = dict(staged.physical_rows)
            impact_rows = materialized_impacts
            del staged

        try:
            canonical_run = self.staging.get_canonical_staging_run(
                project_id,
                staging.run_id,
                expected_content_hash=staging.content_hash,
            )
            if canonical_run is None:
                raise ReadinessError(
                    "The published prepared data could not be verified. "
                    "Prepare the data again."
                )
            effective = None
            resolution_summary = None
            if self.resolution is not None:
                effective, resolution_summary = (
                    self.resolution.evaluate_for_preparation(
                        project_id,
                        canonical_run,
                        staging_run_id=staging.run_id,
                        staging_content_hash=staging.content_hash,
                        actor=actor,
                    )
                )
            quality_run, quality = self.quality.evaluate_and_publish(
                project,
                revision,
                effective_selection,
                canonical_run,
                physical_rows,
                staging,
                effective,
                (
                    resolution_summary.run_id
                    if effective is not None and resolution_summary is not None
                    else None
                ),
                reference_bundle,
                actor=actor,
            )
            normalization = self.normalization.evaluate_and_publish(
                project,
                revision,
                effective_selection,
                canonical_run,
                staging,
                quality_run,
                quality,
                impact_rows,
                source_hashes,
                effective,
                actor=actor,
            )
            if bounded_session_id is not None:
                self.sessions.mark_published(project_id, bounded_session_id)
            return normalization
        except Exception:
            if bounded_session_id is not None:
                try:
                    self.sessions.fail_session(
                        project_id,
                        bounded_session_id,
                        "BOUNDED_PIPELINE_FAILED",
                    )
                except Exception:
                    pass
            raise


def canonical_source_hashes(selection: SourceSelection) -> dict[str, str]:
    """Return one canonical content hash for every frozen source file.

    Conflicting dataset bindings for the same file fail closed so staging and
    normalization evidence cannot claim two source contents for one file ID.
    """

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
    reference_bundle: ReferenceBundle | None = None,
    *,
    collect_transformation_impact: bool = False,
    transformation_detail_limit: int = TRANSFORMATION_IMPACT_DETAIL_LIMIT,
    transformation_impact_sink: Callable[[TransformationImpactRow], None]
    | None = None,
) -> StagedBrowserMapping:
    """Materialize frozen tables, then delegate to the pure row evaluator.

    This is the I/O-to-domain seam for browser preparation. It enforces the
    bounded browser scale, loads only the selected physical tables, and calls
    :func:`impodo.domain.staging.evaluator.evaluate_browser_mapping`. It does
    not publish evidence or contact Odoo.
    """

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
        reference_bundle=reference_bundle,
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
