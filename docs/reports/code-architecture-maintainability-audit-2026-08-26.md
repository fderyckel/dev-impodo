---
audience: developer
kind: report
status: current
---

# Code architecture maintainability audit

## Decision supported by this audit

This audit helps maintainers decide whether Impodo's current code organization
still protects the intended separation among a data project, Data versions,
workspaces, Recipes, and migration runs. It also identifies the organizational
changes that should happen before continued feature growth makes those
boundaries harder to preserve.

The intended reader is a human maintainer or coding agent who needs to decide
where a change belongs, which boundaries it may cross, and which evidence must
remain unchanged.

## Executive conclusion

Impodo has preserved its core domain ownership model. The code, persistence
layout, contracts, and focused tests consistently distinguish these owners:

- A Project owns the business migration effort and the membership lineages for
  its Data versions, runs, workspaces, Recipes, and Cutover plan.
- A Data version owns one complete source delivery and its source evidence.
- A workspace owns the working and operational evidence for one use of a Data
  version. It stores bounded references to accepted source data instead of
  taking ownership of that data.
- A Recipe owns reusable rule revisions. It does not own source rows, target
  credentials, approvals, or migration results.
- A migration run owns shared target and requirement evidence for one use of a
  Data version. Each Recipe application receives its own isolated workspace.

The principal risk is therefore not a broken domain model. The risk is that the
physical code structure no longer makes the correct model easy to see or
enforce. Large orchestration files, broad repositories, a flat top-level Python
package, a very large dependency container, and flat tests now create change
hubs that span several owners. A few application modules also import concrete
adapters, contrary to the portable application-layer intent in ADR-008.

The recommended response is an incremental code-organization refactor around
the accepted model. Impodo should not redesign the domain or change the store
layout as part of this work. The proposed target and sequencing are documented
in the [code organization remediation plan](../plans/code-organization-remediation.md).

## Assessment summary

| Area | Assessment | Reason |
| --- | --- | --- |
| Domain ownership | **Healthy** | Independent identities, owner-specific evidence, and explicit lifecycle contracts are implemented and tested. |
| Persistence ownership | **Healthy** | Data version, workspace, run, Recipe, and protected evidence boundaries are distinct and linkage checks fail closed. |
| Package navigability | **Needs attention** | The top-level package mixes aggregate roots, workbench projections, jobs, ports, adapters, and technical utilities. |
| Dependency direction | **Needs attention** | The intended inward dependency rule is documented but not executable, and current application-to-adapter imports exist. |
| File and class cohesion | **Needs attention** | Several files and classes coordinate too many use cases or persistence responsibilities. |
| Test organization and isolation | **Needs attention** | Tests are flat, several suites are very large, and one current integrated test is order dependent. |
| Browser asset organization | **Needs attention** | One JavaScript file, one stylesheet, and the Mapping template have become broad cross-feature change surfaces. |
| Developer orientation | **Strong but incomplete** | Current contracts and the Python code map explain behavior well, but they do not yet define enforceable file-placement rules. |

## Scope and method

This report assesses commit `d2061c3` on 2026-08-26. It does not assess feature
desirability, visual design, or future product scope. It does not authorize a
storage migration or change current behavior.

The audit used the following evidence:

- ADR-008, ADR-014, and ADR-015;
- the Project, Recipe, integrated-run, and evidence lifecycle contracts;
- the current architecture overview, Python code map, and workflow registry;
- the current Python module and import graph;
- concrete aggregate, service, repository, schema, route, template, and test
  implementations; and
- focused ownership and identity tests.

The current repository contains:

| Surface | Current size |
| --- | ---: |
| Production Python | 275 modules and 128,440 lines |
| Top-level `src/impodo` Python modules | 66 modules |
| Tests | 96 top-level modules and 54,420 lines |
| Jinja templates | 42 files and 7,613 lines |
| Main browser JavaScript | 4,031 lines |
| Main browser stylesheet | 4,280 lines |

File size alone does not prove poor design. These counts matter because the
largest files also contain several distinct business or persistence reasons to
change.

## What remains correctly separated

### Project and root identities

`MigrationProject`, `DataVersion`, `MigrationRun`, `MigrationWorkspace`, and
`Recipe` remain independent types with independent UUIDs. Project creation
creates the Project, first Data version, first run, and first workspace without
creating a Recipe. Recipe publication preserves the existing Project, Data
version, run, and workspace identities.

The current implementation is consistent across
`migration_projects.py`, `data_versions.py`, `migration_runs.py`,
`migration_workspaces.py`, and `recipes.py`. The identity gate in
`tests/test_identity_semantics.py` also checks that one identifier namespace is
not used as another.

### Data version and workspace evidence

The Data version store owns source-package files, catalogues, parsing choices,
logical datasets, and snapshot references. Source artifacts use
`artifacts/dv/<data_version_id>`. The workspace store contains the selected
dataset identities and snapshot hashes, and workspace artifacts use
`artifacts/ws/<workspace_id>`.

`WorkspaceDataVersionSourceService` freezes Data version evidence and then
materializes a workspace projection. `WorkspaceMappingSourceProjection` reads
that projection without copying the Data version database or source rows. The
store schemas and `WorkspaceAccessContext` linkage checks reinforce the same
boundary.

### Recipe meaning

Recipe publication reads eligible workspace evidence and writes a portable,
immutable Recipe revision. The Recipe envelope excludes source rows, physical
source identities, target credentials, numeric Odoo record IDs, approvals,
execution journals, and reconciliation outcomes. Publication provenance
records where the meaning came from without transferring ownership.

### Test run and Recipe application isolation

An integrated Test run uses one Test Data version and one shared run target.
The run planner creates one `RecipeApplication` and one isolated workspace for
each exact Recipe revision. Run-aware adapters project only the required slice
of shared schema and supporting-reference evidence into each application.

The integrated-run tests verify that two Recipes share one target while their
workspaces remain distinct, that source rows are not copied, and that run
progress can be read without opening every workspace.

## Findings that require action

### A1. The filesystem no longer communicates the domain model clearly

**Priority:** High.

The 66 Python modules directly under `src/impodo` include aggregate roots,
domain values, application services, job records, Odoo boundaries, artifact
ports, workbench projections, and technical utilities. The newer `domain`,
`application`, `adapters`, and `web` packages introduce useful layers, but the
older top-level modules remain a second organizational scheme.

For example, a run change may require navigating among
`migration_runs.py`, `migration_test.py`, `migration_run_planning.py`,
`application/test_run_setup_service.py`,
`application/migration_run_planning_service.py`, several DuckDB repositories,
and run routers. A workspace change has a similar split between
`migration_workspaces.py`, `workspace_state.py`, workspace contracts,
application services, and several repositories.

The files use accurate names when read individually. The folder hierarchy does
not explain which file is the domain owner, which file is a use case, which
file is a persistence adapter, or which file is a derived workbench view. This
increases search time and makes it easier for an agent to add a second
implementation beside the real owner.

**Recommendation:** Keep the existing layers, but add a capability package
inside each layer. Project, Data version, workspace, Recipe, run, cutover,
preparation, and execution code should each have an obvious home. The target
tree and placement rules are in the remediation plan.

### A2. Dependency direction is documented but not enforced

**Priority:** High.

ADR-008 requires the domain and application-service layers to remain portable
across local and future hosted composition roots. Most current dependencies
follow that direction, but the import graph has these direct inward-layer
violations:

- `application/bounded_preparation.py` imports the concrete Polars adapter.
- `application/odoo_provenance_service.py` imports the protected provenance
  and comparison adapter codecs.

These imports create three concrete `application -> adapters` edges. The
adapter that imports an application query contract is not a violation; an
outbound adapter is expected to depend inward on the port it implements.

The module graph also contains one cycle between `inspection.py` and
`source_worker.py`. The inspection service imports the isolated worker inside
methods, while the worker imports the inspection types and parser.

No current test defines the permitted layer matrix or rejects a new cycle.
The documentation inventory checks module orientation, not dependency
direction.

**Recommendation:** Introduce consumer-owned ports for the Polars and protected
codec operations, inject the local implementations at composition, and extract
the source inspection contracts or worker entry point so the cycle disappears.
Add an AST-based architecture test that rejects domain-to-outer-layer imports,
application-to-adapter or web imports, and non-trivial module cycles.

### A3. Several files are now multi-responsibility change hubs

**Priority:** High.

The most significant examples are:

| File or class | Current evidence | Reasons it changes today |
| --- | ---: | --- |
| `PreparationSessionRepository` | 4,159-line file; 56 methods on the main class | Session lifecycle, prepared snapshots, derived artifacts, direct rows, relationship findings, quality indexes, normalization facts, reconciliation, hashing, and cleanup. |
| `MigrationFoundationRepository` | 2,511-line file; 81 methods | Project, Data version, source package, run, target setup, workspace, source projection, operation intent, audit, and serialization persistence. |
| `MigrationRunPlanningService` | 1,938-line file; 23 methods | Test review and activation, Production review and activation, application materialization, Odoo defaults, recovery, requirement union, dependency order, and collision checks. |
| `TestRunSetupService` | 1,642-line file; 29 methods | Setup creation, Recipe source requirements, run values, file matching, Odoo-check requirements, ownership resolution, and root creation. |
| `create_local_app` | 707-line function; 25 parameters | Storage, repositories, services, target I/O, jobs, security, route context, and test seams. |
| `WebContext` | 58 dependency fields | Every Project, workspace, run, target, job, artifact, security, and local-stack route boundary. |

Across production Python, 91 files exceed 500 lines and 33 exceed 1,000 lines.
The issue is not the threshold itself. The issue is that the large files above
span several named use cases or owner-specific ports, so unrelated changes
collide in the same review surface.

`MigrationFoundationRepository` is the clearest example of a correct database
boundary represented by an overly broad code boundary. One registry and one
transaction coordinator are appropriate. One class implementing every
Project, Data version, run, workspace, source-package, and operation port is
not required to preserve that transaction.

**Recommendation:** Decompose by stable responsibility while retaining shared
connections and transactions. Multi-root atomic work should remain in an
explicit registry transaction coordinator. Owner-specific repositories should
not silently open independent transactions during a coordinated operation.

### A4. The composition root exposes the whole application to every router

**Priority:** Medium.

`create_local_app` correctly acts as the local composition root, but it now
constructs nearly every repository and service in one 707-line function.
`WebContext` then makes 58 dependencies available to every router builder.
Routes usually use only a small subset.

This arrangement makes local construction easy, but it hides capability
boundaries and raises the cost of a future hosted composition. A route can
also start depending on an unrelated service without any constructor change
that makes the new coupling visible.

**Recommendation:** Keep one top-level local app factory. Delegate assembly to
small capability builders and pass narrow Project, workspace, run, and target
contexts to their router groups. Composition may import adapters; route modules
should depend on application services and presentation contracts only.

### A5. Test layout and isolation reduce refactoring confidence

**Priority:** High.

All 96 tracked test modules are directly under `tests`. Several files mix unit,
persistence, browser, recovery, and end-to-end concerns:

- `tests/test_web_app.py` has 8,313 lines. Its largest test class spans 6,947
  lines, and one end-to-end test method spans 1,911 lines.
- `tests/test_integrated_recipe_runs.py` has 3,020 lines and contains compiler,
  fresh-data matching, persistence, planning, recovery, and browser tests.
- Four test modules exceed 2,000 lines, and eleven exceed 1,000 lines.

The focused ownership command ran 72 tests. Seventy-one passed. The integrated
test named `test_review_projection_routes_required_default_recovery` failed
when earlier tests in its module ran first because the card offered **Start
preparation** instead of **Review Odoo defaults**. The same test passed alone,
and its 12-test class passed alone, while the full 23-test module reproduced
the failure. This is evidence of order-dependent shared state or incomplete
test cleanup.

The failure does not disprove the ownership model. It does mean the current
test organization cannot always provide deterministic evidence for a change.

**Recommendation:** Fix the shared-state leak before relying on the full
integrated module as a gate. Organize tests by architecture layer and
capability, use deterministic builders with explicit ownership, and retain one
small end-to-end smoke scenario rather than making one method prove the whole
browser product.

### A6. Browser files have become cross-feature change surfaces

**Priority:** Medium.

`web/static/app.js` has 4,031 lines and contains navigation state, Mapping
editing, value matching, target connection state, transformation previews, and
several job pollers. `web/static/app.css` has 4,280 lines. The Mapping template
has 1,467 lines.

Current Python tests fetch these assets and assert important strings and
rendered controls. The filesystem does not provide a page or component boundary
for the browser behavior itself.

**Recommendation:** Split JavaScript and CSS into framework-free capability
files loaded by one small entry point. Split the Mapping template into named
partials or macros whose forms retain the same server-side contracts. A new
frontend build tool is not required for this refactor.

### A7. Documentation explains behavior better than change placement

**Priority:** Medium.

The architecture overview, lifecycle contracts, workflow registry, and Python
code map provide strong current-behavior guidance. All 275 Python modules pass
the module-docstring orientation gate. This is a material strength.

The advisory inventory reports 817 undocumented public symbols out of 2,620.
Many are obvious protocol methods or data accessors, so a quota would reward
repetitive prose. The more important gap is that no current document defines:

- the permitted dependency direction;
- the package that owns a new use case;
- the difference between an owner repository and a cross-owner transaction
  coordinator;
- when a workspace cache is allowed to repeat Data version evidence; or
- the tests that a new module location must satisfy.

**Recommendation:** Use the remediation plan as the proposed placement
contract. When the package migration is complete, promote the final placement
rules into a current architecture page and enforce them with tests. Add
docstrings to orchestrators, ports, and non-obvious public behavior through
semantic review, not through a percentage target.

## Recommended priority

1. Resolve the order-dependent integrated test and capture a deterministic
   baseline.
2. Remove the current application-to-adapter imports and the inspection-worker
   cycle, then add the architecture dependency gate.
3. Decompose the foundation, run-planning, and preparation-session change hubs
   without changing stores, schemas, hashes, or transaction semantics.
4. Introduce capability packages inside the existing layers and move one
   owner at a time.
5. Narrow browser contexts and organize tests by capability and layer.
6. Split Mapping browser assets after the server-side ownership seams are
   stable.

## What should not change during remediation

- Project, Data version, run, workspace, Recipe, Recipe application, and
  Cutover plan identities must remain independent.
- Source data and source evidence must remain Data version owned.
- Workspace stores must retain only the permitted references and current
  workspace evidence.
- Recipe revisions must remain portable and immutable.
- Shared target and requirement evidence must remain run owned.
- Each Recipe application must keep an isolated workspace.
- Current storage generations, hashes, audit evidence, and forward-upgrade
  behavior must not change without an explicit contract and architecture
  decision.
- The refactor must not add semantic aliases, dual reads, dual writes, or a
  second implementation hidden behind a compatibility path.

## Verification evidence

The following checks were run during this audit:

- `python scripts/code_documentation_inventory.py --check` passed for all 275
  production Python modules.
- The advisory documentation inventory reported 2,620 public symbols, of which
  1,803 have docstrings and 817 do not.
- The focused ownership, identity, Data version, Project authoring, workspace
  access, and integrated-run command ran 72 tests. Seventy-one passed and one
  order-dependent integrated test failed as described in finding A5.
- The failing integrated test passed alone.
- All 12 tests in its `IntegratedRecipeRunTests` class passed when that class
  ran alone.
- The complete 23-test integrated-run module reproduced the one failure.
- `scripts/documentation_quality.py --check --report` passed for all registered
  workflow stages.
- All seven documentation-quality and code-orientation unit tests passed.
- `git diff --check` passed for the tracked documentation change, and the two
  new documents passed the same whitespace check separately.

This was a code and documentation audit. It did not run a browser screenshot
review, the complete test suite, performance probes, remote Odoo acceptance, or
release qualification. Vale was unavailable in the current environment and was
not run.

## Related authority

- [Architecture overview](../architecture/overview.md)
- [Python code map](../architecture/python-code-map.md)
- [Project and workspace lifecycle](../developer/contracts/project-lifecycle.md)
- [Recipe lifecycle](../developer/contracts/recipe-lifecycle.md)
- [Integrated Test run lifecycle](../developer/contracts/integrated-run-lifecycle.md)
- [Evidence lifecycle](../developer/contracts/evidence-lifecycle.md)
- [Code organization remediation plan](../plans/code-organization-remediation.md)
