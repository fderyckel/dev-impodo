"""Fresh Recipe bindings retain single-source scalar original evidence."""

from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
from unittest import TestCase
from uuid import uuid4

from impodo.application.recipe_application_compilation import RecipeApplicationCompiler
from impodo.application.recipe_compilation_service import RecipeCompiler
from impodo.domain.mapping.contracts import (
    DatasetMapping,
    IdentityComponentMapping,
    ScalarFieldMapping,
    ScalarValueSource,
)
from impodo.domain.preflight.scalar_inputs import (
    scalar_formula_aliases,
    scalar_original_inputs,
)
from impodo.domain.recipe.value_rules import ScalarTransformPolicy
from impodo.domain.workspace.contracts import SourceSelection
from tests.domain.recipe.test_representative_shapes import _column, _dataset


class ScalarOriginalRecipeTests(TestCase):
    def test_value_formula_keeps_its_input_after_fresh_keys_and_column_reordering(self):
        source = _dataset(
            "Measurements",
            (("reference", "string"), ("notes", "string"), ("amount", "string")),
            "a",
        )
        for provider in (
            ScalarValueSource.SOURCE,
            ScalarValueSource.SOURCE_WITH_FALLBACK,
        ):
            with self.subTest(provider=provider):
                field = ScalarFieldMapping(
                    "amount",
                    _column(source, "amount"),
                    value_source=provider,
                    literal_value="0005"
                    if provider is ScalarValueSource.SOURCE_WITH_FALLBACK
                    else None,
                    value_type="decimal",
                    transform=ScalarTransformPolicy(formula="value / 0"),
                )
                mapping = DatasetMapping(
                    source.dataset_id,
                    "x.measurement",
                    source_identity_column_keys=(_column(source, "reference"),),
                    target_identity=(
                        IdentityComponentMapping(
                            (_column(source, "reference"),), ("ref",)
                        ),
                    ),
                    fields=(field,),
                )
                compiler = object.__new__(RecipeCompiler)
                _, dataset_ids, column_ids = compiler._source_identity((source,))
                portable = compiler._mapping((mapping,), dataset_ids, column_ids, {})
                original_portable = deepcopy(portable)
                fresh = replace(
                    source,
                    dataset_id="fresh-measurements",
                    columns=tuple(
                        replace(
                            column,
                            ordinal=index,
                            stable_key=f"fresh:{column.source_name}",
                        )
                        for index, column in enumerate(
                            reversed(source.columns), start=1
                        )
                    ),
                )
                selection = SourceSelection(
                    str(uuid4()),
                    1,
                    str(uuid4()),
                    datetime.now(UTC),
                    "Manager",
                    (fresh,),
                    "sha256:" + "b" * 64,
                )
                _, fresh_datasets, fresh_columns = compiler._source_identity((fresh,))
                bindings = {
                    logical: physical for physical, logical in fresh_datasets.items()
                }
                bindings.update(
                    {
                        logical: physical[1]
                        for physical, logical in fresh_columns.items()
                    }
                )
                (rebound,) = RecipeApplicationCompiler()._mapping_datasets(
                    {"mapping": portable, "control_definitions": {}},
                    bindings,
                    selection,
                    None,
                    None,
                )
                (actual,) = rebound.fields
                inputs = scalar_original_inputs(
                    actual,
                    source_keys=frozenset(
                        column.stable_key for column in fresh.columns
                    ),
                    formula_aliases=scalar_formula_aliases(fresh.columns),
                )
                self.assertEqual(inputs, ("fresh:amount",))
                self.assertEqual(actual.source_column_key, "fresh:amount")
                self.assertEqual(actual.transform, field.transform)
                self.assertEqual(actual.literal_value, field.literal_value)
                self.assertEqual(portable, original_portable)
