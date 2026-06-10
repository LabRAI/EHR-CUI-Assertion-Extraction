#!/usr/bin/env python3
"""Validate full MedCAT shard outputs after the SLURM array finishes."""

from __future__ import annotations

import argparse
import datetime as dt
import gzip
import json
import os
from collections import Counter
from pathlib import Path
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-root", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--output-json", required=True)
    parser.add_argument("--memory-md")
    parser.add_argument("--full-nonempty-notes", type=int, default=2_083_112)
    parser.add_argument("--progress-every-shards", type=int, default=25)
    parser.add_argument("--max-issue-examples", type=int, default=50)
    return parser.parse_args()


def chmod_private(path: Path) -> None:
    try:
        path.chmod(0o600)
    except FileNotFoundError:
        pass


def mkdir_private(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def load_manifest(path: Path) -> dict[int, dict[str, Any]]:
    rows: dict[int, dict[str, Any]] = {}
    with path.open("r", encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            shard_id = int(row["shard_id"])
            if shard_id in rows:
                raise ValueError(f"duplicate shard_id={shard_id} in {path}:{line_no}")
            rows[shard_id] = row
    return rows


def find_candidate_file(shard_dir: Path) -> Path | None:
    for name in ("medcat_candidates.jsonl.gz", "medcat_candidates.jsonl"):
        path = shard_dir / name
        if path.exists():
            return path
    return None


def count_candidate_records(path: Path) -> int:
    if path.name.endswith(".gz"):
        with gzip.open(path, "rt", encoding="utf-8", errors="replace", newline="") as fh:
            return sum(1 for _ in fh)
    with path.open("r", encoding="utf-8", errors="replace", newline="") as fh:
        return sum(1 for _ in fh)


def now_stamp() -> str:
    return dt.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")


def append_memory(memory_md: Path, report: dict[str, Any]) -> None:
    totals = report["totals"]
    projection = report["projection"]
    valid = totals["failed_shards"] == 0 and totals["validated_shards"] == totals["expected_shards"]
    lines = [
        "",
        f"## {now_stamp()} - Full MedCAT compressed shard validation",
        "",
        "- Run:",
        f"  - SLURM job id: `{os.environ.get('SLURM_JOB_ID', 'unknown')}`",
        f"  - Input root: `{report['input_root']}`",
        f"  - Manifest: `{report['manifest']}`",
        f"  - Validation report: `{report['output_json']}`",
        "",
        "- Result:",
        f"  - Expected shards: `{totals['expected_shards']}`",
        f"  - Validated shards: `{totals['validated_shards']}`",
        f"  - Failed shards: `{totals['failed_shards']}`",
        f"  - Notes processed: `{totals['notes_processed']}`",
        f"  - Candidate records from stats: `{totals['candidate_records_from_stats']}`",
        f"  - Candidate records counted from files: `{totals['candidate_records_counted']}`",
        f"  - Candidate gzip/jsonl bytes: `{totals['candidate_bytes']}`",
        f"  - Records per note: `{projection['candidate_records_per_note']:.3f}`",
        f"  - Projected full-corpus candidate records: `{projection['projected_candidate_records']}`",
        f"  - Projected candidate output size from observed bytes: `{projection['projected_candidate_output_tb_from_observed_bytes']:.4f}` TB",
        "",
        "- Interpretation:",
        f"  - Full compressed extraction validation passed: `{'yes' if valid else 'no'}`",
        "  - This stage is still strict-pre-UMLS linker output; UMLS crosswalk annotation is the next stage after validation.",
        "",
        "- Compliance:",
        "  - Validation ran as a SLURM job, not as login-node heavy processing.",
        "  - Outputs/logs remain in private project storage and are permission-restricted.",
        "",
    ]
    with memory_md.open("a", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    chmod_private(memory_md)


def main() -> int:
    args = parse_args()
    input_root = Path(args.input_root)
    manifest_path = Path(args.manifest)
    output_json = Path(args.output_json)
    memory_md = Path(args.memory_md) if args.memory_md else None

    if not input_root.exists():
        raise SystemExit(f"missing input root: {input_root}")
    if not manifest_path.exists():
        raise SystemExit(f"missing manifest: {manifest_path}")

    manifest = load_manifest(manifest_path)
    shard_ids = sorted(manifest)
    issue_examples: list[dict[str, Any]] = []
    shard_rows: list[dict[str, Any]] = []

    totals = Counter(
        {
            "expected_shards": len(shard_ids),
            "validated_shards": 0,
            "failed_shards": 0,
            "notes_processed": 0,
            "notes_with_entities": 0,
            "entities_seen": 0,
            "candidate_records_from_stats": 0,
            "candidate_records_counted": 0,
            "rejected_records_seen": 0,
            "candidate_bytes": 0,
            "rejected_sample_bytes": 0,
        }
    )
    note_categories = Counter()
    output_decisions = Counter()
    filter_actions = Counter()
    concept_categories = Counter()

    for pos, shard_id in enumerate(shard_ids, start=1):
        if args.progress_every_shards > 0 and pos % args.progress_every_shards == 0:
            print(f"[progress] validating shard {pos}/{len(shard_ids)}", flush=True)

        shard_dir = input_root / f"shard_{shard_id}"
        stats_path = shard_dir / "medcat_candidate_stats.json"
        rejected_path = shard_dir / "medcat_rejected_sample.jsonl"
        candidate_path = find_candidate_file(shard_dir)
        issues: list[str] = []
        stats: dict[str, Any] = {}
        counted_records: int | None = None

        if not shard_dir.exists():
            issues.append("missing_shard_dir")
        if not stats_path.exists():
            issues.append("missing_stats")
        else:
            try:
                stats = load_json(stats_path)
            except Exception as exc:  # noqa: BLE001
                issues.append(f"stats_json_error:{type(exc).__name__}:{exc}")
        if candidate_path is None:
            issues.append("missing_candidate_file")
        else:
            try:
                counted_records = count_candidate_records(candidate_path)
            except Exception as exc:  # noqa: BLE001
                issues.append(f"candidate_read_or_gzip_error:{type(exc).__name__}:{exc}")

        expected_records = int(stats.get("candidate_records_written") or 0) if stats else None
        if counted_records is not None and expected_records is not None and counted_records != expected_records:
            issues.append(f"record_count_mismatch:counted={counted_records}:stats={expected_records}")

        expected_source = str(manifest[shard_id].get("path") or "")
        observed_source = str((stats.get("sampling") or {}).get("source_path") or "") if stats else ""
        if stats and observed_source and expected_source and observed_source != expected_source:
            issues.append("source_path_mismatch")

        candidate_bytes = candidate_path.stat().st_size if candidate_path and candidate_path.exists() else 0
        rejected_bytes = rejected_path.stat().st_size if rejected_path.exists() else 0
        row = {
            "shard_id": shard_id,
            "shard_dir": str(shard_dir),
            "candidate_path": str(candidate_path) if candidate_path else None,
            "stats_path": str(stats_path) if stats_path.exists() else None,
            "candidate_bytes": candidate_bytes,
            "rejected_sample_bytes": rejected_bytes,
            "candidate_records_counted": counted_records,
            "candidate_records_from_stats": expected_records,
            "notes_processed": int(stats.get("notes_processed") or 0) if stats else 0,
            "notes_with_entities": int(stats.get("notes_with_entities") or 0) if stats else 0,
            "entities_seen": int(stats.get("entities_seen") or 0) if stats else 0,
            "rejected_records_seen": int(stats.get("rejected_records_seen") or 0) if stats else 0,
            "expected_source_path": expected_source,
            "observed_source_path": observed_source,
            "ok": not issues,
            "issues": issues,
        }
        shard_rows.append(row)

        if issues:
            totals["failed_shards"] += 1
            if len(issue_examples) < args.max_issue_examples:
                issue_examples.append(row)
            continue

        totals["validated_shards"] += 1
        totals["notes_processed"] += row["notes_processed"]
        totals["notes_with_entities"] += row["notes_with_entities"]
        totals["entities_seen"] += row["entities_seen"]
        totals["candidate_records_from_stats"] += int(expected_records or 0)
        totals["candidate_records_counted"] += int(counted_records or 0)
        totals["rejected_records_seen"] += row["rejected_records_seen"]
        totals["candidate_bytes"] += candidate_bytes
        totals["rejected_sample_bytes"] += rejected_bytes
        note_categories.update(stats.get("selected_note_category_counts") or {})
        output_decisions.update(stats.get("output_decision_counts") or {})
        filter_actions.update(stats.get("filter_action_counts") or {})
        concept_categories.update(stats.get("concept_category_preview_counts") or {})

    notes = totals["notes_processed"] or 1
    projection = {
        "full_nonempty_notes_assumption": args.full_nonempty_notes,
        "candidate_records_per_note": totals["candidate_records_from_stats"] / notes,
        "entities_per_note": totals["entities_seen"] / notes,
        "candidate_output_mb_per_note": totals["candidate_bytes"] / notes / (1024**2),
        "projected_candidate_records": round(totals["candidate_records_from_stats"] / notes * args.full_nonempty_notes),
        "projected_candidate_output_tb_from_observed_bytes": totals["candidate_bytes"] / notes * args.full_nonempty_notes / (1024**4),
    }

    report = {
        "created_at": now_stamp(),
        "slurm_job_id": os.environ.get("SLURM_JOB_ID"),
        "input_root": str(input_root),
        "manifest": str(manifest_path),
        "output_json": str(output_json),
        "totals": dict(totals),
        "projection": projection,
        "note_category_counts": dict(note_categories),
        "output_decision_counts": dict(output_decisions),
        "filter_action_counts": dict(filter_actions),
        "concept_category_preview_counts": dict(concept_categories),
        "issue_examples": issue_examples,
        "shards": shard_rows,
    }

    mkdir_private(output_json.parent)
    output_json.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    chmod_private(output_json)
    print(json.dumps(report["totals"], indent=2, ensure_ascii=False), flush=True)

    if memory_md:
        append_memory(memory_md, report)

    return 0 if totals["failed_shards"] == 0 and totals["validated_shards"] == totals["expected_shards"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
