---
audience: developer
document_type: reference
status: current
---

# Python code quality

Impodo uses a focused Python gate while code is being changed and one complete
repository gate before delivery. The Python gate rejects every Ruff or
Basedpyright diagnostic in the checked source. The repository gate also
enforces architecture, test organization, static-asset ownership, and
documentation rules. Neither gate authorizes a static-analysis fix to change
business behavior.

## Sources of authority

Each quality artifact owns one decision:

| Artifact | Responsibility |
| --- | --- |
| Accepted contracts, runtime behavior, and focused tests | Define the required program semantics. |
| `pyproject.toml` | Defines the Ruff and Basedpyright configuration and pins their quality dependency versions. |
| `scripts/check_python_quality.py` | Runs the focused Python checks in their supported order. |
| `scripts/check_repository_quality.py` | Runs every required repository guardrail before delivery. |
| `.github/workflows/python-quality.yml` | Runs the portable repository gate on Linux, macOS, and Windows. |
| `.github/workflows/*-quality.yml` | Runs host-specific tests only on the platform that owns them. |
| GitHub branch protection | Must require the stable `Python quality / quality` check before merge. |
| `AGENTS.md` | Tells coding agents how to apply the policy safely. |

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

## Run the complete repository gate before delivery

Before committing, pushing, or handing off any repository change, run:

```powershell
uv run --frozen --group quality python scripts/check_repository_quality.py
```

The command runs the Python quality gate, verifies the reviewed architecture
inventory, discovers every architecture test, checks repository documentation,
and checks Python code documentation. The architecture suite includes the
test-module and static-asset reviewability limits.

Any failure blocks delivery even when it appears pre-existing, unrelated to
the current change, or owned by another concurrent agent. Report the exact
failure and coordinate its owner. Do not commit, push, or describe the tree as
ready unless the gate passes or a human explicitly names and waives that
failure for the delivery. Focused behavioral tests remain required for the
responsibility that changed.

The workflow fans the complete gate out to Linux, macOS, and Windows. A final
job retains the workflow and job identifiers `Python quality` and `quality` so
an existing required-check rule continues to match `Python quality / quality`.
Host-specific suites live below `tests/platform/` and run in separate workflows;
they are not discovered by the portable repository gate. See
[Platform support and testing](platform-support-and-testing.md).

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

## Upgrade tools deliberately

Upgrade Ruff, Basedpyright, or uv in a dedicated change. Update the pin and
lockfile, run the quality gate, classify new diagnostics, and review any
changed fix behavior before accepting the upgrade. Do not create a baseline
merely because a new tool version reports additional issues.
