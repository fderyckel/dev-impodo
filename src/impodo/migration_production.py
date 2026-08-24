"""Define fresh Production use of one selected qualified CutoverPlan.

Production reuses a plan's qualified meaning while keeping rollout data, target,
credentials, parameters, controls, comparison, approval, execution, and
reconciliation independent from Integrated Test evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Mapping

from .domain.serialization import content_hash
from .migration_foundation import (
    MigrationFoundationError,
    require_aware,
    require_hash,
    require_revision,
    require_uuid,
    required_text,
)
from .migration_runs import MigrationRun
from .migration_workspaces import MigrationWorkspace
from .data_versions import DataVersion


PRODUCTION_RUN_BINDING_CONTRACT_VERSION = 1


class ProductionRunError(MigrationFoundationError):
    """Reject Production setup or activation that is not independently safe."""


class ProductionRunBindingState(StrEnum):
    """Expose whether the latest-data run is still setup-only or activated."""

    SETUP = "SETUP"
    ACTIVE = "ACTIVE"


@dataclass(frozen=True, slots=True)
class ProductionRunBinding:
    """Pin one Production run to selection and fresh rollout evidence.

    The SETUP state grants no Odoo write authority. ACTIVE records the exact
    fresh read and write credential generations used to materialize the
    Production applications; the credentials themselves remain in the vault.
    """

    production_run_binding_id: str
    project_id: str
    migration_run_id: str
    data_version_id: str
    setup_workspace_id: str
    cutover_selection_id: str
    qualification_id: str
    cutover_plan_id: str
    cutover_plan_revision: int
    plan_content_hash: str
    test_target_binding_hash: str
    state: ProductionRunBindingState
    target_binding_id: str | None
    read_credential_generation: str | None
    write_credential_generation: str | None
    write_principal_hash: str | None
    write_permission_hash: str | None
    write_context_hash: str | None
    parameter_values_hash: str | None
    control_values_hash: str | None
    activation_evidence_hash: str | None
    created_at: datetime
    activated_at: datetime | None = None
    contract_version: int = PRODUCTION_RUN_BINDING_CONTRACT_VERSION

    def __post_init__(self) -> None:
        for value, label in (
            (self.production_run_binding_id, "production_run_binding_id"),
            (self.project_id, "project_id"),
            (self.migration_run_id, "migration_run_id"),
            (self.data_version_id, "data_version_id"),
            (self.setup_workspace_id, "setup_workspace_id"),
            (self.cutover_selection_id, "cutover_selection_id"),
            (self.qualification_id, "qualification_id"),
            (self.cutover_plan_id, "cutover_plan_id"),
        ):
            require_uuid(value, label)
        require_revision(self.cutover_plan_revision, "cutover_plan_revision")
        require_hash(self.plan_content_hash, "plan_content_hash")
        require_hash(self.test_target_binding_hash, "test_target_binding_hash")
        object.__setattr__(self, "state", ProductionRunBindingState(self.state))
        if self.contract_version != PRODUCTION_RUN_BINDING_CONTRACT_VERSION:
            raise ProductionRunError("Production run binding contract is unsupported")
        require_aware(self.created_at, "created_at")
        optional_uuids = ((self.target_binding_id, "target_binding_id"),)
        for value, label in optional_uuids:
            if value is not None:
                require_uuid(value, label)
        optional_hashes = (
            (self.write_principal_hash, "write_principal_hash"),
            (self.write_permission_hash, "write_permission_hash"),
            (self.write_context_hash, "write_context_hash"),
            (self.parameter_values_hash, "parameter_values_hash"),
            (self.control_values_hash, "control_values_hash"),
            (self.activation_evidence_hash, "activation_evidence_hash"),
        )
        for value, label in optional_hashes:
            if value is not None:
                require_hash(value, label)
        for value, label in (
            (self.read_credential_generation, "read_credential_generation"),
            (self.write_credential_generation, "write_credential_generation"),
        ):
            if value is not None:
                required_text(value, label, maximum=300)
        activation_values = (
            self.target_binding_id,
            self.read_credential_generation,
            self.write_credential_generation,
            self.write_principal_hash,
            self.write_permission_hash,
            self.write_context_hash,
            self.parameter_values_hash,
            self.control_values_hash,
            self.activation_evidence_hash,
            self.activated_at,
        )
        if self.state is ProductionRunBindingState.SETUP:
            if any(value is not None for value in activation_values):
                raise ProductionRunError(
                    "A Production setup binding cannot contain activation evidence"
                )
        elif any(value is None for value in activation_values):
            raise ProductionRunError(
                "An active Production binding requires complete fresh evidence"
            )
        if self.activated_at is not None:
            require_aware(self.activated_at, "activated_at")

    @property
    def content_hash(self) -> str:
        return content_hash(self.to_dict(include_hash=False))

    def to_dict(self, *, include_hash: bool = True) -> dict[str, object]:
        result: dict[str, object] = {
            "activation_evidence_hash": self.activation_evidence_hash,
            "activated_at": self.activated_at.isoformat() if self.activated_at else None,
            "contract_version": self.contract_version,
            "control_values_hash": self.control_values_hash,
            "created_at": self.created_at.isoformat(),
            "cutover_plan_id": self.cutover_plan_id,
            "cutover_plan_revision": self.cutover_plan_revision,
            "cutover_selection_id": self.cutover_selection_id,
            "data_version_id": self.data_version_id,
            "migration_run_id": self.migration_run_id,
            "parameter_values_hash": self.parameter_values_hash,
            "plan_content_hash": self.plan_content_hash,
            "production_run_binding_id": self.production_run_binding_id,
            "project_id": self.project_id,
            "qualification_id": self.qualification_id,
            "read_credential_generation": self.read_credential_generation,
            "setup_workspace_id": self.setup_workspace_id,
            "state": self.state.value,
            "target_binding_id": self.target_binding_id,
            "test_target_binding_hash": self.test_target_binding_hash,
            "write_context_hash": self.write_context_hash,
            "write_credential_generation": self.write_credential_generation,
            "write_permission_hash": self.write_permission_hash,
            "write_principal_hash": self.write_principal_hash,
        }
        if include_hash:
            result["content_hash"] = self.content_hash
        return result


@dataclass(frozen=True, slots=True)
class ProductionRunSetupBundle:
    """Return the fresh rollout identities created before data acceptance."""

    data_version: DataVersion
    run: MigrationRun
    setup_workspace: MigrationWorkspace
    binding: ProductionRunBinding

    def __post_init__(self) -> None:
        if (
            self.data_version.project_id != self.run.project_id
            or self.run.data_version_id != self.data_version.data_version_id
            or self.setup_workspace.project_id != self.run.project_id
            or self.setup_workspace.data_version_id != self.data_version.data_version_id
            or self.setup_workspace.migration_run_id != self.run.migration_run_id
            or self.binding.project_id != self.run.project_id
            or self.binding.migration_run_id != self.run.migration_run_id
            or self.binding.data_version_id != self.data_version.data_version_id
            or self.binding.setup_workspace_id != self.setup_workspace.workspace_id
        ):
            raise ProductionRunError("Production setup identities do not match")


def activation_evidence_hash(
    *,
    binding: ProductionRunBinding,
    target_binding_hash: str,
    requirement_plan_hash: str,
    write_identity: Mapping[str, object],
    parameter_values_hash: str,
    control_values_hash: str,
) -> str:
    """Identify the exact fresh authority checked before applications exist."""

    for value, label in (
        (target_binding_hash, "target_binding_hash"),
        (requirement_plan_hash, "requirement_plan_hash"),
        (parameter_values_hash, "parameter_values_hash"),
        (control_values_hash, "control_values_hash"),
    ):
        require_hash(value, label)
    return content_hash(
        {
            "contract_version": PRODUCTION_RUN_BINDING_CONTRACT_VERSION,
            "control_values_hash": control_values_hash,
            "cutover_plan_id": binding.cutover_plan_id,
            "cutover_plan_revision": binding.cutover_plan_revision,
            "cutover_selection_id": binding.cutover_selection_id,
            "data_version_id": binding.data_version_id,
            "migration_run_id": binding.migration_run_id,
            "parameter_values_hash": parameter_values_hash,
            "plan_content_hash": binding.plan_content_hash,
            "qualification_id": binding.qualification_id,
            "requirement_plan_hash": requirement_plan_hash,
            "target_binding_hash": target_binding_hash,
            "write_identity": dict(write_identity),
        }
    )
