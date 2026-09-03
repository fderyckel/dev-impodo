"""Portable Stage 8A read-only destination preflight evidence.

The report compares a fresh, bounded destination read with one exact approved
Stage 7 package.  It deliberately contains only technical identities,
aggregate counts, and one-way hashes: business-key values, numeric Odoo IDs,
credentials, and write receipts stay outside this contract.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
import json
import re
from typing import Any

from impodo.domain.serialization import canonical_json, content_hash
from impodo.domain.shared.access import ActorIdentity


TRANSFER_PREFLIGHT_CONTRACT_VERSION = 1
_HASH = re.compile(r"sha256:[0-9a-f]{64}")
_TECHNICAL_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_.]*")
_BLOCKER_CODE = re.compile(r"[A-Z][A-Z0-9_]*")


def _validate_blockers(values: tuple[str, ...]) -> None:
    if values != tuple(sorted(set(values))) or any(
        _BLOCKER_CODE.fullmatch(value) is None for value in values
    ):
        raise ValueError("Transfer-preflight blocker codes are invalid")


@dataclass(frozen=True, slots=True)
class TransferPreflightDataset:
    """Approved versus freshly observed action counts for one Odoo model."""

    dataset_id: str
    dataset_name: str
    model: str
    model_label: str
    key_field: str
    source_row_count: int
    approved_existing_record_count: int
    approved_create_record_count: int
    observed_existing_record_count: int
    observed_create_record_count: int
    approved_key_binding_hash: str
    observed_key_binding_hash: str
    approved_write_fields: tuple[str, ...]
    observed_compatible_fields: tuple[str, ...]
    blocker_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if any(
            not value.strip()
            for value in (
                self.dataset_id,
                self.dataset_name,
                self.model,
                self.model_label,
                self.key_field,
            )
        ):
            raise ValueError("Transfer-preflight dataset identity is incomplete")
        if any(
            _TECHNICAL_NAME.fullmatch(value) is None
            for value in (self.model, self.key_field)
        ):
            raise ValueError("Transfer-preflight dataset identity is invalid")
        counts = (
            self.source_row_count,
            self.approved_existing_record_count,
            self.approved_create_record_count,
            self.observed_existing_record_count,
            self.observed_create_record_count,
        )
        if any(type(value) is not int or value < 0 for value in counts):
            raise ValueError("Transfer-preflight dataset counts are invalid")
        if (
            self.approved_existing_record_count
            + self.approved_create_record_count
            != self.source_row_count
        ):
            raise ValueError("Approved transfer-preflight counts do not reconcile")
        if any(
            _HASH.fullmatch(value) is None
            for value in (
                self.approved_key_binding_hash,
                self.observed_key_binding_hash,
            )
        ):
            raise ValueError("Transfer-preflight key binding is invalid")
        for fields in (
            self.approved_write_fields,
            self.observed_compatible_fields,
        ):
            if fields != tuple(sorted(set(fields))) or any(
                _TECHNICAL_NAME.fullmatch(value) is None for value in fields
            ):
                raise ValueError("Transfer-preflight field scope is invalid")
        _validate_blockers(self.blocker_codes)

    @property
    def ready(self) -> bool:
        return not self.blocker_codes


@dataclass(frozen=True, slots=True)
class TransferPreflightRelationship:
    """Approved versus freshly observed resolution for one relation field."""

    owner_dataset_id: str
    owner_model: str
    field_name: str
    related_dataset_id: str
    related_model: str
    kind: str
    operation: str
    phase: str
    approved_link_count: int
    approved_reused_link_count: int
    approved_incoming_link_count: int
    observed_link_count: int
    observed_reused_link_count: int
    observed_incoming_link_count: int
    blocker_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if any(
            not value.strip()
            for value in (
                self.owner_dataset_id,
                self.owner_model,
                self.field_name,
                self.related_dataset_id,
                self.related_model,
            )
        ):
            raise ValueError("Transfer-preflight relationship identity is incomplete")
        if any(
            _TECHNICAL_NAME.fullmatch(value) is None
            for value in (self.owner_model, self.field_name, self.related_model)
        ):
            raise ValueError("Transfer-preflight relationship identity is invalid")
        if self.kind not in {"many2one", "many2many"}:
            raise ValueError("Transfer-preflight relationship kind is unsupported")
        if self.operation != ("set" if self.kind == "many2one" else "replace"):
            raise ValueError("Transfer-preflight relationship operation is invalid")
        if self.phase not in {"create_or_update", "post_create"}:
            raise ValueError("Transfer-preflight relationship phase is invalid")
        counts = (
            self.approved_link_count,
            self.approved_reused_link_count,
            self.approved_incoming_link_count,
            self.observed_link_count,
            self.observed_reused_link_count,
            self.observed_incoming_link_count,
        )
        if any(type(value) is not int or value < 0 for value in counts):
            raise ValueError("Transfer-preflight relationship counts are invalid")
        if (
            self.approved_reused_link_count + self.approved_incoming_link_count
            != self.approved_link_count
        ):
            raise ValueError("Approved relationship counts do not reconcile")
        _validate_blockers(self.blocker_codes)

    @property
    def ready(self) -> bool:
        return not self.blocker_codes


@dataclass(frozen=True, slots=True)
class TransferPreflightReport:
    """One immutable 8A dry-run report; it never authorizes an Odoo write."""

    workspace_id: str
    review_package_hash: str
    review_approval_hash: str
    approved_match_plan_hash: str
    fresh_match_plan_hash: str
    source_selection_hash: str
    source_schema_hash: str
    destination_target_hash: str
    destination_credential_binding_hash: str
    destination_read_principal_hash: str
    approved_permission_hash: str
    observed_permission_hash: str
    approved_context_hash: str
    observed_context_hash: str
    destination_schema_snapshot_hash: str
    destination_record_snapshot_hash: str
    datasets: tuple[TransferPreflightDataset, ...]
    relationships: tuple[TransferPreflightRelationship, ...]
    recorded_at: datetime
    recorded_by: ActorIdentity
    blocker_codes: tuple[str, ...] = ()
    contract_version: int = TRANSFER_PREFLIGHT_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if self.contract_version != TRANSFER_PREFLIGHT_CONTRACT_VERSION:
            raise ValueError("Transfer-preflight contract version is unsupported")
        if not self.workspace_id.strip() or self.recorded_at.tzinfo is None:
            raise ValueError("Transfer-preflight provenance is incomplete")
        hashes = (
            self.review_package_hash,
            self.review_approval_hash,
            self.approved_match_plan_hash,
            self.fresh_match_plan_hash,
            self.source_selection_hash,
            self.source_schema_hash,
            self.destination_target_hash,
            self.destination_credential_binding_hash,
            self.destination_read_principal_hash,
            self.approved_permission_hash,
            self.observed_permission_hash,
            self.approved_context_hash,
            self.observed_context_hash,
            self.destination_schema_snapshot_hash,
            self.destination_record_snapshot_hash,
        )
        if any(_HASH.fullmatch(value) is None for value in hashes):
            raise ValueError("Transfer-preflight evidence hash is invalid")
        if self.datasets != tuple(sorted(self.datasets, key=lambda item: item.model)):
            raise ValueError("Transfer-preflight datasets must be model ordered")
        if not self.datasets or len({item.dataset_id for item in self.datasets}) != len(
            self.datasets
        ):
            raise ValueError("Transfer-preflight datasets must be present and unique")
        if self.relationships != tuple(
            sorted(self.relationships, key=lambda item: (item.owner_model, item.field_name))
        ):
            raise ValueError("Transfer-preflight relationships must be ordered")
        if len(
            {(item.owner_model, item.field_name) for item in self.relationships}
        ) != len(self.relationships):
            raise ValueError("Transfer-preflight relationships must be unique")
        _validate_blockers(self.blocker_codes)

    @property
    def ready(self) -> bool:
        return bool(
            not self.blocker_codes
            and all(item.ready for item in self.datasets)
            and all(item.ready for item in self.relationships)
        )

    @property
    def content_hash(self) -> str:
        return content_hash(self.to_dict(include_hash=False))

    def to_dict(self, *, include_hash: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "contract_version": self.contract_version,
            "workspace_id": self.workspace_id,
            "review_package_hash": self.review_package_hash,
            "review_approval_hash": self.review_approval_hash,
            "approved_match_plan_hash": self.approved_match_plan_hash,
            "fresh_match_plan_hash": self.fresh_match_plan_hash,
            "source_selection_hash": self.source_selection_hash,
            "source_schema_hash": self.source_schema_hash,
            "destination_target_hash": self.destination_target_hash,
            "destination_credential_binding_hash": self.destination_credential_binding_hash,
            "destination_read_principal_hash": self.destination_read_principal_hash,
            "approved_permission_hash": self.approved_permission_hash,
            "observed_permission_hash": self.observed_permission_hash,
            "approved_context_hash": self.approved_context_hash,
            "observed_context_hash": self.observed_context_hash,
            "destination_schema_snapshot_hash": self.destination_schema_snapshot_hash,
            "destination_record_snapshot_hash": self.destination_record_snapshot_hash,
            "datasets": [asdict(item) for item in self.datasets],
            "relationships": [asdict(item) for item in self.relationships],
            "recorded_at": self.recorded_at.isoformat(),
            "recorded_by": {
                "issuer": self.recorded_by.issuer,
                "subject_id": self.recorded_by.subject_id,
                "display_name": self.recorded_by.display_name,
            },
            "blocker_codes": list(self.blocker_codes),
        }
        if include_hash:
            payload["content_hash"] = self.content_hash
        return payload

    def to_json(self) -> str:
        return canonical_json(self.to_dict())

    @classmethod
    def from_json(cls, value: str) -> "TransferPreflightReport":
        try:
            payload = json.loads(value)
            actor = dict(payload["recorded_by"])
            result = cls(
                workspace_id=str(payload["workspace_id"]),
                review_package_hash=str(payload["review_package_hash"]),
                review_approval_hash=str(payload["review_approval_hash"]),
                approved_match_plan_hash=str(payload["approved_match_plan_hash"]),
                fresh_match_plan_hash=str(payload["fresh_match_plan_hash"]),
                source_selection_hash=str(payload["source_selection_hash"]),
                source_schema_hash=str(payload["source_schema_hash"]),
                destination_target_hash=str(payload["destination_target_hash"]),
                destination_credential_binding_hash=str(
                    payload["destination_credential_binding_hash"]
                ),
                destination_read_principal_hash=str(
                    payload["destination_read_principal_hash"]
                ),
                approved_permission_hash=str(payload["approved_permission_hash"]),
                observed_permission_hash=str(payload["observed_permission_hash"]),
                approved_context_hash=str(payload["approved_context_hash"]),
                observed_context_hash=str(payload["observed_context_hash"]),
                destination_schema_snapshot_hash=str(
                    payload["destination_schema_snapshot_hash"]
                ),
                destination_record_snapshot_hash=str(
                    payload["destination_record_snapshot_hash"]
                ),
                datasets=tuple(
                    TransferPreflightDataset(
                        dataset_id=str(item["dataset_id"]),
                        dataset_name=str(item["dataset_name"]),
                        model=str(item["model"]),
                        model_label=str(item["model_label"]),
                        key_field=str(item["key_field"]),
                        source_row_count=int(item["source_row_count"]),
                        approved_existing_record_count=int(
                            item["approved_existing_record_count"]
                        ),
                        approved_create_record_count=int(
                            item["approved_create_record_count"]
                        ),
                        observed_existing_record_count=int(
                            item["observed_existing_record_count"]
                        ),
                        observed_create_record_count=int(
                            item["observed_create_record_count"]
                        ),
                        approved_key_binding_hash=str(
                            item["approved_key_binding_hash"]
                        ),
                        observed_key_binding_hash=str(
                            item["observed_key_binding_hash"]
                        ),
                        approved_write_fields=tuple(item["approved_write_fields"]),
                        observed_compatible_fields=tuple(
                            item["observed_compatible_fields"]
                        ),
                        blocker_codes=tuple(item.get("blocker_codes", ())),
                    )
                    for item in payload["datasets"]
                ),
                relationships=tuple(
                    TransferPreflightRelationship(
                        owner_dataset_id=str(item["owner_dataset_id"]),
                        owner_model=str(item["owner_model"]),
                        field_name=str(item["field_name"]),
                        related_dataset_id=str(item["related_dataset_id"]),
                        related_model=str(item["related_model"]),
                        kind=str(item["kind"]),
                        operation=str(item["operation"]),
                        phase=str(item["phase"]),
                        approved_link_count=int(item["approved_link_count"]),
                        approved_reused_link_count=int(
                            item["approved_reused_link_count"]
                        ),
                        approved_incoming_link_count=int(
                            item["approved_incoming_link_count"]
                        ),
                        observed_link_count=int(item["observed_link_count"]),
                        observed_reused_link_count=int(
                            item["observed_reused_link_count"]
                        ),
                        observed_incoming_link_count=int(
                            item["observed_incoming_link_count"]
                        ),
                        blocker_codes=tuple(item.get("blocker_codes", ())),
                    )
                    for item in payload["relationships"]
                ),
                recorded_at=datetime.fromisoformat(str(payload["recorded_at"])),
                recorded_by=ActorIdentity(
                    issuer=str(actor["issuer"]),
                    subject_id=str(actor["subject_id"]),
                    display_name=str(actor["display_name"]),
                ),
                blocker_codes=tuple(payload.get("blocker_codes", ())),
                contract_version=int(payload["contract_version"]),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise ValueError("Stored transfer-preflight report is invalid") from error
        if payload.get("content_hash") != result.content_hash:
            raise ValueError("Stored transfer-preflight report hash is invalid")
        return result
