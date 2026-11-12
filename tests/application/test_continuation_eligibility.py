"""Reject origins whose rules the initial continuation cannot retain."""

from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock

from impodo.application.correction_orchestration import CorrectionMappingSeedService
from impodo.domain.correction_origin import CorrectionOriginError
from impodo.domain.preparation.quality import QualityRuleSource
from impodo.domain.shared.access import LOCAL_ACTOR


class ContinuationEligibilityTests(TestCase):
    def service(self, *, derived=False, source=QualityRuleSource.MAPPING_DERIVED):
        mappings = SimpleNamespace(
            authorization=Mock(),
            mappings=SimpleNamespace(
                get_mapping_revision=lambda _: SimpleNamespace(
                    definition=SimpleNamespace(
                        datasets=(
                            SimpleNamespace(
                                dataset_id="derived" if derived else "physical"
                            ),
                        )
                    )
                )
            ),
            sources=SimpleNamespace(
                get_source_selection=lambda _: SimpleNamespace(
                    datasets=(SimpleNamespace(dataset_id="physical"),)
                )
            ),
        )
        quality = SimpleNamespace(
            get_current_quality_ruleset=lambda _: SimpleNamespace(
                rules=(SimpleNamespace(source=source),)
            )
        )
        return CorrectionMappingSeedService(
            schemas=Mock(), mappings=mappings, quality=quality
        )

    def test_standard_direct_rules_can_continue(self):
        self.service().validate_origin("saved", actor=LOCAL_ACTOR)

    def test_generated_tables_are_not_silently_dropped(self):
        with self.assertRaisesRegex(CorrectionOriginError, "combined or generated"):
            self.service(derived=True).validate_origin("saved", actor=LOCAL_ACTOR)

    def test_extra_checks_are_not_silently_dropped(self):
        for source in (
            QualityRuleSource.MANAGER_AUTHORED,
            QualityRuleSource.SCOPE_APPROVED,
        ):
            with (
                self.subTest(source=source),
                self.assertRaisesRegex(
                    CorrectionOriginError, "additional business checks"
                ),
            ):
                self.service(source=source).validate_origin("saved", actor=LOCAL_ACTOR)
