"""Encrypt immutable, bounded Project-level evidence artifacts."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from hashlib import sha256
import os
from pathlib import Path, PurePosixPath
import tempfile

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from ..domain.serialization import canonical_json
from ..domain.cutover.models import MigrationCutoverError
from impodo.domain.project.foundation import require_hash, require_uuid
from impodo.application.shared.secrets import SecretStore, SecretStoreError


_MAGIC = b"IPPRJ001"
_NONCE_BYTES = 12
MAX_PROJECT_EVIDENCE_BYTES = 16 * 1024 * 1024
_ARTIFACT_KINDS = frozenset(
    {"qualifications", "correction-plans", "correction-confirmations"}
)


@dataclass(frozen=True, slots=True)
class StoredProjectEvidence:
    storage_key: str
    size_bytes: int
    artifact_hash: str


class ProtectedProjectEvidenceStore:
    """Authenticate Project evidence payloads on every write and read."""

    def __init__(self, root: str | Path, secrets: SecretStore) -> None:
        self._root = Path(root).resolve() / ".project-evidence-protected"
        self._root.mkdir(parents=True, exist_ok=True)
        self._root.chmod(0o700)
        self._secrets = secrets

    def put(
        self,
        project_id: str,
        *,
        qualification_id: str,
        logical_hash: str,
        payload: bytes,
    ) -> StoredProjectEvidence:
        """Keep the original qualification API as a compatibility facade."""

        return self.put_artifact(
            project_id,
            artifact_kind="qualifications",
            artifact_id=qualification_id,
            logical_hash=logical_hash,
            payload=payload,
        )

    def put_artifact(
        self,
        project_id: str,
        *,
        artifact_kind: str,
        artifact_id: str,
        logical_hash: str,
        payload: bytes,
    ) -> StoredProjectEvidence:
        project_id = require_uuid(project_id, "project_id")
        artifact_id = require_uuid(artifact_id, "artifact_id")
        if artifact_kind not in _ARTIFACT_KINDS:
            raise MigrationCutoverError("Project evidence artifact kind is invalid")
        require_hash(logical_hash, "logical_hash")
        if not payload or len(payload) > MAX_PROJECT_EVIDENCE_BYTES:
            raise MigrationCutoverError("Project evidence payload size is invalid")
        storage_key = self.artifact_storage_key(
            project_id,
            artifact_kind=artifact_kind,
            artifact_id=artifact_id,
            logical_hash=logical_hash,
        )
        path = self._path(storage_key)
        if path.exists():
            existing = self.inspect(
                project_id,
                storage_key=storage_key,
                logical_hash=logical_hash,
            )
            if self.read(
                project_id,
                storage_key=storage_key,
                logical_hash=logical_hash,
                expected_artifact_hash=existing.artifact_hash,
            ) != payload:
                raise MigrationCutoverError(
                    "Stored Project evidence is inconsistent"
                )
            return existing

        path.parent.mkdir(parents=True, exist_ok=True)
        path.parent.chmod(0o700)
        nonce = os.urandom(_NONCE_BYTES)
        encrypted = _MAGIC + nonce + AESGCM(
            self._key(project_id, create=True)
        ).encrypt(
            nonce,
            payload,
            self._associated(project_id, storage_key, logical_hash),
        )
        handle, temporary_name = tempfile.mkstemp(
            prefix=".candidate-",
            suffix=".ipe",
            dir=path.parent,
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(handle, "wb") as stream:
                stream.write(encrypted)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.chmod(0o600)
            temporary.replace(path)
        finally:
            if temporary.exists():
                temporary.unlink()
        return StoredProjectEvidence(
            storage_key=storage_key,
            size_bytes=len(encrypted),
            artifact_hash=self._hash(encrypted),
        )

    def storage_key(
        self,
        project_id: str,
        *,
        qualification_id: str,
        logical_hash: str,
    ) -> str:
        """Return the historical qualification storage-key shape."""

        return self.artifact_storage_key(
            project_id,
            artifact_kind="qualifications",
            artifact_id=qualification_id,
            logical_hash=logical_hash,
        )

    def artifact_storage_key(
        self,
        project_id: str,
        *,
        artifact_kind: str,
        artifact_id: str,
        logical_hash: str,
    ) -> str:
        project_id = require_uuid(project_id, "project_id")
        artifact_id = require_uuid(artifact_id, "artifact_id")
        if artifact_kind not in _ARTIFACT_KINDS:
            raise MigrationCutoverError("Project evidence artifact kind is invalid")
        require_hash(logical_hash, "logical_hash")
        return (
            f"{project_id}/{artifact_kind}/{artifact_id}-"
            f"{logical_hash.removeprefix('sha256:')[:16]}.ipe"
        )

    def inspect(
        self,
        project_id: str,
        *,
        storage_key: str,
        logical_hash: str,
    ) -> StoredProjectEvidence:
        path = self._path(storage_key)
        if not path.is_file():
            raise MigrationCutoverError("Protected Project evidence is missing")
        encrypted = path.read_bytes()
        artifact_hash = self._hash(encrypted)
        self._decrypt(project_id, storage_key, logical_hash, encrypted)
        return StoredProjectEvidence(storage_key, len(encrypted), artifact_hash)

    def read(
        self,
        project_id: str,
        *,
        storage_key: str,
        logical_hash: str,
        expected_artifact_hash: str,
    ) -> bytes:
        require_hash(expected_artifact_hash, "expected_artifact_hash")
        path = self._path(storage_key)
        if not path.is_file():
            raise MigrationCutoverError("Protected Project evidence is missing")
        encrypted = path.read_bytes()
        if self._hash(encrypted) != expected_artifact_hash:
            raise MigrationCutoverError("Protected Project evidence hash changed")
        return self._decrypt(project_id, storage_key, logical_hash, encrypted)

    def _decrypt(
        self,
        project_id: str,
        storage_key: str,
        logical_hash: str,
        encrypted: bytes,
    ) -> bytes:
        project_id = require_uuid(project_id, "project_id")
        require_hash(logical_hash, "logical_hash")
        if len(encrypted) <= len(_MAGIC) + _NONCE_BYTES + 16:
            raise MigrationCutoverError("Protected Project evidence is truncated")
        if not encrypted.startswith(_MAGIC):
            raise MigrationCutoverError("Protected Project evidence header is invalid")
        nonce_start = len(_MAGIC)
        nonce_end = nonce_start + _NONCE_BYTES
        try:
            return AESGCM(self._key(project_id, create=False)).decrypt(
                encrypted[nonce_start:nonce_end],
                encrypted[nonce_end:],
                self._associated(project_id, storage_key, logical_hash),
            )
        except InvalidTag as error:
            raise MigrationCutoverError(
                "Protected Project evidence authentication failed"
            ) from error

    def _key(self, project_id: str, *, create: bool) -> bytes:
        key_id = f"{project_id}:protected:project-evidence-v1"
        encoded = self._secrets.get(key_id)
        if encoded is None:
            if not create:
                raise SecretStoreError("Protected Project evidence key is missing")
            key = os.urandom(32)
            self._secrets.set(
                key_id,
                base64.urlsafe_b64encode(key).decode("ascii"),
                persistent=True,
            )
            return key
        try:
            key = base64.b64decode(
                encoded.encode("ascii"),
                altchars=b"-_",
                validate=True,
            )
        except (UnicodeError, ValueError) as error:
            raise SecretStoreError("Protected Project evidence key is invalid") from error
        if len(key) != 32:
            raise SecretStoreError("Protected Project evidence key is invalid")
        return key

    def _path(self, storage_key: str) -> Path:
        pure = PurePosixPath(storage_key)
        if (
            pure.is_absolute()
            or len(pure.parts) != 3
            or pure.parts[1] not in _ARTIFACT_KINDS
            or ".." in pure.parts
            or not pure.parts[2].endswith(".ipe")
        ):
            raise MigrationCutoverError("Project evidence storage key is invalid")
        require_uuid(pure.parts[0], "project_id")
        path = (self._root / pure).resolve()
        if path.parent.parent.parent != self._root:
            raise MigrationCutoverError("Project evidence storage key escapes its root")
        return path

    @staticmethod
    def _associated(project_id: str, storage_key: str, logical_hash: str) -> bytes:
        return canonical_json(
            {
                "contract": "protected-project-evidence-v1",
                "logical_hash": logical_hash,
                "project_id": project_id,
                "storage_key": storage_key,
            }
        ).encode("utf-8")

    @staticmethod
    def _hash(value: bytes) -> str:
        return "sha256:" + sha256(value).hexdigest()
