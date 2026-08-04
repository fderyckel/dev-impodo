from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from impodo.connectors import MetadataRequest, RecordRequest
from impodo.local_odoo_reader import (
    LocalOdooMetadataReader,
    LocalOdooReaderError,
    LocalShellResult,
)
from impodo.local_stack import LocalStackProfile
from impodo.projects import (
    MigrationProject,
    OdooConnectionMode,
)


ROOT = Path(__file__).resolve().parents[1]
MARKER = "__IMPODO_LOCAL_ODOO_JSON__"


class LocalOdooMetadataReaderTests(unittest.TestCase):
    def setUp(self) -> None:
        (ROOT / ".tmp").mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=ROOT / ".tmp")
        self.workspace = Path(self.temporary.name) / "odoo_ve"
        self.config = self.workspace / "config" / "odoo.conf"
        self.python = self.workspace / "venv" / "Scripts" / "python.exe"
        self.odoo_bin = self.workspace / "odoo" / "odoo-bin"
        for path in (self.config, self.python, self.odoo_bin):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()
        self.project = MigrationProject(
            project_id="project-1",
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
            self.project,
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

    def test_fields_get_captures_effective_inherited_fields_once_per_model(self) -> None:
        calls = []
        payload = {
            "database": "odoo19_local",
            "version": "19.4",
            "models": {
                "res.partner": {
                    "description": "Contact",
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
            self.project,
            self.profile,
            ("res.partner", "res.company"),
        )

        partner_fields = snapshot.models["res.partner"].fields
        self.assertEqual(set(partner_fields), {"name", "message_ids"})
        self.assertEqual(
            partner_fields["message_ids"].relation,
            "mail.message",
        )
        script = calls[0][1]
        self.assertIn(
            "requested_models = ('res.partner', 'res.company')",
            script,
        )
        self.assertEqual(script.count(".fields_get("), 1)
        self.assertIn("for model_name in requested_models:", script)
        self.assertIn("allfields=[]", script)
        self.assertIn("ensure_ascii=True", script)
        self.assertIn("env.cr.rollback()", script)

    def test_profile_must_match_the_exact_local_target(self) -> None:
        mismatched = MigrationProject(
            project_id="project-1",
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
                    "fields": {
                        "ref": {
                            "string": "Reference",
                            "type": "char",
                            "required": False,
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
            self.project,
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
        self.assertEqual(records.records["res.partner"][0].values["ref"], "P-7")
        script = calls[0][1]
        self.assertEqual(script.count(".fields_get("), 1)
        self.assertEqual(script.count(".search_read("), 1)
        self.assertIn("for request in metadata_requests:", script)
        self.assertIn("for request in record_requests:", script)
        self.assertIn("while True:", script)
        self.assertIn("env.cr.rollback()", script)

    def test_reader_exposes_no_generic_shell_or_write_surface(self) -> None:
        public = {
            name
            for name in dir(LocalOdooMetadataReader)
            if not name.startswith("_")
        }
        self.assertEqual(
            public,
            {
                "get_target_fingerprint",
                "get_model_catalog",
                "get_model_metadata",
                "get_preflight_snapshots",
            },
        )


def _result(payload: dict[str, object]) -> LocalShellResult:
    return LocalShellResult(
        returncode=0,
        stdout=MARKER + json.dumps(payload),
        stderr="",
    )


if __name__ == "__main__":
    unittest.main()
