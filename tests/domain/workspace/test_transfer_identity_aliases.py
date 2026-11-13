"""Reviewed reference aliases preserve source evidence and reject coalescing."""

from __future__ import annotations

import unittest
from dataclasses import replace

from impodo.domain.mapping.contracts import MAX_VALUE_MAPPINGS, ValueMapping
from impodo.domain.serialization import canonical_json
from impodo.domain.workspace.transfer_identity_aliases import (
    normalize_transfer_identity_aliases,
    resolve_transfer_identity_aliases,
    transfer_identity_aliases_from_dict,
    transfer_identity_aliases_to_dict,
)


class TransferIdentityAliasTests(unittest.TestCase):
    def resolve(self, rows, *, aliases=(), handling="reference_only", fields=("name",)):
        return resolve_transfer_identity_aliases(
            rows, key_fields=fields, destination_handling=handling, aliases=aliases
        )

    def test_generic_reference_aliases_preserve_source_components_and_row_order(self):
        for handling in ("reference_only", "reuse_only"):
            for field in ("name", "x_reference"):
                with self.subTest(handling=handling, field=field):
                    rows = [("North",), ("South",)]
                    result = self.resolve(
                        rows,
                        aliases=(ValueMapping("North", "Test North"),),
                        handling=handling,
                        fields=(field,),
                    )
                    self.assertEqual(rows, [("North",), ("South",)])
                    self.assertEqual(result.source_identities, tuple(rows))
                    self.assertEqual(
                        result.destination_identities, (("Test North",), ("South",))
                    )
                    self.assertEqual(result.binding.alias_count, 1)

    def test_public_binding_contains_hashes_and_never_raw_choices(self):
        result = self.resolve(
            (("Private source",),),
            aliases=(ValueMapping("Private source", "Private destination"),),
        )
        encoded = canonical_json(result.binding.to_dict())
        self.assertNotIn("Private source", encoded)
        self.assertNotIn("Private destination", encoded)
        self.assertNotIn("source_value", encoded)
        self.assertTrue(result.binding.content_hash.startswith("sha256:"))

    def test_choices_normalize_like_existing_relationship_value_mappings(self):
        result = normalize_transfer_identity_aliases(
            (
                ValueMapping(" South ", " Test South "),
                ValueMapping("North", "Test North"),
            )
        )
        self.assertEqual(
            result,
            (ValueMapping("North", "Test North"), ValueMapping("South", "Test South")),
        )

    def test_alias_order_does_not_change_binding_but_population_order_does(self):
        rows = (("North",), ("South",))
        aliases = (ValueMapping("North", "A"), ValueMapping("South", "B"))
        first = self.resolve(rows, aliases=aliases)
        self.assertEqual(
            first.binding, self.resolve(rows, aliases=aliases[::-1]).binding
        )
        reordered = self.resolve(rows[::-1], aliases=aliases)
        self.assertNotEqual(
            first.binding.source_population_hash,
            reordered.binding.source_population_hash,
        )
        self.assertNotEqual(
            first.binding.destination_population_hash,
            reordered.binding.destination_population_hash,
        )

    def test_alias_change_invalidates_binding_without_changing_source_hash(self):
        first = self.resolve((("North",),), aliases=(ValueMapping("North", "A"),))
        changed = self.resolve((("North",),), aliases=(ValueMapping("North", "B"),))
        self.assertEqual(
            first.binding.source_population_hash, changed.binding.source_population_hash
        )
        self.assertNotEqual(first.binding.content_hash, changed.binding.content_hash)

    def test_aliases_reject_write_capable_handling(self):
        for handling in ("transfer", "reuse_or_create", "unknown"):
            with (
                self.subTest(handling=handling),
                self.assertRaisesRegex(ValueError, "existing-record-only"),
            ):
                self.resolve(
                    (("North",),),
                    aliases=(ValueMapping("North", "A"),),
                    handling=handling,
                )

    def test_aliases_reject_composite_matching_fields(self):
        with self.assertRaisesRegex(ValueError, "one scalar key"):
            self.resolve(
                (("North", "GB"),),
                aliases=(ValueMapping("North", "A"),),
                fields=("name", "code"),
            )

    def test_alias_sources_must_be_unique_and_inside_included_scope(self):
        cases = (
            ((("North",), ("North",)), (ValueMapping("North", "A"),)),
            ((("North",),), (ValueMapping("Missing", "A"),)),
            ((), (ValueMapping("North", "A"),)),
        )
        for rows, aliases in cases:
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                self.resolve(rows, aliases=aliases)

    def test_duplicate_rules_and_whitespace_disguised_duplicates_are_rejected(self):
        for source in ("North", " North "):
            with (
                self.subTest(source=source),
                self.assertRaisesRegex(ValueError, "only once"),
            ):
                self.resolve(
                    (("North",),),
                    aliases=(ValueMapping("North", "A"), ValueMapping(source, "B")),
                )

    def test_target_collisions_include_unmapped_source_records(self):
        for aliases in (
            (ValueMapping("North", "Shared"), ValueMapping("South", "Shared")),
            (ValueMapping("North", "South"),),
        ):
            with (
                self.subTest(aliases=aliases),
                self.assertRaisesRegex(ValueError, "merge distinct"),
            ):
                self.resolve((("North",), ("South",)), aliases=aliases)

    def test_blank_and_noncanonical_source_values_are_rejected_with_aliases(self):
        for value in ("", " ", " South "):
            with (
                self.subTest(value=value),
                self.assertRaisesRegex(ValueError, "nonblank portable"),
            ):
                self.resolve(
                    (("North",), (value,)), aliases=(ValueMapping("North", "A"),)
                )

    def test_empty_aliases_preserve_existing_matching_issues_and_composite_keys(self):
        for rows, fields in (
            ((("",), ("North",), ("North",)), ("name",)),
            ((("North", "GB"), ("North", "US")), ("name", "code")),
        ):
            with self.subTest(fields=fields):
                result = self.resolve(rows, fields=fields, handling="transfer")
                self.assertEqual(result.source_identities, rows)
                self.assertEqual(result.destination_identities, rows)
                self.assertEqual(result.binding.alias_count, 0)

    def test_protected_payload_round_trips_and_rejects_unknown_shapes(self):
        aliases = (ValueMapping("North", "A"),)
        payload = transfer_identity_aliases_to_dict(aliases)
        self.assertEqual(transfer_identity_aliases_from_dict(payload), aliases)
        malformed = (
            {**payload, "record_id": 123},
            {**payload, "contract_version": True},
            {**payload, "contract_version": 2},
            {**payload, "aliases": [{"source_value": "North", "target_value": 123}]},
            {
                **payload,
                "aliases": [{"source_value": "North", "target_value": "A", "id": 123}],
            },
        )
        for item in malformed:
            with self.subTest(payload=item), self.assertRaises((TypeError, ValueError)):
                transfer_identity_aliases_from_dict(item)

    def test_existing_choice_limits_and_blank_rules_are_enforced(self):
        for aliases in (
            (ValueMapping(" ", "A"),),
            (ValueMapping("North", " "),),
            tuple(
                ValueMapping(str(index), "A") for index in range(MAX_VALUE_MAPPINGS + 1)
            ),
        ):
            with self.subTest(count=len(aliases)), self.assertRaises(ValueError):
                normalize_transfer_identity_aliases(aliases)

    def test_binding_rejects_invalid_hashes_and_aliased_write_handling(self):
        binding = self.resolve(
            (("North",),), aliases=(ValueMapping("North", "A"),)
        ).binding
        with self.assertRaises(ValueError):
            replace(binding, aliases_hash="North -> A")
        with self.assertRaises(ValueError):
            replace(binding, destination_handling="transfer")
        with self.assertRaises(ValueError):
            replace(binding, contract_version=True)


if __name__ == "__main__":
    unittest.main()
