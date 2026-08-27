from __future__ import annotations

from inspect import getsource
import unittest

from impodo.application.shared.build_contract import (
    PROCESS_BUILD_CONTRACT,
    calculate_application_build_contract,
)
from impodo.web.app import create_local_app


class ApplicationBuildContractTests(unittest.TestCase):
    def test_process_contract_matches_the_unchanged_package(self) -> None:
        self.assertEqual(
            PROCESS_BUILD_CONTRACT,
            calculate_application_build_contract(),
        )

    def test_web_app_has_no_request_time_build_hash_middleware(self) -> None:
        composition = getsource(create_local_app)

        self.assertNotIn("BuildConsistencyMiddleware", composition)
        self.assertNotIn("calculate_application_build_contract", composition)


if __name__ == "__main__":
    unittest.main()
