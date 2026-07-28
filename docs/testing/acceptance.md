# Acceptance and test strategy

## Current conclusion

The local proof of concept is green:

- 46 tests pass with the real workbook integration enabled;
- the BOM preparation example succeeds;
- the committed 12-candidate golden fixture produces all five outcomes;
- the manifest is deterministic for unchanged saved inputs;
- the live connector is exercised with mocked transport only.

This is not yet UC DEV/TEST acceptance. The required 100–300-record sanitized
slice, live environment runs, Odoo-side ACL evidence, and expected-scale memory
evidence remain pending.

## Validation command

From `/Users/francois/dev-impodo`:

```bash
UC_RUN_WORKBOOK_TESTS=1 \
PYTHONPATH=src \
.venv/bin/python -m unittest discover -s tests -v
```

Without `UC_RUN_WORKBOOK_TESTS=1`, the workbook integration test is skipped.

## Automated test inventory

| File | Current coverage |
| --- | --- |
| `tests/test_profile_and_values.py` | all scalar types, decimal quantization, explicit booleans, null policies, example profiles, unknown keys, validate-only contradiction, and cycles |
| `tests/test_source_and_planner.py` | typed BOM preparation, strict CSV/XLSX loading, native XLSX values, actual worksheet rows, formula rejection, duplicate headers, safe paths/formats, symbolic references, duplicate source identity, minimal metadata fields, one request per model, target-domain preservation |
| `tests/test_catalog_metadata.py` | target duplicate preservation, ID-to-business-reference conversion, complete golden metadata, readonly fields, relation mismatch, missing reference model |
| `tests/test_engine.py` | all five outcomes, exact scalar/many2many differences, target-only resolution, composite relational identity, decimal comparison, scoped matching, missing parent, target ambiguity, grouped evidence, ID leakage, byte determinism, create-only policy, many2many operations |
| `tests/test_connectors.py` | official JSON-2 endpoint shape, bearer/database headers, named `fields_get`, pagination, timeout redaction, closed public surface, API-key redaction |
| `tests/test_reporting_cli.py` | read-only CLI commands, existing profile command, manifest and twelve-sheet workbook generation |

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
- equivalent fixture, DEV, and TEST semantic results.

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

### Required UC slice

Build approximately 100–300 sanitized records covering:

- real standard, extended-standard, and custom UC models;
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
environment fingerprint. Recapturing identical target data at a later time
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
PYTHONPATH=src .venv/bin/python -m uc_migration_profiler benchmark \
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

Structural requirements already apply:

- no connector call inside the row loop;
- requests grouped by model;
- match and reference lookup use indexes;
- pagination is deterministic.

## Acceptance traceability

| Milestone criterion | Local status | Remaining evidence |
| --- | --- | --- |
| Same profile path for fixture, DEV, TEST | architecture supports it | live DEV/TEST runs |
| No connector write operation | verified in code/tests | Odoo ACL proof |
| Every import candidate gets one outcome | verified for compact fixture | larger UC slice |
| Exact before/after updates | verified | UC reviewer confirmation |
| Relations resolved in batches | verified structurally | live call/page evidence |
| Duplicate targets are ambiguous | verified | real-key confirmation |
| No numeric IDs in portable manifest | verified | release artifact scan |
| Unchanged saved inputs are identical | verified | retained acceptance artifacts |
| Composite/scoped identities | verified locally | real UC scopes |
| Relational comparison | verified locally | real UC relationships |
| 100–300 sanitized records | not complete | build and review |
| Live DEV and TEST | not complete | execute smoke tests |
| Historical-scale memory | not complete | profile and document |

## Acceptance gate

Local proof-of-concept validation is complete when all 46 tests pass and the
offline commands reproduce the documented result.

UC milestone acceptance additionally requires:

- the reviewed 100–300-record slice;
- live DEV and TEST smoke runs;
- Odoo-side read-only account evidence;
- partner confirmation of Odoo version, routing, context, keys, scopes,
  decimal, and timezone rules;
- approved snapshot/report retention;
- no unresolved high-severity architecture or data-leak issue.

Neither gate authorizes an Odoo write.
