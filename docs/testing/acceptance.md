# Acceptance and test strategy

## Current conclusion

The repository has automated evidence for the current browser workflow,
profile-driven preflight, local-stack controls, security boundaries, and
internal release process. The maintained preflight fixture produces all five
classifications, and unchanged saved inputs produce deterministic manifests.

Do not copy a fixed test count into documentation. The discovered suite is the
current executable inventory; optional environment-gated integrations must be
reported separately.

The disposable-target practical path has local live-target acceptance: a
150-record sanitized run completed with every row verified and a repeat
preview proposed no writes. The same harness is ready for retained remote
acceptance when the disposable on-premises target is available. Exact-snapshot
direct mappings compiled entirely to the native columnar path are supported
through 100,000 physical rows;
direct mappings requiring the Python oracle remain at 50,000, and
derived/materialized preparation and durable preflight retain their separate
25,000-row boundaries. Broader Odoo-side ACL/record-rule matrices and
representative production sizing remain pending for later risk profiles.

## Validation command

From the repository root on Windows PowerShell:

```powershell
New-Item -ItemType Directory -Force .\.tmp | Out-Null
$env:TEMP = (Resolve-Path .\.tmp).Path
$env:TMP = $env:TEMP
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

On macOS or Linux:

```bash
.venv/bin/python -m unittest discover -s tests -v
```

Set `IMPODO_RUN_WORKBOOK_TESTS=1` only when the optional workbook-rendering
runtime is installed and that integration is part of the acceptance run.

## Fresh-process preparation baseline

The columnar preparation track uses a machine-readable parent harness rather
than comparing repeated runs inside one Python process:

```bash
PYTHONPATH=src .venv/bin/python scripts/benchmark_preparation.py \
  --runs 3 \
  --rows 100000 \
  --columns 30 \
  --mapped-fields 20 \
  --workload products \
  --output .tmp/preparation-products-baseline.json
```

Run the same command with `--workload bom`, and use `--dirty` for the
effect-heavy case. Every child process creates the deterministic fixture and a
new local project, then records fixture checksum/size, revision, Python and
runtime versions, batch size, wall and CPU time, sampled peak and ending
working set, database size, counts, hashes, and detailed phase timings. The
parent refuses to summarize runs whose fixture, runtime, revision, dimensions,
or workload differ. The JSON output contains medians and every individual run;
do not compare medians without retaining those raw observations.

Accepted baseline evidence requires a clean worktree. The harness records that
state and refuses a dirty checkout by default. `--allow-dirty-worktree` exists
only for implementation diagnostics; its output must not be promoted as a
baseline or release result.

The detailed phase timers are benchmark-only wrappers around the unchanged
production path. They separate source batch reading, projection, scalar
value evaluation, inclusive row finalization, prepared-record construction,
canonical construction and serialization, DuckDB appends/finalization,
quality, and normalization. This instrumentation intentionally adds small
call-level overhead, so compare baseline and future backends with the same
harness revision and settings.

The fixture section separately records its peak/ending working set, snapshot
count, and Parquet bytes. It includes original-file ingestion, snapshot
publication, and the remaining project/mapping fixture setup. The preparation
peak begins afterward and therefore measures verified Parquet consumption
rather than XLSX/CSV parsing. The focused `test_source_snapshot_io` suite
proves lossless CSV/XLSX semantics, cell-bounded fragment sizing, exact-file
verification, disk-write cleanup, orphan recovery, transactional pointer
rollback, deterministic reuse, and repeated preview/direct preparation with
the original source unavailable.

## Automated test inventory

| Area | Current test modules |
| --- | --- |
| Browser projects and source workflow | `test_projects`, `test_inspection`, `test_workspace`, `test_source_snapshot`, `test_source_snapshot_io`, `test_web_app` |
| Mapping, preparation, staging, and quality | `test_mapping_validation`, `test_derived_entities`, `test_advanced_coverage`, `test_preparation_session`, `test_readiness`, `test_staging_store`, `test_quality` |
| Profile-driven preflight and practical execution | `test_profile_and_values`, `test_source_and_planner`, `test_catalog_metadata`, `test_engine`, `test_connectors`, `test_preflight_service`, `test_execution_snapshot`, `test_execution_service`, `test_execution_repository`, `test_preflight_scale`, `test_reporting_cli` |
| Local Odoo lifecycle | `test_local_odoo_reader`, `test_local_stack` |
| Security, governance, hosting, and release | `test_project_security`, `test_governance`, `test_hosting_contracts`, `test_internal_release` |

## Classification matrix

| Case | Expected outcome | Current evidence |
| --- | --- | --- |
| Valid identity, no target match | `CREATE` | golden |
| One match, scalar differs | `UPDATE` plus difference | golden |
| One match, several fields differ | one `UPDATE`, several differences | golden product |
| One match, canonical values equal | `UNCHANGED` | golden |
| Two targets share complete identity/scope | `AMBIGUOUS` | golden and unit |
| Source scalar cannot be parsed | `BLOCKED` | type unit plus engine precedence |
| Required source reference absent | `BLOCKED` | golden |
| Target-only reference has no match | `BLOCKED` | golden |
| Target-only reference has two matches | `BLOCKED` | resolver logic; dedicated test pending |
| Incoming parent is missing | child `BLOCKED` | golden |
| Incoming parent is present but blocked | child `BLOCKED_BY_DEPENDENCY` | implementation; dedicated test pending |
| Record or metadata snapshot is incomplete | run fails; no conclusions | connector and exact-projection unit tests |
| Source identity is duplicated | all duplicates `BLOCKED` | unit |
| Create-only identity exists with `block` | `BLOCKED` | unit |
| Create-only identity exists with `unchanged` | `UNCHANGED` | implementation; dedicated test pending |
| Reference-mode row | resolves references without an import decision | dedicated engine test |

## Identity and relation requirements

### Verified

- ordered composite target identity;
- relational identity component;
- company-scoped match;
- duplicate target identity retention;
- target-only many2one business-key resolution;
- incoming parent resolution;
- many2many set equality;
- many2many add/remove/replace behavior;
- missing target-side reverse-reference blocks comparison.

### Additional acceptance cases

- same business key in two scopes with both source scopes represented;
- duplicate source strings that normalize to the same typed target identity;
- composite scalar identity with each component varied independently;
- scoped target-only key collision;
- ambiguous incoming reference caused by duplicate source rows;
- a blocked, rather than missing, incoming parent;
- null-to-relation and relation-to-null for every relation null policy.

## Canonical-value requirements

### Durable staging foundation verified

- the artifact adapter and storage-independent evaluator produce the same
  prepared bundle and canonical content hash;
- server-rendered scalar previews and full-row runtime call the same provider,
  formula-context, transformation, parsing, and validation boundary;
- repeated evaluation of unchanged inputs produces identical canonical JSON;
- every canonical row has stable source, mapping, schema, and derived-plan
  lineage;
- grouped parent and lookup entities retain every contributing physical
  source-row pointer;
- candidate, reference, blocked, quarantined, and excluded counts must
  reconcile exactly to the total row count;
- direct, lookup, parent, and child datasets record input, used, output,
  combined, additional, and unrepresented row controls;
- a blocking source-side issue produces a blocked canonical disposition;
- changed bound evidence changes the canonical run hash;
- changed canonical payload with an old content hash is rejected;
- portable canonical evidence rejects numeric Odoo identifier fields;
- new project databases include the durable staging schema;
- canonical headers and rows publish atomically in bounded database batches;
- failed publication retains the previous current run without partial rows;
- identical current evidence is idempotent and changed evidence preserves
  superseded history;
- target and bound-input changes invalidate the current pointer without
  deleting historical rows;
- readiness reports bind the exact staging run and content hash;
- the browser uses a plain saved/retry state and collapses technical evidence;
- native-columnar direct projects above 100,000 physical rows,
  Python-fallback direct projects above 50,000, and materialized or derived
  projects above 25,000 block before artifact materialization with a plain
  split-the-source instruction;
- explicitly named expected sums use only user-selected mapped numeric fields,
  retain unit/tolerance evidence, persist atomically, and block package creation
  when they differ or contain empty values.

Source-side streaming beyond the bounded browser limit and clean-package
certification remain pending.

### Quality and quarantine verified

- unchanged inputs and rules produce deterministic quality JSON and hashes;
- every physical row has one accounting entry and all canonical fan-out links
  are retained;
- every canonical row has one effective disposition;
- required, bounded value, lookup, relationship, and guided cross-field
  findings reuse the full-row canonical result;
- complete post-transformation identity collision groups are set aside;
- a relationship to a set-aside incoming row propagates to the dependent row;
- failed atomic publication preserves the previous current quality run;
- ownership or retention changes invalidate quality without deleting staging;
- only the exact current project schema opens; every different generation or
  version is rejected without promoting stale quality evidence;
- set-aside rows do not enter Odoo record-request planning;
- the browser shows Ready, Review, Set aside, and Fix setup with technical
  identifiers collapsed;
- automatic checks cannot be switched off and optional checks use guided
  business-language fields and outcomes.

### Durable preflight verified

- comparison reloads exact frozen staging, quality, normalization, and mapping
  evidence without reading the registered source artifact;
- stored-row tampering fails before the target reader is called;
- every browser decision carries its canonical row trace ID;
- reference-mode rows support resolution without import decisions;
- browser-origin and profile-origin compiled plans share classifications,
  differences, issues, and reference resolutions;
- metadata and record projections must contain exactly the planned models and
  fields;
- same-model domain chunks merge identical rows and reject conflicts or
  repeated pagination IDs;
- a second comparison publishes a new current run and snapshot while retaining
  the previous run and unchanged source normalization;
- restart retrieval uses stored evidence without connector access;
- normal result paging reads bounded decision pages from DuckDB rather than an
  in-memory report;
- current-schema history is retained without being promoted to current
  preflight evidence;
- the Review page shows New in Odoo, Different from Odoo, Already matches,
  Needs attention, and Set aside, and disables repeat submission while a
  comparison is running;
- a local project with no matching session profile opens the reusable recovery
  dialog, rejects a different database, and enables comparison only after the
  read-only check succeeds.

### Practical local execution verified

- only the current hash-bound execution snapshot can be loaded;
- Local Odoo 19 and the bounded remote Odoo 19 slice can write, while the exact
  standard/custom models and writable fields come from the current
  captured-schema-bound preview;
- the native adapter exposes exact business-key lookup, bounded create, and
  single-record update rather than generic RPC;
- dependency-ordered creates resolve incoming relationship IDs before
  dependants, including many2one and many2many fields;
- every proposed write is journaled before target I/O and receives a terminal
  row outcome;
- a lost write response is recorded as `OUTCOME_UNKNOWN`, is not retried, and
  blocks the remaining work;
- new project databases include durable execution and reconciliation tables;
- committed rows are checked by exact saved ID, remote-create External IDs are
  checked against their expected model and record, and uncertain creates are
  re-matched by the governed business key before retry safety is classified;
- the browser end-to-end test previews, confirms, journals, reads back, and
  renders a verified load without exposing the submitted API key; and
- reconciliation reports retain status, field names, and recovery guidance,
  but not source or target business values.

The protected P3 adapter, service, persistence, and browser path are covered
automatically. The live representative P4 acceptance is recorded separately
because it requires an explicitly disposable Odoo database. The same runner
now accepts a remote HTTPS Odoo 19 target, binds the current exact writer and
read-back scopes, and emits phase timings and observed rows per second. The
remote run remains pending until a disposable on-premises target is available;
see the [remote acceptance runbook](../developer/runbooks/remote-odoo-acceptance.md).

P4 passed on 2026-08-06 against the isolated `impodo_p4_20260806` database:
125 creates, 20 updates, 5 unchanged, 145 committed writes, 150 verified by
read-back, no fallout or unknown outcomes, and a fresh preview with all 150
unchanged. The [P4 result](../reports/p4-representative-run-2026-08-06.md)
records the target counts and reproduction boundary.

### Verified

- string trim;
- whitespace collapse through fixture comparison;
- integer parsing;
- decimal half-up quantization without float conversion;
- explicit true/false tokens;
- invalid boolean rejection;
- date parsing;
- offset datetime conversion to UTC;
- naive UTC datetime;
- null `distinct`, `equivalent`, and `ignore_source_null`;
- Odoo false normalization in the comparison path.

### Additional acceptance cases

- Unicode case-folding;
- non-UTC naive datetime rejection;
- daylight-saving boundary instants;
- minimum/maximum governed decimal values;
- empty string with `empty_as_null: false`;
- selection value outside the captured selection list;
- target value that cannot be normalized.

Selection fields use Odoo's technical choice codes as the authoritative
domain. Constants and fallbacks are checked against the captured choices;
preflight checks every non-null final prepared value against freshly fetched
Odoo choices. A removed code blocks its affected rows, an empty live choice
set blocks every non-null proposal, and null remains governed by the separate
required/null policies. Choice labels are display-only.

## Snapshot requirements

### Verified

- matching fixture fingerprints;
- exact file hashes appear in the manifest;
- source hashes bind CLI-generated record snapshots;
- deterministic requested-field projection;
- duplicate target IDs across live pages are rejected by implementation;
- incomplete record snapshots are rejected by implementation.

### Hardening required before trusted live evidence

- require the expected `kind`;
- require profile binding on both files;
- require source binding on record files;
- persist and verify a requirements/request hash;
- persist the requested domain;
- validate the complete envelope with a schema;
- add corrupted, truncated, and wrong-kind tests;
- decide whether metadata incompleteness should stop the run rather than
  produce all-blocked decisions.

The current loader is suitable for CLI-generated, retained, trusted local
snapshots. It is not a hardened parser for arbitrary third-party JSON.

## Connector requirements

### Verified with mocked transport

- `POST /json/2/<model>/<method>`;
- bearer authorization;
- `X-Odoo-Database`;
- named JSON arguments;
- deterministic `id asc`;
- pagination across multiple pages;
- configured timeout passed to transport;
- redacted timeout failure;
- API key omitted from object representation;
- no public write or generic-call method.

### Live-only acceptance

- real Odoo 19 endpoint and database routing;
- dedicated service user;
- read-only ACL and record-rule proof;
- exact permitted models and fields;
- company/context behavior;
- module-version visibility;
- HTTP 401/403 behavior from the real gateway;
- proxy/redirect behavior;
- stable pagination while target data is controlled;
- sentinel `write_date` unchanged before/after capture;
- equivalent fixture and live-target semantic results.

No local test creates temporary Odoo records.

## Golden slice

### Current compact fixture

The committed slice contains 12 candidates across:

- standard `res.partner`;
- extended-standard `product.template`;
- custom `x_uc.asset` and `x_uc.asset.line`;
- parent/child identity and relation;
- target-only UoM and tag references;
- many2many;
- scoped product identity;
- create, update, unchanged, ambiguous, and blocked results.

Expected totals:

| Classification | Count |
| --- | ---: |
| `CREATE` | 5 |
| `UPDATE` | 2 |
| `UNCHANGED` | 2 |
| `AMBIGUOUS` | 1 |
| `BLOCKED` | 2 |

The detailed row list is in
[Developer examples and edge cases](../developer/reference/examples-and-edge-cases.md).

### Required organization-specific slice

Build approximately 100–300 sanitized records covering:

- real standard, extended-standard, and custom organization models;
- real governed business keys and scopes;
- parent/child relationships;
- target-only many2one and many2many;
- single- and multi-field updates;
- duplicate source and target identities;
- missing and ambiguous references;
- blocked parent propagation;
- false/null/empty boundaries;
- decimal/date/datetime boundaries;
- create-only and reference modes;
- warnings for non-proposed relation validation.

Store expectations as reviewed decisions and differences, not output generated
by the code under test during assertion.

## Determinism

Current deterministic guarantee:

- identical profile selection;
- identical source file bytes;
- identical saved snapshot bytes;
- identical engine identity;

produce byte-identical canonical manifest JSON.

The semantic hash includes the snapshot timestamp because it is part of the
target fingerprint. Recapturing identical target data at a later time
therefore changes the snapshot and semantic hash. This is intentional evidence
binding, not nondeterminism.

Additional useful tests:

- shuffled target records produce the same result;
- source file enumeration still follows profile order;
- a one-value change changes the semantic hash;
- workbook sheet values reconcile to the manifest, independent of binary XLSX
  package differences.

## Data minimization and leakage

Currently verified:

- planner excludes unrelated fields;
- portable manifest rejects forbidden numeric-ID keys;
- known fixture IDs do not appear in the manifest;
- connector surface has no write/generic operation;
- API keys are redacted;
- workbook rows are generated from portable decisions.

Further hardening:

- recursive ID/secret scans for every release artifact;
- dedicated formula-injection workbook test;
- logs/exception scans under every HTTP failure shape;
- assert saved live snapshots contain only exact requested fields;
- add a report-package completeness check requiring both files.

## Performance

The benchmark command measures dictionary index construction/lookups:

```bash
PYTHONPATH=src .venv/bin/python -m impodo benchmark \
  --rows 360000
```

It is non-gating and does not read source files, call the connector, build
catalogs, run comparisons, measure memory, or generate a workbook.

Before production sizing, record for the 100–300-row slice and historical-scale
synthetic data:

- source preparation time and peak memory;
- request count and page count per model;
- saved snapshot sizes;
- catalog/resolution/comparison time;
- manifest and workbook generation time;
- total peak memory.

Current integrated browser evidence, recorded on the development Windows
workstation on 2026-08-05:

| Physical rows | Canonical and quality rows | End-to-end time | Peak RSS | Project DB |
| ---: | ---: | ---: | ---: | ---: |
| 1,000 | 1,001 | 1.646 s | 95.9 MiB | 15.0 MiB |
| 10,000 | 10,001 | 19.464 s | 179.7 MiB | 34.0 MiB |
| 25,000 | 25,001 | 45.392 s | 348.0 MiB | 66.3 MiB |

The fixture has three columns, one grouped parent, and one child per physical
row. Timings include preparation, atomic canonical publication, quality
evaluation, dual accounting, and atomic quality publication. The policy accepts
at most 25,000 physical rows in the materializing browser adapter and fails
before loading larger inputs. The earlier 100,000-row evaluator-only probe did
not include the integrated quality overlay and no longer defines the product
limit.

### P1 relationship and serialization diagnostic

This opt-in diagnostic is intentionally narrower than the end-to-end browser
gate. It constructs a worst-case dependency chain with one unsafe root, then
measures target-independent quality propagation and canonical quality hashing.
It performs no source-file, DuckDB, or Odoo operation.

Command:

```powershell
$env:IMPODO_RUN_QUALITY_SCALE = '1'
$env:IMPODO_QUALITY_SCALE_ROWS = '100000'
.\.venv\Scripts\python.exe -m unittest `
  tests.test_quality.QualityRelationshipScaleTests.test_deep_dependency_chain_is_linear
```

Results on the Lenovo Windows reference machine on 2026-08-05:

| Version of the same fixture | Rows | Edges | Fixture build | Quality evaluation | Quality hash | Measured phase total | Peak working set |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Queue and compact indexes, before portable-validator optimization | 100,000 | 99,999 | 17.818 s | 24.387 s | 9.158 s | 51.363 s | 847.4 MiB |
| Iterative portable validator | 100,000 | 99,999 | 11.297 s | 16.993 s | 7.024 s | 35.314 s | 847.5 MiB |
| Row-bounded portable validator | 100,000 | 99,999 | 8.422 s | 16.350 s | 7.063 s | 31.835 s | 845.5 MiB |
| Row-bounded portable validator | 10,000 | 9,999 | 0.810 s | 1.944 s | 0.526 s | 3.280 s | 140.4 MiB |

The 100,000-row before/after runs produced the same staging hash
`sha256:658971342eb9bf78c3a95be6ea8d0ee1bbec3cce3ff794aa0a729366425ec1e4`
and quality hash
`sha256:e6550732eb7c1fcc75e4cfa4ff1794c24ae1c04099720e140337ea7306e65abc`.
Measured phase time improved by 38.0%. The 10x row/edge increase from 10,000
to 100,000 remains approximately linear rather than quadratic.

Environment: repository revision `852c25712068c103387d1ad167fb8d1471d3b811`
with the documented optimization patch in a dirty worktree; Windows 11 build
26200; Python 3.12.10; DuckDB 1.5.5; openpyxl 3.1.5; psutil 7.2.2; 31.5 GiB
RAM; Intel64 Family 6 Model 181. Database and temporary-file size are not
applicable to this in-memory diagnostic and are non-gating observations for
the optimization program.

The 845.5 MiB peak belongs to a deliberately all-quarantined fixture retaining
100,000 issues and quarantine entries. It does not pass the complete memory
gate, and this diagnostic alone does not justify raising the 25,000-row browser
limit. Bounded quality publication and the full mixed preparation fixture are
still required.

### Complete wide preparation diagnostic

The opt-in `tests.test_preparation_scale` fixture runs the actual local
preparation application service and DuckDB repositories. It loads a
deterministic CSV with 30 columns, applies 20 mapped scalar fields, validates a
business control total, produces one visible normalization effect per row, and
durably publishes canonical staging, quality, and normalization evidence. Only
the 25,000-row scale guard is patched by the benchmark.

Command:

```powershell
$env:IMPODO_RUN_PREPARATION_SCALE = '1'
$env:IMPODO_PREPARATION_SCALE_ROWS = '100000'
$env:IMPODO_PREPARATION_SCALE_WORKLOAD = 'products' # or 'bom'
.\.venv\Scripts\python.exe -m unittest `
  tests.test_preparation_scale.PreparationWorkflowScaleTests.test_complete_preparation_workflow -v
```

Results on 2026-08-05:

| Physical rows | Source columns | Mapped fields | Complete preparation | Peak working set | Project DB | Result |
| ---: | ---: | ---: | ---: | ---: | ---: | --- |
| 10,000 | 30 | 20 | 40.271 s | 383.8 MiB | 40.3 MiB | Correct; calibration only |
| 100,000 | 30 | 20 | 429.175 s | 2,897.3 MiB | 221.8 MiB | Failed time and RAM gates |

The 100,000-row source is 35,100,271 bytes with SHA-256
`b0c39ad0abcdeb511502ff72b0cef8e6295bc62b31ec535488760cae148b3b52`.
The published staging, quality, and normalization hashes were respectively
`sha256:a5f10f04257b53b233217431793d8a4bfe61e2fd7772e1bd5cd70b80ad3d7200`,
`sha256:09e27895e3b5634b58c006232fb97a31e40d5105a72430306c308eb0bea45498`,
and
`sha256:91e70e7e451eb72179cfe14e2de664de8530bde353d0009f5802e7a695caab2c`.
All 100,000 rows were staged, ready, and normalization-eligible, with no
failed control total. The measured phase times were 121.394 seconds for source
loading and evaluation, 134.635 seconds for canonical publication, 58.315
seconds for quality, and 113.651 seconds for normalization.

This run fails the less-than-120-second and less-than-900-MiB release contract.
The supported browser limit therefore remains 25,000 rows. Database and
temporary-file size remain observations rather than optimization gates.

### Complete durable preflight diagnostic

The opt-in `tests.test_preflight_scale` fixture prepares and freezes a real
project, removes its source artifact, and launches a fresh comparison process.
The measured interval starts with durable frozen-evidence retrieval and ends
after the manifest, decision rows, protected target snapshots, and current
preflight pointer are persisted. Workbook generation runs afterward so its
size is recorded separately. Runtime, working set, and artifact sizes are
diagnostics rather than release gates.

Command:

```bash
IMPODO_RUN_PREFLIGHT_SCALE=1 \
IMPODO_PREFLIGHT_SCALE_ROWS=25000 \
.venv/bin/python -m unittest \
  tests.test_preflight_scale.DurablePreflightScaleTests.test_durable_preflight_workflow -v
```

Result on the MacBook Air M5 on 2026-08-06:

| Source rows | Target rows | Metadata requests | Bounded record chunks | Comparison and persistence | Peak working set | Project DB |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 25,000 | 25,000 | 1 | 50 | 5.273 s | 507.3 MiB | 69.5 MiB |

The protected snapshot was 2,915,076 bytes, the portable manifest was
6,665,592 bytes, and the workbook was 688,774 bytes. All 25,000 decisions were
persisted as `UNCHANGED`; two protected snapshot envelopes and one readiness
run were stored. These observations retain the Slice 5 baseline but no longer
act as pass/fail thresholds under the practical delivery policy.

The ordinary regression suite then ran 262 tests in 19.680 seconds: 252 passed
and 10 environment-gated tests were skipped. There were no failures or errors.

### Slice 6 advanced preparation diagnostic

The opt-in complete-preparation fixture can install an approved scope,
versioned reference list, governed code-list and metric checks, deterministic
resolution policy, compact effective-row persistence, and downstream
normalization by setting `IMPODO_PREPARATION_ADVANCED=1`.

On 2026-08-06 the 25,000-row products fixture completed in 92.811 seconds with
an observed 1,133.8 MiB peak working set, 910.0 MiB ending RSS, and a 108.5 MiB
project database. It published 25,000 canonical rows, 25,000 effective quality
rows, 25,000 normalization effects, no quality issue, no quarantine, and no
failed control total. Its staging, quality, and normalization hashes were
`sha256:a9cdbdca9c738a0514661f994172c5105d42dadee937324f950bb6adcdc1cd5c`,
`sha256:fa64b9c405d035b837ec91ecf5c1d962c3839c352db73939f3e4cece46179977`,
and
`sha256:e9b937f7bb77327e40c7460659d1bdd63857ad988d66c488953653d921028b21`.

Slice 6 treats elapsed time, working set, and database size as retained
operational diagnostics rather than correctness release gates. The supported
browser limit remains 25,000 rows; the separate 100,000-row plan owns any
increase.

Results after the contained CPU and encoded-once publication changes on the
MacBook Air M5 on 2026-08-06:

| Workload | Physical rows | Source columns | Mapped fields | Complete preparation | Ending RSS | Project DB | Effects | Result |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| Products | 100,000 | 30 | 20 | 23.178 s | 1,102.3 MiB | 224.3 MiB | 100,000 | Time passed; RAM failed |
| BOM | 100,000 | 30 | 20 | 27.090 s | 1,237.1 MiB | 302.3 MiB | 300,000 | Time passed; RAM failed |

The products run published staging, quality, and normalization hashes
`sha256:41ec0dcb43ea203224d3b0decb227a434c5a52c056ff2c5306402f7eb3c508e8`,
`sha256:08bd6a1e38a12138e9fb09b1e10b4ed75e674b551974a2deb59bbcc8bff2118f`,
and
`sha256:a5fbf988e58ded0030e778b06b4549be87dee06777e9dbaa81a14077f1bd5a58`.
Its phase times were 11.891 seconds for source loading and evaluation, 2.839
seconds for canonical publication, 2.665 seconds for quality, and 5.665 seconds
for normalization.

The BOM run published staging, quality, and normalization hashes
`sha256:c014ecd78683cc1ee01e7f1c346423b611b008fa61ef4f5bb9e1212131e8d8fa`,
`sha256:0386708c5e3669c166c266e67a0a84565ea8e4b9318ada99f3cc4b2e43c4c741`,
and
`sha256:433c024b4a360905a2577b3db0b000172073a1d8f62e99e33bce07034de2b696`.
Its phase times were 12.267 seconds for source loading and evaluation, 2.898
seconds for canonical publication, 2.720 seconds for quality, and 9.089 seconds
for normalization.

These are non-profiled release-style timing measurements. On macOS the original
harness used end-of-run RSS when Windows `peak_wset` was unavailable, so the
memory column is not a true peak. Both ending values already exceed 900 MiB and
therefore still fail the RAM gate. The 25,000-row supported browser limit is
unchanged. The complete ordinary regression suite then ran 256 tests in 18.759
seconds: 247 passed and 9 opt-in scale tests were skipped.

The Lenovo and Mac measurements above are independent workstation evidence and
must not be used as a numerical before/after comparison.

To isolate the code change, a controlled A/B was run on the same MacBook Air M5
with the exact committed 10,000-row fixture and source checksum. The baseline
was commit `b90782697bfef9c6a3554aa4ab90b41fe6c5cd81`; the optimized snapshot
differed only in the 12 implementation files from proposal phases 1 and 2.
Both sides used the same virtual environment and each ran three times in a
fresh process.

| Same-Mac median | Committed baseline | Optimized | Change |
| --- | ---: | ---: | ---: |
| Complete preparation | 5.487 s | 2.401 s | 56.2% lower |
| Ending RSS | 539.9 MiB | 339.0 MiB | 37.2% lower |
| Source loading and evaluation | 1.416 s | 1.203 s | 15.0% lower |
| Canonical publication | 1.838 s | 0.292 s | 84.1% lower |
| Quality | 0.674 s | 0.276 s | 59.1% lower |
| Normalization | 1.497 s | 0.526 s | 64.9% lower |

Raw `(complete seconds, ending RSS MiB)` pairs were baseline `(5.329, 539.9)`,
`(5.487, 539.6)`, `(5.971, 539.9)` and optimized `(2.456, 338.3)`,
`(2.354, 339.0)`, `(2.401, 340.0)`.

Every A/B run used the 3,510,271-byte source with SHA-256
`1787ff4b764acb36336768d8258c0edaefd2d253c4840b476b42cb4b8018ebad`
and passed identical workload assertions: 10,000 staged and ready rows, no
review, quarantine, blocked rows, or failed total, plus 10,000 eligible and
changed normalization records. Content hashes cannot be compared across these
fresh fixtures because each fixture intentionally creates new project and
mapping identities. Hash-algorithm parity is instead verified by exact
canonical-encoder equivalence tests and repository round-trip tests.

### Durable typed-row quality diagnostic

The next slice removed quality's dependency on the transient `PreparedBundle`.
After canonical publication, preparation releases the staged object and
reloads the exact durable canonical run using bounded row fetches, incremental
hash verification, and typed-value restoration. Quality and normalization then
consume that verified run.

The preparation scale harness now samples process working set throughout the
timed operation on every platform and reports ending RSS separately. This
corrects the earlier macOS fallback described above.

Corrected 100,000-row diagnostics on the MacBook Air M5:

| Workload | Complete preparation | Observed peak | Ending RSS | Project DB | Durable reload | Effects | Result |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| Products | 26.929 s | 1,605.0 MiB | 829.0 MiB | 223.3 MiB | 4.543 s | 100,000 | Time passed; RAM failed |
| BOM | 61.961 s | 1,685.6 MiB | 789.6 MiB | 298.3 MiB | 8.855 s | 300,000 | Time passed; RAM failed |

The BOM run followed several large local probes and is retained as an observed
diagnostic rather than a stable timing baseline. Both runs preserved every
canonical and quality row, had no quality issue or quarantine entry, and
published all expected normalization effects. The complete ordinary suite then
ran 263 tests in 39.969 seconds: 253 passed and 10 environment-gated tests were
skipped.

These results show that releasing `PreparedBundle` lowers downstream residency
but does not solve the actual peak. The peak occurs earlier while source
tables, prepared rows, canonical rows, and transformation impacts coexist. The
next required implementation is bounded source transformation into pending
durable canonical/effect batches.

This is workstation evidence, not a production sizing guarantee. Wide sources,
saved snapshots, workbooks, and Odoo transport still require representative
measurement.

The native-columnar direct path now meets the local 100,000-row target of less
than 120 seconds and 900 MiB peak working set. Direct mappings requiring the
Python oracle remain limited to 50,000 rows, and derived/materialized paths
remain limited to 25,000. Cross-platform repetition and the related/mixed gates
in the [remaining-work plan](../plans/remaining-work.md#1-qualify-related-and-mixed-preparation-at-100000-rows)
remain open. Every optimization appends comparable evidence here rather than
replacing historical results.

### 50,000-row bounded-direct background release evidence

On 2026-08-06 the production background path was exercised on the MacBook Air
M5 with dirty 50,000-row, 30-source-column, 20-mapped-field fixtures. These are
standalone workstation measurements, not comparisons with the Lenovo runs.

| Workload | Complete preparation | Worker peak | Worker exited | Result |
| --- | ---: | ---: | --- | --- |
| Products | 46.431 s | 570.5 MiB | Yes | Passed |
| BOM | 58.073 s | 590.7 MiB | Yes | Passed |

Both jobs used the session-scoped background manager and actual child-process
preparation path without patching the production scale check. Progress state
was held in memory; completed preparation evidence remained in DuckDB. The
bounded direct limit is therefore 50,000 physical rows. Derived/materialized
projects remain capped at 25,000 rows, and 100,000 rows remains a separate
qualification target.

### 100,000-row native-columnar production evidence

On 2026-08-10 the actual spawned preparation-worker path was exercised on the
MacBook Air M5 with 100,000 rows, 30 source columns, and 20 mapped fields. Each
repeat used a new child process, matching production memory reclamation. Before
the repeat, the registered CSV artifact was deleted; preparation therefore had
to reuse the verified source and prepared Parquet evidence. Staging and
normalization hashes were unchanged, the prepared artifact was not rewritten,
and both workers exited.

| Direct workload | Attempt | Complete preparation | Worker peak | Result |
| --- | --- | ---: | ---: | --- |
| Products | First | 41.883 s | 826.2 MiB | Passed |
| Products | Repeat | 42.046 s | 869.0 MiB | Passed |
| BOM-shaped direct table | First | 54.958 s | 854.7 MiB | Passed |
| BOM-shaped direct table | Repeat | 54.249 s | 807.6 MiB | Passed |

The BOM-shaped fixture required the measured one-thread Polars default; the
two-thread configuration reached 906.7 MiB and was rejected. An explicit
`POLARS_MAX_THREADS` environment value still overrides the default. The
100,000-row limit is selected only when every dataset is direct,
columnar-supported, and bound to an exact source snapshot. Otherwise Impodo
retains the preceding 50,000- or 25,000-row limit. Related and mixed-dataset
100,000-row qualification remains follow-on work, not part of this direct-path
claim.

### 2026-08-12 Windows Phase 7 qualification

Phase 7 did **not** qualify a limit increase. The attributable measurements
were captured on clean revision `676b79d9...`; the repository later advanced
to `5b67475f...`, so the current revision remains unqualified.

The direct 100,000-row and related 16,000-Product/80,000-BOM first workers did
not finish within ten minutes (608.888 s and 608.094 s harness failures). The
4,000-row clean effect-heavy child exceeded 900 s, and the dirty/high-effect
first worker failed after 601.553 s. None produced repeat, three-run, storage,
hash-parity, or vectorization evidence.

The customer twin completed three fresh first/repeat pairs and passed its
absolute gates: maximum wall time was 14.313/14.400 s, maximum worker peak was
176.430/168.406 MiB, all workers exited, snapshots were reused, sources were
not reopened, hashes were stable, and parent RSS returned below its pre-job
baseline. Against the exact same-machine Phase-0 median peak of 191.301 MiB,
however, the candidate improved only 7.774%; the required improvement is 30%.

The relationship semantic oracle was stopped before completion and has no
accepted result. The 100,000-row mixed/derived route remains capped and was not
qualified. Therefore existing capability limits and user-facing claims remain
unchanged. Detailed evidence and the discovered `HEAD`-stability blind spot are
recorded in the
[transformation-scale implementation log](../reports/transformation-scale-implementation-log.md#windows-phase-7-qualification-result--2026-08-12).

Structural requirements already apply:

- no connector call inside the row loop;
- requests grouped by model;
- match and reference lookup use indexes;
- DuckDB row evidence is inserted in bounded bulk relations;
- pagination is deterministic.

## Acceptance traceability

| Milestone criterion | Local status | Remaining evidence |
| --- | --- | --- |
| Same profile path for fixture and live targets | architecture supports it | live target runs |
| No connector write operation | verified in code/tests | Odoo ACL proof |
| Every import candidate gets one outcome | verified for compact fixture | larger acceptance slice |
| Exact before/after updates | verified | reviewer confirmation |
| Relations resolved in batches | verified structurally | live call/page evidence |
| Duplicate targets are ambiguous | verified | real-key confirmation |
| No numeric IDs in portable manifest | verified | release artifact scan |
| Unchanged saved inputs are identical | verified | retained acceptance artifacts |
| Composite/scoped identities | verified locally | real target scopes |
| Relational comparison | verified locally | real target relationships |
| 100–300 sanitized records | not complete | build and review |
| Live authorised target | remote harness ready; local P4 passed | run against the disposable on-premises target |
| Historical-scale memory | 50,000-row bounded direct preparation and 25,000-row durable preflight verified | 100,000-row expansion remains separate |

## Acceptance gate

Local automated validation is complete when the currently discovered required
suite passes and the offline commands reproduce the documented result. Record
optional integration skips explicitly; do not treat an unavailable optional
runtime as executed evidence.

deployment milestone acceptance additionally requires:

- the reviewed 100–300-record slice;
- a retained remote-target acceptance result;
- Odoo-side read-only account evidence;
- partner confirmation of Odoo version, routing, context, keys, scopes,
  decimal, and timezone rules;
- approved snapshot/report retention;
- no unresolved high-severity architecture or data-leak issue.

Neither gate authorizes an Odoo write.
