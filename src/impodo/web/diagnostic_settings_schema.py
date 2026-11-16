"""Allowlist configuration metadata separately from source and target values.

Only these domain-owned dataclasses may enter a settings diagnostic. New
fields remain omitted until reviewed here; row-bearing evidence is never
walked. See ``docs/developer/workflow/03-match-data.md`` and
``tests/integration/web/test_diagnostic_settings.py``.
"""

from __future__ import annotations


def _schemas(module: str, definitions: dict[str, str]) -> dict[str, frozenset[str]]:
    """Qualify approved types and their explicitly reviewed metadata fields."""

    return {
        f"impodo.{module}.{name}": frozenset(fields.split())
        for name, fields in definitions.items()
    }


SETTINGS_SCHEMAS = {
    **_schemas(
        "domain.workspace.workbench",
        {
            "WorkspaceState": "workspace_id source_mode data_classification retention_days odoo_connection_mode destination_odoo_connection_mode intended_models intended_applications status revision created_at updated_at registered_at mapping_version current_run_id approval_status odoo_company destination_company destination_verified_odoo_version source_database_generation_hash destination_database_generation_hash destination_match_plan transfer_order_plan saved_restore_generation_id transfer_evidence_recovery_step",
            "SourceFile": "file_id size_bytes sha256 received_at",
        },
    ),
    **_schemas(
        "domain.workspace.contracts",
        {
            "SourceConfiguration": "file_id source_sha256 catalog_hash encoding delimiter selected_table_keys warnings_acknowledged confirmed_at",
            "SourceSelection": "selection_id version data_version_id created_at content_hash datasets",
            "SourceDataset": "dataset_id name source row_count columns",
            "SourceDatasetColumn": "ordinal source_name stable_key candidate_type",
            "OdooSchemaCatalog": "workspace_id policy_hash captured_at connection_mode odoo_version content_hash origin read_context_hash connection_target_hash last_checked_at models pending_refresh",
            "SchemaModel": "name label fields unique_constraints",
            "SchemaField": "name label type required readonly relation relation_field stored computed has_inverse related translated company_dependent searchable sortable exportable digits currency_field create_default_present",
            "MappingWorkingDraft": "mapping_id version workspace_id base_mapping_version definition updated_at",
        },
    ),
    **_schemas(
        "domain.source_binding",
        {
            "FileSourceBinding": "file_id table_key source_sha256 catalog_hash encoding delimiter header_row",
            "OdooSourceBinding": "capture_selection_hash model policy_hash connection_target_hash schema_scope_hash context_hash",
            "DerivedSourceBinding": "rule_hash input_dataset_ids data_hash hierarchy_parent",
        },
    ),
    **_schemas(
        "domain.mapping.artifacts",
        {
            "MappingRevision": "mapping_id version parent_version definition created_at",
            "MappingSubmission": "mapping_id mapping_version mapping_content_hash validation_hash submitted_at",
        },
    ),
    **_schemas(
        "domain.mapping.contracts",
        {
            "MappingDefinition": "mapping_id source_selection_hash schema_hash datasets contract_version",
            "DatasetMapping": "dataset_id target_model row_inclusion mode on_existing source_identity_column_keys source_record_identity generated_ordering target_identity target_scope fields relationships target_field_dispositions approved_write_fields control_definitions control_expectations",
            "RowInclusionPolicy": "mode conditions join included_parent_field",
            "RowInclusionCondition": "condition_id source_column_key operator comparison_value value_type",
            "IdentityComponentMapping": "source_column_keys target_fields value_type resolver null_policy selected_matching_field blank_source_column_key",
            "RelationshipResolver": "origin dataset_id model key_mappings scope_mappings dataset_projection_field value_mappings",
            "ReferenceKeyMapping": "source_column_key target_field preparation",
            "ScalarFieldMapping": "target_field source_column_key value_source literal_value concatenation transform validation value_mappings value_type required required_on_create compare validate_only null_policy reference_lookup categorical_policy selection_rules",
            "RelationshipMapping": "target_field kind source_column_keys resolver compare validate_only required required_on_create on_missing on_ambiguous operation separator null_policy categorical_policy value_source constant_reference blank_source_values blank_source_action blank_create_values selected_matching_field",
            "ScalarConcatenation": "source_column_keys separator blank_handling trim_parts",
            "ConstantBusinessReference": "key_values scope_values",
            "ConstantReferenceComponent": "target_field value",
            "TargetFieldDisposition": "target_field handling",
            "ReferenceLookupMapping": "reference_id reference_content_hash key_source_column_keys value_field on_blank on_unknown",
            "SelectionRuleSet": "rules otherwise_value",
            "SelectionRule": "rule_id conditions target_value join",
            "SelectionCondition": "condition_id source_column_key operator comparison_value value_type",
            "BusinessControlDefinition": "control_id name target_field unit tolerance calculation invariant_expectation",
            "MappingControlExpectation": "control_id expected_total",
        },
    ),
    **_schemas(
        "domain.mapping.source_record_identity",
        {
            "SourceRecordIdentity": "namespace allow_new_records existing_external_id_column_key",
        },
    ),
    **_schemas(
        "domain.mapping.generated_ordering",
        {
            "GeneratedOrdering": "target_field group_column_keys sort_column_keys sort_value_types descending blanks start step",
        },
    ),
    **_schemas(
        "domain.mapping.reference_preparation",
        {
            "ReferencePreparation": "parts formula bindings",
            "ReferencePart": "source_column_key literal",
        },
    ),
    **_schemas(
        "domain.recipe.value_rules",
        {
            "ScalarTransformPolicy": "trim collapse_whitespace empty_as_null case_mode decimal_locale date_format timezone decimal_places rounding_mode formula text_steps",
            "TextTransformStep": "kind search_value replacement_value search_mode replace_all characters",
            "ScalarValidationPolicy": "exact_length segment_location segment_length character_class pattern",
        },
    ),
    **_schemas(
        "domain.schema.governance",
        {
            "SchemaGovernance": "governance_id version workspace_id catalog_hash permitted_models business_keys recorded_at",
            "BusinessKeyDefinition": "key_id model key_fields scope_fields status recommendation_basis recommendation_policy_version",
        },
    ),
    **_schemas(
        "domain.workspace.supporting_review",
        {
            "SupportingReview": "workspace_id version operation_id binding_hash choices recorded_at contract_version",
            "SupportingChoice": "source_model field_name action dataset_id key_fields scope_fields display_field",
        },
    ),
    **_schemas(
        "domain.odoo.company",
        {
            "OdooCompanySelection": "target_hash company_id",
        },
    ),
    **_schemas(
        "domain.workspace.derived_entities",
        {
            "DerivedEntityPlan": "plan_id version workspace_id source_selection_hash rules updated_at contract_version dataset_uses",
            "DerivedEntityRule": "rule_id output_dataset_name source_dataset_id source_column_key target_model target_name_field external_id_namespace parent_separator blank_policy",
            "HierarchicalLookupRule": "rule_id output_dataset_name source_dataset_id source_level_column_keys target_model target_name_field external_id_namespace missing_parent missing_leaf all_blank existing_parent",
            "HierarchyValuePolicy": "mode value",
            "RelatedDatasetRule": "rule_id source_dataset_id parent_dataset_name child_dataset_name parent_key_column_key child_key_column_key scope_column_key blank_policy",
            "SourceDatasetUsePolicy": "dataset_id use",
        },
    ),
    **_schemas(
        "domain.preparation.quality",
        {
            "QualityRuleSet": "ruleset_id workspace_id version parent_version mapping_hash schema_hash rules coverage_scope_hash reference_bundle_hash contract_version",
            "QualityRule": "rule_id dataset family input_fields parameters outcome owner_role source review_by_days evidence_display",
        },
    ),
    **_schemas(
        "domain.preparation.staging",
        {
            "StagingRunSummary": "run_id workspace_id content_hash mapping_id mapping_version contract_version evaluator_version status published_at reconciliation datasets",
        },
    ),
    **_schemas(
        "domain.preparation.staging_contracts",
        {
            "StagingReconciliation": "total_rows candidate_rows reference_rows blocked_rows quarantined_rows excluded_rows",
            "StagingDatasetReconciliation": "dataset target_model physical_dataset_id role input_rows input_rows_used output_rows lineage_links created_rows combined_rows unrepresented_rows candidate_rows reference_rows blocked_rows quarantined_rows excluded_rows",
        },
    ),
    **_schemas(
        "domain.preparation.normalization",
        {
            "NormalizationRunSummary": "run_id workspace_id content_hash staging_run_id staging_content_hash quality_run_id quality_content_hash eligible_dataset_hash status lifecycle_version published_at eligible_record_count changed_record_count automatic_group_count decision_group_count reviewed_group_count set_aside_record_count effective_dataset_run_id effective_dataset_hash",
        },
    ),
    **_schemas(
        "domain.preflight.reports",
        {
            "ReadinessReport": "run_id workspace_id mapping_id mapping_version mapping_content_hash staging_run_id staging_content_hash quality_run_id quality_content_hash normalization_run_id normalization_content_hash normalization_lifecycle_version eligible_dataset_hash frozen_input_hash requirement_plan_hash metadata_snapshot_hash record_snapshot_hash result_hash manifest_hash target_hash target_odoo_version checked_at contract_version operation_id",
        },
    ),
    **_schemas(
        "application.workspace.navigation",
        {
            "WorkspaceNavigationFacts": "workspace_id source_selection_hash mapping_complete schema_attention schema_content_hash staging_run_id resolution_status quality_run_id normalization_run_id normalization_status normalization_decision_count normalization_reviewed_count workflow_issue_must_fix_count workflow_issue_review_count preflight_status execution_state transfer_execution_run_id transfer_reconciliation_status active_preparation_job_id active_load_job_id",
        },
    ),
    **_schemas(
        "application.workspace.execution.navigation",
        {
            "ExecutionNavigationState": "summary execution_run_id execution_status reconciliation_status",
            "ExecutionPreviewSummary": "preflight_run_id snapshot_hash snapshot_root_hash comparison_status create_count update_count unchanged_count blocked_count ambiguous_count relationship_blocker_count target_hash target_odoo_version read_context_hash execution_shape_ready contract_version business_intent_state",
        },
    ),
    **_schemas(
        "domain.execution.load_control",
        {
            "LoadControl": "policy state reason consecutive_limit window_size minimum_results rejection_percent isolation_limit isolation_calls contract_version",
        },
    ),
    **_schemas(
        "domain.structural",
        {
            "StructuralOutputColumn": "column_key source_name candidate_type",
            "StructuralProjection": "output_column_key source_dataset_id source_column_key",
            "JoinKey": "left_column_key right_column_key value_type",
            "ExactJoinRule": "rule_id output_dataset_name left_dataset_id right_dataset_id keys output_columns projections kind require_all_right_rows require_unique_left map_output group_id group_step",
            "UnionBranch": "source_dataset_id projections",
            "UnionAllRule": "rule_id output_dataset_name output_columns branches",
            "GroupKey": "output_column_key source_column_key value_type",
            "AggregateSpec": "output_column_key operation source_column_key unit",
            "GroupAggregateRule": "rule_id output_dataset_name source_dataset_id output_columns group_keys aggregates",
        },
    ),
    **_schemas(
        "domain.workspace.destination_matching",
        {
            "DestinationMatchPlan": "workspace_id source_selection_hash source_schema_hash destination_target_hash destination_read_context_hash destination_schema_snapshot_hash destination_record_snapshot_hash model_matches relationship_matches create_field_decisions recorded_at contract_version row_inclusion_confirmation_hash",
            "DestinationModelMatch": "dataset_id dataset_name model model_label source_column_key key_field key_field_label source_row_count source_distinct_key_count source_blank_row_count source_duplicate_key_count destination_existing_key_count destination_duplicate_key_count destination_create_key_count compatible_fields missing_fields incompatible_fields destination_limit_reached source_column_keys key_fields destination_managed_fields unresolved_create_fields requires_workflow_handler excluded_missing_fields excluded_incompatible_fields destination_handling minimum_create_fields minimum_create_relationship_fields row_inclusion scope_fields scope_dataset_ids",
            "DestinationRelationshipMatch": "dataset_id dataset_name model field_name kind related_dataset_id related_model related_key_field operation inverse_field source_owner_count source_link_count source_blank_owner_count destination_reused_link_count incoming_link_count missing_related_record_count ambiguous_destination_link_count source_evidence_available required related_key_fields",
            "DestinationCreateFieldDecision": "dataset_id model field_name field_type provider_kind decision_kind field_contract_hash reviewed source_field_name related_model related_identity_fields source_dataset_id source_reference_requires_create",
        },
    ),
    **_schemas(
        "domain.workspace.transfer_order",
        {
            "TransferOrderPlan": "workspace_id destination_match_plan_hash source_selection_hash source_schema_hash destination_target_hash datasets dependencies waves blockers recorded_at contract_version",
            "TransferOrderDataset": "dataset_id dataset_name model source_row_count destination_existing_key_count destination_create_key_count wave",
            "TransferOrderDependency": "owner_dataset_id dependency_dataset_id owner_model dependency_model field_name kind strength incoming_link_count deferred",
            "TransferOrderWave": "sequence dataset_ids",
            "TransferOrderBlocker": "dataset_id code field_name dependency_dataset_id",
        },
    ),
    **_schemas(
        "domain.odoo_capture",
        {
            "OdooCaptureSelection": "selection_id version data_version_id dataset_name model field_names filter_clauses filter_policy max_rows page_size consistency policy_hash connection_target_hash schema_scope_hash context_hash created_at content_hash contract_version capture_role",
            "OdooCaptureFilterClause": "field_name operator values",
        },
    ),
    **_schemas(
        "domain.odoo_relationship_scope",
        {
            "OdooRelationshipScope": "scope_id version decisions root_models explicit_actions_by_model recorded_at content_hash contract_version",
            "OdooRelationshipScopeDecision": "source_model field_name relation_model required handling action relationship_type recommendation_profile_id recommendation_profile_version",
        },
    ),
}

# Business literals and authored expressions can contain source values. Keep
# their presence and cardinality, never their contents, in routine diagnostics.
REDACTED_SETTINGS = frozenset(
    {
        "value",
        "values",
        "literal",
        "literal_value",
        "comparison_value",
        "target_value",
        "otherwise_value",
        "value_mappings",
        "blank_source_values",
        "blank_create_values",
        "search_value",
        "replacement_value",
        "characters",
        "formula",
        "pattern",
        "parameters",
        "expected_total",
        "invariant_expectation",
    }
)
