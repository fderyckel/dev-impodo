---
audience: developer
kind: plan
status: current
---

# Local empty-server container rehearsal

## Scope

This report records the local Phase 2 container rehearsal completed on
2026-10-27. It verifies the empty server topology before Impodo is installed on
a remote Debian or Ubuntu host. It uses fictional secrets and contains no
customer data.

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
| Impodo image | `sha256:d4116fc02a2bc7b060f9dc7985196c0663199afa8eb567908d1c3f7aa34cdbcc` |
| Impodo image size | 683,274,838 bytes |
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

## Remaining qualification

The vulnerability scan did not run because Docker Scout may transmit local
image metadata or contents to an external service and that disclosure was not
separately authorized. The pinned PostgreSQL major and proxy version remain
rehearsal candidates rather than accepted production versions.

Production secret-provider integration, production database-role
provisioning, remote certificate policy, restart service, backup and restore,
log rotation, attached-storage behavior, and clean Debian and Ubuntu
rehearsals remain open. Docker Desktop ran from the verified official disk
image for this session; this non-administrator account could not copy the app
into `/Applications`, so a permanent local installation also remains an
operator action.
