"""Keep run decisions within the meaning of a compiled Recipe mapping.

Target value choices and non-invariant control expectations belong to a run.
Providers, transformations, identities, write ownership and control definitions
belong to the pinned Recipe. This projection excludes only run-owned choices.
"""

from __future__ import annotations

from dataclasses import replace

from impodo.domain.mapping.contracts import (
    CategoricalCoveragePolicy,
    MappingDefinition,
    RelationshipValueSource,
    ResolverOrigin,
    ScalarValueSource,
)


def recipe_mapping_shape(definition: MappingDefinition) -> str:
    """Hash the immutable meaning, retaining invariant control expectations."""

    datasets = []
    for dataset in definition.datasets:
        invariant_ids = {
            control.control_id
            for control in dataset.control_definitions
            if control.invariant_expectation
        }
        fields = tuple(
            replace(
                field,
                value_mappings=(),
                categorical_policy=CategoricalCoveragePolicy.EXACT_TARGET_VALUE,
            )
            if field.value_source is ScalarValueSource.SOURCE
            and field.categorical_policy in {
                CategoricalCoveragePolicy.EXACT_TARGET_VALUE,
                CategoricalCoveragePolicy.EXPLICIT_VALUE_MATCH,
            }
            else field
            for field in dataset.fields
        )
        relationships = tuple(
            replace(
                relationship,
                resolver=replace(relationship.resolver, value_mappings=()),
                categorical_policy=CategoricalCoveragePolicy.EXACT_BUSINESS_KEY,
            )
            if relationship.value_source is RelationshipValueSource.SOURCE
            and relationship.resolver.origin is ResolverOrigin.TARGET_CATALOG
            and relationship.categorical_policy in {
                CategoricalCoveragePolicy.EXACT_BUSINESS_KEY,
                CategoricalCoveragePolicy.EXPLICIT_KEY_MATCH,
            }
            else relationship
            for relationship in dataset.relationships
        )
        datasets.append(replace(
            dataset,
            fields=fields,
            relationships=relationships,
            control_expectations=tuple(
                expectation for expectation in dataset.control_expectations
                if expectation.control_id in invariant_ids
            ),
        ))
    return replace(definition, datasets=tuple(datasets)).content_hash
