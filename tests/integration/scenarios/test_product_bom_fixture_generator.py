"""Preserve consumed scenario workbooks while generating only explicit outputs."""

from contextlib import redirect_stderr
from hashlib import sha256
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase, main
from unittest.mock import patch

from openpyxl import load_workbook

from scripts import generate_product_bom_fixture as generator
from tests.support.paths import REPOSITORY_ROOT

ROOT = REPOSITORY_ROOT
EXPECTED = ROOT / "scenarios/product-bom-headers-round-trip/v1/fixtures"
NAMES = ("DEMO_Article.xlsx", "DEMO_BOMVersion.xlsx", "DEMO_BOM.xlsx")


def worksheet_content(path):
    """Compare source meaning, including native dates and text/numeric types."""
    workbook = load_workbook(path, read_only=True, data_only=False)
    try:
        return tuple(
            (
                sheet.title,
                tuple(
                    tuple((cell.value, cell.data_type) for cell in row)
                    for row in sheet.iter_rows()
                ),
            )
            for sheet in workbook.worksheets
        )
    finally:
        workbook.close()


class ProductBomFixtureGeneratorTests(TestCase):
    def test_explicit_new_output_matches_retained_sheets_headers_and_cells(self):
        before = {
            name: sha256((EXPECTED / name).read_bytes()).hexdigest() for name in NAMES
        }
        with TemporaryDirectory() as directory:
            output = Path(directory) / "generated"
            self.assertEqual(generator.main(["--output-directory", str(output)]), 0)
            self.assertEqual({path.name for path in output.iterdir()}, set(NAMES))
            for name in NAMES:
                with self.subTest(workbook=name):
                    self.assertEqual(
                        worksheet_content(output / name),
                        worksheet_content(EXPECTED / name),
                    )
        self.assertEqual(
            before,
            {
                name: sha256((EXPECTED / name).read_bytes()).hexdigest()
                for name in NAMES
            },
        )

    def test_existing_output_is_rejected_without_changing_its_contents(self):
        with TemporaryDirectory() as directory:
            output = Path(directory) / "existing"
            output.mkdir()
            sentinel = output / "previous-evidence.txt"
            sentinel.write_text("preserve this file", encoding="utf-8")
            with self.assertRaises(FileExistsError):
                generator.generate_fixture(output)
            self.assertEqual([path.name for path in output.iterdir()], [sentinel.name])
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "preserve this file")

    def test_output_argument_is_required_before_any_generation(self):
        with (
            patch.object(generator, "generate_fixture") as generate,
            redirect_stderr(StringIO()),
            self.assertRaises(SystemExit) as error,
        ):
            generator.main([])
        self.assertEqual(error.exception.code, 2)
        generate.assert_not_called()

    def test_main_rejects_a_generated_set_with_duplicate_product_identity(self):
        rows = [list(row) for row in generator.ARTICLE_ROWS]
        rows[1][0] = rows[0][0]
        with (
            TemporaryDirectory() as directory,
            patch.object(generator, "ARTICLE_ROWS", rows),
            self.assertRaises(AssertionError),
        ):
            generator.main(["--output-directory", str(Path(directory) / "invalid")])


if __name__ == "__main__":
    main()
