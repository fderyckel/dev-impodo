"""Keep the opt-in live Recipe canary bounded and recover lost create replies."""

import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from impodo.adapters.odoo.connectors import Json2Config
from impodo.domain.preparation.staging_contracts import StagingDisposition
from scripts import qualify_row_inclusion_recipe as qualification


class RowRecipeQualificationSafetyTests(unittest.TestCase):
    def canonical(self, *, excluded=False):
        return SimpleNamespace(
            content_hash="sha256:" + "a" * 64,
            rows=tuple(
                SimpleNamespace(
                    disposition=StagingDisposition.CANDIDATE,
                    target_identity=(f"test-F{number}",),
                    proposed_values={"name": f"Fictional {number}"},
                )
                for number in ((0, 1) if excluded else (1, 2))
            ),
        )

    def test_write_flag_is_required_before_opening_a_key_file(self):
        key_file = Mock()
        with self.assertRaisesRegex(RuntimeError, "allow-write"):
            qualification.qualify(
                SimpleNamespace(allow_write=False, api_key_file=key_file)
            )
        key_file.read_text.assert_not_called()

    def test_excluded_row_is_rejected_before_any_remote_access(self):
        with patch.object(qualification, "_target_json2_config") as configuration:
            with self.assertRaisesRegex(RuntimeError, "excluded Contact"):
                qualification._canary(
                    object(), "fictional-key", self.canonical(excluded=True), "test"
                )
        configuration.assert_not_called()

    def test_lost_create_reply_is_not_retried_and_its_exact_records_are_removed(self):
        present = {}
        writer = Mock()
        writer.find_ids.side_effect = lambda model, domain: (
            (present[domain[0][2]],) if domain[0][2] in present else ()
        )

        def create(model, values):
            present.update(
                {row["ref"]: index for index, row in enumerate(values, start=101)}
            )
            raise RuntimeError("Simulated lost response")

        def transport(url, headers, body, timeout, method):
            self.assertTrue(url.endswith("/json/2/res.partner/unlink"))
            self.assertEqual(method, "POST")
            self.assertEqual(set(json.loads(body)["ids"]), {101, 102})
            present.clear()
            return 200, True

        writer.create_rows.side_effect = create
        writer.transport.side_effect = transport
        config = Json2Config(
            "https://fictional.example.test", "fictional", "fictional-key"
        )
        with (
            patch.object(qualification, "_target_json2_config", return_value=config),
            patch.object(qualification, "Json2WriteExecutor", return_value=writer),
            patch.object(qualification, "Json2ReadConnector"),
        ):
            with self.assertRaisesRegex(RuntimeError, "lost response"):
                qualification._canary(
                    object(), "fictional-key", self.canonical(), "test"
                )
        writer.create_rows.assert_called_once()
        writer.transport.assert_called_once()
        self.assertEqual(present, {})


if __name__ == "__main__":
    unittest.main()
