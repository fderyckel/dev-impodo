"""Qualify direct relationships and relational identities in real workers."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from tests.performance import test_preparation_scale as fixtures


@unittest.skipUnless(
    os.environ.get("IMPODO_RUN_PREPARATION_SCALE") == "1",
    "opt-in preparation scale qualification",
)
class RelationshipPreparationScaleTests(unittest.TestCase):
    """Verify first/repeat publication, source reuse, and released worker memory."""

    setUp = fixtures.PreparationWorkflowScaleTests.setUp
    tearDown = fixtures.PreparationWorkflowScaleTests.tearDown
    _prepare_related_product_bom_project_and_evidence = (
        fixtures.PreparationWorkflowScaleTests._prepare_related_product_bom_project_and_evidence
    )

    def _qualify(self, *, workload: str, rows: int) -> None:
        # Only fixture volume and model shape change; admission, transformations,
        # all downstream checks, persistence, and worker execution remain real.
        with patch.multiple(
            fixtures,
            PREPARATION_SCALE_WORKLOAD=workload,
            PREPARATION_SCALE_PRODUCTS=2_000,
            PREPARATION_SCALE_BOM_LINES=rows - 2_000,
            PREPARATION_SCALE_COLUMNS=8,
            PREPARATION_SCALE_MAPPED_FIELDS=6,
            PREPARATION_SCALE_EFFECT_FIELDS=1,
        ):
            fixtures.PreparationWorkflowScaleTests.test_background_worker_releases_its_working_memory(
                self,
            )

    def test_reported_54_191_row_relationship_selection(self) -> None:
        self._qualify(workload="product-bom", rows=54_191)

    def test_custom_relational_identity_at_100_000_rows(self) -> None:
        self._qualify(workload="documents", rows=100_000)
