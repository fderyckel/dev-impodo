"""Define exact, application-specific Recipe binding and drift evidence.

Recipe revisions contain reusable meaning.  The contracts in this module bind
that meaning to one replacement source package and one current Odoo target.
They deliberately contain no credential value and grant no write authority.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from enum import StrEnum
import re
from typing import Mapping

from ..access import ActorIdentity
from ..models import assert_no_numeric_odoo_ids
from ..recipes import require_hash, require_uuid
from .serialization import canonical_json, content_hash, portable


TARGET_BINDING_CONTRACT_VERSION = 1
RECIPE_PARAMETER_VALUES_CONTRACT_VERSION = 1
RECIPE_CONTROL_VALUES_CONTRACT_VERSION = 1
RECIPE_APPLICATION_CONTRACT_VERSION = 1
MAX_APPLICATION_OVERRIDES = 10_000
MAX_APPLICATION_ISSUES = 2_000
MAX_PARAMETER_VALUES_BYTES = 64 * 1024

_TECHNICAL_ID = re.compile(r"[a-z][a-z0-9_.:-]{0,299}\Z")


class RecipeApplicationError(ValueError):
    """Raised when exact Recipe application evidence is invalid."""


class TargetEnvironment(StrEnum):
    TEST = "TEST"
    PRODUCTION = "PRODUCTION"


class TargetCredentialRole(StrEnum):
    READ = "READ"
    WRITE = "WRITE"


class TargetProbeStatus(StrEnum):
    ACCEPTED = "ACCEPTED"


class RecipeApplicationState(StrEnum):
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    READY = "READY"
    APPLIED = "APPLIED"
    BLOCKED = "BLOCKED"


class RecipeApplicationIssueLevel(StrEnum):
    BLOCKER = "BLOCKER"
    REVIEW = "REVIEW"
    INFORMATION = "INFORMATION"


@dataclass(frozen=True, slots=True)
class TargetBinding:
    """Non-secret evidence for one exact probed Odoo credential generation."""

    target_binding_id: str
    environment: TargetEnvironment
    endpoint: str
    database: str
    connection_target_hash: str
    credential_role: TargetCredentialRole
    credential_generation: str
    credential_storage_class: str
    principal_hash: str
    permission_hash: str
    context_hash: str
    schema_dependency_hash: str
    reference_snapshot_hashes: tuple[str, ...]
    probe_status: TargetProbeStatus
    probed_at: datetime
    captured_by: ActorIdentity
    contract_version: int = TARGET_BINDING_CONTRACT_VERSION

    def __post_init__(self) -> None:
        require_uuid(self.target_binding_id, "target_binding_id")
        object.__setattr__(self, "environment", TargetEnvironment(self.environment))
        object.__setattr__(
            self,
            "credential_role",
            TargetCredentialRole(self.credential_role),
        )
        object.__setattr__(self, "probe_status", TargetProbeStatus(self.probe_status))
        if self.contract_version != TARGET_BINDING_CONTRACT_VERSION:
            raise RecipeApplicationError("TargetBinding contract is unsupported")
        if not self.endpoint.strip() or len(self.endpoint) > 2_000:
            raise RecipeApplicationError("Target endpoint is invalid")
        if not self.database.strip() or len(self.database) > 200:
            raise RecipeApplicationError("Target database is invalid")
        if not self.credential_generation.strip() or len(self.credential_generation) > 200:
            raise RecipeApplicationError("Credential generation is invalid")
        if self.credential_storage_class not in {
            "SESSION",
            "OPERATING_SYSTEM_VAULT",
            "PROJECT_SECRET_STORE",
        }:
            raise RecipeApplicationError("Credential storage class is invalid")
        for value, label in (
            (self.connection_target_hash, "connection_target_hash"),
            (self.principal_hash, "principal_hash"),
            (self.permission_hash, "permission_hash"),
            (self.context_hash, "context_hash"),
            (self.schema_dependency_hash, "schema_dependency_hash"),
        ):
            require_hash(value, label)
        for value in self.reference_snapshot_hashes:
            require_hash(value, "reference_snapshot_hash")
        if self.probed_at.tzinfo is None or self.probed_at.utcoffset() is None:
            raise RecipeApplicationError("Target probe time must be timezone-aware")

    @property
    def content_hash(self) -> str:
        return content_hash(self.to_dict(include_hash=False))

    def to_dict(self, *, include_hash: bool = True) -> dict[str, object]:
        payload = portable(asdict(self))
        if include_hash:
            payload["content_hash"] = self.content_hash
        return payload

    def to_json(self) -> str:
        return canonical_json(self.to_dict())

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "TargetBinding":
        actor = dict(payload["captured_by"])
        result = cls(
            contract_version=int(payload["contract_version"]),
            target_binding_id=str(payload["target_binding_id"]),
            environment=TargetEnvironment(str(payload["environment"])),
            endpoint=str(payload["endpoint"]),
            database=str(payload["database"]),
            connection_target_hash=str(payload["connection_target_hash"]),
            credential_role=TargetCredentialRole(str(payload["credential_role"])),
            credential_generation=str(payload["credential_generation"]),
            credential_storage_class=str(payload["credential_storage_class"]),
            principal_hash=str(payload["principal_hash"]),
            permission_hash=str(payload["permission_hash"]),
            context_hash=str(payload["context_hash"]),
            schema_dependency_hash=str(payload["schema_dependency_hash"]),
            reference_snapshot_hashes=tuple(
                str(item) for item in payload.get("reference_snapshot_hashes", ())
            ),
            probe_status=TargetProbeStatus(str(payload["probe_status"])),
            probed_at=datetime.fromisoformat(str(payload["probed_at"])),
            captured_by=ActorIdentity(
                issuer=str(actor["issuer"]),
                subject_id=str(actor["subject_id"]),
                display_name=str(actor["display_name"]),
            ),
        )
        if payload.get("content_hash") != result.content_hash:
            raise RecipeApplicationError("TargetBinding content hash is invalid")
        return result


@dataclass(frozen=True, slots=True)
class RecipeParameterValues:
    """Fresh declared parameter values for one exact DataVersion."""

    data_version_id: str
    values: Mapping[str, object]
    source: str
    reason: str
    actor: ActorIdentity
    confirmed_at: datetime
    contract_version: int = RECIPE_PARAMETER_VALUES_CONTRACT_VERSION

    def __post_init__(self) -> None:
        require_uuid(self.data_version_id, "data_version_id")
        if self.contract_version != RECIPE_PARAMETER_VALUES_CONTRACT_VERSION:
            raise RecipeApplicationError("Parameter-values contract is unsupported")
        if len(self.values) > 100:
            raise RecipeApplicationError("Too many Recipe parameter values")
        for key in self.values:
            if _TECHNICAL_ID.fullmatch(str(key)) is None:
                raise RecipeApplicationError("Recipe parameter ID is invalid")
        if not self.source.strip() or len(self.source) > 120:
            raise RecipeApplicationError("Parameter source is invalid")
        if not self.reason.strip() or len(self.reason) > 1_000:
            raise RecipeApplicationError("Parameter reason is invalid")
        if self.confirmed_at.tzinfo is None or self.confirmed_at.utcoffset() is None:
            raise RecipeApplicationError("Parameter confirmation time is invalid")
        if len(canonical_json(portable(dict(self.values))).encode("utf-8")) > MAX_PARAMETER_VALUES_BYTES:
            raise RecipeApplicationError("Recipe parameter values are too large")

    @property
    def content_hash(self) -> str:
        return content_hash(self.to_dict(include_hash=False))

    def to_dict(self, *, include_hash: bool = True) -> dict[str, object]:
        payload = portable(asdict(self))
        if include_hash:
            payload["content_hash"] = self.content_hash
        return payload

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "RecipeParameterValues":
        actor = dict(payload["actor"])
        result = cls(
            contract_version=int(payload["contract_version"]),
            data_version_id=str(payload["data_version_id"]),
            values=dict(payload.get("values", {})),
            source=str(payload["source"]),
            reason=str(payload["reason"]),
            actor=ActorIdentity(
                issuer=str(actor["issuer"]),
                subject_id=str(actor["subject_id"]),
                display_name=str(actor["display_name"]),
            ),
            confirmed_at=datetime.fromisoformat(str(payload["confirmed_at"])),
        )
        if payload.get("content_hash") != result.content_hash:
            raise RecipeApplicationError("Parameter-values content hash is invalid")
        return result


@dataclass(frozen=True, slots=True)
class RecipeControlValues:
    """Fresh expected control values for one exact DataVersion."""

    data_version_id: str
    values: Mapping[str, str]
    actor: ActorIdentity
    confirmed_at: datetime
    contract_version: int = RECIPE_CONTROL_VALUES_CONTRACT_VERSION

    def __post_init__(self) -> None:
        require_uuid(self.data_version_id, "data_version_id")
        if self.contract_version != RECIPE_CONTROL_VALUES_CONTRACT_VERSION:
            raise RecipeApplicationError("Control-values contract is unsupported")
        if len(self.values) > 300:
            raise RecipeApplicationError("Too many Recipe control values")
        for key, value in self.values.items():
            if _TECHNICAL_ID.fullmatch(str(key)) is None or len(str(value)) > 200:
                raise RecipeApplicationError("Recipe control value is invalid")
        if self.confirmed_at.tzinfo is None or self.confirmed_at.utcoffset() is None:
            raise RecipeApplicationError("Control confirmation time is invalid")

    @property
    def content_hash(self) -> str:
        return content_hash(self.to_dict(include_hash=False))

    def to_dict(self, *, include_hash: bool = True) -> dict[str, object]:
        payload = portable(asdict(self))
        if include_hash:
            payload["content_hash"] = self.content_hash
        return payload

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "RecipeControlValues":
        actor = dict(payload["actor"])
        result = cls(
            contract_version=int(payload["contract_version"]),
            data_version_id=str(payload["data_version_id"]),
            values={str(key): str(value) for key, value in dict(payload.get("values", {})).items()},
            actor=ActorIdentity(
                issuer=str(actor["issuer"]),
                subject_id=str(actor["subject_id"]),
                display_name=str(actor["display_name"]),
            ),
            confirmed_at=datetime.fromisoformat(str(payload["confirmed_at"])),
        )
        if payload.get("content_hash") != result.content_hash:
            raise RecipeApplicationError("Control-values content hash is invalid")
        return result


@dataclass(frozen=True, slots=True)
class RecipeApplicationIssue:
    """One bounded, fingerprinted source/target/input compatibility result."""

    code: str
    level: RecipeApplicationIssueLevel
    message: str
    recovery_action: str
    logical_id: str = ""

    def __post_init__(self) -> None:
        if _TECHNICAL_ID.fullmatch(self.code.casefold().replace("_", "-")) is None:
            raise RecipeApplicationError("Application issue code is invalid")
        object.__setattr__(self, "level", RecipeApplicationIssueLevel(self.level))
        if not self.message.strip() or len(self.message) > 1_000:
            raise RecipeApplicationError("Application issue message is invalid")
        if not self.recovery_action.strip() or len(self.recovery_action) > 1_000:
            raise RecipeApplicationError("Application recovery action is invalid")

    @property
    def fingerprint(self) -> str:
        return content_hash(
            {
                "code": self.code,
                "level": self.level.value,
                "logical_id": self.logical_id,
                "recovery_action": self.recovery_action,
            }
        )

    @property
    def blocks(self) -> bool:
        return self.level is RecipeApplicationIssueLevel.BLOCKER


@dataclass(frozen=True, slots=True)
class RecipeApplicationDraft:
    """Recoverable exact bindings and focused drift for the current workspace."""

    application_id: str
    recipe_id: str
    recipe_revision: int
    data_version_id: str
    workspace_project_id: str
    target_binding_hash: str
    source_selection_hash: str
    parameter_values_hash: str
    revision: int
    state: RecipeApplicationState
    overrides: Mapping[str, str]
    issues: tuple[RecipeApplicationIssue, ...]
    binding_hash: str
    target_assessment_hash: str
    updated_at: datetime
    updated_by: ActorIdentity

    def __post_init__(self) -> None:
        for value, label in (
            (self.application_id, "application_id"),
            (self.recipe_id, "recipe_id"),
            (self.data_version_id, "data_version_id"),
            (self.workspace_project_id, "workspace_project_id"),
        ):
            require_uuid(value, label)
        for value, label in (
            (self.target_binding_hash, "target_binding_hash"),
            (self.source_selection_hash, "source_selection_hash"),
            (self.parameter_values_hash, "parameter_values_hash"),
            (self.binding_hash, "binding_hash"),
            (self.target_assessment_hash, "target_assessment_hash"),
        ):
            require_hash(value, label)
        object.__setattr__(self, "state", RecipeApplicationState(self.state))
        if self.recipe_revision < 1 or self.revision < 1:
            raise RecipeApplicationError("Application revision is invalid")
        if len(self.overrides) > MAX_APPLICATION_OVERRIDES:
            raise RecipeApplicationError("Application has too many overrides")
        if len(self.issues) > MAX_APPLICATION_ISSUES:
            raise RecipeApplicationError("Application has too many issues")
        if self.updated_at.tzinfo is None or self.updated_at.utcoffset() is None:
            raise RecipeApplicationError("Application update time is invalid")

    @property
    def can_apply(self) -> bool:
        return self.state is RecipeApplicationState.READY and not any(
            item.blocks for item in self.issues
        )

    @property
    def issue_hash(self) -> str:
        return content_hash([item.fingerprint for item in self.issues])

    def to_dict(self) -> dict[str, object]:
        return portable(asdict(self))

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "RecipeApplicationDraft":
        actor = dict(payload["updated_by"])
        return cls(
            application_id=str(payload["application_id"]),
            recipe_id=str(payload["recipe_id"]),
            recipe_revision=int(payload["recipe_revision"]),
            data_version_id=str(payload["data_version_id"]),
            workspace_project_id=str(payload["workspace_project_id"]),
            target_binding_hash=str(payload["target_binding_hash"]),
            source_selection_hash=str(payload["source_selection_hash"]),
            parameter_values_hash=str(payload["parameter_values_hash"]),
            revision=int(payload["revision"]),
            state=RecipeApplicationState(str(payload["state"])),
            overrides={
                str(key): str(value)
                for key, value in dict(payload.get("overrides", {})).items()
            },
            issues=tuple(
                RecipeApplicationIssue(
                    code=str(item["code"]),
                    level=RecipeApplicationIssueLevel(str(item["level"])),
                    message=str(item["message"]),
                    recovery_action=str(item["recovery_action"]),
                    logical_id=str(item.get("logical_id", "")),
                )
                for item in payload.get("issues", ())
            ),
            binding_hash=str(payload["binding_hash"]),
            target_assessment_hash=str(payload["target_assessment_hash"]),
            updated_at=datetime.fromisoformat(str(payload["updated_at"])),
            updated_by=ActorIdentity(
                issuer=str(actor["issuer"]),
                subject_id=str(actor["subject_id"]),
                display_name=str(actor["display_name"]),
            ),
        )


@dataclass(frozen=True, slots=True)
class RecipeApplicationEvidence:
    """Immutable explanation of one blocked or applied Recipe application."""

    application_id: str
    recipe_id: str
    recipe_revision: int
    recipe_semantic_hash: str
    data_version_id: str
    workspace_project_id: str
    source_artifact_hash: str
    source_selection_hash: str
    parameter_values_hash: str
    control_values_hash: str
    target_binding_id: str
    target_binding_hash: str
    target_contract_assessment_hash: str
    binding_hash: str
    issue_hash: str
    mapping_id: str | None
    mapping_content_hash: str | None
    status: RecipeApplicationState
    created_at: datetime
    created_by: ActorIdentity
    contract_version: int = RECIPE_APPLICATION_CONTRACT_VERSION

    def __post_init__(self) -> None:
        for value, label in (
            (self.application_id, "application_id"),
            (self.recipe_id, "recipe_id"),
            (self.data_version_id, "data_version_id"),
            (self.workspace_project_id, "workspace_project_id"),
        ):
            require_uuid(value, label)
        require_uuid(self.target_binding_id, "target_binding_id")
        for value, label in (
            (self.recipe_semantic_hash, "recipe_semantic_hash"),
            (self.source_artifact_hash, "source_artifact_hash"),
            (self.source_selection_hash, "source_selection_hash"),
            (self.parameter_values_hash, "parameter_values_hash"),
            (self.control_values_hash, "control_values_hash"),
            (self.target_binding_hash, "target_binding_hash"),
            (self.target_contract_assessment_hash, "target_contract_assessment_hash"),
            (self.binding_hash, "binding_hash"),
            (self.issue_hash, "issue_hash"),
        ):
            require_hash(value, label)
        if self.mapping_content_hash is not None:
            require_hash(self.mapping_content_hash, "mapping_content_hash")
        object.__setattr__(self, "status", RecipeApplicationState(self.status))
        if self.status not in {RecipeApplicationState.APPLIED, RecipeApplicationState.BLOCKED}:
            raise RecipeApplicationError("Application evidence status is invalid")
        if self.contract_version != RECIPE_APPLICATION_CONTRACT_VERSION:
            raise RecipeApplicationError("Application evidence contract is unsupported")
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise RecipeApplicationError("Application evidence time is invalid")
        assert_no_numeric_odoo_ids(self.to_dict(include_hash=False))

    @property
    def content_hash(self) -> str:
        return content_hash(self.to_dict(include_hash=False))

    def to_dict(self, *, include_hash: bool = True) -> dict[str, object]:
        payload = portable(asdict(self))
        if include_hash:
            payload["content_hash"] = self.content_hash
        return payload

    def to_json(self) -> str:
        return canonical_json(self.to_dict())
