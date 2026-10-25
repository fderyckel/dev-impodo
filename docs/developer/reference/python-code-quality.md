---
audience: developer
document_type: reference
status: current
---

# Python code quality

Impodo uses one repeatable quality gate so developers, coding agents, and
continuous integration apply the same Python rules. The gate rejects every
Ruff or Basedpyright diagnostic in the checked source. It does not authorize a
static-analysis fix to change business behavior.

## Sources of authority

Each quality artifact owns one decision:

| Artifact | Responsibility |
| --- | --- |
| Accepted contracts, runtime behavior, and focused tests | Define the required program semantics. |
| `pyproject.toml` | Defines the Ruff and Basedpyright configuration and pins their quality dependency versions. |
| `scripts/check_python_quality.py` | Runs the repository checks in their supported order. |
| `.github/workflows/python-quality.yml` | Requires the same command for proposed and accepted changes. |
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

## Run the quality gate

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
5. Run the complete quality gate again and report the direct Basedpyright
   result.

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
