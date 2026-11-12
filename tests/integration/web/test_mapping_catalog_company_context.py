"""Keep the selected lookup company visible in linked-field search fragments."""

from unittest import TestCase
from unittest.mock import patch

from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from impodo.domain.workspace.workbench import OdooConnectionMode
from tests.integration.web import test_company_dependent_lookup as company_lookup
from tests.support.database_probe import count_duckdb_statements


class MappingCatalogCompanyContextTests(TestCase):
    def test_linked_field_search_displays_company_for_both_majors_and_access_modes(
        self,
    ):
        fixture = company_lookup.CompanyDependentLookupTests(methodName="runTest")
        fixture.setUp()
        try:
            for major in (19, 20):
                for mode in (OdooConnectionMode.LOCAL, OdooConnectionMode.REMOTE):
                    with self.subTest(major=major, mode=mode):
                        workspace_id, _, _ = fixture.configure(major, mode)
                        with patch.object(
                            fixture.app.state.context,
                            "readiness_reader",
                            side_effect=AssertionError("Field search contacted Odoo"),
                        ):
                            for query, projection in (
                                ("Payable", "miss"),
                                ("Receivable", "hit"),
                            ):
                                with (
                                    collect_duckdb_request_timings() as timings,
                                    count_duckdb_statements() as statements,
                                ):
                                    response = fixture.client.get(
                                        f"/workspaces/{workspace_id}/mapping/field-catalog",
                                        params={
                                            "catalog": "relation",
                                            "relation_query": query,
                                        },
                                    )
                                self.assertEqual(
                                    response.status_code, 200, response.text
                                )
                                self.assertIn("Company for this lookup:", response.text)
                                self.assertIn("Example Wiltz", response.text)
                                self.assertIn(f"Account {query}", response.text)
                                self.assertNotIn("<main", response.text)
                                self.assertEqual(
                                    response.headers["cache-control"], "no-store"
                                )
                                self.assertEqual(
                                    response.headers["x-impodo-catalog-projection"],
                                    projection,
                                )
                                self.assertLessEqual(timings.connection_count, 6)
                                self.assertLessEqual(timings.schema_check_count, 4)
                                self.assertLessEqual(len(statements), 100)
                                self.assertEqual(timings.lock_retry_count, 0)
        finally:
            fixture.tearDown()
