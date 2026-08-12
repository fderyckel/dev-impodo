"""Current source-origin contracts for physical and derived datasets."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import re

from .serialization import content_hash


_HASH = re.compile(r"sha256:[0-9a-f]{64}")
_TECHNICAL_NAME = re.compile(r"[a-z_][a-z0-9_.]{0,127}")


class SourceBindingContractError(ValueError):
    """Raised when a source binding is malformed or ambiguous."""


class SourceOriginKind(StrEnum):
    """Identify the exact origin semantics of one dataset."""

    FILE = "FILE"
    ODOO = "ODOO"
    DERIVED = "DERIVED"


@dataclass(frozen=True, slots=True)
class FileSourceBinding:
    """Bind a dataset to one immutable registered file table."""

    file_id: str
    table_key: str
    source_sha256: str
    catalog_hash: str
    encoding: str | None
    delimiter: str | None
    header_row: int

    def __post_init__(self) -> None:
        if not self.file_id or not self.table_key:
            raise SourceBindingContractError(
                "File source binding requires a file and table identity"
            )
        canonical_source_hash = (
            self.source_sha256
            if self.source_sha256.startswith("sha256:")
            else f"sha256:{self.source_sha256}"
        )
        _require_hash(canonical_source_hash, "file source hash")
        object.__setattr__(self, "source_sha256", canonical_source_hash)
        _require_hash(self.catalog_hash, "file catalog hash")
        if self.header_row < 1:
            raise SourceBindingContractError(
                "File source binding header row must be positive"
            )

    @property
    def origin(self) -> SourceOriginKind:
        return SourceOriginKind.FILE

    @property
    def source_evidence_hash(self) -> str:
        return self.source_sha256

    def to_dict(self) -> dict[str, object]:
        return {
            "origin": self.origin.value,
            "file_id": self.file_id,
            "table_key": self.table_key,
            "source_sha256": self.source_sha256,
            "catalog_hash": self.catalog_hash,
            "encoding": self.encoding,
            "delimiter": self.delimiter,
            "header_row": self.header_row,
        }


@dataclass(frozen=True, slots=True)
class OdooSourceBinding:
    """Bind a dataset to one protected, identity-bound Odoo capture."""

    capture_selection_hash: str
    model: str
    policy_hash: str
    connection_target_hash: str
    schema_scope_hash: str
    read_principal_hash: str
    read_permission_hash: str
    context_hash: str

    def __post_init__(self) -> None:
        if _TECHNICAL_NAME.fullmatch(self.model) is None:
            raise SourceBindingContractError("Odoo source model is invalid")
        for value, label in (
            (self.capture_selection_hash, "capture selection hash"),
            (self.policy_hash, "source policy hash"),
            (self.connection_target_hash, "connection target hash"),
            (self.schema_scope_hash, "schema scope hash"),
            (self.read_principal_hash, "read principal hash"),
            (self.read_permission_hash, "read permission hash"),
            (self.context_hash, "context hash"),
        ):
            _require_hash(value, label)

    @property
    def origin(self) -> SourceOriginKind:
        return SourceOriginKind.ODOO

    @property
    def source_evidence_hash(self) -> str:
        return content_hash(self.to_dict())

    def to_dict(self) -> dict[str, object]:
        return {
            "origin": self.origin.value,
            "capture_selection_hash": self.capture_selection_hash,
            "model": self.model,
            "policy_hash": self.policy_hash,
            "connection_target_hash": self.connection_target_hash,
            "schema_scope_hash": self.schema_scope_hash,
            "read_principal_hash": self.read_principal_hash,
            "read_permission_hash": self.read_permission_hash,
            "context_hash": self.context_hash,
        }


@dataclass(frozen=True, slots=True)
class DerivedSourceBinding:
    """Bind a logical dataset to an exact structural rule and its inputs."""

    rule_hash: str
    input_dataset_ids: tuple[str, ...]
    data_hash: str

    def __post_init__(self) -> None:
        _require_hash(self.rule_hash, "derived rule hash")
        _require_hash(self.data_hash, "derived data hash")
        if (
            not self.input_dataset_ids
            or self.input_dataset_ids
            != tuple(sorted(set(self.input_dataset_ids)))
        ):
            raise SourceBindingContractError(
                "Derived source inputs must be sorted and unique"
            )

    @property
    def origin(self) -> SourceOriginKind:
        return SourceOriginKind.DERIVED

    @property
    def source_evidence_hash(self) -> str:
        return self.data_hash

    def to_dict(self) -> dict[str, object]:
        return {
            "origin": self.origin.value,
            "rule_hash": self.rule_hash,
            "input_dataset_ids": list(self.input_dataset_ids),
            "data_hash": self.data_hash,
        }


SourceBinding = FileSourceBinding | OdooSourceBinding | DerivedSourceBinding


def source_binding_from_dict(value: object) -> SourceBinding:
    """Decode the one current discriminated binding representation."""

    if not isinstance(value, dict):
        raise SourceBindingContractError("Source binding must be an object")
    try:
        origin = SourceOriginKind(value["origin"])
        if origin is SourceOriginKind.FILE:
            _require_exact_keys(
                value,
                {
                    "origin",
                    "file_id",
                    "table_key",
                    "source_sha256",
                    "catalog_hash",
                    "encoding",
                    "delimiter",
                    "header_row",
                },
            )
            _require_hash(str(value["source_sha256"]), "file source hash")
            return FileSourceBinding(
                file_id=str(value["file_id"]),
                table_key=str(value["table_key"]),
                source_sha256=str(value["source_sha256"]),
                catalog_hash=str(value["catalog_hash"]),
                encoding=value["encoding"],
                delimiter=value["delimiter"],
                header_row=int(value["header_row"]),
            )
        if origin is SourceOriginKind.ODOO:
            _require_exact_keys(
                value,
                {
                    "origin",
                    "capture_selection_hash",
                    "model",
                    "policy_hash",
                    "connection_target_hash",
                    "schema_scope_hash",
                    "read_principal_hash",
                    "read_permission_hash",
                    "context_hash",
                },
            )
            return OdooSourceBinding(
                capture_selection_hash=str(value["capture_selection_hash"]),
                model=str(value["model"]),
                policy_hash=str(value["policy_hash"]),
                connection_target_hash=str(value["connection_target_hash"]),
                schema_scope_hash=str(value["schema_scope_hash"]),
                read_principal_hash=str(value["read_principal_hash"]),
                read_permission_hash=str(value["read_permission_hash"]),
                context_hash=str(value["context_hash"]),
            )
        _require_exact_keys(
            value,
            {"origin", "rule_hash", "input_dataset_ids", "data_hash"},
        )
        return DerivedSourceBinding(
            rule_hash=str(value["rule_hash"]),
            input_dataset_ids=tuple(str(item) for item in value["input_dataset_ids"]),
            data_hash=str(value["data_hash"]),
        )
    except (KeyError, TypeError, ValueError) as error:
        if isinstance(error, SourceBindingContractError):
            raise
        raise SourceBindingContractError("Source binding is invalid") from error


def require_file_source(binding: SourceBinding) -> FileSourceBinding:
    """Return a file binding or fail at a file-only boundary."""

    if not isinstance(binding, FileSourceBinding):
        raise SourceBindingContractError("This operation requires a file source")
    return binding


def _require_hash(value: str, label: str) -> None:
    if _HASH.fullmatch(value) is None:
        raise SourceBindingContractError(f"Source binding {label} is invalid")


def _require_exact_keys(value: dict[object, object], expected: set[str]) -> None:
    if set(value) != expected:
        raise SourceBindingContractError(
            "Source binding fields do not match the current contract"
        )
