---
audience: developer
kind: plan
status: current
---

# Shared server detailed phased implementation plan

## Status, reader, and authority

**Status:** Ranked seventh in the cross-plan implementation order. Phases 0 and
1 are complete. Phase 2 supported-host qualification remains open, while the
local Phase 3 persistence trial started on 2026-10-28. The local empty-stack
Compose rehearsal passed. The image now uses a refreshed pinned
Bookworm base. Its repeat local scan found no critical vulnerabilities, eight
high-severity findings, and no fixable critical or high finding. Vulnerability
acceptance or upstream remediation, the first hosted image-quality run,
production secret and database-role provisioning, remote certificate policy,
accepted supporting-image versions, and clean Debian and Ubuntu host
rehearsals remain open. The current delivery mode is local trial only because
no remote Impodo server is allocated. Local Phase 3 persistence increments may
proceed after the local trial continuation gate below, but this does not pass
the Phase 2 supported-host exit gate or authorize a shared release. The current
supported Impodo product remains the local application; the container has
completed the local Phase 3 persistence and Phase 4 account-administration
trials, but it is still qualification infrastructure rather than a shared
multi-user release.

This document is for maintainers who will implement and review the shared
server profile. The parent
[Linux server deployment architecture plan](07-vm-deployment-duckdb-worker-management.md)
defines the product decisions and target boundaries. This document turns those
decisions into ordered delivery work. If the two plans disagree, update and
approve the parent architecture decision before changing this delivery plan.

The sequence is based on dependency, evidence safety, and the ability to keep
local Impodo usable throughout delivery. It intentionally contains no effort
estimates. Implementation difficulty does not decide product scope.

## Completion outcome

This plan is complete when Impodo has two supported compositions:

- The local desktop composition keeps one implicit local operator, local
  DuckDB and Parquet processing, local protected storage, and no server
  administration requirements.
- The shared server composition runs on a supported Debian or Ubuntu host from
  a pinned container image. It uses PostgreSQL for Organizations, named users,
  roles, Project visibility, sessions, jobs, leases, and bounded projections.
  It continues to use DuckDB, Parquet, retained source files, and protected
  artifacts for migration processing and detailed evidence.

The first server release authenticates Impodo-managed accounts. Each customer
user belongs to exactly one Organization. The server provides usable
administration, Project-scoped permissions, conflict-safe collaborative edits,
equal dynamic resource sharing, approved outbound Odoo connectivity, durable
work, selectable Project retention, backup, restore, and operational runbooks.
Corporate identity providers are a later phase over the same user and
authorization model.

## Verified current baseline

The following statements describe the current checkout at the time this plan
was written. They are implementation evidence, not promises about completed
server behavior.

| Current seam or limitation | Delivery consequence |
| --- | --- |
| [`create_local_app`](../../src/impodo/web/composition/local_app.py) constructs the local service graph. [`web/app.py`](../../src/impodo/web/app.py) is a compatibility import, while [`http_app.py`](../../src/impodo/web/composition/http_app.py) installs the shared route set without choosing deployment security. [`service_graph.py`](../../src/impodo/web/composition/service_graph.py) now builds the deployment-neutral Project foundation, authoring, Recipe application, integrated run setup, and preparation service groups from injected dependencies. | Continue moving responsibility-named execution, job, artifact, and target groups out of the large local builder before connecting server adapters. |
| [`WebContext`](../../src/impodo/web/context.py) contains authorization, repositories, job managers, target factories, an optional local-target runtime, and the browser workflow services, but no actor or local launch token. Local fixtures and direct automation now resolve the implicit actor through the same authenticated local identity seam as browser requests. | Keep that request-scoped boundary. Do not restore a local or server actor field on the application context. |
| [`LOCAL_ACTOR`](../../src/impodo/domain/shared/access.py) owns every current capability, and `Actor.has` treats `PROJECT_ADMIN` as a universal bypass. | Keep that trust model local. The server must resolve scoped grants and must not reuse the universal bypass. |
| [`MigrationProjectService.list`](../../src/impodo/application/project/service.py) checks `project.view` once and then returns every Project summary from its repository. | Add an authorization-aware visibility query before exposing a multi-Organization catalogue. |
| [`MigrationFoundationDatabase`](../../src/impodo/adapters/duckdb/migration_foundation_database.py) and [`MigrationFoundationRepository`](../../src/impodo/adapters/duckdb/migration_foundation_repository.py) own the current registry. Its schema mixes lifecycle, catalogue, operation, and migration-evidence concerns. | Classify facts before implementing PostgreSQL. Do not migrate whole tables merely because they currently share one file. |
| [`GovernedArtifactStores`](../../src/impodo/application/shared/artifacts.py), [`JobDispatcher`](../../src/impodo/application/shared/jobs.py), `AuthorizationPolicy`, and `SecretStore` already provide useful ports. | Extend or split the consumer-owned ports instead of importing PostgreSQL, Docker, or server paths into application services. |
| The current `JobRequest` and `JobRecord` support a small allowlist and basic queued, running, successful, failed, and cancelled states. | Extend the portable job contract for durable attempts, interruption, review, leases, fencing, and receipts before moving workflow managers. |
| Preparation, source capture, preflight, Recipe runs, loads, and corrections use several process-local managers and the in-process [`SerialJobRunner`](../../src/impodo/application/shared/serial_job_runner.py). | Migrate one job family at a time onto a common durable protocol while preserving action-specific safety. |
| Architecture tests already enforce inward dependencies and adapter construction, and [`test_hosting_contracts.py`](../../tests/architecture/test_hosting_contracts.py) covers initial actor, artifact, job, and approval seams. | Expand these tests as server contracts become executable. Do not replace them with container-only end-to-end coverage. |

At the initial baseline, no Dockerfile, Compose server topology, PostgreSQL
control-plane repository, Organization dashboard, or corporate sign-in
implementation existed. The dated implementation status sections below
supersede that baseline as each bounded increment lands.

## Non-negotiable implementation rules

1. Domain and application behavior remains independent of FastAPI, PostgreSQL,
   Docker, Linux paths, identity-provider SDKs, and object-storage SDKs.
2. The local and server entry points construct different adapters. Shared
   services do not accumulate deployment-mode conditionals.
3. PostgreSQL never becomes the transformation engine or a row warehouse for
   source and prepared data.
4. A DuckDB database has one writable owner for an operation. A worker never
   opens a DuckDB file over network or object storage.
5. Each mutable fact has one authority. A PostgreSQL projection identifies and
   summarizes committed evidence; it does not become another mutable copy of
   that evidence.
6. Every cross-store mutation is idempotent and recoverable from a durable
   operation identity, exclusive fenced ownership, data-plane receipt, and
   verified projection update.
7. Every server read and command is scoped by the authenticated Organization
   and permitted Project before a repository opens customer evidence.
8. Local behavior remains the compatibility baseline until the server profile
   passes its own release gate. Server delivery must not require local users to
   run PostgreSQL, create accounts, or manage Organizations.
9. Current evidence hashes, journal-before-write behavior, unknown Odoo outcome
   handling, and protected-evidence boundaries remain intact during structural
   refactoring.
10. A new server page is incomplete until its authorization, Organization
    isolation, conflict behavior, database lifetime, and browser journey have
    focused tests.

## Proposed package and deployment shape

The exact filenames may change during review, but responsibility must remain
visible. The target shape is:

```text
src/impodo/
|-- domain/                         shared business meaning
|-- application/                    shared use cases and consumer-owned ports
|-- adapters/
|   |-- duckdb/                     shared migration data plane
|   |-- artifacts/                  local and server filesystem stores
|   |-- postgresql/                 server control-plane repositories
|   |-- identity/                   password and later OIDC adapters
|   |-- jobs/                       inline local and durable server adapters
|   `-- secrets/                    local and server secret adapters
`-- web/
    |-- composition/
    |   |-- http_app.py             deployment-neutral HTTP installation
    |   |-- service_graph.py        deployment-neutral service groups
    |   |-- local_app.py            local composition
    |   `-- server_app.py           shared-server composition
    |-- routers/admin/              planned server administration routes
    |-- presenters/admin/           planned administration wording
    `-- templates/admin/            planned administration pages

deploy/
|-- Dockerfile
|-- compose.yaml
|-- proxy/
`-- examples/                       non-secret configuration templates
```

The public local entry point remains `create_local_app`. A new
`create_server_app` builds the server graph. If moving `create_local_app` out
of `web/app.py` needs a temporary import compatibility layer, remove that layer
before Phase 1 exits and migrate repository callers together.

## Delivery method and quality gates

Each work package should be reviewable independently and must leave the
repository green. A phase may span several pull requests, but its exit gate is
one product boundary and cannot be declared complete from partial scaffolding.

Every implementation pull request must state:

- the phase and work-package identifier it implements;
- the current behavior it preserves;
- the new planned behavior that becomes executable;
- the authoritative store for every changed fact;
- failure, cancellation, retry, and rollback behavior;
- focused tests and omitted live qualification; and
- paired documentation that becomes current only when the behavior ships.

Forward-only schema migrations require a pre-migration backup and an explicit
compatibility statement. An application rollback may use an earlier image only
when its schema compatibility is proven. Otherwise recovery restores the
pre-migration backup to a separate root; it never silently runs old code over a
newer control or evidence schema.

The minimum gate after every phase includes:

- dependency and architecture checks;
- local focused workflow tests for changed shared services;
- server contract and integration tests introduced by that phase;
- documentation quality checks;
- `git diff --check`; and
- a review that logs, error pages, fixtures, and artifacts contain no real
  customer values or secrets.

## Phase dependency map

```text
Phase 0  contracts and ownership
   |
Phase 1  explicit compositions and request identity
   |
Phase 2  Linux container baseline
   |
Phase 3  PostgreSQL control plane and recovery protocol
   |
Phase 4  built-in accounts and Organization administration
   |
Phase 5  scoped roles, separation of duties, and edit conflicts
   |
Phase 6  durable workers and equal dynamic scheduling
   |
Phase 7  Odoo networking, secrets, retention, backup, and operations
   |
Phase 8  multi-Organization pilot and release gate
   |
Phase 9  corporate identity providers
```

Later phases may prepare interfaces early, but they do not bypass an earlier
exit gate. In particular, containerizing the current local application does
not qualify it as a server profile.

## Phase 0 - Freeze contracts and classify ownership

**Implementation status on 2026-10-23:** Complete. The current baseline is
recorded in the
[Phase 0 baseline report](90-2026-10-22-shared-server-phase-0-baseline.md).
The accepted
[server fact-ownership inventory](../architecture/server-fact-ownership.md)
and portable domain contracts passed their focused review gate. The user's
instruction to start the next phase on 2026-10-23 records acceptance of the
fact-ownership inventory and opens Phase 1.

Current Phase 0 delivery state:

- work package 0.1 is complete: the reproducible local baseline and explicit
  omissions are recorded;
- work package 0.2 is complete: ADR-008 is corrected and ADR-017 and ADR-018
  record the accepted target decisions;
- work package 0.3 is accepted: every current durable table and payload family
  has an authority and cross-store recovery rule;
- work package 0.4 is complete at contract level: no new adapter is composed
  into the local runtime; and
- work package 0.5 is complete for the Phase 0 contracts and architecture
  guards.

### Objective and entry conditions

Establish the exact target contract before moving persistence or identity.
This phase starts from the passing local main-branch baseline and the approved
parent architecture plan.

### Work package 0.1 - Record the delivery baseline

- Record the base commit, Python and dependency locks, supported current
  Windows runtime, DuckDB schema generations, and focused architecture tests.
- Record the current project-root layout and representative sanitized store
  sizes. Inspect historical stores without upgrading or rewriting them.
- Record current local page-read connection and schema-check ceilings for the
  routes that later receive shared query changes.
- Record the current process-local job managers, lifecycle states, cancellation
  behavior, and restart behavior.

**Deliverable:** A point-in-time baseline record under `docs/plans/` with
commands and results. It is evidence, not a permanent performance guarantee.

### Work package 0.2 - Accept the architecture decisions

Add or revise accepted decisions for:

- `Organization` and `organization_id` as the server tenancy boundary;
- one customer user belonging to exactly one Organization;
- PostgreSQL as the server control plane and DuckDB, Parquet, and protected
  artifacts as the migration data plane;
- explicit local and server composition roots;
- Impodo-managed accounts before external identity providers;
- remote-only optimistic editing, equal work-conserving scheduling, approved
  Odoo network profiles, and selectable Project retention; and
- a single-server Debian or Ubuntu Docker reference deployment.

Update ADR-008 so “corporate identity” describes the later identity phase
rather than the first authentication milestone.

### Work package 0.3 - Produce the fact-ownership inventory

Review every table and durable payload currently reachable through the
migration foundation and workspace stores. Classify each fact, not merely each
table, into one of these owners:

| Owner | Examples |
| --- | --- |
| PostgreSQL control authority | Organization, customer user, role revision, Project visibility, session, job, lease, idempotency key, bounded current status, storage key, and receipt hash. |
| DuckDB evidence authority | Source selections, schema evidence, mapping revisions, prepared rows, quality, normalization, resolution, comparison, execution rows, and reconciliation detail. |
| Immutable or protected artifact authority | Original source bytes, Parquet snapshots, Recipe packages, reports, protected identifiers, and target-bound evidence. |
| Local-only control authority | Loopback launch session, local-stack ownership, and Windows Credential Manager bindings. |
| Derived projection | Counts, labels, progress, and current pointers that can be rebuilt from an authoritative receipt. |

The inventory must identify mixed current tables such as Project, Recipe,
cutover, operation-intent, and event records whose future control metadata and
detailed evidence require different owners. It must also identify the command
that changes each fact and the recovery rule when two stores participate.

**Deliverable:**
[`docs/architecture/server-fact-ownership.md`](../architecture/server-fact-ownership.md),
linked from the parent plan and ADR. No PostgreSQL schema work begins before
review accepts it.

### Work package 0.4 - Define portable contracts

Create or extend immutable domain and application contracts for:

- `OrganizationId`, authenticated principal, stable `ActorIdentity`, and the
  request's fixed Organization scope;
- effective authorization, including Project scope, role or grant revision,
  policy revision, and expiry;
- visibility-filtered Project queries;
- storage-neutral artifact identities and materialization;
- idempotent operation requests and semantic input hashes;
- durable job request, attempt, state transition, cancellation, interruption,
  progress, and result identities;
- Workspace lease, heartbeat, expiry, release, and fencing token;
- data-plane operation receipt and PostgreSQL projection receipt;
- optimistic mutation revision and conflict result; and
- retention selection, deletion request, deletion result, and minimal deletion
  receipt.

Keep actors and credentials out of portable Recipe payloads. Keep raw source
or target values out of jobs, logs, and PostgreSQL projections.

### Work package 0.5 - Add contract-first tests

- Extend `tests/architecture/test_hosting_contracts.py` or split it into
  focused files before it becomes oversized.
- Prove immutable identity, Organization scope, semantic idempotency, stale
  lease rejection, fencing order, receipt substitution rejection, and
  optimistic-conflict results with in-memory fakes.
- Add architecture checks that forbid PostgreSQL and identity adapters from
  domain and application packages.
- Add a check that new shared code cannot import `LOCAL_ACTOR`.

### Phase 0 exit gate

- Accepted decisions and the fact-ownership inventory agree with the parent
  plan.
- Every proposed PostgreSQL fact has one authority and recovery rule.
- Portable contracts and their focused tests pass without adding server
  behavior to the local runtime.
- The baseline report records all omitted live and performance evidence.

## Phase 1 - Extract explicit compositions and request identity

**Implementation status on 2026-10-28:** Complete. The first increment added
an explicit local entry module, a fail-closed server entry point, immutable
request-identity resolution for the implicit local Organization, and an
Organization-scoped Project query used by the local Project-list route.

The second increment migrates the Project overview, its Recipe detail and
mutation routes, and Workspace access middleware to the cached request
identity. A Project is now resolved through the scoped query before related
DataVersion, run, Workspace, Recipe, or protected evidence reads. Workspace
middleware checks the request Organization and authorization receipt before
resolving canonical parent lineage, checks Project visibility before the route
can open a child store, and returns the same opaque `Workspace not found`
response for missing and unauthorized identities. Focused tests prove that a
foreign Organization does not open the local Project registry, a denied
Workspace does not enter the route or open its Workspace store, and middleware
plus route resolve identity only once. Project mutations require their exact
delete or Recipe-publication capability during that parent lookup, before any
cleanup or contained-resource work starts.

The third increment migrates the Workspace setup, source-file, source review,
related-table, target-connection, Odoo-data, and Match-data request paths. Each
route obtains the cached request identity and passes its actor explicitly to
commands and request-launched background Odoo checks. Shared source-file,
target-reader, schema, mapping, and presenter helpers receive that actor rather
than reaching back to the local process actor. Architecture checks now reject
new `WebContext.actor` reads in every migrated module.

The fourth increment migrates Prepare data, quality, resolution,
normalization, Final review, load, correction, Odoo-transfer, destination
matching, cutover, fresh-data, Test-run, and Production-run request paths.
Request-launched preparation and load jobs now carry the authorizing actor in
their queued work item and pass it to status publication and recovery; a
worker never recovers identity from application-global state. Execution
preview, navigation enrichment, target-credential ownership, and supporting
Odoo reads also require the caller's actor. The architecture rule now scans
every `impodo.web` production module and rejects direct `context.actor` or
`application.actor` reads, including in future modules.

The fifth increment moves the complete local construction graph from
`web/app.py` into `web/composition/local_app.py`. The historical module now
only re-exports `create_local_app`. A deployment-neutral HTTP installer owns
the common route set, error handling, templates, static assets, Workspace
middleware, and job shutdown. The local composition injects its launch
lifecycle router, signed local session, and loopback transport security, so a
future server composition can inject different authentication and transport
security without copying routes.

The sixth increment migrates local fixtures and direct automation to resolve
the implicit actor through `LocalRequestIdentityResolver`. Authorization
tests use one mutable test resolver so middleware and routes receive the same
identity for a request. The local-only `LocalWebContext` compatibility
subclass has been removed, and both deployments now share the actor-free
`WebContext` contract.

The sixth increment also adds `build_foundation_service_graph`. It constructs
Project, Data version, migration-run, run-target, migration-workspace, source-
package, Project-authoring, Odoo-source-restart, Workspace-state, and
Workspace-access services from deployment-neutral repository and
authorization ports. The local composition injects the existing DuckDB-backed
repository. A future server composition can inject the accepted PostgreSQL
control-plane adapters without copying that application-service wiring.

The seventh increment adds `build_preparation_service_graph`. It constructs
the quality, normalization, duplicate-resolution, preparation, and final-
review services from explicit application ports. The local composition still
chooses the DuckDB repositories, governed artifact stores, workspace access
service, and Polars transformation adapter. This keeps the detailed migration
data plane common to local and server deployments without allowing the shared
builder to choose a storage technology or open a database.

The eighth increment adds `build_authoring_service_graph`. It constructs the
source workspace and DataVersion projection, Recipe compilation, publication
and application, schema governance, Match data, categorical and numeric
coverage, transformation-impact, row-inclusion, and matching-order services
from explicit ports. The local composition still constructs and injects every
DuckDB repository, protected filter store, governed artifact store, and local
authorization adapter. One source-projection service is now reused by source
finalization and run planning instead of being constructed twice. This is a
composition-only change: it neither moves detailed data to PostgreSQL nor
enables the server entry point.

The ninth increment adds `build_run_service_graph`. It reuses the shared
foundation and authoring services to construct integrated run planning, Test
setup, Production setup, and the common run-setup facade. The local
composition still creates and injects the DuckDB run-planning repository and
the protected Test-run, Production-run, and Cutover-plan repositories. This
preserves the existing cross-store recovery sequence and does not open or
cache a database in the shared builder. At that increment, the established run
facades still lacked application-owned repository protocols.

The tenth increment closes that type-contract gap. `application/run/ports.py`
now defines the exact foundation, integrated planning, Test setup, Production
setup, and Cutover-plan persistence operations consumed by the run services.
The stable run facades and their focused collaborators depend on those ports,
and `build_run_service_graph` accepts the same ports instead of `object`. The
local composition proves that the current DuckDB repositories implement the
contracts, while a future server composition can inject other adapters without
changing run orchestration. The change does not move any fact, open another
database connection, or alter the existing transaction and recovery owners.
Restart recovery now also validates the typed saved activation payload before
reusing its Workspace revision, reference evidence, credential generations,
and write identity.

The eleventh increment adds `build_execution_service_graph`. It constructs
the reviewed load service, Odoo-to-Odoo transfer execution, reconciliation,
protected reconciliation evidence, and fallout-workbook service from explicit
application ports. The local composition still chooses and injects the DuckDB
journal and evidence repositories, governed artifact stores, Odoo provenance,
credential-binding callback, and workbook renderer. Odoo reader and writer
factories remain deployment-specific and are created only at the bounded
request or worker entry point that uses them. The shared builder opens no
database, caches no connection, and preserves the existing remote read- and
write-identity requirements.

The twelfth increment adds `build_cutover_service_graph`. It reuses the shared
foundation, preparation, and execution graphs to construct the integrated Test
qualification evidence reader and `CutoverPlanService`. The builder receives
the run-planning, Cutover-plan, mapping, staging, and quality ports from the
deployment composition. The application-owned Cutover repository contract now
includes qualification publication, replay, listing, and selection operations
instead of relying on untyped adapter methods. The local composition still
constructs the protected Project evidence store and DuckDB repository. The
shared builder does not contact Odoo, open a database, or grant Production
execution authority.

The thirteenth increment adds `build_correction_service_graph`. It constructs
the completed-load origin publisher, successor workspace service, ordered
authoring review stages, review orchestrator, correction executor, and browser
workflow facade from explicit application ports. The correction services now
depend on named Project, source-package, prepared-evidence, final-review,
execution, and reconciliation contracts instead of untyped repository access.
The local composition still constructs the DuckDB correction repository and
protected correction store. It injects the native Polars review pipeline and a
deployment-owned target-review adapter. That adapter resolves the current
secret, verifies the live Odoo identity, and creates a scope-bound reader only
when review work runs. The shared builder neither receives credentials nor
constructs or caches an Odoo client. Correction jobs remain on the existing
local process manager until Phase 6 connects every long-running workflow to
the common durable job, authorization-receipt, lease, and fencing contracts;
the server must not adapt the closure-based local manager.

The fourteenth increment adds the shared top-level `ApplicationServiceGraph`.
It joins the seven responsibility-named service groups with the remaining
browser-facing application services, then `build_web_context` performs the one
mapping from that graph and four explicit deployment runtime bundles into the
shared route context. The bundles separate request identity and visibility,
job runtimes, infrastructure adapters, and Odoo target adapters. The local
composition now selects those adapters instead of manually repeating the
complete `WebContext` field map. The launch token remains in the local
lifecycle router and is no longer stored on the shared context.

The same increment makes local Odoo control an explicit optional
`LocalTargetRuntime`. The local composition supplies its local-stack and
metadata-reader adapters. A future server composition may omit that runtime;
it is not required to construct a false local adapter. Protected destination
create-field evidence now depends on an application-owned store port rather
than the concrete protected-filesystem class. Correction routes declare a
narrow `CorrectionRouteContext`, while continuing to receive the existing
mutable context object so job and target test seams retain their current
behavior. These are construction and type-boundary changes only. They do not
open another DuckDB connection, change evidence ownership, or enable server
startup.

The fifteenth increment removes the remaining concrete background-manager
types from the shared browser context and top-level graph. The
`application/shared/job_runtimes.py` contracts describe the exact job
lifecycle that routes currently need for Recipe checks, preparation, final
review, loading, corrections, and Odoo source capture. The local composition
continues to supply the existing in-process managers. A server composition can
instead supply durable runtime adapters without changing `WebContext` or the
route signatures. The current `enqueue` methods still accept process-local
work callables, so these ports are a compatibility boundary, not the future
PostgreSQL job-command schema. Phase 6 must replace those callables at the
server boundary with serializable command kinds, immutable input references,
authorization receipts, leases, and fencing tokens.

The same increment adds `BrowserQueryPorts`, `WorkflowSupportPorts`, and
`build_workflow_support_service_graph`. The builder constructs the browser
query facade, canonical-owner views, navigation, source intake and inspection,
derived-data authoring, supporting lookups, Recipe target matching,
preparation recovery, and connection testing from injected application ports.
`local_app.py` now chooses the DuckDB repositories and filesystem artifact
store, but it no longer repeats that application-service wiring. The builder
does not open a database, create a connection cache, contact Odoo, or change a
job's scheduling and shutdown behavior.

The sixteenth increment extracts Odoo source capture, protected provenance,
and atomic publication into `OdooSourcePorts` and
`OdooSourceServiceGraph`. The shared builder names every Workspace, selection,
schema, relationship-scope, protected-evidence, artifact, secret, and codec
dependency without choosing DuckDB, PostgreSQL, a filesystem, or an Odoo
transport. The local composition supplies the existing repositories and
protected codecs, then reuses the resulting provenance service in preparation
and transfer execution and the publication service in its current capture-job
manager. Capture selection, evidence hashing, encryption, publication, and
database lifetime semantics are unchanged.

The same increment removes a transitive local-storage dependency from the
shared browser context. `UnavailableProjectSummary` now lives in a neutral web
projection module; only the local compatibility composition imports the
DuckDB/filesystem migration implementation. A fake-server architecture test
constructs every responsibility-named service graph and the final
`WebContext` from fake ports with `local_target=None`. It also rejects DuckDB,
local identity, local compatibility storage, local application composition,
and concrete in-process job-manager imports from the shared server graph. This
is a Phase 1 composition proof, not a server startup implementation.

The fail-closed server entry still requires an explicit `ServerAdapterBundle`
that names identity, authorization, scoped Project and Workspace access,
operation journal, lease, job, artifact, and secret ports. Server startup
remains disabled even when those ports are supplied because the remaining
service groups and server adapters do not exist yet.

The local Project-list route retains one physical registry connection inside
one `run_page_read` scope. A controlled fresh-Project overview measured two
physical connections, zero schema checks, and zero retries both before and
after the second increment, and its focused ceiling is now two connections.
For a controlled mapping-ready Workspace, the third increment preserved the
same before-and-after route counts:

| Route | Physical connections | Schema checks | Lock retries |
| --- | ---: | ---: | ---: |
| Source data | 6 | 3 | 0 |
| Datasets | 6 | 3 | 0 |
| Odoo data | 6 | 8 | 0 |
| Related tables | 6 | 5 | 0 |
| Target connection | 6 | 2 | 0 |

These figures are controlled fixture evidence, not a live deployment latency
result. The fourth increment did not add DuckDB connections or schema checks;
the focused transfer page-read ceilings remain green.

Shared request, worker, fixture, and direct-automation paths no longer read a
global actor, and `WebContext` cannot carry one. Work package 1.2 is complete.
The foundation, authoring, run-setup, preparation, execution, Cutover, and
correction builders advance work package 1.1. Completed-load correction
orchestration now uses the same deployment-neutral pattern, while its secret
and Odoo reader capability remains deployment-owned. Shared routes and context
mapping now depend on structural job-runtime contracts, and browser-support
services use one deployment-neutral builder. The concrete local job managers,
artifact-store construction, remaining request and worker target factories,
and Cutover qualification artifact construction remain inside the local
composition. Durable server job commands and their PostgreSQL adapters remain
planned. Final context mapping now has one shared owner. At that increment,
work packages 1.1 and 1.4 still remained in progress. No PostgreSQL adapter,
server session, Organization administration page, or Linux runtime had been
added at that point. The later Phase 1 exit audit below closed the composition
work before Phase 2 began.

Focused verification for the sixth increment covers request identity, the
foundation-service wiring, local-stack authorization, Project setup, security,
architecture dependency rules, and transfer page-read ceilings. The selected
batch ran 24 tests without increasing the existing page-read limits. Three
additional local-stack authorization tests passed after moving their mutable
identity to the resolver seam. A separate 27-test batch passed the request-
identity, service-builder, canonical-ownership, and Project-authoring direct-
automation cases. These are controlled test results, not live server
performance evidence.

Focused verification for the seventh increment covers the two service-graph
wiring cases and 46 preparation-readiness, final-review, and preparation-
browser cases. The browser batch includes the existing physical-connection
ceiling for the transformation-impact page. The restricted Windows run first
hit the known disposable `.tmp` ACL limitation; the same tests passed under
the established fixture permission. These are controlled test results, not
live server performance evidence.

Focused verification for the eighth increment covers the three service-graph
wiring cases, five architecture cases, eight documentation cases, and four
representative source, Match-data, mapping-default, and mapping-catalog browser
cases. The mapping-catalog case retains the existing eight-connection ceiling.
The combined browser modules exceeded a five-minute verification window, and
the complete source-workflow module exceeded four minutes, so neither broad
run produced pass-or-fail evidence. These are controlled fixture results, not
live server or PostgreSQL performance evidence.

Focused verification for the ninth increment covers all four service-graph
wiring cases, one integrated-run registry-connection case, and four unaffected
architecture cases. A Fresh-data route case also passed after its harness used
the build fingerprint calculated from the current checkout, which is
equivalent to restarting Impodo after a code update. It retained the existing
connection and schema-check ceilings: 4/3 for the initial page, 8/4 for upload,
4/3 for the uploaded page, 8/7 for registration, and 4/3 for the registered
page. The unadjusted browser batch failed closed because unrelated dirty domain
files changed the build fingerprint; the Odoo-progress unit fixture also lacks
the current request-identity resolver. These are controlled fixture results,
not live server or PostgreSQL performance evidence.

Focused verification for the tenth increment covers all four service-graph
wiring cases, Test and Production saved-value contracts, and the existing Test
and Production activation-recovery paths. The concrete local adapters satisfy
the new application ports with no new Basedpyright diagnostics. These are
controlled fixture and static-contract results, not live server or PostgreSQL
performance evidence.

Focused verification for the eleventh increment covers all five service-graph
wiring cases plus the existing execution, transfer, reconciliation, fallout,
architecture, and documentation contracts. This remains local controlled-test
evidence; it does not claim live server, PostgreSQL, Odoo-network, or Linux
performance qualification.

Focused verification for the twelfth increment covers all six service-graph
wiring cases and the existing integrated qualification, protected-evidence
recovery, dependency-order, and browser explanation cases. The graph is a
composition-only change, so the existing request and repository scopes remain
the performance boundary. This is controlled local evidence, not live server,
PostgreSQL, Odoo-network, or Linux performance qualification.

Focused verification for the thirteenth increment covers all seven service-
graph wiring cases, the correction origin, successor, review, execution, and
local job contracts, and the three existing correction browser journeys. The
browser tests retain safe compact results and protected-value boundaries. The
change moves construction only; it does not add a DuckDB operation, retain a
connection across Odoo work, or claim live server, PostgreSQL, Odoo-network,
or Linux performance qualification.

Focused verification for the fourteenth increment covers the top-level graph,
the single context mapping, omission of the local Odoo runtime, the narrow
correction route contract, all seven existing service-graph builders, and the
architecture dependency rule. The three correction browser journeys and the
local session and authorization cases also pass. The Project setup browser
module currently has three source-upload expectations that return `422`
instead of the expected redirect in this dirty checkout. This increment does
not change the upload service or route, so those failures are not accepted as
verification of this composition boundary. The new graph does not add a
repository call or move an existing call across an asynchronous boundary, so
the established request-scoped DuckDB ownership remains unchanged. This is
controlled local evidence, not live server, PostgreSQL, Odoo-network, or Linux
performance qualification.

Focused verification for the fifteenth increment covers seven top-level graph
and port-wiring cases, the existing local lifecycle contracts for all six job
families, and the hosting contracts for artifact containment and idempotent job
dispatch. The restricted Windows run first hit the known disposable `.tmp`
ACL limitation in the artifact cases; all four hosting cases passed under the
established fixture permission. Eleven existing page-read and DuckDB lifetime
cases also pass. The job and service wiring changed no repository operation
and introduced no cross-request DuckDB connection. This is controlled local
evidence, not live server, PostgreSQL, Odoo-network, or Linux performance
qualification.

Focused verification for the sixteenth increment covers ten Odoo-source,
top-level composition, and fake-server architecture cases; nine request-
identity and legacy-storage compatibility cases; three canonical-ownership
cases that construct the local application; and fourteen existing Odoo capture
publication and job cases. The repository Python quality gate passes with no
Ruff or Basedpyright diagnostics. The builder opens no database and performs no
remote I/O, so the existing bounded DuckDB and Odoo adapter lifetimes remain
the operational boundary. This is controlled local evidence, not live
PostgreSQL, Odoo-network, container, Debian, or Ubuntu qualification.

The Phase 1 architecture exit audit ran 72 cases and all passed before the
Phase 2 container contract was added. The shared
workflow stylesheet is below its enforced limit at 2,424 lines after moving
concept-help and recipe-target-matching ownership to focused stylesheets. The
historical source-workflow test module is now split into four responsibility-
focused modules, each below its enforced 2,000-line limit. This closes the
independent reviewability gate.

The two local-stack recovery fixtures now inspect their source file, confirm
its table, and accept the resulting Data version before exercising local Odoo
recovery. Both focused recovery scenarios pass, and the other thirteen cases
in the same module passed in the immediately preceding run. The journey gate
remains unchanged.

Focused verification for the fourth increment covers the architecture rule,
request-scoped preparation and load job contracts, execution and navigation
services, Load browser routes, Odoo target readers, and transfer page-read
ceilings. The two stale Recipe/Production browser scenarios now follow the
run-owned work area to **Review and load** instead of treating a Recipe
application as an independent authoring workspace. The focused control-only
scenario passes. The guided setup scenario is retained as the bounded check
for the shared read-credential owner and run navigation instead of requiring a
complete monolithic suite run. Phase 1 is closed, and Phase 2 is open.

### Objective and entry conditions

Create maintainable local and server construction seams while preserving the
complete current local workflow. Phase 0 contracts are accepted.

### Work package 1.1 - Split composition without changing behavior

- Move deployment-neutral service assembly into focused builders under
  `web/composition/`.
- Move local filesystem, DuckDB registry, keyring, launch-token, local-stack,
  inline-job, and local Odoo defaults into `local_app.py`.
- Add an initially incomplete `server_app.py` that fails with a clear
  configuration error until its required adapters are supplied. Do not make it
  fall back to local security or local paths.
- Reduce `web/app.py` to public entry points and shared HTTP installation. Do
  not duplicate the route set between deployments.
- Replace broad construction helpers with responsibility-named builders. Keep
  concrete adapter construction inside composition or worker entry points.

### Work package 1.2 - Resolve actors per request

- Introduce an application-facing `AuthenticatedPrincipal` or equivalent that
  represents verified authentication without embedding role names.
- Introduce a request identity resolver. The local resolver returns the fixed
  local Organization scope and `LOCAL_ACTOR`; the server resolver will later
  use the session and PostgreSQL grants.
- Remove `actor` from the shared mutable `WebContext`. Route dependencies
  obtain the current principal and authorized actor for each request.
- Pass that actor explicitly to existing application-service methods. Do not
  store a server user in process-global or application-global state.
- Keep immutable `ActorIdentity` in approval and audit evidence.

### Work package 1.3 - Make Project visibility a port contract

- Replace the current global list flow with a query that receives Organization
  and actor scope.
- Require the repository to return only visible Project summaries. Do not load
  every Project and filter after opening customer stores.
- Make direct Project lookup return not-found or denied without revealing the
  existence, label, path, or status of an unauthorized Project.
- Apply the same parent-lineage check before opening a Data version, run,
  Workspace, artifact, protected evidence store, or job.
- Keep the local repository behavior unchanged by asserting the implicit local
  scope and returning its local Projects.

### Work package 1.4 - Narrow shared route contexts

- Continue extracting route-specific contexts similar to
  `QualityRouteContext` instead of passing the complete `WebContext` into new
  administration and server routes.
- Separate local lifecycle dependencies such as `launch_token`, local-stack
  services, and local Odoo readers from shared migration routes.
- Define explicit request-scoped dependencies for actor resolution,
  Organization scope, authorization, and server session state.

### Work package 1.5 - Preserve local database performance

- Keep synchronous local DuckDB reads inside `run_page_read` and commands
  inside `run_local_operation`.
- Measure physical connections, connection time, schema checks, elapsed time,
  and retries for changed local routes before and after extraction.
- Add focused tests with evidence-based connection ceilings. Do not make a
  server abstraction reopen the local registry once per service call.
- Keep one callback responsible for a complete synchronous repository scope;
  end it before awaiting remote I/O or background work.

### Phase 1 exit gate

- `create_local_app` passes the complete affected local integration and browser
  suites with no login or Organization UI.
- An empty `create_server_app` entry point exists and cannot start without
  server adapters.
- Shared routes no longer depend on a global actor.
- Project visibility and contained-resource authorization have contract tests
  for local allow and server deny cases.
- Architecture tests reject deployment branching and concrete server-adapter
  construction outside composition.

## Phase 2 - Deliver the Linux container baseline

### Objective and entry conditions

Produce a reproducible, non-root server package before enabling customer
accounts or migration work. Phase 1 supplies the explicit server entry point.

Implementation started on 2026-10-26 with the first Work package 2.1
increment. `deploy/Dockerfile` now defines one multi-stage image from a pinned
Python 3.12 Debian Bookworm digest. The builder installs the non-editable
application from `uv.lock`; an allowlist `.dockerignore` excludes tests,
development tools, Git metadata, local data, and unlisted files from the build
context. The runtime uses fixed user and group ID `10001`, keeps the installed
application root-owned and read-only, and names separate artifact, runtime,
and temporary paths.

The `impodo-server` entry point selects `web`, `worker`, `migrate`, or `admin`
from the same image. The web role now runs a production Uvicorn process that
serves only the lifecycle application. The migration role runs the private
PostgreSQL migration command. Worker and administration roles still refuse
startup with a configuration exit code, and no command can launch the local
browser composition. The image now builds and runs in the local Docker Desktop
rehearsal. A repeat local scan of the refreshed base found no critical
vulnerability and no fixable high finding, and local BuildKit validation
emitted provenance plus an SPDX bill of materials. The first hosted
image-quality run and clean Debian and Ubuntu host runs remain open.

The second Phase 2 increment defines one typed, immutable configuration
contract shared by every image role. It separates the public HTTPS origin,
exact trusted-proxy networks, the web bind address and port, a bounded drain
timeout, a PostgreSQL DSN *file reference*, artifact, runtime and temporary
roots, a file or external secret provider, bounded worker capacity, log level,
and an explicit Odoo egress mode. The parser
rejects unknown `IMPODO_` production settings, raw database-DSN variables,
invalid or overlapping paths, unbounded proxy trust, and secret references
below writable data roots. Odoo egress defaults to an explicit disabled mode;
enabled modes require exact destinations, and proxy credentials are accepted
only by file reference.

The same increment adds a platform-neutral mount-policy validator. The
artifact root must be observed as persistent, while every writable root must
exist, be a directory, be owned by UID and GID `10001`, deny world access, and
be writable. PostgreSQL's service-owned data volume remains a Compose and
host-rehearsal responsibility because it must not be mounted into the Impodo
process. An immutable lifecycle model admits new work only after
configuration, artifact, runtime, PostgreSQL, and schema checks pass; draining
immediately withdraws readiness. Its public liveness and readiness payloads
expose no dependency, Organization, Project, target, or secret detail.

The current production wheel builds successfully, contains the server
configuration, storage, lifecycle, active-work, Linux web, and Uvicorn modules
plus the required web templates and static assets, and publishes the
`impodo-server` console entry point. A fresh Python 3.12 installation imports
those process modules, constructs the trusted-proxy Uvicorn configuration, and
returns configuration exit code `78` when the web role has no settings.

The third Phase 2 increment adds an isolated ASGI control application. It
exposes only `GET /health/live` and `GET /health/ready`; documentation,
OpenAPI, migration, Project, and administration routes are absent. Liveness
does not inspect dependencies. Readiness returns HTTP `503` until the atomic
process lifecycle admits new work, then returns HTTP `200`, and immediately
returns `503` again when drain begins. Both responses disable caching and
contain only a generic status.

The Linux storage observer now parses `/proc/self/mountinfo`, selects the
effective containing mount, distinguishes overlay and memory-backed storage
from explicit persistent mounts, reads ownership and mode without following a
root symlink, and performs a create, flush, and remove write probe. It produces
the platform-neutral observations consumed by the existing storage policy.
Windows tests use injected filesystem facts and representative Linux mount
records; they do not claim that the observer has run inside the container.

The fourth Phase 2 increment adds one deployment-neutral runtime coordinator.
It validates the observed storage policy before it calls explicit PostgreSQL
and schema probes, and it admits work only after every proof succeeds in that
order. A bounded probe failure stops startup and reports only its stage; the
underlying adapter message is not copied into the operational error. The
coordinator withdraws readiness before it calls an explicit safe-checkpoint
waiter. If that waiter reports a controlled failure, the process remains in
the draining phase so an operator or process supervisor can retry rather than
recording a false clean stop.

A Linux composition binds the real mount observer to that coordinator without
supplying dependency or checkpoint fallbacks. A separate ASGI lifespan
runs the synchronous startup and drain operations in a worker thread, while
the liveness and readiness routes continue to read only in-memory lifecycle
state. Windows fixtures prove ordering, failure privacy, readiness withdrawal,
retryable drain behavior, and lifespan integration.

The fifth Phase 2 increment locks Psycopg 3 with its self-contained binary
distribution and adds the concrete file-secret and PostgreSQL startup
adapters. A file secret must be one direct child of the configured secret
root. The reader opens the root and child through descriptor-relative,
no-follow Linux operations; requires root ownership, service group `10001`,
private root permissions, one regular non-hard-linked file readable by its
owner and service group, at most 8 KiB, and exactly one trimmed UTF-8 line; and
closes both descriptors without retaining or logging the value. Configuration
also rejects PostgreSQL and proxy-secret references outside that direct-child
boundary.

Startup resolves the PostgreSQL DSN only when the proof runs, opens exactly
one autocommit connection, assumes the fixed `impodo_runtime` database role,
and closes it before startup completes. The session has a five-second connect
timeout, read-only default transactions, a five-second statement timeout, and
a one-second lock timeout. It proves the runtime role and reachability, then
requires exactly one `control` row in
`impodo_control.schema_contract` with generation `impodo-control-v1` and
version `1`. Missing, older, newer, duplicate, or unreadable contract state
fails closed without exposing a DSN or server error. The liveness and readiness
routes still read only in-memory lifecycle state and never open a database.

The sixth Phase 2 increment implements the private `impodo-server migrate`
command. The command uses a separate, role-specific configuration that accepts
only `IMPODO_POSTGRES_MIGRATION_DSN_FILE` and its secret-provider settings. The
runtime parser rejects that privileged setting, while the migration parser
rejects public URL, artifact, worker, Odoo, and runtime-DSN settings. A Compose
migration service can therefore mount the migration credential without making
it available to the web or worker process.

The migration connection assumes the fixed `impodo_migrator` database role
and owns one read-write transaction. Before any schema change, it acquires one
exclusive transaction-level PostgreSQL advisory lock. It bootstraps the
`impodo_control.schema_migration` ledger, verifies that every applied version,
name, and SHA-256 checksum is the exact known prefix, and applies only the
remaining immutable migrations in order. Migration `1` creates the schema
contract and grants the `impodo_runtime` role only schema usage and contract
read access. The migration row, contract, and grants commit atomically; a
statement failure rolls them all back. A repeat run performs no migration, and
an altered, missing-prefix, or future history fails closed instead of being
rewritten.

Connect, lock, statement, and idle-transaction timeouts bound the command.
Operator output contains only current and applied version numbers. Retryable
availability, incompatible history, configuration, and execution failures use
separate process exit classes without exposing a DSN or PostgreSQL error. The
implementation is fixture-tested on Windows, but has not run against a live
PostgreSQL service or inside the Linux image.

The seventh Phase 2 increment implements the process-local active-work
checkpoint and production Uvicorn binding. The runtime opens work admission
only after every startup proof passes. A pure ASGI middleware then retains one
lease until each complete non-health HTTP or WebSocket scope returns. Health
routes remain available during drain, while every new application scope
receives a generic refusal after admission closes.

The Uvicorn signal handler closes admission and withdraws readiness before it
starts transport shutdown. The same monotonic deadline bounds Uvicorn's
graceful request wait and the later active-work checkpoint, so lifespan does
not create a fresh full drain interval after a signal. Lifespan performs the
remaining checkpoint wait outside the event-loop thread. The process uses one
Uvicorn worker, disables access and server-header output, and accepts proxy
headers only from the configured exact trusted networks. The bind address,
unprivileged port, and drain timeout are typed required settings.

The production web command now runs this lifecycle-only application. It does
not mount Project, migration, administration, or shared workflow routes; the
separate fail-closed server application graph remains the boundary for that
future composition. Injected Windows tests prove configuration, signal order,
new-work refusal, full-response lease ownership, and command composition.

The eighth Phase 2 increment adds the
[reference empty-stack topology](../../deploy/compose.yaml). It builds one
Impodo image for the migration, web, and disabled worker roles. A
digest-pinned PostgreSQL service owns a separate persistent volume, while a
digest-pinned Caddy service is the only component that publishes a host port.
The proxy binds that port to loopback, terminates local internal TLS, and
reaches the health-only web process through an internal application network.
PostgreSQL, migration, web, and worker processes use a separate internal
control network, and no database port is published.

The same increment adds local-only PostgreSQL bootstrap role provisioning and
a one-shot secret-volume provisioner. The Impodo containers receive only the
DSN required by their role through root-owned, service-group-readable files.
The web role runs as UID and GID `10001` with a read-only root filesystem,
private temporary mounts, disabled Odoo egress, and one persistent artifact
volume. A helper creates an internally consistent fictional secret set under
the ignored `deploy/.local/` root without printing or replacing values. The
local [rehearsal runbook](../../deploy/README.md) covers configuration
resolution, image build, startup, repeated migration, health, drain, restart,
and volume persistence.

Nine focused container-contract tests verify the topology, digest-qualified
supporting images, role separation, loopback-only publication, disabled worker
profile, secret references, runtime restrictions, and fictional-secret
generation, plus the continuous integration image-quality contract. The
[2026-10-27 local rehearsal](91-2026-10-27-local-empty-server-container-rehearsal.md)
then resolved and built the topology on Docker Desktop for Apple silicon. It
verified live PostgreSQL role creation and migration, repeated migration
no-ops, internal TLS health, drain and recovery, container recreation, and
PostgreSQL and artifact-volume persistence.

That rehearsal corrected macOS bind-mount execution of the PostgreSQL init
script, removed role-specific configuration and an implicit writable volume
from the shared image, retained Caddy's one required file capability, and
removed a duplicated 369 MB image layer. The final Impodo image was 683 MB and
ran as UID and GID `10001` with a read-only root filesystem.

A point-in-time Docker Scout scan on 2026-10-28 found no critical
vulnerabilities and nine high-severity findings. Refreshing the pinned base to
Python 3.12.16 on Bookworm removed the fixable OpenSSL finding. The rebuilt
image passed the clean local rehearsal and the repeat scan reported eight high,
eleven medium, and forty-nine low findings, with no fixable critical or high
finding. The required-attestation and license policies still failed. The
[local rehearsal report](91-2026-10-27-local-empty-server-container-rehearsal.md)
records the sanitized findings and remaining qualification work.

The external secret-provider adapter, production database-role provisioning,
PostgreSQL service-volume qualification, vulnerability acceptance or upstream
remediation, the first hosted image-quality run, accepted PostgreSQL major,
remote certificate policy, and Debian and Ubuntu rehearsals remain open.

### Recommended local container rehearsal

Run a loopback-only container rehearsal before the first remote Debian or
Ubuntu deployment. This rehearsal should use the exact pinned production image
and the reference Compose topology from Work package 2.2. It must not use a
separate development image or enable behavior that the remote stack will not
have.

The local rehearsal should:

- bind the reverse proxy only to the host loopback interface and publish no
  direct web, worker, or PostgreSQL listener;
- use fictional secrets, an empty PostgreSQL database, an empty artifact
  root, and disabled Odoo egress;
- build and inspect the image, run the migration command twice to prove a safe
  no-op replay, and start the lifecycle-only web role;
- verify liveness, readiness failure and recovery, drain behavior, non-root
  execution, the read-only application tree, and private writable roots;
- restart and recreate the containers while proving that the PostgreSQL and
  artifact volumes persist; and
- record the image digest, effective Compose configuration, schema version,
  exposed ports, and sanitized results so the later host rehearsal can compare
  the same facts.

Docker Desktop or another local container engine runs Linux through a managed
virtual machine. A successful local rehearsal catches image, configuration,
secret-mount, migration, health, network, and persistence defects early, but
it does not prove Debian or Ubuntu firewall, filesystem, service-management,
or attached-storage behavior. Work package 2.5 and the Phase 2 exit gate still
require both supported remote host rehearsals.

Current status: the Compose topology, proxy policy, fictional-secret helper,
PostgreSQL role bootstrap, runbook, static contract tests, and one complete
local Docker Desktop rehearsal are implemented. One point-in-time local image
scan and its refreshed-base repeat are recorded. The Linux continuous
integration workflow now contains a credential-free image-quality job. It
builds the pinned image, rejects fixable critical or high vulnerabilities with
a digest-pinned scanner, and retains maximum-mode provenance and an SPDX
software bill of materials. The first hosted run, vulnerability acceptance or
upstream remediation, and the supported Debian and Ubuntu host rehearsals
remain open.

### Work package 2.1 - Build the production image

- Add `deploy/Dockerfile` as a multi-stage build. Pin the runtime base by
  immutable version or digest and install from the repository dependency lock.
- Build wheels and static assets in the builder stage. Exclude compilers,
  caches, tests, development tools, Git metadata, local data, and secrets from
  the runtime stage.
- Create a fixed non-root UID and GID. Make the application tree read-only and
  expose only explicit runtime, temporary, and data paths as writable.
- Use one image with separate web, worker, migration, and administrative
  commands. Do not build behaviorally different application images.
- Generate image provenance and a software bill of materials, and run the
  selected vulnerability scanner in continuous integration.

Current status: the existing Linux workflow now implements this image-quality
lane without publishing the image or requiring a registry credential. Local
validation reproduced its vulnerability threshold and BuildKit evidence
export. A hosted workflow run remains required before the Phase 2 continuous
integration exit criterion is complete.

### Work package 2.2 - Define the Compose topology

Add a reference `deploy/compose.yaml` containing:

- one TLS reverse proxy;
- one Impodo web service;
- one PostgreSQL service on a private network;
- one disabled-by-default or idle worker service until Phase 6;
- a one-shot schema migration command; and
- named or bind-mounted persistent roots for PostgreSQL and Impodo artifacts.

Only the reverse proxy publishes an inbound port. The web and worker services
reach PostgreSQL through the private Docker network. PostgreSQL requires a
health check before application readiness. The worker does not share a writable
temporary directory with the web process.

Current status: the reference topology is implemented for the empty local
rehearsal. It uses separate edge, application, and control networks; publishes
only the loopback TLS proxy; waits for PostgreSQL health and successful schema
migration; and keeps the incomplete worker behind a disabled profile. Docker
Compose resolved and ran the file successfully during the 2026-10-27 local
rehearsal. Clean Debian and Ubuntu execution remains open.

### Work package 2.3 - Validate configuration and mounts

- Define a typed server configuration that separates public URL, trusted proxy,
  database DSN reference, artifact root, secret-provider configuration,
  worker limits, and Odoo network policy.
- Refuse unknown production settings and refuse required secrets supplied as
  command-line arguments or committed configuration.
- Refuse startup when the PostgreSQL or artifact mount is absent, owned by the
  wrong UID or GID, world-readable, read-only when writes are required, or
  resolved to transient container storage.
- Keep development, container rehearsal, on-premises, and cloud examples
  separate. An example must contain only fictional values and secret names.

Current status: separate typed runtime and migration configuration contracts
are implemented. Each rejects settings owned by the other process role, so the
web process cannot accept the privileged migration DSN reference. The
platform-neutral Impodo-root policy and Linux mount observer have
fixture-tested mount parsing, ownership, mode, and write-probe behavior. The
Linux file-secret adapter validates a root-owned, service-group-readable,
direct-child secret through no-follow descriptors. The first fictional local
rehearsal example and a no-overwrite secret generator are implemented. The
local Docker Desktop run verified the expected secret ownership, separate
role mounts, PostgreSQL named volume, and artifact named volume. Supported-host
volume qualification, external secret-provider support, production role
provisioning, and separate on-premises and cloud examples remain open.

### Work package 2.4 - Add lifecycle endpoints and commands

- A liveness endpoint reports only that the process loop is responsive.
- A readiness endpoint verifies configuration, required mounts, schema
  compatibility, PostgreSQL reachability, and the ability to accept new work.
  It exposes no Organization, Project, target, or secret detail.
- A drain command marks the application unavailable for new commands and waits
  for safe checkpoints before controlled shutdown.
- A migration command acquires an exclusive advisory lock and applies each
  forward migration once.
- Administrative commands bootstrap later phases without exposing a public
  setup route.

Current status: the atomic lifecycle owner, readiness blockers, generic public
payloads, controlled transition rules, and isolated ASGI health routes are
implemented. The runtime coordinator orders storage, PostgreSQL, and schema
proofs before accepting work. A process-owned gate admits each non-health ASGI
scope and retains its lease through the full response. Signal handling closes
that gate and withdraws readiness before Uvicorn drains transports. Lifespan
then waits outside the event-loop thread for any remaining lease, using the
deadline established when drain began. The health routes perform no DuckDB or
PostgreSQL work. A concrete one-session Psycopg inspector proves bounded
runtime-role reachability and exact schema generation before readiness. The
private migration command applies a checksum-validated forward suffix in one
transaction under an exclusive advisory lock. Later control-plane migrations,
the shared application graph, and administrative implementations remain open.

### Local trial continuation gate

When no remote server is allocated, maintainers may continue into bounded
Phase 3 persistence work against the fictional local Compose stack after all
of these checks pass:

- Compose resolves without an unknown setting or secret value in tracked
  configuration.
- The migration command is idempotent on two consecutive replays.
- Liveness and readiness pass through the loopback-only TLS proxy.
- The application runs as UID and GID `10001`, and its application tree is
  read-only.
- PostgreSQL, the web process, and the disabled worker expose no host port.
- Odoo egress remains disabled, the worker remains stopped, and no customer
  data enters the trial volumes.

The local checkpoint passed again on 2026-10-28. It authorizes disposable
local schema, repository, and recovery trials only. It does not qualify a
Linux host, production secrets, public certificates, backups, remote Odoo
access, or a shared multi-user Impodo server.

### Work package 2.5 - Qualify Debian and Ubuntu hosts

This work package is deferred while the project has no remote trial host. Do
not substitute the developer laptop for its firewall, service-management,
storage, certificate, or restart evidence.

- Select one primary tested host release and one secondary supported release
  from the Debian and Ubuntu families.
- Verify installation prerequisites, Docker Engine and Compose behavior,
  firewall defaults, filesystem ownership, time synchronization, restart
  policy, log rotation, and attached block storage.
- Run the same image on both host families. Host-specific behavior belongs in
  installation instructions, not application branches.
- Restart and recreate containers while proving that PostgreSQL and artifact
  roots persist and that the application never writes customer data into an
  image layer.

### Phase 2 supported-host exit gate

- Continuous integration builds and scans the pinned image.
- A clean supported Debian host and a clean supported Ubuntu host can start the
  empty stack, reach HTTPS health, drain, restart, and retain both persistent
  roots.
- Direct web, worker, and PostgreSQL listeners are unreachable from the public
  interface.
- No migration workflow is enabled yet, and the rehearsal contacts no Odoo
  target.

## Phase 3 - Implement the PostgreSQL control plane

### Objective and entry conditions

Make shared tenancy, visibility, sessions, catalogues, idempotency, and worker
coordination durable without moving migration rows or detailed evidence into
PostgreSQL. The fact-ownership inventory and Linux database service are ready.

For the current local trial, the entry condition is the local trial
continuation gate. Trial work uses fictional data and the loopback-only Compose
stack. A supported server release still requires the complete Phase 2
supported-host exit gate, including Debian and Ubuntu qualification.

### Work package 3.1 - Establish PostgreSQL persistence conventions

- Select and pin the maintained PostgreSQL driver, connection pool, and schema
  migration mechanism used by the repository.
- Put SQL and row decoding inside `adapters/postgresql`. Application ports
  expose domain values, not cursors, ORM sessions, SQL expressions, or driver
  exceptions.
- Use UTC timestamps, bounded text, explicit enum constraints, foreign keys,
  unique semantic keys, and revision columns. Store identifiers in one
  canonical representation.
- Require `organization_id` on every customer-owned root and on every index
  that resolves customer-owned state. A child row may derive it only through a
  database-enforced foreign key path that cannot cross Organizations.
- Translate serialization, uniqueness, deadlock, timeout, and unavailable
  conditions into bounded application-facing failures. Do not expose SQL or
  row values in public errors.

Current status: Psycopg 3 is selected and locked for the trial. The migration
and startup paths use short-lived, bounded sessions and translate driver
failures without exposing connection details. The connection-pool decision is
deferred until the first concurrent catalogue repository is implemented; no
unused process-wide pool is introduced for the server runtime.

### Work package 3.2 - Create the initial control-plane schema

The initial schema sequence creates these responsibility groups. Exact table
names may change, but their ownership may not collapse into one generic
metadata table.

| Group | Required records and constraints |
| --- | --- |
| Organization | Organization identity, display name, status, policy revision, creation time, suspension, and deletion state. |
| User identity | Customer user, exactly one active `organization_id`, platform-operator separation, normalized login identifier, status, and audit identity. |
| Authentication | Password identity metadata, later external issuer and subject, activation and reset token digests, server sessions, revocation, and login security events. Passwords and raw tokens are never stored. |
| Authorization | Standard and custom role definitions, immutable role revisions, role capabilities, assignments, Project grants, direct exceptional grants, expiry, reason, and access-review state. |
| Project catalogue | Project identity, owning Organization, lifecycle summary, data classification, retention policy, optimistic revision, current bounded status, artifact-root key, and deletion state. |
| Contained catalogue | Data version, Migration run, Workspace, Recipe, and cutover identities with parent ownership, bounded lifecycle, current receipt hash, and storage pointers. Detailed evidence remains outside PostgreSQL. |
| Operations | Idempotent operation request, semantic input hash, actor and policy revision, state, data-plane receipt, projection receipt, and recovery disposition. |
| Jobs | Job request, job kind, Organization and Project scope, immutable manifest key and hash, current state, attempts, progress projection, cancellation request, and terminal result. |
| Leases | Workspace or action resource, owner attempt, fencing token, acquisition, heartbeat, expiry, and release reason. |
| Administration | Non-sensitive security event, access-change event, connection-profile metadata, retention schedule, deletion request, deletion receipt, and backup checkpoint identity. |

Indexes must support Organization-scoped Project lists, Project-scoped job
lists, fair-scheduler claims, active lease checks, session lookup, expiring
grants, retention scans, and idempotency lookup without full-table scans.

Current status: immutable migrations `2` and `3` implement the first local
trial slices. Migration `2` creates Organization and Project catalogue tables.
Migration `3` adds Organization-scoped operation requests, immutable
data-plane and projection receipts, and current Workspace leases with monotonic
fencing tokens. Every Project and coordination key or index begins with
`organization_id`. The runtime role can select, insert, and update the mutable
control records, but it cannot delete them or update immutable receipt rows.
User, authentication, authorization persistence, contained catalogue, jobs,
and administration groups remain unimplemented.

### Work package 3.3 - Implement Project catalogue repositories

- Implement PostgreSQL adapters for Project create, get, visible list, rename,
  lifecycle summary, optimistic revision, and deletion-state transition.
- Keep the full migration Project evidence in its data-plane stores. The
  PostgreSQL row holds the server tenancy and bounded catalogue facts accepted
  by the Phase 0 inventory.
- Scope every query by `organization_id` in addition to object identity. Do not
  rely on a prior route check as the only tenancy filter.
- Add optional PostgreSQL row-level security only as defense in depth after the
  application-scoped repository queries pass. Database policy must not replace
  application authorization or make maintenance jobs silently omnipotent.
- Add repository contract tests that execute against a disposable PostgreSQL
  database and compare applicable semantics with the local repository.

Current local trial status: the bounded Project catalogue contract and Psycopg
adapter implement create, tenant-safe get, visible list, rename, lifecycle
projection, optimistic revision, and deletion-state transition. Visible lists
filter by verified Project visibility and bounded lifecycle states inside
PostgreSQL. They use `updated_at DESC, project_id ASC` keyset ordering, page
sizes from 1 through 100, and versioned `pc1.<base64url>` cursors. Every update
uses `organization_id`, `project_id`, and the expected revision in one SQL
predicate. Deletion changes state and retains the row; the adapter exposes no
hard-delete operation.

Portable value and fake-session tests protect the contract without requiring
PostgreSQL. The Linux-owned suite runs the same adapter against a disposable
PostgreSQL service and proves Organization isolation, stale-revision refusal,
visible pagination, lifecycle projection, and retained soft-deletion rows. It
does not enter the cross-platform repository gate. A deployment-neutral
application service now verifies the request Organization, principal,
capability, expiry, and Project visibility before repository access. Denied
Project reads and mutations return the same opaque not-found failure and open
no repository session; each accepted command makes one bounded repository
call. Browser composition remains deliberately absent until server identity
and request-scope construction exist.

The Linux-owned suite also compares the applicable local DuckDB and hosted
PostgreSQL semantics directly: both start at revision one, increment an
accepted rename atomically, and reject a stale expected revision. A controlled
twelve-read concurrent fixture opened and closed twelve independent Psycopg
sessions. This is structural lifetime evidence, not a latency or capacity
claim. The adapter therefore continues to use one short-lived transaction per
call; pooling remains deferred until measured traffic justifies it. Optional
row-level security also remains deferred because the application and SQL
tenancy predicates now pass and no maintenance policy has yet been designed.
This bounded local-trial implementation completes work package 3.3; hosted CI,
browser/account integration, and supported-host qualification remain separate
gates.

### Work package 3.4 - Implement the cross-store operation protocol

For every command that changes control and data-plane state:

1. PostgreSQL creates or returns the idempotent operation for the same semantic
   request.
2. PostgreSQL authorizes and records the exact Organization, Project, actor,
   grant revision, policy revision, and input hash.
3. The operation acquires the required resource lease and fencing token.
4. The data-plane command stages artifacts, commits DuckDB evidence, and writes
   a receipt containing the operation identity, input hash, fencing token,
   output hashes, and storage keys.
5. The server verifies the receipt and published artifacts before updating the
   PostgreSQL projection and terminal operation state.
6. Recovery either finalizes the proven receipt, records `INTERRUPTED`, or
   requires manual reconciliation. It never repeats target I/O from an
   ambiguous state.

Use failure injection at every boundary: before lease, after lease, after
artifact staging, after DuckDB commit, after artifact publication, before
projection update, and after projection update. An identical replay returns
the same result; changed meaning under the same idempotency key fails closed.

Current local trial status: three bounded coordination increments are
implemented.
The PostgreSQL operation journal reserves one semantic request per
Organization and idempotency key, returns the original request for an exact
replay, and rejects changed meaning. Data-plane and projection receipts are
append-only, bounded to hashes and storage-neutral artifact identities, and
must match the exact Organization, Project, operation, and prior receipt. The
Workspace lease repository acquires, heartbeats, releases, and reacquires one
current row with an increasing fencing token. Each method uses one short-lived
transaction and no retained connection pool.

The deployment-neutral coordinator now distinguishes a new reservation from
an idempotent replay. A replay reads the durable receipt and never repeats the
data-plane command. The coordinator verifies the exact operation, lease,
artifact identities, and projection receipt before it records success.
Recovery finalizes proven data-plane evidence, records `INTERRUPTED` when no
commit receipt exists, or records `MANUAL_REVIEW` when receipt, artifact, or
projection evidence conflicts.

Portable tests cover all seven named failure boundaries and prove that each
boundary has a deterministic recovery result without a second data-plane
execution. A Linux-owned suite against disposable PostgreSQL proves
two-Organization isolation, immutable replay, substituted-receipt rejection,
durable interruption and manual-review state, release and reacquisition,
stale-owner fencing, and a two-contender race with one winner.

The third increment adds the first concrete data-plane command. A verified
file-scenario Preparation stages canonical prepared rows into one Parquet
artifact, commits an immutable receipt and monotonic Workspace fence in
workspace-engine schema version `17`, publishes the exact content-addressed
artifact, and emits the three data-plane checkpoints. The command uses one
bounded physical DuckDB connection for a new execution and retains no
process-wide or cross-request connection cache. Its artifact verifier accepts
only a Workspace-owned prepared snapshot and rechecks both SHA-256 and exact
byte size. Its deterministic projection updater derives only bounded receipt
facts and binds projection revision `1` to the exact data-plane receipt hash.

The Linux-owned concrete suite now injects a failure at all seven boundaries
against disposable PostgreSQL. Failures through artifact staging recover as
`INTERRUPTED`; a receipt committed without published bytes recovers as
`MANUAL_REVIEW` with `ARTIFACT_MISMATCH`; and failures after publication
complete from the durable receipt without a second Preparation execution. The
same suite runs the repository's fictional PLW manufacturing scenario through
the complete DuckDB/artifact/PostgreSQL path, verifies the four dataset counts
and all `37,253` prepared rows, then proves an identical replay returns the
same receipts without repeating the data-plane command.

This closes the concrete local-trial evidence requested by work package 3.4.
Production Preparation job and browser composition remain deferred until the
durable server job protocol and server request identity are implemented. The
current concrete command is deliberately limited to reviewed file scenarios;
other command families still need their own data-plane adapters before remote
deployment.

### Work package 3.5 - Make projections rebuildable

- Define a projection updater that receives only verified receipt facts.
- Record the receipt hash and projection version so a stale or substituted
  projection cannot become current.
- Provide an administrative reconciliation command that scans bounded
  PostgreSQL pointers and verifies their data-plane receipts without reading
  every source or prepared row.
- Rebuild a missing projection from receipts where the contract permits it.
  Do not reconstruct approvals or unknown Odoo outcomes from display status.

Current local trial status: this work package is implemented behind the
deployment-neutral administration boundary. PostgreSQL control schema version
`4` adds one `projection_current` pointer for each Organization, Project, and
projection kind. A projection can advance that pointer only when its immutable
data-commit time and operation identity sort after the current receipt. A late
older projection is retained as `SUPERSEDED` evidence and cannot replace the
newer current pointer. The forward migration backfills each pointer from the
newest exact projection receipt.

`PsycopgOperationJournal.list_reconciliation_page` reads at most `100`
operations in one joined, keyset-ordered PostgreSQL query. Its URL-safe cursor
is bound to the Organization and Project. The adapter reconstructs each
data-plane receipt, verifies its recorded semantic hash, verifies any
projection receipt and current pointer, and returns only bounded dispositions:
`AWAITING_DATA_COMMIT`, `MISSING_PROJECTION`, `CURRENT`, `SUPERSEDED`, or
`NOT_REBUILDABLE`. It does not open DuckDB or read source or prepared rows.

`ProjectionReconciliationService` requires a valid `AUDIT_VIEW` authorization
and Project visibility before scanning. Rebuild requires the exact
`PROJECT_ADMIN` capability and accepts only a `DATA_COMMITTED` operation with
missing projection evidence. It delegates to the existing recovery
coordinator, which re-verifies immutable artifacts and projects the receipt
without executing the data-plane command again. Projected, interrupted, and
manual-review operations are not rebuilt, so the command cannot reconstruct
an approval or an unknown Odoo outcome from display status.

The portable contract suite proves authorization-before-storage, opaque
cross-scope lookup, page bounds, cursor scoping, hash reconstruction, and the
stale-pointer race. The Linux-owned disposable PostgreSQL suite proves the
version-3 receipt backfill, current-versus-superseded ordering, and
Organization isolation. An operator-facing server administration process is
still deferred until the server identity and administration composition can
construct the required authorization receipt; the current local trial does
not expose an unauthenticated maintenance command.

### Work package 3.6 - Protect connection and transaction budgets

- Use one bounded PostgreSQL transaction per control-plane command and explicit
  read scopes for pages.
- Avoid holding a PostgreSQL transaction while parsing files, transforming
  data, waiting for a worker, contacting Odoo, or opening a long DuckDB
  transaction.
- Record query counts, pool waits, transaction duration, and retries for
  catalogue and administration pages. Apply the same metric contract to job
  pages when the durable PostgreSQL job adapter is added in Phase 6.
- Add call-count tests for Organization-scoped list pages. Add the job-claim
  call-count case with the durable claim operation in Phase 6. A Project list
  must not query roles or receipts once per Project.

The implemented PostgreSQL repositories now emit one privacy-safe
`PostgreSQLTransactionMetric` after every opened transaction. The metric uses
an allowlisted repository-operation name and contains only the connection and
query counts, pool wait, transaction duration, retry count, and bounded
outcome. It contains no Organization, Project, operation identity, receipt,
row, query text, parameter, or DSN values. An optional observer is isolated
from the governed repository result, so a diagnostic failure cannot replace a
committed result or convert a rejected command into success.

The Project catalogue and projection-reconciliation list contracts each have
an explicit budget of one short-lived connection, one transaction, and one SQL
query per non-empty page. They continue to use the existing five-second
statement and idle-in-transaction limits. The current adapters do not use a
pool or perform hidden retries, so both reported values are zero. Portable
call-count tests prove that returning several Projects or reconciliation items
does not add per-item role, receipt, or Project queries. The Linux-owned suite
also exercises the measured adapters against disposable PostgreSQL.

This measurement boundary opens only inside one repository call and closes
before control returns to application orchestration. It therefore cannot span
file parsing, DuckDB work, worker waits, Odoo calls, or artifact verification.
No DuckDB lifetime or cache changed in this work package. Durable job pages and
claims do not yet have an executable PostgreSQL repository; their matching
metric and claim call-count test remain an explicit Phase 6 acceptance
criterion rather than a placeholder schema or a false passing result.

### Phase 3 exit gate

- Fresh and upgrade migrations are deterministic and safe under one exclusive
  migration lock.
- Two fictional Organizations cannot observe or mutate each other's catalogue,
  operation, lease, or receipt records through repositories or services. Apply
  the same isolation gate to durable job records when Phase 6 adds their
  PostgreSQL repository.
- Cross-store fault-injection tests prove deterministic recovery at every
  commit boundary.
- PostgreSQL contains no source rows, prepared rows, protected target values,
  credentials, or full Recipe payloads.
- Local repository and performance tests remain green.

Current local-trial status: Phase 3 passes this exit gate for the implemented
control-plane records. The Linux-owned migration suite runs two migrators
against one empty database and proves that the transaction-level advisory lock
allows one fresh application and one no-op replay. It also constructs every
historical schema prefix, upgrades each prefix to version `4`, and proves that
another replay applies no migration. The disposable database is removed after
the local rehearsal.

The portable Phase 3 suite runs `60` focused startup, migration, catalogue,
coordination, cross-store, authorization, and reconciliation tests. The
Linux-owned suite runs `16` live PostgreSQL tests. Those live tests prove
two-Organization catalogue, operation, receipt, projection, and lease
isolation, and they inject failure at all seven cross-store commit boundaries.
The complete fictional PLW path still produces `37,253` prepared rows and an
identical replay without a second data-plane execution.

The checked migration definitions and their contract tests allow only bounded
catalogue facts, hashes, storage keys, receipt metadata, and artifact identity
arrays in PostgreSQL. They reject source-row, protected-target-value, full
Recipe-payload, credential, and hard-delete authority from the runtime schema.
The default performance qualification suite runs `67` tests successfully.
Eight larger scale qualifications remain explicitly opt-in and were omitted
from this local exit audit; they are not reclassified as passing evidence.

This is a local-trial Phase 3 checkpoint. It does not satisfy the deferred
Debian and Ubuntu host qualification, remote backup and restore, Internet
security acceptance, or future durable-job isolation gates. Phase 4 may now
implement built-in identity and administration against the bounded PostgreSQL
foundation while the supported-host release gate remains open.

## Phase 4 - Add built-in accounts and Organization administration

### Objective and entry conditions

Let an administrator provision and operate a shared server without Microsoft,
Google, LDAP, SAML, or another external identity provider. PostgreSQL tenancy
and server sessions are available.

### Work package 4.1 - Bootstrap the first operator

- Add a non-HTTP administrative command that creates the first Platform
  Administrator from an interactive secret or one-time bootstrap file.
- Refuse bootstrap after an active Platform Administrator exists unless an
  explicit audited recovery procedure authorizes another.
- Ship no default username, password, activation URL, or Organization.
- Record only a bounded bootstrap security event. Never log the password,
  token, password hash, or complete activation link.

Current local-trial status: implemented and verified on 2026-10-29. Immutable
migration `5` creates separate Platform Administrator, password-identity, and
bounded platform-security-event tables. `impodo-server admin bootstrap`
accepts an interactive password or a protected one-time file, normalizes the
login email, derives a salted scrypt hash with explicit policy metadata, and
commits the active operator and its event in one short-lived PostgreSQL
transaction. A transaction-level advisory lock makes simultaneous empty-state
attempts deterministic. Once one active Platform Administrator exists, the
ordinary command refuses with no additional row or event.

The opt-in local Compose service completed once with fictional data and its
immediate replay refused as expected. The rebuilt health-only web and proxy
containers remained healthy on schema version `5`. This checkpoint provides
no login route, session, Organization, browser administration, or recovery
override; those remain owned by work packages 4.2 through 4.6. It is local
rehearsal evidence, not remote Debian or Ubuntu qualification.

### Work package 4.2 - Implement Impodo-managed authentication

- Store a current slow password hash with algorithm and parameter metadata so
  a successful login can rehash when policy changes.
- Normalize login identifiers consistently and define their uniqueness. The
  first server uses a globally unique login email because one account belongs
  to one Organization.
- Store activation and reset token digests, one-use state, purpose, expiry, and
  revocation. Provide the generic one-time disclosure contract here; bind and
  show an activation link only when work packages 4.4 and 4.5 create an
  eligible customer account and its governed administration action.
- Apply generic login and reset responses, account and source throttles,
  bounded lockout, and security-event recording without confirming whether an
  account exists.
- Make multi-factor authentication policy an explicit release decision. An
  Internet-accessible deployment cannot pass security acceptance until the
  accepted privileged-account policy is implemented.

Current local-trial status: complete at the authentication and token-policy
boundary, and verified on 2026-10-29. Immutable migration `6` adds a globally
unique login-identity index, password revisions, persistent account and source
throttle rows, and digest-only authentication event fields.
The platform authentication service normalizes the email, gives unknown,
invalid, deactivated, locked, and wrong-password attempts the same rejection,
and still performs one bounded scrypt verification for an unknown account. A
successful verification compare-and-swaps a stale password hash to the current
policy and records the rehash only when that revision update succeeds.

Credential and lock lookup uses one short-lived PostgreSQL transaction.
Scrypt runs after that transaction closes. Outcome persistence then uses a
second short-lived transaction to append the security event, update both
throttle scopes, and apply an optional rehash. The live Linux-owned test proves
fresh and prefix migration to version `6`, concurrent first-operator bootstrap,
five-attempt lockout, source and account scope enforcement, and successful
rehash against disposable PostgreSQL. The exact local server image was then
rebuilt, the persistent rehearsal database advanced to `impodo-control-v1:6`,
and the PostgreSQL, web, and loopback proxy containers all returned healthy.

Immutable migration `7` adds a generic principal ID, digest-only activation and
password-reset token persistence, purpose and expiry, irreversible issued,
consumed, and revoked states, issued password and security revisions, a
one-outstanding-token constraint, separate reset account and source throttles,
and column-level runtime grants with no token delete or digest rewrite. The raw
selector-plus-secret token contains 32 random bytes, is purpose-bound before
SHA-256 storage, stays out of representations and events, and is returned only
after an authorized issuance transaction commits. Low-entropy account and
source hints use deployment-keyed, scope-separated HMAC-SHA-256.

The public reset request returns one constant result for known, unknown,
invalid, inactive, and throttled identities. For an eligible Platform
Administrator, reset consumption locks the subject and token, compares the
issued password and security revisions, updates the slow password hash,
increments both revisions, consumes the token, revokes siblings, and appends
the event in one short-lived transaction. The replacement scrypt hash is
derived before that transaction opens. Unit and repository contract tests
cover opaque responses, purpose binding, replacement issuance, stale-revision
rollback, and secret-safe representations. The exact local image applied the
version `6` to `7` suffix once, replayed it with no changes, and restarted the
web and loopback proxy healthy on `impodo-control-v1:7`.

The privileged-account decision is now explicit: password-only access remains
permitted for this loopback-only trial, but every Platform or Organization
Administrator must have WebAuthn plus either a second WebAuthn authenticator or
unused hashed offline recovery codes before an Internet-accessible deployment
can pass security acceptance. TOTP alone does not satisfy that release gate.
WebAuthn enrollment and enforcement remain implementation work, so the gate is
currently blocked.

The authentication-foundation checkpoint did not expose an HTTP route. Work
package 4.4 binds activation issuance and consumption atomically to a pending
customer account. Work package 4.5 now presents the authorized link once and
provides the browser route that consumes it. Treating the already-active Platform
Administrator or a deactivated account as pending activation remains
incorrect. The server therefore remains suitable only for the local trial.

### Work package 4.3 - Implement server sessions

- Create an opaque server-side session and send only a rotated session handle
  in a secure, HTTP-only, same-site cookie.
- Bind the session to the user, the one Organization, authentication method,
  creation and last-use times, absolute and idle expiry, and security revision.
- Rotate the handle at login and privilege elevation. Revoke all applicable
  sessions after password reset, account suspension, Organization suspension,
  or a security-policy revision that requires reauthentication.
- Keep CSRF, same-origin, host, framing, cache, and forwarded-header controls.
  Trust proxy headers only from configured proxy addresses.
- Provide one bounded current-session view and an administrator session-revoke
  command.

Current local-trial status: implemented and verified on 2026-10-29 at the
session-foundation boundary. Immutable migration `8` adds digest-only opaque
server sessions with principal and optional Organization binding,
authentication method, security revision, creation and last-use times, idle
and absolute expiry, bounded WebAuthn elevation, revocation reason, and
optimistic revision. The runtime role can select, insert, and update only the
named lifecycle columns; it cannot delete a session or rewrite its subject,
creation time, or security revision.

The application service creates a fresh selector and independent CSRF secret
without accepting an anonymous browser identifier, resolves a bounded current
view, rotates both secrets only after WebAuthn elevation, and exposes an exact
administrator revocation command. PostgreSQL stores only domain-separated
SHA-256 digests. Each operation owns one short-lived connection and
transaction; there is no pool or process-wide database connection. Password
reset revokes the subject's active sessions in the same transaction as the
password, security revision, token, and reset event changes.

The Linux web composition now applies exact external-host and unsafe-request
Origin checks, no-store, framing, content-type, referrer, CSP, and permissions
headers. The handle cookie helper enforces the `__Host-` prefix, `Secure`,
`HttpOnly`, `SameSite=Lax`, `/` path, and no Domain attribute. Uvicorn continues
to accept forwarded headers only from the configured proxy CIDRs. Health
routes remain available to the internal container probe without weakening
the external browser policy.

The focused suite covers generation and proof, secret-safe representations,
creation, CSRF validation, idle and security-revision revocation, WebAuthn
rotation, stale administrative revocation, password-reset revocation, schema
grants, cookie attributes, host and Origin rejection, and browser response
headers. The exact image applied the version `7` to `8` suffix once and
replayed it with no changes. A sanitized live lifecycle then proved create,
CSRF-bound resolve, rotation, rejection of the old handle, revocation,
digest-only storage, and runtime-role delete denial before all containers
returned healthy.

The session-foundation checkpoint did not add HTTP routes. Work package 4.5
now connects login, logout, recovery, and session administration for all three
principal kinds to the persisted identity and session services.
Therefore the server is still a loopback-only trial and the Internet security
gate remains blocked on WebAuthn enrollment and route enforcement.

### Work package 4.4 - Implement Organization and user administration services

Create focused application services and PostgreSQL repositories for:

- Organization create, rename, suspend, reactivate, and policy revision;
- Organization Administrator creation and replacement;
- customer user create, activate, suspend, reactivate, and deactivate;
- one-time activation and reset issuance;
- exactly one active Organization membership per customer user;
- user and Organization security-event queries; and
- session listing and revocation.

A Platform Administrator can provision or suspend an Organization but receives
no Project list, artifact, protected evidence, target credential, or migration
role automatically. An Organization Administrator manages only their own
Organization.

Current local-trial status: implemented and verified on 2026-10-29 at the
administration-service boundary. Immutable migration `9` adds scalar
Organization membership for customer accounts, separate customer password
identities, globally unique login identities, one administrator assignment per
Organization, and tenant-scoped administration events. Immutable migration
`10` binds the assignment and login identity to the same customer account,
Organization, and principal kind. The runtime role cannot hard-delete these
facts or rewrite an assignment outside its named lifecycle columns.

`IdentityAdministrationService` now creates, renames, suspends, reactivates,
and advances policy for an Organization. A Platform Administrator can assign
or replace the Organization Administrator, but cannot use the customer-user,
event, or session query contracts. An active Organization Administrator can
create and manage ordinary users only inside their own Organization. Each
mutation carries the Organization, subject, and expected optimistic revision
into the same SQL predicate. Suspension, deactivation, password reset,
administrator replacement, Organization suspension, and policy revision
revoke the applicable active sessions in the same transaction.

Customer creation stores a pending account and purpose-bound activation-token
digest atomically, then returns the raw token only after commit. Activation
stores the first slow password hash and consumes the one-use token in the same
transaction. Authorized reset issuance replaces an outstanding reset token.
Reset consumption compares the issued password and security revisions,
updates the password, advances the security revision, consumes and revokes
tokens, and revokes sessions atomically. Organization-wide and user-filtered
security-event queries and Organization session queries use bounded,
deterministic keyset pagination.

Every repository call opens and closes one short-lived runtime-role
transaction. There is no connection pool or cross-request PostgreSQL or
DuckDB connection cache. The focused source and repository suite covers
tenant authority, token secrecy, optimistic predicates, assignment, and
migration invariants. A Linux-only disposable PostgreSQL test then completed
Organization creation, administrator assignment and activation, ordinary-user
creation and activation, session listing and revocation, reset, suspension,
event queries, runtime delete denial, and database rejection of a cross-kind
administrator assignment. The persistent local image advanced from schema
version `8` through `9` and `10`; both immediate replays applied no migration.

The service-foundation checkpoint added no browser or delivery channel. Work
package 4.5 now supplies the administration dashboard and the browser
activation, reset, and session actions with manual private delivery of links.
Remote Debian and Ubuntu
qualification and the privileged WebAuthn release gate also remain open.

### Work package 4.5 - Build the administration dashboard

Add server-only routes, presenters, and templates for:

- the Platform Administrator's Organization list and creation form;
- the Organization Administrator's user list and user detail;
- activation, suspension, reactivation, reset, and session revocation;
- role and Project-access summaries that Phase 5 will make editable; and
- security and access-change history with bounded pagination.

The dashboard has no Organization switcher. A customer session is permanently
bound to its one Organization. Destructive actions name the affected user or
Organization, explain the effect, require a fresh CSRF-protected confirmation,
and preserve audit identity.

Current local-trial status: the Platform and Organization Administrator read
slices are implemented and verified on 2026-10-29. `POST /login` applies one
generic authentication response and protects both the account hint and
proxy-resolved source with deployment-keyed, scope-separated HMAC-SHA-256.
Successful login creates a fresh opaque session permanently bound to its
administrator identity and, for a customer administrator, its exact
Organization. The browser receives an HTTP-only `__Host-` handle cookie and a
separate `__Host-` CSRF cookie. Host and unsafe-request Origin checks remain
mandatory.

`GET /admin/organizations` resolves the current Platform Administrator session
and returns one bounded Organization page ordered by display name and stable
identity. `POST /admin/organizations` validates the session-bound CSRF token
before it creates the Organization. `POST /logout` revokes the exact touched
session under its current optimistic revision and clears both cookies. The
page states that a Platform Administrator receives no customer Project or
artifact access.

`GET /admin/users` and `GET /admin/users/{user_id}` require an active,
exactly assigned Organization Administrator. The Organization comes only from
the resolved session; neither route accepts a tenant selector or Organization
identifier from the browser. List ordering is stable by display name and user
identity, and a continuation carries only an opaque user identity that is
resolved again under the same Organization predicate. Detail misses and
cross-Organization identities share the same bounded `404`. A valid session on
the wrong administrator surface receives `403` without being revoked.

PostgreSQL schema version `11` binds every Organization Administrator session
to the exact customer membership and principal kind, applies the same tenant
binding to administration-event subjects, and stores customer authentication
and session events in a bounded digest-only server-security table. Session
resolution fails closed after assignment replacement, Organization suspension,
or a security-revision change.

The async routes put each complete synchronous service sequence in one
thread-pool callback. Each repository call opens and closes its own
short-lived PostgreSQL transaction. This browser slice opens no DuckDB
connection and introduces no process-wide database connection or pool. Focused
route tests verify the thread boundary, cookie separation, CSRF enforcement,
exact logout revocation, and role summary. Application and repository tests
cover the keyed account digest and stable Organization keyset.

The rebuilt local image stayed healthy behind the loopback TLS proxy on schema
version `11`; an immediate migration replay applied nothing. The earlier
Platform Administrator trial loaded the Organization dashboard, created the
fictional `local-browser-trial` Organization through its CSRF-protected form,
and retained the exact signed-out session as `REVOKED`. A second sanitized live
trial created and activated a fictional Organization Administrator through the
implemented service boundary without printing its raw activation token. Login
redirected to `/admin/users`; the list and exact detail returned `200`, the
Platform Organization route returned `403`, an unavailable user returned
`404`, and CSRF-protected logout revoked the exact session. The bounded
security events were `PASSWORD_REHASHED`, `AUTHENTICATION_SUCCEEDED`,
`SESSION_CREATED`, and `SESSION_REVOKED`.

Work Package 4.5 is complete for the loopback local trial, verified on
2026-10-31. Platform administrators can open exact Organization details,
assign or replace the administrator, rename, suspend, reactivate, and require
new sign-in for everyone. Organization administrators can create ordinary
users and confirm their suspension, reactivation, or permanent deactivation.
They can inspect and revoke exact tenant-bound sessions and read bounded
newest-first account, authentication, and session history. Ordinary customer
users now authenticate under the same generic policy and receive a session
bound permanently to their Organization. Their landing page exposes only
their account and current session; no Project access is implied.

Both customer account kinds can activate and consume password reset through
public forms. Authorized administrators see a newly issued link once, only
after commit. The token travels in a URL fragment, is moved into the POST form
by a page-owned script, and is removed from browser history. Anonymous forms
have an independent secure CSRF cookie. One-time link responses use
`no-referrer` and `no-store`. Form pages use `same-origin` referrer policy so
real HTML form POSTs retain the exact Origin required by the browser guard.
The first real-browser check exposed and corrected the previous
`no-referrer` policy's `Origin: null` form behavior.

Immutable migration `12` records replacement activation issuance. Pending
ordinary users can receive replacement activation links under their own
administrator. The Platform Administrator can replace the assigned
administrator's activation link or issue their reset link. Reissuance revokes
the earlier token and advances the applicable optimistic revision atomically.
Customer reset issuance now also advances the user's optimistic revision.
The Platform Administrator can create their own reset link from **My account**.
Public reset requests remain generic; local-trial email delivery is explicitly
disabled and the authorized administrator supplies the link privately.

The rebuilt image applied schema `12` once and replayed it with no changes.
The browser rehearsal uses the installed Edge headless browser through the
loopback TLS proxy, performs the complete account journey with fictional
values, and checks that zero network request URLs contain a raw token. It
captures token-free desktop and narrow layouts. The audit review also found
that JSONB parameter ordering caused unnecessary password rehashes; the policy
now compares parameter values by key, while preserving rehash for genuinely
older work parameters.

Project roles and grants remain Phase 5 work. Browser WebAuthn enrollment and
privilege elevation also remain open, so the Internet security gate remains
blocked. Completing the local Phase 4 exit gate does not waive that release
gate or the Phase 2 supported-host qualification.

### Work package 4.6 - Test authentication and administration

- Unit-test password, token, session, expiry, and state-transition contracts.
- Integration-test login throttling, fixation prevention, CSRF, same-origin,
  session rotation, reset revocation, suspension, and expired tokens.
- Browser-test the complete bootstrap-to-first-user journey with fictional
  values.
- Test cross-Organization direct-object requests at route, service, and
  repository layers.
- Test that local `create_local_app` exposes no server login or administration
  routes and continues to use its launch session.

Current local-trial status: implemented and verified on 2026-10-31. Portable
tests cover the domain, service, repository, browser-policy, and route
contracts. Linux-owned PostgreSQL tests exercise all historical migration
prefixes, concurrent migrators and first-operator bootstrap, throttling,
session rotation, and the complete browser lifecycle against a disposable
test database. The browser suite includes expired and replayed tokens,
replacement links, administrator replacement, reset session revocation,
stale mutations, missing confirmations, wrong CSRF, cross-Organization
requests, and the local composition's route isolation. Dashboard reads open
zero DuckDB connections; each PostgreSQL operation owns one bounded
connection and no pool. The Linux workflow installs the test extra for these
browser contracts. `scripts/rehearse_server_identity.py` supplies a separate
reproducible real-browser check of the current local image.

### Phase 4 exit gate

**Local-trial gate: passed on 2026-10-31.** The implemented browser journey
meets the following conditions after the one-time first-operator bootstrap.
Link delivery is manual for this local trial. Production email policy,
privileged WebAuthn, image acceptance, and supported-host qualification retain
their own release gates.

- A fresh server can bootstrap a Platform Administrator, create an
  Organization, create its administrator and users, activate accounts, log in,
  reset a password, revoke sessions, and suspend access.
- Every customer user belongs to one Organization and cannot switch or receive
  a Project grant in another.
- Platform operation and customer-data access are separate in executable
  authorization tests.
- The administration journey is understandable without direct database or
  command-line changes after bootstrap.

## Phase 5 - Enforce roles, separation of duties, and collaborative edits

### Objective and entry conditions

Give Organization administrators usable role and Project-access controls while
enforcing every permission below the browser. Built-in users and Project
catalogue repositories are available.

### Work package 5.1 - Extend the capability vocabulary

Add bounded server capabilities for:

- Organization viewing and management;
- customer-user and membership management;
- role viewing, definition, revision, assignment, and exceptional grants;
- Project-access management;
- session revocation and authentication administration;
- Odoo connection-profile and target-credential management;
- server job viewing, cancellation, and administration;
- retention selection and Project deletion; and
- platform operations that carry no implicit customer-data access.

Retain existing migration capabilities. Keep `local_stack.inspect`,
`local_stack.start`, and `local_stack.stop` impossible to assign through a
server role.

### Work package 5.2 - Remove the server universal bypass

- Change authorization so `PROJECT_ADMIN` no longer means “all capabilities”
  for a server actor.
- Preserve the current trusted local behavior through the local authorization
  adapter rather than a shared `Actor.has` shortcut.
- Resolve effective server capabilities from the active role revision,
  Project grant, direct exception, expiry, Organization policy, and action
  scope.
- Return an immutable authorization receipt used by the command and retained
  by governed evidence.

### Work package 5.3 - Seed and version standard roles

Provide versioned definitions for the roles accepted in the parent plan:

| Role | Initial capability responsibility |
| --- | --- |
| Platform Administrator | Server health, configuration, Organization provisioning, and server recovery only. |
| Organization Administrator | Users, role definitions, Project access, sessions, retention defaults, and approved connection profiles within one Organization. |
| Project Manager | Project lifecycle, participant grants, Data version and run creation, and Workspace coordination. |
| Data Manager | Source inspection and selection, schema governance, mapping, coverage, resolution decisions, normalization decisions, preparation, and preflight. |
| Reviewer | The configured resolution, normalization, export-plan, Recipe, and cutover approval capabilities, without input editing or execution. |
| Migration Operator | Execution of an already approved plan, progress control, and permitted recovery or reconciliation actions. |
| Recipe Steward | Recipe creation, application, publication, qualification, retirement, and catalogue viewing in the granted scope. |
| Auditor | Project and audit views without raw protected evidence unless a separate sensitive-evidence grant exists. |

Store the exact capability set and role revision. Editing a role creates a new
revision and affects future authorization according to an explicit activation
rule. It never rewrites the role or policy revision already recorded on an
approval or execution.

### Work package 5.4 - Implement Project grants and access review

- Grant a user one or more roles on named Projects inside their Organization.
- Allow an explicitly governed Organization-wide role only when its role type
  supports that scope.
- Add visible direct exceptional grants with reason, creator, start, expiry,
  and review status. Do not expose per-user permission checkboxes as the normal
  path.
- Build an effective-access page that answers who can view, change, approve,
  execute, administer, and inspect sensitive evidence for each Project.
- Make account suspension and grant revocation prevent new commands
  immediately. Mark queued jobs for cancellation or review according to their
  action contract.

### Work package 5.5 - Enforce separation of duties

- Define versioned policy rules for decision versus approval, approval versus
  execution, Recipe publication versus qualification, cutover selection, and
  Production activation.
- Bind each rule to the exact governed operation and compare stable actor
  identities. Assigning two roles to one user cannot satisfy an independent-
  actor requirement.
- Let small Organizations combine roles where the selected policy permits it.
  The dashboard explains which action requires another person before the user
  begins the governed step.
- Recheck authority when a worker starts and immediately before an Odoo write.
  A saved approval does not preserve execution authority after revocation.

The default role-capability manifest and default separation policy require a
specific product-ownership review before this work package exits.

### Work package 5.6 - Add remote-only optimistic editing

- Add an `expected_revision` to every mutable server command for Project,
  Data version, Workspace configuration, mapping draft, role, connection
  profile, and retention policy.
- On mismatch, return a typed conflict containing the current revision and
  bounded change summary. Never apply a last-write-wins fallback.
- Preserve the user's submitted values as an encrypted, expiring conflict
  draft owned by that user and Project. The draft is not authoritative
  migration evidence and is deleted after resolution, expiry, or Project
  deletion.
- Show the latest saved values beside the user's draft and offer **Reload**,
  **Review my changes**, and a deliberate reapply action where the command can
  safely revalidate the new base.
- Verify the current revision again when starting a background job so a stale
  open form cannot launch work over newer configuration.
- Do not add this UI or persistence to the local composition.

### Work package 5.7 - Complete authorization coverage

- Inventory every state-changing route and every read that exposes Project,
  artifact, job, target, or protected evidence.
- Add a capability and scope assertion before repository or secret access.
- Add role-matrix tests for each application command, not merely for button
  visibility.
- Add cross-Organization tests for guessed IDs, artifact keys, download URLs,
  job IDs, conflict drafts, connection profiles, and deletion requests.
- Add tests proving that a Platform or Organization Administrator has no
  customer-data access without an explicit Project role.

### Phase 5 exit gate

- Organization administrators can assign standard or custom versioned roles
  and review effective access in business language.
- Service and repository tests cover every command and sensitive read.
- `PROJECT_ADMIN` cannot bypass server authorization.
- Required independent actors remain independent even when one user holds
  several roles.
- Two remote users cannot silently overwrite the same mutable revision, while
  local behavior remains unchanged.

## Phase 6 - Make long-running work durable and fairly scheduled

### Objective and entry conditions

Let users leave, reconnect, and survive process restarts without losing the
meaning or authority of long-running work. PostgreSQL jobs, scoped
authorization, and the cross-store operation protocol are available.

### Work package 6.1 - Complete the durable job model

**Implementation status on 2026-10-29:** Started at the portable contract
boundary only. `DurableJobKind` now allowlists all ten planned job families,
and `DurableJobRequest` binds the Organization, Project, optional Workspace,
requester, authorization receipt and revision, idempotency key, semantic input
hash, and manifest hash. The contract now includes `REVIEW_REQUIRED`,
compare-and-set transition revisions, worker-bound attempts with fencing and
heartbeat facts, and an explicit cancellation disposition receipt. Pure tests
reject invalid transitions, reversed attempt times, ambiguous terminal
outcomes, and a terminal review state returning to `RUNNING`.

The second portable increment binds restart recovery to an exact expired-
attempt receipt check. No-receipt evidence permits a retryable interruption, a
matching committed receipt requires completion from that receipt, and a
mismatch or unavailable check requires review. An `INTERRUPTED` transition now
requires the retained recovery-decision identity, so a restart cannot infer
that state from process loss alone.

The third portable increment binds the existing frozen Final review comparison
request to `DurableJobRequest` through `DurablePreflightSubmission`. It derives
the Organization, Project, Workspace, operation, semantic hash, requester,
request time, and authorization receipt from verified application facts. It
represents the immutable worker command only by its hash, rechecks Final review
authority before enqueue, and rejects a returned reservation that substitutes
any request fact. It adds no credential, callback, path, database owner,
PostgreSQL adapter, route, or worker.

These portable increments do not satisfy the Phase 6 entry gate. They add no
PostgreSQL job table, claim query, worker process, route, scheduler, or Final
review server adapter while Phase 4 identity and Phase 5 scoped authorization
remain incomplete. Those dependencies must still pass before the contract is
wired to server execution.

Extend the portable job contract and PostgreSQL schema with:

- job kinds for source inspection, Odoo capture, preparation, preflight,
  Recipe application, report creation, load, correction, reconciliation, and
  governed deletion;
- `QUEUED`, `RUNNING`, `REVIEW_REQUIRED`, `SUCCEEDED`, `FAILED`, `CANCELLED`,
  and `INTERRUPTED` states;
- immutable Organization, Project, Workspace, requester, request time,
  authorization revision, idempotency key, input hash, and manifest hash;
- one or more attempts with worker identity, fencing token, start, heartbeat,
  finish, bounded failure code, and result receipt;
- bounded progress that contains no source values, target values, credentials,
  protected identifiers, or complete row collections; and
- explicit cancellation request and disposition rather than deletion of job
  history.

State transitions use compare-and-set revisions. A terminal job cannot return
to running. A restart changes an expired running attempt to `INTERRUPTED` only
after its data-plane receipt has been checked.

### Work package 6.2 - Implement leases and fencing

- Claim a queued job and its required Workspace or action resource in one
  PostgreSQL transaction.
- Allocate a monotonically increasing fencing token for that resource.
- Require the worker to include the token in every data-plane commit and
  receipt. A stale token cannot publish or become current after a newer claim.
- Heartbeat at a bounded interval. Lease expiry makes the attempt eligible for
  reconciliation, not automatic replay.
- Release a lease only after verified terminal state or a recorded interrupted
  disposition.
- Keep exclusive Odoo write scope stricter than ordinary Workspace preparation
  scope.

### Work package 6.3 - Build the worker runtime

- Add a worker command to the shared container image. It claims only allowlisted
  job kinds and refuses a manifest with an unsupported contract version.
- Materialize the exact Project, Data version, and Workspace stores from the
  server artifact root. Confirm lineage and hashes before executing work.
- Open only the leased DuckDB databases and staging paths. Do not give a worker
  general access to every Organization root when operating-system or container
  isolation can narrow it.
- Install signal handling that stops claiming work, requests a safe checkpoint,
  records interruption when possible, and exits before the container grace
  period ends.
- Send compact progress to PostgreSQL. Keep DuckDB evidence and execution
  journals authoritative.

### Work package 6.4 - Migrate job families in safety order

Migrate and qualify each family separately:

1. **Preparation** proves CPU, memory, Parquet publication, Workspace leases,
   receipts, restart, and cancellation without target writes.
2. **Source inspection and file intake** prove bounded uploads, validation,
   retained source publication, and cleanup of rejected files.
3. **Odoo capture** adds approved outbound reads, protected origins, and
   immutable publication.
4. **Preflight and Recipe application** add target snapshots, reusable Recipe
   bindings, and review-required outcomes.
5. **Reports** prove bounded read-only artifact generation.
6. **Load, correction, and reconciliation** retain journal-before-write,
   exact target binding, unknown-outcome stop, and manual recovery. These job
   types do not reuse preparation cancellation semantics blindly.

The Preflight migration in item 4 owns the remaining shared-server Final review
work. Its route must authorize before storage access, reserve one
Organization-scoped idempotency key, and retain only an opaque secret reference
and generation. Its worker must recheck authority, resolve only the leased
Workspace, prove the current fencing token before data commit and receipt
publication, and close PostgreSQL transactions before Odoo or DuckDB work.

The Workspace transaction must publish the comparison and its immutable
`DataPlaneOperationReceipt` together. PostgreSQL must derive the bounded ready
or review-required projection from that exact receipt. It may retain bounded
counts, readiness, target-binding hash, artifact identities, and the current
receipt hash. It must not retain comparison rows, target snapshots, the
execution schedule, protected values, credentials, or a complete report.

Qualification must inject failure before and after the DuckDB commit, artifact
publication, data-plane receipt, PostgreSQL projection, and progress update. A
stale worker must be unable to become current. Another Organization must be
unable to list, poll, download, or recover an attempt by guessing its IDs. A
committed data-plane receipt with a missing projection must complete without
repeating the comparison or its Odoo reads. Progress and result discovery must
use bounded PostgreSQL queries and open no Workspace DuckDB database.

Remove or restrict each process-local manager only after its server replacement
passes contract, restart, and browser tests. The local composition may continue
to use focused local managers or adapters behind the same portable contracts.

### Work package 6.5 - Implement equal dynamic scheduling

- Give every active Organization weight `1`. An Organization is active when it
  has a running or runnable job.
- When one Organization is active, let its eligible jobs use every safe idle
  worker slot.
- When several Organizations are active, choose subsequent jobs using a
  work-conserving fair queue based on recent allocated slots or service time.
- Do not preempt a running DuckDB or Odoo operation solely to rebalance. Give a
  newly active under-allocated Organization priority at the next safe claim.
- Preserve FIFO order within one Organization unless an action-specific safety
  rule requires exclusivity or explicit user priority.
- Record a bounded scheduling reason such as `WAITING_FOR_FAIR_SHARE`,
  `WORKSPACE_LEASED`, `ODOO_WRITE_EXCLUSIVE`, or `RESOURCE_LIMIT` so the browser
  can explain the queue.

The first policy has no preferred Organization and no paid priority. Any later
weighting change requires a product and fairness decision, not an unreviewed
configuration edit.

### Work package 6.6 - Bound CPU, memory, threads, and disk

- Configure the total worker pool independently from per-job DuckDB and Polars
  threads.
- Set per-job memory, CPU, temporary-space, file-size, and execution-time
  ceilings appropriate to the job kind.
- Reserve capacity for the web process, PostgreSQL, filesystem flush, health,
  and administrative recovery.
- Give each Organization an equal soft artifact share and permit borrowing of
  unused space. Retain a hard global emergency reserve.
- When the reserve is reached, reject new uploads and growth jobs before they
  write partial data. Keep existing evidence readable and give an explicit
  capacity action.
- Never delete borrowed data merely to rebalance storage.

### Work package 6.7 - Implement durable progress pages

- Query PostgreSQL for job lists and bounded progress without opening every
  Workspace DuckDB database.
- Show queued reason, start, last progress, interruption, failure code, review
  requirement, result, and one safe next action.
- Let a user leave, sign in again, and return to the same authorized job.
- Add an in-application notification list for terminal and review-required
  transitions. Email or external notifications are separate later work.
- Use pagination and bounded refresh. Do not open one PostgreSQL transaction or
  one DuckDB connection per visible job.

### Work package 6.8 - Qualify recovery and capacity

- Inject process exit before and after claim, lease, DuckDB commit, artifact
  publication, receipt, projection update, and Odoo call boundaries.
- Test duplicate browser submissions, changed meaning under one idempotency
  key, cancellation before start, cancellation at a safe checkpoint, stale
  worker completion, and expired authority.
- Benchmark one and two representative preparations on the accepted server
  size. Measure elapsed time, throughput, peak RSS, CPU, DuckDB and Polars
  threads, disk growth, pool waits, physical connections, and lock failures.
- Test one active Organization consuming all safe slots and several active
  Organizations receiving equal dispatch opportunity.
- Retain one worker if two do not improve useful throughput or remain within
  memory and disk limits.

### Phase 6 exit gate

- Every supported long-running server action has a durable job and
  action-specific recovery contract.
- A web or worker restart cannot report false success or cause duplicate target
  writes.
- Fair-scheduler evidence covers idle-capacity borrowing and equal multi-
  Organization dispatch.
- Progress pages remain responsive from PostgreSQL projections without opening
  detailed evidence per row or job.
- The accepted initial worker and thread limits are measured and documented.

## Phase 7 - Complete server networking, secrets, retention, and operations

### Objective and entry conditions

Make the server safe and operable for real Organization data. Durable jobs and
the administration and authorization model are complete.

### Work package 7.1 - Implement approved Odoo connection profiles

Create a non-secret PostgreSQL connection profile owned by one Organization
and environment. It records:

- display name, approved base URL, database, supported Odoo major, and company
  context;
- `PUBLIC_HTTPS`, `PRIVATE_ROUTE`, `CONNECT_PROXY`, or `DIRECT_PRIVATE`
  network mode;
- exact host, port, protocol, approved resolved ranges, and DNS policy;
- public or private CA trust identity;
- read-only or write-capable purpose and permitted model scope;
- profile revision, approving actor, last successful probe, and status; and
- references to separate credential bindings in the server secret provider.

Only an Organization Administrator with connection-profile authority can
create or revise the profile. A data manager or migration operator selects an
approved profile and cannot provide an arbitrary worker URL.

### Work package 7.2 - Enforce remote egress policy

- Default-deny worker egress except DNS, time, package-independent operational
  endpoints, approved proxies, and approved Odoo targets required by the job.
- Resolve and validate the approved host before credential lookup. Revalidate
  after connection establishment where the transport allows it.
- Reject redirects, DNS rebinding, loopback, link-local, metadata-service,
  multicast, and unspecified addresses. Permit private ranges only when the
  approved profile uses an explicit private network mode and route.
- Require HTTPS and normal certificate validation. Install a customer-approved
  private CA chain instead of adding a “disable verification” option.
- Bind proxy credentials, CA material, and Odoo credentials through the secret
  provider. Keep them out of PostgreSQL rows, images, logs, health output, and
  job manifests.
- Recheck target, principal, company context, credential generation, model
  scope, and authorization at worker start and immediately before a write.

The reference server supports fixed public egress, site-to-site VPN or private
cloud routing, a named HTTP CONNECT proxy, and direct on-premises network
access. A generic reverse tunnel or customer site agent is outside the first
release and requires a separate threat model.

### Work package 7.3 - Select and qualify the server secret provider

- Define secret identities independently from their backend path or vendor
  name.
- Store session-signing keys, password security peppers if used, encryption
  keys, Odoo credentials, proxy credentials, and private CA material in the
  selected provider.
- Bind target credentials to Organization, Project or run scope, environment,
  target hash, principal, company context, purpose, and generation.
- Rotate one secret generation without rewriting immutable migration evidence.
  New work uses the new generation; saved receipts keep the non-secret binding
  hash they used.
- Test revoked, missing, changed, wrong-Organization, wrong-target, and wrong-
  purpose credentials before any target call.
- Define break-glass access, provider outage behavior, key backup, and key
  recovery. A provider outage fails closed for new sensitive work.

### Work package 7.4 - Implement Project retention selection

At Project creation, an authorized user selects:

- delete after a fully verified Odoo load and explicit final confirmation;
- delete after 90 days without meaningful Project activity; or
- keep until manual deletion, subject to a disclosed deployment maximum.

Define meaningful activity as an authorized Project mutation, accepted review,
job submission, job result, or explicit retention extension. Page views,
health checks, polling, failed login, and automated retention scans do not
restart the 90-day clock.

Show the selected policy and planned deletion date on the Project. Notify
authorized users before a scheduled deletion and allow a permitted policy
change until deletion enters its irreversible execution state.

### Work package 7.5 - Implement governed deletion

Use an idempotent state machine:

```text
ACTIVE -> PENDING_DELETION -> DELETING -> DELETED
                              `-> DELETION_FAILED
```

- `PENDING_DELETION` blocks new jobs and displays the irreversible consequence.
- Final confirmation names the Project and explains that it cannot be reopened,
  compared, retried, audited in detail, or reproduced.
- Offer permitted final reports and portable Recipe export before deletion. A
  reusable Organization Recipe remains separate unless the user selects it or
  deletes the complete Organization.
- Cancel queued jobs, drain or reconcile running jobs, revoke Project sessions
  and grants, and prevent new target I/O.
- Delete Project-scoped credentials and keys, protected evidence, retained
  sources, DuckDB files, Parquet and report artifacts, conflict drafts, job
  payloads, PostgreSQL catalogue rows, and derived projections in an order that
  supports safe retry.
- Retain only the minimal non-customer-data receipt approved by the parent
  plan. It records opaque Project and Organization identities, policy,
  requesting actor, times, and completion status.
- A partial failure enters `DELETION_FAILED`, remains blocked, and resumes the
  idempotent deletion. It never restores partial Project usability.

### Work package 7.6 - Define backup and restore checkpoints

- Drain new mutations and let safe work finish or become explicitly
  interrupted.
- Record a backup checkpoint in PostgreSQL, flush and close writable DuckDB
  stores, and create consistent PostgreSQL and block-volume backups associated
  with that checkpoint.
- Encrypt backups and separate backup access from ordinary application access.
- Record frequency, retention, recovery-point objective, recovery-time
  objective, verification, and expiry.
- Restore to a separate isolated root and PostgreSQL instance. Start with Odoo
  networking and workers disabled until schema, receipts, hashes, secrets, and
  deletion state are verified.
- Do not revive a Project whose deletion receipt predates the restore point
  without an explicit security and legal recovery decision.

Active-storage deletion cannot promise immediate removal from unexpired backup
media. Show the backup-expiry date before confirmation. Prefer Project-scoped
encryption keys so destroying a key can make retained backup bytes unreadable
when the accepted deletion contract requires that guarantee.

### Work package 7.7 - Add observability without data leakage

Collect bounded metrics and structured events for:

- liveness, readiness, request rate, duration, and bounded failure codes;
- PostgreSQL pool use, waits, transaction duration, retries, and migrations;
- queue length by non-sensitive job kind, fair-share state, worker heartbeat,
  exit, lease expiry, and attempt duration;
- CPU, memory, disk, emergency reserve, artifact publication, and backup result;
- login failure, session revocation, role and grant changes, connection-profile
  changes, retention schedule, and deletion progress; and
- outbound Odoo target profile identity, duration, status class, and bounded
  connector failure code.

Do not emit source values, prepared values, Odoo IDs, credentials, access or
reset tokens, complete URLs with secrets, customer database error bodies,
protected filters, or raw evidence. Test log redaction with hostile example
values.

### Work package 7.8 - Write operational runbooks

Provide tested runbooks for:

- Debian and Ubuntu installation;
- first start, database migration, and Platform Administrator bootstrap;
- certificate installation and renewal;
- secret and Odoo credential rotation;
- worker scaling, fair-share diagnosis, disk expansion, and emergency reserve;
- controlled drain, restart, application upgrade, incompatible rollback, and
  restore;
- PostgreSQL failure, worker crash loop, corrupt artifact, expired lease,
  uncertain Odoo outcome, and deletion failure;
- user compromise, Organization suspension, and break-glass access; and
- Organization export, offboarding, retention, backup expiry, and final
  deletion verification.

An administrator follows commands and expected results. The runbook does not
require editing PostgreSQL rows, DuckDB files, or protected artifacts by hand.

### Phase 7 exit gate

- Each supported Odoo network mode passes allow, deny, redirect, DNS, private-
  CA, wrong-target, proxy, and credential-isolation tests.
- Project retention and deletion complete across PostgreSQL, DuckDB, artifacts,
  secrets, jobs, and drafts with deterministic retry after injected failure.
- Backup and isolated restore recover one consistent checkpoint without
  contacting Odoo.
- Operational logs and metrics pass the protected-data redaction suite.
- A second administrator completes the install, upgrade, secret rotation,
  backup, restore, and deletion rehearsals from the runbooks.

## Phase 8 - Pilot and release the first shared-server profile

### Objective and entry conditions

Prove that the implemented server is usable and safe for the intended
Organization administrators, data managers, reviewers, migration operators,
and auditors. All earlier automated gates and operational rehearsals pass.

### Work package 8.1 - Prepare the pilot environment

- Deploy the exact release-candidate image and configuration on the accepted
  Linux host profile.
- Use fictional or sanitized source data and disposable supported Odoo targets.
- Create at least two Organizations so tenancy isolation and resource fairness
  are exercised even if the first production installation expects one.
- Create separate accounts for Platform Administrator, Organization
  Administrator, two data managers, reviewer, migration operator, Recipe
  steward, and auditor. Do not combine accounts merely to shorten testing.
- Record image digest, schema versions, host resources, worker and thread
  limits, network mode, secret backend, backup policy, and retention policy.

### Work package 8.2 - Run the usability journeys

Observe each actor completing their normal work without developer help:

- The Platform Administrator provisions an Organization without seeing its
  Projects.
- The Organization Administrator creates users, activates accounts, assigns
  standard and custom roles, grants Projects, reviews access, configures an
  approved Odoo profile, and chooses retention.
- Two data managers work on separate Workspaces and then create an intentional
  same-form revision conflict.
- The second data manager recovers their draft without silently overwriting the
  first user's change.
- A reviewer sees the exact governed decision and completes or rejects it
  without editing its input.
- A migration operator executes only the approved target-bound plan and sees
  safe progress and reconciliation.
- An auditor reads permitted history but cannot open protected evidence without
  the separate grant.

Record confusing wording, unnecessary administrator intervention, unclear
queue reasons, and missing next actions as product defects, not training
failures.

### Work package 8.3 - Exercise tenancy and authorization attacks

- Request another Organization's Project, Workspace, artifact, job, profile,
  draft, report, and deletion IDs directly.
- Replay a valid request after role revocation, session revocation, user
  suspension, Organization suspension, and policy revision.
- Attempt to combine roles to bypass independent-review requirements.
- Attempt path traversal, storage-key substitution, receipt substitution,
  stale fencing, idempotency-key collision, CSRF, session fixation, and login
  enumeration.
- Confirm that denied requests reveal no existence, label, path, target, count,
  or protected failure detail.

### Work package 8.4 - Exercise capacity and fairness

- Let one Organization consume all safe idle preparation capacity.
- Add runnable work from the second Organization and verify equal subsequent
  dispatch without terminating the first Organization's safe running job.
- Fill one Organization beyond its equal soft storage share but below the
  global reserve, then activate the second Organization.
- Reach the emergency reserve and prove that new growth stops before partial
  publication while existing evidence remains readable.
- Record user-visible queue reasons and administrator diagnostics.

### Work package 8.5 - Exercise failure and disaster recovery

- Restart the web process during editing and while jobs are queued.
- Terminate a worker before DuckDB commit, after DuckDB commit, after artifact
  publication, and before PostgreSQL projection finalization.
- Expire a lease and start a replacement worker, then reject the stale worker's
  completion.
- Interrupt Odoo transport before a call, after a read, and around a simulated
  unknown write outcome. Preserve the existing action-specific stop and
  reconciliation behavior.
- Restore the accepted backup to an isolated host, verify its checkpoint, and
  keep Odoo disabled until explicit release.
- Execute each retention choice, including post-load deletion, 90-day policy
  simulation, manual retention, partial deletion failure, and backup-expiry
  disclosure.

### Work package 8.6 - Produce the release evidence

The pilot report includes:

- each tested user journey and observed result;
- Organization-isolation and role-matrix results;
- workload sizes, worker allocations, elapsed time, peak memory, CPU, disk,
  pool waits, and lock outcomes;
- restart, recovery, backup, restore, deletion, and Odoo network results;
- security review findings and disposition;
- accepted server size, worker limit, storage reserve, recovery objectives,
  retention default, and operating constraints; and
- every omitted live, provider, host, browser, or Odoo scenario.

### Phase 8 release gate

The first shared-server profile becomes current only when:

- Organization administrators and data managers complete the normal journeys
  without developer intervention;
- no cross-Organization read, write, artifact, job, draft, target, or deletion
  access succeeds;
- concurrent edits, durable jobs, fair scheduling, revocation, recovery,
  retention, backup, and restore meet their accepted contracts;
- the measured host and worker configuration remains within capacity limits;
- security, operations, and product ownership accept the pilot report; and
- current architecture, developer workflow, user guidance, screenshots,
  contracts, code map, runbooks, and release documentation are updated together.

The release does not imply that corporate identity, multiple application
replicas, high availability, object-storage workers, or live local-to-server
synchronization exist.

## Phase 9 - Add corporate identity providers

### Objective and entry conditions

Add Microsoft Entra, Google Identity, and generic OpenID Connect sign-in over
the accepted user, Organization, role, and session model. The built-in account
server profile is already released and remains the authorization authority.

### Work package 9.1 - Define provider configuration

- Store provider type, issuer, client identity, permitted Organization,
  callback URL, requested scopes, claim policy, and status as non-secret
  PostgreSQL configuration.
- Store client secrets or private keys in the server secret provider.
- Require authorization-code flow with PKCE, state, nonce, exact callback, and
  normal issuer, audience, signature, time, and token-type validation.
- Treat OAuth access authorization alone as insufficient for login. Use the
  provider's qualified OpenID Connect identity semantics.

### Work package 9.2 - Link identity to the existing user

- Bind an external identity by verified issuer and subject to one existing
  `user_id` and therefore one Organization.
- Never create or transfer authority from an email match alone.
- Require an authenticated administrator or the already-authenticated user to
  approve linking according to Organization policy.
- Prevent one external identity from binding to two users or Organizations.
- Record link, unlink, provider disablement, and failed-link security events.

### Work package 9.3 - Preserve Impodo authorization

- Continue resolving roles, Project grants, direct exceptions, separation
  policy, and authorization receipts from Impodo.
- Add optional directory-group mapping only as a source of versioned Impodo
  assignments. Define refresh, removal, conflict, overage, and provider-outage
  behavior explicitly.
- Do not query the provider once per Project or authorization check. Cache only
  bounded verified identity facts for the accepted session lifetime.
- A provider account removal or Organization policy change prevents new
  sessions and revokes existing sessions according to policy.

### Work package 9.4 - Preserve recovery access

- Keep a small number of explicitly approved Impodo-managed break-glass
  Platform Administrator accounts.
- Protect them with the accepted stronger authentication, monitoring, storage,
  and periodic test policy.
- A provider outage may block corporate login but must not weaken Project
  authorization, accept unsigned identity, or silently convert an email into a
  built-in account.

### Work package 9.5 - Qualify providers separately

For each provider, test:

- correct login, logout, session rotation, account linking, and unlinking;
- wrong issuer, tenant, audience, signature, nonce, callback state, clock, and
  domain;
- changed email or display name with stable issuer and subject;
- duplicate subject, attempted cross-Organization link, group overage, missing
  group claims, and removed assignment;
- provider timeout, key rotation, metadata outage, callback replay, and
  administrator disablement; and
- continued local desktop behavior and built-in break-glass access.

Classic Active Directory, LDAP, or SAML uses an approved federation bridge or
a separately planned and qualified adapter. It is not implied by generic OIDC
support.

### Phase 9 exit gate

- A federated user reaches the same `user_id`, Organization, roles, Project
  grants, and audit history as their built-in identity.
- Email matching cannot create or transfer access.
- Provider and group removal follow the accepted revocation policy.
- Provider outage fails closed without disabling audited break-glass recovery.
- Each provider's security and browser acceptance evidence is recorded
  separately before its login option is shown as available.

## Cross-phase verification matrix

| Boundary | Required evidence before server release |
| --- | --- |
| Dependency direction | Architecture tests reject adapter imports from domain or application code and reject concrete adapter construction outside compositions and workers. |
| Local compatibility | Focused local application, integration, browser, DuckDB lifetime, recovery, and performance tests pass after every shared change. |
| PostgreSQL repositories | Disposable-database contract tests cover transactions, uniqueness, optimistic revisions, tenancy scope, migrations, timeouts, and recovery. |
| Organization isolation | Route, service, repository, artifact, job, draft, secret, download, and deletion tests attempt cross-Organization IDs and keys. |
| Authentication | Password, activation, reset, session, throttling, CSRF, fixation, expiry, suspension, revocation, and security-event tests pass. |
| Authorization | A generated role matrix exercises every state-changing command and sensitive read; policy tests cover combined roles and independent actors. |
| Cross-store recovery | Fault injection covers every operation boundary and proves receipt integrity, fencing, idempotency, interruption, and projection reconciliation. |
| DuckDB and Parquet | Existing identity, ordering, hash, artifact, schema upgrade, connection-lifetime, and bounded-processing tests remain authoritative. |
| Durable jobs | Claim, heartbeat, expiry, cancellation, restart, stale completion, progress, review, and action-specific Odoo recovery tests pass. |
| Fair scheduling | Deterministic scheduler tests and measured multi-Organization runs prove equal dispatch and idle-capacity borrowing. |
| Odoo networking | Allowlist, SSRF, DNS, redirect, proxy, VPN or private route, private CA, target binding, read/write credential, and error-redaction tests pass. |
| Retention and deletion | Countdown, activity definition, warning, export, drain, deletion retry, secret removal, backup disclosure, receipt, and restore tests pass. |
| Containers and hosts | Image scan, non-root execution, read-only filesystem, mount validation, private networking, Debian and Ubuntu rehearsal, restart, and drain pass. |
| Operations | Backup, isolated restore, upgrade, incompatible rollback, certificate and secret rotation, disk pressure, and incident runbooks are independently rehearsed. |
| Usability | Browser journeys for every standard role use exact current labels and show one safe next action for denial, conflict, queueing, interruption, and deletion. |

Performance qualification must measure elapsed time, connection counts,
connection time, schema checks, pool waits, query counts, retries, memory, CPU,
disk growth, and lock failures where applicable. A structural call-count result
is not a live latency claim.

## Data adoption and upgrade strategy

### First server installation

The first server starts with an empty PostgreSQL control plane and empty
artifact root. It does not point at a local `registry.duckdb` or convert a
developer's current project root during startup.

### Local-to-server Project transfer

Local transfer is a later governed export and import command that:

- freezes one consistent local Project package;
- preserves Project, Data version, run, Workspace, Recipe, operation, artifact,
  and evidence identities where the server has no collision;
- verifies hashes and schema generations before creating PostgreSQL catalogue
  projections;
- excludes local launch sessions, Windows keyring material, local-stack state,
  and reusable target credentials;
- binds the imported Project to one selected Organization and records its
  origin and importer;
- refuses unsupported historical schemas without rewriting the source package;
  and
- leaves the local copy independent. It creates no live synchronization.

The Project is authoritative in one installation for new work. Moving active
authority again requires another explicit export and import decision; a shared
folder is not a transfer mechanism.

### Server upgrades

- Apply PostgreSQL migrations before starting a build that requires them.
- Upgrade mutable structural metadata forward. Never reinterpret or rewrite an
  existing hash-bound source, Recipe, approval, journal, execution, or
  reconciliation payload in place.
- Upgrade DuckDB stores only through their current forward schema mechanisms
  after compatibility inspection and backup.
- Record the application build contract on jobs and receipts. An old worker
  cannot claim a manifest requiring a newer contract.
- Roll back through a compatible earlier image or a restored checkpoint. Do
  not issue reverse migrations that discard accepted evidence.

## Initial implementation backlog

The first reviewable increments should occur in this order:

1. **P0 ownership and ADRs.** Produce the fact inventory and accept the revised
   architecture decisions.
2. **P0 portable contracts.** Add Organization scope, authorization receipt,
   lease, fencing, operation receipt, conflict, and deletion contracts with
   pure tests.
3. **P1 composition extraction.** Move local construction into explicit local
   builders without changing runtime behavior.
4. **P1 request actor.** Remove the global actor from shared route handling and
   make local request resolution explicit.
5. **P1 Project visibility.** Add scoped list and contained-resource contracts,
   using the implicit local Organization adapter first.
6. **P2 container shell.** Build and qualify the non-root image, empty server
   entry point, proxy, PostgreSQL, mounts, health, and migration command. Run
   the loopback-only local container rehearsal before the Debian and Ubuntu
   host qualification.
7. **P3 PostgreSQL foundation.** Add migrations, connection handling,
   Organization and Project catalogue repositories, and disposable-database
   contract tests.
8. **P3 operation recovery.** Implement idempotent operations, leases, fencing,
   receipts, projection finalization, and fault injection before migration
   workflows.
9. **P4 built-in identity.** Add bootstrap, password identity, activation,
   sessions, login, reset, and revocation.
10. **P4 administration.** Add Organization and user services and the first
    administration browser journey.
11. **P5 authorization.** Seed versioned roles, remove the server admin bypass,
    add Project grants, access review, and separation policies.
12. **P5 collaboration.** Add server revision conflicts and protected conflict
    drafts to one representative mutable form, then extend by command inventory.
13. **P6 preparation job.** Deliver the first durable worker, receipt recovery,
    progress page, and measured capacity gate.
14. **P6 remaining jobs.** Migrate each job family in the documented safety
    order and implement equal dynamic scheduling.
15. **P7 operating boundary.** Add connection profiles, egress policy, secrets,
    retention, deletion, backup, restore, observability, and runbooks.
16. **P8 pilot.** Execute the full multi-Organization release protocol and
    update current documentation only after acceptance.
17. **P9 federation.** Add and qualify corporate providers one at a time.

Do not combine the PostgreSQL registry split, request-identity refactor,
containerization, authentication, durable workers, and Odoo networking into one
unreviewable change. The target remains comprehensive; the delivery increments
remain evidence-backed and reversible where the underlying data permits it.

## Decisions that remain explicit gates

The parent plan lists the product and operational choices still requiring
acceptance. This delivery plan additionally requires named decisions for:

- the exact fact-ownership inventory and PostgreSQL projection boundary;
- the primary Debian or Ubuntu host release and supported CPU architecture;
- PostgreSQL driver, migration mechanism, backup method, and deployment model;
- password parameters, privileged-account multi-factor authentication, and
  break-glass policy;
- the server secret provider and Project-key deletion guarantee;
- the exact standard role-capability manifest and separation defaults;
- the fair scheduler's service accounting interval and global resource pools;
- approved remote Odoo network patterns for each deployment;
- legal hold when it conflicts with selected deletion; and
- recovery-point, recovery-time, backup-expiry, and incompatible-rollback
  rules.

Record each accepted choice in an ADR, security contract, or operating
runbook. Do not leave it implicit in environment variables, SQL migrations,
container defaults, or administrator folklore.
