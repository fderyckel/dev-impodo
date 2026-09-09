"""Read a bounded, mutually current preparation publication chain."""

import duckdb

from impodo.application.workspace.preparation.recovery import PreparationRecoveryInputs, RecoveredPreparation
from impodo.application.workspace.preparation.job_models import PreparationJobStatus
from impodo.domain.preparation.staging_contracts import STAGING_CONTRACT_VERSION, BROWSER_EVALUATOR_VERSION
from impodo.domain.preparation.quality import QUALITY_CONTRACT_VERSION, QUALITY_EVALUATOR_VERSION
from impodo.domain.preparation.normalization import NORMALIZATION_CONTRACT_VERSION, NORMALIZATION_EVALUATOR_VERSION
from impodo.domain.resolution import RESOLUTION_EVALUATION_CONTRACT_VERSION, RESOLUTION_SCORER_VERSION
from impodo.domain.workspace.errors import WorkspaceError
from .repository import DuckDbRepository


class PreparationRecoveryRepository(DuckDbRepository):
    """Inspect publication headers and pointers without loading row evidence."""

    def read_current(self, workspace_id: str, inputs: PreparationRecoveryInputs) -> RecoveredPreparation | None:
        path = self.workspace_directory(workspace_id) / "workspace-engine.duckdb"
        try:
            with self._connect(path) as connection:
                self._ensure_workspace_database_schema(connection)
                row = connection.execute(_CURRENT_RESULT, {
                    "mapping_hash": inputs.mapping_hash,
                    "physical_hash": inputs.physical_selection_hash,
                    "source_hash": inputs.source_selection_hash,
                    "schema_hash": inputs.schema_hash,
                    "retention_hash": inputs.retention_context_hash,
                    "staging_contract": STAGING_CONTRACT_VERSION,
                    "staging_evaluator": BROWSER_EVALUATOR_VERSION,
                    "quality_contract": QUALITY_CONTRACT_VERSION,
                    "quality_evaluator": QUALITY_EVALUATOR_VERSION,
                    "normalization_contract": NORMALIZATION_CONTRACT_VERSION,
                    "normalization_evaluator": NORMALIZATION_EVALUATOR_VERSION,
                    "resolution_contract": RESOLUTION_EVALUATION_CONTRACT_VERSION,
                    "resolution_scorer": RESOLUTION_SCORER_VERSION,
                }).fetchone()
        except duckdb.IOException as error:
            raise WorkspaceError("Saved preparation is temporarily unavailable. Return to the run and try again.") from error
        if row is None:
            return None
        return RecoveredPreparation(
            inputs.mapping_hash, str(row[0]), str(row[1]), PreparationJobStatus(str(row[2])),
        )


_CURRENT_RESULT = """
WITH staging AS (
    SELECT s.*, session.status AS session_status
      FROM canonical_staging_current pointer
      JOIN canonical_staging_run s ON s.run_id = pointer.run_id
      JOIN mapping_current mc ON mc.singleton_id = 1
      JOIN mapping_revision m ON m.mapping_id = mc.mapping_id AND m.version = mc.version
      LEFT JOIN preparation_session session ON session.session_id = s.run_id
     WHERE pointer.singleton_id = 1 AND s.status = 'PUBLISHED'
       AND m.content_hash = $mapping_hash AND s.mapping_hash = m.content_hash
       AND s.mapping_id = m.mapping_id AND s.mapping_version = m.version
       AND s.physical_selection_hash = $physical_hash
       AND s.source_selection_hash = $source_hash AND m.source_selection_hash = $source_hash
       AND s.schema_hash = $schema_hash AND m.schema_hash = $schema_hash
       AND s.contract_version = $staging_contract AND s.evaluator_version = $staging_evaluator
       AND s.derived_plan_hash IS NOT DISTINCT FROM (
           SELECT plan.content_hash FROM derived_entity_plan_current pc
           JOIN derived_entity_plan_revision plan ON plan.plan_id = pc.plan_id AND plan.version = pc.version
           WHERE pc.singleton_id = 1
       )
       AND EXISTS (SELECT 1 FROM mapping_submission submitted
                   WHERE submitted.mapping_id = m.mapping_id AND submitted.version = m.version
                     AND submitted.content_hash = m.content_hash)
), resolution AS (
    SELECT r.* FROM resolution_current rc
      JOIN resolution_run r ON r.run_id = rc.run_id
      JOIN staging s ON s.run_id = r.staging_run_id AND s.content_hash = r.staging_content_hash
      JOIN resolution_policy_current pc ON pc.singleton_id = 1
      JOIN resolution_policy_revision policy ON policy.policy_id = pc.policy_id AND policy.version = pc.version
     WHERE rc.singleton_id = 1 AND r.policy_hash = policy.content_hash
       AND r.contract_version = $resolution_contract AND r.scorer_version = $resolution_scorer
), results AS (
    SELECT s.run_id AS staging_run_id, r.run_id AS result_run_id, 'REVIEW_REQUIRED' AS status, 0 AS priority
      FROM staging s JOIN resolution r ON r.staging_run_id = s.run_id
     WHERE r.status = 'REVIEW_REQUIRED'
       AND EXISTS (SELECT 1 FROM resolution_candidate candidate WHERE candidate.run_id = r.run_id)
    UNION ALL
    SELECT s.run_id, n.run_id, 'SUCCEEDED', 1
      FROM staging s
      JOIN quality_current qc ON qc.singleton_id = 1
      JOIN quality_run q ON q.run_id = qc.run_id AND q.staging_run_id = s.run_id AND q.staging_content_hash = s.content_hash
      JOIN quality_ruleset_current rules ON rules.singleton_id = 1
      JOIN quality_ruleset_revision rule ON rule.ruleset_id = rules.ruleset_id AND rule.version = rules.version
      JOIN normalization_current nc ON nc.singleton_id = 1
      JOIN normalization_run n ON n.run_id = nc.run_id
     WHERE (s.session_status IS NULL OR s.session_status = 'PUBLISHED')
       AND q.status = 'PUBLISHED' AND q.ruleset_hash = rule.content_hash
       AND rule.mapping_hash = s.mapping_hash AND rule.schema_hash = s.schema_hash
       AND q.mapping_hash = s.mapping_hash AND q.schema_hash = s.schema_hash
       AND q.retention_context_hash = $retention_hash
       AND q.contract_version = $quality_contract AND q.evaluator_version = $quality_evaluator
       AND n.status IN ('REVIEW_REQUIRED', 'BLOCKED', 'APPROVED', 'FROZEN')
       AND n.staging_run_id = s.run_id AND n.staging_content_hash = s.content_hash
       AND n.quality_run_id = q.run_id AND n.quality_content_hash = q.content_hash
       AND n.mapping_hash = s.mapping_hash AND n.schema_hash = s.schema_hash
       AND n.retention_context_hash = $retention_hash
       AND n.contract_version = $normalization_contract AND n.evaluator_version = $normalization_evaluator
       AND n.effective_dataset_run_id IS NOT DISTINCT FROM q.effective_dataset_run_id
       AND n.effective_dataset_hash IS NOT DISTINCT FROM q.effective_dataset_hash
       AND ((q.effective_dataset_run_id IS NULL AND NOT EXISTS (SELECT 1 FROM resolution_policy_current))
            OR EXISTS (SELECT 1 FROM resolution r WHERE r.run_id = q.effective_dataset_run_id
                       AND r.status = 'FROZEN' AND r.effective_content_hash = q.effective_dataset_hash))
)
SELECT staging_run_id, result_run_id, status FROM results ORDER BY priority LIMIT 1
"""
