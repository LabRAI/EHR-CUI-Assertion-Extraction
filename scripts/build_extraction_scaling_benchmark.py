#!/usr/bin/env python3
"""Create configs and SLURM scripts for extraction runtime scaling benchmarks.

This helper is intentionally site-configurable. It does not hard-code project
paths, SLURM accounts, or licensed modelpack locations.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path


def parse_csv_ints(text: str) -> list[int]:
    return [int(item.strip()) for item in text.split(",") if item.strip()]


def load_manifest(path: Path) -> list[dict]:
    rows: list[dict] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def build_tasks(manifest: list[dict], total_notes: int, workers: int) -> list[dict]:
    """Split the first total_notes nonempty notes into worker-visible chunks."""

    notes_remaining = total_notes
    raw_segments: list[tuple[int, str, int, int]] = []
    for row in manifest:
        if notes_remaining <= 0:
            break
        shard_notes = int(row["notes"])
        take = min(shard_notes, notes_remaining)
        raw_segments.append((int(row["shard_id"]), str(row["path"]), 1, take))
        notes_remaining -= take
    if notes_remaining > 0:
        raise ValueError(f"requested {total_notes} notes exceeds manifest capacity")

    chunk_target = max(100, math.ceil(total_notes / max(1, workers)))
    tasks: list[dict] = []
    global_task_index = 0
    for shard_id, path, start, end in raw_segments:
        pos = start
        local_chunk = 0
        while pos <= end:
            chunk_end = min(end, pos + chunk_target - 1)
            tasks.append(
                {
                    "task_id": f"shard_{shard_id:06d}_chunk_{local_chunk:03d}",
                    "notes_jsonl_gz": path,
                    "shard_idx": shard_id,
                    "chunk_idx": local_chunk,
                    "start_note_index": pos,
                    "end_note_index": chunk_end,
                    "planned_notes": chunk_end - pos + 1,
                    "global_task_index": global_task_index,
                }
            )
            pos = chunk_end + 1
            local_chunk += 1
            global_task_index += 1
    return tasks


def time_limit_for_notes(total_notes: int, workers: int) -> str:
    if total_notes <= 1_000:
        return "02:00:00"
    if total_notes <= 10_000:
        return "06:00:00" if workers <= 1 else "03:00:00"
    if total_notes <= 100_000:
        return "2-00:00:00" if workers <= 1 else "1-00:00:00"
    if total_notes <= 500_000:
        return "5-00:00:00" if workers <= 1 else "3-00:00:00"
    return "7-00:00:00"


def mem_for_workers(workers: int) -> str:
    gb = max(48, min(128, 20 + workers * 12))
    return f"{gb}G"


def sbatch_directive(name: str, value: str | None) -> str:
    return f"#SBATCH --{name}={value}\n" if value else ""


def write_config(
    total_notes: int,
    workers: int,
    stage: str,
    manifest: list[dict],
    config_dir: Path,
) -> Path:
    total_available = sum(int(row["notes"]) for row in manifest)
    capped_notes = min(total_notes, total_available)
    variant = f"scaling_{stage}_n{capped_notes}_cpu{workers}_prefork_batchlinker"
    cfg = {
        "version": "portable_extraction_scaling",
        "stage": stage,
        "variant_name": variant,
        "requested_notes": total_notes,
        "actual_planned_notes": capped_notes,
        "full_nonempty_notes": total_available,
        "workers": workers,
        "lazy_load_notes": True,
        "max_note_chars": 7000,
        "pre_umls_min_keep_score": 0.99,
        "gzip_compresslevel": 1,
        "max_rejected_records": 0,
        "medcat_use_multi_texts": True,
        "medcat_batch_size": -1,
        "medcat_batch_size_chars": 1_000_000,
        "tasks": build_tasks(manifest, capped_notes, workers),
        "resource_profile": {
            "cpus_per_task": workers,
            "mem": mem_for_workers(workers),
            "execution_strategy": "parent_load_once_then_fork_workers_with_get_entities_multi_texts",
        },
    }
    cfg["task_count"] = len(cfg["tasks"])
    path = config_dir / f"{variant}.json"
    path.write_text(json.dumps(cfg, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    path.chmod(0o600)
    return path


def write_sbatch(
    config_path: Path,
    workers: int,
    stage: str,
    args: argparse.Namespace,
    keep_candidates: bool = False,
) -> Path:
    cfg = json.loads(config_path.read_text(encoding="utf-8"))
    variant = cfg["variant_name"]
    bench_root = Path(args.benchmark_root) / variant
    time_limit = time_limit_for_notes(int(cfg["actual_planned_notes"]), workers)
    mem = mem_for_workers(workers)
    sbatch_path = Path(args.slurm_dir) / f"{variant}.sbatch"
    cleanup = "" if keep_candidates else "\nfind \"${BENCH_ROOT}/variant_${VARIANT}/extraction\" -name 'medcat_candidates.jsonl.gz' -type f -delete\n"

    text = f"""#!/bin/bash
#SBATCH --job-name=xs_{stage}_n{cfg['actual_planned_notes']}_c{workers}
{sbatch_directive('account', args.account)}{sbatch_directive('qos', args.qos)}{sbatch_directive('partition', args.partition)}#SBATCH --cpus-per-task={workers}
#SBATCH --mem={mem}
#SBATCH --time={time_limit}
#SBATCH --output={args.log_dir}/%x_%j.out
#SBATCH --error={args.log_dir}/%x_%j.err

set -euo pipefail
umask 077

PROJECT_DIR={args.project_dir}
CONFIG={config_path}
BENCH_ROOT={bench_root}
LOG_DIR={args.log_dir}
MODELPACK={args.modelpack}
POLICY={args.policy}
VENV_DIR={args.venv}
TMPDIR=${{PROJECT_DIR}}/tmp/${{SLURM_JOB_ID}}
PYTHONIOENCODING=utf-8
OMP_NUM_THREADS=1
MALLOC_ARENA_MAX=2
VARIANT={variant}
export TMPDIR PYTHONIOENCODING OMP_NUM_THREADS MALLOC_ARENA_MAX VARIANT

mkdir -p "${{BENCH_ROOT}}" "${{LOG_DIR}}" "${{TMPDIR}}"
chmod 700 "${{BENCH_ROOT}}" "${{LOG_DIR}}" "${{TMPDIR}}"
cd "${{PROJECT_DIR}}"

{args.module_load_line}
source "${{VENV_DIR}}/bin/activate"

echo "[$(date)] stage=extraction_scaling variant=${{VARIANT}} host=$(hostname) cpus=${{SLURM_CPUS_PER_TASK}}"
/usr/bin/time -v -o "${{BENCH_ROOT}}/extract_gnu_time.txt" \\
python "${{PROJECT_DIR}}/scripts/extract_medcat_candidates_prefork4.py" \\
  --config "${{CONFIG}}" \\
  --modelpack "${{MODELPACK}}" \\
  --filter-policy "${{POLICY}}" \\
  --output-root "${{BENCH_ROOT}}" \\
  --workers {workers} \\
  --max-note-chars 7000 \\
  --min-acc 0.0 \\
  --pre-umls-min-keep-score 0.99 \\
  --sentence-chars 420 \\
  --context-chars 650 \\
  --max-rejected-records 0 \\
  --progress-every 1000 \\
  --gzip-compresslevel 1 \\
  --medcat-use-multi-texts \\
  --medcat-batch-size -1 \\
  --medcat-batch-size-chars 1000000 \\
  --seed 20260608
{cleanup}
chmod -R go-rwx "${{BENCH_ROOT}}" "${{LOG_DIR}}"
rm -rf "${{TMPDIR}}"
echo "[$(date)] stage=extraction_scaling complete variant=${{VARIANT}}"
"""
    sbatch_path.write_text(text, encoding="utf-8")
    sbatch_path.chmod(0o700)
    return sbatch_path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-dir", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--benchmark-root", required=True)
    parser.add_argument("--config-dir", required=True)
    parser.add_argument("--log-dir", required=True)
    parser.add_argument("--slurm-dir", required=True)
    parser.add_argument("--modelpack", required=True)
    parser.add_argument("--policy", required=True)
    parser.add_argument("--venv", required=True)
    parser.add_argument("--stage", default="stage1")
    parser.add_argument("--note-counts", default="1000,10000,100000")
    parser.add_argument("--cpus", default="1,4,6")
    parser.add_argument("--account", default="")
    parser.add_argument("--qos", default="")
    parser.add_argument("--partition", default="")
    parser.add_argument("--module-load-line", default="module load python/3.11")
    parser.add_argument("--keep-candidates", action="store_true")
    args = parser.parse_args()

    for dirname in (args.benchmark_root, args.config_dir, args.log_dir, args.slurm_dir):
        path = Path(dirname)
        path.mkdir(parents=True, exist_ok=True)
        path.chmod(0o700)

    manifest = load_manifest(Path(args.manifest))
    total_available = sum(int(row["notes"]) for row in manifest)
    note_counts = [total_available if item.strip().lower() == "full" else int(item) for item in args.note_counts.split(",")]
    cpus = parse_csv_ints(args.cpus)

    rows: list[dict] = []
    for note_count in note_counts:
        for workers in cpus:
            config_path = write_config(note_count, workers, args.stage, manifest, Path(args.config_dir))
            sbatch_path = write_sbatch(config_path, workers, args.stage, args, keep_candidates=args.keep_candidates)
            cfg = json.loads(config_path.read_text(encoding="utf-8"))
            rows.append(
                {
                    "stage": args.stage,
                    "requested_notes": note_count,
                    "actual_planned_notes": cfg["actual_planned_notes"],
                    "cpus": workers,
                    "tasks": cfg["task_count"],
                    "config": str(config_path),
                    "sbatch": str(sbatch_path),
                    "benchmark_dir": str(Path(args.benchmark_root) / cfg["variant_name"]),
                }
            )

    plan_path = Path(args.benchmark_root) / "scaling_sweep_plan.json"
    plan_path.write_text(json.dumps({"sweep": rows}, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    plan_path.chmod(0o600)
    print(json.dumps({"plan": str(plan_path), "jobs": rows}, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
