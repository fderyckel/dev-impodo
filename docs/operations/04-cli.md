# Preflight CLI runbook

## Scope

The CLI is the expert, profile-driven route for producing read-only migration
preflight evidence. It is separate from the browser workflow: browser mapping
revisions are not compiled into CLI profiles.

The CLI can read selected Odoo metadata and records, but it cannot create,
write, unlink, import, or run arbitrary model methods.

Use the installed command:

```powershell
impodo-cli --help
```

From a development checkout, use:

```powershell
.\.venv\Scripts\impodo-cli.exe --help
```

## Connection settings

The live JSON-2 connector reads these environment variables:

```powershell
$env:IMPODO_ODOO_BASE_URL = "https://odoo.example.com"
$env:IMPODO_ODOO_DATABASE = "production"
$env:IMPODO_ODOO_API_KEY = "<secret>"
```

Optional settings are `IMPODO_ODOO_TIMEOUT_SECONDS` and
`IMPODO_ODOO_PAGE_SIZE`.

Keep the API key in the process environment or an approved secret store. Do
not put credentials in profiles, command arguments, source files, snapshots,
or committed scripts. Use a dedicated read-only Odoo account.

## Safe run sequence

The profile and source directory are inputs to every relevant step. The
commands below use the repository's product example.

### 1. Validate and prepare the source

This step does not connect to Odoo.

```powershell
impodo-cli profile `
  --profile .\profiles\examples\products.yaml `
  --input .\examples\golden `
  --output .\build\profile\prepared-records.json
```

Resolve profile, typing, identity, and source issues before capturing target
evidence.

### 2. Capture required metadata

```powershell
impodo-cli snapshot-metadata `
  --profile .\profiles\examples\products.yaml `
  --connector json2 `
  --output .\build\snapshots\metadata.json
```

### 3. Capture relevant target records

```powershell
impodo-cli snapshot-records `
  --profile .\profiles\examples\products.yaml `
  --input .\examples\golden `
  --connector json2 `
  --output .\build\snapshots\records.json
```

The connector plans and batches reads by model. Do not replace this with one
Odoo request per source row; that creates an N+1 bottleneck and weakens the
evidence boundary.

For controlled fixtures, `--connector snapshot --snapshot <path>` can replace
the live connector in either snapshot command.

### 4. Classify offline

```powershell
impodo-cli preflight `
  --profile .\profiles\examples\products.yaml `
  --input .\examples\golden `
  --metadata .\build\snapshots\metadata.json `
  --records .\build\snapshots\records.json `
  --output .\build\preflight
```

Use `--preview-dir <directory>` only when rendered workbook previews are
needed for visual verification.

The preflight step makes no network calls. It produces a canonical manifest
and a review workbook. Review `BLOCKED` and `AMBIGUOUS` decisions first, then
`CREATE`, `UPDATE`, and `UNCHANGED` classifications and their field-level
evidence.

## Exit behavior

| Code | Meaning |
| ---: | --- |
| `0` | Command completed successfully |
| `2` | Invalid command-line arguments |
| `3` | Profile, source, path, or value error |
| `4` | Connector or target-read error |
| `6` | Report-generation error |

Treat any non-zero code as a failed run. Do not promote partial output.

For an isolated index-performance diagnostic, run
`impodo-cli benchmark --rows 360000`. It uses synthetic in-memory data and
does not validate a migration or connect to Odoo.

## Evidence rules

- Keep the profile, source files, metadata snapshot, record snapshot, manifest,
  and workbook together.
- Do not edit captured snapshots or the generated manifest.
- Recapture both snapshots when the target, database, profile, or relevant
  source evidence changes.
- A successful preflight is review evidence only. It is not approval, a clean
  migration package, or permission to write to Odoo.

The normative behavior is defined in the
[profile-driven preflight contract](../contracts/04-preflight.md). Profile
syntax is documented in the [profile authoring guide](03-profile-authoring.md).
