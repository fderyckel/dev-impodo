"""Canonical ordering for mapping definitions."""

from __future__ import annotations

from dataclasses import replace

from .contracts import MappingDefinition


def canonicalize_mapping_definition(
    definition: MappingDefinition,
) -> MappingDefinition:
    """Return the stable semantic ordering used by validation and storage."""

    datasets = tuple(
        sorted(
            (
                replace(
                    item,
                    fields=tuple(
                        sorted(
                            item.fields,
                            key=lambda field: field.target_field,
                        )
                    ),
                    relationships=tuple(
                        sorted(
                            item.relationships,
                            key=lambda relation: relation.target_field,
                        )
                    ),
                    target_field_dispositions=tuple(
                        sorted(
                            item.target_field_dispositions,
                            key=lambda disposition: (
                                disposition.target_field,
                                disposition.handling.value,
                            ),
                        )
                    ),
                    control_definitions=tuple(
                        sorted(
                            item.control_definitions,
                            key=lambda control: control.control_id,
                        )
                    ),
                    control_expectations=tuple(
                        sorted(
                            item.control_expectations,
                            key=lambda expectation: expectation.control_id,
                        )
                    ),
                )
                for item in definition.datasets
            ),
            key=lambda item: item.dataset_id,
        )
    )
    return replace(definition, datasets=datasets)
