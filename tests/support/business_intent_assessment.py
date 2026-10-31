"""Explicit business expectation evidence for isolated load fixtures."""

from datetime import UTC, datetime
from uuid import uuid4

from impodo.domain.workspace.business_intent import (
    BusinessIntent, BusinessIntentBinding, BusinessIntentRevision, BusinessModelIntent,
)
from impodo.domain.workspace.business_intent_assessment import (
    assess_business_intent, observe_business_writes,
)
from tests.support.business_intent import ACTOR


def revision_for(snapshot, *, intent=None, actor=ACTOR, version=1):
    return BusinessIntentRevision(
        snapshot.workspace_id, version, str(uuid4()),
        intent or BusinessIntent(tuple(BusinessModelIntent(model, "upsert")
            for model in sorted({item.target_model for item in snapshot.datasets}))),
        BusinessIntentBinding(snapshot.mapping_content_hash, *("sha256:" + "1" * 64 for _ in range(5))),
        actor.identity, datetime.now(UTC),
    )


def observation_for(snapshot, *, reduced_scope_hash=""):
    operations = tuple(sorted({(row.dataset, effect.field, effect.relation_operation)
        for row in snapshot.rows for effect in row.fields if effect.kind == "relation"}))
    return observe_business_writes(snapshot, authored_operations=operations, reduced_scope_hash=reduced_scope_hash)


def assessment_for(snapshot, *, intent=None):
    return assess_business_intent(observation_for(snapshot), revision_for(snapshot, intent=intent))


def seed_journal_assessment(connection, run, *, actor):
    """Arrange explicit metadata-only expectations for journal adapter tests."""
    from types import SimpleNamespace
    from impodo.adapters.duckdb.business_intent_assessment_repository import publish_business_observation
    from impodo.domain.workspace.business_intent_assessment import BusinessWriteObservation, ObservedModelWrites

    models = sorted({row.target_model for row in run.rows})
    snapshot = SimpleNamespace(workspace_id=run.workspace_id, mapping_content_hash="sha256:" + "1" * 64,
        datasets=tuple(SimpleNamespace(target_model=model) for model in models))
    if connection.execute("SELECT count(*) FROM business_intent_revision").fetchone()[0] == 0:
        revision = revision_for(snapshot, actor=actor)
        connection.execute("INSERT INTO business_intent_revision VALUES (?, ?, ?, ?, ?)",
            [revision.version, revision.operation_id, revision.request_hash, revision.content_hash, revision.to_json()])
    creates = sum(row.operation == "CREATE" for row in run.rows)
    updates = sum(row.operation == "UPDATE" for row in run.rows)
    connection.execute("""
        INSERT OR REPLACE INTO preflight_execution_projection VALUES (
            ?, ?, ?, 'READY', ?, ?, 0, 0, 0, 0, ?, '19.0', '', '', '', '', TRUE, 1, '{}'
        )
    """, [run.preflight_run_id, run.snapshot_hash, run.snapshot_root_hash, creates, updates, run.target_hash])
    observation = BusinessWriteObservation(run.workspace_id, run.preflight_run_id, run.snapshot_hash,
        snapshot.mapping_content_hash, "sha256:" + "1" * 64, "sha256:" + "1" * 64,
        run.target_hash, "", tuple(ObservedModelWrites(model,
            sum(row.operation == "CREATE" and row.target_model == model for row in run.rows),
            sum(row.operation == "UPDATE" and row.target_model == model for row in run.rows),
            (), (), (), ()) for model in models), ())
    return publish_business_observation(connection, observation, publish_review=False).content_hash
