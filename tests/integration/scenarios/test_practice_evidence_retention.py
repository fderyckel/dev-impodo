"""Close practice workflows and report incomplete private evidence on failure."""

from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase, main
from unittest.mock import Mock, patch

from scripts import qualify_practice_customers as retention


class PracticeEvidenceRetentionTests(TestCase):
    def fixture(self):
        return SimpleNamespace(
            client=SimpleNamespace(close=Mock()),
            context=SimpleNamespace(load_jobs=SimpleNamespace(shutdown=Mock())),
            secrets=SimpleNamespace(values={"fictional-role": "fictional-key"}),
            temporary=SimpleNamespace(name="/unused/private/source"),
            tearDown=Mock(),
            close=Mock(),
        )

    def test_controlled_vault_failure_still_closes_and_tears_down(self):
        fixture, report = self.fixture(), {"private_evidence_retained": True}
        with (
            patch.object(
                retention,
                "_write_private",
                side_effect=OSError("private vault unavailable"),
            ),
            patch.object(retention.shutil, "copytree") as copy,
            self.assertRaisesRegex(
                retention.PracticeQualificationError,
                "PRIVATE_EVIDENCE_RETENTION_FAILED",
            ),
        ):
            retention._close_and_record_retention(
                fixture, Path("/private/evidence"), controlled=True, report=report
            )
        fixture.client.close.assert_called_once()
        fixture.context.load_jobs.shutdown.assert_called_once()
        fixture.tearDown.assert_called_once()
        copy.assert_not_called()
        self.assertFalse(report["private_evidence_retained"])

    def test_controlled_copy_failure_still_tears_down_and_reports_gap(self):
        fixture, report = self.fixture(), {}
        with (
            patch.object(retention, "_write_private"),
            patch.object(
                retention.shutil, "copytree", side_effect=OSError("copy interrupted")
            ),
            self.assertRaises(retention.PracticeQualificationError),
        ):
            retention._close_and_record_retention(
                fixture, Path("/private/evidence"), controlled=True, report=report
            )
        fixture.client.close.assert_called_once()
        fixture.tearDown.assert_called_once()
        self.assertFalse(report["private_evidence_retained"])
        self.assertEqual(
            report["private_evidence_failure_code"], "PRIVATE_EVIDENCE_RETENTION_FAILED"
        )

    def test_live_vault_failure_always_closes_without_success_claim(self):
        fixture, report = self.fixture(), {}
        with (
            patch.object(
                retention, "_write_private", side_effect=OSError("vault unavailable")
            ),
            self.assertRaises(retention.PracticeQualificationError),
        ):
            retention._close_and_record_retention(
                fixture, Path("/private/evidence"), controlled=False, report=report
            )
        fixture.close.assert_called_once()
        fixture.tearDown.assert_not_called()
        self.assertFalse(report["private_evidence_retained"])

    def test_success_is_reported_only_after_teardown_finishes(self):
        fixture, report = self.fixture(), {}
        fixture.tearDown.side_effect = lambda: self.assertFalse(
            report["private_evidence_retained"]
        )
        with (
            patch.object(retention, "_write_private"),
            patch.object(retention.shutil, "copytree"),
        ):
            retention._close_and_record_retention(
                fixture, Path("/private/evidence"), controlled=True, report=report
            )
        fixture.tearDown.assert_called_once()
        self.assertTrue(report["private_evidence_retained"])
        self.assertNotIn("private_evidence_failure_code", report)

    def test_teardown_failure_cannot_report_complete_retention(self):
        fixture, report = self.fixture(), {}
        fixture.tearDown.side_effect = RuntimeError("close failed")
        with (
            patch.object(retention, "_write_private"),
            patch.object(retention.shutil, "copytree"),
            self.assertRaises(retention.PracticeQualificationError),
        ):
            retention._close_and_record_retention(
                fixture, Path("/private/evidence"), controlled=True, report=report
            )
        self.assertFalse(report["private_evidence_retained"])


if __name__ == "__main__":
    main()
