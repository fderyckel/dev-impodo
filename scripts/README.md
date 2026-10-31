# Repository scripts

Run scripts from the repository root with the checkout's uv runner. On POSIX,
use `.tools/uv/uv` when present and set `UV_CACHE_DIR=.uv-cache` when needed.
The environment and dependency groups are defined in `pyproject.toml`.

| Purpose | Retained tools |
| --- | --- |
| Required quality checks | `check_python_quality.py`, `check_repository_quality.py`, `architecture_inventory.py`, `documentation_quality.py`, `code_documentation_inventory.py` |
| Reproducible test execution | `run_seeded_unittest.py` |
| Release packaging and installation | `internal_release.py`, `install-internal-release.ps1`, `update-internal-lock.ps1` |
| Documentation and fictional fixtures | `capture_*.py`, including `capture_matching_rule_screenshots.py`; `generate_current_bpmn.py`; `generate_product_bom_fixture.py` |
| Local performance qualification | `benchmark_*.py`, `qualify_transformation_scale.py` |
| Representative migration acceptance | `qualify_representative_migration.py` |
| Disposable Odoo qualification | `qualify_odoo*.py`, `qualify_row_inclusion_*.py`, `qualify_completed_load_correction.py`, `prepare_odoo_compatibility_lab.py`, `odoo20_read_qualification_support.py` |
| Disposable server identity rehearsal | `rehearse_server_identity.py` |

These scripts exercise current behavior or reproduce maintained evidence.
Read each runner's arguments and docstring before running it: qualification
runners that write to Odoo require a disposable target and their documented
write authorization. Screenshot runners create isolated fictional projects.

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
