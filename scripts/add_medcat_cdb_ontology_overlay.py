#!/usr/bin/env python3
"""Add a strict MedCAT-CDB disease ontology overlay to candidate outputs.

This optional stage improves disease anchor coverage by using the licensed
MedCAT/SNOMED modelpack CDB as an exact-match ontology dictionary. It does not
use benchmark labels or private gold labels.

Run project-scale jobs on SLURM; loading a CDB can require substantial memory.
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from medcat.cdb import CDB


TARGET_ACTIONS = {"keep_candidate", "review_candidate", "value_parse", "context_only", "reject_candidate"}
DISEASE_TYPE_ID = "9090192"
TOKEN_RE = re.compile(r"[A-Za-z0-9]+(?:[-/][A-Za-z0-9]+)*")
NON_ALNUM = re.compile(r"[^a-z0-9]+")

GENERIC_STOP = {
    "abnormal",
    "abnormalities",
    "abnormality",
    "adverse effect",
    "adverse effects",
    "adverse reaction",
    "adverse reactions",
    "brain",
    "cancer",
    "cancers",
    "condition",
    "conditions",
    "disease",
    "diseases",
    "disorder",
    "disorders",
    "effect",
    "effects",
    "finding",
    "findings",
    "heart",
    "injury",
    "kidney",
    "liver",
    "lung",
    "pain",
    "renal",
    "sign",
    "signs",
    "sinus",
    "strain",
    "syndrome",
    "syndromes",
    "toxic",
    "toxicity",
    "tumor",
    "tumors",
    "tumour",
    "tumours",
    "vascular",
}

COMMON_WORDS = {
    "able",
    "acid",
    "active",
    "activity",
    "age",
    "after",
    "all",
    "and",
    "another",
    "are",
    "as",
    "at",
    "be",
    "blind",
    "by",
    "caffeine",
    "can",
    "case",
    "child",
    "children",
    "cm",
    "cocaine",
    "control",
    "cortex",
    "cross",
    "day",
    "development",
    "diet",
    "exposure",
    "family",
    "for",
    "from",
    "gave",
    "gene",
    "group",
    "high",
    "in",
    "low",
    "male",
    "map",
    "memory",
    "normal",
    "oct",
    "of",
    "on",
    "or",
    "patient",
    "patients",
    "secondary",
    "term",
    "the",
    "to",
    "type",
    "was",
    "with",
}

SINGLE_WORD_DISEASE_SUFFIXES = (
    "algia",
    "anemia",
    "aemia",
    "arrhythmia",
    "asthma",
    "cardia",
    "carcinoma",
    "dementia",
    "diabetes",
    "edema",
    "oedema",
    "emia",
    "embolism",
    "epilepsy",
    "fibrosis",
    "hemia",
    "haemia",
    "hemorrhage",
    "haemorrhage",
    "infection",
    "infarction",
    "ischemia",
    "ischaemia",
    "itis",
    "leukemia",
    "leukaemia",
    "lymphoma",
    "mania",
    "melanoma",
    "myopathy",
    "necrosis",
    "nephropathy",
    "neuropathy",
    "osis",
    "paresis",
    "pathy",
    "penia",
    "plasia",
    "plegia",
    "poisoning",
    "psychosis",
    "rrhea",
    "rrhoea",
    "sarcoma",
    "sclerosis",
    "sepsis",
    "thrombosis",
    "tension",
)
SINGLE_WORD_DISEASE_WHITELIST = {"anxiety", "catalepsy", "convulsion", "convulsions", "depression", "seizure", "seizures"}


@dataclass(frozen=True)
class TermEntry:
    norm: str
    display: str
    cui: str
    preferred_name: str
    is_upper: bool
    token_count: int
    status: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cdb-path", required=True, help="Path to licensed MedCAT CDB directory or CDB .dat/.zip")
    parser.add_argument("--notes-jsonl-gz", action="append", required=True, help="Notes JSONL(.gz); repeat with matching --candidate-jsonl/--output-dir")
    parser.add_argument("--candidate-jsonl", action="append", required=True, help="Existing candidate JSONL(.gz) file or directory")
    parser.add_argument("--output-dir", action="append", required=True, help="Output directory; repeat for each input")
    parser.add_argument("--name", action="append", default=[], help="Optional run name for each input")
    parser.add_argument("--summary-json", required=True)
    parser.add_argument("--max-words", type=int, default=8)
    parser.add_argument("--dump-lexicon-sample-json", default="")
    parser.add_argument("--allow-broad-single-word", action="store_true", help="Disable strict single-word filtering; usually not recommended")
    parser.add_argument("--allow-no-slurm", action="store_true", help="Developer escape hatch for tiny synthetic tests")
    return parser.parse_args()


def chmod_private(path: Path) -> None:
    try:
        path.chmod(0o600)
    except FileNotFoundError:
        pass


def mkdir_private(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except PermissionError:
        pass


def open_text(path: Path):
    if str(path).endswith(".gz"):
        return gzip.open(path, "rt", encoding="utf-8", errors="replace")
    return path.open("r", encoding="utf-8", errors="replace")


def open_output(path: Path):
    if str(path).endswith(".gz"):
        return gzip.open(path, "wt", encoding="utf-8", compresslevel=1)
    return path.open("w", encoding="utf-8")


def norm_phrase(text: str | None) -> str:
    text = (text or "").replace("~", " ").lower()
    return NON_ALNUM.sub(" ", text).strip()


def display_from_cdb_name(name: str) -> str:
    return re.sub(r"\s+", " ", name.replace("~", " ")).strip()


def disease_like_single_word(token: str, is_upper: bool) -> bool:
    if token in SINGLE_WORD_DISEASE_WHITELIST:
        return True
    if is_upper and 2 <= len(token) <= 8:
        return True
    return token.endswith(SINGLE_WORD_DISEASE_SUFFIXES)


def keep_term(display: str, norm: str, is_upper: bool, max_words: int, strict_single_word: bool) -> bool:
    if not norm or norm in GENERIC_STOP or norm in COMMON_WORDS:
        return False
    tokens = norm.split()
    if not tokens or len(tokens) > max_words or len(norm) > 90:
        return False
    if len(tokens) == 1:
        token = tokens[0]
        if token in GENERIC_STOP or token in COMMON_WORDS:
            return False
        if strict_single_word and not disease_like_single_word(token, is_upper):
            return False
        if len(token) <= 3 and not is_upper:
            return False
        if len(token) < 3:
            return False
    if norm.endswith(" finding") or norm.endswith(" disorder"):
        return False
    if re.fullmatch(r"\d+(?:\s+\d+)*", norm):
        return False
    return bool(display.strip())


def choose_entry(old: TermEntry | None, new: TermEntry) -> TermEntry:
    if old is None:
        return new
    old_score = (old.status == "P", old.is_upper, len(old.display), old.cui)
    new_score = (new.status == "P", new.is_upper, len(new.display), new.cui)
    return new if new_score > old_score else old


def build_disease_lexicon(cdb_path: str, max_words: int, strict_single_word: bool) -> tuple[dict[str, TermEntry], dict[str, Any]]:
    cdb = CDB.load(cdb_path)
    disease_type = cdb.type_id2info[DISEASE_TYPE_ID]
    disease_cuis = set(disease_type.cuis)
    lexicon: dict[str, TermEntry] = {}
    stats = Counter()
    stats["disease_cuis"] = len(disease_cuis)

    for raw_name, info in cdb.name2info.items():
        per_cui = info.get("per_cui_status") or {}
        disease_matches = [(cui, status) for cui, status in per_cui.items() if cui in disease_cuis]
        if not disease_matches:
            continue
        stats["disease_name_rows_seen"] += 1
        display = display_from_cdb_name(str(info.get("name") or raw_name))
        norm = norm_phrase(display)
        is_upper = bool(info.get("is_upper"))
        if not keep_term(display, norm, is_upper, max_words=max_words, strict_single_word=strict_single_word):
            stats["terms_filtered"] += 1
            continue
        for cui, status in disease_matches:
            cui_info = cdb.cui2info.get(cui, {})
            entry = TermEntry(
                norm=norm,
                display=display,
                cui=str(cui),
                preferred_name=str(cui_info.get("preferred_name") or display),
                is_upper=is_upper,
                token_count=len(norm.split()),
                status=str(status),
            )
            lexicon[norm] = choose_entry(lexicon.get(norm), entry)
            stats["term_cui_links_kept"] += 1

    stats["lexicon_terms"] = len(lexicon)
    stats["max_words"] = max_words
    stats["strict_single_word"] = strict_single_word
    return lexicon, dict(stats)


def load_notes(path: Path) -> dict[str, dict[str, str]]:
    notes: dict[str, dict[str, str]] = {}
    with open_text(path) as fh:
        for line_no, line in enumerate(fh, start=1):
            if not line.strip():
                continue
            row = json.loads(line)
            row_id = str(row.get("row_id") or row.get("ROW_ID") or row.get("note_id") or row.get("note_index") or "")
            text = row.get("text") or row.get("TEXT") or ""
            if not row_id:
                raise ValueError(f"Missing row_id/note_id in {path}:{line_no}")
            notes[row_id] = {k: str(v) for k, v in row.items()}
            notes[row_id]["text"] = text
    return notes


def prediction_files(path: Path) -> list[Path]:
    if path.is_file():
        return [path]
    return sorted(path.glob("**/medcat_candidates.jsonl")) + sorted(path.glob("**/medcat_candidates.jsonl.gz"))


def load_predictions(path: Path) -> list[dict[str, Any]]:
    rows = []
    files = prediction_files(path)
    if not files:
        raise FileNotFoundError(f"No medcat_candidates JSONL files found under {path}")
    for pred_file in files:
        with open_text(pred_file) as fh:
            for line in fh:
                if not line.strip():
                    continue
                row = json.loads(line)
                if row.get("filter_action") in TARGET_ACTIONS:
                    rows.append(row)
    return rows


def token_spans(text: str) -> list[tuple[int, int, str]]:
    return [(m.start(), m.end(), m.group(0)) for m in TOKEN_RE.finditer(text)]


def is_short_abbrev_match(surface: str, entry: TermEntry) -> bool:
    compact = re.sub(r"[^A-Za-z0-9]", "", surface)
    if entry.token_count > 1 or len(compact) > 4:
        return True
    return entry.is_upper and compact.isupper()


def find_ontology_matches(text: str, lexicon: dict[str, TermEntry], max_words: int) -> list[dict[str, Any]]:
    toks = token_spans(text)
    candidates = []
    for i in range(len(toks)):
        for j in range(i, min(len(toks), i + max_words)):
            start = toks[i][0]
            end = toks[j][1]
            surface = text[start:end]
            entry = lexicon.get(norm_phrase(surface))
            if entry is None or not is_short_abbrev_match(surface, entry):
                continue
            candidates.append({"span_start": start, "span_end": end, "span": surface, "entry": entry, "length": end - start})

    candidates.sort(key=lambda row: (-row["length"], row["span_start"], row["entry"].norm))
    occupied: list[tuple[int, int]] = []
    kept = []
    for row in candidates:
        start, end = int(row["span_start"]), int(row["span_end"])
        if any(max(start, os) < min(end, oe) for os, oe in occupied):
            continue
        occupied.append((start, end))
        kept.append(row)
    return sorted(kept, key=lambda row: (row["span_start"], row["span_end"]))


def overlaps(a_start: int, a_end: int, b_start: int, b_end: int) -> bool:
    return max(a_start, b_start) < min(a_end, b_end)


def make_ontology_row(match: dict[str, Any], note_id: str, note: dict[str, str]) -> dict[str, Any]:
    entry: TermEntry = match["entry"]
    return {
        "note_row_id": note_id,
        "subject_id": note.get("subject_id") or note.get("SUBJECT_ID") or note_id,
        "hadm_id": note.get("hadm_id") or note.get("HADM_ID") or note_id,
        "chartdate": note.get("chartdate") or "",
        "category": note.get("category") or note.get("CATEGORY") or "unknown",
        "section": "unknown",
        "span_start": match["span_start"],
        "span_end": match["span_end"],
        "span": match["span"],
        "concept_id": entry.cui,
        "concept_id_system": "SNOMED_CT",
        "pretty_name": entry.preferred_name,
        "type_ids": [DISEASE_TYPE_ID],
        "raw_linker_concept_id": entry.cui,
        "raw_linker_pretty_name": entry.preferred_name,
        "concept_override_applied": True,
        "linker_name": "medcat_cdb_disease_ontology_exact_overlay",
        "linker_score": 1.0,
        "score_bin": "06_0.99_1.00",
        "filter_action": "keep_candidate",
        "reason_codes": ["medcat_cdb_disease_ontology_match"],
        "filter_flags": ["ontology_lexicon", "clinical_priority_type_id"],
        "concept_category_preview": "disease_or_diagnosis",
        "assertion_status_preview": "present",
        "experiencer_preview": "patient",
        "temporality_preview": "current",
        "value_status": "not_applicable",
        "numeric_value": None,
        "unit": None,
        "comparator": None,
        "value_text": None,
        "value_parse_method": "not_applicable",
        "measurement_context": "not_applicable",
        "needs_review": False,
        "evidence": match["span"],
        "evidence_context": "",
        "value_context": "",
        "ontology_term_norm": entry.norm,
        "ontology_term_display": entry.display,
        "ontology_term_status": entry.status,
        "ontology_term_is_upper": entry.is_upper,
    }


def process_one(name: str, notes_path: Path, candidate_path: Path, output_dir: Path, lexicon: dict[str, TermEntry], max_words: int) -> dict[str, Any]:
    mkdir_private(output_dir)
    out_path = output_dir / "medcat_candidates.jsonl.gz"
    notes = load_notes(notes_path)
    base_rows = load_predictions(candidate_path)
    base_by_note: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in base_rows:
        base_by_note[str(row.get("note_row_id") or "")].append(row)

    added = 0
    dropped_overlap = 0
    match_counts = Counter()
    with open_output(out_path) as out_fh:
        for note_id, note in notes.items():
            matches = find_ontology_matches(note.get("text") or "", lexicon, max_words=max_words)
            for row in base_by_note.get(note_id, []):
                if row.get("concept_category_preview") == "disease_or_diagnosis":
                    try:
                        ps, pe = int(row.get("span_start")), int(row.get("span_end"))
                    except (TypeError, ValueError):
                        ps, pe = -1, -1
                    if any(overlaps(ps, pe, int(m["span_start"]), int(m["span_end"])) for m in matches):
                        dropped_overlap += 1
                        continue
                out_fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            for match in matches:
                out_fh.write(json.dumps(make_ontology_row(match, note_id, note), ensure_ascii=False) + "\n")
                added += 1
                match_counts[match["entry"].norm] += 1
        for note_id, rows in base_by_note.items():
            if note_id in notes:
                continue
            for row in rows:
                out_fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    chmod_private(out_path)

    stats = {
        "name": name,
        "notes": str(notes_path),
        "base_predictions": str(candidate_path),
        "output": str(out_path),
        "notes_loaded": len(notes),
        "base_rows": len(base_rows),
        "ontology_rows_added": added,
        "overlapping_disease_rows_dropped": dropped_overlap,
        "top_ontology_terms": match_counts.most_common(30),
    }
    stats_path = output_dir / "medcat_cdb_ontology_overlay_stats.json"
    stats_path.write_text(json.dumps(stats, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    chmod_private(stats_path)
    return stats


def main() -> int:
    args = parse_args()
    if not os.environ.get("SLURM_JOB_ID") and not args.allow_no_slurm:
        print("ERROR: Run ontology overlay through SLURM, or use --allow-no-slurm for tiny synthetic tests.", file=sys.stderr)
        return 2
    if not (len(args.notes_jsonl_gz) == len(args.candidate_jsonl) == len(args.output_dir)):
        print("ERROR: --notes-jsonl-gz, --candidate-jsonl, and --output-dir counts must match.", file=sys.stderr)
        return 2

    strict_single_word = not args.allow_broad_single_word
    lexicon, lexicon_stats = build_disease_lexicon(args.cdb_path, max_words=args.max_words, strict_single_word=strict_single_word)
    outputs = []
    for i, (notes_path, candidate_path, output_dir) in enumerate(zip(args.notes_jsonl_gz, args.candidate_jsonl, args.output_dir)):
        name = args.name[i] if i < len(args.name) else Path(output_dir).name
        outputs.append(process_one(name, Path(notes_path), Path(candidate_path), Path(output_dir), lexicon, args.max_words))

    summary = {
        "cdb_path": args.cdb_path,
        "max_words": args.max_words,
        "strict_single_word": strict_single_word,
        "lexicon_stats": lexicon_stats,
        "outputs": outputs,
    }
    summary_path = Path(args.summary_json)
    mkdir_private(summary_path.parent)
    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    chmod_private(summary_path)
    if args.dump_lexicon_sample_json:
        sample = [entry.__dict__ for entry in sorted(lexicon.values(), key=lambda e: (e.token_count, e.norm))[:10000]]
        sample_path = Path(args.dump_lexicon_sample_json)
        mkdir_private(sample_path.parent)
        sample_path.write_text(json.dumps(sample, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        chmod_private(sample_path)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
