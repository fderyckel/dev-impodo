"""Per-preview Odoo API capabilities derived from reviewed execution evidence.

The scope is deliberately not a product allowlist.  It contains only the
models and fields present in one immutable execution snapshot, preventing an
adapter caller from widening a reviewed load while allowing standard,
extension, and custom Odoo schema surfaces without code changes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from hashlib import sha256
from typing import Any

from impodo.domain.execution_snapshot import ExecutionSnapshot
from impodo.domain.shared.models import BusinessReference, canonical_json_bytes

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
    creatable_models: tuple[str, ...] = field(default=(), compare=False)

    def __post_init__(self) -> None:
        if not self.preview_hash.startswith("sha256:"):
            raise ValueError("Odoo API scope requires an execution-preview hash")
        names = tuple(item.model for item in self.models)
        if not names or tuple(sorted(set(names))) != names:
            raise ValueError("Odoo model scopes must be sorted, unique, and non-empty")
        if (tuple(sorted(set(self.creatable_models))) != self.creatable_models
            or not set(self.creatable_models).issubset(item.model for item in self.models if item.write_fields)):
            raise ValueError("Odoo create checks must stay inside the reviewed write scope")

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


def execution_api_scope(snapshot: ExecutionSnapshot) -> OdooApiScope:
    """Derive the exact native API capability from one frozen preview."""

    write_fields: dict[str, set[str]] = {}
    read_fields: dict[str, set[str]] = {}
    lookup_fields: dict[str, set[str]] = {}

    def register_nested_lookup(value: Any) -> None:
        if isinstance(value, BusinessReference):
            fields = (*value.target_fields, *value.target_scope_fields)
            if fields:
                lookup_fields.setdefault(value.model, set()).update(fields)
            for component in (*value.key, *value.scope):
                register_nested_lookup(component)
        elif isinstance(value, tuple):
            for component in value:
                register_nested_lookup(component)

    row_by_id = {row.row_id: row for row in snapshot.rows}
    for dataset in snapshot.datasets:
        lookup_fields.setdefault(dataset.target_model, set()).update(
            (*dataset.identity_fields, *dataset.scope_fields)
        )
    for row in snapshot.rows:
        # Nested identity reads need their exact frozen parent capabilities,
        # including parents that are reference-only datasets with no writes.
        for component in (*row.business_identity, *row.business_scope):
            register_nested_lookup(component)
        if row.disposition not in {"CREATE", "UPDATE"}:
            continue
        for intent in row.fields:
            if intent.action not in {"OMIT", "EXPECT_PROTECTED"}:
                write_fields.setdefault(row.target_model, set()).add(intent.field)
            if intent.action != "OMIT":
                read_fields.setdefault(row.target_model, set()).add(intent.field)
            if intent.kind == "relation" and intent.action != "EXPECT_PROTECTED":
                register_nested_lookup(intent.value)
                lookup_fields.setdefault(intent.related_model, set()).update(
                    (
                        *intent.related_identity_fields,
                        *intent.related_scope_fields,
                    )
                )
                if intent.incoming_projection_field:
                    for dependency_row_id in intent.dependency_row_ids:
                        dependency = row_by_id[dependency_row_id]
                        read_fields.setdefault(
                            dependency.target_model,
                            set(),
                        ).add(intent.incoming_projection_field)
    model_names = sorted(set(write_fields) | set(read_fields) | set(lookup_fields))
    return OdooApiScope(
        preview_hash=snapshot.semantic_hash,
        # This transient permission check is derived from the frozen rows. It
        # does not change historical API-scope hashes used during recovery.
        creatable_models=tuple(sorted({row.target_model for row in snapshot.rows
            if row.disposition == "CREATE" and write_fields.get(row.target_model)})),
        models=tuple(
            OdooModelScope(
                model=model,
                write_fields=tuple(sorted(write_fields.get(model, set()))),
                read_fields=tuple(sorted(read_fields.get(model, set()))),
                lookup_fields=tuple(sorted(lookup_fields.get(model, set()))),
            )
            for model in model_names
            if write_fields.get(model)
            or read_fields.get(model)
            or lookup_fields.get(model)
        )
    )
