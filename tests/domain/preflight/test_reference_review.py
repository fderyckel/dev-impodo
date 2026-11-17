"""Show exact failed linked keys without guessing from issue text or row numbers."""

from dataclasses import replace
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from unittest import TestCase

from impodo.domain.preflight.reference_review import readiness_reference_details
from impodo.domain.recipe.profile import (
    DatasetSpec,
    IdentityComponent,
    ResolveSpec,
    SourceIdentitySpec,
    SourceSpec,
    TargetIdentitySpec,
    TargetSpec,
)
from impodo.domain.shared.models import (
    Classification,
    Decision,
    Issue,
    LogicalReference,
    PreparedRecord,
    ReferenceResolution,
)

ROW_ID = "sha256:" + "1" * 64


class ReferenceReviewTests(TestCase):
    def setUp(self):
        self.reference = LogicalReference(
            "target",
            ("000745",),
            model="x.partner",
            target_fields=("ref",),
            target_scope_fields=("company_id.name", "parent_id.ref"),
            scope=("Example Company", "P-01"),
        )
        self.dataset = DatasetSpec(
            name="notices",
            source=SourceSpec(file="notices.csv"),
            target=TargetSpec(model="x.notice"),
            source_identity=SourceIdentitySpec(fields=("code",)),
            target_identity=TargetIdentitySpec(
                components=(
                    IdentityComponent(source_fields=("code",), target_fields=("name",)),
                )
            ),
        )
        self.record = PreparedRecord(
            "notices",
            2,
            "x.notice",
            ("N-01",),
            ("N-01",),
            (),
            {},
            {"contact_id": self.reference},
            ROW_ID,
        )
        self.issue = Issue(
            "REFERENCE_NOT_FOUND", "Saved missing reference", field="contact_id"
        )
        self.decision = Decision(
            "notices",
            2,
            ("N-01",),
            (),
            Classification.BLOCKED,
            0,
            source_trace_id=ROW_ID,
            issues=(self.issue,),
        )
        self.schema = SimpleNamespace(
            models=(
                SimpleNamespace(
                    name="x.notice",
                    fields=(SimpleNamespace(name="contact_id", label="Contact"),),
                ),
                SimpleNamespace(
                    name="x.partner",
                    fields=(SimpleNamespace(name="ref", label="Reference"),),
                ),
            )
        )

    def details(self, *, record=None, decisions=None, resolutions=None, dataset=None):
        result = SimpleNamespace(
            decisions=decisions or (self.decision,),
            reference_resolutions=resolutions
            if resolutions is not None
            else (
                ReferenceResolution(
                    "notices", "contact_id", self.reference, "NOT_FOUND", 0
                ),
            ),
        )
        return readiness_reference_details(
            result,
            (record or self.record,),
            (dataset or self.dataset,),
            self.schema,
        )

    def test_missing_value_keeps_leading_zeroes_complete_scope_and_matching_fields(
        self,
    ):
        (detail,) = self.details()[ROW_ID]
        self.assertEqual(
            detail,
            (
                'Contact (contact_id): No related record matched. Looked for Reference (ref): "000745" '
                'in Odoo x.partner. Within company_id.name: "Example Company"; parent_id.ref: "P-01".'
            ),
        )

    def test_multi_value_field_only_identifies_the_failed_reference(self):
        matched = replace(self.reference, key=("FOUND-1",))
        record = replace(
            self.record, references={"contact_id": (matched, self.reference)}
        )
        details = self.details(record=record)[ROW_ID]
        self.assertEqual(len(details), 1)
        self.assertIn('"000745"', details[0])
        self.assertNotIn("FOUND-1", details[0])

    def test_identity_and_scope_failures_use_their_own_lookup_and_deduplicate_assignment(
        self,
    ):
        component = IdentityComponent(
            source_fields=("contact",),
            target_fields=("contact_id",),
            resolve=ResolveSpec(target_model="x.partner", target_fields=("ref",)),
        )
        for prefix, identity in (
            ("target_identity:", TargetIdentitySpec(components=(component,))),
            (
                "scope:",
                TargetIdentitySpec(
                    components=self.dataset.target_identity.components,
                    scope=(component,),
                ),
            ),
        ):
            with self.subTest(prefix=prefix):
                dataset = self.dataset.model_copy(update={"target_identity": identity})
                record = replace(
                    self.record,
                    target_identity=(self.reference,)
                    if prefix == "target_identity:"
                    else ("N-01",),
                    target_scope=(self.reference,) if prefix == "scope:" else (),
                )
                field = prefix + "contact_id"
                decision = replace(
                    self.decision, issues=(replace(self.issue, field=field), self.issue)
                )
                details = self.details(
                    record=record,
                    dataset=dataset,
                    decisions=(decision,),
                    resolutions=(
                        ReferenceResolution(
                            "notices", field, self.reference, "NOT_FOUND", 0
                        ),
                        ReferenceResolution(
                            "notices", "contact_id", self.reference, "NOT_FOUND", 0
                        ),
                    ),
                )[ROW_ID]
                self.assertEqual(len(details), 1)
                self.assertIn('"000745"', details[0])

    def test_composite_key_retains_component_boundaries_and_non_text_values(self):
        reference = replace(
            self.reference,
            key=("A / B", Decimal("12.50"), date(2026, 1, 2)),
            target_fields=("code", "rate", "day"),
        )
        (detail,) = self.details(
            record=replace(self.record, references={"contact_id": reference}),
            resolutions=(
                ReferenceResolution("notices", "contact_id", reference, "NOT_FOUND", 0),
            ),
        )[ROW_ID]
        self.assertIn('code: "A / B"; rate: 12.50; day: 2026-01-02', detail)

    def test_incoming_and_hybrid_paths_identify_the_table_and_key_actually_checked(
        self,
    ):
        for origin in ("incoming", "target_then_incoming"):
            with self.subTest(origin=origin):
                reference = replace(
                    self.reference,
                    origin=origin,
                    dataset="contacts",
                    incoming_key=("SOURCE-7",),
                )
                (detail,) = self.details(
                    record=replace(self.record, references={"contact_id": reference}),
                    resolutions=(
                        ReferenceResolution(
                            "notices", "contact_id", reference, "NOT_FOUND", 0
                        ),
                    ),
                )[ROW_ID]
                self.assertIn("incoming table contacts", detail)
                if origin == "target_then_incoming":
                    self.assertIn('Key 1: "SOURCE-7"', detail)

    def test_secondary_missing_fields_and_ambiguity_remain_visible(self):
        other = replace(
            self.reference,
            model="x.region",
            key=("WEST",),
            scope=(),
            target_scope_fields=(),
        )
        decision = replace(
            self.decision,
            issues=(
                self.issue,
                Issue("REFERENCE_AMBIGUOUS", "Ambiguous", field="region_id"),
            ),
        )
        details = self.details(
            record=replace(
                self.record,
                references={"contact_id": self.reference, "region_id": other},
            ),
            decisions=(decision,),
            resolutions=(
                ReferenceResolution(
                    "notices", "contact_id", self.reference, "NOT_FOUND", 0
                ),
                ReferenceResolution("notices", "region_id", other, "AMBIGUOUS", 2),
            ),
        )[ROW_ID]
        self.assertEqual(len(details), 2)
        self.assertIn('"WEST"', details[1])
        self.assertIn("Several related records matched", details[1])

    def test_unavailable_evidence_is_explicit_and_never_uses_another_row(self):
        other = replace(self.record, source_trace_id="sha256:" + "2" * 64)
        (detail,) = self.details(record=other)[ROW_ID]
        self.assertIn("Lookup details were not retained", detail)
        self.assertIn("Saved missing reference", detail)
        self.assertNotIn("000745", detail)
        (detail,) = self.details(resolutions=())[ROW_ID]
        self.assertIn("Lookup details were not retained", detail)
        self.assertNotIn("000745", detail)
