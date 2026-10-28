"""Verify storage-neutral artifact identities against governed local bytes."""

from __future__ import annotations

from impodo.application.shared.artifacts import ArtifactStoreError
from impodo.application.shared.cross_store_coordinator import (
    ArtifactEvidenceMismatchError,
)
from impodo.domain.shared.coordination import ArtifactIdentity

from .local_store import LocalArtifactStore


class LocalArtifactEvidenceVerifier:
    """Verify exact local prepared artifacts without exposing filesystem paths."""

    def __init__(self, artifacts: LocalArtifactStore) -> None:
        self._artifacts = artifacts

    def verify(self, artifact: ArtifactIdentity) -> None:
        """Require the supported owner, kind, hash, and exact byte size."""

        if (
            artifact.owner_kind != "workspace"
            or artifact.artifact_kind != "prepared_snapshot"
            or artifact.media_type != "application/vnd.apache.parquet"
        ):
            raise ArtifactEvidenceMismatchError(
                "Artifact identity is not a supported prepared snapshot"
            )
        try:
            with self._artifacts.materialize_prepared_snapshot(
                artifact.owner_id,
                artifact.storage_key,
                expected_sha256=artifact.sha256,
            ) as path:
                if path.stat().st_size != artifact.size_bytes:
                    raise ArtifactEvidenceMismatchError(
                        "Prepared artifact size does not match its receipt"
                    )
        except ArtifactEvidenceMismatchError:
            raise
        except (ArtifactStoreError, OSError) as error:
            raise ArtifactEvidenceMismatchError(
                "Prepared artifact bytes do not match their receipt"
            ) from error


__all__ = ("LocalArtifactEvidenceVerifier",)
