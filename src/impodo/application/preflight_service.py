"""Stage-H orchestration over approved, frozen source-side evidence.

``PreflightService.compare`` verifies and adapts current Stages Dâ€“G evidence,
builds bounded read requirements, invokes a caller-supplied read-only target
reader, runs the shared comparison engine, and publishes a portable report plus
protected snapshots. It never reloads source files and exposes no Odoo write.
"""

from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
import json
from typing import Callable
from uuid import uuid4

from ..access import Actor, AuthorizationPolicy, Capability
from ..artifacts import ArtifactStore, ArtifactStoreError
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
from ..domain.execution_snapshot import (
    ExecutionSnapshot,
    build_execution_snapshot,
)
from ..domain.preflight.frozen_input import (
    FrozenPreflightInput,
    build_frozen_preflight_input,
)
from ..domain.preflight.reports import (
    ReadinessReport,
    ReadinessRowPage,
    _readiness_report,
)
from ..domain.odoo_comparison import OdooComparisonArtifact
from ..engine import PreflightEngine
from ..models import canonical_json_bytes, target_identity_hash
from ..planner import PreflightRequirementPlan, plan_preflight_requirements
from ..workspace_state import SourceMode
from ..staging import StagingRunSummary
from ..workspace_errors import WorkspaceError
from .readiness_ports import (
    PreflightMappingRepository,
    PreflightEffectiveRepository,
    PreflightNormalizationRepository,
    PreflightProjectRepository,
    PreflightQualityRepository,
    PreflightRepository,
    PreflightSchemaRepository,
    PreflightSourceRepository,
    PreflightStagingRepository,
)
from .odoo_comparison_service import (
    ODOO_COMPARISON_ARTIFACT_NAME,
    build_odoo_comparison_publication,
)
from .odoo_read_failures import (
    OdooReadFailureCode,
    OdooReadWorkflowError,
)
from .odoo_provenance_service import OdooProvenanceService


MANIFEST_NAME = "impodo_preflight_manifest.json"
EXECUTION_SNAPSHOT_NAME = "impodo_execution_snapshot.json"

ReadinessReader = Callable[
    [PreflightRequirementPlan],
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
        schemas: PreflightSchemaRepository | None = None,
        odoo_provenance: OdooProvenanceService | None = None,
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
        self.schemas = schemas
        self.odoo_provenance = odoo_provenance
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

    def current_execution_snapshot(
        self, project_id: str
    ) -> ExecutionSnapshot | None:
        """Load the automatically generated snapshot for current evidence.

        The artifact is usable only while the current report still matches all
        source, normalization, mapping, and target bindings.  Corrupt or
        substituted content fails closed instead of silently rebuilding a
        different execution input.
        """

        if (
            getattr(self.projects.get(project_id), "source_mode", SourceMode.FILE)
            is SourceMode.ODOO
        ):
            return None
        report = self.current_report(project_id)
        if report is None:
            return None
        try:
            snapshot = self.execution_snapshot(project_id, report.run_id)
            with self.artifacts.materialize_report(
                project_id,
                report.run_id,
                MANIFEST_NAME,
            ) as path:
                manifest_content = path.read_bytes()
            manifest = json.loads(manifest_content)
            if not isinstance(manifest, dict) or not isinstance(
                manifest.get("preflight_evidence"), dict
            ):
                raise ValueError("Preflight manifest evidence is invalid")
            manifest_evidence = manifest["preflight_evidence"]
        except (ArtifactStoreError, OSError, ValueError) as error:
            raise ReadinessError(
                "The execution snapshot is missing or invalid. Run the Odoo "
                "comparison again."
            ) from error
        if not _snapshot_matches_report(snapshot, report) or not (
            "sha256:" + sha256(manifest_content).hexdigest()
            == report.manifest_hash
            and manifest_evidence.get("execution_snapshot_hash")
            == snapshot.semantic_hash
        ):
            raise ReadinessError(
                "The execution snapshot no longer matches the current Odoo "
                "comparison. Run the comparison again."
            )
        return snapshot

    def execution_snapshot(
        self,
        project_id: str,
        preflight_run_id: str,
    ) -> ExecutionSnapshot:
        """Load one immutable execution snapshot by its preflight run.

        Reconciliation uses the exact historical artifact named by the load
        journal. It must not silently switch to a newer comparison while
        checking an older write outcome.
        """

        try:
            with self.artifacts.materialize_report(
                project_id,
                preflight_run_id,
                EXECUTION_SNAPSHOT_NAME,
            ) as path:
                snapshot = ExecutionSnapshot.from_json(path.read_text("utf-8"))
        except (ArtifactStoreError, OSError, ValueError) as error:
            raise ReadinessError(
                "The saved load preview is missing or invalid. Compare with "
                "Odoo again before another load."
            ) from error
        if (
            snapshot.project_id != project_id
            or snapshot.preflight_run_id != preflight_run_id
        ):
            raise ReadinessError("The saved load preview does not match this project")
        return snapshot

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
        project = self.projects.get(project_id)
        if getattr(project, "source_mode", SourceMode.FILE) is SourceMode.ODOO:
            return self._compare_odoo_source(
                project,
                reader=reader,
                actor=actor,
            )
        frozen = self._load_frozen_input(project_id)
        requirements = plan_preflight_requirements(
            frozen.plan,
            frozen.prepared.records,
        )
        if any(not request.domain for request in requirements.record_requests):
            raise OdooReadWorkflowError(
                OdooReadFailureCode.MAPPING_EVIDENCE_STALE,
                "An Odoo record read could not be narrowed safely. "
                "Odoo was not contacted.",
            )
        metadata, records = reader(requirements)
        metadata, records = bind_snapshot_hashes(metadata, records)
        _validate_snapshot_projection(
            requirements.metadata_requests,
            requirements.record_requests,
            metadata,
            records,
        )
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
            raise OdooReadWorkflowError(
                OdooReadFailureCode.SCHEMA_EVIDENCE_STALE,
                "Readiness data came from a different Odoo target",
            )
        result = self.engine.run(
            frozen.plan,
            frozen.prepared,
            metadata,
            records,
            captured_schema=getattr(frozen, "captured_schema", None),
        )
        if not result.metadata_snapshot_hash or not result.record_snapshot_hash:
            raise OdooReadWorkflowError(
                OdooReadFailureCode.RESPONSE_INCOMPLETE,
                "Odoo snapshot evidence is incomplete",
            )
        run_id = str(uuid4())
        execution_snapshot = build_execution_snapshot(
            preflight_run_id=run_id,
            frozen=frozen,
            result=result,
        )
        execution_snapshot_content = (
            execution_snapshot.to_json().encode("utf-8") + b"\n"
        )
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
            "execution_snapshot_hash": execution_snapshot.semantic_hash,
            "execution_snapshot_root_hash": execution_snapshot.root_hash,
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
            self.artifacts.write_report(
                project_id,
                run_id,
                EXECUTION_SNAPSHOT_NAME,
                execution_snapshot_content,
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
            for filename in (MANIFEST_NAME, EXECUTION_SNAPSHOT_NAME):
                try:
                    self.artifacts.delete_report(project_id, run_id, filename)
                except Exception:
                    pass
            raise
        return report

    def current_odoo_comparison(
        self,
        project_id: str,
        *,
        actor: Actor,
    ) -> OdooComparisonArtifact | None:
        """Decrypt the current exact-ID comparison for an authorized backend."""

        report = self.current_report(project_id)
        if report is None or (
            getattr(self.projects.get(project_id), "source_mode", SourceMode.FILE)
            is not SourceMode.ODOO
        ):
            return None
        if self.odoo_provenance is None:
            raise ReadinessError("Protected Odoo comparison support is unavailable")
        try:
            with self.artifacts.materialize_report(
                project_id,
                report.run_id,
                MANIFEST_NAME,
            ) as path:
                manifest = json.loads(path.read_text("utf-8"))
            evidence = manifest["preflight_evidence"]
            with self.artifacts.materialize_report(
                project_id,
                report.run_id,
                ODOO_COMPARISON_ARTIFACT_NAME,
            ) as path:
                encrypted = path.read_bytes()
            plaintext = self.odoo_provenance.open_comparison(
                project_id,
                report.run_id,
                str(evidence["capture_manifest_hash"]),
                encrypted,
                expected_logical_hash=str(evidence["protected_logical_hash"]),
                expected_artifact_hash=str(evidence["protected_artifact_hash"]),
                actor=actor,
            )
            artifact = OdooComparisonArtifact.from_json(plaintext.decode("utf-8"))
        except (
            ArtifactStoreError,
            KeyError,
            OSError,
            TypeError,
            ValueError,
            WorkspaceError,
        ) as error:
            raise ReadinessError(
                "The protected Odoo comparison is missing or invalid. Compare again."
            ) from error
        if (
            artifact.project_id != project_id
            or artifact.run_id != report.run_id
            or artifact.frozen_input_hash != report.frozen_input_hash
            or artifact.content_hash != evidence.get("protected_comparison_hash")
        ):
            raise ReadinessError(
                "The protected Odoo comparison no longer matches this review."
            )
        return artifact

    def _compare_odoo_source(
        self,
        project,
        *,
        reader: ReadinessReader,
        actor: Actor,
    ) -> ReadinessReport:
        """Publish a read-only pinned comparison without portable Odoo IDs."""

        if self.odoo_provenance is None:
            raise ReadinessError(
                "Protected Odoo comparison support is unavailable. Odoo was not contacted."
            )
        frozen = self._load_frozen_input(project.project_id)
        selection = self.sources.get_mapping_source_selection(project.project_id)
        if selection is None:
            raise ReadinessError(
                "Refresh the captured Odoo records before comparing. Odoo was not contacted."
            )
        run_id = str(uuid4())
        publication = build_odoo_comparison_publication(
            project=project,
            frozen=frozen,
            selection=selection,
            source_snapshots=self.sources.get_current_source_snapshots(
                project.project_id
            ),
            artifacts=self.artifacts,
            provenance=self.odoo_provenance,
            reader=reader,
            actor=actor,
            run_id=run_id,
        )
        try:
            self.artifacts.write_report(
                project.project_id,
                run_id,
                MANIFEST_NAME,
                publication.portable_manifest,
            )
            self.artifacts.write_report(
                project.project_id,
                run_id,
                ODOO_COMPARISON_ARTIFACT_NAME,
                publication.protected.encrypted_bytes,
            )
            self.preflight.save_readiness_report(
                project.project_id,
                replace(publication.report, rows=()),
                decision_rows=iter(publication.rows),
                decision_count=len(publication.rows),
                metadata_snapshot=publication.metadata_snapshot,
                record_snapshot=publication.redacted_record_snapshot,
                actor=actor,
            )
        except Exception:
            for filename in (MANIFEST_NAME, ODOO_COMPARISON_ARTIFACT_NAME):
                try:
                    self.artifacts.delete_report(project.project_id, run_id, filename)
                except Exception:
                    pass
            raise
        return publication.report

    def _load_frozen_input(self, project_id: str) -> FrozenPreflightInput:
        """Load version-checked durable evidence without source artifacts."""

        project = self.projects.get(project_id)
        revision = self.mappings.get_mapping_revision(project_id)
        if revision is None:
            raise OdooReadWorkflowError(
                OdooReadFailureCode.MAPPING_EVIDENCE_STALE,
                "Submit the mapping before comparing with Odoo",
            )
        submission = self.mappings.get_mapping_submission(
            project_id, revision.version
        )
        if (
            submission is None
            or submission.mapping_content_hash != revision.definition.content_hash
        ):
            raise OdooReadWorkflowError(
                OdooReadFailureCode.MAPPING_EVIDENCE_STALE,
                "Submit the current mapping before comparing with Odoo",
            )
        captured_schema = None
        if self.schemas is not None:
            captured_schema = self.schemas.get_odoo_schema_catalog(project_id)
            governance = self.schemas.get_schema_governance(project_id)
            expected_schema_hash = (
                governance.content_hash
                if governance is not None
                else (
                    captured_schema.content_hash
                    if captured_schema is not None
                    else None
                )
            )
            if (
                captured_schema is None
                or expected_schema_hash != revision.definition.schema_hash
                or (
                    governance is not None
                    and governance.catalog_hash != captured_schema.content_hash
                )
            ):
                raise OdooReadWorkflowError(
                    (
                        OdooReadFailureCode.SCHEMA_EVIDENCE_MISSING
                        if captured_schema is None
                        else OdooReadFailureCode.SCHEMA_EVIDENCE_STALE
                    ),
                    "The captured Odoo fields no longer match the submitted "
                    "mapping. Odoo was not contacted.",
                )
        selection = self.sources.get_mapping_source_selection(project_id)
        staging_summary = self.staging.get_current_staging_summary(project_id)
        quality_summary = self.quality.get_current_quality_summary(project_id)
        normalization = self.normalization.get_current_normalization_summary(
            project_id
        )
        if selection is None or staging_summary is None or quality_summary is None:
            raise OdooReadWorkflowError(
                OdooReadFailureCode.PREPARED_EVIDENCE_STALE,
                "Prepare the data before comparing it with Odoo. "
                "Odoo was not contacted.",
            )
        if normalization is None:
            raise OdooReadWorkflowError(
                OdooReadFailureCode.PREPARED_EVIDENCE_STALE,
                "Approve the prepared data before comparing it with Odoo. "
                "Odoo was not contacted.",
            )
        staging = self.staging.get_canonical_staging_run(
            project_id, staging_summary.run_id
        )
        quality = self.quality.get_quality_run(project_id, quality_summary.run_id)
        effective = None
        if quality_summary.effective_dataset_run_id is not None:
            if self.effective is None:
                raise OdooReadWorkflowError(
                    OdooReadFailureCode.PREPARED_EVIDENCE_STALE,
                    "The approved resolved rows could not be loaded. "
                    "Odoo was not contacted.",
                )
            effective = self.effective.get_current_effective_dataset(project_id)
            if (
                effective is None
                or effective.content_hash != quality_summary.effective_dataset_hash
            ):
                raise OdooReadWorkflowError(
                    OdooReadFailureCode.PREPARED_EVIDENCE_STALE,
                    "The approved resolved rows could not be verified. "
                    "Odoo was not contacted.",
                )
        dry_run = self.normalization.get_normalization_dry_run(
            project_id, normalization.run_id
        )
        if staging is None or quality is None or dry_run is None:
            raise OdooReadWorkflowError(
                OdooReadFailureCode.PREPARED_EVIDENCE_STALE,
                "The approved prepared evidence is incomplete. "
                "Odoo was not contacted.",
            )
        try:
            plan = compile_browser_mapping(
                revision.definition,
                selection,
                derived_plan_hash=staging.derived_plan_hash,
            )
            dataset_labels, source_field_labels = browser_mapping_labels(
                revision.definition,
                selection,
            )
        except ReadinessError as error:
            raise OdooReadWorkflowError(
                OdooReadFailureCode.MAPPING_EVIDENCE_STALE,
                str(error),
            ) from error
        try:
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
                captured_schema=captured_schema,
            )
        except ReadinessError as error:
            raise OdooReadWorkflowError(
                OdooReadFailureCode.PREPARED_EVIDENCE_STALE,
                str(error),
            ) from error


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
        raise OdooReadWorkflowError(
            OdooReadFailureCode.RESPONSE_INCOMPLETE,
            "Odoo metadata snapshot is incomplete",
        )
    for model, fields in expected_metadata.items():
        actual_fields = set(metadata.models[model].fields)
        expected_fields = set(fields)
        if actual_fields != expected_fields:
            if actual_fields - expected_fields:
                raise OdooReadWorkflowError(
                    OdooReadFailureCode.RESPONSE_INCOMPLETE,
                    "Odoo metadata snapshot contains unplanned fields",
                )
            raise OdooReadWorkflowError(
                OdooReadFailureCode.RESPONSE_INCOMPLETE,
                "Odoo metadata snapshot is incomplete",
            )

    expected_records: dict[str, tuple[str, ...]] = {}
    for request in record_requests:
        previous = expected_records.setdefault(request.model, request.fields)
        if previous != request.fields:
            raise OdooReadWorkflowError(
                OdooReadFailureCode.MAPPING_EVIDENCE_STALE,
                "Odoo record plan has inconsistent field projections",
            )
    if set(records.records) != set(expected_records) or set(
        records.requested_fields
    ) != set(expected_records):
        raise OdooReadWorkflowError(
            OdooReadFailureCode.RESPONSE_INCOMPLETE,
            "Odoo record snapshot is incomplete",
        )
    for model, fields in expected_records.items():
        if tuple(records.requested_fields[model]) != tuple(fields):
            raise OdooReadWorkflowError(
                OdooReadFailureCode.RESPONSE_INCOMPLETE,
                "Odoo record snapshot omitted requested fields",
            )


def _snapshot_matches_report(
    snapshot: ExecutionSnapshot,
    report: ReadinessReport,
) -> bool:
    """Bind a stored execution payload to the exact current readiness report."""

    return (
        snapshot.project_id == report.project_id
        and snapshot.preflight_run_id == report.run_id
        and snapshot.mapping_id == report.mapping_id
        and snapshot.mapping_version == report.mapping_version
        and snapshot.mapping_content_hash == report.mapping_content_hash
        and snapshot.staging_run_id == report.staging_run_id
        and snapshot.staging_content_hash == report.staging_content_hash
        and snapshot.quality_run_id == report.quality_run_id
        and snapshot.quality_content_hash == report.quality_content_hash
        and snapshot.normalization_run_id == report.normalization_run_id
        and snapshot.normalization_content_hash
        == report.normalization_content_hash
        and snapshot.normalization_lifecycle_version
        == report.normalization_lifecycle_version
        and snapshot.eligible_dataset_hash == report.eligible_dataset_hash
        and snapshot.frozen_input_hash == report.frozen_input_hash
        and snapshot.preflight_result_hash == report.result_hash
        and snapshot.metadata_snapshot_hash == report.metadata_snapshot_hash
        and snapshot.record_snapshot_hash == report.record_snapshot_hash
        and snapshot.target_hash == report.target_hash
        and snapshot.target_database == report.target_database
        and snapshot.target_odoo_version == report.target_odoo_version
        and snapshot.target_snapshot_at == report.target_snapshot_at
        and dict(snapshot.target_module_versions)
        == dict(report.target_module_versions)
        and dict(snapshot.counts)
        == {
            "AMBIGUOUS": report.ambiguous_count,
            "BLOCKED": report.blocked_count,
            "CREATE": report.create_count,
            "UNCHANGED": report.unchanged_count,
            "UPDATE": report.update_count,
        }
    )

