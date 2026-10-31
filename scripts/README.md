# Repository scripts

Run scripts from the repository root with the checkout's uv runner. On POSIX,
use `.tools/uv/uv` when present and set `UV_CACHE_DIR=.uv-cache` when needed.
The environment and dependency groups are defined in `pyproject.toml`.

| Purpose | Retained tools |
| --- | --- |
| Required quality checks | `check_python_quality.py`, `check_repository_quality.py`, `architecture_inventory.py`, `documentation_quality.py`, `code_documentation_inventory.py` |
| Reproducible test execution | `run_seeded_unittest.py` |
| Release packaging and installation | `internal_release.py`, `install-internal-release.ps1`, `update-internal-lock.ps1` |
| Documentation and fictional fixtures | `capture_*.py`, `generate_current_bpmn.py`, `generate_product_bom_fixture.py` |
| Local performance qualification | `benchmark_*.py`, `qualify_transformation_scale.py` |
| Disposable Odoo qualification | `qualify_odoo*.py`, `qualify_completed_load_correction.py`, `p4_representative_runner.py`, `prepare_odoo_compatibility_lab.py`, `odoo20_read_qualification_support.py` |
| Disposable server identity rehearsal | `rehearse_server_identity.py` |

These scripts exercise current behavior or reproduce maintained evidence.
Read each runner's arguments and docstring before running it: qualification
runners that write to Odoo require a disposable target and their documented
write authorization. Screenshot runners create isolated fictional projects.

Application releases, storage schemas, and portable evidence use separate
versions. See [Storage and release versioning](../docs/developer/reference/storage-and-release-versioning.md).
Development-storage reset and historical layout conversion scripts have been
removed from the first-project baseline.
