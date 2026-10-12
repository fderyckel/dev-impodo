from __future__ import annotations

from datetime import datetime, timezone
import json
import unittest
from uuid import uuid4

from impodo.domain.odoo_relationship_scope import (
    OdooRelationshipCaptureAction,
    OdooRelationshipScope,
    OdooRelationshipScopeDecision,
    OdooRelationshipScopeError,
    propose_relationship_action_defaults,
    relationship_scope_decisions,
    relationship_expansion_suggestions,
    review_relationship_scope,
)
from impodo.domain.odoo_source_scope import (
    RelatedDataHandling,
    RelatedDataSuggestion,
)


class OdooRelationshipScopeTests(unittest.TestCase):
    def test_safe_defaults_are_direction_aware_and_model_compatible(self) -> None:
        defaults = propose_relationship_action_defaults(
            (
                _suggestion(
                    "owner_id",
                    "x.owner",
                    RelatedDataHandling.NEEDS_DECISION,
                ),
                _suggestion(
                    "line_ids",
                    "x.line",
                    RelatedDataHandling.INCLUDE_SUPPORTING,
                    profiled=True,
                    relationship_type="one2many",
                ),
                _suggestion(
                    "history_ids",
                    "x.history",
                    RelatedDataHandling.NEEDS_DECISION,
                    relationship_type="one2many",
                ),
                _suggestion(
                    "company_id",
                    "res.company",
                    RelatedDataHandling.REUSE_DESTINATION,
                    profiled=True,
                ),
            )
        )

        self.assertEqual(
            defaults.actions,
            {
                "res.company": OdooRelationshipCaptureAction.MATCH_EXISTING,
                "x.history": OdooRelationshipCaptureAction.DO_NOT_CAPTURE,
                "x.line": OdooRelationshipCaptureAction.CAPTURE_LINKED,
                "x.owner": OdooRelationshipCaptureAction.PRESERVE_LINKED,
            },
        )
        self.assertEqual(defaults.conflicting_models, ())

    def test_incompatible_edges_to_one_model_are_a_named_exception(self) -> None:
        suggestions = (
            _suggestion(
                "owner_id",
                "x.shared",
                RelatedDataHandling.NEEDS_DECISION,
            ),
            _suggestion(
                "shared_ids",
                "x.shared",
                RelatedDataHandling.NEEDS_DECISION,
                relationship_type="one2many",
            ),
        )

        defaults = propose_relationship_action_defaults(suggestions)
        resolved = propose_relationship_action_defaults(
            suggestions,
            explicit_actions_by_model={
                "x.shared": OdooRelationshipCaptureAction.MATCH_EXISTING,
            },
        )

        self.assertEqual(defaults.actions, {})
        self.assertEqual(defaults.conflicting_models, ("x.shared",))
        self.assertEqual(
            resolved.actions,
            {"x.shared": OdooRelationshipCaptureAction.MATCH_EXISTING},
        )
        self.assertEqual(resolved.conflicting_models, ())

    def test_conflicting_model_edges_remain_unresolved_at_checkpoint(self) -> None:
        suggestions = (
            _suggestion(
                "owner_id",
                "x.shared",
                RelatedDataHandling.NEEDS_DECISION,
            ),
            _suggestion(
                "shared_ids",
                "x.shared",
                RelatedDataHandling.NEEDS_DECISION,
                relationship_type="one2many",
            ),
            _suggestion(
                "message_ids",
                "mail.message",
                RelatedDataHandling.EXCLUDE_HISTORY,
                relationship_type="one2many",
                profiled=True,
            ),
        )
        defaults = propose_relationship_action_defaults(suggestions)

        decisions = relationship_scope_decisions(
            suggestions,
            actions_by_model=defaults.actions,
            unresolved_models=defaults.conflicting_models,
        )
        checkpoint = OdooRelationshipScope.create(
            scope_id=str(uuid4()),
            version=1,
            decisions=decisions,
            root_models=("x.document",),
            recorded_at=datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc),
            recorded_by="Data Manager",
        )
        review = review_relationship_scope(suggestions, checkpoint)

        self.assertEqual(defaults.conflicting_models, ("x.shared",))
        self.assertEqual(
            {(item.source_model, item.field_name) for item in decisions},
            {("x.document", "message_ids")},
        )
        self.assertIs(
            decisions[0].action,
            OdooRelationshipCaptureAction.EXCLUDE_HISTORY,
        )
        self.assertEqual(
            {item.identity for item in review.pending},
            {
                ("x.document", "owner_id"),
                ("x.document", "shared_ids"),
            },
        )

    def test_unresolved_model_rejects_a_simultaneous_choice(self) -> None:
        suggestions = (
            _suggestion(
                "owner_id",
                "x.shared",
                RelatedDataHandling.NEEDS_DECISION,
            ),
        )

        choices = (
            {
                "actions_by_model": {
                    "x.shared": OdooRelationshipCaptureAction.PRESERVE_LINKED,
                },
            },
            {"included_models": {"x.shared"}},
        )
        for choice in choices:
            with self.subTest(choice=choice):
                with self.assertRaisesRegex(
                    OdooRelationshipScopeError,
                    "x.shared cannot be both unresolved and assigned",
                ):
                    relationship_scope_decisions(
                        suggestions,
                        unresolved_models={"x.shared"},
                        **choice,
                    )

    def test_unavailable_conflicted_model_can_remain_unresolved(self) -> None:
        suggestions = (
            _suggestion(
                "owner_id",
                "x.shared",
                RelatedDataHandling.NEEDS_DECISION,
            ),
        )

        decisions = relationship_scope_decisions(
            suggestions,
            actions_by_model={},
            available_models=(),
            unresolved_models={"x.shared"},
        )

        self.assertEqual(decisions, ())
        with self.assertRaisesRegex(
            OdooRelationshipScopeError,
            "x.unknown is not a selectable Odoo relationship",
        ):
            relationship_scope_decisions(
                suggestions,
                actions_by_model={},
                available_models=(),
                unresolved_models={"x.unknown"},
            )

    def test_profile_change_preserves_a_valid_saved_model_policy(self) -> None:
        original = _suggestion(
            "owner_id",
            "x.owner",
            RelatedDataHandling.NEEDS_DECISION,
        )
        scope = OdooRelationshipScope.create(
            scope_id=str(uuid4()),
            version=1,
            decisions=relationship_scope_decisions(
                (original,),
                actions_by_model={
                    "x.owner": OdooRelationshipCaptureAction.MATCH_EXISTING,
                },
            ),
            root_models=("x.document",),
            explicit_actions_by_model=(
                (
                    "x.owner",
                    OdooRelationshipCaptureAction.MATCH_EXISTING,
                ),
            ),
            recorded_at=datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc),
            recorded_by="Data Manager",
        )
        profiled = RelatedDataSuggestion(
            source_model=original.source_model,
            source_label=original.source_label,
            field_name=original.field_name,
            field_label=original.field_label,
            relation_model=original.relation_model,
            relationship_type=original.relationship_type,
            required=original.required,
            handling=RelatedDataHandling.TRANSFER_RELATED,
            recommendation_profile_id="impodo.standard.odoo.relationships",
            recommendation_profile_version=4,
        )

        defaults = propose_relationship_action_defaults(
            (profiled,),
            root_models=scope.root_models,
            current_scope=scope,
        )

        self.assertEqual(
            defaults.actions,
            {"x.owner": OdooRelationshipCaptureAction.MATCH_EXISTING},
        )
        self.assertEqual(defaults.conflicting_models, ())

    def test_legacy_action_is_replaced_by_the_current_safe_default(self) -> None:
        suggestion = _suggestion(
            "owner_id",
            "x.owner",
            RelatedDataHandling.NEEDS_DECISION,
        )
        legacy_decision = OdooRelationshipScopeDecision(
            source_model=suggestion.source_model,
            field_name=suggestion.field_name,
            relation_model=suggestion.relation_model,
            required=suggestion.required,
            handling=suggestion.handling,
            action=OdooRelationshipCaptureAction.MATCH_EXISTING,
        )
        legacy_scope = OdooRelationshipScope(
            scope_id=str(uuid4()),
            version=1,
            decisions=(legacy_decision,),
            root_models=("x.document",),
            recorded_at=datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc),
            recorded_by="Data Manager",
            content_hash="",
            contract_version=3,
            _calculate_content_hash=True,
        )

        defaults = propose_relationship_action_defaults(
            (suggestion,),
            root_models=legacy_scope.root_models,
            current_scope=legacy_scope,
        )

        self.assertEqual(
            defaults.actions,
            {"x.owner": OdooRelationshipCaptureAction.PRESERVE_LINKED},
        )
        self.assertEqual(defaults.conflicting_models, ())

    def test_contract_v5_automatic_action_cannot_become_an_override(self) -> None:
        suggestion = _suggestion(
            "line_ids",
            "x.line",
            RelatedDataHandling.NEEDS_DECISION,
            relationship_type="one2many",
        )
        legacy = OdooRelationshipScope(
            scope_id=str(uuid4()),
            version=3,
            decisions=(
                OdooRelationshipScopeDecision(
                    source_model=suggestion.source_model,
                    field_name=suggestion.field_name,
                    relation_model=suggestion.relation_model,
                    required=suggestion.required,
                    handling=suggestion.handling,
                    action=OdooRelationshipCaptureAction.PRESERVE_LINKED,
                    relationship_type=suggestion.relationship_type,
                ),
            ),
            root_models=("x.document",),
            recorded_at=datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc),
            recorded_by="Data Manager",
            content_hash="",
            contract_version=5,
            _calculate_content_hash=True,
        )

        defaults = propose_relationship_action_defaults(
            (suggestion,),
            root_models=legacy.root_models,
            current_scope=legacy,
        )

        self.assertEqual(
            defaults.actions,
            {"x.line": OdooRelationshipCaptureAction.DO_NOT_CAPTURE},
        )
        self.assertFalse(review_relationship_scope((suggestion,), legacy).complete)

    def test_required_unprofiled_collection_requires_a_choice(self) -> None:
        collection = _suggestion(
            "line_ids",
            "x.line",
            RelatedDataHandling.NEEDS_DECISION,
            required=True,
            relationship_type="one2many",
        )

        defaults = propose_relationship_action_defaults((collection,))
        resolved = propose_relationship_action_defaults(
            (collection,),
            explicit_actions_by_model={
                "x.line": OdooRelationshipCaptureAction.CAPTURE_LINKED,
            },
        )

        self.assertEqual(defaults.actions, {})
        self.assertEqual(defaults.conflicting_models, ("x.line",))
        self.assertEqual(
            resolved.actions,
            {"x.line": OdooRelationshipCaptureAction.CAPTURE_LINKED},
        )

    def test_grouped_choices_expand_to_one_decision_per_field(self) -> None:
        decisions = relationship_scope_decisions(
            (
                _suggestion(
                    "company_id",
                    "res.company",
                    RelatedDataHandling.REUSE_DESTINATION,
                    required=True,
                    profiled=True,
                ),
                _suggestion(
                    "reviewer_id",
                    "x.reviewer",
                    RelatedDataHandling.NEEDS_DECISION,
                ),
                _suggestion(
                    "message_ids",
                    "mail.message",
                    RelatedDataHandling.EXCLUDE_HISTORY,
                    profiled=True,
                ),
            ),
            included_models={"res.company"},
        )

        by_field = {item.field_name: item for item in decisions}
        self.assertIs(
            by_field["company_id"].action,
            OdooRelationshipCaptureAction.CAPTURE_LINKED,
        )
        self.assertIs(
            by_field["reviewer_id"].action,
            OdooRelationshipCaptureAction.DO_NOT_CAPTURE,
        )
        self.assertIs(
            by_field["message_ids"].action,
            OdooRelationshipCaptureAction.EXCLUDE_HISTORY,
        )
        self.assertEqual(
            by_field["company_id"].recommendation_profile_id,
            "impodo.standard.odoo.relationships",
        )

    def test_scope_round_trip_preserves_versioned_edge_evidence(self) -> None:
        decisions = relationship_scope_decisions(
            (
                _suggestion(
                    "company_id",
                    "res.company",
                    RelatedDataHandling.REUSE_DESTINATION,
                    profiled=True,
                ),
            ),
            included_models={"res.company"},
        )
        scope = OdooRelationshipScope.create(
            scope_id=str(uuid4()),
            version=2,
            decisions=decisions,
            root_models=("mrp.bom",),
            recorded_at=datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc),
            recorded_by="Data Manager",
        )

        restored = OdooRelationshipScope.from_json(scope.to_json())

        self.assertEqual(restored, scope)
        self.assertEqual(restored.included_models, frozenset({"res.company"}))
        self.assertEqual(restored.root_models, ("mrp.bom",))

    def test_legacy_scope_without_root_provenance_still_loads(self) -> None:
        legacy = OdooRelationshipScope(
            scope_id=str(uuid4()),
            version=1,
            decisions=(),
            recorded_at=datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc),
            recorded_by="Data Manager",
            content_hash="",
            contract_version=1,
            _calculate_content_hash=True,
        )

        restored = OdooRelationshipScope.from_json(legacy.to_json())

        self.assertEqual(restored, legacy)
        self.assertFalse(restored.root_models)

    def test_match_existing_is_included_but_stops_outgoing_expansion(self) -> None:
        company = _suggestion(
            "company_id",
            "res.company",
            RelatedDataHandling.REUSE_DESTINATION,
        )
        account = RelatedDataSuggestion(
            source_model="res.company",
            source_label="Company",
            field_name="account_id",
            field_label="Account",
            relation_model="account.account",
            relationship_type="many2one",
            required=False,
            handling=RelatedDataHandling.NEEDS_DECISION,
        )
        scope = OdooRelationshipScope.create(
            scope_id=str(uuid4()),
            version=1,
            decisions=relationship_scope_decisions(
                (company,),
                actions_by_model={
                    "res.company": OdooRelationshipCaptureAction.MATCH_EXISTING,
                },
            ),
            recorded_at=datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc),
            recorded_by="Data Manager",
        )

        frontier = relationship_expansion_suggestions((company, account), scope)

        self.assertEqual(frontier, (company,))
        self.assertEqual(scope.included_models, frozenset({"res.company"}))
        self.assertEqual(scope.reference_models, frozenset({"res.company"}))
        self.assertFalse(scope.expanding_models)

    def test_explicit_root_remains_open_when_reached_as_match_existing(self) -> None:
        company = _suggestion(
            "company_id",
            "res.company",
            RelatedDataHandling.REUSE_DESTINATION,
        )
        account = RelatedDataSuggestion(
            source_model="res.company",
            source_label="Company",
            field_name="account_id",
            field_label="Account",
            relation_model="account.account",
            relationship_type="many2one",
            required=False,
            handling=RelatedDataHandling.NEEDS_DECISION,
        )
        scope = OdooRelationshipScope.create(
            scope_id=str(uuid4()),
            version=1,
            decisions=relationship_scope_decisions(
                (company,),
                actions_by_model={
                    "res.company": OdooRelationshipCaptureAction.MATCH_EXISTING,
                },
            ),
            root_models=("res.company",),
            recorded_at=datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc),
            recorded_by="Data Manager",
        )

        frontier = relationship_expansion_suggestions((company, account), scope)

        self.assertEqual(frontier, (company, account))

    def test_match_existing_retains_only_relationship_scoped_identity_edge(self) -> None:
        unit = _suggestion(
            "uom_id",
            "uom.uom",
            RelatedDataHandling.INCLUDE_SUPPORTING,
        )
        category = RelatedDataSuggestion(
            source_model="uom.uom",
            source_label="Unit of Measure",
            field_name="category_id",
            field_label="Unit Category",
            relation_model="uom.category",
            relationship_type="many2one",
            required=False,
            handling=RelatedDataHandling.INCLUDE_SUPPORTING,
            identity_scope=True,
        )
        optional = RelatedDataSuggestion(
            source_model="uom.uom",
            source_label="Unit of Measure",
            field_name="x_owner_id",
            field_label="Owner",
            relation_model="x.owner",
            relationship_type="many2one",
            required=False,
            handling=RelatedDataHandling.NEEDS_DECISION,
        )
        scope = OdooRelationshipScope.create(
            scope_id=str(uuid4()),
            version=1,
            decisions=relationship_scope_decisions(
                (unit,),
                actions_by_model={
                    "uom.uom": OdooRelationshipCaptureAction.MATCH_EXISTING,
                },
            ),
            recorded_at=datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc),
            recorded_by="Data Manager",
        )

        frontier = relationship_expansion_suggestions(
            (unit, category, optional),
            scope,
        )

        self.assertEqual(frontier, (unit, category))

    def test_preserve_linked_is_create_capable_non_expanding_leaf(self) -> None:
        product = _suggestion(
            "product_id",
            "product.product",
            RelatedDataHandling.NEEDS_DECISION,
        )
        category = RelatedDataSuggestion(
            source_model="product.product",
            source_label="Product",
            field_name="categ_id",
            field_label="Product Category",
            relation_model="product.category",
            relationship_type="many2one",
            required=True,
            handling=RelatedDataHandling.NEEDS_DECISION,
        )
        scope = OdooRelationshipScope.create(
            scope_id=str(uuid4()),
            version=1,
            decisions=relationship_scope_decisions(
                (product,),
                actions_by_model={
                    "product.product": (
                        OdooRelationshipCaptureAction.PRESERVE_LINKED
                    ),
                },
            ),
            recorded_at=datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc),
            recorded_by="Data Manager",
        )

        frontier = relationship_expansion_suggestions((product, category), scope)

        self.assertEqual(frontier, (product, category))
        self.assertEqual(scope.included_models, frozenset({"product.product"}))
        self.assertEqual(
            scope.create_if_missing_models,
            frozenset({"product.product"}),
        )
        self.assertEqual(scope.leaf_models, frozenset({"product.product"}))
        self.assertFalse(scope.reference_models)
        self.assertFalse(scope.expanding_models)

    def test_destination_owned_relationship_rejects_preserve_linked(self) -> None:
        with self.assertRaisesRegex(
            OdooRelationshipScopeError,
            "Destination-owned Odoo records cannot use minimum creation",
        ):
            OdooRelationshipScopeDecision(
                source_model="mrp.bom",
                field_name="company_id",
                relation_model="res.company",
                required=True,
                handling=RelatedDataHandling.REUSE_DESTINATION,
                action=OdooRelationshipCaptureAction.PRESERVE_LINKED,
            )

    def test_contract_v2_scope_remains_readable(self) -> None:
        legacy = OdooRelationshipScope(
            scope_id=str(uuid4()),
            version=2,
            decisions=(),
            root_models=("mrp.bom",),
            recorded_at=datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc),
            recorded_by="Data Manager",
            content_hash="",
            contract_version=2,
            _calculate_content_hash=True,
        )

        restored = OdooRelationshipScope.from_json(legacy.to_json())

        self.assertEqual(restored, legacy)

    def test_legacy_contract_rejects_preserve_linked_action(self) -> None:
        decision = OdooRelationshipScopeDecision(
            source_model="x.document",
            field_name="product_id",
            relation_model="product.product",
            required=True,
            handling=RelatedDataHandling.NEEDS_DECISION,
            action=OdooRelationshipCaptureAction.PRESERVE_LINKED,
        )

        with self.assertRaisesRegex(
            OdooRelationshipScopeError,
            "Legacy Odoo relationship scopes cannot preserve linked records",
        ):
            OdooRelationshipScope(
                scope_id=str(uuid4()),
                version=1,
                decisions=(decision,),
                recorded_at=datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc),
                recorded_by="Data Manager",
                content_hash="",
                contract_version=2,
                _calculate_content_hash=True,
            )

    def test_changed_decision_is_rejected_when_hash_was_not_rebuilt(self) -> None:
        scope = OdooRelationshipScope.create(
            scope_id=str(uuid4()),
            version=1,
            decisions=relationship_scope_decisions(
                (
                    _suggestion(
                        "reviewer_id",
                        "x.reviewer",
                        RelatedDataHandling.NEEDS_DECISION,
                    ),
                ),
                included_models=(),
            ),
            recorded_at=datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc),
            recorded_by="Data Manager",
        )
        payload = json.loads(scope.to_json())
        payload["decisions"][0]["action"] = "CAPTURE_LINKED"

        with self.assertRaisesRegex(
            OdooRelationshipScopeError,
            "content hash does not match",
        ):
            OdooRelationshipScope.from_json(json.dumps(payload))

    def test_selectable_edge_rejects_an_automatic_action(self) -> None:
        with self.assertRaisesRegex(
            OdooRelationshipScopeError,
            "Selectable Odoo relationship decision is invalid",
        ):
            OdooRelationshipScopeDecision(
                source_model="x.document",
                field_name="reviewer_id",
                relation_model="x.reviewer",
                required=False,
                handling=RelatedDataHandling.NEEDS_DECISION,
                action=OdooRelationshipCaptureAction.ODOO_MANAGED,
            )

    def test_newly_discovered_selectable_edge_remains_pending(self) -> None:
        first = _suggestion(
            "owner_id",
            "x.owner",
            RelatedDataHandling.NEEDS_DECISION,
        )
        scope = OdooRelationshipScope.create(
            scope_id=str(uuid4()),
            version=1,
            decisions=relationship_scope_decisions(
                (first,),
                actions_by_model={
                    "x.owner": OdooRelationshipCaptureAction.CAPTURE_LINKED,
                },
            ),
            explicit_actions_by_model=(
                ("x.owner", OdooRelationshipCaptureAction.CAPTURE_LINKED),
            ),
            recorded_at=datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc),
            recorded_by="Data Manager",
        )
        second = RelatedDataSuggestion(
            source_model="x.owner",
            source_label="Owner",
            field_name="calendar_id",
            field_label="Calendar",
            relation_model="resource.calendar",
            relationship_type="many2one",
            required=False,
            handling=RelatedDataHandling.NEEDS_DECISION,
        )

        review = review_relationship_scope((first, second), scope)

        self.assertFalse(review.complete)
        self.assertEqual(
            review.reviewed_identities,
            frozenset({("x.document", "owner_id")}),
        )
        self.assertEqual(review.pending, (second,))

    def test_saved_exclusion_completes_selectable_edge_review(self) -> None:
        suggestion = _suggestion(
            "reviewer_id",
            "x.reviewer",
            RelatedDataHandling.NEEDS_DECISION,
        )

        scope = OdooRelationshipScope.create(
            scope_id=str(uuid4()),
            version=1,
            decisions=relationship_scope_decisions(
                (suggestion,),
                actions_by_model={
                    "x.reviewer": OdooRelationshipCaptureAction.DO_NOT_CAPTURE,
                },
            ),
            explicit_actions_by_model=(
                ("x.reviewer", OdooRelationshipCaptureAction.DO_NOT_CAPTURE),
            ),
            recorded_at=datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc),
            recorded_by="Data Manager",
        )

        review = review_relationship_scope((suggestion,), scope)

        self.assertTrue(review.complete)
        self.assertFalse(review.pending)

    def test_changed_profile_provenance_requires_fresh_review(self) -> None:
        original = _suggestion(
            "company_id",
            "res.company",
            RelatedDataHandling.REUSE_DESTINATION,
            profiled=True,
        )
        scope = _scope((original,), included_models={"res.company"})
        changed = RelatedDataSuggestion(
            source_model=original.source_model,
            source_label=original.source_label,
            field_name=original.field_name,
            field_label=original.field_label,
            relation_model=original.relation_model,
            relationship_type=original.relationship_type,
            required=original.required,
            handling=original.handling,
            recommendation_profile_id=original.recommendation_profile_id,
            recommendation_profile_version=2,
        )

        review = review_relationship_scope((changed,), scope)

        self.assertFalse(review.complete)
        self.assertEqual(review.pending, (changed,))

    def test_unavailable_selectable_edge_is_a_named_blocker(self) -> None:
        suggestion = _suggestion(
            "reviewer_id",
            "x.reviewer",
            RelatedDataHandling.NEEDS_DECISION,
        )

        review = review_relationship_scope(
            (suggestion,),
            None,
            available_models={"x.document"},
        )

        self.assertFalse(review.complete)
        self.assertFalse(review.pending)
        self.assertEqual(review.unavailable, (suggestion,))

    def test_saving_available_choices_does_not_exclude_unavailable_edge(
        self,
    ) -> None:
        owner = _suggestion(
            "owner_id",
            "x.owner",
            RelatedDataHandling.NEEDS_DECISION,
        )
        reviewer = _suggestion(
            "reviewer_id",
            "x.reviewer",
            RelatedDataHandling.NEEDS_DECISION,
        )

        decisions = relationship_scope_decisions(
            (owner, reviewer),
            included_models={"x.owner"},
            available_models={"x.owner"},
        )
        scope = OdooRelationshipScope.create(
            scope_id=str(uuid4()),
            version=1,
            decisions=decisions,
            recorded_at=datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc),
            recorded_by="Data Manager",
        )
        review = review_relationship_scope(
            (owner, reviewer),
            scope,
            available_models={"x.owner"},
        )

        self.assertEqual(
            {item.identity for item in decisions},
            {("x.document", "owner_id")},
        )
        self.assertEqual(review.unavailable, (reviewer,))

    def test_saving_other_choices_preserves_reviewed_unavailable_edge(
        self,
    ) -> None:
        owner = _suggestion(
            "owner_id",
            "x.owner",
            RelatedDataHandling.NEEDS_DECISION,
        )
        reviewer = _suggestion(
            "reviewer_id",
            "x.reviewer",
            RelatedDataHandling.NEEDS_DECISION,
        )
        current_scope = _scope(
            (owner, reviewer),
            included_models={"x.reviewer"},
        )

        decisions = relationship_scope_decisions(
            (owner, reviewer),
            included_models={"x.owner"},
            available_models={"x.owner"},
            current_scope=current_scope,
        )

        self.assertEqual(
            {item.field_name: item.action for item in decisions},
            {
                "owner_id": OdooRelationshipCaptureAction.CAPTURE_LINKED,
                "reviewer_id": OdooRelationshipCaptureAction.CAPTURE_LINKED,
            },
        )

    def test_automatic_edge_does_not_require_user_review(self) -> None:
        suggestion = _suggestion(
            "message_ids",
            "mail.message",
            RelatedDataHandling.EXCLUDE_HISTORY,
            profiled=True,
        )

        self.assertTrue(
            review_relationship_scope((suggestion,), None).complete
        )

    def test_contract_v3_scope_is_readable_but_requires_fresh_review(self) -> None:
        suggestion = _suggestion(
            "owner_id",
            "x.owner",
            RelatedDataHandling.NEEDS_DECISION,
        )
        legacy_decision = OdooRelationshipScopeDecision(
            source_model=suggestion.source_model,
            field_name=suggestion.field_name,
            relation_model=suggestion.relation_model,
            required=suggestion.required,
            handling=suggestion.handling,
            action=OdooRelationshipCaptureAction.PRESERVE_LINKED,
        )
        legacy = OdooRelationshipScope(
            scope_id=str(uuid4()),
            version=1,
            decisions=(legacy_decision,),
            root_models=("x.document",),
            recorded_at=datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc),
            recorded_by="Data Manager",
            content_hash="",
            contract_version=3,
            _calculate_content_hash=True,
        )

        restored = OdooRelationshipScope.from_json(legacy.to_json())
        review = review_relationship_scope((suggestion,), restored)

        self.assertEqual(restored, legacy)
        self.assertIsNone(restored.decisions[0].relationship_type)
        self.assertFalse(review.complete)
        self.assertEqual(review.pending, (suggestion,))

    def test_preserve_leaf_retains_profiled_owned_children_only(self) -> None:
        calendar = _suggestion(
            "calendar_id",
            "resource.calendar",
            RelatedDataHandling.NEEDS_DECISION,
        )
        attendance = RelatedDataSuggestion(
            source_model="resource.calendar",
            source_label="Working Hours",
            field_name="attendance_ids",
            field_label="Working Time",
            relation_model="resource.calendar.attendance",
            relationship_type="one2many",
            required=False,
            handling=RelatedDataHandling.INCLUDE_SUPPORTING,
            recommendation_profile_id="impodo.standard.odoo.relationships",
            recommendation_profile_version=4,
        )
        time_off = RelatedDataSuggestion(
            source_model="resource.calendar",
            source_label="Working Hours",
            field_name="leave_ids",
            field_label="Time Off",
            relation_model="resource.calendar.leaves",
            relationship_type="one2many",
            required=False,
            handling=RelatedDataHandling.NEEDS_DECISION,
        )
        scope = OdooRelationshipScope.create(
            scope_id=str(uuid4()),
            version=1,
            decisions=relationship_scope_decisions(
                (calendar,),
                actions_by_model={
                    "resource.calendar": (
                        OdooRelationshipCaptureAction.PRESERVE_LINKED
                    ),
                },
            ),
            recorded_at=datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc),
            recorded_by="Data Manager",
        )

        frontier = relationship_expansion_suggestions(
            (calendar, attendance, time_off),
            scope,
        )

        self.assertEqual(frontier, (calendar, attendance))


def _suggestion(
    field_name: str,
    relation_model: str,
    handling: RelatedDataHandling,
    *,
    required: bool = False,
    profiled: bool = False,
    relationship_type: str = "many2one",
) -> RelatedDataSuggestion:
    return RelatedDataSuggestion(
        source_model="x.document",
        source_label="Document",
        field_name=field_name,
        field_label=field_name.replace("_", " ").title(),
        relation_model=relation_model,
        relationship_type=relationship_type,
        required=required,
        handling=handling,
        recommendation_profile_id=(
            "impodo.standard.odoo.relationships" if profiled else None
        ),
        recommendation_profile_version=1 if profiled else None,
    )


def _scope(
    suggestions: tuple[RelatedDataSuggestion, ...],
    *,
    included_models: set[str],
) -> OdooRelationshipScope:
    return OdooRelationshipScope.create(
        scope_id=str(uuid4()),
        version=1,
        decisions=relationship_scope_decisions(
            suggestions,
            included_models=included_models,
        ),
        recorded_at=datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc),
        recorded_by="Data Manager",
    )


if __name__ == "__main__":
    unittest.main()
