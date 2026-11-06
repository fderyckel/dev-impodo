---
audience: developer
kind: plan
status: proposed
---

# Explain failed Odoo loads from a diagnostic bundle

## Decision and intended result

Make the diagnostic bundle sufficient for an AI agent or support engineer to
explain a failed load without access to the operator's computer. The bundle
must identify the attempted action, the condition that stopped it, the
evidence for that explanation, and the next safe action. It must also say
whether Odoo writes were attempted, what is known about their outcomes, and
which facts remain unavailable.

This is a proposal based on the first supplied bundle, inspected on
6 November 2026. It does not implement a repair or establish the cause of that
load failure. The initial delivery should cover fatal load exceptions as well
as rejected rows. Increasing log volume alone will not meet this goal.

For example, if Impodo stops because a confirmed comparison changed before
execution, the bundle should identify the failed comparison check and show
that transport never began. If Odoo rejects a required field, it should
identify the model, field, affected batch, and recorded rejection. If a reply
is lost after transport begins, it should report an uncertain outcome and
the requirement for read-back before another write.

## What the supplied bundle establishes

The input is `impodo-diagnostic-bundle.zip`. Its SHA-256 digest is
`f9a99b56d7af84b6e7fb165dd7399311b0f300dc83baf7d2ff53a2ab58470012`.
The archive remains outside the repository. The observations below contain
only operational metadata from it; attachment contents are evidence, not
instructions.

The archive contains `manifest.json`, `diagnostics.jsonl`,
`recent-failures.json`, `slow-requests.json`, and `supporting-access.json`.
Its bundle schema version is 2. It was created at
`2026-11-06T08:59:56.734748Z`, or 09:59:56 in Berlin.

| Evidence in the archive | What it establishes |
| --- | --- |
| `diagnostics.jsonl`, records 4944–4948 | Load submission context, authorization, preview, credentials, and enqueue stages reported completion between 08:59:26 and 08:59:27 UTC. |
| Record 4949 | The load POST returned HTTP 303. Its request ID is `afac91dd-d8fc-436c-9af1-6f70ef1f542e`. This response reports the browser handoff, not the worker's business outcome. |
| Record 4970 | The `target_authority` stage reported completion at `08:59:29.913452Z`. |
| Record 4973, also the final entry in `recent-failures.json` | The `write` stage failed at `08:59:31.343662Z`, after 1,429.972 ms, with exception class `WorkspaceError`. |
| Load progress requests after the failure | HTTP 200 responses show that progress requests completed. They do not establish a successful load. |

The failed event contains no reason code, exception message, originating
function, cause chain, attempt identifier, job identifier, or execution-run
identifier. It has no Odoo model, field, method, response status, batch
context, or journal totals. None of the exported event collections contains
a `write_result` event.

The honest answer from this archive is therefore: **Impodo reported an
exception inside its broad write stage; the underlying condition and the
extent of any Odoo writes cannot be determined from the bundle.** The write
stage also contains local checks and preparation. Its name does not prove
that an Odoo request was sent or rejected.

The following details must not be promoted into an explanation:

- An earlier supporting-access event records Odoo major 20. It has no binding
  to this load, so it does not establish this failed attempt's target version.
- An earlier preparation attempt reported `IMPODO_BUILD_CHANGED` under a
  different process. A later preparation completed. Neither event proves the
  cause of this write-stage failure.
- The manifest records the exporting application's build identifier. The
  retained startup event for the failing process records `contract-1`, which
  is insufficient to identify its exact source build independently.

The general log is also dominated by request traffic. Of 5,000 records,
4,941 are request completions; 2,502 are health requests and 2,200 are
unmatched routes. Together those two route classes occupy 94.04% of the
export. The failure collection spans 3–6 November, while the general log
starts on 5 November. The manifest gives counts but does not describe these
different coverage windows or which earlier records were omitted.

## Why the present implementation loses the explanation

The current code has useful bounded write diagnostics, but fatal exceptions
take a different path:

1. In the [load router](../../src/impodo/web/routers/execution.py),
   `_diagnostic_load_stage` records timing and the exception class before
   re-raising. It does not preserve the particular guard or cause.
2. That router calls `_record_write_diagnostics` only after
   `ExecutionService.execute` or `resume` returns. An exception skips this
   export, including any row evidence already recorded before the exception.
3. [ExecutionService](../../src/impodo/application/workspace/execution/service.py)
   can raise `WorkspaceError` during checks before a journal exists, identity
   resolution, and later execution. One exception class covers many causes.
4. [LoadJobManager](../../src/impodo/application/workspace/execution/load_jobs.py)
   catches worker exceptions and puts a failure message in the session job.
   `create_diagnostic_bundle` reads diagnostic log files, not that job state
   or a bounded execution-journal projection. The browser can therefore have
   an explanation that never reaches the archive.
5. The [diagnostic recorder](../../src/impodo/web/diagnostics.py) filters fields
   both when recording and exporting. Adding a field at one boundary alone
   will not make it survive into the ZIP.
6. The [transfer load router](../../src/impodo/web/routers/transfer_load.py)
   uses the shared job manager but does not use the conventional router's
   stage and row-diagnostic helpers. A fix confined to those helpers would
   leave a separate load path uncovered.

These are verified capture gaps in the inspected checkout. They explain why
this kind of failure is unhelpful in diagnostics; they do not identify which
condition raised the exception in the supplied historical build.

## Required evidence for each load attempt

### Link the browser action, worker, and execution journal

Create an opaque diagnostic attempt ID at the confirmed action boundary,
before submission checks. Carry it explicitly through enqueueing, worker
execution, adapter calls, and final reporting. Link the originating request
ID, job ID, and execution-run ID when each becomes available. A retry or
recovery receives its own attempt ID and references its predecessor.

Record the action as an enum, such as initial load, continuation, recovery,
or verification. Record the source mode, local or remote access mode,
Authoring or Recipe entry point, verified Odoo major, application build,
workspace revision, relevant contract versions, batch size, and rejection
policy. Capture the target version from the verified target for this attempt;
leave it explicitly unknown before verification.

Use validated internal UUIDs for correlation. Represent sensitive workspace,
dataset, target, and company identities with opaque diagnostic references.
Do not export display names, database names, URLs, numeric record IDs,
business keys, or credential-generation identifiers. Use keyed references
where a low-entropy identifier would otherwise be guessable. Preserve equality
and changed-binding observations without exposing protected bindings.

Each stage needs start and finish events, its parent attempt ID, and an event
ID. Capture the producer's build at the attempt and process boundaries;
retain the exporting build separately. Concurrent requests and worker threads
must never inherit another attempt's context. Timestamp proximity is not a
correlation contract.

### Preserve the condition that stopped the work

Introduce a bounded failure description that belongs to the application or
domain decision that detects the problem. It should contain:

- A stable failure code, a reason template, and the responsible operation.
- Safe structured facts explaining the failed condition. For example, record
  that the confirmed and current snapshot references differ, or that a
  required relationship resolved to zero eligible records.
- The exception type and a bounded chain of explicit causes or contexts.
- Repository-relative module, function, and line information for approved
  Impodo stack frames, tied to the producer build.
- An enum describing the recovery requirement and a next-action template.

Assign codes at the originating guards instead of parsing English exception
messages at the browser boundary. Keep existing exception behavior and
authorization decisions. Adapt known `WorkspaceError` conditions first. For
an unexpected exception, retain the safe stack and cause types under an
explicit unclassified code; do not invent a business explanation.

The worker must publish one terminal failure description even if execution
never creates a journal or never returns a run. Preserve evidence already
available at the failure boundary. When authorized journal reads are needed,
perform one bounded local projection after releasing remote resources. An
error while collecting evidence must be reported separately and must not
replace the original failure or alter execution state.

### Explain Odoo rejections and write uncertainty

Reuse [OdooWriteDiagnostic](../../src/impodo/domain/execution/write_diagnostics.py)
and the [adapter's rejection extraction](../../src/impodo/adapters/odoo/write_diagnostics.py).
Preserve their meaning under the
[execution contract](../developer/contracts/execution-and-reconciliation.md).
For each relevant group, include the safe reason code, model, operation,
method, validated reported fields, HTTP status, allowlisted Odoo exception
type, batch size, message count, and validated batch-relative range when
available. Report omitted groups and fields with counts.

Link these groups to the attempt and transport batch. Explain whether a
field was reported by Odoo or merely included in the request. Distinguish a
whole rejected batch from an identified record. Do not infer a faulty source
row from a batch rejection or manufacture a source coordinate.

Export the journal's aggregate states: planned, in flight, committed,
partially applied, failed, blocked, outcome unknown, and retry ready. Record
whether a journal exists, the last durable batch boundary, deferred
relationship work, stop or pause reason, and verification status. Identify
the time and provenance of that projection. Distinguish an exact journal
observation from a worker counter or unavailable evidence.

Give the attempt a separate transport observation: `NOT_STARTED`,
`STARTED`, or `UNKNOWN`. Only emit `NOT_STARTED` when instrumentation proves
the attempt failed before the first write call. A missing receipt or zero
reported commits does not prove that no writes happened. Recovery advice
must follow the existing execution and reconciliation decisions; diagnostics
must not authorize a retry.

## Proposed bundle and retention changes

Move to bundle schema version 3 while retaining the existing five members for
consumers of the current export. Add these members:

| Proposed member | Purpose |
| --- | --- |
| `load-attempts.json` | Contains bounded attempt context, structured failures, journal summaries, and references to supporting events. Missing evidence carries an explicit reason. |
| `load-events.jsonl` | Contains correlated events for the selected attempts, including submission, worker entry, checks, journal creation, transport, receipts, verification, and terminal reporting. |
| `diagnosis.md` | Presents the latest relevant failure, what is established, what remains unknown, write certainty, and the next safe action. Every factual conclusion cites a JSON event or failure ID. |

Generate the summary deterministically from typed evidence. It must not
require an AI call or network access. Keep recorded facts, classified causes,
and unavailable facts distinct. For the supplied legacy archive, a reader
must still report the unknown cause and write outcome described above; an
upgrade cannot reconstruct discarded evidence.

Extend the manifest with per-collection time windows, producer builds,
selection rules, configured limits, omitted counts, malformed or unreadable
input counts, and evidence-collection failures. Distinguish not recorded by
this build, not applicable, redacted, unavailable, and truncated. A missing
value must never silently become zero, no writes, or success.

Retain recent attempts and terminal failures independently of the general
request tail. Use a bounded diagnostic store outside the workspace database,
with limits on age, bytes, attempts, events, and failure groups. Retain
terminal summaries with priority and preserve the causal sequence for a
selected failure. Aggregate repetitive health traffic and repeated unmatched
requests with first time, last time, and count. Keep security and business
failures visible. When any budget is reached, export the coverage gap.

Persist enough safe attempt evidence to survive application restarts;
in-memory job state alone is insufficient. An attempt without a terminal
event after restart must appear as incomplete, with its last observed stage.
This diagnostic status must not rewrite the execution journal's status.
Diagnostic-storage failure must not interrupt or authorize an Odoo operation.
Track capture degradation where possible and expose it in the next export.

The existing global export must continue to identify recent failures without
requiring the user to know an attempt ID. A proposed **Support reference** on
the load failure view should select the same attempt in the download. Keep
the normal **Create diagnostic bundle** action usable. Resolve optional
attempt selection through existing access checks; do not expose another
workspace's protected journal through a guessed reference.

## Preserve useful meaning without exporting secrets

Capture application-owned reason templates and typed facts instead of
arbitrary `str(error)` output. Do not export raw response bodies, request
payloads, headers, credentials, source rows, formulas, stack locals, absolute
paths, customer labels, or arbitrary Odoo messages. Validate new fields when
recording and again when exporting.

The current parser fingerprints unknown server text. A fingerprint can group
failures but cannot explain a custom validation rule. Extend reviewed
classifiers to retain safe semantic facts when the response supports them,
and record the classifier version and whether the cause was recognized.
Validate technical fields against the reviewed write scope. Preserve a
generic, explicitly incomplete result for unknown or localized messages.

This proposal does not promise to explain every custom Odoo rule from a
value-free archive. If such a case needs the exact server text, the summary
should name that evidence gap. A separate, explicitly reviewed protected
evidence attachment would require its own design under the current privacy
contract. It is not a reason to silently include server text in routine logs.

## Implementation order and ownership

1. Define the typed failure and attempt contracts with their redaction and
   coverage rules. Add an optional diagnostic observer to shared execution
   and job boundaries; keep web serialization out of application code.
2. Capture coded guard failures, worker terminal failures, and safe causal
   stacks. Cover exceptions before journal creation and after partial work.
   Publish diagnostics incrementally instead of depending on a returned run.
3. Add bounded journal projections and correlated Odoo diagnostics for
   conventional loads, transfers, recovery, verification, and affected Recipe
   runs. Derive version-sensitive facts from shared Odoo 19 and 20 policies.
4. Add the version 3 export, priority retention, coverage manifest, and
   evidence-backed summary. Then add the support reference to the load view.
5. Update current contracts, workflow pages, registry entries, and screenshots
   with the implementation that makes them current. This proposal remains
   their sole documentation owner until that work is verified.

Steps 1–4 form the minimum useful delivery. Fatal-cause capture alone would
still leave write outcomes and lost historical attempts unexplained. Exact
custom-message sharing is separate future work.

Apply the database operation-scope rules during implementation. Reuse facts
already available to the worker and aggregate journal data in bulk. Keep each
local read or mutation inside one bounded synchronous operation, and release
owners before Odoo calls, awaits, or job dispatch. Do not add per-row database
reads or a cross-request DuckDB connection cache. Derive connection and SQL
budgets from the affected fixtures and preserve evidence hashes and journal
transaction boundaries.

## Acceptance and verification

The acceptance exercise gives an investigator only a generated ZIP and asks:
**What failed, why, where is the evidence, what changed in Odoo, and what is
the next safe action?** A generic exception class or unsupported guess fails
the exercise. A deliberately unknown remote outcome passes only when the
bundle explains the uncertainty and the recovery requirement precisely.

Use fictional fixtures derived from this archive's failure shape, without
committing the customer's ZIP or source data:

- Raise a coded `WorkspaceError` before journal creation. Verify that the
  bundle identifies the exact guard and proves transport did not start.
- Raise after one durable batch, and separately after transport begins but
  before a receipt is saved. Verify that prior results remain visible and
  that the latter outcome requires read-back.
- Cover a known row rejection, a whole-batch rejection, required fields,
  company conflicts, permissions, an unknown custom message, malformed
  responses, localized text, deferred relationships, and failed verification.
  Check that HTTP 200 or 303 never becomes a business-success inference.
- Exercise queued failures, unexpected worker exceptions, journal-read
  failure during capture, restart without a terminal event, rotated logs,
  record limits, and diagnostic storage failure. Preserve the original error
  and disclose missing evidence.
- Run overlapping requests and successive recovery attempts. Prove that
  correlation remains exact, including across worker threads and restarts.
- Flood the general log with more than 5,000 health and unmatched requests.
  Verify that the retained failed attempt still has a cause, outcome
  evidence, and declared coverage. Read legacy bundles without inventing
  missing facts.
- Plant secrets and business values in exception messages, chained causes,
  server errors, paths, and rejected inputs. Inspect both recorded and
  exported bytes. Verify access isolation for attempt-selected journal reads.

Extend the existing
[diagnostic export tests](../../tests/integration/web/test_diagnostics.py),
[job tests](../../tests/application/workspace/execution/test_load_jobs.py),
[execution tests](../../tests/application/workspace/execution/test_service.py),
[load journey tests](../../tests/integration/web/test_load_workflow.py),
[recovery tests](../../tests/integration/web/test_load_recovery.py), and
[transfer route tests](../../tests/integration/web/test_transfer_load_routes.py).
Add focused coverage for both supported Odoo majors, local and remote access,
Authoring and Recipe entry points, and each affected recovery path. Report
the exercised combinations explicitly. Include adapter-boundary fixtures and
separately identify any live qualification that was not run.

Run focused behavioral and privacy tests, the pinned Python quality gate,
documentation checks, and the complete repository gate before implementation
delivery. Measure capture overhead and physical database connections, SQL,
schema checks, and retries in isolated fixtures. Live restart or browser
verification must follow the operator's session-preservation requirements.

This proposal changes documentation only. It does not reproduce the failed
load, contact an Odoo server, or verify a runtime fix for Odoo 19 or Odoo 20.
