from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "run_harvard_default.py"


class HarvardDefaultWrapperTests(unittest.TestCase):
    def test_missing_asset_is_a_single_actionable_error(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--notes-jsonl-gz",
                str(ROOT / "examples" / "toy_notes.jsonl"),
                "--modelpack",
                "/tmp/does-not-exist-medcat.zip",
                "--cdb-path",
                "/tmp/does-not-exist-cdb",
                "--allow-no-slurm",
                "--dry-run",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("MedCAT modelpack does not exist", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_dry_run_uses_fixed_preset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            modelpack = root / "modelpack.zip"
            cdb = root / "cdb"
            modelpack.touch()
            cdb.mkdir()
            output = root / "output"
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--notes-jsonl-gz",
                    str(ROOT / "examples" / "toy_notes.jsonl"),
                    "--modelpack",
                    str(modelpack),
                    "--cdb-path",
                    str(cdb),
                    "--output-root",
                    str(output),
                    "--allow-no-slurm",
                    "--dry-run",
                ],
                check=False,
                capture_output=True,
                text=True,
                env={**os.environ, "SLURM_JOB_ID": "test-wrapper"},
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            config = json.loads((output / "harvard_default.run_manifest.json").read_text())
            command = " ".join(config["command"])
            self.assertIn("--output-mode all_non_rejected", command)
            self.assertIn("--medcat-batch-size-chars 1000000", command)
            self.assertNotIn("site-profile", command)


if __name__ == "__main__":
    unittest.main()
