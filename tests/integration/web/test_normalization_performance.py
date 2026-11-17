"""Bound complete prepared-review requests without changing durable decisions."""

import asyncio
from dataclasses import replace
from time import perf_counter
from unittest.mock import patch

import duckdb

from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from impodo.adapters.duckdb.unit_of_work import (
    DuckDbConnectionFactory,
    _read_databases,
)
from impodo.domain.mapping.contracts import ScalarValueSource
from impodo.web.routers import normalization
from tests.support.database_probe import count_duckdb_statements
from tests.support.hierarchy_workflow import HierarchyWorkflowFixture


class NormalizationPerformanceTests(HierarchyWorkflowFixture):
    def prepared(self, major=19, mode="LOCAL"):
        workspace_id, revision = self.hierarchy(major=major, mode=mode)
        datasets = tuple(
            replace(
                dataset,
                fields=tuple(
                    replace(
                        field,
                        value_source=ScalarValueSource.CONSTANT,
                        source_column_key=None,
                        literal_value="Imported product",
                    )
                    if field.target_field == "name"
                    else field
                    for field in dataset.fields
                ),
            )
            if dataset.target_model == "product.template"
            else dataset
            for dataset in revision.definition.datasets
        )
        self.submit(workspace_id, datasets)
        self.prepare(workspace_id)
        summary, groups, _, _ = self.context.normalization.current_group_review(
            workspace_id
        )
        group = next(group for group in groups if group.requires_decision)
        return workspace_id, summary, group

    def form(self, summary, **values):
        return {
            "csrf_token": self.csrf,
            "run_id": summary.run_id,
            "lifecycle_version": str(summary.lifecycle_version),
            **values,
        }

    def bounded(self, callback, *, opens, checks, sql):
        real_connect = DuckDbConnectionFactory._connect_with_lock_wait
        owners = []

        def connect(factory, path, config):
            with self.assertRaises(RuntimeError):
                asyncio.get_running_loop()
            self.assertIsNotNone(getattr(_read_databases, "connections", None))
            owner = real_connect(factory, path, config)
            owners.append(owner)
            return owner

        started = perf_counter()
        with (
            patch.object(DuckDbConnectionFactory, "_connect_with_lock_wait", connect),
            count_duckdb_statements() as statements,
            collect_duckdb_request_timings() as timings,
        ):
            response = callback()
        self.last_measurement = {
            "wall_ms": (perf_counter() - started) * 1000,
            "opens": timings.connection_count,
            "checks": timings.schema_check_count,
            "sql": len(statements),
            "connect_ms": timings.connect_ms,
            "schema_ms": timings.schema_ms,
        }
        for owner in owners:
            with self.assertRaises(duckdb.ConnectionException):
                owner.execute("SELECT 1")
        self.assertLessEqual(timings.connection_count, opens, self.last_measurement)
        self.assertLessEqual(timings.schema_check_count, checks, self.last_measurement)
        self.assertLessEqual(len(statements), sql, self.last_measurement)
        self.assertEqual(timings.lock_retry_count, 0)
        self.assertEqual(
            sum("duckdb_columns()" in statement for statement in statements),
            timings.schema_check_count,
        )
        return response

    def test_odoo19_local(self):
        self.exercise(19, "LOCAL")

    def test_odoo19_remote(self):
        self.exercise(19, "REMOTE")

    def test_odoo20_local(self):
        self.exercise(20, "LOCAL")

    def test_odoo20_remote(self):
        self.exercise(20, "REMOTE")

    def exercise(self, major, mode):
        workspace_id, summary, group = self.prepared(major, mode)
        prefix = f"/workspaces/{workspace_id}/normalization"
        original = summary
        # Admission and the page need four physical store/configuration owners.
        # POST admission has two owners. Group decisions need two command
        # owners; final approval and its source-mode read need four. Committed
        # summary read-back reuses the engine without merging transactions.
        for _ in range(2):
            page = self.bounded(
                lambda: self.client.get(prefix), opens=4, checks=3, sql=54
            )
            self.assertEqual(page.status_code, 200, page.text)
            self.assertIn("Imported product", page.text)
            self.assertEqual(
                self.context.normalization.current_summary(workspace_id), original
            )
        set_aside = self.bounded(
            lambda: self.client.get(prefix + "?status=set_aside"),
            opens=4,
            checks=3,
            sql=77,
        )
        self.assertEqual(set_aside.status_code, 200, set_aside.text)

        def decide(*args, **kwargs):
            with self.assertRaises(RuntimeError):
                asyncio.get_running_loop()
            self.assertIsNotNone(getattr(_read_databases, "connections", None))
            return real_decide(*args, **kwargs)

        real_decide = self.context.normalization.decide_group
        with patch.object(
            self.context.normalization, "decide_group", side_effect=decide
        ):
            accepted = self.bounded(
                lambda: self._post(
                    prefix + f"/groups/{group.group_id}/accept?status=pending&page=2",
                    self.form(summary),
                ),
                opens=4,
                checks=3,
                sql=25,
            )
        self.assertEqual(accepted.status_code, 303, accepted.text)
        self.assertTrue(
            accepted.headers["location"].endswith(
                "/normalization?status=pending&page=2#review-groups"
            )
        )
        accepted_review = self.context.normalization.current_review(workspace_id)
        self.assertGreater(
            accepted_review[0].lifecycle_version, summary.lifecycle_version
        )
        self.assertEqual(accepted_review[0].content_hash, original.content_hash)
        self.assertTrue(accepted_review[2].group_decisions)

        real_render = normalization._render_normalization

        def render(*args, **kwargs):
            with self.assertRaises(RuntimeError):
                asyncio.get_running_loop()
            self.assertIsNotNone(getattr(_read_databases, "connections", None))
            return real_render(*args, **kwargs)

        with patch.object(normalization, "_render_normalization", side_effect=render):
            stale = self.bounded(
                lambda: self._post(
                    prefix + f"/groups/{group.group_id}/accept", self.form(summary)
                ),
                opens=8,
                checks=6,
                sql=68,
            )
        self.assertEqual(stale.status_code, 422, stale.text)
        self.assertIn("another browser window", stale.text)
        self.assertEqual(
            self.context.normalization.current_review(workspace_id), accepted_review
        )

        approved = self.bounded(
            lambda: self._post(prefix + "/approve", self.form(accepted_review[0])),
            opens=6,
            checks=4,
            sql=45,
        )
        self.assertEqual(approved.status_code, 303, approved.text)
        self.assertEqual(
            approved.headers["location"], f"/workspaces/{workspace_id}/summary"
        )
        frozen = self.context.normalization.current_summary(workspace_id)
        self.assertTrue(frozen.frozen)
        self.assertEqual(frozen.content_hash, original.content_hash)
        self.assertEqual(frozen.eligible_dataset_hash, original.eligible_dataset_hash)

    def test_send_back_reopen_and_invalid_reason_preserve_evidence(self):
        workspace_id, summary, group = self.prepared()
        prefix = f"/workspaces/{workspace_id}/normalization"
        before = self.context.normalization.current_review(workspace_id)
        for reason in ("", "x" * 1001):
            with self.subTest(reason_length=len(reason)):
                rejected = self.bounded(
                    lambda reason=reason: self._post(
                        prefix + f"/groups/{group.group_id}/reject",
                        self.form(summary, reason=reason),
                    ),
                    opens=6,
                    checks=4,
                    sql=65,
                )
                self.assertEqual(rejected.status_code, 422, rejected.text)
                self.assertEqual(
                    self.context.normalization.current_review(workspace_id), before
                )
        sent_back = self.bounded(
            lambda: self._post(
                prefix + f"/groups/{group.group_id}/reject",
                self.form(summary, reason="Check the provided product name."),
            ),
            opens=4,
            checks=3,
            sql=35,
        )
        self.assertEqual(sent_back.status_code, 303, sent_back.text)
        blocked = self.context.normalization.current_review(workspace_id)
        self.assertEqual(blocked[0].status, "BLOCKED")
        self.assertEqual(
            blocked[2].group_decisions[-1].evidence.reason,
            "Check the provided product name.",
        )
        reopened = self.bounded(
            lambda: self._post(prefix + "/reopen", self.form(blocked[0])),
            opens=4,
            checks=3,
            sql=35,
        )
        self.assertEqual(reopened.status_code, 303, reopened.text)
        current = self.context.normalization.current_summary(workspace_id)
        self.assertEqual(current.content_hash, before[0].content_hash)
        self.assertEqual(current.eligible_dataset_hash, before[0].eligible_dataset_hash)
        approved = self.bounded(
            lambda: self._post(prefix + "/approve", self.form(current)),
            opens=6,
            checks=4,
            sql=45,
        )
        self.assertEqual(approved.status_code, 303, approved.text)
        self.assertTrue(self.context.normalization.current_summary(workspace_id).frozen)

    def test_render_failure_closes_owners_and_next_page_validates_again(self):
        workspace_id, _, _ = self.prepared()
        real_connect = DuckDbConnectionFactory._connect_with_lock_wait
        owners = []

        def connect(factory, path, config):
            owner = real_connect(factory, path, config)
            owners.append(owner)
            return owner

        with (
            patch.object(DuckDbConnectionFactory, "_connect_with_lock_wait", connect),
            patch.object(
                normalization,
                "_render_normalization",
                side_effect=ValueError("render failed"),
            ),
            self.assertRaisesRegex(ValueError, "render failed"),
        ):
            self.client.get(f"/workspaces/{workspace_id}/normalization")
        self.assertTrue(owners)
        for owner in owners:
            with self.assertRaises(duckdb.ConnectionException):
                owner.execute("SELECT 1")
        page = self.bounded(
            lambda: self.client.get(f"/workspaces/{workspace_id}/normalization"),
            opens=4,
            checks=3,
            sql=54,
        )
        self.assertEqual(page.status_code, 200, page.text)
        self.assertEqual(self.last_measurement["opens"], 4)
        self.assertEqual(self.last_measurement["checks"], 3)

    def test_unprepared_page_keeps_its_recovery_message(self):
        workspace_id, _, _ = self._mapping_ready_workspace(scalar_field_count=0)
        page = self.bounded(
            lambda: self.client.get(f"/workspaces/{workspace_id}/normalization"),
            opens=4,
            checks=3,
            sql=145,
        )
        self.assertEqual(page.status_code, 422, page.text)
        self.assertIn("Prepare the data before reviewing its changes", page.text)
