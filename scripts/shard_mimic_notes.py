#!/usr/bin/env python3
"""Stream MIMIC-III NOTEEVENTS from the staged zip into private JSONL shards."""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import os
import sys
import zipfile
from collections import Counter
from pathlib import Path
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--zip", required=True, help="Path to a locally staged MIMIC-III database zip")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--notes-per-shard", type=int, default=5000)
    parser.add_argument("--max-notes", type=int, default=0, help="0 means all nonempty notes")
    parser.add_argument("--compresslevel", type=int, default=3)
    parser.add_argument("--progress-every", type=int, default=100000)
    parser.add_argument("--allow-no-slurm", action="store_true")
    return parser.parse_args()


def chmod_private(path: Path, mode: int = 0o600) -> None:
    try:
        path.chmod(mode)
    except FileNotFoundError:
        pass


def noteevents_member(zip_path: Path) -> str:
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
        candidates = [name for name in names if name.upper().endswith("NOTEEVENTS.CSV.GZ")]
        candidates += [name for name in names if name.upper().endswith("NOTEEVENTS.CSV")]
        if not candidates:
            raise FileNotFoundError("NOTEEVENTS.csv(.gz) not found inside MIMIC zip")
        return candidates[0]


def iter_noteevents(zip_path: Path):
    try:
        csv.field_size_limit(sys.maxsize)
    except OverflowError:
        csv.field_size_limit(2**31 - 1)

    member = noteevents_member(zip_path)
    with zipfile.ZipFile(zip_path) as zf:
        with zf.open(member) as raw:
            if member.upper().endswith(".GZ"):
                fh = gzip.open(raw, mode="rt", encoding="utf-8", errors="replace", newline="")
                close_fh = True
            else:
                fh = (line.decode("utf-8", errors="replace") for line in raw)
                close_fh = False
            try:
                reader = csv.DictReader(fh)
                for csv_row_number, row in enumerate(reader, start=1):
                    yield csv_row_number, row
            finally:
                if close_fh:
                    fh.close()


def normalize_note(row: dict[str, str], csv_row_number: int, note_index: int) -> dict[str, Any]:
    return {
        "note_index": note_index,
        "csv_row_number": csv_row_number,
        "row_id": row.get("ROW_ID", ""),
        "subject_id": row.get("SUBJECT_ID", ""),
        "hadm_id": row.get("HADM_ID", ""),
        "chartdate": row.get("CHARTDATE", ""),
        "charttime": row.get("CHARTTIME", ""),
        "storetime": row.get("STORETIME", ""),
        "category": row.get("CATEGORY", "") or "unknown",
        "description": row.get("DESCRIPTION", ""),
        "cgid": row.get("CGID", ""),
        "iserror": row.get("ISERROR", ""),
        "text": row.get("TEXT", "") or "",
    }


def open_shard(path: Path, compresslevel: int):
    return gzip.open(path, "wt", encoding="utf-8", compresslevel=compresslevel)


def main() -> int:
    args = parse_args()
    if not os.environ.get("SLURM_JOB_ID") and not args.allow_no_slurm:
        print("ERROR: Run MIMIC note sharding through SLURM.", file=sys.stderr)
        return 2
    if args.notes_per_shard <= 0:
        print("ERROR: --notes-per-shard must be > 0", file=sys.stderr)
        return 2
    if not 1 <= args.compresslevel <= 9:
        print("ERROR: --compresslevel must be between 1 and 9", file=sys.stderr)
        return 2

    zip_path = Path(args.zip)
    out_dir = Path(args.output_dir)
    if not zip_path.exists():
        print(f"ERROR missing MIMIC zip: {zip_path}", file=sys.stderr)
        return 2
    out_dir.mkdir(parents=True, exist_ok=True)
    out_dir.chmod(0o700)

    manifest_rows: list[dict[str, Any]] = []
    category_counts = Counter()
    row_count = 0
    nonempty_notes = 0
    skipped_empty = 0
    total_text_chars = 0
    shard_id = -1
    shard_fh = None
    shard_path: Path | None = None
    shard_note_count = 0
    shard_first_index = None
    shard_last_index = None
    shard_category_counts = Counter()
    shard_text_chars = 0

    def close_current_shard() -> None:
        nonlocal shard_fh, shard_path, shard_note_count, shard_first_index, shard_last_index, shard_category_counts, shard_text_chars
        if shard_fh is None or shard_path is None:
            return
        shard_fh.close()
        chmod_private(shard_path)
        manifest_rows.append(
            {
                "shard_id": shard_id,
                "path": str(shard_path),
                "notes": shard_note_count,
                "first_note_index": shard_first_index,
                "last_note_index": shard_last_index,
                "text_chars": shard_text_chars,
                "category_counts": dict(shard_category_counts),
                "bytes": shard_path.stat().st_size,
            }
        )
        shard_fh = None
        shard_path = None
        shard_note_count = 0
        shard_first_index = None
        shard_last_index = None
        shard_category_counts = Counter()
        shard_text_chars = 0

    def open_next_shard() -> None:
        nonlocal shard_id, shard_fh, shard_path
        shard_id += 1
        shard_path = out_dir / f"notes_shard_{shard_id:06d}.jsonl.gz"
        shard_fh = open_shard(shard_path, args.compresslevel)

    try:
        for csv_row_number, row in iter_noteevents(zip_path):
            row_count += 1
            text = row.get("TEXT", "") or ""
            if not text.strip():
                skipped_empty += 1
                continue
            if args.max_notes and nonempty_notes >= args.max_notes:
                break
            if shard_fh is None or shard_note_count >= args.notes_per_shard:
                close_current_shard()
                open_next_shard()

            nonempty_notes += 1
            note = normalize_note(row, csv_row_number, nonempty_notes)
            assert shard_fh is not None
            shard_fh.write(json.dumps(note, ensure_ascii=False) + "\n")

            category = note["category"]
            text_len = len(note["text"])
            category_counts[category] += 1
            total_text_chars += text_len
            shard_note_count += 1
            shard_first_index = shard_first_index or nonempty_notes
            shard_last_index = nonempty_notes
            shard_category_counts[category] += 1
            shard_text_chars += text_len

            if args.progress_every > 0 and nonempty_notes % args.progress_every == 0:
                print(
                    json.dumps(
                        {
                            "progress_nonempty_notes": nonempty_notes,
                            "csv_rows_read": row_count,
                            "current_shard_id": shard_id,
                        }
                    ),
                    flush=True,
                )
    finally:
        close_current_shard()

    manifest_path = out_dir / "manifest.jsonl"
    with manifest_path.open("w", encoding="utf-8") as fh:
        for row in manifest_rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    chmod_private(manifest_path)

    summary = {
        "zip": str(zip_path),
        "noteevents_member": noteevents_member(zip_path),
        "output_dir": str(out_dir),
        "notes_per_shard": args.notes_per_shard,
        "max_notes": args.max_notes,
        "compresslevel": args.compresslevel,
        "csv_rows_read": row_count,
        "nonempty_notes_written": nonempty_notes,
        "skipped_empty_text_rows": skipped_empty,
        "total_text_chars": total_text_chars,
        "num_shards": len(manifest_rows),
        "category_counts": dict(category_counts),
        "manifest": str(manifest_path),
        "shards": manifest_rows[:5],
        "last_shard": manifest_rows[-1] if manifest_rows else None,
    }
    summary_path = out_dir / "shard_summary.json"
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    chmod_private(summary_path)

    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
