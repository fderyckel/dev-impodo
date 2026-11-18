"""Exercise real Stage 3 order commands across Odoo versions and access modes."""

from threading import Event
from time import monotonic, sleep
from unittest.mock import patch

from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from impodo.adapters.duckdb.unit_of_work import _read_databases
from impodo.domain.odoo.contracts import ConnectorTransportError
from tests.support.browser_scenarios import POST_HEADERS, ProjectSetupBrowserTestCase
from tests.support.database_probe import count_duckdb_statements
from tests.support.hierarchy_workflow import HierarchyWorkflowFixture
from tests.support.matching_order_workspace import ordering_workspace


class MatchingOrderActionsTests(HierarchyWorkflowFixture):
    def test_live_ordering_and_recovery_across_targets(self):
        for major in (19, 20):
            for mode in ("LOCAL", "REMOTE"):
                with self.subTest(major=major, mode=mode):
                    self.check_order_actions(major, mode)

    def check_order_actions(self, major, mode):
        wid, reads = ordering_workspace(self, major=major, mode=mode, partial=True)
        ctx = self.context
        url = f"/workspaces/{wid}/mapping"
        query = "?step=tables&task=links&mapping_dataset=1&order_panel=1"
        route = url + "/order/check" + query

        def post(path, form):
            return self.client.post(
                path,
                data={"csrf_token": self.csrf, **form},
                headers=POST_HEADERS,
                follow_redirects=False,
            )

        def finish(started):
            self.assertEqual(started.status_code, 303)
            location = started.headers["location"]
            self.assertIn("mapping_dataset=1", location)
            self.assertIn("task=links", location)
            check_id = str(started.next_request.url.params["order_check"])
            deadline = monotonic() + 20
            while monotonic() < deadline:
                status = self.client.get(f"{url}/order/check/{check_id}").json()
                if status["terminal"]:
                    return status, self.client.get(location)
                sleep(0.05)
            self.fail("The order check did not finish")

        page = self.client.get(url + query)
        initial = page.context["matching_order"]
        active = initial["active_dataset_id"]
        ids = [r["dataset_id"] for r in initial["rows"]]
        self.assertEqual(reads, [])

        def order(action, version, permutation, **extra):
            return post(
                url + "/order" + query,
                {
                    "action": action,
                    "dataset_order": permutation,
                    "active_dataset_id": active,
                    "expected_order_version": version,
                    **extra,
                },
            )

        self.assertEqual(order("move_down:" + ids[0], "", ids).status_code, 303)
        self.assertEqual(order("reset", "1", ids[::-1]).status_code, 303)
        auto = self.client.get(url + query).context["matching_order"]
        self.assertFalse(auto["custom_order"])
        self.assertEqual(auto["preference_version"], 2)
        self.assertEqual(order("save", "2", ids[::-1]).status_code, 303)
        conflict = order("save", "1", ids)
        self.assertEqual(conflict.status_code, 409)
        self.assertTrue(conflict.context["matching_order"]["custom_warnings"])
        self.assertEqual(reads, [])

        entered, release = Event(), Event()
        reader = ctx.readiness_reader

        def slow(*args):
            self.assertIsNone(getattr(_read_databases, "connections", None))
            entered.set()
            if not release.wait(20):
                raise RuntimeError("Fixture did not release the reader")
            return reader(*args)

        with patch.object(ctx, "readiness_reader", slow):
            with collect_duckdb_request_timings() as admission:
                started = post(route, {})
            try:
                self.assertLessEqual(admission.connection_count, 6)
                self.assertLessEqual(admission.schema_check_count, 4)
                self.assertTrue(entered.wait(10))
                reopened = self.client.get(url + query).context["matching_order"]
                self.assertTrue(reopened["check"]["running"])
                self.assertEqual(reopened["active_dataset_id"], active)
                with (
                    patch.object(
                        ctx.matching_order,
                        "prepare_live_check",
                        side_effect=AssertionError("Duplicate source scan"),
                    ),
                    count_duckdb_statements() as sql,
                    collect_duckdb_request_timings() as timings,
                ):
                    duplicate = post(route, {})
                self.assertEqual(
                    duplicate.headers["location"], started.headers["location"]
                )
                self.assertLessEqual(timings.connection_count, 5)
                self.assertLessEqual(len(sql), 35)
            finally:
                release.set()
            status, page = finish(started)
        self.assertEqual(status["status"], "SUCCEEDED")
        current = page.context["matching_order"]
        self.assertEqual(current["status_label"], "Partial")
        self.assertTrue(current["check"]["can_apply"])
        self.assertEqual([r["dataset_id"] for r in current["rows"]], ids[::-1])
        self.assertEqual(current["active_dataset_id"], active)
        with patch.object(
            ctx, "readiness_reader", side_effect=ConnectorTransportError("fictional")
        ):
            failed, page = finish(post(route, {}))
        self.assertEqual(failed["status"], "FAILED")
        panel = page.text.split('aria-labelledby="matching-order-title"', 1)[1].split(
            "</section>", 1
        )[0]
        self.assertIn('role="alert"', panel)
        self.assertIn("connection to Odoo failed", panel)
        self.assertEqual(page.context["matching_order"]["status_label"], "Partial")
        rejected = order(
            "apply_recommendation", "3", ids[::-1], expected_check_id=failed["check_id"]
        )
        self.assertEqual(rejected.status_code, 422)
        applied = order(
            "apply_recommendation", "3", ids[::-1], expected_check_id=status["check_id"]
        )
        self.assertEqual(applied.status_code, 303)
        final = self.client.get(applied.headers["location"]).context["matching_order"]
        self.assertEqual([r["dataset_id"] for r in final["rows"]], ids)
        self.assertEqual(final["active_dataset_id"], active)
        self.assertEqual(len(reads), 1)


class SavedMatchingOrderTests(ProjectSetupBrowserTestCase):
    def test_table_order_can_be_saved_conflict_checked_and_reset(self) -> None:
        workspace_id, dataset, _business_key = self._mapping_ready_workspace(
            scalar_field_count=1
        )
        order_url = f"/workspaces/{workspace_id}/mapping/order"

        initial = self.client.get(f"/workspaces/{workspace_id}/mapping")

        self.assertEqual(initial.status_code, 200, initial.text)
        self.assertIn("Reorder tables", initial.text)
        self.assertIn("Save table order", initial.text)
        self.assertIn("Move up", initial.text)
        self.assertIn("Move down", initial.text)
        self.assertIn("/static/mapping-order.js", initial.text)

        rejected = self.client.post(
            order_url,
            data={
                "csrf_token": "wrong",
                "action": "save",
                "dataset_order": dataset.dataset_id,
                "expected_order_version": "",
                "active_dataset_id": dataset.dataset_id,
            },
            headers=POST_HEADERS,
        )
        self.assertEqual(rejected.status_code, 403, rejected.text)

        saved = self.client.post(
            order_url,
            data={
                "csrf_token": self.csrf,
                "action": "save",
                "dataset_order": dataset.dataset_id,
                "expected_order_version": "",
                "active_dataset_id": dataset.dataset_id,
            },
            headers=POST_HEADERS,
            follow_redirects=False,
        )

        self.assertEqual(saved.status_code, 303, saved.text)
        custom = self.client.get(saved.headers["location"])
        self.assertEqual(custom.status_code, 200, custom.text)
        self.assertIn("Custom order", custom.text)
        self.assertIn("Use Impodo order", custom.text)

        stale = self.client.post(
            order_url,
            data={
                "csrf_token": self.csrf,
                "action": "save",
                "dataset_order": dataset.dataset_id,
                "expected_order_version": "",
                "active_dataset_id": dataset.dataset_id,
            },
            headers=POST_HEADERS,
        )

        self.assertEqual(stale.status_code, 409, stale.text)
        self.assertIn("newer order was saved", stale.text)
        self.assertIn("Custom order", stale.text)

        reset = self.client.post(
            order_url,
            data={
                "csrf_token": self.csrf,
                "action": "reset",
                "dataset_order": dataset.dataset_id,
                "expected_order_version": "1",
                "active_dataset_id": dataset.dataset_id,
            },
            headers=POST_HEADERS,
            follow_redirects=False,
        )

        self.assertEqual(reset.status_code, 303, reset.text)
        restored = self.client.get(reset.headers["location"])
        self.assertEqual(restored.status_code, 200, restored.text)
        self.assertNotIn("Custom order", restored.text)
        self.assertNotIn("Use Impodo order", restored.text)
