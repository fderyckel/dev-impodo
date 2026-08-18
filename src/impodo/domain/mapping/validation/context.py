"""Typed, indexed inputs shared by mapping validation rules."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Protocol, Sequence

from ...source_binding import SourceBinding, SourceOriginKind
from ...schema.governance import (
    BusinessKeyStatus,
    SchemaGovernance,
)
from ..contracts import DatasetMapping, MappingDefinition


class SourceColumnView(Protocol):
    """Minimal frozen-column shape required by semantic validation."""

    stable_key: str
    candidate_type: str


class SourceDatasetView(Protocol):
    """Minimal frozen-dataset shape required by semantic validation."""

    dataset_id: str
    name: str
    source: SourceBinding
    origin: SourceOriginKind
    columns: Sequence[SourceColumnView]


class SourceSelectionView(Protocol):
    """Minimal complete source-selection shape required by validation."""

    content_hash: str
    datasets: Sequence[SourceDatasetView]


class SchemaFieldView(Protocol):
    """Minimal captured-field metadata used by mapping validators."""

    name: str
    type: str
    required: bool
    readonly: bool
    relation: str | None
    relation_field: str | None
    selection: Sequence[tuple[str, str]]
    stored: bool | None
    computed: bool | None
    related: bool | None
    translated: bool | None
    company_dependent: bool | None


class SchemaModelView(Protocol):
    """Minimal captured-model metadata used by mapping validators."""

    name: str
    fields: Sequence[SchemaFieldView]


class SchemaCatalogView(Protocol):
    """Minimal target-bound schema catalog used by mapping validators."""

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
        """Index every source, schema, mapping, and confirmed-key lookup once."""

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
        """Return whether the exact target key/scope signature is confirmed."""

        return (model, key_fields, scope_fields) in self.governed_key_signatures
