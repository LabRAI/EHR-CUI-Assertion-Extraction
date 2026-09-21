#!/usr/bin/env python3
"""Run the validated extraction stack with adaptive CPU linking."""

from __future__ import annotations

import argparse
import gzip
import json
import os
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

from site_profile import load_site_profile, profile_sha256


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--notes-jsonl-gz", action="append", required=True)
    parser.add_argument("--modelpack", required=True)
    parser.add_argument("--cdb-path", required=True, help="Licensed MedCAT CDB directory or CDB file")
    parser.add_argument("--filter-policy", required=True)
    parser.add_argument("--output-root", required=True)
    parser.add_argument("--variant-name", default="final_extraction")
    parser.add_argument("--workers", type=int, default=0, help="0 auto-detects the allocated CPUs")
    parser.add_argument("--notes-per-task", type=int, default=0)
    parser.add_argument("--max-notes", type=int, default=0)
    parser.add_argument("--max-note-chars", type=int, default=7000)
    parser.add_argument("--linker-min-acc", type=float, default=0.0)
    parser.add_argument("--pre-umls-min-keep-score", type=float, default=0.99)
    parser.add_argument("--sentence-chars", type=int, default=420)
    parser.add_argument("--context-chars", type=int, default=650)
    parser.add_argument("--medcat-batch-size", type=int, default=-1)
    parser.add_argument("--medcat-batch-size-chars", type=int, default=1_000_000)
    parser.add_argument("--gzip-compresslevel", type=int, default=1)
    parser.add_argument("--progress-every", type=int, default=1000)
    parser.add_argument("--ontology-max-words", type=int, default=8)
    parser.add_argument(
        "--ontology-overlap-mode",
        choices=["replace", "add_missing"],
        default="add_missing",
    )
    parser.add_argument("--ontology-allow-broad-single-word", action="store_true")
    parser.add_argument(
        "--refinement-strictness",
        choices=["conservative", "aggressive"],
        default="aggressive",
    )
    parser.add_argument("--disable-refinement-strict-gate", action="store_true")
    parser.add_argument("--disable-boundary-ranker", action="store_true")
    parser.add_argument("--disable-anchor-rescue", action="store_true")
    parser.add_argument("--disable-assertion-judge", action="store_true")
    parser.add_argument("--keep-selected-notes", action="store_true")
    parser.add_argument("--site-profile", help="Optional frozen site_profile.json built on calibration notes")
    parser.add_argument("--deployment-config-sha256", help="SHA-256 of the collaborator run config")
    parser.add_argument("--allow-no-slurm", action="store_true", help="Synthetic/local testing only")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def open_text(path: Path):
    if str(path).endswith(".gz"):
        return gzip.open(path, "rt", encoding="utf-8", errors="replace")
    return path.open("r", encoding="utf-8", errors="replace")


def run(command: list[str]) -> None:
    print("[final-pipeline] " + " ".join(command), flush=True)
    subprocess.run(command, check=True)


def task_intervals(tasks: Iterable[dict[str, Any]]) -> dict[Path, list[tuple[int, int]]]:
    grouped: dict[Path, list[tuple[int, int]]] = defaultdict(list)
    for task in tasks:
        grouped[Path(task["notes_jsonl_gz"])].append(
            (int(task["start_note_index"]), int(task["end_note_index"]))
        )
    return grouped


def write_selected_notes(tasks: list[dict[str, Any]], output_path: Path) -> int:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.parent.chmod(0o700)
    selected = 0
    with gzip.open(output_path, "wt", encoding="utf-8", compresslevel=1) as output:
        for source, intervals in task_intervals(tasks).items():
            intervals = sorted(intervals)
            nonempty_index = 0
            with open_text(source) as handle:
                for line in handle:
                    if not line.strip():
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    text = str(row.get("text") or row.get("TEXT") or "")
                    if not text.strip():
                        continue
                    nonempty_index += 1
                    if any(start <= nonempty_index <= end for start, end in intervals):
                        output.write(json.dumps(row, ensure_ascii=False) + "\n")
                        selected += 1
    output_path.chmod(0o600)
    return selected


def concatenate_candidates(paths: Iterable[Path], output_path: Path) -> int:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.parent.chmod(0o700)
    rows = 0
    with gzip.open(output_path, "wt", encoding="utf-8", compresslevel=1) as output:
        for path in paths:
            if not path.is_file():
                raise FileNotFoundError(path)
            with open_text(path) as handle:
                for line in handle:
                    if line.strip():
                        output.write(line if line.endswith("\n") else line + "\n")
                        rows += 1
    output_path.chmod(0o600)
    return rows


def main() -> int:
    args = parse_args()
    if not os.environ.get("SLURM_JOB_ID") and not args.allow_no_slurm:
        print("ERROR: run final extraction through SLURM.", file=sys.stderr)
        return 2

    script_dir = Path(__file__).resolve().parent
    site_profile = load_site_profile(args.site_profile)
    output_root = Path(args.output_root)
    output_root.mkdir(parents=True, exist_ok=True)
    output_root.chmod(0o700)

    linker_command = [
        sys.executable,
        str(script_dir / "run_extraction_auto.py"),
    ]
    for notes_path in args.notes_jsonl_gz:
        linker_command.extend(["--notes-jsonl-gz", notes_path])
    linker_command.extend(
        [
            "--modelpack", args.modelpack,
            "--filter-policy", args.filter_policy,
            "--output-root", str(output_root),
            "--variant-name", args.variant_name,
            "--workers", str(args.workers),
            "--notes-per-task", str(args.notes_per_task),
            "--max-notes", str(args.max_notes),
            "--max-note-chars", str(args.max_note_chars),
            "--min-acc", str(args.linker_min_acc),
            "--pre-umls-min-keep-score", str(args.pre_umls_min_keep_score),
            "--sentence-chars", str(args.sentence_chars),
            "--context-chars", str(args.context_chars),
            "--output-mode", "all_non_rejected",
            "--medcat-batch-size", str(args.medcat_batch_size),
            "--medcat-batch-size-chars", str(args.medcat_batch_size_chars),
            "--gzip-compresslevel", str(args.gzip_compresslevel),
            "--progress-every", str(args.progress_every),
        ]
    )
    if args.allow_no_slurm:
        linker_command.append("--allow-no-slurm")
    if args.dry_run:
        linker_command.append("--dry-run")
    run(linker_command)
    if args.dry_run:
        print("[final-pipeline] dry run stops before post-linker stages", flush=True)
        return 0

    config_path = output_root / f"{args.variant_name}.config.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    tasks = list(config.get("tasks") or [])
    if not tasks:
        raise RuntimeError(f"no explicit tasks in {config_path}")

    variant_root = output_root / f"variant_{args.variant_name}"
    work_root = variant_root / "_work"
    selected_notes = work_root / "selected_notes.jsonl.gz"
    linked_candidates = variant_root / "01_linker" / "medcat_candidates.jsonl.gz"
    candidate_paths = [
        variant_root / "extraction" / str(task["task_id"]) / "medcat_candidates.jsonl.gz"
        for task in tasks
    ]
    selected_count = write_selected_notes(tasks, selected_notes)
    linked_rows = concatenate_candidates(candidate_paths, linked_candidates)

    measurement = variant_root / "02_measurement"
    ontology = variant_root / "03_ontology"
    assertion = variant_root / "04_assertion"
    final = variant_root / "05_final"
    reports = variant_root / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    reports.chmod(0o700)

    measurement_command = [
        sys.executable, str(script_dir / "add_medcat_measurement_overlay.py"),
        "--notes-jsonl-gz", str(selected_notes),
        "--candidate-jsonl", str(linked_candidates),
        "--output-dir", str(measurement),
        "--summary-json", str(reports / "measurement_summary.json"),
        "--name", args.variant_name,
    ]
    if args.site_profile:
        measurement_command.extend(["--site-profile", args.site_profile])
    run(measurement_command)
    ontology_command = [
        sys.executable, str(script_dir / "add_medcat_cdb_ontology_overlay.py"),
        "--cdb-path", args.cdb_path,
        "--notes-jsonl-gz", str(selected_notes),
        "--candidate-jsonl", str(measurement / "medcat_candidates.jsonl.gz"),
        "--output-dir", str(ontology),
        "--summary-json", str(reports / "ontology_summary.json"),
        "--max-words", str(args.ontology_max_words),
        "--overlap-mode", args.ontology_overlap_mode,
    ]
    if args.ontology_allow_broad_single_word:
        ontology_command.append("--allow-broad-single-word")
    if args.allow_no_slurm:
        ontology_command.append("--allow-no-slurm")
    run(ontology_command)
    assertion_command = [
        sys.executable, str(script_dir / "apply_assertion_scope_overlay.py"),
        "--input", str(ontology / "medcat_candidates.jsonl.gz"),
        "--output", str(assertion / "medcat_candidates.jsonl.gz"),
        "--summary-json", str(reports / "assertion_summary.json"),
    ]
    if args.site_profile:
        assertion_command.extend(["--site-profile", args.site_profile])
    run(assertion_command)
    refinement_command = [
        sys.executable, str(script_dir / "refine_tracka_candidates.py"),
        "--notes-jsonl-gz", str(selected_notes),
        "--candidate-jsonl", str(assertion / "medcat_candidates.jsonl.gz"),
        "--output-dir", str(final),
        "--summary-json", str(reports / "final_refinement_summary.json"),
        "--name", args.variant_name,
        "--strictness", args.refinement_strictness,
    ]
    for disabled, flag in (
        (args.disable_refinement_strict_gate, "--disable-strict-gate"),
        (args.disable_boundary_ranker, "--disable-boundary-ranker"),
        (args.disable_anchor_rescue, "--disable-anchor-rescue"),
        (args.disable_assertion_judge, "--disable-assertion-judge"),
    ):
        if disabled:
            refinement_command.append(flag)
    run(refinement_command)

    manifest = {
        "pipeline": "final_extraction_v1",
        "variant": args.variant_name,
        "slurm_job_id": os.environ.get("SLURM_JOB_ID"),
        "workers": config.get("workers"),
        "linker_output_mode": config.get("output_mode"),
        "selected_notes": selected_count,
        "linked_candidate_rows": linked_rows,
        "site_adaptation_mode": site_profile["adaptation_mode"],
        "site_profile_sha256": profile_sha256(args.site_profile),
        "deployment_config_sha256": args.deployment_config_sha256,
        "hyperparameters": {
            "linker": {
                "max_note_chars": args.max_note_chars,
                "minimum_confidence": args.linker_min_acc,
                "pre_umls_min_keep_score": args.pre_umls_min_keep_score,
                "sentence_chars": args.sentence_chars,
                "context_chars": args.context_chars,
                "medcat_batch_size": args.medcat_batch_size,
                "medcat_batch_size_chars": args.medcat_batch_size_chars,
                "gzip_compresslevel": args.gzip_compresslevel,
            },
            "ontology": {
                "max_words": args.ontology_max_words,
                "overlap_mode": args.ontology_overlap_mode,
                "allow_broad_single_word": args.ontology_allow_broad_single_word,
            },
            "refinement": {
                "strictness": args.refinement_strictness,
                "strict_gate": not args.disable_refinement_strict_gate,
                "boundary_ranker": not args.disable_boundary_ranker,
                "anchor_rescue": not args.disable_anchor_rescue,
                "assertion_judge": not args.disable_assertion_judge,
            },
        },
        "final_candidates": str(final / "medcat_candidates.jsonl.gz"),
        "reports": str(reports),
    }
    manifest_path = variant_root / "final_pipeline_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    manifest_path.chmod(0o600)
    if not args.keep_selected_notes:
        selected_notes.unlink(missing_ok=True)
    print(json.dumps(manifest, indent=2, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
