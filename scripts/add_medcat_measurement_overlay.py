#!/usr/bin/env python3
"""Add a conservative measurement overlay for lab/vital aliases.

This stage is meant to recover common lab/vital anchors that MedCAT may miss
or over-expand, while keeping the output schema identical to the candidate
JSONL used by downstream evaluation and pair construction.
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import re
from collections import Counter, defaultdict
from functools import lru_cache
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
import sys

if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import extract_medcat_candidates as base  # noqa: E402


TARGET_ACTIONS = {"keep_candidate", "review_candidate", "value_parse"}
TARGET_CONCEPTS: dict[str, dict[str, Any]] = {
    "anc": {
        "concept_id": "CLINICAL_MEASUREMENT:ANC",
        "concept_id_system": "CLINICAL_MEASUREMENT",
        "pretty_name": "Absolute neutrophil count",
        "type_ids": ["91187746"],
        "aliases": ["anc", "absolute neutrophil count", "neutrophil count"],
    },
    "ca 19-9": {
        "concept_id": "CLINICAL_MEASUREMENT:CA19_9",
        "concept_id_system": "CLINICAL_MEASUREMENT",
        "pretty_name": "Cancer Antigen 19-9",
        "type_ids": ["91187746"],
        "aliases": ["ca 19-9", "ca19-9", "cancer antigen 19-9", "carbohydrate antigen 19-9"],
    },
    "temperature": {
        "concept_id": "722490005",
        "concept_id_system": "SNOMED_CT",
        "pretty_name": "Body temperature",
        "type_ids": ["2680757"],
        "aliases": ["temperature", "temp"],
    },
    "ph": {
        "concept_id": "264725002",
        "concept_id_system": "SNOMED_CT",
        "pretty_name": "Blood pH",
        "type_ids": ["2680757"],
        "aliases": ["ph", "blood ph", "abg ph", "arterial ph"],
    },
    "bicarbonate": {
        "concept_id": "88645003",
        "concept_id_system": "SNOMED_CT",
        "pretty_name": "Bicarbonate measurement",
        "type_ids": ["91187746"],
        "aliases": ["bicarbonate", "bicarb", "hco3", "co2"],
    },
    "potassium": {
        "concept_id": "88480006",
        "concept_id_system": "SNOMED_CT",
        "pretty_name": "Potassium measurement",
        "type_ids": ["91187746"],
        "aliases": ["potassium", "k"],
    },
    "procalcitonin": {
        "concept_id": "418752001",
        "concept_id_system": "SNOMED_CT",
        "pretty_name": "Procalcitonin measurement",
        "type_ids": ["91187746"],
        "aliases": ["procalcitonin", "pct"],
    },
    "na": {
        "concept_id": "25197003",
        "concept_id_system": "SNOMED_CT",
        "pretty_name": "Sodium measurement",
        "type_ids": ["2680757"],
        "aliases": ["na", "sodium"],
    },
    "bnp": {
        "concept_id": "390917008",
        "concept_id_system": "SNOMED_CT",
        "pretty_name": "Brain natriuretic peptide measurement",
        "type_ids": ["2680757"],
        "aliases": ["bnp", "brain natriuretic peptide"],
    },
    "wbc": {
        "concept_id": "767002",
        "concept_id_system": "SNOMED_CT",
        "pretty_name": "White blood cell count",
        "type_ids": ["2680757"],
        "aliases": ["wbc", "white blood cell count"],
    },
    "bp": {
        "concept_id": "75367002",
        "concept_id_system": "SNOMED_CT",
        "pretty_name": "Blood pressure",
        "type_ids": ["2680757"],
        "aliases": ["bp", "blood pressure"],
    },
    "hr": {
        "concept_id": "364075005",
        "concept_id_system": "SNOMED_CT",
        "pretty_name": "Heart rate",
        "type_ids": ["2680757"],
        "aliases": ["hr", "heart rate"],
    },
    "rr": {
        "concept_id": "86290005",
        "concept_id_system": "SNOMED_CT",
        "pretty_name": "Respiratory rate",
        "type_ids": ["2680757"],
        "aliases": ["rr", "respiratory rate"],
    },
    "lvef": {
        "concept_id": "250908004",
        "concept_id_system": "SNOMED_CT",
        "pretty_name": "Left ventricular ejection fraction",
        "type_ids": ["2680757"],
        "aliases": ["lvef", "ef", "ejection fraction", "left ventricular ejection fraction"],
    },
    "creatinine": {
        "concept_id": "15373003",
        "concept_id_system": "SNOMED_CT",
        "pretty_name": "Creatinine",
        "type_ids": ["91187746"],
        "aliases": ["creatinine", "cr"],
    },
    "troponin": {
        "concept_id": "102680009",
        "concept_id_system": "SNOMED_CT",
        "pretty_name": "Troponin",
        "type_ids": ["91187746"],
        "aliases": ["troponin"],
    },
    "glucose": {
        "concept_id": "67079006",
        "concept_id_system": "SNOMED_CT",
        "pretty_name": "Glucose",
        "type_ids": ["91187746"],
        "aliases": ["glucose", "fingerstick glucose", "fs glucose"],
    },
    "pt": {
        "concept_id": "396451008",
        "concept_id_system": "SNOMED_CT",
        "pretty_name": "Prothrombin time",
        "type_ids": ["2680757"],
        "aliases": ["pt", "prothrombin time"],
    },
    "ptt": {
        "concept_id": "42525009",
        "concept_id_system": "SNOMED_CT",
        "pretty_name": "Partial thromboplastin time, activated",
        "type_ids": ["2680757"],
        "aliases": ["ptt", "partial thromboplastin time", "activated partial thromboplastin time"],
    },
    "inr": {
        "concept_id": "165581004",
        "concept_id_system": "SNOMED_CT",
        "pretty_name": "International normalized ratio",
        "type_ids": ["2680757"],
        "aliases": ["inr", "international normalized ratio"],
    },
    "spo2": {
        "concept_id": "431314004",
        "concept_id_system": "SNOMED_CT",
        "pretty_name": "Peripheral oxygen saturation",
        "type_ids": ["2680757"],
        "aliases": ["spo2", "sao2", "o2 sat", "oxygen saturation"],
    },
    "sao2": {
        "concept_id": "442476006",
        "concept_id_system": "SNOMED_CT",
        "pretty_name": "Arterial oxygen saturation",
        "type_ids": ["2680757"],
        "aliases": ["sao2", "spo2", "o2 sat", "oxygen saturation"],
    },
}


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--notes-jsonl-gz", action="append", required=True)
    p.add_argument("--candidate-jsonl", action="append", required=True)
    p.add_argument("--output-dir", required=True)
    p.add_argument("--summary-json", required=True)
    p.add_argument("--name", default="measurement_overlay")
    p.add_argument("--workers", type=int, default=1)
    return p.parse_args()


def open_text(path: Path):
    if str(path).endswith(".gz"):
        return gzip.open(path, "rt", encoding="utf-8", errors="replace")
    return path.open("r", encoding="utf-8", errors="replace")


def open_output(path: Path):
    if str(path).endswith(".gz"):
        return gzip.open(path, "wt", encoding="utf-8", compresslevel=1)
    return path.open("w", encoding="utf-8")


def chmod_private(path: Path) -> None:
    try:
        path.chmod(0o600)
    except FileNotFoundError:
        pass


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with open_text(path) as fh:
        for line in fh:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def iter_prediction_files(path: Path) -> list[Path]:
    if path.is_file():
        return [path]
    return sorted(path.glob("**/medcat_candidates.jsonl")) + sorted(path.glob("**/medcat_candidates.jsonl.gz"))


def overlap(a_start: int, a_end: int, b_start: int, b_end: int) -> int:
    return max(0, min(a_end, b_end) - max(a_start, b_start))


def alias_patterns() -> dict[str, re.Pattern[str]]:
    patterns: dict[str, re.Pattern[str]] = {}
    for key, spec in TARGET_CONCEPTS.items():
        for alias in spec["aliases"]:
            alias_key = base.norm_key(alias)
            if not alias_key:
                continue
            patterns.setdefault(alias_key, re.compile(rf"(?i)(?<![A-Za-z0-9]){re.escape(alias_key)}(?![A-Za-z0-9])"))
    return patterns


@lru_cache(maxsize=1024)
def spec_for_alias_key(alias_key: str) -> dict[str, Any] | None:
    if alias_key in TARGET_CONCEPTS:
        return TARGET_CONCEPTS[alias_key]
    for spec in TARGET_CONCEPTS.values():
        aliases = {base.norm_key(alias) for alias in spec["aliases"]}
        if alias_key in aliases:
            return spec
    return None


def find_note_rows(notes_paths: list[Path]) -> dict[str, dict[str, Any]]:
    notes: dict[str, dict[str, Any]] = {}
    for path in notes_paths:
        with open_text(path) as fh:
            for line in fh:
                if not line.strip():
                    continue
                row = json.loads(line)
                note_id = str(row.get("row_id") or row.get("note_id") or row.get("ROW_ID") or "")
                text = str(row.get("text") or row.get("TEXT") or "")
                if note_id:
                    notes[note_id] = {
                        "text": text,
                        "subject_id": str(row.get("subject_id") or row.get("SUBJECT_ID") or ""),
                        "hadm_id": str(row.get("hadm_id") or row.get("HADM_ID") or ""),
                        "chartdate": str(row.get("chartdate") or row.get("CHARTDATE") or ""),
                        "category": str(row.get("category") or row.get("CATEGORY") or "unknown"),
                    }
    return notes


def repair_existing_row(row: dict[str, Any], note_text: str) -> tuple[dict[str, Any], bool]:
    raw_span = str(row.get("span") or "")
    concept = base.leading_lab_alias(raw_span)
    if not concept:
        return row, False
    if row.get("filter_action") not in TARGET_ACTIONS:
        return row, False
    start = int(row.get("span_start") or 0)
    end = int(row.get("span_end") or start)
    span, new_start, new_end, alias = base.normalize_lab_value_span(raw_span, start, end)
    alias = alias or concept
    spec = spec_for_alias_key(alias)
    if not spec:
        return row, False
    sentence = base.sentence_for_span(note_text, new_start, new_end, 420)
    context_raw, ctx_start, ctx_end = base.context_with_marker(note_text, new_start, new_end)
    context, ctx_start, ctx_end = base.clean_context_with_span(context_raw, ctx_start, ctx_end)
    value_parse = base.parse_lab_value(alias, spec["pretty_name"], context, ctx_start, ctx_end,
                                      note_text=note_text, note_start=new_start, note_end=new_end)
    if not value_parse.get("numeric_value"):
        return row, False
    updated = dict(row)
    updated["span_start"] = new_start
    updated["span_end"] = new_end
    updated["span"] = span
    updated["concept_id"] = spec["concept_id"]
    updated["concept_id_system"] = spec["concept_id_system"]
    updated["pretty_name"] = spec["pretty_name"]
    updated["type_ids"] = list(spec["type_ids"])
    updated["concept_category_preview"] = "lab_result_or_value"
    updated["filter_action"] = "value_parse"
    updated["reason_codes"] = sorted(set([*(updated.get("reason_codes") or []), "measurement_overlay_repair", "lab_or_vital_value_parsed"]))
    updated["filter_flags"] = sorted(set([*(updated.get("filter_flags") or []), "lab_or_vital_span", "measurement_overlay"]))
    updated["assertion_status_preview"] = "not_applicable"
    updated["value_status"] = value_parse.get("value_status")
    updated["numeric_value"] = value_parse.get("numeric_value")
    updated["unit"] = value_parse.get("unit")
    updated["comparator"] = value_parse.get("comparator")
    updated["value_text"] = value_parse.get("value_text")
    updated["value_parse_method"] = value_parse.get("value_parse_method")
    updated["measurement_context"] = value_parse.get("measurement_context")
    updated["needs_review"] = True
    updated["evidence"] = sentence
    updated["evidence_context"] = base.clip(context, 650)
    updated["value_context"] = base.clip(context, 650)
    updated["span_normalization_applied"] = True
    updated["raw_linker_span"] = raw_span
    updated["raw_linker_pretty_name"] = row.get("raw_linker_pretty_name") or row.get("pretty_name") or ""
    updated["raw_linker_concept_id"] = row.get("raw_linker_concept_id") or row.get("concept_id") or ""
    updated["linker_name"] = row.get("linker_name") or "measurement_overlay"
    return updated, True


def add_missing_rows(
    note_id: str,
    note_meta: dict[str, Any],
    existing: list[dict[str, Any]],
    pattern_map: dict[str, re.Pattern[str]],
) -> list[dict[str, Any]]:
    note_text = str(note_meta.get("text") or "")
    output = list(existing)
    spans = [(int(r.get("span_start") or 0), int(r.get("span_end") or 0)) for r in existing]
    seen = {(int(r.get("span_start") or 0), int(r.get("span_end") or 0), base.norm_key(r.get("span") or "")) for r in existing}
    for alias_key, pattern in pattern_map.items():
        spec = spec_for_alias_key(alias_key)
        if not spec:
            continue
        for match in pattern.finditer(note_text):
            start, end = match.start(), match.end()
            overlapping_indices = []
            skip_new_row = False
            for idx, row in enumerate(output):
                rs = int(row.get("span_start") or 0)
                re_ = int(row.get("span_end") or 0)
                ov = overlap(start, end, rs, re_)
                if ov <= 0:
                    continue
                if row.get("filter_action") == "value_parse":
                    skip_new_row = True
                    break
                row_cat = base.norm_key(row.get("concept_category_preview") or "")
                row_len = max(1, re_ - rs)
                if row_cat != "lab_result_or_value" and ov >= row_len:
                    overlapping_indices.append(idx)
            if skip_new_row:
                continue
            if overlapping_indices:
                remove_set = set(overlapping_indices)
                output = [row for idx, row in enumerate(output) if idx not in remove_set]
                spans = [(int(r.get("span_start") or 0), int(r.get("span_end") or 0)) for r in output]
                seen = {(int(r.get("span_start") or 0), int(r.get("span_end") or 0), base.norm_key(r.get("span") or "")) for r in output}
            key = (start, end, base.norm_key(note_text[start:end]))
            if key in seen:
                continue
            sentence = base.sentence_for_span(note_text, start, end, 420)
            context_raw, ctx_start, ctx_end = base.context_with_marker(note_text, start, end)
            context, ctx_start, ctx_end = base.clean_context_with_span(context_raw, ctx_start, ctx_end)
            value_parse = base.parse_lab_value(alias_key, spec["pretty_name"], context, ctx_start, ctx_end,
                                              note_text=note_text, note_start=start, note_end=end)
            if not value_parse.get("numeric_value"):
                continue
            row = {
                "note_row_id": note_id,
                "subject_id": note_meta.get("subject_id") or "",
                "hadm_id": note_meta.get("hadm_id") or "",
                "chartdate": note_meta.get("chartdate") or "",
                "category": note_meta.get("category") or "unknown",
                "section": base.section_for_span(note_text, start),
                "span_start": start,
                "span_end": end,
                "span": note_text[start:end],
                "concept_id": spec["concept_id"],
                "concept_id_system": spec["concept_id_system"],
                "pretty_name": spec["pretty_name"],
                "type_ids": list(spec["type_ids"]),
                "raw_linker_concept_id": spec["concept_id"],
                "raw_linker_pretty_name": spec["pretty_name"],
                "raw_linker_span": note_text[start:end],
                "concept_override_applied": False,
                "span_normalization_applied": False,
                "linker_name": "medcat_umls_snomed_v2_measurement_overlay",
                "linker_score": 1.0,
                "score_bin": "06_0.99_1.00",
                "filter_action": "value_parse",
                "reason_codes": ["measurement_alias_overlay", "lab_or_vital_value_parsed"],
                "filter_flags": ["lab_or_vital_span", "measurement_overlay"],
                "concept_category_preview": "lab_result_or_value",
                "assertion_status_preview": "not_applicable",
                "experiencer_preview": "patient",
                "temporality_preview": "current",
                "anatomical_location_cue": None,
                "anatomical_location_text": None,
                "modifier_cue": None,
                "modifier_text": None,
                "trend_cue": None,
                "trend_text": None,
                "value_status": value_parse.get("value_status"),
                "numeric_value": value_parse.get("numeric_value"),
                "unit": value_parse.get("unit"),
                "comparator": value_parse.get("comparator"),
                "value_text": value_parse.get("value_text"),
                "value_parse_method": value_parse.get("value_parse_method"),
                "measurement_context": value_parse.get("measurement_context"),
                "needs_review": True,
                "evidence": sentence,
                "evidence_context": base.clip(context, 650),
                "value_context": base.clip(context, 650),
            }
            output.append(row)
            spans.append((start, end))
            seen.add(key)
    return output


_NOTE_STATE = None


def init_note_worker(notes, by_note, pattern_map):
    global _NOTE_STATE
    _NOTE_STATE = (notes, by_note, pattern_map)


def process_note(note_id):
    notes, by_note, pattern_map = _NOTE_STATE
    note_meta = notes.get(note_id) or {}
    note_text = str(note_meta.get("text") or "")
    repaired_rows = []
    repaired_count = 0
    aliases = Counter()
    for row in by_note[note_id]:
        if note_text and row.get("filter_action") in TARGET_ACTIONS:
            repaired, did_repair = repair_existing_row(row, note_text)
            if did_repair:
                repaired_rows.append(repaired)
                repaired_count += 1
                aliases[base.leading_lab_alias(repaired.get("span") or "") or "unknown"] += 1
                continue
        repaired_rows.append(row)
    rows = add_missing_rows(note_id, note_meta, repaired_rows, pattern_map) if note_text else repaired_rows
    rows.sort(key=lambda r: (int(r.get("span_start") or 0), int(r.get("span_end") or 0), str(r.get("span") or "")))
    output = "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows)
    return output, repaired_count, max(0, len(rows) - len(by_note[note_id])), aliases, len(rows)


def main() -> int:
    args = parse_args()
    if args.workers < 1:
        raise SystemExit("--workers must be positive")
    if len(args.notes_jsonl_gz) != len(args.candidate_jsonl):
        raise SystemExit("--notes-jsonl-gz and --candidate-jsonl counts must match")

    notes = find_note_rows([Path(p) for p in args.notes_jsonl_gz])
    pattern_map = alias_patterns()
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        out_dir.chmod(0o700)
    except PermissionError:
        pass
    out_path = out_dir / "medcat_candidates.jsonl.gz"

    summary = {"repaired_rows": 0, "added_rows": 0, "alias_counts": Counter()}
    by_note: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for candidate in args.candidate_jsonl:
        for path in iter_prediction_files(Path(candidate)):
            with open_text(path) as fh:
                for line in fh:
                    if not line.strip():
                        continue
                    row = json.loads(line)
                    note_id = str(row.get("note_row_id") or "")
                    by_note[note_id].append(row)

    from note_workers import ordered_note_results
    total_rows = 0
    with open_output(out_path) as out_fh:
        for output, repaired, added, aliases, count in ordered_note_results(
                process_note, iter(sorted(by_note)), workers=args.workers,
                initializer=init_note_worker, initargs=(notes, by_note, pattern_map)):
            out_fh.write(output)
            summary["repaired_rows"] += repaired
            summary["added_rows"] += added
            summary["alias_counts"].update(aliases)
            total_rows += count
    global _NOTE_STATE
    _NOTE_STATE = None

    summary_path = Path(args.summary_json)
    summary_path.write_text(json.dumps({
        "name": args.name,
        "output": str(out_path),
        "summary": {
            "repaired_rows": summary["repaired_rows"],
            "added_rows": summary["added_rows"],
            "alias_counts": dict(summary["alias_counts"]),
            "total_rows": total_rows,
        },
    }, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    chmod_private(out_path)
    chmod_private(summary_path)
    print(json.dumps({"output": str(out_path), "summary": {"repaired_rows": summary["repaired_rows"], "added_rows": summary["added_rows"], "total_rows": total_rows}}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
