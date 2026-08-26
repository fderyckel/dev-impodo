"""Materialize and stream durable normalization facts and impacts."""

from __future__ import annotations

import json
from collections.abc import Iterator
from hashlib import sha256

from ...domain.staging.preparation_session import (
    PreparationSessionStatus,
    transformation_impact_from_portable_dict,
)
from ...domain.staging.transformation_impact import (
    TransformationImpactRow,
)
from impodo.domain.preparation.normalization import NormalizationEffect
from impodo.domain.preparation.quality import QualityIssue
from impodo.domain.workspace.errors import WorkspaceError
from .constants import (
    PREPARATION_SESSION_ROW_BATCH_SIZE,
)
from .preparation_session_support import (
    _SessionImpacts,
)
from .serialization import (
    _canonical_json,
    _columnar_parameters,
)


class PreparationNormalizationRecords:
    def iter_impacts(
        self,
        workspace_id: str,
        session_id: str,
    ) -> _SessionImpacts:
        """Return a replayable bounded view of persisted transformation impacts."""

        return _SessionImpacts(self, workspace_id, self._session_id(session_id))

    def _iter_impacts(
        self,
        workspace_id: str,
        session_id: str,
    ) -> Iterator[TransformationImpactRow]:
        """Yield persisted impacts in deterministic bounded ordinal pages."""

        database_path = (
            self.workspace_directory(workspace_id) / "workspace-engine.duckdb"
        )
        with self._connect(database_path) as connection:
            self._ensure_workspace_database_schema(connection)
            next_ordinal = 0
            while batch := connection.execute(
                """
                SELECT ordinal, impact_json
                  FROM preparation_impact_row
                 WHERE session_id = ?
                   AND ordinal >= ?
                 ORDER BY ordinal
                 LIMIT ?
                """,
                [
                    self._session_id(session_id),
                    next_ordinal,
                    PREPARATION_SESSION_ROW_BATCH_SIZE,
                ],
            ).fetchall():
                for ordinal, row_text in batch:
                    if int(ordinal) != next_ordinal:
                        raise WorkspaceError(
                            "Stored preparation impacts are not contiguous"
                        )
                    next_ordinal += 1
                    try:
                        yield transformation_impact_from_portable_dict(
                            json.loads(str(row_text))
                        )
                    except (TypeError, ValueError) as error:
                        raise WorkspaceError(
                            "Stored preparation impact is invalid"
                        ) from error

    def _prepare_normalization_facts(
        self,
        workspace_id: str,
        session_id: str,
        *,
        effect_builder,
        finding_builder,
    ) -> dict[str, object]:
        """Construct every effect once and summarize the durable fact ledger."""

        database_path = (
            self.workspace_directory(workspace_id) / "workspace-engine.duckdb"
        )
        canonical_session_id = self._session_id(session_id)
        with self._connect(database_path) as connection:
            self._ensure_workspace_database_schema(connection)
            connection.begin()
            try:
                self._require_status(
                    connection,
                    canonical_session_id,
                    PreparationSessionStatus.READY,
                )
                quality_binding = connection.execute(
                    """
                    SELECT quality.run_id,
                           projection.run_id IS NOT NULL
                      FROM quality_current AS current
                      JOIN quality_run AS quality
                        ON quality.run_id = current.run_id
                       AND quality.status = 'PUBLISHED'
                      LEFT JOIN quality_evidence_projection AS projection
                        ON projection.run_id = quality.run_id
                     WHERE current.singleton_id = 1
                       AND quality.staging_content_hash = (
                           SELECT content_hash
                             FROM canonical_staging_run
                            WHERE run_id = ?
                       )
                    """,
                    [canonical_session_id],
                ).fetchone()
                if quality_binding is None:
                    raise WorkspaceError(
                        "Prepared changes do not match the current data checks"
                    )
                quality_run_id = str(quality_binding[0])
                sparse_quality = bool(quality_binding[1])
                connection.execute(
                    """
                    DELETE FROM normalization_effect
                     WHERE run_id = ?
                       AND NOT EXISTS (
                           SELECT 1 FROM normalization_run WHERE run_id = ?
                       )
                    """,
                    [canonical_session_id, canonical_session_id],
                )
                connection.execute(
                    "DELETE FROM preparation_normalization_group_seed WHERE session_id = ?",
                    [canonical_session_id],
                )
                connection.execute(
                    "DELETE FROM preparation_normalization_finding WHERE session_id = ?",
                    [canonical_session_id],
                )
                connection.execute(
                    """
                    CREATE TEMP TABLE normalization_pending_effect (
                        run_id VARCHAR NOT NULL,
                        ordinal BIGINT NOT NULL,
                        effect_id VARCHAR PRIMARY KEY,
                        group_id VARCHAR NOT NULL,
                        row_id VARCHAR NOT NULL,
                        dataset VARCHAR NOT NULL,
                        source_row BIGINT NOT NULL,
                        target_field VARCHAR NOT NULL,
                        eligible BOOLEAN NOT NULL,
                        effect_json VARCHAR NOT NULL
                    )
                    """
                )
                construction_ordinal = 0
                pending_effects: list[list[object]] = []
                pending_group_seeds: list[list[object]] = []
                bound_rows = self._iter_bound_impacts_with_eligibility(
                    connection,
                    canonical_session_id,
                    quality_run_id,
                    sparse_quality=sparse_quality,
                    batch_size=PREPARATION_SESSION_ROW_BATCH_SIZE,
                )
                for effect, metadata in effect_builder(bound_rows):
                    kind = getattr(metadata["kind"], "value", metadata["kind"])
                    outcome = getattr(
                        metadata["outcome"],
                        "value",
                        metadata["outcome"],
                    )
                    metadata_values = {
                        "dataset": str(metadata["dataset"]),
                        "explanation": str(metadata["explanation"]),
                        "kind": str(kind),
                        "name": str(metadata["name"]),
                        "outcome": str(outcome),
                        "owner_label": str(metadata["owner_label"]),
                        "rule_id": str(metadata["rule_id"]),
                        "target_field": str(metadata["target_field"]),
                    }
                    metadata_hash = (
                        "sha256:"
                        + sha256(
                            _canonical_json(metadata_values).encode("utf-8")
                        ).hexdigest()
                    )
                    pending_effects.append(
                        [
                            canonical_session_id,
                            construction_ordinal,
                            effect.effect_id,
                            effect.group_id,
                            effect.row_id,
                            effect.dataset,
                            effect.source_row,
                            effect.target_field,
                            effect.eligible,
                            _canonical_json(effect.to_portable_dict()),
                        ]
                    )
                    pending_group_seeds.append(
                        [
                            canonical_session_id,
                            effect.group_id,
                            metadata_hash,
                            metadata_values["dataset"],
                            metadata_values["target_field"],
                            metadata_values["rule_id"],
                            metadata_values["kind"],
                            metadata_values["outcome"],
                            metadata_values["name"],
                            metadata_values["explanation"],
                            metadata_values["owner_label"],
                        ]
                    )
                    construction_ordinal += 1
                    if len(pending_effects) >= PREPARATION_SESSION_ROW_BATCH_SIZE:
                        self._insert_prepared_normalization_effects(
                            connection,
                            pending_effects,
                        )
                        self._insert_prepared_normalization_group_seeds(
                            connection,
                            pending_group_seeds,
                        )
                        pending_effects.clear()
                        pending_group_seeds.clear()
                if pending_effects:
                    self._insert_prepared_normalization_effects(
                        connection,
                        pending_effects,
                    )
                    self._insert_prepared_normalization_group_seeds(
                        connection,
                        pending_group_seeds,
                    )

                pending_findings: list[list[object]] = []
                warning_issues = self._iter_eligible_warning_issues(
                    connection,
                    canonical_session_id,
                    quality_run_id,
                    sparse_quality=sparse_quality,
                    batch_size=PREPARATION_SESSION_ROW_BATCH_SIZE,
                )
                for group_id, issue_id, row_id, metadata in finding_builder(
                    warning_issues
                ):
                    kind = getattr(metadata["kind"], "value", metadata["kind"])
                    outcome = getattr(
                        metadata["outcome"],
                        "value",
                        metadata["outcome"],
                    )
                    pending_findings.append(
                        [
                            canonical_session_id,
                            issue_id,
                            group_id,
                            row_id,
                            str(metadata["rule_id"]),
                            str(kind),
                            str(outcome),
                            str(metadata["dataset"]),
                            str(metadata["target_field"]),
                            str(metadata["name"]),
                            str(metadata["explanation"]),
                            str(metadata["owner_label"]),
                        ]
                    )
                    if len(pending_findings) >= PREPARATION_SESSION_ROW_BATCH_SIZE:
                        self._insert_prepared_normalization_findings(
                            connection,
                            pending_findings,
                        )
                        pending_findings.clear()
                if pending_findings:
                    self._insert_prepared_normalization_findings(
                        connection,
                        pending_findings,
                    )

                conflict = connection.execute(
                    """
                    SELECT group_id
                      FROM preparation_normalization_group_seed
                     WHERE session_id = ?
                     GROUP BY group_id
                    HAVING COUNT(*) > 1
                     LIMIT 1
                    """,
                    [canonical_session_id],
                ).fetchone()
                if conflict is not None:
                    raise WorkspaceError(
                        "Prepared normalization group metadata is inconsistent"
                    )

                totals = connection.execute(
                    """
                    SELECT COUNT(*),
                           COUNT(DISTINCT row_id) FILTER (WHERE eligible),
                           (
                               SELECT COUNT(DISTINCT rule_id)
                                 FROM preparation_normalization_group_seed
                                WHERE session_id = ?
                           ),
                           COUNT(DISTINCT group_id),
                           COUNT(DISTINCT (dataset, source_row))
                      FROM normalization_pending_effect
                     WHERE run_id = ?
                    """,
                    [canonical_session_id, canonical_session_id],
                ).fetchone()
                effect_groups = connection.execute(
                    """
                    SELECT seed.group_id, seed.rule_id, seed.kind,
                           seed.outcome, seed.dataset, seed.target_field,
                           seed.name, seed.explanation, seed.owner_label,
                           COUNT(*) FILTER (WHERE effect.eligible),
                           COUNT(*) FILTER (WHERE NOT effect.eligible)
                      FROM preparation_normalization_group_seed AS seed
                      JOIN normalization_pending_effect AS effect
                        ON effect.run_id = seed.session_id
                       AND effect.group_id = seed.group_id
                     WHERE seed.session_id = ?
                     GROUP BY seed.group_id, seed.rule_id, seed.kind,
                              seed.outcome, seed.dataset, seed.target_field,
                              seed.name, seed.explanation, seed.owner_label
                     ORDER BY seed.group_id
                    """,
                    [canonical_session_id],
                ).fetchall()
                example_rows = connection.execute(
                    """
                    WITH top_effect AS (
                        SELECT run_id, group_id,
                               UNNEST(
                                   arg_min(
                                       effect_id,
                                       struct_pack(
                                           source_row := source_row,
                                           row_id := row_id,
                                           effect_id := effect_id
                                       ),
                                       5
                                   )
                               ) AS effect_id
                          FROM normalization_pending_effect
                         WHERE run_id = ? AND eligible
                         GROUP BY run_id, group_id
                    )
                    SELECT effect.group_id, effect.source_row,
                           json_extract_string(effect.effect_json, '$.before'),
                           json_extract_string(effect.effect_json, '$.after')
                      FROM top_effect
                      JOIN normalization_pending_effect AS effect
                        ON effect.run_id = top_effect.run_id
                       AND effect.group_id = top_effect.group_id
                       AND effect.effect_id = top_effect.effect_id
                     ORDER BY effect.group_id, effect.source_row,
                              effect.row_id, effect.effect_id
                    """,
                    [canonical_session_id],
                ).fetchall()
                finding_groups = connection.execute(
                    """
                    SELECT group_id,
                           arg_min(rule_id, issue_id),
                           arg_min(kind, issue_id),
                           arg_min(outcome, issue_id),
                           arg_min(dataset, issue_id),
                           arg_min(target_field, issue_id),
                           arg_min(name, issue_id),
                           arg_min(explanation, issue_id),
                           arg_min(owner_label, issue_id),
                           COUNT(*)
                      FROM preparation_normalization_finding
                     WHERE session_id = ?
                     GROUP BY group_id
                     ORDER BY group_id
                    """,
                    [canonical_session_id],
                ).fetchall()
                connection.execute(
                    """
                    INSERT INTO normalization_effect
                    SELECT run_id, ordinal, effect_id, group_id, row_id,
                           dataset, source_row, target_field, eligible,
                           effect_json
                      FROM normalization_pending_effect
                    """
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

        examples: dict[str, list[tuple[int, str, str]]] = {}
        for group_id, source_row, before, after in example_rows:
            examples.setdefault(str(group_id), []).append(
                (int(source_row), str(before), str(after))
            )
        return {
            "effect_count": int(totals[0]),
            "changed_record_count": int(totals[1]),
            "distinct_rule_count": int(totals[2]),
            "distinct_group_count": int(totals[3]),
            "distinct_source_count": int(totals[4]),
            "effect_groups": tuple(effect_groups),
            "finding_groups": tuple(finding_groups),
            "examples": {
                group_id: tuple(items) for group_id, items in examples.items()
            },
        }

    @staticmethod
    def _insert_prepared_normalization_effects(connection, rows) -> None:
        connection.execute(
            """
            INSERT OR IGNORE INTO normalization_pending_effect
            SELECT
                CAST(unnest(?) AS VARCHAR), CAST(unnest(?) AS BIGINT),
                CAST(unnest(?) AS VARCHAR), CAST(unnest(?) AS VARCHAR),
                CAST(unnest(?) AS VARCHAR), CAST(unnest(?) AS VARCHAR),
                CAST(unnest(?) AS BIGINT), CAST(unnest(?) AS VARCHAR),
                CAST(unnest(?) AS BOOLEAN), CAST(unnest(?) AS VARCHAR)
            """,
            _columnar_parameters(rows),
        )

    @staticmethod
    def _insert_prepared_normalization_group_seeds(connection, rows) -> None:
        connection.execute(
            """
            INSERT OR IGNORE INTO preparation_normalization_group_seed
            SELECT
                CAST(unnest(?) AS VARCHAR), CAST(unnest(?) AS VARCHAR),
                CAST(unnest(?) AS VARCHAR), CAST(unnest(?) AS VARCHAR),
                CAST(unnest(?) AS VARCHAR), CAST(unnest(?) AS VARCHAR),
                CAST(unnest(?) AS VARCHAR), CAST(unnest(?) AS VARCHAR),
                CAST(unnest(?) AS VARCHAR), CAST(unnest(?) AS VARCHAR),
                CAST(unnest(?) AS VARCHAR)
            """,
            _columnar_parameters(rows),
        )

    @staticmethod
    def _insert_prepared_normalization_findings(connection, rows) -> None:
        connection.execute(
            """
            INSERT OR IGNORE INTO preparation_normalization_finding
            SELECT
                CAST(unnest(?) AS VARCHAR), CAST(unnest(?) AS VARCHAR),
                CAST(unnest(?) AS VARCHAR), CAST(unnest(?) AS VARCHAR),
                CAST(unnest(?) AS VARCHAR), CAST(unnest(?) AS VARCHAR),
                CAST(unnest(?) AS VARCHAR), CAST(unnest(?) AS VARCHAR),
                CAST(unnest(?) AS VARCHAR), CAST(unnest(?) AS VARCHAR),
                CAST(unnest(?) AS VARCHAR), CAST(unnest(?) AS VARCHAR)
            """,
            _columnar_parameters(rows),
        )

    def _iter_bound_impacts_with_eligibility(
        self,
        connection,
        session_id: str,
        quality_run_id: str,
        *,
        sparse_quality: bool,
        batch_size: int,
    ):
        """Yield impact, row ID, and eligibility from one set-based join."""

        if sparse_quality:
            disposition = (
                "COALESCE(exception.effective_disposition, canonical.disposition)"
            )
            quality_join = """
                LEFT JOIN quality_row_result AS exception
                  ON exception.run_id = ? AND exception.row_id = canonical.row_id
            """
        else:
            disposition = "quality.effective_disposition"
            quality_join = """
                JOIN quality_row_result AS quality
                  ON quality.run_id = ? AND quality.row_id = canonical.row_id
            """
        next_ordinal = 0
        while batch := connection.execute(
            f"""
            SELECT impact.ordinal, impact.impact_json, canonical.row_id,
                   {disposition} IN ('CANDIDATE', 'REFERENCE') AS eligible
              FROM preparation_impact_row AS impact
              JOIN canonical_staging_row AS canonical
                ON canonical.run_id = impact.session_id
               AND canonical.dataset = impact.dataset
               AND canonical.source_row = impact.source_row
              {quality_join}
             WHERE impact.session_id = ?
               AND impact.ordinal >= ?
             ORDER BY impact.ordinal
             LIMIT ?
            """,
            [quality_run_id, session_id, next_ordinal, batch_size],
        ).fetchall():
            for ordinal, row_text, row_id, eligible in batch:
                if int(ordinal) != next_ordinal:
                    raise WorkspaceError(
                        "Prepared changes do not match one canonical row"
                    )
                next_ordinal += 1
                try:
                    impact = transformation_impact_from_portable_dict(
                        json.loads(str(row_text))
                    )
                except (TypeError, ValueError) as error:
                    raise WorkspaceError(
                        "Stored preparation impact is invalid"
                    ) from error
                yield impact, str(row_id), bool(eligible)

    def _iter_eligible_warning_issues(
        self,
        connection,
        session_id: str,
        quality_run_id: str,
        *,
        sparse_quality: bool,
        batch_size: int,
    ):
        """Yield eligible row-level warnings without building a row-ID set."""

        if sparse_quality:
            disposition = (
                "COALESCE(exception.effective_disposition, canonical.disposition)"
            )
            quality_join = """
                LEFT JOIN quality_row_result AS exception
                  ON exception.run_id = ? AND exception.row_id = canonical.row_id
            """
        else:
            disposition = "quality.effective_disposition"
            quality_join = """
                JOIN quality_row_result AS quality
                  ON quality.run_id = ? AND quality.row_id = canonical.row_id
            """
        last_issue_id = ""
        while batch := connection.execute(
            f"""
            SELECT issue.issue_id, issue.issue_json
              FROM quality_issue AS issue
              JOIN canonical_staging_row AS canonical
                ON canonical.run_id = ? AND canonical.row_id = issue.row_id
              {quality_join}
             WHERE issue.run_id = ?
               AND issue.policy = 'WARNING'
               AND issue.row_id IS NOT NULL
               AND {disposition} IN ('CANDIDATE', 'REFERENCE')
               AND issue.issue_id > ?
             ORDER BY issue.issue_id
             LIMIT ?
            """,
            [
                session_id,
                quality_run_id,
                quality_run_id,
                last_issue_id,
                batch_size,
            ],
        ).fetchall():
            for issue_id, issue_json in batch:
                last_issue_id = str(issue_id)
                try:
                    yield QualityIssue.from_dict(json.loads(str(issue_json)))
                except (TypeError, ValueError) as error:
                    raise WorkspaceError("Stored quality finding is invalid") from error

    @staticmethod
    def _copy_normalization_effects_to_run(
        connection,
        session_id: str,
        run_id: str,
    ) -> int:
        """Verify publication reuses the already-constructed run facts."""

        if run_id != session_id:
            raise WorkspaceError(
                "Prepared normalization facts use a different run identifier"
            )
        return int(
            connection.execute(
                "SELECT COUNT(*) FROM normalization_effect WHERE run_id = ?",
                [run_id],
            ).fetchone()[0]
        )

    @staticmethod
    def _iter_normalization_effect_json_batches(
        connection,
        session_id: str,
        batch_size: int,
    ):
        cursor = connection.execute(
            """
            SELECT effect_json
              FROM normalization_effect
             WHERE run_id = ?
             ORDER BY effect_id
            """,
            [session_id],
        )
        while batch := cursor.fetchmany(batch_size):
            yield tuple(str(item[0]) for item in batch)

    def _iter_prepared_normalization_effects(
        self,
        workspace_id: str,
        session_id: str,
    ):
        database_path = (
            self.workspace_directory(workspace_id) / "workspace-engine.duckdb"
        )
        with self._connect(database_path) as connection:
            self._ensure_workspace_database_schema(connection)
            last_effect_id = ""
            while batch := connection.execute(
                """
                SELECT effect_id, effect_json
                  FROM normalization_effect
                 WHERE run_id = ? AND effect_id > ?
                 ORDER BY effect_id
                 LIMIT ?
                """,
                [session_id, last_effect_id, PREPARATION_SESSION_ROW_BATCH_SIZE],
            ).fetchall():
                for effect_id, effect_json in batch:
                    last_effect_id = str(effect_id)
                    try:
                        yield NormalizationEffect.from_dict(
                            json.loads(str(effect_json))
                        )
                    except (TypeError, ValueError) as error:
                        raise WorkspaceError(
                            "Stored normalization effect is invalid"
                        ) from error
