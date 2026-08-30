---
audience: developer
kind: report
status: current
---

# Completed-load correction Phase 6 local qualification — 2026-08-30

## Decision supported by this report

The implemented correction workflow passes its current local Odoo 19
boundary. The qualification covers 768 scalar Product changes and 37
exact-existing Unit changes in one protected plan. It does not qualify remote
HTTPS Odoo, identity-field changes, supporting-record creation, Odoo-source
corrections, Integrated Test promotion, or Production correction.

The broader correction scope therefore remains unchanged. A separate decision
must follow new evidence before Impodo grants any additional write capability.

## Evidence boundary

The runner used Odoo 19 on literal loopback against a temporary database named
`impodo_correction_20260830`. The database was cloned from the sanitized P4
fixture, not from `odoo19_dev`. The runner created synthetic Product records,
deleted every created record after exact read-back, and verified that none
remained. The Odoo server was then stopped, the temporary API key was removed,
and the temporary database was dropped.

The retained non-secret result is
`.tmp/completed-load-correction-phase6-local-20260830.json`. It contains no API
key, URL, user identity, Product value, Unit value, or Odoo record identifier.
The opt-in runner is
[`scripts/qualify_completed_load_correction.py`](../../scripts/qualify_completed_load_correction.py).

Odoo reported version 19.0 with Base 19.0.1.3, Product 19.0.1.2, and UoM
19.0.1.0. The observed process peak was 120.891 MiB.

## Vectorized comparison

The fixture started from 999 prepared Product intents. The previous and
corrected Parquet artifacts were each read through the production Polars
correction comparison. Polars wrote one sparse 4,545-byte candidate artifact
containing the exact 768 changed `active` intents. The remaining 231 rows did
not enter Python candidate processing or Odoo review.

The observed comparison, including construction of the small prepared fixture
files and sparse candidate adaptation, took 0.011784 seconds. The runner
hashed each prepared artifact once for its immutable snapshot contract. It did
not add a per-row or per-value hash.

## Live review and execution

The main protected plan contained 805 Product field corrections:

| Correction shape | Fields |
| --- | ---: |
| Scalar `active` changes | 768 |
| Exact-existing Unit changes | 37 |
| **Total** | **805** |

Review produced no blocker. It read the exact Product targets in 17 pages of
at most 50 IDs. It resolved the two distinct case-sensitive Unit keys in one
bounded `uom.uom` request and reused those protected IDs for all 37 Products.

Execution grouped identical payloads into 17 Product writes. Each call had a
matching journal-before-write event. The service then used 34 exact-ID Product
read calls: 17 immediately before transport and 17 for automatic
reconciliation. All 805 corrections were committed and verified with zero
fallout.

The correction capability made no `uom.uom` write. Fixture setup also made no
Unit write. Setup selected two unique existing Units in the same Odoo 19 Unit
family and created only synthetic Products.

| Measured stage | Wall time |
| --- | ---: |
| Main review | 0.111945 seconds |
| Execution and read-back | 0.667120 seconds |
| Repeat review | 0.115564 seconds |
| Whole run including setup and cleanup | 2.735333 seconds |

The repeat review used a fresh `CorrectionReviewService`, repeated the same
bounded call shape, classified all 805 fields as already corrected, and
offered zero writes. Durable job reuse and reload remain protected by the
focused correction-job and repository tests; this live runner does not claim
a process-crash acceptance result.

## Safety scenarios

The same disposable target exercised three bounded failure shapes:

- A concurrent Product-field change invalidated the reviewed plan before the
  journal or first correction write. The observed correction write count was
  zero.
- An injected known authorization rejection left two fields failed, zero
  unknown, zero verified, and did not complete the successor binding.
- A write of 50 Products completed in Odoo and then lost its response. Impodo
  recorded all 50 outcomes as unknown, blocked the remaining Product, read the
  exact IDs back, and verified the 50 changes that had reached Odoo. The
  remaining blocker prevented successor completion.

The rejection and lost-response injections occur at the real JSON-2 writer
transport boundary. They prove classification, stop behavior, and read-back;
they do not claim that the disposable server itself lost a network packet.

## Target-class and scope decision

The current Authoring correction boundary is qualified for literal-loopback
local Odoo 19 at the motivating Product scale. The runner also accepts a
non-loopback HTTPS target, but no disposable remote target or credential was
available for this run. Remote Odoo correction therefore remains unqualified.

This result does not authorize:

- a correction to a Product identity field;
- creation, update, rename, or merge of a supporting Unit record;
- correction of an Odoo-source Data version;
- correction inside an Integrated Test or Production run; or
- a larger Product count.

## Reproduction

Run only against an explicitly disposable database whose name begins with
`impodo_correction_`:

```console
PYTHONPATH=src:. .venv/bin/python \
  scripts/qualify_completed_load_correction.py \
  --base-url http://127.0.0.1:8069 \
  --database impodo_correction_example \
  --api-key-file /private/path/to/key \
  --output .tmp/completed-load-correction-phase6-local.json
```

For remote acceptance, use a non-loopback HTTPS URL and a disposable database
with the same required prefix. The credential must be able to read, create,
write, and delete synthetic Products. The correction scope itself still
writes only `product.template.active` and `product.template.uom_id`.
