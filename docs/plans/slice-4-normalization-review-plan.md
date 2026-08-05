# Slice 4 plan: normalization review and freeze

## Status and outcome

**Status:** Implemented on 2026-08-05 for the bounded 25,000-row browser
workflow.

The implemented checkpoint separates local preparation from the read-only
Odoo comparison, persists schema-v16 review evidence and lifecycle revisions,
and requires an exact current frozen result before Odoo can be read. The
data-manager page uses grouped business explanations, bounded examples, and
one next action; identifiers and hashes remain collapsed.

The 25,000-row automatic-change fixture completed in 37.045 seconds with a
309.9 MiB peak Windows working set and a 48.5 MiB project database. The source
evaluator still materializes validated tables within that explicit limit;
streaming remains a later ETL scale extension.

Slice 4 turns the exact canonical values already prepared by Impodo into a
reviewed and frozen normalization result. It does not add another
transformation language, edit registered source files, certify a clean package,
or write to Odoo.

The data-manager workflow becomes:

```text
Check all rows
-> save canonical staging and quality evidence
-> group the changes Impodo proposes
-> review only decisions that need attention
-> approve the complete prepared dataset
-> freeze its exact eligible-row hash
-> Compare with Odoo (separate read-only action)
```

The normal UI uses business labels and one obvious next action. Terms such as
`DryRun`, rule IDs, canonical hashes, evaluator versions, and lifecycle
transitions remain behind **Technical details**. The main approval button says
**Approve prepared data** and states openly that it does not send anything to
Odoo.

## Why this slice comes before Odoo export

Slice 3 proves which canonical rows are eligible and which are set aside. It
does not yet record that a data manager reviewed the transformations applied to
the eligible values. Building an Odoo writer before this boundary would leave
the executor without an exact, current, approved source dataset.

Slice 4 closes that source-side decision boundary. It still grants no Odoo
capability. Clean-package certification, target rehearsal, execution approval,
restricted writes, and reconciliation remain later gates.

## Decisions locked for this slice

### Reuse one evaluator

The canonical evaluator remains the single authority for proposed values.
Normalization evidence is emitted from the same full-row evaluation that
creates canonical staging; Slice 4 does not reload and transform the source a
second time.

The existing transformation-impact page remains a useful advanced projection,
but its display strings and singleton snapshot are not sufficient approval
evidence. Slice 4 adds exact canonical-row and rule bindings while reusing its
server-side filtering, paging, field labels, and CSV concepts.

### Approve values; do not edit them in place

The first integrated correction routes remain deliberately simple:

- accept a grouped proposed change;
- reject it and correct the registered source replacement, field match,
  governed value match, or rule before rerunning;
- acknowledge a review-only quality finding;
- leave unsafe records in the Slice 3 set-aside ledger.

There is no free-form cell editor. Decisions never mutate canonical staging or
quality evidence. Any correction produces a new staging, quality, and
normalization run.

### Review only the effective eligible dataset

The current quality run is the eligibility authority. Slice 4 may start only
when staging and quality evidence are current, completely reconciled, and free
of **Fix setup** blockers.

- `CANDIDATE` and `REFERENCE` rows enter the normalization review.
- `QUARANTINED` and governed `EXCLUDED` rows remain visible in reconciliation
  but do not enter the frozen eligible dataset.
- A physical row that fans out into several canonical records retains exact
  row-level links; one set-aside child cannot hide or authorize an eligible
  sibling.
- Failed declared business totals block review preparation.

### Four review outcomes

Every emitted effect or finding has one manager-facing outcome:

| Outcome | Meaning | Data-manager action |
| --- | --- | --- |
| **Applied by your rules** | A low-risk, deterministic change allowed by the confirmed mapping policy | Visible in the summary; no individual decision |
| **Needs your decision** | The change can affect business meaning, identity, relationships, or supplied values | Accept the group or send it back to fix |
| **Reviewed finding** | A Slice 3 warning did not change a value but requires acknowledgement | Confirm review or send it back to fix |
| **Fix first** | Evidence is stale, incomplete, invalid, colliding, or unreconciled | Correct the indicated source, match, or rule and rerun |

The complete run always needs one final approval, including when every change
was automatic or no value changed.

### Conservative first policy

The first policy is derived from the submitted mapping and is read-only in the
normal UI. It classifies effects as follows:

| Effect | Policy |
| --- | --- |
| Trim or collapse whitespace on a non-identity scalar field | Applied by your rules |
| Lossless type canonicalization on a non-identity scalar field | Applied by your rules |
| Any change to target identity, scope, or an incoming relationship key | Needs your decision, regardless of operation |
| Search/replace, formula, case conversion, rounding, empty-to-null, fallback, constant, or reviewed value match | Needs your decision |
| Quality warning from the exact current quality run | Reviewed finding |
| Parse failure, unknown lookup, relationship ambiguity, identity collision, missing context, or stale evidence | Fix first or remain set aside under the current quality policy |
| Odoo-default intent | Listed as target-supplied intent; Slice 4 never claims to know or approve the eventual Odoo value |

New transformation families must declare their review policy before they can
enter full-row execution. There is no permissive “unknown means automatic”
fallback.

Each confirmed field plan receives one stable rule-chain ID derived from its
ordered providers and transformations. The strictest member of that chain
governs the group: an automatic trim followed by a required replacement is one
decision-required group, never two misleading approvals.

## Versioned normalization evidence

Evolve the existing governance contract instead of creating a parallel
lifecycle. The integrated contract adds deterministic, portable objects around
the current immutable `DryRun` transitions:

- `NormalizationPolicySet`: the versioned classification of existing mapping
  operations into automatic, decision-required, target-supplied, or blocking;
- `NormalizationEffect`: one affected canonical row and field, exact rule
  binding, before/after evidence policy, identity-impact flag, and outcome;
- `NormalizationReviewFinding`: one grouped review-only quality warning;
- `NormalizationReviewGroup`: a business-labelled aggregate with affected,
  set-aside, sample, and collision counts;
- `NormalizationRun`: the existing dry-run lifecycle plus exact staging,
  quality, mapping, schema, policy, evaluator, source, and retention bindings;
- `FrozenNormalization`: the final approval evidence and exact eligible
  canonical-dataset hash.

Portable summaries and approval evidence contain no raw customer values,
credentials, or numeric Odoo IDs. Protected row-level evidence may retain the
minimum before/after display needed by the data manager, subject to project
classification, masking, and retention. Audit messages record group labels,
counts, actors, and hashes—not source values.

### Deterministic hashes

Keep lifecycle metadata separate from semantic content:

- the normalization evidence hash covers sorted effects, review findings,
  group counts, policy, and all upstream bindings;
- actor identity, approval time, and optional reason are append-only lifecycle
  evidence and do not change the deterministic evaluation hash;
- the eligible dataset hash covers every eligible canonical row ID, dataset,
  target model, business identity and scope, proposed typed values, symbolic
  relationships, and the bound quality content hash in deterministic order;
- freezing records both the eligible dataset hash and current normalization
  evidence hash.

The frozen hash is normalization evidence only. It is not the later clean
package hash because it does not include a target snapshot, Odoo action plan,
package manifest, rehearsal, or execution authorization.

## Persistence and invalidation

Add a schema-v16 migration with project-scoped DuckDB tables for:

- immutable normalization-run headers and current pointer;
- immutable row-level effects and review findings;
- grouped summary rows and bounded example links;
- append-only decision, approval, freeze, rejection, supersession, and
  invalidation transitions;
- deterministic policy and eligible-dataset hashes.

Do not overload the project's generic `current_run_id` or
`approval_status` as the normalization authority. The normalization current
pointer and append-only evidence are authoritative; project-level status is a
derived display summary only.

Preparation and every lifecycle transition are atomic and optimistic:

- identical current inputs reuse the current deterministic evaluation;
- a stale browser form cannot decide or approve a newer run;
- a failed batch or transition leaves the prior current state untouched;
- decisions and whole-run approval use the existing
  `normalization.decide` and `normalization.approve` capabilities;
- the UI may combine whole-run approval and hash freeze into one transaction,
  while the domain still enforces both transitions.

### Invalidation matrix

| Change | Current normalization result |
| --- | --- |
| Source selection/content, derived plan, mapping, schema, canonical staging, quality rules/result, normalization policy/evaluator | Invalidate and retain history |
| Data manager, functional owner, data classification, or retention policy | Invalidate approval eligibility and retain history |
| Accepted/rejected group or reviewed-warning decision | Append a new lifecycle revision |
| Identical rerun with identical deterministic evidence | Reuse current evaluation; never duplicate decisions silently |
| Refreshed Odoo record values for an unchanged target contract | Preserve normalization; invalidate only target-dependent preflight evidence |
| Odoo URL, database, permitted models, or captured target schema | Invalidate because the submitted mapping and prepared field meanings are target-contract-bound |
| Browser filter, page, search, or CSV download | No lifecycle effect |

Rejected evidence is never changed back to approved in place. Correcting the
source, mapping, value match, or policy creates a new run.

## Data-manager UI

### Split preparation from Odoo comparison

Change **Check all rows** so it completes target-independent staging, quality,
and normalization preparation without reading Odoo records. When review is
needed, the Review page's single primary action becomes **Review prepared
changes**.

After a successful freeze, the single primary action becomes **Compare with
Odoo**. That action continues to use the existing batched, read-only
compatibility path until Slice 5 reads frozen canonical rows directly.

### Review prepared data page

Use one page with four plain summary cards:

- **Records ready**;
- **Changed by your rules**;
- **Decisions left**;
- **Set aside**.

A blocking banner replaces the action with **Fix first** when necessary. Group
cards show:

- business table and field labels;
- a plain explanation such as “Remove extra spaces” or “Replace source codes”;
- affected eligible-record count and set-aside count;
- up to five masked before/after examples;
- owner label and one recommended next action.

Decision-required correction groups use **Accept this change** and **Send back
to fix**. Review-only findings use **I reviewed this** and **Send back to fix**.
An optional explanation opens only on demand. There is no actor picker, rule-ID
field, JSON, SQL, Odoo domain, model name, hash, or separate “freeze” button.

Once all required groups and findings are reviewed, show one sticky primary
action:

> **Approve prepared data**  
> Saves this exact reviewed dataset. Nothing is sent to Odoo.

If there are only automatic changes—or no changes—the page still shows the
summary and that one final confirmation.

### Progressive disclosure

- Inline examples are bounded to five per group.
- **See affected records** opens the existing server-paged evidence view,
  filtered to the exact group.
- Technical row IDs, rule IDs, policy versions, hashes, fan-out pointers, and
  transition evidence remain collapsed.
- Sensitive values follow the strictest project/field display policy. Masked
  values are never unmasked merely because a CSV export was requested.

## Services and execution boundaries

Add a storage-independent normalization evaluator/service that accepts the
exact staged mapping result and current quality run. It emits effects, grouped
review evidence, and the eligible dataset hash without repository, browser, or
Odoo dependencies.

Effect candidates are emitted during the canonical source pass through a
storage-neutral bounded sink or iterator. Quality then assigns eligible and
set-aside counts without rereading source artifacts. Wide inputs must not force
all field effects into one additional in-memory copy.

The application service owns:

- current-evidence validation;
- normalization preparation/publication;
- group decision and reviewed-finding commands;
- whole-run approval plus freeze;
- current summary and bounded-page queries;
- eligibility proof required before read-only Odoo comparison.

Repository queries and inserts are grouped and bounded. Cross-row identity
effects use one grouped operation or one bounded index. There is no database or
connector query per row, and normalization makes no Odoo call.

## Implementation sequence

### 4A — Harmonize contracts and policy

Extend the existing governance domain with current staging/quality bindings,
review-only findings, deterministic serialization, policy versioning, and the
frozen eligible-dataset contract. Preserve the current capability separation
and immutable transition model.

**Gate:** unsupported or unclassified operations fail closed; no second
lifecycle or transformation language exists.

### 4B — Emit exact normalization effects

Extend the canonical evaluation result so the same scalar execution emits
canonical-row-bound effects and governed before/after display evidence. Build
automatic, required, reviewed-warning, target-supplied, and blocking groups.
Reconcile effects to eligible and set-aside row IDs and compute the eligible
dataset hash.

**Gate:** the approved proposed value is byte-for-byte the staged canonical
value; every group count resolves to exact row evidence; mixed fan-out is
record-specific.

### 4C — Persist runs and immutable transitions

Add schema v16, atomic bulk publication, current pointers, append-only
transitions, optimistic lifecycle versions, bounded review queries,
supersession, and invalidation. Migrate existing projects without presenting
old readiness or generic approval state as normalization approval.

**Gate:** restart retrieval is complete, stale forms fail, failed writes keep
the prior current run, and changed upstream evidence invalidates approval
without deleting history.

### 4D — Integrate decisions, approval, and freeze

Implement application-service commands around the existing domain methods.
Require current evidence and capabilities on every command. Combine final
approval and freeze atomically for the browser while retaining separate domain
evidence. Add the frozen-normalization prerequisite to the read-only Odoo
comparison entry point.

**Gate:** pending or rejected groups, unreviewed quality findings, collisions,
stale hashes, or missing capabilities cannot produce frozen evidence or start
Odoo comparison.

### 4E — Add the data-manager review journey

Add the Review-page summary, dedicated grouped review page, bounded examples,
plain decision buttons, progress state, recovery messages, and collapsed
technical evidence. Reuse the existing visual language and server-side paging.

**Gate:** a data manager can complete the normal flow without knowing rule
codes, field IDs, hashes, lifecycle states, or Odoo model names, and each page
offers one primary next action.

### 4F — Acceptance and scale closure

Run deterministic, restart, invalidation, concurrency, rollback, masking,
warning, collision, fan-out, capability, empty-change, auto-only, required,
rejection, and freeze fixtures. Repeat the integrated 25,000-row measurement
with effect capture and persistence.

**Gate:** the current 25,000-row browser scope remains supported or is lowered
to the highest completed probe; persistence and review paging are bounded; no
N+1 database or Odoo access is introduced; the full suite passes.

For the existing three-column workstation fixture, target acceptance is no
more than 60 seconds end-to-end, less than 512 MiB peak RSS, and less than
128 MiB project-database size. These are local regression guards, not
production sizing guarantees.

## Required acceptance cases

- identical inputs produce identical normalization evidence and eligible
  dataset hashes;
- a run with no changed values still requires one final confirmation and can
  freeze;
- automatic-only changes require no individual group decisions but remain
  visible;
- every identity, scope, relationship-key, formula, replacement, fallback,
  rounding, constant, and value-match change follows the conservative policy;
- an unknown transformation policy blocks rather than becoming automatic;
- required groups cannot be bypassed or approved twice;
- review-only quality warnings require exact current evidence and explicit
  acknowledgement;
- rejection blocks that run and routes the user to source, mapping, or rule
  correction;
- an identity collision still present in the eligible projection blocks even
  an automatic rule; a complete collision group already set aside by Slice 3
  stays visible without blocking the unrelated eligible remainder;
- Slice 3 set-aside rows remain accounted for and never enter the eligible
  dataset hash;
- mixed fan-out decisions apply to exact canonical row IDs, not only physical
  source coordinates;
- failed business totals, unreconciled rows, stale quality, or missing context
  prevent review preparation;
- changed source, derived plan, mapping, schema, staging, quality, policy,
  ownership, classification, or retention invalidates approval eligibility;
- changed Odoo target evidence does not invalidate source normalization;
- stale concurrent decisions and partial writes preserve the prior current
  state;
- reviewer and whole-run approver capabilities remain independently enforced;
- approval plus freeze is atomic and survives restart;
- protected evidence and CSV downloads enforce masking and retention;
- the normal UI exposes business labels, bounded examples, progress, and one
  primary action while hiding technical identifiers;
- normalization evaluation makes zero Odoo calls;
- read-only Odoo comparison refuses to start without the exact current frozen
  normalization result;
- freezing grants no export-plan approval or Odoo write capability;
- schema-v15 projects migrate safely and show normalization as not started.

## Explicit blind spots this plan closes

- The current `DryRun` binds source and rules hashes but not current staging or
  quality evidence; Slice 4 adds those bindings.
- The current `DryRun` keys source hashes by filenames; the integrated envelope
  uses stable source-file IDs and hashes, with display names kept as labels.
- The current transformation-impact snapshot is display-oriented, uses
  technical rule strings, and lacks canonical row IDs; it cannot be promoted
  directly into approval evidence.
- The existing project `approval_status` is also used by readiness and is too
  ambiguous to be authoritative for normalization.
- Slice 3 warning outcomes need an acknowledgement path; otherwise “Ask me to
  review” can never progress safely.
- Physical-row coordinates are insufficient for mixed parent/child fan-out;
  every effect and decision must bind the exact canonical record.
- Target changes and source-normalization changes have different invalidation
  scopes and must not erase each other's valid evidence.
- DuckDB's Python row-by-row insertion path is too slow at the supported scale;
  normalization evidence must use the bounded bulk relation pattern proven in
  Slice 3.

## Out of scope

- free-form spreadsheet or inline canonical-value editing;
- arbitrary Python, SQL, server actions, formulas, or unbounded expressions;
- automatic fuzzy matching, survivorship, merging, or business-rule guessing;
- new quality-rule or transformation authoring families;
- clean-package certification or target rehearsal;
- Odoo create, update, unlink, generic RPC, or production execution approval;
- user-directory, email, or multi-person workflow administration;
- streaming beyond the supported browser limit.

## Definition of done

Slice 4 is complete when a data manager can start from the current Review page,
understand every material proposed change in business language, decide only the
groups that need attention, acknowledge current review findings, and approve
one exact eligible canonical dataset. The frozen result must survive restart,
invalidate correctly, preserve all set-aside accounting, remain deterministic,
and unlock only the existing read-only **Compare with Odoo** action—never an
Odoo write or clean-package claim.
