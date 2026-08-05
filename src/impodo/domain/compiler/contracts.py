"""Versioned runtime semantics shared by migration pipeline consumers."""

from __future__ import annotations

import json
from hashlib import sha256
from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ...models import canonical_json_bytes
from ...profile import DatasetSpec, validate_dataset_graph


COMPILED_MIGRATION_PLAN_VERSION = 1
MIGRATION_COMPILER_VERSION = 1


class CompiledMigrationPlan(BaseModel):
    """Immutable compiled semantics for preparation, staging, and preflight.

    Authoring documents are converted to this contract at their boundary.
    Runtime consumers use the same validated ``DatasetSpec`` contracts, so
    downstream stages cannot reinterpret target semantics independently.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    plan_id: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_-]*$")
    origin: Literal["browser_mapping", "profile_document"]
    origin_hash: str
    datasets: tuple[DatasetSpec, ...] = Field(min_length=1)
    source_selection_hash: str | None = None
    schema_hash: str | None = None
    derived_plan_hash: str | None = None
    contract_version: Literal[1] = COMPILED_MIGRATION_PLAN_VERSION
    compiler_version: Literal[1] = MIGRATION_COMPILER_VERSION

    @field_validator(
        "origin_hash",
        "source_selection_hash",
        "schema_hash",
        "derived_plan_hash",
    )
    @classmethod
    def validate_hash(cls, value: str | None) -> str | None:
        """Require canonical evidence hashes on every optional binding."""

        if value is None:
            return None
        digest = value.removeprefix("sha256:").casefold()
        if len(digest) != 64:
            raise ValueError("compiled plan hashes must be canonical sha256 values")
        try:
            int(digest, 16)
        except ValueError as error:
            raise ValueError(
                "compiled plan hashes must be canonical sha256 values"
            ) from error
        return f"sha256:{digest}"

    @model_validator(mode="after")
    def validate_datasets(self) -> "CompiledMigrationPlan":
        """Apply the same graph validation as the authoring profile."""

        if self.origin == "browser_mapping":
            if self.source_selection_hash is None or self.schema_hash is None:
                raise ValueError(
                    "browser plans require source-selection and schema bindings"
                )
        elif any(
            value is not None
            for value in (
                self.source_selection_hash,
                self.schema_hash,
                self.derived_plan_hash,
            )
        ):
            raise ValueError("profile plans cannot contain browser evidence bindings")
        validate_dataset_graph(self.datasets)
        return self

    def dataset(self, name: str) -> DatasetSpec:
        """Return a compiled dataset by its stable name."""

        for dataset in self.datasets:
            if dataset.name == name:
                return dataset
        raise KeyError(name)

    def to_portable_dict(self) -> dict[str, object]:
        """Return the deterministic contract payload used for evidence hashes."""

        return self.model_dump(mode="json", exclude_none=True)

    def to_json(self) -> str:
        """Serialize the complete compiled contract deterministically."""

        return canonical_json_bytes(self.to_portable_dict()).decode("utf-8")

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "CompiledMigrationPlan":
        """Restore and validate one portable compiled contract."""

        return cls.model_validate(payload)

    @classmethod
    def from_json(cls, value: str) -> "CompiledMigrationPlan":
        """Restore one compiled contract from its canonical JSON form."""

        return cls.from_dict(json.loads(value))

    @property
    def semantic_hash(self) -> str:
        """Bind consumers to the exact compiled contract and its inputs."""

        return "sha256:" + sha256(
            canonical_json_bytes(self.to_portable_dict())
        ).hexdigest()


def compiled_profile_origin_hash(payload: object) -> str:
    """Hash one validated profile document before compiling it."""

    return "sha256:" + sha256(canonical_json_bytes(payload)).hexdigest()
