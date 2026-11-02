"""Preserve worksheet coordinates when sparse Excel exports contain empty cells."""

from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from openpyxl import Workbook
from openpyxl.styles import PatternFill

from impodo.application.data_version.source_files import (
    load_selected_source_table,
    open_selected_source_batches,
)
from impodo.domain.preparation.source import SourceLoadError


class SparseXlsxRowsTests(unittest.TestCase):
    def test_sparse_rows_preserve_coordinates_in_both_readers(self) -> None:
        for named_table in (False, True):
            for declared_dimensions in (False, True):
                with (
                    self.subTest(
                        named_table=named_table,
                        declared_dimensions=declared_dimensions,
                    ),
                    tempfile.TemporaryDirectory() as directory,
                ):
                    path = Path(directory) / "customers.xlsx"
                    workbook = Workbook()
                    worksheet = workbook.active
                    worksheet.title = "Customers"
                    first_column = 3 if named_table else 1
                    for row, values in (
                        (3, ("Code", "Name")),
                        (4, ("C1", "First")),
                        (8, ("C2", "Second")),
                        (10, (None, "No code")),
                    ):
                        for column, value in enumerate(values, first_column):
                            if value is not None:
                                worksheet.cell(row, column, value)
                    worksheet.cell(12, first_column + 1).fill = PatternFill(
                        fill_type="solid", fgColor="FFFF00"
                    )
                    workbook.save(path)
                    workbook.close()
                    if not declared_dimensions:
                        rewritten = Path(directory) / "without-dimensions.xlsx"
                        with (
                            ZipFile(path) as reader,
                            ZipFile(rewritten, "w", compression=ZIP_DEFLATED) as writer,
                        ):
                            for member in reader.infolist():
                                payload = reader.read(member.filename)
                                if member.filename == "xl/worksheets/sheet1.xml":
                                    payload = re.sub(
                                        rb"<dimension[^>]*/>", b"", payload
                                    )
                                writer.writestr(member, payload)
                        path = rewritten
                    options = {
                        "dataset": "customers",
                        "table_key": (
                            "table:Customers:Selected"
                            if named_table
                            else "sheet:Customers"
                        ),
                        "encoding": None,
                        "delimiter": None,
                        "header_row": 3,
                        "named_table_range": "C3:D12" if named_table else None,
                    }

                    materialized = load_selected_source_table(path, **options)

                    self.assertEqual(materialized.headers, ("Code", "Name"))
                    self.assertEqual(
                        [(row.number, row.values) for row in materialized.rows],
                        [
                            (4, {"Code": "C1", "Name": "First"}),
                            (8, {"Code": "C2", "Name": "Second"}),
                            (10, {"Code": None, "Name": "No code"}),
                        ],
                    )
                    for batch_size in (1, 2):
                        with open_selected_source_batches(
                            path, **options, batch_size=batch_size
                        ) as source:
                            batches = tuple(source.iter_batches())
                        self.assertTrue(
                            all(len(batch) <= batch_size for batch in batches)
                        )
                        self.assertEqual(
                            tuple(row for batch in batches for row in batch),
                            materialized.rows,
                        )

    def test_unsafe_cells_after_blank_rows_and_first_cell_are_still_rejected(
        self,
    ) -> None:
        for value in ("=1+1", "#DIV/0!"):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "customers.xlsx"
                workbook = Workbook()
                worksheet = workbook.active
                worksheet.title = "Customers"
                worksheet.append(["Code", "Name"])
                worksheet["B5"] = value
                workbook.save(path)
                workbook.close()

                with self.assertRaisesRegex(SourceLoadError, "Customers!B5"):
                    load_selected_source_table(
                        path,
                        dataset="customers",
                        table_key="sheet:Customers",
                        encoding=None,
                        delimiter=None,
                        header_row=1,
                    )
