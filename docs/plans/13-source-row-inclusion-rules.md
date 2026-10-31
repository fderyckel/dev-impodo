# Complete row inclusion across Recipes and Odoo transfers

## Status and remaining scope

**Status:** Ranked thirteenth in the cross-plan implementation order as of
2026-10-28. The 2026-10-31 delivery implements guided row inclusion for
Odoo-to-Odoo destination matching, dependency checks, transfer review,
preflight, and execution compilation. Portable Recipe reuse passed a live
remote Odoo 20 Enterprise canary. Native columnar execution stays deferred
unless measurement makes the bounded Python fallback a release blocker.

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
Fresh-Recipe missing-column and ambiguous-column browser rehearsals remain a
separate manual acceptance exercise; they were not represented as live checks.

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
version, and any module versions supplied by the reader. This canary does not qualify the normal execution journal,
interrupted-write recovery, or transfer between two live databases. Those
boundaries retain their separate acceptance tests.

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
load and recovery suites remain required. A live transfer rehearsal between
two distinct Odoo 20 servers, including interrupted loading, remains open;
only one live database was supplied for this delivery. Protected same-target
Odoo updates remain ineligible for Recipe publication, and numeric source
identities remain protected.

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
captures are delivered. The remaining acceptance work is the distinct-server
live transfer and interrupted-load matrix and the manual fresh-Recipe browser
edge cases described above. Native parity is a separate deferred follow-up and
is not an acceptance condition for this delivery.
The default remains **Use every row** for old and new mappings. An inclusion
edit still invalidates dependent evidence, and Recipe publication stores only
portable rule meaning, without source values or observed counts.
