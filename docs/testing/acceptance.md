# Acceptance and test strategy

## Current conclusion

The repository has automated evidence for the current browser workflow,
profile-driven preflight, local-stack controls, security boundaries, and
internal release process. The maintained preflight fixture produces all five
classifications, and unchanged saved inputs produce deterministic manifests.

Do not copy a fixed test count into documentation. The discovered suite is the
current executable inventory; optional environment-gated integrations must be
reported separately.

This is not yet live-target acceptance. The local 25,000-row preparation and
durable-preflight scopes have measured workstation evidence. The required
100–300-record sanitized slice, live target runs, Odoo-side ACL evidence, and
representative production sizing remain pending.

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

## Automated test inventory

| Area | Current test modules |
| --- | --- |
| Browser projects and source workflow | `test_projects`, `test_inspection`, `test_workspace`, `test_web_app` |
| Mapping, preparation, staging, and quality | `test_mapping_validation`, `test_derived_entities`, `test_advanced_coverage`, `test_preparation_session`, `test_readiness`, `test_staging_store`, `test_quality` |
| Profile-driven and durable preflight | `test_profile_and_values`, `test_source_and_planner`, `test_catalog_metadata`, `test_engine`, `test_connectors`, `test_preflight_service`, `test_preflight_scale`, `test_reporting_cli` |
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
- project databases migrate to the durable staging schema;
- canonical headers and rows publish atomically in bounded database batches;
- failed publication retains the previous current run without partial rows;
- identical current evidence is idempotent and changed evidence preserves
  superseded history;
- target and bound-input changes invalidate the current pointer without
  deleting historical rows;
- readiness reports bind the exact staging run and content hash;
- the browser uses a plain saved/retry state and collapses technical evidence;
- projects above 25,000 physical rows block before artifact materialization
  with a plain split-the-source instruction;
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
- existing projects migrate without presenting stale quality as current;
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
- schema-v17 history survives migration and is not promoted to current
  preflight evidence;
- the Review page shows New in Odoo, Different from Odoo, Already matches,
  Needs attention, and Set aside, and disables repeat submission while a
  comparison is running.

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

Selection metadata is captured, but the proof of concept does not validate source values
against the Odoo selection list. That should be an explicit policy decision,
not an assumed passing case.

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
[Examples and edge cases](../examples-and-edge-cases.md).

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

The next performance target is complete local preparation of 100,000 physical
rows in less than 120 seconds and less than 900 MiB peak working set. It is not
yet implemented or verified, and the supported browser limit remains 25,000
rows until the gates in the
[100,000-row performance refactor plan](../plans/100k-performance-refactor-plan.md)
pass. Every optimization must append comparable before-and-after evidence here
rather than replacing the historical results above.

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
| Live authorised target | not complete | execute smoke tests |
| Historical-scale memory | 25,000-row browser preparation and durable preflight verified | 100,000-row expansion remains separate |

## Acceptance gate

Local automated validation is complete when the currently discovered required
suite passes and the offline commands reproduce the documented result. Record
optional integration skips explicitly; do not treat an unavailable optional
runtime as executed evidence.

deployment milestone acceptance additionally requires:

- the reviewed 100–300-record slice;
- live target smoke runs;
- Odoo-side read-only account evidence;
- partner confirmation of Odoo version, routing, context, keys, scopes,
  decimal, and timezone rules;
- approved snapshot/report retention;
- no unresolved high-severity architecture or data-leak issue.

Neither gate authorizes an Odoo write.
