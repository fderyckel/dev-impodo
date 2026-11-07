"""Pin manufacturing source intent independently of runtime qualification status.

The source-only application cases use isolated temporary workspaces and normal
freeze/preview/save services. They never capture or call an Odoo target. Native
metadata, execution and Recipe qualification belong to the separate runtime
matrix; this suite checks the pack's explicit declarations, not a status label.
"""

from __future__ import annotations

import csv
import io
import json
from collections import Counter, defaultdict
from decimal import Decimal
from hashlib import sha256
from unittest import TestCase

from impodo.application.workspace.derived_entities import (
    ExactJoinGroupAddition,
    ExactJoinGroupProjection,
    ExactJoinGroupRequest,
)
from impodo.domain.workspace.errors import WorkspaceError
from impodo.web.composition.page_reads import run_local_operation_sync
from tests.support.hierarchy_workflow import HierarchyWorkflowFixture
from tests.support.paths import REPOSITORY_ROOT

PACK = REPOSITORY_ROOT / "scenarios/impodo-practice/manufacturing/v1"
DATES = ("2026-04-15", "2026-05-15")
FILE_COUNTS = {
    "categories.csv": 12,
    "products_core.csv": 32,
    "product_details.csv": 32,
    "bom_headers.csv": 6,
    "bom_rows.csv": 24,
    "bom_operations.csv": 12,
}


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def read_csv(path):
    with path.open(newline="", encoding="utf-8-sig") as stream:
        return list(csv.DictReader(stream))


def row_key(row, *names):
    return tuple(row[name] for name in names)


def french_number(value):
    return Decimal(value.replace(",", "."))


class PracticeManufacturingCorpusTests(TestCase):
    def test_manifest_covers_twelve_sources_and_independent_contract_files(self):
        manifest = read_json(PACK / "manifest.json")
        entries = manifest["files"]
        names = [entry["path"] for entry in entries]
        inputs = {f"deliveries/{date}/{name}" for date in DATES for name in FILE_COUNTS}
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(
            set(names),
            inputs
            | {
                "expected-business-results.json",
                "target-seed-projection.json",
                "exercise-contracts.json",
            },
        )
        self.assertEqual(
            {path.relative_to(PACK).as_posix() for path in PACK.rglob("*.csv")},
            inputs,
        )
        for entry in entries:
            with self.subTest(path=entry["path"]):
                path = PACK / entry["path"]
                self.assertFalse(path.is_symlink())
                self.assertTrue(path.resolve().is_relative_to(PACK.resolve()))
                raw = path.read_bytes()
                self.assertEqual(len(raw), entry["bytes"])
                self.assertEqual(sha256(raw).hexdigest(), entry["sha256"])
                if path.suffix == ".csv":
                    self.assertEqual(len(read_csv(path)), entry["data_rows"])
        self.assertEqual(
            manifest["source_counts"],
            {
                "csv_files": 12,
                "physical_data_rows": 236,
                "baseline_csv_files": 6,
                "baseline_physical_data_rows": 118,
                "reordered_csv_files": 6,
                "reordered_physical_data_rows": 118,
            },
        )

    def test_recipe_delivery_reorders_the_same_complete_business_rows(self):
        contracts = read_json(PACK / "exercise-contracts.json")
        self.assertFalse(contracts["delivery_contract"]["partial_delivery"])
        self.assertFalse(contracts["delivery_contract"]["absent_rows_are_deletes"])
        for name, count in FILE_COUNTS.items():
            with self.subTest(source=name):
                baseline, fresh = (
                    read_csv(PACK / "deliveries" / date / name) for date in DATES
                )
                self.assertEqual((len(baseline), len(fresh)), (count, count))
                self.assertNotEqual(baseline, fresh)
                self.assertEqual(
                    Counter(tuple(sorted(row.items())) for row in baseline),
                    Counter(tuple(sorted(row.items())) for row in fresh),
                )

    def test_compound_product_join_and_scalar_answers_match_both_deliveries(self):
        answers = read_json(PACK / "expected-business-results.json")["baseline"]
        products = {row["product_ref"]: row for row in answers["products"]}
        self.assertEqual(len(products), 32)
        composition = read_json(PACK / "exercise-contracts.json")["source_composition"][
            "products"
        ]
        self.assertEqual(composition["join_keys"], ["CompanyCode", "ProductRef"])
        self.assertEqual(composition["authoritative_name"], "products_core.Name")
        for date in DATES:
            with self.subTest(delivery=date):
                core = read_csv(PACK / "deliveries" / date / "products_core.csv")
                details = read_csv(PACK / "deliveries" / date / "product_details.csv")
                by_key = {
                    row_key(row, "CompanyCode", "ProductRef"): row for row in details
                }
                keys = {row_key(row, "CompanyCode", "ProductRef") for row in core}
                self.assertEqual((len(by_key), len(keys)), (32, 32))
                self.assertEqual(keys, set(by_key))
                self.assertEqual(
                    Counter(row["CompanyCode"] for row in core),
                    {"NORTH": 16, "SOUTH": 16},
                )
                # This corpus prefixes ProductRef globally. Dropping CompanyCode
                # does NOT currently multiply rows; do not copy customer36 here.
                leaf_counts = Counter(row["ProductRef"] for row in details)
                self.assertEqual(set(leaf_counts.values()), {1})
                self.assertEqual(
                    sum(leaf_counts[row["ProductRef"]] for row in core), 32
                )
                for row in core:
                    answer = products[row["ProductRef"]]
                    detail = by_key[row_key(row, "CompanyCode", "ProductRef")]
                    self.assertEqual(answer["name"], " ".join(row["Name"].split()))
                    self.assertNotEqual(row["Name"], detail["Name"])
                    self.assertEqual(
                        Decimal(answer["price"]), french_number(row["SalesPrice"])
                    )
                    self.assertEqual(
                        answer["active"], {"10": True, "20": False}[row["StatusCode"]]
                    )
                    self.assertEqual(
                        answer["target_type"],
                        {"Article": "consu", "Service": "service"}[
                            row["ProductTypeLabel"]
                        ],
                    )
                    self.assertEqual(
                        answer["tags"], sorted(detail["TagCodes"].split(";"))
                    )
                    self.assertEqual(answer["source_category_key"], row["CategoryKey"])
                    self.assertEqual(answer["source_company_code"], row["CompanyCode"])
                    self.assertIsNone(answer["target_company_assignment"])
                self.assertEqual(
                    sum(french_number(row["SalesPrice"]) for row in core),
                    Decimal("1313.50"),
                )
        self.assertEqual(
            {ref for ref, row in products.items() if not row["active"]},
            {"MA-P-0028", "MA-P-0031"},
        )
        self.assertEqual(
            {ref for ref, row in products.items() if Decimal(row["price"]) == 0},
            {"MA-P-0027", "MA-P-0032"},
        )

    def test_recursive_category_paths_preserve_roots_and_ambiguous_leaves(self):
        oracle = read_json(PACK / "expected-business-results.json")
        answers = {row["source_key"]: row for row in oracle["baseline"]["categories"]}
        self.assertEqual(
            oracle["identity_policy"]["product.category"],
            {
                "fields": ["name"],
                "scope_fields": ["parent_id"],
                "parent_identity_recursive": True,
            },
        )
        self.assertEqual(
            sum(row["parent_source_key"] is None for row in answers.values()), 3
        )
        self.assertEqual(
            {
                tuple(row["path"])
                for row in answers.values()
                if row["path"][-1] == "Assembly"
            },
            {
                ("Components", "Metals", "Assembly"),
                ("Components", "Polymers", "Assembly"),
                ("Services", "Assembly"),
            },
        )
        for date in DATES:
            rows = read_csv(PACK / "deliveries" / date / "categories.csv")
            self.assertEqual({row["CategoryKey"] for row in rows}, set(answers))
            for row in rows:
                answer = answers[row["CategoryKey"]]
                self.assertEqual(answer["path"][-1], row["Name"])
                self.assertEqual(
                    answer["parent_source_key"], row["ParentCategoryKey"] or None
                )
                if row["ParentCategoryKey"]:
                    self.assertEqual(
                        answer["path"][:-1], answers[row["ParentCategoryKey"]]["path"]
                    )
                else:
                    self.assertEqual(len(answer["path"]), 1)

    def test_repeated_headers_lines_and_operations_keep_their_own_grains(self):
        oracle = read_json(PACK / "expected-business-results.json")["baseline"]
        composition = read_json(PACK / "exercise-contracts.json")["source_composition"]
        self.assertFalse(composition["operations"]["join_to_lines"])
        headers = {row["bom_ref"]: row for row in oracle["bom_headers"]}
        line_answers = {row["line_ref"]: row for row in oracle["bom_lines"]}
        operation_answers = {row["operation_ref"]: row for row in oracle["operations"]}
        for date in DATES:
            supplied_headers = read_csv(PACK / "deliveries" / date / "bom_headers.csv")
            supplied = {
                row_key(row, "CompanyCode", "BOMRef"): row for row in supplied_headers
            }
            self.assertEqual(len(supplied), 6)
            self.assertNotIn("Sequence", supplied_headers[0])
            self.assertEqual(composition["boms"]["source"], "bom_headers.csv")
            self.assertEqual(composition["boms"]["component_source"], "bom_rows.csv")
            rows = read_csv(PACK / "deliveries" / date / "bom_rows.csv")
            operations = read_csv(PACK / "deliveries" / date / "bom_operations.csv")
            groups = defaultdict(list)
            for row in rows:
                groups[row_key(row, "CompanyCode", "BOMRef")].append(row)
            op_counts = Counter(
                row_key(row, "CompanyCode", "BOMRef") for row in operations
            )
            self.assertEqual(len(groups), 6)
            self.assertEqual({len(group) for group in groups.values()}, {4})
            self.assertEqual(set(op_counts.values()), {2})
            self.assertEqual(set(groups), set(op_counts))
            self.assertEqual(set(groups), set(supplied))
            self.assertEqual(
                {len(group) * op_counts[key] for key, group in groups.items()}, {8}
            )
            self.assertEqual(
                sum(len(group) * op_counts[key] for key, group in groups.items()), 48
            )
            for key, group in groups.items():
                self.assertEqual(sum(row["Sequence"] == "10" for row in group), 1)
                self.assertEqual(
                    sorted(int(row["Sequence"]) for row in group), [10, 20, 30, 40]
                )
                for field in composition["boms"]["header_agreement_fields"]:
                    self.assertEqual(
                        len({row[field] for row in group}), 1, (key, field)
                    )
                for repeated in group:
                    for field, expected in supplied[key].items():
                        self.assertEqual(repeated[field], expected, (key, field))
                header = headers[key[1]]
                self.assertEqual(
                    header["source_header_name"], supplied[key]["HeaderName"]
                )
                self.assertEqual(header["approved_label"], supplied[key]["Approved"])
                self.assertEqual(header["active_label"], supplied[key]["Active"])
                self.assertEqual(
                    header["active"],
                    supplied[key]["Approved"] == "Oui"
                    and supplied[key]["Active"] == "Oui",
                )
                self.assertEqual(header["company_code"], key[0])
                self.assertEqual(
                    header["finished_product_ref"], group[0]["FinishedProductRef"]
                )
                self.assertEqual(
                    Decimal(header["product_qty"]), french_number(group[0]["BaseQty"])
                )
            for row in rows:
                answer = line_answers[row["LineRef"]]
                self.assertEqual(row["QuantitySeries"], "1")
                self.assertEqual(
                    (answer["bom_ref"], answer["sequence"], answer["component_ref"]),
                    (row["BOMRef"], int(row["Sequence"]), row["ComponentRef"]),
                )
                self.assertEqual(
                    Decimal(answer["quantity"]), french_number(row["Quantity"])
                )
            for row in operations:
                answer = operation_answers[row["OperationRef"]]
                self.assertEqual(
                    (
                        answer["bom_ref"],
                        answer["sequence"],
                        answer["name"],
                        answer["work_centre_code"],
                    ),
                    (
                        row["BOMRef"],
                        int(row["Sequence"]),
                        row["Name"],
                        row["WorkCentreCode"],
                    ),
                )
                self.assertEqual(
                    Decimal(answer["minutes"]), french_number(row["Minutes"])
                )
            self.assertEqual(
                sum(french_number(row["Quantity"]) for row in rows), Decimal("22.500")
            )
            self.assertEqual(
                sum(french_number(row["Minutes"]) for row in operations),
                Decimal("94.50"),
            )

    def test_repeated_component_and_two_subassemblies_are_exact_and_acyclic(self):
        oracle = read_json(PACK / "expected-business-results.json")["baseline"]
        repeated = [
            row
            for row in oracle["bom_lines"]
            if row["bom_ref"] == "MA-BOM-003" and row["component_ref"] == "MA-P-0011"
        ]
        self.assertEqual(
            [(row["sequence"], row["quantity"]) for row in repeated],
            [(30, "1.000"), (40, "2.000")],
        )
        product_boms = {
            row["finished_product_ref"]: row["bom_ref"] for row in oracle["bom_headers"]
        }
        edges = defaultdict(set)
        for row in oracle["bom_lines"]:
            if row["component_ref"] in product_boms:
                edges[row["bom_ref"]].add(product_boms[row["component_ref"]])
        self.assertEqual(
            dict(edges), {"MA-BOM-001": {"MA-BOM-005"}, "MA-BOM-002": {"MA-BOM-006"}}
        )

        def visit(node, ancestors):
            self.assertNotIn(node, ancestors, "The legal baseline acquired a BoM cycle")
            for child in edges.get(node, ()):
                visit(child, ancestors | {node})

        for node in edges:
            visit(node, set())

    def test_seed_phases_company_ownership_and_version_fields_are_explicit(self):
        seed = read_json(PACK / "target-seed-projection.json")
        contracts = read_json(PACK / "exercise-contracts.json")
        oracle = read_json(PACK / "expected-business-results.json")["baseline"]
        stages = {stage["id"]: stage for stage in seed["stages"]}
        self.assertEqual(
            [row["path"] for row in stages["M0-initial"]["categories"]],
            [["Components"], ["Components", "Metals"]],
        )
        self.assertEqual(
            seed["preexisting_references"],
            [
                {
                    "source_key": "CAT-10",
                    "path": ["Services"],
                    "xmlid": "product.product_category_services",
                    "must_already_exist": True,
                    "owned": False,
                }
            ],
        )
        self.assertEqual(
            stages["M0-initial"]["available_category_paths_before_first_run"],
            [["Components"], ["Components", "Metals"], ["Services"]],
        )
        self.assertEqual(
            oracle["shared_categories"],
            {"create": 9, "update": 0, "unchanged": 3, "repeat_unchanged": 12},
        )
        self.assertEqual(
            stages["M2-product-seed"]["requires_stage"], "M1-category-complete"
        )
        self.assertEqual(len(stages["M2-product-seed"]["products"]), 8)
        self.assertIs(oracle["initial_product_state"]["active"], True)
        self.assertEqual(
            {row["product_ref"] for row in oracle["products"] if not row["active"]},
            {"MA-P-0028", "MA-P-0031"},
        )
        self.assertEqual(
            stages["M3-all-products-complete"]["requires_stage"], "M3-products-active"
        )
        self.assertEqual(
            stages["M3-all-products-complete"]["required_template_count"], 32
        )
        self.assertEqual(
            seed["version_fields"],
            {
                "19": {
                    "bom_unit_field": "product_uom_id",
                    "bom_line_unit_field": "product_uom_id",
                    "product_unit_field": "uom_id",
                    "product_tags_field": "product_tag_ids",
                    "category_company_field": None,
                },
                "20": {
                    "bom_unit_field": "uom_id",
                    "bom_line_unit_field": "uom_id",
                    "product_unit_field": "uom_id",
                    "product_tags_field": "product_tag_ids",
                    "category_company_field": "company_id",
                },
            },
        )
        self.assertEqual(contracts["matrix"]["odoo_majors"], [19, 20])
        self.assertEqual(contracts["matrix"]["access_modes"], ["LOCAL", "REMOTE"])
        self.assertEqual(contracts["matrix"]["lanes"], ["authoring", "Recipe"])
        self.assertTrue(contracts["scope"]["single_operation_company_per_load"])
        self.assertFalse(
            contracts["scope"]["source_company_code_is_native_company_name"]
        )
        companies = {row["company_code"]: row["name"] for row in seed["companies"]}
        centers = {row["code"]: row for row in seed["work_centres"]}
        self.assertEqual(
            {code: row["company_code"] for code, row in centers.items()},
            {
                "MA-WC-01": "NORTH",
                "MA-WC-02": "SOUTH",
                "MA-WC-03": "NORTH",
                "MA-WC-04": "SOUTH",
            },
        )
        for operation in oracle["operations"]:
            self.assertEqual(
                centers[operation["work_centre_code"]]["company_code"],
                operation["company_code"],
            )
        for header in oracle["bom_headers"]:
            self.assertEqual(header["company_name"], companies[header["company_code"]])
        exercises = {exercise["id"]: exercise for exercise in contracts["exercises"]}
        self.assertEqual(
            exercises["CATEGORIES"]["workspace_counts"],
            {"create": 9, "update": 0, "unchanged": 3},
        )
        for company, expected in (
            ("NORTH", {"create": 12, "update": 0, "unchanged": 16}),
            ("SOUTH", {"create": 12, "update": 1, "unchanged": 15}),
        ):
            exercise = exercises[f"PRODUCTS_{company}"]
            self.assertEqual(exercise["workspace_counts"], expected)
            self.assertEqual(
                exercise["mapped_counts"]["product.category"],
                {"create": 0, "update": 0, "unchanged": 12},
            )
            self.assertEqual(
                exercises[f"BOMS_{company}"]["workspace_counts"],
                {"create": 21, "update": 0, "unchanged": 0},
            )


class PracticeManufacturingSourceRejectionTests(HierarchyWorkflowFixture):
    """Real source authoring rejects unsafe composition before any target work."""

    @staticmethod
    def column_key(dataset, name):
        return next(
            column.stable_key
            for column in dataset.columns
            if column.source_name == name
        )

    def combination(self, workspace_id, primary_name, addition_name):
        selection = self.context.queries.get_source_selection(workspace_id)
        by_name = {dataset.name: dataset for dataset in selection.datasets}
        primary, addition = by_name[primary_name], by_name[addition_name]
        names = (
            ("CompanyCode", "ProductRef")
            if primary_name == "products_core"
            else ("CompanyCode", "BOMRef")
        )
        return ExactJoinGroupRequest(
            output_dataset_name="unsafe_combination",
            primary_dataset_id=primary.dataset_id,
            primary_key_column_keys=tuple(
                self.column_key(primary, name) for name in names
            ),
            key_value_types=("string", "string"),
            additions=(
                ExactJoinGroupAddition(
                    addition.dataset_id,
                    tuple(self.column_key(addition, name) for name in names),
                ),
            ),
            projections=tuple(
                ExactJoinGroupProjection(
                    primary.dataset_id, self.column_key(primary, name), name
                )
                for name in names
            ),
        )

    def assert_rejected_without_mapping_or_target(self, workspace_id, request):
        preview = run_local_operation_sync(
            self.context.derived_entities.preview_join_group, workspace_id, request
        )
        self.assertFalse(preview.can_save)
        self.assertIsNone(preview.reviews[0].actual_output_rows)
        with self.assertRaisesRegex(WorkspaceError, "cannot be saved"):
            run_local_operation_sync(
                self.context.derived_entities.save_join_group,
                workspace_id,
                request,
                expected_parent_version=None,
                actor=self.actor,
            )
        self.assertIsNone(self.context.queries.get_derived_entity_plan(workspace_id))
        self.assertIsNone(
            self.context.mapping_workspace.mappings.get_mapping_revision(workspace_id)
        )
        self.assertEqual(
            (
                self.connection_calls,
                self.schema_calls,
                self.model_catalog_calls,
                self.read_identity_calls,
            ),
            ([], [], [], []),
        )
        return preview.reviews[0]

    def test_duplicate_product_detail_key_blocks_saved_exact_join_before_mapping(self):
        baseline = PACK / "deliveries" / DATES[0]
        rows = read_csv(baseline / "product_details.csv")
        duplicate = io.StringIO(newline="")
        writer = csv.DictWriter(
            duplicate, fieldnames=list(rows[0]), lineterminator="\n"
        )
        writer.writeheader()
        writer.writerows([*rows, rows[0]])
        workspace_id = self.freeze_files(
            {
                "products_core": (baseline / "products_core.csv").read_bytes(),
                "product_details": duplicate.getvalue().encode(),
            }
        )
        request = self.combination(workspace_id, "products_core", "product_details")
        review = self.assert_rejected_without_mapping_or_target(workspace_id, request)
        self.assertEqual((review.left_rows, review.right_rows), (32, 33))
        self.assertEqual(
            (review.duplicate_right_key_groups, review.duplicate_right_key_rows), (1, 2)
        )
        self.assertIn("Join right-side keys are not unique", review.findings)

    def test_operations_cannot_be_combined_with_repeated_component_rows(self):
        baseline = PACK / "deliveries" / DATES[0]
        workspace_id = self.freeze_files(
            {
                name: (baseline / f"{name}.csv").read_bytes()
                for name in ("bom_operations", "bom_rows")
            }
        )
        request = self.combination(workspace_id, "bom_operations", "bom_rows")
        review = self.assert_rejected_without_mapping_or_target(workspace_id, request)
        self.assertEqual((review.left_rows, review.right_rows), (12, 24))
        self.assertEqual(
            (review.duplicate_left_key_groups, review.duplicate_left_key_rows), (6, 12)
        )
        self.assertEqual(
            (review.duplicate_right_key_groups, review.duplicate_right_key_rows),
            (6, 24),
        )
        self.assertIn("Join right-side keys are not unique", review.findings)


if __name__ == "__main__":
    import unittest

    unittest.main()
