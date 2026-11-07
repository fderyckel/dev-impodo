---
audience: developer
document_type: reference
status: current
---

# Morrow Assembly customer practice

Use these fictional exports to practise combining customer records and assigning
payment terms. The same files drive the application qualification runner.
There are 124 physical source rows in ten CSV files: 62 rows in each delivery.
No customer export, credential, live database ID, or private chat row was copied.

The current runner uses names, email addresses, phone numbers, street addresses,
postcodes, cities, and the customer payment-term relationship. Other columns are
retained source evidence; they do not establish additional mapping coverage.

## Select one delivery and one company

Use the five files under `deliveries/2026-04-15/` for the baseline. The five files
under `deliveries/2026-05-15/` are a complete replacement of that customer input
shape for a fresh Recipe run. Their absence of products or other records does
not request target deletes. The dates identify fictional deliveries.

| File | Rows per delivery | Meaning |
| --- | ---: | --- |
| `customers_core.csv` | 18 | One customer account with its portable reference and party link. |
| `party_details.csv` | 18 | One party per partition, with its name and contact details. |
| `addresses.csv` | 18 | One selected address per party and partition. |
| `payment_terms.csv` | 4 | Two intended terms for each company. |
| `payment_term_lines.csv` | 4 | One complete-balance schedule intent for each term. |

NORTH means **Morrow Assembly North**. SOUTH means **Morrow Assembly South**.
Select one operation company per run and include only that company's mapped
rows. `CompanyCode` selects source rows. `CompanyName` supplies the actual
`res.company.name` used in the term's scope. An operation company does not
silently assign ownership to the shared contact records.

Use an allocated disposable target for writes. The setup needs contact and
accounting models, including `account.payment.term` and its line model. The
runner checks captured fields, actual Odoo major, access, and the empty fixture
scope before seeding. It starts no service or browser session.

## Combine customer exports

Combine `customers_core.Partition + Party` with
`party_details.Partition + RecId` and with `addresses.Partition + Party`.
Keep these values as text. The complete review must produce 18 customer rows,
with no missing matches or multiplied rows. Then select the nine NORTH or nine
SOUTH rows for the operation.

Use `party_details.Name` as the contact name. Use `CustomerRef` as Odoo's
Reference (`ref`). Keep `AccountNum`, `Party`, and each table's `RecId` separate;
they have different meanings. The repeated `Name` and `Status` labels do not
justify combining their values automatically.

The customer-only exercise cleans the selected name and email values, cleans
phone formatting, and maps the selected address. Against the declared seed,
one company has seven creates, one update, and one unchanged contact. Read back
the exact values, reopen the saved execution evidence, reconcile, and require
nine unchanged contacts on the repeat comparison.

Use the following field choices after combining the three tables:

| Selected source | Odoo field | Rule |
| --- | --- | --- |
| `CustomerRef` | Reference (`ref`) | Preserve the text as the business key. |
| Party `Name` | Name (`name`) | Trim and collapse repeated whitespace. |
| Party `Email` | Email (`email`) | Trim, collapse whitespace and lowercase; treat empty values as equivalent to blank. |
| Party `Phone` | Phone (`phone`) | Remove spaces, hyphens and dots between digits; replace an initial `00` with `+`. |
| Address `Street`, `Postcode`, `City` | `street`, `zip`, `city` | Trim and collapse whitespace; preserve postcode as text. |

For example, the two partitions both contain `PTY-0001`. Joining on Party
alone would confuse those records. `PART-N + PTY-0001` identifies the NORTH
customer, while `PART-S + PTY-0001` identifies the SOUTH customer.

## Prepare and assign payment terms

`TermCode` is the source identity, not a promised Odoo field. Find a target
term by its Name within its Company. The source identity of an incoming term
is `CompanyCode + TermCode` in that order.

The baseline payment-term exercise runs three separate application workflows:

1. **Headers.** Reuse NET30 and create NET45 for the selected company. Expect
   one create and one unchanged header. Stock Odoo creates one 100% zero-day
   schedule line with a new header. Check that exact intermediate state.
2. **Schedule.** Capture those actual lines and update NET45's generated line
   to 45 days after invoice date. NET30 retains its 30-day schedule. Expect
   one update and one unchanged line, with zero line creates.
3. **Customers.** Assign the complete terms to the selected nine customers.
   Expect seven creates, one update, and one unchanged contact. Verify that
   each selected contact points to the intended company's term.

The raw `Balance` label and zero `ValueAmount` express a legacy complete-balance
rule. The reviewed mapping implements it as one `percent` line with
`value_amount = 100`, `delay_type = days_after`, and `nb_days = Days`.
`Balance` is not an Odoo selection code. Source `Sequence` is provenance;
Odoo's payment-term line model has no sequence field. Do not append another
100% line after Odoo has generated one.

A separate **incoming-immediate** variant deliberately changes the selected
NET45 source intent to a new immediate term. Its intended schedule is the
stock 100% zero-day default. It therefore tests one new term, one existing
contact update, and one new contact create in the same load without
pre-creating that term. Including the reused NET30 header, its comparison is
two creates, one update, and one unchanged record. Both changed contact
relationships must retain their incoming dependency after saved-workspace
reopening.

## Run the application checks

The runner accepts `enrichment`, `payment-terms`, or `incoming-immediate`.
`all` is available for controlled runs, which use an independent stateful Odoo
boundary. Both `authoring` and `recipe` traverse normal source acceptance,
combination, preparation, comparison, saved execution, writer, and read-back
services. Recipe mode publishes reviewed rules and applies them to the fresh
May delivery before the first write.

From the checkout root, for example:

```sh
UV_CACHE_DIR=.uv-cache .tools/uv/uv run --frozen --extra test --group quality python -m scripts.qualify_practice_customers \
  --controlled --expected-major 19 --mode LOCAL --company NORTH \
  --exercise all --entrypoint authoring --output-directory /tmp/morrow-controlled-19-local-authoring
```

Use a new output directory for each run. Repeat the applicable case for Odoo 20,
REMOTE access, and Recipe mode. Controlled REMOTE describes the transport
contract; it is not evidence of a live remote target or Linux deployment.

A live run requires an allocated descriptor and explicit `--allow-write` before
credential files are read. It accepts one exercise per fresh target. The
runner retains the target, journal, application evidence, and private setup
ledger after the run; automatic cleanup and recovery are not implemented.
Portable `result.json` files contain statuses, counts, and hashes. Keep the
`private/` subtree private.

Supply a private JSON descriptor outside this pack. Store the two API keys in
separate private files beside the descriptor; never put their values in a
command or committed file. For example:

```json
{
  "contract_version": 1,
  "disposable": true,
  "purpose": "morrow_practice_qualification",
  "major": 20,
  "mode": "LOCAL",
  "base_url": "http://127.0.0.1:8069",
  "database": "impodo_scenario_morrow20_trial",
  "read_api_key_file": "/private/morrow/read.key",
  "write_api_key_file": "/private/morrow/write.key",
  "local_config": "/private/odoo20/config/morrow.conf",
  "python": "/private/odoo20/.venv/bin/python",
  "odoo_bin": "/private/odoo20/source/odoo-bin"
}
```

LOCAL requires a literal loopback address, the same database and port in the
configuration, and a supported installation inside that configuration's
workspace. Both the selected Python executable's resolved path and `odoo-bin`
must stay inside that workspace. The Python entry point must retain its own
installed Odoo dependencies. REMOTE requires HTTPS and omits the three local
paths. Provisioning and starting Odoo remain the operator's responsibility.

```sh
UV_CACHE_DIR=.uv-cache .tools/uv/uv run --frozen --extra test --group quality python -m scripts.qualify_practice_customers \
  --target-descriptor /private/morrow/connection.json --allow-write \
  --expected-major 20 --mode LOCAL --company NORTH --exercise payment-terms \
  --entrypoint authoring --output-directory /tmp/morrow-live-20-payment-terms
```

The target must have `base`, `contacts`, and `account` installed. Setup refuses
an existing Morrow customer or term and requires a fresh, single-company seed
scope. It creates NORTH and SOUTH, seeds four contacts and two NET30 terms,
and verifies their initial values. It does not reset a failed attempt.

To practise in your existing Impodo session, prepare a separate fresh target
without running the automated migration:

```sh
UV_CACHE_DIR=.uv-cache .tools/uv/uv run --frozen --extra test --group quality python -m scripts.prepare_practice_target \
  --target-descriptor /private/morrow/connection.json --allow-write \
  --expected-major 20 --mode LOCAL --output-directory /tmp/morrow-manual-baseline
```

Then connect Impodo to that disposable database. Upload the April files, follow
the combinations and field choices above, and check your results against the
business answers. The setup command leaves your current Impodo server and
browser running. Use a fresh allocation for a later automated qualification;
the automated runner refuses an already seeded scope.

The full execution snapshots include the reviewed parent headers in later
stages. Their counts are:

| Phase | Create | Update | Unchanged | Verified |
| --- | ---: | ---: | ---: | ---: |
| Customer enrichment | 7 | 1 | 1 | 9 |
| Payment-term headers | 1 | 0 | 1 | 2 |
| Generated schedule update | 0 | 1 | 3 | 4 |
| Customer term assignment | 7 | 1 | 3 | 11 |
| Incoming immediate variant | 2 | 1 | 1 | 4 |

Each phase must have zero blocked or ambiguous rows. After loading, every
reviewed row must be unchanged on repeat comparison. Each exercise also
checks that the excluded company's seed and term schedule remain unchanged.

For focused missing-parent, ambiguous-parent, historical-evidence, and
read-only lookup regressions, run:

```sh
.venv/bin/python -m unittest tests.domain.preparation.test_incoming_reference_shapes tests.domain.execution.test_incoming_reference_snapshot tests.application.workspace.execution.test_nested_reference_scope -v
```

## What the current evidence establishes

All three exercises passed live Odoo 19 and 20 checks through both authoring
and Recipe entrypoints on 6 November 2026. Live checks used native LOCAL reads
and JSON-2 writes on disposable Community targets with PostgreSQL 18.6.
They verified exact business values, complete schedules, saved-workspace
reopening, reconciliation, unchanged repeats and preservation of the excluded
company. The [recorded receipts](qualification-evidence.json) contain counts,
input hashes and the independently verified Odoo source pins.

Controlled tests cover both majors with LOCAL and REMOTE contracts, including
missing or ambiguous parents and prohibited parent writes. Live REMOTE HTTPS,
visual browser behavior, Linux and Windows execution, interruption recovery
and hosted CI were not run for this pack. Hosted workflows remain paused.
No user's running Impodo server or browser was restarted.

The May delivery changes one phone, one payment-term assignment, and a source
language label. The current runner does not map Language, so it does not
qualify review of the new Italian choice. Extra manufacturing, category,
account, patch, supplier-repair, Excel, and fault-injection exercises are
outside this customer pack's current qualification.

`expected-business-results.json` declares the business answers independently.
`target-seed-projection.json` declares the four initial contacts and two NET30
terms. `manifest.json` pins the input and oracle bytes. Source rows and target
IDs observed during a run belong to its private operational evidence.
Status labels inside the pinned seed and answer files describe their original
drafting state. Current qualification results are recorded in
`qualification-evidence.json`.
