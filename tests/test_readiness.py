from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock
from uuid import uuid4

from impodo.access import (
    Actor,
    ActorIdentity,
    AuthorizationError,
    CapabilityAuthorizationPolicy,
)
from impodo.application.preflight_service import PreflightService
from impodo.connectors import MetadataSnapshot, RecordSnapshot
from impodo.derived_entities import (
    DerivedEntityPlan,
    DerivedEntityRule,
    RelatedDatasetRule,
    derived_dataset_links,
    mapping_source_selection,
)
from impodo.engine import PreflightEngine
from impodo.inspection import (
    SourceColumnProfile,
    SourceFileCatalog,
    SourceTableCatalog,
)
from impodo.domain.mapping.contracts import (
    BusinessControlTotal,
    DatasetMapping,
    IdentityComponentMapping,
    MappingDefinition,
    MappingTargetMode,
    ReferenceKeyMapping,
    RelationshipMapping,
    RelationshipResolver,
    ResolverOrigin,
    ScalarFieldMapping,
    ValueMapping,
)
from impodo.domain.mapping.scalar_values import evaluate_scalar_mapping_value
from impodo.value_rules import (
    ScalarTransformPolicy,
    ScalarValidationPolicy,
)
from impodo.models import (
    Classification,
    FieldMetadata,
    ModelMetadata,
    TargetFingerprint,
    target_identity_hash,
)
from impodo.projects import (
    MigrationProject,
    OdooConnectionMode,
    ProjectStatus,
    SourceFile,
)
from impodo.application.preparation_service import (
    canonical_source_hashes,
    stage_browser_mapping,
)
from impodo.domain.errors import ReadinessError
from impodo.domain.staging.evaluator import evaluate_browser_mapping
from impodo.domain.staging.transformation_impact import _display_values_equal
from impodo.domain.staging.scale import (
    BROWSER_EVALUATION_ROW_LIMIT,
    browser_evaluation_scale,
    require_supported_browser_scale,
)
from impodo.source import load_selected_source_table
from impodo.staging_contracts import (
    CanonicalStagingRun,
    StagingDisposition,
)
from impodo.workspace_contracts import (
    SourceDataset,
    SourceDatasetColumn,
    SourceSelection,
)


ROOT = Path(__file__).resolve().parents[1]


class TransformationImpactDisplayTests(unittest.TestCase):
    def test_fast_equality_preserves_cross_type_display_semantics(self) -> None:
        self.assertTrue(_display_values_equal("1.00", Decimal("1.00")))
        self.assertFalse(_display_values_equal(False, 0))
        self.assertTrue(
            _display_values_equal(
                datetime(2026, 1, 2, 3, 4, tzinfo=timezone.utc),
                "2026-01-02T03:04:00Z",
            )
        )


class PreflightAuthorizationTests(unittest.TestCase):
    def test_missing_preflight_capability_blocks_before_evidence_or_target_access(
        self,
    ) -> None:
        repositories = [MagicMock() for _ in range(7)]
        service = PreflightService(
            staging=repositories[0],
            quality=repositories[1],
            normalization=repositories[2],
            mappings=repositories[3],
            projects=repositories[4],
            sources=repositories[5],
            preflight=repositories[6],
            artifacts=MagicMock(),
            authorization=CapabilityAuthorizationPolicy(),
        )
        reader = MagicMock()
        actor = Actor(
            identity=ActorIdentity(
                issuer="test",
                subject_id="restricted-user",
                display_name="Restricted user",
            ),
            capabilities=frozenset(),
        )

        with self.assertRaisesRegex(AuthorizationError, "preflight.run"):
            service.compare(
                "00000000-0000-0000-0000-000000000000",
                reader=reader,
                actor=actor,
            )

        reader.assert_not_called()
        for repository in repositories:
            self.assertEqual(repository.mock_calls, [])


class _SingleArtifactStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    @contextmanager
    def materialize_source(self, project_id: str, storage_key: str):
        del project_id, storage_key
        yield self.path


class BrowserReadinessStagingTests(unittest.TestCase):
    def setUp(self) -> None:
        (ROOT / ".tmp").mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=ROOT / ".tmp")
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_source_hashes_are_canonicalized_before_governance_publication(
        self,
    ) -> None:
        selection = self._evidence((("BOM-A", "1", "COMP-1"),))[2]
        dataset = selection.datasets[0]

        self.assertEqual(
            canonical_source_hashes(selection),
            {dataset.file_id: f"sha256:{dataset.source_sha256}"},
        )
        prefixed = replace(
            selection,
            datasets=(
                replace(dataset, source_sha256=f"sha256:{dataset.source_sha256}"),
            ),
        )
        self.assertEqual(
            canonical_source_hashes(prefixed),
            {dataset.file_id: f"sha256:{dataset.source_sha256}"},
        )
        malformed = replace(
            selection,
            datasets=(replace(dataset, source_sha256="sha256:not-a-digest"),),
        )
        with self.assertRaisesRegex(ReadinessError, "could not verify"):
            canonical_source_hashes(malformed)

    def test_bom_rows_become_unique_headers_and_related_lines(self) -> None:
        evidence = self._evidence(
            (
                (" BOM-A ", "1", "COMP-1"),
                ("BOM-A", "2", "COMP-2"),
                ("BOM-B", "1", "COMP-3"),
            )
        )
        staged = stage_browser_mapping(*evidence)

        by_dataset = staged.prepared.by_dataset()
        self.assertEqual(len(by_dataset["boms"]), 2)
        self.assertEqual(len(by_dataset["bom_components"]), 3)
        self.assertEqual(
            [record.source_identity for record in by_dataset["boms"]],
            [("BOM-A",), ("BOM-B",)],
        )
        reconciliation = {
            item.dataset: item for item in staged.canonical_run.datasets
        }
        self.assertEqual(reconciliation["boms"].role.value, "PARENT")
        self.assertEqual(reconciliation["boms"].input_rows, 3)
        self.assertEqual(reconciliation["boms"].output_rows, 2)
        self.assertEqual(reconciliation["boms"].combined_rows, 1)
        self.assertEqual(reconciliation["bom_components"].role.value, "CHILD")
        self.assertEqual(reconciliation["bom_components"].output_rows, 3)
        bom_a = next(
            item
            for item in staged.canonical_run.rows
            if item.dataset == "boms" and item.source_identity == ("BOM-A",)
        )
        self.assertEqual(bom_a.lineage.physical_source_rows, (2, 3))

        metadata, records = self._snapshots(evidence[0])
        result = PreflightEngine().run(
            staged.plan,
            staged.prepared,
            metadata,
            records,
        )
        self.assertEqual(result.counts[Classification.CREATE.value], 5)
        self.assertEqual(result.counts[Classification.BLOCKED.value], 0)

    def test_canonical_evaluator_is_deterministic_and_storage_independent(
        self,
    ) -> None:
        evidence = self._evidence(
            (
                (" BOM-A ", "1", "COMP-1"),
                ("BOM-A", "2", "COMP-2"),
                ("BOM-B", "1", "COMP-3"),
            )
        )
        (
            project,
            definition,
            physical,
            effective,
            plan,
            _catalogs,
            artifact_store,
        ) = evidence
        physical_dataset = physical.datasets[0]
        loaded = load_selected_source_table(
            artifact_store.path,
            dataset=physical_dataset.name,
            table_key=physical_dataset.table_key,
            encoding=physical_dataset.encoding,
            delimiter=physical_dataset.delimiter,
            header_row=physical_dataset.header_row,
        )

        direct = evaluate_browser_mapping(
            project_id=project.project_id,
            definition=definition,
            physical_selection=physical,
            effective_selection=effective,
            plan=plan,
            loaded_tables={physical_dataset.dataset_id: loaded},
        )
        compatibility = stage_browser_mapping(*evidence)
        repeated = evaluate_browser_mapping(
            project_id=project.project_id,
            definition=definition,
            physical_selection=physical,
            effective_selection=effective,
            plan=plan,
            loaded_tables={physical_dataset.dataset_id: loaded},
        )

        self.assertEqual(direct.prepared, compatibility.prepared)
        self.assertEqual(
            direct.canonical_run.content_hash,
            compatibility.canonical_run.content_hash,
        )
        self.assertEqual(
            direct.canonical_run.to_json(),
            repeated.canonical_run.to_json(),
        )
        self.assertEqual(
            direct.canonical_run.compiled_plan_hash,
            direct.plan.semantic_hash,
        )
        self.assertEqual(direct.plan.origin, "browser_mapping")
        self.assertEqual(
            direct.plan.source_selection_hash,
            effective.content_hash,
        )
        self.assertEqual(direct.canonical_run.reconciliation.total_rows, 5)
        self.assertEqual(direct.canonical_run.reconciliation.candidate_rows, 5)
        self.assertEqual(direct.canonical_run.reconciliation.blocked_rows, 0)
        self.assertTrue(
            all(
                row.lineage.mapping_hash == definition.content_hash
                for row in direct.canonical_run.rows
            )
        )
        self.assertTrue(
            all(
                row.lineage.source_hash
                == f"sha256:{physical_dataset.source_sha256}"
                for row in direct.canonical_run.rows
            )
        )
        self.assertNotIn("odoo_id", direct.canonical_run.to_json())

        restored = CanonicalStagingRun.from_json(direct.canonical_run.to_json())
        self.assertEqual(restored.content_hash, direct.canonical_run.content_hash)
        self.assertEqual(restored.to_json(), direct.canonical_run.to_json())

        legacy = replace(
            direct.canonical_run,
            contract_version=2,
            evaluator_version=1,
            compiled_plan_hash=None,
            control_totals=(),
        )
        restored_legacy = CanonicalStagingRun.from_json(legacy.to_json())
        self.assertEqual(restored_legacy.content_hash, legacy.content_hash)
        self.assertNotIn("control_totals", legacy.to_portable_dict())

        changed_definition = replace(
            definition,
            schema_hash="sha256:" + "9" * 64,
        )
        changed = evaluate_browser_mapping(
            project_id=project.project_id,
            definition=changed_definition,
            physical_selection=physical,
            effective_selection=effective,
            plan=plan,
            loaded_tables={physical_dataset.dataset_id: loaded},
        )
        self.assertNotEqual(
            changed.canonical_run.content_hash,
            direct.canonical_run.content_hash,
        )

        tampered = json.loads(direct.canonical_run.to_json())
        tampered["rows"][0]["proposed_values"]["component_code"] = "TAMPERED"
        with self.assertRaisesRegex(ValueError, "content hash is invalid"):
            CanonicalStagingRun.from_dict(tampered)

    def test_duplicate_bom_line_keys_are_blocked_at_row_level(self) -> None:
        evidence = self._evidence(
            (
                ("BOM-A", "1", "COMP-1"),
                ("BOM-A", "1", "COMP-2"),
            )
        )
        staged = stage_browser_mapping(*evidence)
        metadata, records = self._snapshots(evidence[0])
        result = PreflightEngine().run(
            staged.plan,
            staged.prepared,
            metadata,
            records,
        )

        child_decisions = [
            item for item in result.decisions if item.dataset == "bom_components"
        ]
        self.assertEqual(
            [item.classification for item in child_decisions],
            [Classification.BLOCKED, Classification.BLOCKED],
        )
        self.assertTrue(
            all(
                "SOURCE_IDENTITY_DUPLICATE"
                in {issue.code for issue in item.issues}
                for item in child_decisions
            )
        )

    def test_guided_value_rule_failure_blocks_only_the_affected_row(self) -> None:
        evidence = self._evidence(
            (
                ("BOM-A", "1", "COMP-1"),
                ("BOM-A", "2", "BAD"),
            )
        )
        definition = evidence[1]
        parent, child = definition.datasets
        component = replace(
            child.fields[0],
            validation=ScalarValidationPolicy(exact_length=6),
        )
        definition = replace(
            definition,
            datasets=(parent, replace(child, fields=(component,))),
        )
        evidence = (evidence[0], definition, *evidence[2:])

        staged = stage_browser_mapping(*evidence)
        metadata, records = self._snapshots(evidence[0])
        result = PreflightEngine().run(
            staged.plan,
            staged.prepared,
            metadata,
            records,
        )

        child_decisions = [
            item for item in result.decisions if item.dataset == "bom_components"
        ]
        self.assertEqual(
            [item.classification for item in child_decisions],
            [Classification.CREATE, Classification.BLOCKED],
        )
        self.assertEqual(
            {issue.code for issue in child_decisions[1].issues},
            {"SOURCE_TEXT_LENGTH_INVALID"},
        )
        canonical_row = next(
            item
            for item in staged.canonical_run.rows
            if item.dataset == "bom_components" and item.source_row == 3
        )
        self.assertEqual(canonical_row.disposition, StagingDisposition.BLOCKED)
        self.assertEqual(
            {item.code for item in canonical_row.issues},
            {"SOURCE_TEXT_LENGTH_INVALID"},
        )

    def test_relationship_choice_match_stages_the_odoo_business_key(self) -> None:
        evidence = self._evidence(
            (("BOM-A", "1", "FRA"), ("BOM-A", "2", "BEL"))
        )
        definition = evidence[1]
        parent, child = definition.datasets
        country = RelationshipMapping(
            target_field="country_id",
            kind="many2one",
            source_column_keys=("column:component",),
            resolver=RelationshipResolver(
                origin=ResolverOrigin.TARGET_CATALOG,
                model="res.country",
                key_mappings=(
                    ReferenceKeyMapping("column:component", "code"),
                ),
                value_mappings=(
                    ValueMapping("FRA", "FR"),
                    ValueMapping("BEL", "BE"),
                ),
            ),
        )
        definition = replace(
            definition,
            datasets=(
                parent,
                replace(child, relationships=(country,)),
            ),
        )

        staged = stage_browser_mapping(
            evidence[0],
            definition,
            *evidence[2:],
            collect_transformation_impact=True,
        )
        references = [
            record.references["country_id"].key
            for record in staged.prepared.by_dataset()["bom_components"]
        ]

        self.assertEqual(references, [("FR",), ("BE",)])
        assert staged.transformation_impact is not None
        self.assertEqual(staged.transformation_impact.changed_count, 2)
        self.assertEqual(
            {item.target_field for item in staged.transformation_impact.rows},
            {"country_id"},
        )
        self.assertTrue(
            all(
                item.rules.startswith("Reviewed value match")
                for item in staged.transformation_impact.rows
            )
        )

    def test_transformation_impact_compares_every_raw_and_proposed_value(self) -> None:
        evidence = self._evidence(
            (
                ("BOM-A", "1", " comp-1 "),
                ("BOM-A", "2", "COMP-2"),
            )
        )
        definition = evidence[1]
        parent, child = definition.datasets
        component = replace(
            child.fields[0],
            transform=ScalarTransformPolicy(trim=True, case_mode="uppercase"),
        )
        definition = replace(
            definition,
            datasets=(parent, replace(child, fields=(component,))),
        )

        staged = stage_browser_mapping(
            evidence[0],
            definition,
            *evidence[2:],
            collect_transformation_impact=True,
        )

        report = staged.transformation_impact
        self.assertIsNotNone(report)
        self.assertEqual(report.evaluated_count, 3)
        self.assertEqual(report.changed_count, 1)
        self.assertEqual(report.unchanged_count, 2)
        self.assertEqual(len(report.rows), 1)
        self.assertEqual(report.rows[0].source_row, 2)
        self.assertEqual(report.rows[0].source_column, "Component")
        self.assertEqual(report.rows[0].raw_value, " comp-1 ")
        self.assertEqual(report.rows[0].proposed_value, "COMP-1")
        self.assertIn("Trim", report.rows[0].rules)
        self.assertIn("Case: uppercase", report.rows[0].rules)

    def test_browser_preview_and_full_row_runtime_share_scalar_semantics(self) -> None:
        evidence = self._evidence(
            (("BOM-A", "1", " comp-1 "), ("BOM-A", "2", "COMP-2"))
        )
        definition = evidence[1]
        parent, child = definition.datasets
        amount = ScalarFieldMapping(
            target_field="amount_total",
            source_column_key="column:line_id",
            value_type="decimal",
            transform=ScalarTransformPolicy(
                formula="column_2 * 1.25",
                decimal_places=2,
                rounding_mode="half_up",
            ),
        )
        definition = replace(
            definition,
            datasets=(parent, replace(child, fields=(*child.fields, amount))),
        )

        preview = evaluate_scalar_mapping_value(
            amount,
            "1",
            source_values_by_ordinal={
                1: "BOM-A",
                2: "1",
                3: " comp-1 ",
            },
        )
        staged = stage_browser_mapping(
            evidence[0],
            definition,
            *evidence[2:],
        )
        runtime = staged.prepared.by_dataset()["bom_components"][0]

        self.assertEqual(preview, Decimal("1.25"))
        self.assertEqual(runtime.scalar_values["amount_total"], preview)

    def test_declared_business_total_uses_prepared_values_without_guessing(self) -> None:
        evidence = self._evidence(
            (("BOM-A", "1", "COMP-1"), ("BOM-A", "2", "COMP-2"))
        )
        definition = evidence[1]
        parent, child = definition.datasets
        quantity = ScalarFieldMapping(
            target_field="product_qty",
            source_column_key="column:line_id",
            value_type="decimal",
        )
        definition = replace(
            definition,
            datasets=(
                parent,
                replace(
                    child,
                    fields=(*child.fields, quantity),
                    control_totals=(
                        BusinessControlTotal(
                            name="Component quantity",
                            target_field="product_qty",
                            expected_total="3",
                            unit="units",
                        ),
                    ),
                ),
            ),
        )

        staged = stage_browser_mapping(
            evidence[0],
            definition,
            *evidence[2:],
        )
        total = staged.canonical_run.control_totals[0]

        self.assertTrue(total.passed)
        self.assertEqual(total.actual_total, "3")
        self.assertEqual(total.expected_total, "3")
        self.assertEqual(total.included_rows, 2)
        self.assertEqual(total.empty_rows, 0)
        self.assertEqual(total.unit, "units")

    def test_in_memory_scale_limit_blocks_before_loading_source(self) -> None:
        evidence = self._evidence((("BOM-A", "1", "COMP-1"),))
        physical = evidence[2]
        oversized = replace(
            physical,
            datasets=(
                replace(
                    physical.datasets[0],
                    row_count=BROWSER_EVALUATION_ROW_LIMIT + 1,
                ),
            ),
        )

        scale = browser_evaluation_scale(oversized)

        self.assertFalse(scale.supported)
        with self.assertRaisesRegex(
            ReadinessError,
            "Split the source into smaller projects",
        ):
            require_supported_browser_scale(oversized)

    def test_product_category_column_stages_categories_and_product_links(
        self,
    ) -> None:
        evidence, link = self._lookup_evidence(
            (("P001", "Article"), ("P002", "Service"), ("P003", "Article"))
        )

        staged = stage_browser_mapping(*evidence)
        by_dataset = staged.prepared.by_dataset()

        self.assertEqual(
            [item.source_identity for item in by_dataset["product_categories"]],
            [("article",), ("service",)],
        )
        self.assertEqual(
            [
                item.references["categ_id"].key
                for item in by_dataset["products"]
            ],
            [("article",), ("service",), ("article",)],
        )
        self.assertEqual(
            evidence[3].datasets[0].dataset_id,
            link.derived_dataset_id,
        )
        reconciliation = {
            item.dataset: item for item in staged.canonical_run.datasets
        }["product_categories"]
        self.assertEqual(reconciliation.role.value, "LOOKUP")
        self.assertEqual(reconciliation.input_rows, 3)
        self.assertEqual(reconciliation.output_rows, 2)
        self.assertEqual(reconciliation.combined_rows, 1)

        metadata, records = self._product_snapshots(evidence[0])
        result = PreflightEngine().run(
            staged.plan,
            staged.prepared,
            metadata,
            records,
        )

        self.assertEqual(result.counts[Classification.CREATE.value], 5)
        self.assertEqual(result.counts[Classification.BLOCKED.value], 0)

    def test_blank_derived_reference_blocks_only_affected_product(self) -> None:
        evidence, _link = self._lookup_evidence(
            (("P001", "Article"), ("P002", ""))
        )

        staged = stage_browser_mapping(*evidence)
        products = staged.prepared.by_dataset()["products"]

        self.assertFalse(products[0].blocked)
        self.assertEqual(
            {item.code for item in products[1].issues},
            {"DERIVED_REFERENCE_MISSING"},
        )
        lookup = {
            item.dataset: item for item in staged.canonical_run.datasets
        }["product_categories"]
        self.assertEqual(lookup.input_rows, 2)
        self.assertEqual(lookup.input_rows_used, 1)
        self.assertEqual(lookup.unrepresented_rows, 1)

    def test_conflicting_lookup_spelling_requires_review(self) -> None:
        evidence, _link = self._lookup_evidence(
            (("P001", "Article"), ("P002", "ARTICLE"))
        )

        staged = stage_browser_mapping(*evidence)
        category = staged.prepared.by_dataset()["product_categories"][0]

        self.assertEqual(category.source_identity, ("article",))
        self.assertEqual(
            {item.code for item in category.issues},
            {"DERIVED_ALIAS_REVIEW_REQUIRED"},
        )

    def _evidence(self, rows):
        project_id = str(uuid4())
        file_id = str(uuid4())
        source_path = self.root / f"{file_id}.csv"
        source_path.write_text(
            "BOMId,LineId,Component\n"
            + "".join(",".join(row) + "\n" for row in rows),
            encoding="utf-8",
        )
        digest = sha256(source_path.read_bytes()).hexdigest()
        now = datetime.now(timezone.utc)
        profiles = (
            _column_profile(1, "BOMId", len({row[0].strip() for row in rows})),
            _column_profile(2, "LineId", len({row[1] for row in rows})),
            _column_profile(3, "Component", len({row[2] for row in rows})),
        )
        table = SourceTableCatalog(
            table_key="csv",
            name="bom",
            kind="CSV",
            hidden=False,
            header_row=1,
            row_count=len(rows),
            column_count=3,
            columns=profiles,
            preview_rows=tuple(rows),
        )
        catalog = SourceFileCatalog(
            contract_version=1,
            file_id=file_id,
            display_name="bom.csv",
            source_sha256=digest,
            source_size_bytes=source_path.stat().st_size,
            format="csv",
            inspected_at=now,
            encoding="utf-8",
            delimiter=",",
            tables=(table,),
        )
        columns = (
            SourceDatasetColumn(1, "BOMId", "column:bom_id", "string"),
            SourceDatasetColumn(2, "LineId", "column:line_id", "integer"),
            SourceDatasetColumn(3, "Component", "column:component", "string"),
        )
        physical_dataset = SourceDataset(
            dataset_id="dataset:bom",
            name="bom_rows",
            file_id=file_id,
            table_key="csv",
            source_sha256=digest,
            catalog_hash=catalog.content_hash,
            encoding="utf-8",
            delimiter=",",
            header_row=1,
            row_count=len(rows),
            columns=columns,
        )
        physical = SourceSelection(
            selection_id=str(uuid4()),
            version=1,
            project_id=project_id,
            created_at=now,
            created_by="Tester",
            datasets=(physical_dataset,),
            content_hash="sha256:" + "1" * 64,
        )
        rule = RelatedDatasetRule(
            rule_id=str(uuid4()),
            source_dataset_id=physical_dataset.dataset_id,
            parent_dataset_name="boms",
            child_dataset_name="bom_components",
            parent_key_column_key=columns[0].stable_key,
            child_key_column_key=columns[1].stable_key,
        )
        plan = DerivedEntityPlan(
            plan_id=str(uuid4()),
            version=1,
            project_id=project_id,
            source_selection_hash=physical.content_hash,
            rules=(rule,),
            updated_at=now,
            updated_by="Tester",
        )
        effective = mapping_source_selection(physical, plan, (catalog,))
        parent = next(item for item in effective.datasets if item.name == "boms")
        child = next(
            item for item in effective.datasets if item.name == "bom_components"
        )
        definition = MappingDefinition(
            mapping_id=str(uuid4()),
            source_selection_hash=effective.content_hash,
            schema_hash="sha256:" + "2" * 64,
            datasets=(
                DatasetMapping(
                    dataset_id=parent.dataset_id,
                    target_model="mrp.bom",
                    mode=MappingTargetMode.UPSERT,
                    source_identity_column_keys=(columns[0].stable_key,),
                    target_identity=(
                        IdentityComponentMapping(
                            source_column_keys=(columns[0].stable_key,),
                            target_fields=("code",),
                        ),
                    ),
                    fields=(
                        ScalarFieldMapping(
                            target_field="code",
                            source_column_key=columns[0].stable_key,
                            value_type="string",
                            required=True,
                        ),
                    ),
                ),
                DatasetMapping(
                    dataset_id=child.dataset_id,
                    target_model="mrp.bom.line",
                    mode=MappingTargetMode.UPSERT,
                    source_identity_column_keys=(
                        columns[0].stable_key,
                        columns[1].stable_key,
                    ),
                    target_identity=(
                        IdentityComponentMapping(
                            source_column_keys=(columns[0].stable_key,),
                            target_fields=("bom_id",),
                            resolver=RelationshipResolver(
                                origin=ResolverOrigin.DATASET,
                                dataset_id=parent.dataset_id,
                            ),
                        ),
                        IdentityComponentMapping(
                            source_column_keys=(columns[1].stable_key,),
                            target_fields=("sequence",),
                            value_type="integer",
                        ),
                    ),
                    fields=(
                        ScalarFieldMapping(
                            target_field="component_code",
                            source_column_key=columns[2].stable_key,
                            value_type="string",
                            required=True,
                        ),
                    ),
                ),
            ),
        )
        project = MigrationProject(
            project_id=project_id,
            name="BOM migration",
            source_system="Legacy ERP",
            odoo_connection_mode=OdooConnectionMode.LOCAL,
            odoo_base_url="http://127.0.0.1:8069",
            odoo_database="odoo19_dev",
            intended_models=("mrp.bom", "mrp.bom.line"),
            source_files=(
                SourceFile(
                    file_id=file_id,
                    display_name="bom.csv",
                    stored_name=source_path.name,
                    size_bytes=source_path.stat().st_size,
                    sha256=digest,
                    received_at=now,
                ),
            ),
            status=ProjectStatus.REGISTERED,
            registered_at=now,
        )
        return (
            project,
            definition,
            physical,
            effective,
            plan,
            (catalog,),
            _SingleArtifactStore(source_path),
        )

    def _lookup_evidence(self, rows):
        project_id = str(uuid4())
        file_id = str(uuid4())
        source_path = self.root / f"{file_id}.csv"
        source_path.write_text(
            "DefaultCode,Category\n"
            + "".join(",".join(row) + "\n" for row in rows),
            encoding="utf-8",
        )
        digest = sha256(source_path.read_bytes()).hexdigest()
        now = datetime.now(timezone.utc)
        table = SourceTableCatalog(
            table_key="csv",
            name="products",
            kind="CSV",
            hidden=False,
            header_row=1,
            row_count=len(rows),
            column_count=2,
            columns=(
                _column_profile(1, "DefaultCode", len({row[0] for row in rows})),
                _column_profile(2, "Category", len({row[1] for row in rows})),
            ),
            preview_rows=tuple(rows),
        )
        catalog = SourceFileCatalog(
            contract_version=1,
            file_id=file_id,
            display_name="products.csv",
            source_sha256=digest,
            source_size_bytes=source_path.stat().st_size,
            format="csv",
            inspected_at=now,
            encoding="utf-8",
            delimiter=",",
            tables=(table,),
        )
        columns = (
            SourceDatasetColumn(
                1,
                "DefaultCode",
                "column:default_code",
                "string",
            ),
            SourceDatasetColumn(2, "Category", "column:category", "string"),
        )
        physical_dataset = SourceDataset(
            dataset_id="dataset:products",
            name="products",
            file_id=file_id,
            table_key="csv",
            source_sha256=digest,
            catalog_hash=catalog.content_hash,
            encoding="utf-8",
            delimiter=",",
            header_row=1,
            row_count=len(rows),
            columns=columns,
        )
        physical = SourceSelection(
            selection_id=str(uuid4()),
            version=1,
            project_id=project_id,
            created_at=now,
            created_by="Tester",
            datasets=(physical_dataset,),
            content_hash="sha256:" + "3" * 64,
        )
        rule = DerivedEntityRule(
            rule_id=str(uuid4()),
            output_dataset_name="product_categories",
            source_dataset_id=physical_dataset.dataset_id,
            source_column_key=columns[1].stable_key,
            target_model="product.category",
            target_name_field="name",
            external_id_namespace="legacy",
            blank_policy="block",
        )
        plan = DerivedEntityPlan(
            plan_id=str(uuid4()),
            version=1,
            project_id=project_id,
            source_selection_hash=physical.content_hash,
            rules=(rule,),
            updated_at=now,
            updated_by="Tester",
        )
        effective = mapping_source_selection(physical, plan, (catalog,))
        link = derived_dataset_links(plan)[0]
        category, products = effective.datasets
        definition = MappingDefinition(
            mapping_id=str(uuid4()),
            source_selection_hash=effective.content_hash,
            schema_hash="sha256:" + "4" * 64,
            datasets=(
                DatasetMapping(
                    dataset_id=category.dataset_id,
                    target_model="product.category",
                    mode=MappingTargetMode.UPSERT,
                    source_identity_column_keys=(
                        link.canonical_key_column_key,
                    ),
                    target_identity=(
                        IdentityComponentMapping(
                            source_column_keys=(link.name_column_key,),
                            target_fields=("name",),
                        ),
                    ),
                ),
                DatasetMapping(
                    dataset_id=products.dataset_id,
                    target_model="product.template",
                    mode=MappingTargetMode.UPSERT,
                    source_identity_column_keys=(columns[0].stable_key,),
                    target_identity=(
                        IdentityComponentMapping(
                            source_column_keys=(columns[0].stable_key,),
                            target_fields=("default_code",),
                        ),
                    ),
                    relationships=(
                        RelationshipMapping(
                            target_field="categ_id",
                            kind="many2one",
                            source_column_keys=(columns[1].stable_key,),
                            resolver=RelationshipResolver(
                                origin=ResolverOrigin.DATASET,
                                dataset_id=category.dataset_id,
                            ),
                        ),
                    ),
                ),
            ),
        )
        project = MigrationProject(
            project_id=project_id,
            name="Product migration",
            source_system="Legacy ERP",
            odoo_connection_mode=OdooConnectionMode.LOCAL,
            odoo_base_url="http://127.0.0.1:8069",
            odoo_database="odoo19_dev",
            intended_models=("product.category", "product.template"),
            source_files=(
                SourceFile(
                    file_id=file_id,
                    display_name="products.csv",
                    stored_name=source_path.name,
                    size_bytes=source_path.stat().st_size,
                    sha256=digest,
                    received_at=now,
                ),
            ),
            status=ProjectStatus.REGISTERED,
            registered_at=now,
        )
        return (
            (
                project,
                definition,
                physical,
                effective,
                plan,
                (catalog,),
                _SingleArtifactStore(source_path),
            ),
            link,
        )

    @staticmethod
    def _snapshots(project: MigrationProject):
        fingerprint = TargetFingerprint(
            target_hash=target_identity_hash(
                connection_mode="LOCAL",
                base_url=project.odoo_base_url,
                database=project.odoo_database,
            ),
            connection_mode="LOCAL",
            database=project.odoo_database,
            odoo_version="19.0",
            snapshot_timestamp="2026-08-04T00:00:00Z",
        )
        metadata = MetadataSnapshot(
            fingerprint=fingerprint,
            models={
                "mrp.bom": ModelMetadata(
                    model="mrp.bom",
                    description="Bill of Materials",
                    fields={"code": FieldMetadata("code", "char")},
                ),
                "mrp.bom.line": ModelMetadata(
                    model="mrp.bom.line",
                    description="Bill of Materials Component",
                    fields={
                        "bom_id": FieldMetadata(
                            "bom_id",
                            "many2one",
                            relation="mrp.bom",
                        ),
                        "sequence": FieldMetadata("sequence", "integer"),
                        "component_code": FieldMetadata(
                            "component_code",
                            "char",
                        ),
                    },
                ),
            },
        )
        records = RecordSnapshot(
            fingerprint=fingerprint,
            records={"mrp.bom": (), "mrp.bom.line": ()},
            requested_fields={
                "mrp.bom": ("code",),
                "mrp.bom.line": ("bom_id", "component_code", "sequence"),
            },
        )
        return metadata, records

    @staticmethod
    def _product_snapshots(project: MigrationProject):
        fingerprint = TargetFingerprint(
            target_hash=target_identity_hash(
                connection_mode="LOCAL",
                base_url=project.odoo_base_url,
                database=project.odoo_database,
            ),
            connection_mode="LOCAL",
            database=project.odoo_database,
            odoo_version="19.0",
            snapshot_timestamp="2026-08-04T00:00:00Z",
        )
        metadata = MetadataSnapshot(
            fingerprint=fingerprint,
            models={
                "product.category": ModelMetadata(
                    model="product.category",
                    description="Product Category",
                    fields={"name": FieldMetadata("name", "char")},
                ),
                "product.template": ModelMetadata(
                    model="product.template",
                    description="Product",
                    fields={
                        "default_code": FieldMetadata("default_code", "char"),
                        "categ_id": FieldMetadata(
                            "categ_id",
                            "many2one",
                            relation="product.category",
                        ),
                    },
                ),
            },
        )
        records = RecordSnapshot(
            fingerprint=fingerprint,
            records={"product.category": (), "product.template": ()},
            requested_fields={
                "product.category": ("name",),
                "product.template": ("categ_id", "default_code"),
            },
        )
        return metadata, records


def _column_profile(ordinal: int, name: str, distinct: int) -> SourceColumnProfile:
    return SourceColumnProfile(
        ordinal=ordinal,
        name=name,
        candidate_type="string",
        null_count=0,
        non_null_count=1,
        distinct_count=distinct,
        distinct_count_is_exact=True,
        duplicate_count=0,
        minimum=None,
        maximum=None,
        minimum_length=1,
        maximum_length=20,
    )


if __name__ == "__main__":
    unittest.main()
