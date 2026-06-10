#!/usr/bin/env python3
"""Summarize MedCAT shard output directories without scanning candidate JSONL."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-root", required=True)
    parser.add_argument("--output-json", required=True)
    parser.add_argument("--full-nonempty-notes", type=int, default=2_083_112)
    return parser.parse_args()


def chmod_private(path: Path) -> None:
    try:
        path.chmod(0o600)
    except FileNotFoundError:
        pass


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def find_candidate_file(shard_dir: Path) -> Path | None:
    for name in ("medcat_candidates.jsonl.gz", "medcat_candidates.jsonl"):
        path = shard_dir / name
        if path.exists():
            return path
    return None


def main() -> int:
    args = parse_args()
    input_root = Path(args.input_root)
    output_json = Path(args.output_json)
    if not input_root.exists():
        raise SystemExit(f"missing input root: {input_root}")

    shard_rows: list[dict[str, Any]] = []
    totals = Counter()
    note_categories = Counter()
    output_decisions = Counter()
    filter_actions = Counter()
    concept_categories = Counter()
    candidate_bytes_total = 0
    rejected_bytes_total = 0

    for shard_dir in sorted(input_root.glob("shard_*"), key=lambda p: int(p.name.split("_")[-1])):
        stats_path = shard_dir / "medcat_candidate_stats.json"
        candidate_path = find_candidate_file(shard_dir)
        rejected_path = shard_dir / "medcat_rejected_sample.jsonl"
        row: dict[str, Any] = {
            "shard": shard_dir.name,
            "stats_exists": stats_path.exists(),
            "candidate_path": str(candidate_path) if candidate_path else None,
            "candidate_bytes": candidate_path.stat().st_size if candidate_path and candidate_path.exists() else 0,
            "rejected_sample_bytes": rejected_path.stat().st_size if rejected_path.exists() else 0,
        }
        candidate_bytes_total += row["candidate_bytes"]
        rejected_bytes_total += row["rejected_sample_bytes"]
        if stats_path.exists():
            stats = load_json(stats_path)
            row.update(
                {
                    "notes_processed": int(stats.get("notes_processed") or 0),
                    "notes_with_entities": int(stats.get("notes_with_entities") or 0),
                    "entities_seen": int(stats.get("entities_seen") or 0),
                    "candidate_records_written": int(stats.get("candidate_records_written") or 0),
                    "rejected_records_seen": int(stats.get("rejected_records_seen") or 0),
                    "source_path": (stats.get("sampling") or {}).get("source_path"),
                    "selected_note_category_counts": stats.get("selected_note_category_counts") or {},
                    "output_decision_counts": stats.get("output_decision_counts") or {},
                    "filter_action_counts": stats.get("filter_action_counts") or {},
                    "concept_category_preview_counts": stats.get("concept_category_preview_counts") or {},
                }
            )
            totals["completed_shards"] += 1
            totals["notes_processed"] += row["notes_processed"]
            totals["notes_with_entities"] += row["notes_with_entities"]
            totals["entities_seen"] += row["entities_seen"]
            totals["candidate_records_written"] += row["candidate_records_written"]
            totals["rejected_records_seen"] += row["rejected_records_seen"]
            note_categories.update(row["selected_note_category_counts"])
            output_decisions.update(row["output_decision_counts"])
            filter_actions.update(row["filter_action_counts"])
            concept_categories.update(row["concept_category_preview_counts"])
        else:
            totals["incomplete_shards"] += 1
        shard_rows.append(row)

    notes = totals["notes_processed"] or 1
    candidates_per_note = totals["candidate_records_written"] / notes
    entities_per_note = totals["entities_seen"] / notes
    candidate_mb_per_note = candidate_bytes_total / notes / (1024**2)
    projection = {
        "full_nonempty_notes_assumption": args.full_nonempty_notes,
        "projected_candidate_records": round(candidates_per_note * args.full_nonempty_notes),
        "projected_candidate_output_tb_from_observed_bytes": candidate_bytes_total / notes * args.full_nonempty_notes / (1024**4),
        "candidate_records_per_note": candidates_per_note,
        "entities_per_note": entities_per_note,
        "candidate_output_mb_per_note": candidate_mb_per_note,
    }

    summary = {
        "input_root": str(input_root),
        "shards_seen": len(shard_rows),
        "totals": dict(totals),
        "candidate_bytes_total": candidate_bytes_total,
        "rejected_sample_bytes_total": rejected_bytes_total,
        "note_category_counts": dict(note_categories),
        "output_decision_counts": dict(output_decisions),
        "filter_action_counts": dict(filter_actions),
        "concept_category_preview_counts": dict(concept_categories),
        "projection": projection,
        "shards": shard_rows,
    }
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_json.parent.chmod(0o700)
    output_json.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    chmod_private(output_json)
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
