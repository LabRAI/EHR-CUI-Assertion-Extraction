#!/usr/bin/env python3
"""Build a target-specific SNOMED CT -> UMLS CUI crosswalk from MRCONSO.RRF.

This script expects a locally staged, licensed UMLS Metathesaurus release. It
does not download terminology data or handle credentials. Run candidate-scale
jobs through SLURM.
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


SNOMED_ID = re.compile(r"^[0-9]{5,18}$")
UMLS_CUI = re.compile(r"^C[0-9]{7}$")
MRCONSO_COLUMNS = {
    "CUI": 0,
    "LAT": 1,
    "TS": 2,
    "LUI": 3,
    "STT": 4,
    "SUI": 5,
    "ISPREF": 6,
    "AUI": 7,
    "SAUI": 8,
    "SCUI": 9,
    "SDUI": 10,
    "SAB": 11,
    "TTY": 12,
    "CODE": 13,
    "STR": 14,
    "SRL": 15,
    "SUPPRESS": 16,
    "CVF": 17,
}
PREFERRED_TTYS = {"PT", "FN", "PN", "SY"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mrconso", required=True, help="Licensed UMLS META/MRCONSO.RRF(.gz)")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--candidate-jsonl", action="append", default=[], help="Candidate JSONL to collect SNOMED IDs from")
    parser.add_argument("--target-id-file", action="append", default=[], help="One SNOMED concept ID per line")
    parser.add_argument("--annotate-output-jsonl", help="Optional JSONL path for candidate records annotated with UMLS mapping")
    parser.add_argument("--allow-full", action="store_true", help="Build mapping for all SNOMED rows if no target IDs are provided")
    parser.add_argument("--max-unmatched-examples", type=int, default=500)
    parser.add_argument("--progress-every", type=int, default=1_000_000)
    parser.add_argument("--gzip-compresslevel", type=int, default=3)
    parser.add_argument("--allow-no-slurm", action="store_true", help="Developer escape hatch; do not use for project-scale processing.")
    return parser.parse_args()


def chmod_private(path: Path) -> None:
    try:
        path.chmod(0o600)
    except FileNotFoundError:
        pass


def open_text(path: Path):
    if str(path).endswith(".gz"):
        return gzip.open(path, "rt", encoding="utf-8", errors="replace")
    return path.open("r", encoding="utf-8", errors="replace")


def open_output_text(path: Path, compresslevel: int = 3):
    if str(path).endswith(".gz"):
        return gzip.open(path, "wt", encoding="utf-8", compresslevel=compresslevel)
    return path.open("w", encoding="utf-8")


def iter_jsonl(path: Path):
    with open_text(path) as fh:
        for line_no, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSON on {path}:{line_no}: {exc}") from exc


def collect_target_ids(candidate_paths: list[Path], target_files: list[Path]) -> tuple[set[str], dict[str, Any]]:
    target_ids: set[str] = set()
    stats: dict[str, Any] = {
        "candidate_files": [str(p) for p in candidate_paths],
        "target_id_files": [str(p) for p in target_files],
        "candidate_records_read": 0,
        "candidate_snomed_ids_seen": 0,
        "candidate_snomed_ids_unique": 0,
        "target_file_ids_seen": 0,
    }

    for path in target_files:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                concept_id = line.strip()
                if SNOMED_ID.match(concept_id):
                    target_ids.add(concept_id)
                    stats["target_file_ids_seen"] += 1

    for path in candidate_paths:
        for record in iter_jsonl(path):
            stats["candidate_records_read"] += 1
            for field in ("concept_id", "raw_linker_concept_id"):
                concept_id = str(record.get(field) or "")
                if SNOMED_ID.match(concept_id):
                    target_ids.add(concept_id)
                    stats["candidate_snomed_ids_seen"] += 1

    stats["candidate_snomed_ids_unique"] = len(target_ids)
    return target_ids, stats


def row_score(lat: str, ts: str, ispref: str, sab: str, tty: str, suppress: str) -> int:
    score = 0
    score += 100 if suppress == "N" else -25
    score += 20 if lat == "ENG" else 0
    score += 12 if sab == "SNOMEDCT_US" else 6 if sab.startswith("SNOMEDCT") else 0
    score += 8 if tty in PREFERRED_TTYS else 0
    score += 4 if ispref == "Y" else 0
    score += 2 if ts == "P" else 0
    return score


def snomed_ids_from_mrconso_row(cols: list[str]) -> set[str]:
    ids = set()
    for field in ("CODE", "SCUI", "SDUI"):
        value = cols[MRCONSO_COLUMNS[field]].strip()
        if SNOMED_ID.match(value):
            ids.add(value)
    return ids


def add_mapping_row(mapping: dict[str, dict[str, dict[str, Any]]], snomed_id: str, cols: list[str]) -> None:
    cui = cols[MRCONSO_COLUMNS["CUI"]]
    if not UMLS_CUI.match(cui):
        return
    lat = cols[MRCONSO_COLUMNS["LAT"]]
    ts = cols[MRCONSO_COLUMNS["TS"]]
    ispref = cols[MRCONSO_COLUMNS["ISPREF"]]
    sab = cols[MRCONSO_COLUMNS["SAB"]]
    tty = cols[MRCONSO_COLUMNS["TTY"]]
    suppress = cols[MRCONSO_COLUMNS["SUPPRESS"]]
    name = cols[MRCONSO_COLUMNS["STR"]]
    score = row_score(lat, ts, ispref, sab, tty, suppress)

    cui_info = mapping[snomed_id][cui]
    cui_info["row_count"] = cui_info.get("row_count", 0) + 1
    cui_info["score"] = cui_info.get("score", 0) + score
    if suppress == "N":
        cui_info["non_suppressed_rows"] = cui_info.get("non_suppressed_rows", 0) + 1
    else:
        cui_info["suppressed_rows"] = cui_info.get("suppressed_rows", 0) + 1
    best_score = cui_info.get("best_row_score", -10_000)
    if score > best_score:
        cui_info.update(
            {
                "best_row_score": score,
                "best_name": name,
                "best_sab": sab,
                "best_tty": tty,
                "best_suppress": suppress,
                "best_code": cols[MRCONSO_COLUMNS["CODE"]],
                "best_scui": cols[MRCONSO_COLUMNS["SCUI"]],
            }
        )


def build_crosswalk(mrconso_path: Path, target_ids: set[str] | None, progress_every: int) -> tuple[dict[str, dict[str, dict[str, Any]]], dict[str, Any]]:
    mapping: dict[str, dict[str, dict[str, Any]]] = defaultdict(lambda: defaultdict(dict))
    stats: dict[str, Any] = {
        "mrconso": str(mrconso_path),
        "mrconso_rows_read": 0,
        "snomed_source_rows_seen": 0,
        "snomed_rows_kept": 0,
        "malformed_rows": 0,
        "sabs_seen": Counter(),
    }

    with open_text(mrconso_path) as fh:
        for line in fh:
            stats["mrconso_rows_read"] += 1
            if progress_every > 0 and stats["mrconso_rows_read"] % progress_every == 0:
                print(f"[progress] mrconso_rows_read={stats['mrconso_rows_read']}", flush=True)
            cols = line.rstrip("\n").split("|")
            if len(cols) < 18:
                stats["malformed_rows"] += 1
                continue
            sab = cols[MRCONSO_COLUMNS["SAB"]]
            if not sab.startswith("SNOMEDCT"):
                continue
            stats["snomed_source_rows_seen"] += 1
            stats["sabs_seen"][sab] += 1
            row_snomed_ids = snomed_ids_from_mrconso_row(cols)
            if target_ids is not None:
                row_snomed_ids &= target_ids
            if not row_snomed_ids:
                continue
            stats["snomed_rows_kept"] += 1
            for snomed_id in row_snomed_ids:
                add_mapping_row(mapping, snomed_id, cols)

    return mapping, stats


def summarize_crosswalk(mapping: dict[str, dict[str, dict[str, Any]]], target_ids: set[str] | None, max_unmatched: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    multiplicity = Counter()
    for snomed_id in sorted(mapping, key=lambda x: int(x)):
        cui_rows = []
        for cui, info in mapping[snomed_id].items():
            cui_rows.append(
                {
                    "cui": cui,
                    "row_count": info.get("row_count", 0),
                    "non_suppressed_rows": info.get("non_suppressed_rows", 0),
                    "suppressed_rows": info.get("suppressed_rows", 0),
                    "score": info.get("score", 0),
                    "name": info.get("best_name", ""),
                    "sab": info.get("best_sab", ""),
                    "tty": info.get("best_tty", ""),
                    "suppress": info.get("best_suppress", ""),
                    "code": info.get("best_code", ""),
                    "scui": info.get("best_scui", ""),
                }
            )
        cui_rows.sort(key=lambda row: (-int(row["score"]), row["cui"]))
        multiplicity[len(cui_rows)] += 1
        rows.append(
            {
                "snomed_id": snomed_id,
                "preferred_cui": cui_rows[0]["cui"] if cui_rows else None,
                "mapping_status": "single_cui" if len(cui_rows) == 1 else "multiple_cuis",
                "cui_count": len(cui_rows),
                "cuis": cui_rows,
            }
        )

    unmatched = sorted((target_ids or set()) - set(mapping), key=lambda x: int(x))
    stats = {
        "mapped_snomed_ids": len(mapping),
        "target_snomed_ids": len(target_ids) if target_ids is not None else None,
        "unmatched_snomed_ids": len(unmatched),
        "unmatched_snomed_id_examples": unmatched[:max_unmatched],
        "cui_multiplicity_counts": dict(sorted(multiplicity.items())),
    }
    return rows, stats


def annotate_candidates(
    candidate_paths: list[Path],
    out_path: Path,
    row_by_snomed: dict[str, dict[str, Any]],
    gzip_compresslevel: int,
) -> dict[str, Any]:
    stats = Counter()
    stats["candidate_files_read"] = len(candidate_paths)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.parent.chmod(0o700)
    with open_output_text(out_path, gzip_compresslevel) as out_fh:
        for candidate_path in candidate_paths:
            with open_text(candidate_path) as in_fh:
                for line in in_fh:
                    if not line.strip():
                        continue
                    record = json.loads(line)
                    stats["records_read"] += 1
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
                            record["umls_mapping_status"] = "snomed_not_found_in_mrconso"
                            record["umls_mapping_needs_review"] = True
                            stats["snomed_not_found"] += 1
                        else:
                            candidates = [item["cui"] for item in row["cuis"]]
                            record["umls_cui"] = row["preferred_cui"]
                            record["umls_cui_candidates"] = candidates
                            record["umls_mapping_status"] = f"snomed_{row['mapping_status']}"
                            record["umls_mapping_needs_review"] = len(candidates) != 1
                            stats[record["umls_mapping_status"]] += 1
                    else:
                        record["umls_cui"] = None
                        record["umls_cui_candidates"] = []
                        record["umls_mapping_status"] = "non_snomed_non_umls_or_missing"
                        record["umls_mapping_needs_review"] = True
                        stats["non_snomed_non_umls_or_missing"] += 1
                    out_fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    chmod_private(out_path)
    return dict(stats)


def main() -> int:
    args = parse_args()
    if not os.environ.get("SLURM_JOB_ID") and not args.allow_no_slurm:
        print("ERROR: Run this mapping job through SLURM for project data.", file=sys.stderr)
        return 2

    mrconso_path = Path(args.mrconso)
    out_dir = Path(args.output_dir)
    candidate_paths = [Path(p) for p in args.candidate_jsonl]
    target_files = [Path(p) for p in args.target_id_file]

    for path in [mrconso_path, *candidate_paths, *target_files]:
        if not path.exists():
            print(f"ERROR missing input: {path}", file=sys.stderr)
            return 2

    out_dir.mkdir(parents=True, exist_ok=True)
    out_dir.chmod(0o700)

    target_ids, target_stats = collect_target_ids(candidate_paths, target_files)
    target_set = None if args.allow_full and not target_ids else target_ids
    if target_set is not None and not target_set:
        print("ERROR: No SNOMED target IDs found. Provide --target-id-file, --candidate-jsonl, or --allow-full.", file=sys.stderr)
        return 2

    mapping, parse_stats = build_crosswalk(mrconso_path, target_set, args.progress_every)
    rows, crosswalk_stats = summarize_crosswalk(mapping, target_set, args.max_unmatched_examples)

    crosswalk_path = out_dir / "snomed_to_umls_crosswalk.jsonl"
    with crosswalk_path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    chmod_private(crosswalk_path)

    target_ids_path = out_dir / "target_snomed_ids.txt"
    with target_ids_path.open("w", encoding="utf-8") as fh:
        for concept_id in sorted(target_ids, key=lambda x: int(x)):
            fh.write(concept_id + "\n")
    chmod_private(target_ids_path)

    row_by_snomed = {row["snomed_id"]: row for row in rows}
    annotation_stats = {}
    if args.annotate_output_jsonl:
        annotation_stats = annotate_candidates(
            candidate_paths,
            Path(args.annotate_output_jsonl),
            row_by_snomed,
            args.gzip_compresslevel,
        )

    stats = {
        "target_collection": target_stats,
        "mrconso_parse": {**parse_stats, "sabs_seen": dict(parse_stats["sabs_seen"])},
        "crosswalk": crosswalk_stats,
        "annotation": annotation_stats,
        "outputs": {
            "crosswalk": str(crosswalk_path),
            "target_snomed_ids": str(target_ids_path),
            "annotated_candidates": args.annotate_output_jsonl,
        },
    }
    stats_path = out_dir / "snomed_to_umls_mapping_stats.json"
    stats_path.write_text(json.dumps(stats, indent=2, ensure_ascii=False), encoding="utf-8")
    chmod_private(stats_path)

    print(json.dumps({"output_dir": str(out_dir), **crosswalk_stats, "stats": str(stats_path)}, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
