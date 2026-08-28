# Correct a completed Odoo load

## Status and proposed decision

**Status:** Proposed. This workflow is not implemented.

Add **Correct this Odoo load** as a governed successor workflow for a data
manager who discovers a matching or transformation mistake after Impodo has
already loaded records into Odoo.

Impodo must not reopen or rewrite the completed run. It creates a successor
Authoring run, carries forward the frozen source and reviewed rules, asks the
data manager only for the corrected rule, recalculates every dependent result,
and proposes only the fields that genuinely need correction.

This plan covers the product workflow, evidence model, implementation slices,
Odoo 19 boundary, performance controls, and acceptance gates. It does not make
the proposed buttons or write capability current behavior.

## Reader and intended outcome

This plan is for product owners, maintainers, and reviewers implementing the
correction workflow. A data manager should eventually be able to:

1. open the result of a completed load;
2. choose **Correct this Odoo load**;
3. change the mistaken matching or transformation rule;
4. choose **Review correction**;
5. review only the resulting Odoo changes; and
6. choose **Apply 768 corrections**, followed by automatic verification.

Impodo performs mapping validation, preparation, quality checks, and a fresh
Odoo comparison between steps 4 and 5. Those responsibilities remain real and
auditable, but the data manager does not navigate back through every Authoring
stage or repeatedly press their actions.

## The problem this solves

A completed load can reveal a rule mistake that was not visible during the
original review. For example, a Products load may have used this rule:

```text
Active = Product status code is 60
```

The intended rule was:

```text
Active = Product status code is 10
```

The source file, Product identities, target connection, saved mapping,
prepared evidence, comparison, execution journal, and verification evidence
already exist. Sending the data manager back to **Match data**, **Prepare
data**, **Final review**, and **Load into Odoo** makes them reconstruct a
workflow that Impodo already understands. A new comparison may also treat
records created by the first load as unrelated existing Odoo data unless the
successor run carries forward their protected identities.

The correction is neither a new migration from scratch nor an edit to history.
It is a new governed decision based on one completed load.

## Proposed data-manager journey

```text
Completed and verified load
  -> Correct this Odoo load
  -> Change the mistaken rule
  -> Review correction
     Impodo validates, prepares, checks quality, and compares again
  -> Review the fields that would really change
  -> Apply N corrections
  -> Impodo verifies the result automatically
```

### Entry point

Show **Correct this Odoo load** beside a completed result only when Impodo can
identify the exact Project, frozen source data, original target, loaded rows,
and per-field outcomes. The button is a secondary business action. It does not
write to Odoo.

If a data manager follows an old bookmark back to **Match data** for a completed
run, Impodo must explain that the load is historical evidence and offer the
same successor action. It must not silently mutate the completed workspace.

### Correct the rule

The correction page starts from the exact mapping revision used for the load.
It shows the existing business rules and their effects. The data manager edits
only the mistaken rule. The first supported controls are:

- scalar source, constant, fallback, formula, and value-matching rules already
  supported by Authoring; and
- a many-to-one rule that changes a field to one exact existing Odoo record.

The page keeps one obvious next action: **Review correction**. Saving a local
decision, including **Let Odoo choose**, must immediately recalculate that
field's matching issues. A resolved yellow warning disappears automatically;
an unresolved warning remains with its real cause. The data manager must not
press **Check matches** again merely to refresh the page.

### Automatic preparation and comparison

**Review correction** starts one resumable pipeline:

1. save the corrected mapping draft;
2. validate complete mapping and categorical coverage;
3. confirm the corrected immutable mapping revision;
4. prepare the same frozen source rows;
5. run the required quality and normalization checks;
6. prove the exact Odoo target and current access again;
7. read the affected Odoo records by protected identifier in bounded groups;
8. calculate the three-way field comparison; and
9. publish the correction review or the smallest actionable blocker list.

Each internal result remains immutable evidence with its existing hashes and
lineage. The browser reports progress without turning those internal gates into
nine user decisions. A restart resumes from the last valid saved result.

### Correction review

The normal review starts with a business summary, for example:

> 768 Products need an Active correction. Of those Products, 37 also need the
> standard Odoo Unit. No Product will be created. Descriptions will not be
> rewritten.

The page then shows bounded samples with the Product identity, field, value
loaded previously, current Odoo value, and corrected value. It lists only
conflicts that require a decision. Technical hashes, numeric Odoo identifiers,
and raw transport evidence remain in protected support evidence.

When no field needs correction, Impodo finishes the review without offering a
write action. When safe changes exist, the one main action is **Apply N
corrections**.

### Apply and verify

Confirmation binds the actor, successor run, exact target, current correction
plan, current credentials, and change count. Impodo rechecks the affected
fields immediately before writing, records every planned attempt before
transport, applies only the reviewed field differences, and verifies the
result automatically.

The outcome distinguishes:

- **Correction verified**;
- **Verified with fallout** when Odoo accepted the write but a read-back value
  differs from the intended canonical value;
- **Some corrections rejected** for known Odoo failures; and
- **Outcome needs reconciliation** when a response was lost or cannot be
  trusted.

## The three-way correction rule

For each affected record and field, Impodo compares:

- **Previous intent (A):** the value approved in the completed load;
- **Current Odoo value (B):** a fresh value from the exact target record; and
- **Corrected intent (C):** the value produced by the corrected rule.

| Condition | Correction result | Write behavior |
| --- | --- | --- |
| `A = C` | The corrected rule did not change this field. Any `B` difference is existing verification fallout, not a correction. | Do not write. Surface fallout separately when relevant. |
| `B = A` and `C != A` | Odoo still has the value produced by the completed load. | Ready to correct from `B` to `C`. |
| `B = C` | Odoo already has the corrected value. | Mark already corrected and do not write. |
| `B != A` and `B != C` | The field changed independently after the load. | Conflict. Require review; the first delivery blocks this field. |
| Record missing or inaccessible | The exact prior target cannot be proved. | Block the record. Do not use a business-key fallback. |
| Previous outcome unknown | Impodo cannot establish a safe baseline. | Reconcile the prior load before permitting correction. |

Equality uses the field's governed canonical comparison. It must not be a raw
string comparison where Odoo has a documented canonical form, such as HTML or
translated values.

### Odoo identity wins without overwriting the Odoo record

When a corrected many-to-one value resolves to an existing Odoo record, Odoo
wins only as the identity owner. Impodo reuses that exact record identifier for
the Product relationship. It does not copy incoming values onto the Unit of
Measure record or overwrite that record's name, category, factor, rounding, or
other fields.

For Unit of Measure matching, comparison remains case-sensitive unless the
data manager explicitly defines another governed rule. Therefore `kg` or `KG`
does not silently become Odoo `Kg`. One exact current Odoo value has precedence
over an identical proposed supporting value, so Impodo reuses the existing
record and does not create a duplicate. Distinct values such as `PCE` and
`UNI` remain distinct unless the mapping explicitly directs `UNI` to the exact
standard Odoo Unit record.

## Proposed ownership and lifecycle

The completed run remains immutable. A rule-only correction reuses the same
accepted source DataVersion and creates a successor Authoring run and
workspace:

```text
MigrationProject
|-- Authoring DataVersion: the same frozen accepted source rows
|-- completed Authoring MigrationRun
|   |-- completed workspace and mapping revision A
|   `-- execution, reconciliation, and verified-load baseline
`-- successor correction MigrationRun
    |-- correction link to the completed run and baseline
    |-- same target identity, proved again with fresh access
    |-- successor workspace and corrected mapping revision C
    `-- correction comparison, execution, and reconciliation
```

The objects keep these responsibilities:

- The Project remains the business and governance root.
- The DataVersion remains the immutable accepted source. A rule correction
  does not create a copy of the source rows.
- The completed `MigrationRun`, workspace, mapping, execution, and
  reconciliation remain historical evidence.
- The successor `MigrationRun` owns the correction purpose, target binding,
  lifecycle, and execution attempts.
- The successor workspace owns the corrected mapping and regenerated detailed
  evidence.
- A target-I/O `ExecutionRun` remains one attempt inside the successor
  `MigrationRun`; it is not the correction itself.
- Credentials remain separately resolved capabilities and are never copied
  from the completed run.

If the source rows change, this is no longer a rule-only correction. Impodo
must accept a new DataVersion and create a normal successor run. It must not
alter the prior DataVersion or pretend that the prior load used the new rows.

### Recipe consequences

A Recipe revision used by the completed load remains immutable. The corrected
mapping can remain local to this successor Authoring run. If the data manager
chooses to reuse the corrected rule later, Impodo publishes a successor Recipe
revision after the normal eligibility checks.

An earlier Test qualification or Production selection does not transfer to the
successor Recipe revision. Test and Production continue to require fresh
evidence and their normal approvals.

## Verified-load baseline

### Proposed evidence

Every completed or partially verified load should publish one immutable,
protected **VerifiedLoadBaseline**. For each source row and target field it
contains or references:

- Project, DataVersion, `MigrationRun`, workspace, mapping revision, staging
  run, preflight result, execution snapshot, execution run, and reconciliation
  identifiers and hashes;
- source row lineage and portable business identity;
- target model and exact target-specific Odoo record identifier;
- intended canonical value approved before the load;
- reviewed operation: create, update, unchanged, excluded, or blocked;
- verified post-load canonical value when known;
- per-field verification state and known Odoo side-effect classification;
- target fingerprint, principal class, company context, schema fingerprint,
  and observation time; and
- the evidence needed to prove whether the record was created, updated, or
  already present.

Numeric Odoo identifiers, principal identifiers, company identifiers, and
target-specific values are restricted target evidence. They are encrypted in
the protected Project store and never appear in a Recipe, portable mapping,
review workbook, ordinary browser projection, or another target's run.

The baseline is a projection over existing immutable evidence, not a second
editable truth. Its root hash binds every contributing artifact. Publication
must be atomic: a partial baseline is unavailable for correction.

### Current gap to close

The execution journal records an Odoo identifier for attempted create or
update rows. A row classified as unchanged may have no execution attempt,
although preflight already resolved its protected target identity. The baseline
publisher must consolidate protected preflight identity, execution receipts,
and reconciliation read-back so every eligible row has one exact target.

It must not perform another business-key search to fill this gap. A missing,
ambiguous, or contradictory identity blocks baseline publication for that row.

## Correction lifecycle and invariants

### Proposed states

```text
DRAFT
  -> EVALUATING
  -> READY | REVIEW_REQUIRED | BLOCKED
  -> CONFIRMED
  -> APPLYING
  -> VERIFYING
  -> VERIFIED | VERIFIED_WITH_FALLOUT | OUTCOME_UNKNOWN
```

The registry-owned `MigrationRun` lifecycle remains authoritative. These are
correction workflow states or evidence states inside a successor Authoring run;
they must not create a second competing meaning for `COMPLETED`.

### Invalidation

Changing the corrected mapping invalidates correction preparation, quality,
comparison, confirmation, and execution planning. Changing the accepted source
creates a new DataVersion and exits this workflow. A changed target identity,
schema, company context, credential principal, permission result, or freshly
read affected field invalidates confirmation and requires **Review correction**
again.

Historical artifacts remain readable. Invalidation clears only the successor
run's current pointers and never deletes or rewrites the completed load.

### Field scope

The correction plan contains only fields for which corrected intent differs
from previous intent and current Odoo evidence permits a write. The executor
must not rebuild a full-record payload from the corrected prepared row.

This rule prevents an Active correction from rewriting a Description that the
new mapping did not change. It also reduces side effects, concurrency risk,
review noise, and Odoo request size.

## First supported boundary

The first release supports only:

- one unchanged accepted file-source DataVersion;
- one exact Odoo 19 target used by the completed load;
- a completed load with known row and field outcomes;
- update-only scalar corrections for an allowlisted field set;
- a many-to-one correction to one exact, unique, existing target record after
  scalar qualification passes;
- the same company context and permitted model scope; and
- Authoring against the existing disposable-target write policy.

The first release does not support:

- creates, deletes, archive or unarchive operations;
- identity-field corrections;
- business-key fallback when a prior Odoo identifier is missing;
- source-row changes inside the same DataVersion;
- one-to-many or many-to-many commands;
- creating, editing, merging, or deleting supporting Odoo records;
- computed, related, translated, company-dependent, or unqualified fields;
- arbitrary Odoo methods, imports, direct SQL, `sudo`, or browser automation;
- correction of an unresolved `OUTCOME_UNKNOWN`; or
- direct correction writes in Integrated Test or Production.

A Test or Production mistake instead creates corrected Authoring meaning, a
successor Recipe revision when reusable, a fresh Integrated Test
qualification, and a new Production rollout. The completed Test or Production
run remains immutable.

## Product-status and Unit of Measure acceptance example

Use the motivating Products case as one sanitized acceptance fixture:

- 999 Product rows in the unchanged DataVersion;
- 768 rows with Product status code `10`;
- 141 rows with code `30`;
- 90 rows with code `90`;
- the completed mapping incorrectly made Active true only for code `60`;
- the corrected mapping makes Active true only for code `10`;
- 37 rows use source value `UNI` and the corrected rule selects the exact
  standard Odoo Unit record;
- `PCE` remains a distinct Unit of Measure;
- existing `Kg` and `m` values reuse their exact Odoo records; and
- Product descriptions use the same previous and corrected intent.

The expected correction review is:

- 768 Active updates;
- 37 of those Products also receive the corrected Unit relationship;
- 231 Active values remain unchanged;
- no Product, Unit of Measure, or other supporting record is created;
- no Description field is written; and
- cleanup of an accidentally created custom `UNI` Unit of Measure is a
  separate governed task, not an implicit side effect of Product correction.

## Odoo 19, concurrency, and security boundaries

- The writer uses only the existing scoped Odoo 19 ORM-backed JSON-2 adapter.
- The correction domain and comparison services have no generic write client.
- The captured Odoo 19 schema decides whether a model and field are writable
  and supported. The plan cannot override readonly, computed, related,
  translated, company, access-control, or record-rule evidence.
- The executor accepts only the exact model, record identifier, field, value,
  target fingerprint, and correction-plan hash reviewed by the data manager.
- A just-in-time bounded re-read compares every affected current field with
  the confirmed comparison. Any unexpected value blocks that field before
  write.
- Native JSON-2 cannot make the final read and write atomic. Therefore the
  first delivery remains limited to the accepted disposable-target policy.
- Impodo records planned attempts before transport. A lost or invalid response
  becomes `OUTCOME_UNKNOWN`, stops later writes, and requires reconciliation
  before retry.
- The implementation exposes no arbitrary method name, caller-selected domain,
  unrestricted model, direct PostgreSQL route, `sudo`, import fallback, or
  browser automation.

Odoo automation and constraints can change fields beyond the submitted
payload. The verification allowlist must therefore be qualified by model and
field class. A new supported field requires evidence for serialization,
canonical comparison, constraints, automation, write behavior, read-back, and
idempotence.

## Performance and N+1 controls

The correction path must be proportional to affected models, field shapes,
and bounded pages rather than to rows multiplied by connector calls.

### Baseline and comparison

- Build the verified baseline set-wise from preflight, execution, and
  reconciliation artifacts.
- Group exact Odoo identifiers by model, field scope, company context, and
  target binding before reading.
- Read bounded identifier pages and request only identity, concurrency, and
  affected fields.
- Resolve each corrected many-to-one target key once per model, identity
  shape, scope, and company context. Reuse the protected result for every
  Product row.
- Do not call schema inspection, permission inspection, `search_read`,
  `name_search`, repository lookup, or relationship resolution inside the
  source-row loop.

### Execution and verification

- Group compatible updates only when the existing execution journal can still
  prove the outcome of every row and field.
- Preserve journal-before-transport and stop-on-unknown behavior for every
  batching change.
- Reconcile by model and bounded field scope. Do not read back one row at a
  time.
- Publish request counts, row counts, changed-field counts, page counts, wall
  time, and peak memory for the acceptance fixture and a larger qualified run.

An implementation is not accepted merely because it contains a batch API.
Tests must prove upper bounds on schema, permission, relationship, comparison,
write, and read-back calls.

## Proposed implementation ownership

### Domain

Create `src/impodo/domain/correction/` for pure correction meaning:

- baseline references and per-field verification states;
- the `A/B/C` classifier;
- correction plan rows and field differences;
- conflict and blocker codes;
- state transitions and invalidation inputs; and
- portable-versus-protected assertions.

The domain receives canonical field values and protected opaque target
references. It performs no Odoo, filesystem, DuckDB, browser, or credential
work.

### Application

Create `src/impodo/application/correction/` for use-case orchestration:

- successor correction-run creation through the existing Project and run
  services;
- verified-baseline publication;
- prior-mapping seeding into a new workspace;
- the automatic validation, preparation, quality, and fresh-comparison
  pipeline;
- correction confirmation and execution-snapshot construction; and
- automatic reconciliation and restart recovery.

The orchestrator uses existing mapping, preparation, preflight, execution, and
reconciliation ports. It must not duplicate their business semantics or write
directly into another owner's repository.

### Persistence and protected evidence

Extend the registry and Project-owned protected evidence through explicit
repositories and transaction ports:

- add a successor correction reference to `MigrationRun` or one run-owned
  correction binding without placing target evidence in the portable run
  projection;
- persist immutable verified-baseline manifests and paged per-row/per-field
  artifacts;
- persist the correction plan and confirmation hash;
- support atomic publication, bounded reads, schema-version rejection, and
  historical read-back; and
- retain target-specific identifiers only in the encrypted protected store.

Do not add a second editable copy of mapping, prepared data, or reconciliation
rows to the registry database.

### Browser

Add a correction router, presenters, templates, and background-job projection
under the existing Project authorization boundary. The browser owns only:

- the entry action from a completed result;
- the focused rule editor;
- progress for **Review correction**;
- the compact correction summary and conflict action list;
- explicit confirmation; and
- automatic verification outcome.

The presenter converts stable internal codes into plain business language.
Support details remain optional and never expose restricted target evidence.

## Delivery plan

### Phase 0: approve terms, policy, and fixtures

Freeze the distinction between a rule correction and a source correction,
approve the Authoring-only disposable-target boundary, and capture sanitized
fixtures for scalar, many-to-one, conflict, missing-record, fallout, and unknown
outcomes. Measure current preflight, execution, and reconciliation call counts.

**Exit result:** product, security, and engineering reviewers agree on one
correction meaning and one first-release boundary. No runtime behavior changes.

### Phase 1: publish the verified-load baseline

Implement the immutable baseline manifest, paged protected artifacts,
repository ports, codecs, hash bindings, and atomic publisher. Consolidate
preflight target identities, execution receipts, and reconciliation outcomes,
including rows that required no write.

**Exit result:** every eligible row and field from a completed load has one
known previous intent, exact protected target, and verification state. Missing
or contradictory evidence fails closed.

### Phase 2: implement pure three-way classification

Implement canonical `A/B/C` comparison, field-scope reduction, stable conflict
codes, deterministic ordering, and property tests. The classifier must exclude
unchanged intent before interpreting current Odoo drift as a correction.

**Exit result:** identical evidence produces an identical correction-plan hash,
and the Active/Description fixture proposes only Active fields.

### Phase 3: create successor correction runs

Extend run purpose and lineage through the registry-owned run service. Create a
new Authoring run and workspace over the same DataVersion, seed the exact prior
mapping as a draft, and bind the baseline without reopening the completed run.
Add invalidation and restart recovery.

**Exit result:** the completed run remains byte-for-byte historical evidence;
the successor has independent current pointers and no inherited credentials or
write confirmation.

### Phase 4: automate review for scalar corrections

Build the **Review correction** job over the existing mapping validation,
preparation, quality, target check, and preflight services. Add automatic
field-level recheck after mapping choices. Publish a compact review and
conflict action list. Support only the qualified scalar allowlist.

**Exit result:** correcting the Product Active rule produces 768 safe updates,
231 unchanged fields, zero Description writes, and no manual traversal of the
six Authoring stages.

### Phase 5: execute and automatically verify scalar corrections

Construct a correction-scoped `ExecutionSnapshot`, add just-in-time reread,
require explicit confirmation, reuse journal-before-transport execution, and
start reconciliation automatically. Preserve known-failure and
`OUTCOME_UNKNOWN` semantics.

**Exit result:** only reviewed scalar fields are written to exact prior Odoo
records, and the browser reports a verified or actionable final result without
a manual verification action.

### Phase 6: add exact-existing many-to-one corrections

Reuse the relationship resolver in bounded groups, preserve case-sensitive
matching, prefer one exact existing Odoo record over an identical proposed
supporting value, and bind the protected resolved identity into the correction
plan. Never update or create the related record in this phase.

**Exit result:** the 37 `UNI` Products point to the exact standard Odoo Unit
record; `PCE`, `Kg`, and `m` retain their governed meanings; connector calls do
not grow one-for-one with Product rows.

### Phase 7: finish the focused browser journey

Add **Correct this Odoo load**, the focused rule editor, resumable progress,
the compact correction review, **Apply N corrections**, and automatic outcome
presentation. Redirect attempts to edit a completed workspace into an
explanation and successor action.

Update current user and developer workflow pages, `docs/workflow.yml`, BPMN,
screenshots, contracts, architecture, Python code map, and acceptance guidance
only after each behavior is implemented.

**Exit result:** a data manager completes the supported correction without
thinking about evidence stages, while every safety gate remains provable.

### Phase 8: qualify scale and consider broader scopes

Run local and opt-in remote disposable Odoo 19 acceptance with measured calls,
time, memory, restart, conflict, failure, and read-back evidence. Only after
the first boundary passes may separate decisions consider identity changes,
supporting-record creation, Odoo-source corrections, Test promotion, or
Production correction.

**Exit result:** the supported scale and target class are explicit. No broader
write capability is inferred from the initial success.

## Verification matrix

### Domain and property tests

- Every `A/B/C` combination has the expected classification.
- Canonically equal HTML or normalized values do not create a false correction.
- `A = C` excludes the field even when `B` differs.
- Different row order and page size produce the same plan hash.
- A corrected full prepared row emits only changed field differences.
- Missing, duplicate, or cross-target protected identity fails closed.
- Numeric Odoo identifiers cannot enter portable correction projections.
- Correction state transitions reject confirmation after invalidation.

### Application and repository tests

- Baseline publication joins unchanged, created, and updated rows without a
  business-key fallback.
- Partial publication, hash mismatch, missing pages, and incompatible schema
  versions are rejected.
- Successor creation uses the same DataVersion and creates a new run,
  workspace, and mapping revision.
- Changing source input requires a new DataVersion.
- Mapping changes invalidate every downstream correction pointer.
- The automated review resumes after interruption without duplicating a job or
  execution attempt.
- Completed run and Recipe evidence remain immutable.
- A corrected Recipe revision receives no inherited Test or Production
  qualification.

### Browser tests

- Only an eligible completed result shows **Correct this Odoo load**.
- Opening an old completed-workspace mapping URL cannot mutate that workspace.
- Saving **Let Odoo choose** refreshes issues and removes only resolved
  warnings.
- **Review correction** runs the internal checks once and shows one current
  progress state.
- The review shows correct counts, bounded samples, zero creates, and only real
  conflicts.
- A zero-change result has no write action.
- Confirmation is explicit, stale confirmation is rejected, and automatic
  verification reaches one final outcome.
- Browser projections contain no numeric Odoo identifiers or credentials.

### Odoo 19 integration and call-count tests

- The Active fixture updates the exact 768 Products and a repeat review finds
  zero corrections.
- The Unit fixture updates exactly 37 Product relationships without writing to
  `uom.uom`.
- A concurrent change to one affected field blocks that field before write.
- A missing, archived, inaccessible, company-incompatible, or record-rule
  hidden Product produces a stable blocker.
- A known rejection remains distinguishable from a lost response.
- An unknown response stops all later correction writes and requires read-back.
- Schema, permission, relationship, comparison, and reconciliation calls obey
  explicit upper bounds and do not scale one-for-one with rows.

### Security and lifecycle tests

- Project authorization governs every correction route, job, artifact, and
  outcome.
- Read access never grants correction write access.
- Target and principal rechecks occur after confirmation and before transport.
- The writer rejects a different target, model, record, field, or plan hash.
- Project deletion and retention rules include correction baselines and plans.
- Integrated Test and Production cannot construct the first-release correction
  writer through browser or direct service calls.

## Completion criteria

The first supported correction workflow is complete only when:

- one immutable verified baseline covers every eligible row and field;
- a successor run preserves the completed load unchanged;
- the data manager changes one rule and uses one **Review correction** action;
- corrected intent, current Odoo state, and previous intent are compared by
  field;
- the plan contains zero creates and only exact protected target identifiers;
- scalar writes and exact-existing many-to-one writes pass separate Odoo 19
  qualification;
- no target read, relationship resolution, or read-back N+1 path remains;
- confirmation, journalling, unknown outcomes, and reconciliation preserve the
  existing guarded-write contract;
- the motivating Products case passes with the exact expected counts; and
- current documentation and screenshots change only when the corresponding
  behavior becomes implemented.

## Related current authority and plans

- [Evidence lifecycle](../developer/contracts/evidence-lifecycle.md)
- [Project lifecycle](../developer/contracts/project-lifecycle.md)
- [Recipe lifecycle](../developer/contracts/recipe-lifecycle.md)
- [Preflight contract](../developer/contracts/preflight.md)
- [Execution and reconciliation](../developer/contracts/execution-and-reconciliation.md)
- [Match data developer workflow](../developer/workflow/03-match-data.md)
- [Load into Odoo developer workflow](../developer/workflow/06-load-into-odoo.md)
- [Recipe runs in three pages](recipe-run-three-page-ui-refactor.md)
- [Impodo remaining work](remaining-work.md)
