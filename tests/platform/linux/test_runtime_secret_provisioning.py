"""Exercise secret-volume upgrades and non-root access in real Linux containers."""

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

import yaml

from deploy.local_rehearsal import prepare_local_secrets
from tests.support.paths import REPOSITORY_ROOT


class RuntimeSecretProvisioningLiveTests(unittest.TestCase):
    def test_old_runtime_password_is_removed_and_bootstrap_has_a_private_mount(self):
        runner = shutil.which("docker")
        if runner is None:
            raise RuntimeError(
                "Linux secret-provisioning qualification requires Docker"
            )
        image = yaml.safe_load((REPOSITORY_ROOT / "deploy/compose.yaml").read_text())[
            "services"
        ]["volume-provisioner"]["image"]
        prefix = "impodo-secret-test-" + uuid4().hex
        runtime, bootstrap = prefix + "-runtime", prefix + "-bootstrap"

        def docker(*arguments):
            return subprocess.run(
                [runner, *arguments],
                check=True,
                capture_output=True,
                text=True,
                timeout=60,
            ).stdout.strip()

        try:
            docker("volume", "create", runtime)
            docker("volume", "create", bootstrap)
            with tempfile.TemporaryDirectory(
                prefix="impodo-secret-provision-"
            ) as directory:
                prepare_local_secrets(Path(directory))
                script = (
                    "set -eu; mkdir -p /run/impodo-runtime-secrets; "
                    "printf fictional-old-password > /run/impodo-runtime-secrets/platform-admin-password; "
                    "sh /opt/impodo/provision-runtime-secrets.sh; "
                    "sh /opt/impodo/provision-runtime-secrets.sh; "
                    "test ! -e /run/impodo-runtime-secrets/platform-admin-password; "
                    "cmp /run/secrets/platform-admin-password /run/impodo-bootstrap-secrets/platform-admin-password; "
                    "stat -c '%u %g %a' /run/impodo-bootstrap-secrets/platform-admin-password"
                )
                output = docker(
                    "run",
                    "--rm",
                    "--pull=never",
                    "--network=none",
                    "--user=0:0",
                    "--entrypoint=/bin/sh",
                    "--volume",
                    runtime + ":/run/impodo-runtime-secrets",
                    "--volume",
                    bootstrap + ":/run/impodo-bootstrap-secrets",
                    "--volume",
                    directory + ":/run/secrets:ro",
                    "--volume",
                    str(
                        REPOSITORY_ROOT
                        / "deploy/local-rehearsal/provision-runtime-secrets.sh"
                    )
                    + ":/opt/impodo/provision-runtime-secrets.sh:ro",
                    "--volume",
                    str(REPOSITORY_ROOT / "deploy/postgres/init/010-impodo-roles.sh")
                    + ":/opt/impodo/010-impodo-roles.sh:ro",
                    image,
                    "-c",
                    script,
                )
                self.assertEqual(output, "0 10001 440")
            docker(
                "run",
                "--rm",
                "--pull=never",
                "--network=none",
                "--user=10001:10001",
                "--entrypoint=/bin/sh",
                "--volume",
                runtime + ":/run/secrets/impodo:ro",
                image,
                "-c",
                "test -r /run/secrets/impodo/postgres-runtime-dsn && test ! -e /run/secrets/impodo/platform-admin-password",
            )
        finally:
            subprocess.run(
                [runner, "volume", "rm", "--force", runtime, bootstrap],
                check=False,
                capture_output=True,
                timeout=30,
            )
