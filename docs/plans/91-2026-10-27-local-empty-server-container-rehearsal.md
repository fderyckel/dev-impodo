---
audience: developer
kind: report
status: current
---

# Local empty-server container rehearsal

## Scope

This report records the local Phase 2 container rehearsal completed on
2026-10-27. It verifies the empty server topology before Impodo is installed on
a remote Debian or Ubuntu host. It uses fictional secrets and contains no
customer data. No remote Impodo server is currently allocated; this evidence
supports local trial development only.

The result is point-in-time development evidence. Docker Desktop runs Linux
inside a managed virtual machine, so this result does not qualify a supported
Linux host or its firewall, filesystem, service manager, attached storage, or
certificate policy.

## Rehearsal identity

| Item | Observed value |
| --- | --- |
| Branch and base commit | `main` at `07dde47c076f6cfaaac3ac46fa8a15d8605c4b25` with the uncommitted Phase 2 increment |
| Host | macOS 27.0.1 build 26A434 on Apple arm64 |
| Container runtime | Docker Desktop 4.97.0; Docker Engine 29.9.2; Docker Compose 5.6.1 |
| Container platform | Linux arm64 |
| Resolved Compose SHA-256 | `cc03a1c82f9939c6d2e07392465b1f34a14021c501077bc293d8bfdc480ceabb` |
| Python base image | `python:3.12.16-slim-bookworm@sha256:e697c81897302a4c4333bade000d98038fbb21eb064f8ee1fd42d02ebcd9d337` |
| Impodo image | `sha256:e639d2adf99737ad6206775fd76bf4e9ee74b5019e0c96f25e71c9e759caaa24` |
| Impodo image size | 683,280,936 bytes |
| PostgreSQL image | `postgres:18.6-bookworm@sha256:3725f4e2499eef5134592b3b4ab79a543ed7f8e533b05b5b637af926630f6650` |
| Proxy image | `caddy:2.11.7-alpine@sha256:eb4d35b7452d1ab6171fbdbe8d9275b2301dad6dad92115d91e19fb9b24d510f` |

The operator generated five ignored fictional secret files with mode `0600`.
No secret value was printed or recorded.

## Defects found and corrected

The first live run found three boundaries that static parsing did not expose:

1. Docker Desktop reported a read-only macOS bind-mounted PostgreSQL init
   script as executable, but the mount refused direct execution. The
   provisioner now copies that script into a Linux named volume with mode
   `0644`, so the official PostgreSQL entry point sources it consistently.
2. The shared Impodo image supplied web-only configuration defaults and an
   implicit artifact `VOLUME` to every role. The image no longer supplies
   role-specific settings or implicit writable volumes. Compose gives each
   process only its required configuration and mounts.
3. The official Caddy binary carries `cap_net_bind_service`. Combining that
   file capability with a complete capability drop and
   `no-new-privileges` prevented the binary from executing. The proxy now
   retains only `NET_BIND_SERVICE`; it still runs as UID and GID `10001`, uses
   a read-only root filesystem, and keeps `no-new-privileges`.

The build initially changed permissions in a separate runtime layer, which
duplicated the 369 MB virtual environment. Moving that permission change into
the builder reduced the image from 1.16 GB to 683 MB while preserving the
read-only application tree.

## Accepted local evidence

| Check | Result |
| --- | --- |
| Compose resolution | `docker compose config --quiet` passed. |
| Startup ordering | The volume provisioner completed, PostgreSQL became healthy, migration completed, web became healthy, and the proxy became healthy. |
| PostgreSQL roles | `impodo_migrator` and `impodo_runtime` were created without superuser, database-creation, role-creation, replication, or row-security-bypass privileges. |
| Migration replay | Initial migration applied schema version `1`. Two explicit repeats and every later restart reported version `1` with nothing applied. |
| HTTPS health | `https://localhost:8443/health/live` returned `{"status":"alive"}` and readiness returned `{"status":"ready"}` through Caddy internal TLS. |
| Published ports | Only Caddy published `127.0.0.1:8443`. Web, worker, and PostgreSQL published no host port. |
| Networks | Application and control networks were internal. Only the proxy joined the non-internal edge network. |
| Process isolation | Migration and web used the same final image, UID and GID `10001`, read-only root filesystems, and complete capability drops. |
| Secret isolation | Migration mounted only the migration DSN volume. Web mounted only the runtime DSN volume, and the migration DSN path was absent. |
| Odoo egress | The web process had `IMPODO_ODOO_EGRESS_MODE=disabled`. |
| Worker | The worker profile remained disabled and no worker container started. |
| Drain and recovery | Web shutdown completed cleanly. Readiness failed with HTTP 502 while web was stopped and returned after restart. |
| Persistence | After Compose removed and recreated every container and network, PostgreSQL still reported schema version `1` and the fictional artifact marker remained readable. |
| Image scan | Docker Scout 1.27.0 indexed 198 packages in image `sha256:e639d2adf997`. It reported no critical vulnerabilities and eight high, eleven medium, and forty-nine low vulnerabilities. No critical or high finding had an available fix. The policy evaluation failed with five of seven policies met and a health score of B at 78%. |
| Continuous integration commands | The digest-pinned Trivy 0.76.1 command reported zero fixable critical or high vulnerabilities. The digest-pinned BuildKit scanner emitted SLSA provenance v1 and an SPDX 2.3 bill of materials containing 164 packages. The hosted workflow itself has not run yet. |
| Repeat local checkpoint | On 2026-10-28, Compose resolved, two migration replays applied nothing at schema version `1`, liveness and readiness passed, the web process ran as UID and GID `10001`, `/opt/impodo` was not writable, Odoo egress remained disabled, and only the proxy published `127.0.0.1:8443`. |
| Phase 3 persistence trial | Migration `2` advanced the control contract to version `2` and created empty Organization and Project catalogue tables. A replay applied nothing. The runtime role had select, insert, and update privileges without delete. Every Project index began with `organization_id`. A rollback-only two-Organization insertion left both tables empty, and liveness and readiness remained healthy. |
| Project catalogue repository trial | A separate disposable PostgreSQL 18.6 container proved that two fictional Organizations can reuse one Project UUID without crossing records. The adapter rejected a stale revision, paged only explicitly visible Projects in deterministic order, advanced the lifecycle to ready, and retained the row through deleting and deleted states. A deployment-neutral service denied mismatched scope, identity, capability, expiry, or visibility before storage access. The live suite compared shared DuckDB and PostgreSQL revision semantics and observed twelve concurrent reads open and close twelve independent sessions. The runtime role still had no delete privilege. The disposable container was removed after the test; the Compose rehearsal volumes were not changed. |
| Coordination repository trial | Separate disposable PostgreSQL 18.6 containers applied schema version `3` and exercised fictional operation, receipt, recovery, and Workspace lease rows. Exact operation replay returned the original request, changed meaning failed closed, substituted receipts were rejected, and receipt rows remained immutable. Recovery retained exact receipt evidence in `PROJECTED`, recorded no-commit work as `INTERRUPTED`, and retained committed evidence while moving a conflicting result to `MANUAL_REVIEW`. Lease release and reacquisition increased the fencing token; an old owner was fenced, and two concurrent contenders produced one active lease. Portable coordinator tests injected failures at all seven named boundaries without repeating data-plane work. Each disposable container was removed after its test; the Compose rehearsal volumes were not changed. |

## Remaining qualification

The operator explicitly authorized Docker Scout to store and index the local
image on 2026-10-28. The first scan found nine high-severity vulnerabilities,
including CVE-2026-84782 in OpenSSL. The image now pins Python 3.12.16 on a
refreshed Bookworm digest that contains `openssl 3.0.22-1~deb12u2`. A clean
rebuild and repeat scan removed that finding and reduced the medium and low
counts by three and two respectively.

The repeat scan found eight high-severity vulnerabilities in three Debian
packages. Six affected `util-linux`, and one each affected `zlib` and `perl`.
Scout reported no Bookworm fix for these findings. The current non-root,
read-only, capability-restricted runtime reduces exposure to several local
privilege paths, but it does not waive or resolve those findings.

Scout also failed the license-policy and required-attestation policies. The
Linux continuous integration workflow now builds the pinned image, applies a
credential-free gate against fixable critical or high vulnerabilities, and
exports maximum-mode provenance plus an SPDX software bill of materials as a
retained artifact. Local validation reproduced both the gate and evidence
export; the first hosted run remains open. An explicit license review and a
vulnerability acceptance or upstream remediation decision for the remaining
findings are still required. The pinned PostgreSQL major and proxy version
remain rehearsal candidates rather than accepted production versions.
Switching from Debian to Alpine is not an accepted remediation because it
would change the runtime contract and requires separate compatibility
qualification.

Production secret-provider integration, production database-role
provisioning, remote certificate policy, restart service, backup and restore,
log rotation, attached-storage behavior, and clean Debian and Ubuntu
rehearsals remain open. Docker Desktop ran from the verified official disk
image for this session. Copying it into `/Applications` is not a qualification
requirement and will not be requested from this non-administrator account.
Future local rehearsals may reuse an already available user-launched container
runtime; supported-host qualification belongs on approved external Debian and
Ubuntu hosts.
