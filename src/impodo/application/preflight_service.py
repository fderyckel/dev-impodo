"""Read-only Odoo comparison over an already prepared application context."""

from __future__ import annotations

from typing import Callable
from uuid import uuid4

from ..access import Actor, AuthorizationPolicy, Capability
from ..artifacts import ArtifactStore
from ..connectors import MetadataRequest, MetadataSnapshot, RecordRequest, RecordSnapshot
from ..domain.preflight.reports import ReadinessReport, _readiness_report
from ..engine import PreflightEngine
from ..models import canonical_json_bytes, target_identity_hash
from ..planner import plan_metadata_requests, plan_record_requests
from ..quality import eligible_prepared_bundle
from ..staging import StagingRunSummary
from .errors import ReadinessError
from .preparation_service import PreparedReadinessContext
from .readiness_ports import PreflightRepository


MANIFEST_NAME = "impodo_preflight_manifest.json"

ReadinessReader = Callable[
    [tuple[MetadataRequest, ...], tuple[RecordRequest, ...]],
    tuple[MetadataSnapshot, RecordSnapshot],
]


class PreflightService:
    """Plan batched Odoo reads and publish target-dependent classifications."""

    def __init__(
        self,
        repository: PreflightRepository,
        artifacts: ArtifactStore,
        authorization: AuthorizationPolicy,
    ) -> None:
        self.repository = repository
        self.artifacts = artifacts
        self.authorization = authorization
        self.engine = PreflightEngine()

    def current_report(self, project_id: str) -> ReadinessReport | None:
        staging = self.repository.get_current_staging_summary(project_id)
        if staging is None:
            return None
        quality = self.repository.get_current_quality_summary(project_id)
        if quality is None or quality.staging_run_id != staging.run_id:
            return None
        normalization = self.repository.get_current_normalization_summary(project_id)
        if (
            normalization is None
            or not normalization.frozen
            or normalization.staging_run_id != staging.run_id
            or normalization.quality_run_id != quality.run_id
        ):
            return None
        revision = self.repository.get_mapping_revision(project_id)
        if revision is None:
            return None
        submission = self.repository.get_mapping_submission(
            project_id, revision.version
        )
        if submission is None:
            return None
        return self.repository.get_readiness_report(
            project_id,
            revision.mapping_id,
            revision.version,
            revision.definition.content_hash,
            staging.run_id,
            staging.content_hash,
            quality.run_id,
            quality.content_hash,
        )

    def current_staging(self, project_id: str) -> StagingRunSummary | None:
        return self.repository.get_current_staging_summary(project_id)

    def compare(
        self,
        context: PreparedReadinessContext,
        *,
        reader: ReadinessReader,
        actor: Actor,
    ) -> ReadinessReport:
        """Compare prepared rows without invoking preparation or source loading."""

        project_id = context.project.project_id
        self.authorization.require(
            actor,
            Capability.MAPPING_SUBMIT,
            project_id=project_id,
        )
        if not context.normalization.frozen:
            raise ReadinessError(
                "Approve the prepared data before comparing it with Odoo. "
                "Odoo was not contacted."
            )
        eligible = eligible_prepared_bundle(
            context.staged.canonical_run,
            context.staged.prepared,
            context.quality_run,
        )
        metadata_requests = plan_metadata_requests(context.staged.profile)
        record_requests = plan_record_requests(
            context.staged.profile,
            eligible.records,
        )
        metadata, records = reader(metadata_requests, record_requests)
        project = context.project
        expected_target = target_identity_hash(
            connection_mode=(
                project.odoo_connection_mode.value
                if project.odoo_connection_mode is not None
                else ""
            ),
            base_url=project.odoo_base_url,
            database=project.odoo_database,
        )
        if metadata.fingerprint.target_hash != expected_target:
            raise ReadinessError("Readiness data came from a different Odoo target")
        result = self.engine.run(
            context.staged.profile,
            eligible,
            metadata,
            records,
        )
        run_id = str(uuid4())
        report = _readiness_report(
            run_id,
            project,
            context.revision,
            result,
            context.staged.dataset_labels,
            context.staged.source_field_labels,
            actor,
            context.staging,
            context.quality,
        )
        self.artifacts.write_report(
            project_id,
            run_id,
            MANIFEST_NAME,
            canonical_json_bytes(result.to_portable_dict()) + b"\n",
        )
        self.repository.save_readiness_report(
            project_id,
            report,
            actor=actor,
        )
        return report
