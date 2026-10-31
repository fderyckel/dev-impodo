from __future__ import annotations

import unittest
from dataclasses import replace

from impodo.application.workspace.execution.service import execution_api_scope
from impodo.domain.execution_snapshot import FieldIntent
from impodo.domain.shared.access import LOCAL_ACTOR
from impodo.domain.shared.models import OdooReadIdentity, OdooWriteIdentity
from impodo.domain.workspace.errors import WorkspaceError
from impodo.domain.workspace.workbench import OdooConnectionMode
from tests.application.workspace.execution import test_service as support


class ExecutionCompanyContextTests(unittest.TestCase):
    def test_custom_models_keep_an_explicitly_blank_record_company(self):
        for model, version in (
            ("x.dispatch.zone", "19.0"),
            ("x.equipment.asset", "20.0"),
        ):
            with self.subTest(model=model, version=version):
                original = support._snapshot()
                row = replace(
                    original.rows[0],
                    target_model=model,
                    fields=(
                        *original.rows[0].fields,
                        FieldIntent("company_id", "SET_VALUE", None),
                    ),
                )
                snapshot = replace(
                    original,
                    datasets=(replace(original.datasets[0], target_model=model),),
                    rows=(row,),
                    counts={
                        "CREATE": 1,
                        "UPDATE": 0,
                        "UNCHANGED": 0,
                        "AMBIGUOUS": 0,
                        "BLOCKED": 0,
                    },
                    target_odoo_version=version,
                    target_module_versions={"base": version},
                    read_context_hash="sha256:" + "4" * 64,
                )
                service, journal = support.ExecutionServiceTests()._service(
                    snapshot,
                    mode=OdooConnectionMode.REMOTE,
                )
                scope = execution_api_scope(snapshot)
                identity = OdooWriteIdentity(
                    target_hash=snapshot.target_hash,
                    principal_hash="sha256:" + "5" * 64,
                    permission_hash="sha256:" + "6" * 64,
                    context_hash=snapshot.read_context_hash,
                    readable_models=(model,),
                    writable_models=(model,),
                    observed_at="2026-10-01T00:00:00Z",
                )
                executor = support._Executor(scope.semantic_hash)

                run = service.execute(
                    snapshot.workspace_id,
                    expected_snapshot_hash=snapshot.semantic_hash,
                    executor=executor,
                    actor=LOCAL_ACTOR,
                    write_identity=identity,
                    write_credential_binding_hash="sha256:" + "8" * 64,
                )

                self.assertEqual(run.committed_count, 1)
                self.assertEqual(
                    journal.run.write_context_hash, snapshot.read_context_hash
                )
                self.assertEqual(len(executor.loads), 1)
                self.assertEqual(executor.loads[0][0], model)
                self.assertEqual(executor.loads[0][1][0]["company_id"], "")

    def test_different_users_can_load_under_the_same_reviewed_company(self):
        snapshot = replace(
            support._snapshot(),
            read_credential_binding_hash="sha256:" + "1" * 64,
            read_principal_hash="sha256:" + "2" * 64,
            read_permission_hash="sha256:" + "3" * 64,
            read_context_hash="sha256:" + "4" * 64,
            readable_models=("product.category", "product.template", "res.partner"),
        )
        service, journal = support.ExecutionServiceTests()._service(
            snapshot, mode=OdooConnectionMode.REMOTE
        )
        service.require_remote_read_identity = True
        service.require_remote_write_identity = True
        service.current_read_credential_binding = lambda _state, _actor: (
            snapshot.read_credential_binding_hash
        )
        scope = execution_api_scope(snapshot)
        read_identity = OdooReadIdentity(
            target_hash=snapshot.target_hash,
            principal_hash=snapshot.read_principal_hash,
            permission_hash=snapshot.read_permission_hash,
            context_hash=snapshot.read_context_hash,
            readable_models=snapshot.readable_models,
            observed_at="2026-10-01T00:00:00Z",
        )
        write_identity = OdooWriteIdentity(
            target_hash=snapshot.target_hash,
            principal_hash="sha256:" + "5" * 64,
            permission_hash="sha256:" + "6" * 64,
            context_hash=snapshot.read_context_hash,
            readable_models=tuple(item.model for item in scope.models),
            writable_models=tuple(
                item.model for item in scope.models if item.write_fields
            ),
            observed_at="2026-10-01T00:00:00Z",
        )

        run = service.execute(
            snapshot.workspace_id,
            expected_snapshot_hash=snapshot.semantic_hash,
            executor=support._Executor(scope.semantic_hash),
            actor=LOCAL_ACTOR,
            read_identity=read_identity,
            read_credential_binding_hash=snapshot.read_credential_binding_hash,
            write_identity=write_identity,
            write_credential_binding_hash="sha256:" + "8" * 64,
        )

        self.assertEqual(run.committed_count, 3)
        self.assertEqual(journal.run.write_context_hash, snapshot.read_context_hash)
        self.assertNotEqual(run.write_principal_hash, snapshot.read_principal_hash)

    def test_changed_company_stops_before_journal_or_target_io(self):
        snapshot = replace(support._snapshot(), read_context_hash="sha256:" + "4" * 64)
        service, journal = support.ExecutionServiceTests()._service(
            snapshot,
            mode=OdooConnectionMode.REMOTE,
        )
        scope = execution_api_scope(snapshot)
        identity = OdooWriteIdentity(
            target_hash=snapshot.target_hash,
            principal_hash="sha256:" + "5" * 64,
            permission_hash="sha256:" + "6" * 64,
            context_hash="sha256:" + "7" * 64,
            readable_models=tuple(item.model for item in scope.models),
            writable_models=tuple(
                item.model for item in scope.models if item.write_fields
            ),
            observed_at="2026-10-01T00:00:00Z",
        )
        executor = support._Executor(scope.semantic_hash)

        with self.assertRaisesRegex(WorkspaceError, "reviewed Odoo company context"):
            service.execute(
                snapshot.workspace_id,
                expected_snapshot_hash=snapshot.semantic_hash,
                executor=executor,
                actor=LOCAL_ACTOR,
                write_identity=identity,
                write_credential_binding_hash="sha256:" + "8" * 64,
            )

        self.assertIsNone(journal.run)
        self.assertEqual(executor.creates, [])
        self.assertEqual(executor.updates, [])


if __name__ == "__main__":
    unittest.main()
