# Local Odoo runbook

## Scope

This Windows-only runbook covers the optional browser assistant for an Odoo 19
and PostgreSQL stack on the same computer. Impodo does not install Odoo or
PostgreSQL and never treats a local stack as permission to write data.

The selected `odoo.conf`, executable paths, process identifiers, and service
ownership remain in memory for the current Impodo session.

## Connect to an existing local stack

1. In Project setup, choose **Local Odoo**.
2. Select **Help me connect to local Odoo**.
3. Choose the exact `odoo.conf` used by the intended workspace.
4. Review the detected PostgreSQL, Python, `odoo-bin`, log, HTTP, and database
   details.
5. Select **Check again** after correcting an external problem.
6. When the stack is ready, select **Save and test connection**.
7. Select **Verify access and load models** when a model snapshot must be
   created or refreshed.

Local mode does not require an Odoo API key. Impodo reads only allowlisted
non-secret routing values; it does not retain `db_password` or `admin_passwd`.

## Interpret readiness

| Check | What it proves |
| --- | --- |
| Configuration | Selected paths and loopback routing are structurally safe |
| PostgreSQL | `pg_isready` confirms the configured server accepts connections |
| Odoo server | The loopback HTTP endpoint identifies Odoo 19 |
| Database access | The selected local installation can open the configured database read-only |

Green means ready, orange requires action or is incomplete, red failed, and
grey has not run. Always read the accompanying text; color alone is not the
result.

Stored model/field snapshots are project evidence. Reopening a project can use
them without contacting Odoo. Refresh after relevant module/custom-field
changes or when freshness policy requires it.

## Start PostgreSQL and Odoo

Starting services requires explicit confirmation. Impodo:

1. rereads the selected configuration;
2. checks whether its exact PostgreSQL data directory is already running;
3. starts PostgreSQL only when needed and waits for `pg_isready`;
4. starts Odoo only after PostgreSQL is ready;
5. waits for the configured Odoo 19 loopback endpoint.

Commands use fixed argument lists and detected paths. Users cannot enter a
shell command or arbitrary executable.

Services already running before the check remain external and status-only.
Impodo records ownership only for the exact children it starts during this
session.

## Stop or restart managed services

Before stopping, finish other local work that may use the same PostgreSQL
server.

Impodo stops:

1. the exact Odoo child it launched;
2. PostgreSQL only after the Odoo port is closed and the current
   `postmaster.pid` still matches the PID recorded at startup.

PostgreSQL uses a bounded `pg_ctl stop -m fast`. A changed PID, external
service, or process from another Impodo session is never claimed from its port
or executable path alone. Failed cleanup retains ownership information so the
current session can retry safely.

**Restart managed services** performs the same verified stop before the normal
PostgreSQL-first start sequence.

## End the session

Ownership is not persisted. If the assistant lists managed services, select
**Stop managed services** before **Quit Impodo**. If Impodo or Windows exits
unexpectedly, a new session will not claim the old processes; use the local
workspace's approved manual inspection and shutdown procedure.

Starting, stopping, or restarting the stack never imports or changes Odoo
business records.

## Troubleshooting

| Symptom | Action |
| --- | --- |
| Configuration failed | Confirm the selected file and explicit loopback host/ports |
| PostgreSQL not ready | Review its current log and `postmaster.pid`; do not start a second data directory blindly |
| Odoo version failed | Confirm the selected workspace is Odoo 19 |
| Database access failed | Verify database name, addons/configuration, and local process permissions |
| Stop/Restart unavailable | The service is external or belongs to an earlier session; use the workspace procedure |
| PostgreSQL remains after Stop | Resolve the reported listener/PID mismatch before any manual stop |

