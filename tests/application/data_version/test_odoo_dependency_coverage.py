from datetime import UTC, datetime
from types import SimpleNamespace
import unittest

from impodo.application.odoo_capture_publication_service import (
    _require_captured_relationship_coverage,
)
from impodo.domain.odoo_provenance import (
    OdooOriginBatch,
    OdooRelationshipOriginColumn,
)
from impodo.domain.workspace.errors import WorkspaceError


class CapturedDependencyCoverageTests(unittest.TestCase):
    def test_selected_link_requires_related_row_in_same_capture_set(self) -> None:
        now = datetime.now(UTC)
        owner = OdooOriginBatch(
            first_row_ordinal=1,
            odoo_ids=(10,),
            write_dates=(now,),
            relationships=(
                OdooRelationshipOriginColumn(
                    field_name="partner_id",
                    kind="many2one",
                    relation_model="res.partner",
                    values=((20,),),
                ),
            ),
        )
        result = SimpleNamespace(
            selection=SimpleNamespace(model="sale.order"),
            request=SimpleNamespace(
                relationship_projection=(
                    SimpleNamespace(name="partner_id", relation_model="res.partner"),
                ),
            ),
        )
        with self.assertRaisesRegex(WorkspaceError, "outside the captured res.partner"):
            _require_captured_relationship_coverage(
                (result,),
                {"sale.order": [owner], "res.partner": []},
            )
        related = OdooOriginBatch(
            first_row_ordinal=1,
            odoo_ids=(20,),
            write_dates=(now,),
        )
        _require_captured_relationship_coverage(
            (result,),
            {"sale.order": [owner], "res.partner": [related]},
        )
        incomplete_owner = OdooOriginBatch(
            first_row_ordinal=1,
            odoo_ids=(10,),
            write_dates=(now,),
        )
        with self.assertRaisesRegex(WorkspaceError, "evidence is incomplete"):
            _require_captured_relationship_coverage(
                (result,),
                {"sale.order": [incomplete_owner], "res.partner": [related]},
            )
