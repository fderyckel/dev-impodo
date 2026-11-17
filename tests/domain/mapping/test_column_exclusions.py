"""Protect saved column-exclusion meaning and its target-input boundaries."""

from __future__ import annotations

import copy
import unittest
from dataclasses import replace

from impodo.domain.mapping.canonicalization import canonicalize_mapping_definition
from impodo.domain.mapping.column_exclusions import (
    target_source_column_keys,
    validate_source_column_exclusions,
)
from impodo.domain.mapping.contracts import (
    DatasetMapping,
    IdentityComponentMapping,
    MappingDefinition,
    ReferenceKeyMapping,
    ReferenceLookupMapping,
    RelationshipMapping,
    RelationshipResolver,
    ResolverOrigin,
    RowInclusionCondition,
    RowInclusionMode,
    RowInclusionPolicy,
    ScalarConcatenation,
    ScalarFieldMapping,
    ScalarValueSource,
    SelectionCondition,
    SelectionConditionOperator,
    SelectionRule,
    SelectionRuleSet,
)
from impodo.domain.mapping.generated_ordering import GeneratedOrdering
from impodo.domain.mapping.reference_preparation import (
    ReferencePart,
    ReferencePreparation,
)
from impodo.domain.mapping.source_record_identity import SourceRecordIdentity
from impodo.domain.mapping.validation.validator import MappingSemanticValidator
from impodo.domain.recipe.value_rules import ScalarTransformPolicy
from impodo.domain.serialization import content_hash
from impodo.domain.source_binding import SourceOriginKind
from impodo.domain.workspace.contracts import SourceDatasetColumn
from tests.domain.mapping.test_validation import (
    _schema_catalog,
    _schema_governance,
    _source_selection,
    _valid_definition,
)

_ID = "00000000-0000-0000-0000-000000000001"
_COLUMNS = (
    SourceDatasetColumn(1, "First", "first", "string"),
    SourceDatasetColumn(7, "Other", "other", "string"),
    SourceDatasetColumn(9, "Notes", "notes", "string"),
)


def _dataset(**values) -> DatasetMapping:
    return DatasetMapping(
        dataset_id="dataset:sample", target_model="x.sample", **values
    )


def _validate(dataset: DatasetMapping):
    return validate_source_column_exclusions(
        dataset,
        _COLUMNS,
        origin=SourceOriginKind.FILE,
        path="/datasets/0/excluded_source_column_keys",
    )


class ColumnExclusionContractTests(unittest.TestCase):
    def test_empty_choice_preserves_the_existing_portable_shape_and_hash(self) -> None:
        mapping = MappingDefinition(
            mapping_id="mapping:test",
            source_selection_hash="source",
            schema_hash="schema",
            datasets=(_dataset(),),
        )
        payload = mapping.to_dict()
        self.assertNotIn("excluded_source_column_keys", payload["datasets"][0])
        old_shape = copy.deepcopy(payload)
        old_hash = old_shape.pop("content_hash")
        self.assertEqual(old_hash, content_hash(old_shape))
        self.assertEqual(MappingDefinition.from_dict(payload), mapping)
        self.assertEqual(
            canonicalize_mapping_definition(mapping).content_hash, old_hash
        )

    def test_choices_round_trip_with_canonical_order_and_change_mapping_identity(
        self,
    ) -> None:
        empty = MappingDefinition(
            mapping_id="mapping:test",
            source_selection_hash="source",
            schema_hash="schema",
            datasets=(_dataset(),),
        )
        selected = replace(
            empty, datasets=(_dataset(excluded_source_column_keys=("notes", "other")),)
        )
        reversed_order = replace(
            empty, datasets=(_dataset(excluded_source_column_keys=("other", "notes")),)
        )
        self.assertEqual(selected.content_hash, reversed_order.content_hash)
        self.assertNotEqual(empty.content_hash, selected.content_hash)
        self.assertEqual(
            selected.datasets[0].excluded_source_column_keys, ("notes", "other")
        )
        self.assertEqual(MappingDefinition.from_json(selected.to_json()), selected)
        self.assertEqual(
            selected.to_dict()["datasets"][0]["excluded_source_column_keys"],
            ["notes", "other"],
        )

    def test_changing_a_saved_choice_without_its_hash_is_rejected(self) -> None:
        mapping = MappingDefinition(
            mapping_id="mapping:test",
            source_selection_hash="source",
            schema_hash="schema",
            datasets=(_dataset(excluded_source_column_keys=("notes",)),),
        )
        payload = mapping.to_dict()
        payload["datasets"][0]["excluded_source_column_keys"] = ["other"]
        with self.assertRaisesRegex(ValueError, "content hash"):
            MappingDefinition.from_dict(payload)

    def test_duplicate_blank_or_oversized_keys_are_not_saved(self) -> None:
        for keys in (("notes", "notes"), ("",), (" ",), ("x" * 501,)):
            with (
                self.subTest(keys=keys),
                self.assertRaisesRegex(ValueError, "distinct valid keys"),
            ):
                _dataset(excluded_source_column_keys=keys)


class ColumnExclusionDependencyTests(unittest.TestCase):
    def test_target_identity_and_scope_inputs_cannot_be_excluded(self) -> None:
        dataset = _dataset(
            target_identity=(IdentityComponentMapping(("first",), ("ref",)),),
            target_scope=(IdentityComponentMapping(("other",), ("company_id",)),),
            excluded_source_column_keys=("first", "other"),
        )
        self.assertEqual(
            target_source_column_keys(dataset, _COLUMNS), {"first", "other"}
        )
        self.assertEqual(
            {item.source_column_key for item in _validate(dataset)}, {"first", "other"}
        )
        self.assertTrue(
            all(
                item.code == "MAPPING_SOURCE_EXCLUSION_IN_USE"
                for item in _validate(dataset)
            )
        )

    def test_scalar_concatenation_selection_and_lookup_inputs_are_retained(
        self,
    ) -> None:
        rules = SelectionRuleSet(
            (
                SelectionRule(
                    _ID,
                    (
                        SelectionCondition(
                            _ID, "other", SelectionConditionOperator.IS_BLANK
                        ),
                    ),
                    "value",
                ),
            )
        )
        fields = (
            ScalarFieldMapping("name", source_column_key="first"),
            ScalarFieldMapping(
                "label",
                value_source=ScalarValueSource.CONCATENATE,
                concatenation=ScalarConcatenation(("first", "other")),
            ),
            ScalarFieldMapping(
                "choice",
                value_source=ScalarValueSource.CONDITIONAL_RULES,
                selection_rules=rules,
            ),
            ScalarFieldMapping(
                "looked_up",
                source_column_key="first",
                reference_lookup=ReferenceLookupMapping(
                    _ID,
                    "sha256:" + "a" * 64,
                    ("first", "notes"),
                    "value",
                ),
            ),
        )
        dataset = _dataset(
            fields=fields, excluded_source_column_keys=("first", "other", "notes")
        )
        self.assertEqual(
            target_source_column_keys(dataset, _COLUMNS), {"first", "other", "notes"}
        )
        self.assertEqual(len(_validate(dataset)), 3)

    def test_formula_dependencies_use_real_ordinals_and_ignore_literal_names(
        self,
    ) -> None:
        referenced = _dataset(
            fields=(
                ScalarFieldMapping(
                    "name",
                    value_source=ScalarValueSource.CONSTANT,
                    literal_value="prefix",
                    transform=ScalarTransformPolicy(formula="concat(value, column_7)"),
                ),
            )
        )
        self.assertEqual(target_source_column_keys(referenced, _COLUMNS), {"other"})
        literal = replace(
            referenced,
            fields=(
                replace(
                    referenced.fields[0],
                    transform=ScalarTransformPolicy(
                        formula="concat(value, 'column_7')"
                    ),
                ),
            ),
        )
        self.assertEqual(target_source_column_keys(literal, _COLUMNS), set())
        self.assertEqual(
            _validate(replace(literal, excluded_source_column_keys=("other",))), ()
        )
        self.assertEqual(
            _validate(replace(referenced, excluded_source_column_keys=("other",)))[
                0
            ].code,
            "MAPPING_SOURCE_EXCLUSION_IN_USE",
        )

    def test_relationship_preparation_dependencies_cannot_be_excluded(self) -> None:
        preparation = ReferencePreparation(
            formula="concat(value, column_7)",
            bindings=(("column_7", "other"),),
        )
        relationship = RelationshipMapping(
            "parent_id",
            "many2one",
            ("first",),
            RelationshipResolver(
                ResolverOrigin.TARGET_CATALOG,
                model="x.related",
                key_mappings=(ReferenceKeyMapping("first", "ref", preparation),),
            ),
        )
        dataset = _dataset(
            relationships=(relationship,), excluded_source_column_keys=("other",)
        )
        self.assertEqual(
            target_source_column_keys(dataset, _COLUMNS), {"first", "other"}
        )
        self.assertEqual(_validate(dataset)[0].code, "MAPPING_SOURCE_EXCLUSION_IN_USE")
        parts = ReferencePreparation(parts=(ReferencePart(source_column_key="notes"),))
        prepared_scope = IdentityComponentMapping(
            ("first",),
            ("company_id",),
            resolver=RelationshipResolver(
                ResolverOrigin.TARGET_CATALOG,
                model="x.company",
                key_mappings=(ReferenceKeyMapping("first", "ref", parts),),
            ),
        )
        self.assertEqual(
            target_source_column_keys(
                _dataset(target_scope=(prepared_scope,)), _COLUMNS
            ),
            {"first", "notes"},
        )

    def test_ordering_and_existing_external_id_are_target_inputs(self) -> None:
        dataset = _dataset(
            generated_ordering=GeneratedOrdering(
                "sequence",
                group_column_keys=("first",),
                sort_column_keys=("other",),
                sort_value_types=("string",),
            ),
            source_record_identity=SourceRecordIdentity(
                "saved", existing_external_id_column_key="notes"
            ),
        )
        self.assertEqual(
            target_source_column_keys(dataset, _COLUMNS), {"first", "other", "notes"}
        )

    def test_row_identifier_and_row_filter_alone_do_not_supply_target_values(
        self,
    ) -> None:
        dataset = _dataset(
            source_identity_column_keys=("first",),
            row_inclusion=RowInclusionPolicy(
                RowInclusionMode.MATCHING_ROWS,
                (
                    RowInclusionCondition(
                        _ID, "notes", SelectionConditionOperator.IS_BLANK
                    ),
                ),
            ),
            excluded_source_column_keys=("first", "notes"),
        )
        self.assertEqual(target_source_column_keys(dataset, _COLUMNS), set())
        self.assertEqual(_validate(dataset), ())

    def test_unknown_keys_and_protected_source_choices_are_rejected(self) -> None:
        dataset = _dataset(excluded_source_column_keys=("missing",))
        self.assertEqual(_validate(dataset)[0].code, "MAPPING_SOURCE_EXCLUSION_UNKNOWN")
        self.assertEqual(_validate(dataset)[0].source_column_key, "missing")
        protected = validate_source_column_exclusions(
            dataset,
            _COLUMNS,
            origin=SourceOriginKind.ODOO,
            path="/datasets/0/excluded_source_column_keys",
        )
        self.assertEqual(protected[0].code, "MAPPING_SOURCE_EXCLUSION_ODOO_UNSUPPORTED")
        self.assertEqual(
            validate_source_column_exclusions(
                _dataset(),
                _COLUMNS,
                origin=SourceOriginKind.ODOO,
                path="/excluded",
            ),
            (),
        )

    def test_semantic_validation_rejects_conflicts_for_both_supported_majors(
        self,
    ) -> None:
        selection = _source_selection()
        for major in (19, 20):
            schema = replace(_schema_catalog(), odoo_version=f"{major}.0")
            governance = _schema_governance(schema)
            definition = _valid_definition(selection, governance)
            partner = definition.datasets[1]
            for excluded, expected in (
                (("partner.parent_ref",), set()),
                (("partner.name",), {"MAPPING_SOURCE_EXCLUSION_IN_USE"}),
                (("unknown",), {"MAPPING_SOURCE_EXCLUSION_UNKNOWN"}),
            ):
                with self.subTest(major=major, excluded=excluded):
                    candidate = replace(
                        definition,
                        datasets=(
                            definition.datasets[0],
                            replace(
                                partner,
                                excluded_source_column_keys=excluded,
                            ),
                        ),
                    )
                    result = MappingSemanticValidator().validate(
                        candidate, selection, schema, governance
                    )
                    self.assertEqual(
                        {
                            issue.code
                            for issue in result.issues
                            if issue.code.startswith("MAPPING_SOURCE_EXCLUSION_")
                        },
                        expected,
                    )
