from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from impodo.build_contract import (
    ApplicationBuildContract,
    PROCESS_BUILD_CONTRACT,
)
from impodo.source_worker import _worker


class SourceWorkerBuildContractTests(unittest.TestCase):
    def test_changed_build_stops_before_source_file_is_opened(self) -> None:
        sender = MagicMock()
        start_event = MagicMock()
        changed = ApplicationBuildContract(
            application_build_id="sha256:" + "0" * 64,
            workspace_schema_generation=(
                PROCESS_BUILD_CONTRACT.workspace_schema_generation
            ),
            workspace_schema_version=(
                PROCESS_BUILD_CONTRACT.workspace_schema_version
            ),
        )

        with patch("impodo.source_worker.validate_source_file") as validate:
            _worker("customers.csv", changed, sender, start_event)

        start_event.wait.assert_called_once_with()
        validate.assert_not_called()
        status, message = sender.send.call_args.args[0]
        self.assertEqual(status, "error")
        self.assertIn("Restart Impodo", message)
        sender.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
