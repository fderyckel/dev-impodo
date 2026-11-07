"""Guard manual practice setup before descriptor or credential access."""

from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase, main
from unittest.mock import Mock

from scripts.prepare_practice_target import prepare


class PreparePracticeTargetAuthorizationTests(TestCase):
    def options(self, root, *, allow_write=False):
        return SimpleNamespace(
            target_descriptor=root / "must-not-be-read.json",
            output_directory=root / "new-result",
            expected_major=19,
            mode="LOCAL",
            allow_write=allow_write,
        )

    def test_missing_authorization_never_reads_credentials_or_constructs_client(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            reader = Mock(side_effect=AssertionError("Credential boundary reached"))
            factory = Mock(side_effect=AssertionError("Seed boundary reached"))
            report = prepare(
                self.options(root),
                connection_reader=reader,
                seed_factory=factory,
                corpus_root=root / "missing-public-corpus",
            )
            self.assertEqual(report["status"], "FAILED")
            self.assertEqual(report["failure_code"], "ALLOW_WRITE_REQUIRED")
            self.assertEqual(report["phase"], "authorization")
            self.assertEqual(report["workflow_runs_started"], 0)
            reader.assert_not_called()
            factory.assert_not_called()
            self.assertTrue((root / "new-result/result.json").is_file())

    def test_descriptor_mode_mismatch_never_constructs_seed_client(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            reader = Mock(return_value=SimpleNamespace(mode="REMOTE"))
            factory = Mock(side_effect=AssertionError("Seed boundary reached"))
            report = prepare(
                self.options(root, allow_write=True),
                connection_reader=reader,
                seed_factory=factory,
                corpus_root=root / "missing-public-corpus",
            )
            self.assertEqual(report["failure_code"], "DESCRIPTOR_MODE_DIFFERED")
            factory.assert_not_called()

    def test_existing_result_directory_is_preserved_before_credential_reads(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            options = self.options(root, allow_write=True)
            options.output_directory.mkdir()
            sentinel = options.output_directory / "keep.txt"
            sentinel.write_text("previous evidence")
            reader, factory = Mock(), Mock()
            with self.assertRaises(FileExistsError):
                prepare(options, connection_reader=reader, seed_factory=factory)
            self.assertEqual(sentinel.read_text(), "previous evidence")
            reader.assert_not_called()
            factory.assert_not_called()


if __name__ == "__main__":
    main()
