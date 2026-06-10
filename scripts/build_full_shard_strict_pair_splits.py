#!/usr/bin/env python3
"""Build full-shard strict-eligible CUI/assertion pair splits.

This is the scalable version of the small v2.1 pair split builder. It streams
over the UMLS-annotated full-shard MedCAT outputs, applies the same strict gate,
and writes subject-hash train/dev/test JSONL.GZ files without loading all rows.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

from build_strict_sft_dataset import make_pair, strict_decision


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-root", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--release-name", default="full_shard_strict_pairs_v1")
    parser.add_argument("--min-keep-score", type=float, default=0.99)
    parser.add_argument("--train-frac", type=float, default=0.80)
    parser.add_argument("--dev-frac", type=float, default=0.10)
    parser.add_argument("--gzip-compresslevel", type=int, default=1)
    parser.add_argument("--max-shards", type=int, default=0)
    parser.add_argument("--max-records", type=int, default=0)
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


def shard_sort_key(path: Path) -> tuple[int, str]:
    match = re.search(r"shard_(\d+)", str(path))
    return (int(match.group(1)) if match else 10**9, str(path))


def iter_shard_paths(root: Path, max_shards: int = 0) -> list[Path]:
    paths = sorted(root.glob("shard_*/medcat_candidates_with_umls.jsonl.gz"), key=shard_sort_key)
    return paths[:max_shards] if max_shards else paths


def iter_jsonl_gz(path: Path) -> Iterable[tuple[int, dict[str, Any]]]:
    with gzip.open(path, "rt", encoding="utf-8", errors="replace") as fh:
        for line_no, line in enumerate(fh, 1):
            if line.strip():
                yield line_no, json.loads(line)


def stable_unit_interval(text: str) -> float:
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return int(digest[:16], 16) / float(16**16)


def split_for_subject(subject_id: str, train_frac: float, dev_frac: float) -> str:
    value = stable_unit_interval(subject_id or "missing_subject")
    if value < train_frac:
        return "train"
    if value < train_frac + dev_frac:
        return "dev"
    return "test"


def release_row(
    record: dict[str, Any],
    pair: dict[str, Any],
    split: str,
    release_idx: int,
    release_name: str,
    shard_path: Path,
    shard_line_no: int,
    global_candidate_idx: int,
    strict_reasons: list[str],
) -> dict[str, Any]:
    return {
        "release_example_id": f"{release_name}_{release_idx:010d}",
        "split": split,
        "source_example_id": f"full_shard_candidate_{global_candidate_idx:010d}",
        "subject_id": record.get("subject_id"),
        "hadm_id": record.get("hadm_id"),
        "note_row_id": record.get("note_row_id"),
        "chartdate": record.get("chartdate"),
        "note_category": record.get("category"),
        "source_section": record.get("section") or "unknown",
        "pair_section": pair.get("section") or record.get("section") or "unknown",
        "source_line_no": global_candidate_idx,
        "pair": pair,
        "provenance": {
            "pipeline": "MedCAT full-shard SNOMED/UMLS candidates -> strict schema-aware gate -> full-shard pair split",
            "input_kind": "full_shard_strict_candidate_records",
            "source_candidate_file": str(shard_path),
            "source_candidate_line_no": shard_line_no,
            "global_candidate_idx": global_candidate_idx,
            "strict_reasons": strict_reasons,
            "pre_umls_strict_gate": record.get("pre_umls_strict_gate"),
            "pre_umls_strict_reasons": record.get("pre_umls_strict_reasons"),
            "umls_mapping_status": record.get("umls_mapping_status"),
        },
    }


def open_outputs(out_dir: Path, compresslevel: int):
    paths = {
        "all": out_dir / "all_pairs.jsonl.gz",
        "train": out_dir / "train.jsonl.gz",
        "dev": out_dir / "dev.jsonl.gz",
        "test": out_dir / "test.jsonl.gz",
    }
    handles = {
        split: gzip.open(path, "wt", encoding="utf-8", compresslevel=compresslevel)
        for split, path in paths.items()
    }
    return paths, handles


def main() -> int:
    args = parse_args()
    if not os.environ.get("SLURM_JOB_ID") and not args.allow_no_slurm:
        print("ERROR: run full-shard pair split creation through SLURM.", file=sys.stderr)
        return 2
    if args.train_frac <= 0 or args.dev_frac < 0 or args.train_frac + args.dev_frac >= 1:
        print("ERROR: invalid split fractions.", file=sys.stderr)
        return 2

    root = Path(args.input_root)
    out_dir = Path(args.output_dir)
    mkdir_private(out_dir)
    shard_paths = iter_shard_paths(root, args.max_shards)
    if not shard_paths:
        print(f"ERROR: no shard files under {root}", file=sys.stderr)
        return 2

    split_paths, handles = open_outputs(out_dir, args.gzip_compresslevel)
    counters: Counter[str] = Counter()
    split_subjects: dict[str, set[str]] = {"train": set(), "dev": set(), "test": set()}
    category_counts: Counter[str] = Counter()
    assertion_counts: Counter[str] = Counter()
    note_category_counts: Counter[str] = Counter()
    reject_reasons: Counter[str] = Counter()
    release_idx = 0
    global_idx = 0
    completed_shards = 0

    try:
        for shard_path in shard_paths:
            completed_shards += 1
            for shard_line_no, record in iter_jsonl_gz(shard_path):
                global_idx += 1
                counters["candidate_records_seen"] += 1
                ok, reasons = strict_decision(record, args.min_keep_score)
                if not ok:
                    counters["strict_rejected"] += 1
                    reject_reasons.update(reasons)
                    if args.max_records and global_idx >= args.max_records:
                        raise StopIteration
                    continue

                subject_id = str(record.get("subject_id") or "")
                if not subject_id:
                    counters["missing_subject_skipped"] += 1
                    if args.max_records and global_idx >= args.max_records:
                        raise StopIteration
                    continue

                pair = make_pair(record)
                if not pair.get("cui"):
                    counters["missing_pair_cui_skipped"] += 1
                    if args.max_records and global_idx >= args.max_records:
                        raise StopIteration
                    continue

                split = split_for_subject(subject_id, args.train_frac, args.dev_frac)
                release_idx += 1
                out = release_row(
                    record,
                    pair,
                    split,
                    release_idx,
                    args.release_name,
                    shard_path,
                    shard_line_no,
                    global_idx,
                    reasons,
                )
                line = json.dumps(out, ensure_ascii=False, separators=(",", ":")) + "\n"
                handles["all"].write(line)
                handles[split].write(line)

                counters["strict_accepted"] += 1
                counters[f"{split}_pairs"] += 1
                split_subjects[split].add(subject_id)
                category_counts[pair.get("concept_category") or "missing"] += 1
                assertion_counts[pair.get("assertion_status") or "missing"] += 1
                note_category_counts[record.get("category") or "missing"] += 1

                if args.max_records and global_idx >= args.max_records:
                    raise StopIteration
    except StopIteration:
        counters["stopped_by_max_records"] += 1
    finally:
        for handle in handles.values():
            handle.close()

    for path in split_paths.values():
        chmod_private(path)

    summary = {
        "release_name": args.release_name,
        "input_root": str(root),
        "output_dir": str(out_dir),
        "slurm_job_id": os.environ.get("SLURM_JOB_ID"),
        "min_keep_score": args.min_keep_score,
        "train_frac": args.train_frac,
        "dev_frac": args.dev_frac,
        "test_frac": 1.0 - args.train_frac - args.dev_frac,
        "split_policy": "stable SHA256 hash of subject_id; no subject can cross train/dev/test",
        "shards_found": len(shard_paths),
        "shards_scanned": completed_shards,
        "max_shards": args.max_shards,
        "max_records": args.max_records,
        "counts": dict(counters),
        "subjects": {split: len(values) for split, values in split_subjects.items()},
        "category_counts": dict(category_counts.most_common()),
        "assertion_counts": dict(assertion_counts.most_common()),
        "note_category_counts": dict(note_category_counts.most_common()),
        "top_reject_reasons": reject_reasons.most_common(50),
        "files": {split: str(path) for split, path in split_paths.items()},
    }
    summary_path = out_dir / "split_summary.json"
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    chmod_private(summary_path)

    readme_path = out_dir / "README.md"
    readme_path.write_text(
        "# Full-Shard Strict Pair Splits\n\n"
        "Generated by streaming full UMLS-annotated MedCAT shards through the strict pair gate.\n\n"
        f"- release: `{args.release_name}`\n"
        f"- accepted pairs: `{counters['strict_accepted']}`\n"
        f"- summary: `{summary_path}`\n",
        encoding="utf-8",
    )
    chmod_private(readme_path)
    print(json.dumps(summary, indent=2, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
