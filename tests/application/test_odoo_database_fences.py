"""Composition fences closed Odoo-source seams while preserving File callers."""

import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

from impodo.application.odoo_capture_generation_fence import (
    GenerationFencedOdooSourceCaptureGateway,
)
from impodo.application.odoo_generation_fence import (
    GenerationFencedOdooReadbackReader,
    GenerationFencedOdooWriteExecutor,
)
from impodo.application.odoo_source_capture_service import OdooSourceCapturePort
from impodo.domain.execution.odoo_readback import OdooReadbackError, OdooReadbackReader
from impodo.domain.execution.odoo_write import OdooWriteExecutor, OdooWriteRejected
from impodo.domain.odoo_database_binding import OdooDatabaseBindingRole
from impodo.domain.workspace.errors import WorkspaceError
from impodo.domain.workspace.workbench import (
    OdooConnectionMode,
    SourceMode,
    WorkspaceState,
)
from impodo.web.composition.odoo_database_fences import install_odoo_database_fences
from impodo.web.context import SchemaReadSession


def _workspace():
    return WorkspaceState(
        workspace_id=str(uuid4()),
        name="Frozen source",
        source_system="Odoo",
        source_mode=SourceMode.ODOO,
        odoo_connection_mode=OdooConnectionMode.REMOTE,
        odoo_base_url="https://source.example.test",
        odoo_database="source",
        destination_odoo_connection_mode=OdooConnectionMode.REMOTE,
        destination_odoo_base_url="https://destination.example.test",
        destination_odoo_database="destination",
    )


class OdooDatabaseFenceCompositionTests(unittest.TestCase):
    def _context(self, *, session=True):
        delegates = {
            name: Mock(return_value=object())
            for name in (
                "read_identity_probe",
                "write_identity_probe",
                "schema_reader",
                "model_catalog_reader",
                "destination_match_reader",
                "source_capture_factory",
                "write_executor_factory",
                "readback_reader_factory",
            )
        }
        delegates["source_capture_factory"].return_value = Mock(
            spec=OdooSourceCapturePort
        )
        delegates["write_executor_factory"].return_value = Mock(spec=OdooWriteExecutor)
        delegates["readback_reader_factory"].return_value = Mock(
            spec=OdooReadbackReader
        )
        delegates["schema_read_session_factory"] = (
            Mock(return_value=Mock(spec=SchemaReadSession)) if session else None
        )
        return SimpleNamespace(**delegates), delegates

    def _calls(self, workspace):
        return {
            "read_identity_probe": (workspace, "secret", ("res.partner",)),
            "write_identity_probe": (workspace, "secret", object()),
            "schema_reader": (workspace, "secret"),
            "schema_read_session_factory": (workspace, "secret"),
            "model_catalog_reader": (workspace, "secret"),
            "destination_match_reader": (workspace, "secret", (), ()),
            "source_capture_factory": (workspace, "secret"),
            "write_executor_factory": (workspace, "secret", object()),
            "readback_reader_factory": (workspace, "secret", object()),
        }

    def test_missing_or_reset_binding_refuses_all_seams_before_delegate_factory(self):
        workspace = _workspace()
        for name, args in self._calls(workspace).items():
            with self.subTest(seam=name):
                context, originals = self._context()
                guard_factory = Mock(side_effect=ValueError("private binding details"))
                install_odoo_database_fences(context, guard_factory)
                with self.assertRaises(WorkspaceError) as raised:
                    getattr(context, name)(*args)
                self.assertNotIn("private binding details", str(raised.exception))
                originals[name].assert_not_called()
                self.assertEqual(guard_factory.call_count, 1)

    def test_success_delegates_arguments_and_fences_only_native_protocol_results(self):
        workspace = _workspace()
        wrappers = {
            "source_capture_factory": GenerationFencedOdooSourceCaptureGateway,
            "write_executor_factory": GenerationFencedOdooWriteExecutor,
            "readback_reader_factory": GenerationFencedOdooReadbackReader,
        }
        for name, args in self._calls(workspace).items():
            with self.subTest(seam=name):
                context, originals = self._context()
                events = []

                def guard(events=events):
                    events.append("check")

                def guard_factory(_workspace, _role, events=events, guard=guard):
                    events.append("freeze")
                    return guard

                original_result = originals[name].return_value
                originals[name].side_effect = (
                    lambda *_args, events=events, result=original_result: (
                        events.append("delegate") or result
                    )
                )
                install_odoo_database_fences(context, guard_factory)
                result = getattr(context, name)(*args)
                self.assertEqual(events, ["freeze", "check", "delegate", "check"])
                originals[name].assert_called_once_with(*args)
                if name in wrappers:
                    self.assertIsInstance(result, wrappers[name])
                    self.assertIs(result.delegate, original_result)
                elif name == "schema_read_session_factory":
                    self.assertIs(result.delegate, original_result)
                else:
                    self.assertIs(result, original_result)

    def test_file_and_recipe_callers_preserve_original_results_without_guard(self):
        workspace = replace(_workspace(), source_mode=SourceMode.FILE)
        for name, args in self._calls(workspace).items():
            with self.subTest(seam=name):
                context, originals = self._context()
                guard_factory = Mock(
                    side_effect=AssertionError("File path must remain direct")
                )
                install_odoo_database_fences(context, guard_factory)
                self.assertIs(
                    getattr(context, name)(*args), originals[name].return_value
                )
                originals[name].assert_called_once_with(*args)
                guard_factory.assert_not_called()

    def test_read_roles_use_canonical_destination_scope_and_other_roles_are_explicit(
        self,
    ):
        source = _workspace()
        destination = replace(
            source,
            odoo_base_url="https://DESTINATION.example.test:443/",
            odoo_database="destination",
        )
        for workspace, expected_read in (
            (source, OdooDatabaseBindingRole.SOURCE),
            (destination, OdooDatabaseBindingRole.DESTINATION),
        ):
            for name, args in self._calls(workspace).items():
                with self.subTest(seam=name, read_role=expected_read):
                    context, _ = self._context()
                    guard_factory = Mock(return_value=Mock())
                    install_odoo_database_fences(context, guard_factory)
                    getattr(context, name)(*args)
                    expected = (
                        OdooDatabaseBindingRole.SOURCE
                        if name == "source_capture_factory"
                        else OdooDatabaseBindingRole.DESTINATION
                        if name
                        in {
                            "write_identity_probe",
                            "destination_match_reader",
                            "write_executor_factory",
                            "readback_reader_factory",
                        }
                        else expected_read
                    )
                    guard_factory.assert_called_once_with(workspace, expected)

    def test_readonly_reply_from_changed_generation_is_discarded(self):
        workspace = _workspace()
        for name in (
            "read_identity_probe",
            "write_identity_probe",
            "schema_reader",
            "model_catalog_reader",
            "destination_match_reader",
        ):
            with self.subTest(seam=name):
                context, originals = self._context()
                guard = Mock(side_effect=(None, ValueError("reset")))
                install_odoo_database_fences(context, Mock(return_value=guard))
                with self.assertRaises(WorkspaceError):
                    getattr(context, name)(*self._calls(workspace)[name])
                self.assertEqual(originals[name].call_count, 1)

    def test_schema_session_keeps_frozen_guard_for_each_method_and_optional_none(self):
        workspace = _workspace()
        for method, args in (
            ("read_models", (("res.partner",),)),
            ("read_model_catalog", ()),
            ("verify_identity", (("res.partner",),)),
        ):
            with self.subTest(method=method):
                context, originals = self._context()
                current = [True]

                def guard(current=current):
                    if not current[0]:
                        raise ValueError("reset")

                guard_factory = Mock(return_value=guard)
                install_odoo_database_fences(context, guard_factory)
                session = context.schema_read_session_factory(workspace, "secret")
                delegate = originals["schema_read_session_factory"].return_value
                first = getattr(session, method)(*args)
                self.assertIs(first, getattr(delegate, method).return_value)
                current[0] = False
                with self.assertRaises(WorkspaceError):
                    getattr(session, method)(*args)
                self.assertEqual(getattr(delegate, method).call_count, 1)
                guard_factory.assert_called_once()
        context, _ = self._context(session=False)
        install_odoo_database_fences(context, Mock())
        self.assertIsNone(context.schema_read_session_factory)

    def test_factory_result_cannot_autobind_after_same_endpoint_database_reset(self):
        workspace = _workspace()
        for name, remote, arguments, error in (
            (
                "write_executor_factory",
                "create_rows",
                ("res.partner", ({"name": "Reviewed"},)),
                OdooWriteRejected,
            ),
            (
                "readback_reader_factory",
                "read_ids",
                ("res.partner", (42,), ("name",)),
                OdooReadbackError,
            ),
        ):
            with self.subTest(seam=name):
                context, originals = self._context()
                current = [0]

                def guard_factory(_workspace, _role, current=current):
                    expected = current[0]

                    def guard():
                        if current[0] != expected:
                            raise ValueError("reset")

                    return guard

                factory = Mock(side_effect=guard_factory)
                install_odoo_database_fences(context, factory)
                wrapped = getattr(context, name)(*self._calls(workspace)[name])
                current[0] = 1
                with self.assertRaises(error):
                    getattr(wrapped, remote)(*arguments)
                getattr(originals[name].return_value, remote).assert_not_called()
                factory.assert_called_once()


if __name__ == "__main__":
    unittest.main()
