# VM deployment with DuckDB and managed workers

## Status and decision

**Status:** Proposed implementation plan. No server deployment, authentication,
database migration, or worker behaviour has been changed by this document.

This plan prepares Impodo for one internal VM running Docker. It supports a
small approved group of data managers who may work on separate Projects at the
same time. The first server release keeps Impodo's present file and DuckDB
ownership model. It does not add PostgreSQL, SQLite, object storage,
multi-tenancy, Google sign-in, or a custom username-and-password system.

The implementation branch is `codex/server-duckdb-worker-management`. All
changes described here belong on that branch until the pilot has been accepted.
The branch begins with this plan only; it does not authorize a merge or a
Production deployment.

The decisive constraint is worker ownership. DuckDB can support several
preparation processes when each process owns a different workspace database.
It must not be used as a shared read-write database for independently deployed
web or worker processes. One Impodo application process therefore owns the
shared registry and job coordinator. A preparation child process owns the
workspace that it was assigned for the duration of its job.

## Intended outcome

A data manager signs in through the company's Microsoft Entra tenant over the
internal HTTPS address. The manager can create or work in an authorized
Project, submit preparation, leave the progress page, and return later.
Another manager can prepare a different Workspace at the same time when the
VM has available capacity.

Impodo continues to retain the original source files, immutable source and
prepared snapshots, protected evidence, and the current DuckDB stores on a
private persistent VM volume. The files are not stored as database BLOBs and
are not copied to object storage. Odoo remains a separate system accessed only
through the existing closed JSON-2 boundary.

The initial operational target is **one web application process and at most
two concurrent preparation workers**. A VM with four vCPUs does not by itself
justify four simultaneous preparation jobs. The final capacity is a measured
decision because source parsing, Polars transformations, hashing, Parquet
publication, and DuckDB commits all compete for CPU, memory, and local disk.

## Target boundary

```text
Approved internal user
        |
        | HTTPS and Microsoft Entra sign-in
        v
Internal reverse proxy
        |
        | private HTTP on the Docker network
        v
One Impodo application process
        |-- owns registry.duckdb, sessions, authorization, and the durable job queue
        |-- starts at most two preparation child processes
        |       `-- each child owns one leased Workspace and its local artifacts
        |
        `-- persistent local VM volume
                |-- registry.duckdb
                |-- Project, DataVersion, and Workspace DuckDB files
                |-- original source files and immutable Parquet artifacts
                `-- protected evidence and server-safe secrets

Microsoft Entra                         Odoo 19
identity boundary                       JSON-2 boundary
```

The reverse proxy is the only public-facing component. The application has no
direct Internet listener. The Docker volume must be backed by local VM block
storage, not a file share, a synchronised folder, or a network-attached drive.

## Non-negotiable boundaries

1. A Project remains the lineage root. A DataVersion continues to own the
   immutable source package. A Workspace continues to own operational evidence.
   A Recipe remains portable meaning and must not acquire users, credentials,
   source rows, target IDs, approvals, or job state.
2. The existing source files remain the retained source bytes. The server does
   not make an additional canonical database copy and does not add hashing
   evidence beyond the current workflow.
3. A worker may write only the Workspace and artifact paths it has leased. Two
   workers may never prepare the same Workspace concurrently.
4. The application process is the only writer of shared registry and job
   coordination state. A child worker communicates progress and a terminal
   result to that process; it does not become a second registry writer.
5. An interrupted preparation preserves earlier completed evidence and becomes
   safely retryable. It never becomes successful merely because a progress
   message was received.
6. A first release is a single application instance. It must not run multiple
   Uvicorn workers, multiple application replicas, or independent worker
   containers against the same DuckDB volume.

## Phase 0 — Establish the isolated delivery baseline

**Purpose.** Keep this deployment work separate from current product
development and establish the exact local baseline before changing behaviour.

**Work.**

- Keep all implementation work on `codex/server-duckdb-worker-management`.
- Record the base commit, dependency lock, current focused preparation tests,
  and the current project-root layout in a delivery note.
- Confirm which current project stores need to be represented in a rehearsal.
  Inspect them without mutating or deleting an incompatible store.
- Create a small decision record that fixes the initial topology: one VM, one
  application process, DuckDB stores on a local persistent volume, Microsoft
  Entra single-tenant sign-in, and no PostgreSQL in this release.

**Risk.** The branch may silently absorb unrelated refactoring or the plan may
describe a local-only assumption as though it were server-ready.

**Success criteria.** The branch has a reviewed baseline, the worktree is
clean apart from intentional delivery changes, and each later pull request can
name the phase and acceptance criterion it implements.

## Phase 1 — Define the VM and Docker deployment contract

**Purpose.** Turn the target boundary into an unambiguous, testable deployment
contract before composing containers.

**Work.**

- Define a Compose topology with an internal HTTPS reverse proxy and one
  Impodo application service. Bind only the proxy to the internal network.
- Define the persistent project root, for example
  `/var/lib/impodo/projects`, as a private Docker mount and set
  `IMPODO_PROJECT_ROOT` explicitly. Preserve the complete existing layout:
  registry, Project stores, DataVersion stores, Workspace stores, original
  files, artifacts, and protected evidence.
- Define VM user and container UID/GID ownership, restrictive mount
  permissions, required free disk space, Docker restart policy, health check,
  and a controlled application shutdown sequence.
- Define separate configuration for development, VM rehearsal, and accepted
  internal deployment. Development must never accidentally point at the VM
  project root.
- Specify the initial process limits: one application process, one web worker,
  and one preparation worker until the performance phase authorizes two.

**Risk.** An incorrectly mounted root can make a container appear healthy while
writing transient data, or can expose governed files to another host user.

**Success criteria.** A fresh VM rehearsal can start the empty stack, pass a
health check through HTTPS, restart without losing its project root, and prove
that the application has no listener other than the reverse proxy.

## Phase 2 — Add Microsoft Entra authentication and internal authorization

**Purpose.** Replace the local-launch identity boundary with the company's
Microsoft identity boundary without introducing local passwords.

**Work.**

- Register a single-tenant Entra web application for the internal Impodo URL.
  Use the authorization-code flow with PKCE, state, and nonce validation.
- Restrict access through Entra application assignment. Use one explicitly
  assigned Entra group or application role for approved Impodo users. The
  initial group is intentionally small; no organization or tenant model is
  added to Impodo.
- Validate the token issuer, tenant, audience, signature, expiry, nonce, and
  callback state. Map the stable Entra issuer and subject to Impodo's existing
  actor identity and audit boundaries.
- Create, renew, and end a server-side browser session with secure cookie,
  CSRF, and same-origin protections. Trust forwarded host and HTTPS headers
  only from the named reverse proxy.
- Provide a plain denied-access page. User creation, password changes, and
  password recovery remain Microsoft responsibilities, not Impodo features.

**Risk.** A proxy-header or token-validation mistake can create an access
bypass. Group claims can also be too large or unavailable if the Entra setup is
not chosen deliberately.

**Success criteria.** An assigned employee can sign in and retain the correct
actor identity. An unassigned employee, a modified token, an incorrect tenant,
and a direct application request all fail closed. Existing capability checks
remain in force after sign-in.

## Phase 3 — Make project storage, secrets, backup, and restore server-safe

**Purpose.** Keep the current retained files and stores recoverable on a VM.

**Work.**

- Retain original source bytes and immutable artifacts exactly once in the
  project root. Continue to use local file references and DuckDB metadata;
  do not add BLOB storage or object storage.
- Define a server-compatible protected-secret backend before moving any real
  Odoo credentials. Entra configuration, session-signing keys, and protected
  evidence keys must come from Docker secrets or a host-managed secret store,
  never from the image, source tree, or ordinary logs.
- Define encrypted-at-rest VM storage, backup frequency, retention, access
  control, and an audited restore procedure. A consistent backup covers the
  DuckDB files and their referenced artifact directories as one unit.
- Quiesce new jobs before a filesystem snapshot or stop the application for a
  backup that cannot provide a consistent volume snapshot. Do not copy live
  database files and artifact directories independently.
- Rehearse restoration to a separate root. Verify the restored stores,
  original files, source snapshots, and protected evidence before allowing the
  restored instance to contact Odoo.

**Risk.** A backup can be internally inconsistent even when every copied file
exists. A server deployment can also accidentally replace the current
credential-vault assumptions with plaintext configuration.

**Success criteria.** A documented restore recreates a representative Project
without changing its source bytes or evidence links. The restored instance
starts with Odoo transport disabled until an administrator explicitly enables
the correct target configuration.

## Phase 4 — Make preparation coordination durable and workspace-scoped

**Purpose.** Evolve the existing local `PreparationJobManager` into a
single-application server coordinator without changing preparation semantics.

**Work.**

- Persist a small preparation-job record and a Workspace lease in the existing
  registry. The record contains identity, requested actor, build contract,
  workspace, phase, timestamps, worker ownership, and terminal outcome. It
  contains no source values, credentials, or new row-level hash material.
- Support `QUEUED`, `RUNNING`, `SUCCEEDED`, `REVIEW_REQUIRED`, `CANCELLED`,
  `FAILED`, and `INTERRUPTED` states. An application restart marks an active
  job as `INTERRUPTED`; it does not automatically rerun it.
- Have the parent application process acquire and release the Workspace lease,
  write registry job state, and verify durable workspace evidence before it
  declares a job successful.
- Pass the authorized immutable Workspace identity to the child as today. Make
  the child operate only on its Workspace and DataVersion stores. Remove any
  repeated shared-registry access from the long-running child path; the parent
  lease is the concurrency authority.
- Reject or clearly queue browser actions that would change the leased
  Workspace's mapping, source selection, or preparation inputs. Allow normal
  work in a different Workspace.
- Preserve current forward-only DuckDB schema upgrades and fail closed for an
  unsupported historical store. A server migration must never rewrite an old
  store merely to make it appear current.

**Risk.** A lease or terminal-event race could permit two children to write the
same Workspace, or could mark partial evidence as complete after a restart.
Moving the job record carelessly could also create a cross-store transaction
that has no recovery rule.

**Success criteria.** Focused tests prove that one Workspace has at most one
active job; two separate Workspaces can run; a duplicate request is
idempotently returned or queued; a restart produces `INTERRUPTED`; and the
parent verifies the resulting durable run before releasing the lease. A test
also proves that a preparation child does not need read-write access to
`registry.duckdb`.

## Phase 5 — Add bounded scheduling and resource controls

**Purpose.** Use available VM capacity without making concurrent preparation
slower or less reliable than a queue.

**Work.**

- Expose an explicit `preparation_max_workers` deployment setting. Keep the
  default at one through the first rehearsal, then raise it to two only after
  the benchmarks below pass. Do not enable three or four workers by default.
- Keep a global FIFO preparation queue and the per-Workspace lease from Phase
  4. Show each queued job's position and clear next action without exposing
  internal storage details.
- Set container and child-process CPU, memory, temporary-space, and file-size
  limits. Tune DuckDB and Polars thread counts together so two jobs do not
  oversubscribe a four-vCPU VM. Treat the existing DuckDB connection limits as
  starting safeguards, not a full per-job memory budget.
- Reserve capacity for the web process, source-file validation, and disk flush.
  A preparation job may be CPU and disk intensive even when its DuckDB writes
  are scoped to one Workspace.
- Benchmark one and two realistic representative preparations, including
  hashing, transformations, Parquet publication, quality, and normalization.
  Measure elapsed time, peak memory, CPU use, disk space, and lock failures.
- Keep Odoo capture, Odoo load, and correction application out of the general
  preparation pool until their own durable job contracts are implemented.

**Risk.** Raising concurrency can exhaust RAM or saturate disk I/O, making two
jobs slower than one. A process count can also hide thread oversubscription.

**Success criteria.** Two representative preparations on separate Workspaces
finish with correct, isolated evidence, no cross-process DuckDB lock failure,
and no resource limit breach. The recorded benchmark shows that the chosen
limit improves useful throughput. If it does not, the accepted initial limit
remains one.

## Phase 6 — Bring the remaining long-running actions under the same model

**Purpose.** Avoid a server where preparation is restart-safe but Odoo capture,
load, or correction progress disappears with the web process.

**Work.**

- Inventory the current in-memory Odoo capture, load, and correction job
  managers. Define a durable job and lease contract for each action before
  moving it to background execution.
- Apply action-specific limits. Preparation can use two independent Workspace
  slots after qualification. Odoo writes and correction application require a
  conservative exclusive scope and retain their existing journal and exact
  reconciliation rules.
- Keep worker messages as progress projections. The durable preparation,
  execution, journal, and reconciliation evidence remains the authority for a
  terminal result.
- Update progress pages so a data manager can leave, sign in again, and see a
  safe status, a clear stopped state, and the correct retry or review action.

**Risk.** Treating every action as a generic queue job could weaken the Odoo
write boundary, hide an unknown outcome, or bypass an existing explicit
confirmation.

**Success criteria.** Restart and cancellation tests cover every long-running
action. An uncertain Odoo write still stops for reconciliation, and no
background action can run twice merely because the browser reconnects.

## Phase 7 — Add the server operations runbook and security checks

**Purpose.** Make the VM operable by an internal administrator without relying
on local-developer knowledge.

**Work.**

- Write the Docker deployment, Entra configuration, backup, restore, upgrade,
  secret rotation, worker-limit, and incident runbooks.
- Add health, readiness, and controlled-drain behaviour. A drain stops new
  jobs, lets safe work reach a checkpoint, and records interrupted work before
  a forced restart.
- Add privacy-preserving operational logs and metrics: application health,
  queue length, job state, duration, worker exits, disk capacity, and backup
  result. Do not put source values, Odoo credentials, access tokens, or raw
  protected evidence in logs or metrics.
- Define update ordering: backup, drain, verify the application build and
  DuckDB compatibility, apply the forward migration, smoke test, then reopen
  new work. Include rollback rules that do not overwrite a newer project root.

**Risk.** Operational convenience can unintentionally expose data through logs,
health endpoints, backups, or a broad reverse-proxy rule.

**Success criteria.** An administrator who did not build the feature can
perform a tested deployment, controlled restart, backup, restore rehearsal,
and secret rotation from the runbook. Security review confirms that direct
application access and unassigned Entra access are blocked.

## Phase 8 — Pilot, capacity decision, and release gate

**Purpose.** Prove the chosen architecture with the intended two-person
internal use before treating it as the normal deployment.

**Work.**

- Run a pilot with two approved users on separate Projects and Workspaces.
- Exercise two overlapping preparations, concurrent normal browser edits in a
  different Workspace, a duplicate preparation request, a stop/restart during
  preparation, an Entra session expiry, and a restore rehearsal.
- Record the actual workload sizes and resource use. Review user clarity:
  queue state, progress, denied access, interrupted work, and retry guidance
  must be understandable without administrator intervention.
- Promote only after reviewing security results, focused automated tests,
  deployment evidence, restore evidence, and the pilot report.

**Risk.** A synthetic concurrency test can pass while realistic source data or
user timing exposes disk, memory, authentication, or recovery defects.

**Success criteria.** Two users can complete independent preparation work
without cross-Project leakage, corruption, lost status, or unsafe retry. A
restart preserves the previous completed evidence and gives an accurate next
action. The administrator can restore a known Project. The pilot explicitly
accepts the measured worker limit.

## PostgreSQL decision gate

This plan intentionally does not migrate Impodo to PostgreSQL. PostgreSQL is
reconsidered only when evidence shows that the single-application coordinator
is no longer an acceptable boundary, for example:

- Impodo needs more than one application replica or independently deployed
  worker containers.
- The organization needs durable scheduling shared across several VM processes
  or hosts.
- The two-worker, separate-Workspace model fails its measured throughput or
  reliability criteria after resource tuning.
- Shared-registry coordination remains a material source of contention despite
  the parent-owned lease model.

If one of these triggers is accepted, create a separate PostgreSQL migration
plan. It must move all durable Impodo coordination and operational evidence as
one coherent ownership boundary. It must not introduce a PostgreSQL registry
beside mutable DuckDB workspaces without a defined cross-store recovery model.
Original source files and immutable artifacts would still remain on the VM
volume, while Odoo would remain a separate PostgreSQL system behind JSON-2.

## Delivery-wide acceptance criteria

The server path is complete only when all of the following are true:

1. The VM runs one hardened application instance behind internal HTTPS and
   Microsoft Entra assignment.
2. The persistent volume retains the current DuckDB stores, original files,
   immutable artifacts, and protected evidence without object storage or
   database BLOB copies.
3. The parent application safely coordinates queued jobs and at most the
   accepted number of separate-Workspace preparation workers.
4. A Workspace cannot be changed or prepared concurrently while it is leased.
5. A server restart, worker failure, cancellation, and unknown Odoo outcome
   all preserve the existing fail-closed evidence and recovery rules.
6. A tested backup and restore procedure restores a representative Project
   consistently.
7. The pilot demonstrates the intended two-user workload, and the measured
   worker limit is documented rather than assumed from VM CPU count.
