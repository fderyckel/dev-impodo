"""Exercise delayed admission and hard process loss on the authenticated save route."""

import asyncio
import multiprocessing
import os
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient

from impodo.adapters.protected_evidence.credential_vault import MemorySecretStore
from impodo.web.app import create_local_app
from impodo.web.routers import mapping as routes
from tests.integration.web.test_mapping_command_performance import MappingCommandFixture
from tests.support.browser_scenarios import POST_HEADERS


def _serve(root, session, workspace, entries, phase, result):
    """Serve the original HTTP operation or read its outcome in a fresh process."""
    app = create_local_app(
        root,
        launch_token="launch-secret",
        session_secret="session-secret",
        secret_store=MemorySecretStore(),
    )
    service = app.state.context.mapping_workspace
    fields = dict(entries)
    url = f"/workspaces/{workspace}/mapping"
    with TestClient(app, cookies={"impodo_session": session}) as client:
        if phase == "recover":
            receipt = client.get(f"{url}/mutation-receipts/{fields['operation_id']}")
            receipt.raise_for_status()
            working = app.state.context.queries.get_mapping_working_draft(workspace)
            body = receipt.json()
            result.send(
                (
                    body["operation_id"],
                    body["status"],
                    working.version,
                    body.get("content_identity") == working.content_hash,
                )
            )
            return

        begin = service.begin_mutation

        def reserve(*args, **kwargs):
            if phase == "before_reservation":
                os._exit(73)
            reserved = begin(*args, **kwargs)
            if phase == "after_reservation":
                os._exit(73)
            return reserved

        with patch.object(service, "begin_mutation", reserve):
            response = client.post(
                f"{url}/save",
                json={"entries": entries},
                headers={**POST_HEADERS, "X-CSRF-Token": fields["csrf_token"]},
            )
            assert response.json()["status"] == "committed", response.status_code
            # The caller has received no response when this process dies.
            os._exit(73)


class InterruptedMappingSaveTests(MappingCommandFixture):
    def test_late_admission_can_commit_after_an_absent_receipt(self):
        workspace, entries = self.command_fixture()
        operation_id = dict(entries)["operation_id"]
        path = f"/workspaces/{workspace}/mapping/mutation-receipts/{operation_id}"
        arrived, release = Event(), Event()
        run = routes.run_local_operation

        async def delay_command(function, *args, **kwargs):
            if function.__name__ == "run_mapping_command":
                arrived.set()
                self.assertTrue(await asyncio.to_thread(release.wait, 15))
            return await run(function, *args, **kwargs)

        with self.client, patch.object(routes, "run_local_operation", delay_command):
            with ThreadPoolExecutor(max_workers=1) as pool:
                save = pool.submit(self.post_command, workspace, entries)
                try:
                    self.assertTrue(arrived.wait(15))
                    missing = self.client.get(path).json()
                    self.assertEqual(missing["status"], "not_found")
                    self.assertNotIn("Nothing was saved", missing["message"])
                finally:
                    release.set()
                self.assertEqual(save.result(timeout=15).json()["status"], "committed")
            final = self.client.get(path).json()
            self.assertEqual(final["operation_id"], operation_id)
            self.assertEqual(final["expected_working_draft_version"], 2)

    def test_same_session_recovers_receipt_after_hard_process_exit(self):
        workspace, entries = self.command_fixture(20)
        session = self.client.cookies["impodo_session"]
        processes = multiprocessing.get_context("spawn")
        for phase, expected, version in (
            ("before_reservation", "not_found", 1),
            ("after_reservation", "pending", 1),
            ("after_commit", "committed", 2),
        ):
            with self.subTest(phase=phase):
                operation_id = str(uuid4())
                command = self.changed_entries(entries, operation_id=operation_id)
                reader, writer = processes.Pipe(duplex=False)
                with reader, writer:
                    for step, exitcode in ((phase, 73), ("recover", 0)):
                        process = processes.Process(
                            target=_serve,
                            args=(
                                self.temporary.name,
                                session,
                                workspace,
                                command,
                                step,
                                writer,
                            ),
                        )
                        process.start()
                        try:
                            process.join(40)
                            self.assertEqual(process.exitcode, exitcode)
                        finally:
                            if process.is_alive():
                                process.kill()
                                process.join(10)
                            process.close()
                    self.assertTrue(reader.poll(2))
                    self.assertEqual(
                        reader.recv(),
                        (operation_id, expected, version, expected == "committed"),
                    )
