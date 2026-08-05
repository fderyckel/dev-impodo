# Acceptance and test strategy

## Current conclusion

The repository has automated evidence for the current browser workflow,
profile-driven preflight, local-stack controls, security boundaries, and
internal release process. The maintained preflight fixture produces all five
classifications, and unchanged saved inputs produce deterministic manifests.

Do not copy a fixed test count into documentation. The discovered suite is the
current executable inventory; optional environment-gated integrations must be
reported separately.

This is not yet live-target acceptance. The required 100–300-record sanitized
slice, live target runs, Odoo-side ACL evidence, and expected-scale memory
evidence remain pending.

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
| Mapping, preparation, staging, and quality | `test_mapping_validation`, `test_derived_entities`, `test_readiness`, `test_staging_store`, `test_quality` |
| Profile-driven preflight | `test_profile_and_values`, `test_source_and_planner`, `test_catalog_metadata`, `test_engine`, `test_connectors`, `test_reporting_cli` |
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
| Record snapshot is incomplete | run fails; no conclusions | connector behavior; dedicated saved-file test pending |
| Source identity is duplicated | all duplicates `BLOCKED` | unit |
| Create-only identity exists with `block` | `BLOCKED` | unit |
| Create-only identity exists with `unchanged` | `UNCHANGED` | implementation; dedicated test pending |
| Reference-mode row | no decision | implementation; dedicated test pending |

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

Source-side streaming beyond the bounded browser limit, normalization
approval, and clean-package certification remain pending.

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

Results on 2026-08-05:

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

The normal post-benchmark regression run executed 230 tests in 252.428 seconds:
221 passed, 6 errored, and 3 opt-in scale tests were skipped. All six errors
come from the concurrently changing Slice 5 planner rejecting legacy
`asset_lines` fixtures whose Odoo reads cannot be narrowed safely. The staging,
quality, normalization, and preparation-scale modules reported no ordinary
regression failure. Slice 5 must reconcile those fixtures before a green full
suite can be claimed.

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
| Historical-scale memory | not complete | profile and document |

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
