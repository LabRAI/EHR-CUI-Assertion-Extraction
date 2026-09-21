from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from collaborator_config import (  # noqa: E402
    DEFAULT_CONFIG,
    final_extraction_command,
    load_collaborator_config,
    template_config,
)
from site_profile import load_site_profile  # noqa: E402


class CollaboratorConfigTests(unittest.TestCase):
    def write_config(self, root: Path, payload: dict) -> Path:
        path = root / "run.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_template_has_validated_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            payload = template_config()
            payload["inputs"].update({
                "notes_jsonl": [str(root / "notes.jsonl.gz")],
                "medcat_modelpack": str(root / "modelpack.zip"),
                "medcat_cdb": str(root / "cdb"),
                "filter_policy": str(ROOT / "configs" / "linker_filter_policy.example.json"),
                "output_root": str(root / "outputs"),
            })
            for value in payload["inputs"]["notes_jsonl"][:]:
                Path(value).touch()
            Path(payload["inputs"]["medcat_modelpack"]).touch()
            Path(payload["inputs"]["medcat_cdb"]).mkdir()
            config = load_collaborator_config(self.write_config(root, payload), check_paths=True)
            self.assertEqual(config["hyperparameters"]["ontology"]["overlap_mode"], "add_missing")
            self.assertEqual(config["hyperparameters"]["refinement"]["strictness"], "aggressive")
            self.assertEqual(config["site_adaptation"]["mode"], "frozen_default")

    def test_unknown_parameter_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            payload = template_config()
            payload["hyperparameters"]["linker"]["unknown_distance"] = 17
            with self.assertRaises(ValueError):
                load_collaborator_config(self.write_config(Path(tmp), payload))

    def test_default_command_preserves_validated_settings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            payload = template_config()
            payload["inputs"].update({
                "notes_jsonl": [str(root / "notes.jsonl.gz")],
                "medcat_modelpack": str(root / "modelpack.zip"),
                "medcat_cdb": str(root / "cdb"),
                "filter_policy": str(ROOT / "configs" / "linker_filter_policy.example.json"),
                "output_root": str(root / "outputs"),
            })
            path = self.write_config(root, payload)
            config = load_collaborator_config(path)
            command = final_extraction_command(config, SCRIPTS, dry_run=True)
            self.assertIn("--ontology-overlap-mode", command)
            self.assertIn("add_missing", command)
            self.assertIn("--refinement-strictness", command)
            self.assertIn("aggressive", command)
            self.assertNotIn("--site-profile", command)

    def test_expanding_site_profile_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "profile.json"
            profile = {
                "schema_name": "ehr_cui_assertion_site_profile",
                "schema_version": 1,
                "adaptation_mode": "unlabeled_structural_bounded",
                "assertion_scopes": {"negation": {"before_chars": 151}},
                "measurement_scope": {},
            }
            path.write_text(json.dumps(profile), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_site_profile(path)

    def test_packaged_site_profile_example_loads(self) -> None:
        profile = load_site_profile(ROOT / "configs" / "site_profile.example.json")
        self.assertEqual(profile["assertion_scopes"]["conditional"]["before_chars"], 170)
        self.assertEqual(profile["measurement_scope"]["after_chars"], 130)

    def test_final_entrypoint_accepts_new_options(self) -> None:
        result = subprocess.run(
            [sys.executable, str(SCRIPTS / "run_final_extraction.py"), "--help"],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--ontology-overlap-mode", result.stdout)
        self.assertIn("--deployment-config-sha256", result.stdout)


if __name__ == "__main__":
    unittest.main()
