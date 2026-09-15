"""Exercise recognition separately from enabled operations and paired majors."""

import unittest

from impodo.domain.odoo.compatibility import (
    OdooOperation,
    OdooReleaseStage,
    OdooVersionProblem,
    assess_odoo_operation,
    recognize_odoo_version,
    same_odoo_major,
)


class OdooCompatibilityTests(unittest.TestCase):
    def test_recognizes_versions_without_enabling_new_majors_or_series(self):
        for raw, major, series, stage, saas in (
            ("19.0", 19, "19.0", OdooReleaseStage.FINAL, False),
            ("19.4", 19, "19.4", OdooReleaseStage.FINAL, False),
            ("19.0+e", 19, "19.0", OdooReleaseStage.FINAL, False),
            ("19.0-20260908", 19, "19.0", OdooReleaseStage.FINAL, False),
            ("19.5a1", 19, "19.5", OdooReleaseStage.ALPHA, False),
            ("20.0b2", 20, "20.0", OdooReleaseStage.BETA, False),
            ("20.0rc1", 20, "20.0", OdooReleaseStage.CANDIDATE, False),
            ("20.0", 20, "20.0", OdooReleaseStage.FINAL, False),
            ("21.0", 21, "21.0", OdooReleaseStage.FINAL, False),
            ("saas~19.4", 19, "saas~19.4", OdooReleaseStage.FINAL, True),
            ("saas-20.1", 20, "saas-20.1", OdooReleaseStage.FINAL, True),
        ):
            with self.subTest(raw=raw):
                actual = recognize_odoo_version(raw)
                self.assertEqual((actual.raw, actual.major, actual.series, actual.release_stage, actual.is_saas),
                                 (raw, major, series, stage, saas))
                self.assertIsNone(actual.problem)

    def test_preserves_valid_legacy_19_acceptance_for_each_operation(self):
        for raw in ("19.0", "19.4", "19.0+e", "19.0-20260908", "19.5a1"):
            for operation in OdooOperation:
                with self.subTest(raw=raw, operation=operation):
                    decision = assess_odoo_operation(raw, operation)
                    self.assertTrue(decision.allowed)
                    self.assertEqual(decision.reason, "ODOO_19_LEGACY_ACCEPTED")

    def test_disabled_majors_and_saas_cannot_enable_any_operation(self):
        for raw in ("18.0", "20.0", "20.0rc1", "21.0", "saas~19.4", "saas-20.1"):
            for operation in OdooOperation:
                with self.subTest(raw=raw, operation=operation):
                    self.assertFalse(assess_odoo_operation(raw, operation).allowed)

    def test_missing_and_malformed_strings_never_establish_a_major(self):
        for raw in (None, "", "unknown", 19, True, [], {}, "19", "19.", "19.garbage",
                    "19.0garbage", " 19.0", "19.0\n", "Odoo 19.0", "9" * 5000 + ".0"):
            with self.subTest(raw=str(raw)[:30]):
                actual = recognize_odoo_version(raw)
                self.assertIsNotNone(actual.problem)
                self.assertIsNone(actual.major)
                self.assertFalse(assess_odoo_operation(actual, OdooOperation.WRITE).allowed)

    def test_structured_evidence_matches_upstream_release_format(self):
        for raw, info in (
            ("19.0", [19, 0, 0, "final", 0, ""]),
            ("19.4", (19, 4, 0, "final", 0)),
            ("19.0+e", [19, 0, 0, "final", 0, "+e"]),
            ("19.0+e", [19, 0, 0, "final", 0, "e"]),
            ("19.5a1", [19, 5, 0, "alpha", 1, ""]),
            ("20.0rc1", [20, 0, 0, "candidate", 1, ""]),
            ("saas~19.4", ["saas~19", 4, 0, "final", 0, ""]),
        ):
            with self.subTest(raw=raw):
                self.assertEqual(recognize_odoo_version(raw, version_info=info), recognize_odoo_version(raw))

    def test_structured_conflicts_fail_closed_instead_of_trusting_the_prefix(self):
        for info in ([20, 0, 0, "final", 0, ""], [19, 4, 0, "final", 0, ""],
                     [19, 0, 0, "alpha", 1, ""], [19, 0, 0, "final", 0, "+e"],
                     [19, 0, 0, "final", 0, "e"]):
            with self.subTest(info=info):
                version = recognize_odoo_version("19.0", version_info=info)
                self.assertEqual(version.problem, OdooVersionProblem.CONFLICT)
                self.assertEqual(version.raw, "19.0")
                self.assertIsNone(version.major)
                self.assertFalse(assess_odoo_operation(version, OdooOperation.WRITE).allowed)

    def test_present_but_malformed_structured_evidence_cannot_fall_back(self):
        for info in (None, {}, "19.0", [], [19, 0], [True, 0, 0, "final", 0],
                     ["19", 0, 0, "final", 0], [19, -1, 0, "final", 0],
                     [19, 0, 0, "stable", 0], [19, 0, 0, {}, 0],
                     [19, 0, 0, "final", 1], [19, 0, 0, "final", 0, "garbage"]):
            with self.subTest(info=info):
                version = recognize_odoo_version("19.0", version_info=info)
                self.assertEqual(version.problem, OdooVersionProblem.MALFORMED)
                self.assertFalse(assess_odoo_operation(version, OdooOperation.CONNECT).allowed)

    def test_structured_evidence_does_not_invent_a_missing_reported_version(self):
        version = recognize_odoo_version("unknown", version_info=[19, 0, 0, "final", 0])
        self.assertEqual(version.problem, OdooVersionProblem.UNKNOWN)
        self.assertIsNone(version.major)

    def test_pair_equality_requires_known_majors_and_is_not_support_authority(self):
        for source, target, expected in (
            ("19.0", "19.4", True), ("20.0", "20.0", True),
            ("19.0", "20.0", False), ("20.0", "19.0", False),
            ("unknown", "unknown", False), ("19.garbage", "19.0", False),
        ):
            with self.subTest(source=source, target=target):
                self.assertEqual(same_odoo_major(source, target), expected)
        self.assertFalse(assess_odoo_operation("20.0", OdooOperation.WRITE).allowed)


if __name__ == "__main__":
    unittest.main()
