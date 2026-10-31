"""Business outcomes and durable dependency checks shared by hierarchy journeys."""

from impodo.domain.shared.models import BusinessReference
from tests.support.hierarchy_cases import (
    EXPECTED_CATEGORY_PATHS,
    EXPECTED_PRODUCT_PATHS,
)


def business_path(identity, scope):
    """Explain a nested portable business identity without local source keys."""

    parent = scope[0] if scope else None
    if parent is None:
        return identity
    assert isinstance(parent, BusinessReference)
    return (*business_path(parent.key, parent.scope), *identity)


def assert_prepared_hierarchy(fixture, workspace_id, staging, quality):
    fixture.assertEqual(
        (quality.ready_count, quality.quarantined_count, quality.blocked_count),
        (18, 0, 0),
    )
    categories = [row for row in staging.rows if row.target_model == "product.category"]
    fixture.assertEqual(len(categories), 12)
    roots = [row for row in categories if row.target_scope == (None,)]
    fixture.assertEqual(
        {row.target_identity for row in roots},
        {("Machines",), ("Maintenance",), ("Tools",)},
    )
    seals = [row for row in categories if row.target_identity == ("Seals",)]
    fixture.assertEqual(len(seals), 3)
    fixture.assertEqual(len({str(row.target_scope) for row in seals}), 3)
    physical = fixture.context.queries.get_source_selection(workspace_id)
    expected = {
        (dataset.dataset_id, ordinal)
        for dataset in physical.datasets
        for ordinal in range(2, dataset.row_count + 2)
    }
    actual = {
        (dataset_id, ordinal)
        for row in staging.rows
        for dataset_id, ordinals in row.lineage.physical_sources.items()
        for ordinal in ordinals
    }
    fixture.assertEqual(actual, expected)
    fixture.assertTrue(all(item.unrepresented_rows == 0 for item in staging.datasets))
    fixture.assertEqual(sum(item.output_rows for item in staging.datasets), 18)


def assert_frozen_hierarchy(fixture, snapshot):
    fixture.assertEqual(snapshot.relationship_plan.completions, ())
    fixture.assertEqual(
        snapshot.counts,
        {"CREATE": 13, "UNCHANGED": 5, "UPDATE": 0, "BLOCKED": 0, "AMBIGUOUS": 0},
    )
    categories = {
        row.row_id: row
        for row in snapshot.rows
        if row.target_model == "product.category"
    }
    fixture.assertEqual(
        {
            business_path(row.business_identity, row.business_scope)
            for row in categories.values()
        },
        EXPECTED_CATEGORY_PATHS,
    )
    by_id = {row.row_id: row for row in snapshot.rows}
    for row in snapshot.rows:
        for field in row.fields:
            for parent_id in field.dependency_row_ids:
                parent = by_id[parent_id]
                if parent.disposition == "CREATE":
                    fixture.assertLess(parent.schedule_ordinal, row.schedule_ordinal)


def require_parent_receipts(fixture, workspace_id, target):
    """Observe the saved journal before each target row, independently of the writer."""

    existing = set(target.records)
    checked = []

    def before_row(model, values):
        for field, related_model in (
            ("parent_id", "product.category"),
            ("categ_id", "product.category"),
        ):
            parent_id = values.get(field)
            if not parent_id or (related_model, parent_id) in existing:
                continue
            run = fixture.context.execution.journal.get_current_run(workspace_id)
            fixture.assertIsNotNone(run)
            fixture.assertIn(
                parent_id,
                {
                    row.odoo_id
                    for row in run.rows
                    if row.target_model == related_model
                    and row.status.value == "COMMITTED"
                },
                (model, field, parent_id),
            )
            checked.append((model, field, parent_id))

    target.before_row = before_row
    return checked


def assert_loaded_hierarchy(fixture, target):
    paths = target.category_paths()
    fixture.assertEqual(set(paths.values()), EXPECTED_CATEGORY_PATHS)
    fixture.assertEqual(len(paths), 12)
    products = {
        values["default_code"]: paths[values["categ_id"]]
        for (model, _), values in target.records.items()
        if model == "product.template"
    }
    fixture.assertEqual(products, EXPECTED_PRODUCT_PATHS)
    fixture.assertEqual(
        sum(len(identifiers) for _, _, identifiers in target.writes), 13
    )
