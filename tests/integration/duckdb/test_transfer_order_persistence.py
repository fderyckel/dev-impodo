from __future__ import annotations

from datetime import UTC, datetime
import tempfile
import unittest
from uuid import uuid4

from impodo.adapters.duckdb.database import DuckDbWorkspaceDatabase
from impodo.adapters.duckdb.workspace_state_repository import WorkspaceStateRepository
from impodo.domain.shared.access import LOCAL_ACTOR
from impodo.domain.workspace.transfer_order import (
    TransferOrderDataset,
    TransferOrderPlan,
    TransferOrderWave,
)
from impodo.domain.workspace.workbench import (
    SourceMode,
    WorkspaceState,
    WorkspaceStatus,
)
from tests.support.paths import REPOSITORY_ROOT


HASHES = tuple("sha256:" + character * 64 for character in "abcde")


class TransferOrderPersistenceTests(unittest.TestCase):
    def test_workspace_engine_round_trips_stage_six_evidence(self) -> None:
        workspace_id = str(uuid4())
        dataset_id = str(uuid4())
        now = datetime.now(UTC)
        plan = TransferOrderPlan(
            workspace_id=workspace_id,
            destination_match_plan_hash=HASHES[0],
            source_selection_hash=HASHES[1],
            source_schema_hash=HASHES[2],
            destination_target_hash=HASHES[3],
            datasets=(
                TransferOrderDataset(
                    dataset_id=dataset_id,
                    dataset_name="Products",
                    model="product.template",
                    model_label="Product",
                    source_row_count=2,
                    destination_existing_key_count=1,
                    destination_create_key_count=1,
                    wave=1,
                ),
            ),
            dependencies=(),
            waves=(TransferOrderWave(sequence=1, dataset_ids=(dataset_id,)),),
            blockers=(),
            recorded_at=now,
            recorded_by="Data manager",
        )
        workspace = WorkspaceState(
            workspace_id=workspace_id,
            name="Odoo transfer",
            source_system="Odoo",
            source_mode=SourceMode.ODOO,
            status=WorkspaceStatus.REGISTERED,
            transfer_order_plan=plan,
        )

        (REPOSITORY_ROOT / ".tmp").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=REPOSITORY_ROOT / ".tmp") as directory:
            repository = WorkspaceStateRepository(
                DuckDbWorkspaceDatabase(directory)
            )
            repository.initialize_workbench(workspace, actor=LOCAL_ACTOR)

            restored = repository.get(workspace_id)

        self.assertEqual(restored.transfer_order_plan, plan)
        self.assertEqual(restored.transfer_order_plan.content_hash, plan.content_hash)


if __name__ == "__main__":
    unittest.main()
