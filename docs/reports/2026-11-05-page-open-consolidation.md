---
audience: developer
document_type: report
status: current
---

# Consolidating Stage 2, 3 and 5 page reads

The complete saved pages now open each required local database once per
synchronous operation. The previous optimization reduced SQL and repeated
structural validation, but its separate admission and page scopes still
reopened stores. Match data also opened stores for a separate source gate.
This change removes those repeated physical opens while retaining the checks.

## Matched measurements

The comparison uses preserved before and after source snapshots with identical
fixtures and benchmark code. Only five application files differ: the access
middleware, source gate, and three page routers. Both snapshots use Python
3.12.14 and the same frozen dependency versions in separate environments.
No test or other benchmark ran concurrently with this measurement. Cases
alternate which build runs first to limit host-load and cache-warmth bias.

Each fictional workspace has one source row, two scalar mapping fields, and a
saved target for Odoo 19 or Odoo 20 in local or remote access mode. The summary
also performs the persisted comparison-intent inspection used by normal local
servers. Earlier small fixtures omitted that manager and counted six opens
for summary; this fixture includes its seventh open before consolidation.

Every page warms once, followed by seven measured requests for each of the
four version/access combinations. Each build therefore records 84 samples.
The table reports the median of the four case medians. SQL ranges distinguish
local and remote access. These are local request timings with fictional Odoo
boundaries, not Odoo network timings or live workspace measurements.

| Saved page | Physical opens, before → after | Connection setup, before → after | Total elapsed time, before → after | SQL statements, before → after | Store checks, before → after |
| --- | ---: | ---: | ---: | ---: | ---: |
| Stage 2: Odoo data | 6 → 4 | 41.4 → 27.7 ms | 82.7 → 64.1 ms | 68–70 → 64–66 | 4 → 3 |
| Stage 3: Match data | 8 → 4 | 55.6 → 27.9 ms | 123.3 → 80.6 ms | 125–127 → 114–116 | 6 → 3 |
| Stage 5: Final review | 7 → 4 | 52.0 → 27.9 ms | 95.6 → 65.9 ms | 79–81 → 75–77 | 4 → 3 |

All measured requests had zero lock retries. Both builds use the complete
store-validation counter, so the schema-check comparison has the same meaning.
Column-catalogue reads agree with those checks. Connection setup falls by
about 33%, 50% and 46%; total elapsed time falls by about 22%, 35% and 31%.
Wall-clock values are observations on this host and are not CI assertions.

- [Raw before samples](evidence/2026-11-05-page-opens-before.json).
- [Raw after samples](evidence/2026-11-05-page-opens-after.json).
- [Benchmark](../../scripts/benchmark_stage235.py).

The reproducible saved-page profile includes the persisted comparison-intent
reader:

```sh
UV_CACHE_DIR=.uv-cache .tools/uv/uv run --frozen --extra test --group quality python scripts/benchmark_stage235.py --saved-pages-only --samples 7 --output /tmp/impodo-page-opens.json
```

## Ownership and safety

The four retained owners belong to distinct stores: registry, Workspace
linkage, Workspace engine, and Data version. A page still needs each store in
this fixture. Removing another open would require removing a necessary read,
changing storage ownership, or keeping connections across operations. None of
those changes is part of this optimization. More complex ownership graphs may
legitimately need additional stores.

[`run_workspace_page_read`](../../src/impodo/web/security.py) runs parent
authorization and journey policy before any page projection or active-job
inspection, on the same worker as the local render. Closed-workspace and
Recipe-run redirects retain their existing decisions. The Match data source
gate runs before its active-job check and editor projection in this scope.
The summary's persisted job-intent inspection reuses the registry owner.
Other routes keep their existing admission boundary.

The middleware rejects a successful page that bypasses admission. Every owner
closes before the route resumes on the event loop, including redirects and
rendering errors. No owner spans middleware `call_next`, response streaming,
remote Odoo work, or background dispatch. Successful structural validation is
reused only within this operation; lineage and identity checks still run on
each access. Transactions, evidence hashes, saved decisions, authorization,
idempotency, and Odoo version policy remain unchanged.

The performance skill now requires complete page budgets to include admission
and source gates. Repository tests remain independent of local skill files.

## Verification and limits

The page admission suite covers denied access without child reads or job
inspection, rejection of an admission bypass, active-job redirects, closed and
Recipe-run journeys, release after rendering failure, and independent opens
and validation on the next request. The stage regressions enforce four-open
budgets, SQL budgets, expected page content, and unchanged evidence after
rejected actions for Odoo 19 and Odoo 20 with local and remote access.

Adjacent coverage includes the real-DuckDB owner and transaction suite, scoped
schema validation, session/host/origin controls, worker boundaries, stale Odoo
source recovery, completed-workspace correction, and generated Recipe reuse
for both supported majors in both access modes. The Final review regressions
retain prepared comparison evidence and confirm that job dispatch occurs
after owners close.

The final focused run passed 18 tests and 26 subtests, including the five
documentation contract tests. The adjacent owner, validation, security,
worker, and generated Recipe matrix passed 27 tests and four subtests. Six
alternative supplied-hierarchy Recipe cases were deselected in that run; the
four generated cases cover Odoo 19 and 20 in local and remote modes. The stale
Odoo source and correction suites also passed in the earlier focused run.
An initial admission test setup and the documentation registry's expected
helper list needed correction; their focused reruns passed. No safety or SQL
budget was relaxed.

The complete repository gate passed: Ruff, Basedpyright with zero diagnostics,
compilation, whitespace, all 88 architecture tests, documentation checks, and
the code-documentation inventory. The changed benchmark and admission test
passed a separate Ruff check. The updated performance skill passed validation.
Pytest emitted one third-party plugin deprecation warning.

Live retiming of this second change requires a restart of the user's server.
The existing server and browser were left running; no live store was opened
by a benchmark. Linux/PostgreSQL live qualification, live Odoo calls, browser
rendering, and large-data latency were not measured in this change. The
[earlier live check](2026-11-05-stage235-performance.md#live-saved-workspace-check)
belongs to the preceding build and does not establish this change's live gain.
