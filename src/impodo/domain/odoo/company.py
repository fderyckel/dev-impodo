"""Target-specific Odoo operation company, kept outside portable Recipes.

See docs/developer/workflow/02-odoo-data.md.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass

from impodo.domain.serialization import content_hash


@dataclass(frozen=True, slots=True)
class OdooCompanySelection:
    """One explicitly confirmed company on one exact Odoo database."""

    target_hash: str
    company_id: int
    name: str

    def __post_init__(self) -> None:
        if (
            re.fullmatch(r"sha256:[0-9a-f]{64}", self.target_hash) is None
            or type(self.company_id) is not int
            or self.company_id <= 0
            or not isinstance(self.name, str)
            or not self.name.strip()
            or len(self.name) > 200
        ):
            raise ValueError("The selected Odoo company is invalid")

    @property
    def binding_hash(self) -> str:
        """Bind the protected selection without exposing its numeric identifier."""
        return content_hash(asdict(self))

    @property
    def context(self) -> dict[str, object]:
        """Return the closed context used for all operations on this target."""
        return {"allowed_company_ids": [self.company_id]}

    @property
    def context_hash(self) -> str:
        """Match the connector's observed company-context identity contract."""
        return content_hash(
            {
                "contract_version": 4,
                "kind": "ODOO_AVAILABLE_COMPANY_SCOPE",
                "primary_company_id": self.company_id,
                "allowed_company_ids": [self.company_id],
            }
        )

    def to_json(self) -> str:
        """Serialize only into protected target-specific storage."""
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_json(cls, value: str) -> OdooCompanySelection:
        """Validate a protected selection before projecting it into a workspace."""
        payload = json.loads(value)
        if not isinstance(payload, dict) or set(payload) != {
            "target_hash",
            "company_id",
            "name",
        }:
            raise ValueError("The saved Odoo company is invalid")
        return cls(**payload)


@dataclass(frozen=True, slots=True)
class OdooCompanyAccess:
    """A bounded, transient company list for one authenticated checking user."""

    login: str
    default_company_name: str
    choices: tuple[OdooCompanySelection, ...]


@dataclass(frozen=True, slots=True)
class CompanyReferenceCheck:
    """Protected results of an authoritative, bounded Odoo company-domain read."""

    parent_model: str
    field: str
    relation_model: str
    incompatible_ids: tuple[int, ...]

    def __post_init__(self) -> None:
        if (
            any(
                re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]{0,127}", value) is None
                for value in (self.parent_model, self.field, self.relation_model)
            )
            or tuple(sorted(set(self.incompatible_ids))) != self.incompatible_ids
            or any(type(item) is not int or item <= 0 for item in self.incompatible_ids)
        ):
            raise ValueError("Odoo company reference evidence is invalid")

    def to_dict(self) -> dict[str, object]:
        """Serialize only inside the protected target metadata snapshot."""
        return {**asdict(self), "incompatible_ids": list(self.incompatible_ids)}

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> CompanyReferenceCheck:
        ids = value.get("incompatible_ids")
        if not isinstance(ids, list):
            raise TypeError("Odoo company reference evidence is invalid")
        return cls(
            str(value["parent_model"]),
            str(value["field"]),
            str(value["relation_model"]),
            tuple(ids),
        )
