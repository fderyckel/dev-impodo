"""Define immutable inputs for one governed end-to-end scenario.

Migration stages: cross-cutting source through reconciliation. Layer: domain.

The contracts contain no credentials, source values, Odoo identifiers, or
executable callbacks. Adapters validate files and resolve secret handles only
after this complete definition passes. The first executable slice supports a
profile-driven file source; Recipe and bounded Odoo-source definitions are
retained in the same contract for later product-owned orchestration.

See ``docs/plans/end-to-end-trial-and-scenario-qualification.md`` and
``tests/domain/scenarios/test_models.py``.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import PurePosixPath, PureWindowsPath
import re
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from impodo.domain.execution.models import MAX_CREATE_BATCH_ROWS
from impodo.domain.serialization import content_hash


SCENARIO_CONTRACT_VERSION = 1
MAX_CAPTURE_MODELS = 10
MAX_CAPTURE_FIELDS_PER_MODEL = 50
MAX_CAPTURE_ROWS_PER_MODEL = 10_000
MAX_CAPTURE_TOTAL_ROWS = 100_000
MAX_CAPTURE_DEPTH = 8

_TECHNICAL_NAME = re.compile(r"[a-z_][a-z0-9_.]*")
_REFERENCE = re.compile(r"[a-z][a-z0-9_.:/-]*")
_SCENARIO_ID = re.compile(r"[a-z][a-z0-9_-]*")
_SHA256 = re.compile(r"sha256:[0-9a-f]{64}")


class ScenarioSourceMode(StrEnum):
    """Identify where one scenario obtains its source evidence."""

    FILE = "FILE"
    ODOO = "ODOO"


class ScenarioDestinationMode(StrEnum):
    """Identify the disposable Odoo destination class."""

    LOCAL_ODOO = "LOCAL_ODOO"
    REMOTE_ODOO = "REMOTE_ODOO"


class ScenarioPurpose(StrEnum):
    """Name why the reviewed scenario is allowed to run."""

    MANUAL_TRIAL = "MANUAL_TRIAL"
    PULL_REQUEST = "PULL_REQUEST"
    DAILY = "DAILY"
    NIGHTLY = "NIGHTLY"
    RELEASE_QUALIFICATION = "RELEASE_QUALIFICATION"


class ScenarioWritePolicy(StrEnum):
    """State whether the scenario may cross the Odoo write boundary."""

    READ_ONLY = "READ_ONLY"
    DISPOSABLE_SCENARIO_ONLY = "DISPOSABLE_SCENARIO_ONLY"


class ScenarioStopAfter(StrEnum):
    """Name the last product checkpoint that a scenario must prove."""

    PREPARATION = "PREPARATION"
    FIRST_COMPARISON = "FIRST_COMPARISON"
    RECONCILIATION = "RECONCILIATION"
    REPEAT_COMPARISON = "REPEAT_COMPARISON"


class ScenarioExpectedOutcome(StrEnum):
    """Distinguish a successful round trip from an intentional safe block."""

    PASS = "PASS"
    EXPECTED_BLOCK = "EXPECTED_BLOCK"


class StrictScenarioModel(BaseModel):
    """Reject unknown scenario input and keep accepted values immutable."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


def _contained_relative_path(value: str, field_name: str) -> str:
    clean = value.strip().replace("\\", "/")
    posix = PurePosixPath(clean)
    windows = PureWindowsPath(value.strip())
    if (
        not clean
        or posix.is_absolute()
        or windows.is_absolute()
        or ".." in posix.parts
        or ".." in windows.parts
        or clean.endswith("/")
    ):
        raise ValueError(f"{field_name} must be a contained relative path")
    return clean


def _unique(values: tuple[str, ...], field_name: str) -> tuple[str, ...]:
    normalized = tuple(item.strip() for item in values)
    if any(not item for item in normalized) or len(set(normalized)) != len(normalized):
        raise ValueError(f"{field_name} must contain unique non-blank values")
    return normalized


class FileScenarioSource(StrictScenarioModel):
    """Refer to one contained, immutable set of CSV or XLSX fixtures."""

    mode: Literal[ScenarioSourceMode.FILE] = ScenarioSourceMode.FILE
    fixture_set: str = Field(min_length=1, max_length=500)
    fixture_hash: str

    @model_validator(mode="after")
    def validate_fixture_set(self) -> "FileScenarioSource":
        object.__setattr__(
            self,
            "fixture_set",
            _contained_relative_path(self.fixture_set, "source.fixture_set"),
        )
        if _SHA256.fullmatch(self.fixture_hash) is None:
            raise ValueError("source.fixture_hash must be a SHA-256 content hash")
        return self


class OdooCaptureRelationship(StrictScenarioModel):
    """Allow one explicitly reviewed relationship edge during capture."""

    field: str = Field(min_length=1, max_length=200)
    target_model: str = Field(min_length=3, max_length=200)
    kind: Literal["many2one", "one2many", "many2many"]
    identity_fields: tuple[str, ...] = Field(min_length=1, max_length=8)
    required_for_migration: bool = False

    @model_validator(mode="after")
    def validate_relationship(self) -> "OdooCaptureRelationship":
        if _TECHNICAL_NAME.fullmatch(self.field) is None:
            raise ValueError("capture relationship field is invalid")
        if _TECHNICAL_NAME.fullmatch(self.target_model) is None:
            raise ValueError("capture relationship target_model is invalid")
        fields = _unique(self.identity_fields, "capture relationship identity_fields")
        if any(_TECHNICAL_NAME.fullmatch(item) is None for item in fields):
            raise ValueError("capture relationship identity field is invalid")
        object.__setattr__(self, "identity_fields", fields)
        return self


class OdooCaptureModel(StrictScenarioModel):
    """Bound the fields, rows, and relationship edges for one source model."""

    model: str = Field(min_length=3, max_length=200)
    fields: tuple[str, ...] = Field(
        min_length=1,
        max_length=MAX_CAPTURE_FIELDS_PER_MODEL,
    )
    maximum_rows: int = Field(ge=1, le=MAX_CAPTURE_ROWS_PER_MODEL)
    active_records: Literal["ACTIVE_ONLY", "ACTIVE_AND_ARCHIVED"] = "ACTIVE_ONLY"
    relationships: tuple[OdooCaptureRelationship, ...] = ()

    @model_validator(mode="after")
    def validate_model(self) -> "OdooCaptureModel":
        if _TECHNICAL_NAME.fullmatch(self.model) is None:
            raise ValueError("capture model is invalid")
        fields = _unique(self.fields, "capture fields")
        if any(_TECHNICAL_NAME.fullmatch(item) is None for item in fields):
            raise ValueError("capture field is invalid")
        relationship_fields = tuple(item.field for item in self.relationships)
        if len(set(relationship_fields)) != len(relationship_fields):
            raise ValueError("capture relationship fields must be unique per model")
        object.__setattr__(self, "fields", fields)
        return self


class OdooScenarioSource(StrictScenarioModel):
    """Describe a closed, reviewable Odoo-source relationship capture."""

    mode: Literal[ScenarioSourceMode.ODOO] = ScenarioSourceMode.ODOO
    source_profile: str = Field(min_length=1, max_length=200)
    root_models: tuple[str, ...] = Field(min_length=1, max_length=MAX_CAPTURE_MODELS)
    models: tuple[OdooCaptureModel, ...] = Field(
        min_length=1,
        max_length=MAX_CAPTURE_MODELS,
    )
    maximum_total_records: int = Field(ge=1, le=MAX_CAPTURE_TOTAL_ROWS)
    maximum_depth: int = Field(ge=0, le=MAX_CAPTURE_DEPTH)
    allowed_company_keys: tuple[str, ...] = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def validate_capture(self) -> "OdooScenarioSource":
        if _REFERENCE.fullmatch(self.source_profile) is None:
            raise ValueError("source.source_profile is invalid")
        roots = _unique(self.root_models, "source.root_models")
        if any(_TECHNICAL_NAME.fullmatch(item) is None for item in roots):
            raise ValueError("source root model is invalid")
        model_names = tuple(item.model for item in self.models)
        if len(set(model_names)) != len(model_names):
            raise ValueError("source capture models must be unique")
        if not set(roots).issubset(model_names):
            raise ValueError("every source root model must have a capture plan")
        if self.maximum_total_records > sum(item.maximum_rows for item in self.models):
            raise ValueError(
                "source.maximum_total_records exceeds the model capture bounds"
            )
        allowed = _unique(
            self.allowed_company_keys,
            "source.allowed_company_keys",
        )
        object.__setattr__(self, "root_models", roots)
        object.__setattr__(self, "allowed_company_keys", allowed)
        return self


ScenarioSource = Annotated[
    FileScenarioSource | OdooScenarioSource,
    Field(discriminator="mode"),
]


class RecipeRevisionReference(StrictScenarioModel):
    """Pin one exact reusable Recipe revision."""

    recipe_id: UUID
    revision: int = Field(ge=1)


class ScenarioRules(StrictScenarioModel):
    """Choose either an expert profile or exact Project-owned Recipes."""

    profile: str | None = Field(default=None, min_length=1, max_length=500)
    profile_hash: str | None = None
    recipe_revisions: tuple[RecipeRevisionReference, ...] = ()

    @model_validator(mode="after")
    def validate_rule_source(self) -> "ScenarioRules":
        if (self.profile is None) == (not self.recipe_revisions):
            raise ValueError(
                "rules must select either one profile or Recipe revisions"
            )
        if self.profile is not None:
            clean = _contained_relative_path(self.profile, "rules.profile")
            if PurePosixPath(clean).suffix.casefold() not in {".yml", ".yaml"}:
                raise ValueError("rules.profile must be a YAML file")
            object.__setattr__(self, "profile", clean)
            if self.profile_hash is None or _SHA256.fullmatch(self.profile_hash) is None:
                raise ValueError("rules.profile_hash must pin the profile bytes")
        elif self.profile_hash is not None:
            raise ValueError("rules.profile_hash requires rules.profile")
        recipe_keys = tuple(
            (item.recipe_id, item.revision) for item in self.recipe_revisions
        )
        if len(set(recipe_keys)) != len(recipe_keys):
            raise ValueError("rules.recipe_revisions must be unique")
        return self


class ScenarioDestination(StrictScenarioModel):
    """Refer to one separately configured disposable Odoo target."""

    mode: ScenarioDestinationMode
    target_profile: str = Field(min_length=1, max_length=200)
    expected_seed: str = Field(min_length=1, max_length=200)
    relevant_modules: tuple[str, ...] = Field(default=("base",), max_length=50)

    @model_validator(mode="after")
    def validate_destination(self) -> "ScenarioDestination":
        if _REFERENCE.fullmatch(self.target_profile) is None:
            raise ValueError("destination.target_profile is invalid")
        if _REFERENCE.fullmatch(self.expected_seed) is None:
            raise ValueError("destination.expected_seed is invalid")
        modules = _unique(self.relevant_modules, "destination.relevant_modules")
        if any(_TECHNICAL_NAME.fullmatch(item) is None for item in modules):
            raise ValueError("destination module name is invalid")
        object.__setattr__(self, "relevant_modules", modules)
        return self


class ScenarioExecution(StrictScenarioModel):
    """Declare the final checkpoint and maximum disposable write authority."""

    stop_after: ScenarioStopAfter
    write_policy: ScenarioWritePolicy
    create_batch_rows: int = Field(default=10, ge=1, le=MAX_CREATE_BATCH_ROWS)

    @model_validator(mode="after")
    def validate_write_boundary(self) -> "ScenarioExecution":
        write_stages = {
            ScenarioStopAfter.RECONCILIATION,
            ScenarioStopAfter.REPEAT_COMPARISON,
        }
        if (
            self.stop_after in write_stages
            and self.write_policy is not ScenarioWritePolicy.DISPOSABLE_SCENARIO_ONLY
        ):
            raise ValueError(
                "execution past comparison requires DISPOSABLE_SCENARIO_ONLY"
            )
        if (
            self.stop_after not in write_stages
            and self.write_policy is not ScenarioWritePolicy.READ_ONLY
        ):
            raise ValueError(
                "a read-only stopping point cannot request scenario write authority"
            )
        return self


class ComparisonExpectation(StrictScenarioModel):
    """State independently reviewed comparison totals."""

    create: int = Field(ge=0)
    update: int = Field(ge=0)
    unchanged: int = Field(ge=0)
    blocked: int = Field(ge=0)
    ambiguous: int = Field(ge=0)

    @property
    def total(self) -> int:
        return self.create + self.update + self.unchanged + self.blocked + self.ambiguous

    def as_classification_counts(self) -> dict[str, int]:
        """Return the keys used by the existing preflight result."""

        return {
            "CREATE": self.create,
            "UPDATE": self.update,
            "UNCHANGED": self.unchanged,
            "BLOCKED": self.blocked,
            "AMBIGUOUS": self.ambiguous,
        }


class ReconciliationExpectation(StrictScenarioModel):
    """State the required post-write read-back totals."""

    verified: int = Field(ge=0)
    fallout: int = Field(ge=0)
    outcome_unknown: int = Field(ge=0)

    @property
    def total(self) -> int:
        return self.verified + self.fallout + self.outcome_unknown


class ScenarioExpectations(StrictScenarioModel):
    """Keep scenario oracles independent from the implementation output."""

    expected_outcome: ScenarioExpectedOutcome = ScenarioExpectedOutcome.PASS
    target_projection: str | None = Field(default=None, min_length=1, max_length=500)
    target_projection_hash: str | None = None
    prepared_rows: int = Field(ge=0)
    source_issues: int = Field(default=0, ge=0)
    first_comparison: ComparisonExpectation | None = None
    reconciliation: ReconciliationExpectation | None = None
    repeat_comparison: ComparisonExpectation | None = None

    @model_validator(mode="after")
    def validate_projection(self) -> "ScenarioExpectations":
        if self.target_projection is not None:
            clean = _contained_relative_path(
                self.target_projection,
                "expectations.target_projection",
            )
            if PurePosixPath(clean).suffix.casefold() != ".json":
                raise ValueError("expectations.target_projection must be JSON")
            object.__setattr__(self, "target_projection", clean)
            if (
                self.target_projection_hash is None
                or _SHA256.fullmatch(self.target_projection_hash) is None
            ):
                raise ValueError(
                    "expectations.target_projection_hash must pin the projection bytes"
                )
        elif self.target_projection_hash is not None:
            raise ValueError(
                "expectations.target_projection_hash requires target_projection"
            )
        if (
            self.first_comparison is not None
            and self.first_comparison.total != self.prepared_rows
        ):
            raise ValueError(
                "expectations.first_comparison must account for every prepared row"
            )
        if (
            self.repeat_comparison is not None
            and self.repeat_comparison.total != self.prepared_rows
        ):
            raise ValueError(
                "expectations.repeat_comparison must account for every prepared row"
            )
        return self


class ScenarioDefinition(StrictScenarioModel):
    """Bind one complete, safe, and deterministically hashable scenario."""

    contract_version: Literal[SCENARIO_CONTRACT_VERSION]
    scenario_id: str = Field(min_length=1, max_length=100)
    purpose: ScenarioPurpose
    source: ScenarioSource
    rules: ScenarioRules
    destination: ScenarioDestination
    execution: ScenarioExecution
    expectations: ScenarioExpectations

    @model_validator(mode="after")
    def validate_scenario(self) -> "ScenarioDefinition":
        if _SCENARIO_ID.fullmatch(self.scenario_id) is None:
            raise ValueError("scenario_id is invalid")
        if self.source.mode is ScenarioSourceMode.ODOO and self.rules.profile is None:
            raise ValueError(
                "the current Odoo-source scenario contract requires a reviewed profile"
            )

        comparison_stages = {
            ScenarioStopAfter.FIRST_COMPARISON,
            ScenarioStopAfter.RECONCILIATION,
            ScenarioStopAfter.REPEAT_COMPARISON,
        }
        if (
            self.execution.stop_after in comparison_stages
            and self.expectations.first_comparison is None
        ):
            raise ValueError("a comparison scenario requires first_comparison expectations")

        write_capable = (
            self.execution.write_policy
            is ScenarioWritePolicy.DISPOSABLE_SCENARIO_ONLY
        )
        if write_capable and (
            self.expectations.expected_outcome is not ScenarioExpectedOutcome.PASS
            or self.expectations.target_projection is None
            or self.expectations.reconciliation is None
        ):
            raise ValueError(
                "a write-capable scenario requires PASS, target projection, "
                "and reconciliation expectations"
            )
        if write_capable and self.expectations.reconciliation is not None and (
            self.expectations.reconciliation.total
            != self.expectations.prepared_rows
            or self.expectations.reconciliation.fallout
            or self.expectations.reconciliation.outcome_unknown
        ):
            raise ValueError(
                "a write-capable scenario requires complete verified reconciliation"
            )
        if (
            self.execution.stop_after is ScenarioStopAfter.REPEAT_COMPARISON
            and self.expectations.repeat_comparison is None
        ):
            raise ValueError(
                "a repeat-comparison scenario requires repeat_comparison expectations"
            )
        if (
            self.execution.stop_after is not ScenarioStopAfter.REPEAT_COMPARISON
            and self.expectations.repeat_comparison is not None
        ):
            raise ValueError(
                "repeat_comparison expectations require the matching stopping point"
            )
        if (
            self.expectations.expected_outcome is ScenarioExpectedOutcome.EXPECTED_BLOCK
            and (
                write_capable
                or self.expectations.first_comparison is None
                or not (
                    self.expectations.first_comparison.blocked
                    or self.expectations.first_comparison.ambiguous
                )
            )
        ):
            raise ValueError(
                "EXPECTED_BLOCK requires read-only blocking or ambiguous comparison rows"
            )
        return self

    @property
    def semantic_hash(self) -> str:
        """Identify the accepted scenario meaning without external file bytes."""

        return content_hash(self.model_dump(mode="json"))


ScenarioScalar = str | int | float | bool | None


class TargetProjectionRecord(StrictScenarioModel):
    """State independently reviewed scalar values for one business identity."""

    model: str = Field(min_length=1, max_length=200)
    identity: dict[str, ScenarioScalar] = Field(min_length=1, max_length=20)
    values: dict[str, ScenarioScalar] = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def validate_record(self) -> "TargetProjectionRecord":
        fields = {*self.identity, *self.values}
        if (
            _TECHNICAL_NAME.fullmatch(self.model) is None
            or any(_TECHNICAL_NAME.fullmatch(name) is None for name in fields)
            or "id" in fields
            or any(value is None for value in self.identity.values())
            or any(
                name.endswith(("_id", "_ids")) and type(value) is int
                for values in (self.identity, self.values)
                for name, value in values.items()
            )
        ):
            raise ValueError("target projection record is invalid")
        return self


class TargetProjection(StrictScenarioModel):
    """Hold a small reviewed oracle that is independent from prepared output."""

    contract_version: Literal[1]
    records: tuple[TargetProjectionRecord, ...] = Field(min_length=1, max_length=10_000)

    @model_validator(mode="after")
    def validate_unique_records(self) -> "TargetProjection":
        keys = [
            content_hash(
                {
                    "model": record.model,
                    "identity": dict(sorted(record.identity.items())),
                }
            )
            for record in self.records
        ]
        if len(set(keys)) != len(keys):
            raise ValueError("target projection identities must be unique")
        return self
