"""Regression evidence for the governance-light ordinary migration path."""

from __future__ import annotations

import unittest
from unittest.mock import Mock

from impodo.access import LOCAL_ACTOR
from impodo.application.resolution_service import ResolutionService


HASH = "sha256:" + "a" * 64


class PracticalPreparationPathTests(unittest.TestCase):
    """Keep Slice 6 facilities optional for an ordinary preparation run."""

    def test_missing_advanced_inputs_produce_the_legacy_pass_through_path(self) -> None:
        repository = Mock()
        repository.get_validated_reference_bundle.return_value = None
        repository.get_resolution_policy.return_value = None
        service = ResolutionService(repository)

        reference_bundle = service.current_reference_bundle("project-1")
        effective, summary = service.evaluate_for_preparation(
            "project-1",
            Mock(),
            staging_run_id="staging-1",
            staging_content_hash=HASH,
            actor=LOCAL_ACTOR,
        )

        self.assertIsNone(reference_bundle)
        self.assertIsNone(effective)
        self.assertIsNone(summary)
        repository.publish_resolution_evaluation.assert_not_called()
        repository.freeze_effective_dataset.assert_not_called()


if __name__ == "__main__":
    unittest.main()
