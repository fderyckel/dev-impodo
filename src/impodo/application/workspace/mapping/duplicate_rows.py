"""Compare current duplicate members and draft exact source-key exclusions.

The protected identity index owns group membership. Frozen source values supply
the comparison and the unique source key; positions are never persisted as row
rules. Callers keep review, preview and save in one bounded local operation.
"""

from collections.abc import Mapping
from dataclasses import dataclass, replace
from hashlib import sha256
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
from impodo.domain.mapping.row_inclusion import (
    _source_key,
    incoming_parent_links,
    row_is_included,
)
from impodo.domain.mapping.source_conditions import parse_source_text_list
from impodo.domain.matching_order import (
    MatchingIdentityChoice,
    MatchingIdentityChoiceCause,
    MatchingIdentityChoiceFilter,
    MatchingIdentityChoicePage,
)
from impodo.domain.preparation.quality import (
    QualityCauseImpact,
    QualityReviewPage,
    quality_identity_key,
)
from impodo.domain.relationship_dependencies import extract_dataset_dependency_edges
from impodo.domain.serialization import content_hash
from impodo.domain.shared.access import Actor, Capability
from impodo.domain.shared.models import portable_value
from impodo.domain.workspace.contracts import (
    MappingWorkingDraft,
    SourceDataset,
    SourceSelection,
)
from impodo.domain.workspace.errors import WorkspaceError

from .categorical_coverage import CategoricalCoverageService
from .order_service import MatchingOrderService
from .service import MappingWorkspaceService

PREPARED_DUPLICATE_REASONS = frozenset({
    "SOURCE_IDENTITY_DUPLICATE", "POST_TRANSFORM_IDENTITY_COLLISION",
})


@dataclass(frozen=True, slots=True)
class PreparedDuplicateEvidence:
    """Current preparation finding and its bounded protected membership."""

    run_id: str
    page: QualityReviewPage
    collision_sizes: Mapping[str, int]


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
    exclusion_source_key: str
    identifier_message: str
    quality_cause: str = ""
    source_numbers: tuple[object, ...] = ()


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
        exclusion_source_key: str = "",
        prepared: PreparedDuplicateEvidence | None = None,
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
        cause = prepared.page.selected_cause if prepared is not None else None
        dataset = next((
            item for item in selection.datasets
            if (item.name == cause.dataset if cause is not None else item.dataset_id == dataset_id)
        ), None)
        if dataset is None or (dataset_id and dataset_id != dataset.dataset_id):
            raise WorkspaceError("Choose a current table for duplicate review.")
        dataset_id = dataset.dataset_id
        mapping = next(
            (
                item
                for item in draft.definition.datasets
                if item.dataset_id == dataset_id
            ),
            None,
        )
        if mapping is None:
            raise WorkspaceError("Choose a current table for duplicate review.")
        exclusion_key = exclusion_source_key or (
            mapping.source_identity_column_keys[0]
            if len(mapping.source_identity_column_keys) == 1
            else ""
        )
        columns_by_key = {column.stable_key: column for column in dataset.columns}
        if exclusion_key and exclusion_key not in columns_by_key:
            raise WorkspaceError("Choose a current source column for exclusions.")
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
        ) if prepared is None else None
        keys = tuple(
            dict.fromkeys(
                (
                    *mapping.source_identity_column_keys,
                    *((exclusion_key,) if exclusion_key else ()),
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
        raw = self.values.source_condition_rows(
            workspace_id, dataset_id, keys,
            include_source_numbers=prepared is not None,
        )
        admitted = tuple(
            (position, row)
            for position, values in enumerate(raw)
            if row_is_included(mapping.row_inclusion, row := dict(zip(
                keys, values if prepared is None else values[1:], strict=True,
            )), gate)
        )
        included = tuple(row for _, row in admitted)
        if prepared is not None:
            page = _prepared_duplicate_page(
                prepared, dataset, draft,
                {raw[position][0]: index for index, (position, _) in enumerate(admitted, 1)},
            )
        if page is None or any(item.member_position > len(included) for item in page.rows):
            raise WorkspaceError(
                "The duplicate rows no longer agree with this check. Check current data again."
            )
        identifier_message = self._identifier_problem(
            included,
            exclusion_key,
            columns_by_key[exclusion_key].source_name if exclusion_key else "",
        )
        columns = tuple(item.stable_key for item in dataset.columns)
        full_rows = self.values.source_condition_rows_at_positions(
            workspace_id,
            dataset_id,
            columns,
            tuple(admitted[item.member_position - 1][0] for item in page.rows),
        )
        if identifier_message and not exclusion_source_key:
            # The operator has already selected the rows. Find a source column
            # that can save exactly that choice without changing matching keys.
            candidates = self._candidate_columns(columns, full_rows)
            if candidates:
                probe_keys = tuple(dict.fromkeys((*keys, *candidates)))
                probe_rows = self.values.source_condition_rows(
                    workspace_id, dataset_id, probe_keys
                )
                included = tuple(
                    row
                    for values in probe_rows
                    if row_is_included(
                        mapping.row_inclusion,
                        row := dict(zip(probe_keys, values, strict=True)),
                        gate,
                    )
                )
                if len(included) != len(admitted):
                    raise WorkspaceError(
                        "The source rows changed. Check current data again before excluding rows."
                    )
                for candidate in candidates:
                    if not self._identifier_problem(
                        included, candidate, columns_by_key[candidate].source_name
                    ):
                        exclusion_key, identifier_message = candidate, ""
                        break
        unavailable = self._unavailable(mapping, exclusion_key)
        return DuplicateRows(
            page,
            draft,
            selection,
            dataset,
            mapping,
            tuple(dict(zip(columns, values, strict=True)) for values in full_rows),
            included,
            unavailable,
            exclusion_key,
            identifier_message,
            cause.cause_key if cause is not None else "",
            tuple(raw[admitted[item.member_position - 1][0]][0] for item in page.rows)
            if prepared is not None else (),
        )

    @staticmethod
    def _candidate_columns(
        columns: tuple[str, ...], rows: tuple[tuple[object, ...], ...]
    ) -> tuple[str, ...]:
        """Bound fallback discovery before one narrow whole-population proof."""
        candidates = []
        for index, column in enumerate(columns):
            values = tuple(
                "" if row[index] is None else str(row[index]) for row in rows
            )
            if (
                values
                and len(set(values)) == len(values)
                and all(
                    value and value == value.strip() and "," not in value
                    for value in values
                )
                and sum(len(value) + 2 for value in values) <= 10_000
            ):
                candidates.append(column)
                if len(candidates) == 8:
                    break
        return tuple(candidates)

    @staticmethod
    def _unavailable(
        mapping: DatasetMapping,
        key: str,
    ) -> str:
        """Preserve policies that cannot safely accept the selected condition."""
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

    @staticmethod
    def _identifier_problem(
        rows: tuple[dict[str, object], ...], key: str, label: str
    ) -> str:
        """Explain why the exclusion column cannot yet distinguish single rows."""
        if not key:
            return "Impodo could not find a unique source identifier to save these individual row exclusions safely. Your selections and matching choices are kept."
        values = [None if row.get(key) is None else str(row[key]) for row in rows]
        if any(value is None or not value.strip() for value in values) or len(
            {value.strip() if value is not None else None for value in values}
        ) != len(values):
            return f"{label} is blank or repeated, and Impodo could not find another unique source identifier to save these individual row exclusions safely. Your selections and matching choices are kept."
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
        if review.identifier_message:
            raise WorkspaceError(review.identifier_message)
        allowed = {item.member_position for item in review.page.rows}
        selected = set(positions)
        if (
            not selected
            or len(selected) != len(positions)
            or not selected.issubset(allowed)
        ):
            raise WorkspaceError("Select duplicate source rows from the current page.")
        key = review.exclusion_source_key
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
        impacts = self._impacts(workspace_id, review, updated)
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
            "exclusion_column": next(
                column.source_name
                for column in review.dataset.columns
                if column.stable_key == key
            ),
            "preview_hash": content_hash(
                {
                    "check": review.page.check_id,
                    "quality_cause": review.quality_cause,
                    "draft": review.draft.content_hash,
                    "data": review.selection.content_hash,
                    "data_version_id": review.selection.data_version_id,
                    "positions": sorted(selected),
                    "exclusion_source_key": key,
                    "impacts": impacts,
                }
            ),
        }
        return updated, preview

    def _impacts(
        self,
        workspace_id: str,
        review: DuplicateRows,
        updated: RowInclusionPolicy,
    ) -> list[dict[str, object]]:
        """Count currently included children without silently cascading decisions."""
        results = []
        names = {item.dataset_id: item.name for item in review.selection.datasets}
        edges = extract_dataset_dependency_edges(review.draft.definition.datasets)
        identity = review.mapping.source_identity_column_keys
        before_keys = {_source_key(identity, row) for row in review.included}
        after_keys = {
            _source_key(identity, row)
            for row in review.included
            if row_is_included(updated, row)
        }
        removed_keys = {
            key for key in before_keys - after_keys if key and None not in key
        }
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
            if not links or not removed_keys:
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
                    and _source_key(link.source_keys, row) in removed_keys
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


def _prepared_duplicate_page(
    evidence: PreparedDuplicateEvidence,
    dataset: SourceDataset,
    draft: MappingWorkingDraft,
    included_positions: Mapping[object, int],
) -> MatchingIdentityChoicePage:
    """Adapt current preparation evidence to the shared exact exclusion preview."""

    page = evidence.page
    cause = page.selected_cause
    if cause is None or cause.reason_code not in PREPARED_DUPLICATE_REASONS:
        raise WorkspaceError("Choose a current duplicate finding from Prepare data.")
    members = []
    for item in page.items:
        if item.cause_impact is not QualityCauseImpact.DIRECT:
            continue
        row = item.prepared_row
        if (
            row is None
            or row.dataset != dataset.name
            or row.lineage.physical_sources != {dataset.dataset_id: (row.source_row,)}
            or row.lineage.mapping_hash != draft.content_hash
            or row.lineage.source_selection_hash != draft.definition.source_selection_hash
            or row.source_row not in included_positions
        ):
            raise WorkspaceError("The prepared duplicate rows no longer match the saved source and rules. Prepare data again.")
        if cause.reason_code == "SOURCE_IDENTITY_DUPLICATE":
            count = next((issue.affected_count for issue in row.issues if issue.code == cause.reason_code), 0)
            group_hash = content_hash(portable_value(row.source_identity))
            label = " / ".join("(blank)" if value is None else str(value) for value in row.source_identity)
            field = "Source identifier"
        else:
            count = evidence.collision_sizes.get(row.row_id, 0)
            key = quality_identity_key(row)
            if count < 2 or key is None:
                raise WorkspaceError("The saved Odoo collision group is unavailable. Prepare data again.")
            group_hash = "sha256:" + sha256(key).hexdigest()
            label = " / ".join(str(value) for _, value in item.prepared_identity_values) or item.row.record_label
            field = "Odoo match"
        members.append(MatchingIdentityChoice(
            ordinal=len(members), dataset_id=dataset.dataset_id,
            target_field=field, related_model=row.target_model,
            related_fields=(field,), key=(label[:4096],),
            cause=MatchingIdentityChoiceCause.REPEATED_SOURCE,
            target_match_count=0, incoming_match_count=0,
            group_hash=group_hash, member_origin="source",
            member_position=included_positions[row.source_row], source_match_count=count,
        ))
    if not members:
        raise WorkspaceError("There are no directly affected duplicate rows on this page.")
    start = (page.page - 1) * 100 + 1
    return MatchingIdentityChoicePage(
        tuple(members), page.matching_count, start, min(start + 99, page.matching_count),
        page.page - 1 if page.page > 1 else None,
        page.page + 1 if page.page < page.page_count else None,
        check_id=evidence.run_id,
    )
