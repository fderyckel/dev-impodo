---
audience: developer
document_type: report
status: current
---

# Stage 0 and 1 file intake performance

Registration now closes each bounded database phase before source workers
start. Files and Source data page reads share admission and rendering in one
operation. Rechecking unchanged files reuses profiles proven by the current
service instance, after verifying all source bytes in the normal isolated
worker. The initial inspection still profiles every row.

## Matched measurements

The comparison uses private, hash-verified copies of the two reported XLSX
artifacts, with 7,162,356 and 454,153 bytes. The larger workbook contains a
16,233-row, 193-column worksheet and a second 57-row, two-column worksheet.
The smaller workbook contains 16,249 rows and three columns. No operator
DuckDB store was opened by the benchmark. Temporary workspaces receive copies
through the normal multipart upload, registration, and inspection routes.

Before and after source snapshots have separate environments with Python
3.12.14 and identical frozen dependencies. Only the nine application files
belonging to this fix differ. Later concurrent application edits are excluded
from the measured candidate. Six runs alternate before and after; each warms
the complete sequence once, then measures it once. The table shows medians
from three measured sequences per build. This task started no concurrent test
or benchmark jobs during measurement; shared-host load can still vary. The
normal spawned workers, build checks, memory limits, and
timeouts remain enabled.

| Action | Elapsed, before → after | DuckDB opens | Connection setup, before → after | SQL statements | Store checks |
| --- | ---: | ---: | ---: | ---: | ---: |
| Empty files page | 79.4 → 62.5 ms | 6 → 4 | 38.7 → 25.8 ms | 53 → 49 | 4 → 3 |
| Upload both files | 1,463.6 → 1,458.1 ms | 17 → 14 | 112.2 → 94.0 ms | 178 → 181 | 9 → 10 |
| Uploaded files page | 73.1 → 55.7 ms | 6 → 4 | 39.0 → 26.0 ms | 53 → 49 | 4 → 3 |
| Register and inspect both files | 9,076.7 → 8,133.7 ms | 58 → 14 | 363.4 → 91.8 ms | 219 → 168 | 29 → 10 |
| Source data page | 104.2 → 87.0 ms | 6 → 4 | 38.9 → 25.9 ms | 60 → 56 | 4 → 3 |
| Recheck both unchanged files | 8,777.2 → 1,441.9 ms | 33 → 10 | 201.9 → 66.4 ms | 135 → 141 | 16 → 7 |
| Preview equivalent settings on the smaller file | 1,305.1 → 835.7 ms | 37 → 12 | 226.9 → 79.0 ms | 145 → 151 | 17 → 8 |

Registration removes 44 physical opens and about 75% of connection setup time.
Its total duration falls about 10%; parsing the wide workbook remains the
dominant cost. Repeat checks fall about 84%, and the configuration preview
falls about 36%. Upload time is effectively unchanged: isolated worker startup
and container validation still dominate this action.

Upload has one additional structural check because authorization and attachment
now validate independently on either side of the file worker. They previously
shared an owner across validation. Repeat checks and equivalent previews use
six additional SQL statements to load and recheck the canonical catalog before
publishing reused profiles. These checks are deliberate. A lower physical-open
count must not remove verification between independent operations.

Every sample has zero lock retries and zero lock-wait time. Column-catalogue
reads equal the reported store-check count. Both builds use the complete
store-validation counter. Catalog meanings match across builds and after every
recheck and preview, excluding receipt timestamps and opaque file identities.
Input bytes remain unchanged and no Odoo calls occur.

- [Before samples](evidence/2026-11-05-file-intake-before.json).
- [After samples](evidence/2026-11-05-file-intake-after.json).
- [Reproducible benchmark](../../scripts/benchmark_file_intake.py).

The benchmark defaults to fictional wide and narrow workbooks. Private input
copies can be supplied through `--input-directory`; output contains timings,
counts, and opaque comparison digests, with no file names or cell values.

```sh
UV_CACHE_DIR=.uv-cache .tools/uv/uv run --frozen --extra test --group quality python scripts/benchmark_file_intake.py --samples 3 --output /tmp/impodo-file-intake.json
```

## Safety and regression boundaries

Intake preserves bounded streaming, immutable storage, authorization, optimistic
revision checks, partial-upload behavior, and deletion compensation after a
failed attachment. Registration and rejected-form recovery run off the event
loop. Inspection captures local inputs, releases owners, runs file workers,
then rechecks authorization, workspace state, lifecycle, and source selection
before atomic publication. No DuckDB connection crosses a worker boundary or
survives a request.

Profile reuse requires one of at most 64 receipts produced successfully by the
current service instance. A receipt binds the Data version, complete source
record, canonical catalog hash, and parsing options. Every reuse reads and
hashes the whole file and validates its container in the existing isolated
worker. Publication rechecks the canonical catalog. Changed settings or
evidence, a new service instance, and eviction take the full inspection path.
Failed publication never adds trusted receipts. Receipt timestamps and normal
downstream invalidation still advance.

The first XLSX scan avoids constructing an XML element for every cell during
feature counting and avoids method calls for empty profile cells. It continues
to validate complete XML, reject entities, count formulas and merged ranges,
and record errors, overflow cells, and bounded statistics. CSV detection reads
only its existing bounded sample. Source-worker build, time, and memory checks
are unchanged.

The local performance skill now names file capture, worker, and publication
boundaries and requires regression coverage for first checks, repeat checks,
changed settings, and rejected publication. Earlier saved-page budgets did not
exercise this intake path or bound worksheet parsing. The new tests cover
those responsibilities directly; repository tests do not depend on local
skill files.

## Verification

The frozen after candidate passes 91 focused tests and 33 subtests covering
intake, inspection, parser safety, project setup, file-source acceptance, Odoo
source lifecycle, page admission, worker lifetimes, and DuckDB scope reuse.
Four additional hierarchy Recipe tests pass for Odoo 19 local, Odoo 19 remote,
Odoo 20 local, and Odoo 20 remote. Each exercises authoring and another accepted
delivery through Recipe reuse with isolated Odoo adapters. This is regression
coverage for both supported versions, not a connection to live Odoo servers.

The Python gate checks Ruff, Basedpyright, compilation, and diff whitespace.
The complete repository gate also checks the architecture inventory, complete
architecture suite, documentation, and code documentation.
`scripts/check_repository_quality.py` passes on the shared checkout, including
all 88 architecture tests; Ruff and Basedpyright report no diagnostics. The
local skill passes its frontmatter and structure validator. The complete
portable behavioral suite
and Windows/Linux platform suites were not run on this Mac; focused worker
tests preserve their runtime boundaries. Browser screenshots were not
recaptured because this change alters no visible controls or decision points.

## Live evidence and limits

The reported running instance recorded 6,420.9 ms for upload and 23,668.0 ms for
registration and initial inspection, with 17 and 58 opens respectively. These
are diagnostic observations from the old live build, not matched benchmark
samples. Host load and application state differ from the isolated fixture, so
the isolated after result must not be presented as a live 23.7-to-8.1-second
improvement.

The approved supervised restart loaded the fix at the original address and
retained the browser session: the replacement process served 72 authenticated
health requests. Both runtime processes later disappeared without a recorded
clean shutdown, and temporary verification environments were also cleared.
Impodo was restored at the same address without opening a browser tab. Since
the old sign-in secret existed only in memory, restoration requires a fresh
sign-in in the existing tab. The server responds and enforces authentication.

Live file-page and inspection timings remain pending an authenticated visit;
health requests do not measure file loading. No live workspace files, choices,
or saved evidence were modified by this task. Private benchmark copies were
removed after the comparison. The split-build worker guard remains active.
