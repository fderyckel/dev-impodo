# Python code documentation plan

## Status and outcome

**Status:** Initial DOC-0–DOC-6 rollout complete on 2026-08-06; continuous
maintenance remains active.

Current progress:

- DOC-0: documentation standard accepted as the working convention; advisory
  inventory command and Python code map added. Per-module stage classification
  remains to be completed with the deeper documentation phases.
- DOC-1: complete. All six navigation journeys from project registration
  through durable read-only preflight are mapped. Their composition, route,
  application, central domain, and persistence entry points have navigation
  docstrings and establish the navigation spine used by deeper phases.
- DOC-2: complete. Stage A–D service/repository ports, artifact and isolated-
  source boundaries, workspace evidence objects, schema governance, derived-
  dataset plans, mapping contracts, validation context/evidence, optimistic
  revisions, and invalidation responsibilities are documented. Transparent
  ``BrowserQueryService`` forwarders remain an explicit coverage exception.
- DOC-3: complete. The target-independent preparation chain now documents
  compiled evaluation, canonical rows/lineage, reconciliation and control
  totals, transformation impact, quality eligibility/quarantine, grouped
  normalization decisions, approval/freeze, and the service ports and DuckDB
  repositories that bind each artifact to the preceding content hashes.
- DOC-4: complete. Both Stage-H entry paths now document frozen-input or
  strict-profile preparation, bounded request planning, closed local/remote
  Odoo reads, snapshot provenance/projection checks, metadata validation,
  reference resolution, classification precedence, portable reports,
  protected target evidence, and transactional publication.
- DOC-5: complete. Cross-cutting ownership now covers capability authorization,
  loopback/session/CSRF security, secure project and artifact containment,
  credential storage, idempotent jobs, local-stack process ownership, hardened
  DuckDB connections/unit-of-work/migrations, downstream invalidation, stable
  serialization/hashing, transactional audit, composition, and error translation.
- DOC-6: complete. Standalone approval objects and future Stages I–K are now
  explicitly separated from implemented Stage-H readiness. Read/composition
  boundaries state that no writer, execution journal, or post-write
  reconciliation exists, and normal tests enforce module orientation while
  public-symbol gaps remain an advisory review report.

This plan makes `src/impodo/` understandable from inside the Python files.
Its success criterion is practical: after opening a module, a maintainer should
be able to identify its responsibility, migration stage, architectural layer,
main entry points, important classes and methods, collaborators, side effects,
and downstream evidence without first reconstructing the application from
imports and tests.

This is a code-navigation plan, not a replacement for the existing contracts,
architecture, operations, or product documentation. Code docstrings point to
those authorities and explain how the current implementation realizes them.

## Terminology

The repository already assigns specific meanings to two naming systems:

- **Stages A–K** are the end-to-end migration lifecycle in the
  [product vision](../product-vision.md#4-product-stages).
- Numeric **Phases** such as Phase 1, Phase 2A, Phase 2B, and Phase 2C.1 are
  product delivery increments.

To avoid creating a third ambiguous meaning, this plan names its increments
**documentation phases** and prefixes them `DOC-0` through `DOC-6`.

## Baseline

A static inventory on 2026-08-06 found 133 Python modules and approximately
40,500 lines under `src/impodo/`. Almost every module already has a module
docstring, but many are only a title. The package contains roughly 324
top-level classes, 460 top-level functions, and 678 class methods.

An initial syntax-tree count found 936 public classes, functions, and methods,
of which 564 have no docstring. This count is a prioritization signal rather
than a coverage target: enum members, obvious data accessors, protocol methods,
and thin adapters do not all need repetitive text. The most important gaps are
semantic:

- application ports state signatures but seldom explain the contract between
  services and repositories;
- application services expose the migration workflow but many public methods
  do not describe prerequisites, invalidation, publication, or the next call;
- large domain modules such as `quality.py`, `normalization.py`, and
  `staging_contracts.py` contain many related states and evidence objects whose
  organization is not visible from a single class docstring;
- repository methods often do not state transaction boundaries, immutable
  versus current-pointer behavior, or the evidence they invalidate;
- web routes show HTTP mechanics but not which application operation they
  invoke or which migration transition they represent.

The existing module boundaries are a good foundation. The documentation work
should improve orientation without reorganizing code merely to improve a
docstring metric.

## Migration-stage map for the code

The code map must distinguish implemented behavior from future boundaries.
The [active staging plan](data-quality-and-staging-plan.md#current-reality) is
the authority for current implementation status.

| Product stage | Current code responsibility | Primary Python areas to document |
| --- | --- | --- |
| A — Register a migration project | Implemented project registration, ownership, retention, target identity, and lifecycle | `projects.py`, `project_security.py`, `application/`, `adapters/duckdb/project_repository.py`, `web/routers/projects.py` |
| B — Inspect source files | Implemented intake, isolated inspection, confirmation, and frozen datasets | `intake.py`, `inspection.py`, `source_worker.py`, `source.py`, `application/source_workspace_service.py`, `adapters/duckdb/source_repository.py`, source routers |
| C — Discover the Odoo target schema | Implemented read-only catalogue and schema capture with governed scope and business keys | `connectors.py`, `local_odoo_reader.py`, `domain/schema/`, `application/schema_workspace_service.py`, `adapters/duckdb/schema_repository.py`, schema and target routers |
| D — Build and approve the mapping | Implemented mapping drafts, immutable revisions, validation, submission, scalar rules, relationships, and derived entities | `domain/mapping/`, `domain/compiler/`, `derived_entities.py`, `application/mapping_workspace_service.py`, `adapters/duckdb/mapping_repository.py`, mapping routers and presenters |
| E — Normalize and validate | Implemented mapping evaluation, quality/quarantine, transformation impact, grouped normalization review, decisions, and freeze | `domain/staging/evaluator.py`, `quality.py`, `normalization.py`, `governance.py`, quality and normalization services/repositories/routers |
| F — Store canonical staging data | Implemented immutable canonical staging, lineage, reconciliation, control totals, and current-run pointers | `staging_contracts.py`, `staging.py`, `domain/staging/`, `application/preparation_service.py`, `adapters/duckdb/staging_repository.py` |
| G — Resolve relationships | Implemented mapping validation, derived parent/child preparation, symbolic references, and preflight lookup resolution within the current bounded workflow | `derived_entities.py`, `reference_keys.py`, `domain/mapping/validation/`, `domain/compiler/`, `engine.py` |
| H — Read-only target preflight | Implemented durable-input loading, bounded target-read planning, metadata validation, comparison, classifications, and reports | `application/preflight_service.py`, `domain/preflight/`, `planner.py`, `metadata.py`, `catalog.py`, `engine.py`, `reporting.py`, connectors and preflight repositories/routes |
| I — Freeze an approved import plan | Domain approval objects exist, but integrated clean-package certification and a frozen executable import plan are not implemented | `approvals.py` and explicit future-facing boundaries only |
| J — Controlled Odoo execution | Not implemented; current Odoo surfaces are read-only | document the prohibition and extension boundary in connectors, authorization, and architecture-facing modules |
| K — Reconcile | Not implemented as a post-write workflow | document as a future boundary; do not imply that source/staging accounting is post-write reconciliation |

This table belongs in a shorter, navigable form in the eventual code map. A
module may serve several stages, but it should name one primary stage and list
secondary stages only when that improves orientation.

## Documentation standard

### Module docstrings: answer “where am I?”

Every non-trivial module should begin with a concise description containing:

1. **Purpose** — the one responsibility owned here.
2. **Migration stage** — primary Stage A–K, or “cross-cutting”.
3. **Layer** — web, application, domain, adapter, Odoo-read boundary, or
   composition root.
4. **Entry points** — the small set of functions/classes a reader should open
   first.
5. **Flow** — the important upstream caller and downstream collaborator.
6. **Invariants and side effects** — especially immutability, hash binding,
   invalidation, transaction, filesystem, Odoo-read, and no-Odoo-write rules.
7. **Authoritative references** — links or paths to the governing contract and
   the nearest focused test module.

Keep the opening summary visible in an editor tooltip. Longer flow or invariant
detail may follow in paragraphs or short sections. A module docstring should
not repeat the complete contract.

Example shape:

```python
"""Publish canonical staging for a submitted browser mapping.

Migration stage: F — Store canonical staging data.
Layer: DuckDB adapter.

Entry point: ``StagingRepository.publish_canonical_staging``.
Called by ``PreparationService.prepare`` after pure mapping evaluation. It
atomically writes immutable rows, advances the current staging pointer, and
invalidates downstream quality evidence when content changes.

See ``docs/contracts/03-canonical-staging.md`` and
``tests/test_staging_store.py``.
"""
```

### Classes: answer “what does this object own?”

Document public services, repositories, ports, domain aggregates, evidence
objects, and non-obvious value objects. State:

- the single responsibility and whether the object is stateful, immutable, or
  an interface;
- its place in the lifecycle and the evidence it owns;
- its important collaborators and why they are dependencies;
- invariants that apply to all methods;
- for protocols, what implementations must guarantee beyond the type
  signature.

Related data classes in large modules should receive a short organizing
comment or module section explaining the family before individual class
details. Do not give every passive data carrier the same boilerplate.

### Methods and functions: answer “what happens if I call this?”

Public workflow operations and non-obvious private algorithms should describe:

- the outcome, not merely restate the name;
- required current evidence or lifecycle state;
- meaningful arguments whose business meaning is not clear from their type;
- return evidence and important absence semantics such as `None` meaning
  “no current compatible run”;
- domain exceptions callers are expected to handle;
- persistent, filesystem, authorization, or Odoo-read side effects;
- the next important method(s) in the flow when delegation is not obvious.

Use `Args`, `Returns`, and `Raises` sections only when they add information.
Simple accessors and transparent delegations can remain one sentence. Private
helpers need documentation when they implement a business rule, hash contract,
batching constraint, security boundary, or complex algorithm.

### Connections: curate stable flows instead of copying a call graph

Connections should appear at three levels:

- module docstrings name the immediate upstream entry point and downstream
  owner;
- orchestration method docstrings name the few consequential calls and the
  evidence transition they cause;
- `docs/architecture/python-code-map.md` shows stable end-to-end call chains
  and package/class relationships.

Do not add “called by” text to every helper or mechanically list every import.
That becomes stale quickly and obscures the business flow. The code map should
cover at least these journeys:

1. register project and store source evidence;
2. inspect, confirm, and freeze datasets;
3. capture schema and govern keys;
4. save, validate, and submit mapping evidence;
5. prepare rows → canonical staging → quality → normalization freeze;
6. load frozen evidence → plan bounded Odoo reads → compare → publish report.

Each journey should identify the route or CLI entry point, application service,
domain operation, repository transaction, resulting evidence, invalidation
effects, and principal tests.

### Inline comments: explain constraints, not syntax

Add comments where the reason is otherwise invisible: fail-closed decisions,
hash composition, deterministic ordering, transaction sequencing, bounded
batching, numeric-ID containment, and invalidation. Avoid comments that narrate
ordinary Python statements.

## Documentation phases

### DOC-0 — Agree the standard and inventory

**Size:** Small.

- Accept this docstring standard and choose one consistent section style.
- Produce an advisory inventory of modules, public symbols, missing semantic
  docs, file size, and internal dependencies.
- Classify each module by architectural layer and primary migration stage.
- Create `docs/architecture/python-code-map.md` with the package map,
  migration-stage table, and placeholders for the six critical journeys.
- Record documentation exceptions for trivial accessors, obvious immutable
  carriers, framework callbacks, and generated/vendor code.

**Exit gate:** reviewers can tell what must be documented and what is
deliberately exempt; no source behavior changes are included.

### DOC-1 — Build the A–H navigation spine

**Size:** Medium.

Document only the top-level path first:

- composition in `web/app.py` and `web/context.py`;
- CLI entry points in `cli.py` and `__main__.py`;
- one route entry point and primary application service per current stage;
- main service methods and the repositories/domain operations they call;
- the six critical journeys in the code map, including evidence created and
  invalidated at each transition.

This phase intentionally crosses all implemented stages at shallow depth. It
gives immediate navigation value and prevents later phases from becoming
isolated file-by-file commentary.

**Exit gate:** starting at the browser action or CLI command, a maintainer can
reach the owning service, domain operation, persistence operation, output, and
test without a repository-wide search.

### DOC-2 — Document Stages A–D in depth

**Size:** Large.

Cover project registration, access, artifacts, source intake and inspection,
frozen selections, Odoo schema capture, business-key governance, mapping,
mapping validation/compilation, and derived-entity authoring.

Priorities are:

- lifecycle prerequisites and downstream invalidation;
- distinctions among physical source selection, effective mapping selection,
  schema evidence, mapping draft, revision, validation, and submission;
- service/port/repository contracts;
- domain aggregate and value-object families;
- read-only target boundaries and source-worker safety constraints.

**Exit gate:** every public operation that changes or selects Stage A–D
evidence explains its prerequisites, durable result, invalidation effects, and
next workflow transition.

### DOC-3 — Document Stages E–G in depth

**Size:** Extra large.

**Status:** Complete on 2026-08-06.

Cover the most interconnected target-independent pipeline: compiled mapping
evaluation, canonical staging, lineage, reconciliation, control totals,
quality/quarantine, transformation impact, normalization review, decisions,
approval, and eligible-row freeze.

Start with the active service chain:

```text
PreparationService.prepare
-> stage_browser_mapping
-> evaluate_browser_mapping
-> publish_canonical_staging
-> QualityService.evaluate_and_publish
-> NormalizationService.evaluate_and_publish
```

Then document the related domain families and DuckDB repositories. Give
special attention to why the order is fixed, which objects are immutable,
which “current” pointers may move, how content hashes bind the stages, and
which changes retire downstream evidence.

Because this area is actively changing, apply documentation after overlapping
refactors are stable and review docstrings in the same change as semantic
updates.

**Exit gate:** a reader can trace one physical row through canonical rows,
quality disposition, normalization effects/decisions, and frozen eligibility,
including fan-out and set-aside behavior.

### DOC-4 — Document Stage H in depth

**Size:** Large.

**Status:** Complete on 2026-08-06.

Cover both entry paths into the shared preflight engine:

- the submitted-browser-mapping path using durable frozen evidence;
- the strict profile/CLI path.

Document the boundary among compilation, frozen-input adaptation, request
planning, closed Odoo reads, metadata/catalog validation, relationship
resolution, comparison, classification precedence, target snapshots, and
report projections. Explicitly identify bounded reads, the prohibition on
empty/unrestricted domains, numeric Odoo ID containment, and the zero-write
guarantee.

**Exit gate:** a reader can explain why Odoo is or is not contacted, how reads
are batched, how a prepared record becomes a decision, and where portable and
protected evidence diverge.

### DOC-5 — Document cross-cutting infrastructure

**Size:** Medium.

**Status:** Complete on 2026-08-06.

Cover the code that spans stages rather than owning one:

- authorization, actor evidence, security, sessions, and CSRF;
- project filesystem/artifact boundaries and secrets;
- jobs and local stack lifecycle;
- DuckDB unit of work, schema migrations, serialization, hashing, audit, and
  invalidation helpers;
- error taxonomy and where errors are translated for browser or CLI users;
- web presenters and forms where they encode workflow semantics rather than
  formatting alone.

Add one package dependency diagram showing intended direction among web,
application, domain, adapters, and external read boundaries. Treat dependency
violations as architecture work, not something documentation should disguise.

**Exit gate:** cross-cutting guarantees are described once at their owner and
referenced from consumers instead of being inconsistently repeated.

### DOC-6 — Mark future Stages I–K and keep docs current

**Size:** Small initially, then continuous.

**Status:** Initial implementation complete on 2026-08-06; maintenance is
continuous.

- Document `approvals.py` as standalone domain behavior, not an integrated
  executable import plan.
- Mark clean-package certification, controlled Odoo writes, execution journal,
  and post-write reconciliation as future boundaries.
- State the read-only restriction at every connector/composition boundary
  where a future writer might otherwise be inferred.
- Add an advisory documentation report to normal verification, then consider
  making missing docs blocking only for newly added public services, ports,
  repositories, and domain operations.
- Add a pull-request checklist item: when a workflow connection, evidence
  binding, side effect, or migration-stage status changes, update the owning
  docstring, code map, contract/plan, and focused tests together.

**Exit gate:** current code never claims to implement Stages I–K, and new
public workflow APIs cannot be merged without an explicit documentation
decision.

## Review and delivery rules

Documentation should be delivered in small, reviewable slices rather than one
repository-wide docstring patch:

- one migration journey or one cohesive domain family per change;
- no behavior refactor mixed into a documentation-only change unless required
  to correct a factual defect;
- verify referenced methods and files with static checks and focused tests;
- have the reviewer navigate from the entry point using only the new
  docstrings and code map;
- update current status from active contracts/plans, not from historical
  comments or inferred roadmap intent.

The first implementation slice should be DOC-0 plus the DOC-1 preparation and
preflight journeys. They cross the most important current boundary—approved
source-side evidence to read-only target comparison—and establish the pattern
before expanding it across 133 modules.

## Completion criteria

The plan is complete when:

- every Python module is classified by layer and migration stage;
- every public service, port, repository, and non-obvious domain operation has
  a useful semantic docstring or a recorded exemption;
- every central class states its responsibility, lifecycle, collaborators,
  and invariants;
- the six critical journeys are traceable from entry point to evidence and
  tests in `docs/architecture/python-code-map.md`;
- Stage A–H documentation matches the implemented current reality;
- Stage I–K boundaries are explicit and cannot be mistaken for implemented
  execution capability;
- documentation checks prevent regression on newly introduced workflow APIs;
- a maintainer unfamiliar with a file can answer “what does this do, where is
  it called, what does it call, what evidence does it change, and what should I
  open next?” without reverse-engineering the entire package.
