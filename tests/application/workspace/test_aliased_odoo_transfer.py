"""Keep reviewed reference aliases protected and separate from source identity."""

from dataclasses import replace
from unittest import TestCase

from impodo.application.transfer_execution_service import (
    compile_transfer_execution_snapshot,
)
from impodo.application.transfer_preflight_service import TransferPreflightService
from impodo.domain.mapping.contracts import ValueMapping
from impodo.domain.preparation.source import SourceRow
from impodo.domain.shared.access import LOCAL_ACTOR
from impodo.domain.shared.models import (
    BusinessReference,
    TargetRecord,
    target_record_binding_hash,
)
from impodo.domain.workspace.destination_matching import DestinationMatchPlan
from impodo.domain.workspace.errors import WorkspaceError
from tests.application.workspace.test_scoped_odoo_transfer import ScopedTransferFixture


class AliasedTransferFixture(ScopedTransferFixture):
    """Expose one reviewed no-write alias journey for application and route tests."""

    def __init__(self, major=19, mode="REMOTE", *, scoped=False):
        super().__init__(major, mode)
        self.choices = (
            replace(self.choices[0], scope_fields=("parent_id",) if scoped else ()),
            replace(
                self.choices[1],
                destination_handling="reuse_only",
                identity_aliases=(
                    ValueMapping("Parent A", "Reviewed parent A"),
                    ValueMapping("Parent B", "Reviewed parent B"),
                ),
            ),
        )
        self.targets["x.scope_parent"] = (
            TargetRecord("x.scope_parent", 901, {"name": "Reviewed parent A"}),
            TargetRecord("x.scope_parent", 902, {"name": "Reviewed parent B"}),
        )
        if not scoped:
            self.rows[self.child.dataset_id] = (
                SourceRow(1, {"default_code": "FIRST", "name": "First"}),
                SourceRow(2, {"default_code": "SECOND", "name": "Second"}),
            )
            self.targets["x.scope_record"] = (
                TargetRecord(
                    "x.scope_record", 1001, {"default_code": "FIRST", "parent_id": 901}
                ),
            )


class AliasedOdooTransferTests(TestCase):
    def test_many_to_many_alias_links_keep_exact_reviewed_members_and_bindings(self):
        for major in (19, 20):
            for mode in ("LOCAL", "REMOTE"):
                with self.subTest(major=major, mode=mode):
                    fixture = AliasedTransferFixture(major, mode)
                    child, parent = fixture.schema.models
                    fixture.schema = replace(
                        fixture.schema,
                        models=(
                            replace(
                                child,
                                fields=tuple(
                                    replace(field, name="parent_ids", type="many2many")
                                    if field.name == "parent_id"
                                    else field
                                    for field in child.fields
                                ),
                            ),
                            parent,
                        ),
                    )
                    metadata = fixture.metadata.models[child.name]
                    fixture.metadata = replace(
                        fixture.metadata,
                        models={
                            **fixture.metadata.models,
                            child.name: replace(
                                metadata,
                                fields={
                                    **{
                                        name: field
                                        for name, field in metadata.fields.items()
                                        if name != "parent_id"
                                    },
                                    "parent_ids": replace(
                                        metadata.fields["parent_id"],
                                        name="parent_ids",
                                        type="many2many",
                                    ),
                                },
                            ),
                        },
                    )
                    origin = fixture.origins[fixture.child.dataset_id][0]
                    fixture.origins[fixture.child.dataset_id] = (
                        replace(
                            origin,
                            relationships=(
                                replace(
                                    origin.relationships[0],
                                    field_name="parent_ids",
                                    kind="many2many",
                                    values=((7, 8), (8,)),
                                ),
                            ),
                        ),
                    )
                    snapshot = fixture.compile()
                    links = tuple(
                        intent
                        for row in snapshot.rows
                        if row.target_model == child.name
                        for intent in row.fields
                        if intent.field == "parent_ids"
                    )
                    first = BusinessReference(parent.name, ("Reviewed parent A",))
                    second = BusinessReference(parent.name, ("Reviewed parent B",))
                    self.assertEqual(
                        tuple(intent.value for intent in links),
                        ((first, second), (second,)),
                    )
                    self.assertEqual(
                        tuple(intent.relation_operation for intent in links),
                        ("replace", "replace"),
                    )
                    first_binding = target_record_binding_hash(parent.name, 901)
                    second_binding = target_record_binding_hash(parent.name, 902)
                    self.assertEqual(
                        tuple(intent.target_binding_hashes for intent in links),
                        ((first_binding, second_binding), (second_binding,)),
                    )
                    self.assertTrue(
                        all(
                            row.disposition == "UNCHANGED" and not row.fields
                            for row in snapshot.rows
                            if row.target_model == parent.name
                        )
                    )

    def test_aliases_reuse_exact_destination_records_and_preserve_source_values(self):
        for major in (19, 20):
            for mode in ("LOCAL", "REMOTE"):
                with self.subTest(major=major, mode=mode):
                    fixture = AliasedTransferFixture(major, mode)
                    original_rows = fixture.rows.copy()
                    snapshot = fixture.compile()
                    self.assertEqual(fixture.rows, original_rows)
                    parent = next(
                        item
                        for item in fixture.match_plan.model_matches
                        if item.model == "x.scope_parent"
                    )
                    self.assertEqual(
                        (
                            parent.destination_existing_key_count,
                            parent.destination_create_key_count,
                        ),
                        (2, 0),
                    )
                    self.assertEqual(len(fixture.match_plan.identity_alias_bindings), 1)
                    public = fixture.match_plan.to_json()
                    self.assertNotIn("Reviewed parent", public)
                    self.assertNotIn("Parent A", public)
                    restored = DestinationMatchPlan.from_json(public)
                    self.assertIsNone(restored.identity_alias_evidence)
                    self.assertEqual(
                        restored.identity_alias_bindings,
                        fixture.match_plan.identity_alias_bindings,
                    )
                    parents = tuple(
                        row
                        for row in snapshot.rows
                        if row.target_model == "x.scope_parent"
                    )
                    self.assertEqual(
                        tuple(row.source_identity for row in parents),
                        (("Parent A",), ("Parent B",)),
                    )
                    self.assertEqual(
                        tuple(row.business_identity for row in parents),
                        (("Reviewed parent A",), ("Reviewed parent B",)),
                    )
                    self.assertTrue(
                        all(
                            row.disposition == "UNCHANGED" and not row.fields
                            for row in parents
                        )
                    )
                    children = tuple(
                        row
                        for row in snapshot.rows
                        if row.target_model == "x.scope_record"
                    )
                    links = tuple(
                        intent.value
                        for row in children
                        for intent in row.fields
                        if intent.field == "parent_id"
                    )
                    self.assertEqual(
                        links,
                        (
                            BusinessReference("x.scope_parent", ("Reviewed parent A",)),
                            BusinessReference("x.scope_parent", ("Reviewed parent B",)),
                        ),
                    )
                    reviewed = next(
                        item
                        for item in fixture.package.datasets
                        if item.model == "x.scope_parent"
                    )
                    self.assertEqual(reviewed.identity_alias_count, 2)
                    self.assertTrue(
                        reviewed.identity_alias_binding_hash.startswith("sha256:")
                    )

    def test_alias_target_must_exist_uniquely_and_cannot_fall_back_to_original_name(
        self,
    ):
        for rows in (
            (TargetRecord("x.scope_parent", 901, {"name": "Parent A"}),),
            (
                TargetRecord("x.scope_parent", 901, {"name": "Reviewed parent A"}),
                TargetRecord("x.scope_parent", 903, {"name": "Reviewed parent A"}),
            ),
        ):
            fixture = AliasedTransferFixture()
            fixture.targets["x.scope_parent"] = rows
            with self.assertRaisesRegex(
                WorkspaceError, "alias must resolve to exactly one"
            ):
                fixture.match()
            self.assertEqual(fixture.captured, [])

    def test_aliases_cannot_write_merge_or_name_an_uncaptured_source(self):
        cases = (
            ("transfer", (ValueMapping("Parent A", "Reviewed parent A"),)),
            ("reuse_only", (ValueMapping("Parent A", "Parent B"),)),
            ("reuse_only", (ValueMapping("Not captured", "Reviewed parent A"),)),
        )
        for handling, aliases in cases:
            fixture = AliasedTransferFixture()
            fixture.choices = (
                fixture.choices[0],
                replace(
                    fixture.choices[1],
                    destination_handling=handling,
                    identity_aliases=aliases,
                ),
            )
            with self.assertRaises(WorkspaceError):
                fixture.match()
            self.assertEqual(fixture.calls, [])

    def test_changed_aliases_block_preflight_even_with_identical_record_counts(self):
        fixture = AliasedTransferFixture()
        fixture.approve()
        prior = fixture.match_plan
        fixture.choices = (
            fixture.choices[0],
            replace(
                fixture.choices[1],
                identity_aliases=(
                    ValueMapping("Parent A", "Reviewed parent B"),
                    ValueMapping("Parent B", "Reviewed parent A"),
                ),
            ),
        )
        fresh = fixture.match()
        report = TransferPreflightService().build(
            fixture.approved,
            fixture.package,
            fixture.approval,
            prior,
            fresh,
            recorded_by=LOCAL_ACTOR.identity,
        )
        self.assertFalse(report.ready)
        self.assertTrue(
            any(
                "DESTINATION_IDENTITY_ALIAS_DRIFT" in item.blocker_codes
                for item in report.datasets
            )
        )

    def test_compilation_requires_protected_alias_choices_and_rechecks_source_population(
        self,
    ):
        fixture = AliasedTransferFixture()
        fixture.approve()
        from types import SimpleNamespace

        def compile_match(match):
            return compile_transfer_execution_snapshot(
                fixture.workspace,
                fixture.selection,
                fixture.schema,
                fixture.package,
                fixture.report,
                match,
                fixture.records,
                source_rows=fixture.rows,
                source_origins=fixture.origins,
                source_snapshots={
                    item.dataset_id: SimpleNamespace(content_hash="sha256:" + "7" * 64)
                    for item in fixture.selection.datasets
                },
                source_manifest_hashes={
                    item.dataset_id: "sha256:" + "7" * 64
                    for item in fixture.selection.datasets
                },
            )

        with self.assertRaisesRegex(
            WorkspaceError, "protected reviewed identity aliases"
        ):
            compile_match(replace(fixture.match_plan, identity_alias_evidence=None))
        fixture.rows[fixture.parent.dataset_id] = (
            SourceRow(1, {"name": "Changed source"}),
            fixture.rows[fixture.parent.dataset_id][1],
        )
        with self.assertRaisesRegex(
            WorkspaceError, "included source scope|source population"
        ):
            compile_match(fixture.match_plan)
