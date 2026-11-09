"""Resolve comparison findings through the actions on their migration cards."""

from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from impodo.domain.mapping.contracts import (
    DatasetMapping,
    IdentityComponentMapping,
    ReferenceKeyMapping,
    RelationshipResolver,
    ResolverOrigin,
)
from impodo.domain.schema.governance import BusinessKeyDefinition, BusinessKeyStatus
from impodo.domain.workspace.contracts import SchemaField, SchemaModel
from tests.integration.web.test_mapping_form_roundtrip import _RenderedControls
from tests.support.browser_scenarios import POST_HEADERS
from tests.support.hierarchy_odoo import HierarchyOdoo
from tests.support.hierarchy_workflow import HierarchyWorkflowFixture


class DeferredFindingWorkflowTests(HierarchyWorkflowFixture):
    def test_preview_one_finding_then_include_the_other_and_continue(self):
        for major in (19, 20):
            for mode in ("LOCAL", "REMOTE"):
                with self.subTest(major=major, mode=mode):
                    self._resolve_findings(major, mode)

    def _resolve_findings(self, major, mode):
        workspace = self.freeze_files({
            "assignments": (
                b"Assignment,Area,Resource\n"
                b"A1,UNKNOWN-AREA,RESOURCE\n"
                b"A2,AREA,UNKNOWN-RESOURCE\n"
                b"A3,UNKNOWN-AREA,UNKNOWN-RESOURCE\n"
                b"A4,AREA,RESOURCE\n"
            ),
        })
        schema = self.schema(
            workspace, major=major, mode=mode,
            extra_models=(
                SchemaModel("x.lookup", "Lookup", (_field("code"),)),
                SchemaModel("x.assignment", "Assignment", (
                    _field("ref"), _field("area_id", "x.lookup"), _field("resource_id", "x.lookup"),
                )),
            ),
            extra_keys=(
                BusinessKeyDefinition("lookup:code", "x.lookup", ("code",), status=BusinessKeyStatus.CONFIRMED),
                BusinessKeyDefinition(
                    "assignment:resource-area", "x.assignment", ("ref", "resource_id"),
                    scope_fields=("area_id",), status=BusinessKeyStatus.CONFIRMED,
                ),
            ),
        )
        dataset = self.context.queries.get_source_selection(workspace).datasets[0]
        columns = {column.source_name: column.stable_key for column in dataset.columns}

        def linked(column, field):
            key = columns[column]
            return IdentityComponentMapping((key,), (field,), resolver=RelationshipResolver(
                ResolverOrigin.TARGET_CATALOG, model="x.lookup",
                key_mappings=(ReferenceKeyMapping(key, "code"),),
            ))

        self.submit(workspace, (DatasetMapping(
            dataset.dataset_id, "x.assignment",
            source_identity_column_keys=(columns["Assignment"],),
            target_identity=(
                IdentityComponentMapping((columns["Assignment"],), ("ref",)),
                linked("Resource", "resource_id"),
            ),
            target_scope=(linked("Area", "area_id"),),
        ),))
        self.prepare(workspace)
        self.approve(workspace)
        self.expect_business_writes(workspace)
        target = HierarchyOdoo(schema)
        target.records.update({("x.lookup", 1): {"code": "AREA"}, ("x.lookup", 2): {"code": "RESOURCE"}})
        report = self.context.preflight.compare(workspace, reader=target.compare, actor=self.actor)
        reads = len(target.requests)
        url = f"/workspaces/{workspace}/summary"
        page = self.client.get(url + "?status=attention")
        cards = page.text.split('id="migration-issues"', 1)[1].split("</section>", 1)[0]
        self.assertEqual(cards.count("Preview rows to set aside"), 2, report.issue_counts)
        area = next(card for card in cards.split("</article>") if "scope:area_id" in card)
        area_form = _controls(area)
        self.assertEqual(area_form["comparison_id"], report.run_id)
        preview_url = url + "/deferred-groups/preview"
        with collect_duckdb_request_timings() as timings:
            partial = self._post(preview_url, area_form)
        self.assertEqual(partial.status_code, 200, partial.text)
        self.assertLessEqual(timings.connection_count, 6)
        self.assertLessEqual(timings.schema_check_count, 4)
        self.assertEqual(timings.lock_retry_count, 0)
        self.assertIn("The remainder is not safe yet", partial.text)
        self.assertIn("<td>2</td>", partial.text)
        self.assertIn("<td>4</td>", partial.text)
        self.assertNotIn("<td>3</td>", partial.text)
        self.assertNotIn("/deferred-groups/accept", partial.text)
        self.assertNotIn('name="issue_id"', partial.text)

        other = next(card for card in cards.split("</article>") if "target_identity:resource_id" in card)
        choices = partial.text.split('aria-labelledby="finding-options-title"', 1)[1].split("</form>", 1)[0]
        selected = _controls(choices)
        self.assertEqual(selected["finding"], area_form["finding"])
        selected["finding"] = [selected["finding"], _controls(other)["finding"]]
        combined = self.client.post(preview_url, data=selected, headers=POST_HEADERS)
        self.assertEqual(combined.status_code, 200, combined.text)
        self.assertIn("Set aside affected groups for this load", combined.text)
        self.assertIn("<td>3</td>", combined.text)
        accept_url = url + "/deferred-groups/accept"
        form = combined.text.split(f'action="{accept_url}"', 1)[1].split("</form>", 1)[0]
        controls = _RenderedControls()
        controls.feed(form)
        data = dict(controls.entries)
        data["finding"] = [value for name, value in controls.entries if name == "finding"]
        self.assertEqual(len(data["finding"]), 2)
        self.assertIsNone(self.context.preflight.deferred_scope_review(workspace).decision)
        stale = self._post(preview_url, {**area_form, "comparison_id": "stale"})
        self.assertEqual(stale.status_code, 422)
        self.assertIn("The comparison changed", stale.text)
        altered = self.client.post(
            accept_url, data={**data, "finding": area_form["finding"]}, headers=POST_HEADERS,
        )
        self.assertEqual(altered.status_code, 422)
        self.assertIn("The group preview changed", altered.text)
        self.assertIsNone(self.context.preflight.deferred_scope_review(workspace).decision)
        with collect_duckdb_request_timings() as timings:
            accepted = self.client.post(accept_url, data=data, headers=POST_HEADERS, follow_redirects=False)
        self.assertEqual(accepted.status_code, 303, accepted.text)
        self.assertEqual(accepted.headers["location"], url)
        self.assertLessEqual(timings.connection_count, 6)
        self.assertLessEqual(timings.schema_check_count, 4)
        self.assertEqual(timings.lock_retry_count, 0)

        result = self.client.get(url)
        self.assertIn("Ready with records set aside", result.text)
        self.assertIn("Continue to Load into Odoo", result.text)
        self.assertNotIn("Preview rows to set aside", result.text)
        self.assertNotIn("Must fix", result.text)
        review = self.context.preflight.deferred_scope_review(workspace)
        self.assertEqual(review.preview.newly_set_aside_count, 3)
        self.assertEqual(review.preview.remaining_write_count, 1)
        self.assertEqual(len(target.requests), reads)
        self.assertEqual(target.writes, [])


def _field(name, relation=None):
    return SchemaField(name, name, "many2one" if relation else "char", False, False, relation, None, ())


def _controls(html):
    controls = _RenderedControls()
    controls.feed(html)
    return dict(controls.entries)
