"""Portable Stage 7 review and approval evidence for Odoo transfers.

The review package binds the exact Stage 5 matching decision, Stage 6 order,
destination snapshot, write scope, relationship operations, and control totals
to the canonical frozen export-plan contract. It contains no business values,
numeric Odoo identifiers, credentials, or write receipts.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
import json
import re
from typing import Any

from impodo.domain.cutover.approvals import (
    ApprovalEvidence,
    ExportPlanApproval,
    FrozenExportPlan,
)
from impodo.domain.serialization import canonical_json, content_hash
from impodo.domain.shared.access import Actor, ActorIdentity, Capability


TRANSFER_REVIEW_CONTRACT_VERSION = 1
TRANSFER_REVIEW_APPROVAL_CONTRACT_VERSION = 1
TRANSFER_REVIEW_POLICY_VERSION = "odoo-transfer-review-v1"
_HASH = re.compile(r"sha256:[0-9a-f]{64}")
_TECHNICAL_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_.]*")
_SOURCE_HASH_NAMES = frozenset(
    {
        "destination_context",
        "destination_matching",
        "destination_permission",
        "destination_target",
        "data_version",
        "source_schema",
        "source_selection",
        "transfer_order",
    }
)


@dataclass(frozen=True, slots=True)
class TransferReviewDataset:
    """One record type and the exact fields proposed for destination writes."""

    dataset_id: str
    dataset_name: str
    model: str
    model_label: str
    key_field: str
    key_field_label: str
    source_row_count: int
    destination_existing_record_count: int
    destination_create_record_count: int
    wave: int
    scalar_write_fields: tuple[str, ...]
    relationship_write_fields: tuple[str, ...]

    def __post_init__(self) -> None:
        if any(
            not value.strip()
            for value in (
                self.dataset_id,
                self.dataset_name,
                self.model,
                self.model_label,
                self.key_field,
                self.key_field_label,
            )
        ):
            raise ValueError("Transfer-review dataset identity is incomplete")
        if any(
            _TECHNICAL_NAME.fullmatch(value) is None
            for value in (self.model, self.key_field)
        ):
            raise ValueError("Transfer-review dataset identity is invalid")
        if any(
            value < 0
            for value in (
                self.source_row_count,
                self.destination_existing_record_count,
                self.destination_create_record_count,
            )
        ) or self.wave < 1:
            raise ValueError("Transfer-review dataset totals are invalid")
        if (
            self.destination_existing_record_count
            + self.destination_create_record_count
            != self.source_row_count
        ):
            raise ValueError("Transfer-review dataset totals are inconsistent")
        for fields in (self.scalar_write_fields, self.relationship_write_fields):
            if fields != tuple(sorted(set(fields))) or any(
                _TECHNICAL_NAME.fullmatch(value) is None for value in fields
            ):
                raise ValueError("Transfer-review write fields are invalid")
        if set(self.scalar_write_fields).intersection(
            self.relationship_write_fields
        ):
            raise ValueError("Transfer-review write fields overlap")


@dataclass(frozen=True, slots=True)
class TransferReviewRelationship:
    """One approved generic relationship operation and its execution phase."""

    owner_dataset_id: str
    related_dataset_id: str
    owner_model: str
    owner_model_label: str
    related_model: str
    related_model_label: str
    field_name: str
    field_label: str
    related_key_field: str
    kind: str
    operation: str
    inverse_field: str | None
    required: bool
    source_link_count: int
    destination_reused_link_count: int
    incoming_link_count: int
    phase: str

    def __post_init__(self) -> None:
        if any(
            not value.strip()
            for value in (
                self.owner_dataset_id,
                self.related_dataset_id,
                self.owner_model,
                self.owner_model_label,
                self.related_model,
                self.related_model_label,
                self.field_name,
                self.field_label,
                self.related_key_field,
            )
        ):
            raise ValueError("Transfer-review relationship identity is incomplete")
        technical = (
            self.owner_model,
            self.related_model,
            self.field_name,
            self.related_key_field,
        )
        if any(_TECHNICAL_NAME.fullmatch(value) is None for value in technical):
            raise ValueError("Transfer-review relationship identity is invalid")
        if self.kind not in {"many2one", "many2many"}:
            raise ValueError("Transfer-review relationship kind is unsupported")
        expected_operation = "set" if self.kind == "many2one" else "replace"
        if self.operation != expected_operation:
            raise ValueError("Transfer-review relationship operation is invalid")
        if self.inverse_field is not None and (
            _TECHNICAL_NAME.fullmatch(self.inverse_field) is None
        ):
            raise ValueError("Transfer-review relationship inverse is invalid")
        if any(
            value < 0
            for value in (
                self.source_link_count,
                self.destination_reused_link_count,
                self.incoming_link_count,
            )
        ) or (
            self.destination_reused_link_count + self.incoming_link_count
            != self.source_link_count
        ):
            raise ValueError("Transfer-review relationship totals are inconsistent")
        if self.phase not in {"create_or_update", "post_create"}:
            raise ValueError("Transfer-review relationship phase is invalid")
        if self.phase == "post_create" and self.incoming_link_count == 0:
            raise ValueError("A post-create relationship must resolve incoming links")


@dataclass(frozen=True, slots=True)
class TransferReviewTotals:
    """Reconciled package-level control totals."""

    dataset_count: int
    wave_count: int
    source_record_count: int
    destination_existing_record_count: int
    destination_create_record_count: int
    scalar_write_field_count: int
    relationship_write_field_count: int
    source_relationship_link_count: int
    destination_reused_link_count: int
    incoming_link_count: int
    post_create_link_count: int

    def __post_init__(self) -> None:
        values = tuple(asdict(self).values())
        if any(type(value) is not int or value < 0 for value in values):
            raise ValueError("Transfer-review control totals are invalid")
        if self.dataset_count == 0 or self.wave_count == 0:
            raise ValueError("Transfer-review control totals are incomplete")
        if (
            self.destination_existing_record_count
            + self.destination_create_record_count
            != self.source_record_count
        ):
            raise ValueError("Transfer-review record controls do not reconcile")
        if (
            self.destination_reused_link_count + self.incoming_link_count
            != self.source_relationship_link_count
        ):
            raise ValueError("Transfer-review relationship controls do not reconcile")
        if self.post_create_link_count > self.source_relationship_link_count:
            raise ValueError("Transfer-review post-create controls are invalid")


@dataclass(frozen=True, slots=True)
class TransferReviewPackage:
    """One frozen, reviewable execution scope before write authorization."""

    export_plan: FrozenExportPlan
    datasets: tuple[TransferReviewDataset, ...]
    relationships: tuple[TransferReviewRelationship, ...]
    totals: TransferReviewTotals
    built_by: ActorIdentity
    matched_record_policy: str = "update_selected_fields"
    missing_record_policy: str = "create"
    unmatched_destination_policy: str = "leave_unchanged"
    contract_version: int = TRANSFER_REVIEW_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if self.contract_version != TRANSFER_REVIEW_CONTRACT_VERSION:
            raise ValueError("Transfer-review contract version is unsupported")
        if self.matched_record_policy != "update_selected_fields":
            raise ValueError("Transfer-review matched-record policy is invalid")
        if self.missing_record_policy != "create":
            raise ValueError("Transfer-review missing-record policy is invalid")
        if self.unmatched_destination_policy != "leave_unchanged":
            raise ValueError("Transfer-review unmatched-record policy is invalid")
        if set(self.export_plan.source_hashes) != _SOURCE_HASH_NAMES:
            raise ValueError("Transfer-review source bindings are incomplete")
        if self.export_plan.mapping_hash != self.destination_match_plan_hash:
            raise ValueError("Transfer-review matching binding is inconsistent")
        if self.export_plan.canonical_dataset_hash != self.source_selection_hash:
            raise ValueError("Transfer-review source binding is inconsistent")
        if self.datasets != tuple(sorted(self.datasets, key=lambda item: item.model)):
            raise ValueError("Transfer-review datasets must be model ordered")
        dataset_ids = {item.dataset_id for item in self.datasets}
        if len(dataset_ids) != len(self.datasets) or len(
            {item.model for item in self.datasets}
        ) != len(self.datasets):
            raise ValueError("Transfer-review datasets must be unique")
        expected_relationships = tuple(
            sorted(
                self.relationships,
                key=lambda item: (item.owner_model, item.field_name),
            )
        )
        if self.relationships != expected_relationships or len(
            {(item.owner_model, item.field_name) for item in self.relationships}
        ) != len(self.relationships):
            raise ValueError("Transfer-review relationships must be ordered and unique")
        if any(
            item.owner_dataset_id not in dataset_ids
            or item.related_dataset_id not in dataset_ids
            for item in self.relationships
        ):
            raise ValueError("Transfer-review relationship dataset is unknown")
        relationship_fields: dict[str, set[str]] = {
            dataset_id: set() for dataset_id in dataset_ids
        }
        for item in self.relationships:
            relationship_fields[item.owner_dataset_id].add(item.field_name)
        if any(
            set(item.relationship_write_fields) != relationship_fields[item.dataset_id]
            for item in self.datasets
        ):
            raise ValueError("Transfer-review relationship write scope is inconsistent")
        if self.totals != _totals(self.datasets, self.relationships):
            raise ValueError("Transfer-review control totals do not match the package")
        if self.export_plan.actions_hash != transfer_review_actions_hash(
            self.datasets,
            self.relationships,
            self.totals,
        ):
            raise ValueError("Transfer-review action binding is inconsistent")

    @property
    def workspace_id(self) -> str:
        return self.export_plan.workspace_id

    @property
    def source_selection_hash(self) -> str:
        return self.export_plan.source_hashes["source_selection"]

    @property
    def source_schema_hash(self) -> str:
        return self.export_plan.source_hashes["source_schema"]

    @property
    def destination_match_plan_hash(self) -> str:
        return self.export_plan.source_hashes["destination_matching"]

    @property
    def transfer_order_plan_hash(self) -> str:
        return self.export_plan.source_hashes["transfer_order"]

    @property
    def destination_target_hash(self) -> str:
        return self.export_plan.source_hashes["destination_target"]

    @property
    def content_hash(self) -> str:
        return content_hash(self.to_dict(include_hash=False))

    def to_dict(self, *, include_hash: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "contract_version": self.contract_version,
            "export_plan": _export_plan_dict(self.export_plan),
            "datasets": [asdict(item) for item in self.datasets],
            "relationships": [asdict(item) for item in self.relationships],
            "totals": asdict(self.totals),
            "built_by": _actor_dict(self.built_by),
            "matched_record_policy": self.matched_record_policy,
            "missing_record_policy": self.missing_record_policy,
            "unmatched_destination_policy": self.unmatched_destination_policy,
        }
        if include_hash:
            payload["content_hash"] = self.content_hash
        return payload

    def to_json(self) -> str:
        return canonical_json(self.to_dict())

    @classmethod
    def from_json(cls, value: str) -> "TransferReviewPackage":
        try:
            payload = json.loads(value)
            result = cls(
                export_plan=_export_plan_from_dict(payload["export_plan"]),
                datasets=tuple(
                    TransferReviewDataset(
                        dataset_id=str(item["dataset_id"]),
                        dataset_name=str(item["dataset_name"]),
                        model=str(item["model"]),
                        model_label=str(item["model_label"]),
                        key_field=str(item["key_field"]),
                        key_field_label=str(item["key_field_label"]),
                        source_row_count=int(item["source_row_count"]),
                        destination_existing_record_count=int(
                            item["destination_existing_record_count"]
                        ),
                        destination_create_record_count=int(
                            item["destination_create_record_count"]
                        ),
                        wave=int(item["wave"]),
                        scalar_write_fields=tuple(item["scalar_write_fields"]),
                        relationship_write_fields=tuple(
                            item["relationship_write_fields"]
                        ),
                    )
                    for item in payload["datasets"]
                ),
                relationships=tuple(
                    TransferReviewRelationship(
                        owner_dataset_id=str(item["owner_dataset_id"]),
                        related_dataset_id=str(item["related_dataset_id"]),
                        owner_model=str(item["owner_model"]),
                        owner_model_label=str(item["owner_model_label"]),
                        related_model=str(item["related_model"]),
                        related_model_label=str(item["related_model_label"]),
                        field_name=str(item["field_name"]),
                        field_label=str(item["field_label"]),
                        related_key_field=str(item["related_key_field"]),
                        kind=str(item["kind"]),
                        operation=str(item["operation"]),
                        inverse_field=(
                            str(item["inverse_field"])
                            if item.get("inverse_field") is not None
                            else None
                        ),
                        required=bool(item["required"]),
                        source_link_count=int(item["source_link_count"]),
                        destination_reused_link_count=int(
                            item["destination_reused_link_count"]
                        ),
                        incoming_link_count=int(item["incoming_link_count"]),
                        phase=str(item["phase"]),
                    )
                    for item in payload["relationships"]
                ),
                totals=TransferReviewTotals(
                    **{
                        name: int(number)
                        for name, number in dict(payload["totals"]).items()
                    }
                ),
                built_by=_actor_from_dict(payload["built_by"]),
                matched_record_policy=str(payload["matched_record_policy"]),
                missing_record_policy=str(payload["missing_record_policy"]),
                unmatched_destination_policy=str(
                    payload["unmatched_destination_policy"]
                ),
                contract_version=int(payload["contract_version"]),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise ValueError("Stored transfer-review package is invalid") from error
        if payload.get("content_hash") != result.content_hash:
            raise ValueError("Stored transfer-review package hash is invalid")
        return result


@dataclass(frozen=True, slots=True)
class TransferReviewApproval:
    """Approval of one exact review package through export-plan governance."""

    workspace_id: str
    review_package_hash: str
    export_approval: ExportPlanApproval
    contract_version: int = TRANSFER_REVIEW_APPROVAL_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if self.contract_version != TRANSFER_REVIEW_APPROVAL_CONTRACT_VERSION:
            raise ValueError("Transfer-review approval version is unsupported")
        if not self.workspace_id.strip() or _HASH.fullmatch(
            self.review_package_hash
        ) is None:
            raise ValueError("Transfer-review approval binding is invalid")
        if self.export_approval.policy_version != TRANSFER_REVIEW_POLICY_VERSION:
            raise ValueError("Transfer-review approval policy is invalid")

    @classmethod
    def approve(
        cls,
        package: TransferReviewPackage,
        *,
        approval_id: str,
        actor: Actor,
        approved_at: datetime,
        reason: str = "",
    ) -> "TransferReviewApproval":
        return cls(
            workspace_id=package.workspace_id,
            review_package_hash=package.content_hash,
            export_approval=ExportPlanApproval.approve(
                package.export_plan,
                approval_id=approval_id,
                actor=actor,
                approved_at=approved_at,
                policy_version=TRANSFER_REVIEW_POLICY_VERSION,
                reason=reason,
            ),
        )

    def authorizes(
        self,
        package: TransferReviewPackage,
        *,
        at: datetime,
    ) -> bool:
        return bool(
            self.workspace_id == package.workspace_id
            and self.review_package_hash == package.content_hash
            and self.export_approval.authorizes(package.export_plan, at=at)
        )

    @property
    def approved_by(self) -> ActorIdentity:
        return self.export_approval.evidence.approved_by

    @property
    def approved_at(self) -> datetime:
        return self.export_approval.evidence.approved_at

    @property
    def reason(self) -> str:
        return self.export_approval.evidence.reason

    @property
    def content_hash(self) -> str:
        return content_hash(self.to_dict(include_hash=False))

    def to_dict(self, *, include_hash: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "contract_version": self.contract_version,
            "workspace_id": self.workspace_id,
            "review_package_hash": self.review_package_hash,
            "export_approval": {
                "approval_id": self.export_approval.approval_id,
                "plan_hash": self.export_approval.plan_hash,
                "evidence": self.export_approval.evidence.to_portable_dict(),
                "policy_version": self.export_approval.policy_version,
                "expires_at": (
                    self.export_approval.expires_at.isoformat()
                    if self.export_approval.expires_at is not None
                    else None
                ),
            },
        }
        if include_hash:
            payload["content_hash"] = self.content_hash
        return payload

    def to_json(self) -> str:
        return canonical_json(self.to_dict())

    @classmethod
    def from_json(cls, value: str) -> "TransferReviewApproval":
        try:
            payload = json.loads(value)
            approval_payload = dict(payload["export_approval"])
            expires_at = approval_payload.get("expires_at")
            result = cls(
                workspace_id=str(payload["workspace_id"]),
                review_package_hash=str(payload["review_package_hash"]),
                export_approval=ExportPlanApproval(
                    approval_id=str(approval_payload["approval_id"]),
                    plan_hash=str(approval_payload["plan_hash"]),
                    evidence=ApprovalEvidence.from_dict(
                        dict(approval_payload["evidence"])
                    ),
                    policy_version=str(approval_payload["policy_version"]),
                    expires_at=(
                        datetime.fromisoformat(str(expires_at))
                        if expires_at is not None
                        else None
                    ),
                ),
                contract_version=int(payload["contract_version"]),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise ValueError("Stored transfer-review approval is invalid") from error
        if payload.get("content_hash") != result.content_hash:
            raise ValueError("Stored transfer-review approval hash is invalid")
        return result


def transfer_review_totals(
    datasets: tuple[TransferReviewDataset, ...],
    relationships: tuple[TransferReviewRelationship, ...],
) -> TransferReviewTotals:
    """Build public reconciled totals for a proposed package."""

    return _totals(datasets, relationships)


def transfer_review_actions_hash(
    datasets: tuple[TransferReviewDataset, ...],
    relationships: tuple[TransferReviewRelationship, ...],
    totals: TransferReviewTotals,
) -> str:
    """Bind every execution-relevant action and fixed transfer policy."""

    return content_hash(
        {
            "contract_version": TRANSFER_REVIEW_CONTRACT_VERSION,
            "datasets": [asdict(item) for item in datasets],
            "relationships": [asdict(item) for item in relationships],
            "totals": asdict(totals),
            "matched_record_policy": "update_selected_fields",
            "missing_record_policy": "create",
            "unmatched_destination_policy": "leave_unchanged",
        }
    )


def _totals(
    datasets: tuple[TransferReviewDataset, ...],
    relationships: tuple[TransferReviewRelationship, ...],
) -> TransferReviewTotals:
    return TransferReviewTotals(
        dataset_count=len(datasets),
        wave_count=max((item.wave for item in datasets), default=0),
        source_record_count=sum(item.source_row_count for item in datasets),
        destination_existing_record_count=sum(
            item.destination_existing_record_count for item in datasets
        ),
        destination_create_record_count=sum(
            item.destination_create_record_count for item in datasets
        ),
        scalar_write_field_count=sum(
            len(item.scalar_write_fields) for item in datasets
        ),
        relationship_write_field_count=len(relationships),
        source_relationship_link_count=sum(
            item.source_link_count for item in relationships
        ),
        destination_reused_link_count=sum(
            item.destination_reused_link_count for item in relationships
        ),
        incoming_link_count=sum(item.incoming_link_count for item in relationships),
        post_create_link_count=sum(
            item.source_link_count
            for item in relationships
            if item.phase == "post_create"
        ),
    )


def _export_plan_dict(plan: FrozenExportPlan) -> dict[str, Any]:
    return {
        "plan_id": plan.plan_id,
        "workspace_id": plan.workspace_id,
        "run_id": plan.run_id,
        "source_hashes": dict(plan.source_hashes),
        "mapping_hash": plan.mapping_hash,
        "ruleset_hash": plan.ruleset_hash,
        "canonical_dataset_hash": plan.canonical_dataset_hash,
        "target_snapshot_hash": plan.target_snapshot_hash,
        "actions_hash": plan.actions_hash,
        "frozen_at": plan.frozen_at.isoformat(),
        "contract_version": plan.contract_version,
        "semantic_hash": plan.semantic_hash,
    }


def _export_plan_from_dict(value: object) -> FrozenExportPlan:
    payload = dict(value)
    result = FrozenExportPlan(
        plan_id=str(payload["plan_id"]),
        workspace_id=str(payload["workspace_id"]),
        run_id=str(payload["run_id"]),
        source_hashes={
            str(name): str(digest)
            for name, digest in dict(payload["source_hashes"]).items()
        },
        mapping_hash=str(payload["mapping_hash"]),
        ruleset_hash=str(payload["ruleset_hash"]),
        canonical_dataset_hash=str(payload["canonical_dataset_hash"]),
        target_snapshot_hash=str(payload["target_snapshot_hash"]),
        actions_hash=str(payload["actions_hash"]),
        frozen_at=datetime.fromisoformat(str(payload["frozen_at"])),
        contract_version=int(payload["contract_version"]),
    )
    if payload.get("semantic_hash") != result.semantic_hash:
        raise ValueError("Stored frozen export-plan hash is invalid")
    return result


def _actor_dict(actor: ActorIdentity) -> dict[str, str]:
    return {
        "issuer": actor.issuer,
        "subject_id": actor.subject_id,
        "display_name": actor.display_name,
    }


def _actor_from_dict(value: object) -> ActorIdentity:
    payload = dict(value)
    return ActorIdentity(
        issuer=str(payload["issuer"]),
        subject_id=str(payload["subject_id"]),
        display_name=str(payload["display_name"]),
    )
