"""DuckDB persistence for Slice 6 scope, references, and resolution evidence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from uuid import uuid4

from ...access import Actor, AuthorizationError, Capability
from ...domain.coverage import (
    CoverageScopeRevision,
    ReferenceBundle,
    ReferenceValueKind,
    validate_odoo_selection_reference_outputs,
)
from ...domain.mapping.artifacts import MappingRevision
from ...domain.resolution import (
    EffectiveDataset,
    ResolutionDecision,
    ResolutionEvaluation,
    ResolutionFinding,
    ResolutionCandidate,
    ResolutionPolicy,
    ResolutionReconciliation,
    ResolutionRowAccounting,
    EffectiveRow,
    ResolutionState,
    pass_through_effective_row,
)
from ...domain.serialization import canonical_json
from ...workspace_errors import WorkspaceError
from ...workspace_contracts import OdooSchemaCatalog, SourceSelection
from .constants import RESOLUTION_ROW_BATCH_SIZE
from .database import DuckDbWorkspaceDatabase
from .repository import DuckDbRepository


@dataclass(frozen=True, slots=True)
class ResolutionRunSummary:
    run_id: str
    staging_run_id: str
    evaluation_hash: str
    staging_content_hash: str
    policy_hash: str
    status: str
    lifecycle_version: int
    candidate_count: int
    finding_count: int
    decision_count: int
    effective_content_hash: str | None


class AdvancedCoverageRepository(DuckDbRepository):
    """Store advanced input revisions and immutable reviewed effective data."""

    def __init__(self, database: DuckDbWorkspaceDatabase) -> None:
        super().__init__(database)

    def save_coverage_scope(
        self,
        workspace_id: str,
        scope: CoverageScopeRevision,
        *,
        expected_parent_version: int | None,
        actor: Actor,
    ) -> None:
        _require(actor, Capability.COVERAGE_SCOPE)
        if scope.workspace_id != workspace_id or scope.approved_by != actor.identity:
            raise WorkspaceError("Coverage scope approval identity is invalid")
        database_path = self.workspace_directory(workspace_id) / "workspace-engine.duckdb"
        with self._connect(database_path) as connection:
            self._ensure_workspace_database_schema(connection)
            connection.begin()
            try:
                selected = connection.execute(
                    "SELECT selection_json FROM source_selection WHERE singleton_id = 1"
                ).fetchone()
                if selected is None or SourceSelection.from_json(
                    str(selected[0])
                ).content_hash != scope.source_selection_hash:
                    raise WorkspaceError(
                        "Coverage scope no longer matches the frozen source selection"
                    )
                current = connection.execute(
                    "SELECT scope_id, version FROM coverage_scope_current WHERE singleton_id = 1"
                ).fetchone()
                actual_parent = int(current[1]) if current else None
                if actual_parent != expected_parent_version:
                    raise WorkspaceError("Coverage scope changed; reload before saving")
                if scope.parent_version != expected_parent_version:
                    raise WorkspaceError("Coverage scope parent version is stale")
                if current is not None and str(current[0]) != scope.scope_id:
                    raise WorkspaceError("Coverage scope identity cannot change mid-history")
                connection.execute(
                    """
                    INSERT INTO coverage_scope_revision
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    [
                        scope.scope_id,
                        scope.version,
                        scope.content_hash,
                        scope.source_selection_hash,
                        scope.to_json(),
                    ],
                )
                self._invalidate_canonical_staging(
                    connection,
                    reason="COVERAGE_SCOPE_CHANGED",
                )
                connection.execute(
                    "INSERT OR REPLACE INTO coverage_scope_current VALUES (1, ?, ?)",
                    [scope.scope_id, scope.version],
                )
                self._insert_workspace_audit(
                    connection,
                    revision=self._workspace_revision(connection),
                    event_type="COVERAGE_SCOPE_APPROVED",
                    detail=f"version {scope.version}: {scope.content_hash}",
                    actor=actor,
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def get_coverage_scope(self, workspace_id: str) -> CoverageScopeRevision | None:
        value = self._read_singleton_json(
            workspace_id,
            """
            SELECT revision.scope_json
              FROM coverage_scope_current AS current
              JOIN coverage_scope_revision AS revision
                ON revision.scope_id = current.scope_id
               AND revision.version = current.version
             WHERE current.singleton_id = 1
            """,
        )
        return CoverageScopeRevision.from_dict(json.loads(value)) if value else None

    def save_reference_bundle(
        self,
        workspace_id: str,
        bundle: ReferenceBundle,
        *,
        actor: Actor,
    ) -> None:
        _require(actor, Capability.COVERAGE_SCOPE)
        if bundle.workspace_id != workspace_id:
            raise WorkspaceError("Reference bundle belongs to another workspace")
        database_path = self.workspace_directory(workspace_id) / "workspace-engine.duckdb"
        with self._connect(database_path) as connection:
            self._ensure_workspace_database_schema(connection)
            connection.begin()
            try:
                current = connection.execute(
                    "SELECT content_hash FROM reference_bundle_current WHERE singleton_id = 1"
                ).fetchone()
                if current is not None and str(current[0]) == bundle.content_hash:
                    connection.rollback()
                    return
                connection.execute(
                    "INSERT OR IGNORE INTO reference_bundle_revision VALUES (?, ?)",
                    [bundle.content_hash, canonical_json(bundle.to_portable_dict())],
                )
                self._invalidate_canonical_staging(
                    connection,
                    reason="REFERENCE_BUNDLE_CHANGED",
                )
                connection.execute(
                    "INSERT OR REPLACE INTO reference_bundle_current VALUES (1, ?)",
                    [bundle.content_hash],
                )
                self._insert_workspace_audit(
                    connection,
                    revision=self._workspace_revision(connection),
                    event_type="REFERENCE_BUNDLE_PUBLISHED",
                    detail=f"{len(bundle.datasets)} list(s): {bundle.content_hash}",
                    actor=actor,
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def get_reference_bundle(self, workspace_id: str) -> ReferenceBundle | None:
        value = self._read_singleton_json(
            workspace_id,
            """
            SELECT revision.bundle_json
              FROM reference_bundle_current AS current
              JOIN reference_bundle_revision AS revision
                ON revision.content_hash = current.content_hash
             WHERE current.singleton_id = 1
            """,
        )
        return ReferenceBundle.from_dict(json.loads(value)) if value else None

    def get_validated_reference_bundle(
        self,
        workspace_id: str,
    ) -> ReferenceBundle | None:
        """Load current references and verify selection keys against frozen schema."""

        bundle = self.get_reference_bundle(workspace_id)
        if bundle is None:
            return None
        if not any(
            kind is ReferenceValueKind.ODOO_SELECTION_KEY
            for dataset in bundle.datasets
            for kind in dataset.value_kinds.values()
        ):
            return bundle
        database_path = self.workspace_directory(workspace_id) / "workspace-engine.duckdb"
        with self._connect(database_path) as connection:
            self._ensure_workspace_database_schema(connection)
            row = connection.execute(
                """
                SELECT mapping.revision_json, schema.catalog_json
                  FROM mapping_current AS current
                  JOIN mapping_revision AS mapping
                    ON mapping.mapping_id = current.mapping_id
                   AND mapping.version = current.version
                  JOIN odoo_schema_catalog AS schema
                    ON schema.singleton_id = 1
                 WHERE current.singleton_id = 1
                """
            ).fetchone()
        if row is None:
            raise WorkspaceError(
                "Reference data cannot be verified without current mapping and schema"
            )
        try:
            mapping = MappingRevision.from_json(str(row[0])).definition
            schema = OdooSchemaCatalog.from_json(str(row[1]))
            validate_odoo_selection_reference_outputs(bundle, mapping, schema)
        except (TypeError, ValueError, KeyError) as error:
            raise WorkspaceError(
                "Reference data no longer matches the captured Odoo selections"
            ) from error
        return bundle

    def save_resolution_policy(
        self,
        workspace_id: str,
        policy: ResolutionPolicy,
        *,
        expected_parent_version: int | None,
        actor: Actor,
    ) -> None:
        _require(actor, Capability.COVERAGE_SCOPE)
        if policy.workspace_id != workspace_id:
            raise WorkspaceError("Resolution policy belongs to another workspace")
        database_path = self.workspace_directory(workspace_id) / "workspace-engine.duckdb"
        with self._connect(database_path) as connection:
            self._ensure_workspace_database_schema(connection)
            connection.begin()
            try:
                bindings = connection.execute(
                    """
                    SELECT scope.content_hash, reference.content_hash,
                           mapping.content_hash, mapping.schema_hash
                      FROM coverage_scope_current AS scope_current
                      JOIN coverage_scope_revision AS scope
                        ON scope.scope_id = scope_current.scope_id
                       AND scope.version = scope_current.version
                      JOIN reference_bundle_current AS reference
                        ON reference.singleton_id = 1
                      JOIN mapping_current AS mapping_current
                        ON mapping_current.singleton_id = 1
                      JOIN mapping_revision AS mapping
                        ON mapping.mapping_id = mapping_current.mapping_id
                       AND mapping.version = mapping_current.version
                     WHERE scope_current.singleton_id = 1
                    """
                ).fetchone()
                if bindings is None or (
                    str(bindings[0]) != policy.coverage_scope_hash
                    or str(bindings[1]) != policy.reference_bundle_hash
                    or str(bindings[2]) != policy.mapping_hash
                    or str(bindings[3]) != policy.schema_hash
                ):
                    raise WorkspaceError(
                        "Resolution policy no longer matches its approved inputs"
                    )
                current = connection.execute(
                    "SELECT policy_id, version FROM resolution_policy_current WHERE singleton_id = 1"
                ).fetchone()
                actual_parent = int(current[1]) if current else None
                if actual_parent != expected_parent_version or policy.parent_version != actual_parent:
                    raise WorkspaceError("Resolution policy changed; reload before saving")
                if current is not None and str(current[0]) != policy.policy_id:
                    raise WorkspaceError("Resolution policy identity cannot change mid-history")
                connection.execute(
                    "INSERT INTO resolution_policy_revision VALUES (?, ?, ?, ?)",
                    [
                        policy.policy_id,
                        policy.version,
                        policy.content_hash,
                        canonical_json(policy.to_portable_dict()),
                    ],
                )
                self._invalidate_resolution(
                    connection,
                    reason="RESOLUTION_POLICY_CHANGED",
                )
                connection.execute(
                    "INSERT OR REPLACE INTO resolution_policy_current VALUES (1, ?, ?)",
                    [policy.policy_id, policy.version],
                )
                self._insert_workspace_audit(
                    connection,
                    revision=self._workspace_revision(connection),
                    event_type="RESOLUTION_POLICY_PUBLISHED",
                    detail=f"version {policy.version}: {policy.content_hash}",
                    actor=actor,
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def get_resolution_policy(self, workspace_id: str) -> ResolutionPolicy | None:
        value = self._read_singleton_json(
            workspace_id,
            """
            SELECT revision.policy_json
              FROM resolution_policy_current AS current
              JOIN resolution_policy_revision AS revision
                ON revision.policy_id = current.policy_id
               AND revision.version = current.version
             WHERE current.singleton_id = 1
            """,
        )
        return ResolutionPolicy.from_dict(json.loads(value)) if value else None

    def publish_resolution_evaluation(
        self,
        workspace_id: str,
        evaluation: ResolutionEvaluation,
        *,
        staging_run_id: str,
        actor: Actor,
    ) -> ResolutionRunSummary:
        _require(actor, Capability.RESOLUTION_DECIDE)
        if evaluation.workspace_id != workspace_id:
            raise WorkspaceError("Resolution evaluation belongs to another workspace")
        database_path = self.workspace_directory(workspace_id) / "workspace-engine.duckdb"
        with self._connect(database_path) as connection:
            self._ensure_workspace_database_schema(connection)
            connection.begin()
            try:
                upstream = connection.execute(
                    """
                    SELECT staging.run_id, staging.content_hash, policy.content_hash
                      FROM canonical_staging_current AS staging_current
                      JOIN canonical_staging_run AS staging
                        ON staging.run_id = staging_current.run_id
                      JOIN resolution_policy_current AS policy_current
                        ON policy_current.singleton_id = 1
                      JOIN resolution_policy_revision AS policy
                        ON policy.policy_id = policy_current.policy_id
                       AND policy.version = policy_current.version
                     WHERE staging_current.singleton_id = 1
                       AND staging.status = 'PUBLISHED'
                    """
                ).fetchone()
                if upstream is None or (
                    str(upstream[0]) != staging_run_id
                    or str(upstream[1]) != evaluation.staging_content_hash
                    or str(upstream[2]) != evaluation.policy_hash
                ):
                    raise WorkspaceError("Resolution input is no longer current")
                existing = connection.execute(
                    """
                    SELECT run.run_id
                      FROM resolution_current AS current
                      JOIN resolution_run AS run ON run.run_id = current.run_id
                     WHERE current.singleton_id = 1
                       AND run.evaluation_hash = ?
                    """,
                    [evaluation.content_hash],
                ).fetchone()
                if existing is not None:
                    connection.rollback()
                    summary = self.get_resolution_summary(workspace_id, str(existing[0]))
                    if summary is None:
                        raise WorkspaceError("Current resolution evidence is incomplete")
                    return summary
                self._invalidate_resolution(
                    connection,
                    reason="NEW_RESOLUTION_EVALUATION",
                )
                run_id = str(uuid4())
                now = datetime.now(timezone.utc).isoformat()
                status = "BLOCKED" if evaluation.blocked else "REVIEW_REQUIRED"
                connection.execute(
                    """
                    INSERT INTO resolution_run (
                        run_id, workspace_id, staging_run_id,
                        staging_content_hash, policy_hash, evaluation_hash,
                        compared_pair_count, scorer_version, contract_version,
                        status, lifecycle_version, published_at, published_by
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
                    """,
                    [
                        run_id,
                        workspace_id,
                        staging_run_id,
                        evaluation.staging_content_hash,
                        evaluation.policy_hash,
                        evaluation.content_hash,
                        evaluation.compared_pair_count,
                        evaluation.scorer_version,
                        evaluation.contract_version,
                        status,
                        now,
                        actor.identity.display_name,
                    ],
                )
                self._insert_candidates(connection, run_id, evaluation.candidates)
                self._insert_findings(connection, run_id, evaluation.findings)
                connection.execute(
                    "INSERT OR REPLACE INTO resolution_current VALUES (1, ?)",
                    [run_id],
                )
                self._insert_workspace_audit(
                    connection,
                    revision=self._workspace_revision(connection),
                    event_type="RESOLUTION_EVALUATED",
                    detail=(
                        f"{len(evaluation.candidates)} candidate(s), "
                        f"{len(evaluation.findings)} finding(s)"
                    ),
                    actor=actor,
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        summary = self.get_resolution_summary(workspace_id, run_id)
        if summary is None:
            raise WorkspaceError("Published resolution evidence could not be verified")
        return summary

    def append_resolution_decision(
        self,
        workspace_id: str,
        run_id: str,
        decision: ResolutionDecision,
        *,
        expected_lifecycle_version: int,
        actor: Actor,
    ) -> ResolutionRunSummary:
        _require(actor, Capability.RESOLUTION_DECIDE)
        if decision.actor != actor.identity:
            raise WorkspaceError("Resolution decision identity is invalid")
        database_path = self.workspace_directory(workspace_id) / "workspace-engine.duckdb"
        with self._connect(database_path) as connection:
            self._ensure_workspace_database_schema(connection)
            connection.begin()
            try:
                current = connection.execute(
                    """
                    SELECT run.lifecycle_version, run.evaluation_hash, run.status
                      FROM resolution_current AS current
                      JOIN resolution_run AS run ON run.run_id = current.run_id
                     WHERE current.singleton_id = 1 AND run.run_id = ?
                    """,
                    [run_id],
                ).fetchone()
                if current is None:
                    raise WorkspaceError("Resolution review is no longer current")
                if str(current[2]) != "REVIEW_REQUIRED":
                    raise WorkspaceError("Resolution review cannot accept decisions")
                if int(current[0]) != expected_lifecycle_version:
                    raise WorkspaceError("Resolution review changed; reload it")
                if decision.lifecycle_version != expected_lifecycle_version + 1:
                    raise WorkspaceError("Resolution decision lifecycle version is invalid")
                if decision.evaluation_hash != str(current[1]):
                    raise WorkspaceError("Resolution decision belongs to another evaluation")
                duplicate = connection.execute(
                    """
                    SELECT 1 FROM resolution_decision
                     WHERE run_id = ? AND group_id = ?
                       AND COALESCE(field, '') = COALESCE(?, '')
                    """,
                    [run_id, decision.group_id, decision.field],
                ).fetchone()
                if duplicate is not None:
                    raise WorkspaceError("This resolution choice was already recorded")
                connection.execute(
                    "INSERT INTO resolution_decision VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [
                        run_id,
                        decision.lifecycle_version,
                        decision.decision_id,
                        decision.group_id,
                        decision.field,
                        decision.kind.value,
                        canonical_json(decision.to_portable_dict()),
                    ],
                )
                connection.execute(
                    "UPDATE resolution_run SET lifecycle_version = ? WHERE run_id = ?",
                    [decision.lifecycle_version, run_id],
                )
                self._insert_workspace_audit(
                    connection,
                    revision=self._workspace_revision(connection),
                    event_type="RESOLUTION_DECISION_RECORDED",
                    detail=f"{decision.kind.value}: {decision.group_id}",
                    actor=actor,
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        summary = self.get_resolution_summary(workspace_id, run_id)
        if summary is None:
            raise WorkspaceError("Resolution decision could not be verified")
        return summary

    def get_resolution_decisions(
        self,
        workspace_id: str,
        run_id: str,
    ) -> tuple[ResolutionDecision, ...]:
        values = self._read_json_rows(
            workspace_id,
            """
            SELECT decision_json FROM resolution_decision
             WHERE run_id = ? ORDER BY lifecycle_version
            """,
            [run_id],
        )
        return tuple(ResolutionDecision.from_dict(json.loads(item)) for item in values)

    def freeze_effective_dataset(
        self,
        workspace_id: str,
        run_id: str,
        effective: EffectiveDataset,
        *,
        expected_lifecycle_version: int,
        actor: Actor,
    ) -> ResolutionRunSummary:
        _require(actor, Capability.RESOLUTION_APPROVE)
        if effective.workspace_id != workspace_id:
            raise WorkspaceError("Effective dataset belongs to another workspace")
        database_path = self.workspace_directory(workspace_id) / "workspace-engine.duckdb"
        with self._connect(database_path) as connection:
            self._ensure_workspace_database_schema(connection)
            connection.begin()
            try:
                current = connection.execute(
                    """
                    SELECT run.lifecycle_version, run.evaluation_hash,
                           run.staging_content_hash, run.policy_hash, run.status
                      FROM resolution_current AS current
                      JOIN resolution_run AS run ON run.run_id = current.run_id
                     WHERE current.singleton_id = 1 AND run.run_id = ?
                    """,
                    [run_id],
                ).fetchone()
                if current is None or str(current[4]) != "REVIEW_REQUIRED":
                    raise WorkspaceError("Resolution review is no longer eligible")
                if int(current[0]) != expected_lifecycle_version:
                    raise WorkspaceError("Resolution review changed; reload it")
                if (
                    effective.evaluation_hash != str(current[1])
                    or effective.staging_content_hash != str(current[2])
                    or effective.policy_hash != str(current[3])
                ):
                    raise WorkspaceError("Effective dataset bindings are stale")
                decisions = tuple(
                    ResolutionDecision.from_dict(json.loads(str(item[0])))
                    for item in connection.execute(
                        """
                        SELECT decision_json FROM resolution_decision
                         WHERE run_id = ? ORDER BY lifecycle_version
                        """,
                        [run_id],
                    ).fetchall()
                )
                from ...domain.serialization import content_hash

                stored_decisions_hash = content_hash(
                    [item.to_portable_dict() for item in sorted(decisions, key=lambda item: item.decision_id)]
                )
                if stored_decisions_hash != effective.decisions_hash:
                    raise WorkspaceError("Effective dataset decisions are incomplete")
                self._invalidate_quality(
                    connection,
                    reason="EFFECTIVE_DATASET_CHANGED",
                )
                self._insert_effective_rows(connection, run_id, effective.rows)
                self._insert_accounting(connection, run_id, effective.accounting)
                connection.execute(
                    "INSERT INTO effective_dataset_reconciliation VALUES (?, ?)",
                    [run_id, canonical_json(effective.reconciliation.to_portable_dict())],
                )
                frozen_at = datetime.now(timezone.utc).isoformat()
                connection.execute(
                    """
                    UPDATE resolution_run
                       SET status = 'FROZEN', lifecycle_version = ?,
                           effective_content_hash = ?, decisions_hash = ?,
                           frozen_at = ?, frozen_by = ?
                     WHERE run_id = ?
                    """,
                    [
                        expected_lifecycle_version + 1,
                        effective.content_hash,
                        effective.decisions_hash,
                        frozen_at,
                        actor.identity.display_name,
                        run_id,
                    ],
                )
                connection.execute(
                    "INSERT OR REPLACE INTO effective_dataset_current VALUES (1, ?)",
                    [run_id],
                )
                self._insert_workspace_audit(
                    connection,
                    revision=self._workspace_revision(connection),
                    event_type="EFFECTIVE_DATASET_FROZEN",
                    detail=(
                        f"{effective.reconciliation.effective_rows} row(s): "
                        f"{effective.content_hash}"
                    ),
                    actor=actor,
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        summary = self.get_resolution_summary(workspace_id, run_id)
        if summary is None:
            raise WorkspaceError("Frozen effective data could not be verified")
        return summary

    def get_current_effective_dataset(self, workspace_id: str) -> EffectiveDataset | None:
        database_path = self.workspace_directory(workspace_id) / "workspace-engine.duckdb"
        with self._connect(database_path) as connection:
            self._ensure_workspace_database_schema(connection)
            header = connection.execute(
                """
                SELECT run.workspace_id, run.staging_content_hash, run.policy_hash,
                       run.evaluation_hash, run.decisions_hash,
                       run.effective_content_hash, run.run_id
                  FROM effective_dataset_current AS current
                  JOIN resolution_run AS run ON run.run_id = current.run_id
                 WHERE current.singleton_id = 1 AND run.status = 'FROZEN'
                """
            ).fetchone()
            if header is None:
                return None
            run_id = str(header[6])
            stored_rows = connection.execute(
                """
                SELECT effective.effective_json, effective.state,
                       effective.canonical_row_id, canonical.row_json
                  FROM effective_row AS effective
                  JOIN resolution_run AS resolution
                    ON resolution.run_id = effective.run_id
                  LEFT JOIN canonical_staging_row AS canonical
                    ON canonical.run_id = resolution.staging_run_id
                   AND canonical.row_id = effective.canonical_row_id
                 WHERE effective.run_id = ?
                 ORDER BY effective.ordinal
                """,
                [run_id],
            ).fetchall()
            rows = tuple(_restore_effective_row(item) for item in stored_rows)
            accounting = tuple(
                ResolutionRowAccounting.from_dict(json.loads(str(item[0])))
                for item in connection.execute(
                    "SELECT accounting_json FROM resolution_accounting WHERE run_id = ? ORDER BY ordinal",
                    [run_id],
                ).fetchall()
            )
            reconciliation_row = connection.execute(
                "SELECT reconciliation_json FROM effective_dataset_reconciliation WHERE run_id = ?",
                [run_id],
            ).fetchone()
        if reconciliation_row is None:
            raise WorkspaceError("Effective dataset reconciliation is missing")
        result = EffectiveDataset(
            workspace_id=str(header[0]),
            staging_content_hash=str(header[1]),
            policy_hash=str(header[2]),
            evaluation_hash=str(header[3]),
            decisions_hash=str(header[4]),
            rows=rows,
            accounting=accounting,
            reconciliation=ResolutionReconciliation.from_dict(
                json.loads(str(reconciliation_row[0]))
            ),
        )
        if result.content_hash != str(header[5]):
            raise WorkspaceError("Effective dataset content hash is invalid")
        return result

    def get_resolution_evaluation(
        self,
        workspace_id: str,
        run_id: str,
    ) -> ResolutionEvaluation | None:
        database_path = self.workspace_directory(workspace_id) / "workspace-engine.duckdb"
        with self._connect(database_path) as connection:
            self._ensure_workspace_database_schema(connection)
            header = connection.execute(
                """
                SELECT workspace_id, staging_content_hash, policy_hash,
                       compared_pair_count, scorer_version, contract_version,
                       evaluation_hash
                  FROM resolution_run WHERE run_id = ?
                """,
                [run_id],
            ).fetchone()
            if header is None:
                return None
            candidates = tuple(
                ResolutionCandidate.from_dict(json.loads(str(item[0])))
                for item in connection.execute(
                    "SELECT candidate_json FROM resolution_candidate WHERE run_id = ? ORDER BY ordinal",
                    [run_id],
                ).fetchall()
            )
            findings = tuple(
                ResolutionFinding.from_dict(json.loads(str(item[0])))
                for item in connection.execute(
                    "SELECT finding_json FROM resolution_finding WHERE run_id = ? ORDER BY ordinal",
                    [run_id],
                ).fetchall()
            )
        evaluation = ResolutionEvaluation(
            workspace_id=str(header[0]),
            staging_content_hash=str(header[1]),
            policy_hash=str(header[2]),
            compared_pair_count=int(header[3]),
            scorer_version=int(header[4]),
            contract_version=int(header[5]),
            candidates=candidates,
            findings=findings,
        )
        if evaluation.content_hash != str(header[6]):
            raise WorkspaceError("Resolution evaluation content hash is invalid")
        return evaluation

    def get_resolution_summary(
        self,
        workspace_id: str,
        run_id: str,
    ) -> ResolutionRunSummary | None:
        database_path = self.workspace_directory(workspace_id) / "workspace-engine.duckdb"
        with self._connect(database_path) as connection:
            self._ensure_workspace_database_schema(connection)
            row = connection.execute(
                """
                SELECT run.run_id, run.staging_run_id, run.evaluation_hash,
                       run.staging_content_hash, run.policy_hash,
                       run.status, run.lifecycle_version,
                       (SELECT COUNT(*) FROM resolution_candidate c WHERE c.run_id = run.run_id),
                       (SELECT COUNT(*) FROM resolution_finding f WHERE f.run_id = run.run_id),
                       (SELECT COUNT(*) FROM resolution_decision d WHERE d.run_id = run.run_id),
                       run.effective_content_hash
                  FROM resolution_run AS run
                 WHERE run.run_id = ?
                """,
                [run_id],
            ).fetchone()
        if row is None:
            return None
        return ResolutionRunSummary(
            run_id=str(row[0]),
            staging_run_id=str(row[1]),
            evaluation_hash=str(row[2]),
            staging_content_hash=str(row[3]),
            policy_hash=str(row[4]),
            status=str(row[5]),
            lifecycle_version=int(row[6]),
            candidate_count=int(row[7]),
            finding_count=int(row[8]),
            decision_count=int(row[9]),
            effective_content_hash=(str(row[10]) if row[10] is not None else None),
        )

    def get_current_resolution_summary(
        self,
        workspace_id: str,
    ) -> ResolutionRunSummary | None:
        database_path = self.workspace_directory(workspace_id) / "workspace-engine.duckdb"
        with self._connect(database_path) as connection:
            self._ensure_workspace_database_schema(connection)
            row = connection.execute(
                "SELECT run_id FROM resolution_current WHERE singleton_id = 1"
            ).fetchone()
        return (
            self.get_resolution_summary(workspace_id, str(row[0]))
            if row is not None
            else None
        )

    @staticmethod
    def _insert_candidates(connection, run_id: str, items) -> None:
        for start in range(0, len(items), RESOLUTION_ROW_BATCH_SIZE):
            batch = items[start : start + RESOLUTION_ROW_BATCH_SIZE]
            connection.executemany(
                "INSERT INTO resolution_candidate VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        run_id,
                        start + offset,
                        item.candidate_id,
                        item.dataset,
                        item.left_row_id,
                        item.right_row_id,
                        item.score,
                        canonical_json(item.to_portable_dict()),
                    )
                    for offset, item in enumerate(batch)
                ],
            )

    @staticmethod
    def _insert_findings(connection, run_id: str, items) -> None:
        for start in range(0, len(items), RESOLUTION_ROW_BATCH_SIZE):
            batch = items[start : start + RESOLUTION_ROW_BATCH_SIZE]
            connection.executemany(
                "INSERT INTO resolution_finding VALUES (?, ?, ?, ?, ?)",
                [
                    (
                        run_id,
                        start + offset,
                        item.finding_id,
                        item.blocking,
                        canonical_json(item.to_portable_dict()),
                    )
                    for offset, item in enumerate(batch)
                ],
            )

    @staticmethod
    def _insert_effective_rows(connection, run_id: str, items) -> None:
        for start in range(0, len(items), RESOLUTION_ROW_BATCH_SIZE):
            batch = items[start : start + RESOLUTION_ROW_BATCH_SIZE]
            connection.executemany(
                """
                INSERT INTO effective_row (
                    run_id, ordinal, row_id, dataset, state,
                    effective_json, canonical_row_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        run_id,
                        start + offset,
                        item.row_id,
                        item.canonical_row.dataset,
                        item.state.value,
                        (
                            "null"
                            if _is_canonical_reference(item)
                            else canonical_json(item.to_portable_dict())
                        ),
                        item.row_id if _is_canonical_reference(item) else None,
                    )
                    for offset, item in enumerate(batch)
                ],
            )

    @staticmethod
    def _insert_accounting(connection, run_id: str, items) -> None:
        for start in range(0, len(items), RESOLUTION_ROW_BATCH_SIZE):
            batch = items[start : start + RESOLUTION_ROW_BATCH_SIZE]
            connection.executemany(
                "INSERT INTO resolution_accounting VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (
                        run_id,
                        start + offset,
                        item.source_row_id,
                        item.effective_row_id,
                        item.state.value,
                        canonical_json(item.to_portable_dict()),
                    )
                    for offset, item in enumerate(batch)
                ],
            )


def _require(actor: Actor, capability: Capability) -> None:
    if not actor.has(capability):
        raise AuthorizationError(
            f"{actor.identity.display_name} lacks {capability.value}"
        )


def _is_canonical_reference(item: EffectiveRow) -> bool:
    """Whether an unchanged row can reuse its immutable staging payload."""

    return (
        item.contributing_row_ids == (item.row_id,)
        and item.state in {
            ResolutionState.PASSED_THROUGH,
            ResolutionState.KEPT_DISTINCT,
        }
        and all(provenance.kind.value == "COPIED" for provenance in item.field_provenance)
    )


def _restore_effective_row(record) -> EffectiveRow:
    payload, state_value, canonical_row_id, canonical_json_value = record
    if canonical_row_id is None:
        return EffectiveRow.from_dict(json.loads(str(payload)))
    if str(payload) != "null" or canonical_json_value is None:
        raise WorkspaceError("Effective canonical-row reference is invalid")
    from ...staging_contracts import CanonicalRow

    canonical = CanonicalRow.from_dict(json.loads(str(canonical_json_value)))
    if canonical.row_id != str(canonical_row_id):
        raise WorkspaceError("Effective canonical-row binding is invalid")
    effective = pass_through_effective_row(canonical)
    state = ResolutionState(str(state_value))
    if state is ResolutionState.PASSED_THROUGH:
        return effective
    if state is not ResolutionState.KEPT_DISTINCT:
        raise WorkspaceError("Effective canonical-row state is invalid")
    return EffectiveRow(
        canonical_row=effective.canonical_row,
        contributing_row_ids=effective.contributing_row_ids,
        state=state,
        field_provenance=effective.field_provenance,
    )
