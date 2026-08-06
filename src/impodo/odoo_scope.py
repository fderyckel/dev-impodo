"""Per-preview Odoo API capabilities derived from reviewed execution evidence.

The scope is deliberately not a product allowlist.  It contains only the
models and fields present in one immutable execution snapshot, preventing an
adapter caller from widening a reviewed load while allowing standard,
extension, and custom Odoo schema surfaces without code changes.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import re

from .models import canonical_json_bytes


_MODEL_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_.]*")
_FIELD_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


@dataclass(frozen=True, slots=True)
class OdooModelScope:
    """Exact operations permitted for one model in one reviewed preview."""

    model: str
    write_fields: tuple[str, ...] = ()
    read_fields: tuple[str, ...] = ()
    lookup_fields: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if _MODEL_NAME.fullmatch(self.model) is None:
            raise ValueError(f"Invalid captured Odoo model name: {self.model}")
        for label, fields in (
            ("write", self.write_fields),
            ("read", self.read_fields),
            ("lookup", self.lookup_fields),
        ):
            if tuple(sorted(set(fields))) != fields:
                raise ValueError(f"Odoo {label} fields must be sorted and unique")
            invalid = next(
                (field for field in fields if _FIELD_NAME.fullmatch(field) is None),
                None,
            )
            if invalid is not None:
                raise ValueError(f"Invalid captured Odoo field name: {invalid}")
        if not (self.write_fields or self.read_fields or self.lookup_fields):
            raise ValueError("An Odoo model scope must permit one reviewed operation")
        if not set(self.write_fields).issubset(self.read_fields):
            raise ValueError("Every reviewed write field must support read-back")

    def portable_dict(self) -> dict[str, object]:
        return {
            "model": self.model,
            "write_fields": list(self.write_fields),
            "read_fields": list(self.read_fields),
            "lookup_fields": list(self.lookup_fields),
        }


@dataclass(frozen=True, slots=True)
class OdooApiScope:
    """Closed native-API capability for one immutable load preview."""

    preview_hash: str
    models: tuple[OdooModelScope, ...]

    def __post_init__(self) -> None:
        if not self.preview_hash.startswith("sha256:"):
            raise ValueError("Odoo API scope requires an execution-preview hash")
        names = tuple(item.model for item in self.models)
        if not names or tuple(sorted(set(names))) != names:
            raise ValueError("Odoo model scopes must be sorted, unique, and non-empty")

    @property
    def semantic_hash(self) -> str:
        return "sha256:" + sha256(
            canonical_json_bytes(
                {
                    "preview_hash": self.preview_hash,
                    "models": [item.portable_dict() for item in self.models],
                }
            )
        ).hexdigest()

    def model(self, name: str) -> OdooModelScope | None:
        return next((item for item in self.models if item.model == name), None)

    def write_fields(self, model: str) -> frozenset[str]:
        item = self.model(model)
        return frozenset(item.write_fields) if item is not None else frozenset()

    def read_fields(self, model: str) -> frozenset[str]:
        item = self.model(model)
        return frozenset(item.read_fields) if item is not None else frozenset()

    def lookup_fields(self, model: str) -> frozenset[str]:
        item = self.model(model)
        return frozenset(item.lookup_fields) if item is not None else frozenset()
