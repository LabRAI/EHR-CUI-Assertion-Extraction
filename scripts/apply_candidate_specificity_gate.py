#!/usr/bin/env python3
"""Demote broad disease candidates before strict pair construction.

This optional production-safe stage is meant to run after MedCAT candidate
extraction and, when used, after the MedCAT-CDB ontology overlay. It keeps the
candidate JSONL shape unchanged but changes very broad disease anchors to
`reject_candidate`, so they do not become strict CUI-assertion training pairs.
"""

from __future__ import annotations

import argparse
import gzip
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any


TARGET_ACTIONS = {"keep_candidate", "review_candidate", "value_parse"}
NON_ALNUM = re.compile(r"[^a-z0-9]+")

BROAD_DISEASE_ANCHORS = {
    "abnormal",
    "abnormalities",
    "abnormality",
    "adverse effect",
    "adverse effects",
    "adverse reaction",
    "adverse reactions",
    "blind",
    "brain",
    "condition",
    "conditions",
    "cross",
    "disease",
    "diseases",
    "disorder",
    "disorders",
    "effect",
    "effects",
    "eye",
    "finding",
    "findings",
    "group b",
    "kidney",
    "liver",
    "normal",
    "optic nerve",
    "pan",
    "pathophysiology",
    "renal",
    "sign",
    "signs",
    "sinus",
    "strain",
    "syndrome",
    "syndromes",
    "toxic",
    "vascular",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate-jsonl", action="append", required=True, help="Candidate JSONL(.gz) file or directory")
    parser.add_argument("--output-dir", action="append", required=True, help="Output directory; repeat for each input")
    parser.add_argument("--name", action="append", default=[], help="Optional run name for each input")
    parser.add_argument("--summary-json", required=True)
    return parser.parse_args()


def norm(text: str | None) -> str:
    return NON_ALNUM.sub(" ", text or "").strip().lower()


def open_text(path: Path):
    if str(path).endswith(".gz"):
        return gzip.open(path, "rt", encoding="utf-8", errors="replace")
    return path.open("r", encoding="utf-8", errors="replace")


def open_output(path: Path):
    if str(path).endswith(".gz"):
        return gzip.open(path, "wt", encoding="utf-8", compresslevel=1)
    return path.open("w", encoding="utf-8")


def chmod_private(path: Path) -> None:
    try:
        path.chmod(0o600)
    except FileNotFoundError:
        pass


def mkdir_private(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except PermissionError:
        pass


def prediction_files(path: Path) -> list[Path]:
    if path.is_file():
        return [path]
    return sorted(path.glob("**/medcat_candidates.jsonl")) + sorted(path.glob("**/medcat_candidates.jsonl.gz"))


def should_demote(row: dict[str, Any]) -> bool:
    if row.get("filter_action") not in TARGET_ACTIONS:
        return False
    if row.get("concept_category_preview") != "disease_or_diagnosis":
        return False
    if int(row.get("span_end") or 0) <= int(row.get("span_start") or 0):
        return False
    return norm(row.get("span")) in BROAD_DISEASE_ANCHORS


def process_one(candidate_path: Path, output_dir: Path, name: str) -> dict[str, Any]:
    mkdir_private(output_dir)
    out_path = output_dir / "medcat_candidates.jsonl.gz"
    summary: Counter[str] = Counter()
    top_demoted: Counter[str] = Counter()

    with open_output(out_path) as out_fh:
        for pred_file in prediction_files(candidate_path):
            with open_text(pred_file) as in_fh:
                for line in in_fh:
                    if not line.strip():
                        continue
                    row = json.loads(line)
                    summary["rows_seen"] += 1
                    if should_demote(row):
                        phrase = norm(row.get("span"))
                        row = dict(row)
                        row["filter_action"] = "reject_candidate"
                        row["needs_review"] = True
                        row["reason_codes"] = list(row.get("reason_codes") or []) + [
                            "specificity_gate_broad_disease_anchor"
                        ]
                        row["filter_flags"] = list(row.get("filter_flags") or []) + ["specificity_gate"]
                        summary["demoted"] += 1
                        top_demoted[phrase] += 1
                    out_fh.write(json.dumps(row, sort_keys=True) + "\n")
                    summary["rows_written"] += 1

    chmod_private(out_path)
    return {
        "name": name,
        "input": str(candidate_path),
        "output": str(out_path),
        "summary": dict(summary),
        "top_demoted": top_demoted.most_common(30),
    }


def main() -> int:
    args = parse_args()
    if len(args.candidate_jsonl) != len(args.output_dir):
        raise SystemExit("--candidate-jsonl and --output-dir counts must match")
    names = list(args.name) if args.name else []
    while len(names) < len(args.candidate_jsonl):
        names.append(f"run_{len(names)}")

    outputs = []
    for idx, candidate in enumerate(args.candidate_jsonl):
        outputs.append(process_one(Path(candidate), Path(args.output_dir[idx]), names[idx]))

    summary_path = Path(args.summary_json)
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps({"outputs": outputs}, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    chmod_private(summary_path)
    print(json.dumps({"outputs": outputs}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
