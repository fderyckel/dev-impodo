# Browser language review

## Outcome

The 2026-08-23 review applies the approved data-manager language to all current
browser templates. Normal copy now uses **data project**, **data version**,
**workspace**, **Recipe**, **Recipe version**, **Test run**, **Recipe work
area**, **Cutover plan**, and **Production run**. Developer identifiers remain
available in code, form field names, and optional support details.

The review covered 39 templates: the previous 37-template browser surface plus
the new shared concept-help partial and Concepts page.

## Finding inventory

| Template or surface | Previous phrase | Category | Problem | Approved replacement | Status |
| --- | --- | --- | --- | --- | --- |
| `project_list.html` | Data projects plus package terminology | Core concept | The containment model was incomplete. | Explain one migration effort, source deliveries, workspaces, and optional Recipes. | Applied |
| `project_business_overview.html` | belongs to `DataVersion` | Core concept | Implied that a workspace owned accepted data. | The workspace **uses Data version N**. | Applied |
| `project_business_overview.html` | publish a Recipe revision | Workflow action | Two verbs described one data-manager action. | **Save a new Recipe version**. | Applied |
| `project_new.html` | Project name | Core concept | Generic casing hid the approved product term. | **Data project name**. | Applied |
| `base.html` and setup templates | Project | Core concept | Normal copy mixed an internal root name with the browser term. | **Data project**. | Applied |
| `workspace_mapping.html` | recipe publication | Workflow action | Developer language appeared in a recovery message. | Save or reuse the Recipe. | Applied |
| `workspace_odoo_capture_progress.html` | capture is published | Evidence boundary | Publication did not describe the data manager's result. | Impodo makes the data version current after capture finishes. | Applied |
| `workspace_odoo_capture_selection.html` and `workspace_schema.html` | field catalog | Support detail | Technical collection language appeared in the main path. | **Odoo field list**; retain hashes in Support details. | Applied |
| `project_test_run_new.html` | Recipe revisions and application workspaces | Core concept | Internal run terms obscured the separate work areas. | **Recipe versions** and **Recipe work areas**. | Applied |
| `project_integrated_run.html` | unioned requirements | Support detail | Implementation wording did not state the business result. | **Combined Odoo requirements**. | Applied |
| `project_integrated_qualification.html` | raw internal status | Status | Internal state values lacked a decision meaning. | Selected, Qualified, Ready to qualify, or Current. | Applied |
| Production templates | application workspaces and plan revision | Core concept | Test-to-Production relationships were difficult to follow. | **Recipe work areas** and **Cutover plan version**. | Applied |
| Frozen/registered status surfaces | raw state names | Status | Persistence states appeared as the normal result. | Accepted, Ready, Complete, or Current where applicable. | Applied |
| `_concept_help.html` | no shared pattern | Accessibility | Explanations would otherwise drift or require hover. | Deep-link fallback plus native dialog and focus return. | Applied |
| `concepts.html` | no complete mental model | Core concept | Help was dispersed across workflow pages. | One permanent data-manager Concepts page. | Applied |

## Reviewed template coverage

- Shared surfaces: `_comparison_recovery.html`, `_concept_help.html`,
  `_load_row_pagination.html`, `_local_odoo_dialog.html`,
  `_setup_blockers.html`, `_source_file_remove_dialog.html`,
  `_source_file_remove_form.html`, `_steps.html`, `base.html`, `concepts.html`,
  and `goodbye.html`.
- Data project and workspace setup: `project_list.html`, `project_new.html`,
  `project_business_overview.html`, `workspace_overview.html`,
  `workspace_details.html`, `workspace_governance.html`, `workspace_files.html`,
  `workspace_review.html`, and `workspace_target.html`.
- Source and Odoo setup: `workspace_sources.html`, `workspace_datasets.html`,
  `workspace_derived_entities.html`, `workspace_odoo_capture_progress.html`,
  `workspace_odoo_capture_selection.html`, and `workspace_schema.html`.
- Match, prepare, compare, and load: `workspace_mapping.html`,
  `workspace_transformation_impact.html`, `workspace_prepare.html`,
  `workspace_preparation_progress.html`, `workspace_normalization.html`,
  `workspace_resolution.html`, `workspace_summary.html`, and `workspace_load.html`.
- Integrated Test and Production: `project_test_run_new.html`,
  `project_integrated_run.html`, `project_integrated_qualification.html`,
  `project_production_run_new.html`, and `project_production_activation.html`.

## Drift guard

`tests/test_concept_help.py` checks the canonical concept registry, stable
anchors, accessible fallback links, native-dialog enhancement, focus return,
the one-query data project list, and forbidden internal terms in normal
template copy. The Concepts route renders static presentation data and does not
open a project database or any Odoo boundary.
