from __future__ import annotations

import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from impodo.adapters.odoo.local_reader import (
    LocalOdooMetadataReader,
    LocalOdooReaderError,
    LocalShellResult,
)
from impodo.adapters.odoo.local_stack import LocalStackProfile
from impodo.domain.odoo.company import OdooCompanySelection
from impodo.domain.odoo.contracts import MetadataRequest, RecordRequest
from impodo.domain.shared.models import target_identity_hash
from impodo.domain.workspace.workbench import (
    OdooConnectionMode,
    WorkspaceState,
)
from tests.support.paths import REPOSITORY_ROOT

ROOT = REPOSITORY_ROOT
MARKER = "__IMPODO_LOCAL_ODOO_JSON__"


class LocalOdooMetadataReaderTests(unittest.TestCase):
    def test_local_version_evidence_uses_the_shared_policy(self) -> None:
        for version, info, allowed in (
            ("19.0", [19, 0, 0, "final", 0, ""], True),
            ("19.4", [19, 4, 0, "final", 0, ""], True),
            ("19.5a1", [19, 5, 0, "alpha", 1, ""], True),
            ("20.0", [20, 0, 0, "final", 0, ""], True),
            ("20.0rc1", [20, 0, 0, "candidate", 1, ""], False),
            ("19.0", [20, 0, 0, "final", 0, ""], False),
            ("19.0", None, False),
            ("unknown", [19, 0, 0, "final", 0, ""], False),
        ):
            with self.subTest(version=version, info=info):
                payload = {
                    "database": "odoo19_local",
                    "version": version,
                    "version_info": info,
                }
                reader = LocalOdooMetadataReader(
                    runner=lambda *_, payload=payload: _result(payload)
                )
                if allowed:
                    self.assertEqual(
                        reader.get_target_fingerprint(
                            self.workspace_state, self.profile
                        ).odoo_version,
                        version,
                    )
                else:
                    with self.assertRaises(LocalOdooReaderError):
                        reader.get_target_fingerprint(
                            self.workspace_state, self.profile
                        )

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temporary.name) / "odoo_ve"
        self.config = self.workspace / "config" / "odoo.conf"
        self.python = self.workspace / "venv" / "Scripts" / "python.exe"
        self.odoo_bin = self.workspace / "odoo" / "odoo-bin"
        for path in (self.config, self.python, self.odoo_bin):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()
        self.workspace_state = WorkspaceState(
            workspace_id="project-1",
            name="Local metadata",
            source_system="CSV",
            odoo_connection_mode=OdooConnectionMode.LOCAL,
            odoo_base_url="http://127.0.0.1:8069",
            odoo_database="odoo19_local",
            intended_models=("res.partner", "res.company"),
        )
        self.profile = LocalStackProfile(
            config_path=self.config,
            workspace_root=self.workspace,
            db_host="127.0.0.1",
            db_port=5432,
            db_user="odoo",
            http_interface="127.0.0.1",
            http_port=8069,
            base_url="http://127.0.0.1:8069",
            database_hint="odoo19_local",
            pg_isready_path=None,
            pg_ctl_path=None,
            pg_data_path=None,
            python_path=self.python,
            odoo_bin_path=self.odoo_bin,
            logs_path=None,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_catalogue_is_one_fixed_read_without_an_api_key(self) -> None:
        calls = []
        payload = {
            "database": "odoo19_local",
            "version": "19.0",
            "records": [
                {
                    "id": 2,
                    "name": "Company",
                    "model": "res.company",
                    "abstract": False,
                    "transient": False,
                    "modules": "base",
                    "state": "base",
                },
                {
                    "id": 1,
                    "name": "Contact",
                    "model": "res.partner",
                    "abstract": False,
                    "transient": False,
                    "modules": "base, contacts",
                    "state": "base",
                },
            ],
        }

        def runner(command, script, cwd, timeout):
            calls.append((command, script, cwd, timeout))
            return _result(payload)

        snapshot = LocalOdooMetadataReader(runner=runner).get_model_catalog(
            self.workspace_state,
            self.profile,
        )

        self.assertEqual(
            [record.odoo_id for record in snapshot.records["ir.model"]],
            [1, 2],
        )
        command, script, cwd, timeout = calls[0]
        self.assertEqual(command[:3], (str(self.python), str(self.odoo_bin), "shell"))
        self.assertIn("--no-http", command)
        self.assertNotIn("api", " ".join(command).casefold())
        self.assertEqual(cwd, self.workspace)
        self.assertEqual(timeout, 120)
        self.assertEqual(script.count(".search_read("), 1)
        self.assertIn('env["ir.model"].sudo().search_read', script)
        self.assertIn("env.cr.rollback()", script)

    def test_fields_get_captures_effective_inherited_fields_once_per_model(
        self,
    ) -> None:
        calls = []
        payload = {
            "database": "odoo19_local",
            "version": "19.4",
            "models": {
                "res.partner": {
                    "description": "Contact",
                    "unique_constraints": [
                        {
                            "name": "res_partner_ref_uniq",
                            "definition": "UNIQUE(ref)",
                        }
                    ],
                    "fields": {
                        "name": {
                            "string": "Name",
                            "type": "char",
                            "required": True,
                            "readonly": False,
                        },
                        "message_ids": {
                            "string": "Messages",
                            "type": "one2many",
                            "required": False,
                            "readonly": True,
                            "relation": "mail.message",
                            "relation_field": "res_id",
                        },
                    },
                },
                "res.company": {
                    "description": "Company",
                    "fields": {
                        "name": {
                            "string": "Company Name",
                            "type": "char",
                            "required": True,
                            "readonly": False,
                        }
                    },
                },
            },
        }

        def runner(command, script, cwd, timeout):
            calls.append((command, script, cwd, timeout))
            return _result(payload)

        snapshot = LocalOdooMetadataReader(runner=runner).get_model_metadata(
            self.workspace_state,
            self.profile,
            ("res.partner", "res.company"),
        )

        partner_fields = snapshot.models["res.partner"].fields
        self.assertEqual(set(partner_fields), {"name", "message_ids"})
        self.assertEqual(
            partner_fields["message_ids"].relation,
            "mail.message",
        )
        self.assertEqual(
            snapshot.models["res.partner"].unique_constraints[0].definition,
            "UNIQUE(ref)",
        )
        script = calls[0][1]
        self.assertIn(
            "requested_models = ('res.partner', 'res.company')",
            script,
        )
        self.assertEqual(script.count(".fields_get("), 1)
        self.assertIn("for model_name in requested_models:", script)
        self.assertIn('env["ir.model.constraint"].sudo().search', script)
        self.assertIn("allfields=[]", script)
        self.assertIn("ensure_ascii=True", script)
        self.assertIn("env.cr.rollback()", script)

    def test_local_schema_captures_required_many2one_create_default(self) -> None:
        state = replace(
            self.workspace_state,
            odoo_company=OdooCompanySelection(
                target_identity_hash(
                    connection_mode="LOCAL",
                    base_url=self.workspace_state.odoo_base_url,
                    database=self.workspace_state.odoo_database,
                ),
                1,
                "Fixture",
            ),
        )
        for major in (19, 20):
            with self.subTest(major=major):
                self._check_required_many2one_default(state, major)

    def _check_required_many2one_default(
        self, state: WorkspaceState, major: int
    ) -> None:
        env = MagicMock()
        env.return_value = env
        env.cr.dbname = state.odoo_database
        env["ir.model.constraint"].sudo().search.return_value = []
        product = MagicMock()
        env.__getitem__.side_effect = {
            "product.template": product,
            "ir.model.constraint": env["ir.model.constraint"],
        }.__getitem__
        product.sudo.return_value = product
        product._description = "Product"
        product.fields_get.return_value = {
            "uom_id": {
                "type": "many2one", "required": True, "relation": "uom.uom",
            },
            "categ_id": {
                "type": "many2one", "required": False, "relation": "product.category",
            },
            "readonly_ref": {"type": "many2one", "required": True, "readonly": True},
        }
        product.default_get.side_effect = lambda fields: {
            field: 1 for field in fields
        }
        release = SimpleNamespace(
            version=f"{major}.0", version_info=(major, 0, 0, "final", 0, "")
        )

        def runner(_command, script, _cwd, _timeout):
            output = StringIO()
            with (
                patch.dict(sys.modules, {"odoo": SimpleNamespace(release=release)}),
                redirect_stdout(output),
            ):
                exec(script, {"env": env})  # noqa: S102 -- Run the fixed shell program against the fake Odoo environment.
            return LocalShellResult(0, output.getvalue(), "")

        snapshot = LocalOdooMetadataReader(runner=runner).get_model_metadata(
            state, self.profile, ("product.template",)
        )
        self.assertEqual(
            snapshot.create_defaults["product.template"], {"uom_id": 1}
        )
        product.default_get.assert_called_once_with(["uom_id"])
        env.cr.rollback.assert_called_once_with()


    def test_profile_must_match_the_exact_local_target(self) -> None:
        mismatched = WorkspaceState(
            workspace_id="project-1",
            name="Wrong target",
            source_system="CSV",
            odoo_connection_mode=OdooConnectionMode.LOCAL,
            odoo_base_url="http://127.0.0.1:8070",
            odoo_database="odoo19_local",
        )
        reader = LocalOdooMetadataReader(
            runner=lambda *_args: self.fail("runner must not be called")
        )

        with self.assertRaisesRegex(
            LocalOdooReaderError,
            "does not match",
        ):
            reader.get_target_fingerprint(mismatched, self.profile)

    def test_preflight_capture_batches_models_in_one_rolled_back_shell(self) -> None:
        calls = []
        payload = {
            "database": "odoo19_local",
            "version": "19.0",
            "models": {
                "res.partner": {
                    "description": "Contact",
                    "create_defaults": {"ref": "NEW"},
                    "fields": {
                        "ref": {
                            "string": "Reference",
                            "type": "char",
                            "required": True,
                            "readonly": False,
                        }
                    },
                }
            },
            "records": {
                "res.partner": [
                    {"id": 7, "ref": "P-7"},
                ]
            },
        }

        def runner(command, script, cwd, timeout):
            calls.append((command, script, cwd, timeout))
            return _result(payload)

        metadata, records = LocalOdooMetadataReader(
            runner=runner
        ).get_preflight_snapshots(
            self.workspace_state,
            self.profile,
            (MetadataRequest(model="res.partner", fields=("ref",)),),
            (
                RecordRequest(
                    model="res.partner",
                    fields=("ref",),
                    domain=(("ref", "in", ("P-7",)),),
                ),
            ),
        )

        self.assertEqual(metadata.fingerprint, records.fingerprint)
        self.assertEqual(metadata.create_defaults["res.partner"]["ref"], "NEW")
        self.assertEqual(records.records["res.partner"][0].values["ref"], "P-7")
        self.assertEqual(len(calls), 1)
        script = calls[0][1]
        self.assertEqual(script.count(".fields_get("), 1)
        self.assertEqual(script.count(".search_read("), 1)
        self.assertEqual(script.count(".default_get("), 1)
        self.assertIn("for request in metadata_requests:", script)
        self.assertIn("for request in record_requests:", script)
        self.assertIn("with_context(active_test=False)", script)
        self.assertIn("while True:", script)
        self.assertIn("env.cr.rollback()", script)

    def test_preflight_allows_only_explicit_linked_models_outside_target_scope(
        self,
    ) -> None:
        payload = {
            "database": "odoo19_local",
            "version": "19.0",
            "models": {},
            "records": {
                "res.country": [
                    {"id": 1, "code": "FR", "name": "France"},
                ]
            },
        }
        reader = LocalOdooMetadataReader(runner=lambda *_args: _result(payload))

        _metadata, records = reader.get_preflight_snapshots(
            self.workspace_state,
            self.profile,
            (),
            (
                RecordRequest(
                    model="res.country",
                    fields=("code", "name"),
                ),
            ),
            related_models=("res.country",),
        )

        self.assertEqual(
            records.records["res.country"][0].values["code"],
            "FR",
        )
        with self.assertRaisesRegex(
            LocalOdooReaderError,
            "linked-model scope",
        ):
            reader.get_preflight_snapshots(
                self.workspace_state,
                self.profile,
                (),
                (
                    RecordRequest(
                        model="res.currency",
                        fields=("name",),
                    ),
                ),
                related_models=("res.country",),
            )

    def test_reader_exposes_no_generic_shell_or_write_surface(self) -> None:
        public = {
            name for name in dir(LocalOdooMetadataReader) if not name.startswith("_")
        }
        self.assertEqual(
            public,
            {
                "get_target_fingerprint",
                "get_model_catalog",
                "get_model_metadata",
                "get_preflight_snapshots",
                "get_company_access",
            },
        )

    def test_company_choices_and_selected_context_use_the_fixed_rolled_back_shell(self):
        calls = []
        payload = {
            "database": "odoo19_local",
            "version": "19.0",
            "login": "local-metadata@example.test",
            "default_company_name": "Fictional Group",
            "companies": [
                {"id": 3, "name": "Fictional Group"},
                {"id": 7, "name": "Fictional Factory"},
            ],
        }

        def runner(_command, script, _cwd, _timeout):
            calls.append(script)
            return _result(payload)

        reader = LocalOdooMetadataReader(runner=runner)
        access = reader.get_company_access(self.workspace_state, self.profile)
        self.assertEqual([company.company_id for company in access.choices], [3, 7])
        selected = replace(self.workspace_state, odoo_company=access.choices[1])
        reader.get_target_fingerprint(selected, self.profile)
        self.assertTrue(
            calls[1].startswith(
                "env = env(context=dict(env.context, allowed_company_ids=[7]))"
            )
        )
        self.assertIn("env.cr.rollback()", calls[1])


def _result(payload: dict[str, object]) -> LocalShellResult:
    return LocalShellResult(
        returncode=0,
        stdout=MARKER + json.dumps(payload),
        stderr="",
    )


if __name__ == "__main__":
    unittest.main()
