"""Adapters that resume the existing Authoring owners for correction review."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from impodo.application.correction_orchestration import (
    CorrectionDatasetReviewInput,
    CorrectionTargetReviewEvidence,
)
from impodo.domain.compiler.columnar_transformation import (
    ColumnarSupport,
    compile_columnar_transformation_programs,
)
from impodo.domain.correction_origin import (
    CorrectionOriginError,
    CorrectionOriginManifest,
)
from impodo.domain.mapping.artifacts import MappingRevision
from impodo.domain.mapping.validation.evidence import (
    MappingValidationStatus,
    mapping_issue_fingerprint,
)
from impodo.domain.shared.access import Actor


class CurrentCorrectionMappingReviewStage:
    """Check and submit the successor's exact current mapping draft."""

    def __init__(self, mappings) -> None:
        self.mappings = mappings

    def validate_and_submit(
        self,
        manifest: CorrectionOriginManifest,
        successor_workspace_id: str,
        *,
        actor: Actor,
    ) -> MappingRevision:
        draft = self.mappings.mappings.get_mapping_working_draft(
            successor_workspace_id
        )
        revision = self.mappings.mappings.get_mapping_revision(
            successor_workspace_id
        )
        if draft is None and revision is None:
            raise CorrectionOriginError("Correction rules are missing")
        datasets = (
            draft.definition.datasets
            if draft is not None
            else revision.definition.datasets
        )
        revision, validation = self.mappings.check_definition(
            successor_workspace_id,
            datasets=datasets,
            expected_parent_version=(revision.version if revision else None),
            expected_working_draft_version=(draft.version if draft else None),
            actor=actor,
        )
        if validation.status is MappingValidationStatus.INVALID:
            first = next(
                item for item in validation.issues if item.severity == "error"
            )
            raise CorrectionOriginError(
                f"Correction rules need attention: {first.message}"
            )
        current_draft = self.mappings.mappings.get_mapping_working_draft(
            successor_workspace_id
        )
        acknowledgements = tuple(
            mapping_issue_fingerprint(item)
            for item in validation.issues
            if item.severity == "warning"
        )
        self.mappings.submit_current(
            successor_workspace_id,
            datasets=revision.definition.datasets,
            expected_version=revision.version,
            expected_working_draft_version=(
                current_draft.version if current_draft is not None else None
            ),
            warning_acknowledgements=acknowledgements,
            actor=actor,
        )
        return revision


class CurrentCorrectionPreparationReviewStage:
    """Run native preparation and bind prior/current Parquet evidence once."""

    def __init__(
        self,
        *,
        preparation,
        sessions,
        mappings,
        sources,
        artifacts,
    ) -> None:
        self.preparation = preparation
        self.sessions = sessions
        self.mappings = mappings
        self.sources = sources
        self.artifacts = artifacts

    def prepare_native(
        self,
        manifest: CorrectionOriginManifest,
        mapping: MappingRevision,
        successor_workspace_id: str,
        *,
        actor: Actor,
    ) -> tuple[CorrectionDatasetReviewInput, ...]:
        self.preparation.prepare(successor_workspace_id, actor=actor)
        previous_mapping = self.mappings.get_mapping_revision(
            manifest.completed_workspace_id
        )
        previous_selection = self.sources.get_mapping_source_selection(
            manifest.completed_workspace_id
        )
        corrected_selection = self.sources.get_mapping_source_selection(
            successor_workspace_id
        )
        if (
            previous_mapping is None
            or previous_selection is None
            or corrected_selection is None
        ):
            raise CorrectionOriginError(
                "Correction preparation inputs are incomplete"
            )
        previous_programs = _native_programs(
            previous_mapping,
            previous_selection,
        )
        corrected_programs = _native_programs(mapping, corrected_selection)
        corrected_snapshots = {
            item.dataset_id: item
            for item in self.sessions.current_prepared_snapshots(
                successor_workspace_id
            )
        }
        inputs: list[CorrectionDatasetReviewInput] = []
        for prior in manifest.prepared_artifacts:
            previous_snapshot = self.sessions.find_prepared_snapshot(
                manifest.completed_workspace_id,
                prior.dataset_id,
                prior.logical_hash,
            )
            corrected_snapshot = corrected_snapshots.get(prior.dataset_id)
            if previous_snapshot is None or corrected_snapshot is None:
                raise CorrectionOriginError(
                    "Correction prepared evidence is incomplete"
                )
            with self.artifacts.materialize_prepared_snapshot(
                manifest.completed_workspace_id,
                previous_snapshot.parquet_storage_key,
                expected_sha256=previous_snapshot.parquet_sha256,
            ) as previous_path:
                verified_previous_path = Path(previous_path)
            with self.artifacts.materialize_prepared_snapshot(
                successor_workspace_id,
                corrected_snapshot.parquet_storage_key,
                expected_sha256=corrected_snapshot.parquet_sha256,
            ) as corrected_path:
                verified_corrected_path = Path(corrected_path)
            inputs.append(
                CorrectionDatasetReviewInput(
                    previous_path=verified_previous_path,
                    previous_snapshot=previous_snapshot,
                    previous_program=previous_programs[prior.dataset_id],
                    corrected_path=verified_corrected_path,
                    corrected_snapshot=corrected_snapshot,
                    corrected_program=corrected_programs[prior.dataset_id],
                )
            )
        return tuple(inputs)


class CurrentCorrectionQualityReviewStage:
    """Require current checks and freeze the correction's prepared values."""

    def __init__(self, *, quality, normalization) -> None:
        self.quality = quality
        self.normalization = normalization

    def require_current_quality(
        self,
        manifest: CorrectionOriginManifest,
        mapping: MappingRevision,
        datasets: tuple[CorrectionDatasetReviewInput, ...],
        successor_workspace_id: str,
        *,
        actor: Actor,
    ) -> None:
        quality = self.quality.current_summary(successor_workspace_id)
        if quality is None or not quality.ready_for_package:
            raise CorrectionOriginError(
                "Correction data checks need attention before review"
            )
        if any(
            item.corrected_snapshot.mapping_hash
            != mapping.definition.content_hash
            for item in datasets
        ):
            raise CorrectionOriginError(
                "Correction data checks no longer match the rules"
            )
        normalization = self.normalization.current_summary(
            successor_workspace_id
        )
        if normalization is None:
            raise CorrectionOriginError("Correction prepared review is missing")
        if not normalization.frozen:
            normalization = self.normalization.approve(
                successor_workspace_id,
                normalization.run_id,
                expected_version=normalization.lifecycle_version,
                actor=actor,
                reason="Included in completed-load correction review",
            )
        if not normalization.frozen:
            raise CorrectionOriginError(
                "Correction prepared values could not be fixed for review"
            )


CorrectionTargetCapability = Callable[
    [
        CorrectionOriginManifest,
        MappingRevision,
        tuple[CorrectionDatasetReviewInput, ...],
        str,
        Actor,
    ],
    CorrectionTargetReviewEvidence,
]


class CallbackCorrectionTargetReviewStage:
    """Resolve the web-owned target credential and closed read capability."""

    def __init__(self, capability: CorrectionTargetCapability) -> None:
        self.capability = capability

    def refresh_read_capability(
        self,
        manifest: CorrectionOriginManifest,
        mapping: MappingRevision,
        datasets: tuple[CorrectionDatasetReviewInput, ...],
        successor_workspace_id: str,
        *,
        actor: Actor,
    ) -> CorrectionTargetReviewEvidence:
        return self.capability(
            manifest,
            mapping,
            datasets,
            successor_workspace_id,
            actor,
        )


def _native_programs(revision, selection):
    decisions = compile_columnar_transformation_programs(
        revision.definition,
        selection,
    )
    if any(
        item.support is not ColumnarSupport.SUPPORTED or item.program is None
        for item in decisions
    ):
        raise CorrectionOriginError(
            "Correction review requires the native Polars preparation path"
        )
    return {item.dataset_id: item.program for item in decisions}


__all__ = [
    "CallbackCorrectionTargetReviewStage",
    "CurrentCorrectionMappingReviewStage",
    "CurrentCorrectionPreparationReviewStage",
    "CurrentCorrectionQualityReviewStage",
]
