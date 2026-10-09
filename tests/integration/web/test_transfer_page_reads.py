"""Keep transfer-page reads bounded and off the event loop."""

from __future__ import annotations

import asyncio
from threading import get_ident
from unittest.mock import patch

from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from impodo.domain.workspace.workbench import (
    OdooConnectionMode,
    transfer_destination_identity_hash,
)
from impodo.web.routers import (
    destination_matching,
    transfer_destination,
    transfer_order,
    transfer_preflight,
    transfer_review,
)
from impodo.web.target_credentials import (
    TargetCredentialRole,
    store_target_credential,
)
from tests.support.browser_scenarios import ProjectSetupBrowserTestCase


class TransferPageReadTests(ProjectSetupBrowserTestCase):
    """Exercise transfer HTML GETs with real isolated DuckDB repositories."""

    def _verified_destination_workspace(self) -> str:
        """Build the shared transfer state without contacting either Odoo."""

        context = self.app.state.context
        frozen, _schema = self._frozen_odoo_source_workspace()
        configured = context.workspace_states.configure_transfer_destination(
            frozen.workspace_id,
            actor=context.actor,
            expected_revision=frozen.revision,
            odoo_connection_mode=OdooConnectionMode.REMOTE,
            odoo_base_url="https://destination.example.test",
            odoo_database="destination",
        )
        credential = store_target_credential(
            self.secrets,
            configured,
            TargetCredentialRole.DESTINATION_TRANSFER,
            "fictional-destination-key",
            persistent=False,
        )
        verified = context.workspace_states.verify_transfer_destination(
            frozen.workspace_id,
            actor=context.actor,
            expected_revision=configured.revision,
            target_hash=transfer_destination_identity_hash(configured),
            credential_binding_hash=credential.binding_hash,
            read_principal_hash="sha256:" + "1" * 64,
            odoo_version="19.0",
        )
        self.assertTrue(verified.destination_verified)
        return verified.workspace_id

    def _matched_destination_workspace(self) -> str:
        """Add a current match plan through the browser command boundary."""

        workspace_id = self._verified_destination_workspace()
        context = self.app.state.context
        workspace_state = context.queries.get(workspace_id)
        selection = context.queries.get_source_selection(workspace_id)
        schema = context.queries.get_odoo_schema_catalog(workspace_id)
        self.assertIsNotNone(selection)
        self.assertIsNotNone(schema)
        assert selection is not None and schema is not None
        dataset = selection.datasets[0]
        response = self._post(
            f"/workspaces/{workspace_id}/destination-matching",
            {
                "csrf_token": self.csrf,
                "revision": str(workspace_state.revision),
                "match_key": (
                    f"{dataset.dataset_id}::{dataset.columns[0].stable_key}"
                ),
                "destination_handling": f"{dataset.dataset_id}::transfer",
            },
        )
        self.assertEqual(response.status_code, 303, response.text)
        matched = context.queries.get(workspace_id)
        self.assertTrue(
            matched.destination_match_ready(
                source_selection_hash=selection.content_hash,
                source_schema_hash=schema.content_hash,
            )
        )
        return workspace_id

    def _ordered_transfer_workspace(self) -> str:
        """Add a current transfer order through the browser command boundary."""

        workspace_id = self._matched_destination_workspace()
        context = self.app.state.context
        matched = context.queries.get(workspace_id)
        response = self._post(
            f"/workspaces/{workspace_id}/transfer-order",
            {
                "csrf_token": self.csrf,
                "revision": str(matched.revision),
            },
        )
        self.assertEqual(response.status_code, 303, response.text)
        ordered = context.queries.get(workspace_id)
        self.assertIsNotNone(ordered.transfer_order_plan)
        assert ordered.transfer_order_plan is not None
        self.assertTrue(ordered.transfer_order_plan.ready)
        return workspace_id

    def _approved_transfer_workspace(self) -> str:
        """Add an approved review package through browser command boundaries."""

        workspace_id = self._ordered_transfer_workspace()
        context = self.app.state.context
        ordered = context.queries.get(workspace_id)
        built = self._post(
            f"/workspaces/{workspace_id}/transfer-review/build",
            {
                "csrf_token": self.csrf,
                "revision": str(ordered.revision),
            },
        )
        self.assertEqual(built.status_code, 303, built.text)
        reviewed = context.queries.get(workspace_id)
        self.assertIsNotNone(reviewed.transfer_review_package)
        approved = self._post(
            f"/workspaces/{workspace_id}/transfer-review/approve",
            {
                "csrf_token": self.csrf,
                "revision": str(reviewed.revision),
                "confirmation": "approve",
                "reason": "Reviewed fictional transfer evidence.",
            },
        )
        self.assertEqual(approved.status_code, 303, approved.text)
        approved_state = context.queries.get(workspace_id)
        self.assertIsNotNone(approved_state.transfer_review_approval)
        return workspace_id

    def _worker_probe(self, worker_threads: list[int]):
        """Wrap a page operation and record its non-event-loop worker."""

        def wrap(function):
            def call(*args, **kwargs):
                with self.assertRaises(RuntimeError):
                    asyncio.get_running_loop()
                worker_threads.append(get_ident())
                return function(*args, **kwargs)

            return call

        return wrap

    def test_destination_matching_reads_share_database_owners(self) -> None:
        workspace_id = self._verified_destination_workspace()
        context = self.app.state.context
        worker_threads: list[int] = []
        worker_probe = self._worker_probe(worker_threads)

        with (
            patch.object(
                context.queries,
                "get",
                side_effect=worker_probe(context.queries.get),
            ) as workspace_read,
            patch.object(
                context.queries,
                "get_source_selection",
                side_effect=worker_probe(context.queries.get_source_selection),
            ) as source_selection_read,
            patch.object(
                context.queries,
                "get_odoo_schema_catalog",
                side_effect=worker_probe(context.queries.get_odoo_schema_catalog),
            ) as schema_read,
            patch.object(
                destination_matching,
                "_render",
                side_effect=worker_probe(destination_matching._render),
            ) as render,
            collect_duckdb_request_timings() as timings,
        ):
            response = self.client.get(
                f"/workspaces/{workspace_id}/destination-matching",
                follow_redirects=False,
            )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("data-matching-builder", response.text)
        workspace_read.assert_called()
        source_selection_read.assert_called()
        schema_read.assert_called()
        render.assert_called_once()
        self.assertTrue(worker_threads)
        self.assertEqual(len(set(worker_threads)), 1)
        self.assertGreater(timings.connection_count, 0)
        self.assertLessEqual(timings.connection_count, 6)

    def test_transfer_destination_reads_share_database_owners(self) -> None:
        workspace_id = self._verified_destination_workspace()
        context = self.app.state.context
        worker_threads: list[int] = []
        worker_probe = self._worker_probe(worker_threads)

        with (
            patch.object(
                context.queries,
                "get",
                side_effect=worker_probe(context.queries.get),
            ) as workspace_read,
            patch.object(
                context.queries,
                "get_source_selection",
                side_effect=worker_probe(context.queries.get_source_selection),
            ) as source_selection_read,
            patch.object(
                context.remote_connections,
                "get",
                side_effect=worker_probe(context.remote_connections.get),
            ) as connection_status_read,
            patch.object(
                transfer_destination,
                "get_target_credential_status",
                side_effect=worker_probe(
                    transfer_destination.get_target_credential_status
                ),
            ) as credential_status_read,
            patch.object(
                transfer_destination,
                "_render",
                side_effect=worker_probe(transfer_destination._render),
            ) as render,
            patch.object(
                context.odoo_connection_tests,
                "test_read",
            ) as remote_connection_test,
            collect_duckdb_request_timings() as timings,
        ):
            response = self.client.get(
                f"/workspaces/{workspace_id}/transfer-destination",
                follow_redirects=False,
            )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn(
            "Connect the Odoo instance that will receive the data",
            response.text,
        )
        self.assertIn("Source and destination stay separate", response.text)
        workspace_read.assert_called()
        source_selection_read.assert_called()
        connection_status_read.assert_called()
        credential_status_read.assert_called()
        render.assert_called_once()
        remote_connection_test.assert_not_called()
        self.assertTrue(worker_threads)
        self.assertEqual(len(set(worker_threads)), 1)
        self.assertGreater(timings.connection_count, 0)
        self.assertLessEqual(timings.connection_count, 6)

    def test_transfer_destination_still_requires_frozen_source(self) -> None:
        workspace_state, _schema = self._registered_remote_schema_workspace()

        response = self.client.get(
            f"/workspaces/{workspace_state.workspace_id}/transfer-destination",
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 303, response.text)
        self.assertEqual(
            response.headers["location"],
            f"/workspaces/{workspace_state.workspace_id}/sources",
        )

    def test_transfer_order_reads_share_database_owners(self) -> None:
        workspace_id = self._matched_destination_workspace()
        context = self.app.state.context
        worker_threads: list[int] = []
        worker_probe = self._worker_probe(worker_threads)

        with (
            patch.object(
                context.queries,
                "get",
                side_effect=worker_probe(context.queries.get),
            ) as workspace_read,
            patch.object(
                context.queries,
                "get_source_selection",
                side_effect=worker_probe(context.queries.get_source_selection),
            ) as source_selection_read,
            patch.object(
                context.queries,
                "get_odoo_schema_catalog",
                side_effect=worker_probe(context.queries.get_odoo_schema_catalog),
            ) as schema_read,
            patch.object(
                transfer_order,
                "_render",
                side_effect=worker_probe(transfer_order._render),
            ) as render,
            collect_duckdb_request_timings() as timings,
        ):
            response = self.client.get(
                f"/workspaces/{workspace_id}/transfer-order",
                follow_redirects=False,
            )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn(
            "Put related Odoo records in a safe transfer order",
            response.text,
        )
        workspace_read.assert_called()
        source_selection_read.assert_called()
        schema_read.assert_called()
        render.assert_called_once()
        self.assertTrue(worker_threads)
        self.assertEqual(len(set(worker_threads)), 1)
        self.assertGreater(timings.connection_count, 0)
        self.assertLessEqual(timings.connection_count, 6)

    def test_transfer_review_reads_share_database_owners(self) -> None:
        workspace_id = self._ordered_transfer_workspace()
        context = self.app.state.context
        worker_threads: list[int] = []
        worker_probe = self._worker_probe(worker_threads)

        with (
            patch.object(
                context.queries,
                "get",
                side_effect=worker_probe(context.queries.get),
            ) as workspace_read,
            patch.object(
                context.queries,
                "get_source_selection",
                side_effect=worker_probe(context.queries.get_source_selection),
            ) as source_selection_read,
            patch.object(
                context.queries,
                "get_odoo_schema_catalog",
                side_effect=worker_probe(context.queries.get_odoo_schema_catalog),
            ) as schema_read,
            patch.object(
                transfer_review,
                "_render",
                side_effect=worker_probe(transfer_review._render),
            ) as render,
            collect_duckdb_request_timings() as timings,
        ):
            response = self.client.get(
                f"/workspaces/{workspace_id}/transfer-review",
                follow_redirects=False,
            )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("Approve the exact Odoo transfer package", response.text)
        workspace_read.assert_called()
        source_selection_read.assert_called()
        schema_read.assert_called()
        render.assert_called_once()
        self.assertTrue(worker_threads)
        self.assertEqual(len(set(worker_threads)), 1)
        self.assertGreater(timings.connection_count, 0)
        self.assertLessEqual(timings.connection_count, 6)

    def test_transfer_preflight_reads_share_database_owners(self) -> None:
        workspace_id = self._approved_transfer_workspace()
        context = self.app.state.context
        worker_threads: list[int] = []
        worker_probe = self._worker_probe(worker_threads)

        with (
            patch.object(
                context.queries,
                "get",
                side_effect=worker_probe(context.queries.get),
            ) as workspace_read,
            patch.object(
                context.queries,
                "get_source_selection",
                side_effect=worker_probe(context.queries.get_source_selection),
            ) as source_selection_read,
            patch.object(
                context.queries,
                "get_odoo_schema_catalog",
                side_effect=worker_probe(context.queries.get_odoo_schema_catalog),
            ) as schema_read,
            patch.object(
                transfer_preflight,
                "get_target_credential_status",
                side_effect=worker_probe(
                    transfer_preflight.get_target_credential_status
                ),
            ) as credential_status_read,
            patch.object(
                transfer_preflight,
                "_render",
                side_effect=worker_probe(transfer_preflight._render),
            ) as render,
            patch.object(context, "read_identity_probe") as remote_identity_probe,
            collect_duckdb_request_timings() as timings,
        ):
            response = self.client.get(
                f"/workspaces/{workspace_id}/transfer-preflight",
                follow_redirects=False,
            )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("Recheck the destination before loading", response.text)
        workspace_read.assert_called()
        source_selection_read.assert_called()
        schema_read.assert_called()
        credential_status_read.assert_called_once()
        render.assert_called_once()
        remote_identity_probe.assert_not_called()
        self.assertTrue(worker_threads)
        self.assertEqual(len(set(worker_threads)), 1)
        self.assertGreater(timings.connection_count, 0)
        self.assertLessEqual(timings.connection_count, 6)
