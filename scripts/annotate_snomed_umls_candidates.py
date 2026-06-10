#!/usr/bin/env python3
"""Annotate one or more MedCAT candidate files with a prebuilt SNOMED->UMLS crosswalk."""

from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any


SNOMED_ID = re.compile(r"^[0-9]{5,18}$")
UMLS_CUI = re.compile(r"^C[0-9]{7}$")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--crosswalk-jsonl", required=True)
    parser.add_argument("--candidate-jsonl", action="append", required=True)
    parser.add_argument("--output-jsonl", required=True)
    parser.add_argument("--summary-json", required=True)
    parser.add_argument("--source-stats-json", action="append", default=[])
    parser.add_argument("--gzip-compresslevel", type=int, default=3)
    parser.add_argument("--progress-every", type=int, default=100_000)
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


def open_output_text(path: Path, compresslevel: int):
    if str(path).endswith(".gz"):
        return gzip.open(path, "wt", encoding="utf-8", compresslevel=compresslevel)
    return path.open("w", encoding="utf-8")


def iter_jsonl(path: Path):
    with open_text(path) as fh:
        for line_no, line in enumerate(fh, start=1):
            if not line.strip():
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSON on {path}:{line_no}: {exc}") from exc


def load_crosswalk(path: Path) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for row in iter_jsonl(path):
        snomed_id = str(row.get("snomed_id") or "")
        if SNOMED_ID.match(snomed_id):
            rows[snomed_id] = row
    return rows


def annotate_record(record: dict[str, Any], row_by_snomed: dict[str, dict[str, Any]], stats: Counter) -> dict[str, Any]:
    concept_id = str(record.get("concept_id") or "")
    if UMLS_CUI.match(concept_id):
        record["umls_cui"] = concept_id
        record["umls_cui_candidates"] = [concept_id]
        record["umls_mapping_status"] = "already_umls_cui"
        record["umls_mapping_needs_review"] = False
        stats["already_umls_cui"] += 1
    elif SNOMED_ID.match(concept_id):
        row = row_by_snomed.get(concept_id)
        if not row:
            record["umls_cui"] = None
            record["umls_cui_candidates"] = []
            record["umls_mapping_status"] = "snomed_not_found_in_crosswalk"
            record["umls_mapping_needs_review"] = True
            stats["snomed_not_found"] += 1
        else:
            candidates = [item["cui"] for item in row.get("cuis", []) if item.get("cui")]
            record["umls_cui"] = row.get("preferred_cui")
            record["umls_cui_candidates"] = candidates
            record["umls_mapping_status"] = f"snomed_{row.get('mapping_status') or 'unknown'}"
            record["umls_mapping_needs_review"] = len(candidates) != 1
            stats[record["umls_mapping_status"]] += 1
    else:
        record["umls_cui"] = None
        record["umls_cui_candidates"] = []
        record["umls_mapping_status"] = "non_snomed_non_umls_or_missing"
        record["umls_mapping_needs_review"] = True
        stats["non_snomed_non_umls_or_missing"] += 1
    return record


def load_source_stats(paths: list[Path]) -> dict[str, Any]:
    totals = Counter()
    note_categories = Counter()
    rows = []
    for path in paths:
        stats = json.loads(path.read_text(encoding="utf-8"))
        rows.append({"path": str(path), **stats})
        totals["notes_processed"] += int(stats.get("notes_processed") or 0)
        totals["notes_with_entities"] += int(stats.get("notes_with_entities") or 0)
        totals["entities_seen"] += int(stats.get("entities_seen") or 0)
        totals["candidate_records_written"] += int(stats.get("candidate_records_written") or 0)
        totals["rejected_records_seen"] += int(stats.get("rejected_records_seen") or 0)
        note_categories.update(stats.get("selected_note_category_counts") or {})
    return {
        "source_stats_files": [str(path) for path in paths],
        "source_stats_rows": rows,
        "source_totals": dict(totals),
        "source_note_category_counts": dict(note_categories),
    }


def main() -> int:
    args = parse_args()
    if not os.environ.get("SLURM_JOB_ID") and not args.allow_no_slurm:
        print("ERROR: Run project-scale UMLS annotation through SLURM.", file=sys.stderr)
        return 2

    crosswalk_path = Path(args.crosswalk_jsonl)
    candidate_paths = [Path(path) for path in args.candidate_jsonl]
    output_path = Path(args.output_jsonl)
    summary_path = Path(args.summary_json)
    source_stats_paths = [Path(path) for path in args.source_stats_json]

    for path in [crosswalk_path, *candidate_paths, *source_stats_paths]:
        if not path.exists():
            print(f"ERROR missing input: {path}", file=sys.stderr)
            return 2

    mkdir_private(output_path.parent)
    mkdir_private(summary_path.parent)
    row_by_snomed = load_crosswalk(crosswalk_path)
    stats = Counter()
    stats["candidate_files_read"] = len(candidate_paths)
    stats["crosswalk_snomed_ids_loaded"] = len(row_by_snomed)

    with open_output_text(output_path, args.gzip_compresslevel) as out_fh:
        for candidate_path in candidate_paths:
            for record in iter_jsonl(candidate_path):
                stats["records_read"] += 1
                if args.progress_every > 0 and stats["records_read"] % args.progress_every == 0:
                    print(f"[progress] records_read={stats['records_read']}", flush=True)
                out_fh.write(json.dumps(annotate_record(record, row_by_snomed, stats), ensure_ascii=False) + "\n")

    chmod_private(output_path)
    source_stats = load_source_stats(source_stats_paths) if source_stats_paths else {}
    expected = (source_stats.get("source_totals") or {}).get("candidate_records_written")
    if expected is not None and int(expected) != int(stats["records_read"]):
        stats["record_count_mismatch"] = 1
        stats["expected_records_from_source_stats"] = int(expected)
    else:
        stats["record_count_mismatch"] = 0

    summary = {
        "crosswalk_jsonl": str(crosswalk_path),
        "candidate_jsonl": [str(path) for path in candidate_paths],
        "output_jsonl": str(output_path),
        "output_bytes": output_path.stat().st_size,
        "annotation": dict(stats),
        "source_stats": source_stats,
    }
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    chmod_private(summary_path)
    print(json.dumps(summary["annotation"], indent=2, ensure_ascii=False), flush=True)
    return 0 if stats["record_count_mismatch"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

