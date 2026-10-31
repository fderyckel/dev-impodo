"""Keep repository-owned supporting images reproducible and security-gated."""

import re
import unittest

from tests.support.paths import REPOSITORY_ROOT


class SupportingImageContractTests(unittest.TestCase):
    def test_every_compiler_and_runtime_base_is_digest_pinned(self):
        for name, runtime in (
            ("postgres", "postgres:18.6-bookworm"),
            ("caddy", "caddy:2.11.7-alpine"),
        ):
            with self.subTest(image=name):
                source = (
                    REPOSITORY_ROOT / "deploy/images" / name / "Dockerfile"
                ).read_text()
                self.assertRegex(
                    source,
                    r"ARG GO_IMAGE=golang:1\.27\.3-alpine3\.23@sha256:[0-9a-f]{64}",
                )
                self.assertRegex(
                    source,
                    r"ARG [A-Z]+_IMAGE=" + re.escape(runtime) + r"@sha256:[0-9a-f]{64}",
                )
                self.assertIn("FROM --platform=$BUILDPLATFORM ${GO_IMAGE}", source)
                self.assertIn("GOTOOLCHAIN=local", source)
                self.assertIn("go build -mod=readonly", source)

    def test_source_and_module_dependencies_are_checked(self):
        postgres = (REPOSITORY_ROOT / "deploy/images/postgres/Dockerfile").read_text()
        self.assertRegex(postgres, r"ADD --checksum=sha256:[0-9a-f]{64}")
        self.assertIn("tianon/gosu/archive/refs/tags/1.19.tar.gz", postgres)
        folder = REPOSITORY_ROOT / "deploy/images/caddy"
        module = (folder / "go.mod").read_text()
        checksums = (folder / "go.sum").read_text()
        for name, version in (
            ("github.com/caddyserver/caddy/v2", "v2.11.7"),
            ("github.com/go-jose/go-jose/v4", "v4.1.6"),
            ("golang.org/x/net", "v0.60.0"),
        ):
            with self.subTest(module=name):
                self.assertIn(name + " " + version, module)
                self.assertRegex(
                    checksums, re.escape(name + " " + version) + r" h1:[A-Za-z0-9+/=]+"
                )
        self.assertIn("go mod verify", (folder / "Dockerfile").read_text())

    def test_ci_builds_the_images_it_tests_and_keeps_scan_and_supply_chain_evidence(
        self,
    ):
        workflow = (
            REPOSITORY_ROOT / ".github/workflows/linux-server-quality.yml"
        ).read_text()
        self.assertIn("build --pull postgres proxy", workflow)
        self.assertIn("Start the disposable patched PostgreSQL server", workflow)
        self.assertIn("--scanners vuln --exit-code 1 --ignore-unfixed", workflow)
        self.assertIn("Export supporting-image provenance and SPDX SBOM", workflow)
        self.assertIn("supporting-image-evidence/sbom.spdx.json", workflow)
        self.assertNotIn('docker pull "${SUPPORTING_IMAGE}"', workflow)
