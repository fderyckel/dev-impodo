"""Prove opt-in target isolation and live-runner behavior without a live host."""

import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

from impodo.adapters.odoo.local_stack import (
    LocalStackCheck,
    LocalStackStatus,
    ReadinessLevel,
)
from impodo.adapters.protected_evidence.credential_vault import MemorySecretStore
from impodo.domain.odoo.contracts import ConnectorError
from impodo.web.target_credentials import TargetCredentialRole, store_target_credential
from scripts import qualify_hierarchy_workflow as runner
from scripts.hierarchy_qualification_journey import FaultObserver, InjectedInterruption
from scripts.hierarchy_qualification_target import (
    FixtureTarget,
    OwnedVault,
    QualificationError,
    TargetDescriptor,
    require_runtime,
)
from tests.support.hierarchy_cases import (
    EXPECTED_CATEGORY_PATHS,
    EXPECTED_PRODUCT_PATHS,
)
from tests.support.hierarchy_qualification_wire import (
    OfflineLocalReader,
    QualificationWire,
)


class HierarchyQualificationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.payload = {
            "contract_version": 1,
            "disposable": True,
            "purpose": "hierarchy_qualification",
            "base_url": "https://fictional.example",
            "database": "impodo_hierarchy_offline",
            "credential_owner_id": str(uuid4()),
        }
        self.path = self.root / "target.json"
        self.save_descriptor()

    def save_descriptor(self):
        self.path.write_text(json.dumps(self.payload), encoding="utf-8")

    def descriptor(self, mode="REMOTE", major=19):
        return TargetDescriptor.read(self.path, mode=mode, major=major)

    def args(self, **kwargs):
        return SimpleNamespace(
            **{
                "allow_write": True,
                "target_descriptor": self.path,
                "source_shape": "generated",
                "mode": "REMOTE",
                "expected_major": 19,
                "fault": "none",
                "output_directory": self.root / "receipt",
                **kwargs,
            }
        )

    def vault(self, mode="REMOTE", major=19):
        vault = MemorySecretStore()
        for role, key in (
            (TargetCredentialRole.READ, "private-read-secret"),
            (TargetCredentialRole.WRITE, "private-write-secret"),
        ):
            store_target_credential(
                vault,
                self.descriptor(mode, major).workspace(),
                role,
                key,
                persistent=True,
            )
        return vault

    def target(self, wire):
        target = FixtureTarget(
            self.descriptor(), "private-read-secret", "private-write-secret", self.root
        )
        target.reader._transport = wire.transport
        target.seeder.transport = wire.transport
        return target

    def test_authorization_precedes_credentials_and_target_construction(self):
        vault, target, journey = Mock(), Mock(), Mock()
        report = runner.qualify(
            self.args(allow_write=False),
            vault=vault,
            target_factory=target,
            journey_factory=journey,
        )
        self.assertEqual(
            (report["status"], report["failure_code"], report["cleanup"]),
            ("FAILED", "ALLOW_WRITE_REQUIRED", "NOT_NEEDED"),
        )
        vault.get.assert_not_called()
        target.assert_not_called()
        journey.assert_not_called()
        self.assertTrue((self.root / "receipt/result.json").is_file())

    def test_descriptor_refuses_unsafe_allocations_before_credentials(self):
        for changes in (
            {"disposable": False},
            {"database": "production"},
            {"database": "impodo_hierarchy_" + "x" * 64},
            {"base_url": "http://fictional.example"},
            {"lab_target": "preview", "source_commit": "a" * 40},
            {"api_key": "private-secret"},
            {"credential_owner_id": "not-a-uuid"},
        ):
            with self.subTest(changes=changes):
                original = self.payload.copy()
                self.payload.update(changes)
                self.save_descriptor()
                with self.assertRaises(
                    (QualificationError, ValueError, ConnectorError)
                ):
                    self.descriptor()
                self.payload = original

    def test_only_exact_target_and_role_vault_envelopes_are_accepted(self):
        vault = self.vault()
        self.assertEqual(
            self.descriptor().credentials(vault),
            ("private-read-secret", "private-write-secret"),
        )
        self.payload["database"] = "impodo_hierarchy_other"
        self.save_descriptor()
        with self.assertRaisesRegex(QualificationError, "TARGET_CREDENTIALS_REQUIRED"):
            self.descriptor().credentials(vault)

    def test_lab_descriptor_requires_the_exact_final_pin_for_its_major(self):
        manifest = json.loads(
            (
                Path(__file__).resolve().parents[3]
                / "fixtures/odoo-compatibility/lab-targets.json"
            ).read_text()
        )
        for major, lab in ((19, "baseline"), (20, "odoo20")):
            with self.subTest(major=major):
                self.payload.update(
                    lab_target=lab, source_commit=manifest["targets"][lab]["commit"]
                )
                self.save_descriptor()
                self.assertEqual(self.descriptor(major=major).lab_target, lab)
                self.payload["source_commit"] = "a" * 40
                self.save_descriptor()
                with self.assertRaisesRegex(QualificationError, "LAB_PIN_MISMATCH"):
                    self.descriptor(major=major)

    def test_existing_result_directory_is_never_overwritten_or_used_for_writes(self):
        output = self.root / "receipt"
        output.mkdir()
        result = output / "result.json"
        result.write_text("previous receipt", encoding="utf-8")
        vault = Mock()
        with self.assertRaises(FileExistsError):
            runner.qualify(self.args(), vault=vault)
        vault.get.assert_not_called()
        self.assertEqual(result.read_text(), "previous receipt")

    def test_final_runtime_matrix_rejects_preview_saas_and_other_majors(self):
        for major in (19, 20):
            self.assertEqual(require_runtime(f"{major}.0", major), f"{major}.0")
            for version in (
                "unknown",
                "20.0rc1",
                "20.0beta1",
                "saas~19.5",
                "19.5",
                "21.0",
                f"{39 - major}.0",
            ):
                with (
                    self.subTest(major=major, version=version),
                    self.assertRaises(QualificationError),
                ):
                    require_runtime(version, major)

    def test_vault_cleanup_never_adopts_existing_keys_and_retains_evidence_keys(self):
        vault = self.vault()
        originals = dict(vault.values)
        owned = OwnedVault(vault, self.root / "ledger.json")
        existing = next(iter(originals))
        with self.assertRaisesRegex(QualificationError, "VAULT_ENTRY_ALREADY_EXISTS"):
            owned.set(existing, "replacement", persistent=True)
        with self.assertRaisesRegex(QualificationError, "UNOWNED_VAULT_DELETE"):
            owned.delete(existing)
        owned.set("fixture:read:opaque", "new-secret", persistent=True)
        owned.set("fixture:protected:opaque", "protected-secret", persistent=True)
        owned.cleanup()
        self.assertEqual(
            vault.values, {**originals, "fixture:protected:opaque": "protected-secret"}
        )
        self.assertNotIn("secret", (self.root / "ledger.json").read_text())

    def test_validation_refuses_collisions_modules_and_permissions_without_writes(self):
        for problem in (
            "collision",
            "modules",
            "permission",
            "read-permission",
            "runtime",
        ):
            with self.subTest(problem=problem):
                wire = QualificationWire()
                if problem == "collision":
                    wire.records[("product.category", 88)] = {
                        "name": "Service",
                        "parent_id": False,
                    }
                elif problem == "modules":
                    wire.module_names = ("base", "product")
                elif problem == "permission":
                    wire.denied = ("product.category", "unlink")
                elif problem == "read-permission":
                    wire.denied = ("product.template", "read")
                else:
                    wire.major = 20
                with (
                    patch(
                        "impodo.adapters.odoo.connectors.build_opener",
                        return_value=wire,
                    ),
                    self.assertRaises((QualificationError, ConnectorError)),
                ):
                    self.target(wire).validate()
                self.assertFalse(wire.writes)
                self.assertFalse(wire.unlinks)

    def test_seed_uses_five_owned_records_and_real_parent_receipts(self):
        wire = QualificationWire()
        target = self.target(wire)
        target.seed()
        self.assertEqual(len(target.owned["product.category"]), 5)
        paths = wire.category_paths()
        self.assertEqual(
            set(paths.values()) - {("All",)},
            {
                ("Machines",),
                ("Machines", "Pumps"),
                ("Machines", "Pumps", "Service"),
                ("Machines", "Pumps", "Service", "Seals"),
                ("Maintenance",),
            },
        )
        self.assertNotIn(50, target.owned["product.category"])
        self.assertEqual(len(wire.writes), 4)

    def test_archived_fixture_collision_also_refuses_seeding(self):
        wire = QualificationWire()
        wire.records[("product.template", 88)] = {
            "name": "Archived",
            "default_code": "PX101",
            "active": False,
        }
        with (
            patch("impodo.adapters.odoo.connectors.build_opener", return_value=wire),
            self.assertRaisesRegex(QualificationError, "FIXTURE_SCOPE_NOT_EMPTY"),
        ):
            self.target(wire).validate()
        self.assertFalse(wire.writes)

    def completed_target(self):
        wire = QualificationWire()
        target = self.target(wire)
        target.seed()
        paths = {path: identifier for identifier, path in wire.category_paths().items()}
        for path in sorted(EXPECTED_CATEGORY_PATHS, key=lambda path: (len(path), path)):
            if path not in paths:
                (identifier,) = wire.create_rows(
                    "product.category",
                    ({"name": path[-1], "parent_id": paths.get(path[:-1], False)},),
                )
                target.owned["product.category"].add(identifier)
                paths[path] = identifier
        identifiers = wire.load_create_rows(
            "product.template",
            tuple(
                {"default_code": reference, "name": reference, "categ_id": paths[path]}
                for reference, path in EXPECTED_PRODUCT_PATHS.items()
            ),
            tuple("fixture." + reference for reference in EXPECTED_PRODUCT_PATHS),
        )
        target.owned["product.template"].update(identifiers)
        target.external_ids = tuple(wire.external_ids)
        return target, wire

    def test_cleanup_removes_only_receipted_ids_in_dependency_order_and_checks_hooks(
        self,
    ):
        target, wire = self.completed_target()
        target.cleanup()
        self.assertEqual(
            wire.records,
            {("product.category", 50): {"name": "All", "parent_id": False}},
        )
        self.assertEqual(
            [model for model, _ in wire.unlinks][:2],
            ["ir.model.data", "product.template"],
        )
        self.assertFalse(wire.external_ids)
        self.assertIn(("product.product", "search_read"), wire.calls)

    def test_cleanup_refuses_unowned_records_foreign_dependents_and_rebound_external_ids(
        self,
    ):
        for problem in (
            "unowned",
            "foreign-product",
            "foreign-child",
            "external-binding",
            "foreign-binding",
        ):
            with self.subTest(problem=problem):
                target, wire = self.completed_target()
                category = min(target.owned["product.category"])
                if problem == "unowned":
                    target.owned["product.category"].remove(category)
                elif problem == "foreign-product":
                    wire.records[("product.template", 999)] = {
                        "default_code": "FOREIGN",
                        "categ_id": category,
                        "active": False,
                    }
                elif problem == "foreign-child":
                    wire.records[("product.category", 999)] = {
                        "name": "Foreign",
                        "parent_id": category,
                    }
                elif problem == "foreign-binding":
                    wire.records[("ir.model.data", 999)] = {
                        "module": "foreign",
                        "name": "alias",
                        "model": "product.category",
                        "res_id": category,
                    }
                else:
                    wire.records[("ir.model.data", 1)]["res_id"] = 999
                with self.assertRaises(QualificationError):
                    target.cleanup()
                self.assertFalse(wire.unlinks)

    def test_cleanup_does_not_replay_an_uncertain_unlink(self):
        target, wire = self.completed_target()
        target.seeder.transport = Mock(
            side_effect=TimeoutError("private target details")
        )
        with self.assertRaises(TimeoutError):
            target.cleanup()
        target.seeder.transport.assert_called_once()
        self.assertFalse(wire.unlinks)

    def test_failed_and_incomplete_cleanup_receipts_retain_evidence_without_error_text(
        self,
    ):
        for cleanup in (False, True):
            with self.subTest(cleanup=cleanup):
                target = Mock(seeded=True, variant_count=6)
                target.validate.return_value = "19.0"
                journey = Mock(metrics=[])
                journey.hierarchy.return_value = ("workspace", None)
                journey.publish_recipe.return_value = ("project", "publication")
                journey.load.return_value = SimpleNamespace(committed_count=13)
                journey.compare.return_value = SimpleNamespace(
                    create_count=0, update_count=0, unchanged_count=18
                )
                secret_error = QualificationError(
                    "private-read-secret https://private.example 987654"
                )
                if cleanup:
                    target.cleanup.side_effect = secret_error
                else:
                    journey.prepare_review.side_effect = secret_error
                output = self.root / ("cleanup" if cleanup else "failed")
                report = runner.qualify(
                    self.args(output_directory=output),
                    vault=self.vault(),
                    target_factory=Mock(return_value=target),
                    journey_factory=Mock(return_value=journey),
                )
                self.assertEqual(report["status"], "FAILED")
                self.assertEqual(report["failure_code"], "QUALIFICATION_PHASE_FAILED")
                self.assertEqual(
                    report["cleanup"],
                    "INCOMPLETE" if cleanup else "RETAINED_FOR_DIAGNOSIS",
                )
                self.assertNotIn(
                    str(secret_error), (output / "result.json").read_text()
                )
                self.assertTrue((output / "private").is_dir())
                journey.close.assert_called_once()
                self.assertEqual(target.cleanup.call_count, int(cleanup))

    def test_partial_seed_failure_retains_uncertain_target_and_does_not_construct_journey(
        self,
    ):
        target = Mock(seeded=True, variant_count=6)
        target.validate.return_value = "19.0"
        target.seed.side_effect = TimeoutError("lost seed receipt")
        journey = Mock()
        report = runner.qualify(
            self.args(),
            vault=self.vault(),
            target_factory=Mock(return_value=target),
            journey_factory=journey,
        )
        self.assertEqual(report["cleanup"], "RETAINED_FOR_DIAGNOSIS")
        target.cleanup.assert_not_called()
        journey.assert_not_called()

    def local_descriptor(self):
        config = self.root / "odoo.conf"
        config.write_text(
            "[options]\nhttp_interface = 127.0.0.1\nhttp_port = 8069\ndb_name = impodo_hierarchy_offline\n",
            encoding="utf-8",
        )
        for name in ("python", "odoo_bin"):
            path = self.root / name
            path.write_text("offline host placeholder", encoding="utf-8")
            self.payload[name] = str(path)
        self.payload.update(base_url="http://127.0.0.1:8069", local_config=str(config))
        self.save_descriptor()

    def test_local_descriptor_checks_exact_config_and_lab_source_pin_without_starting_services(
        self,
    ):
        self.local_descriptor()
        self.assertEqual(
            self.descriptor("LOCAL").profile().database_hint, self.payload["database"]
        )
        self.payload["database"] = "impodo_hierarchy_different"
        self.save_descriptor()
        with self.assertRaisesRegex(
            QualificationError, "LOCAL_PROFILE_TARGET_MISMATCH"
        ):
            self.descriptor("LOCAL")
        self.payload["database"] = "impodo_hierarchy_offline"
        self.payload["source_commit"] = "a" * 40
        self.save_descriptor()
        with (
            patch(
                "scripts.hierarchy_qualification_target.subprocess.check_output",
                return_value="b" * 40,
            ),
            self.assertRaisesRegex(QualificationError, "LOCAL_SOURCE_PIN_MISMATCH"),
        ):
            self.descriptor("LOCAL")

    def test_remote_fault_matrix_reopens_and_recovers_actual_target_state(self):
        for fault in ("before-dispatch", "after-commit", "after-receipt"):
            with self.subTest(fault=fault):
                wire = QualificationWire(20)
                report = self.run_wire(
                    wire,
                    self.args(
                        expected_major=20,
                        source_shape="supplied",
                        fault=fault,
                        output_directory=self.root / fault,
                    ),
                )
                self.assertEqual(report["status"], "PASSED", report)
                self.assertTrue(report["fault_triggered"])
                self.assertEqual(sum(len(ids) for _, _, ids in wire.writes), 36)
                self.assertEqual(report["cleanup"], "COMPLETE")

    def test_known_rejection_and_unknown_outcome_are_failed_retained_results(self):
        for fault in ("known-rejection", "unknown-outcome"):
            with self.subTest(fault=fault):
                wire = QualificationWire()
                report = self.run_wire(
                    wire, self.args(fault=fault, output_directory=self.root / fault)
                )
                self.assertEqual(
                    (report["status"], report["failure_code"], report["cleanup"]),
                    ("FAILED", "LOAD_INCOMPLETE", "RETAINED_FOR_DIAGNOSIS"),
                )
                self.assertTrue(report["fault_triggered"])
                self.assertFalse(wire.unlinks)
                self.assertLess(sum(len(ids) for _, _, ids in wire.writes), 18)

    def run_wire(self, wire, args):
        with patch("impodo.adapters.odoo.connectors.build_opener", return_value=wire):
            return runner.qualify(
                args, vault=self.vault(args.mode, args.expected_major)
            )

    def test_local_mode_uses_local_schema_and_comparison_boundary_and_json2_writes(
        self,
    ):
        self.local_descriptor()
        wire = QualificationWire(20, "LOCAL")
        local = OfflineLocalReader(wire, self.payload["database"])

        def probe(profile):
            return LocalStackStatus(
                str(profile.config_path),
                profile.base_url,
                profile.database_hint,
                tuple(
                    LocalStackCheck(key, key, ReadinessLevel.READY, "offline")
                    for key in ("odoo", "postgresql", "api")
                ),
                profile,
            )

        with ExitStack() as stack:
            for module in (
                "scripts.hierarchy_qualification_target",
                "impodo.web.composition.local_app",
            ):
                stack.enter_context(
                    patch(module + ".LocalOdooMetadataReader", return_value=local)
                )
            stack.enter_context(
                patch(
                    "scripts.hierarchy_qualification_journey.probe_local_stack",
                    side_effect=probe,
                )
            )
            start = stack.enter_context(
                patch("impodo.adapters.odoo.local_stack.start_local_stack")
            )
            stop = stack.enter_context(
                patch("impodo.adapters.odoo.local_stack._stop_owned_odoo")
            )
            report = self.run_wire(
                wire,
                self.args(mode="LOCAL", expected_major=20, source_shape="supplied"),
            )
        self.assertEqual(report["status"], "PASSED", report)
        self.assertEqual(local.calls.count("comparison"), 3)
        self.assertEqual(local.calls.count("schema"), 1)
        self.assertIn(("product.template", "create"), wire.calls)
        self.assertNotIn(("product.template", "load"), wire.calls)
        start.assert_not_called()
        stop.assert_not_called()

    def test_transport_faults_only_follow_actual_accepted_writes_and_never_retry(self):
        for fault, exception in (
            ("after-commit", InjectedInterruption),
            ("unknown-outcome", TimeoutError),
        ):
            for endpoint, result in (
                ("create", [71]),
                ("load", {"ids": [71, 72], "messages": []}),
            ):
                with self.subTest(fault=fault, endpoint=endpoint):
                    original = Mock(return_value=(200, result))
                    observer = FaultObserver(fault)
                    with self.assertRaises(exception):
                        observer.transport(original)(
                            "https://fictional.example/json/2/product.category/"
                            + endpoint,
                            {},
                            b"{}",
                            1,
                            "POST",
                        )
                    original.assert_called_once()
                    self.assertTrue(observer.fired)
                    self.assertEqual(observer.accepted_batches, 1)
        for endpoint, reply in (
            ("search_read", (200, [{"id": 71}])),
            ("load", (403, {"ids": [71]})),
            ("load", (200, {"ids": False})),
            ("create", (200, [True])),
        ):
            observer = FaultObserver("after-commit")
            original = Mock(return_value=reply)
            self.assertEqual(
                observer.transport(original)(
                    "https://fictional.example/json/2/product.category/" + endpoint,
                    {},
                    b"{}",
                    1,
                    "POST",
                ),
                reply,
            )
            self.assertFalse(observer.fired)
            self.assertEqual(observer.accepted_batches, 0)

    def test_rejection_skips_transport_and_receipt_fault_follows_durable_boundary(self):
        original = Mock()
        observer = FaultObserver("known-rejection")
        self.assertEqual(
            observer.transport(original)(
                "https://fictional.example/json/2/product.category/create",
                {},
                b"{}",
                1,
                "POST",
            )[0],
            403,
        )
        original.assert_not_called()
        for boundary in ("before-dispatch", "after-receipt"):
            observer = FaultObserver(boundary)
            journal = Mock(return_value="durable")
            wrapped = observer.receipt(journal, boundary)
            with self.assertRaises(InjectedInterruption):
                wrapped("row")
            journal.assert_called_once_with("row")
            self.assertEqual(wrapped("row"), "durable")

    def test_real_remote_journey_captures_defaults_replays_recipe_and_cleans_exact_ids(
        self,
    ):
        wire = QualificationWire()
        vault = self.vault()
        original_credentials = dict(vault.values)
        with patch("impodo.adapters.odoo.connectors.build_opener", return_value=wire):
            report = runner.qualify(self.args(), vault=vault)
        self.assertEqual(report["status"], "PASSED", report)
        self.assertEqual(report["cleanup"], "COMPLETE")
        self.assertEqual(
            report["phases"],
            [
                {
                    "name": "authoring",
                    "committed": 13,
                    "verified": 18,
                    "generated_variants": 6,
                },
                {
                    "name": "recipe",
                    "committed": 13,
                    "verified": 18,
                    "generated_variants": 6,
                },
            ],
        )
        self.assertEqual(
            wire.records,
            {("product.category", 50): {"name": "All", "parent_id": False}},
        )
        self.assertIn(("product.template", "default_get"), wire.calls)
        self.assertTrue(wire.unlinks)
        self.assertTrue(all(50 not in identifiers for _, identifiers in wire.unlinks))
        self.assertTrue(original_credentials.items() <= vault.values.items())
        portable = (self.root / "receipt/result.json").read_text()
        for value in (
            "private-read-secret",
            "private-write-secret",
            self.payload["base_url"],
            self.payload["database"],
            self.payload["credential_owner_id"],
        ):
            self.assertNotIn(value, portable)
        self.assertEqual(report["services_started"], 0)
        self.assertEqual(len(report["database_timings"]), 3)
        for timings in report["database_timings"]:
            self.assertLessEqual(timings["connections"], 12)
            self.assertLessEqual(timings["schema_checks"], 5)
            self.assertEqual(timings["lock_retries"], 0)


if __name__ == "__main__":
    unittest.main()
