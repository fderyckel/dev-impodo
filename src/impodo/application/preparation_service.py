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
from ..artifacts import ArtifactStore, ArtifactStoreError
from ..derived_entities import DerivedEntityPlan
from ..domain.contracts import TRANSFORMATION_IMPACT_DETAIL_LIMIT
from ..domain.coverage import ReferenceBundle
from ..domain.source_snapshot import SourceSnapshot
from ..domain.source_binding import require_file_source
from ..domain.staging.evaluator import (
    StagedBrowserMapping,
    evaluate_browser_mapping,
)
from ..domain.staging.scale import (
    require_supported_browser_scale,
)
from ..domain.staging.transformation_impact import TransformationImpactRow
from ..domain.staging.preparation_session import StoredCanonicalStagingRun
from ..inspection import SourceFileCatalog
from ..domain.mapping.contracts import MappingDefinition
from ..normalization import NormalizationRunSummary
from ..preparation_jobs import PreparationPhase
from ..projects import MigrationProject
from ..source import SourceTable, load_selected_source_table
from ..source_snapshot_io import (
    load_source_snapshot_table,
    validate_snapshot_for_dataset,
)
from ..workspace_contracts import SourceSelection
from ..domain.errors import ReadinessError
from .bounded_preparation import (
    prepare_bounded_direct_session,
    supports_bounded_direct_preparation,
)
from .normalization_service import NormalizationService
from .preparation_capability import compile_preparation_capability
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
        progress: Callable[[PreparationPhase, int, int, str], None] | None = None,
        cancellation_checkpoint: Callable[[], None] | None = None,
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

        report_progress = progress or (lambda _phase, _completed, _total, _message: None)
        check_cancelled = cancellation_checkpoint or (lambda: None)
        report_progress(
            PreparationPhase.VALIDATING,
            0,
            0,
            "Checking the saved setup",
        )
        check_cancelled()
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
        source_snapshots = self.sources.get_current_source_snapshots(project_id)
        total_rows = sum(item.row_count for item in physical_selection.datasets)
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
        capability = compile_preparation_capability(
            definition=revision.definition,
            physical_selection=physical_selection,
            effective_selection=effective_selection,
            source_snapshots=source_snapshots,
            derived_plan=derived_plan,
            current_ruleset=self.quality.current_ruleset(project_id),
            reference_bundle=reference_bundle,
        )
        capability.require_supported()
        bounded_session_id: str | None = None
        bounded_canonical_run: StoredCanonicalStagingRun | None = None
        impact_rows: Iterable[TransformationImpactRow]

        def report_source_batch(completed: int, total: int) -> None:
            check_cancelled()
            report_progress(
                PreparationPhase.TRANSFORMING,
                completed,
                total,
                "Preparing source rows",
            )

        report_progress(
            PreparationPhase.TRANSFORMING,
            0,
            total_rows,
            "Preparing source rows",
        )
        check_cancelled()
        if supports_bounded_direct_preparation(
            physical_selection,
            effective_selection,
            derived_plan,
        ):
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
                actor=actor,
                source_snapshots=source_snapshots,
                batch_progress=report_source_batch,
            )
            bounded_session_id = bounded.session_id
            staging_input = bounded.run
            bounded_canonical_run = bounded.run
            try:
                check_cancelled()
                report_progress(
                    PreparationPhase.PUBLISHING,
                    total_rows,
                    total_rows,
                    "Saving prepared data",
                )
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
                source_snapshots=source_snapshots,
                collect_transformation_impact=True,
                transformation_detail_limit=0,
                transformation_impact_sink=materialized_impacts.append,
            )
            check_cancelled()
            report_progress(
                PreparationPhase.PUBLISHING,
                total_rows,
                total_rows,
                "Saving prepared data",
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
            if bounded_canonical_run is not None:
                canonical_run = bounded_canonical_run
            else:
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
            report_progress(
                PreparationPhase.QUALITY,
                total_rows,
                total_rows,
                "Running data checks",
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
                allow_materialized_fallback=(
                    capability.permits_materialized_fallback
                ),
            )
            report_progress(
                PreparationPhase.NORMALIZING,
                total_rows,
                total_rows,
                "Organizing changes for review",
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
                allow_materialized_fallback=(
                    capability.permits_materialized_fallback
                ),
            )
            if bounded_session_id is not None:
                self.sessions.mark_published(project_id, bounded_session_id)
            report_progress(
                PreparationPhase.COMPLETE,
                total_rows,
                total_rows,
                "Prepared data is ready for review",
            )
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
        binding = require_file_source(dataset.source)
        file_id = binding.file_id
        source_hash = binding.source_sha256
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
    source_snapshots: Iterable[SourceSnapshot] | None = None,
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
        source_snapshots=source_snapshots,
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
    *,
    source_snapshots: Iterable[SourceSnapshot] | None = None,
) -> dict[str, SourceTable]:
    """Materialize and validate physical tables before pure evaluation."""

    catalog_by_file = {item.file_id: item for item in catalogs}
    source_file_by_id = {item.file_id: item for item in project.source_files}
    snapshot_by_dataset = {
        item.dataset_id: item for item in (source_snapshots or ())
    }
    if source_snapshots is not None and (
        len(snapshot_by_dataset) != len(physical_selection.datasets)
        or set(snapshot_by_dataset)
        != {item.dataset_id for item in physical_selection.datasets}
    ):
        raise ReadinessError("Frozen source snapshots are incomplete")
    loaded: dict[str, SourceTable] = {}
    with ExitStack() as stack:
        for physical in physical_selection.datasets:
            binding = require_file_source(physical.source)
            source_file = source_file_by_id.get(binding.file_id)
            catalog = catalog_by_file.get(binding.file_id)
            table_catalog = next(
                (
                    item
                    for item in (catalog.tables if catalog else ())
                    if item.table_key == binding.table_key
                ),
                None,
            )
            if source_file is None or catalog is None or table_catalog is None:
                raise ReadinessError("Frozen source evidence is incomplete")
            snapshot = snapshot_by_dataset.get(physical.dataset_id)
            if snapshot is not None:
                try:
                    validate_snapshot_for_dataset(
                        physical_selection,
                        physical,
                        snapshot,
                    )
                    path = stack.enter_context(
                        artifacts.materialize_source_snapshot(
                            project.project_id,
                            snapshot.parquet_storage_key,
                            expected_sha256=snapshot.parquet_sha256,
                        )
                    )
                    loaded[physical.dataset_id] = load_source_snapshot_table(
                        path,
                        snapshot,
                    )
                except (ArtifactStoreError, OSError, ValueError) as error:
                    raise ReadinessError(
                        "The frozen source snapshot could not be verified"
                    ) from error
                continue
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
                table_key=binding.table_key,
                encoding=binding.encoding,
                delimiter=binding.delimiter,
                header_row=binding.header_row,
                named_table_range=named_range,
                source_display_name=source_file.display_name,
            )
    return loaded
