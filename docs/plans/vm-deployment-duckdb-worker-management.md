# Linux server deployment with PostgreSQL control and DuckDB workers

## Status and decision

**Status:** Approved target architecture; Phase 1 implementation is in
progress. The current supported product remains the local Windows application.
No server deployment, PostgreSQL repository, Organization administration,
server authentication, or durable worker described here is implemented merely
by this document.

The
[shared server detailed phased implementation plan](linux-server-postgresql-duckdb-implementation.md)
turns this architecture into ordered work packages, proposed code boundaries,
persistence changes, verification requirements, and phase exit gates.
The Phase 0
[server fact-ownership inventory](../architecture/server-fact-ownership.md)
classifies every current durable fact before a PostgreSQL schema is introduced.

This revision replaces the earlier Entra-first, DuckDB-registry VM proposal. It
records the agreed target for a shared Impodo server that can run on a customer
premises or in a cloud VM:

- Impodo calls the customer tenancy boundary an **Organization**. Its canonical
  identifier is `organization_id`. An Odoo `res.company` is migration data and
  never establishes Impodo tenancy or access.
- PostgreSQL is the server control plane for Organizations, identities,
  memberships, roles, Project access, sessions, durable jobs, Workspace leases,
  and bounded workflow projections.
- DuckDB, Parquet, retained source files, and protected artifacts remain the
  data plane for migration data, transformation, comparison, execution, and
  detailed evidence.
- The first server identity phase uses Impodo-managed accounts and an
  administration dashboard. A later phase adds Microsoft Entra, Google, and
  other corporate identity providers.
- A customer user belongs to exactly one Organization. The first server
  profile does not support Organization switching or cross-Organization user
  memberships.
- The shared server runs on Linux, with Debian and Ubuntu as supported host
  families. Impodo provides a production `Dockerfile` and a Docker Compose
  deployment for the reference single-server topology.
- Local desktop and shared server installations use the same domain and
  application services through separate composition roots. Server concerns do
  not leak into local business rules, and local trust assumptions do not leak
  into the server.

The product priorities are usability for Organization administrators and data
managers, agility for new migration use cases, and equal product support for
local and server installations. Implementation difficulty may influence
delivery order, testing, or operational safeguards. It must not reduce the
target product model or force the server to inherit local single-user
limitations.

This plan does not prescribe an implementation branch. The former
`codex/server-duckdb-worker-management` branch does not exist in the current
checkout. Delivery work must record its actual base commit and branch when
implementation starts.

## Intended outcome

An Organization administrator signs in to a shared Impodo server and creates
the Organization's users. The administrator assigns understandable roles and
grants each user access to selected Projects. A data manager can then work on
an authorized Project, submit long-running work, leave the page, and return to
a durable result. A reviewer can approve governed decisions without receiving
data-management or execution authority merely because they can review them.

Another data manager can work in a different Workspace at the same time when
the server has available capacity. Impodo prevents two workers from changing
the same Workspace concurrently. A restart preserves completed evidence and
shows an interrupted operation accurately instead of reporting success or
silently repeating work.

The local desktop installation keeps the present low-friction experience. It
uses one implicit local operator, requires no Organization administration, and
continues to store its registry and migration data locally. Internally, the
local composition may provide one reserved local Organization scope when a
shared application contract requires it, but the local user does not manage
memberships or roles.

For the first shared-server release, a cloud VM and an on-premises server use
the same Linux container topology. Their network, certificate, backup, and
secret providers may differ, but their Impodo behavior does not.

## Deployment profiles

| Concern | Local desktop profile | Shared server profile |
| --- | --- | --- |
| User boundary | One implicit local operator. | Named users belong to an Impodo Organization. |
| Authentication | Loopback launch session. | Impodo-managed account first; corporate identity later. |
| Authorization | The local operator receives local capabilities. | PostgreSQL resolves Organization membership, Project access, roles, and capabilities for each request. |
| Control state | Local DuckDB registry and local adapters. | PostgreSQL repositories and durable coordination. |
| Data processing | DuckDB and Parquet on the local machine. | DuckDB and Parquet on worker-owned server storage. |
| Artifacts | Contained local filesystem. | Private persistent Linux block storage behind an artifact port. |
| Jobs | Local job adapter, which may execute in process. | Durable PostgreSQL queue, leases, and managed worker processes. |
| Secrets | Windows Credential Manager and local protected storage. | Server secret provider and encrypted protected storage. |
| Composition | `create_local_app` or an equivalent explicit entry point. | `create_server_app` or an equivalent explicit entry point. |

Shared domain and application code must not contain scattered `if local` or
`if server` decisions. Each profile supplies identity, authorization,
persistence, artifact, secret, job, clock, and deployment adapters at its
composition root.

## Target server boundary

```text
Organization user
        |
        | HTTPS and server session
        v
TLS reverse proxy
        |
        | private Docker network
        v
Impodo web application
        |-- PostgreSQL control plane
        |     |-- Organizations, users, memberships, and roles
        |     |-- Project catalogue and access grants
        |     |-- sessions, administrative audit, and security events
        |     `-- jobs, idempotency records, leases, and bounded status
        |
        |-- managed worker processes or containers
        |     `-- one fenced lease for each writable Workspace
        |
        `-- private persistent block storage
              |-- retained source files
              |-- Project, DataVersion, and Workspace DuckDB files
              |-- immutable Parquet artifacts
              `-- protected migration and execution evidence

Server secret provider                         Odoo target
keys and target credentials                    closed JSON-2 boundary
```

The reverse proxy is the only inbound HTTP component. PostgreSQL, workers, and
the application listener remain private. A DuckDB database must be opened only
from the worker or application process that owns its operation. Impodo must
not open a DuckDB database over NFS, SMB, object storage, a synchronized
folder, or another general-purpose network filesystem.

The initial reference topology uses one Linux server and attached block
storage. The design keeps artifact access behind a port so a later distributed
deployment can use object storage for immutable artifacts. A later deployment
may materialize a worker-local DuckDB database from those artifacts, but it
must not treat an object-store key as a shared DuckDB file.

## Storage ownership

Each fact has one authoritative owner. A bounded PostgreSQL projection may
help the browser list and filter work, but it must not become a second mutable
copy of detailed migration evidence.

| Information | Local authority | Shared-server authority |
| --- | --- | --- |
| Organization, user, membership, role, and Project-access records | Not applicable beyond the implicit local scope. | PostgreSQL. |
| Authentication identities, sessions, activation, reset, and revocation | Local launch-session adapter. | PostgreSQL plus server-side secret material. |
| Project catalogue, Organization ownership, visibility, and lifecycle summary | Local registry. | PostgreSQL. |
| Durable job, idempotency, lease, fencing, progress, and terminal status | Local job adapter. | PostgreSQL. |
| Bounded counts, hashes, storage keys, and current-revision pointers used by server pages | Local registry or source store. | PostgreSQL projection derived from committed data-plane evidence. |
| Original source bytes and accepted Data version artifacts | Local protected project storage. | Private server artifact storage. |
| Source rows, prepared rows, mappings, quality results, normalization, relationship evidence, comparisons, and detailed execution results | DuckDB, Parquet, and protected artifacts. | DuckDB, Parquet, and protected artifacts. |
| Full Recipe revision and qualification package | Protected local artifact. | Protected server artifact; PostgreSQL stores its catalogue metadata, hash, and storage key. |
| Odoo credentials and encryption keys | Windows Credential Manager and local protected storage. | Server secret provider; never ordinary PostgreSQL columns or container environment dumps. |

PostgreSQL must not become the transformation engine or a warehouse for source
and prepared rows. DuckDB and Parquet remain responsible for the analytical
operations that make Impodo useful to data managers.

## Cross-store command and recovery protocol

A server command may update PostgreSQL control state and DuckDB or artifact
state. Those stores cannot share one database transaction, so every such
command requires an explicit recovery contract:

1. PostgreSQL authorizes the actor for the Organization and Project. It creates
   or reuses an idempotent operation and records the exact input identity.
2. PostgreSQL grants one Workspace lease with a monotonically increasing
   fencing token. A stale worker cannot commit after a replacement worker has
   acquired a newer token.
3. The worker receives an immutable job manifest. It writes only its leased
   Workspace and stages new artifacts without replacing the previous current
   result.
4. The worker commits a durable data-plane receipt that identifies the
   operation, input hashes, output hashes, artifact keys, and fencing token.
5. The application verifies the receipt and artifacts before PostgreSQL moves
   the bounded projection and job to a terminal successful state.
6. If the application or worker stops, recovery compares PostgreSQL state with
   the data-plane receipt. It may finalize a proven commit or mark the job
   `INTERRUPTED`. It never infers success from a progress message and never
   repeats an Odoo write automatically.

This protocol applies to preparation and to every later background action.
Odoo capture, load, correction, and reconciliation retain their more specific
identity, approval, journal, and unknown-outcome safeguards.

## Organization, user, and identity model

An **Organization** contains memberships, role definitions, Project access
rules, security policy, and deployment-facing administrative settings. It does
not contain or derive its identity from an Odoo company. One Project belongs
to exactly one Organization on a shared server.

Each customer user also belongs to exactly one Organization. PostgreSQL
enforces one active Organization membership for that `user_id`, and every
session is bound to the same Organization. The first server profile has no
Organization switcher and no cross-Organization Project grant. A Platform
Administrator is a separate server operator and does not become a customer
Organization member automatically.

A stable Impodo `user_id` owns memberships, Project grants, and audit history.
Authentication credentials are separate identities attached to that user:

```text
User
|-- Impodo password identity                    first server phase
|-- Microsoft Entra issuer and subject          later identity phase
|-- Google issuer and subject                   later identity phase
`-- other approved identity-provider binding    later identity phase
```

Changing sign-in method must not create a new user or transfer authority by
matching an unverified email address. A federated identity is bound by its
verified issuer and subject. Impodo continues to own Organization membership,
Project access, roles, capabilities, and audit evidence after federation.

An external identity can bind to only one active Impodo user and therefore to
only one Organization. A person who needs to work for another Organization
must leave the first Organization or use a separately governed future feature;
the initial server does not offer simultaneous access to both.

### First server identity phase: Impodo-managed accounts

The first server release includes an administration dashboard. It does not
require Microsoft, Google, LDAP, or another external directory.

The initial bootstrap creates one Platform Administrator through a one-time
server-side procedure. There is no shipped default password and no public
self-registration. A Platform Administrator creates an Organization and its
first Organization Administrator. The Organization Administrator can then:

- create a user with a name and email address;
- activate, suspend, and reactivate the user without deleting historical
  actions;
- generate a one-time activation or password-reset link;
- assign Organization responsibilities;
- grant access to selected Projects and assign Project roles;
- create or revise Organization-specific role definitions; and
- review membership, role, session, and access-change history.

The account implementation must use a current password-hashing scheme, secure
server-side sessions, CSRF protection, login throttling, safe activation and
reset tokens, session revocation, and security-event audit. The production
security review must decide the mandatory multi-factor authentication policy
before an Internet-accessible deployment is accepted.

### Later identity phase: corporate sign-in

The second identity phase adds Microsoft Entra, Google Identity, and generic
OpenID Connect adapters. OAuth authorization alone is not treated as user
authentication. A customer that needs classic Active Directory, LDAP, or SAML
requires either a supported federation bridge or a separately qualified
adapter.

Corporate sign-in changes authentication, not Impodo authorization. Directory
groups may later map to Impodo roles, but Impodo records the resolved grant and
policy revision used for every governed action. Selected recovery accounts may
retain Impodo-managed authentication under an explicit break-glass policy.

## Roles and permissions for a shared server

### Authorization rules

- Application services check capabilities, never role names. A role is a
  versioned, understandable bundle of capabilities.
- Organization membership does not imply access to every Project. A user must
  have an active membership and an explicit Project grant or an explicitly
  governed Organization-wide role.
- The browser presents roles as the normal administrative action. An
  Organization Administrator may copy a standard role and adjust its
  capability set for a new use case.
- Direct user exceptions are advanced grants. They must be visible, carry a
  reason, optionally expire, and appear in access reviews. Impodo must not
  accumulate invisible permission checkboxes on individual users.
- Suspension or role revocation blocks new commands immediately. A worker
  rechecks authority when it starts and immediately before an Odoo write. A
  revoked queued job is cancelled or sent for review according to its action
  contract.
- Audit evidence records the stable actor, Organization, Project, effective
  capabilities, role or grant revision, policy revision, time, and exact
  governed input.
- Separation of duties is an operation rule, not merely a role label. Where a
  policy requires an independent reviewer, the actor who made the governed
  decision cannot approve or execute that same decision even if the actor has
  both capabilities.

The current `PROJECT_ADMIN` behavior grants every existing capability through
`Actor.has`. That shortcut is acceptable only for the trusted local operator.
It must not become the remote Project Manager policy. The server authorization
model must replace the universal bypass with explicit scoped capabilities.

### Proposed standard roles

| Role | Scope | Normal responsibility | Important exclusions |
| --- | --- | --- | --- |
| Platform Administrator | Server | Operates the service and provisions Organizations. | Receives no customer Project or protected-evidence access automatically. |
| Organization Administrator | Organization | Manages members, role definitions, security policy, and Project access. | Receives no migration-data or execution access automatically. |
| Project Manager | Project | Creates and manages the Project lifecycle, participants, runs, and Workspaces. | Cannot approve governed data decisions or execute an Odoo load by default. |
| Data Manager | Project | Selects source data, governs schema, maps fields, prepares data, resolves issues, and runs preflight checks. | Cannot approve their own governed decisions or execute an Odoo load. |
| Reviewer | Project | Reviews and approves resolution, normalization, Recipe qualification, cutover, and export decisions allowed by policy. | Cannot edit the reviewed input or execute the approved load by default. |
| Migration Operator | Project and target | Executes an already approved plan and performs required recovery or reconciliation actions. | Cannot change mappings or approve the plan being executed. |
| Recipe Steward | Organization or Project | Creates, publishes, retires, applies, and qualifies reusable Recipes within the granted scope. | Receives no Project data or Odoo execution access merely from owning Recipes. |
| Auditor | Organization or Project | Reads permitted history, approvals, job status, and audit reports. | Protected values, credentials, and raw sensitive evidence require a separate grant. |

Small Organizations may assign several roles to one person. The permission
engine still enforces any configured independent-review rule for the same
operation. The dashboard should show effective access in business language so
an administrator can answer who can view, change, approve, and execute work in
each Project.

### Capability groups

The existing business capabilities remain the starting vocabulary. Standard
roles group them by responsibility:

- Project lifecycle capabilities include `project.view`, `project.create`,
  `project.edit`, `project.register`, and the separately governed
  `project.delete`.
- Data-management capabilities include Data version, Migration run, Workspace,
  source, schema, mapping, coverage, resolution-decision,
  normalization-decision, and preflight capabilities.
- Recipe capabilities include `recipe.view`, `recipe.create`, `recipe.apply`,
  `recipe.publish`, `recipe.qualify`, and the separately governed
  `recipe.delete`.
- Review capabilities include `resolution.approve`, `normalization.approve`,
  `export_plan.approve`, `cutover_plan.qualify`, `cutover.select`, and
  `production_run.activate` as permitted by deployment and workflow policy.
- Execution capabilities include `export_plan.execute` and the relevant
  Migration run and Workspace commands. An execution capability does not grant
  approval or unrestricted credential access.
- Audit and sensitive-evidence capabilities include `audit.view`,
  `protected_evidence.read`, and `protected_evidence.manage`. These remain
  separate because ordinary audit access must not expose target identifiers or
  protected values.
- `local_stack.inspect`, `local_stack.start`, and `local_stack.stop` remain
  local-only and can never be assigned through a server role.

The shared server also requires new administrative capabilities. Exact symbols
must be fixed in the authorization contract before implementation. The minimum
set covers Organization viewing and management, membership management, role
definition and assignment, Project-access management, session revocation,
authentication management, target-credential management, and server job
administration. Server operations and customer-data access remain separate.

## Remote collaborative editing

Collaborative-edit conflict handling belongs only to the shared server
profile. The local desktop profile keeps its single-operator behavior and does
not acquire remote edit-conflict pages, remote revision checks, or PostgreSQL
coordination.

Every mutable server form carries the revision that the user opened. When the
user saves, PostgreSQL or the authoritative server command compares that
expected revision with the current revision before changing any state. If
another user has already saved a change, Impodo must not silently overwrite it.
It shows what changed, preserves the second user's submitted values as a draft,
and lets that user reload, compare, and deliberately reapply compatible work.

Background Workspace leases and browser edit revisions solve different
problems. A lease prevents two workers from writing the same Workspace. A
revision check prevents two people from unknowingly replacing each other's
configuration. Starting a background job also verifies that the submitted
configuration revision is still current.

## Linux and Docker deployment contract

The first reference deployment targets a single Linux server running a
supported Debian or Ubuntu release. The delivery must choose and continuously
test one primary distribution while keeping the container contract portable
across both host families.

The repository must provide:

- a multi-stage production `Dockerfile` that builds one immutable Impodo image
  for the web and worker commands;
- a pinned runtime base image and dependency lock, with the final image running
  as a non-root user and containing no build toolchain;
- a `compose.yaml` reference topology with reverse proxy, application,
  PostgreSQL, and worker services;
- health, readiness, and controlled-drain commands;
- explicit CPU, memory, process, temporary-space, and file-size limits;
- read-only container filesystems where practical, with explicit writable
  mounts for data and runtime state;
- separate persistent volumes or bind mounts for PostgreSQL data and the
  Impodo artifact root;
- configuration templates that contain no password, signing key, encryption
  key, target credential, or activation secret; and
- image provenance, vulnerability scanning, upgrade, rollback, backup, and
  restore instructions.

The reference paths should make ownership visible, for example:

```text
/var/lib/impodo/postgres       PostgreSQL control-plane volume
/var/lib/impodo/projects       DuckDB, Parquet, retained source, and evidence
/run/secrets/impodo            runtime-mounted server secrets
```

The exact paths may change, but the PostgreSQL and migration-data lifecycles
must remain separate. The application must refuse to start when a required
mount is missing or resolves to transient container storage.

TLS may terminate in the supplied reverse proxy or in an explicitly supported
upstream load balancer. Impodo trusts forwarded host, scheme, and client
information only from configured proxy addresses. PostgreSQL is never exposed
on the host's public interface.

## Remote Odoo network access

This section applies only to the shared server profile. It does not change the
local desktop connector, loopback Odoo access, or local-stack controls.

An Organization Administrator creates an approved Odoo connection profile for
an Organization and environment. A data manager or migration operator selects
that approved profile; they do not turn an arbitrary URL into worker network
access. The profile identifies the Odoo host, database, permitted company
context, supported Odoo major, network route, trust configuration, and whether
the credential is read-only or write-capable. Credentials remain in the server
secret provider and are not stored in the profile.

The first shared server supports these network patterns:

1. **Public HTTPS with a fixed Impodo egress address.** The customer allowlists
   the server's egress address on the Odoo side. This is the normal cloud-to-
   cloud path.
2. **Site-to-site VPN or private cloud networking.** Impodo reaches a private
   Odoo address through customer-approved routing. This is the preferred path
   when a cloud Impodo server must reach an on-premises Odoo target.
3. **Customer HTTP CONNECT proxy.** The worker uses a named outbound proxy when
   direct routing or VPN connectivity is unavailable. Proxy credentials and
   trust roots use the server secret provider.
4. **Direct private-network access.** An on-premises Impodo server reaches an
   Odoo target on the same governed customer network.

Every pattern remains outbound from Impodo to Odoo. Impodo does not require an
inbound Odoo callback and does not create a generic reverse tunnel. If a future
customer cannot provide one of these paths, a separately designed outbound
site connector may be considered; it is not part of the first server release.

The server denies network access by default. It allows only the approved host,
port, protocol, and resolved address ranges for the selected profile. It
rejects redirects, DNS rebinding, loopback and link-local destinations,
unapproved private ranges, and changes to the resolved target after approval.
HTTPS certificate validation remains mandatory. A customer that uses a private
certificate authority installs its approved CA chain rather than disabling
verification.

A worker receives only the network profile and purpose-specific credential for
its authorized job. Read and write credentials remain separate. Before a job
contacts Odoo, Impodo rechecks the Organization, Project, environment, target,
principal, company context, model scope, and credential generation. Logs and
health pages may identify the connection profile and outcome, but never expose
credentials, tokens, source values, or Odoo error bodies containing protected
data.

## Remote resource sharing

Remote worker capacity is shared dynamically and equally between active
Organizations. The initial policy gives every Organization the same weight;
there is no paid or administrative priority class.

An Organization is active while it has a running or runnable job. When only
one Organization is active, its eligible jobs may use all available worker
slots and their permitted CPU and memory. When several Organizations are
active, a work-conserving fair scheduler dispatches new jobs approximately
equally between them. Unused capacity is borrowed automatically. A newly
active Organization receives priority in subsequent dispatch decisions until
the running allocation becomes fair; Impodo does not terminate safe work that
is already running merely to rebalance slots.

Fairness applies after safety restrictions. A Workspace lease, an exclusive
Odoo execution boundary, or a per-job memory limit may keep capacity idle that
another job cannot safely use. The scheduler records its allocation decision
so an administrator can explain why a job is queued.

Disk space cannot be reclaimed as quickly as a worker slot. Each Organization
therefore has an equal soft share of the configured artifact capacity and may
borrow unused space while the server retains a global emergency reserve.
Borrowed data is never deleted merely because another Organization becomes
active. If the reserve is reached, Impodo blocks new uploads and jobs that
would grow storage, preserves existing evidence, and directs the administrator
to delete eligible data or add capacity.

## Remote Project retention and deletion

The shared server lets the Organization choose how long Impodo retains a
Project. The local desktop profile remains under the local user's filesystem
control and does not receive server retention automation.

At Project creation, an authorized user chooses one of these policies:

- **Delete after verified Odoo load.** After the load and reconciliation are
  fully verified, Impodo asks for an explicit irreversible confirmation and
  then deletes the Project-owned migration data.
- **Delete after 90 days of inactivity.** This is the proposed server default.
  Project activity restarts the countdown, and the dashboard shows the planned
  deletion date before deletion begins.
- **Keep until manually deleted.** The Organization keeps the Project until an
  authorized user requests deletion, subject to any clearly disclosed maximum
  retention policy for that deployment.

Before accepting either automatic or manual deletion, Impodo explains that the
Project cannot be reopened, compared, retried, audited in detail, or used to
reproduce a migration after deletion. The user can export the permitted final
reports and portable Recipe material first. A reusable Recipe is a separate
Organization object and is not deleted with one Project unless the user also
selects it or deletes the complete Organization.

Project deletion removes its PostgreSQL catalogue and access state, source
files, DuckDB databases, Parquet artifacts, protected evidence, job payloads,
and Project-scoped secrets. Impodo retains only a minimal non-customer-data
deletion receipt containing the opaque Project and Organization identifiers,
policy, requesting actor, deletion time, and completion status.

Deletion from active storage does not by itself remove every historical backup
copy. The confirmation must show the backup-expiry date. The backup design
should use Project-scoped encryption keys when practical so destroying the key
can make remaining backup bytes unreadable. Impodo must not claim complete
physical erasure until active storage, protected keys, replicas, and expired
backups have all satisfied the documented deletion contract.

## Worker, storage, and capacity boundaries

1. One worker may write one leased Workspace at a time. Different workers may
   operate concurrently only on different Workspace databases and artifact
   staging paths.
2. PostgreSQL owns the job and lease state. It does not make DuckDB safe for
   concurrent writers; the fencing and filesystem boundaries must enforce that
   ownership independently.
3. The first deployment starts with one preparation worker. It enables a
   second worker only after representative measurements prove useful throughput
   without memory, CPU, disk, or DuckDB lock failures.
4. DuckDB and Polars thread counts must be tuned with the worker count. A
   four-vCPU VM does not justify four concurrent jobs automatically.
5. Odoo capture, load, correction, and reconciliation use action-specific job
   contracts. Odoo writes remain conservative, journaled, and fail closed on
   unknown outcomes.
6. A Project is authoritative in one installation at a time. Local-to-server
   transfer is a governed export and import that preserves identities and
   hashes. This plan does not provide live bidirectional synchronization or a
   shared Project folder between local and server installations.
7. Browser edit-revision conflicts, fair Organization scheduling, and automatic
   retention are remote-only policies. They do not change the local desktop
   composition.

## Delivery phases

### Phase 0 - Freeze contracts and protect the local baseline

**Purpose.** Establish the target boundaries and prove that later refactoring
does not change the current local workflow accidentally.

**Work.**

- Record the implementation base commit and the current local acceptance and
  architecture test baselines.
- Add or revise architecture decisions for the Organization boundary,
  PostgreSQL control plane, DuckDB and Parquet data plane, built-in first-phase
  authentication, and Linux container deployment.
- Classify every current registry table and repository fact as local-only,
  PostgreSQL control state, detailed DuckDB evidence, protected artifact, or
  bounded projection.
- Define the cross-store operation receipt, lease, fencing, and recovery
  contracts before moving mutable state.

**Exit gate.** Reviewers can identify one authority and one recovery rule for
every migrated fact. Current local tests remain unchanged and passing.

### Phase 1 - Extract explicit local and server compositions

**Purpose.** Keep one maintainable product without creating a lowest-common-
denominator deployment.

**Work.**

- Separate the current large web composition into shared construction, local
  composition, and server composition.
- Introduce a request-scoped authenticated principal and a Project-scoped
  authorized actor or authorization receipt. Remove reliance on one global web
  actor from shared request handling.
- Make Project-list queries authorization-aware. A global `project.view`
  capability must not reveal every Project on a shared server.
- Keep domain and application services independent of PostgreSQL, Docker,
  external identity providers, and filesystem paths.
- Implement the local adapters first against the new ports so current local
  behavior remains the executable compatibility baseline.

**Current implementation note, 2026-10-25.** The local and fail-closed server
entry points now share an actor-free route context and common HTTP installer.
Deployment-neutral builders now wire the Project foundation and the complete
source and Recipe authoring, Match data, Recipe application, preparation, and
final-review service groups, together with integrated run planning and setup,
from injected dependencies. They also wire reviewed loading, transfer
execution, reconciliation, protected reconciliation evidence, and fallout
workbooks. The builders require application-owned ports; the local composition
supplies the existing DuckDB repositories, governed artifacts, authorization,
Odoo provenance, and deployment-specific credential callback. A shared
top-level application graph now joins those groups, and one shared context
builder maps explicit request, job, infrastructure, and target runtime bundles
into the browser context. The launch token remains local, and the server may
omit the explicit local Odoo runtime instead of constructing a false local
adapter. Shared routes and that graph now depend on structural job-runtime
ports instead of concrete local manager classes. A deployment-neutral support
builder constructs browser queries, owner views, navigation, source intake and
inspection, derived-data authoring, supporting lookups, Recipe target matching,
preparation recovery, and connection testing from explicit ports. The local
composition still selects the in-process manager implementations, DuckDB
repositories, and filesystem artifacts. Its current closure-based enqueue
calls are a compatibility boundary; server workers still require serializable
commands, authorization receipts, leases, and fencing. Artifact-store
construction, Odoo target factories, and correction and Cutover qualification
artifact construction remain in the local composition. Integrated Test
qualification now reuses the shared foundation, preparation, and execution
graphs while the local deployment still owns its protected Project evidence
store and DuckDB Cutover repository. All PostgreSQL
server adapters are still planned. Completed-load correction orchestration now
uses a deployment-neutral service graph as well. The local composition injects
the DuckDB correction repository, protected correction evidence, native Polars
review pipeline, and a deployment-owned target adapter. That adapter resolves
the current secret and creates a scope-bound Odoo reader only when the review
runs; the shared graph never receives credentials or caches a client. The
current closure-based correction job manager remains local-only. The server
will use the common durable job, authorization-receipt, lease, and fencing
contracts instead of adapting that process-local manager.

Odoo source capture, protected provenance, and atomic publication now form one
deployment-neutral `OdooSourceServiceGraph` built from an explicit
`OdooSourcePorts` bundle. The local composition supplies its existing DuckDB,
artifact, secret, filter, and codec implementations. A fake-server composition
test assembles the complete application graph from fake ports with no local
Odoo runtime and rejects imports of DuckDB, local identity, local compatibility
storage, and concrete in-process job managers from the shared graph. The
unavailable-Project browser projection is also neutral; the DuckDB/filesystem
compatibility migration remains local-only. This closes the remaining shared
Odoo-source construction dependency but does not yet provide PostgreSQL
adapters or enable server startup.

**Exit gate.** Local Impodo starts through the explicit local composition and
passes its focused workflow suites. Architecture tests reject direct adapter
dependencies and scattered deployment-mode branching.

**Current exit-audit result, 2026-10-25.** The Odoo-source and fake-server
composition checks pass. The complete architecture suite passes 66 of 68 cases;
the remaining failures require splitting the oversized shared workflow style
sheet and historical source-workflow test module. Phase 1 remains open until
those maintainability limits pass, so the Linux container phase has not been
formally opened.

### Phase 2 - Deliver the Linux container baseline

**Purpose.** Create a reproducible server package before enabling multi-user
behavior.

**Work.**

- Add the production `Dockerfile`, Compose topology, reverse proxy,
  PostgreSQL service, application command, and worker command.
- Qualify the image on the selected Debian and Ubuntu host versions.
- Prove non-root execution, persistent mounts, private networking, health,
  readiness, controlled shutdown, and restart behavior.
- Keep Odoo transport disabled in the empty deployment rehearsal.

**Exit gate.** A clean supported Linux host can build or pull the pinned image,
start the stack, pass HTTPS health checks, restart it, and prove that both
persistent stores survive.

### Phase 3 - Implement the PostgreSQL control plane

**Purpose.** Make shared tenancy, access, catalogues, jobs, and coordination
durable without moving migration processing into PostgreSQL.

**Work.**

- Implement PostgreSQL repositories for Organizations, users, memberships,
  role definitions, grants, Project catalogue, sessions, security audit, jobs,
  idempotency, leases, and bounded projections.
- Enforce exactly one active Organization membership for each customer user
  and bind every server session to that Organization.
- Add forward-only PostgreSQL migrations and a deployment migration command.
- Implement the operation-receipt and projection-finalization protocol.
- Add contract suites that run the same repository, artifact, authorization,
  and job behaviors against the applicable local and server adapters.

**Exit gate.** PostgreSQL loss or application restart cannot manufacture a
successful data-plane result. A data-plane commit can be reconciled without
duplicating work.

### Phase 4 - Add built-in accounts and the administration dashboard

**Purpose.** Let an administrator operate the first shared server without an
external identity provider.

**Work.**

- Implement one-time Platform Administrator bootstrap, login, activation,
  reset, session revocation, throttling, and security-event audit.
- Add Organization, user, membership, role-definition, Project-access, and
  effective-access pages.
- Omit an Organization switcher. The administrator moves or deactivates the
  user rather than granting simultaneous access to another Organization.
- Make suspension and revocation effective for new commands and queued work.
- Provide an access-review view that answers who can view, change, approve,
  execute, administer, and inspect sensitive evidence for each Project.

**Exit gate.** An administrator can create an Organization and users, assign
roles and Projects, revoke access, and inspect the resulting audit history.
Cross-Organization and unauthorized Project requests fail closed in service,
repository, route, and browser tests.

### Phase 5 - Enforce scoped roles and separation of duties

**Purpose.** Make the proposed roles useful to data managers without relying on
route visibility as a security control.

**Work.**

- Replace the server-side `PROJECT_ADMIN` universal bypass with explicit
  scoped capabilities.
- Seed the standard roles and allow versioned Organization-specific roles.
- Bind approvals and executions to effective grants and policy revisions.
- Enforce independent-review policies by comparing actors on the exact
  governed operation.
- Recheck sensitive authorization at worker start and immediately before an
  Odoo write.
- Add remote-only optimistic revision checks to mutable Project and Workspace
  forms. Preserve a conflicting submission as a draft and show both revisions
  instead of silently overwriting either user's work.

**Exit gate.** Role-matrix tests cover every state-changing command. Tests
prove that combined roles do not bypass configured separation of duties and
that a Platform or Organization Administrator has no implicit customer-data
access.

### Phase 6 - Make all long-running work durable

**Purpose.** Let users leave and return without losing trustworthy job state.

**Work.**

- Move preparation to the PostgreSQL job, lease, fencing, and receipt model.
- Bring Odoo capture, preflight, Recipe application, load, correction, and
  reconciliation under action-specific durable job contracts.
- Show queued, running, review-required, cancelled, failed, interrupted, and
  successful states with one safe next action.
- Implement equal-weight, work-conserving scheduling between active
  Organizations. Allow one active Organization to use otherwise idle capacity
  and rebalance through later dispatches when another becomes active.
- Benchmark one and two representative preparations and record elapsed time,
  peak memory, CPU, disk use, connection counts, and lock failures.

**Exit gate.** Restart, cancellation, stale worker, duplicate request, changed
authority, and unknown Odoo response tests preserve the current evidence and
never duplicate a write.

### Phase 7 - Complete server security, backup, restore, and operations

**Purpose.** Make the deployment operable without developer knowledge.

**Work.**

- Select the production secret provider and define key ownership, rotation,
  backup, and recovery.
- Implement approved remote Odoo connection profiles, purpose-specific network
  routes, default-deny egress, private-CA trust, and fixed-target validation.
- Define consistent PostgreSQL and artifact backup checkpoints. Rehearse a
  restore to a separate server with Odoo transport disabled until validation.
- Implement Project retention selection, deletion warnings, the 90-day
  inactivity countdown, complete active-store deletion, backup-expiry
  disclosure, and the minimal deletion receipt.
- Add privacy-preserving metrics and logs for application health, queue state,
  worker exits, capacity, access changes, backup results, and security events.
- Write install, upgrade, rollback, certificate, backup, restore, secret
  rotation, worker-limit, incident, and Organization offboarding runbooks.

**Exit gate.** An administrator who did not build Impodo can deploy, upgrade,
drain, restart, back up, restore, rotate secrets, and revoke an account from the
runbooks. Restored evidence retains its identities and hashes.

### Phase 8 - Pilot and release the first server profile

**Purpose.** Validate usability and safety with realistic Organizations and
data managers.

**Work.**

- Run a pilot with an Organization Administrator, two data managers, a
  reviewer, a migration operator, and an auditor using separate test accounts.
- Exercise overlapping work in separate Workspaces, conflicting work in one
  Workspace, simultaneous form edits, role change, account suspension,
  session expiry, fair sharing between Organizations, sole-Organization burst
  capacity, each supported Odoo network pattern, retention deletion, restart,
  disk pressure, backup, and restore.
- Evaluate whether users can understand access, queue state, interruption,
  review, execution, and recovery without developer help.
- Record the measured server size, worker limit, recovery objectives, and
  accepted operational constraints.

**Exit gate.** The pilot demonstrates correct Organization isolation, Project
visibility, role behavior, durable work, recovery, and usable administration.

### Phase 9 - Add corporate identity providers

**Purpose.** Integrate the accepted server authorization model with customer
identity systems without redesigning users or permissions.

**Work.**

- Add Microsoft Entra, Google Identity, and generic OpenID Connect adapters in
  the order required by actual deployments.
- Add explicit identity linking, unlinking, provider disablement, and
  break-glass recovery procedures.
- Add optional directory-group mapping with conflict, revocation, and audit
  semantics.
- Test issuer, audience, signature, expiry, nonce, callback state, account
  linking, domain restrictions, group overage, and provider outage behavior.

**Exit gate.** Corporate sign-in changes only the authentication route. The
same Impodo user retains the same audited memberships and grants, and a failed
or removed provider cannot create or inherit authority by email matching.

## Delivery-wide acceptance criteria

The first shared-server profile is complete only when all of the following are
true:

1. A supported Debian or Ubuntu server runs the pinned Impodo image behind
   HTTPS with private application, PostgreSQL, and worker networking.
2. An administrator can create an Organization, users, roles, and Project
   grants without an external identity provider.
3. Each customer user belongs to exactly one Organization, and no session,
   query, artifact request, or job can switch that user into another one.
4. PostgreSQL is authoritative for shared control state, while DuckDB, Parquet,
   retained files, and protected artifacts remain authoritative for detailed
   migration data and evidence.
5. Local desktop Impodo continues to run without PostgreSQL, server login,
   Organization administration, collaborative edit checks, fair-server
   scheduling, or automatic server retention.
6. Organization and Project isolation is enforced below the browser layer for
   reads, commands, jobs, artifacts, and protected evidence.
7. Every cross-store operation has an idempotent request, exclusive fenced
   ownership, a durable receipt, and deterministic recovery.
8. Concurrent remote edits cannot silently overwrite a newer Project or
   Workspace revision.
9. Roles are usable permission bundles, custom roles are versioned, sensitive
   grants are visible, and configured separation of duties survives combined
   role assignments.
10. Restart, cancellation, stale workers, access revocation, and unknown Odoo
   outcomes preserve the existing fail-closed evidence rules.
11. One active Organization can use idle worker capacity, while several active
    Organizations receive equal dispatch opportunity and cannot consume the
    global disk reserve.
12. Every remote Odoo connection follows an approved outbound network profile
    and rejects an unapproved or changed destination.
13. A user can select immediate post-load deletion, the default 90-day
    inactivity policy, or manual retention and sees the irreversibility and
    backup-expiry consequences before confirming.
14. Backup and restore recover PostgreSQL and migration artifacts to one
   consistent checkpoint, including the keys required to read protected
   evidence.
15. A realistic pilot confirms the administration, data-management, review,
    execution, audit, and recovery experience before server availability is
    described as current product behavior.

## Decisions still required before implementation

This plan fixes the product and ownership direction. The following choices
still need explicit acceptance during the named delivery phases:

- the primary tested Debian or Ubuntu host release and supported CPU
  architecture;
- the reverse proxy and certificate-management implementation;
- the PostgreSQL deployment model for cloud and on-premises installations;
- password policy, multi-factor authentication, activation delivery, and
  break-glass rules for Impodo-managed accounts;
- the production secret provider and encryption-key recovery model;
- default role-capability mappings and which approvals require independent
  actors;
- backup frequency, retention, recovery-point objective, and recovery-time
  objective;
- the total worker and storage pools, per-job ceilings, global emergency disk
  reserve, and operational expansion thresholds;
- backup expiry and the Project-key design needed to meet the deletion promise;
- the customer-specific remote Odoo network pattern and trust roots;
- legal-hold behavior when it conflicts with a user's selected deletion
  policy; and
- the governed local-to-server import and export format.

These are product, security, and operating decisions. They must not be hidden
inside Docker configuration or left for an adapter implementation to decide.
