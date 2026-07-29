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

## The quickest safe route

For a routine project, use this checklist:

- Start Impodo and keep its launcher window open.
- Open an existing project or select **New project**.
- Record the project owner, functional owner, data classification, retention,
  source system, and export date.
- Add the final CSV or Excel source files.
- Configure only an approved Odoo DEV or TEST target.
- Inspect every file and resolve or acknowledge its warnings.
- Give each selected table a short, stable dataset name.
- Freeze the selection.
- Capture only the Odoo models approved for this migration.
- Confirm a real business key for every target model.
- Map identity first, ordinary fields second, and relationships third.
- Save and validate.
- Resolve all blocking findings and review every warning.
- Submit the exact validated revision.
- Use **Quit Impodo** when finished.

## Who does what?

The two roles work on the same project. The split below is a practical
operating model, not a software permission boundary.

| Activity | Data analyst | Data manager |
| --- | --- | --- |
| Prepare source files | Checks structure, headers, and data quality | Confirms the files are complete and authorised |
| Register the project | Supplies source and export details | Owns purpose, classification, retention, and accountability |
| Inspect source data | Leads previews, parsing choices, and table selection | Reviews material warnings and exclusions |
| Name and freeze datasets | Proposes clear dataset names | Confirms the selected migration scope |
| Confirm Odoo business keys | Tests whether source columns can supply them | Obtains functional/Odoo-owner approval |
| Build mappings | Leads field and relationship mapping | Confirms business meaning and migration policy |
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

![Impodo project list showing an existing registered migration and the New
project button.](../images/impodo-local-browser-guide/01-project-list.jpg)

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
the source system.](../images/impodo-local-browser-guide/02-create-project.jpg)

### Project details

Use a name that a reviewer will recognise six months later.

Good examples:

- `Belgium contacts migration - rehearsal 2`
- `Legacy products and categories - Odoo TEST`
- `France opening balances - approved extract 2026-06-30`

Avoid names such as `test`, `new migration`, or `final v2`.

### Governance

Record:

- **Data manager**: the person accountable for handling the data;
- **Functional owner**: the person who approves its business meaning;
- **Classification**: the sensitivity of the source;
- **Retention**: how long the local project may be kept;
- **Purpose**: why the data is being processed.

If any of these are unknown, pause registration and obtain the answer. These
fields are part of the evidence, not optional notes.

### Source files

Add one or more CSV or Excel files. Impodo stores a protected project copy and
records its size and digital fingerprint. The original source file is not
modified.

### Target

Choose one:

- **Local Odoo** for an Odoo DEV instance on the same computer;
- **Remote / on-premises Odoo** for an approved HTTPS DEV or TEST server.

Enter the exact URL and database supplied by the Odoo administrator. A
connection test is read-only. Saving an API key stores it in the operating
system's credential manager, not in the project database.

Before selecting **Register project**, use the completeness list to confirm
that every required section is finished.

## 3. Inspect every source file

From the project page, continue to source inspection. Impodo checks that each
stored file still matches the copy registered earlier before it reads the
contents.

![Source inspection showing two registered files and their inspected
status.](../images/impodo-local-browser-guide/03-source-inspection.jpg)

For every file, review:

- the detected CSV encoding and delimiter;
- the proposed header row;
- Excel worksheets and named tables;
- the bounded row preview;
- candidate column types;
- null, distinct, minimum, and maximum statistics;
- duplicate headers, formula cells, and other warnings.

![A selected CSV table showing its proposed header, bounded data preview, and
column statistics.](../images/impodo-local-browser-guide/03-source-preview.jpg)

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

## 4. Name and freeze the datasets

After all files are confirmed, select the exact worksheet, named table, or CSV
table that participates in the migration. Give each one a stable dataset name.

![The frozen-dataset selection page with companies and partners selected and
named.](../images/impodo-local-browser-guide/04-freeze-datasets.jpg)

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

Open **Odoo schema** and enter only the technical model names approved for the
project. For the worked example:

- `res.company`
- `res.partner`
- `res.partner.category`

Impodo reads field definitions for those models. It does not read every Odoo
model, and schema capture does not change Odoo.

Review each field's label, type, required status, read-only status, and
relationship target. If a needed field is missing, ask the Odoo administrator
to review the allowlist and access rights. Do not substitute a similarly named
field without functional confirmation.

## 6. Confirm target business keys

A business key is the stable business value used to find exactly one Odoo
record. It is not the internal numeric Odoo ID.

![Business-key governance showing a confirmed company key and its scope
field.](../images/impodo-local-browser-guide/05-business-keys.jpg)

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
later runtime phase.

## 7. Build the dataset mapping

Open **Mapping**. Impodo binds the mapping to the exact frozen source and
governed Odoo schema shown in the evidence panel.

![The mapping workspace with evidence binding and the first dataset
section.](../images/impodo-local-browser-guide/06-mapping-overview.jpg)

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

Map the source values that should be compared with writable Odoo fields.

Worked example:

| Dataset | Source | Odoo field | Notes |
| --- | --- | --- | --- |
| companies | `company_name` | `name` | Required when a new company is prepared |
| partners | `contact_name` | `name` | Required when a new contact is prepared |
| partners | `email` | `email` | Optional; blank policy must be deliberate |
| partners | `active_flag` | `active` | Source values must be convertible to true/false |

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

![Relationship mapping for a many-to-one contact category resolved through
an existing Odoo catalog.](../images/impodo-local-browser-guide/07-relationship-mapping.jpg)

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
action.](../images/impodo-local-browser-guide/08-validation-and-submit.jpg)

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

### Save draft versus submit

- **Save and validate draft** creates an immutable mapping revision and
  records its findings.
- **Submit exact validated mapping** records that exact validated revision and
  its exact fingerprint.

If the source selection, Odoo schema, or governed business keys change, the old
revision remains in history but is no longer the active mapping for the new
evidence. Revalidate and submit a new revision.

Submission is not an Odoo import, an execution approval, or a write action.

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
- Does the displayed evidence belong to the intended DEV or TEST project?
- Is the exact validated revision the one being submitted?

## Common problems

| What you see | Likely reason | What to do |
| --- | --- | --- |
| The browser says the session is unavailable | The one-time local session ended | Restart Impodo from its launcher |
| A source file is no longer confirmed | Its content or parsing choice changed | Inspect and confirm it again |
| Mapping is no longer active | Frozen source, schema, or business keys changed | Review, save, and validate a new revision |
| No source column is available | The intended table was not frozen | Return to source selection and freeze the correct dataset |
| No target field is available | Model was not captured or field is not permitted | Ask the Odoo administrator to review the model and access |
| “Business key not confirmed” | The target model has no governed identity | Obtain approval and confirm the natural key and scope |
| Required target field is unmapped | A new record could not be prepared safely | Map a suitable source value or correct the migration policy |
| Relationship is unresolved | Resolver, key component, or source column is missing | Complete the relationship through a confirmed business key |
| Relationship is ambiguous | The key can match more than one record | Strengthen the key/scope; do not ignore ambiguity |
| Dependency cycle | Two or more incoming datasets require each other first | Redesign the ownership or split the migration sequence |
| Connection test fails | Odoo is stopped, URL/database is wrong, or access is insufficient | Verify the approved target details with the Odoo administrator |
| Stored API key no longer works | It expired or the target details changed | Supply a new key for the exact target |

## End the session safely

Use **Quit Impodo** in the footer. Closing only the browser tab does not stop
the local Impodo process.

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
| Revision | An immutable saved version of the mapping |
| Validation finding | A blocking error or warning produced by semantic rules |
| Submission | A record that one exact validated mapping revision was handed forward |

## Current limits

The local browser currently supports project registration, CSV/XLSX source
discovery, source freezing, allowlisted Odoo schema capture, governed business
keys, scalar mapping, relationship mapping, semantic validation, and exact
mapping submission.

It does not yet provide transformations and constants, mapping import/export,
mapping approval, durable canonical staging, controlled Odoo loading, or
post-load reconciliation. It has no Odoo write capability.
