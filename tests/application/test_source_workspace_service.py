from datetime import datetime, timezone
import unittest

from impodo.application.data_version.inspection import (
    CATALOG_CONTRACT_VERSION,
    SourceFileCatalog,
    SourceTableCatalog,
)
from impodo.application.source_workspace_service import (
    _blocking_header_problem,
    _dataset_name_violations,
)


class DatasetNameValidationTests(unittest.TestCase):
    def test_valid_name_has_no_violations(self) -> None:
        self.assertEqual(_dataset_name_violations("product_with_uom_v1"), ())

    def test_blank_name_explains_that_a_name_is_required(self) -> None:
        self.assertEqual(
            _dataset_name_violations(""),
            ("Enter a name.",),
        )

    def test_long_name_explains_the_length_limit(self) -> None:
        self.assertEqual(
            _dataset_name_violations("a" * 64),
            ("Use no more than 63 characters.",),
        )

    def test_uppercase_name_explains_each_broken_rule(self) -> None:
        self.assertEqual(
            _dataset_name_violations("Product_withUoM_v1"),
            (
                "Start with a lowercase letter from a to z.",
                "Use only lowercase letters, numbers, and underscores.",
            ),
        )

    def test_punctuation_explains_the_allowed_characters(self) -> None:
        self.assertEqual(
            _dataset_name_violations("product-with-uom"),
            ("Use only lowercase letters, numbers, and underscores.",),
        )


class SourceHeaderValidationTests(unittest.TestCase):
    def test_data_beyond_headers_is_explained_during_source_review(self) -> None:
        table = SourceTableCatalog(
            table_key="sheet:PLW",
            name="PLW",
            kind="WORKSHEET",
            hidden=False,
            header_row=1,
            row_count=1,
            column_count=2,
            columns=(),
            preview_rows=(),
            warnings=(
                "1 data row(s) contain cells beyond the candidate header; "
                "the first value is at C2",
            ),
        )
        catalog = SourceFileCatalog(
            contract_version=CATALOG_CONTRACT_VERSION,
            file_id="stored-source-id",
            display_name="PLW-Article.xlsx",
            source_sha256="sha256:" + "a" * 64,
            source_size_bytes=100,
            format="XLSX",
            inspected_at=datetime.now(timezone.utc),
            encoding=None,
            delimiter=None,
            tables=(table,),
        )

        self.assertEqual(
            _blocking_header_problem(catalog, table),
            "Source file 'PLW-Article.xlsx', sheet 'PLW' has data in column C, "
            "but header cell C1 is empty. Add a name in C1, or remove the "
            "unexpected data from column C, then update the preview.",
        )


if __name__ == "__main__":
    unittest.main()
