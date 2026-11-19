"""Compare Contact websites without treating Odoo's default scheme as a change."""

import unittest
from itertools import product

from impodo.domain.compiler.contracts import CompiledMigrationPlan
from impodo.domain.odoo.contracts import MetadataSnapshot, RecordSnapshot
from impodo.domain.odoo.website import matches_default_contact_website
from impodo.domain.preparation.preflight import PreflightEngine
from impodo.domain.preparation.source import PreparedBundle
from impodo.domain.recipe.profile import (
    DatasetSpec,
    FieldSpec,
    IdentityComponent,
    SourceIdentitySpec,
    SourceSpec,
    TargetIdentitySpec,
    TargetSpec,
)
from impodo.domain.shared.models import (
    Classification,
    FieldMetadata,
    ModelMetadata,
    PreparedRecord,
    TargetFingerprint,
    TargetRecord,
)

MODEL = "res.partner"
HASH = "sha256:" + "1" * 64


class ContactWebsiteComparisonTests(unittest.TestCase):
    def test_exact_default_scheme_preserves_other_url_differences(self):
        for prepared, observed, matches in (
            ("www.example.test", "http://www.example.test", True),
            ("//example.test/page", "http://example.test/page", True),
            ("www.example.test/page?q=a#b", "http://www.example.test/page?q=a#b", True),
            ("www.example.test", "https://www.example.test", False),
            ("https://example.test", "http://example.test", False),
            ("http://example.test", "http://http://example.test", False),
            ("www.example.test", "http://other.example.test", False),
            ("example.test/page", "http://example.test/other", False),
            ("example.test?q=a", "http://example.test?q=b", False),
            ("example.test#one", "http://example.test#two", False),
            ("", "http://", False),
            ("/page", "http:///page", False),
            ("[invalid", "http://[invalid", False),
        ):
            with self.subTest(prepared=prepared, observed=observed):
                self.assertEqual(
                    matches_default_contact_website(
                        MODEL, "website", prepared, observed
                    ),
                    matches,
                )
        for model, field in ((MODEL, "comment"), ("x.other", "website")):
            with self.subTest(model=model, field=field):
                self.assertFalse(
                    matches_default_contact_website(
                        model,
                        field,
                        "example.test",
                        "http://example.test",
                    )
                )

    def test_preflight_keeps_only_material_website_changes_on_both_majors(self):
        for major, mode, origin in product(
            (19, 20), ("LOCAL", "REMOTE"), ("profile_document", "browser_mapping")
        ):
            with self.subTest(major=major, mode=mode, origin=origin):
                plan = CompiledMigrationPlan(
                    plan_id="contact_websites",
                    origin=origin,
                    origin_hash=HASH,
                    schema_hash=HASH if origin == "browser_mapping" else None,
                    source_selection_hash=HASH if origin == "browser_mapping" else None,
                    contract_version=6,
                    compiler_version=7,
                    datasets=(
                        DatasetSpec(
                            name="contacts",
                            source=SourceSpec(file="contacts.csv"),
                            source_identity=SourceIdentitySpec(fields=("ref",)),
                            target=TargetSpec(model=MODEL),
                            target_identity=TargetIdentitySpec(
                                components=(
                                    IdentityComponent(
                                        source_fields=("ref",),
                                        target_fields=("ref",),
                                        type="string",
                                    ),
                                )
                            ),
                            fields={
                                "website": FieldSpec(source="website", type="string")
                            },
                        ),
                    ),
                )
                fingerprint = TargetFingerprint(
                    HASH, mode, "fictional", f"{major}.0", "2026-11-19T00:00:00Z"
                )
                metadata = MetadataSnapshot(
                    fingerprint,
                    {
                        MODEL: ModelMetadata(
                            MODEL,
                            "Contact",
                            {
                                "ref": FieldMetadata("ref", "char"),
                                "website": FieldMetadata("website", "char"),
                            },
                        )
                    },
                )
                prepared = []
                targets = []
                for row, source, target in (
                    (2, "www.example.test", "http://www.example.test"),
                    (3, "https://www.example.test", "http://www.example.test"),
                ):
                    ref = str(row)
                    prepared.append(
                        PreparedRecord(
                            dataset="contacts",
                            source_row=row,
                            target_model=MODEL,
                            source_identity=(ref,),
                            target_identity=(ref,),
                            target_scope=(),
                            scalar_values={"website": source},
                            references={},
                        )
                    )
                    targets.append(
                        TargetRecord(MODEL, row, {"ref": ref, "website": target})
                    )
                result = PreflightEngine().run(
                    plan,
                    PreparedBundle(tuple(prepared), {}, ()),
                    metadata,
                    RecordSnapshot(
                        fingerprint,
                        {MODEL: tuple(targets)},
                        {MODEL: ("ref", "website")},
                    ),
                )
                self.assertEqual(
                    [item.classification for item in result.decisions],
                    [Classification.UNCHANGED, Classification.UPDATE],
                )
                self.assertEqual(result.decisions[0].differences, ())
                self.assertEqual(len(result.decisions[1].differences), 1)
                self.assertEqual(
                    result.decisions[1].differences[0].proposed,
                    "https://www.example.test",
                )
