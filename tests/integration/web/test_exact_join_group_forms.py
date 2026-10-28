from __future__ import annotations

import unittest
from datetime import UTC, datetime
from uuid import uuid4

from starlette.datastructures import FormData

from impodo.domain.source_binding import FileSourceBinding
from impodo.domain.workspace.contracts import (
    SourceDataset,
    SourceDatasetColumn,
    SourceSelection,
)
from impodo.web.exact_join_forms import (
    default_exact_join_group_form,
    exact_join_group_projection_candidates,
    parse_exact_join_group_form,
    retain_exact_join_group_form,
)


class ExactJoinGroupFormTests(unittest.TestCase):
    def setUp(self) -> None:
        source = FileSourceBinding(
            file_id=str(uuid4()),
            table_key="customers.csv",
            source_sha256="1" * 64,
            catalog_hash="sha256:" + "2" * 64,
            encoding="utf-8",
            delimiter=",",
            header_row=1,
        )
        self.selection = SourceSelection(
            selection_id=str(uuid4()),
            version=1,
            data_version_id=str(uuid4()),
            created_at=datetime.now(UTC),
            created_by="Test operator",
            datasets=(
                SourceDataset(
                    dataset_id="dataset:primary",
                    name="customers",
                    source=source,
                    row_count=10,
                    columns=(
                        SourceDatasetColumn(1, "Account", "account", "string"),
                        SourceDatasetColumn(2, "RecId", "recid", "integer"),
                        SourceDatasetColumn(3, "Unused", "unused", "empty"),
                    ),
                ),
                SourceDataset(
                    dataset_id="dataset:details",
                    name="CustTable",
                    source=source,
                    row_count=10,
                    columns=(
                        SourceDatasetColumn(1, "Account", "detail_account", "string"),
                        SourceDatasetColumn(2, "RecId", "detail_recid", "integer"),
                        SourceDatasetColumn(3, "City", "city", "string"),
                        SourceDatasetColumn(4, "Unused", "detail_unused", "empty"),
                    ),
                ),
            ),
            content_hash="sha256:" + "3" * 64,
        )

    def test_default_selects_only_non_empty_primary_fields(self) -> None:
        form = default_exact_join_group_form(self.selection, None)
        candidates = exact_join_group_projection_candidates(self.selection)

        selected = {
            (candidates[index][0].name, candidates[index][1].source_name)
            for index in form.projection_indexes
        }
        self.assertEqual(
            selected,
            {("customers", "Account"), ("customers", "RecId")},
        )
        names = {
            (dataset.name, column.source_name): form.projection_names[index]
            for index, (dataset, column) in enumerate(candidates)
        }
        self.assertEqual(names[("customers", "RecId")], "RecId")
        self.assertEqual(names[("CustTable", "RecId")], "CustTable.RecId")
        self.assertEqual(names[("CustTable", "Account")], "CustTable.Account")

    def test_more_than_two_hundred_selected_fields_is_rejected_early(self) -> None:
        form = FormData(
            (
                ("group_addition", "1"),
                *(("group_projection", str(index)) for index in range(201)),
            )
        )

        with self.assertRaisesRegex(ValueError, "no more than 200"):
            parse_exact_join_group_form(form)

        retained = retain_exact_join_group_form(form)
        self.assertEqual(len(retained.projection_indexes), 201)

    def test_invalid_submission_retains_bounded_group_choices(self) -> None:
        form = FormData(
            (
                ("group_id", "not-a-uuid"),
                ("output_dataset_name", "records_complete"),
                ("primary_dataset_id", "dataset:primary"),
                ("primary_key_field_1", "dataset:primary|account"),
                ("group_addition", "1"),
                ("group_addition_key_1_1", "dataset:details|detail_account"),
                ("group_projection", "0"),
                ("group_projection_name", "Account"),
            )
        )

        retained = retain_exact_join_group_form(form)

        self.assertEqual(retained.output_dataset_name, "records_complete")
        self.assertEqual(retained.primary_dataset_id, "dataset:primary")
        self.assertEqual(retained.projection_indexes, (0,))
        self.assertEqual(retained.projection_names, ("Account",))
        self.assertEqual(retained.additions[0].dataset_index, 1)


if __name__ == "__main__":
    unittest.main()
