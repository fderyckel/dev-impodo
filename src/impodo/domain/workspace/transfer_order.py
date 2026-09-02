"""Portable Stage 6 ordering evidence for Odoo-to-Odoo transfers.

The contract stores dataset identities, relationship semantics, deterministic
creation waves, and blockers. It never contains Odoo numeric identifiers,
business-key values, API credentials, or write receipts.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
import json
import re
from typing import Any

from impodo.domain.serialization import canonical_json, content_hash


TRANSFER_ORDER_CONTRACT_VERSION = 1
_HASH = re.compile(r"sha256:[0-9a-f]{64}")
_TECHNICAL_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_.]*")


@dataclass(frozen=True, slots=True)
class TransferOrderDataset:
    """One selected model and its position in the create schedule."""

    dataset_id: str
    dataset_name: str
    model: str
    model_label: str
    source_row_count: int
    destination_existing_key_count: int
    destination_create_key_count: int
    wave: int | None

    def __post_init__(self) -> None:
        if any(
            not value.strip()
            for value in (
                self.dataset_id,
                self.dataset_name,
                self.model,
                self.model_label,
            )
        ):
            raise ValueError("Transfer-order dataset identity is incomplete")
        if _TECHNICAL_NAME.fullmatch(self.model) is None:
            raise ValueError("Transfer-order model is invalid")
        if any(
            value < 0
            for value in (
                self.source_row_count,
                self.destination_existing_key_count,
                self.destination_create_key_count,
            )
        ):
            raise ValueError("Transfer-order dataset counts must be nonnegative")
        if self.wave is not None and self.wave < 1:
            raise ValueError("Transfer-order wave must be positive")


@dataclass(frozen=True, slots=True)
class TransferOrderDependency:
    """One incoming related-record dependency between selected datasets."""

    owner_dataset_id: str
    dependency_dataset_id: str
    owner_model: str
    dependency_model: str
    field_name: str
    field_label: str
    kind: str
    strength: str
    incoming_link_count: int
    deferred: bool

    def __post_init__(self) -> None:
        if any(
            not value.strip()
            for value in (
                self.owner_dataset_id,
                self.dependency_dataset_id,
                self.owner_model,
                self.dependency_model,
                self.field_name,
                self.field_label,
            )
        ):
            raise ValueError("Transfer-order dependency identity is incomplete")
        if any(
            _TECHNICAL_NAME.fullmatch(value) is None
            for value in (
                self.owner_model,
                self.dependency_model,
                self.field_name,
            )
        ):
            raise ValueError("Transfer-order dependency identity is invalid")
        if self.kind not in {"many2one", "many2many"}:
            raise ValueError("Transfer-order dependency kind is unsupported")
        if self.strength not in {"hard", "deferrable"}:
            raise ValueError("Transfer-order dependency strength is invalid")
        if self.incoming_link_count <= 0:
            raise ValueError("Transfer-order dependency must have incoming links")
        if self.deferred and self.strength != "deferrable":
            raise ValueError("A hard transfer dependency cannot be deferred")

    @property
    def is_self_reference(self) -> bool:
        return self.owner_dataset_id == self.dependency_dataset_id


@dataclass(frozen=True, slots=True)
class TransferOrderWave:
    """One parallel-safe group of datasets in the create pass."""

    sequence: int
    dataset_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.sequence < 1 or not self.dataset_ids:
            raise ValueError("Transfer-order wave is invalid")
        if self.dataset_ids != tuple(sorted(set(self.dataset_ids))):
            raise ValueError("Transfer-order wave datasets must be sorted and unique")


@dataclass(frozen=True, slots=True)
class TransferOrderBlocker:
    """One deterministic reason a dataset cannot enter the create schedule."""

    dataset_id: str
    code: str
    field_name: str = ""
    dependency_dataset_id: str = ""

    def __post_init__(self) -> None:
        if not self.dataset_id.strip() or self.code not in {
            "HARD_DEPENDENCY_CYCLE",
            "BLOCKED_DEPENDENCY",
        }:
            raise ValueError("Transfer-order blocker is invalid")
        if self.field_name and _TECHNICAL_NAME.fullmatch(self.field_name) is None:
            raise ValueError("Transfer-order blocker field is invalid")


@dataclass(frozen=True, slots=True)
class TransferOrderPlan:
    """Current Stage 6 decision bound to one ready Stage 5 plan."""

    workspace_id: str
    destination_match_plan_hash: str
    source_selection_hash: str
    source_schema_hash: str
    destination_target_hash: str
    datasets: tuple[TransferOrderDataset, ...]
    dependencies: tuple[TransferOrderDependency, ...]
    waves: tuple[TransferOrderWave, ...]
    blockers: tuple[TransferOrderBlocker, ...]
    recorded_at: datetime
    recorded_by: str
    contract_version: int = TRANSFER_ORDER_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if self.contract_version != TRANSFER_ORDER_CONTRACT_VERSION:
            raise ValueError("Transfer-order contract version is unsupported")
        if not self.workspace_id.strip() or not self.recorded_by.strip():
            raise ValueError("Transfer-order provenance is incomplete")
        for value in (
            self.destination_match_plan_hash,
            self.source_selection_hash,
            self.source_schema_hash,
            self.destination_target_hash,
        ):
            if _HASH.fullmatch(value) is None:
                raise ValueError("Transfer-order evidence hash is invalid")
        if self.recorded_at.tzinfo is None:
            raise ValueError("Transfer-order time must be timezone-aware")
        if self.datasets != tuple(
            sorted(self.datasets, key=lambda item: item.model)
        ):
            raise ValueError("Transfer-order datasets must be model ordered")
        dataset_ids = {item.dataset_id for item in self.datasets}
        if len(dataset_ids) != len(self.datasets) or len(
            {item.model for item in self.datasets}
        ) != len(self.datasets):
            raise ValueError("Transfer-order datasets must be unique")
        if self.dependencies != tuple(
            sorted(
                self.dependencies,
                key=lambda item: (
                    item.owner_model,
                    item.field_name,
                    item.dependency_model,
                ),
            )
        ):
            raise ValueError("Transfer-order dependencies must be ordered")
        if any(
            item.owner_dataset_id not in dataset_ids
            or item.dependency_dataset_id not in dataset_ids
            for item in self.dependencies
        ):
            raise ValueError("Transfer-order dependency dataset is unknown")
        if self.waves != tuple(
            sorted(self.waves, key=lambda item: item.sequence)
        ) or tuple(item.sequence for item in self.waves) != tuple(
            range(1, len(self.waves) + 1)
        ):
            raise ValueError("Transfer-order waves must be contiguous")
        wave_datasets = tuple(
            dataset_id for wave in self.waves for dataset_id in wave.dataset_ids
        )
        if len(set(wave_datasets)) != len(wave_datasets) or any(
            dataset_id not in dataset_ids for dataset_id in wave_datasets
        ):
            raise ValueError("Transfer-order wave membership is invalid")
        wave_by_dataset = {
            dataset_id: wave.sequence
            for wave in self.waves
            for dataset_id in wave.dataset_ids
        }
        if any(item.wave != wave_by_dataset.get(item.dataset_id) for item in self.datasets):
            raise ValueError("Transfer-order dataset wave is inconsistent")
        if self.blockers != tuple(
            sorted(
                self.blockers,
                key=lambda item: (
                    item.dataset_id,
                    item.code,
                    item.field_name,
                    item.dependency_dataset_id,
                ),
            )
        ) or any(item.dataset_id not in dataset_ids for item in self.blockers):
            raise ValueError("Transfer-order blockers are invalid")

    @property
    def ready(self) -> bool:
        return bool(self.datasets) and not self.blockers and all(
            item.wave is not None for item in self.datasets
        )

    @property
    def deferred_dependency_count(self) -> int:
        return sum(1 for item in self.dependencies if item.deferred)

    @property
    def content_hash(self) -> str:
        return content_hash(self.to_dict(include_hash=False))

    def to_dict(self, *, include_hash: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "contract_version": self.contract_version,
            "workspace_id": self.workspace_id,
            "destination_match_plan_hash": self.destination_match_plan_hash,
            "source_selection_hash": self.source_selection_hash,
            "source_schema_hash": self.source_schema_hash,
            "destination_target_hash": self.destination_target_hash,
            "datasets": [asdict(item) for item in self.datasets],
            "dependencies": [asdict(item) for item in self.dependencies],
            "waves": [asdict(item) for item in self.waves],
            "blockers": [asdict(item) for item in self.blockers],
            "recorded_at": self.recorded_at.isoformat(),
            "recorded_by": self.recorded_by,
        }
        if include_hash:
            payload["content_hash"] = self.content_hash
        return payload

    def to_json(self) -> str:
        return canonical_json(self.to_dict())

    @classmethod
    def from_json(cls, value: str) -> "TransferOrderPlan":
        try:
            payload = json.loads(value)
            result = cls(
                workspace_id=str(payload["workspace_id"]),
                destination_match_plan_hash=str(
                    payload["destination_match_plan_hash"]
                ),
                source_selection_hash=str(payload["source_selection_hash"]),
                source_schema_hash=str(payload["source_schema_hash"]),
                destination_target_hash=str(payload["destination_target_hash"]),
                datasets=tuple(
                    TransferOrderDataset(
                        dataset_id=str(item["dataset_id"]),
                        dataset_name=str(item["dataset_name"]),
                        model=str(item["model"]),
                        model_label=str(item["model_label"]),
                        source_row_count=int(item["source_row_count"]),
                        destination_existing_key_count=int(
                            item["destination_existing_key_count"]
                        ),
                        destination_create_key_count=int(
                            item["destination_create_key_count"]
                        ),
                        wave=int(item["wave"]) if item["wave"] is not None else None,
                    )
                    for item in payload["datasets"]
                ),
                dependencies=tuple(
                    TransferOrderDependency(
                        owner_dataset_id=str(item["owner_dataset_id"]),
                        dependency_dataset_id=str(item["dependency_dataset_id"]),
                        owner_model=str(item["owner_model"]),
                        dependency_model=str(item["dependency_model"]),
                        field_name=str(item["field_name"]),
                        field_label=str(item["field_label"]),
                        kind=str(item["kind"]),
                        strength=str(item["strength"]),
                        incoming_link_count=int(item["incoming_link_count"]),
                        deferred=bool(item["deferred"]),
                    )
                    for item in payload["dependencies"]
                ),
                waves=tuple(
                    TransferOrderWave(
                        sequence=int(item["sequence"]),
                        dataset_ids=tuple(item["dataset_ids"]),
                    )
                    for item in payload["waves"]
                ),
                blockers=tuple(
                    TransferOrderBlocker(
                        dataset_id=str(item["dataset_id"]),
                        code=str(item["code"]),
                        field_name=str(item.get("field_name", "")),
                        dependency_dataset_id=str(
                            item.get("dependency_dataset_id", "")
                        ),
                    )
                    for item in payload["blockers"]
                ),
                recorded_at=datetime.fromisoformat(str(payload["recorded_at"])),
                recorded_by=str(payload["recorded_by"]),
                contract_version=int(payload["contract_version"]),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise ValueError("Stored transfer-order plan is invalid") from error
        if payload.get("content_hash") != result.content_hash:
            raise ValueError("Stored transfer-order plan hash is invalid")
        return result
