from __future__ import annotations

import argparse
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import apply_assertion_scope_overlay as assertion  # noqa: E402
import build_site_profile as builder  # noqa: E402
import compare_adaptive_metrics as metric_gate  # noqa: E402
import extract_medcat_candidates as extraction  # noqa: E402
from site_profile import (  # noqa: E402
    DEFAULT_ASSERTION_SCOPES,
    DEFAULT_MEASUREMENT_SCOPE,
    SCHEMA_NAME,
    SCHEMA_VERSION,
    load_site_profile,
)


def candidate(text: str, span: str, category: str = "disease_or_diagnosis") -> dict[str, object]:
    return {
        "span": span,
        "evidence": text,
        "evidence_context": text,
        "filter_action": "keep_candidate",
        "concept_category_preview": category,
        "assertion_status_preview": "present",
    }


class SiteProfileValidationTests(unittest.TestCase):
    def test_no_profile_is_exact_frozen_default(self) -> None:
        profile = load_site_profile(None)
        self.assertEqual(profile["assertion_scopes"], DEFAULT_ASSERTION_SCOPES)
        self.assertEqual(profile["measurement_scope"], DEFAULT_MEASUREMENT_SCOPE)
        self.assertEqual(profile["adaptation_mode"], "frozen_default")

    def test_invalid_profile_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "invalid.json"
            path.write_text(json.dumps({
                "schema_name": SCHEMA_NAME,
                "schema_version": SCHEMA_VERSION,
                "assertion_scopes": {"negation": {"before_chars": 10000}},
                "measurement_scope": {},
            }), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_site_profile(path)

    def test_external_profile_cannot_claim_frozen_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "misleading.json"
            path.write_text(json.dumps({
                "schema_name": SCHEMA_NAME,
                "schema_version": SCHEMA_VERSION,
                "adaptation_mode": "frozen_default",
                "assertion_scopes": {},
                "measurement_scope": {},
            }), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_site_profile(path)


class AssertionAdaptationTests(unittest.TestCase):
    def tearDown(self) -> None:
        assertion.configure_site_profile(None)

    def test_frozen_harvard_conditional_example_stays_present(self) -> None:
        row = candidate(
            "Continue intermittent hemodialysis and consider CRRT if hemodynamically unable to tolerate.",
            "intermittent hemodialysis",
            "procedure",
        )
        output, _changed = assertion.overlay_row(row)
        self.assertEqual(output["assertion_status_preview"], "present")

    def test_clause_boundary_prevents_negation_leakage(self) -> None:
        row = candidate("No murmur. S1 heart sound is normal.", "S1 heart sound", "finding")
        output, _changed = assertion.overlay_row(row)
        self.assertEqual(output["assertion_status_preview"], "present")

    def test_tight_profile_blocks_distant_negation(self) -> None:
        text = "No evidence of acute symptoms with repeated review and stable examination showing pneumonia"
        frozen, _changed = assertion.overlay_row(candidate(text, "pneumonia"))
        self.assertEqual(frozen["assertion_status_preview"], "absent")

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "site_profile.json"
            path.write_text(json.dumps({
                "schema_name": SCHEMA_NAME,
                "schema_version": SCHEMA_VERSION,
                "adaptation_mode": "unlabeled_structural_bounded",
                "assertion_scopes": {
                    "negation": {
                        "before_chars": 45,
                        "after_chars": 54,
                        "before_gap_chars": 20,
                        "after_gap_chars": 12,
                    }
                },
                "measurement_scope": DEFAULT_MEASUREMENT_SCOPE,
            }), encoding="utf-8")
            assertion.configure_site_profile(path)
            adapted, _changed = assertion.overlay_row(candidate(text, "pneumonia"))
        self.assertEqual(adapted["assertion_status_preview"], "present")


class MeasurementAdaptationTests(unittest.TestCase):
    def test_measurement_window_can_tighten_without_changing_default(self) -> None:
        context = "glucose" + (" " * 70) + "180 mg/dL"
        frozen = extraction.parse_lab_value("glucose", "Glucose", context, 0, len("glucose"))
        tightened = extraction.parse_lab_value(
            "glucose",
            "Glucose",
            context,
            0,
            len("glucose"),
            local_before=60,
            local_after=40,
        )
        self.assertEqual(frozen["numeric_value"], "180")
        self.assertIsNone(tightened["numeric_value"])


class PrivacyTests(unittest.TestCase):
    def test_built_profile_contains_only_aggregates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            notes = root / "notes.jsonl"
            candidates = root / "candidates.jsonl"
            notes.write_text(json.dumps({
                "note_id": "PRIVATE-1",
                "text": "Assessment:\nNo secret-pneumonia.\nGlucose: 180 mg/dL",
            }) + "\n", encoding="utf-8")
            candidates.write_text(json.dumps({
                "span": "secret-pneumonia",
                "evidence": "No secret-pneumonia",
                "evidence_context": "No secret-pneumonia",
                "filter_action": "keep_candidate",
                "concept_category_preview": "disease_or_diagnosis",
            }) + "\n", encoding="utf-8")
            args = argparse.Namespace(
                notes_jsonl_gz=[str(notes)],
                candidate_jsonl=[str(candidates)],
                output=str(root / "site_profile.json"),
                quantile=0.95,
                minimum_observations=1,
                max_notes=0,
                allow_no_slurm=True,
            )
            profile = builder.build_profile(args)
            serialized = json.dumps(profile).lower()
            self.assertNotIn("secret", serialized)
            self.assertNotIn("pneumonia", serialized)
            self.assertNotIn("private-1", serialized)
            self.assertEqual(profile["aggregate_diagnostics"]["layout"]["notes"], 1)


class MetricGateTests(unittest.TestCase):
    def test_gate_fails_on_any_regression(self) -> None:
        baseline = {
            "entity_exact": {"f1": 0.8, "precision": 0.8, "recall": 0.8},
            "entity_relaxed": {"f1": 0.9, "precision": 0.9, "recall": 0.9},
            "assertion_on_relaxed_matches": {
                "macro_f1_supported": 0.85,
                "accuracy": 0.9,
                "per_label": {"present": {"f1": 0.9, "support": 10}},
            },
            "value_field_metrics": {
                "bundle_accuracy": 0.7,
                "category_accuracy": 0.9,
                "value_status_accuracy": 0.8,
                "measurement_context_accuracy": 0.9,
                "numeric_value_accuracy": 0.9,
                "unit_accuracy": 0.95,
            },
        }
        adaptive = json.loads(json.dumps(baseline))
        adaptive["assertion_on_relaxed_matches"]["macro_f1_supported"] = 0.84
        result = metric_gate.compare(baseline, adaptive, 1e-12)
        self.assertFalse(result["passed"])
        failed = [row["metric"] for row in result["metrics"] if not row["passed"]]
        self.assertEqual(failed, ["assertion_macro_f1"])


if __name__ == "__main__":
    unittest.main()
