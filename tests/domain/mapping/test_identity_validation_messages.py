"""Identity checks name the unfinished Odoo field and preserve source row keys."""

from dataclasses import replace
from unittest import TestCase

from impodo.domain.mapping.validation.validator import MappingSemanticValidator
from tests.domain.mapping.test_validation import (
    _schema_catalog,
    _schema_governance,
    _source_selection,
    _valid_definition,
)


class IdentityValidationMessageTests(TestCase):
    def test_missing_matching_source_names_field_without_rejecting_composite_row_key(
        self,
    ):
        for version in ("19.0", "20.0"):
            with self.subTest(version=version):
                selection = _source_selection()
                schema = replace(_schema_catalog(), odoo_version=version)
                governance = _schema_governance(schema)
                definition = _valid_definition(selection, governance)
                company, partner = definition.datasets
                missing = replace(
                    partner,
                    target_identity=(
                        replace(partner.target_identity[0], source_column_keys=()),
                    ),
                )
                result = MappingSemanticValidator().validate(
                    replace(definition, datasets=(company, missing)),
                    selection,
                    schema,
                    governance,
                )
                issues = [
                    item
                    for item in result.issues
                    if item.code == "MAPPING_IDENTITY_ARITY_INVALID"
                ]
                self.assertEqual(len(issues), 1)
                self.assertEqual(issues[0].target_field, "ref")
                self.assertIn("has no source column selected", issues[0].message)
                self.assertIn("Choose a source column", issues[0].remediation)
                self.assertFalse(
                    any(
                        "/source_identity_column_keys" in item.path
                        for item in result.issues
                    )
                )
