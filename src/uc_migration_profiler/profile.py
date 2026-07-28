"""Strict profile models and profile loading."""

from __future__ import annotations

from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator


ScalarType = Literal[
    "string",
    "integer",
    "decimal",
    "boolean",
    "date",
    "datetime",
]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ProfileIdentity(StrictModel):
    id: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_-]*$")
    description: str | None = None


class SourceSpec(StrictModel):
    file: str = Field(min_length=1)
    encoding: str = "utf-8-sig"
    delimiter: str = Field(default=",", min_length=1, max_length=1)
    sheet: str | None = None
    header_row: int = Field(default=1, ge=1, le=1_048_576)

    @model_validator(mode="after")
    def validate_source(self) -> "SourceSpec":
        posix_path = PurePosixPath(self.file)
        windows_path = PureWindowsPath(self.file)
        if (
            posix_path.is_absolute()
            or windows_path.is_absolute()
            or ".." in posix_path.parts
            or ".." in windows_path.parts
        ):
            raise ValueError("source.file must be a contained relative path")

        suffix = Path(self.file).suffix.casefold()
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
    model: str = Field(min_length=3)
    mode: Literal["upsert", "create", "reference"] = "upsert"
    on_existing: Literal["block", "unchanged"] | None = None

    @model_validator(mode="after")
    def validate_create_policy(self) -> "TargetSpec":
        if self.mode == "create" and self.on_existing is None:
            raise ValueError("target.on_existing is required when target.mode is 'create'")
        if self.mode != "create" and self.on_existing is not None:
            raise ValueError("target.on_existing is only valid when target.mode is 'create'")
        return self


class NormalizationSpec(StrictModel):
    trim: bool = False
    collapse_whitespace: bool = False
    casefold: bool = False
    empty_as_null: bool = True
    decimal_places: int | None = Field(default=None, ge=0, le=18)
    timezone: str = "UTC"


class FieldSpec(StrictModel):
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
        if self.validate_only and self.compare:
            raise ValueError("validate_only fields cannot also set compare: true")
        if self.type != "decimal" and self.normalize.decimal_places is not None:
            raise ValueError("normalize.decimal_places is only valid for decimal fields")
        return self


class ResolveSpec(StrictModel):
    dataset: str | None = None
    target_source_fields: tuple[str, ...] = ()
    target_model: str | None = None
    target_fields: tuple[str, ...] = ()
    target_scope_fields: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_origin(self) -> "ResolveSpec":
        incoming = self.dataset is not None
        target = self.target_model is not None
        if incoming == target:
            raise ValueError(
                "resolve must declare exactly one of dataset or target_model"
            )
        if incoming:
            if not self.target_source_fields:
                raise ValueError(
                    "dataset resolution requires target_source_fields"
                )
            if self.target_fields or self.target_scope_fields:
                raise ValueError(
                    "target_fields/target_scope_fields are invalid for dataset resolution"
                )
        if target and not self.target_fields:
            raise ValueError("target-only resolution requires target_fields")
        return self

    @property
    def origin(self) -> Literal["incoming", "target"]:
        return "incoming" if self.dataset is not None else "target"


class RelationSpec(StrictModel):
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
    source_fields: tuple[str, ...] = Field(min_length=1)
    target_fields: tuple[str, ...] = Field(min_length=1)
    type: ScalarType = "string"
    normalize: NormalizationSpec = Field(default_factory=NormalizationSpec)
    resolve: ResolveSpec | None = None

    @model_validator(mode="after")
    def validate_arity(self) -> "IdentityComponent":
        if self.resolve is None and len(self.source_fields) != len(self.target_fields):
            raise ValueError(
                "scalar identity source_fields and target_fields must have equal arity"
            )
        if self.resolve is not None and len(self.target_fields) != 1:
            raise ValueError("relational identity components target one relation field")
        return self


class TargetIdentitySpec(StrictModel):
    components: tuple[IdentityComponent, ...] = Field(min_length=1)
    scope: tuple[IdentityComponent, ...] = ()


class SourceIdentitySpec(StrictModel):
    fields: tuple[str, ...] = Field(min_length=1)


class DatasetSpec(StrictModel):
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
        overlap = set(self.fields).intersection(self.relations)
        if overlap:
            raise ValueError(
                f"target fields cannot be both scalar and relational: {sorted(overlap)}"
            )
        return self


class ProfileDocument(StrictModel):
    profile: ProfileIdentity
    datasets: tuple[DatasetSpec, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_datasets(self) -> "ProfileDocument":
        names = [dataset.name for dataset in self.datasets]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise ValueError(f"duplicate dataset names: {duplicates}")
        known = set(names)
        for dataset in self.datasets:
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
                if resolve.dataset is not None and resolve.dataset not in known:
                    raise ValueError(
                        f"dataset {dataset.name!r} resolves unknown dataset "
                        f"{resolve.dataset!r}"
                    )
                if resolve.dataset is not None and resolve.dataset in known:
                    target_dataset = next(
                        item
                        for item in self.datasets
                        if item.name == resolve.dataset
                    )
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
        self._validate_dependency_cycles()
        return self

    def _validate_dependency_cycles(self) -> None:
        graph: dict[str, set[str]] = {dataset.name: set() for dataset in self.datasets}
        for dataset in self.datasets:
            specs = [
                component.resolve
                for component in (
                    *dataset.target_identity.components,
                    *dataset.target_identity.scope,
                )
                if component.resolve is not None
            ]
            specs.extend(relation.resolve for relation in dataset.relations.values())
            graph[dataset.name].update(
                resolve.dataset for resolve in specs if resolve.dataset is not None
            )

        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(name: str) -> None:
            if name in visiting:
                raise ValueError(f"deferred dataset reference cycle includes {name!r}")
            if name in visited:
                return
            visiting.add(name)
            for dependency in graph[name]:
                visit(dependency)
            visiting.remove(name)
            visited.add(name)

        for dataset_name in graph:
            visit(dataset_name)

    def dataset(self, name: str) -> DatasetSpec:
        for dataset in self.datasets:
            if dataset.name == name:
                return dataset
        raise KeyError(name)


class ProfileLoadError(ValueError):
    """Raised when a profile cannot be parsed or validated."""


def load_profile(path: str | Path) -> ProfileDocument:
    profile_path = Path(path)
    try:
        loaded = yaml.safe_load(profile_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ProfileLoadError(f"cannot read profile {profile_path}: {exc}") from exc
    if not isinstance(loaded, dict):
        raise ProfileLoadError(f"profile {profile_path} must contain a YAML object")

    try:
        return ProfileDocument.model_validate(loaded)
    except ValidationError as exc:
        errors = []
        for error in exc.errors(include_url=False):
            location = ".".join(str(item) for item in error["loc"])
            errors.append(f"{location or '<root>'}: {error['msg']}")
        raise ProfileLoadError(
            f"invalid profile {profile_path}:\n- " + "\n- ".join(errors)
        ) from exc
