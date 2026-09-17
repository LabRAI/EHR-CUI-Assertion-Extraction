#!/usr/bin/env python3
"""Fail closed when a site-adaptive evaluation regresses core metrics."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


METRICS = {
    "entity_exact_f1": ("entity_exact", "f1"),
    "entity_exact_precision": ("entity_exact", "precision"),
    "entity_exact_recall": ("entity_exact", "recall"),
    "entity_relaxed_f1": ("entity_relaxed", "f1"),
    "entity_relaxed_precision": ("entity_relaxed", "precision"),
    "entity_relaxed_recall": ("entity_relaxed", "recall"),
    "assertion_macro_f1": ("assertion_on_relaxed_matches", "macro_f1_supported"),
    "assertion_accuracy": ("assertion_on_relaxed_matches", "accuracy"),
    "value_bundle_accuracy": ("value_field_metrics", "bundle_accuracy"),
    "value_category_accuracy": ("value_field_metrics", "category_accuracy"),
    "value_status_accuracy": ("value_field_metrics", "value_status_accuracy"),
    "measurement_context_accuracy": ("value_field_metrics", "measurement_context_accuracy"),
    "numeric_value_accuracy": ("value_field_metrics", "numeric_value_accuracy"),
    "unit_accuracy": ("value_field_metrics", "unit_accuracy"),
}

ASSERTION_LABELS = ("present", "absent", "possible", "conditional", "hypothetical", "historical")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--adaptive", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--tolerance", type=float, default=1e-12)
    return parser.parse_args()


def load_metrics(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"evaluation root must be an object: {path}")
    overall = payload.get("overall")
    return overall if isinstance(overall, dict) else payload


def nested_float(payload: dict[str, Any], path: tuple[str, ...]) -> float | None:
    value: Any = payload
    for key in path:
        if not isinstance(value, dict) or key not in value:
            return None
        value = value[key]
    if value is None:
        return None
    return float(value)


def compare(baseline: dict[str, Any], adaptive: dict[str, Any], tolerance: float) -> dict[str, Any]:
    rows = []
    passed = True
    for name, path in METRICS.items():
        before = nested_float(baseline, path)
        after = nested_float(adaptive, path)
        if before is None and after is None:
            continue
        metric_passed = before is not None and after is not None and after + tolerance >= before
        passed = passed and metric_passed
        rows.append({
            "metric": name,
            "baseline": before,
            "adaptive": after,
            "delta": (after - before) if before is not None and after is not None else None,
            "passed": metric_passed,
        })
    for label in ASSERTION_LABELS:
        support_path = ("assertion_on_relaxed_matches", "per_label", label, "support")
        baseline_support = nested_float(baseline, support_path)
        if not baseline_support:
            continue
        path = ("assertion_on_relaxed_matches", "per_label", label, "f1")
        before = nested_float(baseline, path)
        after = nested_float(adaptive, path)
        metric_passed = before is not None and after is not None and after + tolerance >= before
        passed = passed and metric_passed
        rows.append({
            "metric": f"assertion_{label}_f1",
            "baseline": before,
            "adaptive": after,
            "delta": (after - before) if before is not None and after is not None else None,
            "passed": metric_passed,
        })
    if not rows:
        passed = False
    return {"passed": passed, "tolerance": tolerance, "metrics": rows}


def main() -> int:
    args = parse_args()
    if args.tolerance < 0:
        raise ValueError("--tolerance must be non-negative")
    result = compare(
        load_metrics(Path(args.baseline)),
        load_metrics(Path(args.adaptive)),
        args.tolerance,
    )
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
