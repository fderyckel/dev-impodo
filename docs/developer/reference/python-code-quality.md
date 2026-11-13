---
audience: developer
document_type: reference
status: current
---

# Python code quality

Impodo uses a focused Python gate while code is being changed and one complete
repository gate before delivery. The Python gate rejects every Ruff or
Basedpyright diagnostic in the checked source. The repository gate also
reports the current architecture inventory and enforces architectural
invariants, test organization, static-asset ownership, and documentation
rules. Neither gate authorizes a static-analysis fix to change business
behavior.

## Sources of authority

Each quality artifact owns one decision:

| Artifact | Responsibility |
| --- | --- |
| Accepted contracts, runtime behavior, and focused tests | Define the required program semantics. |
| `pyproject.toml` | Defines the Ruff and Basedpyright configuration and pins their quality dependency versions. |
| `scripts/check_python_quality.py` | Runs the focused Python checks in their supported order. |
| `scripts/check_repository_quality.py` | Runs every required repository guardrail before delivery. |
| `.github/workflows/python-quality.yml` | Runs the repository gate and portable behavioral tests on Linux, macOS, and Windows, and the default performance tests on Linux. |
| `.github/workflows/*-quality.yml` | Runs host-specific tests only on the platform that owns them. |
| Maintainer delivery and release approval | Requires the complete local gate before an authorized direct push to `main`. Public releases also require the hosted qualification described below. |

Ruff and Basedpyright are complementary:

| Concern | Owner |
| --- | --- |
| Imports, modernization, unused code, suspicious non-type patterns, and Ruff formatting | Ruff |
| Annotations, type compatibility, narrowing, protocols, and interface conformance | Basedpyright |
| Intended behavior when a suggestion could change semantics | Accepted contracts and focused tests |

When both tools report the same line, satisfy both diagnostics when possible.
If an exact duplicate needs one owner, retain the diagnostic that matches this
table and disable only the duplicate in the non-owning tool. Record the reason
beside the narrow configuration or expression.

## Run the Python quality gate

Run this command from the repository root:

```powershell
uv run --frozen --group quality python scripts/check_python_quality.py
```

The command uses the versions locked in `uv.lock` and performs these checks:

1. Ruff checks `src/impodo` and the quality runner.
2. Basedpyright runs in Standard mode and reports every type diagnostic.
3. Python compiles `src/impodo`.
4. Git checks the working diff locally and the complete pull-request or push
   range in continuous integration for whitespace errors.

The gate does not replace focused behavioral tests. Run the tests owned by the
changed route, service, domain rule, adapter, or repository after the static
checks pass.

## Main integration during stabilization

On 7 November 2026 the maintainer adopted direct integration on `main`. Agents
inspect shared changes, save a delivery cutoff, and check one fixed candidate.
Commit and push the exact checked tree when authorized. Keep later edits for
the next delivery and preserve the running Impodo session. The owning
[release plan](../../plans/17-first-public-release.md) records this process.

## Run the complete repository gate before delivery

Before committing, pushing, or handing off any repository change, run:

```powershell
uv run --frozen --group quality python scripts/check_repository_quality.py
```

The command runs the Python quality gate, prints the current architecture
inventory, discovers every architecture test, checks repository documentation,
and checks Python code documentation. Module counts, import counts, and graph
digests provide review information without blocking an intentional structural
change. The architecture tests still reject unclassified modules, forbidden
dependencies, runtime cycles, and violations of the test-module and
static-asset reviewability limits.

Any failure blocks delivery even when it appears pre-existing, unrelated to
the current change, or owned by another concurrent agent. Report the exact
failure and coordinate its owner. Do not commit, push, or describe the tree as
ready unless the gate passes or a human explicitly names and waives that
failure for the delivery. Focused behavioral tests remain required for the
responsibility that changed.

The workflow runs the complete gate and a separate behavioral matrix on Linux,
macOS, and Windows. The behavioral matrix runs all tests except
`tests/platform/` and `tests/performance/`; its Linux job also runs the default
performance suite. A final job requires both matrices to pass and retains the
workflow and job identifiers `Python quality` and `quality` so an existing
required-check rule continues to match `Python quality / quality`.
Host-specific suites live below `tests/platform/` and run in separate workflows;
they are not discovered by the portable repository gate. See
[Platform support and testing](platform-support-and-testing.md).

During platform stabilization, all repository workflows require manual dispatch
by the maintainer. Pushes and pull requests do not start hosted jobs. Keep them
disabled until the manual-trigger configuration is merged into `main`, then
enable them for manual use. Select the intended branch when starting a run and
record its exact commit. A manual-only trigger does not waive required checks
or make an unexecuted check pass.

## Keep type diagnostics visible

The repository has no Basedpyright baseline. Do not create one or run
`basedpyright --writebaseline`. Correct the type contract and support the change
with a focused behavioral test. When the correct runtime contract is unclear,
keep the diagnostic visible and ask for a semantic decision instead of
guessing or hiding the error.

## Review fixes safely

Use this sequence for a cleanup or refactor:

1. Run the quality gate without changing files and group findings by rule and
   responsibility.
2. Apply one bounded family of Ruff fixes that the tool marks safe.
3. Inspect the complete diff and run the focused tests for the affected code.
4. Review every unsafe Ruff fix and every manual type correction against the
   runtime contract before editing.
5. Run the Python quality gate again and report the direct Basedpyright result.
6. Run the complete repository gate before delivery.

Do not use blanket `noqa`, `type: ignore`, directory exclusions, or disabled
diagnostic categories to obtain a green result. A necessary narrow suppression
must state why runtime behavior cannot be represented safely and must identify
the smallest affected expression.

Ruff is the formatting owner, but repository-wide formatting is not yet a
quality gate because the existing tree has substantial formatting drift. Do
not mix a broad formatting rewrite with a semantic change. New files should be
formatted with Ruff, and an existing file should be formatted only in a
separately reviewed scope.

## Verify concurrency performance on Python 3.12

Python 3.12 is the current implementation and benchmark target on macOS and
Windows. Do not make a concurrency change depend on Python 3.14, free-threaded
Python, or newer concurrency APIs without an explicit runtime decision.

Every change to thread counts, worker counts, native thread pools, or parallel
request dispatch requires focused correctness tests and before-and-after
performance measurements. Use one fixed candidate and identical fixtures on
each host, change one concurrency setting at a time, and repeat measurements
at least three times. Keep first and repeat runs separate. Include related
data, data with issues, wide inputs, and supported compatibility paths when
those responsibilities are affected.

Record end-to-end and phase elapsed time, CPU time, sampled process memory,
effective concurrency, and applicable database or remote-request activity.
Verify unchanged values, ordering, identities, evidence bindings, findings,
cancellation, and recovery. Measure diagnostic collection overhead separately
and preserve its failure isolation. A faster isolated query or a requested
thread count alone does not prove a workflow improvement.

Report macOS and Windows evidence separately. User diagnostic bundles provide
operational observations; same-fixture controlled runs provide benchmark
evidence. A missing platform, interrupted capture, or simulated Windows test
does not qualify real Windows performance. Keep the existing default until
repeatable gains and bounded resource use justify changing it. The
[preparation performance workflow](../workflow/04-prepare-data.md#bounded-native-threads-and-performance-evidence)
owns the current Stage 4 controls and benchmark.

## Retain performance results from shared diagnostics

Whenever the maintainer shares an Impodo diagnostic ZIP, inspect its available
performance evidence even when the main question concerns another issue.
Update one durable private file named **Impodo performance history** beside
the user's diagnostic files. Read the current history before updating it and
preserve the existing file identity. This is a standing working instruction;
the maintainer does not need to request it again for each ZIP.

- Record the original ZIP's filename and durable file reference, capture time,
  analysis date, and evidence schema version. Keep one observation per attempt.
  Deduplicate overlapping ZIPs using attempt ID and build. Merge more complete
  later captures without counting the same attempt twice, and flag conflicts.
- Retain available execution-time build, Python and native-library versions,
  platform, machine capacity, workload shape and route, requested and effective
  concurrency, durations, CPU time, sampled memory, database or remote-request
  counts, outcome, and coverage. Preserve original units and distinguish nested
  or inclusive timings.
- Represent missing measurements as unavailable, never zero. Record an explicit
  unavailable-performance entry for an older ZIP without capture. Keep failed,
  cancelled, interrupted, and incomplete attempts distinct from successful runs.
- Compare previous observations only when machine, input and mapping, phase,
  runtime, and configuration establish comparability. Identify the intentionally
  changed variable. Row counts or CPU specifications alone do not prove the
  same workload or machine. Show absolute and percentage changes only when
  valid; label other comparisons as contextual observations, not proven gains.
- Keep macOS and Windows evidence separate. Include a brief history update in
  the diagnostic response: what improved, regressed, remained inconclusive, or
  needs a paired benchmark. Do not infer disk, antivirus, network, or server
  causes solely from elapsed time.
- Keep the private history and original ZIPs outside Git. Retain sanitized
  operational measurements and provenance without source values, credentials,
  paths, or machine names. If durable saving is unavailable, report that the
  history was not updated. Do not automatically send records to other services.

## Upgrade tools deliberately

Upgrade Ruff, Basedpyright, or uv in a dedicated change. Update the pin and
lockfile, run the quality gate, classify new diagnostics, and review any
changed fix behavior before accepting the upgrade. Do not create a baseline
merely because a new tool version reports additional issues.
