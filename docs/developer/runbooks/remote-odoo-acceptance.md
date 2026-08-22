# Remote Odoo 19 acceptance run

## Purpose

Use this opt-in run after a fresh, disposable on-premises Odoo 19 database is
available. It exercises the real Impodo path against three models and 150
sanitized records:

- It captures schema and target evidence without writing.
- It creates and updates scalar values.
- It resolves an incoming product-category many2one relationship.
- It creates remote records through `Model.load()` and External IDs.
- It updates records through ORM `write()`.
- It reads results back by exact ID and External ID.
- It runs a second preview that must classify all 150 rows as unchanged.

This is acceptance evidence, not a production cutover command. The runner
does not delete or roll back records.

## Target prerequisites

- Odoo 19 with the Base, Contacts, and Product applications installed.
- A fresh database whose name begins with `impodo_p4_`.
- A non-loopback target exposed through HTTPS with a certificate trusted by
  the workstation. Literal-loopback development Odoo remains supported.
- A dedicated acceptance user and API key with the required read, create,
  import, and write access on `product.category`, `product.template`, and
  `res.partner`. The user also needs the read access required for schema,
  constraint, module, and External-ID verification.
- A database backup or a database that can simply be discarded afterward.

Do not point the runner at production. It refuses database names outside the
`impodo_p4_` namespace and refuses insecure non-loopback HTTP URLs.

## Run

Put the API key in a private file outside the repository, then run:

```bash
PYTHONPATH=src .venv/bin/python scripts/p4_representative_runner.py \
  --base-url https://odoo-acceptance.example.test \
  --database impodo_p4_20260810 \
  --api-key-file /private/path/odoo-api-key.txt \
  --output build/acceptance/remote-odoo-19.json
```

The first empty-target run seeds 25 controlled records so the reviewed load
contains 125 creates, 20 updates, and 5 unchanged rows. A successful result
then reports:

- It reports 145 committed writes.
- It verifies 150 rows with no fallout or unknown result.
- Its repeat preview contains 150 unchanged rows.
- It reports the observed execution and read-back rows per second.

Timing is evidence, not a release threshold. Keep the JSON result with the
Odoo database name, module build information, server logs, and workstation
details used for the run. The JSON deliberately excludes the URL, API key,
and business values.

## Failure handling

- Do not blindly rerun after a lost write response. Inspect the saved output,
  Impodo/Odoo logs, and the target first.
- If the target is neither empty, at the expected controlled seed state, nor
  already fully migrated and unchanged, the runner stops before loading.
- Reset or recreate the disposable database when a clean rerun is required.
- Treat blocked, ambiguous, partially applied, fallout, or unknown results as
  failed acceptance evidence.

After this standard-model gate passes, repeat the normal browser workflow
with a small sanitized profile covering the customer's installed custom
modules, custom models, custom fields, and representative relationships.
