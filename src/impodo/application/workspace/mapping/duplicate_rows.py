"""Compare current duplicate members and draft exact source-key exclusions.

The protected identity index owns group membership. Frozen source values supply
the comparison and the unique source key; positions are never persisted as row
rules. Callers keep review, preview and save in one bounded local operation.
"""

from dataclasses import dataclass, replace
from uuid import NAMESPACE_URL, uuid5

from impodo.domain.mapping.contracts import (
    MAX_ROW_INCLUSION_CONDITIONS,
    DatasetMapping,
    RowInclusionCondition,
    RowInclusionJoin,
    RowInclusionMode,
    RowInclusionPolicy,
    SelectionConditionOperator,
)
from impodo.domain.mapping.row_inclusion import incoming_parent_links, row_is_included
from impodo.domain.mapping.source_conditions import parse_source_text_list
from impodo.domain.matching_order import (
    MatchingIdentityChoiceFilter,
    MatchingIdentityChoicePage,
)
from impodo.domain.relationship_dependencies import extract_dataset_dependency_edges
from impodo.domain.serialization import content_hash
from impodo.domain.shared.access import Actor, Capability
from impodo.domain.workspace.contracts import (
    MappingWorkingDraft,
    SourceDataset,
    SourceSelection,
)
from impodo.domain.workspace.errors import WorkspaceError

from .categorical_coverage import CategoricalCoverageService
from .order_service import MatchingOrderService
from .service import MappingWorkspaceService


@dataclass(frozen=True, slots=True)
class DuplicateRows:
    """One current protected page with row-aligned frozen values."""

    page: MatchingIdentityChoicePage
    draft: MappingWorkingDraft
    selection: SourceSelection
    dataset: SourceDataset
    mapping: DatasetMapping
    rows: tuple[dict[str, object], ...]
    included: tuple[dict[str, object], ...]
    unavailable: str


class DuplicateRowReview:
    """Reuse current identity proof and existing portable row-rule semantics."""

    def __init__(
        self,
        mapping: MappingWorkspaceService,
        order: MatchingOrderService,
        values: CategoricalCoverageService,
    ) -> None:
        self.mapping = mapping
        self.order = order
        self.values = values

    def review(
        self,
        workspace_id: str,
        dataset_id: str,
        check_id: str,
        *,
        after: int | None = None,
        before: int | None = None,
        actor: Actor,
    ) -> DuplicateRows:
        """Authorize and bind the displayed rows to the exact saved check."""
        for capability in (Capability.MAPPING_EDIT, Capability.PROTECTED_EVIDENCE_READ):
            self.mapping.authorization.require(
                actor, capability, workspace_id=workspace_id
            )
        selection = self.mapping.sources.get_mapping_source_selection(workspace_id)
        schema = self.mapping.schemas.get_odoo_schema_catalog(workspace_id)
        governance = self.mapping.schemas.get_schema_governance(workspace_id)
        draft = self.mapping.mappings.get_mapping_working_draft(workspace_id)
        if selection is None or schema is None or draft is None or not check_id:
            raise WorkspaceError(
                "Save your matching choices and check current data before reviewing duplicate rows."
            )
        dataset = next(
            (item for item in selection.datasets if item.dataset_id == dataset_id), None
        )
        mapping = next(
            (
                item
                for item in draft.definition.datasets
                if item.dataset_id == dataset_id
            ),
            None,
        )
        if dataset is None or mapping is None:
            raise WorkspaceError("Choose a current table for duplicate review.")
        page = self.order.identity_choice_review_page(
            workspace_id,
            selection,
            schema,
            governance,
            draft,
            MatchingIdentityChoiceFilter(
                dataset_id=dataset_id, kind="duplicates", cause="repeated_source"
            ),
            page_size=100,
            after=after,
            before=before,
            expected_check_id=check_id,
            actor=actor,
        )
        keys = tuple(
            dict.fromkeys(
                (
                    *mapping.source_identity_column_keys,
                    *(
                        item.source_column_key
                        for item in mapping.row_inclusion.conditions
                    ),
                )
            )
        )
        parents = self.values.included_parent_rows(
            workspace_id, draft.definition.datasets
        )
        gate = parents.get(dataset_id)
        keys = tuple(dict.fromkeys((*keys, *(gate.source_keys if gate else ())))) or (
            dataset.columns[0].stable_key,
        )
        raw = self.values.source_condition_rows(workspace_id, dataset_id, keys)
        admitted = tuple(
            (position, row)
            for position, values in enumerate(raw)
            if row_is_included(
                mapping.row_inclusion, row := dict(zip(keys, values, strict=True)), gate
            )
        )
        included = tuple(row for _, row in admitted)
        if any(item.member_position > len(included) for item in page.rows):
            raise WorkspaceError(
                "The duplicate rows no longer agree with this check. Check current data again."
            )
        unavailable = self._unavailable(mapping, included)
        columns = tuple(item.stable_key for item in dataset.columns)
        full_rows = self.values.source_condition_rows_at_positions(
            workspace_id,
            dataset_id,
            columns,
            tuple(admitted[item.member_position - 1][0] for item in page.rows),
        )
        return DuplicateRows(
            page,
            draft,
            selection,
            dataset,
            mapping,
            tuple(dict(zip(columns, values, strict=True)) for values in full_rows),
            included,
            unavailable,
        )

    @staticmethod
    def _unavailable(
        mapping: DatasetMapping, rows: tuple[dict[str, object], ...]
    ) -> str:
        """Refuse rules that cannot distinguish individual rows exactly."""
        if len(mapping.source_identity_column_keys) != 1:
            return "Choose one unique source identifier before excluding individual rows. You can still compare the rows below."
        key = mapping.source_identity_column_keys[0]
        values = [None if row.get(key) is None else str(row[key]) for row in rows]
        if any(value is None or not value.strip() for value in values) or len(
            {value.strip() if value is not None else None for value in values}
        ) != len(values):
            return "The source identifier is blank or repeated. Choose a unique source identifier before excluding individual rows."
        policy = mapping.row_inclusion
        if policy.included_parent_field or policy.join is not RowInclusionJoin.ALL:
            return "This table uses a parent rule or an any-condition rule. Review Rows to use manually so that your existing selection is preserved."
        if len(policy.conditions) >= MAX_ROW_INCLUSION_CONDITIONS and not any(
            item.source_column_key == key
            and item.operator is SelectionConditionOperator.NOT_IN
            and item.value_type == "string"
            for item in policy.conditions
        ):
            return "Rows to use already has eight conditions. Review its conditions before adding exclusions."
        return ""

    def preview(
        self,
        workspace_id: str,
        review: DuplicateRows,
        positions: tuple[int, ...],
    ) -> tuple[RowInclusionPolicy, dict[str, object]]:
        """Prove the exact removed population and count affected incoming links."""
        if review.unavailable:
            raise WorkspaceError(review.unavailable)
        allowed = {item.member_position for item in review.page.rows}
        selected = set(positions)
        if (
            not selected
            or len(selected) != len(positions)
            or not selected.issubset(allowed)
        ):
            raise WorkspaceError("Select duplicate source rows from the current page.")
        key = review.mapping.source_identity_column_keys[0]
        values = tuple(
            str(review.included[position - 1][key]) for position in sorted(selected)
        )
        if any(value != value.strip() or "," in value for value in values):
            raise WorkspaceError(
                "These source identifiers cannot be represented as an exact exclusion list. Review Rows to use manually."
            )
        policy = review.mapping.row_inclusion
        existing = next(
            (
                item
                for item in policy.conditions
                if item.source_column_key == key
                and item.operator is SelectionConditionOperator.NOT_IN
                and item.value_type == "string"
            ),
            None,
        )
        excluded = tuple(
            dict.fromkeys(
                (
                    *(
                        parse_source_text_list(existing.comparison_value)
                        if existing
                        else ()
                    ),
                    *values,
                )
            )
        )
        condition = RowInclusionCondition(
            condition_id=existing.condition_id
            if existing
            else str(
                uuid5(
                    NAMESPACE_URL,
                    f"impodo:duplicate-exclusions:{review.dataset.dataset_id}:{key}",
                )
            ),
            source_column_key=key,
            operator=SelectionConditionOperator.NOT_IN,
            comparison_value=", ".join(excluded),
            value_type="string",
        )
        # Validate the literal with the same parser used by checking and Recipes.
        parse_source_text_list(condition.comparison_value)
        updated = RowInclusionPolicy(
            mode=RowInclusionMode.MATCHING_ROWS,
            join=RowInclusionJoin.ALL,
            conditions=tuple(
                condition if item == existing else item for item in policy.conditions
            )
            if existing
            else (*policy.conditions, condition),
        )
        removed = {
            position
            for position, row in enumerate(review.included, 1)
            if not row_is_included(updated, row)
        }
        if removed != selected:
            raise WorkspaceError(
                "This rule would exclude other rows. Review Rows to use manually."
            )
        if len(removed) == len(review.included):
            raise WorkspaceError(
                "Keep at least one row in this table before applying exclusions."
            )
        impacts = self._impacts(workspace_id, review, set(values))
        groups: dict[str, tuple[int, int]] = {}
        for item in review.page.rows:
            total, removed_count = groups.get(
                item.group_hash, (item.source_match_count, 0)
            )
            groups[item.group_hash] = (
                total,
                removed_count + int(item.member_position in selected),
            )
        preview = {
            "excluded_count": len(removed),
            "included_count": len(review.included) - len(removed),
            "remaining_groups_on_page": sum(
                total - count > 1 for total, count in groups.values()
            ),
            "impacts": impacts,
            "preview_hash": content_hash(
                {
                    "check": review.page.check_id,
                    "draft": review.draft.content_hash,
                    "data": review.selection.content_hash,
                    "data_version_id": review.selection.data_version_id,
                    "positions": sorted(selected),
                    "impacts": impacts,
                }
            ),
        }
        return updated, preview

    def _impacts(
        self, workspace_id: str, review: DuplicateRows, removed: set[str]
    ) -> list[dict[str, object]]:
        """Count currently included children without silently cascading decisions."""
        results = []
        names = {item.dataset_id: item.name for item in review.selection.datasets}
        edges = extract_dataset_dependency_edges(review.draft.definition.datasets)
        normalized_removed = {value.strip() for value in removed}
        parents = self.values.included_parent_rows(
            workspace_id, review.draft.definition.datasets
        )
        for mapping in review.draft.definition.datasets:
            links = tuple(
                link
                for link in dict.fromkeys(incoming_parent_links(mapping))
                if link.dataset_id == review.dataset.dataset_id
            )
            for edge in edges:
                if (
                    edge.owner_dataset == mapping.dataset_id
                    and edge.dependency_dataset == review.dataset.dataset_id
                    and edge.target_field not in {link.target_field for link in links}
                ):
                    results.append(
                        {
                            "table": names.get(mapping.dataset_id, mapping.dataset_id),
                            "field": edge.target_field,
                            "count": None,
                            "follows_parent": False,
                        }
                    )
            if not links:
                continue
            gate = parents.get(mapping.dataset_id)
            columns = tuple(
                dict.fromkeys(
                    (
                        *(key for link in links for key in link.source_keys),
                        *(
                            item.source_column_key
                            for item in mapping.row_inclusion.conditions
                        ),
                        *(gate.source_keys if gate else ()),
                    )
                )
            )
            rows = self.values.source_condition_rows(
                workspace_id, mapping.dataset_id, columns
            )
            for link in links:
                count = sum(
                    row_is_included(mapping.row_inclusion, row, gate)
                    and len(link.source_keys) == 1
                    and str(row[link.source_keys[0]]).strip() in normalized_removed
                    for values in rows
                    for row in (dict(zip(columns, values, strict=True)),)
                )
                if count:
                    results.append(
                        {
                            "table": names.get(mapping.dataset_id, mapping.dataset_id),
                            "field": link.target_field,
                            "count": count,
                            "follows_parent": mapping.row_inclusion.included_parent_field
                            == link.target_field,
                        }
                    )
        return results

    def save(
        self,
        workspace_id: str,
        review: DuplicateRows,
        policy: RowInclusionPolicy,
        *,
        operation_id: str,
        actor: Actor,
    ) -> MappingWorkingDraft:
        """Persist only the reviewed policy through the normal versioned owner."""
        return self.mapping.save_working_draft(
            workspace_id,
            datasets=tuple(
                replace(item, row_inclusion=policy)
                if item.dataset_id == review.dataset.dataset_id
                else item
                for item in review.draft.definition.datasets
            ),
            expected_version=review.draft.version,
            operation_id=operation_id,
            actor=actor,
        )
