"""Define deterministic evidence for immutable columnar source snapshots.

CSV and XLSX remain governed ingestion formats.  A later adapter will encode
each accepted source cell into two Parquet columns: an ordinal-derived UTF-8
value column containing the exact Python text used by current scalar mapping
semantics, and a compact unsigned kind column used to restore the original
source scalar for the Python oracle.  This mapping-independent representation
keeps user headers out of physical paths and column identifiers, preserves
mixed XLSX columns, and lets a Polars plan project only the value columns it
needs.

This module is a domain contract only.  Slice 1 deliberately does not publish
Parquet files or change the production source reader.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from enum import IntEnum
import json
import math
from pathlib import PurePosixPath
import re
from typing import Any, Iterable

from .serialization import canonical_json, content_hash


SOURCE_SNAPSHOT_CONTRACT_VERSION = 1
SOURCE_READER_CONTRACT_VERSION = 1
SOURCE_ROW_COLUMN = "__impodo_source_row"
SOURCE_VALUE_PHYSICAL_TYPE = "utf8"
SOURCE_KIND_PHYSICAL_TYPE = "uint8"

_DATASET_ID = re.compile(r"dataset:([0-9a-f]{24})")
_SHA256 = re.compile(r"sha256:([0-9a-f]{64})")
_DURATION = re.compile(
    r"(?:(?P<days>-?\d+) day(?:s)?, )?"
    r"(?P<hours>\d+):(?P<minutes>\d{2}):(?P<seconds>\d{2})"
    r"(?:\.(?P<microseconds>\d{1,6}))?"
)


class SourceSnapshotContractError(ValueError):
    """Raised when source snapshot evidence violates its deterministic contract."""


class SourceCellKind(IntEnum):
    """Stable physical codes for scalar values accepted by the strict reader."""

    NULL = 0
    STRING = 1
    BOOLEAN = 2
    INTEGER = 3
    FLOAT = 4
    DECIMAL = 5
    DATE = 6
    DATETIME = 7
    TIME = 8
    DURATION = 9


@dataclass(frozen=True, slots=True)
class EncodedSourceCell:
    """One lossless source scalar encoded for the snapshot's tagged UTF-8 layout."""

    kind: SourceCellKind
    text: str | None

    def __post_init__(self) -> None:
        if self.kind is SourceCellKind.NULL:
            if self.text is not None:
                raise SourceSnapshotContractError(
                    "A null source cell cannot contain text"
                )
        elif self.text is None:
            raise SourceSnapshotContractError(
                "A non-null source cell must contain text"
            )

    @classmethod
    def from_python(cls, value: Any) -> "EncodedSourceCell":
        """Encode one strict-reader scalar without losing type or text semantics."""

        if value is None:
            return cls(SourceCellKind.NULL, None)
        if isinstance(value, str):
            return cls(SourceCellKind.STRING, value)
        if isinstance(value, bool):
            return cls(SourceCellKind.BOOLEAN, str(value))
        if isinstance(value, int):
            return cls(SourceCellKind.INTEGER, str(value))
        if isinstance(value, float):
            if not math.isfinite(value):
                raise SourceSnapshotContractError(
                    "Non-finite source numbers are not supported"
                )
            return cls(SourceCellKind.FLOAT, str(value))
        if isinstance(value, Decimal):
            if not value.is_finite():
                raise SourceSnapshotContractError(
                    "Non-finite source decimals are not supported"
                )
            return cls(SourceCellKind.DECIMAL, str(value))
        if isinstance(value, datetime):
            return cls(SourceCellKind.DATETIME, str(value))
        if isinstance(value, date):
            return cls(SourceCellKind.DATE, str(value))
        if isinstance(value, time):
            return cls(SourceCellKind.TIME, str(value))
        if isinstance(value, timedelta):
            return cls(SourceCellKind.DURATION, str(value))
        raise SourceSnapshotContractError(
            f"Unsupported source scalar type: {type(value).__name__}"
        )

    def to_python(self) -> Any:
        """Restore the scalar type consumed by the existing Python evaluator."""

        if self.kind is SourceCellKind.NULL:
            return None
        assert self.text is not None
        if self.kind is SourceCellKind.STRING:
            return self.text
        if self.kind is SourceCellKind.BOOLEAN:
            if self.text == "True":
                return True
            if self.text == "False":
                return False
            raise SourceSnapshotContractError("Invalid encoded Boolean source cell")
        if self.kind is SourceCellKind.INTEGER:
            try:
                return int(self.text, 10)
            except ValueError as error:
                raise SourceSnapshotContractError(
                    "Invalid encoded integer source cell"
                ) from error
        if self.kind is SourceCellKind.FLOAT:
            try:
                value = float(self.text)
            except ValueError as error:
                raise SourceSnapshotContractError(
                    "Invalid encoded floating-point source cell"
                ) from error
            if not math.isfinite(value):
                raise SourceSnapshotContractError(
                    "Invalid encoded floating-point source cell"
                )
            return value
        if self.kind is SourceCellKind.DECIMAL:
            try:
                value = Decimal(self.text)
            except Exception as error:
                raise SourceSnapshotContractError(
                    "Invalid encoded decimal source cell"
                ) from error
            if not value.is_finite():
                raise SourceSnapshotContractError(
                    "Invalid encoded decimal source cell"
                )
            return value
        if self.kind is SourceCellKind.DATE:
            try:
                return date.fromisoformat(self.text)
            except ValueError as error:
                raise SourceSnapshotContractError(
                    "Invalid encoded date source cell"
                ) from error
        if self.kind is SourceCellKind.DATETIME:
            try:
                return datetime.fromisoformat(self.text)
            except ValueError as error:
                raise SourceSnapshotContractError(
                    "Invalid encoded datetime source cell"
                ) from error
        if self.kind is SourceCellKind.TIME:
            try:
                return time.fromisoformat(self.text)
            except ValueError as error:
                raise SourceSnapshotContractError(
                    "Invalid encoded time source cell"
                ) from error
        if self.kind is SourceCellKind.DURATION:
            return _decode_duration(self.text)
        raise SourceSnapshotContractError("Unknown encoded source cell kind")

    def to_portable_dict(self) -> dict[str, object]:
        """Return the exact physical kind/text pair used by a future writer."""

        return {"kind": int(self.kind), "text": self.text}

    @classmethod
    def from_portable_dict(cls, payload: dict[str, object]) -> "EncodedSourceCell":
        """Restore and validate one physical kind/text pair."""

        try:
            kind = SourceCellKind(int(payload["kind"]))
        except (KeyError, TypeError, ValueError) as error:
            raise SourceSnapshotContractError(
                "Invalid encoded source cell kind"
            ) from error
        text_value = payload.get("text")
        if text_value is not None and not isinstance(text_value, str):
            raise SourceSnapshotContractError(
                "Encoded source cell text must be a string or null"
            )
        cell = cls(kind=kind, text=text_value)
        restored = cell.to_python()
        if restored is not None and str(restored) != text_value:
            raise SourceSnapshotContractError(
                "Encoded source cell text is not canonical"
            )
        return cell


@dataclass(frozen=True, slots=True)
class SourceSnapshotColumn:
    """Bind one logical source column to safe deterministic Parquet columns."""

    ordinal: int
    stable_key: str
    source_name: str
    candidate_type: str
    value_column: str
    kind_column: str

    def __post_init__(self) -> None:
        if self.ordinal < 1:
            raise SourceSnapshotContractError("Source column ordinal must be positive")
        if not self.stable_key:
            raise SourceSnapshotContractError("Source column stable key is required")
        if not self.source_name:
            raise SourceSnapshotContractError("Source column display name is required")
        if not self.candidate_type:
            raise SourceSnapshotContractError("Source candidate type is required")
        if self.value_column != source_value_column(self.ordinal):
            raise SourceSnapshotContractError(
                "Source value column does not match its ordinal"
            )
        if self.kind_column != source_kind_column(self.ordinal):
            raise SourceSnapshotContractError(
                "Source kind column does not match its ordinal"
            )

    @classmethod
    def create(
        cls,
        *,
        ordinal: int,
        stable_key: str,
        source_name: str,
        candidate_type: str,
    ) -> "SourceSnapshotColumn":
        """Create safe physical identifiers from a governed source ordinal."""

        return cls(
            ordinal=ordinal,
            stable_key=stable_key,
            source_name=source_name,
            candidate_type=candidate_type,
            value_column=source_value_column(ordinal),
            kind_column=source_kind_column(ordinal),
        )

    def to_portable_dict(self) -> dict[str, object]:
        return {
            "candidate_type": self.candidate_type,
            "kind_column": self.kind_column,
            "kind_physical_type": SOURCE_KIND_PHYSICAL_TYPE,
            "ordinal": self.ordinal,
            "source_name": self.source_name,
            "stable_key": self.stable_key,
            "value_column": self.value_column,
            "value_physical_type": SOURCE_VALUE_PHYSICAL_TYPE,
        }


@dataclass(frozen=True, slots=True)
class SourceSnapshotSchema:
    """Mapping-independent deterministic physical schema for one dataset."""

    contract_version: int
    source_row_column: str
    columns: tuple[SourceSnapshotColumn, ...]

    def __post_init__(self) -> None:
        if self.contract_version != SOURCE_SNAPSHOT_CONTRACT_VERSION:
            raise SourceSnapshotContractError(
                "Unsupported source snapshot contract version"
            )
        if self.source_row_column != SOURCE_ROW_COLUMN:
            raise SourceSnapshotContractError("Source row column is not canonical")
        if not self.columns:
            raise SourceSnapshotContractError(
                "A source snapshot schema requires at least one source column"
            )
        ordinals = tuple(item.ordinal for item in self.columns)
        if ordinals != tuple(sorted(ordinals)) or len(set(ordinals)) != len(ordinals):
            raise SourceSnapshotContractError(
                "Source snapshot columns must have unique sorted ordinals"
            )
        stable_keys = tuple(item.stable_key for item in self.columns)
        if len(set(stable_keys)) != len(stable_keys):
            raise SourceSnapshotContractError(
                "Source snapshot stable column keys must be unique"
            )

    @classmethod
    def create(
        cls,
        columns: Iterable[SourceSnapshotColumn],
    ) -> "SourceSnapshotSchema":
        return cls(
            contract_version=SOURCE_SNAPSHOT_CONTRACT_VERSION,
            source_row_column=SOURCE_ROW_COLUMN,
            columns=tuple(columns),
        )

    @property
    def content_hash(self) -> str:
        return content_hash(self.to_portable_dict())

    def to_portable_dict(self) -> dict[str, object]:
        return {
            "columns": [item.to_portable_dict() for item in self.columns],
            "contract_version": self.contract_version,
            "source_row_column": self.source_row_column,
            "source_row_physical_type": "int64",
        }

    @classmethod
    def from_portable_dict(
        cls,
        payload: dict[str, object],
    ) -> "SourceSnapshotSchema":
        raw_columns = payload.get("columns")
        if not isinstance(raw_columns, list):
            raise SourceSnapshotContractError("Source snapshot columns are missing")
        if payload.get("source_row_physical_type") != "int64":
            raise SourceSnapshotContractError(
                "Source row physical type is not canonical"
            )
        try:
            for item in raw_columns:
                if not isinstance(item, dict):
                    raise TypeError
                if (
                    item.get("value_physical_type")
                    != SOURCE_VALUE_PHYSICAL_TYPE
                    or item.get("kind_physical_type")
                    != SOURCE_KIND_PHYSICAL_TYPE
                ):
                    raise SourceSnapshotContractError(
                        "Source column physical type is not canonical"
                    )
            columns = tuple(
                SourceSnapshotColumn(
                    ordinal=int(item["ordinal"]),
                    stable_key=str(item["stable_key"]),
                    source_name=str(item["source_name"]),
                    candidate_type=str(item["candidate_type"]),
                    value_column=str(item["value_column"]),
                    kind_column=str(item["kind_column"]),
                )
                for item in raw_columns
            )
            return cls(
                contract_version=int(payload["contract_version"]),
                source_row_column=str(payload["source_row_column"]),
                columns=columns,
            )
        except (KeyError, TypeError, ValueError) as error:
            if isinstance(error, SourceSnapshotContractError):
                raise
            raise SourceSnapshotContractError(
                "Source snapshot schema is invalid"
            ) from error


@dataclass(frozen=True, slots=True)
class SourceSnapshot:
    """Manifest binding one immutable Parquet artifact to governed source evidence."""

    project_id: str
    dataset_id: str
    dataset_name: str
    file_id: str
    table_key: str
    source_sha256: str
    catalog_hash: str
    physical_selection_hash: str
    reader_contract_version: int
    schema: SourceSnapshotSchema
    row_count: int
    logical_hash: str
    parquet_storage_key: str
    parquet_sha256: str
    created_at: datetime

    def __post_init__(self) -> None:
        for value, label in (
            (self.project_id, "project ID"),
            (self.dataset_name, "dataset name"),
            (self.file_id, "source file ID"),
            (self.table_key, "source table key"),
        ):
            if not value:
                raise SourceSnapshotContractError(f"Source snapshot {label} is required")
        _dataset_digest(self.dataset_id)
        for value, label in (
            (self.source_sha256, "source hash"),
            (self.catalog_hash, "catalog hash"),
            (self.physical_selection_hash, "physical selection hash"),
            (self.logical_hash, "logical hash"),
            (self.parquet_sha256, "Parquet hash"),
        ):
            _hash_digest(value, label)
        if self.reader_contract_version < 1:
            raise SourceSnapshotContractError(
                "Source reader contract version must be positive"
            )
        if self.row_count < 0:
            raise SourceSnapshotContractError("Source snapshot row count is negative")
        if self.created_at.tzinfo is None:
            raise SourceSnapshotContractError(
                "Source snapshot creation time must be timezone-aware"
            )
        if self.logical_hash != self.expected_logical_hash:
            raise SourceSnapshotContractError("Source snapshot logical hash is invalid")
        expected_key = source_snapshot_storage_key(
            self.dataset_id,
            self.logical_hash,
            self.parquet_sha256,
        )
        if self.parquet_storage_key != expected_key:
            raise SourceSnapshotContractError(
                "Source snapshot storage key is not content-addressed"
            )

    @classmethod
    def create(
        cls,
        *,
        project_id: str,
        dataset_id: str,
        dataset_name: str,
        file_id: str,
        table_key: str,
        source_sha256: str,
        catalog_hash: str,
        physical_selection_hash: str,
        schema: SourceSnapshotSchema,
        row_count: int,
        parquet_sha256: str,
        created_at: datetime,
        reader_contract_version: int = SOURCE_READER_CONTRACT_VERSION,
    ) -> "SourceSnapshot":
        logical_hash = source_snapshot_logical_hash(
            project_id=project_id,
            dataset_id=dataset_id,
            dataset_name=dataset_name,
            file_id=file_id,
            table_key=table_key,
            source_sha256=source_sha256,
            catalog_hash=catalog_hash,
            physical_selection_hash=physical_selection_hash,
            reader_contract_version=reader_contract_version,
            schema_hash=schema.content_hash,
            row_count=row_count,
        )
        return cls(
            project_id=project_id,
            dataset_id=dataset_id,
            dataset_name=dataset_name,
            file_id=file_id,
            table_key=table_key,
            source_sha256=source_sha256,
            catalog_hash=catalog_hash,
            physical_selection_hash=physical_selection_hash,
            reader_contract_version=reader_contract_version,
            schema=schema,
            row_count=row_count,
            logical_hash=logical_hash,
            parquet_storage_key=source_snapshot_storage_key(
                dataset_id,
                logical_hash,
                parquet_sha256,
            ),
            parquet_sha256=parquet_sha256,
            created_at=created_at,
        )

    @property
    def expected_logical_hash(self) -> str:
        return source_snapshot_logical_hash(
            project_id=self.project_id,
            dataset_id=self.dataset_id,
            dataset_name=self.dataset_name,
            file_id=self.file_id,
            table_key=self.table_key,
            source_sha256=self.source_sha256,
            catalog_hash=self.catalog_hash,
            physical_selection_hash=self.physical_selection_hash,
            reader_contract_version=self.reader_contract_version,
            schema_hash=self.schema.content_hash,
            row_count=self.row_count,
        )

    @property
    def content_hash(self) -> str:
        """Bind the logical snapshot to the exact immutable Parquet artifact."""

        return content_hash(
            {
                "logical_hash": self.logical_hash,
                "parquet_sha256": self.parquet_sha256,
                "parquet_storage_key": self.parquet_storage_key,
            }
        )

    def to_portable_dict(self) -> dict[str, object]:
        return {
            "catalog_hash": self.catalog_hash,
            "content_hash": self.content_hash,
            "created_at": self.created_at.isoformat(),
            "dataset_id": self.dataset_id,
            "dataset_name": self.dataset_name,
            "file_id": self.file_id,
            "logical_hash": self.logical_hash,
            "parquet_sha256": self.parquet_sha256,
            "parquet_storage_key": self.parquet_storage_key,
            "physical_selection_hash": self.physical_selection_hash,
            "project_id": self.project_id,
            "reader_contract_version": self.reader_contract_version,
            "row_count": self.row_count,
            "schema": self.schema.to_portable_dict(),
            "source_sha256": self.source_sha256,
            "table_key": self.table_key,
        }

    def to_json(self) -> str:
        return canonical_json(self.to_portable_dict())

    @classmethod
    def from_json(cls, value: str) -> "SourceSnapshot":
        try:
            payload = json.loads(value)
            schema_payload = payload["schema"]
            if not isinstance(payload, dict) or not isinstance(schema_payload, dict):
                raise TypeError
            snapshot = cls(
                project_id=str(payload["project_id"]),
                dataset_id=str(payload["dataset_id"]),
                dataset_name=str(payload["dataset_name"]),
                file_id=str(payload["file_id"]),
                table_key=str(payload["table_key"]),
                source_sha256=str(payload["source_sha256"]),
                catalog_hash=str(payload["catalog_hash"]),
                physical_selection_hash=str(payload["physical_selection_hash"]),
                reader_contract_version=int(payload["reader_contract_version"]),
                schema=SourceSnapshotSchema.from_portable_dict(schema_payload),
                row_count=int(payload["row_count"]),
                logical_hash=str(payload["logical_hash"]),
                parquet_storage_key=str(payload["parquet_storage_key"]),
                parquet_sha256=str(payload["parquet_sha256"]),
                created_at=datetime.fromisoformat(str(payload["created_at"])),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            if isinstance(error, SourceSnapshotContractError):
                raise
            raise SourceSnapshotContractError(
                "Source snapshot manifest is invalid"
            ) from error
        if payload.get("content_hash") != snapshot.content_hash:
            raise SourceSnapshotContractError(
                "Source snapshot manifest content hash is invalid"
            )
        return snapshot


def source_value_column(ordinal: int) -> str:
    """Return the safe Parquet value-column name for one source ordinal."""

    if ordinal < 1:
        raise SourceSnapshotContractError("Source column ordinal must be positive")
    return f"v_{ordinal:06d}"


def source_kind_column(ordinal: int) -> str:
    """Return the safe Parquet kind-column name for one source ordinal."""

    if ordinal < 1:
        raise SourceSnapshotContractError("Source column ordinal must be positive")
    return f"k_{ordinal:06d}"


def source_snapshot_storage_key(
    dataset_id: str,
    logical_hash: str,
    parquet_sha256: str,
) -> str:
    """Return a project-relative path containing no caller-controlled segment."""

    dataset_digest = _dataset_digest(dataset_id)
    snapshot_digest = _hash_digest(logical_hash, "logical hash")
    artifact_digest = _hash_digest(parquet_sha256, "Parquet hash")
    return str(
        PurePosixPath(
            "snapshots",
            "source",
            f"v{SOURCE_SNAPSHOT_CONTRACT_VERSION}",
            dataset_digest,
            snapshot_digest,
            f"{artifact_digest}.parquet",
        )
    )


def source_snapshot_logical_hash(
    *,
    project_id: str,
    dataset_id: str,
    dataset_name: str,
    file_id: str,
    table_key: str,
    source_sha256: str,
    catalog_hash: str,
    physical_selection_hash: str,
    reader_contract_version: int,
    schema_hash: str,
    row_count: int,
) -> str:
    """Hash governed logical content independently of write time and file bytes."""

    return content_hash(
        {
            "catalog_hash": catalog_hash,
            "contract_version": SOURCE_SNAPSHOT_CONTRACT_VERSION,
            "dataset_id": dataset_id,
            "dataset_name": dataset_name,
            "file_id": file_id,
            "physical_selection_hash": physical_selection_hash,
            "project_id": project_id,
            "reader_contract_version": reader_contract_version,
            "row_count": row_count,
            "schema_hash": schema_hash,
            "source_sha256": source_sha256,
            "table_key": table_key,
        }
    )


def _dataset_digest(value: str) -> str:
    match = _DATASET_ID.fullmatch(value)
    if match is None:
        raise SourceSnapshotContractError("Source snapshot dataset ID is invalid")
    return match.group(1)


def _hash_digest(value: str, label: str) -> str:
    match = _SHA256.fullmatch(value)
    if match is None:
        raise SourceSnapshotContractError(f"Source snapshot {label} is invalid")
    return match.group(1)


def _decode_duration(value: str) -> timedelta:
    match = _DURATION.fullmatch(value)
    if match is None:
        raise SourceSnapshotContractError("Invalid encoded duration source cell")
    microseconds = (match.group("microseconds") or "").ljust(6, "0")
    minutes = int(match.group("minutes"))
    seconds = int(match.group("seconds"))
    if minutes >= 60 or seconds >= 60:
        raise SourceSnapshotContractError("Invalid encoded duration source cell")
    return timedelta(
        days=int(match.group("days") or 0),
        hours=int(match.group("hours")),
        minutes=minutes,
        seconds=seconds,
        microseconds=int(microseconds or 0),
    )
