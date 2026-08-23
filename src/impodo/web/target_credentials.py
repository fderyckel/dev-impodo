"""Keep Odoo read and write credentials role- and target-bound.

The vault stores one versioned envelope per exact project/target/role.  The
random binding ID lets durable read evidence identify a credential generation
without persisting a secret or secret-derived verifier.  It is deliberately a
credential binding, not a claim about the authenticated Odoo user; a principal
probe remains a separate contract.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
import hashlib
import json
from uuid import UUID, uuid4

from ..access import Actor
from ..domain.serialization import content_hash
from ..models import target_identity_hash
from ..workspace_state import WorkspaceState, WorkspaceStateService
from ..secrets import SecretStore, SecretStoreError


class TargetCredentialRole(StrEnum):
    """Separate Odoo capabilities that must never fall back to each other."""

    READ = "READ"
    WRITE = "WRITE"


class TargetCredentialRemovalReason(StrEnum):
    """Governed reasons for removing role-qualified target credentials."""

    TARGET_CHANGED = "TARGET_CHANGED"
    RECIPE_DELETED = "RECIPE_DELETED"
    USER_REQUESTED = "USER_REQUESTED"


class TargetCredentialAvailability(StrEnum):
    """Safe presentation state for one role-qualified vault entry."""

    MISSING = "MISSING"
    SESSION = "SESSION"
    PERSISTENT = "PERSISTENT"
    UNAVAILABLE = "UNAVAILABLE"


TARGET_CREDENTIAL_CONTRACT_VERSION = 2


@dataclass(frozen=True, slots=True)
class TargetCredential:
    """Return one vault secret with its safe evidence binding."""

    secret: str
    binding_hash: str
    replaced: bool = field(default=False, compare=False)
    persistent: bool = field(default=False, compare=False)


@dataclass(frozen=True, slots=True)
class TargetCredentialStatus:
    """Expose credential availability without exposing the stored secret."""

    availability: TargetCredentialAvailability
    binding_hash: str | None = field(default=None, repr=False)
    support_error: str | None = field(default=None, repr=False)

    @property
    def available(self) -> bool:
        """Whether Impodo can use the credential in this process."""

        return self.availability in {
            TargetCredentialAvailability.SESSION,
            TargetCredentialAvailability.PERSISTENT,
        }

    @property
    def label(self) -> str:
        """Return a short non-secret status label."""

        labels = {
            TargetCredentialAvailability.MISSING: "Not available",
            TargetCredentialAvailability.SESSION: "Available this session",
            TargetCredentialAvailability.PERSISTENT: "Saved on this computer",
            TargetCredentialAvailability.UNAVAILABLE: "Could not be accessed",
        }
        return labels[self.availability]

    @property
    def guidance(self) -> str:
        """Explain retention and the next action in business language."""

        guidance = {
            TargetCredentialAvailability.MISSING: (
                "Enter the read-only key before Impodo checks Odoo data."
            ),
            TargetCredentialAvailability.SESSION: (
                "Impodo will forget this key when Impodo closes."
            ),
            TargetCredentialAvailability.PERSISTENT: (
                "Windows Credential Manager will keep this key after Impodo closes."
            ),
            TargetCredentialAvailability.UNAVAILABLE: (
                "Enter the read-only key again or review Windows Credential Manager."
            ),
        }
        return guidance[self.availability]


@dataclass(frozen=True, slots=True)
class TargetCredentialRemovalReceipt:
    """Non-secret proof that one present vault generation was removed."""

    role: TargetCredentialRole
    reason: TargetCredentialRemovalReason
    connection_target_hash: str
    credential_binding_hash: str | None
    storage_class: str
    removed_at: datetime
    receipt_hash: str


def target_read_credential_id(project: WorkspaceState) -> str:
    """Return the opaque vault ID for read-only Odoo access."""

    return _target_credential_id(project, TargetCredentialRole.READ)


def target_write_credential_id(project: WorkspaceState) -> str:
    """Return the opaque vault ID for Odoo write and read-back access."""

    return _target_credential_id(project, TargetCredentialRole.WRITE)


def store_target_credential(
    store: SecretStore,
    project: WorkspaceState,
    role: TargetCredentialRole,
    secret: str,
    *,
    persistent: bool,
) -> TargetCredential:
    """Replace one exact role binding with a new random generation."""

    clean_secret = secret.strip()
    if not clean_secret:
        raise SecretStoreError("API key is empty")
    credential_id = _target_credential_id(project, role)
    replaced = store.get(credential_id) is not None
    binding_id = str(uuid4())
    target_hash = _target_hash(project)
    envelope = json.dumps(
        {
            "contract_version": TARGET_CREDENTIAL_CONTRACT_VERSION,
            "role": role.value,
            "target_hash": target_hash,
            "binding_id": binding_id,
            "storage_class": (
                "OPERATING_SYSTEM_VAULT" if persistent else "SESSION"
            ),
            "secret": clean_secret,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    store.set(
        credential_id,
        envelope,
        persistent=persistent,
    )
    return TargetCredential(
        secret=clean_secret,
        binding_hash=_binding_hash(role, target_hash, binding_id),
        replaced=replaced,
        persistent=persistent,
    )


def get_target_credential(
    store: SecretStore,
    project: WorkspaceState,
    role: TargetCredentialRole,
) -> TargetCredential | None:
    """Load and validate one exact role envelope without cross-role fallback."""

    encoded = store.get(_target_credential_id(project, role))
    if encoded is None:
        return None
    try:
        payload = json.loads(encoded)
        if set(payload) != {
            "binding_id",
            "contract_version",
            "role",
            "secret",
            "storage_class",
            "target_hash",
        }:
            raise ValueError("unexpected credential envelope fields")
        binding_id = str(UUID(str(payload["binding_id"])))
        secret = str(payload["secret"]).strip()
        storage_class = str(payload["storage_class"])
        target_hash = _target_hash(project)
        valid = (
            payload["contract_version"] == TARGET_CREDENTIAL_CONTRACT_VERSION
            and payload["role"] == role.value
            and payload["target_hash"] == target_hash
            and storage_class in {"SESSION", "OPERATING_SYSTEM_VAULT"}
            and bool(secret)
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise SecretStoreError(
            f"Stored Odoo {role.value.lower()} credential is invalid; enter it again"
        ) from error
    if not valid:
        raise SecretStoreError(
            f"Stored Odoo {role.value.lower()} credential does not match this target"
        )
    return TargetCredential(
        secret=secret,
        binding_hash=_binding_hash(role, target_hash, binding_id),
        persistent=storage_class == "OPERATING_SYSTEM_VAULT",
    )


def get_target_credential_status(
    store: SecretStore,
    project: WorkspaceState,
    role: TargetCredentialRole,
) -> TargetCredentialStatus:
    """Return a safe status even when the operating-system vault is unavailable."""

    try:
        credential = get_target_credential(store, project, role)
    except SecretStoreError as error:
        return TargetCredentialStatus(
            TargetCredentialAvailability.UNAVAILABLE,
            support_error=str(error),
        )
    if credential is None:
        return TargetCredentialStatus(TargetCredentialAvailability.MISSING)
    return TargetCredentialStatus(
        (
            TargetCredentialAvailability.PERSISTENT
            if credential.persistent
            else TargetCredentialAvailability.SESSION
        ),
        binding_hash=credential.binding_hash,
    )


def audit_stored_target_credential(
    projects: WorkspaceStateService,
    project: WorkspaceState,
    role: TargetCredentialRole,
    credential: TargetCredential,
    *,
    actor: Actor,
) -> None:
    """Record one successful secret-store mutation using safe evidence only."""

    projects.record_credential_event(
        project.project_id,
        actor=actor,
        role=role.value,
        action="REPLACED" if credential.replaced else "STORED",
        binding_hash=credential.binding_hash,
        persistent=credential.persistent,
    )


def delete_target_credentials(
    store: SecretStore,
    project: WorkspaceState,
    *,
    reason: TargetCredentialRemovalReason,
) -> tuple[TargetCredentialRemovalReceipt, ...]:
    """Delete present role entries and return secret-independent receipts."""

    removed_at = datetime.now(timezone.utc)
    receipts: list[TargetCredentialRemovalReceipt] = []
    for role in TargetCredentialRole:
        receipt = _delete_target_credential(
            store,
            project,
            role,
            reason=reason,
            removed_at=removed_at,
        )
        if receipt is not None:
            receipts.append(receipt)
    return tuple(receipts)


def delete_target_credential(
    store: SecretStore,
    project: WorkspaceState,
    role: TargetCredentialRole,
    *,
    reason: TargetCredentialRemovalReason,
) -> TargetCredentialRemovalReceipt | None:
    """Delete one role only and return non-secret removal evidence."""

    return _delete_target_credential(
        store,
        project,
        role,
        reason=reason,
        removed_at=datetime.now(timezone.utc),
    )


def _delete_target_credential(
    store: SecretStore,
    project: WorkspaceState,
    role: TargetCredentialRole,
    *,
    reason: TargetCredentialRemovalReason,
    removed_at: datetime,
) -> TargetCredentialRemovalReceipt | None:
    credential_id = _target_credential_id(project, role)
    encoded = store.get(credential_id)
    if encoded is None:
        return None
    binding_hash: str | None = None
    storage_class = "UNKNOWN"
    target_hash = _target_hash(project)
    try:
        payload = json.loads(encoded)
        binding_id = str(UUID(str(payload["binding_id"])))
        if (
            set(payload)
            == {
                "binding_id",
                "contract_version",
                "role",
                "secret",
                "storage_class",
                "target_hash",
            }
            and payload["contract_version"]
            == TARGET_CREDENTIAL_CONTRACT_VERSION
            and payload["role"] == role.value
            and payload["target_hash"] == target_hash
            and payload["storage_class"]
            in {"SESSION", "OPERATING_SYSTEM_VAULT"}
        ):
            binding_hash = _binding_hash(role, target_hash, binding_id)
            storage_class = str(payload["storage_class"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        pass
    store.delete(credential_id)
    semantic = {
        "credential_binding_hash": binding_hash,
        "credential_role": role.value,
        "connection_target_hash": target_hash,
        "reason": reason.value,
        "removed_at": removed_at,
        "storage_class": storage_class,
    }
    return TargetCredentialRemovalReceipt(
        role=role,
        reason=reason,
        connection_target_hash=target_hash,
        credential_binding_hash=binding_hash,
        storage_class=storage_class,
        removed_at=removed_at,
        receipt_hash=content_hash(semantic),
    )


def audit_removed_target_credentials(
    projects: WorkspaceStateService,
    project: WorkspaceState,
    receipts: tuple[TargetCredentialRemovalReceipt, ...],
    *,
    actor: Actor,
) -> None:
    """Persist actor-bound receipts outside the deletable project database."""

    for receipt in receipts:
        projects.record_credential_removal_receipt(
            receipt_hash=receipt.receipt_hash,
            project_id=project.project_id,
            role=receipt.role.value,
            reason=receipt.reason.value,
            connection_target_hash=receipt.connection_target_hash,
            credential_binding_hash=receipt.credential_binding_hash,
            storage_class=receipt.storage_class,
            removed_at=receipt.removed_at,
            actor=actor,
        )


def local_read_credential_binding_hash(project: WorkspaceState) -> str:
    """Bind no-key local metadata evidence without claiming user identity."""

    return content_hash(
        {
            "credential_role": TargetCredentialRole.READ.value,
            "kind": "LOCAL_NO_KEY_METADATA",
            "target_hash": _target_hash(project),
        }
    )


def _target_credential_id(
    project: WorkspaceState,
    role: TargetCredentialRole,
) -> str:
    target = "\0".join(
        (
            project.project_id,
            role.value,
            _connection_mode(project),
            project.odoo_base_url,
            project.odoo_database,
        )
    ).encode("utf-8")
    digest = hashlib.sha256(target).hexdigest()[:24]
    return f"{project.project_id}:{role.value.lower()}:{digest}"


def _binding_hash(
    role: TargetCredentialRole,
    target_hash: str,
    binding_id: str,
) -> str:
    return content_hash(
        {
            "credential_role": role.value,
            "target_hash": target_hash,
            "binding_id": binding_id,
        }
    )


def _target_hash(project: WorkspaceState) -> str:
    return target_identity_hash(
        connection_mode=_connection_mode(project),
        base_url=project.odoo_base_url,
        database=project.odoo_database,
    )


def _connection_mode(project: WorkspaceState) -> str:
    return (
        project.odoo_connection_mode.value
        if project.odoo_connection_mode is not None
        else ""
    )

