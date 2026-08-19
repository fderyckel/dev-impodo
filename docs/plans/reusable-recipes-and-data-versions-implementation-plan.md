# Recipe-first test-to-production implementation plan

## Status and authority

**Status:** Active implementation plan from 2026-08-19 and the only current
product-delivery priority.

Phases R0 through R6, including the R4.5 clean-root consolidation, completed on
2026-08-19. Phase R7 is the next implementation phase. The active contracts are
in the
[Recipe-first Phase R0 contract](reusable-recipes-phase-r0-contracts.md).

Product ownership replaced and removed the earlier project-series proposal with
a Recipe-first architecture. The mapping contract v11 work completed on
2026-08-18 remains valid foundation work.
The aggregate, target, credential, qualification, and cutover boundaries are
accepted in
[ADR-013](../decisions/README.md#adr-013--recipe-is-the-aggregate-root-and-target-bindings-are-application-specific).

Until the definition of done in this plan passes, the authoritative
[Impodo remaining work](remaining-work.md) defers the related/mixed 100,000-row
qualification, general Odoo-source guarded updates, optional certification,
general remote-production hardening, target-side gateway, and hosted
composition tracks. Existing implemented limits and security controls remain
in force; deferral is not permission to weaken them.

This plan is authoritative for the first complete workflow in which a data
manager:

1. authors a reusable Recipe with representative migration data;
2. applies and fine-tunes immutable Recipe revisions against a remote Test Odoo
   server;
3. qualifies one exact revision from successful execution and reconciliation;
4. pins that qualified revision as the rollout candidate;
5. uploads the latest same-format-kind data on rollout day;
6. binds the Recipe to a different Production Odoo server using current,
   independently supplied API credentials;
7. reviews only source or target drift; and
8. explicitly approves, executes, and reconciles the production load using
   entirely fresh evidence.

## 1. Product outcome

Impodo's primary business object is the **Recipe**: the reusable migration
knowledge that a data manager authors, tests, qualifies, applies, and improves.

The intended product promise is:

> Build and qualify the Recipe before rollout. On rollout day, apply that exact
> qualified revision to the latest data and current Production Odoo server,
> review only drift, and execute with fresh approval and reconciliation.

The main lifecycle is:

```text
Recipe Draft
    |
    | publish reusable meaning
    v
Recipe Revision
    |
    | apply with representative data + Test Odoo binding
    v
Test Recipe Application
    |
    | prepare, compare, execute, reconcile
    v
Qualification Evidence
    |
    | explicitly select
    v
Cutover Candidate
    |
    | latest data + Production Odoo binding + new credentials
    v
Production Recipe Application
    |
    | fresh compare, approval, execution, reconciliation
    v
Completed rollout
```

This is not project cloning. It is not reuse of a prior approval, target
snapshot, API key, or execution package. It is deterministic recompilation of
portable business meaning against new exact source and target evidence.

## 2. First-release scope

The first release supports:

- complete replacement CSV/XLSX source packages;
- one Recipe containing one or more logically related datasets;
- source packages that retain the same logical purpose and approximately the
  same shape while allowing controlled, reviewed drift;
- remote Test and Production Odoo 19 servers with different endpoints,
  databases, principals, permissions, and API keys;
- multiple immutable Recipe revisions produced by a data manager's test and
  fine-tuning loop;
- one exact test-qualified revision pinned for rollout;
- fresh data-version parameters and control expectations;
- fresh target schema, reference, permission, comparison, approval, execution,
  and reconciliation evidence for every application; and
- existing `UPSERT`, `CREATE`, and `REFERENCE` mapping modes that satisfy the
  Recipe eligibility contract.

Examples include:

- Customers;
- Products;
- Product plus BOM when their source preparation and dependency order belong
  to one reusable unit; and
- stock levels applied for a declared warehouse and as-of date.

The first release does not include:

- unattended or scheduled production execution;
- inferred deletion from records absent in a replacement export;
- delta-file semantics;
- sharing a Recipe across unrelated authorization tenants;
- a public Recipe catalogue;
- automatic fuzzy source or target binding;
- arbitrary Odoo RPC or caller-selected methods;
- automatic promotion from Test to Production;
- reuse of Test credentials, approvals, snapshots, write journals, or target
  record IDs in Production;
- support for materially different business purposes under one Recipe; or
- orchestration of several independent Recipes as one cutover plan.

A thin multi-Recipe cutover plan may be considered only after Customers and at
least one related or parameterized Recipe complete this plan independently.

## 3. Product vocabulary

### 3.1 Recipe

The aggregate root and operator-facing identity for one reusable migration
purpose, such as **Customer migration** or **Opening stock by warehouse**.

A Recipe owns its name, purpose, classification, retention policy, optimistic
revision, current RecipeRevision pointer, optional rollout-candidate pointer,
Recipe revision lineage, DataVersion lineage, and bounded status projections.
It does not own one fixed Odoo endpoint, database, credential, source file, or
approval.

### 3.2 RecipeDraft

Mutable authoring coordination for the next RecipeRevision. In the first
release it references the current contained workspace's exact authoring
evidence rather than duplicating mapping, preparation, quality, reference, and
control drafts into a second source of truth.

Publishing compiles that exact current authoring state into an immutable
RecipeRevision. A stale or incomplete draft cannot publish.

### 3.3 RecipeRevision

An immutable, append-only, content-hashed version of reusable migration
semantics. It contains no server endpoint, database, credential, authenticated
principal, source snapshot, target snapshot, approval, execution result, or
numeric Odoo ID.

### 3.4 DataVersion

One exact source package used to author, test, rehearse, or roll out a Recipe.
Examples include **Rehearsal export - 18 August** and **Rollout export - 31
August**.

Each DataVersion has an independent `data_version_id` and owns one existing
contained `MigrationProject` workspace through `workspace_project_id`. The
workspace retains its own DuckDB, artifacts, credentials, audit, singleton
current pointers, and exact evidence chain.

### 3.5 MigrationProject workspace

The existing `MigrationProject`, identified by `project_id`, remains the
internal containment, authorization, credential, filesystem, and evidence
boundary. It is not renamed to Recipe or DataVersion and is not exposed as the
primary product concept.

### 3.6 OdooTargetContract

The environment-neutral Odoo requirements stored in a RecipeRevision:

- Odoo major version;
- required applications, models, and technical fields;
- field types, relations, required/readonly semantics, and intended write use;
- ordered business keys and scope;
- required selection codes;
- required custom fields or module-dependent capabilities;
- reference dependencies; and
- approved write fields.

It describes what a compatible Odoo target must provide. It does not identify
where that target is hosted or who may access it.

### 3.7 TargetBinding

The exact, application-specific binding to a current Odoo server. It includes
non-secret endpoint/database identity, connection-target hash, credential
generation, authenticated principal, permissions, context, schema snapshot,
reference snapshots, probe evidence, and timestamps.

A Test binding and a Production binding are always distinct even when they
happen to use similar settings. A credential rotation creates a new binding
generation.

### 3.8 RecipeApplicationDraft

A mutable, project-local recovery object used while logical datasets and
columns are bound to one DataVersion and one TargetBinding. It stores only
binding overrides, dependency hashes, bounded issue fingerprints, optimistic
revision, and recovery state. It cannot authorize preparation or Odoo writes.

### 3.9 RecipeApplicationEvidence

Immutable evidence explaining how one RecipeRevision, DataVersion, parameter
set, and TargetBinding produced one exact `MappingDefinition` or a terminal
blocked assessment.

### 3.10 RecipeQualificationEvidence

Immutable evidence that one RecipeRevision successfully passed the declared
Test Odoo rehearsal policy. It binds the application, preparation, quality,
comparison, execution, read-back, reconciliation, controls, actor, and exact
Test TargetBinding used.

Qualification demonstrates tested logic. It is not Production authorization.

### 3.11 CutoverCandidate

An explicit pointer to one test-qualified RecipeRevision and its qualification
evidence. It pins reusable semantics for rollout but contains no Production
server, API key, source file, comparison, approval, or write authority.

### 3.12 MappingDefinition

The compiled, evidence-bound result of applying a RecipeRevision. It correctly
contains the exact source-selection hash, schema hash, physical dataset/column
bindings, and mapping ID required by the existing evidence pipeline. It is not
the reusable Recipe.

## 4. Architecture decision

### 4.1 Recipe is the aggregate root

There is no `ProjectSeries` aggregate in the active design. The earlier series
added an identity above a lineage that already has one Recipe and one current
RecipeRevision. Recipe now owns the lineage directly:

```text
Recipe
|-- RecipeDraft
|-- RecipeRevision 1
|-- RecipeRevision 2
|-- RecipeRevision 3  [test-qualified, cutover candidate]
|-- DataVersion 1     [authoring/rehearsal workspace]
|-- DataVersion 2     [later rehearsal workspace]
`-- DataVersion 3     [rollout workspace]
```

`recipe_id`, `data_version_id`, and `workspace_project_id` are independently
generated UUIDs. Routes, repositories, authorization checks, logs, and
deletion services never accept one identity type where another is required.

### 4.2 Preserve contained workspaces

Every DataVersion receives a clean project workspace. Do not add
`data_version_id` to every existing project table and do not clone a prior
project database.

The contained workspace preserves existing singleton pointers and exact
invalidation behavior. A new workspace prevents old source rows, mappings,
quality results, approvals, target snapshots, credentials, execution journals,
or current pointers from becoming current for the new DataVersion.

### 4.3 Compile through the existing pipeline

Recipe application creates a normal fresh `MappingWorkingDraft` only after
structural binding is complete. The existing pipeline remains authoritative:

```text
RecipeRevision
    + DataVersion
    + ParameterValues
    + TargetBinding
             |
             v
RecipeApplicationService
             |
             v
MappingDefinition / MappingWorkingDraft
             |
             v
submission -> preparation -> quality -> comparison -> execution -> reconciliation
```

There is no parallel Recipe execution engine.

### 4.4 Keep portable meaning separate from exact evidence

RecipeRevision stores reusable intent. DataVersion, TargetBinding,
RecipeApplicationEvidence, RecipeQualificationEvidence, and the current
workspace store exact evidence.

Reusable configuration never includes:

- source or target rows;
- source-selection, snapshot, or full-schema hashes;
- server endpoints or database names;
- API keys or secret references that could authorize access;
- credential generations, principals, or permission snapshots;
- numeric Odoo IDs;
- data-version control expectations;
- comparisons, approvals, execution snapshots, journals, or reconciliation
  outcomes; or
- observed normalization corrections unless explicitly promoted to supported
  Recipe semantics.

## 5. Composite RecipeRevision contract

### 5.1 Reusable semantic composition

`RecipeDefinition` is independent of `MappingDefinition` and composes strict
subcontracts:

- `SourceShapeRecipe`;
- `RecipeParameterDefinitions`;
- `SourcePreparationRecipe`;
- `MappingRecipe`;
- `OdooTargetContract`;
- `TargetGovernanceRecipe`;
- `QualityRecipe`;
- `ReferenceDependencies`; and
- `ControlDefinitions`.

Every semantic node uses a deterministic logical ID. Physical file IDs,
dataset IDs, column stable keys, project IDs, random rule IDs, project-local
reference IDs, engine decisions, and ordinals never become reusable identity.

The Recipe remains execution-engine neutral. It specifies what must happen,
not whether the workspace uses Python, Polars, DuckDB, Parquet, PostgreSQL, or
another supported implementation.

### 5.2 Hashes

Every RecipeRevision has:

- `semantic_hash`, covering canonical reusable business meaning; and
- `payload_hash`, covering complete stored bytes including compatibility hints
  and provenance.

Changing transformations, value matches, identities, target dependencies,
quality rules, references, parameter definitions, or control definitions
changes the semantic hash. Changing author, timestamp, origin workspace,
prior file name, endpoint, database, API credential, or provenance does not.

Reads verify payload integrity before parsing and recompute semantic identity
after canonicalization.

### 5.3 Declared parameters

A Recipe may declare bounded parameters that legitimately vary by DataVersion,
such as:

- export/as-of date;
- warehouse business key;
- company business key;
- batch reference;
- effective date; and
- other reviewed semantic constants whose variability is intentional.

Each definition contains a logical parameter ID, label, type, required flag,
validation constraints, allowed use sites, and whether it participates in
identity, scope, transformations, references, or controls.

Each DataVersion supplies fresh `RecipeParameterValues` with actor, source,
reason, timestamp, and content hash. Values bind application and compiled
mapping evidence. They do not create a new RecipeRevision when they remain
within the declared parameter contract.

Undeclared mutable constants are forbidden. A proposed new parameter changes
Recipe semantics and requires a new revision and test qualification.

### 5.4 Controls

RecipeRevision stores reusable control definitions: logical control ID, name,
dataset, field, unit, tolerance, and calculation.

Each DataVersion supplies fresh expected values unless the Recipe explicitly
declares an invariant expectation. Prior expected totals never transfer
silently from Test to Production or from one export to another.

### 5.5 References and relationships

Recipes store portable strings, ordered business keys, scope, and protected
content-addressed reference dependencies. They never store numeric target IDs.

Application resolves current target records in bounded batches under the
current TargetBinding. Missing or ambiguous Production references block that
Production application even when the Test rehearsal resolved successfully.

## 6. Same-format-kind source compatibility

Recipe application is conservative and deterministic:

1. bind datasets by confirmed logical name;
2. bind columns by exact unique source name;
3. allow reordered columns after exact logical binding;
4. treat candidate-type inference drift as review unless provider semantics
   prove incompatibility;
5. require explicit confirmation for a renamed used column;
6. reject duplicate headers before application;
7. block a missing dataset or used column;
8. report new unused datasets or columns as information;
9. rebuild all supported derived and related datasets from the new source; and
10. scan categorical domains set-wise and surface only new or stale choices.

Fuzzy or AI suggestions may be displayed as bounded, non-authoritative help,
but never become bindings without explicit confirmation.

A missing prior business key is not a delete instruction. At most it appears
in an informational cross-version summary.

## 7. Odoo portability and compatibility

### 7.1 Recipe target requirements

The Recipe's `OdooTargetContract` retains stable technical model and field
names, types, relation models, business-key order, scope, selection codes,
required/readonly semantics, custom dependencies, and intended write use.

It does not retain a whole prior schema as the compatibility rule. Application
checks only required dependency semantics, then binds the compiled mapping to
the complete current schema/governance hash.

### 7.2 Test and Production are independent bindings

Test qualification records the exact Test TargetBinding as provenance. It does
not make that target part of Recipe semantic identity.

Production application always:

- accepts or selects current Production connection settings;
- establishes a current credential generation;
- probes and captures the authenticated principal and permissions;
- captures fresh required schema and reference evidence;
- compares required semantics with the Recipe target contract; and
- creates new application, comparison, approval, execution, and reconciliation
  evidence.

An unrelated Production schema addition does not block. A missing custom
field, changed relation, readonly intended write, newly required field without
a provider, unavailable selection code, or missing/ambiguous governed target
key does block.

A Production incompatibility blocks only that application. It does not rewrite
or retroactively invalidate honest Test qualification evidence.

## 8. Credential and remote-server contract

### 8.1 Credentials are never Recipe content

Remote API keys remain only in the governed secret store. Recipe, registry,
application, qualification, browser, logs, errors, URLs, and exported evidence
must not contain secret material.

Non-secret evidence may contain:

- connection-target hash;
- credential role and generation;
- storage class;
- principal hash;
- permission hash;
- context hash;
- probe result and timestamp; and
- revocation/removal receipt hashes.

### 8.2 Different server means new credentials

No credential is copied automatically from Test to Production or between
different endpoint/database identities. The operator establishes the current
Production read credential explicitly. Write authority is established
separately at the existing load boundary.

Even when the same secret text is supplied for read and write, the bindings
remain separate evidence roles.

### 8.3 Rotation creates a new binding generation

When an API key changes on the same server, Impodo creates or selects a new
credential generation and must:

1. re-probe connectivity;
2. recapture principal and permission evidence;
3. refresh target-dependent schema and reference evidence;
4. invalidate comparison or load readiness bound to the prior generation; and
5. leave RecipeRevision and source evidence unchanged.

If credential generation, connection target, principal, permissions, or target
context changes after comparison and before load, execution stops. The
operator must refresh target evidence and regain exact current readiness.

### 8.4 Read and write separation

Read credentials support schema inspection, reference resolution, comparison,
and reconciliation reads. They do not authorize writes.

Write credentials are requested or established only for explicit execution,
receive a new probe, bind the execution snapshot, and never inherit authority
from Test qualification, a prior DataVersion, or a read credential.

Unknown remote write outcomes continue to use the existing execution journal
and reconciliation recovery. Recipe reuse never authorizes blind retry.

## 9. Authoring, testing, qualification, and rollout lifecycle

### 9.1 Create and author

The normal journey starts with **Create Recipe**. Impodo natively creates the
Recipe root, DataVersion 1, and its clean contained workspace as one recoverable
creation operation.

The data manager completes source inspection, Odoo target inspection, mapping,
preparation, quality, references, parameters, and controls through existing
workspace boundaries. RecipeDraft is a coordination projection over those
exact drafts and evidence.

### 9.2 Publish a revision

Only a current, submitted, Recipe-eligible mapping plus all supported reusable
preparation, quality, reference, governance, coverage, parameter, and control
configuration may publish.

Publication compiles logical reusable meaning, verifies eligibility and bounds,
stores the protected payload, records provenance, and advances the current
RecipeRevision through an idempotent publication intent.

An interruption after mapping submission leaves the mapping submitted and the
publication intent recoverable. Recovery either finishes the exact operation or
abandons it without pointing current at a missing or corrupt payload.

### 9.3 Test and fine-tune

The data manager selects or enters a Test TargetBinding and applies the current
RecipeRevision to representative data. Every application creates fresh exact
mapping and downstream evidence.

If the rehearsal exposes incorrect transformations, relationships, controls,
or quality semantics, the data manager creates a RecipeDraft based on the
current revision, corrects it, publishes the next revision, and tests again.

Example:

```text
Recipe v1 -> Test failed: country mapping incomplete
Recipe v2 -> Test failed: Product Category hierarchy incorrect
Recipe v3 -> Test execution and reconciliation passed
```

Prior revisions and test evidence remain immutable.

### 9.4 Qualification

A RecipeRevision is test-qualified only when the configured policy proves:

- the exact application completed without unresolved blockers;
- preparation and mandatory quality evidence are current;
- every required control reconciles;
- comparison is current for the exact Test TargetBinding;
- the authorized Test execution reached terminal known outcomes;
- read-back and reconciliation account for every proposed record;
- unknown writes are resolved;
- required expected outcomes match; and
- the qualifying actor explicitly confirms the result.

Where the Test target can be reset or recreated, the qualification records its
baseline identity. A repeated preview after successful execution should
propose no unexpected writes. The first release records an idempotency result
when the chosen recipe mode and test-target policy permit it; it never fabricates
one where the target cannot be reset or compared safely.

Qualification statuses are derived as `UNTESTED`, `TEST_FAILED`,
`TEST_QUALIFIED`, or `QUALIFICATION_STALE`.

Publishing any later semantic revision makes that new revision `UNTESTED`; it
does not mutate the prior revision's qualification history.

### 9.5 Select the cutover candidate

The data manager explicitly selects one `TEST_QUALIFIED` revision. The
CutoverCandidate pins the Recipe revision and qualification evidence.

Selecting a candidate does not reserve Production credentials, approve a
future source package, or authorize a write. Replacing the candidate is an
explicit actor-bound action.

### 9.6 Run with latest data

The primary rollout action is **Run with latest data**:

1. pin the selected cutover candidate;
2. reserve a new DataVersion and clean workspace;
3. upload the complete latest source package;
4. enter fresh parameter values and control expectations;
5. establish and probe the Production TargetBinding;
6. capture current Production schema and reference evidence;
7. apply the exact RecipeRevision;
8. review only structural, categorical, parameter, target, or credential drift;
9. submit the fresh compiled mapping;
10. prepare, run quality, and compare with Production Odoo;
11. establish explicit current Production write authority;
12. confirm and execute the exact current load snapshot; and
13. reconcile every write outcome.

Day-of semantic editing is exceptional. A one-off physical column binding may
remain application evidence. A reusable semantic correction creates a new
RecipeRevision, which is not automatically test-qualified or selected for
cutover.

## 10. Persistence and recovery design

### 10.1 Registry metadata

Add a transactionally versioned registry migration and bounded tables.

`recipe`

- `recipe_id` UUID primary key;
- display name, business purpose, classification, and retention policy;
- current RecipeRevision pointer;
- optional cutover-candidate pointer;
- current DataVersion pointer;
- optimistic revision; and
- created/updated timestamps.

`recipe_revision`

- `(recipe_id, version)` primary key;
- optional parent version;
- semantic hash and payload hash;
- protected storage key and byte length;
- constituent contract versions;
- bounded origin/provenance metadata; and
- actor and timestamp.

`data_version`

- `data_version_id` UUID primary key;
- `recipe_id`;
- positive version number unique within the Recipe;
- independent `workspace_project_id`;
- optional parent DataVersion;
- purpose: `AUTHORING`, `TEST`, or `PRODUCTION`;
- lifecycle state: `ACTIVE` or `SEALED`;
- pinned Recipe revision, nullable during initial authoring;
- label, export/as-of date, and parameter hash;
- bounded workflow/row/status projections; and
- created/sealed actor and timestamps.

`recipe_application`

- application ID, Recipe revision, and DataVersion;
- workspace project ID;
- source-selection and parameter hashes;
- TargetBinding dependency hashes and credential generation;
- binding and issue hashes;
- resulting mapping ID/content hash when applied;
- terminal status; and
- actor and timestamps.

The detailed immutable application evidence remains project-local or in a
protected Recipe-scoped store according to classification. The global registry
contains only bounded projections and protected storage keys.

`recipe_qualification`

- qualification ID, Recipe revision, and application ID;
- Test TargetBinding hashes;
- preparation, quality, control, comparison, execution, read-back,
  reconciliation, and optional idempotency hashes;
- status and bounded findings;
- qualifying actor and timestamp; and
- protected evidence storage key/hash.

`cutover_candidate`

- Recipe ID;
- pinned Recipe revision and qualification ID;
- optimistic Recipe revision used for selection; and
- selecting actor and timestamp.

Selections are append-only history. `recipe.cutover_candidate_id` identifies the
current selection without overwriting any prior qualified selection.

### 10.2 Protected RecipeStore

Recipe payloads and confidential qualification/application evidence use a
protected Recipe-scoped store with payload hashing, semantic hashing,
classification, retention, backup, authorization, and deletion policy. Full
Recipe JSON is not casually queryable global-registry content.

### 10.3 Project-local additions

Add only the minimum project-database state required for:

- the DataVersion/workspace linkage marker;
- historical workspace seal enforcement;
- RecipeApplicationDraft recovery; and
- exact Recipe/Application provenance required by contained evidence.

Do not add a DataVersion discriminator to all existing project tables.

### 10.4 Intents and outboxes

Cross-store operations use explicit idempotent intents for:

- Recipe publication;
- DataVersion/workspace creation and activation;
- qualification publication;
- cutover-candidate selection where protected evidence and registry pointers
  cross stores.

Initial Recipe/workspace creation uses the project-registry synchronization
journal because no Recipe aggregate exists yet. An unpublished Recipe draft is
deleted directly after exact Recipe and workspace revisions are validated.
Published Recipe deletion remains outside the current product surface.

Credential entry and rotation use the existing secret-store boundary and
project credential events. There is no Test-to-Production credential-copy
intent. Optional reuse of a saved credential on the exact same target remains
an explicit separately authorized action and always requires a new probe.

### 10.5 Clean-root migration and provisional workspaces

Every newly created workspace is Recipe-native. There is no shell backfill,
lazy hydration, bootstrap adoption, or standalone-project creation route.

The clean-root registry migration removes the superseded bootstrap columns,
single-active Recipe state, inert retry counter, deletion-intent tables, and
single-row cutover constraint while preserving valid Recipe, DataVersion, and
cutover history. A later DataVersion workspace is temporarily unlinked only
while it is being provisioned. Startup retains it only when an incomplete exact
DataVersion-creation intent references it; otherwise startup removes it.

## 11. Authorization and isolation

Add explicit Recipe-scoped capabilities for:

- Recipe view/edit/delete;
- Recipe revision publish;
- DataVersion create/resume/discard/view;
- Recipe apply;
- test qualification;
- cutover-candidate select;
- Production application; and
- protected Recipe evidence access.

Every operation also authorizes the exact workspace project and credential
role it accesses. Recipe membership is not an authority shortcut.

Test execution authority never implies Production execution authority.
Qualification authority never implies credential access. Production
application authority never implies write authority.

Errors, cards, audit projections, and URLs must not leak Recipe content,
credentials, connection secrets, paths, Odoo numeric IDs, or data from another
Recipe.

## 12. Browser experience

UI continuity is a constraint, not a prohibition on change. Refactor the
surfaces whose user task changes under Recipe ownership—landing, creation,
overview, target binding, qualification, rollout, and history. Preserve mature
workspace interactions where the task is unchanged. In particular, keep the
current matching phase largely intact and add RecipeRevision/DataVersion
context through navigation and surrounding status rather than building a
second matching interface.

### 12.1 Recipe list

The landing page becomes **Recipes**. Each card shows:

- Recipe name and purpose;
- current revision;
- qualification status;
- cutover-candidate revision;
- last Test run;
- latest DataVersion; and
- one context-appropriate primary action.

Examples:

> Customer migration<br>
> Recipe v3 - Test qualified<br>
> Rollout candidate: v3<br>
> **Run with latest data**

> Product and BOM<br>
> Recipe v5 - Test failed<br>
> 2 issues to fix<br>
> **Review test results**

### 12.2 Authoring and testing

The Recipe overview shows reusable meaning separately from the current
DataVersion evidence. Primary actions progress through:

- **Continue authoring**;
- **Publish Recipe revision**;
- **Test on Odoo**;
- **Review test results**;
- **Qualify Recipe revision**; and
- **Select for rollout**.

### 12.3 Application review

After application, show:

- reused semantic rules;
- confirmed physical rebindings;
- fields needing review;
- new source choices;
- stale target choices or references;
- parameter/control requirements;
- target contract mismatches; and
- credential/probe freshness.

Compatible rules remain collapsed. Every blocker remains visible outside
search and pagination and links to one recovery action.

### 12.4 Server and credential UX

Test and Production server forms clearly state which environment is being
used. The browser may retain non-secret endpoint/database settings, but secret
entry uses the governed credential boundary.

The UI must never suggest that a Test API key will be reused in Production.
When a saved same-target credential is offered, it states that Impodo will
probe it again and that changed principal or permissions invalidate dependent
evidence.

### 12.5 History

Recipe history presents two independent axes:

- Recipe revisions and their Test qualification evidence; and
- DataVersions/applications and their exact target environment outcomes.

Opening a sealed historical workspace is read-only and never makes it current.

## 13. Failure and recovery invariants

- A stale Recipe optimistic revision cannot publish, reserve a DataVersion, or
  select a new cutover candidate.
- A provisional workspace never displaces the current active DataVersion before
  its exact creation intent commits.
- Interrupted Recipe publication creates or reuses exactly one semantic
  revision.
- Corrupt Recipe bytes fail payload verification before parse.
- Source or target dependency changes during application abort before a mapping
  draft is saved while retaining safe physical binding overrides.
- A credential probe failure leaves no accepted TargetBinding.
- Credential rotation invalidates target-dependent readiness, not Recipe or
  source evidence.
- A Production target mismatch blocks only the Production application.
- Test qualification remains immutable and cannot authorize Production.
- A changed RecipeRevision has no inherited qualification or cutover status.
- Old expected control values never satisfy a new DataVersion.
- Old comparisons, approvals, execution snapshots, or journals never satisfy
  a new application.
- Missing source records never imply target deletion.
- Unknown writes are reconciled before retry.
- Draft deletion validates exact Recipe and workspace revisions before removing
  credentials, keys, contained workspace state, and registry lineage.

## 14. Performance and boundedness

Recipe implementation must respect current proven preparation limits:

- 100,000 physical rows only for the already verified exact-snapshot,
  single-dataset native-columnar direct route;
- 50,000 physical rows for current direct Python-fallback/relationship routes;
  and
- 25,000 physical rows for current derived or materialized routes.

This plan does not raise those limits and does not require the deferred
related/mixed 100,000-row qualification.

Recipe operations must nevertheless be bounded:

- Recipe list/history uses bounded registry queries and opens no project
  database per card;
- source categorical discovery scans each physical dataset once for all
  relevant fields;
- target keys resolve in bounded model/key batches with no Odoo call per row;
- compatibility scales with datasets, columns, rules, parameters, and distinct
  governed choices rather than total rows, apart from set-based scans;
- Recipe/application/qualification payload and issue bounds are checked before
  persistence or rendering; and
- customer, Product/BOM, and stock-level fixtures run at representative
  business volumes within their currently supported route limits.

Performance work outside a concrete Recipe acceptance blocker remains deferred.

## 15. Implementation sequence

### Foundation F1 - Mapping contract v11

**Status:** Completed on 2026-08-18.

Retain:

- strict categorical policies;
- immutable validation-bound categorical evidence;
- split reusable control definitions and DataVersion expectations;
- deterministic v8-v10 upgrade review;
- shared application-layer source-choice scanning; and
- current relationship target existence/uniqueness deferral to fresh
  preparation evidence.

Evidence is recorded in the
[Phase 1 implementation report](../reports/reusable-recipes-phase-1-mapping-contract-2026-08-18.md).

### Phase R0 - Rebase contracts around Recipe

**Status:** Completed on 2026-08-19.

- Supersede the ProjectSeries architecture decision and frozen contract.
- Freeze Recipe, RecipeRevision, DataVersion, parameter, OdooTargetContract,
  TargetBinding, application, qualification, cutover-candidate, lifecycle,
  intent, and bound contracts.
- Replace `series_id` fixtures with independent Recipe, DataVersion, and
  workspace IDs.
- Extend the existing Customer fixture with distinct remote Test and Production
  targets and different credential generations without storing secret values.
- Add credential rotation between Production comparison and load.
- Add expected transformation, test execution, reconciliation, qualification,
  and production application outcomes.
- Record exact bounds and recovery actions.
- Update the authoritative roadmap and documentation links.

**Gate:** deterministic fixtures prove that Recipe semantic identity is
independent of source files, Test/Production endpoints, databases, actors, and
credential generations, while application and qualification hashes change
with exact bound evidence.

**Evidence:** the
[Recipe-first Phase R0 contract](reusable-recipes-phase-r0-contracts.md),
active [Customer Recipe v3
fixture](../../fixtures/recipes/phase-r0/customer-recipe-v3.json),
[Test-to-Production acceptance
fixture](../../fixtures/recipes/phase-r0/acceptance-contract.json), and focused
[contract test](../../tests/test_recipe_phase_r0_contract.py) freeze and verify
the gate. This phase intentionally makes no browser or runtime behavior change.

### Phase R1 - Add Recipe root, lineage, and protected persistence

**Status:** Completed on 2026-08-19.

- Add the registry migration ledger and native Recipe/DataVersion creation.
- Implement Recipe repository, bounded projections, lifecycle policy, protected
  RecipeStore, and project linkage/seal markers.
- Add publication, DataVersion creation, qualification, and cutover-selection
  intents with fault injection.
- Add Recipe/DataVersion/application/qualification contracts and authorization.
- Preserve current project routes temporarily through explicit Recipe and
  DataVersion resolution.

**Gate:** every creation produces one Recipe with one authoring DataVersion
without opening workspace databases during list queries; Recipe, DataVersion,
and project IDs cannot be confused; interruption recovery is deterministic.

**Evidence:** the
[Phase R1 implementation report](../reports/reusable-recipes-phase-r1-persistence-2026-08-19.md)
and focused
[`test_recipe_persistence`](../../tests/test_recipe_persistence.py) suite cover
registry-only listing, disjoint identity resolution, encrypted
protected storage, project linkage and sealing, runtime publication guards,
and fault recovery for publication, DataVersion creation, qualification, and
cutover selection.

### Phase R2 - Create, author, and publish a Customer Recipe

**Status:** Completed on 2026-08-19.

- Make **Create Recipe** provision Recipe plus DataVersion 1 workspace.
- Implement RecipeDraft as a projection over exact current authoring evidence.
- Convert source shape, preparation, submitted mapping, governance, quality,
  references, parameters, and controls into logical RecipeDefinition.
- Reject every unsupported or nonportable construct with one recovery action.
- Store and verify semantic/payload hashes and publication provenance.
- Keep the existing matching and downstream workspace screens as contained
  Recipe authoring surfaces.

**Gate:** semantically identical Customer authoring over different physical
file/project IDs produces the same Recipe semantic hash; every semantic change
produces a new immutable revision.

**Evidence:** the
[Phase R2 implementation report](../reports/reusable-recipes-phase-r2-authoring-2026-08-19.md)
and focused
[`test_recipe_authoring`](../../tests/test_recipe_authoring.py) suite cover
Recipe-native creation, contained workspace routing, readiness projection,
portable compilation, exact envelope validation, identity-independent hashes,
semantic-change hashes, publication, and revision history.

### Phase R3 - Bind remote Test Odoo and apply same-ish data

**Status:** Completed on 2026-08-19.

- Implement TargetBinding creation from current non-secret target settings,
  credential generation, probe, principal, permission, context, schema, and
  reference evidence.
- Implement exact source and target compatibility, parameter values, controls,
  binding overrides, issue fingerprints, and RecipeApplicationEvidence.
- Materialize fresh source preparation, references, governance, quality rules,
  categorical evidence, and MappingWorkingDraft.
- Integrate focused issues into existing mapping UX.
- Ensure Test credentials remain project-local secrets.

**Gate:** Customer Recipe application reuses compatible rules, blocks only
`German`, `LUX`, structural, target, parameter, or credential drift, and creates
a fresh exact MappingDefinition without copying old evidence.

**Evidence:** the
[Phase R3 implementation report](../reports/reusable-recipes-phase-r3-test-application-2026-08-19.md)
and focused
[`test_recipe_application`](../../tests/test_recipe_application.py) suite cover
fresh Test DataVersions, exact TargetBindings, same-ish source binding,
structural preparation, categorical blockers, target and credential drift,
mapping-bound quality seeds, fresh MappingWorkingDraft creation, and protected
application evidence.

### Phase R4 - Fine-tune and qualify on Test Odoo

**Status:** Completed on 2026-08-19.

- Run preparation, quality, comparison, execution, read-back, and reconciliation
  against the Test TargetBinding.
- Support Recipe v1 -> v2 -> v3 refinement without mutating prior revisions.
- Implement immutable qualification evidence and explicit qualification action.
- Record expected outcomes and optional safe repeat-preview/idempotency result.
- Derive qualification status and make later revisions untested.
- Implement explicit cutover-candidate selection.

**Gate:** Recipe v3 can be selected only after current Test execution and
reconciliation satisfy the qualification policy; neither v1/v2 evidence nor a
changed v4 inherits that status.

**Evidence:** the
[Phase R4 implementation report](../reports/reusable-recipes-phase-r4-test-qualification-2026-08-19.md),
focused
[`test_recipe_qualification`](../../tests/test_recipe_qualification.py),
[`test_recipe_qualification_web`](../../tests/test_recipe_qualification_web.py),
and registry persistence tests cover exact Test readiness, immutable protected
qualification, explicit outcome confirmation, stale target credentials,
current-revision-only status, explicit rollout-candidate selection, and the
focused Recipe UI layered over the existing six-stage workspace.

### Phase R4.5 - Consolidate the clean Recipe root

**Status:** Completed on 2026-08-19.

- Remove Recipe shells, project backfill, lazy setup hydration, bootstrap
  adoption, standalone project deletion, deletion intents, and list/create route
  aliases.
- Make creation native and restart-safe with exact Recipe, DataVersion, and
  workspace identities from the first write.
- Keep only meaningful DataVersion and intent states; remove inert Recipe state,
  retry counters, and obsolete setup/intake fields.
- Make cutover selections append-only and retain the current pointer on Recipe.
- Delete unpublished drafts through Recipe ownership and keep published deletion
  outside the current product surface.
- Preserve the familiar contained workspace UI while making `/recipes` and
  `/recipes/new` the only list and creation entry points.

**Gate:** a clean install and an upgraded development registry expose the same
Recipe-native model; restart completes reserved DataVersion creation, removes
true provisional orphans, and never resurrects a compatibility shell.

**Evidence:** the
[Phase R4.5 clean-root report](../reports/reusable-recipes-phase-r4-5-clean-root-2026-08-19.md),
focused persistence, authoring, web, hosting, and workspace tests, and the full
regression suite.

### Phase R5 - Run qualified Recipe with latest Production data

**Status:** Completed on 2026-08-19.

- Add **Run with latest data** from the selected cutover candidate.
- Create a clean Production DataVersion/workspace and collect the latest source,
  parameters, and controls.
- Establish a different Production endpoint/database and fresh read credential.
- Capture and validate the Production TargetBinding against the Recipe contract.
- Apply the exact qualified revision and review only current drift.
- Run fresh preparation, quality, comparison, approval, execution, and
  reconciliation.
- Require separately established current Production write authority.

**Gate:** the exact test-qualified v3 runs with a later source package on a
different compatible Production server and different API key, while no Test
credential/evidence satisfies Production readiness.

**Evidence:** the
[Phase R5 implementation report](../reports/reusable-recipes-phase-r5-production-application-2026-08-19.md),
focused Recipe application, persistence, authoring, and browser tests, and the
existing preparation, quality, comparison, execution, credential, and
reconciliation regression suites cover exact candidate pinning, clean
Production workspaces, independent Production TargetBindings and read keys,
fresh downstream evidence, and separately probed write authority.

### Phase R6 - Credential rotation and remote failure qualification

**Status:** Completed on 2026-08-19.

- Rotate the same Production server's API key before comparison, between
  comparison and load, and during recovery.
- Prove new generation/probe/principal/permission bindings and exact
  invalidation.
- Inject expiry, ACL change, connection failure, schema drift, missing reference,
  unknown write, and read-back failure.
- Preserve journal and reconciliation safety and redact every secret/error path.

**Gate:** no operation proceeds with stale credential-dependent evidence, no
secret leaks, and unknown writes are never retried blindly.

**Evidence:** the
[Phase R6 implementation report](../reports/reusable-recipes-phase-r6-credential-rotation-2026-08-19.md),
focused credential-generation, execution-snapshot, remote read, write,
reconciliation, connector, and browser tests, and the complete regression suite
cover exact read-generation invalidation, load-time identity re-probe, safe
write-key rotation during recovery, failure classification, redaction, and
unknown-write reconciliation.

### Phase R7 - Expand representative Recipe shapes

- Qualify Products.
- Qualify Product plus BOM with related/derived dependencies inside current
  supported limits.
- Qualify parameterized stock levels using warehouse and as-of parameters plus
  fresh per-DataVersion controls.
- Capture browser, accessibility, security, and representative-volume evidence.
- Update current user/developer documentation only for behavior that has passed.

**Gate:** the Recipe abstraction supports one scalar/reference-heavy, one
related/derived, and one parameterized transactional snapshot without adding a
parallel execution path or weakening evidence semantics.

## 16. Acceptance scenarios

### 16.1 Customer refinement and qualification

Recipe v1 fails Test because one country mapping is incomplete. Recipe v2 fixes
the country but exposes an incorrect transformation. Recipe v3 passes current
Test preparation, comparison, execution, read-back, reconciliation, and
controls and becomes the explicitly selected cutover candidate. v1 and v2
remain immutable and unqualified.

### 16.2 Different Production server and API key

Qualified v3 is applied to the latest Customer export using a Production
endpoint/database and API key never used in Test. Production required
dependencies are compatible, fresh comparison succeeds, and only fresh
Production approval/write authority can execute.

### 16.3 Credential rotation

The Production API key changes after comparison. The old credential generation,
principal, permission, target-reference, comparison, and execution readiness
cannot authorize load. A new probe and required target refresh are mandatory;
Recipe v3 and source evidence remain unchanged.

### 16.4 Production target incompatibility

Production lacks a required custom field or exposes it readonly for an intended
write. Application blocks with one direct recovery action. Test qualification
remains honest historical evidence and no mapping/load snapshot is created for
the incompatible target.

### 16.5 Compatible source replacement

Reordered uniquely named columns bind automatically. A renamed used column
requires an explicit override. A new unused column is informational. A missing
used column blocks. Duplicate headers fail source confirmation.

### 16.6 Categorical drift

The latest export introduces `German` and `LUX`. Existing rules remain reused;
only those source choices block under explicit policies. Confirmed portable
matches clear the issues without numeric target IDs.

### 16.7 Parameters and controls

The stock-level Recipe declares warehouse and as-of parameters. Each DataVersion
supplies current values and expected quantity controls. A new warehouse value
does not create a Recipe revision when valid under the declaration; a new
undeclared parameter use does.

### 16.8 No inferred deletion

A customer present in rehearsal but absent from the rollout export is not
deleted, archived, or proposed for write. It may appear only in an informational
summary.

### 16.9 Qualification invalidation

Publishing v4 after v3 qualification leaves v3 history and cutover selection
intact until explicitly replaced, while v4 is `UNTESTED`. Selecting v4 is
rejected until v4 qualifies.

### 16.10 Interruption and concurrency

Stale or concurrent publication, DataVersion creation, qualification, cutover
selection, activation, and draft deletion commands cannot create two current
pointers, erase selection history, or lose the last valid state. Recoverable
cross-store operations are idempotent.

### 16.11 Authorization and isolation

An actor with Test execution cannot execute Production. An actor with Recipe
view cannot read credentials or protected evidence. A Recipe ID cannot be used
as a DataVersion/project ID, and another Recipe's payload or target evidence
never leaks.

### 16.12 Bounded performance

Listing 100 Recipes opens no workspace databases. Application performs no Odoo
call per source row, scans each dataset once for categorical needs, and fails
closed at every documented payload, issue, distinct-value, batch, row, and byte
bound.

## 17. Expected code and documentation boundaries

Likely new modules:

- `src/impodo/domain/recipes/`;
- `src/impodo/domain/data_versions.py`;
- `src/impodo/domain/recipe_applications.py`;
- `src/impodo/domain/recipe_qualification.py`;
- `src/impodo/application/recipe_service.py`;
- `src/impodo/application/data_version_service.py`;
- `src/impodo/application/recipe_application_service.py`;
- `src/impodo/application/recipe_qualification_service.py`;
- protected Recipe store and registry adapters;
- intent/outbox recovery services; and
- `src/impodo/web/routers/recipes.py`.

Likely existing modules to change:

- `src/impodo/projects.py` and project repository only for narrow workspace
  provisioning/link/seal boundaries;
- `src/impodo/access.py` for Recipe, qualification, cutover, and environment
  capabilities;
- registry schema and bounded queries;
- mapping workspace service for publication and application provenance;
- source preparation, quality, reference, governance, control, comparison,
  execution, and reconciliation services only through their existing ports;
- target credential and secret adapters for explicit per-application bindings
  and rotation invalidation;
- app composition, navigation, presenters, routers, templates, and JavaScript;
  and
- workflow, architecture, glossary, user/developer, testing, and release
  documentation after behavior becomes current.

Focused tests include:

- Recipe semantic/payload hashing and bounds;
- registry migration and ID confusion;
- protected-store integrity;
- publication, DataVersion, qualification, and cutover fault injection;
- native creation recovery, provisional-workspace cleanup, and exact draft
  deletion conflicts;
- application compatibility and binding override persistence;
- Test/Production target contract compatibility;
- credential generation, principal, ACL, expiry, rotation, and redaction;
- qualification and cutover state transitions;
- Customers, Product/BOM, and stock parameter/control scenarios;
- no inferred delete;
- unknown-write reconciliation;
- bounded Recipe list/application query counts; and
- complete regression, documentation, security, browser, accessibility, and
  Windows acceptance suites.

## 18. Approaches explicitly rejected

### Keep ProjectSeries above Recipe

Rejected because the first supported business lineage owns exactly one Recipe.
The extra aggregate weakens the Recipe-centered product model and duplicates
identity without adding current business meaning.

### Bind Recipe to one endpoint/database

Rejected because the required workflow qualifies on Test and applies on a
different Production server. Recipe owns target requirements; application owns
the exact target binding.

### Store or copy API keys in Recipe metadata

Rejected because credentials rotate, differ across environments, and confer
authority. Secrets remain only in the secret store and are reprobed per
binding.

### Treat Test qualification as Production approval

Rejected because Production data, target state, principal, permissions,
credentials, comparison, and write risks are new evidence.

### Mutate a qualified Recipe revision

Rejected because qualification must retain exact meaning. Corrections create a
new immutable revision that requires new testing.

### Duplicate every workspace draft into RecipeDraft

Rejected because two mutable sources of truth would drift. RecipeDraft
coordinates exact existing authoring evidence and publication compiles it.

### Copy a prior project database

Rejected because it copies stale rows, pointers, credentials, approvals,
snapshots, journals, and evidence.

### Copy raw MappingDefinition as the Recipe

Rejected because it is intentionally bound to physical source and schema
evidence.

### Automatically promote physical overrides

Rejected because a one-off renamed-column binding may not be reusable business
meaning. Promotion is explicit and creates a new revision.

### Fuzzy-bind authoritative source or target fields

Rejected. Suggestions may assist review but never authorize bindings.

### Infer deletes from missing replacement rows

Rejected because exports may be filtered or incomplete. Deletes need a separate
explicit policy and evidence model.

### Resume unrelated roadmap work before Recipe completion

Rejected by the current product-priority decision. Existing safety maintenance
and fixes remain allowed; competing feature/scale plans stay deferred.

## 19. Definition of done

The Recipe-first feature is implemented only when:

- Recipe is the aggregate root and primary browser concept;
- every new Recipe starts with one independent authoring
  DataVersion/workspace and no standalone project shell;
- RecipeDraft publishes complete immutable composite Recipe revisions with
  verified semantic and payload hashes;
- Recipe semantics exclude endpoints, databases, credentials, principals,
  physical IDs, source/target snapshots, approvals, and numeric Odoo IDs;
- a data manager can fine-tune v1 -> v2 -> v3 on a remote Test Odoo server;
- v3 can become test-qualified only from exact successful preparation,
  comparison, execution, read-back, reconciliation, and controls;
- a qualified revision can be explicitly selected as cutover candidate;
- rollout can apply that exact revision to the latest same-format-kind source
  on a different compatible Production Odoo server with different API keys;
- Production application creates fresh target, mapping, preparation, quality,
  comparison, approval, execution, and reconciliation evidence;
- credential rotation changes binding generation and invalidates all dependent
  readiness without changing Recipe/source evidence;
- Test credentials and authority never satisfy Production access or writes;
- source, target, categorical, parameter, reference, and credential drift fail
  closed with focused recovery actions;
- missing source rows never imply deletion;
- current row and payload bounds remain enforced and the deferred 100,000-row
  mixed/related goal is not a release dependency;
- publication, creation, qualification, cutover, rotation, and execution
  interruption recovery is deterministic and idempotent, while draft deletion
  fails closed on stale revisions;
- no Recipe path introduces Odoo N+1 reads or blind unknown-write retries;
- Customers, Product/BOM, and warehouse-parameterized stock acceptance paths
  pass within their current supported limits;
- complete regression, security, documentation, browser, accessibility, and
  required Windows acceptance evidence passes; and
- only then does product ownership explicitly reopen another roadmap track.
