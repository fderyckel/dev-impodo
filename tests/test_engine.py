from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import unittest

import yaml

from impodo.catalog import TargetCatalog
from impodo.connectors import MetadataSnapshot, SnapshotConnector
from impodo.domain.compiler import CompiledMigrationPlan, compile_profile_document
from impodo.engine import PreflightEngine, _relation_difference, _resolve_records
from impodo.models import (
    BusinessReference,
    Classification,
    FieldMetadata,
    ModelMetadata,
    LogicalReference,
    PreparedRecord,
    assert_no_numeric_odoo_ids,
    canonical_json_bytes,
)
from impodo.planner import (
    plan_metadata_requests,
    plan_record_requests,
)
from impodo.profile import (
    ProfileDocument,
    RelationSpec,
    ResolveSpec,
    load_profile,
)
from impodo.source import prepare_sources
from impodo.workspace_contracts import (
    OdooSchemaCatalog,
    SchemaField,
    SchemaModel,
    SchemaOrigin,
)
from impodo.domain.odoo_source_policy import CURRENT_ODOO_SOURCE_POLICY


ROOT = Path(__file__).resolve().parents[1]


class PortableEvidenceValidationTests(unittest.TestCase):
    def test_nested_numeric_odoo_identifier_is_rejected_with_its_path(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            r"\$\.rows\[0\]\.record_id",
        ):
            assert_no_numeric_odoo_ids(
                {"rows": [{"name": "Contact", "record_id": 42}]}
            )

    def test_nested_business_keys_remain_portable(self) -> None:
        assert_no_numeric_odoo_ids(
            {
                "rows": [
                    {
                        "business_key": ["CONTACT-001"],
                        "references": ({"country_code": "FR"},),
                    }
                ]
            }
        )


class CyclicRelationshipResolutionTests(unittest.TestCase):
    def test_optional_relationship_cycle_resolves_each_business_key_once(self) -> None:
        data = yaml.safe_load((ROOT / "profiles/template.yaml").read_text())
        first = data["datasets"][0]
        first["name"] = "one"
        first["source_identity"]["fields"] = ["source_key"]
        first["relations"]["uom_id"]["required_on_create"] = False
        first["relations"]["uom_id"]["resolve"] = {
            "dataset": "two",
            "target_source_fields": ["source_key"],
        }
        second = yaml.safe_load(yaml.safe_dump(first))
        second["name"] = "two"
        second["source"]["file"] = "two.csv"
        second["relations"]["uom_id"]["resolve"] = {
            "dataset": "one",
            "target_source_fields": ["source_key"],
        }
        data["datasets"] = [first, second]
        plan = compile_profile_document(ProfileDocument.model_validate(data))
        records = (
            PreparedRecord(
                dataset="one",
                source_row=2,
                target_model=first["target"]["model"],
                source_identity=("ONE",),
                target_identity=("ONE",),
                target_scope=(),
                scalar_values={},
                references={
                    "uom_id": LogicalReference(
                        origin="incoming",
                        key=("TWO",),
                        dataset="two",
                    )
                },
            ),
            PreparedRecord(
                dataset="two",
                source_row=2,
                target_model=second["target"]["model"],
                source_identity=("TWO",),
                target_identity=("TWO",),
                target_scope=(),
                scalar_values={},
                references={
                    "uom_id": LogicalReference(
                        origin="incoming",
                        key=("ONE",),
                        dataset="one",
                    )
                },
            ),
        )

        resolved, evidence = _resolve_records(plan, records, TargetCatalog({}))

        self.assertEqual(
            resolved[0].references["uom_id"],
            BusinessReference(records[1].target_model, ("TWO",)),
        )
        self.assertEqual(
            resolved[1].references["uom_id"],
            BusinessReference(records[0].target_model, ("ONE",)),
        )
        self.assertEqual(len(evidence), 2)
        self.assertTrue(all(item.status == "RESOLVED" for item in evidence))


def golden_result():
    profile = compile_profile_document(
        load_profile(ROOT / "profiles/examples/golden_slice.yaml")
    )
    prepared = prepare_sources(profile, ROOT / "examples/golden")
    connector = SnapshotConnector(
        combined_path=ROOT / "fixtures/golden/target_snapshot.json"
    )
    metadata = connector.get_model_metadata(plan_metadata_requests(profile))
    records = connector.get_records(
        plan_record_requests(profile, prepared.records)
    )
    return PreflightEngine().run(profile, prepared, metadata, records)


class PreflightClassificationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.result = golden_result()
        cls.by_identity = {
            (
                decision.dataset,
                json.dumps(
                    decision.business_identity,
                    default=str,
                    sort_keys=True,
                ),
            ): decision
            for decision in cls.result.decisions
        }

    def test_all_five_classifications_are_present(self) -> None:
        self.assertEqual(
            self.result.counts,
            {
                "CREATE": 5,
                "UPDATE": 2,
                "UNCHANGED": 2,
                "AMBIGUOUS": 1,
                "BLOCKED": 2,
            },
        )

    def test_create_update_unchanged_ambiguous_blocked(self) -> None:
        products = {
            decision.business_identity[0]: decision
            for decision in self.result.decisions
            if decision.dataset == "products"
        }
        self.assertEqual(products["P-CREATE"].classification, Classification.CREATE)
        self.assertEqual(products["P-UPDATE"].classification, Classification.UPDATE)
        self.assertEqual(products["P-SAME"].classification, Classification.UNCHANGED)
        self.assertEqual(products["P-AMB"].classification, Classification.AMBIGUOUS)
        self.assertEqual(products["P-BLOCK"].classification, Classification.BLOCKED)

    def test_update_has_exact_scalar_and_many2many_differences(self) -> None:
        update = next(
            decision
            for decision in self.result.decisions
            if decision.dataset == "products"
            and decision.business_identity[0] == "P-UPDATE"
        )
        by_field = {difference.field: difference for difference in update.differences}
        self.assertEqual(by_field["name"].existing, "Old product name")
        self.assertEqual(by_field["name"].proposed, "New product name")
        self.assertEqual(
            {reference.key for reference in by_field["tag_ids"].proposed},
            {("BLUE",), ("FOOD",)},
        )

    def test_target_only_many2one_resolves_to_business_key(self) -> None:
        resolved = next(
            item
            for item in self.result.reference_resolutions
            if item.dataset == "products"
            and item.field == "uom_id"
            and item.reference.key == ("KG",)
        )
        self.assertEqual(resolved.status, "RESOLVED")
        self.assertEqual(resolved.match_count, 1)

    def test_composite_relational_identity_matches_and_compares_decimal(self) -> None:
        line = next(
            decision
            for decision in self.result.decisions
            if decision.dataset == "asset_lines"
            and decision.source_row == 2
        )
        self.assertEqual(line.classification, Classification.UPDATE)
        self.assertIsInstance(line.business_identity[0], BusinessReference)
        self.assertEqual(line.business_identity[1], 1)
        self.assertEqual([item.field for item in line.differences], ["quantity"])

    def test_scoped_identity_does_not_match_other_company(self) -> None:
        scoped = next(
            decision
            for decision in self.result.decisions
            if decision.dataset == "products"
            and decision.business_identity[0] == "P-SCOPE"
        )
        self.assertEqual(scoped.classification, Classification.CREATE)
        self.assertEqual(scoped.business_scope[0].key, ("BE",))
        portable = next(
            decision
            for decision in self.result.to_portable_dict()["decisions"]
            if decision["dataset"] == "products"
            and decision["business_identity"][0] == "P-SCOPE"
        )
        self.assertEqual(portable["business_scope"][0]["key"], ["BE"])

    def test_missing_dataset_reference_blocks_child(self) -> None:
        blocked = next(
            decision
            for decision in self.result.decisions
            if decision.dataset == "asset_lines"
            and decision.source_row == 4
        )
        self.assertEqual(blocked.classification, Classification.BLOCKED)
        self.assertIn("REFERENCE_NOT_FOUND", {issue.code for issue in blocked.issues})

    def test_duplicate_target_identity_is_ambiguous(self) -> None:
        ambiguous = next(
            decision
            for decision in self.result.decisions
            if decision.dataset == "products"
            and decision.business_identity[0] == "P-AMB"
        )
        self.assertEqual(ambiguous.target_match_count, 2)
        self.assertEqual(ambiguous.differences, ())

    def test_grouped_reference_root_causes_have_affected_counts(self) -> None:
        unit = next(
            item
            for item in self.result.reference_resolutions
            if item.dataset == "products"
            and item.field == "uom_id"
            and item.reference.key == ("UNIT",)
        )
        self.assertEqual(unit.affected_count, 4)

    def test_manifest_has_no_numeric_odoo_identifiers(self) -> None:
        manifest = self.result.to_portable_dict()
        text = canonical_json_bytes(manifest).decode()
        self.assertEqual(manifest["engine"], {"name": "impodo"})
        self.assertEqual(manifest["profile"], {"id": "golden_slice"})
        self.assertNotIn("odoo_id", text)
        self.assertNotIn('"id":100', text)
        self.assertNotIn('"id":300', text)

    def test_repeated_run_is_byte_deterministic(self) -> None:
        first = canonical_json_bytes(self.result.to_portable_dict())
        second = canonical_json_bytes(golden_result().to_portable_dict())
        self.assertEqual(first, second)

    def test_final_selection_value_must_exist_in_fresh_odoo_choices(self) -> None:
        plan, prepared, metadata, records = self._selection_evidence(
            allowed_names={"New product name"}
        )
        changed_records = tuple(
            replace(
                record,
                scalar_values={**record.scalar_values, "name": "Legacy label"},
            )
            if record.dataset == "products"
            and record.target_identity == ("P-UPDATE",)
            else record
            for record in prepared.records
        )

        result = PreflightEngine().run(
            plan,
            replace(prepared, records=changed_records),
            metadata,
            records,
        )

        rejected = next(
            item
            for item in result.decisions
            if item.dataset == "products"
            and item.business_identity == ("P-UPDATE",)
        )
        self.assertEqual(rejected.classification, Classification.BLOCKED)
        issue = next(
            item
            for item in rejected.issues
            if item.code == "TARGET_SELECTION_VALUE_UNAVAILABLE"
        )
        self.assertEqual(issue.field, "name")
        self.assertIn("current Odoo choices", issue.message)

    def test_null_selection_value_does_not_require_a_choice_code(self) -> None:
        plan, prepared, metadata, records = self._selection_evidence(
            allowed_names=set()
        )
        changed_records = tuple(
            replace(
                record,
                scalar_values={**record.scalar_values, "name": None},
            )
            if record.dataset == "products"
            and record.target_identity == ("P-UPDATE",)
            else record
            for record in prepared.records
        )

        result = PreflightEngine().run(
            plan,
            replace(prepared, records=changed_records),
            metadata,
            records,
        )

        decision = next(
            item
            for item in result.decisions
            if item.dataset == "products"
            and item.business_identity == ("P-UPDATE",)
        )
        self.assertNotEqual(decision.classification, Classification.BLOCKED)
        self.assertNotIn(
            "TARGET_SELECTION_VALUE_UNAVAILABLE",
            {item.code for item in decision.issues},
        )

    def test_selection_choice_drift_warns_and_removed_used_code_blocks(self) -> None:
        plan, prepared, metadata, records = self._selection_evidence(
            allowed_names={"Other product"}
        )
        captured = OdooSchemaCatalog(
            project_id="project:test",
            policy_hash=CURRENT_ODOO_SOURCE_POLICY.content_hash,
            captured_at=datetime(2026, 8, 7, tzinfo=timezone.utc),
            captured_by="Test operator",
            connection_mode="LOCAL",
            database="odoo19_test",
            odoo_version="19.0",
            models=(
                SchemaModel(
                    name="product.template",
                    label="Product",
                    fields=(
                        SchemaField(
                            name="name",
                            label="Name",
                            type="selection",
                            required=True,
                            readonly=False,
                            relation=None,
                            relation_field=None,
                            selection=(
                                ("New product name", "New product"),
                                ("Other product", "Other product"),
                            ),
                        ),
                    ),
                ),
            ),
            content_hash="sha256:captured-schema",
            origin=SchemaOrigin.LIVE_API,
            read_credential_binding_hash="sha256:read-credential",
            read_principal_hash="sha256:read-principal",
            read_permission_hash="sha256:read-permission",
            read_context_hash="sha256:read-context",
            connection_target_hash="sha256:connection-target",
        )

        result = PreflightEngine().run(
            plan,
            prepared,
            metadata,
            records,
            captured_schema=captured,
        )

        update = next(
            item
            for item in result.decisions
            if item.dataset == "products"
            and item.business_identity == ("P-UPDATE",)
        )
        self.assertEqual(update.classification, Classification.BLOCKED)
        self.assertIn(
            "TARGET_SELECTION_VALUE_UNAVAILABLE",
            {item.code for item in update.issues},
        )
        drift = next(
            item
            for item in result.issues
            if item.code == "TARGET_SELECTION_CHOICES_CHANGED"
        )
        self.assertEqual(drift.severity.value, "warning")

    @staticmethod
    def _selection_evidence(*, allowed_names: set[str]):
        plan = compile_profile_document(
            load_profile(ROOT / "profiles/examples/golden_slice.yaml")
        )
        prepared = prepare_sources(plan, ROOT / "examples/golden")
        connector = SnapshotConnector(
            combined_path=ROOT / "fixtures/golden/target_snapshot.json"
        )
        metadata = connector.get_model_metadata(plan_metadata_requests(plan))
        model = metadata.models["product.template"]
        fields = dict(model.fields)
        original = fields["name"]
        fields["name"] = FieldMetadata(
            name=original.name,
            type="selection",
            label=original.label,
            required=original.required,
            readonly=original.readonly,
            relation=original.relation,
            relation_field=original.relation_field,
            selection=tuple((value, value) for value in sorted(allowed_names)),
        )
        models = dict(metadata.models)
        models["product.template"] = ModelMetadata(
            model=model.model,
            description=model.description,
            fields=fields,
            unique_constraints=model.unique_constraints,
        )
        live_metadata = MetadataSnapshot(
            fingerprint=metadata.fingerprint,
            models=models,
            complete=metadata.complete,
            limitations=metadata.limitations,
        )
        records = connector.get_records(
            plan_record_requests(plan, prepared.records)
        )
        return plan, prepared, live_metadata, records

    def test_create_only_dataset_blocks_existing_identity(self) -> None:
        profile = compile_profile_document(
            load_profile(ROOT / "profiles/examples/golden_slice.yaml")
        )
        products = profile.dataset("products")
        changed_target = products.target.model_copy(
            update={"mode": "create", "on_existing": "block"}
        )
        changed_products = products.model_copy(update={"target": changed_target})
        changed_profile = profile.model_copy(
            update={
                "datasets": tuple(
                    changed_products if item.name == "products" else item
                    for item in profile.datasets
                )
            }
        )
        prepared = prepare_sources(changed_profile, ROOT / "examples/golden")
        connector = SnapshotConnector(
            combined_path=ROOT / "fixtures/golden/target_snapshot.json"
        )
        result = PreflightEngine().run(
            changed_profile,
            prepared,
            connector.get_model_metadata(
                plan_metadata_requests(changed_profile)
            ),
            connector.get_records(
                plan_record_requests(changed_profile, prepared.records)
            ),
        )
        same = next(
            decision
            for decision in result.decisions
            if decision.dataset == "products"
            and decision.business_identity[0] == "P-SAME"
        )
        self.assertEqual(same.classification, Classification.BLOCKED)
        self.assertIn("CREATE_IDENTITY_EXISTS", {issue.code for issue in same.issues})

    def test_reference_mode_rows_resolve_without_import_decisions(self) -> None:
        profile = compile_profile_document(
            load_profile(ROOT / "profiles/examples/golden_slice.yaml")
        )
        products = profile.dataset("products")
        reference_products = products.model_copy(
            update={
                "target": products.target.model_copy(update={"mode": "reference"})
            }
        )
        changed_profile = profile.model_copy(
            update={
                "datasets": tuple(
                    reference_products if item.name == "products" else item
                    for item in profile.datasets
                )
            }
        )
        prepared = prepare_sources(changed_profile, ROOT / "examples/golden")
        connector = SnapshotConnector(
            combined_path=ROOT / "fixtures/golden/target_snapshot.json"
        )
        result = PreflightEngine().run(
            changed_profile,
            prepared,
            connector.get_model_metadata(plan_metadata_requests(changed_profile)),
            connector.get_records(
                plan_record_requests(changed_profile, prepared.records)
            ),
        )

        self.assertFalse(
            any(decision.dataset == "products" for decision in result.decisions)
        )
        self.assertTrue(
            any(
                resolution.dataset == "products"
                for resolution in result.reference_resolutions
            )
        )

    def test_browser_and_profile_plans_share_portable_preflight_semantics(self) -> None:
        profile_plan = compile_profile_document(
            load_profile(ROOT / "profiles/examples/golden_slice.yaml")
        )
        browser_plan = CompiledMigrationPlan.model_validate(
            {
                **profile_plan.model_dump(mode="python"),
                "origin": "browser_mapping",
                "source_selection_hash": "sha256:" + "1" * 64,
                "schema_hash": "sha256:" + "2" * 64,
            }
        )
        prepared = prepare_sources(profile_plan, ROOT / "examples/golden")
        connector = SnapshotConnector(
            combined_path=ROOT / "fixtures/golden/target_snapshot.json"
        )
        metadata = connector.get_model_metadata(
            plan_metadata_requests(profile_plan)
        )
        records = connector.get_records(
            plan_record_requests(profile_plan, prepared.records)
        )

        profile_result = PreflightEngine().run(
            profile_plan,
            prepared,
            metadata,
            records,
        )
        browser_result = PreflightEngine().run(
            browser_plan,
            prepared,
            metadata,
            records,
        )

        self.assertEqual(browser_result.decisions, profile_result.decisions)
        self.assertEqual(
            browser_result.reference_resolutions,
            profile_result.reference_resolutions,
        )
        self.assertEqual(browser_result.issues, profile_result.issues)


class Many2ManyOperationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.blue = BusinessReference("x.tag", ("BLUE",))
        self.food = BusinessReference("x.tag", ("FOOD",))
        self.resolve = ResolveSpec(
            target_model="x.tag",
            target_fields=("code",),
        )

    def relation(self, operation: str) -> RelationSpec:
        return RelationSpec(
            kind="many2many",
            source_fields=("tags",),
            resolve=self.resolve,
            operation=operation,
        )

    def test_replace_is_set_based(self) -> None:
        changed, final = _relation_difference(
            self.relation("replace"),
            (self.blue, self.food),
            (self.food, self.blue),
        )
        self.assertFalse(changed)
        self.assertEqual(set(final), {self.blue, self.food})

    def test_add_only_adds_missing_values(self) -> None:
        changed, final = _relation_difference(
            self.relation("add"),
            (self.blue,),
            (self.food,),
        )
        self.assertTrue(changed)
        self.assertEqual(set(final), {self.blue, self.food})

    def test_remove_only_removes_named_values(self) -> None:
        changed, final = _relation_difference(
            self.relation("remove"),
            (self.blue, self.food),
            (self.blue,),
        )
        self.assertTrue(changed)
        self.assertEqual(final, (self.food,))


if __name__ == "__main__":
    unittest.main()
