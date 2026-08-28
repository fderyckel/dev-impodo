"""Define output-based correction meaning independently of mapping controls.

The correction workflow compares canonical target-field intent.  It does not
care whether that intent came from a source field, Selection rule, constant,
fallback, casing transformation, formula, or resolved relationship.  Target
I/O, persistence, encryption, and Polars execution remain outside this module.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import StrEnum
import json
from typing import Any, Callable

from impodo.domain.project.foundation import (
    require_aware,
    require_hash,
    require_uuid,
    required_text,
)
from impodo.domain.serialization import canonical_json, content_hash
from impodo.domain.shared.access import ActorIdentity
from impodo.domain.shared.models import (
    OdooReadIdentity,
    OdooWriteIdentity,
    portable_value,
    restore_portable_value,
)


CORRECTION_PLAN_CONTRACT = "correction-plan-v1"
CORRECTION_CONFIRMATION_CONTRACT = "correction-confirmation-v1"


class CorrectionValueKind(StrEnum):
    """Name the canonical comparison family for one target-field value."""

    SCALAR = "SCALAR"
    MANY2ONE = "MANY2ONE"


class CorrectionFieldOutcome(StrEnum):
    """Classify one previous/current/corrected target-field comparison."""

    UNCHANGED_INTENT = "UNCHANGED_INTENT"
    READY = "READY"
    ALREADY_CORRECTED = "ALREADY_CORRECTED"
    CONFLICT = "CONFLICT"


class CorrectionPlanError(ValueError):
    """Reject an unsafe, stale, or malformed protected correction plan."""


CanonicalEquality = Callable[[Any, Any], bool]


@dataclass(frozen=True, slots=True)
class CorrectionCandidate:
    """One field whose corrected canonical intent differs from prior intent."""

    dataset: str
    source_row: int
    target_model: str
    target_field: str
    value_kind: CorrectionValueKind
    previous: Any
    corrected: Any

    def __post_init__(self) -> None:
        for value, label in (
            (self.dataset, "dataset"),
            (self.target_model, "target model"),
            (self.target_field, "target field"),
        ):
            if not value or len(value) > 200:
                raise ValueError(f"Correction {label} is invalid")
        if self.source_row < 1:
            raise ValueError("Correction source row is invalid")
        object.__setattr__(self, "value_kind", CorrectionValueKind(self.value_kind))


@dataclass(frozen=True, slots=True)
class CorrectionFieldDecision:
    """Bind a changed-intent candidate to one freshly read current value."""

    candidate: CorrectionCandidate
    current: Any
    outcome: CorrectionFieldOutcome

    @property
    def writable(self) -> bool:
        """Return whether the first correction delivery may write this field."""

        return self.outcome is CorrectionFieldOutcome.READY


def classify_correction_field(
    candidate: CorrectionCandidate,
    current: Any,
    *,
    equal: CanonicalEquality | None = None,
) -> CorrectionFieldDecision:
    """Apply the fail-closed three-way rule to canonical field values.

    Candidate generation normally removes unchanged intent with native Polars
    expressions.  The first branch remains part of the domain truth table so a
    caller cannot turn an accidentally supplied unchanged candidate into a
    write.
    """

    same = equal or _equal
    previous = candidate.previous
    corrected = candidate.corrected
    if same(previous, corrected):
        outcome = CorrectionFieldOutcome.UNCHANGED_INTENT
    elif same(current, corrected):
        outcome = CorrectionFieldOutcome.ALREADY_CORRECTED
    elif same(current, previous):
        outcome = CorrectionFieldOutcome.READY
    else:
        outcome = CorrectionFieldOutcome.CONFLICT
    return CorrectionFieldDecision(
        candidate=candidate,
        current=current,
        outcome=outcome,
    )


def _equal(left: Any, right: Any) -> bool:
    """Compare already canonical values without lossy string coercion."""

    return left == right


@dataclass(frozen=True, slots=True)
class CorrectionPlanField:
    """One reviewed scalar update bound to an exact protected Odoo record."""

    dataset: str
    source_row: int
    row_id: str
    target_model: str
    odoo_id: int
    completed_disposition: str
    target_binding_hash: str
    target_field: str
    value_kind: CorrectionValueKind
    previous: Any
    current: Any
    corrected: Any

    def __post_init__(self) -> None:
        for value, name in (
            (self.dataset, "dataset"),
            (self.row_id, "row_id"),
            (self.target_model, "target_model"),
            (self.target_field, "target_field"),
        ):
            _plan_text(value, name)
        if self.source_row < 1:
            raise CorrectionPlanError("Correction plan source_row is invalid")
        if type(self.odoo_id) is not int or self.odoo_id <= 0:
            raise CorrectionPlanError("Correction plan Odoo identifier is invalid")
        if self.completed_disposition not in {"CREATE", "UPDATE", "UNCHANGED"}:
            raise CorrectionPlanError(
                "Correction plan completed disposition is invalid"
            )
        if self.target_binding_hash:
            _plan_hash(self.target_binding_hash, "target_binding_hash")
        object.__setattr__(self, "value_kind", CorrectionValueKind(self.value_kind))
        if self.value_kind is not CorrectionValueKind.SCALAR:
            raise CorrectionPlanError(
                "Relationship corrections require separate qualification"
            )
        try:
            canonical_json(
                portable_value((self.previous, self.current, self.corrected))
            )
        except (TypeError, ValueError) as error:
            raise CorrectionPlanError(
                "Correction plan values are not canonically serializable"
            ) from error

    @property
    def key(self) -> tuple[str, int, str, str]:
        """Return stable source lineage plus affected target field."""

        return (
            self.dataset,
            self.source_row,
            self.target_model,
            self.target_field,
        )

    def protected_dict(self) -> dict[str, object]:
        """Serialize the field for encrypted Project evidence only."""

        return {
            "completed_disposition": self.completed_disposition,
            "corrected": portable_value(self.corrected),
            "current": portable_value(self.current),
            "dataset": self.dataset,
            "odoo_id": self.odoo_id,
            "previous": portable_value(self.previous),
            "row_id": self.row_id,
            "source_row": self.source_row,
            "target_binding_hash": self.target_binding_hash,
            "target_field": self.target_field,
            "target_model": self.target_model,
            "value_kind": self.value_kind.value,
        }

    @classmethod
    def from_protected_dict(cls, payload: Mapping[str, object]) -> "CorrectionPlanField":
        return cls(
            dataset=str(payload["dataset"]),
            source_row=int(payload["source_row"]),
            row_id=str(payload["row_id"]),
            target_model=str(payload["target_model"]),
            odoo_id=int(payload["odoo_id"]),
            completed_disposition=str(payload["completed_disposition"]),
            target_binding_hash=str(payload["target_binding_hash"]),
            target_field=str(payload["target_field"]),
            value_kind=CorrectionValueKind(str(payload["value_kind"])),
            previous=restore_portable_value(payload.get("previous")),
            current=restore_portable_value(payload.get("current")),
            corrected=restore_portable_value(payload.get("corrected")),
        )


@dataclass(frozen=True, slots=True)
class CorrectionPlanSummaryGroup:
    """Target-independent count safe for an ordinary review projection."""

    dataset: str
    target_model: str
    target_field: str
    changed_field_count: int


@dataclass(frozen=True, slots=True)
class CorrectionPlanSummary:
    """Public correction counts without identifiers, values, or evidence hashes."""

    field_count: int
    record_count: int
    groups: tuple[CorrectionPlanSummaryGroup, ...]


@dataclass(frozen=True, slots=True)
class CorrectionPlan:
    """Immutable whole-plan evidence published once after safe A/B/C review.

    Numeric target identifiers and raw field values deliberately appear only in
    ``protected_dict``.  Ordinary projections must use ``public_summary``.
    """

    plan_id: str
    project_id: str
    completed_migration_run_id: str
    successor_migration_run_id: str
    workspace_id: str
    origin_evidence_hash: str
    previous_prepared_hash: str
    corrected_prepared_hash: str
    target_hash: str
    read_credential_binding_hash: str
    read_principal_hash: str
    read_permission_hash: str
    read_context_hash: str
    read_observed_at: str
    readable_models: tuple[str, ...]
    fields: tuple[CorrectionPlanField, ...]
    created_by: ActorIdentity
    created_at: datetime
    plan_hash: str

    def __post_init__(self) -> None:
        for value, name in (
            (self.plan_id, "plan_id"),
            (self.project_id, "project_id"),
            (self.completed_migration_run_id, "completed_migration_run_id"),
            (self.successor_migration_run_id, "successor_migration_run_id"),
            (self.workspace_id, "workspace_id"),
        ):
            _plan_uuid(value, name)
        for value, name in (
            (self.origin_evidence_hash, "origin_evidence_hash"),
            (self.previous_prepared_hash, "previous_prepared_hash"),
            (self.corrected_prepared_hash, "corrected_prepared_hash"),
            (self.target_hash, "target_hash"),
            (self.read_credential_binding_hash, "read_credential_binding_hash"),
            (self.read_principal_hash, "read_principal_hash"),
            (self.read_permission_hash, "read_permission_hash"),
            (self.read_context_hash, "read_context_hash"),
            (self.plan_hash, "plan_hash"),
        ):
            _plan_hash(value, name)
        _plan_text(self.read_observed_at, "read_observed_at")
        require_aware(self.created_at, "created_at")
        object.__setattr__(
            self,
            "created_at",
            self.created_at.astimezone(timezone.utc),
        )
        fields = tuple(self.fields)
        if not fields:
            raise CorrectionPlanError("Correction plan has no writable fields")
        if fields != tuple(sorted(fields, key=lambda item: item.key)):
            raise CorrectionPlanError("Correction plan fields are not deterministic")
        if len({item.key for item in fields}) != len(fields):
            raise CorrectionPlanError("Correction plan contains a duplicate field")
        exact_fields = {
            (item.target_model, item.odoo_id, item.target_field)
            for item in fields
        }
        if len(exact_fields) != len(fields):
            raise CorrectionPlanError(
                "Correction plan contains an ambiguous exact target field"
            )
        readable_models = tuple(sorted(set(self.readable_models)))
        if not readable_models or readable_models != self.readable_models:
            raise CorrectionPlanError("Correction plan read scope is invalid")
        if not {item.target_model for item in fields}.issubset(readable_models):
            raise CorrectionPlanError("Correction plan read scope is incomplete")

    @classmethod
    def create(
        cls,
        *,
        plan_id: str,
        project_id: str,
        completed_migration_run_id: str,
        successor_migration_run_id: str,
        workspace_id: str,
        origin_evidence_hash: str,
        previous_prepared_hash: str,
        corrected_prepared_hash: str,
        read_credential_binding_hash: str,
        read_identity: OdooReadIdentity,
        fields: Iterable[CorrectionPlanField],
        created_by: ActorIdentity,
        created_at: datetime,
    ) -> "CorrectionPlan":
        ordered = tuple(sorted(fields, key=lambda item: item.key))
        unhashed = cls(
            plan_id=plan_id,
            project_id=project_id,
            completed_migration_run_id=completed_migration_run_id,
            successor_migration_run_id=successor_migration_run_id,
            workspace_id=workspace_id,
            origin_evidence_hash=origin_evidence_hash,
            previous_prepared_hash=previous_prepared_hash,
            corrected_prepared_hash=corrected_prepared_hash,
            target_hash=read_identity.target_hash,
            read_credential_binding_hash=read_credential_binding_hash,
            read_principal_hash=read_identity.principal_hash,
            read_permission_hash=read_identity.permission_hash,
            read_context_hash=read_identity.context_hash,
            read_observed_at=read_identity.observed_at,
            readable_models=tuple(sorted(set(read_identity.readable_models))),
            fields=ordered,
            created_by=created_by,
            created_at=created_at,
            plan_hash="sha256:" + "0" * 64,
        )
        return replace(
            unhashed,
            plan_hash=content_hash(unhashed._meaning_dict(portable=True)),
        )

    def public_summary(self) -> CorrectionPlanSummary:
        """Return counts without leaking protected record evidence."""

        counts: dict[tuple[str, str, str], int] = {}
        records: set[tuple[str, int]] = set()
        for field in self.fields:
            key = (field.dataset, field.target_model, field.target_field)
            counts[key] = counts.get(key, 0) + 1
            records.add((field.target_model, field.odoo_id))
        return CorrectionPlanSummary(
            field_count=len(self.fields),
            record_count=len(records),
            groups=tuple(
                CorrectionPlanSummaryGroup(*key, changed_field_count=count)
                for key, count in sorted(counts.items())
            ),
        )

    def protected_dict(self) -> dict[str, object]:
        """Return canonical encrypted-store payload including its one plan hash."""

        return {
            "contract": CORRECTION_PLAN_CONTRACT,
            **self._meaning_dict(portable=True),
            "plan_hash": self.plan_hash,
        }

    def protected_json(self) -> bytes:
        return canonical_json(self.protected_dict()).encode("utf-8")

    @classmethod
    def from_protected_json(cls, payload: bytes) -> "CorrectionPlan":
        try:
            raw = json.loads(payload)
            if not isinstance(raw, dict) or raw.get("contract") != CORRECTION_PLAN_CONTRACT:
                raise CorrectionPlanError("Correction plan contract is unsupported")
            fields = tuple(
                CorrectionPlanField.from_protected_dict(item)
                for item in raw["fields"]
            )
            plan = cls(
                plan_id=str(raw["plan_id"]),
                project_id=str(raw["project_id"]),
                completed_migration_run_id=str(raw["completed_migration_run_id"]),
                successor_migration_run_id=str(raw["successor_migration_run_id"]),
                workspace_id=str(raw["workspace_id"]),
                origin_evidence_hash=str(raw["origin_evidence_hash"]),
                previous_prepared_hash=str(raw["previous_prepared_hash"]),
                corrected_prepared_hash=str(raw["corrected_prepared_hash"]),
                target_hash=str(raw["target_hash"]),
                read_credential_binding_hash=str(
                    raw["read_credential_binding_hash"]
                ),
                read_principal_hash=str(raw["read_principal_hash"]),
                read_permission_hash=str(raw["read_permission_hash"]),
                read_context_hash=str(raw["read_context_hash"]),
                read_observed_at=str(raw["read_observed_at"]),
                readable_models=tuple(str(item) for item in raw["readable_models"]),
                fields=fields,
                created_by=_actor_from_dict(raw["created_by"]),
                created_at=_timestamp_from_text(str(raw["created_at"])),
                plan_hash=str(raw["plan_hash"]),
            )
        except CorrectionPlanError:
            raise
        except (KeyError, TypeError, ValueError) as error:
            raise CorrectionPlanError("Correction plan payload is invalid") from error
        if content_hash(plan._meaning_dict(portable=True)) != plan.plan_hash:
            raise CorrectionPlanError("Correction plan hash changed")
        return plan

    def _meaning_dict(self, *, portable: bool = False) -> dict[str, object]:
        fields: object = self.fields
        created_by: object = self.created_by
        created_at: object = self.created_at
        if portable:
            fields = [item.protected_dict() for item in self.fields]
            created_by = _actor_dict(self.created_by)
            created_at = _timestamp_text(self.created_at)
        return {
            "completed_migration_run_id": self.completed_migration_run_id,
            "corrected_prepared_hash": self.corrected_prepared_hash,
            "created_at": created_at,
            "created_by": created_by,
            "fields": fields,
            "origin_evidence_hash": self.origin_evidence_hash,
            "plan_id": self.plan_id,
            "previous_prepared_hash": self.previous_prepared_hash,
            "project_id": self.project_id,
            "read_context_hash": self.read_context_hash,
            "read_credential_binding_hash": self.read_credential_binding_hash,
            "read_observed_at": self.read_observed_at,
            "read_permission_hash": self.read_permission_hash,
            "read_principal_hash": self.read_principal_hash,
            "readable_models": self.readable_models,
            "successor_migration_run_id": self.successor_migration_run_id,
            "target_hash": self.target_hash,
            "workspace_id": self.workspace_id,
        }


@dataclass(frozen=True, slots=True)
class CorrectionConfirmation:
    """Explicit actor confirmation bound to a fresh, separate write identity."""

    confirmation_id: str
    plan_id: str
    plan_hash: str
    target_hash: str
    field_count: int
    record_count: int
    write_credential_binding_hash: str
    write_principal_hash: str
    write_permission_hash: str
    write_context_hash: str
    write_observed_at: str
    writable_models: tuple[str, ...]
    confirmed_by: ActorIdentity
    confirmed_at: datetime
    confirmation_hash: str

    def __post_init__(self) -> None:
        _plan_uuid(self.confirmation_id, "confirmation_id")
        _plan_uuid(self.plan_id, "plan_id")
        for value, name in (
            (self.plan_hash, "plan_hash"),
            (self.target_hash, "target_hash"),
            (self.write_credential_binding_hash, "write_credential_binding_hash"),
            (self.write_principal_hash, "write_principal_hash"),
            (self.write_permission_hash, "write_permission_hash"),
            (self.write_context_hash, "write_context_hash"),
            (self.confirmation_hash, "confirmation_hash"),
        ):
            _plan_hash(value, name)
        if self.field_count < 1 or self.record_count < 1:
            raise CorrectionPlanError("Correction confirmation counts are invalid")
        _plan_text(self.write_observed_at, "write_observed_at")
        if tuple(sorted(set(self.writable_models))) != self.writable_models:
            raise CorrectionPlanError("Correction write scope is invalid")
        require_aware(self.confirmed_at, "confirmed_at")
        object.__setattr__(
            self,
            "confirmed_at",
            self.confirmed_at.astimezone(timezone.utc),
        )

    @classmethod
    def create(
        cls,
        *,
        confirmation_id: str,
        plan: CorrectionPlan,
        write_credential_binding_hash: str,
        write_identity: OdooWriteIdentity,
        confirmed_by: ActorIdentity,
        confirmed_at: datetime,
    ) -> "CorrectionConfirmation":
        summary = plan.public_summary()
        affected_models = {item.target_model for item in plan.fields}
        writable_models = tuple(sorted(set(write_identity.writable_models)))
        if (
            write_identity.target_hash != plan.target_hash
            or not affected_models.issubset(writable_models)
            or not affected_models.issubset(set(write_identity.readable_models))
        ):
            raise CorrectionPlanError(
                "Correction write identity does not cover the confirmed plan"
            )
        unhashed = cls(
            confirmation_id=confirmation_id,
            plan_id=plan.plan_id,
            plan_hash=plan.plan_hash,
            target_hash=plan.target_hash,
            field_count=summary.field_count,
            record_count=summary.record_count,
            write_credential_binding_hash=write_credential_binding_hash,
            write_principal_hash=write_identity.principal_hash,
            write_permission_hash=write_identity.permission_hash,
            write_context_hash=write_identity.context_hash,
            write_observed_at=write_identity.observed_at,
            writable_models=writable_models,
            confirmed_by=confirmed_by,
            confirmed_at=confirmed_at,
            confirmation_hash="sha256:" + "0" * 64,
        )
        return replace(
            unhashed,
            confirmation_hash=content_hash(
                unhashed._meaning_dict(portable=True)
            ),
        )

    def assert_current(
        self,
        plan: CorrectionPlan,
        *,
        write_credential_binding_hash: str,
        write_identity: OdooWriteIdentity,
    ) -> None:
        """Reject stale plan, target, credential, principal, or count evidence."""

        summary = plan.public_summary()
        if (
            self.plan_id != plan.plan_id
            or self.plan_hash != plan.plan_hash
            or self.target_hash != plan.target_hash
            or self.field_count != summary.field_count
            or self.record_count != summary.record_count
            or self.write_credential_binding_hash != write_credential_binding_hash
            or self.write_principal_hash != write_identity.principal_hash
            or self.write_permission_hash != write_identity.permission_hash
            or self.write_context_hash != write_identity.context_hash
            or self.write_observed_at != write_identity.observed_at
            or self.writable_models
            != tuple(sorted(set(write_identity.writable_models)))
            or write_identity.target_hash != plan.target_hash
            or not {item.target_model for item in plan.fields}.issubset(
                set(write_identity.readable_models)
            )
        ):
            raise CorrectionPlanError("Correction confirmation is stale")

    def protected_dict(self) -> dict[str, object]:
        return {
            "contract": CORRECTION_CONFIRMATION_CONTRACT,
            **self._meaning_dict(portable=True),
            "confirmation_hash": self.confirmation_hash,
        }

    def protected_json(self) -> bytes:
        return canonical_json(self.protected_dict()).encode("utf-8")

    @classmethod
    def from_protected_json(cls, payload: bytes) -> "CorrectionConfirmation":
        try:
            raw = json.loads(payload)
            if (
                not isinstance(raw, dict)
                or raw.get("contract") != CORRECTION_CONFIRMATION_CONTRACT
            ):
                raise CorrectionPlanError(
                    "Correction confirmation contract is unsupported"
                )
            confirmation = cls(
                confirmation_id=str(raw["confirmation_id"]),
                plan_id=str(raw["plan_id"]),
                plan_hash=str(raw["plan_hash"]),
                target_hash=str(raw["target_hash"]),
                field_count=int(raw["field_count"]),
                record_count=int(raw["record_count"]),
                write_credential_binding_hash=str(
                    raw["write_credential_binding_hash"]
                ),
                write_principal_hash=str(raw["write_principal_hash"]),
                write_permission_hash=str(raw["write_permission_hash"]),
                write_context_hash=str(raw["write_context_hash"]),
                write_observed_at=str(raw["write_observed_at"]),
                writable_models=tuple(str(item) for item in raw["writable_models"]),
                confirmed_by=_actor_from_dict(raw["confirmed_by"]),
                confirmed_at=_timestamp_from_text(str(raw["confirmed_at"])),
                confirmation_hash=str(raw["confirmation_hash"]),
            )
        except CorrectionPlanError:
            raise
        except (KeyError, TypeError, ValueError) as error:
            raise CorrectionPlanError(
                "Correction confirmation payload is invalid"
            ) from error
        if (
            content_hash(confirmation._meaning_dict(portable=True))
            != confirmation.confirmation_hash
        ):
            raise CorrectionPlanError("Correction confirmation hash changed")
        return confirmation

    def _meaning_dict(self, *, portable: bool = False) -> dict[str, object]:
        confirmed_by: object = self.confirmed_by
        confirmed_at: object = self.confirmed_at
        if portable:
            confirmed_by = _actor_dict(self.confirmed_by)
            confirmed_at = _timestamp_text(self.confirmed_at)
        return {
            "confirmation_id": self.confirmation_id,
            "confirmed_at": confirmed_at,
            "confirmed_by": confirmed_by,
            "field_count": self.field_count,
            "plan_hash": self.plan_hash,
            "plan_id": self.plan_id,
            "record_count": self.record_count,
            "target_hash": self.target_hash,
            "writable_models": self.writable_models,
            "write_context_hash": self.write_context_hash,
            "write_credential_binding_hash": self.write_credential_binding_hash,
            "write_observed_at": self.write_observed_at,
            "write_permission_hash": self.write_permission_hash,
            "write_principal_hash": self.write_principal_hash,
        }


def _actor_dict(actor: ActorIdentity) -> dict[str, str]:
    return {
        "display_name": actor.display_name,
        "issuer": actor.issuer,
        "subject_id": actor.subject_id,
    }


def _actor_from_dict(value: object) -> ActorIdentity:
    if not isinstance(value, Mapping):
        raise CorrectionPlanError("Correction actor identity is invalid")
    return ActorIdentity(
        issuer=str(value["issuer"]),
        subject_id=str(value["subject_id"]),
        display_name=str(value["display_name"]),
    )


def _timestamp_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _timestamp_from_text(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    require_aware(parsed, "timestamp")
    return parsed.astimezone(timezone.utc)


def _plan_text(value: str, name: str) -> str:
    try:
        return required_text(value, name, maximum=500)
    except ValueError as error:
        raise CorrectionPlanError(f"Correction plan {name} is invalid") from error


def _plan_uuid(value: str, name: str) -> str:
    try:
        return require_uuid(value, name)
    except ValueError as error:
        raise CorrectionPlanError(f"Correction plan {name} is invalid") from error


def _plan_hash(value: str, name: str) -> str:
    try:
        return require_hash(value, name)
    except ValueError as error:
        raise CorrectionPlanError(f"Correction plan {name} is invalid") from error


__all__ = [
    "CanonicalEquality",
    "CorrectionCandidate",
    "CorrectionConfirmation",
    "CorrectionFieldDecision",
    "CorrectionFieldOutcome",
    "CorrectionPlan",
    "CorrectionPlanError",
    "CorrectionPlanField",
    "CorrectionPlanSummary",
    "CorrectionPlanSummaryGroup",
    "CorrectionValueKind",
    "classify_correction_field",
]
