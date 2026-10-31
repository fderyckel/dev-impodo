# Complete row inclusion across Recipes and Odoo transfers

## Status and remaining scope

**Status:** The requested implementation and acceptance scope are complete as
of 2026-10-31. The delivery implements guided row inclusion for
Odoo-to-Odoo destination matching, dependency checks, transfer review,
preflight, and execution compilation. Portable Recipe reuse passed a live
remote Odoo 20 Enterprise canary. The two-server transfer and interrupted-load
matrix also passed. Native columnar execution stays deferred.

The current rule and confirmation behavior are documented in the
[Match data workflow](../developer/workflow/03-match-data.md), and exclusion
accounting is documented in [Prepare data](../developer/workflow/04-prepare-data.md)
and the [canonical staging contract](../developer/contracts/canonical-staging.md).
Completed contract and browser delivery details remain in Git history.

The compiler currently returns `COLUMNAR_ROW_INCLUSION_UNSUPPORTED` and selects
the bounded Python evaluator for the entire dataset. Native execution must not
be advertised or selected for this rule until parity has been proved.

## Documentation and browser qualification

The paired Match data, Prepare data, Final review, and Load workflows now
describe the implemented population boundary. The quality and execution
contracts, workflow registry, and Python code map identify its owners.

Authenticated fictional captures at 1440 by 1024 show the transfer builder,
the complete source counts and exact confirmation, and the protected decision
review. [`capture_transfer_row_inclusion.py`](../../scripts/capture_transfer_row_inclusion.py)
also verifies keyboard mode changes, condition addition and removal, filtered
review, and confirmation. HTTP regressions cover stale confirmations, edited
rules, invalid comparisons, zero matches, and cannot-evaluate values. Existing
Recipe contract tests distinguish missing columns from ambiguous bindings.

The 2026-10-31 fresh-Recipe browser checks now pass in authenticated Edge at
1440 by 1024. The stored fictional Odoo 20 Recipe requires **Row status** only
for row inclusion. One delivery omits that column; another supplies both
**Row status** and **Row-status**, which normalize to the same name. In both
cases, Fresh data shows **Needs attention**, withholds **Use this fresh data**,
and rejects a direct acceptance request with HTTP 422. Neither delivery becomes
accepted source data.

Intake uses the same message for both cases: **No safe detected table contains
every required column.** This check does not establish a browser chooser for
individual replacement columns. The operator removes the incompatible file,
adds a corrected file, and checks it again. Both browser journeys complete that
recovery, accept three corrected rows, and activate the real Recipe compiler
against fictional final Odoo `20.0+e` metadata. The result requires a fresh
confirmation for two inclusions and one exclusion. Keyboard confirmation
persists that exact population and leaves the published Recipe unchanged.

[`test_recipe_row_columns.py`](../../tests/e2e/test_recipe_row_columns.py)
reproduces both journeys with Playwright. Set `IMPODO_BROWSER_CHANNEL` to an
installed Chromium channel, such as `msedge`, and run:

```sh
uv run --frozen --group quality --with playwright python -m unittest tests.e2e.test_recipe_row_columns -v
```

Set `IMPODO_RECIPE_ROW_SCREENSHOT_DIRECTORY` to retain the blocked, corrected,
and fresh-confirmation images. These checks use isolated local storage and
fictional metadata; they do not contact or write to a remote Odoo database.

## Odoo 20 and remote Recipe qualification

Final Odoo `20.0` and `20.0+e` use the shared version policy and HTTPS JSON-2
connection. A Recipe must use the destination's major version. Row rules
evaluate the accepted source snapshot locally. This plan does not widen the
version policy or other load approvals.

The live 2026-10-31 canary captured final remote `20.0+e` metadata and reviewed
three required Contact defaults under the API user's current company. It
published a real Recipe from two rows with one inclusion. Reuse on three
fresh rows required a new confirmation for two inclusions and one exclusion.
The reused Recipe prepared only the two included Contacts. A scoped JSON-2
adapter created and read back those two records, verified the excluded
reference was absent, removed the synthetic records, and verified cleanup.
No key, company label, source value, or Odoo identifier enters the receipt.

[`qualify_row_inclusion_recipe.py`](../../scripts/qualify_row_inclusion_recipe.py)
reproduces that check with an explicit `--allow-write` flag and a private key
file. It writes a sanitized receipt with the application build, reported Odoo
version, and any module versions supplied by the reader. This canary does not
qualify the normal execution journal, interrupted-write recovery, or transfer
between two live databases. The separate matrix below qualifies those paths.

## Odoo-to-Odoo implementation and qualification

Destination-matching contract version 16 stores the shared row policy and
ordered rule-excluded and cannot-evaluate source ordinals. The exact population
hash binds every dataset's counts and decisions. Confirmation requires the
current plan hash. Explicit put-aside decisions remain independent of rule
exclusions; their union counts an overlapping omission only once.

The implementation requires fresh confirmation for exclusions and blocks
unknown columns, invalid comparisons, unreadable typed operands, and empty
retained populations. It reads exact operands instead of normalized identity
keys, preserving padded text and Boolean false. The rule loop performs no
repository or Odoo calls. A protected page shows at most 100 decisions at a
time. Confirmation uses one bounded local operation; the route fixture records
at most six connections and one schema check.

Rule changes invalidate downstream transfer evidence. Fresh preflight checks
the same policy again, and execution compilation verifies frozen decisions
before omitting source rows. An included owner whose required dependency was
excluded stays blocked. Recovery retains the original journal-bound snapshot.
Historical plan versions keep their serialized layout and hashes.

Fixtures cover final 20-to-20 matching with rules and cross-major rejection,
repeated checks, stale confirmation, exclusions during execution compilation,
dependency blocking, and exact frozen-value rechecks. The existing transfer
load and recovery suites remain required. The live rehearsal below now covers
two distinct Odoo 20 servers and interrupted loading. Protected same-target
Odoo updates remain ineligible for Recipe publication, and numeric source
identities remain protected.

## Live two-server transfer and recovery qualification

The 2026-10-31 matrix used a disposable local Community server reporting
`20.0-20261015` and the supplied remote Enterprise server reporting `20.0+e`.
The local Docker stack used the official image
`odoo@sha256:d5e3a26baa044867c826dddc09058f10e2582706bf83c146ea04fea889720c76`,
PostgreSQL 18.6, and the Base and Contacts applications. Only the Odoo port was
published, on literal loopback. Both endpoints used the governed JSON-2
adapters with their corresponding local or remote connection mode.

The official package appends its build date to the version string while its
release tuple reports final `20.0`. Recognition now accepts that valid dated
package form only when the release and edition agree. It preserves the exact
reported version. [Domain regressions](../../tests/domain/test_odoo_compatibility.py)
and [connector regressions](../../tests/integration/odoo/test_connectors.py)
keep contradictory majors, series, editions, stages, and invalid dates blocked.

Each case seeded three uniquely referenced fictional Contacts. The row rule
included two and excluded one. The runner completed source discovery,
explicit related-data review, filtered capture, destination matching, row
confirmation, required-default review, transfer order, review approval,
fresh preflight, execution compilation, journalled loading, and read-back.

| Direction | Load | Committed and verified | Excluded writes | Repeat creates |
| --- | --- | ---: | ---: | ---: |
| Local to remote | Normal | 2 | 0 | 0 |
| Local to remote | Interrupted, then recovered | 2 | 0 | 0 |
| Remote to local | Normal | 2 | 0 | 0 |
| Remote to local | Interrupted, then recovered | 2 | 0 | 0 |

The interrupted cases stopped client processing after Odoo accepted the first
one-row `Model.load()` batch and before its receipt reached the journal. One
row remained in flight. The runner reopened persisted application state with
the same private credential vault and invoked the normal recovery action.
Recovery retained the original journal and snapshot, recognized the accepted
record, and created only the remaining included Contact. Exactly two create
batches and two created records were observed in each case.

Initial attempts encountered remote `context_get` timeouts before any journal
or destination write existed. The runner permits at most two retries only
after verifying that no journal or destination records exist and that the
reviewed snapshot is unchanged. The completed matrix automatically verified
all four loads. Its final remote cleanup was initially unverified; a later
scoped check verified absence of the exact synthetic namespace and completed cleanup.
The receipt records that cleanup recovery.

All synthetic source and destination records are absent. The disposable local
containers, volumes, network, and generated credentials were removed. The
sanitized receipt at `build/acceptance/row-inclusion-transfer/result.json`
records the application build, both versions, image digests, case results,
and cleanup verification without keys, business values, database names, or
Odoo identifiers.

[`qualify_row_inclusion_transfer.py`](../../scripts/qualify_row_inclusion_transfer.py)
reproduces the matrix with explicit local and remote connection arguments,
private API-key files, an output directory, and `--allow-write`. Its
[safety regressions](../../tests/integration/odoo/test_row_transfer_qualification.py)
cover interruption after an accepted reply and cleanup after an unknown unlink
outcome. Failed cleanup preserves a private temporary reference manifest.
This matrix covers scalar Contact creates. Customer-specific models, updates,
and relationship-heavy transfers retain their separate acceptance suites.

## Deferred native execution follow-up

Activate this follow-up only after measurement justifies native execution.
Implement expressions only with parity against `source_condition_matches`
and `row_is_included`. Cover text, numbers, Boolean values, dates,
date-times, blanks, and values that cannot be evaluated.

The native route must preserve evaluation order: decide inclusion before
transforming target fields or relationships. Excluded or blocked rows retain
lineage and complete source accounting; only included rows enter comparison
and execution. A zero-match dataset remains blocked.

Verify identical ordered values, decisions, lineage, issues, and hashes across
batch sizes. Prove bounded memory and the absence of repository or Odoo access
inside source-row loops before changing the compiler capability. Existing row
limits remain in force until the complete route is qualified.

## Completion

The requested implementation, remote Recipe canary, and authenticated transfer
captures are delivered. The missing-column and ambiguous-column Recipe browser
checks and the live two-server transfer and interrupted-load matrix are
complete. Both recovery directions preserved the original execution snapshot,
kept excluded rows out of writes, and created no duplicates. Native parity
remains a separate deferred follow-up and is not an acceptance condition for
this delivery.
The default remains **Use every row** for old and new mappings. An inclusion
edit still invalidates dependent evidence, and Recipe publication stores only
portable rule meaning, without source values or observed counts.
