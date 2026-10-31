"""Reviewed supporting-data browser commands over real contained DuckDB stores."""

from dataclasses import replace
from unittest.mock import patch
from uuid import uuid4

from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from impodo.adapters.duckdb.unit_of_work import _read_databases
from impodo.domain.odoo.contracts import MetadataSnapshot, RecordSnapshot
from impodo.domain.shared.models import FieldMetadata, ModelMetadata, TargetRecord
from impodo.domain.shared.schema import WORKSPACE_SCHEMA_VERSION
from impodo.domain.workspace.contracts import SchemaField
from impodo.domain.workspace.errors import WorkspaceError
from impodo.domain.workspace.supporting_sources import SupportingSourceLimitError
from impodo.web.composition.page_reads import run_local_operation_sync
from impodo.domain.mapping.contracts import DatasetMapping, ReferenceKeyMapping, RelationshipMapping, RelationshipResolver, ResolverOrigin
from tests.support.browser_scenarios import (
    POST_HEADERS,
    ProjectSetupBrowserTestCase,
    _browser_schema,
    _browser_model_catalog,
)
from tests.support.identity import resolve_local_actor


class SupportingReviewBrowserTests(ProjectSetupBrowserTestCase):
    def setUp(self):
        super().setUp()
        self.workspace_id, _dataset, _key = self._mapping_ready_workspace(
            scalar_field_count=2
        )
        self.context = self.app.state.context
        self.actor = resolve_local_actor(self.context)
        self.workspace = self.context.queries.get(self.workspace_id)
        schema = self.context.queries.get_odoo_schema_catalog(self.workspace_id)
        country = SchemaField(
            "country_id", "Country", "many2one", True, False, "res.country", None, ()
        )
        birthplace = replace(country, name="birth_country_id", label="Country of birth")
        self.schema = replace(
            schema,
            content_hash="sha256:" + "9" * 64,
            models=(
                replace(
                    schema.models[0],
                    label="Contacts",
                    fields=(*schema.models[0].fields, country, birthplace),
                ),
            ),
        )
        self.context.schema_workspace.schemas.save_odoo_schema_catalog(
            self.workspace_id, self.schema, actor=self.actor
        )
        catalogue = _browser_model_catalog(self.workspace)
        country_record = TargetRecord(
            "ir.model",
            500,
            {
                "name": "Country",
                "model": "res.country",
                "abstract": False,
                "transient": False,
                "modules": "base",
                "state": "base",
            },
        )
        self.context.schema_workspace.discover_models(
            self.workspace_id,
            replace(
                catalogue,
                records={"ir.model": (*catalogue.records["ir.model"], country_record)},
            ),
            read_credential_binding_hash=self.schema.read_credential_binding_hash,
            actor=self.actor,
        )

    def page_and_form(self):
        page = self.client.get(f"/workspaces/{self.workspace_id}/schema")
        self.assertEqual(page.status_code, 200, page.text)
        view = page.context["supporting_review"]
        form = {
            "csrf_token": self.csrf,
            "binding_hash": view["binding_hash"],
            "expected_version": str(view["version"]),
            "operation_id": view["operation_id"],
            "choice_count": str(len(view["rows"])),
        }
        for index, row in enumerate(view["rows"]):
            form[f"edge_{index}"] = f"{row['model']}|{row['field']}"
            form[f"action_{index}"] = "REUSE" if row["required"] else ""
        return page, form

    def save(self, form):
        return self.client.post(
            f"/workspaces/{self.workspace_id}/schema/supporting/save",
            data=form,
            headers=POST_HEADERS,
            follow_redirects=False,
        )

    def test_current_choices_idempotency_conflict_and_page_connection_ceiling(self):
        before_calls = len(self.readiness_calls)
        with collect_duckdb_request_timings() as timings:
            page, form = self.page_and_form()
        self.assertIn("Review supporting-data choices", page.text)
        self.assertIn("matching-rules-blocked-reason", page.text)
        self.assertLessEqual(timings.connection_count, 6)
        self.assertLessEqual(timings.schema_check_count, 6)
        self.assertEqual(timings.lock_retry_count, 0)
        self.assertEqual(len(self.readiness_calls), before_calls)
        self.assertEqual(self.save(form).status_code, 303)
        original = self.context.schema_workspace.supporting.current(
            self.workspace_id, actor=self.actor
        )
        self.assertEqual(original.version, 1)
        self.assertEqual(self.save(form).status_code, 303)
        self.assertEqual(
            self.context.schema_workspace.supporting.current(
                self.workspace_id, actor=self.actor
            ),
            original,
        )
        stale = {**form, "operation_id": str(uuid4())}
        self.assertEqual(self.save(stale).status_code, 422)
        self.assertEqual(
            self.context.queries.get(self.workspace_id).intended_models,
            self.workspace.intended_models,
        )
        page, _form = self.page_and_form()
        self.assertTrue(page.context["supporting_review"]["current"])

    def test_committed_summary_rebuild_and_unchanged_decision_keep_evidence(self):
        _page, form = self.page_and_form()
        self.assertEqual(self.save(form).status_code, 303)
        service = self.context.schema_workspace.supporting
        summary = service.summary(self.workspace_id, actor=self.actor)
        review = service.current(self.workspace_id, actor=self.actor)
        self.assertEqual((summary.choice_count, summary.reuse_count, summary.incoming_count), (2, 2, 0))
        self.assertEqual(summary.evidence_hash, review.content_hash)
        self.assertEqual(summary.state_for(review.binding_hash), "CURRENT")
        self.assertEqual(summary.state_for("changed-inputs"), "STALE")
        repository = self.context.schema_workspace.schemas.local
        path = repository.workspace_directory(self.workspace_id) / "workspace-engine.duckdb"
        with repository._connect(path) as connection:
            connection.execute("DELETE FROM supporting_review_projection")
        before_calls = len(self.readiness_calls)
        rebuilt = service.rebuild_summary(self.workspace_id, actor=self.actor)
        self.assertEqual(rebuilt, summary)
        self.assertEqual(len(self.readiness_calls), before_calls)
        _page, same = self.page_and_form()
        with patch.object(repository, "_invalidate_canonical_staging", wraps=repository._invalidate_canonical_staging) as invalidate:
            self.assertEqual(self.save(same).status_code, 303)
        invalidate.assert_not_called()
        self.assertEqual(service.summary(self.workspace_id, actor=self.actor).version, 2)
        committed = service.current(self.workspace_id, actor=self.actor)
        committed_summary = service.summary(self.workspace_id, actor=self.actor)
        packet = self.context.schema_workspace.supporting_review_inputs(self.workspace_id, actor=self.actor)
        with patch.object(repository, "_insert_workspace_audit", side_effect=RuntimeError("interrupted audit")):
            with self.assertRaisesRegex(RuntimeError, "interrupted audit"):
                service.save(packet, expected_binding_hash=packet.binding_hash, expected_version=2,
                    operation_id=str(uuid4()), choices=committed.choices, actor=self.actor)
        self.assertEqual(service.current(self.workspace_id, actor=self.actor), committed)
        self.assertEqual(service.summary(self.workspace_id, actor=self.actor), committed_summary)

    def test_mapping_consumer_checks_choices_in_one_local_scope_before_scans(self):
        _page, form = self.page_and_form()
        self.assertEqual(self.save(form).status_code, 303)
        selection = self.context.queries.get_source_selection(self.workspace_id)
        dataset = selection.datasets[0]
        source = dataset.columns[0].stable_key
        relations = tuple(RelationshipMapping(
            name, "many2one", (source,),
            RelationshipResolver(ResolverOrigin.TARGET_CATALOG, model="res.country", key_mappings=(ReferenceKeyMapping(source, "code"),)),
        ) for name in ("country_id", "birth_country_id"))
        mappings = (DatasetMapping(dataset.dataset_id, "res.partner", relationships=relations),)
        guard = self.context.mapping_workspace.supporting_decisions
        with collect_duckdb_request_timings() as timings:
            run_local_operation_sync(lambda: guard.require_mapping(self.workspace_id, mappings, self.schema, None))
        self.assertLessEqual(timings.connection_count, 4)
        self.assertLessEqual(timings.schema_check_count, 4)
        self.assertEqual(timings.lock_retry_count, 0)
        bad = (replace(mappings[0], relationships=(replace(relations[0], resolver=replace(relations[0].resolver, origin=ResolverOrigin.TARGET_THEN_DATASET, dataset_id=dataset.dataset_id)), relations[1])),)
        with patch.object(self.context.mapping_workspace.categorical_coverage, "collect") as scan:
            with self.assertRaisesRegex(WorkspaceError, "country_id.*conflicts"):
                self.context.mapping_workspace.check_definition(
                    self.workspace_id, datasets=bad, expected_parent_version=None,
                    expected_working_draft_version=None, actor=self.actor,
                )
        scan.assert_not_called()

    def test_version_21_review_upgrades_without_rewriting_decision_evidence(self):
        _page, form = self.page_and_form()
        self.assertEqual(self.save(form).status_code, 303)
        service = self.context.schema_workspace.supporting
        review = service.current(self.workspace_id, actor=self.actor)
        repository = self.context.schema_workspace.schemas.local
        path = repository.workspace_directory(self.workspace_id) / "workspace-engine.duckdb"
        with repository._connect(path) as connection:
            original = connection.execute("SELECT review_json FROM supporting_data_review").fetchone()[0]
            connection.execute("DROP TABLE business_intent_approval")
            connection.execute("DROP TABLE business_intent_current")
            connection.execute("DROP TABLE business_intent_assessment")
            connection.execute("DROP TABLE business_write_observation")
            connection.execute("DROP TABLE odoo_company_binding")
            connection.execute("DROP TABLE business_intent_revision")
            connection.execute("DROP TABLE supporting_review_projection")
            connection.execute("UPDATE schema_version SET version = 21")
        self.assertEqual(service.current(self.workspace_id, actor=self.actor), review)
        self.assertIsNone(service.summary(self.workspace_id, actor=self.actor))
        self.assertEqual(service.rebuild_summary(self.workspace_id, actor=self.actor).evidence_hash, review.content_hash)
        with repository._connect(path) as connection:
            self.assertEqual(connection.execute("SELECT review_json FROM supporting_data_review").fetchone()[0], original)
            self.assertEqual(connection.execute("SELECT version FROM schema_version").fetchone()[0], WORKSPACE_SCHEMA_VERSION)

    def test_oversized_metadata_shows_blocking_issue_without_a_partial_save_form(self):
        before_calls = len(self.readiness_calls)
        with patch(
            "impodo.web.presenters.supporting_review.supporting_review_packet",
            side_effect=SupportingSourceLimitError(
                "Supporting source metadata exceeds its limit"
            ),
        ):
            page = self.client.get(f"/workspaces/{self.workspace_id}/schema")
        self.assertEqual(page.status_code, 200, page.text)
        self.assertFalse(page.context["supporting_review"]["current"])
        self.assertIn("Supporting source metadata exceeds its limit", page.text)
        self.assertIn('id="supporting-data-review"', page.text)
        self.assertIn("matching-rules-blocked-reason", page.text)
        self.assertNotIn("Save supporting choices</button>", page.text)
        self.assertEqual(len(self.readiness_calls), before_calls)

    def read_values(self, workspace, metadata, records):
        self.assertFalse(
            getattr(_read_databases, "connections", None),
            "DuckDB owner crossed the Odoo boundary",
        )
        self.assertEqual(len(metadata), 1)
        self.assertEqual(metadata[0].model, "res.country")
        self.assertEqual(metadata[0].fields, ("code", "name"))
        fingerprint = _browser_schema(workspace).fingerprint
        fields = {
            name: FieldMetadata(
                name=name, type="char", label=name, required=True, readonly=False
            )
            for name in ("code", "name")
        }
        return (
            MetadataSnapshot(
                fingerprint,
                {"res.country": ModelMetadata("res.country", "Country", fields)},
            ),
            RecordSnapshot(
                fingerprint,
                {
                    "res.country": (
                        TargetRecord(
                            "res.country", 101, {"code": "BE", "name": "Belgium"}
                        ),
                    )
                },
                {"res.country": ("code", "name")},
            ),
        )

    def test_explicit_check_groups_two_edges_and_revalidates_before_publication(self):
        _page, form = self.page_and_form()
        self.assertEqual(self.save(form).status_code, 303)
        page, _form = self.page_and_form()
        view = page.context["supporting_review"]
        payload = {
            "csrf_token": self.csrf,
            "binding_hash": view["binding_hash"],
            "expected_version": str(view["version"]),
        }
        with (
            patch.object(
                self.context, "readiness_reader", side_effect=self.read_values
            ) as reader,
            patch.object(
                self.context.supporting_lookups,
                "capture",
                wraps=self.context.supporting_lookups.capture,
            ) as capture,
        ):
            checked = self.client.post(
                f"/workspaces/{self.workspace_id}/schema/supporting/check",
                data=payload,
                headers=POST_HEADERS,
                follow_redirects=False,
            )
        self.assertEqual(
            checked.status_code,
            303,
            checked.context.get("error") if checked.status_code != 303 else "",
        )
        reader.assert_called_once()
        capture.assert_called_once()
        with patch.object(
            self.context, "readiness_reader", side_effect=self.read_values
        ) as reader:
            self.page_and_form()
        reader.assert_not_called()

    def test_source_or_schema_change_rejects_stale_form_and_blocks_governance(self):
        _page, form = self.page_and_form()
        changed = replace(self.schema, content_hash="sha256:" + "8" * 64)
        self.context.schema_workspace.schemas.save_odoo_schema_catalog(
            self.workspace_id, changed, actor=self.actor
        )
        response = self.save(form)
        self.assertEqual(response.status_code, 422)
        self.assertIn("Supporting data changed", response.text)
        with self.assertRaisesRegex(WorkspaceError, "supporting-data choices"):
            self.context.schema_workspace.govern_complete(
                self.workspace_id,
                business_keys=(),
                expected_catalog_hash=changed.content_hash,
                actor=self.actor,
            )

    def test_changed_inputs_during_odoo_read_never_publish_old_values(self):
        _page, form = self.page_and_form()
        self.assertEqual(self.save(form).status_code, 303)
        page, _form = self.page_and_form()
        view = page.context["supporting_review"]

        def changed_read(*args):
            result = self.read_values(*args)
            changed = replace(self.schema, content_hash="sha256:" + "7" * 64)
            self.context.schema_workspace.schemas.save_odoo_schema_catalog(
                self.workspace_id, changed, actor=self.actor
            )
            return result

        with (
            patch.object(self.context, "readiness_reader", side_effect=changed_read),
            patch.object(
                self.context.supporting_lookups,
                "capture",
                wraps=self.context.supporting_lookups.capture,
            ) as capture,
        ):
            response = self.client.post(
                f"/workspaces/{self.workspace_id}/schema/supporting/check",
                data={
                    "csrf_token": self.csrf,
                    "binding_hash": view["binding_hash"],
                    "expected_version": str(view["version"]),
                },
                headers=POST_HEADERS,
                follow_redirects=False,
            )
        self.assertEqual(response.status_code, 422)
        capture.assert_not_called()
