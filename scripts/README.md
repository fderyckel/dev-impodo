# Repository scripts

Run scripts from the repository root with the checkout's uv runner. On POSIX,
use `.tools/uv/uv` when present and set `UV_CACHE_DIR=.uv-cache` when needed.
The environment and dependency groups are defined in `pyproject.toml`.

| Purpose | Retained tools |
| --- | --- |
| Required quality checks | `check_python_quality.py`, `check_repository_quality.py`, `architecture_inventory.py`, `documentation_quality.py`, `code_documentation_inventory.py` |
| Reproducible test execution | `run_seeded_unittest.py`; `pytest_sharding.py`, loaded explicitly with `python -m pytest -p scripts.pytest_sharding --shard-count 4 --shard-index 1` for one CI shard |
| Release packaging and installation | `internal_release.py`, `install-internal-release.ps1`, `update-internal-lock.ps1` |
| Documentation and fictional fixtures | `capture_*.py`, including `capture_matching_rule_screenshots.py`; `generate_current_bpmn.py`; `generate_product_bom_fixture.py` |
| Local performance qualification | `benchmark_*.py`, `qualify_transformation_scale.py` |
| Representative migration acceptance | `qualify_representative_migration.py` |
| Disposable Odoo qualification | `qualify_odoo*.py`, `qualify_hierarchy_workflow.py`, `qualify_row_inclusion_*.py`, `qualify_completed_load_correction.py`, `prepare_odoo_compatibility_lab.py`, `odoo20_read_qualification_support.py` |
| Disposable server identity rehearsal | `rehearse_server_identity.py` |

These scripts exercise current behavior or reproduce maintained evidence.
Read each runner's arguments and docstring before running it: qualification
runners that write to Odoo require a disposable target and their documented
write authorization. Screenshot runners create isolated fictional projects.

For the hierarchy journey, run `python -m scripts.qualify_hierarchy_workflow
--help` from a checkout with the `test` extra installed. The runner accepts an
already running, explicitly allocated disposable Odoo 19 or final Odoo 20
database. Provisioning, Linux server administration, and service start or stop
remain with their existing owners. `REMOTE` selects the Odoo JSON-2 transport;
the runner itself uses an isolated local Impodo application.

Supply a private JSON descriptor with these fields:

```json
{
  "contract_version": 1,
  "disposable": true,
  "purpose": "hierarchy_qualification",
  "base_url": "https://fictional.example",
  "database": "impodo_hierarchy_example",
  "credential_owner_id": "00000000-0000-4000-8000-000000000001"
}
```

The UUID identifies existing READ and WRITE credential envelopes for this exact
target in `CredentialVault`. Save both through the established protected
credential mechanism (`store_target_credential`); the descriptor and command
line contain no keys. LOCAL additionally requires absolute `local_config`,
`python`, and `odoo_bin` file paths. Its loopback URL and database must match
the selected configuration. When using the compatibility lab, include
`lab_target` (`baseline` or `odoo20`) and its exact `source_commit` from
[`lab-targets.json`](../fixtures/odoo-compatibility/lab-targets.json). The preview
pin is rejected. A LOCAL source pin is checked against the installation's Git
HEAD. For REMOTE, the source pin is an operator attestation; the runtime version
is independently checked in both modes.

For example, run the following with a fresh output directory:

```sh
UV_CACHE_DIR=.uv-cache .tools/uv/uv run --frozen --extra test --group quality \
  python -m scripts.qualify_hierarchy_workflow \
  --target-descriptor /private/target.json --source-shape generated \
  --mode REMOTE --expected-major 20 \
  --output-directory /private/hierarchy-result --allow-write
```

The target must have `base`, `contacts`, `product`, and `mrp` installed, with
read, create, write, and cleanup permissions. The runner refuses collisions
with its fixed category names or six Product references, including archived
records, before seeding five
categories. It verifies 13 creates, 5 unchanged records, all 18 business paths,
and 6 generated variants. It then requires 18 unchanged records on repeat
comparison, removes only its verified fixture, and repeats the load through a
published Recipe with reordered fresh files.

`--fault before-dispatch`, `after-commit`, or `after-receipt` interrupts the
authoring load once, reads actual target state, reopens private application
storage, and recovers the existing journal. `known-rejection` and
`unknown-outcome` are diagnostic failure cases: they return a failed result and
retain target state. Writes with an uncertain outcome are never blindly
replayed. Every run writes a sanitized `result.json`; exact IDs, workspace
evidence, and the ledger of newly created vault entries stay under `private/`.
Successful cleanup retains protected-evidence encryption keys so that evidence
remains readable. Failure or uncertain cleanup retains fixture state and keys
for diagnosis. Inspect that evidence before arranging any later cleanup.

For the Odoo 20 row-rule Recipe canary, use `python -m
scripts.qualify_row_inclusion_recipe --help`. It requires an HTTPS target, a
private API-key file, an output receipt path, and `--allow-write`. It publishes
and reuses a Recipe in disposable local storage, creates only two included
fictional Contacts, verifies exclusion, and removes its unique test records.
The receipt distinguishes this adapter canary from journalled execution and a
transfer between two live servers. The key is never copied into the receipt.

For the complete row-rule transfer and interruption matrix, run `python -m
scripts.qualify_row_inclusion_transfer --help`. Supply distinct disposable
local and remote Odoo 20 connections, private key files, an output directory,
and `--allow-write`. It uses the normal capture, review, preflight, journalled
load, and read-back routes. Interrupted cases stop client processing after one
accepted create batch, reopen persisted application state with the same private
vault, and recover the original journal. Each case verifies two included
Contacts, exclusion of the third, no duplicate creates, and exact cleanup.

Run `python -m scripts.capture_transfer_row_inclusion` with Playwright installed
to reproduce the authenticated row-builder, count-confirmation, and protected
decision-review screenshots. Its optional `--browser-channel` selects the
installed Chromium browser; the default is `msedge`.

Application releases, storage schemas, and portable evidence use separate
versions. See [Storage and release versioning](../docs/developer/reference/storage-and-release-versioning.md).
Development-storage reset and historical layout conversion scripts have been
removed from the first-project baseline.
