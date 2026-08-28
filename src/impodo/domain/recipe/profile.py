"""Strict, immutable mapping-profile contracts and YAML loading.

Profiles are the declarative authority for source preparation, request
planning, relationship resolution, and comparison. Pydantic validates the
entire document before any source file or Odoo snapshot is read. Unknown keys,
unsafe paths, contradictory policies, broken dataset references, and
required-at-create dependency cycles therefore fail at the boundary.

The module contains no source-data or Odoo access. `ProfileDocument` is an
authoring input; the compiler converts it to `CompiledMigrationPlan` before
source preparation, request planning, metadata validation, or comparison.
"""

from __future__ import annotations

from pathlib import PurePosixPath, PureWindowsPath
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from impodo.domain.relationship_dependencies import (
    extract_dataset_dependency_edges,
    required_cross_dataset_cycle,
)


ScalarType = Literal[
    "string",
    "integer",
    "decimal",
    "boolean",
    "date",
    "datetime",
]


class StrictModel(BaseModel):
    """Base for immutable contracts that reject every unknown property."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class ProfileIdentity(StrictModel):
    """Human description and stable machine identifier of one profile."""

    id: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_-]*$")
    description: str | None = None


class SourceSpec(StrictModel):
    """Location and parsing parameters for one contained CSV/XLSX source."""

    file: str = Field(min_length=1)
    encoding: str = "utf-8-sig"
    delimiter: str = Field(default=",", min_length=1, max_length=1)
    sheet: str | None = None
    header_row: int = Field(default=1, ge=1, le=1_048_576)

    @model_validator(mode="after")
    def validate_source(self) -> "SourceSpec":
        """Reject escaping paths and format-specific option contradictions."""

        posix_path = PurePosixPath(self.file)
        windows_path = PureWindowsPath(self.file)
        if (
            posix_path.is_absolute()
            or windows_path.is_absolute()
            or ".." in posix_path.parts
            or ".." in windows_path.parts
        ):
            raise ValueError("source.file must be a contained relative path")

        suffix = PurePosixPath(self.file.replace("\\", "/")).suffix.casefold()
        if suffix not in {".csv", ".xlsx"}:
            raise ValueError("source.file must use the .csv or .xlsx extension")
        if suffix == ".xlsx":
            if self.sheet is None or not self.sheet.strip():
                raise ValueError("source.sheet is required for .xlsx files")
            if "encoding" in self.model_fields_set or "delimiter" in self.model_fields_set:
                raise ValueError(
                    "source.encoding and source.delimiter are only valid for CSV"
                )
        else:
            if self.sheet is not None:
                raise ValueError("source.sheet is only valid for .xlsx files")
            if self.header_row != 1:
                raise ValueError("source.header_row can differ from 1 only for .xlsx")
        return self


class TargetSpec(StrictModel):
    """Target Odoo model and create/upsert/reference behavior."""

    model: str = Field(min_length=3)
    mode: Literal[
        "upsert",
        "create",
        "reference",
        "odoo_pinned_update",
    ] = "upsert"
    on_existing: Literal["block", "unchanged"] | None = None

    @model_validator(mode="after")
    def validate_create_policy(self) -> "TargetSpec":
        """Require an explicit collision policy only for create-only data."""

        if self.mode == "create" and self.on_existing is None:
            raise ValueError("target.on_existing is required when target.mode is 'create'")
        if self.mode != "create" and self.on_existing is not None:
            raise ValueError("target.on_existing is only valid when target.mode is 'create'")
        return self


class NormalizationSpec(StrictModel):
    """Deterministic type-parsing normalization used by the current POC.

    These options describe technical canonicalization for comparison. The
    separate normalization-governance work will add manager-approved source
    correction rules rather than silently expanding this contract.
    """

    trim: bool = False
    collapse_whitespace: bool = False
    casefold: bool = False
    empty_as_null: bool = True
    decimal_places: int | None = Field(default=None, ge=0, le=18)
    timezone: str = "UTC"


class FieldSpec(StrictModel):
    """Map one source column to one scalar target field."""

    source: str = Field(min_length=1)
    type: ScalarType
    required: bool = False
    required_on_create: bool = False
    compare: bool = True
    validate_only: bool = False
    normalize: NormalizationSpec = Field(default_factory=NormalizationSpec)
    null_policy: Literal["distinct", "equivalent", "ignore_source_null"] = "distinct"

    @model_validator(mode="after")
    def validate_semantics(self) -> "FieldSpec":
        """Reject comparison and decimal settings that cannot coexist."""

        if self.validate_only and self.compare:
            raise ValueError("validate_only fields cannot also set compare: true")
        if self.type != "decimal" and self.normalize.decimal_places is not None:
            raise ValueError("normalize.decimal_places is only valid for decimal fields")
        return self


class ResolveSpec(StrictModel):
    """Describe one symbolic relationship-resolution origin.

    A relationship may resolve against another prepared dataset, a captured
    Odoo model, or use the target first with an incoming fallback. The hybrid
    shape retains exact reviewed target-key translations without rewriting the
    incoming source identity.
    """

    dataset: str | None = None
    target_source_fields: tuple[str, ...] = ()
    target_model: str | None = None
    target_fields: tuple[str, ...] = ()
    target_scope_fields: tuple[str, ...] = ()
    target_value_mappings: tuple[tuple[str, str], ...] | None = None

    @model_validator(mode="after")
    def validate_origin(self) -> "ResolveSpec":
        """Require one complete incoming, target, or hybrid resolution shape."""

        incoming = self.dataset is not None
        target = self.target_model is not None
        if not incoming and not target:
            raise ValueError(
                "resolve must declare dataset, target_model, or both"
            )
        if incoming:
            if not self.target_source_fields:
                raise ValueError(
                    "dataset resolution requires target_source_fields"
                )
            if not target and (self.target_fields or self.target_scope_fields):
                raise ValueError(
                    "target fields are invalid for incoming-only resolution"
                )
        if target and not self.target_fields:
            raise ValueError("target-only resolution requires target_fields")
        if self.target_value_mappings is not None:
            if not incoming or not target:
                raise ValueError(
                    "target value mappings require target-first incoming resolution"
                )
            if len(self.target_fields) != 1 or self.target_scope_fields:
                raise ValueError(
                    "target value mappings require one unscoped target key"
                )
            sources = tuple(item[0] for item in self.target_value_mappings)
            if len(set(sources)) != len(sources) or any(
                not source or not target_value
                for source, target_value in self.target_value_mappings
            ):
                raise ValueError("target value mappings are invalid")
        return self

    @property
    def origin(self) -> Literal["incoming", "target", "target_then_incoming"]:
        """Return the portable origin label stored on `LogicalReference`."""

        if self.dataset is not None and self.target_model is not None:
            return "target_then_incoming"
        return "incoming" if self.dataset is not None else "target"


class RelationSpec(StrictModel):
    """Map source business keys to a many2one or many2many target field."""

    kind: Literal["many2one", "many2many"]
    source_fields: tuple[str, ...] = Field(min_length=1)
    resolve: ResolveSpec
    compare: bool = True
    validate_only: bool = False
    required: bool = False
    required_on_create: bool = False
    on_missing: Literal["error", "warning"] = "error"
    on_ambiguous: Literal["error", "warning"] = "error"
    operation: Literal["replace", "add", "remove"] = "replace"
    separator: str = ";"
    null_policy: Literal["distinct", "equivalent", "ignore_source_null"] = "distinct"

    @model_validator(mode="after")
    def validate_semantics(self) -> "RelationSpec":
        """Reject unsafe relation operations and non-blocking compare policies."""

        if self.validate_only and self.compare:
            raise ValueError("validate_only relations cannot also set compare: true")
        if self.kind == "many2one" and self.operation != "replace":
            raise ValueError("many2one relations only support operation: replace")
        if self.kind == "many2many" and len(self.source_fields) != 1:
            raise ValueError(
                "many2many relations require exactly one list-valued source field"
            )
        if (
            self.compare or self.required or self.required_on_create
        ) and self.on_missing != "error":
            raise ValueError(
                "a compared or required relation must use on_missing: error"
            )
        if self.compare and self.on_ambiguous != "error":
            raise ValueError(
                "a compared relation must use on_ambiguous: error"
            )
        return self


class IdentityComponent(StrictModel):
    """One ordered scalar or relational component of identity/scope."""

    source_fields: tuple[str, ...] = Field(min_length=1)
    target_fields: tuple[str, ...] = Field(min_length=1)
    type: ScalarType = "string"
    normalize: NormalizationSpec = Field(default_factory=NormalizationSpec)
    resolve: ResolveSpec | None = None

    @model_validator(mode="after")
    def validate_arity(self) -> "IdentityComponent":
        """Keep flattened source and target identity shapes reconstructable."""

        if self.resolve is None and len(self.source_fields) != len(self.target_fields):
            raise ValueError(
                "scalar identity source_fields and target_fields must have equal arity"
            )
        if self.resolve is not None and len(self.target_fields) != 1:
            raise ValueError("relational identity components target one relation field")
        return self


class TargetIdentitySpec(StrictModel):
    """Ordered target business identity plus optional uniqueness scope."""

    components: tuple[IdentityComponent, ...] = ()
    scope: tuple[IdentityComponent, ...] = ()


class SourceIdentitySpec(StrictModel):
    """Ordered source trace key used for duplicate detection and relations."""

    fields: tuple[str, ...] = ()


class DatasetSpec(StrictModel):
    """Complete mapping contract for one logical source dataset."""

    name: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_]*$")
    source: SourceSpec
    target: TargetSpec
    source_identity: SourceIdentitySpec
    target_identity: TargetIdentitySpec
    fields: dict[str, FieldSpec] = Field(default_factory=dict)
    relations: dict[str, RelationSpec] = Field(default_factory=dict)
    target_domain: tuple[Any, ...] = ()

    @model_validator(mode="after")
    def validate_target_fields(self) -> "DatasetSpec":
        """Prevent one target field from having scalar and relation semantics."""

        overlap = set(self.fields).intersection(self.relations)
        if overlap:
            raise ValueError(
                f"target fields cannot be both scalar and relational: {sorted(overlap)}"
            )
        pinned = self.target.mode == "odoo_pinned_update"
        if pinned and (
            self.source_identity.fields
            or self.target_identity.components
            or self.target_identity.scope
            or self.relations
        ):
            raise ValueError(
                "odoo_pinned_update uses protected origins and scalar fields only"
            )
        if not pinned and (
            not self.source_identity.fields
            or not self.target_identity.components
        ):
            raise ValueError(
                "source and target identities are required outside pinned updates"
            )
        return self


class ProfileDocument(StrictModel):
    """Validated root profile and its dependency-ordered dataset contracts."""

    profile: ProfileIdentity
    datasets: tuple[DatasetSpec, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_datasets(self) -> "ProfileDocument":
        """Validate unique names, incoming references, identity keys, and cycles."""

        validate_dataset_graph(self.datasets)
        return self

    def dataset(self, name: str) -> DatasetSpec:
        """Return a dataset by stable name or raise `KeyError`."""

        for dataset in self.datasets:
            if dataset.name == name:
                return dataset
        raise KeyError(name)


def validate_dataset_graph(datasets: tuple[DatasetSpec, ...]) -> None:
    """Validate cross-dataset references shared by authoring and compiled contracts."""

    by_name: dict[str, DatasetSpec] = {}
    duplicates: set[str] = set()
    for dataset in datasets:
        if dataset.name in by_name:
            duplicates.add(dataset.name)
        else:
            by_name[dataset.name] = dataset
    if duplicates:
        raise ValueError(f"duplicate dataset names: {sorted(duplicates)}")
    known = set(by_name)
    dependency_edges = extract_dataset_dependency_edges(datasets)
    unknown_edge = next(
        (
            edge
            for edge in dependency_edges
            if edge.dependency_dataset not in known
        ),
        None,
    )
    if unknown_edge is not None:
        raise ValueError(
            f"dataset {unknown_edge.owner_dataset!r} resolves unknown dataset "
            f"{unknown_edge.dependency_dataset!r}"
        )
    for dataset in datasets:
        resolves = [
            component.resolve
            for component in (
                *dataset.target_identity.components,
                *dataset.target_identity.scope,
            )
            if component.resolve is not None
        ]
        resolves.extend(relation.resolve for relation in dataset.relations.values())
        for resolve in resolves:
            if resolve.dataset is not None and resolve.dataset in known:
                target_dataset = by_name[resolve.dataset]
                if (
                    tuple(resolve.target_source_fields)
                    != tuple(target_dataset.source_identity.fields)
                ):
                    raise ValueError(
                        f"dataset {dataset.name!r} resolves "
                        f"{resolve.dataset!r} through "
                        "target_source_fields that do not match the "
                        "referenced dataset source identity"
                    )
    # Self-references require row evidence and are intentionally retained for
    # Phase 2. Only hard cycles spanning distinct datasets are invalid here.
    cycle = required_cross_dataset_cycle(dependency_edges, known)
    if cycle is not None:
        raise ValueError(
            "required-at-create dataset reference cycle includes "
            + " -> ".join(repr(name) for name in cycle)
        )
