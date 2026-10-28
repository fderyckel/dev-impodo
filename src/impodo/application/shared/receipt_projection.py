"""Build a deterministic bounded control-plane projection from one receipt."""

from __future__ import annotations

from uuid import NAMESPACE_URL, uuid5

from impodo.domain.serialization import content_hash
from impodo.domain.shared.coordination import (
    ControlPlaneProjectionReceipt,
    DataPlaneOperationReceipt,
)


class ReceiptFactsProjectionUpdater:
    """Project only receipt facts that PostgreSQL can safely retain."""

    def project(
        self,
        receipt: DataPlaneOperationReceipt,
    ) -> ControlPlaneProjectionReceipt:
        """Return the same revision for every replay of the exact receipt."""

        projection_hash = content_hash(
            {
                "artifact_count": len(receipt.artifacts),
                "artifact_hashes": [
                    artifact.sha256 for artifact in receipt.artifacts
                ],
                "data_plane_receipt_hash": receipt.semantic_hash,
                "evidence_hash": receipt.evidence_hash,
                "operation_kind": receipt.operation_kind,
                "workspace_id": receipt.workspace_id,
            }
        )
        return ControlPlaneProjectionReceipt.for_data_plane(
            receipt,
            projection_receipt_id=str(
                uuid5(
                    NAMESPACE_URL,
                    f"impodo:receipt-projection:{receipt.semantic_hash}",
                )
            ),
            projection_hash=projection_hash,
            projection_revision=1,
            projected_at=receipt.committed_at,
        )


__all__ = ("ReceiptFactsProjectionUpdater",)
