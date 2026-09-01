# Make Match data saves observable and formula-safe

## Status and proposed decision

**Status:** Slices 0 through 5 were implemented on 2026-09-01. The macOS
responsiveness qualification remains open, including concurrent catalogue
reads from separate editor identities, before cross-platform acceptance is
complete.

Add immediate, field-level validation for advanced formulas and make every
**Match data** request end in one visible, recoverable state. A malformed
formula is a **Must fix** issue. It prevents **Check matches**, but it does not
prevent **Save progress** from preserving the recoverable draft.

The first delivery should also add bounded request timeouts, an unambiguous
save receipt, stale-tab recovery, persistent local diagnostics, and protection
against overlapping catalogue work. These changes address the same operator
problem: Impodo must never leave a data manager wondering whether work was
saved, rejected, or still running.

## The problem this solves

Before Slice 1, a data manager could enter an invalid advanced formula and
select **Save progress** without seeing the syntax problem. Impodo stores the
recoverable draft and discovers the formula error only when the data manager
selects **Check matches**. The later validation is correct, but the distance
between the edited field and the reported error makes the save appear to have
failed.

The browser also waits indefinitely for a save response. A stopped or
unresponsive local server can therefore leave the page on **Saving
progress...**. When a response does fail, the detailed message appears near
the top of a long form while the data manager is working beside the buttons at
the bottom. A stale tab produces a concurrency error, but the current layout
can hide the recovery instruction.

A point-in-time diagnosis on 2026-09-01 found these implementation boundaries
before Slice 0:

- `validate_formula` already provides one authoritative safe-formula parser,
  but semantic mapping validation calls it during **Check matches** rather
  than while the formula is being authored.
- **Save progress** stores the draft and returns a normal success message
  without returning draft issues.
- The browser save request has no timeout or unknown-outcome recovery check.
- The detailed save error is rendered above the paged mapping catalogues. The
  bottom action area reports only a shorter status.
- Browser field searches abort an obsolete browser request, but work that has
  already entered the server thread pool can continue.
- The complete mapping page performs its synchronous render directly from an
  asynchronous route.
- The local launcher disables access logging and does not persist a structured
  reason when the server exits.

One affected local workspace took about 5.4 seconds for its first complete
page render. Overlapping cold catalogue searches reached about 10 seconds.
These measurements are diagnostic evidence, not a general performance
guarantee.

Slice 0 added a repeatable 1,000-field browser baseline on the same development
machine. Two instrumented runs measured 2,363 to 4,461 ms for a cold complete
page, 494 to 925 ms for median warm search, and 1,219 to 1,288 ms wall time for
four concurrent searches. Save took 2,868 to 3,099 ms, while stale-version
rejection took 2,321 to 2,402 ms. The test prints measurements rather than
enforcing a release budget because repeated Windows qualification still needs
to establish stable thresholds.

## The data-manager experience

Suppose a data manager enters this formula for an Odoo **Active** field:

```text
value 1= "UNI"
```

After the data manager pauses or leaves the formula box, Impodo shows this
message beside that box:

> **Must fix:** This formula is not valid. Use `==` for “equals” or `!=` for
> “does not equal”. For example, `value == "UNI"`.

The bottom action area also shows **1 issue needs attention** and offers
**Go to issue**. This message remains visible when the affected field is on
another catalogue page or hidden by a search.

The data manager can still select **Save progress**. Impodo stores the exact
draft and returns:

> **Saved — needs attention:** Your progress was saved. Correct 1 formula
> before checking the matches.

**Check matches** remains unavailable while the known syntax error exists.
The data manager does not need to rediscover the problem through a later full
validation pass. The server still performs authoritative validation when the
data manager checks the matches, so bypassing or disabling browser code cannot
weaken the mapping contract.

## Proposed request states and messages

Use the same state model beside the bottom workflow actions and in a sticky
page-level status. Field-specific problems also remain beside their controls.

| State | Meaning | Required message and action |
| --- | --- | --- |
| **Unsaved** | The browser contains edits that the server has not stored. | Show **Unsaved changes** and keep **Save progress** available. |
| **Validating** | Impodo is checking changed formulas without storing the draft. | Show **Checking formula...** beside the affected field. Do not block unrelated editing. |
| **Saved** | The server returned the new working-draft version and content identity. | Show the save time and **Progress saved**. |
| **Saved — needs attention** | The server stored the draft, but authoring issues prevent a checked revision. | Show the issue count and **Go to issue**. |
| **Conflict** | Another tab or request saved a newer working-draft version. | Preserve the current form and offer **Reload saved version** and **Copy my edits**. |
| **Outcome unknown** | The browser timed out after sending a mutation and cannot yet prove whether it committed. | Disable blind retry, check the operation receipt, then report **Saved** or **Not saved**. |
| **Disconnected** | The local server health check failed. | Clear the busy state and explain how to reopen Impodo while keeping the current tab available for copying unsaved choices. |
| **Failed** | The server returned a known rejection and did not commit the requested change. | Show the exact correction and a support reference beside the action area. |

Do not use **warning** for invalid syntax. A warning permits a reviewed mapping
to continue, while malformed syntax cannot produce defined mapping behavior.
Use **Must fix** for the formula and reserve warnings for advisory findings,
such as a source type that differs from the selected output type.

## Formula validation design

### One authoritative parser

Keep `domain/recipe/value_rules.py::validate_formula` as the semantic source
of truth. Do not create a second JavaScript formula grammar that can drift from
the runtime evaluator.

Add a small authenticated route that validates one formula against the
current dataset's permitted `value` and `column_N` names. The route must:

- derive permitted column names from the current frozen source selection;
- use the existing session, workspace authorization, host, and CSRF controls;
- perform no repository write and make no Odoo call;
- return a stable issue code, a plain-language message, and a character
  position when the parser can identify one; and
- avoid echoing unrelated source values or credentials.

The browser calls this route after a 500-millisecond pause and when the field
loses focus. It aborts an obsolete request and uses a monotonically increasing
client generation so a late response cannot replace a newer result. The
screen-reader announcement should be polite after the pause rather than
firing for every keystroke.

### Save and check semantics

Before a mutation, the browser checks whether any current formula control has
a known blocking result. **Save progress** remains available because the
working draft is recovery evidence. The save response includes the exact
working-draft version, save time, and current lightweight authoring issues.

**Check matches** does not send a request while a known formula syntax issue
remains. The server continues to parse and validate the complete submitted
definition because browser validation is usability support, not an authority
boundary. An invalid draft never creates a valid mapping revision, validation
approval, or submission.

## Recommendations for silent failures and freezes

### 1. Give every mutation an operation identity and receipt

Generate one operation identity for each save, check, default decision, and
confirmation. Store the identity with the atomic outcome. If the browser loses
the response, it queries a read-only receipt endpoint before offering retry.
The receipt states whether the operation committed and names the resulting
working-draft or mapping-revision version.

This prevents duplicate or conflicting work after a timeout. The browser must
not automatically repeat a mutation whose outcome is unknown.

### 2. Bound waiting and always clear the busy state

Apply an explicit timeout to every browser request. The first proposed limits
are 15 seconds for local mutations, 10 seconds for catalogue fragments, and 5
seconds for health checks. Qualification should adjust these limits using
measured representative workspaces.

Every request path must clear `aria-busy`, re-enable the relevant controls,
and enter one terminal state in a `finally` block. A timeout is not proof that
a save failed; it enters **Outcome unknown** until receipt read-back resolves
it.

### 3. Keep errors beside the action and the affected field

Render the complete error or recovery message in a sticky status beside the
bottom workflow actions. Also render formula, selection, and relationship
issues beside their controls. **Go to issue** opens the correct dataset,
catalogue page, and field even when the current search hides it.

Move keyboard focus to the sticky status only after an explicit action fails.
Do not move focus while the data manager is typing.

### 4. Make stale tabs recoverable

Return HTTP 409 for a working-draft version conflict and include the current
server version, the submitted version, and a stable conflict code. Preserve
the current form in the tab. Offer a deliberate reload and a copyable summary
of the unsaved edits.

Add an editor-session marker so a second tab can warn that the workspace is
already open elsewhere. The marker must not become an exclusive lock that
traps the user after a browser crash. Use a short renewable lease and retain
the repository's optimistic concurrency check as the final authority.

Do not persist the complete form in unrestricted browser local storage. The
server-side working draft remains the durable recovery source, and the old tab
retains unsaved form values only until the operator chooses to discard or
reload them.

### 5. Detect a stopped or unresponsive local server

Add a lightweight authenticated health endpoint and a browser heartbeat while
a workflow page is open. After consecutive failures, show **Impodo is not
responding** and the recovery instructions. Do not leave the last action in a
busy state.

Run the web server under a small launcher supervisor. The supervisor records
the child process identifier, port, start time, normal shutdown, unexpected
exit code, and restart attempt. If security and port ownership checks pass, it
should restart once on the same loopback port so the existing browser origin
can recover. Repeated exits stop automatic restart and direct the operator to
the diagnostic bundle.

### 6. Persist privacy-safe diagnostics

Write bounded rotating local JSON logs under the Impodo application-data root.
Every request records a request identity, route class, status, duration,
working-draft version when relevant, and exception class. Startup and shutdown
records distinguish an operator exit, launcher termination, server exception,
and unknown process loss.

Do not log credentials, CSRF or launch tokens, raw source values, formula
contents, Odoo record contents, or complete form bodies. Record formula issue
codes and field paths instead. Provide **Create diagnostic bundle** so the
operator can export redacted logs, version information, schema versions, and
recent slow-request summaries without exporting business data.

### 7. Prevent obsolete searches from consuming the server

Coalesce catalogue searches per browser editor and make the newest generation
authoritative on the server as well as in the browser. Cache the immutable
catalogue projection by source-selection hash, schema hash, dataset, and
mapping version. Apply search and pagination to that cached projection instead
of rebuilding the complete mapping view for every query.

Move the complete synchronous mapping render off the asynchronous event loop.
Instrument workspace reads, view construction, template rendering, queue wait,
and total response time separately. Persist a slow-request record when a
configured threshold is exceeded.

### 8. Add a local responsiveness watchdog

Measure event-loop delay and thread-pool queue delay. When either remains over
the qualified threshold, record a bounded stack summary and show a degraded
performance status. The watchdog must never include source rows or secrets in
its evidence.

The purpose is to distinguish a slow render from a stopped process. It should
not kill a request or restart the server without first preserving the
operation outcome and working-draft state.

## Delivery sequence

### Slice 0 — establish diagnostics and performance baselines

**Implementation status:** Completed on 2026-09-01.

- Add request identities, rotating redacted logs, startup and shutdown
  records, and slow-request timing.
- Add representative cold-page, warm-search, concurrent-search, save, and
  stale-version measurements to `test_mapping_catalog_scale.py` or a dedicated
  mapping responsiveness qualification.
- Record the current behavior before changing request scheduling.

The production launcher now secures a sibling `diagnostics` directory and
writes bounded rotating JSON records. Each request receives an identity and a
terminal record containing only allowlisted operational fields. Mapping
responses report workspace-read, view-build, projection, render, queue-wait,
and total timings where applicable. A lightweight monitor records event-loop
delay above the slow-request threshold. Lifecycle records cover launcher and
application start, normal stopping, shutdown failure, and caught server
exceptions.

The recorder has no fields for raw URLs, query strings, headers, bodies,
source values, formula text, credentials, CSRF tokens, or launch tokens. A hard
process termination cannot write a final record; a latest start without its
matching stop remains the loss evidence until Slice 4 adds supervision.

**Verified result:** completed requests identify slow server phases, thread-pool
queue wait, status, and exception class. Event-loop stalls and caught server
exits have separate records. An abrupt process loss is distinguishable by the
missing terminal lifecycle record.

### Slice 1 — add formula authoring feedback

**Implementation status:** Completed on 2026-09-01.

- Add the read-only formula-validation route using the existing parser.
- Add inline **Must fix** feedback, character location, and **Go to issue**.
- Return lightweight authoring issues with **Save progress**.
- Preserve invalid drafts while preventing **Check matches** from proceeding.

**Exit result:** a malformed formula is visible beside the field before the
data manager attempts a full mapping check, and saved progress is never
misreported as a checked mapping.

The browser now validates formulas after a 500 ms pause and on blur through a
CSRF-protected read-only route. Obsolete checks are aborted and stale responses
are ignored. Parser failures appear inline with **Must fix**, an optional
character location, and **Go to issue**. Saving preserves the exact invalid
draft and returns a lightweight formula-free issue list with **Saved — needs
attention**. The browser prevents **Check matches** while an issue is known or
pending; the server semantic validator still rejects an invalid formula when
that browser guard is bypassed.

**Verified result:** parser, route, CSRF, saved-draft, server-bypass, template,
browser-contract, and static-ownership tests cover the Slice 1 behavior.

### Slice 2 — make save outcomes recoverable

- Add mutation operation identities and durable receipts.
- Add timeouts, outcome read-back, and terminal request states.
- Return structured HTTP 409 conflicts and add stale-tab recovery controls.
- Put the complete message beside the bottom workflow actions.

**Exit result:** every save attempt becomes provably saved, not saved,
conflicted, or temporarily unknown. No request leaves the page busy forever.

**Implemented in Slice 2:** mapping mutations now carry operation identities
and atomic DuckDB receipts. The browser bounds a mutation at 15 seconds,
reads the receipt before deciding what happened, and ends in **Saved**, **Not
saved**, **Conflict**, or **Unknown**. Structured version conflicts preserve
the current form and offer **Copy my edits** and **Reload saved version** at
the bottom workflow actions.

**Verified result:** receipt commit, replay, rejection, pending, missing,
timeout-readback, and stale-version behavior are covered by domain, schema,
browser workflow, large-catalogue, static-ownership, and architecture contract
tests. Every submit path clears its busy state in a `finally` block.

### Slice 3 — remove mapping-page contention

- Offload the complete mapping render from the asynchronous event loop.
- Cache immutable catalogue projections and coalesce obsolete searches.
- Bound server work by current editor generation where cancellation is safe.
- Qualify the page and search budgets on Windows with representative large
  Odoo field catalogues.

**Exit result:** typing in field search cannot create an unbounded queue that
delays saving or makes the page appear frozen.

**Implemented in Slice 3:** the complete mapping renderer now runs in the
bounded thread pool. Each browser editor sends a monotonically increasing
search generation. The server discards obsolete generations, serializes
catalogue projection work per workspace, and reuses a bounded content-keyed
field projection for search and pagination.

**Verified result:** the representative 1,000-field Windows qualification
measured a 4.9-second cold page, a 1.1-second median cached search, a
1.7-second four-generation coalesced burst, a 4.1-second save, a 3.2-second
stale-version response, and 226 MiB process peak memory. Runtime tests prove
that only the newest waiting editor generation runs and that separate editors
share one workspace projection gate. Route tests prove that stale generations
return HTTP 204 before projection and that the complete renderer has no
running event loop.

**macOS follow-up:** repeat the separate-editor concurrency qualification on
macOS. Confirm whether concurrent catalogue reads can reproduce the DuckDB
attachment collision observed on Windows, verify that the workspace projection
gate prevents the collision, and record the macOS timing and memory results
before treating this safeguard as cross-platform qualified.

### Slice 4 — add process recovery

**Implementation status:** Completed on 2026-09-01.

- Add the authenticated heartbeat and disconnected-state banner.
- Add the launcher supervisor, same-port single restart, and repeated-exit
  circuit breaker.
- Add the redacted diagnostic-bundle action.

**Exit result:** a stopped server is visible within a bounded interval, one
safe restart can recover the current browser origin, and repeated failures
produce useful local evidence.

**Implemented in Slice 4:** every rendered browser page runs an authenticated
same-origin heartbeat. Three consecutive failures show **Impodo is not
responding** and clear shared or Match data busy state without repeating the
action. The active-tab timeout is bounded at 18 seconds when all three health
requests reach their timeout. A later successful check shows **Impodo is
responding again** so the operator can review the save outcome before retrying.

The launcher now runs FastAPI and Uvicorn in a spawned child process. After one
non-zero child exit, the parent acquires a fresh exclusive listener on the same
loopback port and starts one replacement with the same session-signing secret.
The fresh handle avoids the Windows I/O completion-port ownership conflict that
occurs when one asyncio listener is reused across processes. A lost port or a
second unexpected exit opens the circuit and prevents another automatic
restart.

The authenticated, CSRF-protected **Create diagnostic bundle** action exports
application and schema versions, re-sanitized bounded JSON logs, and recent
slow-request summaries. It excludes source rows, formulas, credentials,
tokens, bodies, headers, raw URLs, query strings, and arbitrary exception
messages. Bundle construction runs outside the asynchronous event loop.

**Verified result:** focused tests cover authentication, CSRF, bundle contents,
tampered-log redaction, one-restart policy, repeated-exit circuit breaking,
same-port ownership, and lifecycle evidence. A Windows qualification started
two real server processes in sequence on one loopback port and proved that the
authenticated browser session remained valid after the process replacement.

### Slice 5 — update current documentation after implementation

**Implementation status:** Completed on 2026-09-01.

- Update the paired user and developer **Match data** pages.
- Update `docs/workflow.yml`, code references, and affected docstrings.
- Capture the formula error, saved-with-issues, conflict, and disconnected
  states at 1440 by 1024 using fictional data.
- Keep each remaining behavior marked proposed until its acceptance criteria
  are verified.

The paired Match data pages now describe the shipped formula, mutation receipt,
catalogue scheduling, server recovery, and privacy boundaries. The workflow
registry links the authoritative runtime symbols and the reproducible browser
capture helper. The Python code map and evidence lifecycle contract describe
the same implemented ownership boundaries.

The screenshot helper created an isolated fictional Contact workspace, served
the current authenticated application on an ephemeral loopback port, and drove
installed Microsoft Edge at 1440 by 1024 CSS pixels with device scale factor
1. The four current user images show inline **Must fix**, **Saved — needs
attention**, stale-tab **Conflict**, and **Impodo is not responding**. No
operator workspace, source data, credential, or external Odoo service entered
the captures.

**Verified result:** the current browser produced all four states through the
implemented routes and JavaScript. The disconnected capture stopped the
isolated server and waited for the real three-failure heartbeat threshold.
Documentation registration, links, images, symbols, focused browser contracts,
and repository formatting pass the Slice 5 checks.

## Verification matrix

### Formula and mapping behavior

- Valid comparison formulas such as `value == 10` and `value != "UNI"` pass
  both authoring and semantic validation.
- Invalid syntax receives the same stable issue code from authoring and full
  semantic validation.
- Unknown `column_N` names, unsupported functions, excessive length, and
  excessive complexity remain fail-closed.
- Saving an invalid formula increments the recoverable working-draft version
  but creates no mapping revision, validation approval, or submission.
- **Check matches** remains server-authoritative when browser code is absent or
  bypassed.

### Browser recovery

- A rejected fetch, malformed response, JavaScript exception, timeout, and
  aborted request each clear the busy state.
- A disconnected server produces a visible recovery message within the
  qualified timeout.
- A timed-out committed save resolves to **Saved** through its operation
  receipt. A timed-out uncommitted save resolves to **Not saved**.
- A stale tab receives HTTP 409, preserves its current form, and cannot
  overwrite the newer working draft.
- The complete failure message remains visible beside the action buttons and
  is reachable by keyboard and screen reader.

### Responsiveness and operations

- Repeated search input makes only the newest result visible and does not
  accumulate unbounded server work.
- The full mapping page does not block health checks or save responses on the
  asynchronous event loop.
- Representative Windows qualification records cold and warm page time,
  search latency, mutation latency, event-loop delay, queue delay, and peak
  memory.
- An unexpected server exit writes a redacted shutdown or loss record. A
  normal exit is distinguishable from a crash.
- The diagnostic bundle contains no credentials, tokens, source rows, Odoo
  record contents, complete forms, or formula text.

Focused verification should extend:

- `tests/domain/mapping/test_validation.py`;
- the formula tests under `tests/domain/recipe/`;
- `tests/integration/web/test_mapping_forms.py`;
- `tests/integration/web/test_mapping_workflow.py`;
- `tests/integration/web/test_mapping_catalog_scale.py`;
- `tests/integration/web/test_security.py`; and
- a new launcher and request-receipt recovery test package.

## Acceptance criteria

The proposal is complete only when all of these statements are true:

1. A data manager sees a malformed formula beside the affected field before a
   full save or check request is needed.
2. **Save progress** preserves an invalid draft and reports **Saved — needs
   attention** with an exact issue count.
3. **Check matches** cannot accept an invalid formula, even when browser
   validation is bypassed.
4. Every mutation has an operation identity, an atomic receipt, and one
   visible terminal state.
5. A timeout never causes an automatic blind retry or an ambiguous claim that
   work was lost.
6. Stale-tab, disconnected-server, and known server errors remain visible
   beside the workflow actions and preserve recoverable edits.
7. Search bursts cannot starve save and health requests through obsolete view
   construction.
8. Unexpected process exit, slow request, and normal shutdown produce distinct
   privacy-safe local evidence.
9. Representative Windows and macOS responsiveness and restart tests pass
   repeatedly. The macOS run must include concurrent catalogue reads from
   separate editor identities.
10. Current user and developer documentation and screenshots describe only
    the behavior that has actually shipped.

## Non-goals

The first delivery does not:

- replace the safe formula language with arbitrary Python, spreadsheet
  formulas, imports, loops, file access, network access, or Odoo methods;
- turn browser validation into a security or evidence authority;
- prevent the data manager from saving incomplete recoverable work;
- automatically merge conflicting edits from two browser tabs;
- store complete mapping forms in unrestricted browser storage;
- treat a timeout as proof that a mutation failed; or
- hide current semantic blockers merely because authoring validation found
  one problem earlier.

## Implementation ownership

| Responsibility | Proposed owner |
| --- | --- |
| Safe formula grammar and stable issues | `domain/recipe/value_rules.py` and `domain/mapping/validation/scalars.py` |
| Draft and mutation receipt semantics | `application/workspace/mapping/service.py` and the DuckDB mapping repository |
| Formula validation, save, receipt, and health routes | `web/routers/mapping.py` and the local application router |
| Inline field feedback and terminal request states | `web/static/mapping-editor.js` and mapping templates |
| Catalogue coalescing and cache integration | `web/static/mapping-catalogs.js`, mapping presenters, and mapping routes |
| Request timing, redacted logs, and exception handling | web middleware and the local launcher |
| Process supervision and restart evidence | `web/launcher.py` |

## Related documentation

- [User workflow: Match data](../user/workflow/03-match-data.md)
- [Developer workflow: Match data](../developer/workflow/03-match-data.md)
- [Workflow evidence lifecycle contract](../developer/contracts/evidence-lifecycle.md)
- [Match data: questions and answers](../user/tutorials/match-data-questions-and-answers.md)
