"""Typed, indexed inputs shared by mapping validation rules."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from impodo.domain.workspace.reference_keys import StandardReferenceFieldContract
from impodo.domain.workspace.supporting_lookups import SupportingLookupSnapshot

from ...schema.governance import (
    BusinessKeyStatus,
    SchemaGovernance,
)
from ...source_binding import SourceBinding, SourceOriginKind
from ..contracts import DatasetMapping, MappingDefinition


class SourceColumnView(Protocol):
    """Minimal frozen-column shape required by semantic validation."""

    @property
    def stable_key(self) -> str: ...

    @property
    def source_name(self) -> str: ...

    @property
    def candidate_type(self) -> str: ...


class SourceDatasetView(Protocol):
    """Minimal frozen-dataset shape required by semantic validation."""

    @property
    def dataset_id(self) -> str: ...

    @property
    def name(self) -> str: ...

    @property
    def source(self) -> SourceBinding: ...

    @property
    def origin(self) -> SourceOriginKind: ...

    @property
    def columns(self) -> Sequence[SourceColumnView]: ...


class SourceSelectionView(Protocol):
    """Minimal complete source-selection shape required by validation."""

    @property
    def content_hash(self) -> str: ...

    @property
    def datasets(self) -> Sequence[SourceDatasetView]: ...


class SchemaFieldView(Protocol):
    """Minimal captured-field metadata used by mapping validators."""

    @property
    def name(self) -> str: ...

    @property
    def type(self) -> str: ...

    @property
    def required(self) -> bool: ...

    @property
    def readonly(self) -> bool: ...

    @property
    def relation(self) -> str | None: ...

    @property
    def relation_field(self) -> str | None: ...

    @property
    def selection(self) -> Sequence[tuple[str, str]]: ...

    @property
    def stored(self) -> bool | None: ...

    @property
    def computed(self) -> bool | None: ...

    @property
    def related(self) -> bool | None: ...

    @property
    def translated(self) -> bool | None: ...

    @property
    def company_dependent(self) -> bool | None: ...

    @property
    def digits(self) -> tuple[int, int] | None: ...

    @property
    def create_default_present(self) -> bool: ...

    @property
    def create_default_value(self) -> bool | int | float | str | None: ...


class SchemaModelView(Protocol):
    """Minimal captured-model metadata used by mapping validators."""

    @property
    def name(self) -> str: ...

    @property
    def fields(self) -> Sequence[SchemaFieldView]: ...


class SchemaCatalogView(Protocol):
    """Minimal target-bound schema catalog used by mapping validators."""

    @property
    def content_hash(self) -> str: ...

    @property
    def odoo_version(self) -> str: ...

    @property
    def models(self) -> Sequence[SchemaModelView]: ...


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
    supporting_reference_contracts: Mapping[
        BusinessKeySignature,
        tuple[StandardReferenceFieldContract, ...],
    ]

    @classmethod
    def build(
        cls,
        definition: MappingDefinition,
        source_selection: SourceSelectionView,
        schema_catalog: SchemaCatalogView,
        schema_governance: SchemaGovernance | None,
        supporting_references: Sequence[SupportingLookupSnapshot] = (),
    ) -> ValidationContext:
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
        supporting_contracts = {
            (
                item.relation_model,
                item.key_fields,
                item.scope_fields,
            ): item.field_contracts
            for item in supporting_references
        }
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
                
                    (
                        item.model,
                        item.key_fields,
                        item.scope_fields,
                    )
                    for item in governed_keys
                
            ).union(
                supporting_contracts
            ),
            supporting_reference_contracts=supporting_contracts,
        )

    def has_governed_key(
        self,
        model: str,
        key_fields: tuple[str, ...],
        scope_fields: tuple[str, ...],
    ) -> bool:
        """Return whether the exact target key/scope signature is confirmed."""

        return (model, key_fields, scope_fields) in self.governed_key_signatures
