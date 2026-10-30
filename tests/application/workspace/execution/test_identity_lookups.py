"""Bound native ancestor lookups and reject missing or widened parent keys."""

from unittest import TestCase

from impodo.application.workspace.execution.identity_lookups import NestedIdentityLookup
from impodo.domain.execution_snapshot import ExecutionDataset, ExecutionRow
from impodo.domain.shared.models import BusinessReference
from impodo.domain.workspace.errors import WorkspaceError


class NestedIdentityLookupTests(TestCase):
    def _planner(self, lookup, *, parents=("Root",), page_size=20):
        dataset = ExecutionDataset(
            dataset="categories",
            sequence=0,
            target_model="product.category",
            dependencies=(),
            existing_policy="update",
            identity_fields=("name",),
            scope_fields=("parent_id",),
        )
        rows = tuple(
            ExecutionRow(
                row_id=f"row:{index}",
                dataset="categories",
                source_row=index + 2,
                source_trace_id=f"source:{index}",
                source_identity=(f"ROW{index}",),
                target_model="product.category",
                business_identity=("Child",),
                business_scope=(
                    BusinessReference(
                        "product.category",
                        (parent,),
                        (None,),
                        target_fields=("name",),
                        target_scope_fields=("parent_id",),
                    ),
                ),
                disposition="CREATE",
                target_match_count=0,
                proposed_external_id="",
                fields=(),
            )
            for index, parent in enumerate(parents)
        )
        return NestedIdentityLookup(
            rows, {"categories": dataset}, lookup, page_size=page_size
        )

    def test_many_children_share_one_exact_ancestor_read(self):
        calls = []

        def lookup(model, domains):
            calls.append((model, domains))
            self.assertEqual(
                domains, ((("name", "=", "Root"), ("parent_id", "=", None)),)
            )
            return ((101,),)

        planner = self._planner(lookup)
        domains = tuple(
            (
                ("name", "=", f"Child {index}"),
                ("parent_id.name", "=", "Root"),
                ("parent_id.parent_id", "=", None),
            )
            for index in range(1000)
        )
        result = planner.translate("product.category", domains)
        self.assertEqual(len(result), 1000)
        self.assertTrue(all(domain[-1] == ("parent_id", "=", 101) for domain in result))
        self.assertEqual(planner.translate("product.category", domains), result)
        self.assertEqual(len(calls), 1)

    def test_missing_and_ambiguous_ancestors_fail_before_a_child_lookup(self):
        for identifiers in ((), (101, 102)):
            with self.subTest(identifiers=identifiers):
                planner = self._planner(lambda model, domains, matched=identifiers: (matched,))
                with self.assertRaisesRegex(WorkspaceError, "missing or ambiguous"):
                    planner.translate(
                        "product.category",
                        (
                            (
                                ("parent_id.name", "=", "Root"),
                                ("parent_id.parent_id", "=", None),
                            ),
                        ),
                    )

    def test_unknown_relation_path_is_not_an_extra_read_capability(self):
        def forbidden(*args):
            self.fail("An unreviewed path reached the Odoo port")

        with self.assertRaisesRegex(WorkspaceError, "outside the reviewed"):
            self._planner(forbidden).translate(
                "product.category", ((("unreviewed.name", "=", "Root"),),)
            )

    def test_distinct_ancestors_use_bounded_model_pages(self):
        calls = []

        def lookup(model, domains):
            calls.append((model, len(domains)))
            self.assertLessEqual(len(domains), 2)
            self.assertTrue(
                all("." not in field for domain in domains for field, _, _ in domain)
            )
            return tuple((101 + index,) for index, _ in enumerate(domains))

        parents = tuple(f"Root {index}" for index in range(5))
        planner = self._planner(lookup, parents=parents, page_size=2)
        result = planner.translate(
            "product.category",
            tuple(
                (("parent_id.name", "=", parent), ("parent_id.parent_id", "=", None))
                for parent in parents
            ),
        )
        self.assertEqual(len(result), 5)
        self.assertEqual([count for _, count in calls], [2, 2, 1])
