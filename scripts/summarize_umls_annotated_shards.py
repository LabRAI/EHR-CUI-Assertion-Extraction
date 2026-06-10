#!/usr/bin/env python3
"""Summarize full shard-wise UMLS-annotated MedCAT candidate outputs."""

from __future__ import annotations

import argparse
import datetime as dt
import gzip
import json
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from build_strict_sft_dataset import strict_decision


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-root", required=True)
    parser.add_argument("--source-stats-root", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--output-json", required=True)
    parser.add_argument("--memory-md")
    parser.add_argument("--min-keep-score", type=float, default=0.99)
    parser.add_argument("--full-nonempty-notes", type=int, default=2_083_112)
    parser.add_argument("--progress-every-shards", type=int, default=25)
    parser.add_argument("--progress-every-records", type=int, default=1_000_000)
    parser.add_argument("--allow-no-slurm", action="store_true")
    return parser.parse_args()


def chmod_private(path: Path) -> None:
    try:
        path.chmod(0o600)
    except FileNotFoundError:
        pass


def mkdir_private(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)


def open_text(path: Path):
    if str(path).endswith(".gz"):
        return gzip.open(path, "rt", encoding="utf-8", errors="replace")
    return path.open("r", encoding="utf-8", errors="replace")


def iter_jsonl(path: Path):
    with open_text(path) as fh:
        for line_no, line in enumerate(fh, start=1):
            if not line.strip():
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSON on {path}:{line_no}: {exc}") from exc


def load_manifest(path: Path) -> list[int]:
    shard_ids = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                shard_ids.append(int(json.loads(line)["shard_id"]))
    return sorted(shard_ids)


def find_annotated_file(shard_dir: Path) -> Path | None:
    for name in ("medcat_candidates_with_umls.jsonl.gz", "medcat_candidates_with_umls.jsonl"):
        path = shard_dir / name
        if path.exists():
            return path
    return None


def now_stamp() -> str:
    return dt.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")


def append_memory(memory_md: Path, summary: dict[str, Any]) -> None:
    totals = summary["totals"]
    strict = summary["strict_gate_counts"]
    projection = summary["projection"]
    lines = [
        "",
        f"## {now_stamp()} - Full UMLS-annotated strict eligibility summary",
        "",
        "- Run:",
        f"  - SLURM job id: `{os.environ.get('SLURM_JOB_ID', 'unknown')}`",
        f"  - Input root: `{summary['input_root']}`",
        f"  - Source MedCAT stats root: `{summary['source_stats_root']}`",
        f"  - Output summary: `{summary['output_json']}`",
        "",
        "- Result:",
        f"  - Expected shards: `{totals['expected_shards']}`",
        f"  - Completed annotated shards: `{totals['completed_shards']}`",
        f"  - Failed/missing shards: `{totals['failed_shards']}`",
        f"  - Notes processed: `{totals['notes_processed']}`",
        f"  - UMLS-annotated candidate records: `{strict['total']}`",
        f"  - Strict eligible records: `{strict['eligible']}`",
        f"  - Strict rejected records: `{strict['rejected']}`",
        f"  - Strict eligible rate: `{summary['strict_eligible_rate']:.4f}`",
        f"  - Eligible per note: `{summary['eligible_per_note']:.3f}`",
        f"  - Projected strict eligible candidates: `{projection['projected_strict_eligible_candidates']}`",
        "",
        "- Interpretation:",
        "  - This is the full-corpus UMLS annotation and strict-gate summary for the linker-derived candidate layer.",
        "  - It is the gating checkpoint before full NuExtract conversion / extraction-LLM SFT input creation.",
        "",
        "- Compliance:",
        "  - Summary scanned full data through SLURM.",
        "  - Outputs/logs remain in private project storage and are permission-restricted.",
        "",
    ]
    with memory_md.open("a", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    chmod_private(memory_md)


def main() -> int:
    args = parse_args()
    if not os.environ.get("SLURM_JOB_ID") and not args.allow_no_slurm:
        print("ERROR: Run full annotated-shard summary through SLURM.", file=sys.stderr)
        return 2

    input_root = Path(args.input_root)
    source_stats_root = Path(args.source_stats_root)
    manifest = Path(args.manifest)
    output_json = Path(args.output_json)
    memory_md = Path(args.memory_md) if args.memory_md else None
    for path in (input_root, source_stats_root, manifest):
        if not path.exists():
            print(f"ERROR missing input: {path}", file=sys.stderr)
            return 2

    totals = Counter({"expected_shards": 0, "completed_shards": 0, "failed_shards": 0})
    strict_counts = Counter()
    reject_reasons = Counter()
    mapping_status = Counter()
    eligible_action = Counter()
    eligible_category = Counter()
    eligible_assertion = Counter()
    eligible_note_category = Counter()
    note_categories = Counter()
    shard_rows: list[dict[str, Any]] = []

    shard_ids = load_manifest(manifest)
    totals["expected_shards"] = len(shard_ids)
    for pos, shard_id in enumerate(shard_ids, start=1):
        if args.progress_every_shards > 0 and pos % args.progress_every_shards == 0:
            print(f"[progress] shard {pos}/{len(shard_ids)} records={strict_counts['total']}", flush=True)
        shard_dir = input_root / f"shard_{shard_id}"
        source_stats_path = source_stats_root / f"shard_{shard_id}" / "medcat_candidate_stats.json"
        annotated_path = find_annotated_file(shard_dir)
        issues = []
        if annotated_path is None:
            issues.append("missing_annotated_candidate_file")
        if not source_stats_path.exists():
            issues.append("missing_source_stats")

        source_stats: dict[str, Any] = {}
        if source_stats_path.exists():
            source_stats = json.loads(source_stats_path.read_text(encoding="utf-8"))
            totals["notes_processed"] += int(source_stats.get("notes_processed") or 0)
            totals["notes_with_entities"] += int(source_stats.get("notes_with_entities") or 0)
            totals["entities_seen"] += int(source_stats.get("entities_seen") or 0)
            totals["raw_candidate_records_written"] += int(source_stats.get("candidate_records_written") or 0)
            totals["raw_rejected_records_seen"] += int(source_stats.get("rejected_records_seen") or 0)
            note_categories.update(source_stats.get("selected_note_category_counts") or {})

        shard_total = 0
        shard_eligible = 0
        if annotated_path is not None:
            for record in iter_jsonl(annotated_path):
                strict_counts["total"] += 1
                shard_total += 1
                if args.progress_every_records > 0 and strict_counts["total"] % args.progress_every_records == 0:
                    print(f"[progress] records={strict_counts['total']}", flush=True)
                mapping_status[record.get("umls_mapping_status") or ""] += 1
                ok, reasons = strict_decision(record, args.min_keep_score)
                if ok:
                    strict_counts["eligible"] += 1
                    shard_eligible += 1
                    eligible_action[record.get("filter_action") or ""] += 1
                    eligible_category[record.get("concept_category_preview") or ""] += 1
                    eligible_assertion[record.get("assertion_status_preview") or ""] += 1
                    eligible_note_category[record.get("category") or ""] += 1
                else:
                    strict_counts["rejected"] += 1
                    reject_reasons.update(reasons)

        expected_records = int(source_stats.get("candidate_records_written") or 0) if source_stats else None
        if expected_records is not None and shard_total != expected_records:
            issues.append(f"record_count_mismatch:annotated={shard_total}:source_stats={expected_records}")

        if issues:
            totals["failed_shards"] += 1
        else:
            totals["completed_shards"] += 1
        shard_rows.append(
            {
                "shard_id": shard_id,
                "annotated_path": str(annotated_path) if annotated_path else None,
                "source_stats_path": str(source_stats_path) if source_stats_path.exists() else None,
                "records": shard_total,
                "eligible": shard_eligible,
                "issues": issues,
                "ok": not issues,
            }
        )

    notes_for_projection = totals["notes_processed"] or 1
    eligible_per_note = strict_counts["eligible"] / notes_for_projection
    candidates_per_note = strict_counts["total"] / notes_for_projection
    annotated_bytes = sum((find_annotated_file(input_root / f"shard_{sid}") or Path()).stat().st_size for sid in shard_ids if find_annotated_file(input_root / f"shard_{sid}"))
    projection = {
        "full_nonempty_notes_assumption": args.full_nonempty_notes,
        "projected_strict_eligible_candidates": round(eligible_per_note * args.full_nonempty_notes),
        "projected_annotated_candidate_records": round(candidates_per_note * args.full_nonempty_notes),
        "annotated_jsonl_gz_mb_per_note": annotated_bytes / notes_for_projection / (1024**2),
        "projected_annotated_jsonl_gz_tb": annotated_bytes / notes_for_projection * args.full_nonempty_notes / (1024**4),
    }
    summary: dict[str, Any] = {
        "created_at": now_stamp(),
        "slurm_job_id": os.environ.get("SLURM_JOB_ID"),
        "input_root": str(input_root),
        "source_stats_root": str(source_stats_root),
        "manifest": str(manifest),
        "output_json": str(output_json),
        "min_keep_score": args.min_keep_score,
        "totals": dict(totals),
        "strict_gate_counts": dict(strict_counts),
        "strict_eligible_rate": strict_counts["eligible"] / strict_counts["total"] if strict_counts["total"] else 0.0,
        "eligible_per_note": eligible_per_note,
        "candidates_per_note": candidates_per_note,
        "umls_mapping_status_counts": dict(mapping_status),
        "eligible_action_counts": dict(eligible_action),
        "eligible_category_counts": dict(eligible_category),
        "eligible_assertion_preview_counts": dict(eligible_assertion),
        "eligible_note_category_counts": dict(eligible_note_category),
        "note_category_counts": dict(note_categories),
        "top_reject_reasons": reject_reasons.most_common(100),
        "projection": projection,
        "shards": shard_rows,
    }
    mkdir_private(output_json.parent)
    output_json.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    chmod_private(output_json)
    print(json.dumps({"totals": summary["totals"], "strict_gate_counts": summary["strict_gate_counts"]}, indent=2), flush=True)
    if memory_md:
        append_memory(memory_md, summary)
    return 0 if totals["failed_shards"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
