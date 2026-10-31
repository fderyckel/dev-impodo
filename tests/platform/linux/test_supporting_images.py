"""Exercise the rebuilt helpers and proxy as Linux non-root container processes."""

import shutil
import subprocess
import unittest

import yaml

from tests.support.paths import REPOSITORY_ROOT


class SupportingImageRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.runner = shutil.which("docker")
        if self.runner is None:
            raise RuntimeError("Supporting-image qualification requires Docker")
        self.services = yaml.safe_load(
            (REPOSITORY_ROOT / "deploy/compose.yaml").read_text()
        )["services"]

    def run_image(
        self, service, entrypoint, *arguments, user="10001:10001", capabilities=()
    ):
        command = [
            self.runner,
            "run",
            "--rm",
            "--pull=never",
            "--network=none",
            "--read-only",
            "--user",
            user,
            "--cap-drop=ALL",
        ]
        for capability in capabilities:
            command.extend(("--cap-add", capability))
        command.extend(
            ("--entrypoint", entrypoint, self.services[service]["image"], *arguments)
        )
        return subprocess.run(
            command, check=True, capture_output=True, text=True, timeout=60
        ).stdout

    def test_gosu_preserves_uid_drop_with_the_patched_compiler(self):
        version = self.run_image("postgres", "gosu", "--version")
        self.assertIn("1.19 (go1.27.3 on linux/", version)
        dropped = self.run_image(
            "postgres",
            "gosu",
            "nobody",
            "id",
            "-u",
            user="0:0",
            capabilities=("SETUID", "SETGID"),
        )
        self.assertEqual(dropped.strip(), "65534")

    def test_non_root_proxy_keeps_required_modules_and_patched_dependencies(self):
        self.assertIn("v2.11.7", self.run_image("proxy", "caddy", "version"))
        build = self.run_image("proxy", "caddy", "build-info")
        self.assertIn("go1.27.3", build)
        self.assertRegex(build, r"github\.com/go-jose/go-jose/v4\s+v4\.1\.6\b")
        self.assertRegex(build, r"golang\.org/x/net\s+v0\.60\.0\b")
        modules = self.run_image("proxy", "caddy", "list-modules")
        for required in (
            "http.handlers.reverse_proxy",
            "http.handlers.request_body",
            "tls.issuance.internal",
            "http.encoders.gzip",
            "http.encoders.zstd",
        ):
            self.assertIn(required, modules.splitlines())
        self.assertIn("curl 8.23.0", self.run_image("proxy", "curl", "--version"))
