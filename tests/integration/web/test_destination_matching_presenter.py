from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
import unittest
from uuid import uuid4

from impodo.domain.workspace.contracts import (
    SchemaField,
    SchemaModel,
    SourceDataset,
    SourceDatasetColumn,
)
from impodo.web.routers.destination_matching import _matching_rows
from tests.application.workspace.test_destination_matching import (
    _binding,
    _selection,
    _source_schema,
    _workspace,
)


class DestinationMatchingPresenterTests(unittest.TestCase):
    def test_destination_setup_defaults_to_reference_only_per_record_type(self) -> None:
        now = datetime.now(UTC)
        workspace = _workspace(now)
        selection = _selection(now)
        schema = _source_schema(workspace, now)
        company = SourceDataset(
            dataset_id=str(uuid4()),
            name="Companies",
            source=_binding("res.company"),
            row_count=1,
            columns=(
                SourceDatasetColumn(1, "name", "company-name", "TEXT"),
            ),
        )
        company_relation = SchemaField(
            name="company_id",
            label="Company",
            type="many2one",
            required=False,
            readonly=False,
            relation="res.company",
            relation_field=None,
            selection=(),
            related=False,
            company_dependent=False,
            exportable=True,
        )
        company_name = SchemaField(
            name="name",
            label="Company Name",
            type="char",
            required=True,
            readonly=False,
            relation=None,
            relation_field=None,
            selection=(),
            stored=True,
            related=True,
            company_dependent=False,
            exportable=True,
        )
        selection = replace(
            selection,
            datasets=(selection.datasets[0], company),
        )
        schema = replace(
            schema,
            models=(
                replace(
                    schema.models[0],
                    fields=(*schema.models[0].fields, company_relation),
                ),
                SchemaModel("res.company", "Company", (company_name,)),
            ),
        )

        rows = {
            row["model"]: row
            for row in _matching_rows(workspace, selection, schema)
        }

        self.assertEqual(rows["product.template"]["destination_handling"], "transfer")
        self.assertEqual(
            rows["res.company"]["destination_handling"],
            "reference_only",
        )
        self.assertEqual(rows["res.company"]["selected_keys"], ("company-name",))


if __name__ == "__main__":
    unittest.main()
