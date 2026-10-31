"""Store business expectations separately from operational idempotency records.

The application owns authority and narrowing. This data-plane adapter owns CAS,
same-actor retries, input pointer rechecks and atomic revision/audit recording.
It never changes comparison approval or an already-started execution journal.
"""

from __future__ import annotations

import duckdb

from impodo.domain.shared.access import Actor
from impodo.domain.workspace.business_intent import (
    BusinessIntentBinding,
    BusinessIntentError,
    BusinessIntentRevision,
)

from .business_intent_assessment_repository import invalidate_business_review
from .repository import DuckDbRepository


class BusinessIntentRepository(DuckDbRepository):
    """Read one bounded immutable revision and publish within one transaction."""

    def get_business_intent(
        self, workspace_id: str, version: int | None = None
    ) -> BusinessIntentRevision | None:
        parameters: list[object] | None
        if version is None:
            query, parameters = (
                "SELECT revision_json FROM business_intent_revision ORDER BY version DESC LIMIT 1",
                None,
            )
        else:
            if type(version) is not int or version < 1:
                raise BusinessIntentError("Business intent version must be positive")
            query = "SELECT revision_json FROM business_intent_revision WHERE version = ? LIMIT 1"
            parameters = [version]
        rows = self._read_json_rows(workspace_id, query, parameters)
        result = BusinessIntentRevision.from_json(rows[0]) if rows else None
        if result and result.workspace_id != workspace_id:
            raise BusinessIntentError("Business intent belongs to another workspace")
        return result

    def save_business_intent(
        self,
        workspace_id: str,
        revision: BusinessIntentRevision,
        *,
        expected_version: int,
        actor: Actor,
    ) -> BusinessIntentRevision:
        self._assert_workspace_mutable(workspace_id)
        if type(expected_version) is not int or not 0 <= expected_version < 2**31 - 1:
            raise BusinessIntentError("Expected business intent version is invalid")
        if (
            revision.workspace_id != workspace_id
            or revision.version != expected_version + 1
        ):
            raise BusinessIntentError(
                "Business intent has a different owner or expected revision"
            )
        if revision.author != actor.identity:
            raise BusinessIntentError(
                "Business intent author does not match the verified Actor"
            )
        path = self.workspace_directory(workspace_id) / "workspace-engine.duckdb"
        with self._connect(path) as connection:
            self._ensure_workspace_database_schema(connection)
            connection.begin()
            try:
                prior = connection.execute(
                    "SELECT revision_json FROM business_intent_revision WHERE operation_id = ?",
                    [revision.operation_id],
                ).fetchone()
                if prior:
                    existing = BusinessIntentRevision.from_json(str(prior[0]))
                    if (
                        existing.workspace_id != workspace_id
                        or existing.request_hash != revision.request_hash
                        or (existing.author.issuer, existing.author.subject_id)
                        != (actor.identity.issuer, actor.identity.subject_id)
                    ):
                        raise BusinessIntentError(
                            "This business intent operation already records another request or author"
                        )
                    connection.rollback()
                    return existing
                current = connection.execute(
                    "SELECT version FROM business_intent_revision ORDER BY version DESC LIMIT 1"
                ).fetchone()
                if (int(current[0]) if current else 0) != expected_version:
                    raise BusinessIntentError(
                        "Business intent changed in another request; review its current revision"
                    )
                _require_current_inputs(connection, revision.binding)
                try:
                    connection.execute(
                        "INSERT INTO business_intent_revision VALUES (?, ?, ?, ?, ?)",
                        [
                            revision.version,
                            revision.operation_id,
                            revision.request_hash,
                            revision.content_hash,
                            revision.to_json(),
                        ],
                    )
                except (
                    duckdb.ConstraintException,
                    duckdb.TransactionException,
                ) as error:
                    # Both uniqueness keys are command identities. A racing
                    # publication must not become a second authoritative row.
                    raise BusinessIntentError(
                        "Business intent changed in another request; retry the exact operation or review its current revision"
                    ) from error
                self._insert_workspace_audit(
                    connection,
                    revision=self._workspace_revision(connection),
                    event_type="BUSINESS_INTENT_RECORDED",
                    detail=f"intent revision {revision.version}: {len(revision.intent.models)} record types",
                    actor=actor,
                )
                # Keep the observation for local reassessment, but retire the
                # approval candidate. Journal start touches this same row so
                # a racing edit cannot commit alongside a stale approval.
                try:
                    connection.execute("UPDATE business_intent_current SET assessment_hash = '' WHERE singleton_id = 1")
                    invalidate_business_review(connection)
                except duckdb.TransactionException as error:
                    raise BusinessIntentError("A load or business expectations changed; retry the exact operation or review the current revision") from error
            except Exception:
                connection.rollback()
                raise
            try:
                connection.commit()
            except (duckdb.ConstraintException, duckdb.TransactionException) as error:
                # DuckDB aborts a failed commit itself. A second rollback would
                # mask the publication conflict with "no transaction is active".
                raise BusinessIntentError(
                    "Business intent could not be published; retry the exact operation or review its current revision"
                ) from error
        return revision


def _require_current_inputs(connection, binding: BusinessIntentBinding) -> None:
    """Recheck current pointer hashes in the write transaction without decoding history."""
    mapping = connection.execute("""
        SELECT revision.mapping_id, revision.version, revision.content_hash,
               revision.source_selection_hash, revision.schema_hash
          FROM mapping_current AS current
          JOIN mapping_revision AS revision USING (mapping_id, version)
         WHERE current.singleton_id = 1
    """).fetchone()
    if mapping is None or tuple(str(value) for value in mapping[2:]) != (
        binding.mapping_hash,
        binding.source_selection_hash,
        binding.governance_hash or binding.schema_hash,
    ):
        raise BusinessIntentError(
            "The checked mapping changed while saving business intent"
        )
    validation = connection.execute(
        """
        SELECT validation_hash FROM mapping_validation
         WHERE mapping_id = ? AND version = ?
         ORDER BY created_at DESC, validation_hash LIMIT 1
    """,
        [mapping[0], mapping[1]],
    ).fetchone()
    catalog = connection.execute(
        "SELECT json_extract_string(catalog_json, '$.content_hash') FROM odoo_schema_catalog WHERE singleton_id = 1"
    ).fetchone()
    governance = connection.execute("""
        SELECT revision.content_hash FROM schema_governance_current AS current
          JOIN schema_governance_revision AS revision USING (governance_id, version)
         WHERE current.singleton_id = 1
    """).fetchone()
    if (
        (str(validation[0]) if validation else None) != binding.validation_hash
        or (str(catalog[0]) if catalog else None) != binding.schema_hash
        or (str(governance[0]) if governance else None) != binding.governance_hash
    ):
        raise BusinessIntentError(
            "The checked Odoo details or validation changed while saving business intent"
        )
