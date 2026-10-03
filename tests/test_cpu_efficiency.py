from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import cached_regex
import extract_medcat_candidates as extraction
import run_final_extraction as pipeline


class CPUEfficiencyTests(unittest.TestCase):
    def test_pipeline_pins_subprocess_hash_seed_and_honors_override(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(pipeline.subprocess, "run") as run:
            pipeline.run([sys.executable, "--version"])
            self.assertEqual(run.call_args.kwargs["env"]["PYTHONHASHSEED"], "20260610")
        with patch.dict(os.environ, {"PYTHONHASHSEED": "7"}), patch.object(pipeline.subprocess, "run") as run:
            pipeline.run([sys.executable, "--version"])
            self.assertEqual(run.call_args.kwargs["env"]["PYTHONHASHSEED"], "7")

    def test_regex_semantics_survive_cache_eviction(self):
        cases = [
            (r"\bno\s+(\w+)", "No chest pain; no fever", re.I),
            (r"(?P<n>\d{1,3}(?:,\d{3})*(?:\.\d+)?)", "CA19-9: 1,041 (H)", 0),
            (r"^S[12]", "Normal\nS1 heart sound", re.M),
            (re.compile(r"\bif\b", re.I), "Continue HD; consider CRRT if needed", 0),
        ]
        for iteration in range(2):
            if iteration:
                for i in range(16390):
                    cached_regex.compile(f"unique_pattern_{i}")
            for pattern, text, flags in cases:
                expected = [(m.span(), m.groups(), m.groupdict()) for m in re.finditer(pattern, text, flags)]
                actual = [(m.span(), m.groups(), m.groupdict()) for m in cached_regex.finditer(pattern, text, flags)]
                self.assertEqual(expected, actual)
                self.assertEqual(re.sub(pattern, "X", text, flags=flags),
                                 cached_regex.sub(pattern, "X", text, flags=flags))
                self.assertEqual(re.split(pattern, text, flags=flags),
                                 cached_regex.split(pattern, text, flags=flags))
        self.assertLessEqual(cached_regex.compile.cache_info().currsize, 16384)
        with self.assertRaises(ValueError):
            cached_regex.search(re.compile("x"), "x", re.I)

    def test_alias_callers_cannot_mutate_cached_lists(self):
        before = extraction.aliases_for_lab("Na", "Sodium")
        extraction.aliases_for_lab("Na", "Sodium").clear()
        self.assertEqual(before, extraction.aliases_for_lab("Na", "Sodium"))
        before = extraction.all_known_lab_aliases()
        extraction.all_known_lab_aliases().clear()
        self.assertEqual(before, extraction.all_known_lab_aliases())

    def test_killed_worker_fails_instead_of_hanging(self):
        code = r'''
import json, os, sys, types
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, sys.argv[1])
import extract_medcat_candidates_prefork4 as runner
root = Path(sys.argv[2])
config = root / 'config.json'
config.write_text(json.dumps({'task_count': 1, 'variant_name': 'failure_test'}))
runner.parse_args = lambda: SimpleNamespace(workers=1, allow_no_slurm=False,
    config=str(config), modelpack=str(config), filter_policy=str(config),
    output_root=str(root / 'output'))
runner.base.load_policy = lambda _: {}
runner.base.prepare_policy_runtime = lambda _: {}
runner.prepare_tasks = lambda *args: {'killed_task': {}}
fake = types.ModuleType('medcat.cat')
fake.CAT = SimpleNamespace(load_model_pack=lambda _: object())
sys.modules['medcat.cat'] = fake
def die(_):
    os._exit(17)
runner.process_task = die
runner.main()
'''
        with tempfile.TemporaryDirectory() as tmp:
            proc = subprocess.run([sys.executable, "-c", code, str(SCRIPTS), tmp],
                                  capture_output=True, text=True, timeout=30,
                                  env={**os.environ, "SLURM_JOB_ID": "worker-failure-test"})
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("BrokenProcessPool", proc.stderr)


if __name__ == "__main__":
    unittest.main()
