"""Native Polars implementation of the correction A/C review pipeline."""

from __future__ import annotations

from pathlib import Path
import tempfile
from typing import Iterator, Protocol

from impodo.adapters.polars_correction import (
    iter_polars_correction_candidate_batches,
    write_polars_correction_candidates,
)
from impodo.application.correction_orchestration import (
    CorrectionAuthoringReviewResult,
    CorrectionDatasetReviewInput,
    CorrectionReviewEvidence,
)
from impodo.domain.correction import CorrectionCandidate
from impodo.domain.correction_origin import (
    CorrectionOriginError,
    CorrectionOriginManifest,
)
from impodo.domain.serialization import content_hash
from impodo.domain.shared.access import Actor


class CorrectionAuthoringStageOwner(Protocol):
    """Resume current authoring stages without duplicating their semantics."""

    def prepare_review(
        self,
        manifest: CorrectionOriginManifest,
        successor_workspace_id: str,
        *,
        actor: Actor,
    ) -> CorrectionAuthoringReviewResult:
        """Validate, submit, prepare, quality-check, and refresh target evidence."""
        ...


class NativeCorrectionReviewPipeline:
    """Reduce A/C with native Polars before any exact-ID Odoo read."""

    def __init__(self, stages: CorrectionAuthoringStageOwner) -> None:
        self.stages = stages

    def run(
        self,
        manifest: CorrectionOriginManifest,
        successor_workspace_id: str,
        *,
        actor: Actor,
    ) -> CorrectionReviewEvidence:
        result = self.stages.prepare_review(
            manifest,
            successor_workspace_id,
            actor=actor,
        )
        datasets = tuple(
            sorted(
                result.datasets,
                key=lambda item: item.previous_snapshot.dataset_id,
            )
        )
        expected = {
            item.dataset_id: item for item in manifest.prepared_artifacts
        }
        if (
            not datasets
            or len(datasets) != len(expected)
            or len({item.previous_snapshot.dataset_id for item in datasets})
            != len(datasets)
            or result.mapping.definition.content_hash
            != datasets[0].corrected_snapshot.mapping_hash
        ):
            raise CorrectionOriginError(
                "Correction review preparation does not cover the origin"
            )
        for item in datasets:
            prior = expected.get(item.previous_snapshot.dataset_id)
            if (
                prior is None
                or prior.logical_hash != item.previous_snapshot.logical_hash
                or prior.content_hash != item.previous_snapshot.content_hash
                or prior.parquet_sha256 != item.previous_snapshot.parquet_sha256
                or prior.parquet_storage_key
                != item.previous_snapshot.parquet_storage_key
                or item.corrected_snapshot.workspace_id != successor_workspace_id
                or item.corrected_snapshot.source_snapshot_hash
                != item.previous_snapshot.source_snapshot_hash
                or item.corrected_snapshot.mapping_hash
                != result.mapping.definition.content_hash
            ):
                raise CorrectionOriginError(
                    "Correction review requires current native prepared evidence"
                )
        corrected_prepared_hash = content_hash(
            [item.corrected_snapshot.to_portable_dict() for item in datasets]
        )
        return CorrectionReviewEvidence(
            mapping=result.mapping,
            previous_prepared_hash=manifest.prepared_set_hash,
            corrected_prepared_hash=corrected_prepared_hash,
            candidate_batches=self._candidate_batches(datasets),
            reader=result.reader,
            reader_scope_hash=result.reader_scope_hash,
            read_credential_binding_hash=result.read_credential_binding_hash,
            read_identity=result.read_identity,
            reviewed_at=result.reviewed_at,
        )

    @staticmethod
    def _candidate_batches(
        datasets: tuple[CorrectionDatasetReviewInput, ...],
    ) -> Iterator[tuple[CorrectionCandidate, ...]]:
        """Keep transient sparse files alive only while their batches are read."""

        with tempfile.TemporaryDirectory(prefix="impodo-correction-") as directory:
            root = Path(directory)
            for ordinal, item in enumerate(datasets):
                artifact = write_polars_correction_candidates(
                    item.previous_path,
                    item.previous_snapshot,
                    item.previous_program,
                    item.corrected_path,
                    item.corrected_snapshot,
                    item.corrected_program,
                    root / f"candidates-{ordinal:04d}.parquet",
                )
                yield from iter_polars_correction_candidate_batches(artifact)


__all__ = [
    "CorrectionAuthoringReviewResult",
    "CorrectionAuthoringStageOwner",
    "CorrectionDatasetReviewInput",
    "NativeCorrectionReviewPipeline",
]
