"""Exercise compact navigation on both new engines and populated version-nine engines."""

from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import duckdb
import pytest

from impodo.adapters.duckdb.database import DuckDbWorkspaceDatabase
from impodo.adapters.duckdb.navigation_repository import WorkspaceNavigationRepository
from impodo.adapters.duckdb.workspace_state_repository import WorkspaceStateRepository
from impodo.application.transfer_order_service import TransferOrderService
from impodo.application.workspace.navigation import TransferNavigationProgress
from impodo.domain.shared.access import LOCAL_ACTOR
from impodo.domain.workspace.destination_matching import DestinationMatchPlan
from impodo.domain.workspace.workbench import (
    OdooConnectionMode,
    transfer_destination_identity_hash,
)
from tests.application.workspace.test_transfer_order import (
    _match_plan,
    _model,
    _workspace,
)
from tests.integration.duckdb.test_forward_upgrades import _schema_fingerprint
from tests.support.odoo_database_bindings import reviewed_destination


@pytest.mark.parametrize("major", (19, 20))
@pytest.mark.parametrize("mode", (OdooConnectionMode.LOCAL, OdooConnectionMode.REMOTE))
def test_large_saved_transfer_and_populated_upgrade(major, mode):
    match = _match_plan((_model("product.template", "Product", create=2),), ())
    original = replace(
        _workspace(match),
        destination_verified_odoo_version=f"{major}.0",
        destination_odoo_connection_mode=mode,
    )
    target = transfer_destination_identity_hash(original)
    sizes = []
    with TemporaryDirectory() as directory:
        for excluded in (0, 25000):
            model = replace(
                match.model_matches[0],
                excluded_source_row_numbers=tuple(range(1, excluded + 1)),
            )
            saved = replace(
                match, destination_target_hash=target, model_matches=(model,)
            )
            workspace = replace(
                original,
                destination_match_plan=saved,
                destination_verified_target_hash=target,
            )
            order = TransferOrderService().build(
                workspace, saved, recorded_by="Data manager"
            )
            workspace = replace(workspace, transfer_order_plan=order)
            database = DuckDbWorkspaceDatabase(Path(directory) / str(excluded))
            states = WorkspaceStateRepository(database)
            states.initialize_workbench(workspace, actor=LOCAL_ACTOR)
            navigation = WorkspaceNavigationRepository(
                database, destination_binding=reviewed_destination
            )
            path = (
                database.workspace_directory(workspace.workspace_id)
                / "workspace-engine.duckdb"
            )
            expected = TransferNavigationProgress.from_workspace(workspace)
            assert expected.order_ready and expected.match_ready


            assert_bounded_progress(navigation, workspace.workspace_id, expected)
            with duckdb.connect(str(path)) as connection:
                sizes.append(
                    connection.execute(
                        "SELECT length(to_json(t)) FROM transfer_navigation t"
                    ).fetchone()[0]
                )
                connection.execute("DROP TABLE transfer_navigation")
                connection.execute("UPDATE schema_version SET version = 9")
                assert (
                    _schema_fingerprint(connection)
                    == "abf25ce311d044d7690ce6bb2670ee2fca6f18c795b6c52dd03d0a4bd785fa89"
                )
            # The first open backfills from the authoritative plan before subsequent bounded reads.
            upgraded = navigation.get_snapshot(workspace.workspace_id)
            assert upgraded.facts.transfer_progress == expected
            assert_bounded_progress(navigation, workspace.workspace_id, expected)
            assert (
                states.get(workspace.workspace_id).destination_match_plan.content_hash
                == saved.content_hash
            )
    assert sizes[0] == sizes[1]


def assert_bounded_progress(navigation, workspace_id, expected):
    with patch.object(
        DestinationMatchPlan,
        "from_json",
        side_effect=AssertionError("full matching read"),
    ):
        snapshot = navigation.get_snapshot(workspace_id)
    assert snapshot.facts.transfer_progress == expected
    assert snapshot.workspace_state.destination_verified
    assert snapshot.workspace_state.destination_match_plan is None

