---
audience: developer
kind: report
status: current
---

# Scalable relationship planning Phase 4 recovery and reconciliation — 2026-08-28

## Decision supported by this report

Phase 4 adds deterministic recovery to the accepted relationship execution
design without introducing a generic workflow engine, a second recovery
aggregate, or a Product and bill-of-materials executor. The data manager keeps
control of the selected rows, business keys, mappings, and optional
relationships. Recovery can continue only the writes that the immutable
**Check changes** result already approved.

This result supports starting Phase 5. It does not add new browser recovery
controls, raise the related-data limit, qualify the 16,000-Product and
80,000-BOM-line shape, or authorize a Production load.

## Durable transport checkpoint

`ExecutionRowAttempt` now distinguishes `IN_FLIGHT` and `RETRY_READY` from the
existing planned and terminal states. It stores the frozen schedule component,
component page, global transport-batch number, and create, update, or
relationship-completion phase.

`ExecutionService` commits that exact batch immediately before every Odoo
transport call. It records the response in a second short transaction. A
process exit between those transactions therefore leaves one exact durable
question instead of an inferred progress percentage.

The checkpoint remains in the existing execution row JSON. This avoids a new
table and keeps the recovery evidence attached to the row journal that already
owns Odoo outcomes. `ExecutionRepository` validates the original row identity,
attempt number, component, phase, page, batch, and known update or completion
identifier on every transition.

## Read-back-gated recovery

`ReconciliationService.assess_recovery` reads a current `RUNNING` execution
without publishing the final reconciliation result. It checks the same target,
snapshot, API scope, and write identity as the original execution. It reads
known records by exact Odoo ID and searches an interrupted create by its
reviewed business key.

`ExecutionService.resume` accepts the resulting hash-bound report only when it
covers every write row. It classifies rows as follows:

- A previously committed row must still match every intended final field.
- An in-flight create becomes `COMMITTED` when it matches, `RETRY_READY` when
  no matching record exists, or `PARTIALLY_APPLIED` when only its frozen
  deferred relationship fields differ.
- An in-flight exact update may become `RETRY_READY` only when the intended
  reviewed fields still differ on the same Odoo record.
- A partially applied create remains resumable only when all differences are
  contained by its frozen deferred fields.
- Ambiguity, a missing accepted receipt, an unexpected changed field, another
  target, another principal, or stale evidence stops recovery.

The repository atomically writes the recovery-report semantic hash to every
row before transport can resume. If Impodo stops after this classification but
before another Odoo call, the same report can be reused. If it stops during a
new call, the new `IN_FLIGHT` batch requires fresh read-back.

Known Odoo rejections remain deterministic terminal evidence and stop
independent later components in the first delivery. A caught
`OUTCOME_UNKNOWN` response remains non-retryable inside that execution. The
same-run recovery path is for a process interruption whose response was not
journalled.

## Exact reconciliation scope

Read-back now groups records by target model and exact requested field set.
Rows for the same model but different reviewed fields no longer produce one
broad union-field read. Relationship comparison still resolves incoming and
existing references in bounded groups and proves the schedule's intended final
values, including optional cycle completions.

## Verification evidence

The following checks passed on 2026-08-28:

- 111 focused snapshot, scheduler, dependency benchmark, execution,
  reconciliation, load-job, and DuckDB journal tests passed.
- A simulated process exit after the create checkpoint reloads one exact
  in-flight batch and cannot finish the run.
- An absent interrupted create becomes retry ready only after exact read-back,
  resumes with attempt 2, and does not duplicate any previously committed row.
- A process exit during an optional-cycle completion resumes only the frozen
  relationship field and does not repeat either create.
- Recovery assessment remains unpublished, while the durable journal records
  one report hash across every row in one transaction.
- A known rejection performs one Odoo call and blocks both dependent and
  independent later components.
- Same-model rows with different fields produce separate exact-scope read-back
  calls.
- Ruff passed for the Phase 4 code, scripts, and focused tests.
- The architecture dependency, inventory, and code-documentation checks
  passed without a runtime cycle or forbidden application-to-adapter edge.

The repository-wide run executed 980 tests with 13 optional skips. It first
reported five failures. The one Phase 4 load-progress fixture failure was
corrected and its focused browser test then passed. Four unrelated guards
remain red: new completed-load correction identities are not yet classified,
the mapping template remains at 102 lines against its 100-line limit, the
2,068-line mapping workflow test still exceeds its organization limit, and
one end-to-end source-discovery assertion expects stale copy. Documentation
quality, workflow symbol resolution, `git diff --check`, and the affected
browser load-progress test passed after the correction.

## Remaining boundary

Phase 5 owns compact browser guidance and recovery presentation. Phase 6 still
owns representative Product/BOM scale and disposable Odoo 19 qualification.
The application recovery contract is intentionally narrow; additional
exception classes should first be tested against the same checkpoint and exact
read-back rules. If they cannot fit those rules cleanly, the design should be
revisited before adding special-case transitions.

No Odoo API key was needed for Phase 4 because this phase changes the durable
journal, restart classification, and bounded read-back orchestration. Phase 6
still requires an explicitly disposable Odoo 19 database to qualify real
transport, permissions, timing, and scale.
