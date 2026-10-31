---
audience: developer
kind: contract
status: current
---

# Business intent contract

## Scope

`BusinessIntent` records explicit business expectations for a workspace. It
contains portable technical model and field names, write modes, relationship
operations, and optional create and update count ranges. It contains no source
values, target record identifiers, credentials, connection details, or approval.

The contract is version 1. A model rule chooses `none`, `create`, `update`, or
`upsert`. Unlisted models permit no writes. Scalar fields and relationship
operations have separate create and update permissions. An omitted permission
list adds no restriction to the checked technical scope; an empty list permits
none. An omitted count limit supplies no expectation; zero is an explicit
limit. Minimum and maximum counts must be nonnegative integers in order.

Relationship operations are the authored `replace`, `add`, and `remove`
semantics. They are distinct from the normalized commands eventually sent to
Odoo. Deterministic ordering and serialization give equivalent rules the same
content hash. An intent has at most 100 models, each permission list has at
most 1,000 fields, and its serialized body is limited to 1 MiB.

## Application boundary

`BusinessIntentService.inputs` requires `PROJECT_VIEW` and returns the current
technical ceiling and its binding hash. `save` requires `MAPPING_EDIT`, an
explicit intent, the expected current revision number, the reviewed binding
hash, and a UUID operation identity. Revision zero means no earlier intent.
The verified Actor supplies the persisted author; caller data cannot replace it.
`get` requires `PROJECT_VIEW` and reads either one exact historical revision or
the latest revision. Missing intent returns no revision.

The service checks one current mapping, effective source selection, captured
live schema, governance revision, and mapping validation. Their hashes must
agree. It derives the write ceiling once through the existing mapping compiler
and compatibility policy. Intent can narrow that ceiling only. Create-only
fields cannot become update fields; fields supplied by Odoo defaults cannot
become explicit writes. Pinned Odoo-source updates retain their approved
Tier 1 field policy. Run purpose comes from verified workspace access, and
final Odoo 20 Production remains disabled. An unsupported path grants no mode.

A separately configured Odoo-to-Odoo destination requires its destination-owned
scope. This command rejects that context rather than deriving destination
authority from a source schema.

## Persistence and concurrency

`BusinessIntentRevision` binds the intent, workspace, monotonic revision,
operation identity, verified author, UTC timestamp, and the checked mapping,
source, catalog, governance, validation, and scope hashes. Its content hash
protects the complete revision. The request hash binds meaning, inputs, and
expected parent revision so an exact retry can return its original receipt.

The DuckDB adapter appends to `business_intent_revision` and records
`BUSINESS_INTENT_RECORDED` in `audit_event` in one transaction. It compares the
expected revision and rechecks current mapping, validation, schema, and
governance headers before publication. Competing edits cannot publish two
revisions with the same version. A reused operation identity must retain the
same request and stable Actor identity. Failed publication rolls back both
revision and audit event.

Workspace-engine schema version 23 adds an empty table. Upgrading a historical
workspace creates no intent or approval and preserves existing evidence. Its
first intent requires an explicit authorized save at expected revision zero.

Detailed rules remain data-plane evidence, separate from the operational
`project_operation_intent` idempotency store. The deployment-neutral service
uses injected ports. Local composition supplies DuckDB; a server browser does
not open worker databases or copy the rules into PostgreSQL. Any server
projection is bounded state, revision, and a verified worker receipt hash.

## Database lifetime and execution boundary

The caller encloses local reads and publication in one bounded synchronous
operation. Each distinct database owner is opened once and closed before the
operation returns. Reads select one current or exact revision and bounded
metadata. Publication rechecks headers without decoding validation history.
There are no per-field repository calls, source row scans, Odoo calls, or
process-wide connection caches.

Saving intent preserves the current comparison, execution snapshot, and any
already-started journal. It retires the assessment candidate for the next load
while retaining its observation for local reassessment. Saving grants no write
permission.

## Assessment and enforcement

The file-source comparison builds `BusinessWriteObservation` while its exact
execution snapshot and compiled rules are in memory. The observation records
model counts, scalar fields, and authored relationship operations. It binds
the snapshot, prepared input, mapping, target, comparison, and any reviewed
reduced-scope decision. It contains no row values or Odoo identifiers.

Each business identity counts once per create or update phase. Supporting
creates count as writes. A relationship completed after creation belongs to
that create and adds no update count. Reusing a target reference adds no write
to the referenced model. Omitted fields add no effect. Authored add and remove
operations retain their meaning even when the writer uses a final replacement
payload.

The comparison transaction publishes the observation and an immutable
`BusinessIntentAssessment` against the current intent. Missing intent, a
different checked mapping, forbidden effects, or count deviations block loading.
Accepting reduced scope creates a fresh observation and assessment, including
minimum counts. Row confirmation cannot acknowledge a business deviation.

After an explicit intent edit, `PreflightService.reassess_business_intent`
requires `PREFLIGHT_RUN`, verifies the current saved snapshot, and reassesses
its bounded observation in one local operation. It performs no Odoo read and
does not evaluate source rows. Ordinary review reads return only the verified
assessment hash, readiness, and one blocker rather than the full deviation list.

The confirmation form carries the exact assessment hash. The application
rechecks it before enqueue and at worker entry before constructing the writer.
`ExecutionService` repeats the check before journal creation. The journal
transaction rechecks the current intent and assessment and records the Actor's
approval atomically with the new run. Intent editing and journal publication
touch the same current row, so a racing edit cannot commit alongside a stale
approval. A zero-write completion must satisfy the same count expectations.

An interrupted journal retains its original approved intent. Later edits
govern a later run; they cannot replace that approval during recovery. Recovery
still requires its exact read-back assessment and existing target and credential
checks. Workspace-engine version 25 preserves the authority of journals that
already existed before upgrade, including terminal history, without approving
an unstarted load or creating an intent automatically.

This enforcement covers the conventional file-source load boundary.
Odoo-to-Odoo transfers and completed-load corrections retain their separate
existing authorization contracts. This contract grants neither destination nor
correction authority.

## Verification

- [Portable contract tests](../../../tests/domain/workspace/test_business_intent.py)
- [Supported-scope tests](../../../tests/domain/workspace/test_business_intent_scope.py)
- [Authorized service tests](../../../tests/application/workspace/test_business_intent_service.py)
- [Persistence, migration, concurrency, and connection-budget tests](../../../tests/integration/duckdb/test_business_intent.py)
- [Observed effects and count assessment tests](../../../tests/domain/workspace/test_business_intent_assessment.py)
- [Comparison, stale confirmation, concurrency, and recovery qualification](../../../tests/integration/duckdb/test_business_intent_assessment.py)

## Related documentation

- [Load into Odoo developer workflow](../workflow/06-load-into-odoo.md)
- [Workflow evidence lifecycle](evidence-lifecycle.md)
- [Server fact ownership](../../architecture/server-fact-ownership.md)
