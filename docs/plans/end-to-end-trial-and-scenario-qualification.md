# End-to-end trial and scenario qualification

## Status and decision

**Status:** Proposal for product and architecture review, 2026-09-04.

This proposal asks Impodo to add one shared end-to-end execution capability
with two controlled ways to use it:

1. A data manager can run an interactive trial with supplied files or a
   bounded Odoo source selection.
2. Developers and operators can run reviewed, deterministic scenarios in the
   background to detect workflow regressions.

Approve the shared scenario contract and the local file-to-Odoo first slice.
Do not interpret this proposal as approval for unbounded Odoo graph capture,
Odoo-to-Odoo writes, Production writes, or unattended use of customer data.

## Why this is the right capability

The request is valid, but it contains two different jobs.

A one-off trial answers, “Can Impodo move this supplied data through the whole
workflow, and where does this migration need a business decision?” The data
manager may provide customer, Product, bill-of-material, and bill-of-material
line files. The trial should use a reviewed Recipe when one exists. Without a
Recipe, it should proceed through discovery and stop at each decision that
requires the data manager to choose a business key, mapping, transformation,
or relationship rule.

A background scenario answers, “Does a previously reviewed workflow still
produce the same safe result with this Impodo build and Odoo version?” This
scenario needs sanitized or generated fixtures, saved business decisions, a
known target starting state, and independently reviewed expected outcomes. It
must not invent a mapping or accept its own output as the expected result.

Both jobs should execute the same source, mapping, preparation, comparison,
load, and reconciliation capabilities that the product uses. They should not
create a second migration engine just for tests.

## Mental model

The proposed model keeps the reusable business meaning separate from each
execution:

```text
Scenario definition
|-- source specification
|-- exact Recipe revisions or reviewed authoring decisions
|-- destination specification
|-- permitted write policy
`-- independently reviewed expectations
    |
    `-- Scenario run
        |-- one normal Impodo Project and its Data versions
        |-- normal workspaces and migration runs
        |-- normal background jobs and Odoo capabilities
        `-- a compact scenario result that refers to product evidence
```

The scenario definition describes how to exercise Impodo. It does not replace
a Data version, Recipe, workspace, migration run, execution journal, or
reconciliation result. Those existing objects remain the authorities for what
Impodo read, decided, wrote, and verified.

## Separate the execution environment from the data path

“Local” can describe where the runner executes or which Odoo destination it
uses. Those are different choices. The scenario contract should model them as
separate axes:

| Axis | Initial choices |
| --- | --- |
| Runner environment | A developer workstation, a dedicated internal worker, or a release job. |
| Source mode | Supplied CSV or XLSX files, or a bounded Odoo 19 capture. |
| Destination mode | A disposable local Odoo 19 database or an explicitly permitted disposable remote Odoo 19 database. |
| Workflow intent | Author a new Recipe, apply an existing Recipe, update captured records in place, or transfer captured records to another database. |
| Execution policy | Stop after preparation, stop after read-only comparison, or load and reconcile on a disposable destination. |

This model supports the requested paths without creating a special meaning for
“local machine.” For example, a scheduled worker may capture records from a
remote Odoo source and load a local disposable Odoo destination.

## Current boundary

The proposal must build from current behavior instead of describing every
requested path as already available.

| Path | Current position | Proposed scenario result |
| --- | --- | --- |
| Files to a local disposable Odoo | The browser can prepare, compare, load, and reconcile. | Automate the normal path and prove a second comparison is unchanged. |
| Files to a remote disposable Odoo | The governed load path exists. The opt-in representative runner exercises production services, but it is not the complete browser journey. | Reuse the shared scenario contract and retain remote acceptance evidence. |
| Odoo source to a different local or remote Odoo | Bounded capture, transformation, destination matching, transfer ordering, review, Stage 8A preflight, and explicitly confirmed Stage 8B loading and read-back exist. | Add a disposable-target scenario that proves the no-write gates, confirmed transfer, relationship order, and verified read-back. |
| Odoo source updated in the same Odoo database | Bounded capture and offline comparison exist. Guarded update execution remains deferred. | Add an update-only scenario after the protected same-instance update contract is implemented. |
| Continuous end-to-end monitoring | Unit, integration, browser-request, performance, and opt-in live acceptance evidence exist. There is no common scheduled scenario catalogue and result contract. | Add one runner, catalogue, scheduler entry point, and comparable result format. |
| Every record and every linked record from one Odoo model | Current capture is explicitly selected and bounded. It does not recursively crawl arbitrary Odoo links. | Add a reviewed, bounded relationship-capture plan. Never expose an unrestricted graph crawl. |

The existing `scripts/p4_representative_runner.py` is a valuable seed. It
already proves a disposable database namespace, generated sanitized inputs,
real preflight and execution services, reconciliation, repeat comparison, and
credential-safe JSON output. The common runner should absorb those properties
rather than maintain the P4 path as a permanent one-off implementation.

## The two product surfaces

### Interactive trial

The interactive trial helps a data manager answer whether supplied data can
complete the workflow.

The data manager provides one of these source inputs:

- The data manager uploads CSV or XLSX files.
- The data manager supplies a source Odoo connection and confirms a bounded
  capture plan.

The data manager then chooses an existing Recipe or starts normal authoring.
Files alone are not enough for an unattended complete load unless a Recipe or
equivalent reviewed decisions already define the target models, business keys,
field mappings, transformations, and relationships.

The minimum supplied information depends on the path:

| Trial path | Required input beyond the source data |
| --- | --- |
| Files to Odoo | The destination identity, the appropriate connection roles, a Recipe or reviewed mapping decisions, and permission to stop at comparison or load a disposable target. |
| Odoo source to another Odoo | The source URL and database, a read-only source key, a bounded model and relationship selection, the destination URL and database, destination credential roles, transformation rules, and the permitted stopping point. |
| Odoo records updated in place | The source and target must be the same verified disposable database. The trial also needs an update-only policy and reviewed field changes. This path remains planned rather than current. |

An API key by itself is therefore not a complete migration instruction. It
authorizes a narrowly defined connection role; it does not choose a database,
record scope, business identity, transformation, destination, or write policy.

The trial uses the ordinary browser decisions. It may automate deterministic
work such as inspection, preparation, dependency planning, and read-only
comparison. It must stop for a missing or ambiguous business choice. It may
write only after the normal target review and explicit confirmation.

The result should say one of the following:

- **Complete** means reconciliation verified the expected target state and a
  repeat comparison proposed no writes.
- **Needs attention** means the trial reached a normal business blocker and
  identifies the owning workflow stage.
- **Unsafe to continue** means target identity, credentials, evidence, or a
  write outcome cannot be trusted.

### Background scenario runner

The background runner executes only registered scenario definitions. A
reviewed definition supplies every decision that a person would otherwise
make. It also supplies run-scoped authorization for writes to one permitted
disposable target class. That authorization cannot be used for a Production
run or a database outside the scenario namespace.

The runner should drive public application use cases and normal job managers.
At least one golden scenario should also drive the real browser through a
headless browser so routing, forms, status polling, CSRF handling, templates,
and worker transitions are covered. Direct DuckDB changes, direct fixture
publication into internal tables, and replacement test-only migration logic
would not constitute end-to-end evidence.

## Scenario definition

Store a versioned definition as YAML and validate it against an allowlisted
schema before creating a Project. The definition should contain references,
not secrets or unrestricted executable code.

```yaml
contract_version: 1
scenario_id: file-products-and-boms-local
purpose: RELEASE_QUALIFICATION

source:
  mode: FILE
  fixture_set: scenarios/file-products-and-boms/v1

rules:
  recipe_revisions:
    - product-recipe-revision-reference
    - bom-recipe-revision-reference

destination:
  mode: LOCAL_ODOO
  target_profile: disposable-odoo-19-manufacturing
  expected_seed: empty-with-standard-uom

execution:
  stop_after: REPEAT_COMPARISON
  write_policy: DISPOSABLE_SCENARIO_ONLY

expectations:
  target_projection: scenarios/file-products-and-boms/v1/expected-target.json
  prepared_rows: 120
  first_comparison:
    create: 120
    update: 0
    unchanged: 0
    blocked: 0
    ambiguous: 0
  reconciliation:
    verified: 120
    different: 0
    missing: 0
    outcome_unknown: 0
  repeat_comparison:
    unchanged: 120
```

The exact schema may change during implementation, but the following meaning
must remain explicit:

- The definition pins its fixture version and exact Recipe revisions.
- The destination profile identifies a target type, installed modules, and
  seed fingerprint. It does not contain an API key.
- The write policy declares whether the run must remain read-only or may write
  to a disposable target.
- Expected classifications and target outcomes are reviewed inputs. The runner
  never generates them from the current implementation during assertion.
- Expected target values and relationships come from a sanitized, independently
  reviewed projection. Classification counts and an all-unchanged repeat are
  necessary, but they are not sufficient to detect a consistently wrong
  transformation.
- A scenario may expect a safe blocker. Such a scenario passes only when the
  expected blocker occurs before any write.

## Bounded Odoo relationship capture

“All links” should mean the reviewed closure needed for a migration, not every
record reachable from an Odoo model.

An unrestricted traversal can leave the intended application, cross company
boundaries, enter users, messages, followers, attachments, accounting records,
or other sensitive models, and revisit cycles indefinitely. Computed and
related fields may also trigger expensive or permission-dependent behavior.

For an Odoo source, the scenario should first create a relationship-capture
plan with these bounds:

- The operator selects one or more root models and a saved, reviewable record
  filter.
- The plan lists the exact scalar fields to capture for each model.
- The plan lists each relationship edge that Impodo may follow.
- Each edge declares its relationship type, target model, business identity,
  direction, and whether the target record is required for the migration.
- The plan sets maximum records per model, maximum total records, maximum
  depth, allowed companies, and permitted models.
- Impodo de-duplicates visited records by protected source identity and detects
  cycles without exposing numeric Odoo identifiers as portable meaning.
- Impodo previews model, field, edge, and record counts before reading the
  complete values.
- A limit, access failure, schema change, ambiguous identity, or unexpected
  model stops the capture. Impodo does not silently omit the affected branch.

The current capture policy permits at most ten datasets, 50 closed scalar
fields per model, and 10,000 rows per model. The first relationship plan should
remain within those limits. Raising a limit or capturing relational values
requires separate performance, privacy, and evidence qualification.

The default should follow no relationship automatically. A future convenience
option may propose required many-to-one dependencies from captured schema, but
the data manager must review that proposal before capture. One-to-many and
many-to-many expansion should remain explicit because their fan-out is less
predictable.

The captured result becomes normal immutable Data version evidence. The
subsequent transformation and destination workflow must consume that frozen
evidence without returning to the source Odoo database.

## Execution lifecycle

One scenario run should advance through explicit checkpoints:

```text
Validate scenario and target policy
  -> provision or verify the disposable starting state
  -> create the normal Project and source evidence
  -> freeze the Data version
  -> apply pinned Recipes or reviewed authoring decisions
  -> prepare and validate every row
  -> compare against the destination
  -> assert the expected pre-write result
  -> obtain scenario-scoped disposable write authority
  -> execute through the normal journalled writer
  -> reconcile through the separate read-back capability
  -> compare again and require the expected unchanged result
  -> publish the scenario result
```

Every checkpoint must be restart-aware. A runner restart may resume a safe
read or preparation step from current evidence. It must use the existing
recovery assessment after an in-flight or unknown write and must never restart
the scenario from the beginning against a target that may already have
changed.

The target provisioner and the migration runner have separate ownership. The
runner verifies the starting fingerprint and performs the migration. It does
not erase an unexpected database to make a test pass. An infrastructure
provider may destroy a database that it created for the scenario after it has
retained the required evidence.

## What a passing scenario proves

A successful write-capable scenario requires more than a zero process exit
code. It must prove all of the following:

- Impodo accepted the intended source bytes or exact Odoo capture.
- The expected Recipe revisions and target schema were used.
- Preparation produced the expected row, issue, and control totals.
- The first comparison produced the reviewed create, update, unchanged,
  ambiguous, and blocked totals.
- No write outside the approved models, fields, records, and dependency order
  entered the execution journal.
- Reconciliation verified every intended final value and relationship.
- No row remained missing, different, partially applied, or outcome unknown.
- A second comparison against the same prepared intent proposed no writes.
- The number and shape of Odoo requests stayed within the scenario's bounded
  request budget.
- No credential, protected identifier, or business value entered the compact
  result, ordinary logs, or CI output.

Failure results should distinguish a product regression, an expected business
blocker, fixture or target drift, an infrastructure failure, a credential
failure, and an unsafe write outcome. This distinction prevents an unavailable
test database from being reported as a mapping regression.

## Evidence and reporting

Each run should publish one compact machine-readable result and optional human
summary. The result should contain:

- the scenario ID, contract hash, fixture hash, and expectation hash;
- the Impodo revision and build contract;
- the Odoo version, installed-module fingerprint, and non-secret target
  fingerprint;
- the normal Project, Data version, migration-run, workspace, execution, and
  reconciliation evidence references;
- phase durations, peak resource observations where available, and bounded
  request counts;
- expected and actual classification, write, and reconciliation totals;
- the repeat-comparison result;
- the final scenario status and one owning failure stage; and
- redacted diagnostic artifact references.

The runner should also emit JUnit-compatible output for release jobs. A trend
store may retain durations, row rates, and request counts, but timing alone
must not turn a functionally correct run into a failure until a reviewed
performance threshold exists.

## Credential, data, and write safety

The scenario definition refers to secret handles. A credential provider
resolves the actual keys only for the owning operation. The source Odoo key is
read-only. The destination read key and destination write key remain separate
roles when the current product contract requires them. No key is stored in the
definition, result, source fixture, Git repository, command line, or log.

Write-capable background runs require all of these safeguards:

- The target database name matches a dedicated scenario namespace such as
  `impodo_scenario_`.
- The target provider proves that the database is disposable and presents the
  expected seed fingerprint.
- The scenario declares exact writable models and fields through its pinned
  Recipes and current destination evidence.
- A dedicated service actor has only scenario execution capabilities.
- The scenario contract cannot select a Production-purpose migration run.
- A changed target, credential generation, principal, permission context,
  module fingerprint, or starting state stops before the first write.
- The normal journal-before-transport and reconciliation contracts remain in
  force.
- Retention and cleanup rules are explicit before customer-derived data is
  accepted.

An ad hoc trial with customer data is not automatically eligible for scheduled
reuse. Converting it into a background scenario requires sanitization or
generation, reviewed expectations, fixture ownership, and a retention
decision.

## Proposed implementation ownership

The scenario layer should orchestrate existing capabilities rather than enter
their storage boundaries.

| Responsibility | Proposed owner |
| --- | --- |
| Immutable scenario definition, expectation, authorization, and result contracts | `src/impodo/domain/scenarios/` |
| Checkpoint orchestration and status classification | `src/impodo/application/scenarios/` |
| YAML loading, secret-handle resolution, result writing, and target providers | `src/impodo/adapters/scenarios/` |
| Manual and scheduled command entry | A new `impodo-cli scenario` command composed in the existing CLI boundary. |
| Optional browser-driven golden journey | `tests/scenarios/` with a real browser and an externally started Impodo process. |
| Sanitized committed fixtures | `scenarios/fixtures/`, separated by scenario and immutable fixture version. |
| Local and remote target instructions | Developer runbooks that identify provisioning, starting fingerprints, retention, and teardown ownership. |

`ScenarioRunner` should call the same application commands that the browser
uses. It may poll normal job status and read normal evidence projections. It
must not import adapter repositories to create a prepared result, approved
comparison, execution journal, or reconciliation record directly.

The first implementation should support a CLI before adding a browser page for
scenario administration. This keeps the safety contract reviewable and makes
it useful on a workstation and in a release job. The interactive trial can
continue to use the existing browser workflow while the shared result summary
is introduced.

## Initial scenario catalogue

Start with a small risk-based catalogue instead of trying every combination.

| Scenario | Source and destination | Required proof |
| --- | --- | --- |
| Contact round trip | A small CSV loads into a local disposable Odoo database. | The run covers creates, updates, unchanged rows, reconciliation, and an all-unchanged repeat. |
| Product and bill-of-material dependency | Product, bill-of-material, and line files load into a local disposable Odoo database. | The run proves target-only references, incoming relationships, dependency order, and no hidden request per row. |
| Recipe on fresh files | Renamed but structurally compatible files use pinned Recipe revisions. | The run proves logical source matching and blocks a missing or ambiguous table. |
| Remote representative | Generated files load into a remote disposable Odoo database. | The run preserves the current P4 safety properties and retains remote request evidence. |
| Odoo capture to local preflight | A bounded remote Odoo capture is transformed for a local destination. | The run proves source capture, frozen offline transformation, destination matching, order, review, and Stage 8A with zero writes. |
| Odoo-to-Odoo transfer | A bounded source database transfers to a distinct disposable remote database. | The run proves separate source and destination credentials, target identity, relationship order, zero Stage 8A and Stage 8B preparation writes, an explicit confirmed load, and verified read-back. |
| Expected missing relationship | A source row refers to an absent or ambiguous target. | The run passes only when preparation or comparison blocks it and the journal remains absent. |
| Target drift before load | The destination changes after comparison. | The run passes only when the pre-write check detects the drift and performs zero writes. |
| Lost write response | A controlled transport fault occurs around a write. | The run proves journal state, stop behavior, read-back assessment, and no blind retry. |
| Schema or permission drift | The Odoo schema or service-user access changes. | The run stops at the owning read-only gate and reports the changed evidence. |

Stage 8B now supplies the product boundary for the Odoo-to-Odoo write scenario;
the scenario runner still needs to qualify it on a disposable target. Add the
same-instance captured-record update scenario only after its separate guarded
execution path is implemented.

The catalogue should tag each scenario by source mode, target mode, field
types, relationship types, create or update behavior, failure class, and Odoo
modules. A release report can then show risk coverage without multiplying
every tag into an impractical Cartesian product.

## Scheduling policy

Use different suites for different feedback needs:

- A pull-request suite runs deterministic service and browser-request scenarios
  with fake Odoo transport and no external credentials.
- A daily local canary runs the Contact scenario against one freshly
  provisioned local Odoo database.
- A nightly representative suite adds Product and bill-of-material
  relationships, Recipe reuse, and selected failure injection.
- A release suite runs the complete local catalogue and the permitted remote
  disposable scenarios.
- A manually triggered supplied-data trial runs only after the operator has
  reviewed its source, destination, credentials, retention, and write policy.

A scheduler starts the command and retains the result. It does not own
migration semantics. Concurrent runs must receive separate Projects, project
storage, worker ownership, and target databases unless a scenario explicitly
tests concurrency.

## Delivery plan

### Phase 0: define the contract and baseline

Create the versioned scenario definition, expectation, result, status, and
test-only authorization contracts. Record the existing P4 runner output as a
baseline and identify which steps use normal production services and which
steps remain harness-specific.

This phase exits when invalid definitions, embedded secrets, unapproved write
policies, unexpected database names, and mutable fixture references fail
before a Project or Odoo connection is created.

### Phase 1: complete one file-to-local scenario

Implement the CLI and application orchestrator for a Contact fixture against
a resettable local Odoo 19 target. Drive normal source acceptance, Recipe
application or reviewed mapping, preparation, comparison, load,
reconciliation, and repeat comparison.

This phase exits when the scenario passes from a fresh process, a deliberate
expected blocker passes with zero writes, and an unexpected result fails with
actionable retained evidence.

### Phase 2: add Product and bill-of-material coverage

Add the multi-Recipe or multi-dataset fixture with Products, bill-of-material
headers, and component lines. Measure dependency planning, relationship
resolution, worker recovery, Odoo request counts, and repeat idempotence at a
small reviewable size before adding a scale variant.

This phase exits when reversed file order does not change the dependency-safe
write result and every relationship is verified from Odoo read-back.

### Phase 3: add browser and remote acceptance lanes

Drive one golden local scenario through a real headless browser and an
externally started Impodo process. Generalize the existing P4 runner into the
shared contract for an opt-in remote disposable target.

This phase exits when browser, service, local, and remote results use the same
status and evidence vocabulary without sharing credentials or target state.

### Phase 4: add Odoo-source scenarios through the current boundary

Add bounded Odoo relationship-capture definitions and exercise Odoo source to
local destination and Odoo source to remote destination through Stage 8A.
Require separate source and destination credentials and prove that the frozen
source supports offline transformation.

This phase remains read-only at the destination. It exits when the runner
proves the approved transfer package and fresh destination preflight with zero
write calls.

### Phase 5: qualify current cross-instance writes, then extend

Stage 8B is current, so add Odoo-to-Odoo execution and reconciliation through
the normal implementation and retain its no-blind-retry evidence. When guarded
same-instance updates become current, add their update-only scenario. The
scenario runner must use the product confirmation, journal, writer, and
read-back boundaries rather than bypassing them.

### Phase 6: schedule and operate the catalogue

Add the daily, nightly, and release schedules; trend reporting; failure
ownership; evidence retention; credential rotation checks; and target cleanup
runbooks. Start with an internal worker before considering a hosted service.

This phase exits when a failed canary identifies its owning stage and retained
evidence without exposing a key or business value, and when an unavailable
target is distinguishable from a product regression.

## Acceptance criteria

The proposal is complete when the implementation can demonstrate all of the
following:

- One command can execute a registered scenario on a workstation or a
  dedicated worker.
- The same scenario meaning applies to local and permitted remote disposable
  targets.
- Supplied files can proceed through the whole implemented workflow when a
  reviewed Recipe or reviewed authoring decisions are available.
- An Odoo source can be captured through an explicit bounded relationship plan
  and transformed from immutable local evidence.
- Every write-capable scenario uses the normal product confirmation authority,
  journal, writer, recovery, and reconciliation boundaries.
- A pass requires verified read-back and an expected repeat comparison.
- Expected blockers prove zero writes.
- Scenario definitions and compact results contain no credentials, protected
  Odoo identifiers, or business values.
- Background execution is limited to registered sanitized or generated data
  and disposable scenario targets.
- Odoo-to-Odoo writes remain unavailable in the runner until its Stage 8B
  disposable-target scenario is added. Same-instance writes remain unavailable
  until their product contract becomes current.
- Documentation distinguishes the interactive trial, background
  qualification, current product behavior, and planned behavior.

## Non-goals

This proposal does not:

- make arbitrary Odoo models or business actions safe to load;
- recursively download an unrestricted Odoo object graph;
- infer or approve business mappings without a data manager;
- use customer data as a permanent test fixture by default;
- allow a background scenario to write to Production;
- replace existing unit, integration, browser-request, performance, or
  security tests;
- treat an HTTP success response as migration completion;
- delete or roll back Odoo records as part of the migration runner; or
- promise exhaustive coverage of every Odoo module combination.

## Documentation and verification impact

This proposal is a plan, so it does not change the current user workflow or
`docs/workflow.yml`. As each phase becomes current, review the owning user and
developer pages, the integrated-run and execution contracts, the CLI
documentation, local and remote Odoo runbooks, BPMN, screenshots, architecture
maps, and acceptance strategy together.

Focused implementation verification should include:

- contract parsing and rejection tests for definitions and expectations;
- secret, path, database namespace, and Production-purpose refusal tests;
- fresh-process and restart tests for every checkpoint;
- a real-browser golden scenario;
- target drift, credential drift, schema drift, and permission drift tests;
- expected blocker and zero-writer assertions;
- lost-response and reconciliation recovery tests;
- local live Odoo read-back and all-unchanged repeat evidence;
- bounded request-count assertions for Product and bill-of-material
  relationships; and
- the existing repository documentation, architecture, security, and complete
  test gates.

## Related documentation

- [Acceptance and test strategy](../testing/acceptance.md)
- [Integrated Test run lifecycle](../developer/contracts/integrated-run-lifecycle.md)
- [Execution and reconciliation](../developer/contracts/execution-and-reconciliation.md)
- [Source data implementation](../developer/workflow/01-source-data.md)
- [Load into Odoo implementation](../developer/workflow/06-load-into-odoo.md)
- [Remote Odoo 19 acceptance](../developer/runbooks/remote-odoo-acceptance.md)
- [End-to-end training tutorial](../user/tutorials/end-to-end-training.md)
- [Impodo remaining work](remaining-work.md)
