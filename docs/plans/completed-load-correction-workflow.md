# Correct a completed Odoo load

## Status and proposed decision

**Status:** In progress. Phases 1, 2, 3, and 4 are implemented at the domain,
application, protected-evidence, registry, execution, reconciliation, and
focused browser boundaries. A verified completed
Authoring load can now publish one encrypted lean origin manifest and compact
exact-target index while atomically closing its historical run and workspace.
The restart-safe successor coordinator creates a new Authoring run and
workspace over the same frozen DataVersion, copies only credential-free target
setup, seeds the prior rules as an explicitly unverified draft, orders mapping,
native preparation, quality, and fresh target-read owners, reduces `A/C` with
Polars, reads `B` only for sparse candidates, and publishes one current
protected scalar plan. Mapping or prepared-evidence changes clear that pointer.
Confirmed scalar corrections now use exact protected identifiers, a bounded
just-in-time reread, the existing durable journal, compatible update batches,
and automatic exact-ID reconciliation. The browser now provides one safe
successor journey with resumable review/apply progress and compact public
counts.

The implemented browser has no generic correction write entry point. A
blocker-free scalar review is sealed into one protected plan and one separately
bound write confirmation. Apply re-probes the write identity,
checks the run-owned current pointers, and performs a just-in-time exact-ID
reread before the execution journal or any Odoo write is created.
Plan hashing is one whole-artifact operation; it does not add per-row or
per-value hashes. The review supports scalar output differences from direct
fields, value mappings, Selection outcomes, constants, fallbacks, and native
transformations such as casing. It emits the same candidate contract for
many-to-one keys, but the review returns a stable
`RELATIONSHIP_NOT_QUALIFIED` blocker until the separate relationship
qualification is complete.

Add **Correct this Odoo load** as a governed successor workflow for a data
manager who discovers a matching or transformation mistake after Impodo has
already loaded records into Odoo.

Impodo must not reopen or rewrite the completed run. It creates a successor
Authoring run, carries forward the frozen source and reviewed rules, asks the
data manager only for the corrected rule, recalculates every dependent result,
and proposes only the fields that genuinely need correction.

This plan covers the product workflow, lean evidence model, implementation
slices, Odoo 19 boundary, performance controls, and acceptance gates. The first
delivery supports scalar corrections. Exact-existing many-to-one corrections
follow only after the scalar path qualifies independently. This plan does not
make the proposed buttons or write capability current behavior.

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

### Correction meaning is independent of the editor control

Impodo identifies a correction from changed target-field intent, not from the
kind of mapping control the data manager edited. The same comparison therefore
covers:

- a source field that was matched to the wrong scalar rule;
- an incorrect Selection value mapping or conditional Selection outcome;
- an incorrect constant or fallback;
- a missing, extra, uppercase, lowercase, trimming, or replacement
  transformation; and
- after separate relationship qualification, a many-to-one value that should
  point to another exact existing Odoo record.

Formula correction is only one way to produce a different corrected intent. It
must not receive its own correction model or execution path. Scalar values and
relationship identities use different canonical comparators, but both publish
the same field-difference meaning for review and execution.

Changing which Odoo target field receives a value is a separate field-scope
correction. Removing the old field from a mapping does not prove what value
should restore that Odoo field, especially when the completed load created the
record. The first release therefore requires the corrected rule to retain the
same target field. A later field-scope slice must use preserved pre-load target
evidence and fail closed when it cannot prove a restoration value.

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
only the mistaken rule. The first release supports scalar source, constant,
fallback, formula, and value-matching rules already supported by Authoring. A
later independently qualified slice adds a many-to-one rule that changes a
field to one exact existing Odoo record.

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
4. open the completed run's hash-verified prepared Parquet snapshot as previous
   intent;
5. prepare the corrected intent through the existing compiled Polars and
   Parquet path;
6. lazy-scan both prepared snapshots, project only stable row lineage and the
   eligible scalar fields, and filter changed intent with vectorized Polars
   expressions;
7. run the required quality checks and confirm that no resolution or
   normalization decision changes an eligible field;
8. prove the exact Odoo target and current read access again;
9. read only the candidate Odoo records by protected identifier in bounded
   groups;
10. calculate the three-way field comparison set-wise; and
11. publish the correction review or the smallest actionable blocker list.

Each internal result remains immutable evidence with its existing hashes and
lineage. The browser reports progress without turning those internal gates into
separate user decisions. A restart resumes from the last valid saved result.

The correction path must not reconstruct previous intent by rerunning the old
mapping through the current application build. For the first scalar boundary,
the completed prepared Parquet snapshot is the approved previous intent because
eligible fields cannot have been changed later by resolution, normalization, or
relationship materialization. If that snapshot is absent, outside retention,
incompatible, or fails its existing integrity check, the completed load is not
eligible for this correction workflow.

### Correction review

The normal review starts with a business summary, for example:

> 768 Products need an Active correction. No Product will be created. Units of
> Measure and Descriptions will not be rewritten.

The page then shows bounded samples with the Product identity, field, value
loaded previously, current Odoo value, and corrected value. It lists conflicts
as blockers. The first scalar delivery does not offer a conflict override or a
partial apply action. Any conflict, missing record, inaccessible record, or
unproved prior outcome disables **Apply N corrections** until the cause is
resolved and the data manager runs **Review correction** again. Technical
hashes, numeric Odoo identifiers, and raw transport evidence remain in
protected support evidence.

When no field needs correction, Impodo finishes the review without offering a
write action. When safe changes exist and no blocker remains, the one main
action is **Apply N corrections**.

### Apply and verify

Confirmation binds the actor, successor run, exact target, current correction
plan, freshly resolved write-capable principal, credential generation, and
change count. Review-time reads and apply-time writes use separate capabilities
and record their principal provenance separately, even when both capabilities
resolve to the same Odoo user. Impodo rechecks the affected fields immediately
before writing, records every planned attempt before transport, applies only
the reviewed field differences, and verifies the result automatically.

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
| `B != A` and `B != C` | The field changed independently after the load. | Conflict. The first delivery blocks the whole correction plan. |
| Record missing or inaccessible | The exact prior target cannot be proved. | Block the whole correction plan. Do not use a business-key fallback. |
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
|   |-- completed workspace, mapping, and prepared intent A
|   `-- execution, reconciliation, correction-origin manifest, and protected target index
`-- successor correction MigrationRun
    |-- correction link to the completed run and origin manifest
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

## Correction-origin evidence

### Lean manifest and protected target index

An eligible completed load publishes one immutable
`CorrectionOriginManifest`. The manifest points to the existing evidence that
already proves the completed load. It does not copy prepared field values,
execution rows, or reconciliation rows into a second baseline.

The manifest records:

- the Project, DataVersion, completed `MigrationRun`, workspace, mapping,
  prepared snapshot, preflight, execution, and reconciliation identities and
  their existing hashes;
- the exact target fingerprint, schema fingerprint, company context, and
  observation boundary;
- the protected target-index storage reference and its integrity value; and
- an eligibility summary that proves the completed run has no unresolved
  outcome.

The protected target index contains one compact entry for each eligible source
row and target model. Each entry binds stable source lineage to the exact Odoo
record identifier and its known completed-load outcome. Previous canonical
field values for the eligible direct scalar scope remain in the completed
prepared Parquet snapshot. They are not copied into the index and do not
receive per-value or per-row hashes. A future scope whose executed value can
differ from that snapshot requires a separate evidence decision; it must not
silently reuse this assumption.

Numeric Odoo identifiers, principal identifiers, company identifiers, and
target-specific values are restricted target evidence. Impodo encrypts them in
the protected Project store. They never appear in a Recipe, portable mapping,
review workbook, ordinary browser projection, or another target's run.

Publication is atomic. Impodo computes the compact index integrity value while
streaming the index to protected storage and computes the manifest hash once
from its references. It reuses the existing hashes of referenced artifacts and
does not create a new root hash by rereading and rehashing every contributing
artifact.

At the start of a correction-review job, Impodo verifies each required artifact
through its owning store once before use. Browser rendering, progress polling,
classification, confirmation, and read-back must not repeat those full-file
hashing passes. A restarted job may verify its inputs again at its new trust
boundary.

### Current identity gap to close

The execution journal records an Odoo identifier for attempted create or
update rows. A row classified as unchanged may have no execution attempt,
although preflight already resolved its protected target identity. The target
index publisher must consolidate protected preflight identity, execution
receipts, and reconciliation read-back set-wise so every in-scope source row has
one exact target.

The publisher must not perform another business-key search to fill this gap. A
missing, ambiguous, cross-target, or contradictory identity makes the completed
load ineligible for correction. An absent or unverifiable completed prepared
snapshot has the same result.

## Correction lifecycle and invariants

### Derive progress from existing owners

The correction feature must not persist a second correction state machine. The
registry-owned `MigrationRun` lifecycle remains authoritative. The browser
derives correction progress from the current owner of each result:

| Data-manager progress | Authoritative current evidence |
| --- | --- |
| Editing the correction | The successor workspace and mapping draft |
| Reviewing | The resumable correction-review job |
| Ready to apply | One current correction plan with no blockers |
| Needs attention | The current review blockers or invalidated plan |
| Applying | The correction-scoped `ExecutionRun` |
| Verifying | Reconciliation for that execution |
| Complete or needs attention | The current reconciliation outcome |

The browser may use a projection that combines these facts, but that projection
is not another lifecycle authority. It cannot advance independently or create a
second meaning for a completed `MigrationRun`.

### Invalidation

Changing the corrected mapping invalidates correction preparation, quality,
comparison, confirmation, and execution planning. Changing the accepted source
creates a new DataVersion and exits this workflow. A changed target identity,
schema, company context, read-principal provenance, write-principal provenance,
credential generation, permission result, or freshly read affected field
invalidates confirmation and requires **Review correction** again.

Historical artifacts remain readable. Invalidation clears only the successor
run's current pointers and never deletes or rewrites the completed load.

### Field scope

The correction plan contains only fields for which corrected intent differs
from previous intent and current Odoo evidence permits a write. The executor
must not rebuild a full-record payload from the corrected prepared row.

This rule prevents an Active correction from rewriting a Description that the
new mapping did not change. It also reduces side effects, concurrency risk,
review noise, and Odoo request size.

Impodo computes the correction-plan hash once while it publishes the canonical
protected plan. Confirmation and execution compare the stored hash and bound
identities. They do not reconstruct and rehash every source or evidence
artifact.

## First scalar boundary

The first release supports only:

- one unchanged accepted file-source DataVersion;
- one exact Odoo 19 target used by the completed load;
- a completed load with known row and field outcomes;
- a direct physical dataset whose accepted mapping uses the native compiled
  columnar backend;
- eligible fields whose approved execution intent is exactly the value in the
  completed prepared Parquet snapshot, without a later resolution,
  normalization, or relationship-materialization change;
- field types whose governed canonical comparison is implemented with native
  Polars expressions;
- the same target-field scope in the completed and corrected mappings;
- update-only scalar corrections for an allowlisted field set;
- the same company context and permitted model scope; and
- Authoring against the existing disposable-target write policy.

The first release does not support:

- creates, deletes, archive or unarchive operations;
- identity-field corrections;
- moving a value from one Odoo target field to another;
- business-key fallback when a prior Odoo identifier is missing;
- source-row changes inside the same DataVersion;
- Python-fallback or derived/materialized transformation paths;
- fields whose final approved value was changed after the prepared Parquet
  snapshot by resolution or normalization;
- many-to-one, one-to-many, or many-to-many relationship corrections;
- creating, editing, merging, or deleting supporting Odoo records;
- computed, related, translated, company-dependent, or unqualified fields;
- arbitrary Odoo methods, imports, direct SQL, `sudo`, or browser automation;
- correction of an unresolved `OUTCOME_UNKNOWN`; or
- direct correction writes in Integrated Test or Production.

A Test or Production mistake instead creates corrected Authoring meaning, a
successor Recipe revision when reusable, a fresh Integrated Test
qualification, and a new Production rollout. The completed Test or Production
run remains immutable.

## Scalar acceptance and later Unit of Measure example

Use the motivating Products case as one sanitized acceptance fixture:

- 999 Product rows in the unchanged DataVersion;
- 768 rows with Product status code `10`;
- 141 rows with code `30`;
- 90 rows with code `90`;
- the completed mapping incorrectly made Active true only for code `60`;
- the corrected mapping makes Active true only for code `10`;
- 37 rows use source value `UNI`, which is retained for the later relationship
  qualification fixture; and
- Product descriptions use the same previous and corrected intent.

The expected first scalar correction review is:

- 768 Active updates;
- 231 Active values remain unchanged;
- no Product or supporting record is created; and
- no Unit of Measure or Description field is written.

After the scalar release qualifies, the separate many-to-one slice reuses the
same sanitized dataset. Its expected review proposes 37 Product relationship
updates to the exact standard Odoo Unit record. `PCE`, `Kg`, and `m` retain
their governed meanings. No Unit of Measure record is created, edited, merged,
or deleted. Cleanup of an accidentally created custom `UNI` Unit of Measure is
a separate governed task, not an implicit side effect of Product correction.

## Odoo 19, concurrency, and security boundaries

- The writer uses only the existing scoped Odoo 19 ORM-backed JSON-2 adapter.
- The correction domain and comparison services have no generic write client.
- The captured Odoo 19 schema decides whether a model and field are writable
  and supported. The plan cannot override readonly, computed, related,
  translated, company, access-control, or record-rule evidence.
- Review resolves a read-only target capability and records its credential
  generation and principal provenance. It cannot construct the correction
  writer.
- Apply separately resolves the existing narrow write capability and records
  its credential generation, write principal, permissions, and context. The
  successor run never inherits a credential or write confirmation from the
  completed run.
- The executor accepts only the exact model, record identifier, field, value,
  target fingerprint, and correction-plan hash reviewed by the data manager.
- The executor passes that protected record identifier directly to the scoped
  Odoo writer. It must not search for the row again by business key before the
  read, write, or read-back.
- A just-in-time bounded re-read compares every affected current field with
  the confirmed comparison. Any unexpected value invalidates the plan and
  blocks all writes in the first scalar delivery.
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

### Local intent comparison

- Generate corrected intent through the existing compiled columnar
  transformation program and prepared Parquet publisher. The first scalar
  release rejects an accepted-rule shape that would require the Python
  fallback.
- Lazy-scan previous intent `A` and corrected intent `C` from their immutable
  prepared Parquet snapshots. Validate stable source lineage, then project only
  that lineage and the allowlisted correction fields.
- Compare `A` with `C` through native Polars expressions. Filter unchanged
  intent before any Odoo record read and preserve bounded or streaming
  execution where the Polars operation supports it.
- Permit one existing integrity verification and one logical columnar scan of
  each prepared artifact per review attempt. Do not rescan either artifact for
  browser summaries, progress polling, confirmation, or read-back.
- Do not use a Python UDF, Python source-row classifier, per-row hash, database
  query, or repository lookup for the `A/C` comparison.

### Protected target comparison

- Join changed source lineage to the protected exact-target index set-wise.
  Do not resolve a candidate record again from its business key.
- Group exact Odoo identifiers by model, field scope, company context, and
  target binding before reading.
- Read bounded identifier pages and request only identity, concurrency, and
  affected fields. Convert each returned page to typed canonical values and
  classify `A/B/C` for the page set-wise.
- Read Odoo only for `A/C` candidates. Connector calls must not depend on the
  total source-row count when most intent is unchanged.
- In the later many-to-one slice, resolve each distinct corrected target key
  once per model, identity shape, scope, and company context. Join the protected
  result back to every affected Product row.
- Do not call schema inspection, permission inspection, `search_read`,
  `name_search`, repository lookup, or relationship resolution inside a
  source-row loop.

### Execution and verification

- Group compatible updates only when the existing execution journal can still
  prove the outcome of every row and field.
- Preserve journal-before-transport and stop-on-unknown behavior for every
  batching change.
- Reconcile by model and bounded field scope. Do not read back one row at a
  time.
- Publish request counts, row counts, changed-field counts, page counts, wall
  time, peak memory, prepared-artifact scan counts, and selected transformation
  backend for the acceptance fixture and a larger qualified run.

An implementation is not accepted merely because it contains a batch API.
Tests must prove upper bounds on schema, permission, relationship, comparison,
write, and read-back calls.

## Proposed implementation ownership

### Domain

Start with one small pure correction module under `src/impodo/domain/`. It owns:

- correction-origin manifest and protected target-index contracts;
- canonical `A/B/C` truth-table semantics that can be compiled into vectorized
  expressions;
- correction plan rows and field differences;
- conflict and blocker codes;
- invalidation inputs; and
- portable-versus-protected assertions.

The domain receives canonical field values and protected opaque target
references. It performs no Odoo, filesystem, DuckDB, browser, or credential
work. Do not create a correction aggregate, repository, or persisted state type
for each browser progress label.

### Application

Add one correction application service that coordinates:

- successor correction-run creation through the existing Project and run
  services;
- correction-origin manifest and protected target-index publication;
- prior-mapping seeding into a new workspace;
- the automatic validation, native columnar preparation, quality, and
  fresh-comparison pipeline;
- correction confirmation and execution-snapshot construction; and
- automatic reconciliation and restart recovery.

The orchestrator uses existing mapping, preparation, preflight, execution, and
reconciliation ports. It must not duplicate their business semantics or write
directly into another owner's repository. Split the service only when measured
responsibility or test isolation requires it; the plan does not require a new
application subsystem.

### Persistence and protected evidence

Extend the registry and Project-owned protected evidence through explicit
repositories and transaction ports:

- add a successor correction reference to `MigrationRun` or one run-owned
  correction binding without placing target evidence in the portable run
  projection;
- persist one immutable correction-origin manifest and one compact protected
  exact-target index;
- persist the correction plan and confirmation hash;
- support atomic publication, bounded reads, schema-version rejection, and
  historical read-back; and
- retain target-specific identifiers only in the encrypted protected store.

Do not add a second editable copy of mapping, prepared data, or reconciliation
rows to the registry database. Prefer one run-owned correction binding and the
two protected artifacts over a family of correction tables and repositories.

### Browser

Add a correction router, presenters, templates, and background-job projection
under the existing Project authorization boundary. The browser owns only:

- the entry action from a completed result;
- the focused rule editor;
- progress for **Review correction**;
- the compact correction summary and blocker list;
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

### Phase 1: seal completed work and publish lean origin evidence — complete

Enforce completed-run immutability in the application services before adding a
browser redirect. Every mutation entry point must reject a completed workspace.
Publish the correction-origin manifest and compact protected target index
atomically. Consolidate preflight target identities, execution receipts, and
reconciliation outcomes set-wise, including rows that required no write.

**Exit result:** the completed run remains historical evidence, every in-scope
source row has one exact protected target, and missing or contradictory evidence
fails closed. The implementation adds no duplicate prepared values and no
per-row or per-field hash tree.

Implemented by `domain/correction_origin.py`,
`application/correction_orchestration.py`,
`adapters/protected_correction_store.py`, and the single registry-owned
`correction_run_binding` table. Origin files are authenticated before their
registry reference becomes visible; a fault before the registry commit leaves
the run and workspace open, while replay returns the same binding. The
application and workspace-engine write boundaries reject later mutation of the
closed workspace. Integrity is one whole-index hash and one whole-manifest
hash; prepared values remain only in their existing Parquet artifacts.

### Phase 2: create and review a vectorized scalar correction — complete

Extend run purpose and lineage through the registry-owned run service. Create a
new Authoring run and workspace over the same DataVersion, seed the exact prior
mapping as a draft, and bind the correction-origin manifest without reopening
the completed run.

Build **Review correction** over the existing validation, native columnar
preparation, quality, target-check, and preflight services. Lazy-scan `A` and
`C`, reduce to changed intent with Polars, read `B` only for candidates, and
apply the canonical three-way truth table set-wise. Publish one deterministic
protected plan and its hash. Any conflict or blocker prevents confirmation.
Add invalidation and restart recovery without a new correction state machine.

**Exit result:** correcting the Product Active rule produces 768 safe updates,
231 unchanged fields, zero Description writes, and no manual traversal of the
six Authoring stages. Tests prove that the accepted fixture uses the native
columnar backend without Python row processing.

Implemented by `CorrectionSuccessorService`,
`CorrectionMappingSeedService`, `CorrectionAuthoringStageCoordinator`,
`NativeCorrectionReviewPipeline`, `CorrectionReviewOrchestrator`, and the
existing exact-ID review and plan services. The coordinator exposes one
application use case; Phase 4 still needs to compose it into the browser and
background progress presentation. The native reducer scans each previous and
corrected prepared Parquet artifact once, adapts only sparse changed rows, and
never introduces a Python source-row classifier, per-row hash, per-value hash,
or connector call inside a source-row loop. Saving semantically changed mapping
intent in the successor clears its prepared, plan, and confirmation pointers
before the draft write; a canonically unchanged save keeps current evidence.

### Phase 3: execute and automatically verify scalar corrections — complete

Construct a correction-scoped `ExecutionSnapshot` that carries protected exact
Odoo identifiers. Add a bounded just-in-time reread, require explicit
confirmation, resolve the narrow write capability separately, reuse
journal-before-transport execution, and start reconciliation automatically.
Neither execution nor reconciliation may search again by business key. Preserve
known-failure and `OUTCOME_UNKNOWN` semantics.

**Exit result:** only reviewed scalar fields are written to exact prior Odoo
records, and the service reports a verified or actionable final result without
a manual verification action.

Implemented by `domain/correction_execution.py` and
`application/correction_execution.py`, reusing `ExecutionRun`,
`ExecutionRowAttempt`, `ReconciliationRun`, and their existing DuckDB
repositories. The correction scope has no lookup fields. Exact IDs are read in
pages of at most 50; compatible identical sparse payloads are written in pages
of at most 50 after their rows are journalled `IN_FLIGHT`. A known rejection or
unknown response stops later batches, and exact-ID reconciliation starts
automatically. A verified result closes the successor run and workspace
through the registry-owned correction binding. Tests prove that 768 compatible
updates use 16 write calls and 32 total read calls: 16 for the just-in-time
gate and 16 for final verification. No per-row or per-value hash tree was
added.

### Phase 4: finish the focused browser journey — complete

Add **Correct this Odoo load**, the focused rule editor, resumable progress,
the compact correction review, **Apply N corrections**, and automatic outcome
presentation. Redirect attempts to edit a completed workspace into an
explanation and successor action. The application-level immutability guard from
Phase 1 remains authoritative.

Update current user and developer workflow pages, `docs/workflow.yml`, BPMN,
screenshots, contracts, architecture, Python code map, and acceptance guidance
only after each behavior is implemented.

**Exit result:** a data manager completes the supported scalar correction
without thinking about evidence stages, while every safety gate remains
provable.

Implemented by `application/correction_workflow.py`,
`application/correction_stages.py`, `application/correction_jobs.py`,
`web/routers/corrections.py`, and the correction templates. The Project page
shows the action only for a published eligible binding. A closed historical
mapping URL redirects to the correction explanation. The existing Match data
editor presents correction-specific guidance, while **Review correction**
resumes current mapping, Polars preparation, quality, and exact-ID target-read
owners in one background attempt. The compact review contains counts only and
**Apply N corrections** requires explicit confirmation plus a fresh write
probe. Zero-change review has no write action; blockers remove it. Current user
and developer workflow pages, workflow ownership, BPMN, code map, and browser
tests now describe and verify the implemented boundary.

### Phase 5: qualify exact-existing many-to-one corrections

Reuse the relationship resolver in bounded groups, preserve case-sensitive
matching, prefer one exact existing Odoo record over an identical proposed
supporting value, and bind the protected resolved identity into the correction
plan. Resolve each distinct key once and join it back to the candidate frame.
Never update or create the related record in this phase.

**Exit result:** the 37 `UNI` Products point to the exact standard Odoo Unit
record; `PCE`, `Kg`, and `m` retain their governed meanings; connector calls do
not grow one-for-one with Product rows.

### Phase 6: qualify scale and consider broader scopes

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
- Direct source, value-mapping, conditional Selection, constant, fallback, and
  casing edits produce the same field-difference contract when their corrected
  output changes.
- Changing the editor control without changing its canonical output produces no
  correction.
- Moving a mapping to another target field fails the first-release scope check
  instead of treating omission as a value to write.
- Canonically equal HTML or normalized values do not create a false correction.
- `A = C` excludes the field even when `B` differs.
- Different row order and page size produce the same plan hash.
- A corrected full prepared row emits only changed field differences.
- Missing, duplicate, or cross-target protected identity fails closed.
- Numeric Odoo identifiers cannot enter portable correction projections.
- Confirmation reads the authoritative current job, plan, execution, and
  reconciliation evidence and rejects an invalidated plan.

### Application and repository tests

- Correction-origin publication joins unchanged, created, and updated rows
  without a business-key fallback.
- Partial manifest or target-index publication, hash mismatch, missing index
  entries, and incompatible schema versions are rejected.
- Successor creation uses the same DataVersion and creates a new run,
  workspace, and mapping revision.
- Changing source input requires a new DataVersion.
- Mapping changes invalidate every downstream correction pointer.
- The scalar fixture selects the native compiled columnar backend. A Python
  fallback fails qualification instead of silently processing rows.
- Each prepared artifact receives at most one integrity verification and one
  logical Polars scan per review attempt. Progress, review rendering,
  confirmation, and read-back add no scans.
- Odoo reads contain only exact identifiers for changed-intent candidates.
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
- Any conflict or blocker removes the apply action in the first scalar release.
- A zero-change result has no write action.
- Confirmation is explicit, stale confirmation is rejected, and automatic
  verification reaches one final outcome.
- Browser projections contain no numeric Odoo identifiers or credentials.

### Odoo 19 integration and call-count tests

- The Active fixture updates the exact 768 Products and a repeat review finds
  zero corrections.
- After separate Phase 5 qualification, the Unit fixture updates exactly 37
  Product relationships without writing to `uom.uom`.
- A concurrent change to one affected field blocks the whole plan before the
  first write.
- A missing, archived, inaccessible, company-incompatible, or record-rule
  hidden Product produces a stable blocker.
- A known rejection remains distinguishable from a lost response.
- An unknown response stops all later correction writes and requires read-back.
- Schema, permission, relationship, comparison, and reconciliation calls obey
  explicit upper bounds and do not scale one-for-one with rows.
- Execution and reconciliation use the protected exact identifiers from the
  confirmed plan and perform no business-key search.

### Security and lifecycle tests

- Project authorization governs every correction route, job, artifact, and
  outcome.
- Read access never grants correction write access.
- Target and principal rechecks occur after confirmation and before transport.
- The writer rejects a different target, model, record, field, or plan hash.
- Project deletion and retention rules include correction-origin evidence and
  plans.
- Integrated Test and Production cannot construct the first-release correction
  writer through browser or direct service calls.

## Completion criteria

The first supported scalar correction workflow is complete only when:

- one immutable origin manifest references existing completed evidence and one
  compact protected index covers every in-scope source row and exact target;
- a successor run preserves the completed load unchanged;
- the data manager changes one rule and uses one **Review correction** action;
- corrected intent, current Odoo state, and previous intent are compared by
  field;
- previous and corrected intent use the compiled Polars and Parquet path without
  Python source-row processing;
- the plan contains zero creates and only exact protected target identifiers;
- scalar writes pass their Odoo 19 qualification independently of later
  relationship support;
- no target read or read-back N+1 path remains;
- integrity verification, Parquet scanning, and plan hashing obey their stated
  per-attempt bounds;
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
