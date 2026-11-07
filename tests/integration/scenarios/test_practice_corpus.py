"""Pin current practice ownership and the independent source/answer contracts."""

import csv
import json
from collections import Counter
from hashlib import sha256
from unittest import TestCase

from tests.support.paths import REPOSITORY_ROOT

CURRENT = REPOSITORY_ROOT / "scenarios/impodo-practice/v1"
MANUFACTURING = REPOSITORY_ROOT / "scenarios/impodo-practice/manufacturing/v1"
EXTENSIONS = REPOSITORY_ROOT / "docs/plans/24-shared-practice-data"
DATES = ("2026-04-15", "2026-05-15")
CUSTOMER_FILES = (
    "customers_core.csv",
    "party_details.csv",
    "addresses.csv",
    "payment_terms.csv",
    "payment_term_lines.csv",
)
EXTERNAL_CURRENT = "../../../scenarios/impodo-practice/v1/"
EXTERNAL_MANUFACTURING = "../../../scenarios/impodo-practice/manufacturing/v1/"


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def read_csv(path):
    with path.open(newline="", encoding="utf-8-sig") as stream:
        return list(csv.DictReader(stream))


def key(row, *fields):
    return tuple(row[field] for field in fields)


class PracticeCorpusTests(TestCase):
    def assert_manifest_files(self, directory):
        manifest = read_json(directory / "manifest.json")
        entries = manifest["files"]
        names = [entry["path"] for entry in entries]
        self.assertEqual(len(names), len(set(names)), "A manifest path was duplicated")
        for entry in entries:
            with self.subTest(manifest=directory.name, path=entry["path"]):
                path = directory / entry["path"]
                self.assertFalse(path.is_symlink())
                self.assertTrue(path.resolve().is_relative_to(directory.resolve()))
                raw = path.read_bytes()
                self.assertEqual(len(raw), entry["bytes"])
                self.assertEqual(sha256(raw).hexdigest(), entry["sha256"])
                if path.suffix == ".csv":
                    self.assertEqual(len(read_csv(path)), entry["data_rows"])
        return manifest, set(names)

    def test_current_manifest_pins_the_ten_customer_inputs_and_two_answer_files(self):
        manifest, names = self.assert_manifest_files(CURRENT)
        inputs = {
            f"deliveries/{date}/{name}" for date in DATES for name in CUSTOMER_FILES
        }
        self.assertEqual(
            names,
            inputs | {"expected-business-results.json", "target-seed-projection.json"},
        )
        actual_csvs = {
            path.relative_to(CURRENT).as_posix() for path in CURRENT.rglob("*.csv")
        }
        self.assertEqual(actual_csvs, inputs)
        self.assertEqual(
            manifest["source_counts"],
            {
                "csv_files": 10,
                "physical_rows": 124,
                "baseline_rows": 62,
                "fresh_rows": 62,
            },
        )
        for date in DATES:
            counts = {
                name: len(read_csv(CURRENT / "deliveries" / date / name))
                for name in CUSTOMER_FILES
            }
            self.assertEqual(
                counts,
                {
                    "customers_core.csv": 18,
                    "party_details.csv": 18,
                    "addresses.csv": 18,
                    "payment_terms.csv": 4,
                    "payment_term_lines.csv": 4,
                },
            )

    def test_extension_manifest_has_complete_local_coverage_without_baseline_copies(
        self,
    ):
        manifest, names = self.assert_manifest_files(EXTENSIONS)
        actual_files = {
            path.relative_to(EXTENSIONS).as_posix()
            for path in EXTENSIONS.rglob("*")
            if path.is_file() and path.name != "manifest.json"
        }
        self.assertEqual(names, actual_files)
        csvs = [name for name in names if name.endswith(".csv")]
        self.assertEqual(len(csvs), 2)
        self.assertEqual(sum(len(read_csv(EXTENSIONS / name)) for name in csvs), 14)
        self.assertEqual(
            (manifest["counts"]["csv_files"], manifest["counts"]["source_rows_total"]),
            (2, 14),
        )
        for date in DATES:
            for name in CUSTOMER_FILES:
                self.assertFalse((EXTENSIONS / "deliveries" / date / name).exists())
        for name in (
            "categories.csv",
            "products_core.csv",
            "product_details.csv",
            "bom_rows.csv",
            "bom_operations.csv",
        ):
            self.assertFalse((EXTENSIONS / "deliveries" / DATES[0] / name).exists())
        faults = sorted((EXTENSIONS / "faults").glob("*.json"))
        self.assertEqual(len(faults), 18)
        for path in faults:
            fault = read_json(path)
            if not fault.get("base_file"):
                continue
            rows = read_csv(self.resolve_proposal_path(fault["base_file"]))
            for mutation in fault["mutations"]:
                with self.subTest(fault=path.name):
                    self.assertEqual(
                        sum(
                            all(
                                row.get(field) == value
                                for field, value in mutation["key"].items()
                            )
                            for row in rows
                        ),
                        1,
                    )
        contracts = read_json(EXTENSIONS / "exercise-contracts.json")
        for exercise in contracts["exercises"]:
            for source in exercise.get("source_files", ()):
                self.assertTrue(self.resolve_proposal_path(source).is_file(), source)
        overlay = read_json(EXTENSIONS / "target-overlays/vendor-term-repair.json")
        self.assertTrue(self.resolve_proposal_path(overlay["source_file"]).is_file())
        self.assertTrue(self.resolve_proposal_path(overlay["base_seed"]).is_file())
        for name in ("expected-business-results.json", "target-seed-projection.json"):
            document = read_json(EXTENSIONS / name)
            for owner in ("current_customer_pack", "current_manufacturing_pack"):
                reference = document[owner]
                for field in ("readme", "expected_business_results", "target_seed"):
                    self.assertTrue(
                        self.resolve_proposal_path(reference[field]).is_file()
                    )

    def resolve_proposal_path(self, relative):
        path = (EXTENSIONS / relative).resolve()
        permitted = (
            CURRENT
            if relative.startswith(EXTERNAL_CURRENT)
            else MANUFACTURING
            if relative.startswith(EXTERNAL_MANUFACTURING)
            else EXTENSIONS
        )
        self.assertTrue(path.is_relative_to(permitted.resolve()), relative)
        return path

    def test_both_customer_deliveries_require_the_partition_and_match_declared_answers(
        self,
    ):
        oracle = read_json(CURRENT / "expected-business-results.json")
        for date, section in zip(
            DATES, ("baseline", "fresh_customer_delivery"), strict=True
        ):
            with self.subTest(delivery=date):
                directory = CURRENT / "deliveries" / date
                accounts = read_csv(directory / "customers_core.csv")
                parties = read_csv(directory / "party_details.csv")
                addresses = read_csv(directory / "addresses.csv")
                by_party = {key(row, "Partition", "RecId"): row for row in parties}
                by_address = {key(row, "Partition", "Party"): row for row in addresses}
                primary_keys = {key(row, "Partition", "Party") for row in accounts}
                self.assertEqual(len(primary_keys), 18)
                self.assertEqual(len(by_party), 18)
                self.assertEqual(len(by_address), 18)
                self.assertEqual(primary_keys, set(by_party))
                self.assertEqual(primary_keys, set(by_address))
                self.assertEqual(
                    Counter(row["CompanyCode"] for row in accounts),
                    {"NORTH": 9, "SOUTH": 9},
                )
                # The defect-triggering alternative really would multiply rows.
                leaf_counts = Counter(row["RecId"] for row in parties)
                self.assertEqual(set(leaf_counts.values()), {2})
                self.assertEqual(sum(leaf_counts[row["Party"]] for row in accounts), 36)
                answers = {
                    row["customer_ref"]: row for row in oracle[section]["customers"]
                }
                self.assertEqual(len(answers), 18)
                self.assertEqual(set(answers), {row["CustomerRef"] for row in accounts})
                for row in accounts:
                    answer = answers[row["CustomerRef"]]
                    joined_name = by_party[key(row, "Partition", "Party")]["Name"]
                    self.assertEqual(answer["company_code"], row["CompanyCode"])
                    self.assertEqual(answer["name"], " ".join(joined_name.split()))
                    self.assertNotEqual(row["Name"], joined_name)
                    address = by_address[key(row, "Partition", "Party")]
                    self.assertEqual(answer["address"]["street"], address["Street"])
                    self.assertEqual(answer["address"]["postcode"], address["Postcode"])
                    self.assertEqual(answer["address"]["city"], address["City"])

    def test_terms_have_explicit_company_names_and_one_complete_reviewed_schedule(self):
        oracle = read_json(CURRENT / "expected-business-results.json")
        seed = read_json(CURRENT / "target-seed-projection.json")
        companies = {row["company_code"]: row["name"] for row in seed["companies"]}
        self.assertEqual(set(companies), {"NORTH", "SOUTH"})
        answers = {
            tuple(row["key"]): row for row in oracle["baseline"]["payment_terms"]
        }
        self.assertEqual(
            set(answers),
            {(company, term) for company in companies for term in ("NET30", "NET45")},
        )
        for date in DATES:
            directory = CURRENT / "deliveries" / date
            headers = read_csv(directory / "payment_terms.csv")
            lines = read_csv(directory / "payment_term_lines.csv")
            self.assertEqual(
                len({key(row, "CompanyCode", "TermCode") for row in headers}), 4
            )
            self.assertEqual(
                Counter(key(row, "CompanyCode", "TermCode") for row in lines),
                {value: 1 for value in answers},
            )
            for row in headers:
                answer = answers[key(row, "CompanyCode", "TermCode")]
                self.assertEqual(row["CompanyName"], companies[row["CompanyCode"]])
                self.assertEqual(row["Name"], answer["name"])
            for row in lines:
                schedule = answers[key(row, "CompanyCode", "TermCode")]["schedule"]
                self.assertEqual(int(row["Days"]), schedule["nb_days"])
                self.assertEqual(
                    (
                        schedule["line_count"],
                        schedule["value"],
                        schedule["value_amount"],
                        schedule["delay_type"],
                    ),
                    (1, "percent", "100", "days_after"),
                )
