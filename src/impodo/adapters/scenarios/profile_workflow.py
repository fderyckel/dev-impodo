"""Adapt a profile scenario to the existing preparation and preflight engines.

Migration stages: source preparation through first comparison. Layer: adapter.

This adapter never writes to Odoo. It keeps prepared business values in
process memory and returns only allowlisted counts and hashes to the scenario
orchestrator.

See ``docs/plans/end-to-end-trial-and-scenario-qualification.md`` and
``tests/integration/scenarios/test_profile_workflow.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Callable
from uuid import uuid4

from impodo.adapters.odoo.connectors import Json2Config, Transport
from impodo.adapters.odoo.readback import Json2ReadbackReader
from impodo.adapters.odoo.writer import Json2WriteExecutor
from impodo.adapters.artifacts.profile_loader import ProfileLoadError, load_profile
from impodo.adapters.scenarios.execution_evidence import (
    ScenarioExecutionJournal,
    ScenarioReconciliationResults,
    write_scenario_execution_snapshot,
)
from impodo.adapters.scenarios.loader import LoadedScenario
from impodo.application.data_version.source_files import prepare_sources
from impodo.application.scenarios import (
    ScenarioComparisonEvidence,
    ScenarioExecutionEvidence,
    ScenarioPreparationEvidence,
    ScenarioProjectionEvidence,
    ScenarioReconciliationEvidence,
    ScenarioWorkflowFailure,
)
from impodo.application.workspace.execution.reconciliation import ReconciliationService
from impodo.application.workspace.execution.service import (
    ExecutionService,
    execution_api_scope,
)
from impodo.domain.compiler import compile_profile_document
from impodo.domain.execution.models import ExecutionRun
from impodo.domain.execution.planner import plan_metadata_requests, plan_record_requests
from impodo.domain.execution_snapshot import build_execution_snapshot
from impodo.domain.odoo.contracts import (
    ConnectorError,
    OdooReadConnector,
    bind_snapshot_hashes,
)
from impodo.domain.preflight.frozen_input import FrozenPreflightInput
from impodo.domain.preparation.preflight import PreflightEngine
from impodo.domain.preparation.source import PreparedBundle
from impodo.domain.scenarios import (
    FileScenarioSource,
    ScenarioDefinition,
    ScenarioFailureStage,
    ScenarioReasonCode,
)
from impodo.domain.shared.access import CapabilityAuthorizationPolicy, LOCAL_ACTOR
from impodo.domain.serialization import content_hash
from impodo.domain.workspace.errors import WorkspaceError
from impodo.domain.workspace.workbench import OdooConnectionMode, SourceMode


@dataclass(frozen=True, slots=True)
class _PreparedProfileState:
    plan: object
    prepared: PreparedBundle


class ProfileScenarioWorkflow:
    """Run a contained profile fixture against one bounded read connector."""

    def __init__(
        self,
        loaded: LoadedScenario,
        *,
        connector: OdooReadConnector | None,
        connector_factory: Callable[[], OdooReadConnector] | None = None,
        write_config: Json2Config | None = None,
        evidence_directory: str | Path | None = None,
        write_transport: Transport | None = None,
        readback_transport: Transport | None = None,
    ) -> None:
        self._loaded = loaded
        self._connector = connector
        self._connector_factory = connector_factory
        self._write_config = write_config
        self._write_transport = write_transport
        self._readback_transport = readback_transport
        self._evidence_directory = (
            Path(evidence_directory).resolve()
            if evidence_directory is not None
            else None
        )
        self._last_preflight = None
        self._last_records = None
        self._workspace_id = str(uuid4())
        self._snapshot = None

    def prepare(self, definition: ScenarioDefinition) -> ScenarioPreparationEvidence:
        if (
            definition != self._loaded.definition
            or not isinstance(definition.source, FileScenarioSource)
            or self._loaded.fixture_directory is None
            or self._loaded.profile_path is None
        ):
            raise ScenarioWorkflowFailure(
                ScenarioFailureStage.PREPARATION,
                ScenarioReasonCode.SOURCE_INPUT_INVALID,
            )
        try:
            profile = load_profile(self._loaded.profile_path)
            plan = compile_profile_document(profile)
            prepared = prepare_sources(plan, self._loaded.fixture_directory)
        except (ProfileLoadError, OSError, ValueError) as exc:
            raise ScenarioWorkflowFailure(
                ScenarioFailureStage.PREPARATION,
                ScenarioReasonCode.SOURCE_INPUT_INVALID,
            ) from exc
        return ScenarioPreparationEvidence(
            prepared_rows=len(prepared.records),
            source_issues=len(prepared.issues),
            state=_PreparedProfileState(plan=plan, prepared=prepared),
        )

    def compare(
        self,
        definition: ScenarioDefinition,
        preparation: ScenarioPreparationEvidence,
    ) -> ScenarioComparisonEvidence:
        del definition
        connector = (
            self._connector_factory()
            if self._connector_factory is not None
            else self._connector
        )
        if connector is None or not isinstance(
            preparation.state, _PreparedProfileState
        ):
            raise ScenarioWorkflowFailure(
                ScenarioFailureStage.FIRST_COMPARISON,
                ScenarioReasonCode.COMPARISON_FAILED,
            )
        state = preparation.state
        try:
            metadata, records = bind_snapshot_hashes(
                connector.get_model_metadata(
                    plan_metadata_requests(state.plan)
                ),
                connector.get_records(
                    plan_record_requests(state.plan, state.prepared.records)
                ),
            )
            preflight = PreflightEngine().run(
                state.plan,
                state.prepared,
                metadata,
                records,
            )
        except (ConnectorError, OSError, ValueError) as exc:
            raise ScenarioWorkflowFailure(
                ScenarioFailureStage.INFRASTRUCTURE,
                ScenarioReasonCode.TARGET_UNAVAILABLE,
            ) from exc
        self._last_preflight = preflight
        self._last_records = records
        return ScenarioComparisonEvidence(
            counts=preflight.counts,
            target_hash=preflight.fingerprint.target_hash,
            preflight_hash=preflight.semantic_hash,
            odoo_version=preflight.fingerprint.odoo_version,
            module_versions_hash=content_hash(
                dict(sorted(preflight.fingerprint.module_versions.items()))
            ),
        )

    def execute(
        self,
        definition: ScenarioDefinition,
        preparation: ScenarioPreparationEvidence,
        comparison: ScenarioComparisonEvidence,
    ) -> ScenarioExecutionEvidence:
        state = self._prepared_state(preparation)
        approved_preflight = self._last_preflight
        fresh_comparison = self.compare(definition, preparation)
        if (
            approved_preflight is None
            or _stable_preflight_hash(self._last_preflight)
            != _stable_preflight_hash(approved_preflight)
            or fresh_comparison.target_hash != comparison.target_hash
        ):
            raise ScenarioWorkflowFailure(
                ScenarioFailureStage.TARGET_POLICY,
                ScenarioReasonCode.TARGET_POLICY_MISMATCH,
            )
        if (
            self._write_config is None
            or self._evidence_directory is None
            or self._last_preflight is None
            or not self._write_config.database.startswith("impodo_scenario_")
            or self._write_config.connection_mode != "LOCAL"
        ):
            raise ScenarioWorkflowFailure(
                ScenarioFailureStage.TARGET_POLICY,
                ScenarioReasonCode.TARGET_POLICY_MISMATCH,
            )
        journal = ScenarioExecutionJournal(self._evidence_directory)
        if journal.path.exists():
            raise ScenarioWorkflowFailure(
                ScenarioFailureStage.EXECUTION,
                ScenarioReasonCode.UNSAFE_WRITE_OUTCOME,
            )
        frozen = _frozen(self._workspace_id, state.plan, state.prepared)
        try:
            snapshot = build_execution_snapshot(
                preflight_run_id=str(uuid4()),
                frozen=frozen,
                result=self._last_preflight,
            )
            write_scenario_execution_snapshot(snapshot, self._evidence_directory)
            preflight = SimpleNamespace(
                current_execution_snapshot=lambda _workspace_id: snapshot,
                execution_snapshot=lambda _workspace_id, _run_id: snapshot,
            )
            workspace = SimpleNamespace(
                workspace_id=self._workspace_id,
                source_mode=SourceMode.FILE,
                odoo_connection_mode=OdooConnectionMode.LOCAL,
            )
            execution = ExecutionService(
                SimpleNamespace(get=lambda _workspace_id: workspace),
                preflight,
                journal,
                CapabilityAuthorizationPolicy(),
            )
            scope = execution_api_scope(snapshot)
            executor = (
                Json2WriteExecutor(self._write_config, scope)
                if self._write_transport is None
                else Json2WriteExecutor(
                    self._write_config,
                    scope,
                    transport=self._write_transport,
                )
            )
            run = execution.execute(
                self._workspace_id,
                expected_snapshot_hash=snapshot.semantic_hash,
                executor=executor,
                actor=LOCAL_ACTOR,
                batch_rows=definition.execution.create_batch_rows,
            )
        except (OSError, ValueError, WorkspaceError) as exc:
            raise ScenarioWorkflowFailure(
                ScenarioFailureStage.EXECUTION,
                ScenarioReasonCode.UNSAFE_WRITE_OUTCOME,
            ) from exc
        self._snapshot = snapshot
        return ScenarioExecutionEvidence(
            committed=run.committed_count,
            failed=run.failed_count,
            partially_applied=run.partially_applied_count,
            outcome_unknown=run.unknown_count,
            snapshot_hash=snapshot.semantic_hash,
            state=run,
        )

    def reconcile(
        self,
        definition: ScenarioDefinition,
        preparation: ScenarioPreparationEvidence,
        execution: ScenarioExecutionEvidence,
    ) -> ScenarioReconciliationEvidence:
        del definition
        self._prepared_state(preparation)
        if (
            self._write_config is None
            or self._evidence_directory is None
            or self._snapshot is None
            or not isinstance(execution.state, ExecutionRun)
        ):
            raise ScenarioWorkflowFailure(
                ScenarioFailureStage.RECONCILIATION,
                ScenarioReasonCode.UNSAFE_WRITE_OUTCOME,
            )
        snapshot = self._snapshot
        preflight = SimpleNamespace(
            current_execution_snapshot=lambda _workspace_id: snapshot,
            execution_snapshot=lambda _workspace_id, _run_id: snapshot,
        )
        scope = execution_api_scope(snapshot)
        try:
            reader = (
                Json2ReadbackReader(self._write_config, scope)
                if self._readback_transport is None
                else Json2ReadbackReader(
                    self._write_config,
                    scope,
                    transport=self._readback_transport,
                )
            )
            report = ReconciliationService(
                preflight,
                ScenarioExecutionJournal(self._evidence_directory),
                ScenarioReconciliationResults(self._evidence_directory),
                CapabilityAuthorizationPolicy(),
            ).reconcile(
                self._workspace_id,
                expected_execution_run_id=execution.state.run_id,
                reader=reader,
                actor=LOCAL_ACTOR,
            )
        except (OSError, ValueError, WorkspaceError) as exc:
            raise ScenarioWorkflowFailure(
                ScenarioFailureStage.RECONCILIATION,
                ScenarioReasonCode.UNSAFE_WRITE_OUTCOME,
            ) from exc
        return ScenarioReconciliationEvidence(
            verified=report.verified_count,
            fallout=report.fallout_count,
            outcome_unknown=report.unknown_count,
            result_hash=report.semantic_hash,
            state=report,
        )

    def verify_projection(
        self,
        definition: ScenarioDefinition,
        preparation: ScenarioPreparationEvidence,
        reconciliation: ScenarioReconciliationEvidence,
    ) -> ScenarioProjectionEvidence:
        del definition, reconciliation
        state = self._prepared_state(preparation)
        projection = self._loaded.target_projection
        connector = (
            self._connector_factory()
            if self._connector_factory is not None
            else self._connector
        )
        if connector is None or projection is None:
            raise ScenarioWorkflowFailure(
                ScenarioFailureStage.RECONCILIATION,
                ScenarioReasonCode.TARGET_PROJECTION_MISMATCH,
            )
        try:
            records = connector.get_records(
                plan_record_requests(state.plan, state.prepared.records)
            )
        except (ConnectorError, OSError, ValueError) as exc:
            raise ScenarioWorkflowFailure(
                ScenarioFailureStage.INFRASTRUCTURE,
                ScenarioReasonCode.TARGET_UNAVAILABLE,
            ) from exc
        verified = 0
        differences = 0
        for expected in projection.records:
            matches = tuple(
                record
                for record in records.records.get(expected.model, ())
                if all(
                    record.values.get(field) == value
                    for field, value in expected.identity.items()
                )
            )
            if len(matches) != 1:
                differences += 1
                continue
            actual = matches[0]
            if all(
                actual.values.get(field) == value
                for field, value in expected.values.items()
            ):
                verified += 1
            else:
                differences += 1
        return ScenarioProjectionEvidence(
            expected_records=len(projection.records),
            verified_records=verified,
            difference_count=differences,
        )

    @staticmethod
    def _prepared_state(
        preparation: ScenarioPreparationEvidence,
    ) -> _PreparedProfileState:
        if not isinstance(preparation.state, _PreparedProfileState):
            raise ScenarioWorkflowFailure(
                ScenarioFailureStage.PREPARATION,
                ScenarioReasonCode.SOURCE_INPUT_INVALID,
            )
        return preparation.state


def _frozen(
    workspace_id: str,
    plan: object,
    prepared: PreparedBundle,
) -> FrozenPreflightInput:
    binding_hash = plan.semantic_hash
    return FrozenPreflightInput(
        workspace_id=workspace_id,
        revision=SimpleNamespace(
            mapping_id=str(uuid4()),
            version=1,
            definition=SimpleNamespace(content_hash=binding_hash),
        ),
        staging=SimpleNamespace(run_id=str(uuid4()), content_hash=binding_hash),
        quality=SimpleNamespace(
            run_id=str(uuid4()),
            content_hash=binding_hash,
            effective_dataset_run_id=None,
            effective_dataset_hash=binding_hash,
        ),
        normalization=SimpleNamespace(
            run_id=str(uuid4()),
            content_hash=binding_hash,
            lifecycle_version=1,
            eligible_dataset_hash=binding_hash,
        ),
        plan=plan,
        prepared=prepared,
        dataset_labels={item.name: item.name for item in plan.datasets},
        source_field_labels={},
        eligible_row_ids=tuple(item.source_trace_id for item in prepared.records),
    )


def _stable_preflight_hash(preflight: object) -> str:
    payload = preflight.to_portable_dict(include_hash=False)
    target = dict(payload["target"])
    target.pop("snapshot_timestamp", None)
    payload["target"] = target
    payload["snapshot_hashes"] = {"metadata": None, "records": None}
    return content_hash(payload)
