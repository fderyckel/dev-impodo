"""Exercise bounded, model-neutral parent lookup without a live Odoo host."""

import unittest
from dataclasses import replace

from impodo.application.hierarchy_parent_lookup import (
    HierarchyParentChoice,
    browse_parent_choices,
    hierarchy_lookup_shape,
    read_parent_choices,
    selected_parent_domain,
)
from impodo.domain.odoo.contracts import MetadataSnapshot, RecordSnapshot
from impodo.domain.shared.models import (
    FieldMetadata,
    ModelMetadata,
    TargetFingerprint,
    TargetRecord,
)
from impodo.domain.workspace.errors import WorkspaceError

MODEL = "x.folder"
HASH = "sha256:" + "2" * 64


def metadata(major=19):
    return MetadataSnapshot(
        TargetFingerprint(
            HASH, "REMOTE", "fictional", f"{major}.0", "2026-11-12T00:00:00Z"
        ),
        {
            MODEL: ModelMetadata(
                MODEL,
                "Folder",
                {
                    "title": FieldMetadata("title", "char"),
                    "container": FieldMetadata("container", "many2one", relation=MODEL),
                    "company_id": FieldMetadata(
                        "company_id", "many2one", relation="res.company"
                    ),
                    "active": FieldMetadata("active", "boolean"),
                },
            )
        },
    )


class Reader:
    def __init__(
        self, captured, *, count=1, cycle=False, incomplete=False, unique=False
    ):
        self.captured, self.count, self.cycle, self.incomplete = (
            captured,
            count,
            cycle,
            incomplete,
        )
        self.requests = []
        self.unique = unique

    def __call__(self, requests, scoped):
        request = requests[0]
        self.requests.append((request, scoped))
        if request.model == "res.company":
            rows = (TargetRecord("res.company", 7, {"name": "Example"}),)
        elif scoped:
            rows = tuple(
                TargetRecord(
                    MODEL,
                    number,
                    {
                        "title": f"Shelf / {number}" if self.unique else "Shelf / 1",
                        "container": 80,
                        "company_id": [7, "Untrusted display label"],
                        "active": True,
                    },
                )
                for number in range(1, self.count + 1)
            )
        else:
            rows = (
                TargetRecord(
                    MODEL,
                    80,
                    {
                        "title": "Physical",
                        "container": 80 if self.cycle else False,
                        "company_id": False,
                        "active": True,
                    },
                ),
            )
        return RecordSnapshot(
            self.captured.fingerprint,
            {request.model: rows},
            {request.model: request.fields},
            complete=not self.incomplete,
        )


class HierarchyParentLookupTests(unittest.TestCase):
    def test_browse_needs_no_name_and_reports_a_bounded_partial_list(self):
        for major in (19, 20):
            with self.subTest(major=major):
                captured = metadata(major)
                shape = hierarchy_lookup_shape(captured, MODEL, "title", "")
                reader = Reader(captured, count=51, unique=True)
                page = browse_parent_choices(shape, reader)
                self.assertTrue(page.has_more)
                self.assertEqual(len(page.choices), 50)
                self.assertEqual(len(reader.requests), 3)
                self.assertEqual(reader.requests[0][0].domain, (("active", "=", True),))
                self.assertEqual(reader.requests[0][0].limit, 51)
                self.assertNotIn(
                    "Shelf / 51", {choice.path[-1] for choice in page.choices}
                )
                # A picker page never establishes that a chosen path is unique.
                # The exact read must still detect duplicate records.
                with self.assertRaisesRegex(WorkspaceError, "ambiguous"):
                    read_parent_choices(
                        shape, Reader(captured, count=2), selected=page.choices[0].value
                    )

    def test_optional_filter_empty_result_and_invalid_filter(self):
        for major in (19, 20):
            with self.subTest(major=major):
                captured = metadata(major)
                shape = hierarchy_lookup_shape(captured, MODEL, "title", "")
                reader = Reader(captured, count=0)
                page = browse_parent_choices(shape, reader, search="  absent  ")
                self.assertEqual(page.choices, ())
                self.assertFalse(page.has_more)
                self.assertEqual(len(reader.requests), 1)
                self.assertEqual(
                    reader.requests[0][0].domain,
                    (("title", "ilike", "absent"), ("active", "=", True)),
                )
                with self.assertRaises(WorkspaceError):
                    browse_parent_choices(shape, reader, search="x" * 201)
                self.assertEqual(len(reader.requests), 1)

    def test_search_fetches_full_ancestry_and_company_keys_in_bounded_batches(self):
        for major in (19, 20):
            with self.subTest(major=major):
                captured = metadata(major)
                shape = hierarchy_lookup_shape(captured, MODEL, "title", "")
                reader = Reader(captured)
                choices = read_parent_choices(shape, reader, search="Shelf")
                self.assertEqual(
                    choices,
                    (
                        HierarchyParentChoice(
                            ("Physical", "Shelf / 1"), (None, "Example")
                        ),
                    ),
                )
                self.assertEqual(len(reader.requests), 3)
                self.assertEqual(
                    reader.requests[0][0].domain,
                    (("title", "ilike", "Shelf"), ("active", "=", True)),
                )
                self.assertEqual(reader.requests[0][0].limit, 51)
                self.assertEqual(reader.requests[1][0].domain, (("id", "in", (80,)),))
                self.assertNotIn("id", choices[0].value)
                domain = selected_parent_domain(choices[0].value, shape)
                self.assertIn(("container.title", "=", "Physical"), domain)
                self.assertIn(("container.container", "=", False), domain)
                self.assertIn(("company_id.name", "=", "Example"), domain)
                self.assertIn(("container.company_id", "=", False), domain)

    def test_duplicate_complete_paths_are_never_arbitrarily_selected(self):
        captured = metadata()
        with self.assertRaisesRegex(WorkspaceError, "same full path"):
            read_parent_choices(
                hierarchy_lookup_shape(captured, MODEL, "title", ""),
                Reader(captured, count=2),
                search="Shelf",
            )

    def test_truncation_requires_a_narrower_search(self):
        captured = metadata()
        reader = Reader(captured, count=51)
        with self.assertRaisesRegex(WorkspaceError, "specific name"):
            read_parent_choices(
                hierarchy_lookup_shape(captured, MODEL, "title", ""),
                reader,
                search="Shelf",
            )
        self.assertEqual(len(reader.requests), 1)

    def test_incomplete_results_and_cycles_cannot_be_saved_as_parents(self):
        captured = metadata()
        shape = hierarchy_lookup_shape(captured, MODEL, "title", "")
        for options in ({"cycle": True}, {"incomplete": True}):
            with self.subTest(options=options), self.assertRaises(WorkspaceError):
                read_parent_choices(shape, Reader(captured, **options), search="Shelf")

    def test_selected_path_is_rechecked_with_exact_company_and_ancestry(self):
        captured = metadata()
        shape = hierarchy_lookup_shape(captured, MODEL, "title", "")
        selected = HierarchyParentChoice(("Physical", "Shelf / 1"), (None, "Example"))
        reader = Reader(captured)
        self.assertEqual(
            read_parent_choices(shape, reader, selected=selected.value), (selected,)
        )
        self.assertIn(("title", "=", "Shelf / 1"), reader.requests[0][0].domain)

    def test_unknown_or_ambiguous_relationship_requires_an_explicit_choice(self):
        captured = metadata()
        model = captured.models[MODEL]
        captured = replace(
            captured,
            models={
                MODEL: replace(
                    model,
                    fields={
                        **model.fields,
                        "other_parent": FieldMetadata(
                            "other_parent", "many2one", relation=MODEL
                        ),
                    },
                )
            },
        )
        with self.assertRaisesRegex(WorkspaceError, "parent relationship"):
            hierarchy_lookup_shape(captured, MODEL, "title", "")
        self.assertEqual(
            hierarchy_lookup_shape(captured, MODEL, "title", "container").parent_field,
            "container",
        )
