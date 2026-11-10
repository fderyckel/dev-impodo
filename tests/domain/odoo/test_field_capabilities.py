"""Exercise scalar capabilities independently of any specific Odoo model."""

import unittest
from dataclasses import replace

from impodo.domain.odoo.field_capabilities import (
    is_editable_scalar_field,
    is_editable_scalar_projection,
)
from impodo.domain.shared.models import FieldMetadata


class OdooScalarFieldCapabilitiesTests(unittest.TestCase):
    def projection(self, name: str = "portable_reference") -> FieldMetadata:
        return FieldMetadata(
            name=name,
            label="Portable reference",
            type="char",
            required=False,
            readonly=False,
            stored=False,
            computed=None,
            has_inverse=None,
            related=False,
            company_dependent=False,
            searchable=True,
            exportable=True,
        )

    def test_native_public_editable_projection_is_model_independent(self) -> None:
        for name in ("code", "x_customer_reference", "external_reference"):
            with self.subTest(name=name):
                field = self.projection(name)
                self.assertTrue(is_editable_scalar_projection(field))
                self.assertTrue(is_editable_scalar_field(field))

    def test_explicit_inverse_supports_known_computed_projection(self) -> None:
        field = replace(self.projection(), computed=True, has_inverse=True)
        self.assertTrue(is_editable_scalar_projection(field))
        self.assertTrue(is_editable_scalar_field(field))

    def test_known_compute_without_inverse_is_not_inferred_editable(self) -> None:
        for computed, inverse in ((True, None), (True, False), (None, False)):
            with self.subTest(computed=computed, inverse=inverse):
                field = replace(
                    self.projection(), computed=computed, has_inverse=inverse
                )
                self.assertFalse(is_editable_scalar_projection(field))
                self.assertFalse(is_editable_scalar_field(field))

    def test_stored_editable_computation_does_not_require_inverse(self) -> None:
        field = replace(
            self.projection(), stored=True, computed=True, has_inverse=False
        )
        self.assertFalse(is_editable_scalar_projection(field))
        self.assertTrue(is_editable_scalar_field(field))

    def test_projection_requires_positive_search_export_and_storage_evidence(
        self,
    ) -> None:
        for changes in (
            {"searchable": False},
            {"searchable": None},
            {"exportable": False},
            {"exportable": None},
            {"stored": None},
            {"company_dependent": True},
            {"company_dependent": None},
            {"readonly": True},
            {"related": True},
        ):
            with self.subTest(changes=changes):
                field = replace(self.projection(), **changes)
                self.assertFalse(is_editable_scalar_projection(field))

    def test_non_scalar_and_relational_values_do_not_qualify(self) -> None:
        for field_type in ("many2one", "one2many", "many2many", "binary", "json"):
            with self.subTest(field_type=field_type):
                field = replace(self.projection(), type=field_type)
                self.assertFalse(is_editable_scalar_projection(field))
                self.assertFalse(is_editable_scalar_field(field))
        field = replace(self.projection(), relation="x.related")
        self.assertFalse(is_editable_scalar_projection(field))
        self.assertFalse(is_editable_scalar_field(field))

    def test_stored_values_keep_portable_scalar_boundaries(self) -> None:
        stored = replace(self.projection(), stored=True)
        for changes in (
            {"readonly": True},
            {"related": True},
            {"company_dependent": True},
            {"company_dependent": None},
            {"exportable": False},
            {"exportable": None},
        ):
            with self.subTest(changes=changes):
                self.assertFalse(is_editable_scalar_field(replace(stored, **changes)))

    def test_missing_field_has_no_capability(self) -> None:
        self.assertFalse(is_editable_scalar_projection(None))
        self.assertFalse(is_editable_scalar_field(None))


if __name__ == "__main__":
    unittest.main()
