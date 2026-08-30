---
audience: developer
kind: report
status: current
---

# Completed-load correction Phase 6 remote qualification — 2026-08-30

## Decision supported by this report

The implemented correction workflow passes its current hosted Odoo Online 19
Product boundary over HTTPS. The qualification covers 768 scalar changes and
37 exact-existing Unit changes in one protected plan. This result complements
the local qualification. It does not qualify every remote network topology,
identity-field changes, supporting-record creation, Odoo-source corrections,
Integrated Test promotion, or Production correction.

The broader correction scope therefore remains unchanged. A separate decision
must follow new evidence before Impodo grants any additional write capability.

## Evidence boundary

The runner used a user-authorized Odoo Online 19 demo database over HTTPS. The
credential was read from an owner-only local file and was not written to the
result. The retained ignored result is
`.tmp/completed-load-correction-phase6-remote-edu-ifitwala-20260830.json`.
It contains no API key, URL, login, Product value, Unit value, or Odoo record
identifier.

Odoo reported version 19.0+e with Base 19.0.1.3, Product 19.0.1.2, and UoM
19.0.1.0. The observed process peak was 117.453 MiB.

## First attempt and bounded read retry

The first full attempt reached post-write reconciliation, where one exact-ID
HTTPS read timed out after 30 seconds. Impodo did not publish a verified
result. The runner deleted all 859 synthetic Products, and a separate exact
prefix read confirmed that none remained.

This attempt exposed one narrow remote-read gap. `Json2ReadbackReader` did not
use the connector's bounded retry setting even though exact read-back is
read-only and idempotent. It now retries only transport failures and HTTP 429,
502, 503, or 504 responses within that configured bound. The qualification
runner permits two retries for a remote target and none for local loopback.
The writer was not changed: Impodo never retries a write after an uncertain
response.

The focused retry test raises one read timeout and verifies that the exact
read succeeds on the second bounded attempt. The successful live rerun did not
need an additional attempt; its request counts match the local call shape.

## Vectorized comparison

Polars reduced 999 prepared Product intents to one sparse Parquet artifact
containing 768 changed `active` intents. The remaining 231 rows did not enter
Python candidate processing or Odoo review. The measured comparison took
0.015980 seconds and produced a 4,545-byte candidate artifact.

The runner hashes each prepared artifact once for its immutable snapshot
contract. It does not add a per-row or per-value hash.

## Live review and execution

The main protected plan contained 805 Product field corrections:

| Correction shape | Fields |
| --- | ---: |
| Scalar `active` changes | 768 |
| Exact-existing Unit changes | 37 |
| **Total** | **805** |

Review produced no blocker. It read the exact Product targets in 17 pages and
resolved two distinct case-sensitive Unit keys in one bounded request.

Execution grouped identical payloads into 17 Product writes. Each write had a
matching journal-before-write event. The service then used 34 exact-ID Product
reads for the just-in-time check and automatic reconciliation. All 805 fields
were committed and verified with zero fallout and zero Unit writes.

| Measured stage | Wall time |
| --- | ---: |
| Fixture creation | 19.881231 seconds |
| Main review | 11.709545 seconds |
| Execution and read-back | 36.420592 seconds |
| Repeat review | 11.821092 seconds |
| Whole run including cleanup | 100.833865 seconds |

The repeat review used a fresh service, classified all 805 fields as already
corrected, and offered zero writes.

## Safety scenarios

The remote target exercised the same bounded safety shapes as local Odoo:

- A concurrent Product-field change invalidated the reviewed plan before the
  journal or first correction write. The correction write count was zero.
- An injected authorization rejection left two fields failed, zero unknown,
  zero verified, and did not complete the successor binding.
- A write of 50 Products completed and then lost its response. Impodo recorded
  50 unknown outcomes, blocked the remaining Product, and verified the 50
  applied changes by exact read-back. The blocker prevented successor
  completion.

The injected failures operate at the real JSON-2 writer transport boundary.
They test classification and recovery without asking the hosted server to
drop a packet or reject a valid key.

## Cleanup and scope decision

The successful run created 859 synthetic Products and deleted all 859 after
exact read-back. A separate authenticated prefix query confirmed zero
remaining fixture records. Setup and correction made no Unit write.

The current Authoring correction boundary is now qualified at its motivating
Product scale for literal-loopback Odoo 19 and for this hosted Odoo Online 19
HTTPS target class. This does not authorize:

- a correction to a Product identity field;
- creation, update, rename, or merge of a supporting Unit record;
- correction of an Odoo-source Data version;
- correction inside an Integrated Test or Production run;
- an on-premises proxy, custom gateway, or other untested remote topology; or
- a larger Product count.

The committed runner still accepts non-prefixed databases only through the
normal disposable-database guard. This run used an exact, process-local
authorization for the user-approved demo database. It did not weaken the
default guard or create a reusable shortcut.

## Repository verification

The 30 focused correction, retry, orchestration, Polars, and browser tests
passed. Documentation quality and generated-workflow checks also passed.

The full current-workspace suite ran 1,034 tests with 13 skips and two
failures from the preceding mapping-contract revision `25f53d8`:

- The mapping-contract parser now emits its specific version-13 relationship
  resolver error, while one test still expects the older `current contract`
  wording.
- `mapping/page.html` contains 105 lines, while its architecture guard permits
  100.

Neither failure imports or exercises the correction runner, Odoo read-back
adapter, or remote qualification evidence. They remain release-level
repository failures and must be resolved separately; this report does not
present the whole repository suite as green.
