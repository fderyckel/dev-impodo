from __future__ import annotations

import shutil
import unittest
from pathlib import Path
from uuid import uuid4

from impodo.adapters.scenarios import ScenarioLoadError, load_scenario


SCENARIO = """\
contract_version: 1
scenario_id: contact-read-only
purpose: PULL_REQUEST
source:
  mode: FILE
  fixture_set: fixtures/v1
  fixture_hash: sha256:183046c91ff74d8fe3143b47605ed852e9ecb34e8df3a0c4d47455bcd414010e
rules:
  profile: profile.yaml
  profile_hash: sha256:35b2af1aeebcb38fa92d512255356405461ecccec47b019ae9c7237ca8c28c68
destination:
  mode: LOCAL_ODOO
  target_profile: local.contacts
  expected_seed: empty-contacts
execution:
  stop_after: FIRST_COMPARISON
  write_policy: READ_ONLY
expectations:
  prepared_rows: 1
  first_comparison:
    create: 1
    update: 0
    unchanged: 0
    blocked: 0
    ambiguous: 0
"""


class ScenarioLoaderTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary_root = Path(__file__).resolve().parents[3] / ".tmp"
        temporary_root.mkdir(exist_ok=True)
        self.root = temporary_root / f"scenario-loader-{uuid4()}"
        self.root.mkdir()
        fixture = self.root / "fixtures" / "v1"
        fixture.mkdir(parents=True)
        (fixture / "contacts.csv").write_text(
            "reference,name\nC001,Example\n",
            encoding="utf-8",
            newline="\n",
        )
        (self.root / "profile.yaml").write_text(
            "profile:\n  id: example\ndatasets: []\n",
            encoding="utf-8",
            newline="\n",
        )
        self.definition = self.root / "scenario.yaml"
        self.definition.write_text(SCENARIO, encoding="utf-8", newline="\n")

    def tearDown(self) -> None:
        shutil.rmtree(self.root)

    def test_loads_and_hashes_contained_fixture(self) -> None:
        loaded = load_scenario(self.definition)

        self.assertEqual(loaded.definition.scenario_id, "contact-read-only")
        self.assertEqual(loaded.fixture_file_count, 1)
        self.assertGreater(loaded.fixture_bytes, 0)
        self.assertRegex(loaded.fixture_hash, r"^sha256:[0-9a-f]{64}$")
        self.assertRegex(loaded.scenario_hash, r"^sha256:[0-9a-f]{64}$")

    def test_changed_fixture_is_rejected_as_mutable_drift(self) -> None:
        load_scenario(self.definition)
        (self.root / "fixtures" / "v1" / "contacts.csv").write_text(
            "reference,name\nC001,Changed\n",
            encoding="utf-8",
            newline="\n",
        )

        with self.assertRaisesRegex(ScenarioLoadError, "fixture bytes"):
            load_scenario(self.definition)

    def test_changed_profile_is_rejected_as_mutable_drift(self) -> None:
        (self.root / "profile.yaml").write_text(
            "profile:\n  id: changed\ndatasets: []\n",
            encoding="utf-8",
            newline="\n",
        )

        with self.assertRaisesRegex(ScenarioLoadError, "profile bytes"):
            load_scenario(self.definition)

    def test_rejects_secret_property_before_domain_validation(self) -> None:
        self.definition.write_text(
            SCENARIO + "api_key: do-not-store-this\n",
            encoding="utf-8",
            newline="\n",
        )

        with self.assertRaisesRegex(ScenarioLoadError, "cannot contain credentials"):
            load_scenario(self.definition)

    def test_rejects_missing_referenced_file(self) -> None:
        (self.root / "profile.yaml").unlink()

        with self.assertRaisesRegex(ScenarioLoadError, "does not exist"):
            load_scenario(self.definition)


if __name__ == "__main__":
    unittest.main()
