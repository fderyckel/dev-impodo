"""Bind a confirmed scalar correction to one lean execution snapshot.

The protected correction plan remains the authority for exact Odoo IDs and
values.  This snapshot groups that plan into update records while hashing only
its immutable evidence references; it creates no per-record or per-field hash
tree and performs no business-key resolution.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid5

from impodo.domain.correction import (
    CorrectionConfirmation,
    CorrectionPlan,
    CorrectionPlanError,
    CorrectionValueKind,
)
from impodo.domain.project.foundation import require_hash, require_uuid, required_text
from impodo.domain.serialization import canonical_json, content_hash
from impodo.domain.shared.models import portable_value, target_record_binding_hash

CORRECTION_EXECUTION_SNAPSHOT_CONTRACT = "correction-execution-snapshot-v1"


@dataclass(frozen=True, slots=True)
class CorrectionExecutionField:
    """One confirmed field and its exact intended final value."""

    target_field: str
    value_kind: CorrectionValueKind
    confirmed_current: Any
    corrected: Any

    def __post_init__(self) -> None:
        required_text(self.target_field, "target_field", maximum=200)
        object.__setattr__(self, "value_kind", CorrectionValueKind(self.value_kind))
        if self.value_kind is CorrectionValueKind.MANY2ONE:
            for value in (self.confirmed_current, self.corrected):
                if value is not None and (type(value) is not int or value <= 0):
                    raise CorrectionPlanError(
                        "Correction execution relationship identity is invalid"
                    )
        try:
            canonical_json(
                portable_value((self.confirmed_current, self.corrected))
            )
        except (TypeError, ValueError) as error:
            raise CorrectionPlanError(
                "Correction execution value is not canonically serializable"
            ) from error


@dataclass(frozen=True, slots=True)
class CorrectionExecutionRecord:
    """One exact prior Odoo record with its sparse scalar update payload."""

    row_id: str
    dataset: str
    source_row: int
    target_model: str
    odoo_id: int
    target_binding_hash: str
    fields: tuple[CorrectionExecutionField, ...]

    def __post_init__(self) -> None:
        for value, name in (
            (self.row_id, "row_id"),
            (self.dataset, "dataset"),
            (self.target_model, "target_model"),
        ):
            required_text(value, name, maximum=300)
        if self.source_row < 1 or type(self.odoo_id) is not int or self.odoo_id <= 0:
            raise CorrectionPlanError("Correction execution target is invalid")
        if self.target_binding_hash:
            require_hash(self.target_binding_hash, "target_binding_hash")
            if self.target_binding_hash != target_record_binding_hash(
                self.target_model,
                self.odoo_id,
            ):
                raise CorrectionPlanError(
                    "Correction execution target binding changed"
                )
        ordered = tuple(sorted(self.fields, key=lambda item: item.target_field))
        if (
            not ordered
            or ordered != self.fields
            or len({item.target_field for item in ordered}) != len(ordered)
        ):
            raise CorrectionPlanError(
                "Correction execution fields are not deterministic"
            )

    @property
    def key(self) -> tuple[str, int, str, int]:
        return (self.dataset, self.source_row, self.target_model, self.odoo_id)


@dataclass(frozen=True, slots=True)
class CorrectionExecutionSnapshot:
    """Lean execution boundary rebuilt from a protected plan and confirmation."""

    snapshot_id: str
    project_id: str
    completed_migration_run_id: str
    successor_migration_run_id: str
    workspace_id: str
    plan_id: str
    plan_hash: str
    confirmation_id: str
    confirmation_hash: str
    origin_evidence_hash: str
    corrected_prepared_hash: str
    target_hash: str
    target_database: str
    write_credential_binding_hash: str
    write_principal_hash: str
    write_permission_hash: str
    write_context_hash: str
    records: tuple[CorrectionExecutionRecord, ...]

    def __post_init__(self) -> None:
        for value, name in (
            (self.snapshot_id, "snapshot_id"),
            (self.project_id, "project_id"),
            (self.completed_migration_run_id, "completed_migration_run_id"),
            (self.successor_migration_run_id, "successor_migration_run_id"),
            (self.workspace_id, "workspace_id"),
            (self.plan_id, "plan_id"),
            (self.confirmation_id, "confirmation_id"),
        ):
            require_uuid(value, name)
        for value, name in (
            (self.plan_hash, "plan_hash"),
            (self.confirmation_hash, "confirmation_hash"),
            (self.origin_evidence_hash, "origin_evidence_hash"),
            (self.corrected_prepared_hash, "corrected_prepared_hash"),
            (self.target_hash, "target_hash"),
            (
                self.write_credential_binding_hash,
                "write_credential_binding_hash",
            ),
            (self.write_principal_hash, "write_principal_hash"),
            (self.write_permission_hash, "write_permission_hash"),
            (self.write_context_hash, "write_context_hash"),
        ):
            require_hash(value, name)
        required_text(self.target_database, "target_database", maximum=200)
        ordered = tuple(sorted(self.records, key=lambda item: item.key))
        if (
            not ordered
            or ordered != self.records
            or len({(item.target_model, item.odoo_id) for item in ordered})
            != len(ordered)
            or len({item.row_id for item in ordered}) != len(ordered)
        ):
            raise CorrectionPlanError(
                "Correction execution records are ambiguous"
            )

    @classmethod
    def create(
        cls,
        plan: CorrectionPlan,
        confirmation: CorrectionConfirmation,
        *,
        target_database: str,
    ) -> CorrectionExecutionSnapshot:
        """Group the protected plan without copying it into another artifact."""

        if (
            confirmation.plan_id != plan.plan_id
            or confirmation.plan_hash != plan.plan_hash
            or confirmation.target_hash != plan.target_hash
        ):
            raise CorrectionPlanError(
                "Correction execution confirmation does not match its plan"
            )
        grouped: dict[
            tuple[str, int, str, int],
            dict[str, object],
        ] = {}
        for item in plan.fields:
            key = (
                item.dataset,
                item.source_row,
                item.target_model,
                item.odoo_id,
            )
            group = grouped.setdefault(
                key,
                {
                    "row_id": item.row_id,
                    "target_binding_hash": item.target_binding_hash,
                    "fields": [],
                },
            )
            if (
                group["row_id"] != item.row_id
                or group["target_binding_hash"] != item.target_binding_hash
            ):
                raise CorrectionPlanError(
                    "Correction execution target lineage is ambiguous"
                )
            fields = group["fields"]
            assert isinstance(fields, list)
            fields.append(
                CorrectionExecutionField(
                    target_field=item.target_field,
                    value_kind=item.value_kind,
                    confirmed_current=item.current,
                    corrected=item.corrected,
                )
            )
        records = tuple(
            CorrectionExecutionRecord(
                row_id=str(grouped[key]["row_id"]),
                dataset=key[0],
                source_row=key[1],
                target_model=key[2],
                odoo_id=key[3],
                target_binding_hash=str(grouped[key]["target_binding_hash"]),
                fields=tuple(
                    sorted(
                        grouped[key]["fields"],  # type: ignore[arg-type]
                        key=lambda item: item.target_field,
                    )
                ),
            )
            for key in sorted(grouped)
        )
        return cls(
            snapshot_id=str(
                uuid5(UUID(confirmation.confirmation_id), "correction-execution")
            ),
            project_id=plan.project_id,
            completed_migration_run_id=plan.completed_migration_run_id,
            successor_migration_run_id=plan.successor_migration_run_id,
            workspace_id=plan.workspace_id,
            plan_id=plan.plan_id,
            plan_hash=plan.plan_hash,
            confirmation_id=confirmation.confirmation_id,
            confirmation_hash=confirmation.confirmation_hash,
            origin_evidence_hash=plan.origin_evidence_hash,
            corrected_prepared_hash=plan.corrected_prepared_hash,
            target_hash=plan.target_hash,
            target_database=target_database,
            write_credential_binding_hash=(
                confirmation.write_credential_binding_hash
            ),
            write_principal_hash=confirmation.write_principal_hash,
            write_permission_hash=confirmation.write_permission_hash,
            write_context_hash=confirmation.write_context_hash,
            records=records,
        )

    @property
    def field_count(self) -> int:
        return sum(len(item.fields) for item in self.records)

    @property
    def semantic_hash(self) -> str:
        """Hash only immutable references; the protected plan binds row values."""

        return content_hash(self._reference_dict())

    @property
    def root_hash(self) -> str:
        return content_hash(
            {
                "contract": CORRECTION_EXECUTION_SNAPSHOT_CONTRACT,
                "snapshot_hash": self.semantic_hash,
                "origin_evidence_hash": self.origin_evidence_hash,
                "corrected_prepared_hash": self.corrected_prepared_hash,
            }
        )

    def assert_matches(
        self,
        plan: CorrectionPlan,
        confirmation: CorrectionConfirmation,
    ) -> None:
        expected = type(self).create(
            plan,
            confirmation,
            target_database=self.target_database,
        )
        if expected != self:
            raise CorrectionPlanError(
                "Correction execution snapshot changed after confirmation"
            )

    def _reference_dict(self) -> Mapping[str, object]:
        return {
            "confirmation_hash": self.confirmation_hash,
            "confirmation_id": self.confirmation_id,
            "contract": CORRECTION_EXECUTION_SNAPSHOT_CONTRACT,
            "field_count": self.field_count,
            "plan_hash": self.plan_hash,
            "plan_id": self.plan_id,
            "record_count": len(self.records),
            "snapshot_id": self.snapshot_id,
            "target_database": self.target_database,
            "target_hash": self.target_hash,
            "workspace_id": self.workspace_id,
            "write_context_hash": self.write_context_hash,
            "write_credential_binding_hash": (
                self.write_credential_binding_hash
            ),
            "write_permission_hash": self.write_permission_hash,
            "write_principal_hash": self.write_principal_hash,
        }


__all__ = [
    "CorrectionExecutionField",
    "CorrectionExecutionRecord",
    "CorrectionExecutionSnapshot",
]
