from __future__ import annotations

import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from impodo.build_contract import (
    ApplicationBuildContract,
    PROCESS_BUILD_CONTRACT,
    calculate_application_build_contract,
)
from impodo.web.security import BuildConsistencyMiddleware


class ApplicationBuildContractTests(unittest.TestCase):
    def test_process_contract_matches_the_unchanged_package(self) -> None:
        self.assertEqual(
            PROCESS_BUILD_CONTRACT,
            calculate_application_build_contract(),
        )

    def test_web_request_stops_when_the_running_build_changed(self) -> None:
        app = FastAPI()
        app.add_middleware(
            BuildConsistencyMiddleware,
            expected=ApplicationBuildContract(
                application_build_id="sha256:" + "0" * 64,
                workspace_schema_generation=(
                    PROCESS_BUILD_CONTRACT.workspace_schema_generation
                ),
                workspace_schema_version=(
                    PROCESS_BUILD_CONTRACT.workspace_schema_version
                ),
            ),
            check_interval_seconds=0,
        )

        @app.get("/")
        async def index():
            return {"status": "unsafe"}

        response = TestClient(app).get("/")

        self.assertEqual(response.status_code, 409)
        self.assertIn("Restart Impodo", response.text)
        self.assertIn("saved work is unchanged", response.text)


if __name__ == "__main__":
    unittest.main()
