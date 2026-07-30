# Source catalog contract

## Purpose

The source catalog is the first Stage B artifact. It describes an immutable
project source file before a mapping exists, allowing a data manager to inspect
what was imported without writing YAML.

Every catalog is bound to:

- the project source-file identifier;
- the registered display name and byte size;
- the exact registered SHA-256 hash;
- the catalog contract version and inspection timestamp.

Inspection recalculates the stored file's size and hash and fails closed if
either differs from the registration evidence. It never modifies the source.

## Execution boundary

The browser runs inspection in a spawned worker with a time limit and memory
limit. The existing XLSX container checks still reject traversal, encryption,
macros, external links and connections, embedded objects, suspicious
compression, and unsafe archive structure before workbook parsing.

Catalogs are stored in the project DuckDB database. They are separate Stage B
artifacts: generating a catalog does not reopen, revise, or invalidate the
registered Stage A project.

## CSV catalog

The current contract generation:

- detects UTF-8 with or without a byte-order mark, then Windows-1252;
- detects comma, semicolon, tab, or pipe delimiters;
- defaults to row 1 as the candidate header;
- reports blank and duplicate candidate headers rather than silently
  disambiguating them;
- counts all governed data rows;
- reports rows shorter or longer than the candidate header.

The browser can regenerate the catalog with an explicit supported encoding,
delimiter, and header row. The override is applied in the same isolated,
hash-verifying worker as automatic detection.

## XLSX catalog

The first contract generation inventories:

- visible and hidden worksheets;
- Excel named tables and their cell ranges;
- a candidate header row;
- row and column counts;
- merged ranges;
- formula and error cells.

Each named table is cataloged as a separately selectable table using its exact
cell range. If a named table exists, its first row is also preferred as the
parent worksheet's candidate header. Otherwise, the inspector scores the first
25 worksheet rows and selects the strongest header candidate. Formulas are
never executed or trusted; the catalog only reports their presence and shows
bounded formula text as source evidence.

## Preview and column profiles

Each table or worksheet contains at most 20 preview rows. Each rendered value
is limited to 200 characters. HTML auto-escaping and the authenticated
loopback-only browser boundary apply to preview values.

Column profiles contain:

- a non-binding candidate type;
- null and non-null counts;
- distinct and duplicate counts;
- minimum and maximum where values are comparable;
- minimum and maximum rendered lengths.

Candidate types are `empty`, `boolean`, `integer`, `decimal`, `date`,
`datetime`, `string`, or `mixed`. Type inference is advisory. In particular,
digit strings with leading zeroes remain candidate strings.

Distinct values are tracked exactly through 10,000 unique values per column,
subject to a 100,000-value budget per table. Above either boundary the catalog
reports a lower bound and the exact duplicate count is deliberately
unavailable. Values are represented by hashes in the distinct index. These
limits prevent wide or high-cardinality inputs from creating an unbounded
in-memory set.

## Confirmation and invalidation

The user confirms selected tables against the exact catalog content hash and
acknowledges any warnings. Blank or duplicate candidate headers block
confirmation rather than being silently renamed.

Applying new source settings regenerates that catalog and invalidates its prior
confirmation. Any source reinspection invalidates the frozen selection and
mapping draft, preventing stale downstream decisions.

Confirmed tables are named and frozen through the
[source workspace contract](source-workspace.md). This contract still does not
join or transform source tables and never writes to Odoo.
