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
                )
                for item in definition.datasets
            ),
            key=lambda item: item.dataset_id,
        )
    )
    return replace(definition, datasets=datasets)
