"""Keep the opt-in live transfer rehearsal scoped and interruption-aware."""

import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from impodo.adapters.odoo.connectors import Json2Config
from impodo.domain.odoo.contracts import ConnectorTransportError
from impodo.domain.shared.models import TargetRecord
from scripts import qualify_row_inclusion_transfer as qualification


class RowTransferQualificationSafetyTests(unittest.TestCase):
    def test_write_flag_is_checked_before_any_credential_is_read(self):
        key_file = Mock()
        with self.assertRaisesRegex(RuntimeError, "allow-write"):
            qualification.qualify(
                SimpleNamespace(allow_write=False, local_api_key_file=key_file)
            )
        key_file.read_text.assert_not_called()

    def test_interruption_occurs_after_one_real_accepted_load_without_retry(self):
        transport = Mock(return_value=(200, {"ids": [71], "messages": []}))
        executor = SimpleNamespace(transport=transport)
        context = SimpleNamespace(write_executor_factory=Mock(return_value=executor))
        counts = {"accepted_batches": 0, "accepted_rows": 0, "interrupted": False}
        qualification.observe_loads(context, counts, interrupt=True)
        writer = context.write_executor_factory("target", "fictional-key", "scope")
        with self.assertRaises(qualification.InjectedInterruption):
            writer.transport(
                "https://fictional.example/json/2/res.partner/load",
                {},
                b"{}",
                5,
                "POST",
            )
        transport.assert_called_once()
        self.assertEqual(
            counts, {"accepted_batches": 1, "accepted_rows": 1, "interrupted": True}
        )

    def test_read_requests_and_rejected_loads_are_never_marked_accepted(self):
        for url, result in (
            (
                "https://fictional.example/json/2/res.partner/search_read",
                (200, [{"id": 71}]),
            ),
            (
                "https://fictional.example/json/2/res.partner/load",
                (200, {"ids": False, "messages": []}),
            ),
            ("https://fictional.example/json/2/res.partner/load", (403, {"ids": [71]})),
        ):
            with self.subTest(url=url, status=result[0]):
                executor = SimpleNamespace(transport=Mock(return_value=result))
                context = SimpleNamespace(
                    write_executor_factory=Mock(return_value=executor)
                )
                counts = {
                    "accepted_batches": 0,
                    "accepted_rows": 0,
                    "interrupted": False,
                }
                qualification.observe_loads(context, counts, interrupt=True)
                self.assertEqual(
                    context.write_executor_factory().transport(
                        url, {}, b"{}", 5, "POST"
                    ),
                    result,
                )
                self.assertEqual(
                    counts,
                    {"accepted_batches": 0, "accepted_rows": 0, "interrupted": False},
                )

    def test_cleanup_rejects_an_unrelated_record_before_constructing_a_writer(self):
        connection = SimpleNamespace(
            records=lambda refs: (SimpleNamespace(values={"ref": "unrelated"}),)
        )
        with (
            patch.object(qualification, "Json2WriteExecutor") as writer,
            self.assertRaisesRegex(RuntimeError, "namespace"),
        ):
            qualification.cleanup(connection, ("own-exact-reference",))
        writer.assert_not_called()

    def test_cleanup_uses_exact_target_ids_and_verifies_absence(self):
        config = Json2Config("https://fictional.example", "fictional", "fictional-key")
        connection = SimpleNamespace(
            config=lambda: config,
            records=Mock(
                side_effect=[
                    (TargetRecord("res.partner", 71, {"ref": "own-reference"}),),
                    (),
                ]
            ),
        )
        writer = Mock()
        writer.transport.return_value = 200, True
        with patch.object(qualification, "Json2WriteExecutor", return_value=writer):
            qualification.cleanup(connection, ("own-reference",))
        body = writer.transport.call_args.args[2]
        self.assertEqual(json.loads(body)["ids"], [71])
        self.assertEqual(connection.records.call_count, 2)

    def test_cleanup_rechecks_absence_after_a_lost_unlink_reply(self):
        config = Json2Config("https://fictional.example", "fictional", "fictional-key")
        connection = SimpleNamespace(
            config=lambda: config,
            records=Mock(
                side_effect=[
                    (TargetRecord("res.partner", 71, {"ref": "own-reference"}),),
                    (),
                    (),
                ]
            ),
        )
        writer = Mock()
        writer.transport.side_effect = ConnectorTransportError(
            "Lost synthetic unlink reply"
        )
        with patch.object(qualification, "Json2WriteExecutor", return_value=writer):
            qualification.cleanup(connection, ("own-reference",))
        writer.transport.assert_called_once()
        self.assertEqual(connection.records.call_count, 3)
