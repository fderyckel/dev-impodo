"""Typed, indexed inputs shared by mapping validation rules."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Protocol, Sequence

from ...schema.governance import (
    BusinessKeyStatus,
    SchemaGovernance,
)
from ..contracts import DatasetMapping, MappingDefinition


class SourceColumnView(Protocol):
    stable_key: str
    candidate_type: str


class SourceDatasetView(Protocol):
    dataset_id: str
    name: str
    columns: Sequence[SourceColumnView]


class SourceSelectionView(Protocol):
    content_hash: str
    datasets: Sequence[SourceDatasetView]


class SchemaFieldView(Protocol):
    name: str
    type: str
    required: bool
    readonly: bool
    relation: str | None
    relation_field: str | None
    selection: Sequence[tuple[str, str]]


class SchemaModelView(Protocol):
    name: str
    fields: Sequence[SchemaFieldView]


class SchemaCatalogView(Protocol):
    content_hash: str
    models: Sequence[SchemaModelView]


BusinessKeySignature = tuple[str, tuple[str, ...], tuple[str, ...]]


@dataclass(frozen=True, slots=True)
class ValidationContext:
    """One-pass indexes for deterministic, in-memory mapping validation."""

    definition: MappingDefinition
    source_selection: SourceSelectionView
    schema_catalog: SchemaCatalogView
    schema_governance: SchemaGovernance | None
    source_datasets: Mapping[str, SourceDatasetView]
    schema_models: Mapping[str, SchemaModelView]
    fields_by_model: Mapping[str, Mapping[str, SchemaFieldView]]
    datasets_by_id: Mapping[str, DatasetMapping]
    dataset_targets: Mapping[str, str]
    governed_key_signatures: frozenset[BusinessKeySignature]

    @classmethod
    def build(
        cls,
        definition: MappingDefinition,
        source_selection: SourceSelectionView,
        schema_catalog: SchemaCatalogView,
        schema_governance: SchemaGovernance | None,
    ) -> "ValidationContext":
        source_datasets = {
            item.dataset_id: item for item in source_selection.datasets
        }
        schema_models = {item.name: item for item in schema_catalog.models}
        governed_keys = tuple(
            item
            for item in (
                schema_governance.business_keys
                if schema_governance is not None
                else ()
            )
            if item.status is BusinessKeyStatus.CONFIRMED
        )
        datasets_by_id: dict[str, DatasetMapping] = {}
        for item in definition.datasets:
            datasets_by_id.setdefault(item.dataset_id, item)
        return cls(
            definition=definition,
            source_selection=source_selection,
            schema_catalog=schema_catalog,
            schema_governance=schema_governance,
            source_datasets=source_datasets,
            schema_models=schema_models,
            fields_by_model={
                model.name: {field.name: field for field in model.fields}
                for model in schema_catalog.models
            },
            datasets_by_id=datasets_by_id,
            dataset_targets={
                item.dataset_id: item.target_model
                for item in definition.datasets
            },
            governed_key_signatures=frozenset(
                (item.model, item.key_fields, item.scope_fields)
                for item in governed_keys
            ),
        )

    def has_governed_key(
        self,
        model: str,
        key_fields: tuple[str, ...],
        scope_fields: tuple[str, ...],
    ) -> bool:
        return (model, key_fields, scope_fields) in self.governed_key_signatures
