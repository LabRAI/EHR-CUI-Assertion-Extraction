#!/usr/bin/env python3
"""Run the fixed, validated Harvard default extraction pipeline."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent
DEFAULT_OUTPUT_ROOT = PROJECT_DIR / "harvard_default_output"
DEFAULT_POLICY = PROJECT_DIR / "configs" / "linker_filter_policy.example.json"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the fixed Harvard default extraction pipeline."
    )
    parser.add_argument(
        "--notes-jsonl-gz",
        action="append",
        required=True,
        help="Private note JSONL/JSONL.GZ file; repeat for additional files.",
    )
    parser.add_argument("--modelpack", required=True, help="Licensed MedCAT modelpack ZIP")
    parser.add_argument("--cdb-path", required=True, help="Licensed MedCAT CDB directory or file")
    parser.add_argument(
        "--output-root",
        default=str(DEFAULT_OUTPUT_ROOT),
        help="Private output directory; defaults to ./harvard_default_output",
    )
    parser.add_argument("--variant-name", default="harvard_default")
    parser.add_argument("--max-notes", type=int, default=0, help="0 means all notes")
    parser.add_argument("--progress-every", type=int, default=1000)
    parser.add_argument("--keep-selected-notes", action="store_true")
    parser.add_argument("--allow-no-slurm", action="store_true", help="Synthetic smoke tests only")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def require_path(value: str, label: str) -> str:
    path = Path(value).expanduser().resolve(strict=False)
    if not path.exists():
        raise FileNotFoundError(f"{label} does not exist: {path}")
    return str(path)


def main() -> int:
    args = parse_args()
    if not os.environ.get("SLURM_JOB_ID") and not args.allow_no_slurm:
        print(
            "ERROR: run Harvard extraction through SLURM; this wrapper refuses login-node execution.",
            file=sys.stderr,
        )
        return 2

    try:
        notes = [require_path(value, "notes file") for value in args.notes_jsonl_gz]
        modelpack = require_path(args.modelpack, "MedCAT modelpack")
        cdb_path = require_path(args.cdb_path, "MedCAT CDB")
        policy = require_path(str(DEFAULT_POLICY), "packaged filter policy")
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    output_root = Path(args.output_root).expanduser().resolve(strict=False)

    command = [
        sys.executable,
        str(SCRIPT_DIR / "run_final_extraction.py"),
    ]
    for path in notes:
        command.extend(["--notes-jsonl-gz", path])
    command.extend(
        [
            "--modelpack", modelpack,
            "--cdb-path", cdb_path,
            "--filter-policy", policy,
            "--output-root", str(output_root),
            "--variant-name", args.variant_name,
            "--workers", "0",
            "--notes-per-task", "0",
            "--max-notes", str(args.max_notes),
            "--max-note-chars", "7000",
            "--medcat-batch-size", "-1",
            "--medcat-batch-size-chars", "1000000",
            "--progress-every", str(args.progress_every),
        ]
    )
    if args.keep_selected_notes:
        command.append("--keep-selected-notes")
    if args.allow_no_slurm:
        command.append("--allow-no-slurm")
    if args.dry_run:
        command.append("--dry-run")

    print("[harvard-default] fixed preset: 4-CPU allocation is set by SLURM", flush=True)
    print("[harvard-default] " + " ".join(command), flush=True)
    return subprocess.run(command, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
