"""Keep Odoo read and write credentials role- and target-bound.

The vault stores one versioned envelope per exact project/target/role.  The
random binding ID lets durable read evidence identify a credential generation
without persisting a secret or secret-derived verifier.  It is deliberately a
credential binding, not a claim about the authenticated Odoo user; a principal
probe remains a separate contract.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
import hashlib
import json
from uuid import UUID, uuid4

from ..access import Actor
from ..domain.serialization import content_hash
from ..models import target_identity_hash
from ..projects import MigrationProject, ProjectService
from ..secrets import SecretStore, SecretStoreError


class TargetCredentialRole(StrEnum):
    """Separate Odoo capabilities that must never fall back to each other."""

    READ = "READ"
    WRITE = "WRITE"


@dataclass(frozen=True, slots=True)
class TargetCredential:
    """Return one vault secret with its safe evidence binding."""

    secret: str
    binding_hash: str
    replaced: bool = field(default=False, compare=False)
    persistent: bool = field(default=False, compare=False)


def target_read_credential_id(project: MigrationProject) -> str:
    """Return the opaque vault ID for read-only Odoo access."""

    return _target_credential_id(project, TargetCredentialRole.READ)


def target_write_credential_id(project: MigrationProject) -> str:
    """Return the opaque vault ID for Odoo write and read-back access."""

    return _target_credential_id(project, TargetCredentialRole.WRITE)


def store_target_credential(
    store: SecretStore,
    project: MigrationProject,
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
            "contract_version": 1,
            "role": role.value,
            "target_hash": target_hash,
            "binding_id": binding_id,
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
    project: MigrationProject,
    role: TargetCredentialRole,
) -> TargetCredential | None:
    """Load and validate one exact role envelope without cross-role fallback."""

    encoded = store.get(_target_credential_id(project, role))
    if encoded is None:
        return None
    try:
        payload = json.loads(encoded)
        binding_id = str(UUID(str(payload["binding_id"])))
        secret = str(payload["secret"]).strip()
        target_hash = _target_hash(project)
        valid = (
            payload["contract_version"] == 1
            and payload["role"] == role.value
            and payload["target_hash"] == target_hash
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
    )


def audit_stored_target_credential(
    projects: ProjectService,
    project: MigrationProject,
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
    project: MigrationProject,
) -> None:
    """Delete both current role-qualified credential entries."""

    store.delete(target_read_credential_id(project))
    store.delete(target_write_credential_id(project))


def local_read_credential_binding_hash(project: MigrationProject) -> str:
    """Bind no-key local metadata evidence without claiming user identity."""

    return content_hash(
        {
            "credential_role": TargetCredentialRole.READ.value,
            "kind": "LOCAL_NO_KEY_METADATA",
            "target_hash": _target_hash(project),
        }
    )


def _target_credential_id(
    project: MigrationProject,
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


def _target_hash(project: MigrationProject) -> str:
    return target_identity_hash(
        connection_mode=_connection_mode(project),
        base_url=project.odoo_base_url,
        database=project.odoo_database,
    )


def _connection_mode(project: MigrationProject) -> str:
    return (
        project.odoo_connection_mode.value
        if project.odoo_connection_mode is not None
        else ""
    )
