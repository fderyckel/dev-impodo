# P4 representative migration result

**Status:** Passed on 2026-08-06.

## Scope

The practical path was exercised against a newly created disposable local
Odoo 19 database named `impodo_p4_20260806`. The pre-existing `odoo19_dev`
database was not used or changed.

The deterministic sanitized fixture contained:

| Record type | Rows |
| --- | ---: |
| Product categories | 10 |
| Contacts | 100 |
| Products | 40 |
| **Total** | **150** |

Five categories were seeded unchanged, ten contacts were seeded with older
values, and ten products were seeded with older values. This made the live
preview exercise creates, explicit updates, unchanged records, and incoming
category relationships.

## Observed result

| Stage | Create | Update | Unchanged | Failed | Unknown |
| --- | ---: | ---: | ---: | ---: | ---: |
| First preview | 125 | 20 | 5 | 0 | 0 |
| Execution | 125 | 20 | — | 0 | 0 |
| Odoo read-back | — | — | 150 verified | 0 fallout | 0 |
| Repeat preview | 0 | 0 | 150 | 0 | 0 |

The final exact-key target capture contained 10 categories, 100 contacts, and
40 products. No duplicate, ambiguous, blocked, failed, missing, differing, or
unknown row remained. The second read-only preview proposed no write, proving
idempotency for this representative scope.

## Reproduction

The opt-in runner is
[`scripts/p4_representative_runner.py`](../../scripts/p4_representative_runner.py)
and the mapping is
[`profiles/examples/p4_representative.yaml`](../../profiles/examples/p4_representative.yaml).
The runner refuses non-loopback URLs and databases outside the `impodo_p4_`
namespace. It generates sanitized source rows in a temporary directory, reads
the API key from a private file, and never prints or persists the credential.

Runtime and memory were treated as observations only; no 60-second or 512-MiB
release gate was applied.
