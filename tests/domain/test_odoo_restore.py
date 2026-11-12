"""Verify portable schema adaptation and field-policy restore decisions."""

from __future__ import annotations

import unittest
from dataclasses import replace
from decimal import Decimal

from impodo.domain.odoo_restore import (
    RestoreCoverageKind,
    RestoreCreateDefault,
    RestoreFieldRule,
    RestoreLinkPolicy,
    RestoreValuePolicy,
    apply_odoo_restore,
    compile_odoo_restore,
)
from impodo.domain.shared.models import BusinessReference
from impodo.domain.workspace.contracts import SchemaField, SchemaModel


def _field(name: str, field_type: str = "char", **changes) -> SchemaField:
    return replace(
        SchemaField(
            name=name,
            label=name,
            type=field_type,
            required=False,
            readonly=False,
            relation=None,
            relation_field=None,
            selection=(),
            stored=True,
            computed=False,
            has_inverse=False,
            related=False,
            translated=False,
            company_dependent=False,
            exportable=True,
        ),
        **changes,
    )


def _model(*fields: SchemaField, name: str = "x.fixture") -> SchemaModel:
    return SchemaModel(name, name, fields)


def _compile(
    source: SchemaModel,
    destination: SchemaModel | None = None,
    *,
    major: int = 20,
    **options,
):
    return compile_odoo_restore(
        source,
        destination or source,
        source_version=f"{major}.0",
        destination_version=f"{major}.0",
        **options,
    )


def _reference(code: str, model: str = "x.tag") -> BusinessReference:
    return BusinessReference(model, (code,), target_fields=("code",))


class OdooRestoreSchemaTests(unittest.TestCase):
    def test_same_major_compilation_is_generic_and_hashes_policies(self) -> None:
        for major in (19, 20):
            for name in ("account.account", "x.equipment"):
                with self.subTest(major=major, model=name):
                    source = _model(_field("code"), _field("name"), name=name)
                    plan = _compile(source, major=major, identity_fields=("code",))
                    self.assertTrue(plan.ready)
                    self.assertEqual(
                        plan.content_hash,
                        _compile(
                            source, major=major, identity_fields=("code",)
                        ).content_hash,
                    )
                    changed = _compile(
                        source,
                        major=major,
                        identity_fields=("code",),
                        rules=(
                            RestoreFieldRule(
                                "name", policy=RestoreValuePolicy.KEEP_DESTINATION
                            ),
                        ),
                    )
                    self.assertNotEqual(plan.content_hash, changed.content_hash)

    def test_cross_major_prerelease_and_unknown_versions_are_blocked(self) -> None:
        source = _model(_field("name"))
        for source_version, target_version in (
            ("19.0", "20.0"),
            ("20.0", "19.0"),
            ("20.0rc1", "20.0"),
            ("20.0", "21.0"),
            ("unknown", "20.0"),
        ):
            with self.subTest(source=source_version, target=target_version):
                plan = compile_odoo_restore(
                    source,
                    source,
                    source_version=source_version,
                    destination_version=target_version,
                )
                self.assertFalse(plan.ready)
                self.assertEqual(plan.issues[0].code, "RESTORE_VERSION_INCOMPATIBLE")

    def test_captured_fields_define_saved_population_not_the_source_schema(
        self,
    ) -> None:
        source = _model(_field("name"), _field("description"))
        plan = _compile(source, captured_fields=("name",))
        self.assertEqual([item.source.name for item in plan.fields], ["name"])
        result = apply_odoo_restore(
            plan, {"name": "Saved"}, {"name": "Current", "description": "Preserve"}
        )
        self.assertEqual(dict(result.writes), {"name": "Saved"})

    def test_removed_field_needs_an_explicit_exclusion_or_mapping(self) -> None:
        source = _model(_field("name"), _field("legacy_note"))
        target = _model(_field("name"), _field("note"))
        blocked = _compile(source, target)
        self.assertIn("RESTORE_FIELD_REMOVED", {issue.code for issue in blocked.issues})
        excluded = _compile(
            source, target, rules=(RestoreFieldRule("legacy_note", excluded=True),)
        )
        self.assertTrue(excluded.ready)
        self.assertIn(
            RestoreCoverageKind.EXCLUDED, {item.kind for item in excluded.coverage}
        )
        remapped = _compile(
            source,
            target,
            rules=(RestoreFieldRule("legacy_note", target_field="note"),),
        )
        self.assertTrue(remapped.ready)
        self.assertEqual(
            dict(
                apply_odoo_restore(
                    remapped, {"name": "A", "legacy_note": "Saved"}
                ).writes
            ),
            {"name": "A", "note": "Saved"},
        )

    def test_type_change_or_relationship_change_never_coerces(self) -> None:
        pairs = (
            (_field("value", "integer"), _field("value", "char")),
            (
                _field("value", "many2one", relation="x.a"),
                _field("value", "many2one", relation="x.b"),
            ),
        )
        for saved, target in pairs:
            with self.subTest(type=saved.type):
                plan = _compile(_model(saved), _model(target))
                self.assertIn(
                    "RESTORE_FIELD_INCOMPATIBLE", {issue.code for issue in plan.issues}
                )

    def test_company_dependent_and_readonly_coverage_is_explicit(self) -> None:
        for changes in (
            {"readonly": True},
            {"company_dependent": True},
            {"type": "binary"},
        ):
            with self.subTest(changes=changes):
                plan = _compile(
                    _model(_field("value")), _model(_field("value", **changes))
                )
                self.assertFalse(plan.ready)
                self.assertIn(
                    RestoreCoverageKind.NOT_WRITABLE
                    if "type" not in changes
                    else RestoreCoverageKind.INCOMPATIBLE,
                    {item.kind for item in plan.coverage},
                )

    def test_new_optional_fields_do_not_block_creates(self) -> None:
        source = _model(_field("name"))
        target = _model(_field("name"), _field("optional"))
        plan = _compile(source, target)
        self.assertTrue(plan.ready)
        self.assertEqual(
            dict(apply_odoo_restore(plan, {"name": "Saved"}).writes), {"name": "Saved"}
        )

    def test_new_required_fields_use_checked_native_or_explicit_defaults(self) -> None:
        source = _model(_field("name"))
        target = _model(_field("name"), _field("required", required=True))
        blocked = _compile(source, target)
        self.assertIn(
            "RESTORE_REQUIRED_VALUE_MISSING", {issue.code for issue in blocked.issues}
        )
        explicit = _compile(
            source, target, defaults=(RestoreCreateDefault("required", "Fixed"),)
        )
        result = apply_odoo_restore(explicit, {"name": "Saved"})
        self.assertTrue(result.ready)
        self.assertEqual(dict(result.writes)["required"], "Fixed")
        native = _compile(
            source,
            _model(
                _field("name"),
                _field(
                    "required",
                    required=True,
                    create_default_present=True,
                    create_default_value="Native",
                ),
            ),
        )
        result = apply_odoo_restore(native, {"name": "Saved"})
        self.assertTrue(result.ready)
        self.assertEqual(result.odoo_default_fields, ("required",))
        self.assertEqual(dict(result.expected_values)["required"], "Native")
        self.assertNotIn("required", dict(result.writes))

    def test_updates_do_not_need_creation_defaults_but_actual_creates_still_do(
        self,
    ) -> None:
        source = _model(_field("name"))
        target = _model(_field("name"), _field("required", required=True))
        plan = _compile(source, target, creates_expected=False)
        self.assertTrue(plan.ready)
        self.assertTrue(
            apply_odoo_restore(
                plan, {"name": "Saved"}, {"name": "Old", "required": "Current"}
            ).ready
        )
        self.assertFalse(apply_odoo_restore(plan, {"name": "Saved"}).ready)

    def test_required_empty_saved_values_block_all_row_writes(self) -> None:
        plan = _compile(_model(_field("name", required=True), _field("note")))
        result = apply_odoo_restore(plan, {"name": "", "note": "Valid"})
        self.assertFalse(result.ready)
        self.assertEqual(result.writes, ())

    def test_identity_remaps_exclusions_and_nondefault_policies_are_blocked(
        self,
    ) -> None:
        source = _model(_field("code"), _field("new_code"))
        for rule in (
            RestoreFieldRule("code", excluded=True),
            RestoreFieldRule("code", target_field="new_code"),
            RestoreFieldRule("code", policy=RestoreValuePolicy.KEEP_DESTINATION),
        ):
            with self.subTest(rule=rule):
                self.assertFalse(
                    _compile(source, identity_fields=("code",), rules=(rule,)).ready
                )

    def test_conflicting_field_targets_and_malformed_saved_rules_are_rejected(
        self,
    ) -> None:
        source = _model(_field("a"), _field("b"))
        plan = _compile(source, rules=(RestoreFieldRule("a", target_field="b"),))
        self.assertIn("RESTORE_FIELD_AMBIGUOUS", {issue.code for issue in plan.issues})
        rule = RestoreFieldRule(
            "a", target_field="b", policy=RestoreValuePolicy.FILL_EMPTY
        )
        self.assertEqual(RestoreFieldRule.from_dict(rule.to_dict()), rule)
        with self.assertRaises(ValueError):
            RestoreFieldRule.from_dict({**rule.to_dict(), "excluded": "false"})
        with self.assertRaises(ValueError):
            _compile(source, rules=(rule, rule))


class OdooRestoreValueTests(unittest.TestCase):
    def test_overwrite_keep_and_fill_apply_to_effective_verification_values(
        self,
    ) -> None:
        source = _model(_field("overwrite"), _field("keep"), _field("fill"))
        plan = _compile(
            source,
            rules=(
                RestoreFieldRule("keep", policy=RestoreValuePolicy.KEEP_DESTINATION),
                RestoreFieldRule("fill", policy=RestoreValuePolicy.FILL_EMPTY),
            ),
        )
        result = apply_odoo_restore(
            plan,
            {"overwrite": "Saved", "keep": "Saved", "fill": "Saved"},
            {"overwrite": "Old", "keep": "Current", "fill": ""},
        )
        self.assertTrue(result.ready)
        self.assertEqual(dict(result.writes), {"overwrite": "Saved", "fill": "Saved"})
        self.assertEqual(
            dict(result.expected_values),
            {"overwrite": "Saved", "keep": "Current", "fill": "Saved"},
        )

    def test_false_and_zero_are_meaningful_fill_empty_values(self) -> None:
        source = _model(
            _field("active", "boolean"),
            _field("sequence", "integer"),
            _field("cost", "monetary"),
        )
        plan = _compile(
            source,
            rules=tuple(
                RestoreFieldRule(field.name, policy=RestoreValuePolicy.FILL_EMPTY)
                for field in source.fields
            ),
        )
        result = apply_odoo_restore(
            plan,
            {"active": True, "sequence": 9, "cost": Decimal("12.50")},
            {"active": False, "sequence": 0, "cost": Decimal(0)},
        )
        self.assertTrue(result.ready)
        self.assertEqual(result.writes, ())
        self.assertEqual(
            dict(result.expected_values),
            {"active": False, "sequence": 0, "cost": Decimal(0)},
        )

    def test_absent_saved_field_does_not_become_an_empty_write(self) -> None:
        plan = _compile(_model(_field("name"), _field("note")))
        result = apply_odoo_restore(
            plan, {"name": "New"}, {"name": "Old", "note": "Keep"}
        )
        self.assertEqual(dict(result.writes), {"name": "New"})
        cleared = apply_odoo_restore(plan, {"note": None}, {"note": "Clear"})
        self.assertEqual(dict(cleared.writes), {"note": None})

    def test_matching_ambiguity_or_identity_change_cannot_produce_writes(self) -> None:
        plan = _compile(
            _model(_field("code"), _field("name")), identity_fields=("code",)
        )
        for count, current in (
            (2, {"code": "A"}),
            (1, {"code": "B"}),
            (0, {"code": "A"}),
        ):
            with self.subTest(count=count, current=current):
                result = apply_odoo_restore(
                    plan,
                    {"code": "A", "name": "Saved"},
                    current,
                    destination_match_count=count,
                )
                self.assertFalse(result.ready)
                self.assertEqual(result.writes, ())

    def test_missing_matching_value_cannot_create_an_unidentifiable_record(
        self,
    ) -> None:
        plan = _compile(
            _model(_field("code"), _field("name")), identity_fields=("code",)
        )
        result = apply_odoo_restore(plan, {"name": "Saved"})
        self.assertFalse(result.ready)
        self.assertEqual(result.writes, ())
        self.assertIn("RESTORE_IDENTITY_MISSING", {item.code for item in result.issues})

    def test_keep_requires_current_values_and_cannot_preserve_raw_relation_ids(
        self,
    ) -> None:
        model = _model(_field("tags", "many2many", relation="x.tag"))
        plan = _compile(
            model,
            rules=(
                RestoreFieldRule("tags", policy=RestoreValuePolicy.KEEP_DESTINATION),
            ),
        )
        missing = apply_odoo_restore(plan, {"tags": (_reference("A"),)}, {})
        self.assertFalse(missing.ready)
        self.assertIn(
            "RESTORE_DESTINATION_VALUE_MISSING", {item.code for item in missing.issues}
        )
        invalid = apply_odoo_restore(plan, {"tags": (_reference("A"),)}, {"tags": (7,)})
        self.assertFalse(invalid.ready)
        self.assertEqual(invalid.writes, ())

    def test_keep_and_fill_policies_use_saved_values_for_new_records(self) -> None:
        for major in (19, 20):
            for policy in (
                RestoreValuePolicy.KEEP_DESTINATION,
                RestoreValuePolicy.FILL_EMPTY,
            ):
                with self.subTest(major=major, policy=policy):
                    plan = _compile(
                        _model(_field("name", required=True)),
                        major=major,
                        rules=(RestoreFieldRule("name", policy=policy),),
                    )
                    result = apply_odoo_restore(plan, {"name": "Saved"})
                    self.assertTrue(result.ready)
                    self.assertEqual(dict(result.writes), {"name": "Saved"})

    def test_many2many_replace_and_add_are_sets_without_record_deletion(self) -> None:
        source = _model(_field("tags", "many2many", relation="x.tag"))
        a, b = _reference("A"), _reference("B")
        replaced = apply_odoo_restore(_compile(source), {"tags": (b,)}, {"tags": (a,)})
        self.assertEqual(dict(replaced.writes), {"tags": (b,)})
        added = apply_odoo_restore(
            _compile(
                source,
                rules=(
                    RestoreFieldRule("tags", link_policy=RestoreLinkPolicy.ADD_MISSING),
                ),
            ),
            {"tags": (b,)},
            {"tags": (a,)},
        )
        self.assertEqual(set(dict(added.writes)["tags"]), {a, b})
        reordered = apply_odoo_restore(
            _compile(source), {"tags": (b, a)}, {"tags": (a, b)}
        )
        self.assertEqual(reordered.writes, ())
        cleared = apply_odoo_restore(_compile(source), {"tags": ()}, {"tags": (a,)})
        self.assertEqual(dict(cleared.writes), {"tags": ()})

    def test_many2many_keep_and_fill_empty_retain_existing_membership(self) -> None:
        source = _model(_field("tags", "many2many", relation="x.tag"))
        a, b = _reference("A"), _reference("B")
        for rule in (
            RestoreFieldRule("tags", link_policy=RestoreLinkPolicy.KEEP),
            RestoreFieldRule("tags", policy=RestoreValuePolicy.FILL_EMPTY),
        ):
            with self.subTest(rule=rule):
                result = apply_odoo_restore(
                    _compile(source, rules=(rule,)), {"tags": (b,)}, {"tags": (a,)}
                )
                self.assertTrue(result.ready)
                self.assertEqual(result.writes, ())
                self.assertEqual(dict(result.expected_values)["tags"], (a,))
        filled = apply_odoo_restore(
            _compile(
                source,
                rules=(RestoreFieldRule("tags", policy=RestoreValuePolicy.FILL_EMPTY),),
            ),
            {"tags": (b,)},
            {"tags": ()},
        )
        self.assertEqual(dict(filled.writes), {"tags": (b,)})

    def test_portable_references_are_required_and_relation_models_must_match(
        self,
    ) -> None:
        source = _model(_field("parent", "many2one", relation="x.parent"))
        plan = _compile(source)
        for value in (
            99,
            "Parent",
            _reference("A"),
            BusinessReference("x.parent", (99,), target_fields=("id",)),
        ):
            with self.subTest(value=value):
                self.assertFalse(apply_odoo_restore(plan, {"parent": value}).ready)
        valid = _reference("P", model="x.parent")
        self.assertEqual(
            dict(apply_odoo_restore(plan, {"parent": valid}).writes), {"parent": valid}
        )

    def test_no_type_coercion_and_removed_selection_values_are_blocking(self) -> None:
        source = _model(
            _field("count", "integer"),
            _field("state", "selection", selection=(("ready", "Ready"),)),
        )
        plan = _compile(source)
        for values in ({"count": "3"}, {"count": True}, {"state": "retired"}):
            with self.subTest(values=values):
                self.assertFalse(apply_odoo_restore(plan, values).ready)
        self.assertTrue(apply_odoo_restore(plan, {"count": 0, "state": "ready"}).ready)


if __name__ == "__main__":
    unittest.main()
