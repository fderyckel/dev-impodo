"""Stage 3 expectations persist and protect a same-project retry of existing lists."""

from dataclasses import replace
from datetime import UTC, datetime
import re
from unittest.mock import patch

from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from impodo.domain.mapping.contracts import (
    CategoricalCoveragePolicy,
    DatasetMapping,
    IdentityComponentMapping,
    RelationshipMapping,
    RelationshipResolver,
    ResolverOrigin,
    ScalarFieldMapping,
)
from impodo.domain.odoo.company import OdooCompanySelection
from impodo.domain.schema.governance import BusinessKeyDefinition, BusinessKeyStatus
from impodo.domain.serialization import content_hash
from impodo.domain.shared.models import OdooReadIdentity, OdooWriteIdentity
from impodo.domain.workspace.contracts import SchemaField, SchemaModel
from impodo.domain.workspace.errors import WorkspaceError
from impodo.web.composition.page_reads import run_local_operation_sync
from impodo.web.target_credentials import TargetCredentialRole, store_target_credential
from tests.support.hierarchy_odoo import HierarchyOdoo
from tests.support.hierarchy_workflow import HierarchyWorkflowFixture


def expectations_form(page):
    """Use the page's actual binding/version tokens, rather than rebuilding them."""
    html = re.search(
        r'<form method="post" action="[^"]+/mapping/expectations">(.*?)</form>',
        page,
        re.S,
    ).group(1)
    def field(html, name):
        return re.search(r'name="' + name + r'" value="([^"]*)"', html).group(1)

    names = (
        "csrf_token",
        "expected_version",
        "binding_hash",
        "operation_id",
        "model_count",
    )
    form = {name: field(html, name) for name in names}
    for index in range(int(form["model_count"])):
        form[f"model_{index}"] = field(html, f"model_{index}")
        form[f"mode_{index}"] = "upsert"
    return form


class MatchingExpectationsTests(HierarchyWorkflowFixture):
    def setup_lists(self, *, customer_count=3):
        workspace_id = self.freeze_files(
            {
                "lists": (
                    "Code,Name\n"
                    + "\n".join(f"chain{i:03},GROUP {i:03}" for i in range(150))
                    + "\n"
                ).encode(),
                "people": (
                    "Code,Name,List\n"
                    + "\n".join(
                        f"person{i:05},Person {i:05},chain{i % 150:03}"
                        for i in range(customer_count)
                    )
                    + "\n"
                ).encode(),
            }
        )

        def text(name, required=False):
            return SchemaField(name, name, "char", required, False, None, None, ())

        schema = self.schema(
            workspace_id,
            extra_models=(
                SchemaModel(
                    "x.chain", "Company chains", (text("code"), text("name", True))
                ),
                SchemaModel(
                    "x.person",
                    "People",
                    (
                        text("code"),
                        text("name", True),
                        SchemaField(
                            "x_chain_id",
                            "Chain",
                            "many2one",
                            False,
                            False,
                            "x.chain",
                            None,
                            (),
                        ),
                    ),
                ),
            ),
            extra_keys=tuple(
                BusinessKeyDefinition(
                    f"{model}:code",
                    model,
                    ("code",),
                    status=BusinessKeyStatus.CONFIRMED,
                )
                for model in ("x.chain", "x.person")
            ),
        )
        state = self.context.queries.get(workspace_id)
        company = OdooCompanySelection(
            schema.connection_target_hash, 907, "Another factory"
        )
        self.context.workspace_states.select_odoo_company(
            workspace_id,
            actor=self.actor,
            expected_revision=state.revision,
            selection=company,
        )
        schema = replace(schema, read_context_hash=company.context_hash)
        self.context.schema_workspace.schemas.save_odoo_create_defaults(
            workspace_id,
            schema,
            expected_content_hash=schema.content_hash,
            expected_read_credential_binding_hash=schema.read_credential_binding_hash,
            expected_company_context_hash=company.context_hash,
            actor=self.actor,
        )
        source = self.context.queries.get_source_selection(workspace_id)
        by_name = {dataset.name: dataset for dataset in source.datasets}
        lists = by_name["lists"]
        definitions = []
        for name, model in (("lists", "x.chain"), ("people", "x.person")):
            dataset = by_name[name]
            columns = {
                column.source_name: column.stable_key for column in dataset.columns
            }
            definitions.append(
                DatasetMapping(
                    dataset.dataset_id,
                    model,
                    mode="upsert",
                    source_identity_column_keys=(columns["Code"],),
                    target_identity=(
                        IdentityComponentMapping((columns["Code"],), ("code",)),
                    ),
                    fields=(ScalarFieldMapping("name", columns["Name"]),),
                    relationships=()
                    if name == "lists"
                    else (
                        RelationshipMapping(
                            "x_chain_id",
                            "many2one",
                            (columns["List"],),
                            categorical_policy=CategoricalCoveragePolicy.EXACT_BUSINESS_KEY,
                            resolver=RelationshipResolver(
                                origin=ResolverOrigin.DATASET,
                                dataset_id=lists.dataset_id,
                            ),
                        ),
                    ),
                )
            )
        mapping = self.submit(workspace_id, definitions)
        self.prepare(workspace_id)
        self.approve(workspace_id)
        target = HierarchyOdoo(schema)
        target.records = {
            ("x.chain", 2000 + i): {"code": f"chain{i:03}", "name": f"GROUP {i:03}"}
            for i in range(150)
        }
        return workspace_id, mapping, source, target

    def test_same_project_retry_reuses_150_lists_and_loads_4753_people(self):
        workspace_id, mapping, source, target = self.setup_lists(customer_count=4753)
        report = self.context.preflight.compare(
            workspace_id, reader=target.compare, actor=self.actor
        )
        self.assertEqual(report.create_count, 4753)
        self.assertEqual(report.unchanged_count, 150)
        page = self.client.get(f"/workspaces/{workspace_id}/mapping")
        self.assertEqual(page.status_code, 200, page.text[:1000])
        self.assertIn("What may this load change?", page.text)
        self.assertRegex(
            page.text,
            r'<details[^>]*\bid="business-expectations"[^>]*>',
        )
        self.assertNotRegex(
            page.text,
            r'<details[^>]*\bid="business-expectations"[^>]*\bopen(?:\s|=|>)',
        )
        form = expectations_form(page.text)
        chain_index = next(
            i
            for i in range(int(form["model_count"]))
            if form[f"model_{i}"] == "x.chain"
        )
        form[f"mode_{chain_index}"] = "none"
        form[f"create_max_{chain_index}"] = "0"
        form[f"update_max_{chain_index}"] = "0"
        with (
            patch.object(
                self.context.preflight,
                "current_execution_snapshot",
                side_effect=AssertionError("No full snapshot read"),
            ),
            patch.object(
                target, "compare", side_effect=AssertionError("No Odoo read on save")
            ),
            collect_duckdb_request_timings() as timings,
        ):
            saved = self._post(f"/workspaces/{workspace_id}/mapping/expectations", form)
        self.assertEqual(saved.status_code, 303, saved.text[:1000])
        # Four owners; the read-to-write transition reopens the engine and
        # audit owner. The number does not depend on rows, fields or models.
        self.assertLessEqual(timings.connection_count, 6)
        self.assertLessEqual(timings.schema_check_count, 2)
        self.assertEqual(timings.lock_retry_count, 0)
        revision = self.context.business_intents.get(workspace_id, actor=self.actor)
        chain = next(item for item in revision.intent.models if item.model == "x.chain")
        self.assertEqual(chain.creates.maximum, 0)
        self.assertIsNone(chain.creates.minimum)
        self.assertIn('value="0"', self.client.get(saved.headers["location"]).text)
        preview = self.context.execution.current_preview(workspace_id, actor=self.actor)
        assessment = self.context.preflight.current_business_intent_assessment(
            workspace_id, preview.snapshot.semantic_hash
        )
        self.assertTrue(assessment.ready, assessment)
        snapshot = preview.snapshot
        observed = datetime.now(UTC).isoformat()
        read = OdooReadIdentity(
            target_hash=snapshot.target_hash,
            principal_hash=snapshot.read_principal_hash,
            permission_hash=snapshot.read_permission_hash,
            context_hash=snapshot.read_context_hash,
            readable_models=snapshot.readable_models,
            observed_at=observed,
        )
        write = OdooWriteIdentity(
            target_hash=snapshot.target_hash,
            principal_hash=content_hash("writer"),
            permission_hash=content_hash("writer permission"),
            context_hash=snapshot.read_context_hash,
            readable_models=tuple(item.model for item in preview.api_scope.models),
            writable_models=tuple(item.model for item in preview.api_scope.models if item.write_fields),
            observed_at=observed,
        )
        state = self.context.queries.get(workspace_id)
        owner = self.context.target_credential_workspace(
            workspace_id, workspace_state=state, actor=self.actor
        )
        credential = store_target_credential(
            self.context.secret_store,
            owner,
            TargetCredentialRole.WRITE,
            "write-key",
            persistent=False,
        )
        target.scope_hash = preview.api_scope.semantic_hash
        execution = self.context.execution.execute(
            workspace_id,
            expected_snapshot_hash=snapshot.semantic_hash,
            expected_intent_assessment_hash=assessment.content_hash,
            executor=target,
            actor=self.actor,
            read_identity=read,
            read_credential_binding_hash=snapshot.read_credential_binding_hash,
            write_identity=write,
            write_credential_binding_hash=credential.binding_hash,
        )
        self.assertEqual(execution.status.value, "COMPLETED")
        self.assertEqual(execution.committed_count, 4753)
        self.assertEqual({model for model, _, _ in target.writes}, {"x.person"})
        self.assertEqual(sum(model == "x.chain" for model, _ in target.records), 150)
        self.assertTrue(
            all(
                2000 <= values["x_chain_id"] < 2150
                for (model, _), values in target.records.items()
                if model == "x.person"
            )
        )
        result = self.context.reconciliation.reconcile(
            workspace_id,
            expected_execution_run_id=execution.run_id,
            reader=target,
            actor=self.actor,
            write_identity=write,
            write_credential_binding_hash=credential.binding_hash,
        )
        self.assertEqual(result.status.value, "VERIFIED", result.rows[:3])
        self.assertEqual(result.verified_count, 4903)
        self.assertEqual(
            self.context.queries.get_source_selection(workspace_id), source
        )
        self.assertEqual(
            self.context.mapping_workspace.mappings.get_mapping_revision(workspace_id),
            mapping,
        )

    def test_missing_list_stops_before_journal_or_writes_and_stale_form_is_rejected(
        self,
    ):
        workspace_id, _, _, target = self.setup_lists()
        del target.records[("x.chain", 2149)]
        self.context.preflight.compare(
            workspace_id, reader=target.compare, actor=self.actor
        )
        form = expectations_form(
            self.client.get(f"/workspaces/{workspace_id}/mapping").text
        )
        index = next(
            i
            for i in range(int(form["model_count"]))
            if form[f"model_{i}"] == "x.chain"
        )
        form[f"create_max_{index}"] = "0"
        response = self._post(f"/workspaces/{workspace_id}/mapping/expectations", form)
        self.assertEqual(response.status_code, 303, response.text[:1000])
        preview = self.context.execution.current_preview(workspace_id, actor=self.actor)
        assessment = self.context.preflight.current_business_intent_assessment(
            workspace_id, preview.snapshot.semantic_hash
        )
        self.assertFalse(assessment.ready)
        with self.assertRaises(WorkspaceError):
            self.context.execution.require_business_intent(
                workspace_id,
                expected_snapshot_hash=preview.snapshot.semantic_hash,
                expected_assessment_hash=assessment.content_hash,
                actor=self.actor,
            )
        self.assertEqual(target.writes, [])
        self.assertIsNone(self.context.execution.journal.get_current_run(workspace_id))
        form["operation_id"] = "d9cd239b-8811-43df-b574-d79785dfd128"
        stale = self._post(f"/workspaces/{workspace_id}/mapping/expectations", form)
        self.assertEqual(stale.status_code, 422)
        self.assertEqual(
            self.context.business_intents.get(workspace_id, actor=self.actor).version, 1
        )

    def test_editor_rejects_forged_model_and_negative_counts_without_saving(self):
        workspace_id, _, _, _ = self.setup_lists()
        form = expectations_form(
            self.client.get(f"/workspaces/{workspace_id}/mapping").text
        )
        for changes in (
            {"model_0": "res.users"},
            {"create_max_0": "-1"},
            {"create_min_0": "9", "create_max_0": "1"},
            {"limit_create_fields_0": "1", "create_fields_0": "password"},
        ):
            response = self._post(
                f"/workspaces/{workspace_id}/mapping/expectations", {**form, **changes}
            )
            self.assertEqual(response.status_code, 422, response.text[:300])
            self.assertIsNone(
                self.context.business_intents.get(workspace_id, actor=self.actor)
            )
