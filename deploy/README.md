---
audience: developer
document_type: runbook
status: partial
---

# Local empty-server container rehearsal

## Purpose

This rehearsal lets a developer find image, configuration, secret-mount,
migration, health, network, and persistence defects before Impodo is installed
on a remote Debian or Ubuntu host. It starts the shared-server health and
Platform Administrator browser routes and can run the opt-in first-operator
bootstrap command. The Platform Administrator can sign in, list Organizations,
create an Organization, and sign out. Customer login, Projects, migration
workflows, workers, and Odoo connectivity remain unavailable.

The current delivery mode is a local trial. No remote Impodo server is
allocated. This runbook therefore supports repeatable local development; it is
not an installation guide and it does not imply that a shared server exists.

The rehearsal uses internal TLS and binds its only host port to `127.0.0.1`.
It uses fictional secrets and dedicated Docker volumes. A successful result is
development evidence; it does not qualify a supported Linux host.

## Prerequisites

- Run Docker Engine or Docker Desktop with Docker Compose v2.
- Run the commands from the repository root.
- Keep port `8443` available on the host, or set `IMPODO_PROXY_PORT` to another
  unprivileged port.
- Use a new `COMPOSE_PROJECT_NAME` for a provably empty rehearsal. To reuse the
  example name, first remove only its local rehearsal containers and volumes:

  ```sh
  docker compose --env-file deploy/examples/local-rehearsal.env -f deploy/compose.yaml down --volumes --remove-orphans
  ```

  This intentionally deletes that rehearsal's PostgreSQL, artifact, secret,
  and proxy state. Never put customer data in those volumes.

## Prepare fictional secrets

Create one internally consistent secret set:

```sh
.tools/uv/uv run --frozen python deploy/local_rehearsal.py
```

The command writes seven files below the ignored `deploy/.local/secrets`
directory. It prints no secret value and refuses to replace an existing set.
Remove that directory deliberately before generating a new empty rehearsal.

If an existing rehearsal has the original five-file set, add only the new
fictional Platform Administrator password without replacing its database
credentials:

```sh
.tools/uv/uv run --frozen python deploy/local_rehearsal.py --platform-admin-only
docker compose --env-file deploy/examples/local-rehearsal.env -f deploy/compose.yaml run --rm --no-deps volume-provisioner
```

If an existing rehearsal has the six-file set that includes the Platform
Administrator password, add only the deployment-keyed identifier digest
secret. This preserves every database credential and the existing
administrator password:

```sh
.tools/uv/uv run --frozen python deploy/local_rehearsal.py --identifier-digest-key-only
docker compose --env-file deploy/examples/local-rehearsal.env -f deploy/compose.yaml run --rm --no-deps volume-provisioner
```

## Validate and start the stack

Ask Compose to resolve the configuration before it creates a container:

```sh
docker compose --env-file deploy/examples/local-rehearsal.env -f deploy/compose.yaml config --quiet
docker compose --env-file deploy/examples/local-rehearsal.env -f deploy/compose.yaml build --pull
docker compose --env-file deploy/examples/local-rehearsal.env -f deploy/compose.yaml up --detach --wait
```

The topology initializes PostgreSQL, creates the fixed runtime and migration
roles, applies the current migration suffix through schema version `10`, starts
the authenticated Platform administration web process, and then starts the
loopback TLS proxy. The
worker remains disabled behind the `worker` profile because its durable
runtime is not implemented. The first-operator command remains disabled behind
the `admin` profile until a developer invokes it explicitly. The
[2026-10-27 local rehearsal](../docs/plans/91-2026-10-27-local-empty-server-container-rehearsal.md)
records the first complete result on Docker Desktop for Apple silicon.

## Verify the local contract

Open the generic health endpoints through the only published listener:

```sh
curl --insecure https://localhost:8443/health/live
curl --insecure https://localhost:8443/health/ready
docker compose --env-file deploy/examples/local-rehearsal.env -f deploy/compose.yaml ps
```

The two responses contain only generic status. The service list must show no
published web, worker, or PostgreSQL port.

Run the migration command twice after the initial start. Both runs must report
schema version `10` and apply no new migration:

```sh
docker compose --env-file deploy/examples/local-rehearsal.env -f deploy/compose.yaml run --rm migrate
docker compose --env-file deploy/examples/local-rehearsal.env -f deploy/compose.yaml run --rm migrate
```

## Bootstrap the fictional first operator

Set a fictional login email and display name for this local rehearsal. Compose
reads the generated password from the protected secret volume; the command
does not accept the password as a command-line argument and does not print it.

```sh
IMPODO_BOOTSTRAP_LOGIN_EMAIL=platform.operator@example.test \
IMPODO_BOOTSTRAP_DISPLAY_NAME='Fictional platform operator' \
docker compose --env-file deploy/examples/local-rehearsal.env -f deploy/compose.yaml --profile admin run --rm admin-bootstrap
```

The first run must report completion. Repeating the command must refuse with
exit code `78` because one active Platform Administrator now exists. Open
`https://localhost:8443/login` and sign in with the fictional email and the
generated Platform Administrator password. The browser can list and create
Organizations, and **Sign out** revokes the exact current session. The
customer-user list, activation and reset forms, customer session actions, and
security-history pages remain later Work Package 4.5 increments.

Confirm that the application runs as the fixed unprivileged identity and that
its application tree is not writable. Then add a fictional marker to the
otherwise empty artifact volume for the persistence check:

```sh
docker compose --env-file deploy/examples/local-rehearsal.env -f deploy/compose.yaml exec web id
docker compose --env-file deploy/examples/local-rehearsal.env -f deploy/compose.yaml exec web test ! -w /opt/impodo
docker compose --env-file deploy/examples/local-rehearsal.env -f deploy/compose.yaml exec web sh -c 'umask 077; printf "local-rehearsal\n" > /var/lib/impodo/artifacts/.local-rehearsal-marker'
```

## Exercise drain, restart, and persistence

Stop the web process with the configured grace period, start it again, and
confirm that readiness returns:

```sh
docker compose --env-file deploy/examples/local-rehearsal.env -f deploy/compose.yaml stop --timeout 30 web
if curl --fail --insecure https://localhost:8443/health/ready; then exit 1; fi
docker compose --env-file deploy/examples/local-rehearsal.env -f deploy/compose.yaml start web
curl --insecure https://localhost:8443/health/ready
```

Recreate the stack without deleting its volumes:

```sh
docker compose --env-file deploy/examples/local-rehearsal.env -f deploy/compose.yaml down
docker compose --env-file deploy/examples/local-rehearsal.env -f deploy/compose.yaml up --detach --wait
docker compose --env-file deploy/examples/local-rehearsal.env -f deploy/compose.yaml run --rm migrate
docker compose --env-file deploy/examples/local-rehearsal.env -f deploy/compose.yaml exec web test -r /var/lib/impodo/artifacts/.local-rehearsal-marker
```

The final migration result must still report schema version `10` with no newly
applied migration, and the final marker check must pass. Together these prove
that the PostgreSQL and artifact volumes survived container recreation. No
customer file belongs in this rehearsal.

## Evidence and limitations

Record the application, PostgreSQL, and proxy image digests, the output of
`docker compose config`, the service and port list, migration results, health
results, and sanitized shutdown logs. Do not record the generated secret
files.

Store sanitized point-in-time results under `docs/reports/`. Keep generated
secrets and disposable runtime evidence below the ignored `deploy/.local/`
root.

The pinned PostgreSQL `18.6` and Caddy `2.11.7` images are local rehearsal
candidates. Their inclusion does not accept the production PostgreSQL major or
replace image-vulnerability review and explicit production version approval.

Docker Desktop runs the Linux containers through a managed virtual machine.
This rehearsal therefore does not prove the remote host firewall, filesystem,
time synchronization, restart service, log rotation, or attached-storage
contract. Work package 2.5 still requires clean Debian and Ubuntu hosts before
a supported server release. While no remote host is allocated, record that
qualification as deferred instead of treating the laptop as a substitute.
