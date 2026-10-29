"""Protect the single-image, fail-closed Linux deployment baseline."""

from __future__ import annotations

import tempfile
import unittest
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from urllib.parse import unquote, urlsplit

import yaml

from deploy.local_rehearsal import (
    SECRET_FILENAMES,
    prepare_identifier_digest_key,
    prepare_local_secrets,
    prepare_platform_admin_secret,
)
from impodo.web.composition.server_cli import (
    CONFIGURATION_ERROR_EXIT,
    main,
)
from tests.support.paths import REPOSITORY_ROOT

ROOT = REPOSITORY_ROOT
DOCKERFILE = ROOT / "deploy" / "Dockerfile"
COMPOSE_FILE = ROOT / "deploy" / "compose.yaml"
PROXY_CONFIG = ROOT / "deploy" / "proxy" / "Caddyfile"
POSTGRES_ROLE_INIT = ROOT / "deploy" / "postgres" / "init" / "010-impodo-roles.sh"
SECRET_PROVISIONER = (
    ROOT / "deploy" / "local-rehearsal" / "provision-runtime-secrets.sh"
)
LINUX_SERVER_WORKFLOW = (
    ROOT / ".github" / "workflows" / "linux-server-quality.yml"
)


def _compose() -> dict[str, object]:
    payload = yaml.safe_load(COMPOSE_FILE.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise TypeError("deploy/compose.yaml must contain one mapping")
    return payload


class LinuxContainerContractTests(unittest.TestCase):
    def test_image_is_pinned_and_installed_from_the_repository_lock(self) -> None:
        dockerfile = DOCKERFILE.read_text(encoding="utf-8")

        self.assertRegex(
            dockerfile,
            r"ARG PYTHON_IMAGE=python:3\.12\.\d+-slim-bookworm@sha256:[0-9a-f]{64}",
        )
        self.assertEqual(dockerfile.count("FROM ${PYTHON_IMAGE}"), 2)
        self.assertIn(
            "uv sync --frozen --no-dev --no-editable --no-cache",
            dockerfile,
        )

    def test_runtime_is_non_root_with_only_named_writable_paths(self) -> None:
        dockerfile = DOCKERFILE.read_text(encoding="utf-8")

        self.assertIn("USER 10001:10001", dockerfile)
        self.assertIn("&& chmod -R a-w /opt/impodo/venv", dockerfile)
        self.assertNotIn("VOLUME [", dockerfile)
        self.assertNotIn("IMPODO_ARTIFACT_ROOT=", dockerfile)
        self.assertNotIn("IMPODO_RUNTIME_ROOT=", dockerfile)
        for path in (
            "/var/lib/impodo/artifacts",
            "/var/lib/impodo/runtime",
            "/var/tmp/impodo",
        ):
            self.assertIn(path, dockerfile)

    def test_build_context_is_an_explicit_package_allowlist(self) -> None:
        entries = tuple(
            line.strip()
            for line in (ROOT / ".dockerignore")
            .read_text(encoding="utf-8")
            .splitlines()
            if line.strip()
        )

        self.assertEqual(entries[0], "**")
        self.assertEqual(
            set(entries[1:]),
            {
                "!pyproject.toml",
                "!uv.lock",
                "!README.md",
                "!LICENSE",
                "!src/",
                "!src/**",
                "!deploy/",
                "!deploy/Dockerfile",
            },
        )

    def test_ci_builds_scans_and_exports_supply_chain_evidence(self) -> None:
        workflow = LINUX_SERVER_WORKFLOW.read_text(encoding="utf-8")

        self.assertIn("image-quality:", workflow)
        for build_input in (
            ".dockerignore",
            "LICENSE",
            "README.md",
            "pyproject.toml",
            "uv.lock",
            "deploy/**",
            "src/**",
        ):
            with self.subTest(build_input=build_input):
                self.assertIn(f"- {build_input}", workflow)

        action_references = tuple(
            line.split("uses:", maxsplit=1)[1].split("#", maxsplit=1)[0].strip()
            for line in workflow.splitlines()
            if "uses:" in line
        )
        self.assertTrue(action_references)
        for action_reference in action_references:
            with self.subTest(action_reference=action_reference):
                self.assertRegex(action_reference, r"^[^@\s]+@[0-9a-f]{40}$")

        self.assertRegex(
            workflow,
            r"SBOM_GENERATOR: [^\s]+@sha256:[0-9a-f]{64}",
        )
        self.assertRegex(
            workflow,
            r"TRIVY_IMAGE: [^\s]+@sha256:[0-9a-f]{64}",
        )
        for required_contract in (
            "--pull",
            "--scanners vuln",
            "--exit-code 1",
            "--ignore-unfixed",
            "--severity CRITICAL,HIGH",
            "--provenance=mode=max",
            '--sbom="generator=${SBOM_GENERATOR}"',
            "provenance.json",
            "sbom.spdx.json",
            "if-no-files-found: error",
        ):
            with self.subTest(required_contract=required_contract):
                self.assertIn(required_contract, workflow)

        self.assertNotIn("docker login", workflow.lower())
        self.assertNotIn("secrets.", workflow)
        self.assertNotIn("--push", workflow)

    def test_one_entry_point_exposes_explicit_fail_closed_roles(self) -> None:
        project = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        dockerfile = DOCKERFILE.read_text(encoding="utf-8")

        self.assertIn(
            'impodo-server = "impodo.web.composition.server_cli:main"',
            project,
        )
        self.assertIn('ENTRYPOINT ["impodo-server"]', dockerfile)
        self.assertIn('CMD ["web"]', dockerfile)
        self.assertNotIn("impodo.web.launcher", dockerfile)

        for command in ("web", "worker", "migrate", "admin"):
            with self.subTest(command=command):
                error = StringIO()
                with redirect_stderr(error):
                    result = main((command,))
                self.assertEqual(result, CONFIGURATION_ERROR_EXIT)
                self.assertIn(f"{command} startup refused", error.getvalue())

    def test_compose_defines_the_complete_empty_private_topology(self) -> None:
        compose = _compose()
        services = compose["services"]
        self.assertIsInstance(services, dict)
        assert isinstance(services, dict)

        self.assertEqual(
            set(services),
            {
                "postgres",
                "volume-provisioner",
                "migrate",
                "web",
                "worker",
                "admin-bootstrap",
                "proxy",
            },
        )
        self.assertNotIn("ports", services["postgres"])
        self.assertNotIn("ports", services["web"])
        self.assertNotIn("ports", services["worker"])
        self.assertEqual(
            services["proxy"]["ports"],
            ["127.0.0.1:${IMPODO_PROXY_PORT:-8443}:8443"],
        )
        self.assertEqual(services["worker"]["profiles"], ["worker"])
        self.assertEqual(services["worker"]["restart"], "no")
        self.assertEqual(services["admin-bootstrap"]["profiles"], ["admin"])
        self.assertEqual(services["admin-bootstrap"]["restart"], "no")
        self.assertEqual(
            {
                service_name
                for service_name, service in services.items()
                if "build" in service
            },
            {"migrate"},
        )
        self.assertEqual(
            {
                services[name]["image"]
                for name in ("migrate", "web", "worker", "admin-bootstrap")
            },
            {"impodo-server:local"},
        )

        networks = compose["networks"]
        self.assertTrue(networks["application"]["internal"])
        self.assertTrue(networks["control"]["internal"])
        self.assertEqual(
            services["web"]["environment"]["IMPODO_TRUSTED_PROXY_CIDRS"],
            "172.30.10.2/32",
        )
        self.assertEqual(
            services["web"]["environment"]["IMPODO_ODOO_EGRESS_MODE"],
            "disabled",
        )
        proxy_config = PROXY_CONFIG.read_text(encoding="utf-8")
        self.assertIn("tls internal", proxy_config)
        self.assertIn("reverse_proxy web:8000", proxy_config)

    def test_compose_uses_pinned_images_and_fail_closed_runtime_security(
        self,
    ) -> None:
        services = _compose()["services"]
        assert isinstance(services, dict)

        for service_name in ("postgres", "volume-provisioner", "proxy"):
            with self.subTest(service=service_name):
                self.assertRegex(
                    services[service_name]["image"],
                    r"^[a-z0-9./:-]+@sha256:[0-9a-f]{64}$",
                )
        self.assertEqual(
            services["postgres"]["healthcheck"]["test"][0],
            "CMD-SHELL",
        )
        self.assertEqual(
            services["migrate"]["depends_on"]["postgres"]["condition"],
            "service_healthy",
        )
        self.assertEqual(
            services["postgres"]["depends_on"]["volume-provisioner"]["condition"],
            "service_completed_successfully",
        )
        self.assertIn(
            "impodo-postgres-init:/docker-entrypoint-initdb.d:ro",
            services["postgres"]["volumes"],
        )
        self.assertFalse(
            any(
                str(volume).startswith("./postgres/init/")
                for volume in services["postgres"]["volumes"]
            )
        )
        self.assertEqual(
            services["web"]["depends_on"]["migrate"]["condition"],
            "service_completed_successfully",
        )
        self.assertEqual(services["proxy"]["healthcheck"]["test"][1], "curl")
        self.assertEqual(services["proxy"]["cap_drop"], ["ALL"])
        self.assertEqual(services["proxy"]["cap_add"], ["NET_BIND_SERVICE"])
        self.assertIn("no-new-privileges:true", services["proxy"]["security_opt"])

        for service_name in ("migrate", "web", "worker", "admin-bootstrap"):
            with self.subTest(service=service_name):
                service = services[service_name]
                self.assertEqual(service["user"], "10001:10001")
                self.assertTrue(service["read_only"])
                self.assertEqual(service["cap_drop"], ["ALL"])
                self.assertIn("no-new-privileges:true", service["security_opt"])

    def test_compose_uses_file_references_without_committed_secret_values(
        self,
    ) -> None:
        compose_text = COMPOSE_FILE.read_text(encoding="utf-8")
        compose = _compose()
        secrets_config = compose["secrets"]
        self.assertIsInstance(secrets_config, dict)
        assert isinstance(secrets_config, dict)

        self.assertNotIn("postgresql://", compose_text)
        self.assertEqual(
            set(secrets_config),
            {
                "postgres-bootstrap-password",
                "postgres-runtime-password",
                "postgres-migration-password",
                "postgres-runtime-dsn",
                "postgres-migration-dsn",
                "platform-admin-password",
                "identifier-digest-key",
            },
        )
        for value in secrets_config.values():
            self.assertIsInstance(value, dict)
            assert isinstance(value, dict)
            self.assertTrue(str(value["file"]).startswith("./.local/secrets/"))

        role_init = POSTGRES_ROLE_INIT.read_text(encoding="utf-8")
        self.assertIn("CREATE ROLE impodo_runtime LOGIN NOSUPERUSER", role_init)
        self.assertIn("CREATE ROLE impodo_migrator LOGIN NOSUPERUSER", role_init)
        self.assertIn("GRANT CREATE ON DATABASE %I TO impodo_migrator", role_init)
        self.assertNotIn("SUPERUSER PASSWORD", role_init)

        provisioner = SECRET_PROVISIONER.read_text(encoding="utf-8")
        self.assertEqual(provisioner.count("-o 0 -g 10001 -m 0440"), 4)
        self.assertIn('postgres_uid="$(id -u postgres)"', provisioner)
        self.assertEqual(provisioner.count("-m 0400 \\"), 3)
        self.assertIn("-o 0 -g 0 -m 0644", provisioner)
        self.assertIn("/var/lib/impodo/artifacts", provisioner)
        self.assertIn("/data \\", provisioner)

        volume_provisioner = compose["services"]["volume-provisioner"]
        self.assertEqual(
            set(volume_provisioner["secrets"]),
            set(secrets_config),
        )
        self.assertIn(
            "impodo-postgres-secrets:/run/impodo-postgres-secrets",
            volume_provisioner["volumes"],
        )
        self.assertIn(
            "impodo-postgres-init:/run/impodo-postgres-init",
            volume_provisioner["volumes"],
        )
        self.assertEqual(
            compose["services"]["migrate"]["volumes"],
            ["impodo-migration-secrets:/run/secrets/impodo:ro"],
        )
        self.assertIn(
            "impodo-runtime-secrets:/run/secrets/impodo:ro",
            compose["services"]["web"]["volumes"],
        )
        self.assertEqual(
            compose["services"]["admin-bootstrap"]["volumes"],
            ["impodo-runtime-secrets:/run/secrets/impodo:ro"],
        )
        self.assertNotIn(
            "impodo-migration-secrets:/run/secrets/impodo:ro",
            compose["services"]["web"]["volumes"],
        )

        ignore_rules = (ROOT / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("/deploy/.local/", ignore_rules.splitlines())

    def test_local_secret_preparation_is_consistent_private_and_no_overwrite(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            secret_directory = Path(temporary_directory) / "secrets"
            created = prepare_local_secrets(secret_directory)

            self.assertEqual(tuple(path.name for path in created), SECRET_FILENAMES)
            runtime_password = (
                (secret_directory / "postgres-runtime-password")
                .read_text(encoding="utf-8")
                .strip()
            )
            migration_password = (
                (secret_directory / "postgres-migration-password")
                .read_text(encoding="utf-8")
                .strip()
            )
            runtime_dsn = urlsplit(
                (secret_directory / "postgres-runtime-dsn")
                .read_text(encoding="utf-8")
                .strip()
            )
            migration_dsn = urlsplit(
                (secret_directory / "postgres-migration-dsn")
                .read_text(encoding="utf-8")
                .strip()
            )

            self.assertEqual(runtime_dsn.username, "impodo_runtime")
            self.assertEqual(unquote(runtime_dsn.password or ""), runtime_password)
            self.assertEqual(migration_dsn.username, "impodo_migrator")
            self.assertEqual(
                unquote(migration_dsn.password or ""),
                migration_password,
            )
            with self.assertRaises(FileExistsError):
                prepare_local_secrets(secret_directory)

    def test_older_rehearsal_can_add_only_the_missing_admin_secret(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            secret_directory = Path(temporary_directory) / "secrets"
            created = prepare_local_secrets(secret_directory)
            admin_secret = secret_directory / "platform-admin-password"
            original_runtime_dsn = (
                secret_directory / "postgres-runtime-dsn"
            ).read_bytes()
            admin_secret.unlink()

            added = prepare_platform_admin_secret(secret_directory)

            self.assertEqual(added, admin_secret)
            self.assertTrue(added.read_text(encoding="utf-8").strip())
            self.assertEqual(
                (secret_directory / "postgres-runtime-dsn").read_bytes(),
                original_runtime_dsn,
            )
            self.assertEqual(len(created), len(SECRET_FILENAMES))
            with self.assertRaises(FileExistsError):
                prepare_platform_admin_secret(secret_directory)

    def test_older_rehearsal_can_add_only_the_identifier_digest_key(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            secret_directory = Path(temporary_directory) / "secrets"
            prepare_local_secrets(secret_directory)
            digest_key = secret_directory / "identifier-digest-key"
            original_runtime_dsn = (
                secret_directory / "postgres-runtime-dsn"
            ).read_bytes()
            digest_key.unlink()

            added = prepare_identifier_digest_key(secret_directory)

            self.assertEqual(added, digest_key)
            self.assertGreaterEqual(len(added.read_text(encoding="utf-8").strip()), 32)
            self.assertEqual(
                (secret_directory / "postgres-runtime-dsn").read_bytes(),
                original_runtime_dsn,
            )
            with self.assertRaises(FileExistsError):
                prepare_identifier_digest_key(secret_directory)


if __name__ == "__main__":
    unittest.main()
