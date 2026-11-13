"""A frozen database binding fences every writer and read-back operation."""

import unittest
from copy import deepcopy
from functools import partial
from unittest.mock import Mock, patch

from impodo.application.odoo_generation_fence import (
    GenerationFencedOdooReadbackReader,
    GenerationFencedOdooWriteExecutor,
)
from impodo.application.workspace.execution.service import execution_api_scope
from impodo.domain.execution.models import ExecutionRowStatus, ExecutionRunStatus
from impodo.domain.execution.odoo_readback import (
    OdooReadbackError,
    OdooReadbackReader,
    ReadbackLookup,
)
from impodo.domain.execution.odoo_write import (
    OdooWriteExecutor,
    OdooWriteOutcomeUnknown,
    OdooWriteRejected,
)
from impodo.domain.execution.write_diagnostics import (
    OdooWriteDiagnostic,
    WriteFailureCode,
)
from impodo.domain.shared.access import LOCAL_ACTOR
from tests.application.workspace.execution import test_service as execution_fixtures

_WRITES = {
    "create_rows": ("res.partner", ({"name": "Reviewed"},)),
    "load_create_rows": ("res.partner", ({"name": "Reviewed"},), ("impodo.row",)),
    "update_row": ("res.partner", 42, {"name": "Reviewed"}),
    "update_rows": ("res.partner", (42, 43), {"name": "Reviewed"}),
}
_WRITER_READS = {
    "find_ids": ("res.partner", (("ref", "=", "reviewed"),)),
    "find_ids_many": ("res.partner", ((("ref", "=", "reviewed"),),)),
    "read_projected_ids": ("res.partner", (42,), "parent_id", "res.partner"),
}
_READBACK = {
    "read_ids": ("res.partner", (42,), ("name",)),
    "find_records": ("res.partner", (("ref", "=", "reviewed"),), ("name",)),
    "find_records_many": (
        "res.partner",
        (ReadbackLookup((("ref", "=", "reviewed"),), ("name",)),),
    ),
    "read_external_ids": (("impodo.row",),),
}


class FrozenBindingAssertion:
    """Model a guard comparing the current record to its frozen exact hash."""

    def __init__(self):
        self.frozen_hash = "workspace-role-generation-one"
        self.current_hash = self.frozen_hash
        self.checks = 0

    def __call__(self):
        self.checks += 1
        if self.current_hash != self.frozen_hash:
            raise ValueError("private binding details")


class OdooGenerationFenceTests(unittest.TestCase):
    def test_every_method_rechecks_before_and_after_and_preserves_results(self):
        for protocol, wrapper, methods in (
            (
                OdooWriteExecutor,
                GenerationFencedOdooWriteExecutor,
                _WRITES | _WRITER_READS,
            ),
            (OdooReadbackReader, GenerationFencedOdooReadbackReader, _READBACK),
        ):
            for name, arguments in methods.items():
                with self.subTest(wrapper=wrapper.__name__, method=name):
                    guard = FrozenBindingAssertion()
                    delegate = Mock(spec=protocol)
                    receipt = (
                        None if name in {"update_row", "update_rows"} else object()
                    )
                    getattr(delegate, name).return_value = receipt
                    fenced = wrapper(delegate, guard)
                    self.assertIs(getattr(fenced, name)(*arguments), receipt)
                    getattr(delegate, name).assert_called_once_with(*arguments)
                    self.assertEqual(guard.checks, 2)

    def test_missing_or_stale_binding_rejects_every_method_without_remote_io(self):
        for protocol, wrapper, methods, error_type in (
            (
                OdooWriteExecutor,
                GenerationFencedOdooWriteExecutor,
                _WRITES | _WRITER_READS,
                OdooWriteRejected,
            ),
            (
                OdooReadbackReader,
                GenerationFencedOdooReadbackReader,
                _READBACK,
                OdooReadbackError,
            ),
        ):
            for reason in (
                ValueError("missing binding"),
                PermissionError("access denied"),
                RuntimeError("store unavailable"),
            ):
                for name, arguments in methods.items():
                    with self.subTest(
                        wrapper=wrapper.__name__, reason=type(reason), method=name
                    ):
                        delegate = Mock(spec=protocol)
                        guard = Mock(side_effect=reason)
                        with self.assertRaises(error_type) as raised:
                            getattr(wrapper(delegate, guard), name)(*arguments)
                        self.assertEqual(delegate.mock_calls, [])
                        guard.assert_called_once_with()
                        self.assertNotIn(str(reason), str(raised.exception))

    def test_reset_after_every_accepted_write_is_unknown_and_never_retried(self):
        for name, arguments in _WRITES.items():
            with self.subTest(method=name):
                guard = FrozenBindingAssertion()
                delegate = Mock(spec=OdooWriteExecutor)
                journal = {"row": {"status": "IN_FLIGHT", "attempt": 1}}
                before = deepcopy(journal)

                def accepted(*_args, guard=guard):
                    guard.current_hash = "workspace-role-generation-two"
                    return (42,)

                getattr(delegate, name).side_effect = accepted
                fenced = GenerationFencedOdooWriteExecutor(delegate, guard)
                with self.assertRaises(OdooWriteOutcomeUnknown) as raised:
                    getattr(fenced, name)(*arguments)
                self.assertIn("do not retry", str(raised.exception))
                self.assertNotIn("private binding details", str(raised.exception))
                self.assertEqual(getattr(delegate, name).call_count, 1)
                self.assertEqual(journal, before)
                self.assertEqual(guard.checks, 2)
                with self.assertRaises(OdooWriteRejected):
                    getattr(fenced, name)(*arguments)
                self.assertEqual(getattr(delegate, name).call_count, 1)
                self.assertEqual(guard.frozen_hash, "workspace-role-generation-one")

    def test_reset_during_every_read_discards_results_and_cannot_verify(self):
        for protocol, wrapper, methods, error_type in (
            (
                OdooWriteExecutor,
                GenerationFencedOdooWriteExecutor,
                _WRITER_READS,
                OdooWriteRejected,
            ),
            (
                OdooReadbackReader,
                GenerationFencedOdooReadbackReader,
                _READBACK,
                OdooReadbackError,
            ),
        ):
            for name, arguments in methods.items():
                with self.subTest(wrapper=wrapper.__name__, method=name):
                    guard = FrozenBindingAssertion()
                    delegate = Mock(spec=protocol)

                    def read_from_reset_database(*_args, guard=guard):
                        guard.current_hash = "workspace-role-generation-two"
                        return (42,)

                    getattr(delegate, name).side_effect = read_from_reset_database
                    with self.assertRaises(error_type):
                        getattr(wrapper(delegate, guard), name)(*arguments)
                    self.assertEqual(getattr(delegate, name).call_count, 1)
                    self.assertEqual(guard.checks, 2)

    def test_transport_rejections_and_unknown_diagnostics_keep_their_classification(
        self,
    ):
        diagnostic = OdooWriteDiagnostic(
            code=WriteFailureCode.UNKNOWN, model="res.partner", method="create"
        )
        for error in (
            OdooWriteRejected("definite rejection", diagnostic=diagnostic),
            OdooWriteOutcomeUnknown("transport interrupted", diagnostic=diagnostic),
            OdooReadbackError("transport unavailable"),
        ):
            with self.subTest(error=type(error)):
                guard = FrozenBindingAssertion()
                if isinstance(error, OdooReadbackError):
                    delegate = Mock(spec=OdooReadbackReader)
                    delegate.read_ids.side_effect = error
                    call = partial(
                        GenerationFencedOdooReadbackReader(delegate, guard).read_ids,
                        "res.partner",
                        (42,),
                        ("name",),
                    )
                else:
                    delegate = Mock(spec=OdooWriteExecutor)
                    delegate.create_rows.side_effect = error
                    call = partial(
                        GenerationFencedOdooWriteExecutor(delegate, guard).create_rows,
                        "res.partner",
                        ({"name": "Reviewed"},),
                    )
                with self.assertRaises(type(error)) as raised:
                    call()
                self.assertIs(raised.exception, error)
                self.assertEqual(guard.checks, 1)
                if not isinstance(error, OdooReadbackError):
                    self.assertIs(error.diagnostic, diagnostic)

    def test_properties_preserve_connection_and_scope_hashes_without_guard_or_io(self):
        for protocol, wrapper in (
            (OdooWriteExecutor, GenerationFencedOdooWriteExecutor),
            (OdooReadbackReader, GenerationFencedOdooReadbackReader),
        ):
            with self.subTest(wrapper=wrapper.__name__):
                delegate = Mock(spec=protocol)
                delegate.target_hash = "same-url-and-database-hash"
                delegate.scope_hash = "same-reviewed-scope-hash"
                delegate.imports_external_ids = True
                guard = Mock(side_effect=AssertionError("Properties do not call guard"))
                fenced = wrapper(delegate, guard)
                self.assertEqual(fenced.target_hash, delegate.target_hash)
                self.assertEqual(fenced.scope_hash, delegate.scope_hash)
                if isinstance(fenced, GenerationFencedOdooReadbackReader):
                    self.assertTrue(fenced.imports_external_ids)
                guard.assert_not_called()
                self.assertEqual(delegate.mock_calls, [])

    def test_execution_journals_accepted_write_as_unknown_and_stops_other_writes(self):
        snapshot = execution_fixtures._snapshot()
        service, journal = execution_fixtures.ExecutionServiceTests()._service(snapshot)
        delegate = execution_fixtures._Executor(
            execution_api_scope(snapshot).semantic_hash
        )
        guard = FrozenBindingAssertion()
        original_create = delegate.create_rows

        def accepted_then_reset(model, values):
            self.assertEqual(journal.run.rows[0].status, ExecutionRowStatus.IN_FLIGHT)
            receipt = original_create(model, values)
            guard.current_hash = "workspace-role-generation-two"
            return receipt

        with patch.object(delegate, "create_rows", side_effect=accepted_then_reset):
            result = service.execute(
                snapshot.workspace_id,
                expected_snapshot_hash=snapshot.semantic_hash,
                executor=GenerationFencedOdooWriteExecutor(delegate, guard),
                actor=LOCAL_ACTOR,
            )
        self.assertEqual(result.status, ExecutionRunStatus.OUTCOME_UNKNOWN)
        self.assertEqual(result.unknown_count, 1)
        self.assertEqual(len(delegate.creates), 1)
        self.assertEqual(delegate.updates, [])
        self.assertEqual(result.rows[0].attempt, 1)
        self.assertEqual(result.rows[0].status, ExecutionRowStatus.OUTCOME_UNKNOWN)
        self.assertIs(journal.run, result)


if __name__ == "__main__":
    unittest.main()
