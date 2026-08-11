"""Executable Slice 6 contracts for scope, references, and resolution."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest
from uuid import UUID, uuid4

from impodo.access import Capability, LOCAL_ACTOR
from impodo.adapters.duckdb.advanced_coverage_repository import (
    AdvancedCoverageRepository,
)
from impodo.adapters.duckdb.database import DuckDbDatabase
from impodo.adapters.duckdb.project_repository import ProjectRepository
from impodo.adapters.duckdb.quality_repository import QualityRepository
from impodo.adapters.duckdb.staging_repository import StagingRepository
from impodo.domain.coverage import (
    CoverageApplicability,
    CoverageDeclaration,
    CoverageFamily,
    CoverageScopeRevision,
    ReferenceBundle,
    ReferenceDataSet,
    ReferenceEntry,
    ReferenceValueKind,
    validate_odoo_selection_reference_outputs,
)
from impodo.domain.resolution import (
    EffectiveDataset,
    EffectiveRow,
    FieldProvenanceKind,
    FuzzyComparisonField,
    ResolutionDecision,
    ResolutionDecisionKind,
    ResolutionEvaluation,
    ResolutionPolicy,
    ResolutionRule,
    SimilarityAlgorithm,
    build_effective_dataset,
    evaluate_resolution_candidates,
    pass_through_effective_row,
)
from impodo.domain.serialization import content_hash
from impodo.derived_entities import DerivedEntityPlan, mapping_source_selection
from impodo.domain.mapping.contracts import (
    DatasetMapping,
    IdentityComponentMapping,
    MappingDefinition,
    ReferenceLookupMapping,
    ScalarFieldMapping,
)
from impodo.domain.staging.evaluator import evaluate_browser_mapping
from impodo.domain.structural import (
    AggregateOperation,
    AggregateSpec,
    ExactJoinRule,
    GroupAggregateRule,
    GroupKey,
    JoinKey,
    JoinKind,
    StructuralError,
    StructuralOutputColumn,
    StructuralProjection,
    UnionAllRule,
    UnionBranch,
    execute_structural_rules,
    structural_dataset_id,
    structural_mapping_selection,
)
from impodo.source import SourceRow, SourceTable
from impodo.staging_contracts import (
    CanonicalLineage,
    CanonicalRow,
    CanonicalStagingRun,
    StagingDisposition,
    StagingDatasetReconciliation,
    StagingDatasetRole,
    StagingReconciliation,
)
from impodo.projects import MigrationProject, OdooConnectionMode, ProjectStatus
from impodo.quality import (
    QualityOutcomePolicy,
    QualityOwnerRole,
    QualityRule,
    QualityRuleFamily,
    QualityRuleSet,
    QualityRuleSource,
    default_quality_ruleset,
    evaluate_quality,
)
from impodo.workspace_contracts import (
    OdooSchemaCatalog,
    SchemaField,
    SchemaModel,
    SchemaOrigin,
    SourceDataset,
    SourceDatasetColumn,
    SourceSelection,
)


HASH_A = "sha256:" + "a" * 64
HASH_B = "sha256:" + "b" * 64
HASH_C = "sha256:" + "c" * 64
HASH_D = "sha256:" + "d" * 64
NOW = datetime(2026, 8, 6, 9, 0, tzinfo=timezone.utc)
ROOT = Path(__file__).resolve().parents[1]


def _hash(number: int) -> str:
    return "sha256:" + format(number, "064x")


def _scope(*, project_id: str = "project-1", source_hash: str = HASH_A) -> CoverageScopeRevision:
    declarations = tuple(
        CoverageDeclaration(
            family=family,
            applicability=(
                CoverageApplicability.APPLICABLE
                if family in {CoverageFamily.TC_14, CoverageFamily.TC_15}
                else CoverageApplicability.INAPPLICABLE
            ),
            rationale=(
                "Partner matching and reviewed consolidation are required."
                if family in {CoverageFamily.TC_14, CoverageFamily.TC_15}
                else "Not used by this accepted contract fixture."
            ),
            datasets=("partners",)
            if family in {CoverageFamily.TC_14, CoverageFamily.TC_15}
            else (),
        )
        for family in CoverageFamily
    )
    return CoverageScopeRevision(
        scope_id=str(uuid4()),
        project_id=project_id,
        version=1,
        parent_version=None,
        source_selection_hash=source_hash,
        declarations=declarations,
        approved_by=LOCAL_ACTOR.identity,
        approved_at=NOW,
    )


def _reference_dataset() -> ReferenceDataSet:
    entries = (
        ReferenceEntry(key=("BE",), values={"country_code": "BE"}),
        ReferenceEntry(key=("LU",), values={"country_code": "LU"}),
    )
    entries = tuple(sorted(entries, key=lambda item: item.key_hash))
    return ReferenceDataSet(
        reference_id=str(uuid4()),
        version=1,
        name="Approved country translations",
        key_fields=("source_country",),
        value_kinds={"country_code": ReferenceValueKind.ODOO_SELECTION_KEY},
        entries=entries,
        owner="Functional owner",
        classification="internal",
        effective_label="Approved 2026-08-06",
    )


def _policy(
    *,
    project_id: str = "project-1",
    coverage_scope_hash: str = HASH_A,
    mapping_hash: str = HASH_B,
    schema_hash: str = HASH_C,
    reference_bundle_hash: str = HASH_D,
    max_block_size: int = 50,
    max_candidates: int = 5,
) -> ResolutionPolicy:
    rule = ResolutionRule(
        rule_id=str(UUID("00000000-0000-0000-0000-000000000010")),
        dataset="partners",
        blocking_fields=("country",),
        comparison_fields=(
            FuzzyComparisonField(
                field="name",
                algorithm=SimilarityAlgorithm.NORMALIZED_LEVENSHTEIN,
                weight="0.7",
            ),
            FuzzyComparisonField(
                field="street",
                algorithm=SimilarityAlgorithm.TOKEN_JACCARD,
                weight="0.3",
            ),
        ),
        candidate_threshold="0.7",
        survivor_fields=("country", "name", "street"),
        correctable_fields=("name", "street"),
        max_block_size=max_block_size,
        max_candidates_per_row=max_candidates,
    )
    return ResolutionPolicy(
        policy_id=str(uuid4()),
        project_id=project_id,
        version=1,
        parent_version=None,
        coverage_scope_hash=coverage_scope_hash,
        mapping_hash=mapping_hash,
        schema_hash=schema_hash,
        reference_bundle_hash=reference_bundle_hash,
        rules=(rule,),
    )


def _row(number: int, *, name: str, country: str = "BE", street: str = "Main 1") -> CanonicalRow:
    row_id = _hash(number)
    return CanonicalRow(
        row_id=row_id,
        dataset="partners",
        source_row=number,
        target_model="res.partner",
        disposition=StagingDisposition.CANDIDATE,
        source_identity=(f"P-{number}",),
        target_identity=(f"P-{number}",),
        target_scope=(),
        proposed_values={"country": country, "name": name, "street": street},
        references={},
        issues=(),
        lineage=CanonicalLineage(
            source_selection_hash=HASH_A,
            source_hash=HASH_B,
            mapping_hash=HASH_C,
            schema_hash=HASH_D,
            derived_plan_hash=None,
            dataset="partners",
            source_row=number,
            physical_dataset_id="dataset:partners",
            physical_source_rows=(number,),
            field_sources={
                "country": ("country",),
                "name": ("name",),
                "street": ("street",),
            },
        ),
    )


def _staging_run(project_id: str, rows: tuple[CanonicalRow, ...]) -> CanonicalStagingRun:
    dataset = StagingDatasetReconciliation.from_rows(
        dataset="partners",
        target_model="res.partner",
        physical_dataset_id="dataset:partners",
        role=StagingDatasetRole.DIRECT,
        input_rows=len(rows),
        source_rows=tuple(item.source_row for item in rows),
        lineage_links=len(rows),
        rows=rows,
    )
    return CanonicalStagingRun(
        project_id=project_id,
        mapping_id="mapping:partners",
        physical_selection_hash=HASH_A,
        source_selection_hash=HASH_A,
        mapping_hash=HASH_C,
        schema_hash=HASH_D,
        derived_plan_hash=None,
        datasets=(dataset,),
        rows=tuple(
            sorted(rows, key=lambda item: (item.dataset, item.source_row, item.row_id))
        ),
        issues=(),
        reconciliation=StagingReconciliation.from_rows(rows),
        compiled_plan_hash=HASH_C,
    )


def _structural_selection() -> tuple[SourceSelection, dict[str, SourceTable]]:
    specs = (
        SourceDataset(
            dataset_id="customers",
            name="customers",
            file_id="file-customers",
            table_key="customers.csv",
            source_sha256=_hash(301),
            catalog_hash=_hash(302),
            encoding="utf-8",
            delimiter=",",
            header_row=1,
            row_count=2,
            columns=(
                SourceDatasetColumn(1, "Customer", "customer_key", "string"),
                SourceDatasetColumn(2, "Name", "customer_name", "string"),
            ),
        ),
        SourceDataset(
            dataset_id="details",
            name="details",
            file_id="file-details",
            table_key="details.csv",
            source_sha256=_hash(303),
            catalog_hash=_hash(304),
            encoding="utf-8",
            delimiter=",",
            header_row=1,
            row_count=2,
            columns=(
                SourceDatasetColumn(1, "Customer", "detail_customer_key", "string"),
                SourceDatasetColumn(2, "Country", "country", "string"),
            ),
        ),
    )
    selection = SourceSelection(
        selection_id=str(uuid4()),
        version=1,
        project_id="project-1",
        created_at=NOW,
        created_by=LOCAL_ACTOR.identity.display_name,
        datasets=specs,
        content_hash=_hash(305),
    )
    tables = {
        "customers": SourceTable(
            dataset="customers",
            path=Path("customers.csv"),
            headers=("Customer", "Name"),
            rows=(
                SourceRow(1, {"Customer": "C1", "Name": "Alpha"}),
                SourceRow(2, {"Customer": "C2", "Name": "Beta"}),
            ),
            content_hash=_hash(301),
        ),
        "details": SourceTable(
            dataset="details",
            path=Path("details.csv"),
            headers=("Customer", "Country"),
            rows=(
                SourceRow(1, {"Customer": "C1", "Country": "BE"}),
                SourceRow(2, {"Customer": "C2", "Country": "LU"}),
            ),
            content_hash=_hash(303),
        ),
    }
    return selection, tables


def _join_rule() -> ExactJoinRule:
    return ExactJoinRule(
        rule_id=str(uuid4()),
        output_dataset_name="customer_enriched",
        left_dataset_id="customers",
        right_dataset_id="details",
        keys=(JoinKey("customer_key", "detail_customer_key"),),
        output_columns=(
            StructuralOutputColumn("customer_key", "Customer", "string"),
            StructuralOutputColumn("customer_name", "Name", "string"),
            StructuralOutputColumn("country", "Country", "string"),
        ),
        projections=(
            StructuralProjection("customer_key", "customers", "customer_key"),
            StructuralProjection("customer_name", "customers", "customer_name"),
            StructuralProjection("country", "details", "country"),
        ),
        kind=JoinKind.LEFT,
    )


class CoverageScopeTests(unittest.TestCase):
    def test_complete_scope_round_trips_and_hash_excludes_lifecycle_approval(self) -> None:
        scope = _scope()
        restored = CoverageScopeRevision.from_dict(scope.to_portable_dict())

        self.assertEqual(restored.content_hash, scope.content_hash)
        self.assertEqual(
            scope.declaration(CoverageFamily.TC_14).applicability,
            CoverageApplicability.APPLICABLE,
        )
        self.assertIn(Capability.COVERAGE_SCOPE, LOCAL_ACTOR.capabilities)

    def test_missing_family_and_applicable_family_without_dataset_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "at least one dataset"):
            CoverageDeclaration(
                family=CoverageFamily.TC_14,
                applicability=CoverageApplicability.APPLICABLE,
                rationale="Required",
            )

        declarations = _scope().declarations[:-1]
        with self.assertRaisesRegex(ValueError, "incomplete"):
            CoverageScopeRevision(
                scope_id=str(uuid4()),
                project_id="project-1",
                version=1,
                parent_version=None,
                source_selection_hash=HASH_A,
                declarations=declarations,
                approved_by=LOCAL_ACTOR.identity,
                approved_at=NOW,
            )


class ReferenceDataTests(unittest.TestCase):
    def test_exact_reference_data_and_bundle_round_trip(self) -> None:
        dataset = _reference_dataset()
        bundle = ReferenceBundle(project_id="project-1", datasets=(dataset,))
        restored = ReferenceBundle.from_dict(bundle.to_portable_dict())

        self.assertEqual(restored.content_hash, bundle.content_hash)
        self.assertEqual(dataset.lookup(("BE",)).values["country_code"], "BE")

    def test_duplicate_reference_key_is_rejected(self) -> None:
        entry = ReferenceEntry(key=("BE",), values={"country_code": "BE"})
        with self.assertRaisesRegex(ValueError, "unique"):
            ReferenceDataSet(
                reference_id=str(uuid4()),
                version=1,
                name="Duplicate list",
                key_fields=("source_country",),
                value_kinds={"country_code": ReferenceValueKind.BUSINESS_KEY},
                entries=(entry, entry),
                owner="Functional owner",
                classification="internal",
                effective_label="Test",
            )

    def test_exact_reference_lookup_runs_inside_canonical_staging(self) -> None:
        reference = _reference_dataset()
        bundle = ReferenceBundle(project_id="project-1", datasets=(reference,))
        selection = SourceSelection(
            selection_id=str(uuid4()),
            version=1,
            project_id="project-1",
            created_at=NOW,
            created_by="Data Manager",
            datasets=(
                SourceDataset(
                    dataset_id="partners",
                    name="partners",
                    file_id="file-partners",
                    table_key="partners.csv",
                    source_sha256=HASH_B,
                    catalog_hash=_hash(401),
                    encoding="utf-8",
                    delimiter=",",
                    header_row=1,
                    row_count=2,
                    columns=(
                        SourceDatasetColumn(1, "Key", "partner_key", "string"),
                        SourceDatasetColumn(2, "Country", "country", "string"),
                    ),
                ),
            ),
            content_hash=HASH_A,
        )
        definition = MappingDefinition(
            mapping_id="mapping:reference",
            source_selection_hash=selection.content_hash,
            schema_hash=HASH_D,
            datasets=(
                DatasetMapping(
                    dataset_id="partners",
                    target_model="res.partner",
                    source_identity_column_keys=("partner_key",),
                    target_identity=(
                        IdentityComponentMapping(
                            source_column_keys=("partner_key",),
                            target_fields=("ref",),
                        ),
                    ),
                    fields=(
                        ScalarFieldMapping(
                            target_field="country_code",
                            source_column_key="country",
                            reference_lookup=ReferenceLookupMapping(
                                reference_id=reference.reference_id,
                                reference_content_hash=reference.content_hash,
                                key_source_column_keys=("country",),
                                value_field="country_code",
                            ),
                        ),
                    ),
                ),
            ),
        )
        staged = evaluate_browser_mapping(
            project_id="project-1",
            definition=definition,
            physical_selection=selection,
            effective_selection=selection,
            plan=None,
            loaded_tables={
                "partners": SourceTable(
                    dataset="partners",
                    path=Path("partners.csv"),
                    headers=("Key", "Country"),
                    rows=(
                        SourceRow(1, {"Key": "P1", "Country": "BE"}),
                        SourceRow(2, {"Key": "P2", "Country": "XX"}),
                    ),
                    content_hash=HASH_B,
                )
            },
            reference_bundle=bundle,
        )

        self.assertEqual(
            staged.canonical_run.rows[0].proposed_values["country_code"],
            "BE",
        )
        self.assertEqual(
            staged.canonical_run.rows[1].disposition,
            StagingDisposition.BLOCKED,
        )
        self.assertEqual(
            staged.canonical_run.rows[1].issues[0].code,
            "REFERENCE_KEY_UNKNOWN",
        )

    def test_odoo_selection_reference_requires_captured_technical_key(self) -> None:
        reference = _reference_dataset()
        bundle = ReferenceBundle(project_id="project-1", datasets=(reference,))
        definition = MappingDefinition(
            mapping_id="mapping:selection-reference",
            source_selection_hash=HASH_A,
            schema_hash=HASH_D,
            datasets=(
                DatasetMapping(
                    dataset_id="partners",
                    target_model="res.partner",
                    source_identity_column_keys=("partner_key",),
                    target_identity=(
                        IdentityComponentMapping(
                            source_column_keys=("partner_key",),
                            target_fields=("ref",),
                        ),
                    ),
                    fields=(
                        ScalarFieldMapping(
                            target_field="country_code",
                            source_column_key="country",
                            reference_lookup=ReferenceLookupMapping(
                                reference_id=reference.reference_id,
                                reference_content_hash=reference.content_hash,
                                key_source_column_keys=("country",),
                                value_field="country_code",
                            ),
                        ),
                    ),
                ),
            ),
        )
        catalog = OdooSchemaCatalog(
            project_id="project-1",
            target_hash=HASH_A,
            captured_at=NOW,
            captured_by="Data Manager",
            connection_mode="LOCAL",
            database="odoo19_local",
            odoo_version="19.0",
            models=(
                SchemaModel(
                    name="res.partner",
                    label="Contact",
                    fields=(
                        SchemaField(
                            name="country_code",
                            label="Country code",
                            type="selection",
                            required=False,
                            readonly=False,
                            relation=None,
                            relation_field=None,
                            selection=(("BE", "Belgium"),),
                        ),
                    ),
                ),
            ),
            content_hash=HASH_D,
            origin=SchemaOrigin.LIVE_API,
        )

        with self.assertRaisesRegex(ValueError, "unknown Odoo selection key"):
            validate_odoo_selection_reference_outputs(bundle, definition, catalog)


class StructuralPreparationTests(unittest.TestCase):
    def test_exact_join_preserves_cardinality_and_both_source_lineages(self) -> None:
        selection, tables = _structural_selection()
        rule = _join_rule()

        mapped = structural_mapping_selection(selection, (rule,))
        execution = execute_structural_rules(
            selection=selection,
            loaded_tables=tables,
            rules=(rule,),
        )
        output = execution.outputs[0]

        self.assertEqual(mapped.datasets[-1].dataset_id, structural_dataset_id(rule))
        self.assertEqual(len(output.table.rows), 2)
        self.assertEqual(output.table.rows[0].values["Country"], "BE")
        self.assertEqual(
            output.lineage[1],
            {"customers": (1,), "details": (1,)},
        )
        self.assertEqual(output.reconciliation.matched_left_rows, 2)
        self.assertEqual(output.reconciliation.unmatched_left_rows, 0)
        self.assertEqual(output.reconciliation.unmatched_right_rows, 0)

    def test_join_duplicate_right_keys_block_before_output(self) -> None:
        selection, tables = _structural_selection()
        duplicate = SourceTable(
            dataset="details",
            path=Path("details.csv"),
            headers=tables["details"].headers,
            rows=(
                SourceRow(1, {"Customer": "C1", "Country": "BE"}),
                SourceRow(2, {"Customer": "C1", "Country": "LU"}),
            ),
            content_hash=_hash(303),
        )
        tables["details"] = duplicate

        with self.assertRaisesRegex(StructuralError, "not unique"):
            execute_structural_rules(
                selection=selection,
                loaded_tables=tables,
                rules=(_join_rule(),),
            )

    def test_inner_join_refuses_unmatched_rows_instead_of_dropping_them(self) -> None:
        selection, tables = _structural_selection()
        tables["details"] = SourceTable(
            dataset="details",
            path=Path("details.csv"),
            headers=tables["details"].headers,
            rows=(SourceRow(1, {"Customer": "C1", "Country": "BE"}),),
            content_hash=_hash(303),
        )
        rule = ExactJoinRule.from_dict(
            {**_join_rule().to_dict(), "kind": "INNER"}
        )

        with self.assertRaisesRegex(StructuralError, "unmatched left"):
            execute_structural_rules(
                selection=selection,
                loaded_tables=tables,
                rules=(rule,),
            )

    def test_union_all_reconciles_every_branch_row(self) -> None:
        selection, tables = _structural_selection()
        rule = UnionAllRule(
            rule_id=str(uuid4()),
            output_dataset_name="all_customer_keys",
            output_columns=(
                StructuralOutputColumn("customer_key", "Customer", "string"),
            ),
            branches=(
                UnionBranch(
                    "customers",
                    (
                        StructuralProjection(
                            "customer_key",
                            "customers",
                            "customer_key",
                        ),
                    ),
                ),
                UnionBranch(
                    "details",
                    (
                        StructuralProjection(
                            "customer_key",
                            "details",
                            "detail_customer_key",
                        ),
                    ),
                ),
            ),
        )

        output = execute_structural_rules(
            selection=selection,
            loaded_tables=tables,
            rules=(rule,),
        ).outputs[0]

        self.assertEqual(output.reconciliation.input_rows, 4)
        self.assertEqual(output.reconciliation.output_rows, 4)
        self.assertEqual(len(output.lineage), 4)

    def test_grouping_uses_decimal_sum_count_and_complete_fan_in(self) -> None:
        selection = SourceSelection(
            selection_id=str(uuid4()),
            version=1,
            project_id="project-1",
            created_at=NOW,
            created_by=LOCAL_ACTOR.identity.display_name,
            datasets=(
                SourceDataset(
                    dataset_id="lines",
                    name="lines",
                    file_id="file-lines",
                    table_key="lines.csv",
                    source_sha256=_hash(401),
                    catalog_hash=_hash(402),
                    encoding="utf-8",
                    delimiter=",",
                    header_row=1,
                    row_count=3,
                    columns=(
                        SourceDatasetColumn(1, "Order", "order_key", "string"),
                        SourceDatasetColumn(2, "Amount", "amount", "decimal"),
                    ),
                ),
            ),
            content_hash=_hash(403),
        )
        tables = {
            "lines": SourceTable(
                dataset="lines",
                path=Path("lines.csv"),
                headers=("Order", "Amount"),
                rows=(
                    SourceRow(1, {"Order": "O1", "Amount": "1.20"}),
                    SourceRow(2, {"Order": "O1", "Amount": "2.30"}),
                    SourceRow(3, {"Order": "O2", "Amount": "4.00"}),
                ),
                content_hash=_hash(401),
            )
        }
        rule = GroupAggregateRule(
            rule_id=str(uuid4()),
            output_dataset_name="order_totals",
            source_dataset_id="lines",
            output_columns=(
                StructuralOutputColumn("order_key", "Order", "string"),
                StructuralOutputColumn("line_count", "Line count", "integer"),
                StructuralOutputColumn("amount_total", "Amount total", "decimal"),
            ),
            group_keys=(GroupKey("order_key", "order_key"),),
            aggregates=(
                AggregateSpec("line_count", AggregateOperation.COUNT),
                AggregateSpec("amount_total", AggregateOperation.SUM, "amount", "EUR"),
            ),
        )

        output = execute_structural_rules(
            selection=selection,
            loaded_tables=tables,
            rules=(rule,),
        ).outputs[0]

        self.assertEqual(len(output.table.rows), 2)
        self.assertEqual(output.table.rows[0].values["Amount total"], Decimal("3.50"))
        self.assertEqual(output.table.rows[0].values["Line count"], 2)
        self.assertEqual(output.lineage[1], {"lines": (1, 2)})
        self.assertEqual(output.reconciliation.input_rows, 3)
        self.assertEqual(output.reconciliation.output_rows, 2)

    def test_structural_join_is_part_of_the_existing_plan_and_canonical_lineage(self) -> None:
        physical, tables = _structural_selection()
        rule = _join_rule()
        plan = DerivedEntityPlan(
            plan_id=str(uuid4()),
            version=1,
            project_id=physical.project_id,
            source_selection_hash=physical.content_hash,
            rules=(rule,),
            updated_at=NOW,
            updated_by=LOCAL_ACTOR.identity.display_name,
        )
        effective = mapping_source_selection(physical, plan)
        by_id = {item.dataset_id: item for item in effective.datasets}
        output = by_id[structural_dataset_id(rule)]

        def mapping(dataset_id: str, identity_key: str, model: str, fields=()):
            return DatasetMapping(
                dataset_id=dataset_id,
                target_model=model,
                source_identity_column_keys=(identity_key,),
                target_identity=(
                    IdentityComponentMapping(
                        source_column_keys=(identity_key,),
                        target_fields=("x_business_key",),
                    ),
                ),
                fields=fields,
            )

        definition = MappingDefinition(
            mapping_id=str(uuid4()),
            source_selection_hash=effective.content_hash,
            schema_hash=HASH_D,
            datasets=(
                mapping("customers", "customer_key", "x.customer"),
                mapping("details", "detail_customer_key", "x.detail"),
                mapping(
                    output.dataset_id,
                    "customer_key",
                    "res.partner",
                    fields=(
                        ScalarFieldMapping(
                            target_field="name",
                            source_column_key="customer_name",
                        ),
                        ScalarFieldMapping(
                            target_field="country_code",
                            source_column_key="country",
                        ),
                    ),
                ),
            ),
        )

        staged = evaluate_browser_mapping(
            project_id=physical.project_id,
            definition=definition,
            physical_selection=physical,
            effective_selection=effective,
            plan=plan,
            loaded_tables=tables,
        )
        structural_rows = tuple(
            item
            for item in staged.canonical_run.rows
            if item.dataset == "customer_enriched"
        )
        restored = CanonicalStagingRun.from_dict(
            staged.canonical_run.to_portable_dict()
        )

        self.assertEqual(len(structural_rows), 2)
        self.assertEqual(
            structural_rows[0].lineage.physical_sources,
            {"customers": (1,), "details": (1,)},
        )
        self.assertEqual(restored.content_hash, staged.canonical_run.content_hash)
        self.assertEqual(
            next(
                item.role
                for item in staged.canonical_run.datasets
                if item.dataset == "customer_enriched"
            ),
            StagingDatasetRole.JOIN,
        )


class AdvancedQualityTests(unittest.TestCase):
    def test_approved_code_and_metric_rules_are_deterministic_and_non_mutating(self) -> None:
        project = MigrationProject(
            project_id="project-1",
            name="Advanced checks",
            source_system="CSV",
            data_manager="Data Manager",
            functional_owner="Functional Owner",
            business_unit="Operations",
            odoo_connection_mode=OdooConnectionMode.LOCAL,
            odoo_base_url="http://127.0.0.1:8069",
            odoo_database="odoo19_local",
            intended_models=("res.partner",),
            status=ProjectStatus.REGISTERED,
            registered_at=NOW,
        )
        rows = (
            _row(1, name="Valid", country="BE"),
            _row(2, name="Unknown", country="XX"),
        )
        base = default_quality_ruleset(
            project_id=project.project_id,
            mapping_hash=HASH_C,
            schema_hash=HASH_D,
            datasets=("partners",),
        )
        reference = _reference_dataset()
        bundle = ReferenceBundle(project_id=project.project_id, datasets=(reference,))
        approved_code = QualityRule(
            rule_id=_hash(501),
            dataset="partners",
            family=QualityRuleFamily.APPROVED_CODE_LIST,
            name="Approved countries",
            explanation="Require a country from the approved project list.",
            input_fields=("country",),
            parameters={
                "reference_id": reference.reference_id,
                "reference_content_hash": reference.content_hash,
            },
            outcome=QualityOutcomePolicy.QUARANTINE,
            owner_role=QualityOwnerRole.FUNCTIONAL_OWNER,
            source=QualityRuleSource.SCOPE_APPROVED,
        )
        count_boundary = QualityRule(
            rule_id=_hash(502),
            dataset="partners",
            family=QualityRuleFamily.METRIC_BOUNDARY,
            name="Expected partner volume",
            explanation="Warn when the prepared population is below the approved floor.",
            input_fields=(),
            parameters={"metric": "count", "minimum": "3"},
            outcome=QualityOutcomePolicy.WARNING,
            owner_role=QualityOwnerRole.DATA_MANAGER,
            source=QualityRuleSource.SCOPE_APPROVED,
        )
        ruleset = QualityRuleSet(
            ruleset_id=base.ruleset_id,
            project_id=base.project_id,
            version=1,
            parent_version=None,
            mapping_hash=base.mapping_hash,
            schema_hash=base.schema_hash,
            rules=tuple(sorted((*base.rules, approved_code, count_boundary), key=lambda item: item.rule_id)),
            coverage_scope_hash=HASH_A,
            reference_bundle_hash=bundle.content_hash,
        )

        first = evaluate_quality(
            project=project,
            staging=_staging_run(project.project_id, rows),
            physical_rows={"dataset:partners": (1, 2)},
            ruleset=ruleset,
            reference_bundle=bundle,
        )
        second = evaluate_quality(
            project=project,
            staging=_staging_run(project.project_id, rows),
            physical_rows={"dataset:partners": (1, 2)},
            ruleset=ruleset,
            reference_bundle=bundle,
        )

        self.assertEqual(first.content_hash, second.content_hash)
        self.assertEqual(first.quarantined_count, 1)
        self.assertEqual(first.review_count, 1)
        self.assertEqual(rows[1].proposed_values["country"], "XX")


class ResolutionContractTests(unittest.TestCase):
    def test_policy_and_evaluation_round_trip(self) -> None:
        policy = _policy()
        restored_policy = ResolutionPolicy.from_dict(policy.to_portable_dict())
        rows = (
            _row(1, name="Acme SA", street="1 Main Street"),
            _row(2, name="ACME S.A.", street="Main Street 1"),
            _row(3, name="Different Company", street="Other Road"),
        )

        first = evaluate_resolution_candidates(
            policy=restored_policy,
            staging_content_hash=HASH_A,
            rows=rows,
        )
        second = evaluate_resolution_candidates(
            policy=restored_policy,
            staging_content_hash=HASH_A,
            rows=reversed(rows),
        )
        restored = ResolutionEvaluation.from_dict(first.to_portable_dict())

        self.assertEqual(first.content_hash, second.content_hash)
        self.assertEqual(restored.content_hash, first.content_hash)
        self.assertEqual(first.compared_pair_count, 3)
        self.assertFalse(first.blocked)
        self.assertGreaterEqual(len(first.candidates), 1)
        self.assertTrue(all(item.left_row_id < item.right_row_id for item in first.candidates))

    def test_oversized_blocks_stop_without_all_pairs_comparison(self) -> None:
        policy = _policy(max_block_size=2)
        evaluation = evaluate_resolution_candidates(
            policy=policy,
            staging_content_hash=HASH_A,
            rows=tuple(_row(number, name=f"Acme {number}") for number in range(1, 4)),
        )

        self.assertTrue(evaluation.blocked)
        self.assertEqual(evaluation.compared_pair_count, 0)
        self.assertEqual(evaluation.candidates, ())
        self.assertEqual(evaluation.findings[0].code, "FUZZY_BLOCK_TOO_LARGE")

    def test_candidate_degree_is_bounded_deterministically(self) -> None:
        policy = _policy(max_candidates=2)
        rows = tuple(_row(number, name="Acme") for number in range(1, 8))
        evaluation = evaluate_resolution_candidates(
            policy=policy,
            staging_content_hash=HASH_A,
            rows=rows,
        )
        degree: dict[str, int] = {}
        for candidate in evaluation.candidates:
            degree[candidate.left_row_id] = degree.get(candidate.left_row_id, 0) + 1
            degree[candidate.right_row_id] = degree.get(candidate.right_row_id, 0) + 1

        self.assertTrue(degree)
        self.assertLessEqual(max(degree.values()), 2)
        self.assertEqual(evaluation.compared_pair_count, 21)

    def test_blank_blocking_values_create_a_blocking_finding(self) -> None:
        evaluation = evaluate_resolution_candidates(
            policy=_policy(),
            staging_content_hash=HASH_A,
            rows=(_row(1, name="Acme", country=""),),
        )

        self.assertTrue(evaluation.blocked)
        self.assertEqual(evaluation.findings[0].code, "FUZZY_BLOCKING_VALUE_MISSING")

    def test_pair_and_field_decisions_are_strict_and_portable(self) -> None:
        rows = tuple(sorted((_hash(1), _hash(2))))
        group_id = _hash(100)
        decision = ResolutionDecision(
            decision_id=str(uuid4()),
            evaluation_hash=HASH_A,
            group_id=group_id,
            kind=ResolutionDecisionKind.SAME_RECORD,
            row_ids=rows,
            reason="The functional owner confirmed both source rows are one partner.",
            actor=LOCAL_ACTOR.identity,
            decided_at=NOW,
            lifecycle_version=1,
        )
        restored = ResolutionDecision.from_dict(decision.to_portable_dict())

        self.assertEqual(restored.content_hash, decision.content_hash)
        self.assertIn(Capability.RESOLUTION_DECIDE, LOCAL_ACTOR.capabilities)
        self.assertIn(Capability.RESOLUTION_APPROVE, LOCAL_ACTOR.capabilities)

        with self.assertRaisesRegex(ValueError, "not in the decision group"):
            ResolutionDecision(
                decision_id=str(uuid4()),
                evaluation_hash=HASH_A,
                group_id=group_id,
                kind=ResolutionDecisionKind.SELECT_SOURCE,
                row_ids=rows,
                field="name",
                selected_row_id=_hash(3),
                reason="Use the third row.",
                actor=LOCAL_ACTOR.identity,
                decided_at=NOW,
                lifecycle_version=2,
            )

    def test_pass_through_rows_have_complete_field_provenance(self) -> None:
        row = _row(1, name="Acme")
        effective = pass_through_effective_row(row)
        restored = EffectiveRow.from_dict(effective.to_portable_dict())

        self.assertEqual(restored.row_id, row.row_id)
        self.assertEqual(
            {item.kind for item in restored.field_provenance},
            {FieldProvenanceKind.COPIED},
        )
        self.assertEqual(
            {item.field for item in restored.field_provenance},
            set(row.proposed_values),
        )

    def test_reviewed_merge_creates_one_survivor_with_field_provenance(self) -> None:
        policy = _policy()
        rows = (
            _row(1, name="Acme SA", street="1 Main Street"),
            _row(2, name="ACME S.A.", street="Main Street 1"),
        )
        evaluation = evaluate_resolution_candidates(
            policy=policy,
            staging_content_hash=HASH_A,
            rows=rows,
        )
        self.assertEqual(len(evaluation.candidates), 1)
        candidate = evaluation.candidates[0]
        group_rows = tuple(sorted((rows[0].row_id, rows[1].row_id)))
        group_id = content_hash(
            {"policy_hash": policy.content_hash, "row_ids": list(group_rows)}
        )
        decisions = (
            ResolutionDecision(
                decision_id=str(uuid4()),
                evaluation_hash=evaluation.content_hash,
                group_id=candidate.candidate_id,
                kind=ResolutionDecisionKind.SAME_RECORD,
                row_ids=group_rows,
                reason="The owner confirmed both rows are the same partner.",
                actor=LOCAL_ACTOR.identity,
                decided_at=NOW,
                lifecycle_version=1,
            ),
            ResolutionDecision(
                decision_id=str(uuid4()),
                evaluation_hash=evaluation.content_hash,
                group_id=group_id,
                kind=ResolutionDecisionKind.SELECT_SOURCE,
                row_ids=group_rows,
                field="name",
                selected_row_id=rows[0].row_id,
                reason="Use the registered legal name.",
                actor=LOCAL_ACTOR.identity,
                decided_at=NOW,
                lifecycle_version=2,
            ),
            ResolutionDecision(
                decision_id=str(uuid4()),
                evaluation_hash=evaluation.content_hash,
                group_id=group_id,
                kind=ResolutionDecisionKind.REVIEWER_CORRECTION,
                row_ids=group_rows,
                field="street",
                replacement_value="Main Street 1",
                reason="Use the address confirmed by the functional owner.",
                actor=LOCAL_ACTOR.identity,
                decided_at=NOW,
                lifecycle_version=3,
            ),
            ResolutionDecision(
                decision_id=str(uuid4()),
                evaluation_hash=evaluation.content_hash,
                group_id=group_id,
                kind=ResolutionDecisionKind.SELECT_SOURCE,
                row_ids=group_rows,
                field="__identity__",
                selected_row_id=rows[0].row_id,
                reason="Keep the first governed business identity.",
                actor=LOCAL_ACTOR.identity,
                decided_at=NOW,
                lifecycle_version=4,
            ),
        )

        effective = build_effective_dataset(
            policy=policy,
            evaluation=evaluation,
            rows=rows,
            decisions=decisions,
        )
        restored = EffectiveDataset.from_dict(effective.to_portable_dict())

        self.assertEqual(len(restored.rows), 1)
        self.assertEqual(restored.reconciliation.merged_input_rows, 2)
        self.assertEqual(restored.reconciliation.survivor_rows, 1)
        self.assertEqual(restored.reconciliation.corrected_effective_rows, 1)
        self.assertEqual(
            restored.rows[0].canonical_row.proposed_values["name"],
            "Acme SA",
        )
        self.assertEqual(
            {item.kind for item in restored.rows[0].field_provenance},
            {
                FieldProvenanceKind.UNANIMOUS,
                FieldProvenanceKind.SELECTED_SOURCE,
                FieldProvenanceKind.REVIEWER_CORRECTION,
            },
        )

    def test_keep_separate_requires_no_survivor_field_decisions(self) -> None:
        policy = _policy()
        rows = (
            _row(1, name="Acme SA", street="1 Main Street"),
            _row(2, name="ACME S.A.", street="Main Street 1"),
        )
        evaluation = evaluate_resolution_candidates(
            policy=policy,
            staging_content_hash=HASH_A,
            rows=rows,
        )
        candidate = evaluation.candidates[0]
        decision = ResolutionDecision(
            decision_id=str(uuid4()),
            evaluation_hash=evaluation.content_hash,
            group_id=candidate.candidate_id,
            kind=ResolutionDecisionKind.KEEP_SEPARATE,
            row_ids=tuple(sorted((rows[0].row_id, rows[1].row_id))),
            reason="The similar names belong to two legal entities.",
            actor=LOCAL_ACTOR.identity,
            decided_at=NOW,
            lifecycle_version=1,
        )

        effective = build_effective_dataset(
            policy=policy,
            evaluation=evaluation,
            rows=rows,
            decisions=(decision,),
        )

        self.assertEqual(len(effective.rows), 2)
        self.assertEqual(effective.reconciliation.kept_distinct_rows, 2)
        self.assertEqual(effective.reconciliation.survivor_rows, 0)

    def test_effective_dataset_refuses_incomplete_candidate_review(self) -> None:
        policy = _policy()
        rows = (
            _row(1, name="Acme SA", street="1 Main Street"),
            _row(2, name="ACME S.A.", street="Main Street 1"),
        )
        evaluation = evaluate_resolution_candidates(
            policy=policy,
            staging_content_hash=HASH_A,
            rows=rows,
        )

        with self.assertRaisesRegex(ValueError, "Every fuzzy candidate"):
            build_effective_dataset(
                policy=policy,
                evaluation=evaluation,
                rows=rows,
                decisions=(),
            )

    def test_reviewer_correction_must_preserve_the_existing_scalar_type(self) -> None:
        policy = _policy()
        rows = (
            _row(1, name="Acme SA", street="1 Main Street"),
            _row(2, name="ACME S.A.", street="Main Street 1"),
        )
        evaluation = evaluate_resolution_candidates(
            policy=policy,
            staging_content_hash=HASH_A,
            rows=rows,
        )
        candidate = evaluation.candidates[0]
        group_rows = tuple(sorted((rows[0].row_id, rows[1].row_id)))
        group_id = content_hash(
            {"policy_hash": policy.content_hash, "row_ids": list(group_rows)}
        )
        decisions = (
            ResolutionDecision(
                decision_id=str(uuid4()),
                evaluation_hash=evaluation.content_hash,
                group_id=candidate.candidate_id,
                kind=ResolutionDecisionKind.SAME_RECORD,
                row_ids=group_rows,
                reason="One governed partner.",
                actor=LOCAL_ACTOR.identity,
                decided_at=NOW,
                lifecycle_version=1,
            ),
            ResolutionDecision(
                decision_id=str(uuid4()),
                evaluation_hash=evaluation.content_hash,
                group_id=group_id,
                kind=ResolutionDecisionKind.REVIEWER_CORRECTION,
                row_ids=group_rows,
                field="name",
                replacement_value=42,
                reason="Invalid typed correction fixture.",
                actor=LOCAL_ACTOR.identity,
                decided_at=NOW,
                lifecycle_version=2,
            ),
            ResolutionDecision(
                decision_id=str(uuid4()),
                evaluation_hash=evaluation.content_hash,
                group_id=group_id,
                kind=ResolutionDecisionKind.SELECT_SOURCE,
                row_ids=group_rows,
                field="street",
                selected_row_id=rows[0].row_id,
                reason="Keep the first source address.",
                actor=LOCAL_ACTOR.identity,
                decided_at=NOW,
                lifecycle_version=3,
            ),
            ResolutionDecision(
                decision_id=str(uuid4()),
                evaluation_hash=evaluation.content_hash,
                group_id=group_id,
                kind=ResolutionDecisionKind.SELECT_SOURCE,
                row_ids=group_rows,
                field="__identity__",
                selected_row_id=rows[0].row_id,
                reason="Keep the first identity.",
                actor=LOCAL_ACTOR.identity,
                decided_at=NOW,
                lifecycle_version=4,
            ),
        )

        with self.assertRaisesRegex(ValueError, "preserve the field value type"):
            build_effective_dataset(
                policy=policy,
                evaluation=evaluation,
                rows=rows,
                decisions=decisions,
            )


class AdvancedCoveragePersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        (ROOT / ".tmp").mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=ROOT / ".tmp")
        self.database = DuckDbDatabase(self.temporary.name)
        self.projects = ProjectRepository(self.database)
        self.staging = StagingRepository(self.database)
        self.repository = AdvancedCoverageRepository(self.database)
        self.quality = QualityRepository(self.database, self.projects)
        self.project = MigrationProject(
            project_id=str(uuid4()),
            name="Advanced partner preparation",
            source_system="CSV",
            data_manager="Data Manager",
            functional_owner="Functional Owner",
            business_unit="Operations",
            odoo_connection_mode=OdooConnectionMode.LOCAL,
            odoo_base_url="http://127.0.0.1:8069",
            odoo_database="odoo19_local",
            intended_models=("res.partner",),
            status=ProjectStatus.REGISTERED,
            registered_at=NOW,
        )
        self.projects.create(self.project, actor=LOCAL_ACTOR)
        selection = SourceSelection(
            selection_id=str(uuid4()),
            version=1,
            project_id=self.project.project_id,
            created_at=NOW,
            created_by=LOCAL_ACTOR.identity.display_name,
            datasets=(
                SourceDataset(
                    dataset_id="dataset:partners",
                    name="partners",
                    file_id=str(uuid4()),
                    table_key="csv",
                    source_sha256=HASH_B,
                    catalog_hash=_hash(200),
                    encoding="utf-8",
                    delimiter=",",
                    header_row=1,
                    row_count=2,
                    columns=(
                        SourceDatasetColumn(1, "Country", "country", "string"),
                        SourceDatasetColumn(2, "Name", "name", "string"),
                        SourceDatasetColumn(3, "Street", "street", "string"),
                    ),
                ),
            ),
            content_hash=HASH_A,
        )
        database_path = (
            self.repository.project_directory(self.project.project_id)
            / "project.duckdb"
        )
        with self.repository._connect(database_path) as connection:
            connection.execute(
                "INSERT INTO source_selection VALUES (1, ?)",
                [selection.to_json()],
            )
            connection.execute(
                """
                INSERT INTO mapping_revision
                VALUES ('mapping:partners', 1, NULL, ?, ?, ?, ?, '{}')
                """,
                [HASH_C, HASH_A, HASH_D, NOW.isoformat()],
            )
            connection.execute(
                "INSERT INTO mapping_current VALUES (1, 'mapping:partners', 1)"
            )
            connection.execute(
                """
                INSERT INTO mapping_submission
                VALUES (?, 'mapping:partners', 1, ?, ?, ?, '{}')
                """,
                [str(uuid4()), HASH_C, _hash(201), NOW.isoformat()],
            )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_scope_reference_policy_decisions_and_effective_rows_survive_restart(self) -> None:
        scope = _scope(project_id=self.project.project_id)
        bundle = ReferenceBundle(
            project_id=self.project.project_id,
            datasets=(_reference_dataset(),),
        )
        policy = _policy(
            project_id=self.project.project_id,
            coverage_scope_hash=scope.content_hash,
            mapping_hash=HASH_C,
            schema_hash=HASH_D,
            reference_bundle_hash=bundle.content_hash,
        )
        self.repository.save_coverage_scope(
            self.project.project_id,
            scope,
            expected_parent_version=None,
            actor=LOCAL_ACTOR,
        )
        self.repository.save_reference_bundle(
            self.project.project_id,
            bundle,
            actor=LOCAL_ACTOR,
        )
        self.repository.save_resolution_policy(
            self.project.project_id,
            policy,
            expected_parent_version=None,
            actor=LOCAL_ACTOR,
        )
        self.assertEqual(
            self.repository.get_coverage_scope(self.project.project_id).content_hash,
            scope.content_hash,
        )
        self.assertEqual(
            self.repository.get_reference_bundle(self.project.project_id).content_hash,
            bundle.content_hash,
        )
        self.assertEqual(
            self.repository.get_resolution_policy(self.project.project_id).content_hash,
            policy.content_hash,
        )

        rows = (
            _row(1, name="Acme SA", street="1 Main Street"),
            _row(2, name="ACME S.A.", street="Main Street 1"),
        )
        staging_summary = self.staging.publish_canonical_staging(
            self.project.project_id,
            _staging_run(self.project.project_id, rows),
            mapping_version=1,
            actor=LOCAL_ACTOR,
        )
        evaluation = evaluate_resolution_candidates(
            policy=policy,
            staging_content_hash=staging_summary.content_hash,
            rows=rows,
        )
        resolution = self.repository.publish_resolution_evaluation(
            self.project.project_id,
            evaluation,
            staging_run_id=staging_summary.run_id,
            actor=LOCAL_ACTOR,
        )
        candidate = evaluation.candidates[0]
        decision = ResolutionDecision(
            decision_id=str(uuid4()),
            evaluation_hash=evaluation.content_hash,
            group_id=candidate.candidate_id,
            kind=ResolutionDecisionKind.KEEP_SEPARATE,
            row_ids=(candidate.left_row_id, candidate.right_row_id),
            reason="The functional owner confirmed two separate legal entities.",
            actor=LOCAL_ACTOR.identity,
            decided_at=NOW,
            lifecycle_version=1,
        )
        resolution = self.repository.append_resolution_decision(
            self.project.project_id,
            resolution.run_id,
            decision,
            expected_lifecycle_version=0,
            actor=LOCAL_ACTOR,
        )
        effective = build_effective_dataset(
            policy=policy,
            evaluation=evaluation,
            rows=rows,
            decisions=(decision,),
        )
        frozen = self.repository.freeze_effective_dataset(
            self.project.project_id,
            resolution.run_id,
            effective,
            expected_lifecycle_version=1,
            actor=LOCAL_ACTOR,
        )

        restarted = AdvancedCoverageRepository(
            DuckDbDatabase(self.temporary.name)
        )
        restored = restarted.get_current_effective_dataset(self.project.project_id)
        self.assertIsNotNone(restored)
        self.assertEqual(restored.content_hash, effective.content_hash)
        self.assertEqual(frozen.status, "FROZEN")
        self.assertEqual(frozen.lifecycle_version, 2)
        self.assertEqual(frozen.decision_count, 1)
        database_path = (
            restarted.project_directory(self.project.project_id) / "project.duckdb"
        )
        with restarted._connect(database_path) as connection:
            compact_rows = connection.execute(
                """
                SELECT COUNT(*) FROM effective_row
                 WHERE effective_json = 'null' AND canonical_row_id IS NOT NULL
                """
            ).fetchone()
        self.assertEqual(int(compact_rows[0]), 2)

        ruleset = default_quality_ruleset(
            project_id=self.project.project_id,
            mapping_hash=HASH_C,
            schema_hash=HASH_D,
            datasets=("partners",),
        )
        self.quality.publish_quality_ruleset(
            self.project.project_id,
            ruleset,
            actor=LOCAL_ACTOR,
        )
        quality_run = evaluate_quality(
            project=self.project,
            staging=_staging_run(self.project.project_id, rows),
            physical_rows={"dataset:partners": (1, 2)},
            ruleset=ruleset,
            published_staging_content_hash=staging_summary.content_hash,
            effective=effective,
        )
        quality_summary = self.quality.publish_quality_run(
            self.project.project_id,
            quality_run,
            staging_run_id=staging_summary.run_id,
            effective_dataset_run_id=frozen.run_id,
            actor=LOCAL_ACTOR,
        )
        self.assertEqual(
            quality_summary.effective_dataset_hash,
            effective.content_hash,
        )
        restored_quality = self.quality.get_quality_run(
            self.project.project_id,
            quality_summary.run_id,
        )
        self.assertEqual(restored_quality.effective_dataset_hash, effective.content_hash)

    def test_pair_decision_cannot_be_replaced_by_the_opposite_choice(self) -> None:
        scope = _scope(project_id=self.project.project_id)
        bundle = ReferenceBundle(project_id=self.project.project_id, datasets=())
        policy = _policy(
            project_id=self.project.project_id,
            coverage_scope_hash=scope.content_hash,
            mapping_hash=HASH_C,
            schema_hash=HASH_D,
            reference_bundle_hash=bundle.content_hash,
        )
        self.repository.save_coverage_scope(
            self.project.project_id,
            scope,
            expected_parent_version=None,
            actor=LOCAL_ACTOR,
        )
        self.repository.save_reference_bundle(
            self.project.project_id,
            bundle,
            actor=LOCAL_ACTOR,
        )
        self.repository.save_resolution_policy(
            self.project.project_id,
            policy,
            expected_parent_version=None,
            actor=LOCAL_ACTOR,
        )
        rows = (
            _row(1, name="Acme SA", street="1 Main Street"),
            _row(2, name="ACME S.A.", street="Main Street 1"),
        )
        staging_summary = self.staging.publish_canonical_staging(
            self.project.project_id,
            _staging_run(self.project.project_id, rows),
            mapping_version=1,
            actor=LOCAL_ACTOR,
        )
        evaluation = evaluate_resolution_candidates(
            policy=policy,
            staging_content_hash=staging_summary.content_hash,
            rows=rows,
        )
        summary = self.repository.publish_resolution_evaluation(
            self.project.project_id,
            evaluation,
            staging_run_id=staging_summary.run_id,
            actor=LOCAL_ACTOR,
        )
        candidate = evaluation.candidates[0]
        first = ResolutionDecision(
            decision_id=str(uuid4()),
            evaluation_hash=evaluation.content_hash,
            group_id=candidate.candidate_id,
            kind=ResolutionDecisionKind.SAME_RECORD,
            row_ids=(candidate.left_row_id, candidate.right_row_id),
            reason="First reviewed choice.",
            actor=LOCAL_ACTOR.identity,
            decided_at=NOW,
            lifecycle_version=1,
        )
        self.repository.append_resolution_decision(
            self.project.project_id,
            summary.run_id,
            first,
            expected_lifecycle_version=0,
            actor=LOCAL_ACTOR,
        )
        opposite = ResolutionDecision(
            decision_id=str(uuid4()),
            evaluation_hash=evaluation.content_hash,
            group_id=candidate.candidate_id,
            kind=ResolutionDecisionKind.KEEP_SEPARATE,
            row_ids=(candidate.left_row_id, candidate.right_row_id),
            reason="Conflicting reviewed choice.",
            actor=LOCAL_ACTOR.identity,
            decided_at=NOW,
            lifecycle_version=2,
        )

        with self.assertRaisesRegex(Exception, "already recorded"):
            self.repository.append_resolution_decision(
                self.project.project_id,
                summary.run_id,
                opposite,
                expected_lifecycle_version=1,
                actor=LOCAL_ACTOR,
            )



if __name__ == "__main__":
    unittest.main()
