#!/usr/bin/env python3
"""Adaptive CPU runner for the MedCAT candidate-linking stage.

The script detects available CPU cores, builds an explicit task config, and
dispatches the optimized prefork extractor. Use run_final_extraction.py for the
complete production pipeline.
"""

from __future__ import annotations

import argparse
import gzip
import json
import math
import os
import subprocess
import sys
from pathlib import Path
from typing import Iterable


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--notes-jsonl-gz", action="append", required=True, help="One or more private note shard JSONL(.gz) files")
    parser.add_argument("--modelpack", required=True, help="Licensed MedCAT modelpack zip")
    parser.add_argument("--filter-policy", required=True, help="Schema-aware filter policy JSON")
    parser.add_argument("--output-root", required=True, help="Private output directory")
    parser.add_argument("--variant-name", default="auto_extraction")
    parser.add_argument("--workers", type=int, default=0, help="0 means auto-detect from SLURM or CPU affinity")
    parser.add_argument("--notes-per-task", type=int, default=0, help="0 means auto split by worker count")
    parser.add_argument("--max-notes", type=int, default=0, help="0 means all notes in provided shards")
    parser.add_argument("--max-note-chars", type=int, default=7000)
    parser.add_argument("--min-acc", type=float, default=0.0)
    parser.add_argument(
        "--output-mode",
        choices=["all_non_rejected", "strict_pre_umls"],
        default="strict_pre_umls",
        help="Candidate output contract; complete downstream pipelines require all_non_rejected.",
    )
    parser.add_argument("--pre-umls-min-keep-score", type=float, default=0.99)
    parser.add_argument("--sentence-chars", type=int, default=420)
    parser.add_argument("--context-chars", type=int, default=650)
    parser.add_argument("--progress-every", type=int, default=1000)
    parser.add_argument("--gzip-compresslevel", type=int, default=1)
    parser.add_argument("--medcat-batch-size", type=int, default=-1)
    parser.add_argument("--medcat-batch-size-chars", type=int, default=1_000_000)
    parser.add_argument("--seed", type=int, default=20260610)
    parser.add_argument("--delete-candidate-files", action="store_true", help="Delete large candidate JSONL.GZ files after stats are written")
    parser.add_argument("--allow-no-slurm", action="store_true", help="Developer/synthetic-data escape hatch")
    parser.add_argument("--dry-run", action="store_true", help="Write config and command without running extraction")
    return parser.parse_args()


def detect_workers(requested: int) -> int:
    if requested > 0:
        return requested
    for env_name in ("SLURM_CPUS_PER_TASK", "SLURM_CPUS_ON_NODE"):
        value = os.environ.get(env_name)
        if value and value.isdigit() and int(value) > 0:
            return int(value)
    try:
        affinity_count = len(os.sched_getaffinity(0))
        if affinity_count > 0:
            return affinity_count
    except AttributeError:
        pass
    return max(1, os.cpu_count() or 1)


def open_text(path: Path):
    if str(path).endswith(".gz"):
        return gzip.open(path, "rt", encoding="utf-8", errors="replace")
    return path.open("r", encoding="utf-8", errors="replace")


def count_nonempty_notes(path: Path, limit_remaining: int = 0) -> int:
    count = 0
    with open_text(path) as fh:
        for line in fh:
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            text = str(record.get("text") or record.get("TEXT") or "")
            if not text.strip():
                continue
            count += 1
            if limit_remaining and count >= limit_remaining:
                break
    return count


def shard_counts(paths: Iterable[Path], max_notes: int) -> list[tuple[Path, int]]:
    rows: list[tuple[Path, int]] = []
    remaining = max_notes
    for path in paths:
        if not path.exists():
            raise FileNotFoundError(path)
        cap = remaining if remaining > 0 else 0
        count = count_nonempty_notes(path, cap)
        if count:
            rows.append((path, count))
        if remaining > 0:
            remaining -= count
            if remaining <= 0:
                break
    return rows


def build_tasks(shards: list[tuple[Path, int]], workers: int, notes_per_task: int, max_notes: int) -> list[dict]:
    total_notes = sum(count for _, count in shards)
    if max_notes:
        total_notes = min(total_notes, max_notes)
    if total_notes <= 0:
        raise ValueError("no nonempty notes found in the provided shards")
    chunk_size = notes_per_task or max(100, math.ceil(total_notes / max(1, workers)))
    tasks: list[dict] = []
    global_index = 0
    remaining = total_notes
    for shard_idx, (path, shard_count) in enumerate(shards):
        take = min(shard_count, remaining)
        pos = 1
        local_chunk = 0
        while pos <= take:
            end = min(take, pos + chunk_size - 1)
            tasks.append(
                {
                    "task_id": f"shard_{shard_idx:06d}_chunk_{local_chunk:03d}",
                    "notes_jsonl_gz": str(path),
                    "shard_idx": shard_idx,
                    "chunk_idx": local_chunk,
                    "start_note_index": pos,
                    "end_note_index": end,
                    "planned_notes": end - pos + 1,
                    "global_task_index": global_index,
                }
            )
            global_index += 1
            local_chunk += 1
            pos = end + 1
        remaining -= take
        if remaining <= 0:
            break
    return tasks


def chmod_private(path: Path, mode: int = 0o600) -> None:
    try:
        path.chmod(mode)
    except FileNotFoundError:
        pass


def main() -> int:
    args = parse_args()
    if not os.environ.get("SLURM_JOB_ID") and not args.allow_no_slurm:
        print("ERROR: run extraction through SLURM for restricted clinical data.", file=sys.stderr)
        print("Use --allow-no-slurm only for synthetic/local smoke tests.", file=sys.stderr)
        return 2

    workers = detect_workers(args.workers)
    paths = [Path(item) for item in args.notes_jsonl_gz]
    output_root = Path(args.output_root)
    output_root.mkdir(parents=True, exist_ok=True)
    output_root.chmod(0o700)

    shards = shard_counts(paths, int(args.max_notes))
    tasks = build_tasks(shards, workers, int(args.notes_per_task), int(args.max_notes))
    total_planned = sum(int(task["planned_notes"]) for task in tasks)
    config = {
        "version": "auto_extraction_v1",
        "variant_name": args.variant_name,
        "workers": workers,
        "lazy_load_notes": True,
        "max_note_chars": int(args.max_note_chars),
        "output_mode": args.output_mode,
        "pre_umls_min_keep_score": float(args.pre_umls_min_keep_score),
        "gzip_compresslevel": int(args.gzip_compresslevel),
        "max_rejected_records": 0,
        "medcat_use_multi_texts": True,
        "medcat_batch_size": int(args.medcat_batch_size),
        "medcat_batch_size_chars": int(args.medcat_batch_size_chars),
        "task_count": len(tasks),
        "planned_notes": total_planned,
        "tasks": tasks,
        "resource_detection": {
            "requested_workers": int(args.workers),
            "effective_workers": workers,
            "slurm_job_id": os.environ.get("SLURM_JOB_ID"),
            "slurm_cpus_per_task": os.environ.get("SLURM_CPUS_PER_TASK"),
            "cpu_affinity_count": len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else None,
            "os_cpu_count": os.cpu_count(),
        },
    }
    config_path = output_root / f"{args.variant_name}.config.json"
    config_path.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    chmod_private(config_path)

    script_dir = Path(__file__).resolve().parent
    cmd = [
        sys.executable,
        str(script_dir / "extract_medcat_candidates_prefork4.py"),
        "--config",
        str(config_path),
        "--modelpack",
        str(Path(args.modelpack)),
        "--filter-policy",
        str(Path(args.filter_policy)),
        "--output-root",
        str(output_root),
        "--workers",
        str(workers),
        "--max-note-chars",
        str(args.max_note_chars),
        "--min-acc",
        str(args.min_acc),
        "--output-mode",
        args.output_mode,
        "--pre-umls-min-keep-score",
        str(args.pre_umls_min_keep_score),
        "--sentence-chars",
        str(args.sentence_chars),
        "--context-chars",
        str(args.context_chars),
        "--max-rejected-records",
        "0",
        "--progress-every",
        str(args.progress_every),
        "--gzip-compresslevel",
        str(args.gzip_compresslevel),
        "--medcat-use-multi-texts",
        "--medcat-batch-size",
        str(args.medcat_batch_size),
        "--medcat-batch-size-chars",
        str(args.medcat_batch_size_chars),
        "--seed",
        str(args.seed),
    ]
    if args.allow_no_slurm:
        cmd.append("--allow-no-slurm")

    run_manifest = {
        "config_path": str(config_path),
        "output_root": str(output_root),
        "planned_notes": total_planned,
        "workers": workers,
        "task_count": len(tasks),
        "output_mode": args.output_mode,
        "command": cmd,
        "dry_run": bool(args.dry_run),
    }
    manifest_path = output_root / f"{args.variant_name}.run_manifest.json"
    manifest_path.write_text(json.dumps(run_manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    chmod_private(manifest_path)
    print(json.dumps(run_manifest, indent=2, sort_keys=True), flush=True)

    if args.dry_run:
        return 0
    rc = subprocess.run(cmd, check=False).returncode
    if rc == 0 and args.delete_candidate_files:
        for candidate_path in output_root.glob(f"variant_{args.variant_name}/extraction/*/medcat_candidates.jsonl.gz"):
            candidate_path.unlink(missing_ok=True)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
