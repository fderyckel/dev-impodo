"""Exercise dataset-route observations through real preparation collaborators."""

from unittest.mock import patch

from impodo.domain.mapping.contracts import (
    DatasetMapping,
    IdentityComponentMapping,
    ScalarFieldMapping,
)
from impodo.domain.recipe.value_rules import ScalarTransformPolicy
from tests.support.hierarchy_workflow import HierarchyWorkflowFixture


class PreparationDatasetRouteTests(HierarchyWorkflowFixture):
    def prepared_workspace(self, *, major=19, mode="LOCAL", formula=False):
        workspace_id = self.freeze_files(
            {
                "records": b"Reference,Name\nR1,10\nR2,20\n",
            }
        )
        self.schema(workspace_id, major=major, mode=mode)
        selection = self.context.queries.get_source_selection(workspace_id)
        dataset = selection.datasets[0]
        columns = {item.source_name: item.stable_key for item in dataset.columns}
        self.submit(
            workspace_id,
            (
                DatasetMapping(
                    dataset_id=dataset.dataset_id,
                    target_model="product.template",
                    source_identity_column_keys=(columns["Reference"],),
                    target_identity=(
                        IdentityComponentMapping(
                            (columns["Reference"],),
                            ("default_code",),
                        ),
                    ),
                    fields=(
                        ScalarFieldMapping(
                            "name",
                            columns["Name"],
                            transform=ScalarTransformPolicy(
                                formula="value / 10" if formula else ""
                            ),
                        ),
                    ),
                ),
            ),
        )
        return workspace_id

    @staticmethod
    def routes(events):
        return [
            item["workload"]
            for item in events
            if "dataset_routes" in item.get("workload", {})
        ]

    def test_native_route_covers_both_odoo_majors_and_access_modes(self):
        for major in (19, 20):
            for mode in ("LOCAL", "REMOTE"):
                with self.subTest(major=major, mode=mode):
                    workspace_id = self.prepared_workspace(major=major, mode=mode)
                    events = []
                    result = self.context.preparation.prepare(
                        workspace_id,
                        actor=self.actor,
                        performance=events.append,
                    )
                    observation = self.routes(events)[-1]
                    (record,) = observation["dataset_routes"]
                    self.assertEqual(record["predicted_route"], "NATIVE_COLUMNAR")
                    self.assertEqual(record["actual_route"], "NATIVE_COLUMNAR")
                    self.assertEqual(record["adaptation_route"], "NATIVE_PROJECTION")
                    self.assertEqual(record["outcome"], "SUCCEEDED")
                    self.assertEqual(record["row_count"], 2)
                    self.assertEqual(result.workspace_id, workspace_id)

    def test_formula_route_is_bounded_python_and_keeps_its_reason(self):
        workspace_id = self.prepared_workspace(major=20, mode="REMOTE", formula=True)
        events = []
        self.context.preparation.prepare(
            workspace_id,
            actor=self.actor,
            performance=events.append,
        )
        snapshots = self.routes(events)
        (record,) = snapshots[-1]["dataset_routes"]
        self.assertEqual(record["predicted_route"], "BOUNDED_PYTHON")
        self.assertEqual(record["actual_route"], "BOUNDED_PYTHON")
        self.assertEqual(record["adaptation_route"], "PYTHON_RECORDS")
        self.assertIn("COLUMNAR_FORMULA_UNSUPPORTED", record["reason_codes"])
        self.assertTrue(
            all(item["dataset_route_coverage"]["row_count"] == 2 for item in snapshots)
        )

    def test_native_projection_miss_is_one_dataset_with_python_adaptation(self):
        workspace_id = self.prepared_workspace()
        events = []
        with patch(
            "impodo.adapters.duckdb.preparation_direct_writer.supports_clean_native_projection",
            return_value=False,
        ):
            self.context.preparation.prepare(
                workspace_id,
                actor=self.actor,
                performance=events.append,
            )
        observations = self.routes(events)
        self.assertEqual(len(observations), 3)
        (record,) = observations[-1]["dataset_routes"]
        self.assertEqual(record["actual_route"], "NATIVE_COLUMNAR")
        self.assertEqual(record["adaptation_route"], "PYTHON_ROW_ADAPTATION")
        self.assertEqual(record["outcome"], "SUCCEEDED")
        self.assertEqual(
            observations[-1]["dataset_route_coverage"]["captured_row_count"], 2
        )

    def test_interruption_retains_partial_observation_and_retry_keeps_meaning(self):
        workspace_id = self.prepared_workspace()
        events = []
        with (
            patch(
                "impodo.adapters.duckdb.preparation_direct_writer.append_clean_native_projection",
                side_effect=RuntimeError("injected evaluator interruption"),
            ),
            self.assertRaisesRegex(RuntimeError, "injected evaluator interruption"),
        ):
            self.context.preparation.prepare(
                workspace_id,
                actor=self.actor,
                performance=events.append,
            )
        (failed,) = self.routes(events)[-1]["dataset_routes"]
        self.assertEqual(failed["outcome"], "INTERRUPTED")
        self.assertNotEqual(failed["adaptation_route"], "PYTHON_ROW_ADAPTATION")
        events.clear()
        first = self.context.preparation.prepare(
            workspace_id,
            actor=self.actor,
            performance=events.append,
        )
        second = self.context.preparation.prepare(workspace_id, actor=self.actor)
        self.assertEqual(first.content_hash, second.content_hash)
        (finished,) = self.routes(events)[-1]["dataset_routes"]
        self.assertEqual(finished["outcome"], "SUCCEEDED")
