"""Stage-H orchestration over approved, frozen source-side evidence.

``PreflightService.compare`` verifies and adapts current Stages D–G evidence,
builds bounded read requirements, invokes a caller-supplied read-only target
reader, runs the shared comparison engine, and publishes a portable report plus
protected snapshots. It never reloads source files and exposes no Odoo write.
"""

from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
from typing import Callable
from uuid import uuid4

from ..access import Actor, AuthorizationPolicy, Capability
from ..artifacts import ArtifactStore
from ..connectors import (
    MetadataRequest,
    MetadataSnapshot,
    RecordRequest,
    RecordSnapshot,
    bind_snapshot_hashes,
)
from ..domain.compiler.browser_mapping_compiler import (
    browser_mapping_labels,
    compile_browser_mapping,
)
from ..domain.errors import ReadinessError
from ..domain.preflight.frozen_input import (
    FrozenPreflightInput,
    build_frozen_preflight_input,
)
from ..domain.preflight.reports import (
    ReadinessReport,
    ReadinessRowPage,
    _readiness_report,
)
from ..engine import PreflightEngine
from ..models import canonical_json_bytes, target_identity_hash
from ..planner import plan_preflight_requirements
from ..staging import StagingRunSummary
from .readiness_ports import (
    PreflightMappingRepository,
    PreflightEffectiveRepository,
    PreflightNormalizationRepository,
    PreflightProjectRepository,
    PreflightQualityRepository,
    PreflightRepository,
    PreflightSourceRepository,
    PreflightStagingRepository,
)


MANIFEST_NAME = "impodo_preflight_manifest.json"

ReadinessReader = Callable[
    [tuple[MetadataRequest, ...], tuple[RecordRequest, ...]],
    tuple[MetadataSnapshot, RecordSnapshot],
]


class PreflightService:
    """Plan batched Odoo reads and publish target-dependent classifications.

    The service is the browser workflow's only point at which Odoo may be
    contacted. Every source-side prerequisite and every record domain is
    checked first; failures explicitly occur before calling ``reader``.
    """

    def __init__(
        self,
        staging: PreflightStagingRepository,
        quality: PreflightQualityRepository,
        normalization: PreflightNormalizationRepository,
        mappings: PreflightMappingRepository,
        projects: PreflightProjectRepository,
        sources: PreflightSourceRepository,
        preflight: PreflightRepository,
        artifacts: ArtifactStore,
        authorization: AuthorizationPolicy,
        effective: PreflightEffectiveRepository | None = None,
    ) -> None:
        self.staging = staging
        self.quality = quality
        self.normalization = normalization
        self.mappings = mappings
        self.projects = projects
        self.sources = sources
        self.preflight = preflight
        self.artifacts = artifacts
        self.authorization = authorization
        self.effective = effective
        self.engine = PreflightEngine()

    def current_report(self, project_id: str) -> ReadinessReport | None:
        """Return the report only if every current upstream/target binding matches.

        A report is treated as absent when staging, quality, normalization,
        mapping submission, lifecycle version, eligible hash, or configured
        target identity moved since publication.
        """

        staging = self.staging.get_current_staging_summary(project_id)
        if staging is None:
            return None
        quality = self.quality.get_current_quality_summary(project_id)
        if quality is None or quality.staging_run_id != staging.run_id:
            return None
        normalization = self.normalization.get_current_normalization_summary(
            project_id
        )
        if (
            normalization is None
            or not normalization.frozen
            or normalization.staging_run_id != staging.run_id
            or normalization.quality_run_id != quality.run_id
        ):
            return None
        revision = self.mappings.get_mapping_revision(project_id)
        if revision is None:
            return None
        submission = self.mappings.get_mapping_submission(
            project_id, revision.version
        )
        if (
            submission is None
            or submission.mapping_content_hash != revision.definition.content_hash
        ):
            return None
        report = self.preflight.get_readiness_report(
            project_id,
            revision.mapping_id,
            revision.version,
            revision.definition.content_hash,
            staging.run_id,
            staging.content_hash,
            quality.run_id,
            quality.content_hash,
            normalization.run_id,
            normalization.content_hash,
            normalization.lifecycle_version,
            normalization.eligible_dataset_hash,
        )
        if report is None:
            return None
        project = self.projects.get(project_id)
        expected_target = target_identity_hash(
            connection_mode=(
                project.odoo_connection_mode.value
                if project.odoo_connection_mode is not None
                else ""
            ),
            base_url=project.odoo_base_url,
            database=project.odoo_database,
        )
        return report if report.target_hash == expected_target else None

    def current_staging(self, project_id: str) -> StagingRunSummary | None:
        """Return the current staging summary used by package eligibility UI."""

        return self.staging.get_current_staging_summary(project_id)

    def readiness_rows(
        self,
        project_id: str,
        run_id: str,
        *,
        status: str = "",
        dataset: str = "",
        page: int = 1,
        page_size: int = 100,
    ) -> ReadinessRowPage:
        """Load one filtered, bounded page from a published readiness run."""

        return self.preflight.get_readiness_rows(
            project_id,
            run_id,
            status=status,
            dataset=dataset,
            page=page,
            page_size=page_size,
        )

    def compare(
        self,
        project_id: str,
        *,
        reader: ReadinessReader,
        actor: Actor,
    ) -> ReadinessReport:
        """Compare approved rows without invoking preparation or source loading.

        The fixed sequence is: authorize; verify frozen input; create narrowed
        requests; read and hash one target snapshot; verify its projection and
        target identity; run deterministic comparison; write the protected
        manifest; and atomically publish report rows plus snapshots. A failed
        database publication removes the otherwise orphaned manifest.
        """

        self.authorization.require(
            actor,
            Capability.PREFLIGHT_RUN,
            project_id=project_id,
        )
        frozen = self._load_frozen_input(project_id)
        requirements = plan_preflight_requirements(
            frozen.plan,
            frozen.prepared.records,
        )
        if any(not request.domain for request in requirements.record_requests):
            raise ReadinessError(
                "An Odoo record read could not be narrowed safely. "
                "Odoo was not contacted."
            )
        metadata, records = reader(
            requirements.metadata_requests,
            requirements.record_requests,
        )
        metadata, records = bind_snapshot_hashes(metadata, records)
        _validate_snapshot_projection(
            requirements.metadata_requests,
            requirements.record_requests,
            metadata,
            records,
        )
        project = self.projects.get(project_id)
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
            frozen.plan,
            frozen.prepared,
            metadata,
            records,
        )
        if not result.metadata_snapshot_hash or not result.record_snapshot_hash:
            raise ReadinessError("Odoo snapshot evidence is incomplete")
        run_id = str(uuid4())
        frozen_input_hash = frozen.content_hash
        requirement_plan_hash = requirements.semantic_hash
        manifest = result.to_portable_dict()
        manifest["preflight_evidence"] = {
            "frozen_input_hash": frozen_input_hash,
            "normalization_run_id": frozen.normalization.run_id,
            "normalization_content_hash": frozen.normalization.content_hash,
            "normalization_lifecycle_version": (
                frozen.normalization.lifecycle_version
            ),
            "eligible_dataset_hash": frozen.normalization.eligible_dataset_hash,
            "compiled_migration_plan_hash": frozen.plan.semantic_hash,
            "requirement_plan_hash": requirement_plan_hash,
            "requirement_model_count": requirements.model_count,
            "requirement_chunk_count": requirements.chunk_count,
            "source_record_count": requirements.source_record_count,
        }
        manifest_content = canonical_json_bytes(manifest) + b"\n"
        del manifest
        report = _readiness_report(
            run_id,
            project,
            frozen.revision,
            result,
            frozen.dataset_labels,
            frozen.source_field_labels,
            actor,
            frozen.staging,
            frozen.quality,
            frozen.normalization,
            frozen_input_hash=frozen_input_hash,
            requirement_plan_hash=requirement_plan_hash,
            metadata_snapshot_hash=result.metadata_snapshot_hash,
            record_snapshot_hash=result.record_snapshot_hash,
        )
        report = replace(
            report,
            manifest_hash="sha256:" + sha256(manifest_content).hexdigest(),
        )
        decision_count = len(report.rows)
        decision_rows = iter(report.rows)
        report = replace(report, rows=())
        del frozen, requirements, result
        try:
            self.artifacts.write_report(
                project_id,
                run_id,
                MANIFEST_NAME,
                manifest_content,
            )
            self.preflight.save_readiness_report(
                project_id,
                report,
                decision_rows=decision_rows,
                decision_count=decision_count,
                metadata_snapshot=metadata,
                record_snapshot=records,
                actor=actor,
            )
        except Exception:
            try:
                self.artifacts.delete_report(project_id, run_id, MANIFEST_NAME)
            except Exception:
                pass
            raise
        return report

    def _load_frozen_input(self, project_id: str) -> FrozenPreflightInput:
        """Load version-checked durable evidence without source artifacts."""

        project = self.projects.get(project_id)
        revision = self.mappings.get_mapping_revision(project_id)
        if revision is None:
            raise ReadinessError("Submit the mapping before comparing with Odoo")
        submission = self.mappings.get_mapping_submission(
            project_id, revision.version
        )
        if (
            submission is None
            or submission.mapping_content_hash != revision.definition.content_hash
        ):
            raise ReadinessError(
                "Submit the current mapping before comparing with Odoo"
            )
        selection = self.sources.get_mapping_source_selection(project_id)
        staging_summary = self.staging.get_current_staging_summary(project_id)
        quality_summary = self.quality.get_current_quality_summary(project_id)
        normalization = self.normalization.get_current_normalization_summary(
            project_id
        )
        if selection is None or staging_summary is None or quality_summary is None:
            raise ReadinessError(
                "Prepare the data before comparing it with Odoo. Odoo was not contacted."
            )
        if normalization is None:
            raise ReadinessError(
                "Approve the prepared data before comparing it with Odoo. "
                "Odoo was not contacted."
            )
        staging = self.staging.get_canonical_staging_run(
            project_id, staging_summary.run_id
        )
        quality = self.quality.get_quality_run(project_id, quality_summary.run_id)
        effective = None
        if quality_summary.effective_dataset_run_id is not None:
            if self.effective is None:
                raise ReadinessError(
                    "The approved resolved rows could not be loaded. Odoo was not contacted."
                )
            effective = self.effective.get_current_effective_dataset(project_id)
            if (
                effective is None
                or effective.content_hash != quality_summary.effective_dataset_hash
            ):
                raise ReadinessError(
                    "The approved resolved rows could not be verified. Odoo was not contacted."
                )
        dry_run = self.normalization.get_normalization_dry_run(
            project_id, normalization.run_id
        )
        if staging is None or quality is None or dry_run is None:
            raise ReadinessError(
                "The approved prepared evidence is incomplete. Odoo was not contacted."
            )
        plan = compile_browser_mapping(
            revision.definition,
            selection,
            derived_plan_hash=staging.derived_plan_hash,
        )
        dataset_labels, source_field_labels = browser_mapping_labels(
            revision.definition,
            selection,
        )
        return build_frozen_preflight_input(
            project_id=project.project_id,
            revision=revision,
            selection=selection,
            staging_summary=staging_summary,
            staging=staging,
            quality_summary=quality_summary,
            quality=quality,
            normalization=normalization,
            dry_run=dry_run,
            plan=plan,
            dataset_labels=dataset_labels,
            source_field_labels=source_field_labels,
            effective=effective,
        )


def _validate_snapshot_projection(
    metadata_requests: tuple[MetadataRequest, ...],
    record_requests: tuple[RecordRequest, ...],
    metadata: MetadataSnapshot,
    records: RecordSnapshot,
) -> None:
    """Require exact planned models/fields before comparison.

    Extra fields are rejected as well as omissions, proving that the protected
    snapshot came from the bounded requirement plan rather than a broad target
    export.
    """

    expected_metadata = {item.model: item.fields for item in metadata_requests}
    if set(metadata.models) != set(expected_metadata):
        raise ReadinessError("Odoo metadata snapshot is incomplete")
    for model, fields in expected_metadata.items():
        actual_fields = set(metadata.models[model].fields)
        expected_fields = set(fields)
        if actual_fields != expected_fields:
            if actual_fields - expected_fields:
                raise ReadinessError("Odoo metadata snapshot contains unplanned fields")
            raise ReadinessError("Odoo metadata snapshot is incomplete")

    expected_records: dict[str, tuple[str, ...]] = {}
    for request in record_requests:
        previous = expected_records.setdefault(request.model, request.fields)
        if previous != request.fields:
            raise ReadinessError("Odoo record plan has inconsistent field projections")
    if set(records.records) != set(expected_records) or set(
        records.requested_fields
    ) != set(expected_records):
        raise ReadinessError("Odoo record snapshot is incomplete")
    for model, fields in expected_records.items():
        if tuple(records.requested_fields[model]) != tuple(fields):
            raise ReadinessError("Odoo record snapshot omitted requested fields")
