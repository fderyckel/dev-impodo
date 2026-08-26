"""Application-encrypted, Recipe-scoped immutable payload storage."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from hashlib import sha256
import os
from pathlib import Path, PurePosixPath
import re
import tempfile
from uuid import UUID

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from ..domain.serialization import canonical_json
from ..domain.recipe.models import RecipeIntegrityError
from impodo.domain.project.foundation import require_hash, require_uuid
from impodo.application.shared.secrets import SecretStore, SecretStoreError


_MAGIC = b"IPRCP001"
_NONCE_BYTES = 12
_KINDS = frozenset({"applications", "qualifications", "revisions"})
_OBJECT_ID = re.compile(r"(?:v[1-9][0-9]{0,8}|[0-9a-f-]{36})\Z")
MAX_RECIPE_PAYLOAD_BYTES = 4 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class StoredRecipePayload:
    storage_key: str
    size_bytes: int
    artifact_hash: str


class ProtectedRecipeStore:
    """Encrypt immutable Recipe payloads and verify them on every read."""

    def __init__(self, root: str | Path, secrets: SecretStore) -> None:
        self._root = Path(root).resolve() / ".recipes-protected"
        self._root.mkdir(parents=True, exist_ok=True)
        self._root.chmod(0o700)
        self._secrets = secrets

    def storage_key(
        self,
        recipe_id: str,
        *,
        kind: str,
        object_id: str,
        logical_hash: str,
    ) -> str:
        """Return the deterministic opaque key for one immutable payload."""

        recipe_id = require_uuid(recipe_id, "recipe_id")
        normalized_kind = self._kind(kind)
        if _OBJECT_ID.fullmatch(object_id) is None:
            raise RecipeIntegrityError("Recipe payload object identifier is invalid")
        require_hash(logical_hash, "logical_hash")
        return (
            f"{recipe_id}/{normalized_kind}/{object_id}-"
            f"{logical_hash.removeprefix('sha256:')}.ipr"
        )

    def put(
        self,
        recipe_id: str,
        *,
        kind: str,
        object_id: str,
        logical_hash: str,
        payload: bytes,
    ) -> StoredRecipePayload:
        """Encrypt and atomically publish one bounded immutable payload."""

        if not payload or len(payload) > MAX_RECIPE_PAYLOAD_BYTES:
            raise RecipeIntegrityError("Recipe payload size is invalid")
        storage_key = self.storage_key(
            recipe_id,
            kind=kind,
            object_id=object_id,
            logical_hash=logical_hash,
        )
        path = self._path(storage_key)
        if path.exists():
            existing = self.inspect(
                recipe_id,
                storage_key=storage_key,
                logical_hash=logical_hash,
            )
            if self.read(
                recipe_id,
                storage_key=storage_key,
                logical_hash=logical_hash,
                expected_artifact_hash=existing.artifact_hash,
            ) != payload:
                raise RecipeIntegrityError("Stored Recipe payload is inconsistent")
            return existing

        path.parent.mkdir(parents=True, exist_ok=True)
        path.parent.chmod(0o700)
        nonce = os.urandom(_NONCE_BYTES)
        associated = self._associated(recipe_id, storage_key, logical_hash)
        encrypted = _MAGIC + nonce + AESGCM(self._key(recipe_id, create=True)).encrypt(
            nonce,
            payload,
            associated,
        )
        temporary_handle, temporary_name = tempfile.mkstemp(
            prefix=".candidate-",
            suffix=".ipr",
            dir=path.parent,
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(temporary_handle, "wb") as stream:
                stream.write(encrypted)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.chmod(0o600)
            temporary.replace(path)
        finally:
            if temporary.exists():
                temporary.unlink()
        return StoredRecipePayload(
            storage_key=storage_key,
            size_bytes=len(encrypted),
            artifact_hash=self._hash(encrypted),
        )

    def inspect(
        self,
        recipe_id: str,
        *,
        storage_key: str,
        logical_hash: str,
    ) -> StoredRecipePayload:
        """Return bounded artifact metadata after authenticating the payload."""

        path = self._path(storage_key)
        if not path.is_file():
            raise RecipeIntegrityError("Protected Recipe payload is missing")
        encrypted = path.read_bytes()
        artifact_hash = self._hash(encrypted)
        self._decrypt(recipe_id, storage_key, logical_hash, encrypted)
        return StoredRecipePayload(storage_key, len(encrypted), artifact_hash)

    def read(
        self,
        recipe_id: str,
        *,
        storage_key: str,
        logical_hash: str,
        expected_artifact_hash: str,
    ) -> bytes:
        """Verify exact encrypted bytes and return authenticated plaintext."""

        require_hash(expected_artifact_hash, "expected_artifact_hash")
        path = self._path(storage_key)
        if not path.is_file():
            raise RecipeIntegrityError("Protected Recipe payload is missing")
        encrypted = path.read_bytes()
        if self._hash(encrypted) != expected_artifact_hash:
            raise RecipeIntegrityError("Protected Recipe artifact hash changed")
        return self._decrypt(recipe_id, storage_key, logical_hash, encrypted)

    def exists(self, storage_key: str) -> bool:
        """Return whether the exact validated storage key exists."""

        return self._path(storage_key).is_file()

    def delete_recipe(self, recipe_id: str) -> None:
        """Remove only one exact Recipe directory and its encryption key."""

        recipe_id = require_uuid(recipe_id, "recipe_id")
        directory = (self._root / recipe_id).resolve()
        if directory.parent != self._root:
            raise RecipeIntegrityError("Recipe storage path is invalid")
        if directory.exists():
            for path in sorted(directory.rglob("*"), reverse=True):
                if path.is_file():
                    path.unlink()
                elif path.is_dir():
                    path.rmdir()
            directory.rmdir()
        self._secrets.delete(self._key_id(recipe_id))

    def _decrypt(
        self,
        recipe_id: str,
        storage_key: str,
        logical_hash: str,
        encrypted: bytes,
    ) -> bytes:
        require_uuid(recipe_id, "recipe_id")
        require_hash(logical_hash, "logical_hash")
        if len(encrypted) <= len(_MAGIC) + _NONCE_BYTES + 16:
            raise RecipeIntegrityError("Protected Recipe payload is truncated")
        if not encrypted.startswith(_MAGIC):
            raise RecipeIntegrityError("Protected Recipe payload header is invalid")
        nonce_start = len(_MAGIC)
        nonce_end = nonce_start + _NONCE_BYTES
        try:
            return AESGCM(self._key(recipe_id, create=False)).decrypt(
                encrypted[nonce_start:nonce_end],
                encrypted[nonce_end:],
                self._associated(recipe_id, storage_key, logical_hash),
            )
        except InvalidTag as error:
            raise RecipeIntegrityError(
                "Protected Recipe payload authentication failed"
            ) from error

    def _key(self, recipe_id: str, *, create: bool) -> bytes:
        key_id = self._key_id(recipe_id)
        encoded = self._secrets.get(key_id)
        if encoded is None:
            if not create:
                raise SecretStoreError("Protected Recipe key is missing")
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
        except (ValueError, UnicodeError) as error:
            raise SecretStoreError("Protected Recipe key is invalid") from error
        if len(key) != 32:
            raise SecretStoreError("Protected Recipe key is invalid")
        return key

    def _path(self, storage_key: str) -> Path:
        pure = PurePosixPath(storage_key)
        if pure.is_absolute() or len(pure.parts) != 3 or ".." in pure.parts:
            raise RecipeIntegrityError("Recipe storage key is invalid")
        recipe_id, kind, filename = pure.parts
        require_uuid(recipe_id, "recipe_id")
        self._kind(kind)
        if not filename.endswith(".ipr"):
            raise RecipeIntegrityError("Recipe storage key is invalid")
        path = (self._root / recipe_id / kind / filename).resolve()
        if path.parent.parent.parent != self._root:
            raise RecipeIntegrityError("Recipe storage key escapes its root")
        return path

    @staticmethod
    def _associated(recipe_id: str, storage_key: str, logical_hash: str) -> bytes:
        return canonical_json(
            {
                "contract": "protected-recipe-payload-v1",
                "logical_hash": logical_hash,
                "recipe_id": recipe_id,
                "storage_key": storage_key,
            }
        ).encode("utf-8")

    @staticmethod
    def _hash(value: bytes) -> str:
        return "sha256:" + sha256(value).hexdigest()

    @staticmethod
    def _kind(value: str) -> str:
        normalized = value.strip().casefold()
        if normalized not in _KINDS:
            raise RecipeIntegrityError("Recipe payload kind is invalid")
        return normalized

    @staticmethod
    def _key_id(recipe_id: str) -> str:
        UUID(recipe_id)
        return f"{recipe_id}:protected:recipe-v1"
