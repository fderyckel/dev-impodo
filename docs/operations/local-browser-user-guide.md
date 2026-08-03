# Impodo local-browser user guide

## For data analysts and data managers

Impodo helps a migration team turn CSV and Excel exports into a governed,
reviewable mapping for Odoo. It runs on the user's own computer and opens in a
normal web browser. The customer files, project history, and mapping evidence
remain local.

This guide covers the current workflow:

1. register the migration project;
2. inspect and confirm the source files;
3. freeze the datasets that will be mapped;
4. capture the permitted Odoo field catalog;
5. confirm the business keys used to find records;
6. map ordinary fields and relationships;
7. validate and submit the exact mapping revision.

Impodo is currently a planning and validation tool. It cannot create, change,
or delete Odoo records, and it does not offer a Production target.

The screenshots in this guide show the current local-browser interface at a
1440 × 1024 desktop viewport. All names, files, people, hashes, and target
details shown in them belong to an isolated fictional training project.

The current browser can author constants, source fallbacks, explicit
leave-unset/Odoo-default intent, and a small allowlist of scalar
transformations. It previews one bounded inspected sample and validates the
mapping definition. It does not yet execute those rules against every source
row or produce a clean canonical package. See
[Normalization, transformation, and cleaning](#normalization-transformation-and-cleaning)
before submitting a mapping.

## The quickest safe route

For a routine project, use this checklist:

- Start Impodo and keep its launcher window open.
- Open an existing project or select **New project**.
- Record the project owner, functional owner, data classification, retention,
  source system, and source export date.
- Add the final CSV or Excel source files.
- Configure only an approved Odoo DEV or TEST target.
- Inspect every file and resolve or acknowledge its warnings.
- Give each selected table a short, stable dataset name.
- Freeze the selection.
- Capture only the Odoo models approved for this migration.
- Confirm a real business key for every target model.
- Map identity first, ordinary fields second, and relationships third.
- Configure supported scalar providers and transformations in the mapping.
  Record every unsupported lookup, structural transform, domain rule, or
  correction in an approved transformation register.
- Save and validate.
- Resolve all blocking findings and review every warning.
- Submit the exact validated revision.
- Use **Quit Impodo** when finished.

## Who does what?

The two roles work on the same project. The split below is a practical
operating model, not a software permission boundary.

| Activity | Data analyst | Data manager |
| --- | --- | --- |
| Prepare source files | Identifies structural and data-quality issues without changing the registered evidence | Confirms the files are complete and authorised |
| Register the project | Supplies source and export details | Owns purpose, classification, retention, and accountability |
| Inspect source data | Leads previews, parsing choices, and table selection | Reviews material warnings and exclusions |
| Name and freeze datasets | Proposes clear dataset names | Confirms the selected migration scope |
| Confirm Odoo business keys | Tests whether source columns can supply them | Obtains functional/Odoo-owner approval |
| Build mappings | Leads field and relationship mapping | Confirms business meaning and migration policy |
| Specify cleaning and transformations | Configures supported mapping rules and records unsupported rules in the transformation register | Approves rule meaning, ownership, and exceptions |
| Resolve validation findings | Corrects mappings and documents exceptions | Accepts warnings and confirms readiness |
| Submit a mapping | Prepares the validated revision | Verifies the evidence and submits, or delegates explicitly |

## Before starting

### Source-file checklist

Have the following ready:

- the final migration extracts, in `.csv` or `.xlsx` format;
- one clear header row for every table;
- stable business identifiers, such as customer reference or product code;
- no passwords, API keys, or connection strings inside the source files;
- an explanation for any unusual delimiter, encoding, title rows, or blank
  rows;
- confirmation that Excel formula cells are not being used as source values.

Keep the original export unchanged. If the source must be corrected, create a
new approved export instead of editing the registered evidence in place.

### Odoo checklist

Ask the Odoo administrator or functional owner for:

- the approved DEV or TEST URL and database name;
- a dedicated, least-privilege API key;
- the exact technical model names in scope, such as `res.partner`;
- the target fields that may be mapped;
- the natural business key for each model;
- any company, tenant, site, or parent field that limits that key's scope.

Do not use an Odoo master password, a database password, or a personal
interactive password.

## Start Impodo

Use the Impodo shortcut supplied by your technical team. During a pilot or
source checkout, the approved Windows command is:

```powershell
.\.venv\Scripts\impodo.exe
```

Impodo opens the default browser automatically. The address begins with
`http://127.0.0.1:` followed by a temporary port number. This means the page is
served only from the same computer.

Do not bookmark, copy, or share the launch address. It contains a short-lived,
single-use sign-in token. If the session has ended, return to the launcher and
start Impodo again.

## 1. Open or create a project

The first page lists the projects stored on this computer.

![Impodo project list showing the workflow sidebar, an existing registered
migration, and the New project button.](../images/impodo-local-browser-guide/01-project-list.png)

- Select a project card to resume it.
- Select **New project** to begin a separate migration.
- Treat one project as one governed migration scope. Do not combine unrelated
  customers, target databases, or approval chains in one project.

### Example project

This guide follows one fictional project:

| Item | Example |
| --- | --- |
| Project | Belgium contacts migration |
| Source system | Dynamics AX 2012 |
| Source files | `companies.csv`, `contacts.csv` |
| Target | Odoo 19 DEV |
| Odoo models | Companies, contacts, contact categories |
| Goal | Prepare companies and contacts while preserving company and category relationships |

The worked example uses a fictional training schema. Always use the fields,
types, and business keys captured from the organisation's own Odoo instance.

## 2. Register the project

Select **New project**, enter a business-friendly project name, choose the
source system, and select **Create draft**. Impodo then leads you through
project details, governance, source files, target configuration, and a final
review.

![The first New project page, where the user names the migration and chooses
the source system.](../images/impodo-local-browser-guide/02-create-project.png)

### Project details

Use a name that a reviewer will recognise six months later.

Good examples:

- `Belgium contacts migration - rehearsal 2`
- `Legacy products and categories - Odoo TEST`
- `France opening balances - approved extract 2026-06-30`

Avoid names such as `test`, `new migration`, or `final v2`.

### Source export readiness

Use **Export planned** while the export is expected but the final source
extract has not yet been received and accepted. Use **Files received** only
when the agreed extract has arrived; then record its source export date: the
date the source system produced the files, not a date of import into Odoo.

This is a data-manager governance declaration, not a result Impodo infers from
uploading a file. It records whether the source evidence is ready to progress.
It does not change file parsing, schema capture, mapping, or Odoo access.

Impodo cannot register the project until **Files received** is selected, a
source export date is recorded, and at least one source file has been added.

### Governance

Record:

- **Data manager**: the person accountable for handling the data;
- **Functional owner**: the person who approves its business meaning;
- **Classification**: the sensitivity of the source;
- **Retention after acceptance**: the maximum number of calendar days Impodo
  may retain the local project after the functional owner formally accepts the
  migration outcome;
- **Purpose**: why the data is being processed.

If any of these are unknown, pause registration and obtain the answer. These
fields are part of the evidence, not optional notes.

### Choosing the retention period

Count the retention period from formal functional acceptance, not from the
source export date, project registration, mapping submission, or any later
Odoo import date. Mapping submission is not acceptance.

The period covers the local Impodo project data, including the protected source
file copies and derived project artifacts. Use the organisation's approved
retention policy; the default is 90 days, and the field accepts 1 to 3,650
days. Choose the shortest period that still permits the required review,
support, audit, and reconciliation work.

The recorded period does not make Impodo delete the project automatically.
When it ends, the data manager must follow the organisation's approved
disposal process.

### Source files

Add every CSV or Excel file that belongs to this migration project before
selecting **Register project**. One project can contain related files for many
Odoo models. For example, one product migration project may contain
`products.csv`, `product_categories.csv`, and `units_of_measure.xlsx`.

The file chooser accepts one file at a time. Select a file and choose
**Add file**; when its name and fingerprint appear in the registered-file list,
select the next file and choose **Add file** again. Repeat until every required
file is listed.

**Continue** only moves the draft to the Odoo-target step. It does not register
the project. While the project is still a draft, use **3. Files** in the setup
steps to return and add another file.

### Important: source-file boundary

Selecting **Register project** records the source-file list as immutable
evidence. After registration, the source-inspection page can inspect and
confirm the registered files, but it cannot add, replace, or remove a file.
This prevents a later mapping from silently using a different migration scope.

Before registering, confirm that the registered-file list contains every
related source file. If a file was omitted and the project is already
registered, create a new project or rehearsal with the complete source set.
Do not alter the registered project files or project database directly.

For each registered file, Impodo stores a protected project copy and records
its size and digital fingerprint. The original source file is not modified.

### Target

Choose one:

- **Local Odoo** for an Odoo DEV instance on the same computer;
- **Remote / on-premises Odoo** for an approved HTTPS DEV or TEST server.

Enter the exact URL and database supplied by the Odoo administrator. A
connection test is read-only. Local Windows mode uses the selected
`odoo.conf` and does not require an Odoo API key. A Remote-mode API key can be
stored in the operating system's credential manager, never in the project
database.

#### Local Odoo readiness assistant

In **Local Odoo** mode, select **Help me connect to local Odoo** when the
PostgreSQL or Odoo status is unclear. Then:

1. Select **Choose odoo.conf**.
2. In the Windows file chooser, select the configuration used by the local
   Odoo instance.
3. Review each status result.
4. Expand **Detected machine-local profile** and verify the detected
   `pg_ctl.exe`, PostgreSQL data directory, Python, `odoo-bin`, and logs
   directory.
5. If PostgreSQL or Odoo needs to be started, select the confirmation checkbox
   and then **Start PostgreSQL and Odoo**.
6. If Impodo starts one or both services, the assistant lists exactly which
   ones it manages and shows **Stop managed services** and **Restart managed
   services**. If you correct the configuration or start a service another
   way, select **Check again**.
7. When PostgreSQL and Odoo are ready, select **Save and test connection**.
   No Odoo API key is required in Local mode.
8. The first time only, select **Verify access and load models**. Move on
   after **Verified model snapshot stored** identifies the database, Odoo
   version, capture time, and number of persistent models.
9. In later Impodo sessions, use that DuckDB snapshot directly. A grey
   **Live connection not checked this session** message is not a blocker.
   Select `odoo.conf` again only to refresh models or fetch fields after the
   selected scope changes.

The assistant shows four separate results:

| Result | What it proves |
| --- | --- |
| **Configuration** | The selected file contains a valid, explicit loopback PostgreSQL and Odoo HTTP configuration. |
| **PostgreSQL** | Green means `pg_isready.exe` confirmed that PostgreSQL is accepting connections on the configured host and port. Orange means action is required or only an open port could be detected. |
| **Odoo server** | Green means the loopback HTTP endpoint answered `/web/webclient/version_info` and identified itself as Odoo 19. |
| **Impodo metadata reader** | This remains grey until **Save and test connection** proves that the selected local Odoo installation can open the configured database. |

Green means ready, orange means action is needed or the result is incomplete,
red means the check failed or the configuration is unsafe, and grey means the
check has not run yet. The text beside every colour is authoritative; colour
is not the only status indicator.

Selecting `odoo.conf` does not upload or copy it. Impodo extracts only the
non-secret routing settings needed for these checks and does not retain
`db_password` or `admin_passwd`. The selected path and detected executable
paths live only in memory for the current Impodo session; they are not added
to the migration project or its evidence.

The model catalogue and effective fields are different: they are safe,
hash-bound project evidence stored in DuckDB. Page navigation, schema review,
and mapping use those stored snapshots without an Odoo request. Refresh after
an Odoo module upgrade, custom-field change, or before a future approved
import freshness check.

The assistant can start only detected paths under the selected Odoo workspace;
users cannot enter a command or executable path. After explicit confirmation,
Impodo:

1. rereads the live `odoo.conf`;
2. checks whether the selected PostgreSQL data directory already has a running
   server;
3. starts PostgreSQL only when needed and waits for `pg_isready`;
4. does not start Odoo unless PostgreSQL is ready;
5. launches Odoo in a separate Windows console and waits up to 30 seconds for
   the Odoo 19 endpoint.

If the newly launched Odoo process exits, reports another Odoo version, or
does not become ready within 30 seconds, Impodo reports the failure and stops
that newly launched Odoo process. If cleanup cannot complete, Impodo retains
the process handle so that **Stop managed services** remains available.
PostgreSQL may remain running so its logs and state can be inspected and,
when Impodo started it, is listed as managed.

**Stop managed services** and **Restart managed services** apply only to
services started by the current Impodo process for this project:

1. Impodo stops the exact Odoo child process that it launched.
2. It waits until the configured Odoo port is closed. If another process is
   still listening there, PostgreSQL is left running and the assistant reports
   the problem.
3. If Impodo also started PostgreSQL, it runs a bounded `pg_ctl stop` in
   `fast` mode for the selected data directory, but only after the current
   `postmaster.pid` still matches the PID recorded at startup. It then verifies
   that the server stopped.
4. **Restart** then rereads the live configuration and repeats the normal
   PostgreSQL-first startup and readiness checks.

An Odoo or PostgreSQL service that was already running before Impodo checked
it remains status-only and cannot be stopped or restarted from the assistant.
If PostgreSQL is listed as managed, stopping it may disconnect other local
tools that started using that server after Impodo launched it. Finish or save
their work first.

Ownership is held only in memory. Use **Stop managed services** before
**Quit Impodo**. If Impodo or the computer exits unexpectedly, reopen the
workspace and use its established manual shutdown procedure because a new
Impodo session will not claim ownership of the old processes.

Starting, stopping, or restarting the local stack does not import data or
write to Odoo.

**Migration application scope** is optional reviewer context, such as
Contacts or Inventory. It does not grant Odoo access and does not control
which technical models Impodo can read or map.

Before selecting **Register project**, use the completeness list to confirm
that every required section is finished, including the complete source-file
list. Registration locks that list; it is not possible to append another file
from source inspection later.

## 3. Inspect every source file

From the project page, continue to source inspection. Impodo checks that each
stored file still matches the copy registered earlier before it reads the
contents.

![Source inspection showing two registered files and their inspected
status.](../images/impodo-local-browser-guide/03-source-inspection.png)

For every file, review:

- the detected CSV encoding and delimiter;
- the proposed header row;
- Excel worksheets and named tables;
- the bounded row preview;
- candidate column types;
- null, distinct, minimum, and maximum statistics;
- duplicate headers, formula cells, and other warnings.

This page is for the files already registered to the project. It has no
**Add file** action: source-file additions are allowed only while the project
is a draft, before registration.

![A selected CSV table showing its proposed header, bounded data preview, and
column statistics.](../images/impodo-local-browser-guide/03-source-preview.png)

### Example: interpreting a preview

Suppose `contacts.csv` contains:

| contact_ref | contact_name | company_code | category_code | tag_codes |
| --- | --- | --- | --- | --- |
| BE-1001 | Elise Martin | C-BE-01 | CUSTOMER | VIP;NEWS |
| BE-1002 | Omar Diallo | C-BE-01 | SUPPLIER | NEWS |

The analyst should check that:

- `contact_ref` has no blanks and appears unique within its company;
- `company_code` matches the key used in the companies file;
- `category_code` uses the codes maintained in Odoo;
- `tag_codes` is consistently separated by semicolons;
- leading zeroes and accents are displayed correctly.

If the preview is wrong, adjust only the supported parsing settings and
preview again. For example:

- choose semicolon instead of comma for a European CSV;
- choose row 3 as the header when rows 1 and 2 contain a report title;
- select the named Excel table `Contacts_Export` instead of the whole sheet.

Confirm the source only when the preview represents the intended table.
Warnings must be understood and explicitly acknowledged; acknowledgement does
not correct the underlying data.

### Inspection is diagnosis, not cleaning

Inspection may show surrounding spaces, inconsistent case, unexpected blanks,
mixed candidate types, or duplicate-looking values. It does not trim, replace,
deduplicate, recalculate, or rewrite those values.

For example, confirming a preview containing `"  Acme Belgium  "` confirms
that those exact spaces were observed in the registered source. It does not
turn the value into `"Acme Belgium"`. Likewise, selecting `boolean` later in
the mapping does not prove that every source token can be converted to
`true` or `false`.

## 4. Name and freeze the datasets

After all files are confirmed, select the exact worksheet, named table, or CSV
table that participates in the migration. Give each one a stable dataset name.

![The frozen-dataset selection page with companies and contacts selected and
named.](../images/impodo-local-browser-guide/04-freeze-datasets.png)

Good dataset names:

- `companies`
- `partners`
- `product_categories`
- `opening_balances`

Avoid file-version details such as `contacts_final_v7`. The frozen version and
source fingerprints already preserve that evidence.

Select **Freeze selection** only after checking:

- every required table is present;
- no irrelevant worksheet is included;
- dataset names are unique;
- the selection reflects the agreed migration scope.

Freezing creates a versioned definition of the source. If a file is
reconfirmed or the selection is frozen again later, Impodo keeps the old
history but requires the active mapping to be reviewed against the new source.

## 5. Capture the Odoo schema

Open **Odoo schema** and set only the technical model names approved for the
project. This is the enforced allowlist for schema capture and mapping. For
the worked example:

- `res.company`
- `res.partner`
- `res.partner.category`

Save the permitted model scope before capture. Changing it later clears the
captured schema, confirmed business keys, and active mapping, so the new scope
must be captured and reviewed again. Impodo reads field definitions only for
those models. It does not read every Odoo model, and schema capture does not
change Odoo.

Review each field's label, type, required status, read-only status, and
relationship target. If a needed field is missing, ask the Odoo administrator
to review the allowlist and access rights. Do not substitute a similarly named
field without functional confirmation.

## 6. Confirm target business keys

A business key is the stable business value used to find exactly one Odoo
record. It is not the internal numeric Odoo ID.

![Business-key governance showing the confirmed contact reference key and the
separate optional scope field.](../images/impodo-local-browser-guide/05-business-keys.png)

For each target model, enter:

- **Natural key field(s)**: the field or ordered set of fields that identifies
  the record;
- **Scope field(s)**: company, tenant, site, or parent context within which the
  natural key is unique;
- **Meaning**: a plain-language explanation a reviewer can understand.

### Business-key examples

| Target | Natural key | Scope | Why |
| --- | --- | --- | --- |
| Contact | `ref` | `company_id` | Customer reference is unique within one company |
| Product | `default_code` | `company_id` | Internal product reference may be reused by another company |
| Account | `code` | `company_id` | Account codes repeat across legal entities |
| Country | `code` | none | ISO country code is globally stable |
| Category | `code` | none | Governed category code is unique in the target |

Poor choices include:

- `name`, when names can be duplicated or changed;
- an internal numeric `id`;
- an email address when contacts may share one;
- a key that is unique only in today's small sample;
- a guessed technical field.

Business-key confirmation states the intended rule. Actual duplicate and
missing-reference checks against staged source and target records happen in a
later runtime stage.

## 7. Build the dataset mapping

Open **Mapping**. Impodo binds the mapping to the exact frozen source and
governed Odoo schema shown in the evidence panel.

![The mapping workspace with evidence binding and the first dataset
section.](../images/impodo-local-browser-guide/06-mapping-overview.png)

Work through one dataset at a time.

### Choose the target and operating mode

Choose the Odoo model, then choose a mode:

| Mode | Plain-language meaning | Typical use |
| --- | --- | --- |
| **Upsert** | Find the record by business key; update it if found or prepare a new one if absent | Customers, products, companies |
| **Create** | Prepare only new records; finding an existing key is treated according to the selected policy | One-time transactions or tightly controlled master-data additions |
| **Reference** | Use the dataset for identity or relationship resolution without preparing create/update values | Lookup-only or supporting datasets |

For create mode, choose what should happen if the business key already exists.
Use the conservative blocking policy unless the migration owner has approved a
different behaviour.

### Map source trace identity

Source trace identity answers: “Which exact source row produced this result?”
It should be stable and useful during investigation.

Examples:

- companies: `company_code`;
- partners: `contact_ref` plus `company_code`;
- invoice lines: `invoice_number` plus `line_number`.

This identity is for traceability. It is separate from the Odoo target
business key.

### Map target identity and scope

Map source columns to the confirmed Odoo natural key and scope:

| Dataset | Source column | Odoo identity field |
| --- | --- | --- |
| companies | `company_code` | `res.company.x_legacy_code` |
| partners | `contact_ref` | `res.partner.ref` |
| partners | `company_code` | `res.partner.company_id` scope relationship |

Every confirmed key component must be supplied exactly once. A missing,
duplicate, or unconfirmed identity component is a blocking validation finding.

### Map ordinary fields

Choose one value provider for each writable Odoo field:

- **Source column** uses the selected frozen column;
- **Constant value** supplies the same governed literal to every row;
- **Source + fallback** uses the literal only when the source is missing or
  becomes null under the selected empty-to-null policy;
- **Leave unset / Odoo default** omits the field from the future create
  payload. This requires warning acknowledgement because metadata capture
  cannot prove the runtime default.

Worked example:

| Dataset | Source | Odoo field | Notes |
| --- | --- | --- | --- |
| companies | `company_name` | `name` | Required when a new company is prepared |
| partners | `contact_name` | `name` | Required when a new contact is prepared |
| partners | `email` | `email` | Optional; blank policy must be deliberate |
| partners | `active_flag` | `active` | Values will need row-level boolean validation during a later staging/preflight stage |

Use only the allowlisted transformations shown by the editor: trim, collapse
whitespace, empty-to-null, uppercase/lowercase for strings, declared decimal
locale, explicit date format, and UTC for datetimes. The preview shows one
bounded inspected sample as `raw -> proposed`; save the draft to run
mapping-level semantic validation.

For each field, consider:

- **Compare**: include it in difference reporting;
- **Required**: a source value must be present;
- **Required on create**: it may be blank for an update but not for a new
  record;
- **Blank/null policy**: decide whether a blank means “leave unchanged,”
  “clear the target,” or is invalid.

Do not map read-only Odoo fields. Do not map the same target field twice in one
dataset.

## 8. Map relationships

Relationships must be expressed through business keys, never numeric Odoo IDs.

![Relationship mapping for contact tags resolved through the existing Odoo
catalog and a related company resolved from the incoming companies
dataset.](../images/impodo-local-browser-guide/07-relationship-mapping.png)

### Many-to-one: one related record

Examples include a contact's company, a product's category, or an invoice's
customer.

Choose where the referenced key comes from:

- **Incoming dataset** when the related record is part of this migration;
- **Existing Odoo catalog** when the related record is expected to exist
  already in the target.

Worked examples:

| Source value | Target relationship | Resolver |
| --- | --- | --- |
| `partners.company_code` | `res.partner.company_id` | Incoming `companies` dataset by `x_legacy_code` |
| `partners.category_code` | `res.partner.category_id` | Existing `res.partner.category` catalog by `code` |

Mark a relationship as required only when a missing or unresolved value must
block the record.

### Many-to-many: a list of related records

For `tag_codes = VIP;NEWS` mapped to contact tags:

- select the source column `tag_codes`;
- choose semicolon as the separator;
- resolve each code through the category business key;
- choose the approved operation:
  - **Replace**: the source list becomes the complete target list;
  - **Add**: add the listed values without removing current values;
  - **Remove**: remove only the listed values.

Replace is easy to understand but can remove target values that were not in
the source. Add and remove are safer for partial lists, but they require a
clear business rule.

### One-to-many: map from the child

Do not map a parent's one-to-many list directly. Map the inverse many-to-one
field on each child dataset.

Example:

- do not map `sale.order.order_line` as a list on the order;
- in the order-lines dataset, map each line's `order_id` relationship to the
  order dataset using the order business key.

This makes ownership and dependency order explicit.

### Relationship examples and expected result

| Situation | Expected validation |
| --- | --- |
| Company relationship targets a matching incoming companies dataset | Valid when key components align |
| Category relationship uses a confirmed target key | Valid |
| Relationship uses an internal numeric Odoo ID | Blocking |
| Required relationship has no resolver | Blocking |
| Compared relationship permits ambiguous matches | Blocking |
| Two datasets depend on each other for creation | Blocking dependency cycle |
| Many-to-many has no separator | Blocking |

## 9. Validate, review, and submit

Select **Save and validate draft** after a meaningful group of changes.
Validation is deterministic: the same frozen evidence and mapping produce the
same result.

![Semantic validation showing a valid mapping, zero findings, and the submit
action.](../images/impodo-local-browser-guide/08-validation-and-submit.png)

### Understand the result

| Result | Meaning | Action |
| --- | --- | --- |
| **Invalid** | At least one rule would make the mapping unsafe or incomplete | Correct every blocking finding |
| **Valid with warnings** | The mapping is structurally valid but needs a conscious review decision | Read and acknowledge each warning |
| **Valid** | No semantic findings remain for the current evidence | Perform the final review and submit |

Validation checks mapping meaning and structure. It does not yet prove that
every source key is unique, that every related record exists in the live
target, or that an eventual load will succeed. Those checks belong to staging
and preflight.

It also does not execute cleaning rules against every row. A semantically
valid mapping can still contain values with invalid dates, ambiguous decimal
formats, unwanted spaces, unknown lookup codes, or collisions that appear
only after normalization.

### Save draft versus submit

- **Save and validate draft** creates an immutable mapping revision and
  records its findings.
- **Submit exact validated mapping** records that exact validated revision and
  its exact fingerprint.

If the source selection, Odoo schema, or governed business keys change, the old
revision remains in history but is no longer the active mapping for the new
evidence. Revalidate and submit a new revision.

Submission is not an Odoo import, an execution approval, or a write action.

## Normalization, transformation, and cleaning

### What is available now

The local browser currently stores constants, source fallbacks, explicit
leave-unset/Odoo-default intent, canonical types, comparison/null policies, and
allowlisted trim, whitespace-collapse, empty-to-null, casing, declared-decimal,
explicit-date, boolean, and UTC-datetime policies in the exact mapping hash.
It previews one bounded inspected source sample as raw and proposed values.

The browser does not currently provide:

- authoritative execution of those rules against every source row;
- governed value-replacement dictionaries or source-to-Odoo selection lookups;
- Unicode-form, domain-specific, conditional, split, concatenate, join, pivot,
  grouping, fuzzy-match, or survivorship operations;
- row-level raw, governed, and canonical evidence;
- post-correction duplicate and relationship checks over the full source;
- quarantine/reprocessing or a clean-package release gate;
- durable canonical staging.

Technical teams may also use Impodo's expert-profile preflight path for fixed
type parsing and comparison normalization. Neither a bounded browser preview
nor that expert path is the complete governed cleaning workflow, and neither
must be represented as data-manager approval of a clean source package.

### Where this belongs in the future workflow

In the product lifecycle, browser rule configuration belongs to **Stage D**;
the implemented delivery increment is **Phase 2C.1**. Row-level normalization
belongs to **Stage E**, and durable canonical staging begins in **Stage F** /
delivery **Phase 3**. The planned workflow separates three responsibilities:

1. **Stage D — Mapping and rule definition:** choose an allowlisted
   transformation, record its business purpose, and preview its likely impact.
2. **Stage E — Normalize and validate:** apply the approved rules to every
   source row, retain the raw value, create the governed value, parse the
   canonical type, and run post-correction identity, relationship, and
   business checks.
3. **Stage F — Canonical staging:** store the resulting rows, lineage, rule
   versions, warnings, rejections, and correction evidence before target
   preflight or approval.

Rules will be declarative and allowlisted. They will not execute arbitrary
Python, SQL, spreadsheet formulas, Odoo methods, or user-supplied regular
expressions.

### Worked examples and availability

**Mapping preview** means the current browser can store the rule and preview a
bounded sample. It does not mean every row has passed. **Future governed
execution** means full-row staging and evidence are still required.

| Raw source value | Governed rule | Canonical/proposed result | Availability | Required safeguard |
| --- | --- | --- | --- | --- |
| `"  Acme   Belgium  "` | Trim and collapse whitespace | `"Acme Belgium"` | Mapping preview | Do not apply automatically to legal names or brands without functional approval |
| `prod-001` | Uppercase an approved product-code field | `PROD-001` | Mapping preview | Recheck duplicates after case conversion |
| `00123` | Preserve identifier as text | `"00123"` | Mapping preview | Never infer that an identifier is an integer |
| `1.234,56` | Parse using an explicitly selected decimal convention | `1234.56` | Mapping preview | Never guess from the computer's locale |
| `31/12/2026` | Parse using an explicitly selected day-month-year convention | `2026-12-31` | Mapping preview | Reject ambiguous dates when no convention is approved |
| `Yes` | Map through the supported boolean tokens | `true` | Mapping preview | Unknown tokens such as `Maybe` must warn or block during full-row execution |
| blank cell | Apply empty-to-null, fallback, leave-unset, and null policies deliberately | null, fallback, unchanged, clear, or blocked | Mapping preview, depending on provider | Blank, zero, `false`, and the text `"NULL"` must remain distinct unless a rule says otherwise |
| `BE;LU;BE;;` | Split a many-to-many list and apply its duplicate/empty-token policy | `BE`, `LU` | Future governed execution | Report duplicates and empty items; do not silently discard them |
| `Belgium` | Use an approved country lookup | `BE` | Future governed execution | Unknown or multiply matched labels must not be guessed |
| blank `country_code` | Supply approved fallback `BE` | `BE` | Mapping preview | A fallback fills only a governed null; it must not overwrite a meaningful value |
| `first_name` + `last_name` | Concatenate with an approved separator and null policy | proposed display name | Future governed execution | Show the exact result when either input is blank |

Constants and lookups must use business values, not internal numeric Odoo IDs.
For example, a company default should use a governed company code resolved
through a confirmed business key, never a remembered `company_id`.

### Best way to use Impodo today

Until full-row governed execution and canonical staging are implemented:

1. Keep the original source-system export unchanged and register that evidence
   in Impodo.
2. Use inspection to distinguish parsing problems from actual data-quality
   problems. A wrong delimiter or header row is a parsing problem; inconsistent
   customer codes are a data-quality problem.
3. Configure supported providers and scalar transformations in the mapping so
   they are included in the immutable revision. Use the raw-to-proposed preview
   as evidence of intent, not as proof that every source row is clean.
4. Maintain an approved transformation register outside Impodo for unsupported
   lookups, structural changes, domain validation, entity resolution,
   exceptions, and derived values. Use a controlled spreadsheet, project
   document, or ticketing record approved by the data manager, under the same
   classification, access, and retention controls as the source. Include at
   least the dataset, source field, target field, rule, before/after example,
   rule owner, approval status, and exception policy.
5. Prefer a corrected export produced by the source-system owner. When that is
   impossible, create a separately governed derivative and retain both its
   lineage and the unchanged original.
6. Do not overwrite a registered project file. Register a corrected package as
   a new project or rehearsal so its new hashes, inspection, frozen selection,
   and mapping review remain explicit.
7. Recheck business-key uniqueness and relationship keys after every proposed
   correction. Trimming or changing case can turn two apparently different
   keys into one collision.
8. Treat **Valid** and **Submitted** as statements about the mapping revision,
   not proof that the source rows are clean, target-ready, or approved for
   import.

For unsupported rules, or when policy requires an independent approval record,
a practical transformation register can begin with:

| Dataset | Source field(s) | Target | Proposed rule | Example | Owner | Decision |
| --- | --- | --- | --- | --- | --- | --- |
| products | `item_code` | `default_code` | Trim, uppercase, preserve leading zeros | `" 001a "` → `"001A"` | Product owner | Proposed |
| partners | `country_name` | `country_id` | Approved name-to-ISO lookup | `Belgium` → `BE` | Data manager | Approved for rehearsal |
| partners | `active_flag` | `active` | `Y/Yes/1` → true; `N/No/0` → false | `Y` → `true` | Functional owner | Proposed |

### Coverage required before claiming a clean import package

The normalization design covers the core ideas, but a data manager should not
call a package clean until all applicable cases below have explicit rules,
full-row results, resolved exceptions, and release evidence:

| Area | Proposed required behavior |
| --- | --- |
| Unicode and invisible text | Distinguish ordinary spaces, non-breaking spaces, zero-width characters, control characters, and Unicode normalization forms; show what changed |
| Multilingual case handling | Test accents, German `ß`, Turkish dotted/dotless `I`, apostrophes, and hyphenated words; never title-case names, brands, or legal entities by default |
| Numbers and money | Require decimal and thousands conventions; define currency, percentage, scientific notation, accounting negatives, precision, and rounding behavior |
| Dates and datetimes | Handle Excel's date system explicitly; require date order and timezone; detect invalid dates and ambiguous or nonexistent daylight-saving times |
| Nulls and booleans | Keep null, empty text, whitespace-only text, zero, `false`, `"N/A"`, and `"NULL"` distinct unless an approved field rule maps them |
| Identifiers | Preserve leading zeros and significant punctuation; prohibit silent numeric coercion, scientific notation, rounding, or case changes |
| Length and truncation | Validate Odoo and business length limits; never truncate silently; report the original length and approved resolution |
| Lookups and selections | Bind source values to Odoo technical selection values or business keys through an explicit versioned table; block unknown and ambiguous matches |
| Constants and defaults | Distinguish “fill only when blank” from “always set”; show scope and prevent a default from overwriting meaningful source data silently |
| Split and concatenate | Define separators, quoting, escaping, empty tokens, duplicate items, ordering, and null behavior before processing lists or combined fields |
| Identity and relationships | Apply the same governed key representation on both sides, then detect post-correction duplicates, missing references, ambiguity, and cycles |
| Odoo context | Treat monetary values with their currency, company-dependent fields with company scope, and translated fields with an explicit language context |
| Cross-field and cross-row rules | Support conditional requirements, date order, totals, balances, checksums, and parent/child consistency as separately approved rules |
| Evidence and privacy | Retain raw/governed/canonical lineage while masking sensitive before/after values in reports and keeping secrets out of logs |
| Repeatability and scale | Require idempotent rules, deterministic results, bounded processing, and batched relationship checks instead of one live Odoo request per row |

These proposals require engineering, data-management, functional, and security
approval before implementation. Rules that can alter financial totals, legal
status, company ownership, tax meaning, names, brands, or master-data semantics
must never be enabled as generic automatic corrections.

The product-stage definitions are maintained in the
[end-to-end product vision](../product-vision.md). The detailed
[data-quality rules implementation plan](../data-quality-rules-implementation-plan.md)
and the normative
[data-transformation coverage contract](../contracts/data-transformation-coverage.md)
define the proposed delivery scope. They are not statements that every
capability is already available in the browser.

## Final review checklist

Before submission, the analyst and manager should be able to answer “yes” to
all of these:

- Are the correct registered files and tables frozen?
- Can every source row be traced through its source identity?
- Does every target model have an approved business key?
- Is company, tenant, site, or parent scope represented separately where
  needed?
- Is every target identity component mapped exactly once?
- Are only writable Odoo fields mapped?
- Are required-on-create values available?
- Does every relationship use an incoming dataset or confirmed target key?
- Are many-to-many separators and operations deliberate?
- Are dependency cycles absent?
- Are all blocking findings resolved?
- Has every warning been understood and acknowledged?
- Are supported providers and transformations stored in the exact mapping
  revision, with every unsupported or externally governed rule recorded in the
  transformation register?
- Is everyone clear that current semantic validation has not executed those
  rules against every source row?
- Does the displayed evidence belong to the intended DEV or TEST project?
- Is the exact validated revision the one being submitted?

## Common problems

| What you see | Likely reason | What to do |
| --- | --- | --- |
| The browser says the session is unavailable | The one-time local session ended | Restart Impodo from its launcher |
| A source file is no longer confirmed | Its content or parsing choice changed | Inspect and confirm it again |
| I need to add another source file | The project was already registered and its source list is immutable | Create a new project or rehearsal with the complete file set; do not modify the registered evidence directly |
| Mapping is no longer active | Frozen source, schema, or business keys changed | Review, save, and validate a new revision |
| No source column is available | The intended table was not frozen | Return to source selection and freeze the correct dataset |
| No target field is available | Model was not captured or field is not permitted | Ask the Odoo administrator to review the model and access |
| “Business key not confirmed” | The target model has no governed identity | Obtain approval and confirm the natural key and scope |
| Required target field is unmapped | A new record could not be prepared safely | Map a suitable source value or correct the migration policy |
| Relationship is unresolved | Resolver, key component, or source column is missing | Complete the relationship through a confirmed business key |
| Relationship is ambiguous | The key can match more than one record | Strengthen the key/scope; do not ignore ambiguity |
| Dependency cycle | Two or more incoming datasets require each other first | Redesign the ownership or split the migration sequence |
| Preview still shows spaces, inconsistent case, or dirty values | Inspection displays registered evidence; it does not clean it | Configure a supported mapping rule, or record the unsupported rule and obtain an approved corrected export or governed derivative |
| Mapping is valid but some row values look invalid | Current validation checks mapping semantics, not every row | Do not treat the mapping as load-ready; record the issue for later governed staging/preflight |
| Connection test fails | Odoo is stopped, URL/database is wrong, or access is insufficient | Verify the approved target details with the Odoo administrator |
| Stored API key no longer works | It expired or the target details changed | Supply a new key for the exact target |
| Stop and Restart are not available | The services were already running or were started by an earlier Impodo session | Use the workspace's approved manual shutdown procedure; the current session will not claim those processes |
| PostgreSQL remains after Stop | Another listener remained on the configured Odoo port, or PostgreSQL could not be verified as stopped | Read the assistant error, close the unrelated listener safely, and use the workspace procedure before retrying |

## End the session safely

If the local Odoo assistant lists managed services, finish other local work
using the same PostgreSQL server and select **Stop managed services** first.
Then use **Quit Impodo** in the footer. Closing only the browser tab does not
stop the local Impodo process or any managed Odoo/PostgreSQL service.

Keep project data only for the recorded retention period. Follow the
organisation's approved disposal process when the project no longer needs to
be retained.

## Short glossary

| Term | Plain-language meaning |
| --- | --- |
| Dataset | One confirmed table selected from a CSV file, worksheet, or named Excel table |
| Frozen source | A versioned statement of exactly which source tables participate |
| Schema | The permitted Odoo models and fields visible to the project |
| Business key | Stable business value(s) used to identify a record |
| Scope | Company, tenant, site, or parent context that limits a key |
| Scalar field | An ordinary value such as name, date, amount, or active flag |
| Relationship | A link to another business record |
| Resolver | The rule used to find that related record |
| Raw value | The exact value read from the unchanged registered source |
| Governed value | A future value produced by an approved correction rule, with evidence |
| Canonical value | A typed, consistently represented value used for validation and comparison |
| Transformation register | The controlled external record for unsupported or externally governed corrections, lookups, structural rules, exceptions, and derived values |
| Revision | An immutable saved version of the mapping |
| Validation finding | A blocking error or warning produced by semantic rules |
| Submission | A record that one exact validated mapping revision was handed forward |

## Current limits

The local browser currently supports project registration, CSV/XLSX source
discovery, source freezing, allowlisted Odoo schema capture, governed business
keys, scalar mapping, relationship mapping, semantic validation, and exact
mapping submission.

It can author and preview the supported providers and scalar transformations,
but it does not yet execute them authoritatively against every source row. It
also does not yet provide governed lookup dictionaries, structural and
entity-resolution transformations, domain validation, mapping import/export,
mapping approval, durable canonical staging, controlled Odoo loading, or
post-load reconciliation. The proposed user workflow and coverage requirements
are documented in
[Normalization, transformation, and cleaning](#normalization-transformation-and-cleaning).
It has no Odoo write capability.
