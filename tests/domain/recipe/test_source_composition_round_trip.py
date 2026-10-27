"""Prove exact source compositions remain portable across Recipe reuse."""

from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

from impodo.application.recipe_application_compilation import (
    RecipeApplicationCompiler,
)
from impodo.application.recipe_compilation_service import RecipeCompiler
from impodo.domain.preparation.source import SourceRow, SourceTable
from impodo.domain.shared.access import LOCAL_ACTOR
from impodo.domain.source_binding import FileSourceBinding
from impodo.domain.structural import (
    ExactJoinRule,
    JoinKey,
    JoinKind,
    StructuralError,
    StructuralOutputColumn,
    StructuralProjection,
    execute_structural_rules,
    structural_dataset_id,
    structural_mapping_selection,
)
from impodo.domain.workspace.contracts import (
    SourceDataset,
    SourceDatasetColumn,
    SourceSelection,
)
from impodo.domain.workspace.derived_entities import (
    DerivedEntityPlan,
    RelatedDatasetRule,
    SourceDatasetUse,
    SourceDatasetUsePolicy,
    related_dataset_links,
)


def _hash(marker: str) -> str:
    return f"sha256:{sha256(marker.encode('utf-8')).hexdigest()}"


def _dataset(
    dataset_id: str,
    name: str,
    marker: str,
    columns: tuple[tuple[str, str], ...],
    *,
    table_key: str | None = None,
) -> SourceDataset:
    return SourceDataset(
        dataset_id=dataset_id,
        name=name,
        source=FileSourceBinding(
            file_id=f"file-{dataset_id}",
            table_key=table_key or f"{name}.csv",
            source_sha256=_hash(marker),
            catalog_hash=_hash(marker),
            encoding="utf-8",
            delimiter=",",
            header_row=1,
        ),
        row_count=2,
        columns=tuple(
            SourceDatasetColumn(index, source_name, stable_key, "string")
            for index, (source_name, stable_key) in enumerate(columns, start=1)
        ),
    )


def _selection(*datasets: SourceDataset, marker: str) -> SourceSelection:
    return SourceSelection(
        selection_id=str(uuid4()),
        version=1,
        data_version_id=f"data-version-{marker}",
        created_at=datetime.now(UTC),
        created_by=LOCAL_ACTOR.identity.display_name,
        datasets=tuple(datasets),
        content_hash=_hash(marker),
    )


def _customer_rule(left: SourceDataset, right: SourceDataset) -> ExactJoinRule:
    return ExactJoinRule(
        rule_id=str(uuid4()),
        output_dataset_name="customer_contacts",
        left_dataset_id=left.dataset_id,
        right_dataset_id=right.dataset_id,
        keys=(
            JoinKey("account_company", "party_company"),
            JoinKey("account_party", "party_id", "integer"),
        ),
        output_columns=(
            StructuralOutputColumn("output_account", "Account", "string"),
            StructuralOutputColumn("output_name", "Name", "string"),
        ),
        projections=(
            StructuralProjection(
                "output_account",
                left.dataset_id,
                "account_number",
            ),
            StructuralProjection("output_name", right.dataset_id, "party_name"),
        ),
        kind=JoinKind.INNER,
        require_all_right_rows=False,
    )


def _portable_preparation(
    selection: SourceSelection,
    rules: tuple[ExactJoinRule, ...],
) -> tuple[dict[str, object], dict[str, str], dict[tuple[str, str], str]]:
    effective = structural_mapping_selection(selection, rules)
    compiler = object.__new__(RecipeCompiler)
    _datasets, dataset_ids, columns = compiler._source_identity(effective.datasets)
    plan = DerivedEntityPlan(
        plan_id=str(uuid4()),
        version=1,
        workspace_id="authoring-workspace",
        source_selection_hash=selection.content_hash,
        rules=rules,
        updated_at=datetime.now(UTC),
        updated_by=LOCAL_ACTOR.identity.display_name,
        dataset_uses=tuple(
            SourceDatasetUsePolicy(
                dataset.dataset_id,
                SourceDatasetUse.PREPARATION_ONLY,
            )
            for dataset in selection.datasets
        ),
    )
    return (
        compiler._preparation(plan, dataset_ids, columns, effective.datasets),
        dataset_ids,
        columns,
    )


def _fresh_bindings(
    authoring: SourceSelection,
    fresh: SourceSelection,
    dataset_ids: dict[str, str],
    columns: dict[tuple[str, str], str],
) -> dict[str, str]:
    fresh_by_name = {item.name: item for item in fresh.datasets}
    bindings: dict[str, str] = {}
    for original in authoring.datasets:
        replacement = fresh_by_name[original.name]
        bindings[dataset_ids[original.dataset_id]] = replacement.dataset_id
        replacement_columns = {
            item.source_name: item.stable_key for item in replacement.columns
        }
        for column in original.columns:
            if column.source_name in replacement_columns:
                bindings[columns[(original.dataset_id, column.stable_key)]] = (
                    replacement_columns[column.source_name]
                )
    return bindings


class _SourceRepository:
    def __init__(self, selection: SourceSelection) -> None:
        self.selection = selection

    def get_source_selection(self, workspace_id: str) -> SourceSelection:
        del workspace_id
        return self.selection


class _PlanRepository:
    def __init__(self) -> None:
        self.current: DerivedEntityPlan | None = None

    def get_derived_entity_plan(self, workspace_id: str) -> DerivedEntityPlan | None:
        del workspace_id
        return self.current

    def save_derived_entity_plan(
        self,
        workspace_id: str,
        plan: DerivedEntityPlan,
        *,
        expected_parent_version: int | None,
        actor,
    ) -> None:
        del workspace_id, expected_parent_version, actor
        self.current = plan


def _materialize(
    preparation: dict[str, object],
    fresh: SourceSelection,
    bindings: dict[str, str],
) -> DerivedEntityPlan:
    compiler = RecipeApplicationCompiler()
    compiler.sources = _SourceRepository(fresh)
    plans = _PlanRepository()
    compiler.preparation = plans
    result = compiler._materialize_preparation(
        "fresh-workspace",
        {"source_preparation": preparation},
        bindings,
        actor=LOCAL_ACTOR,
    )
    assert result is not None
    return result


class RecipeSourceCompositionRoundTripTests(unittest.TestCase):
    def setUp(self) -> None:
        self.accounts = _dataset(
            "authoring-accounts",
            "customer_accounts",
            "a",
            (
                ("Company", "account_company"),
                ("Party", "account_party"),
                ("Account", "account_number"),
                ("Unused", "unused_account"),
            ),
        )
        self.parties = _dataset(
            "authoring-parties",
            "party_details",
            "b",
            (
                ("Company", "party_company"),
                ("Party", "party_id"),
                ("Name", "party_name"),
                ("Private note", "private_note"),
            ),
        )
        self.authoring = _selection(self.accounts, self.parties, marker="c")

    def test_customer_join_and_use_policy_round_trip_without_physical_evidence(
        self,
    ) -> None:
        rule = _customer_rule(self.accounts, self.parties)
        portable, dataset_ids, columns = _portable_preparation(
            self.authoring,
            (rule,),
        )

        serialized = str(portable)
        self.assertNotIn(self.accounts.dataset_id, serialized)
        self.assertNotIn(self.parties.dataset_id, serialized)
        self.assertNotIn(rule.rule_id, serialized)
        self.assertNotIn("private_note", serialized)
        self.assertEqual(
            {item["use"] for item in portable["dataset_uses"]},
            {"preparation_only"},
        )

        fresh_accounts = _dataset(
            "fresh-accounts",
            "customer_accounts",
            "d",
            (
                ("Company", "fresh_account_company"),
                ("Party", "fresh_account_party"),
                ("Account", "fresh_account_number"),
            ),
            table_key="renamed-customer-export-2027.csv",
        )
        fresh_parties = _dataset(
            "fresh-parties",
            "party_details",
            "e",
            (
                ("Company", "fresh_party_company"),
                ("Party", "fresh_party_id"),
                ("Name", "fresh_party_name"),
            ),
            table_key="renamed-party-export-2027.csv",
        )
        fresh = _selection(fresh_accounts, fresh_parties, marker="f")
        plan = _materialize(
            portable,
            fresh,
            _fresh_bindings(self.authoring, fresh, dataset_ids, columns),
        )

        self.assertEqual(
            {item.dataset_id for item in plan.dataset_uses},
            {"fresh-accounts", "fresh-parties"},
        )
        self.assertEqual(
            {item.use for item in plan.dataset_uses},
            {SourceDatasetUse.PREPARATION_ONLY},
        )
        fresh_rule = plan.rules[0]
        assert isinstance(fresh_rule, ExactJoinRule)
        tables = {
            "fresh-accounts": SourceTable(
                dataset="customer_accounts",
                path=Path("renamed-customer-export-2027.csv"),
                headers=("Company", "Party", "Account"),
                rows=(
                    SourceRow(1, {"Company": "UC", "Party": 11, "Account": "C01"}),
                    SourceRow(2, {"Company": "UC", "Party": 12, "Account": "C02"}),
                ),
                content_hash=_hash("d"),
            ),
            "fresh-parties": SourceTable(
                dataset="party_details",
                path=Path("renamed-party-export-2027.csv"),
                headers=("Company", "Party", "Name"),
                rows=(
                    SourceRow(1, {"Company": "UC", "Party": 11, "Name": "Alpha"}),
                    SourceRow(2, {"Company": "UC", "Party": 12, "Name": "Beta"}),
                ),
                content_hash=_hash("e"),
            ),
        }
        output = execute_structural_rules(
            selection=fresh,
            loaded_tables=tables,
            rules=(fresh_rule,),
        ).outputs[0]

        self.assertEqual(len(output.table.rows), 2)
        self.assertEqual(output.table.rows[0].values["Name"], "Alpha")
        self.assertEqual(
            output.lineage[1],
            {"fresh-accounts": (1,), "fresh-parties": (1,)},
        )

        tables["fresh-parties"] = SourceTable(
            dataset="party_details",
            path=Path("renamed-party-export-2027.csv"),
            headers=("Company", "Party", "Name"),
            rows=(
                SourceRow(1, {"Company": "UC", "Party": 11, "Name": "Alpha"}),
                SourceRow(2, {"Company": "UC", "Party": 11, "Name": "Duplicate"}),
            ),
            content_hash=_hash("g"),
        )
        with self.assertRaisesRegex(StructuralError, "not unique"):
            execute_structural_rules(
                selection=fresh,
                loaded_tables=tables,
                rules=(fresh_rule,),
            )

    def test_related_split_and_retained_input_use_round_trip(self) -> None:
        stock = _dataset(
            "authoring-stock",
            "stock_rows",
            "stock-a",
            (("Item", "item"), ("Batch", "batch"), ("Quantity", "quantity")),
        )
        authoring = _selection(stock, marker="stock-selection")
        rule = RelatedDatasetRule(
            rule_id=str(uuid4()),
            source_dataset_id=stock.dataset_id,
            parent_dataset_name="product_lots",
            child_dataset_name="lotted_stock_rows",
            parent_key_column_key="batch",
            child_key_column_key="item",
        )
        plan = DerivedEntityPlan(
            plan_id=str(uuid4()),
            version=1,
            workspace_id="authoring-workspace",
            source_selection_hash=authoring.content_hash,
            rules=(rule,),
            updated_at=datetime.now(UTC),
            updated_by=LOCAL_ACTOR.identity.display_name,
            dataset_uses=(
                SourceDatasetUsePolicy(
                    stock.dataset_id,
                    SourceDatasetUse.MAP_AND_PREPARE,
                ),
            ),
        )
        link = related_dataset_links(plan)[0]
        effective_datasets = (
            stock,
            replace(
                stock,
                dataset_id=link.parent_dataset_id,
                name=rule.parent_dataset_name,
                columns=(stock.columns[1],),
            ),
            replace(
                stock,
                dataset_id=link.child_dataset_id,
                name=rule.child_dataset_name,
            ),
        )
        compiler = object.__new__(RecipeCompiler)
        _datasets, dataset_ids, columns = compiler._source_identity(
            effective_datasets
        )
        portable = compiler._preparation(
            plan,
            dataset_ids,
            columns,
            effective_datasets,
        )

        fresh_stock = _dataset(
            "fresh-stock",
            "stock_rows",
            "stock-b",
            (
                ("Item", "fresh-item"),
                ("Batch", "fresh-batch"),
                ("Quantity", "fresh-quantity"),
            ),
            table_key="stock-2027.csv",
        )
        fresh = _selection(fresh_stock, marker="fresh-stock-selection")
        rebuilt = _materialize(
            portable,
            fresh,
            _fresh_bindings(authoring, fresh, dataset_ids, columns),
        )

        self.assertEqual(
            rebuilt.dataset_uses,
            (
                SourceDatasetUsePolicy(
                    fresh_stock.dataset_id,
                    SourceDatasetUse.MAP_AND_PREPARE,
                ),
            ),
        )
        rebuilt_rule = rebuilt.rules[0]
        assert isinstance(rebuilt_rule, RelatedDatasetRule)
        self.assertEqual(rebuilt_rule.source_dataset_id, fresh_stock.dataset_id)
        self.assertEqual(rebuilt_rule.parent_key_column_key, "fresh-batch")
        self.assertEqual(rebuilt_rule.child_key_column_key, "fresh-item")

    def test_join_chain_rebinds_a_prior_logical_output(self) -> None:
        first = _customer_rule(self.accounts, self.parties)
        first_output = structural_mapping_selection(
            self.authoring,
            (first,),
        ).datasets[-1]
        regions = _dataset(
            "authoring-regions",
            "account_regions",
            "h",
            (("Account", "region_account"), ("Region", "region_name")),
        )
        authoring = _selection(self.accounts, self.parties, regions, marker="i")
        second = ExactJoinRule(
            rule_id=str(uuid4()),
            output_dataset_name="account_directory",
            left_dataset_id=first_output.dataset_id,
            right_dataset_id=regions.dataset_id,
            keys=(JoinKey("output_account", "region_account"),),
            output_columns=(
                StructuralOutputColumn("directory_account", "Account", "string"),
                StructuralOutputColumn("directory_region", "Region", "string"),
            ),
            projections=(
                StructuralProjection(
                    "directory_account",
                    first_output.dataset_id,
                    "output_account",
                ),
                StructuralProjection(
                    "directory_region",
                    regions.dataset_id,
                    "region_name",
                ),
            ),
        )
        portable, dataset_ids, columns = _portable_preparation(
            authoring,
            (first, second),
        )
        fresh_datasets = tuple(
            _dataset(
                f"fresh-{item.name}",
                item.name,
                marker,
                tuple(
                    (column.source_name, f"fresh:{item.name}:{column.ordinal}")
                    for column in item.columns
                ),
            )
            for item, marker in zip(authoring.datasets, ("j", "k", "l"), strict=True)
        )
        fresh = _selection(*fresh_datasets, marker="m")
        bindings = _fresh_bindings(authoring, fresh, dataset_ids, columns)
        plan = _materialize(portable, fresh, bindings)

        by_name = {item.output_dataset_name: item for item in plan.rules}
        rebuilt_first = by_name["customer_contacts"]
        rebuilt_second = by_name["account_directory"]
        assert isinstance(rebuilt_first, ExactJoinRule)
        assert isinstance(rebuilt_second, ExactJoinRule)
        self.assertEqual(
            rebuilt_second.left_dataset_id,
            structural_dataset_id(rebuilt_first),
        )

        compiler = RecipeApplicationCompiler()
        compiler.sources = _SourceRepository(fresh)
        plans = _PlanRepository()
        plans.current = DerivedEntityPlan.from_json(plan.to_json())
        compiler.preparation = plans
        reapplied = compiler._materialize_preparation(
            "fresh-workspace",
            {"source_preparation": portable},
            bindings,
            actor=LOCAL_ACTOR,
        )
        self.assertEqual(reapplied, plans.current)


if __name__ == "__main__":
    unittest.main()
