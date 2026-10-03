#!/usr/bin/env python3
"""Extract schema-aware MedCAT linker candidates from MIMIC notes.

This is not a final CUI/assertion pair extractor. It runs MedCAT, applies the
schema-aware policy, and writes bounded candidate records for later NuExtract
input design and manual/LLM review.

Run only on a compute node through SLURM.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import os
import random
import cached_regex as re
import sys
import zipfile
from collections import Counter, defaultdict
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable


SPACE = re.compile(r"\s+")
SECTION_HEADER = re.compile(r"^\s*[A-Za-z][A-Za-z0-9 /,_()#&+-]{1,80}:?\s*$")
KNOWN_SECTION_HEADERS = {
    "action",
    "allergies",
    "assessment",
    "balance",
    "brief hospital course",
    "case presentation",
    "case report",
    "chief complaint",
    "clinical presentation",
    "discharge diagnosis",
    "discharge medications",
    "diagnostic assessment",
    "differential diagnosis",
    "family history",
    "functional status / home / family assessment",
    "follow-up",
    "follow up",
    "history of present illness",
    "imaging",
    "investigations",
    "laboratory data",
    "labs",
    "medications",
    "medications on admission",
    "narrative / plan",
    "outcome",
    "outcome and follow-up",
    "patient information",
    "past medical history",
    "past surgical history",
    "physical exam",
    "presentation",
    "review of systems",
    "social history",
    "status",
    "therapeutic intervention",
    "timeline",
    "treatment",
    "vitals",
}
NUMERIC_CONTEXT = re.compile(
    r"(?i)(?:<=|>=|<|>)?\s*(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?:/\d+(?:\.\d+)?(?:\(\d+\))?)?"
    r"\s*(?:%|percent|mg/dl|mg/l(?:\s*feu)?|mmol/l|meq/l|k/u?l|cells/u?l|x10\^?3|x10\^?9/l|10\^?9/l|g/dl|gm/dl|g/l|"
    r"beats?/min(?:ute)?|breaths?/min(?:ute)?|ratio|p?h|sec|seconds?|s|bpm|mmhg|mm hg|degrees?\s*c|deg(?:ree)?s?\s*c|"
    r"cmh2o|iu|u/l|ng/ml|mcg/ml|pg/ml|mg/hr|insp/min|/min|per[_ ]?min)?"
)
NEGATION_CUE = re.compile(
    r"\b(no|not|none|neither|nor|denies|denied|without|w/o|negative for|negative|"
    r"free of|ruled out|no evidence of|no signs? of|absen(?:t|ce)|lack of|"
    r"did not (?:show|reveal|demonstrate)|does not (?:show|reveal|demonstrate)|"
    r"failed to (?:show|reveal|demonstrate)|unremarkable for|normal)\b",
    re.I,
)
POSSIBLE_CUE = re.compile(
    r"\b(possible|possibly|probable|probably|suspected|suspicious for|concern for|"
    r"concerning for|question of|rule out|r/o|cannot rule out|cannot exclude|"
    r"cannot be excluded|may have|may represent|may reflect|might represent|"
    r"could represent|could reflect|suggestive of|compatible with|consistent with|"
    r"vs\.?|versus)\b",
    re.I,
)
CONDITIONAL_CUE = re.compile(
    r"\b(if|unless|when|whenever|provided that|in case of|prn|as needed|"
    r"if needed|if necessary|hold if|call if|return if|use as needed|"
    r"take as needed)\b",
    re.I,
)
HYPOTHETICAL_CUE = re.compile(r"\b(should|would|consider|monitor for|watch for|plan to|return for)\b", re.I)
HISTORICAL_CUE = re.compile(
    r"\b(history of|h/o|hx of|past medical history|past surgical history|pmh|pmhx|"
    r"previous|previously|prior|remote|former|status post|s/p|underwent|"
    r"was diagnosed|were diagnosed|had been diagnosed)\b",
    re.I,
)
FAMILY_CUE = re.compile(r"\b(family history|fhx|mother|father|sister|brother|daughter|son|parent|maternal|paternal|aunt|uncle|wife|husband)\b", re.I)
SOCIAL_CUE = re.compile(r"\b(social history|smok(?:e|er|ing)|tobacco|alcohol|ethanol|drug use|lives with|homeless|occupation|job|married)\b", re.I)

ANATOMY_CUE_PATTERNS = [
    ("cardiac", re.compile(r"\b(cardiac|heart|myocard(?:ial)?|coronary|atri(?:al|um)|ventric(?:le|ular)|aortic|mitral|tricuspid)\b", re.I)),
    ("pulmonary", re.compile(r"\b(pulmonary|lung|lungs|chest|pleural|bronch(?:us|ial)?|respiratory)\b", re.I)),
    ("renal", re.compile(r"\b(renal|kidney|nephr(?:ic|ology)|ureter|bladder)\b", re.I)),
    ("hepatic", re.compile(r"\b(hepatic|liver|biliary|gallbladder)\b", re.I)),
    ("gastrointestinal", re.compile(r"\b(gastric|stomach|bowel|colon|intestinal|abdomen|abdominal|pancreas|esophag(?:us|eal))\b", re.I)),
    ("neurologic", re.compile(r"\b(neuro(?:logic|logical)?|brain|cerebral|spinal|cranial|seizure|stroke)\b", re.I)),
    ("extremity", re.compile(r"\b(arm|leg|hand|foot|knee|hip|shoulder|elbow|ankle|wrist|extremit(?:y|ies))\b", re.I)),
    ("skin", re.compile(r"\b(skin|wound|ulcer|rash|lesion|incision)\b", re.I)),
]
MODIFIER_CUE_PATTERNS = [
    ("mild", re.compile(r"\b(mild|mildly|trace|minimal)\b", re.I)),
    ("moderate", re.compile(r"\b(moderate|moderately)\b", re.I)),
    ("severe", re.compile(r"\b(severe|severely|marked|markedly|critical)\b", re.I)),
    ("acute", re.compile(r"\b(acute|acutely|new onset|new-onset)\b", re.I)),
    ("chronic", re.compile(r"\b(chronic|chronically|long[- ]standing)\b", re.I)),
    ("stable", re.compile(r"\b(stable|unchanged|baseline)\b", re.I)),
]
TREND_CUE_PATTERNS = [
    ("improving", re.compile(r"\b(improved|improving|better|resolving|decreased|decreasing|downtrending|down-trending|down to)\b", re.I)),
    ("worsening", re.compile(r"\b(worse|worsened|worsening|increased|increasing|rising|uptrending|up-trending|up to)\b", re.I)),
    ("stable", re.compile(r"\b(stable|unchanged|no significant change|similar to prior)\b", re.I)),
]

UMLS_CUI = re.compile(r"^C[0-9]{7}$")
SNOMED_ID = re.compile(r"^[0-9]{5,18}$")
EF_OVERRIDE_CONCEPT = {
    "concept_id": "250908004",
    "concept_id_system": "SNOMED_CT",
    "pretty_name": "Left ventricular ejection fraction",
    "type_ids": ["2680757"],
    "reason": "ef_context_override_to_ejection_fraction",
}
COAG_OVERRIDE_CONCEPTS = {
    "pt": {
        "concept_id": "396451008",
        "concept_id_system": "SNOMED_CT",
        "pretty_name": "Prothrombin time",
        "type_ids": ["2680757"],
        "reason": "coag_context_override_to_prothrombin_time",
    },
    "ptt": {
        "concept_id": "42525009",
        "concept_id_system": "SNOMED_CT",
        "pretty_name": "Partial thromboplastin time, activated",
        "type_ids": ["2680757"],
        "reason": "coag_context_override_to_partial_thromboplastin_time",
    },
    "inr": {
        "concept_id": "165581004",
        "concept_id_system": "SNOMED_CT",
        "pretty_name": "International normalized ratio",
        "type_ids": ["2680757"],
        "reason": "coag_context_override_to_international_normalized_ratio",
    },
}
FAMILY_ROLE_TERMS = {
    "aunt",
    "brother",
    "child",
    "children",
    "daughter",
    "daughters",
    "father",
    "grandchild",
    "grandchildren",
    "husband",
    "maternal",
    "mother",
    "parent",
    "paternal",
    "sibling",
    "sister",
    "sisters",
    "son",
    "uncle",
    "wife",
}

VALUE_STATUS_PATTERNS = [
    ("abnormal", re.compile(r"\b(abnormal)\b", re.I)),
    ("normal", re.compile(r"\b(normal|within normal limits|wnl)\b", re.I)),
    ("high", re.compile(r"\b(high|elevated|increased|up to|rose to|rising to)\b", re.I)),
    ("low", re.compile(r"\b(low|decreased|reduced|depressed|down to|dropped to|fell to)\b", re.I)),
    ("positive", re.compile(r"\b(positive)\b", re.I)),
    ("negative", re.compile(r"\b(negative)\b", re.I)),
]

LAB_ALIASES = {
    "alanine aminotransferase": ["alt", "alanine aminotransferase"],
    "albumin": ["albumin"],
    "amylase": ["amylase"],
    "anc": ["anc", "absolute neutrophil count", "neutrophil count"],
    "alkaline phosphatase": ["alkphos", "alk phos", "alkaline phosphatase"],
    "alt": ["alt", "alanine aminotransferase"],
    "alkphos": ["alkphos", "alk phos", "alkaline phosphatase"],
    "aspartate aminotransferase": ["ast", "aspartate aminotransferase"],
    "ast": ["ast", "aspartate aminotransferase"],
    "bicarbonate": ["bicarbonate", "bicarb", "hco3", "co2"],
    "bilirubin": ["bilirubin", "t bili", "tbili", "total bilirubin"],
    "calcium": ["calcium"],
    "ca 19-9": ["ca 19-9", "ca19-9", "cancer antigen 19-9", "carbohydrate antigen 19-9"],
    "cancer antigen 19-9": ["ca 19-9", "ca19-9", "cancer antigen 19-9", "carbohydrate antigen 19-9"],
    "crp": ["crp", "c reactive protein", "c-reactive protein"],
    "d-dimer": ["d-dimer", "ddimer", "d dimer"],
    "ldh": ["ldh", "lactate dehydrogenase", "l-lactate dehydrogenase"],
    "bp": ["bp", "blood pressure"],
    "blood pressure": ["bp", "blood pressure"],
    "bnp": ["bnp", "brain natriuretic peptide", "b-type natriuretic peptide"],
    "brain natriuretic peptide": ["bnp", "brain natriuretic peptide", "b-type natriuretic peptide"],
    "bun": ["bun", "urea"],
    "blood leukocyte number above reference range": ["wbc", "white blood cell count"],
    "creatinine": ["creatinine", "cr"],
    "ef": ["ef", "lvef", "ejection fraction", "left ventricular ejection fraction"],
    "glucose": ["glucose", "fingerstick glucose", "fs glucose"],
    "hct": ["hct", "hematocrit"],
    "hematocrit": ["hct", "hematocrit"],
    "hemoglobin": ["hgb", "hemoglobin"],
    "hgb": ["hgb", "hemoglobin"],
    "heart rate": ["hr", "heart rate"],
    "hr": ["hr", "heart rate"],
    "lactate": ["lactate"],
    "lipase": ["lipase"],
    "magnesium": ["magnesium", "mag"],
    "inr": ["inr"],
    "international normalized ratio": ["inr", "international normalized ratio"],
    "left ventricular ejection fraction": ["lvef", "ef", "ejection fraction", "left ventricular ejection fraction"],
    "lvef": ["lvef", "ef", "ejection fraction", "left ventricular ejection fraction"],
    "ph": ["ph", "blood ph", "abg ph", "arterial ph"],
    "pao2": ["pao2", "arterial oxygen partial pressure", "partial pressure of oxygen"],
    "arterial oxygen partial pressure": ["pao2", "arterial oxygen partial pressure", "partial pressure of oxygen"],
    "pco2": ["pco2", "co2"],
    "phosphorus": ["phosphorus", "phos"],
    "platelet": ["plt", "platelet", "platelets"],
    "platelets": ["plt", "platelet", "platelets"],
    "plt": ["plt", "platelet", "platelets"],
    "potassium": ["potassium", "k"],
    "procalcitonin": ["procalcitonin", "pct"],
    "partial thromboplastin time": ["ptt", "partial thromboplastin time", "activated partial thromboplastin time"],
    "prothrombin time": ["pt", "prothrombin time"],
    "pt": ["pt", "prothrombin time"],
    "ptt": ["ptt", "partial thromboplastin time", "activated partial thromboplastin time"],
    "temperature": ["temperature", "temp"],
    "temp": ["temperature", "temp"],
    "respiratory rate": ["rr", "respiratory rate"],
    "rr": ["rr", "respiratory rate"],
    "sao2": ["sao2", "spo2", "o2 sat"],
    "na": ["na", "sodium"],
    "o2 sat": ["o2 sat", "oxygen saturation", "spo2", "sao2"],
    "o2 saturation": ["o2 saturation", "o2 sat", "oxygen saturation", "spo2", "sao2"],
    "oxygen saturation": ["o2 sat", "oxygen saturation", "spo2", "sao2"],
    "peripheral oxygen saturation": ["o2 saturation", "o2 sat", "oxygen saturation", "spo2", "sao2"],
    "sodium": ["sodium", "na"],
    "spo2": ["spo2", "sao2", "o2 sat"],
    "troponin": ["troponin", "troponin t", "troponin i"],
    "wbc": ["wbc", "white blood cell count"],
    "white blood cell count": ["wbc", "white blood cell count"],
}

UNIT_PATTERN = (
    r"%|percent|mg/dl|mg/l(?:\s*feu)?|mmol/l|meq/l|k/u?l|cells/u?l|x10\^?3|x10\^?9/l|10\^?9/l|g/dl|gm/dl|g/l|"
    r"beats?/min(?:ute)?|breaths?/min(?:ute)?|ratio|p?h|degrees?\s*c|deg(?:ree)?s?\s*c|°\s*c|"
    r"u/ml|units/ml|"
    r"seconds?|secs|sec|bpm|mmhg|mm hg|cmh2o|iu|u/l|ng/ml|mcg/ml|pg/ml|mg/hr|insp/min|/min|per[_ ]?min"
)
NUMERIC_TOKEN_PATTERN = r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
RANGE_SEPARATOR_PATTERN = r"(?:-|\u2013|\u2014|to)"
NUMERIC_VALUE_PATTERN = rf"{NUMERIC_TOKEN_PATTERN}(?:'?s)?(?:\s*{RANGE_SEPARATOR_PATTERN}\s*{NUMERIC_TOKEN_PATTERN}(?:'?s)?)?(?:/{NUMERIC_TOKEN_PATTERN}(?:\({NUMERIC_TOKEN_PATTERN}\))?)?"
SIMPLE_NUMERIC_VALUE_PATTERN = rf"{NUMERIC_TOKEN_PATTERN}(?:\s*{RANGE_SEPARATOR_PATTERN}\s*{NUMERIC_TOKEN_PATTERN})?"
LAB_INLINE_STATUS_PATTERN = r"(?:\s*(?:\(\s*[HLAN]\s*\)|\b(?:high|low|normal|abnormal|elevated|increased|decreased|reduced|depressed|positive|negative|H|L)\b))?"
REFERENCE_RANGE_CUE_PATTERN = r"(?:ref(?:erence)?\s*range|normal\s*range)"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--zip", help="Path to a locally staged MIMIC zip")
    parser.add_argument("--notes-jsonl-gz", help="Optional pre-sharded MIMIC notes JSONL(.gz) produced by shard_mimic_notes.py")
    parser.add_argument("--modelpack", required=True, help="Licensed MedCAT modelpack")
    parser.add_argument("--filter-policy", required=True, help="Schema-aware filter policy JSON")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument(
        "--selection-mode",
        choices=["sample", "range"],
        default="sample",
        help="sample keeps the existing stratified reservoir pilot behavior; range processes a contiguous nonempty-note index range.",
    )
    parser.add_argument("--start-note-index", type=int, default=1, help="1-based nonempty note index for --selection-mode range")
    parser.add_argument("--end-note-index", type=int, default=0, help="Inclusive nonempty note index for --selection-mode range; 0 means uncapped")
    parser.add_argument("--max-total-notes", type=int, default=240)
    parser.add_argument("--max-notes-per-category", type=int, default=20)
    parser.add_argument("--max-note-chars", type=int, default=7000)
    parser.add_argument("--min-acc", type=float, default=0.0)
    parser.add_argument(
        "--output-mode",
        choices=["all_non_rejected", "strict_pre_umls"],
        default="all_non_rejected",
        help="strict_pre_umls writes only records that pass the strict gate except for real UMLS mapping.",
    )
    parser.add_argument("--pre-umls-min-keep-score", type=float, default=0.99)
    parser.add_argument("--seed", type=int, default=20260511)
    parser.add_argument("--sentence-chars", type=int, default=420)
    parser.add_argument("--context-chars", type=int, default=650, help="Max chars for bounded context fields written to JSONL")
    parser.add_argument("--max-rejected-records", type=int, default=5000)
    parser.add_argument("--progress-every", type=int, default=25)
    parser.add_argument("--compress-output", action="store_true", help="Write large candidate JSONL as gzip-compressed JSONL.gz")
    parser.add_argument("--gzip-compresslevel", type=int, default=3, help="gzip level for --compress-output")
    parser.add_argument("--medcat-n-process", type=int, default=1, help="MedCAT internal n_process for get_entities_multi_texts; 1 preserves old per-note behavior")
    parser.add_argument("--medcat-batch-size", type=int, default=-1, help="MedCAT multi-text batch_size; only used when --medcat-n-process > 1")
    parser.add_argument("--medcat-batch-size-chars", type=int, default=1000000, help="MedCAT multi-text batch_size_chars; only used when --medcat-n-process > 1")
    parser.add_argument("--medcat-prefer-gpu", action="store_true", help="Ask spaCy/thinc to prefer GPU before loading MedCAT; benchmark-only unless validated")
    parser.add_argument("--medcat-require-gpu", action="store_true", help="Require GPU before loading MedCAT; benchmark-only")
    parser.add_argument("--note-section-keep-regex", default="", help="Benchmark-only: before MedCAT, keep only note section blocks whose section header matches this regex")
    parser.add_argument("--note-section-drop-regex", default="", help="Benchmark-only: before MedCAT, drop note section blocks whose section header matches this regex")
    parser.add_argument("--note-section-include-unknown", action="store_true", help="With --note-section-keep-regex, also keep text before the first detected section")
    parser.add_argument("--allow-no-slurm", action="store_true", help="Developer escape hatch; do not use for MIMIC/MedCAT processing.")
    return parser.parse_args()


def clean(text: str) -> str:
    return SPACE.sub(" ", text or "").strip()


def norm_key(text: str) -> str:
    return clean(text).strip(" :;,.()[]{}").lower()


def load_policy(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def prepare_policy_runtime(policy: dict[str, Any]) -> dict[str, Any]:
    """Attach precomputed immutable lookup sets for hot per-entity checks."""

    section_policy = policy.get("section_policy", {}) or {}
    policy["_runtime"] = {
        "reject_exact_spans": set(policy.get("reject_exact_spans", [])),
        "context_only_spans": set(policy.get("context_only_spans", [])),
        "unit_or_route_spans": set(policy.get("unit_or_route_spans", [])),
        "lab_or_vital_spans": set(policy.get("lab_or_vital_spans", [])),
        "high_risk_abbreviations": set(policy.get("high_risk_abbreviations", [])),
        "context_or_noise_type_ids": set(policy.get("context_or_noise_type_ids", {})),
        "clinical_priority_type_ids": set(policy.get("clinical_priority_type_ids", {})),
        "admin_sections": {norm_key(s) for s in section_policy.get("admin_sections", [])},
    }
    return policy


def open_output_text(path: Path, compresslevel: int = 3):
    if str(path).endswith(".gz"):
        return gzip.open(path, "wt", encoding="utf-8", compresslevel=compresslevel)
    return path.open("w", encoding="utf-8")


def score_bin(score: float) -> str:
    if score < 0.25:
        return "00_0.00_0.25"
    if score < 0.50:
        return "01_0.25_0.50"
    if score < 0.70:
        return "02_0.50_0.70"
    if score < 0.80:
        return "03_0.70_0.80"
    if score < 0.90:
        return "04_0.80_0.90"
    if score < 0.99:
        return "05_0.90_0.99"
    return "06_0.99_1.00"


def default_action_for_score(score: float) -> str:
    if score >= 0.90:
        return "keep_candidate"
    if score >= 0.70:
        return "review_candidate"
    return "reject_candidate"


def sentence_for_span(text: str, start: int, end: int, max_chars: int) -> str:
    boundary = re.compile(r"(?<!\d)[.!?](?!\d)|\n|;")
    left = -1
    for match in boundary.finditer(text, 0, start):
        left = match.start()
    right = len(text)
    for match in boundary.finditer(text, end):
        right = match.start()
        break
    if right == len(text):
        right = min(len(text), end + 220)
    return clean(text[left + 1 : right + 1])[:max_chars]


def context_window(text: str, start: int, end: int, before: int = 260, after: int = 620) -> str:
    return clean(text[max(0, start - before) : min(len(text), end + after)])


def clip(text: str, max_chars: int) -> str:
    text = clean(text)
    if max_chars <= 0:
        return text
    return text[:max_chars]


def section_for_span(text: str, start: int) -> str:
    prefix = text[:start]
    for line in reversed(prefix.splitlines()[-90:]):
        candidate = clean(line).strip(":")
        lowered = candidate.lower()
        if not candidate or len(candidate) > 100:
            continue
        if SECTION_HEADER.match(line) and (line.strip().endswith(":") or lowered in KNOWN_SECTION_HEADERS):
            return candidate
    return "unknown"


def is_section_header_line(line: str) -> bool:
    candidate = clean(line).strip(":")
    lowered = candidate.lower()
    if not candidate or len(candidate) > 100:
        return False
    return bool(SECTION_HEADER.match(line) and (line.strip().endswith(":") or lowered in KNOWN_SECTION_HEADERS))


def filter_note_sections(
    text: str,
    keep_pattern: re.Pattern[str] | None,
    drop_pattern: re.Pattern[str] | None,
    include_unknown: bool,
) -> str:
    if keep_pattern is None and drop_pattern is None:
        return text

    blocks: list[tuple[str, list[str]]] = []
    current_header = "unknown"
    current_lines: list[str] = []
    for line in text.splitlines():
        if is_section_header_line(line):
            if current_lines:
                blocks.append((current_header, current_lines))
            current_header = clean(line).strip(":") or "unknown"
            current_lines = [line]
        else:
            current_lines.append(line)
    if current_lines:
        blocks.append((current_header, current_lines))
    if not blocks:
        return text

    kept_lines: list[str] = []
    for header, lines in blocks:
        header_key = header.lower()
        keep = True
        if keep_pattern is not None:
            keep = bool(keep_pattern.search(header_key)) or (include_unknown and header_key == "unknown")
        if drop_pattern is not None and drop_pattern.search(header_key):
            keep = False
        if keep:
            kept_lines.extend(lines)
    if not kept_lines:
        return ""
    return "\n".join(kept_lines)


def apply_note_text_filters(
    selected_notes: list[dict[str, str]],
    keep_regex: str,
    drop_regex: str,
    include_unknown: bool,
) -> dict[str, Any]:
    keep_pattern = re.compile(keep_regex, re.I) if keep_regex else None
    drop_pattern = re.compile(drop_regex, re.I) if drop_regex else None
    stats = {
        "enabled": bool(keep_pattern or drop_pattern),
        "keep_regex": keep_regex,
        "drop_regex": drop_regex,
        "include_unknown": include_unknown,
        "notes": len(selected_notes),
        "notes_changed": 0,
        "notes_emptied": 0,
        "chars_before": 0,
        "chars_after": 0,
    }
    if not stats["enabled"]:
        stats["chars_before"] = sum(len(row.get("TEXT", "") or "") for row in selected_notes)
        stats["chars_after"] = stats["chars_before"]
        return stats
    for row in selected_notes:
        original = row.get("TEXT", "") or ""
        filtered = filter_note_sections(original, keep_pattern, drop_pattern, include_unknown)
        stats["chars_before"] += len(original)
        stats["chars_after"] += len(filtered)
        if filtered != original:
            stats["notes_changed"] += 1
        if original.strip() and not filtered.strip():
            stats["notes_emptied"] += 1
        row["TEXT"] = filtered
    return stats


def iter_notes(zip_path: Path):
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
        candidates = [n for n in names if n.upper().endswith("NOTEEVENTS.CSV.GZ")]
        candidates += [n for n in names if n.upper().endswith("NOTEEVENTS.CSV")]
        if not candidates:
            raise FileNotFoundError("NOTEEVENTS.csv(.gz) not found")
        name = candidates[0]
        with zf.open(name) as raw:
            if name.upper().endswith(".GZ"):
                fh = gzip.open(raw, mode="rt", encoding="utf-8", errors="replace", newline="")
            else:
                fh = (line.decode("utf-8", errors="replace") for line in raw)
            reader = csv.DictReader(fh)
            for row in reader:
                yield row


def iter_sharded_notes(path: Path):
    opener = gzip.open if str(path).endswith(".gz") else open
    with opener(path, mode="rt", encoding="utf-8", errors="replace") as fh:
        for line_no, line in enumerate(fh, 1):
            if not line.strip():
                continue
            record = json.loads(line)
            yield {
                "ROW_ID": str(record.get("row_id") or record.get("ROW_ID") or ""),
                "SUBJECT_ID": str(record.get("subject_id") or record.get("SUBJECT_ID") or ""),
                "HADM_ID": str(record.get("hadm_id") or record.get("HADM_ID") or ""),
                "CATEGORY": str(record.get("category") or record.get("CATEGORY") or "unknown"),
                "CHARTDATE": str(record.get("chartdate") or record.get("CHARTDATE") or ""),
                "TEXT": str(record.get("text") or record.get("TEXT") or ""),
                "_note_index": record.get("note_index"),
                "_csv_row_number": record.get("csv_row_number"),
                "_source_line_no": line_no,
                "_source_shard": str(path),
            }


def note_stub(row: dict[str, str]) -> dict[str, str]:
    return {
        "ROW_ID": row.get("ROW_ID", ""),
        "SUBJECT_ID": row.get("SUBJECT_ID", ""),
        "HADM_ID": row.get("HADM_ID", ""),
        "CATEGORY": row.get("CATEGORY", "") or "unknown",
        "CHARTDATE": row.get("CHARTDATE", ""),
        "TEXT": row.get("TEXT", "") or "",
    }


def sample_notes_from_iter(note_iter, per_category: int, max_total: int, seed: int):
    rng = random.Random(seed)
    reservoirs: dict[str, list[dict[str, str]]] = defaultdict(list)
    category_population = Counter()
    rows_read = 0
    nonempty_rows = 0

    for row in note_iter:
        rows_read += 1
        text = row.get("TEXT", "") or ""
        if not text.strip():
            continue
        nonempty_rows += 1
        category = row.get("CATEGORY", "") or "unknown"
        category_population[category] += 1
        bucket = reservoirs[category]
        seen = category_population[category]
        if len(bucket) < per_category:
            bucket.append(note_stub(row))
        else:
            replacement_index = rng.randrange(seen)
            if replacement_index < per_category:
                bucket[replacement_index] = note_stub(row)

    sampled = []
    for category in sorted(reservoirs):
        sampled.extend(reservoirs[category])
    if max_total > 0 and len(sampled) > max_total:
        sampled = rng.sample(sampled, max_total)
    sampled.sort(key=lambda r: (r.get("CATEGORY", ""), int(r.get("ROW_ID", "0") or 0)))
    return sampled, category_population, rows_read, nonempty_rows


def sample_notes(zip_path: Path, per_category: int, max_total: int, seed: int):
    return sample_notes_from_iter(iter_notes(zip_path), per_category, max_total, seed)


def sample_notes_from_shard(notes_path: Path, per_category: int, max_total: int, seed: int):
    return sample_notes_from_iter(iter_sharded_notes(notes_path), per_category, max_total, seed)


def range_notes_from_iter(note_iter, start_note_index: int, end_note_index: int, max_total: int):
    """Select a contiguous 1-based range over nonempty NOTEEVENTS rows.

    This mode is for full-corpus SLURM array preparation. Each shard can process
    a bounded slice without holding the full note population in memory.
    """

    selected: list[dict[str, str]] = []
    category_seen = Counter()
    rows_read = 0
    nonempty_rows = 0

    if start_note_index < 1:
        raise ValueError("--start-note-index must be >= 1")
    if end_note_index and end_note_index < start_note_index:
        raise ValueError("--end-note-index must be 0 or >= --start-note-index")
    if end_note_index == 0 and max_total <= 0:
        raise ValueError("--selection-mode range requires --end-note-index or --max-total-notes to avoid unbounded memory use")

    for row in note_iter:
        rows_read += 1
        text = row.get("TEXT", "") or ""
        if not text.strip():
            continue
        nonempty_rows += 1
        category = row.get("CATEGORY", "") or "unknown"
        category_seen[category] += 1
        if nonempty_rows < start_note_index:
            continue
        if end_note_index and nonempty_rows > end_note_index:
            break
        selected.append(note_stub(row))
        if max_total > 0 and len(selected) >= max_total:
            break

    selected.sort(key=lambda r: int(r.get("ROW_ID", "0") or 0))
    return selected, category_seen, rows_read, nonempty_rows


def range_notes(zip_path: Path, start_note_index: int, end_note_index: int, max_total: int):
    return range_notes_from_iter(iter_notes(zip_path), start_note_index, end_note_index, max_total)


def range_notes_from_shard(notes_path: Path, start_note_index: int, end_note_index: int, max_total: int):
    return range_notes_from_iter(iter_sharded_notes(notes_path), start_note_index, end_note_index, max_total)


def entity_field(ent: dict[str, Any], *names: str, default=None):
    for name in names:
        if name in ent and ent[name] not in (None, ""):
            return ent[name]
    return default


def concept_id_system(concept_id: str) -> str:
    if UMLS_CUI.match(concept_id):
        return "UMLS_CUI"
    if SNOMED_ID.match(concept_id):
        return "SNOMED_CT"
    return "unknown"


def infer_concept_category(span: str, pretty_name: str, type_ids: list[str], flags: set[str]) -> str:
    lower = norm_key(f"{span} {pretty_name}")
    type_set = set(type_ids)
    if "lab_or_vital_span" in flags:
        return "lab_result_or_value"
    if "family_history_context" in flags and any(word in lower for word in ("family history", "mother", "father", "sister", "brother")):
        return "family_history"
    if "social_context" in flags and any(word in lower for word in ("smoker", "smoking", "tobacco", "alcohol", "homeless", "occupation", "lives with")):
        return "social_history"
    if "9090192" in type_set:
        return "disease_or_diagnosis"
    if "91187746" in type_set:
        return "medication_or_substance"
    if "37552161" in type_set:
        return "anatomy"
    if "28321150" in type_set or "47503797" in type_set:
        return "procedure"
    if "67667581" in type_set:
        return "finding"
    if "32816260" in type_set:
        return "device"
    if "2680757" in type_set or "7882689" in type_set or "43039974" in type_set:
        return "clinical_attribute"
    return "other"


def scoped_span_pattern(span: str) -> str:
    span_key = clean(span)
    if not span_key:
        return ""
    return rf"\b{re.escape(span_key)}\b"


def has_scoped_conditional(text: str, span: str) -> bool:
    target = scoped_span_pattern(span)
    if not target:
        return bool(CONDITIONAL_CUE.search(text))
    for match in re.finditer(rf"{target}[^.;:]{{0,100}}\b(if|unless|when|whenever|prn|as needed|if needed|if necessary|hold if|call if|return if|use as needed|take as needed)\b", text, re.I):
        middle = text[match.start() : match.end()]
        if not re.search(r"\b(consider|monitor for|watch for|evaluate for|plan to|think about|suggest)\b", middle, re.I):
            return True
    return bool(
        re.search(rf"\b(if|unless|when|whenever|provided that|in case of|return if|call if)\b[^.;:]{{0,140}}{target}", text, re.I)
        or re.search(rf"\b(prn|as needed|if needed|if necessary|use as needed|take as needed)\b[^.;:]{{0,100}}{target}", text, re.I)
    )


def has_scoped_negation(text: str, span: str) -> bool:
    target = scoped_span_pattern(span)
    if not target:
        return bool(NEGATION_CUE.search(text))
    return bool(
        re.search(
            rf"\b(no|none|neither|nor|without|w/o|denies|denied|negative for|"
            rf"no evidence of|no signs? of|absen(?:t|ce) of|lack of|free of)\b[^.;:]{{0,140}}{target}",
            text,
            re.I,
        )
        or re.search(rf"\b(did not|does not|failed to)\s+(?:show|reveal|demonstrate)[^.;:]{{0,140}}{target}", text, re.I)
        or re.search(rf"{target}[^.;:]{{0,80}}\b(absent|negative|not seen|not present|ruled out|excluded)\b", text, re.I)
        or re.search(rf"\bnormal\b[^.;:]{{0,60}}{target}", text, re.I)
        or re.search(rf"{target}[^.;:]{{0,60}}\bnormal\b", text, re.I)
    )


def has_scoped_possible(text: str, span: str) -> bool:
    target = scoped_span_pattern(span)
    if not target:
        return bool(POSSIBLE_CUE.search(text))
    for match in re.finditer(
        rf"\b(possible|possibly|probable|probably|suspected|suspicious for|concern(?:ing)? for|"
        rf"question of|rule out|r/o|cannot rule out|cannot exclude|cannot be excluded|"
        rf"may have|may represent|may reflect|might represent|could represent|could reflect|"
        rf"suggestive of|compatible with|consistent with)\b[^.;:]{{0,140}}{target}",
        text,
        re.I,
    ):
        middle = text[match.start() : match.end()]
        if not re.search(r"\b(after|with|on|from|due to|because|in|during|through|of)\b", middle, re.I):
            return True
    return bool(re.search(rf"{target}[^.;:]{{0,120}}\b(cannot be excluded|cannot exclude|cannot rule out|rule out|r/o)\b", text, re.I))


def has_scoped_hypothetical(text: str, span: str) -> bool:
    target = scoped_span_pattern(span)
    if not target:
        return bool(HYPOTHETICAL_CUE.search(text))
    return bool(
        re.search(rf"\b(should|would|consider|monitor for|watch for|plan to|evaluate for)\b[^.;:]{{0,140}}{target}", text, re.I)
        or re.search(rf"{target}[^.;:]{{0,100}}\b(should|would|consider|monitor|watch|plan)\b", text, re.I)
    )


def has_scoped_historical(text: str, span: str) -> bool:
    target = scoped_span_pattern(span)
    if not target:
        return bool(HISTORICAL_CUE.search(text))
    return bool(
        re.search(
            rf"\b(history of|h/o|hx of|past medical history|past surgical history|pmh|pmhx|"
            rf"history includes|previous|previously|prior|remote|former|status post|s/p|underwent|"
            rf"was diagnosed|were diagnosed|had been diagnosed|known|longstanding|home meds include|home medications include|home med includes)\b[^.;:]{{0,160}}{target}",
            text,
            re.I,
        )
    )


LOW_VALUE_ASSERTION_SPANS = {
    "",
    "a",
    "acls",
    "alert",
    "awake",
    "child",
    "diet",
    "eomi",
    "finding",
    "findings",
    "heart",
    "lung",
    "lungs",
    "moves",
    "nose",
    "ns",
    "perrl",
    "related",
    "report",
    "reports",
    "skin",
    "study",
    "studies",
    "term",
}
LOW_VALUE_ASSERTION_PATTERNS = [
    re.compile(r"^(?:right|left|bilateral|upper|lower)$", re.I),
    re.compile(r"^(?:finger to )?nose$", re.I),
    re.compile(r"^(?:awake|alert|oriented)(?: x?\d)?$", re.I),
    re.compile(r"^(?:normal|abnormal|positive|negative)$", re.I),
    re.compile(r"^(?:moves?|moving|movement)$", re.I),
]
PAST_HISTORY_SECTION = re.compile(r"\b(past medical history|past surgical history|past history|pmh|pmhx|psh|medical history)\b", re.I)
FAMILY_HISTORY_SECTION = re.compile(r"\b(family history|fhx|family hx)\b", re.I)
SOCIAL_HISTORY_SECTION = re.compile(r"\b(social history|social hx)\b", re.I)
CURRENT_EVENT_SECTION = re.compile(
    r"\b(history of present illness|hpi|chief complaint|brief hospital course|hospital course|"
    r"pertinent results?|imaging|physical exam|discharge diagnoses?|diagnosis)\b",
    re.I,
)


def assertion_evaluable_preview(span: str, concept_category: str) -> tuple[bool, str]:
    if concept_category not in {"disease_or_diagnosis", "finding"}:
        return False, "non_core_category"
    span_key = norm_key(span).strip(" :;,.()[]{}")
    if span_key in LOW_VALUE_ASSERTION_SPANS:
        return False, "low_value_span_stoplist"
    if len(span_key) <= 2 and span_key not in {"mi", "pe"}:
        return False, "too_short_non_specific_span"
    if any(pattern.search(span_key) for pattern in LOW_VALUE_ASSERTION_PATTERNS):
        return False, "low_value_span_pattern"
    return True, "assertion_evaluable"


def span_positions(text: str, span: str) -> list[tuple[int, int]]:
    span_key = norm_key(span).strip(" :;,.()[]{}")
    if not span_key:
        return []
    pattern = re.compile(rf"(?<!\w){re.escape(span_key)}(?!\w)", re.I)
    out = [(match.start(), match.end()) for match in pattern.finditer(text)]
    if out:
        return out
    idx = text.find(span_key)
    return [(idx, idx + len(span_key))] if idx >= 0 else []


def same_clause_before(fragment: str) -> str:
    idx = max(fragment.rfind("."), fragment.rfind(";"), fragment.rfind("\n"))
    return fragment[idx + 1 :] if idx >= 0 else fragment


def same_clause_after(fragment: str) -> str:
    stops = [idx for idx in (fragment.find("."), fragment.find(";"), fragment.find("\n")) if idx >= 0]
    return fragment[: min(stops)] if stops else fragment


def window_before(text: str, span: str, width: int, same_clause: bool = True) -> list[str]:
    windows = [text[max(0, start - width) : start] for start, _ in span_positions(text, span)]
    return [same_clause_before(window) for window in windows] if same_clause else windows


def window_after(text: str, span: str, width: int, same_clause: bool = True) -> list[str]:
    windows = [text[end : end + width] for _, end in span_positions(text, span)]
    return [same_clause_after(window) for window in windows] if same_clause else windows


def any_window_has(windows: list[str], patterns: list[str]) -> bool:
    return any(re.search(pattern, window, re.I) for window in windows for pattern in patterns)


def overlay_text(sentence: str, evidence_context: str = "") -> str:
    text = clean(evidence_context) if evidence_context else clean(sentence)
    return norm_key(text)


def has_overlay_conditional(text: str, span: str) -> bool:
    before = window_before(text, span, 180)
    after = window_after(text, span, 120)
    return any_window_has(
        before,
        [
            r"\bif\b",
            r"\bwhen\b",
            r"\bunless\b",
            r"\bin case of\b",
            r"\bcall (?:md|doctor|pcp|provider|surgeon).* if\b",
            r"\breturn (?:to|for).* if\b",
            r"\b(?:prn|as needed|if needed|if necessary)\b",
        ],
    ) or any_window_has(after, [r"\b(?:prn|as needed|if needed|if necessary)\b"])


def has_overlay_negation(text: str, span: str) -> bool:
    before = window_before(text, span, 170)
    after = window_after(text, span, 100)
    if any_window_has(
        before,
        [
            r"\bno (?:change|changes|improvement|increase|decrease) in\b",
            r"\bnot (?:improve|respond|controlled|tolerated) with\b",
            r"\bwithout (?:change|improvement|increase|decrease) in\b",
        ],
    ):
        return False
    return any_window_has(
        before,
        [
            r"\bno\b",
            r"\bdenies?\b",
            r"\bdenied\b",
            r"\bwithout\b",
            r"\bw/o\b",
            r"\bnegative for\b",
            r"\bfree of\b",
            r"\bno evidence of\b",
            r"\bno sign(?:s)? of\b",
            r"\babsence of\b",
            r"\black of\b",
            r"\bruled out\b",
        ],
    ) or any_window_has(
        after,
        [
            r"\b(?:was|were)? ?ruled out\b",
            r"\b(?:negative|absent|normal|unremarkable)\b",
            r"\bnot (?:seen|found|detected|present)\b",
        ],
    )


def has_overlay_possible(text: str, span: str) -> bool:
    before = window_before(text, span, 190)
    after = window_after(text, span, 130)
    return any_window_has(
        before,
        [
            r"\b(?:possible|possibly|probable|probably|likely)\b",
            r"\b(?:suspected|presumed|suspicious for|concern for|concerning for)\b",
            r"\b(?:question of|questionable|\?|\br/o\b|rule out)\b",
            r"\b(?:may|might|could) (?:represent|be|reflect|indicate|suggest|have led to|be due to|be related to)\b",
            r"\b(?:suggestive of|consistent with|compatible with)\b",
            r"\bdifferential (?:also )?(?:includes|included)\b",
            r"\b(?:felt|thought|considered) (?:to be )?(?:likely|possibly|probably)? ?(?:secondary to|due to|related to)\b",
        ],
    ) or any_window_has(
        after,
        [
            r"^\W*(?:is|was|were|be)?\W*(?:possible|probable|suspected|likely)\b",
            r"^\W*(?:vs\.?|versus|and/or)\b",
            r"\b(?:cannot be excluded|cannot exclude|cannot rule out)\b",
        ],
    )


def has_span_historical_cue(span: str) -> bool:
    span_text = clean(span)
    return bool(
        re.search(
            r"^(?:history of|history includes|h/o|hx of|former|prior|remote|past|previous|status post|s/p)\b",
            span_text,
            re.I,
        )
        or re.search(r"\bformer smoker\b", span_text, re.I)
    )


def has_preceding_conditional_cue(text: str, span: str) -> bool:
    positions = span_positions(text, span)
    if not positions:
        return bool(CONDITIONAL_CUE.search(text))
    start, _ = positions[0]
    prefix = same_clause_before(text[max(0, start - 180) : start])
    return bool(CONDITIONAL_CUE.search(prefix))


def has_overlay_hypothetical(text: str, span: str) -> bool:
    before = window_before(text, span, 180)
    after = window_after(text, span, 130)
    return any_window_has(
        before,
        [
            r"\b(?:monitor|monitoring|watch|observe) for\b",
            r"\b(?:evaluate|evaluation|workup|screening|screen) for\b",
            r"\b(?:prophylaxis|prevent|prevention) (?:of|for)\b",
            r"\b(?:risk of|risk for)\b",
            r"\b(?:may|can|could) cause\b",
            r"\bto avoid\b",
            r"\b(?:plan|plans|planned|planning) (?:for|to)\b",
            r"\b(?:scheduled|schedule) (?:for|to)\b",
        ],
    ) or any_window_has(after, [r"\b(?:would require|will require|may require|could lead to|may cause)\b"])


def has_close_history_cue(text: str, span: str) -> bool:
    before = window_before(text, span, 80)
    after = window_after(text, span, 60)
    return any_window_has(
        before,
        [
            r"\b(?:history of|h/o|hx of|hx)\b",
            r"\b(?:past|prior|previous|remote|old)\b",
            r"\bstatus post\b",
            r"\bs/p\b",
            r"\bknown\b",
            r"\blongstanding\b",
        ],
    ) or any_window_has(after, [r"\b(?:in the past|previously|prior to admission)\b"])


def has_overlay_historical(text: str, section: str, span: str) -> bool:
    if (
        PAST_HISTORY_SECTION.search(section or "")
        or FAMILY_HISTORY_SECTION.search(section or "")
    ):
        return True
    if has_span_historical_cue(span):
        return True
    before = window_before(text, span, 190)
    after = window_after(text, span, 100)
    return any_window_has(
        before,
        [
            r"\b(?:history of|history includes|h/o|hx of|hx)\b",
            r"\b(?:past|prior|previous|remote|old)\b",
            r"\bstatus post\b",
            r"\bs/p\b",
            r"\bhome (?:meds|medications?) include\b",
            r"\bpreviously (?:diagnosed|treated)\b",
            r"\b(?:family history|fhx|family hx)\b",
        ],
    ) or any_window_has(after, [r"\b(?:in the past|previously|prior to admission)\b"])


def has_overlay_current_event(text: str, section: str, span: str) -> bool:
    if has_close_history_cue(text, span):
        return False
    if not CURRENT_EVENT_SECTION.search(section or ""):
        return False
    before = window_before(text, span, 170)
    after = window_after(text, span, 80)
    return any_window_has(
        before,
        [
            r"\bcurrently\b",
            r"\btoday\b",
            r"\bon arrival\b",
            r"\bin the (?:ed|er|icu|micu|ccu)\b",
            r"\bat admission\b",
            r"\bsince\b",
            r"\bfor (?:the )?(?:past|last)? ?\d+\b",
            r"\b(?:presents?|presented|presenting) (?:with|for|from)\b",
            r"\bp/w\b",
            r"\bc/o\b",
            r"\bcomplain(?:s|ed|ing)? of\b",
            r"\breports?\b",
            r"\bendorsi(?:ng|es|ed)\b",
            r"\bdevelop(?:ed|s|ing)\b",
            r"\bwas noted to (?:be|have)\b",
            r"\bfound to (?:be|have)\b",
            r"\b(?:showed|shows|showing|did show)\b",
            r"\b(?:revealed|demonstrated|confirmed)\b",
            r"\bread to have\b",
            r"\btreated (?:with|for)\b",
            r"\bstarted on\b",
            r"\breceived\b",
            r"\bgiven\b",
            r"\b(?:has|had|is|was|were) (?:a |an |some |mild |moderate |severe |acute |chronic |new |recurrent )?$",
        ],
    ) or any_window_has(after, [r"^\W*(?:x|for) \d+", r"\b(?:was|were) treated\b"])


def infer_assertion_preview(
    sentence: str,
    section: str,
    action: str,
    span: str = "",
    evidence_context: str = "",
    concept_category: str = "",
) -> str:
    if action == "value_parse":
        return "not_applicable"
    text = clean(sentence)
    if has_overlay_negation(text, span) or has_scoped_negation(sentence, span):
        return "absent"
    if has_span_historical_cue(span):
        return "historical"
    if has_overlay_historical(text, section, span):
        return "historical"
    if has_overlay_conditional(text, span) or has_scoped_conditional(sentence, span):
        return "conditional"
    if has_overlay_possible(text, span) or has_scoped_possible(sentence, span):
        return "possible"
    if has_overlay_hypothetical(text, span) or has_scoped_hypothetical(sentence, span):
        return "hypothetical"
    if has_overlay_current_event(text, section, span):
        return "present"
    return "present"


def refine_assertion_preview(
    sentence: str,
    section: str,
    action: str,
    span: str = "",
    evidence_context: str = "",
    concept_category: str = "",
    initial_assertion: str | None = None,
) -> tuple[str, bool, list[str]]:
    """Apply a second local-scope pass to catch conditional leakage.

    The first pass is intentionally permissive so we do not miss genuine
    conditional targets. This second pass keeps conditional only when the cue
    is still anchored to the target span after the local scope check.
    """

    if action == "value_parse":
        return "not_applicable", False, ["value_parse"]

    raw_assertion = initial_assertion
    if raw_assertion is None:
        raw_assertion = infer_assertion_preview(
            sentence,
            section,
            action,
            span,
            evidence_context,
            concept_category,
        )

    if raw_assertion != "conditional":
        return raw_assertion, False, []

    # Keep the second pass sentence-local so later-section cues do not leak
    # backward into an earlier target span.
    text = clean(sentence)
    if has_scoped_possible(text, span) and not has_preceding_conditional_cue(text, span):
        return "possible", True, ["conditional_post_judge_to_possible"]
    if has_scoped_hypothetical(text, span) and not has_preceding_conditional_cue(text, span):
        return "hypothetical", True, ["conditional_post_judge_to_hypothetical"]
    if has_scoped_conditional(text, span):
        return "conditional", False, ["conditional_scope_confirmed"]

    if has_scoped_negation(text, span):
        return "absent", True, ["conditional_post_judge_to_absent"]
    if has_scoped_possible(text, span):
        return "possible", True, ["conditional_post_judge_to_possible"]
    if has_overlay_current_event(text, section, span):
        return "present", True, ["conditional_post_judge_to_present"]
    if has_overlay_historical(text, section, span):
        return "historical", True, ["conditional_post_judge_to_historical"]
    return "present", True, ["conditional_post_judge_to_present_default"]


def infer_temporality_preview(sentence: str, section: str, span: str = "") -> str:
    text = f"{section} {sentence}"
    if has_scoped_conditional(text, span) or has_scoped_hypothetical(text, span):
        return "future_or_hypothetical"
    if has_scoped_historical(text, span):
        return "historical"
    return "current"


def infer_experiencer_preview(sentence: str, section: str, flags: set[str]) -> str:
    text = f"{section} {sentence}"
    if "social_target_context" in flags:
        return "patient"
    if "family_history_context" in flags or is_family_history_context(sentence, section):
        return "family"
    return "patient"


def infer_context_cue(context: str, patterns: list[tuple[str, re.Pattern[str]]]) -> dict[str, Any]:
    """Return a conservative local cue without adding a second model pass."""

    matches = []
    for label, pattern in patterns:
        match = pattern.search(context)
        if match:
            matches.append({"label": label, "text": clean(match.group(0).lower())})
    if not matches:
        return {"label": None, "text": None}
    return matches[0]


def infer_value_status(context: str) -> str:
    for status, pattern in VALUE_STATUS_PATTERNS:
        if pattern.search(context):
            return status
    return "unknown"


def infer_local_value_status(context: str, start: int | None = None, end: int | None = None) -> str:
    if start is None or end is None:
        return infer_value_status(context)
    local = context[max(0, start - 55) : min(len(context), end + 55)]
    return infer_value_status(local)


@lru_cache(maxsize=1)
def _known_lab_aliases() -> tuple[str, ...]:
    aliases: set[str] = set(LAB_ALIASES)
    for values in LAB_ALIASES.values():
        aliases.update(values)
    return tuple(sorted((alias for alias in aliases if alias), key=len, reverse=True))


def all_known_lab_aliases() -> list[str]:
    return list(_known_lab_aliases())


@lru_cache(maxsize=8192)
def leading_lab_alias(span: str) -> str | None:
    span_key = norm_key(span)
    if not span_key:
        return None
    for alias in _known_lab_aliases():
        if span_key == alias or span_key.startswith(f"{alias} "):
            return alias
    return None


def lab_like_candidate(span: str, pretty_name: str, sentence: str, section: str, context: str) -> bool:
    span_key = norm_key(span)
    pretty_key = norm_key(pretty_name)
    text = f"{span_key} {pretty_key}"
    if leading_lab_alias(span):
        return bool(NUMERIC_CONTEXT.search(f"{sentence} {context}"))
    if any(alias in LAB_ALIASES for alias in (span_key, pretty_key)):
        return bool(NUMERIC_CONTEXT.search(f"{sentence} {context}"))
    if re.search(r"\b(?:leukocyte|white blood cell|natriuretic peptide|oxygen saturation)\b", text, re.I):
        return bool(NUMERIC_CONTEXT.search(f"{sentence} {context}"))
    if re.search(r"\b(?:labs?|laboratory|vitals?|coag(?:s|ulation)?|hematology)\b", section, re.I):
        return bool(leading_lab_alias(span) or pretty_key in LAB_ALIASES)
    return False


def normalize_lab_value_span(span: str, start: int, end: int) -> tuple[str, int, int, str | None]:
    alias = leading_lab_alias(span)
    if not alias:
        return span, start, end, None
    span_key = norm_key(span)
    if span_key == alias:
        return span, start, end, None
    suffix = span_key[len(alias) :].strip()
    if not re.match(r"^(?:high|low|elevated|increased|decreased|reduced|depressed|abnormal|normal|stable)\b", suffix):
        return span, start, end, None
    raw = clean(span)
    alias_len = len(raw[: len(alias)])
    return raw[:alias_len], start, start + alias_len, alias


def normalize_numeric_value(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = clean(str(value))
    normalized = re.sub(r"(?<=\d),(?=\d{3}\b)", "", normalized)
    normalized = re.sub(r"\s*(?:\u2013|\u2014|-)\s*", "-", normalized)
    normalized = re.sub(r"\s*/\s*", "/", normalized)
    return normalized


def normalize_value_status_text(text: str) -> str:
    cleaned = clean(text)
    cleaned = re.sub(r"\b(?:high|low)[ -]?flow\b", "flow", cleaned, flags=re.I)
    return cleaned


def numeric_scalar(value: str | None) -> float | None:
    normalized = normalize_numeric_value(value)
    if not normalized:
        return None
    normalized = re.sub(r"^(?:<=|>=|<|>|~)\s*", "", normalized)
    if "/" in normalized or "-" in normalized:
        return None
    try:
        return float(normalized)
    except ValueError:
        return None


def numeric_range_bounds(low: str | None, high: str | None) -> tuple[float, float] | None:
    low_value = numeric_scalar(low)
    high_value = numeric_scalar(high)
    if low_value is None or high_value is None:
        return None
    if low_value > high_value:
        low_value, high_value = high_value, low_value
    return low_value, high_value


def infer_inline_value_status(text: str) -> str | None:
    lower = normalize_value_status_text(text).lower()
    if re.search(r"\(\s*h\s*\)|(?:^|[\s:;,\(])h(?:$|[\s\).,;])|\b(?:high|elevated|increased)\b", lower):
        return "high"
    if re.search(r"\(\s*l\s*\)|(?:^|[\s:;,\(])l(?:$|[\s\).,;])|\b(?:low|decreased|reduced|depressed)\b", lower):
        return "low"
    if re.search(r"\(\s*n\s*\)|\b(?:normal|within normal limits|wnl)\b", lower):
        return "normal"
    if re.search(r"\(\s*a\s*\)|\babnormal\b", lower):
        return "abnormal"
    if re.search(r"\bpositive\b", lower):
        return "positive"
    if re.search(r"\bnegative\b", lower):
        return "negative"
    return None


def status_from_reference_bounds(value: str | None, low: str | None, high: str | None) -> str | None:
    scalar = numeric_scalar(value)
    bounds = numeric_range_bounds(low, high)
    if scalar is None or bounds is None:
        return None
    low_value, high_value = bounds
    if scalar < low_value:
        return "low"
    if scalar > high_value:
        return "high"
    return "normal"


def infer_reference_range_status_near_value(
    context: str,
    numeric_value: str | None,
    start: int | None,
    end: int | None,
) -> str | None:
    if numeric_value is None or start is None or end is None:
        return None
    left = max(0, start - 130)
    local = context[left : min(len(context), end + 170)]
    value_mid = ((start + end) / 2) - left
    pattern = re.compile(
        rf"(?i)\b{REFERENCE_RANGE_CUE_PATTERN}\b\s*[:=]?\s*"
        rf"(?P<low>{NUMERIC_TOKEN_PATTERN})\s*{RANGE_SEPARATOR_PATTERN}\s*"
        rf"(?P<high>{NUMERIC_TOKEN_PATTERN})"
    )
    matches = sorted(pattern.finditer(local), key=lambda match: min(abs(match.start() - value_mid), abs(match.end() - value_mid)))
    for match in matches:
        status = status_from_reference_bounds(numeric_value, match.group("low"), match.group("high"))
        if status:
            return status
    return None


def infer_threshold_value_status(span: str, pretty_name: str, numeric_value: str | None) -> str | None:
    aliases = set(aliases_for_lab(span, pretty_name))
    scalar = numeric_scalar(numeric_value)
    if aliases & {"temperature", "temp"} and scalar is not None:
        if scalar < 36:
            return "low"
        if scalar >= 38:
            return "high"
        return "normal"
    if aliases & {"ph", "blood ph", "abg ph", "arterial ph"} and scalar is not None:
        if scalar < 7.35:
            return "low"
        if scalar > 7.45:
            return "high"
        return "normal"
    if aliases & {"anc", "absolute neutrophil count", "neutrophil count"} and scalar is not None:
        if scalar < 1.5:
            return "low"
        if scalar > 7.5:
            return "high"
        return "normal"
    if aliases & {"na", "sodium"} and scalar is not None:
        if scalar < 135:
            return "low"
        if scalar > 145:
            return "high"
        return "normal"
    if aliases & {"potassium", "k"} and scalar is not None:
        if scalar < 3.5:
            return "low"
        if scalar > 5.1:
            return "high"
        return "normal"
    if aliases & {"wbc", "white blood cell count"} and scalar is not None:
        if scalar < 4.0:
            return "low"
        if scalar > 10.5:
            return "high"
        return "normal"
    if aliases & {"platelet", "platelets", "plt"} and scalar is not None:
        if scalar < 150:
            return "low"
        if scalar > 450:
            return "high"
        return "normal"
    if aliases & {"hemoglobin", "hgb"} and scalar is not None:
        if scalar < 12.0:
            return "low"
        if scalar > 17.0:
            return "high"
        return "normal"
    if aliases & {"hematocrit", "hct"} and scalar is not None:
        if scalar < 36.0:
            return "low"
        if scalar > 52.0:
            return "high"
        return "normal"
    if aliases & {"bnp", "brain natriuretic peptide", "b-type natriuretic peptide"} and scalar is not None:
        if scalar > 100:
            return "high"
        return "normal"
    if aliases & {"troponin", "troponin t", "troponin i"} and scalar is not None:
        if scalar > 0.04:
            return "high"
        return "normal"
    if aliases & {"creatinine", "cr"} and scalar is not None:
        if scalar > 1.3:
            return "high"
        return "normal"
    if aliases & {"glucose", "fingerstick glucose", "fs glucose"} and scalar is not None:
        if scalar < 70:
            return "low"
        if scalar > 180:
            return "high"
        return "normal"
    if aliases & {"bun", "urea"} and scalar is not None:
        if scalar > 20:
            return "high"
        return "normal"
    if aliases & {"calcium"} and scalar is not None:
        if scalar < 8.5:
            return "low"
        if scalar > 10.5:
            return "high"
        return "normal"
    if aliases & {"magnesium", "mag"} and scalar is not None:
        if scalar < 1.6:
            return "low"
        if scalar > 2.6:
            return "high"
        return "normal"
    if aliases & {"phosphorus", "phos"} and scalar is not None:
        if scalar < 2.5:
            return "low"
        if scalar > 4.5:
            return "high"
        return "normal"
    if aliases & {"lactate"} and scalar is not None:
        if scalar > 2.0:
            return "high"
        return "normal"
    if aliases & {"albumin"} and scalar is not None:
        if scalar < 3.5:
            return "low"
        if scalar > 5.0:
            return "high"
        return "normal"
    if aliases & {"alt", "alanine aminotransferase", "ast", "aspartate aminotransferase"} and scalar is not None:
        if scalar > 40:
            return "high"
        return "normal"
    if aliases & {"alkphos", "alk phos", "alkaline phosphatase"} and scalar is not None:
        if scalar > 120:
            return "high"
        return "normal"
    if aliases & {"bilirubin", "t bili", "tbili", "total bilirubin"} and scalar is not None:
        if scalar > 1.2:
            return "high"
        return "normal"
    if aliases & {"bicarbonate", "bicarb", "hco3", "co2"} and scalar is not None:
        if scalar < 22:
            return "low"
        if scalar > 29:
            return "high"
        return "normal"
    if aliases & {"crp", "c reactive protein", "c-reactive protein"} and scalar is not None:
        if scalar > 3:
            return "high"
        return "normal"
    if aliases & {"d-dimer", "ddimer", "d dimer"} and scalar is not None:
        if scalar > 0.5:
            return "high"
        return "normal"
    if aliases & {"ldh", "lactate dehydrogenase", "l-lactate dehydrogenase"} and scalar is not None:
        if scalar > 250:
            return "high"
        return "normal"
    if aliases & {"procalcitonin", "pct"} and scalar is not None:
        if scalar >= 0.25:
            return "high"
        return "normal"
    if aliases & {"ca 19-9", "ca19-9", "cancer antigen 19-9", "carbohydrate antigen 19-9"} and scalar is not None:
        if scalar > 37:
            return "high"
        return "normal"
    if aliases & {"pt", "prothrombin time"} and scalar is not None:
        if scalar > 13.5:
            return "high"
        return "normal"
    if aliases & {"ptt", "partial thromboplastin time", "activated partial thromboplastin time"} and scalar is not None:
        if scalar > 35:
            return "high"
        return "normal"
    if aliases & {"inr", "international normalized ratio"} and scalar is not None:
        if scalar > 1.2:
            return "high"
        return "normal"
    if aliases & {"rr", "respiratory rate"} and scalar is not None:
        if scalar >= 21:
            return "high"
        if scalar <= 11:
            return "low"
        return "normal"
    if aliases & {"hr", "heart rate"} and scalar is not None:
        if scalar >= 100:
            return "high"
        if scalar <= 50:
            return "low"
        if 60 <= scalar <= 100:
            return "normal"
    if aliases & {"spo2", "sao2", "o2 sat", "o2 saturation", "oxygen saturation", "peripheral oxygen saturation"} and scalar is not None:
        if scalar <= 93:
            return "low"
        if 94 <= scalar <= 100:
            return "normal"
    if aliases & {"pao2", "arterial oxygen partial pressure", "partial pressure of oxygen"} and scalar is not None:
        if scalar < 80:
            return "low"
        if scalar <= 100:
            return "normal"
        return "high"
    if aliases & {"ef", "lvef", "ejection fraction", "left ventricular ejection fraction"} and scalar is not None:
        if scalar < 50:
            return "low"
        return "normal"
    if aliases & {"bp", "blood pressure"}:
        value = normalize_numeric_value(numeric_value) or ""
        bp_match = re.match(r"(?P<sys>\d{2,3})/(?P<dia>\d{2,3})", value)
        if bp_match:
            systolic = float(bp_match.group("sys"))
            diastolic = float(bp_match.group("dia"))
            if systolic < 100 or diastolic <= 60:
                return "low"
            if systolic >= 140 or diastolic >= 90:
                return "high"
            return "normal"
    return None


def infer_adjacent_value_status(context: str, start: int | None, end: int | None) -> str | None:
    if start is None or end is None:
        return None
    suffix = context[end : min(len(context), end + 28)]
    suffix = re.split(r"[;\n]|\b(?:and|or)\b", suffix, maxsplit=1, flags=re.I)[0]
    if not re.match(
        r"^\s*(?:\(\s*[HLAN]\s*\)|\b(?:high|low|normal|abnormal|elevated|increased|decreased|reduced|depressed|positive|negative)\b)",
        suffix,
        re.I,
    ):
        return None
    return infer_inline_value_status(suffix)


def threshold_unit_is_compatible(span: str, pretty_name: str, matched_text: str) -> bool:
    aliases = set(aliases_for_lab(span, pretty_name))
    unit_match = re.search(rf"(?i)(?:^|\s)(?P<unit>{UNIT_PATTERN})(?=$|[\s(),;])", matched_text)
    if not unit_match:
        return False
    unit = norm_key(unit_match.group("unit"))
    if aliases & {"na", "sodium", "potassium", "k"}:
        return unit in {"mmol/l", "meq/l"}
    if aliases & {"wbc", "white blood cell count", "anc", "absolute neutrophil count", "neutrophil count", "platelet", "platelets", "plt"}:
        return unit in {"k/ul", "k/l", "cells/ul", "x10^3", "x10^9/l", "10^9/l"}
    if aliases & {"hemoglobin", "hgb", "hematocrit", "hct", "albumin"}:
        return unit in {"g/dl", "gm/dl", "g/l", "%", "percent"}
    if aliases & {"pt", "prothrombin time", "ptt", "partial thromboplastin time", "activated partial thromboplastin time"}:
        return unit in {"s", "sec", "secs", "second", "seconds"}
    if aliases & {"inr", "international normalized ratio"}:
        return unit == "ratio"
    if aliases & {"temperature", "temp"}:
        return unit in {"degrees c", "degree c", "degrees f", "degree f", "c", "f", "celsius", "fahrenheit"}
    if aliases & {"hr", "heart rate"}:
        return unit in {"bpm", "beat/min", "beats/min", "beat/minute", "beats/minute"}
    if aliases & {"rr", "respiratory rate"}:
        return unit in {"breath/min", "breaths/min", "breath/minute", "breaths/minute", "/min"}
    if aliases & {"bp", "blood pressure", "pao2", "arterial oxygen partial pressure", "partial pressure of oxygen"}:
        return unit in {"mmhg", "mm hg"}
    if aliases & {"spo2", "sao2", "o2 sat", "o2 saturation", "oxygen saturation", "peripheral oxygen saturation", "ef", "lvef", "ejection fraction", "left ventricular ejection fraction"}:
        return unit in {"%", "percent"}
    return True


def preserve_numeric_qualifier(value_parse: dict[str, Any]) -> dict[str, Any]:
    value = normalize_numeric_value(value_parse.get("numeric_value"))
    if not value:
        return value_parse
    comparator = clean(str(value_parse.get("comparator") or ""))
    if comparator and not value.startswith(comparator):
        value = f"{comparator}{value}"
    elif not re.match(r"^(?:<=|>=|<|>|~)", value):
        value_text = str(value_parse.get("value_text") or "")
        if re.search(rf"~\s*{re.escape(value)}\b", value_text):
            value = f"~{value}"
    value_parse["numeric_value"] = value
    return value_parse


def infer_value_status_from_match(
    span: str,
    pretty_name: str,
    text: str,
    context: str,
    start: int | None,
    end: int | None,
    numeric_value: str | None = None,
) -> str:
    text = normalize_value_status_text(text)
    context = normalize_value_status_text(context)
    inline_status = infer_inline_value_status(text)
    if inline_status:
        return inline_status
    adjacent_status = infer_adjacent_value_status(context, start, end)
    if adjacent_status:
        return adjacent_status
    if re.search(rf"\b{REFERENCE_RANGE_CUE_PATTERN}\b", text, re.I):
        reference_status = infer_reference_range_status_near_value(text, numeric_value, 0, len(text))
        if reference_status:
            return reference_status
    if threshold_unit_is_compatible(span, pretty_name, text):
        threshold_status = infer_threshold_value_status(span, pretty_name, numeric_value)
        if threshold_status:
            return threshold_status
    return "unknown"


@lru_cache(maxsize=8192)
def _aliases_for_lab(span: str, pretty_name: str) -> tuple[str, ...]:
    candidates = [norm_key(span), norm_key(pretty_name)]
    aliases = []
    for candidate in candidates:
        if not candidate:
            continue
        aliases.append(candidate)
        aliases.extend(LAB_ALIASES.get(candidate, []))
        leading_alias = leading_lab_alias(candidate)
        if leading_alias:
            aliases.append(leading_alias)
            aliases.extend(LAB_ALIASES.get(leading_alias, []))
    # Keep short aliases after longer aliases so "blood pressure" matches before "bp".
    return tuple(sorted(set(a for a in aliases if a), key=len, reverse=True))


def aliases_for_lab(span: str, pretty_name: str) -> list[str]:
    return list(_aliases_for_lab(span, pretty_name))


def context_with_marker(text: str, start: int, end: int, before: int = 260, after: int = 620) -> tuple[str, int, int]:
    left = max(0, start - before)
    right = min(len(text), end + after)
    context = text[left:right]
    return context, start - left, end - left


def clean_context_with_span(context: str, local_start: int, local_end: int) -> tuple[str, int, int]:
    prefix = clean(context[:local_start])
    span = clean(context[local_start:local_end])
    suffix = clean(context[local_end:])
    cleaned = " ".join(part for part in (prefix, span, suffix) if part)
    cleaned_start = len(prefix) + (1 if prefix and span else 0)
    cleaned_end = cleaned_start + len(span)
    return cleaned, cleaned_start, cleaned_end


def local_span_slice(context: str, start: int | None, end: int | None, before: int = 90, after: int = 130) -> tuple[str, int | None, int | None]:
    if start is None or end is None:
        return context, None, None
    left = max(0, start - before)
    right = min(len(context), end + after)
    return context[left:right], start - left, end - left


def sort_matches_by_locality(matches: list[re.Match[str]], start: int | None, end: int | None) -> list[re.Match[str]]:
    if start is None or end is None:
        return matches
    return sorted(
        matches,
        key=lambda match: (
            0 if match.start() <= end + 3 and match.end() >= start - 3 else 1,
            min(abs(match.start() - start), abs(match.start() - end), abs(match.end() - start), abs(match.end() - end)),
            match.start(),
        ),
    )


def match_anchored_to_span(match: re.Match[str], start: int | None, end: int | None, tolerance: int = 6) -> bool:
    if start is None or end is None:
        return True
    return match.start() <= start + tolerance and match.end() >= end - tolerance


def maybe_numeric_from_vital_table(span: str, pretty_name: str, context: str, start: int | None, end: int | None) -> dict[str, Any] | None:
    if start is None or end is None:
        return None
    aliases = set(aliases_for_lab(span, pretty_name))
    target_alias = None
    for alias in ("hr", "bp", "rr", "spo2", "sao2", "o2 sat"):
        if alias in aliases:
            target_alias = alias
            break
    if target_alias not in {"hr", "bp", "rr", "spo2", "sao2", "o2 sat"}:
        return None
    header_start = max(0, start - 80)
    row_end = min(len(context), end + 160)
    table_text = context[header_start:row_end]
    if not re.search(r"\b(?:position\s+)?hr\s+bp\s+rr\b", table_text, re.I):
        return None
    rows = [
        re.search(
            r"(?i)\b(?:rest|activity|recovery)\s+\S+\s+"
            r"(?P<hr>\d{2,3})\s+"
            r"(?P<bp>\d{2,3}/\d{2,3})\s+"
            r"(?P<rr>\d{1,3})\s+"
            r"(?P<spo2>\d{2,3}%?)",
            table_text,
        ),
        re.search(
            r"(?i)\b(?:supine|rest|sit|sitting|standing|activity|recovery|ambulation)"
            r"(?:\s*/\s*(?:supine|rest|sit|sitting|standing|activity|recovery|ambulation))*"
            r"[^0-9]{0,45}"
            r"(?P<hr>\d{2,3})\s+"
            r"(?P<bp>\d{2,3}/\d{2,3})\s+"
            r"(?P<rr>\d{1,3})\s+"
            r"(?P<spo2>\d{2,3}%?)",
            table_text,
        ),
        re.search(
            r"(?i)\b(?:supine|rest|sit|sitting|standing|activity|recovery|ambulation)"
            r"(?:\s*/\s*(?:supine|rest|sit|sitting|standing|activity|recovery|ambulation))*"
            r"[^0-9]{0,45}"
            r"(?P<hr>\d{2,3})\s+"
            r"(?P<bp>\d{2,3}/\d{2,3})\s+"
            r"(?P<spo2>\d{2,3}%?)"
            r"(?:\s+on\s+(?:RA|\d+\s*L))?",
            table_text,
        ),
        re.search(
            r"(?i)\b(?:rest|activity|recovery)\s+\S+\s+"
            r"(?P<hr>\d{2,3})\s+"
            r"(?P<bp>\d{2,3}/\d{2,3})\s+"
            r"(?P<spo2>\d{1,3}%?(?:-\d{1,3}%?[A-Za-z]*)?)",
            table_text,
        ),
    ]
    row = next((candidate for candidate in rows if candidate), None)
    if not row:
        return None
    if target_alias == "hr":
        value = row.group("hr")
        unit = "beats/min"
        label = "HR"
    elif target_alias == "bp":
        value = row.group("bp")
        unit = "mmHg"
        label = "BP"
    elif target_alias == "rr" and "rr" in row.groupdict():
        value = row.group("rr")
        unit = "breaths/min"
        label = "RR"
    elif target_alias in {"spo2", "sao2", "o2 sat"} and "spo2" in row.groupdict():
        value = row.group("spo2")
        unit = "%"
        label = target_alias.upper()
    else:
        return None
    if not value:
        return None
    value = normalize_numeric_value(value)
    value_text = f"{label}={value}" + (f" {unit}" if unit else "")
    return {
        "numeric_value": value,
        "unit": unit,
        "comparator": None,
        "value_text": value_text,
        "value_status": infer_value_status_from_match(span, pretty_name, value_text, context, start, end, value),
        "value_parse_method": "vital_table_position_row_regex",
        "measurement_context": "observed_patient",
    }


def default_unit_for_lab(aliases: Iterable[str]) -> str | None:
    aliases = set(aliases)
    if aliases & {"na", "sodium"}:
        return "mmol/L"
    if aliases & {"wbc", "white blood cell count"}:
        return "K/uL"
    if aliases & {"bnp", "brain natriuretic peptide", "b-type natriuretic peptide"}:
        return "pg/mL"
    if aliases & {"hr", "heart rate"}:
        return "beats/min"
    if aliases & {"rr", "respiratory rate"}:
        return "breaths/min"
    if aliases & {"bp", "blood pressure"}:
        return "mmHg"
    if aliases & {"spo2", "sao2", "o2 sat", "o2 saturation", "oxygen saturation", "peripheral oxygen saturation"}:
        return "%"
    if aliases & {"ef", "lvef", "ejection fraction", "left ventricular ejection fraction"}:
        return "%"
    if aliases & {"inr", "international normalized ratio"}:
        return "ratio"
    if aliases & {"pt", "prothrombin time", "ptt", "partial thromboplastin time", "activated partial thromboplastin time"}:
        return "sec"
    if aliases & {"temperature", "temp"}:
        return "degrees C"
    if aliases & {"ph", "blood ph", "abg ph", "arterial ph"}:
        return "pH"
    if aliases & {"pao2", "arterial oxygen partial pressure", "partial pressure of oxygen"}:
        return "mmHg"
    if aliases & {"ca 19-9", "ca19-9", "cancer antigen 19-9", "carbohydrate antigen 19-9"}:
        return "U/mL"
    if aliases & {"creatinine", "cr"}:
        return "mg/dL"
    if aliases & {"troponin", "troponin t", "troponin i"}:
        return "ng/mL"
    if aliases & {"glucose", "fingerstick glucose", "fs glucose"}:
        return "mg/dL"
    if aliases & {"bun", "urea"}:
        return "mg/dL"
    if aliases & {"calcium"}:
        return "mg/dL"
    if aliases & {"magnesium", "mag"}:
        return "mg/dL"
    if aliases & {"phosphorus", "phos"}:
        return "mg/dL"
    if aliases & {"lactate"}:
        return "mmol/L"
    if aliases & {"albumin"}:
        return "g/dL"
    if aliases & {"alanine aminotransferase", "alt", "aspartate aminotransferase", "ast", "alkphos", "alk phos", "alkaline phosphatase", "ldh"}:
        return "U/L"
    if aliases & {"bilirubin", "t bili", "tbili", "total bilirubin"}:
        return "mg/dL"
    if aliases & {"crp"}:
        return "mg/L"
    if aliases & {"d-dimer", "ddimer", "d dimer"}:
        return "mg/L FEU"
    return None


def maybe_numeric_from_reference_range_row(
    span: str,
    pretty_name: str,
    context: str,
    start: int | None,
    end: int | None,
) -> dict[str, Any] | None:
    aliases = aliases_for_lab(span, pretty_name)
    alias_set = set(aliases)
    local_context, local_start, local_end = local_span_slice(context, start, end, before=35, after=220)
    patterns = []
    for alias in aliases:
        escaped = re.escape(alias)
        name_boundary = rf"(?<![A-Za-z0-9]){escaped}(?![A-Za-z0-9])"
        patterns.extend(
            [
                re.compile(
                    rf"(?i){name_boundary}[^.\n]{{0,90}}\b{REFERENCE_RANGE_CUE_PATTERN}\b\s*[:=]?\s*"
                    rf"(?P<low>{NUMERIC_TOKEN_PATTERN})\s*{RANGE_SEPARATOR_PATTERN}\s*"
                    rf"(?P<high>{NUMERIC_TOKEN_PATTERN})\s*(?P<range_unit>{UNIT_PATTERN})?"
                    rf"[^.\n\d]{{0,45}}(?:;|,|\bresult\b|\bvalue\b|\bactual\b)[^.\n\d]{{0,45}}"
                    rf"(?P<comparator><=|>=|<|>)?\s*(?P<value>{NUMERIC_TOKEN_PATTERN})\s*(?P<unit>{UNIT_PATTERN})?"
                    rf"{LAB_INLINE_STATUS_PATTERN}"
                ),
                re.compile(
                    rf"(?i){name_boundary}\s*(?:\([^)]{{0,30}}\))?\s*(?:[:=,\-]|\bof\b)?\s*"
                    rf"(?P<comparator><=|>=|<|>)?\s*(?P<value>{NUMERIC_TOKEN_PATTERN})(?!\s*{RANGE_SEPARATOR_PATTERN})\s*"
                    rf"(?P<unit>{UNIT_PATTERN})?{LAB_INLINE_STATUS_PATTERN}"
                    rf"[^.\n]{{0,100}}\b{REFERENCE_RANGE_CUE_PATTERN}\b\s*[:=]?\s*"
                    rf"(?P<low>{NUMERIC_TOKEN_PATTERN})\s*{RANGE_SEPARATOR_PATTERN}\s*"
                    rf"(?P<high>{NUMERIC_TOKEN_PATTERN})\s*(?P<range_unit>{UNIT_PATTERN})?"
                ),
            ]
        )
    all_matches: list[re.Match[str]] = []
    for pattern in patterns:
        all_matches.extend(pattern.finditer(local_context))
    matches = [
        match
        for match in sort_matches_by_locality(all_matches, local_start, local_end)
        if match_anchored_to_span(match, local_start, local_end, tolerance=8)
    ]
    for match in matches:
        value = normalize_numeric_value(match.group("value"))
        inline_status = infer_inline_value_status(match.group(0))
        status = inline_status or status_from_reference_bounds(value, match.group("low"), match.group("high"))
        status = status or infer_threshold_value_status(span, pretty_name, value) or "unknown"
        unit = clean(match.group("unit") or match.group("range_unit") or "") or default_unit_for_lab(alias_set)
        return {
            "numeric_value": value,
            "unit": unit,
            "comparator": match.group("comparator"),
            "value_text": clean(match.group(0)),
            "value_status": status,
            "value_parse_method": "reference_range_result_regex",
            "measurement_context": infer_measurement_context(span, pretty_name, local_context, match.start("value"), match.end("value")),
        }
    return None


def maybe_numeric_from_approx_phrase(span: str, pretty_name: str, context: str, start: int | None, end: int | None) -> dict[str, Any] | None:
    aliases = aliases_for_lab(span, pretty_name)
    alias_set = set(aliases)
    local_context, local_start, local_end = local_span_slice(context, start, end, before=35, after=90)
    for alias in aliases:
        escaped = re.escape(alias)
        name_boundary = rf"(?<![A-Za-z0-9]){escaped}(?![A-Za-z0-9])"
        pattern = re.compile(
            rf"(?i){name_boundary}"
            rf"(?P<bridge>[^.\n;\d<>]{{0,55}}\b(?:to|at|up to|down to|high at|low at|elevated to|increased to|decreased to|reduced to|dropped to|rose to|stable at|ranged? to|as high as|as low as|in the|(?:back\s+)?into)\b[^.\n;\d<>]{{0,20}})"
            rf"(?P<comparator><=|>=|<|>)?\s*(?P<value>{NUMERIC_VALUE_PATTERN})\s*(?P<unit>{UNIT_PATTERN})?"
            rf"{LAB_INLINE_STATUS_PATTERN}"
        )
        matches = [
            match
            for match in sort_matches_by_locality(list(pattern.finditer(local_context)), local_start, local_end)
            if match_anchored_to_span(match, local_start, local_end)
        ]
        for match in matches:
            bridge = match.group("bridge") or ""
            if len(bridge) > 55:
                continue
            value = normalize_numeric_value(match.group("value"))
            return {
                "numeric_value": value,
                "unit": clean(match.group("unit") or "") or default_unit_for_lab(alias_set),
                "comparator": match.group("comparator"),
                "value_text": clean(match.group(0)),
                "value_status": infer_value_status_from_match(span, pretty_name, match.group(0), local_context, match.start(), match.end(), value),
                "value_parse_method": "approximate_or_range_phrase_regex",
                "measurement_context": "observed_patient",
            }
    return None


def maybe_numeric_from_trend_phrase(span: str, pretty_name: str, context: str, start: int | None, end: int | None) -> dict[str, Any] | None:
    aliases = aliases_for_lab(span, pretty_name)
    alias_set = set(aliases)
    local_context, local_start, local_end = local_span_slice(context, start, end, before=35, after=130)
    for alias in aliases:
        escaped = re.escape(alias)
        name_boundary = rf"(?<![A-Za-z0-9]){escaped}(?![A-Za-z0-9])"
        pattern = re.compile(
            rf"(?i){name_boundary}\s+"
            rf"(?:improved|improving|decreased|dropped|fell|increased|rose|worsened|changed|trended|went)?\s*"
            rf"(?:from\s+)?(?P<from_value>{NUMERIC_VALUE_PATTERN})\s*(?P<from_unit>{UNIT_PATTERN})?\s*"
            rf"(?:to|->|=>|down\s+to|up\s+to|then)\s*"
            rf"(?P<comparator><=|>=|<|>)?\s*(?P<value>{NUMERIC_VALUE_PATTERN})\s*(?P<unit>{UNIT_PATTERN})?"
            rf"{LAB_INLINE_STATUS_PATTERN}"
        )
        matches = [
            match
            for match in sort_matches_by_locality(list(pattern.finditer(local_context)), local_start, local_end)
            if match_anchored_to_span(match, local_start, local_end, tolerance=8)
        ]
        for match in matches:
            value = normalize_numeric_value(match.group("value"))
            unit = clean(match.group("unit") or match.group("from_unit") or "") or default_unit_for_lab(alias_set)
            return {
                "numeric_value": value,
                "unit": unit,
                "comparator": match.group("comparator"),
                "value_text": clean(match.group(0)),
                "value_status": infer_value_status_from_match(span, pretty_name, match.group(0), local_context, match.start("value"), match.end(), value),
                "value_parse_method": "trend_final_value_regex",
                "measurement_context": "observed_patient",
            }
    return None


def maybe_numeric_from_temperature(span: str, pretty_name: str, context: str, start: int | None, end: int | None) -> dict[str, Any] | None:
    aliases = aliases_for_lab(span, pretty_name)
    if not set(aliases) & {"temperature", "temp"}:
        return None
    local_context, local_start, local_end = local_span_slice(context, start, end, before=35, after=110)
    patterns = []
    for alias in aliases:
        if alias not in {"temperature", "temp"}:
            continue
        escaped = re.escape(alias)
        name_boundary = rf"(?<![A-Za-z0-9]){escaped}(?![A-Za-z0-9])"
        patterns.append(
            re.compile(
                rf"(?i){name_boundary}\s*(?:\([^)]{{0,30}}\))?\s*(?:[:=,\-]|\bof\b)?\s*"
                rf"(?P<comparator><=|>=|<|>)?\s*(?P<value>{NUMERIC_VALUE_PATTERN})\s*"
                rf"(?P<unit>°?\s*[CF]|deg(?:ree)?s?\s*[CF]|celsius|fahrenheit)?{LAB_INLINE_STATUS_PATTERN}"
            )
        )
    all_matches: list[re.Match[str]] = []
    for pattern in patterns:
        all_matches.extend(pattern.finditer(local_context))
    matches = [
        match
        for match in sort_matches_by_locality(all_matches, local_start, local_end)
        if match_anchored_to_span(match, local_start, local_end, tolerance=8)
    ]
    for match in matches:
        value = normalize_numeric_value(match.group("value"))
        unit = clean(match.group("unit") or "") or default_unit_for_lab(aliases)
        return {
            "numeric_value": value,
            "unit": unit or None,
            "comparator": match.group("comparator"),
            "value_text": clean(match.group(0)),
            "value_status": infer_value_status_from_match(span, pretty_name, match.group(0), local_context, match.start(), match.end(), value),
            "value_parse_method": "temperature_regex",
            "measurement_context": "observed_patient",
        }
    return None


def parse_coag_panel_value(span: str, pretty_name: str, context: str, start: int | None = None, end: int | None = None) -> dict[str, Any] | None:
    aliases = set(aliases_for_lab(span, pretty_name))
    span_key = norm_key(span)
    pretty_key = norm_key(pretty_name)
    explicit_coag_span = span_key in {"pt", "ptt", "inr"}
    wants_pt = span_key == "pt" or (not explicit_coag_span and ("prothrombin time" in pretty_key or "pt" in aliases))
    wants_ptt = span_key == "ptt" or (not explicit_coag_span and ("partial thromboplastin" in pretty_key or "ptt" in aliases))
    wants_inr = span_key == "inr" or (not explicit_coag_span and ("international normalized ratio" in pretty_key or "inr" in aliases))
    if not (wants_pt or wants_ptt or wants_inr):
        return None

    panel_pattern = re.compile(
        rf"(?i)\bpt\s*/\s*ptt\s*/\s*inr\s*[:=]\s*"
        rf"(?P<pt>{SIMPLE_NUMERIC_VALUE_PATTERN})(?:\s*(?P<pt_unit>seconds?|secs|sec|s))?\s*/\s*"
        rf"(?P<ptt>{SIMPLE_NUMERIC_VALUE_PATTERN})(?:\s*(?P<ptt_unit>seconds?|secs|sec|s))?\s*/\s*"
        rf"(?P<inr>{SIMPLE_NUMERIC_VALUE_PATTERN})(?:\s*(?P<inr_unit>ratio))?"
    )
    panel_matches = sort_matches_by_locality(list(panel_pattern.finditer(context)), start, end)
    if panel_matches:
        panel = panel_matches[0]
        panel_text = clean(panel.group(0))
        if wants_pt:
            label = "PT"
            value = panel.group("pt")
            unit = clean(panel.group("pt_unit") or "") or ("seconds" if "seconds" in panel_text.lower() else "sec")
        elif wants_ptt:
            label = "PTT"
            value = panel.group("ptt")
            unit = clean(panel.group("ptt_unit") or "") or ("seconds" if "seconds" in panel_text.lower() else "sec")
        else:
            label = "INR"
            value = panel.group("inr")
            unit = clean(panel.group("inr_unit") or "") or ("ratio" if "ratio" in panel_text.lower() else None)
        return {
            "numeric_value": value,
            "unit": unit,
            "comparator": None,
            "value_text": f"{label}={value}" + (f" {unit}" if unit else ""),
            "value_status": infer_value_status_from_match(span, pretty_name, panel_text, context, panel.start(), panel.end(), value),
            "value_parse_method": "coag_panel_pt_ptt_inr_regex",
            "measurement_context": "observed_patient",
        }

    adjacent_panel_pattern = re.compile(
        rf"(?i)\bpt\s*(?:[-:=]|\s)\s*(?P<pt>{SIMPLE_NUMERIC_VALUE_PATTERN})\s+"
        rf"ptt\s*(?:[-:=]|\s)\s*(?P<ptt>{SIMPLE_NUMERIC_VALUE_PATTERN})\s+"
        rf"(?:inr(?:\s*\(\s*pt\s*\))?)\s*(?:[-:=]|\s)\s*(?P<inr>{SIMPLE_NUMERIC_VALUE_PATTERN})(?:\s*(?P<inr_unit>ratio))?"
    )
    adjacent_matches = sort_matches_by_locality(list(adjacent_panel_pattern.finditer(context)), start, end)
    if adjacent_matches:
        panel = adjacent_matches[0]
        panel_text = clean(panel.group(0))
        if wants_pt:
            label = "PT"
            value = panel.group("pt")
            unit = "seconds" if "seconds" in panel_text.lower() else "sec"
        elif wants_ptt:
            label = "PTT"
            value = panel.group("ptt")
            unit = "seconds" if "seconds" in panel_text.lower() else "sec"
        else:
            label = "INR"
            value = panel.group("inr")
            unit = clean(panel.group("inr_unit") or "") or ("ratio" if "ratio" in panel_text.lower() else None)
        return {
            "numeric_value": value,
            "unit": unit,
            "comparator": None,
            "value_text": f"{label}={value}" + (f" {unit}" if unit else ""),
            "value_status": infer_value_status_from_match(span, pretty_name, panel_text, context, panel.start(), panel.end(), value),
            "value_parse_method": "coag_adjacent_pt_ptt_inr_regex",
            "measurement_context": "observed_patient",
        }

    narrative_panel_pattern = re.compile(
        rf"(?i)\bpt\s+(?:of\s+)?(?P<pt>{SIMPLE_NUMERIC_VALUE_PATTERN})\s*,?\s+"
        rf"ptt\s+(?:of\s+)?(?P<ptt>{SIMPLE_NUMERIC_VALUE_PATTERN})\s*,?\s+"
        rf"(?:and\s+)?(?:an?\s+)?inr\s+(?:of\s+)?(?P<inr>{SIMPLE_NUMERIC_VALUE_PATTERN})(?:\s*(?P<inr_unit>ratio))?"
    )
    narrative_matches = sort_matches_by_locality(list(narrative_panel_pattern.finditer(context)), start, end)
    if narrative_matches:
        panel = narrative_matches[0]
        panel_text = clean(panel.group(0))
        if wants_pt:
            label = "PT"
            value = panel.group("pt")
            unit = "seconds" if "seconds" in panel_text.lower() else "sec"
        elif wants_ptt:
            label = "PTT"
            value = panel.group("ptt")
            unit = "seconds" if "seconds" in panel_text.lower() else "sec"
        else:
            label = "INR"
            value = panel.group("inr")
            unit = clean(panel.group("inr_unit") or "") or ("ratio" if "ratio" in panel_text.lower() else None)
        return {
            "numeric_value": value,
            "unit": unit,
            "comparator": None,
            "value_text": f"{label}={value}" + (f" {unit}" if unit else ""),
            "value_status": infer_value_status_from_match(span, pretty_name, panel_text, context, panel.start(), panel.end(), value),
            "value_parse_method": "coag_narrative_pt_ptt_inr_regex",
            "measurement_context": "observed_patient",
        }

    labeled_pattern = re.compile(
        rf"(?i)\b(?P<label>ptt|pt|inr)\s*(?:[-:=]|\s)\s*"
        rf"(?P<value>{SIMPLE_NUMERIC_VALUE_PATTERN})(?!\s*/)\s*(?P<unit>seconds?|secs|sec|s|ratio)?"
        rf"{LAB_INLINE_STATUS_PATTERN}"
    )
    labeled_matches = sort_matches_by_locality(list(labeled_pattern.finditer(context)), start, end)
    for labeled in labeled_matches:
        label = labeled.group("label").lower()
        if (label == "pt" and wants_pt) or (label == "ptt" and wants_ptt) or (label == "inr" and wants_inr):
            value = normalize_numeric_value(labeled.group("value"))
            unit = clean(labeled.group("unit") or "")
            if not unit:
                unit = "ratio" if label == "inr" else "sec"
            return {
                "numeric_value": value,
                "unit": unit,
                "comparator": None,
                "value_text": clean(labeled.group(0)),
                "value_status": infer_value_status_from_match(span, pretty_name, labeled.group(0), context, labeled.start("value"), labeled.end(), value),
                "value_parse_method": "coag_single_label_regex",
                "measurement_context": "observed_patient",
            }

    return None


def parse_slash_panel_value(span: str, pretty_name: str, context: str, start: int | None = None, end: int | None = None) -> dict[str, Any] | None:
    aliases = set(aliases_for_lab(span, pretty_name))
    span_key = norm_key(span)
    pretty_key = norm_key(pretty_name)

    wants_alt = span_key == "alt" or "alanine aminotransferase" in pretty_key or "alt" in aliases
    wants_ast = span_key == "ast" or "aspartate aminotransferase" in pretty_key or "ast" in aliases
    wants_alk_phos = span_key in {"alkphos", "alk phos", "alkaline phosphate", "alkaline phosphatase"} or "alkaline phosphatase" in pretty_key
    wants_bili = span_key in {"t bili", "tbili", "total bilirubin", "bilirubin"} or "bilirubin" in pretty_key

    panels = [
        (
            re.compile(
                rf"(?i)\balt\s*/\s*ast\s*[:=]\s*"
                rf"(?P<alt>{SIMPLE_NUMERIC_VALUE_PATTERN})\s*/\s*"
                rf"(?P<ast>{SIMPLE_NUMERIC_VALUE_PATTERN})"
            ),
            {"alt": ("ALT", wants_alt, None), "ast": ("AST", wants_ast, None)},
        ),
        (
            re.compile(
                rf"(?i)\balk(?:aline)?[-\s]*phos(?:phatase)?\s*/\s*t\s*bili\s*[:=]\s*"
                rf"(?P<alk_phos>{SIMPLE_NUMERIC_VALUE_PATTERN})\s*/\s*"
                rf"(?P<bili>{SIMPLE_NUMERIC_VALUE_PATTERN})"
            ),
            {"alk_phos": ("Alk Phos", wants_alk_phos, None), "bili": ("T Bili", wants_bili, "mg/dL")},
        ),
    ]

    for pattern, fields in panels:
        matches = sort_matches_by_locality(list(pattern.finditer(context)), start, end)
        for panel in matches:
            for field, (label, wanted, unit) in fields.items():
                if not wanted:
                    continue
                value = panel.group(field)
                return {
                    "numeric_value": value,
                    "unit": unit,
                    "comparator": None,
                    "value_text": f"{label}={value}" + (f" {unit}" if unit else ""),
                    "value_status": infer_local_value_status(context, panel.start(), panel.end()),
                    "value_parse_method": "slash_panel_regex",
                    "measurement_context": "observed_patient",
                }
    return None


def infer_measurement_context(
    span: str,
    pretty_name: str,
    context: str,
    value_start: int | None = None,
    value_end: int | None = None,
) -> str:
    aliases = set(aliases_for_lab(span, pretty_name))
    local = context
    if value_start is not None and value_end is not None:
        local = context[max(0, value_start - 60) : min(len(context), value_end + 80)]
    lower = local.lower()
    full_lower = context.lower()
    medication_section = re.search(
        r"\b(?:home medications?|current medications?|medications on transfer|discharge medications?|transfer medications?)\b",
        full_lower,
    )
    if medication_section and re.search(
        r"\b(?:daily|qhs|qam|qd|bid|tid|qid|po|tablet|capsule|dose|twice a day|once a day)\b",
        full_lower,
    ):
        return "medication_dose"
    if aliases & {"rr", "respiratory rate"}:
        if re.search(r"\brr\s*\(\s*set\s*\)", lower):
            return "ventilator_set"
        if re.search(r"\brr\s*\(\s*spontaneous\s*\)", lower):
            return "ventilator_spontaneous"
    if re.search(r"\b(?:goal|target|maintain|keep|avoid)\b", lower):
        return "goal_or_target"
    if re.search(r"\b(?:medications?|current medications?|dextrose|drip|infusion|@\\s*\d+\\s*ml/hr|daily|bid|tid|qid|qhs|qam|po)\\b", lower):
        return "medication_dose"
    return "observed_patient"


def parse_lab_value(span: str, pretty_name: str, context: str, start: int | None = None, end: int | None = None) -> dict[str, Any]:
    aliases = aliases_for_lab(span, pretty_name)
    alias_set = set(aliases)
    local_context, local_start, local_end = local_span_slice(context, start, end)
    coag_value = parse_coag_panel_value(span, pretty_name, local_context, local_start, local_end)
    if coag_value:
        return preserve_numeric_qualifier(coag_value)

    slash_panel_value = parse_slash_panel_value(span, pretty_name, local_context, local_start, local_end)
    if slash_panel_value:
        return preserve_numeric_qualifier(slash_panel_value)

    table_value = maybe_numeric_from_vital_table(span, pretty_name, context, start, end)
    if table_value:
        return preserve_numeric_qualifier(table_value)

    reference_range_value = maybe_numeric_from_reference_range_row(span, pretty_name, context, start, end)
    if reference_range_value:
        return preserve_numeric_qualifier(reference_range_value)

    trend_value = maybe_numeric_from_trend_phrase(span, pretty_name, context, start, end)
    if trend_value:
        return preserve_numeric_qualifier(trend_value)

    approx_value = maybe_numeric_from_approx_phrase(span, pretty_name, context, start, end)
    if approx_value:
        return preserve_numeric_qualifier(approx_value)

    temperature_value = maybe_numeric_from_temperature(span, pretty_name, context, start, end)
    if temperature_value:
        return preserve_numeric_qualifier(temperature_value)

    for alias in aliases:
        escaped = re.escape(alias)
        name_boundary = rf"(?<![A-Za-z0-9]){escaped}(?![A-Za-z0-9])"
        pattern = re.compile(
            rf"(?i){name_boundary}\s*(?:\([^)]{{0,30}}\))?\s*(?:[:=,\-]|\bof\b)?\s*"
            rf"(?:is|was|are|were|estimated|approximately|about|around|~|at|"
            rf"stable\s+at|normal\s+at|high\s+at|low\s+at|elevated\s+to|"
            rf"increased\s+to|decreased\s+to|reduced\s+to|dropped\s+to|"
            rf"current(?:ly)?|remains?|remained|measures?|measured|reported|returned|noted|revealed|showed|shows|found|result(?:ed)?|level(?:s)?(?:\s+of)?|value(?:d)?(?:\s+at|(?:\s+of)?)?)?\s*"
            rf"(?P<comparator><=|>=|<|>)?\s*(?P<value>{NUMERIC_VALUE_PATTERN})\s*"
            rf"(?P<unit>{UNIT_PATTERN})?{LAB_INLINE_STATUS_PATTERN}(?:\s*(?P<trailing_unit>{UNIT_PATTERN}))?"
        )
        matches = [
            match
            for match in sort_matches_by_locality(list(pattern.finditer(local_context)), local_start, local_end)
            if match_anchored_to_span(match, local_start, local_end)
        ]
        for match in matches:
            value = normalize_numeric_value(match.group("value"))
            return preserve_numeric_qualifier({
                "numeric_value": value,
                "unit": clean(match.group("unit") or match.group("trailing_unit") or "") or default_unit_for_lab(alias_set),
                "comparator": match.group("comparator"),
                "value_text": clean(match.group(0)),
                "value_status": infer_value_status_from_match(span, pretty_name, match.group(0), local_context, match.start(), match.end(), value),
                "value_parse_method": "span_followed_by_numeric_regex",
                "measurement_context": infer_measurement_context(span, pretty_name, local_context, match.start(), match.end()),
            })

    fraction_like = re.search(
        rf"(?i)\b(?:lvef|ef|ejection fraction|left ventricular ejection fraction)\b[^\d<>]{{0,40}}"
        rf"\(?\s*(?P<comparator><=|>=|<|>)?\s*(?P<value>{NUMERIC_VALUE_PATTERN})\s*(?P<unit>%)\s*\)?",
        local_context,
    )
    if fraction_like and any(a in {"lvef", "ef", "ejection fraction", "left ventricular ejection fraction"} for a in aliases):
        value = normalize_numeric_value(fraction_like.group("value"))
        return preserve_numeric_qualifier({
            "numeric_value": value,
            "unit": "%",
            "comparator": fraction_like.group("comparator"),
            "value_text": clean(fraction_like.group(0)),
            "value_status": infer_value_status_from_match(span, pretty_name, fraction_like.group(0), local_context, fraction_like.start(), fraction_like.end(), value),
            "value_parse_method": "ejection_fraction_regex",
            "measurement_context": "observed_patient",
        })

    bp_like = re.search(
        rf"(?i)\b(?:bp|blood pressure)\b\s*(?:[:=]|\bis\b|\bwas\b|\bcurrent(?:ly)?\b|\bremains?\b|\bremained\b|\bat\b)?\s*"
        rf"(?P<value>\d{{2,3}}/\d{{2,3}}(?:\(\d{{2,3}}\))?)",
        local_context,
    )
    if bp_like and any(a in {"bp", "blood pressure"} for a in aliases):
        value = normalize_numeric_value(bp_like.group("value"))
        return preserve_numeric_qualifier({
            "numeric_value": value,
            "unit": "mmHg",
            "comparator": None,
            "value_text": clean(bp_like.group(0)),
            "value_status": infer_value_status_from_match(span, pretty_name, bp_like.group(0), local_context, bp_like.start(), bp_like.end(), value),
            "value_parse_method": "blood_pressure_regex",
            "measurement_context": "observed_patient",
        })

    return {
        "numeric_value": None,
        "unit": None,
        "comparator": None,
        "value_text": None,
        "value_status": infer_value_status(context),
        "value_parse_method": "not_parsed",
        "measurement_context": "not_applicable",
    }


def looks_like_header(span: str) -> bool:
    stripped = clean(span)
    return bool(stripped.endswith(":") or (len(stripped) > 3 and stripped.isupper()))


def is_pt_therapy_context(span_key: str, sentence: str, category: str) -> bool:
    if span_key != "pt":
        return False
    text = f"{category} {sentence}".lower()
    return bool(re.search(r"\b(pt|physical therapy|physiotherapy)\s*(eval|evaluation|therapy|ot|sn|rehab|home care|vna)\b", text) or "rehab" in text)


def is_social_target(span_key: str, pretty_key: str, sentence: str) -> bool:
    candidate_text = f"{span_key} {pretty_key}".lower()
    if any(phrase in candidate_text for phrase in ("lives alone", "lives with", "living alone", "household")):
        return True
    if "household composition" in candidate_text and re.search(r"\blives?\s+(?:with|alone)\b", sentence, re.I):
        return True
    if any(phrase in candidate_text for phrase in ("smoker", "smoking", "tobacco", "alcohol", "ethanol", "homeless", "occupation", "drug use")):
        return True
    return False


def is_lab_pt_context(span_key: str, pretty_key: str, sentence: str, section: str) -> bool:
    if span_key not in {"pt", "ptt", "inr"} and pretty_key not in {"prothrombin time", "partial thromboplastin time", "international normalized ratio"}:
        return False
    text = f"{section} {sentence}".lower()
    if re.search(r"\b(pulses?|dp|posterior tibial|dorsalis pedis|carotid|femoral|popliteal)\b", text):
        return False
    if re.search(r"\b(inr|ptt|prothrombin|thromboplastin|coag|hematology|labs?|laboratory)\b", text):
        return True
    if re.search(r"\bpt\s*[:=]?\s*\d", text):
        return True
    return False


def is_coag_context(span_key: str, pretty_key: str, sentence: str, section: str, context: str) -> bool:
    if span_key not in {"pt", "ptt", "inr"} and not any(
        phrase in pretty_key
        for phrase in (
            "prothrombin time",
            "partial thromboplastin time",
            "international normalized ratio",
        )
    ):
        return False
    text = f"{section} {sentence} {context}".lower()
    if re.search(r"\bpt\s*/\s*ptt\s*/\s*inr\s*[:=]?\s*\d", text):
        return True
    if re.search(r"\bpt\s*(?:[-:=]|\s)\s*\d+(?:\.\d+)?\s+ptt\s*(?:[-:=]|\s)\s*\d+(?:\.\d+)?\s+(?:inr(?:\s*\(\s*pt\s*\))?)\s*(?:[-:=]|\s)\s*\d", text):
        return True
    if re.search(r"\bpt\s+(?:of\s+)?\d+(?:\.\d+)?\s*,?\s+ptt\s+(?:of\s+)?\d+(?:\.\d+)?\s*,?\s+(?:and\s+)?(?:an?\s+)?inr\s+(?:of\s+)?\d", text):
        return True
    if re.search(r"\binr\s*\(\s*pt\s*\)\s*[-:=]?\s*\d", text):
        return True
    if re.search(r"\bpt\s*/\s*inr\b", text):
        return True
    if re.search(r"\b(pulses?|posterior tibial|dorsalis pedis|physical therapy|physiotherapy|rehab|home pt)\b", text):
        return False
    if span_key in {"ptt", "inr"} and re.search(rf"\b{span_key}\s*(?:[-:=]|\s)\s*\d+(?:\.\d+)?\b", text):
        return True
    if span_key == "pt" and re.search(r"\b(?:heparin|coumadin|warfarin|coag|coags|anticoag|laborator(?:y|ies)|labs?)\b", text) and re.search(
        r"\bpt\s*(?:[-:=]|\s)\s*\d+(?:\.\d+)?\b",
        text,
    ):
        return True
    if re.search(r"\b(laborator(?:y|ies)|pertinent results|on admission|coags?)\b", text) and re.search(r"\bpt\s*(?:[-:=]|\s)\s*\d", text):
        return True
    if re.search(r"\b(coag|coags|coagulation|hematology|labs?|laboratory|other labs)\b", text) and re.search(r"\b(ptt|inr|prothrombin|thromboplastin)\b", text):
        return True
    return False


def is_ef_context(span_key: str, pretty_key: str, sentence: str, section: str, context: str) -> bool:
    if span_key not in {"ef", "lvef", "ejection fraction", "left ventricular ejection fraction"} and "ejection fraction" not in pretty_key:
        return False
    text = f"{section} {sentence} {context}".lower()
    if re.search(r"\b(?:lvef|ef|ejection fraction)\b[^\n.;]{0,40}(?:%|<|>|=|\d)", text):
        return True
    if re.search(r"\b(?:echo|echocardiogram|tte|tee|cardiomyopathy|heart failure|systolic|ventricular|chf)\b", text):
        return True
    if re.search(r"\b(?:low|normal|reduced|depressed|preserved)\s+(?:lvef|ef)\b", text):
        return True
    return False


def is_lab_monitoring_without_concrete_value(span_key: str, pretty_key: str, sentence: str, section: str) -> bool:
    if span_key not in {"pt", "ptt", "inr", "hr", "rr", "bp", "wbc", "hgb", "hct"} and not any(
        phrase in pretty_key
        for phrase in (
            "prothrombin time",
            "partial thromboplastin",
            "international normalized ratio",
            "heart rate",
            "respiratory rate",
            "blood pressure",
            "white blood cell",
            "hemoglobin",
            "hematocrit",
        )
    ):
        return False
    text = f"{section} {sentence}".lower()
    if re.search(r"\b(checks?|checking|checked|monitor(?:ing)?|trend(?:ing)?|follow(?:ing)?|repeat(?:ed)?|measure(?:d|ment)?|draw(?:n)?|ordered)\b", text):
        return True
    if re.search(r"\b(?:daily|q\s*\d+\s*h|q\d+h|every\s+\d+\s+hours?)\b.{0,80}\b(checks?|monitor|labs?|pt/inr|ptt|inr)\b", text):
        return True
    return False


def is_medication_dose_not_lab_value(
    span_key: str,
    pretty_key: str,
    section: str,
    context: str,
    value_parse: dict[str, Any],
) -> bool:
    if value_parse.get("numeric_value") is None:
        return False
    if value_parse.get("measurement_context") == "medication_dose":
        return True
    if (span_key == "dextrose" or "glucose" in pretty_key) and re.search(
        r"\bdextrose\b\s*\d+(?:\.\d+)?\s*%|d\d+w|d5w|d10w|@\s*\d+\s*ml/hr|drip|infusion",
        f"{value_parse.get('value_text') or ''} {context}".lower(),
    ):
        return True
    analyte_like = {
        "calcium",
        "magnesium",
        "potassium",
        "sodium",
        "phosphorus",
        "phosphate",
        "iron",
        "albumin",
    }
    if span_key not in analyte_like and not any(term in pretty_key for term in analyte_like):
        return False
    value_text = str(value_parse.get("value_text") or "")
    text = f"{section} {value_text} {context}".lower()
    if re.search(r"\b(?:home medications?|current medications?|medications on transfer|discharge medications?|transfer medications?|medications?)\b", text):
        if re.search(r"\b(?:daily|qhs|qam|qd|bid|tid|qid|po|tablet|capsule|dose|twice a day|once a day|sr|xr)\b", text):
            if not re.search(r"\b(?:mg/dl|mmol/l|meq/l|g/dl|k/u?l|iu/l|u/l)\b", value_text.lower()):
                return True
    if re.search(rf"\b{re.escape(span_key)}\b\s*\d+(?:\.\d+)?(?:\s*\+\s*[a-z])?\s*(?:mg|mcg|g|units?)?\s*(?:po\s*)?(?:daily|qhs|qam|qd|bid|tid|qid|q\s*\d+h|prn|twice a day|once a day)\b", text):
        return True
    if re.search(r"\b(?:medications?|home meds?|on admission|discharge medications?)\b", text) and re.search(
        r"\b(?:daily|qhs|qam|qd|bid|tid|qid|po|tablet|capsule|dose|twice a day|once a day)\b", text
    ):
        if not re.search(r"\b(?:mg/dl|mmol/l|meq/l|g/dl|k/u?l)\b", value_text.lower()):
            return True
    return False


def is_non_observed_value_context(
    span_key: str,
    pretty_key: str,
    sentence: str,
    section: str,
    context: str,
    value_parse: dict[str, Any],
) -> bool:
    if value_parse.get("numeric_value") is None:
        return False
    text = f"{section} {sentence} {context}".lower()
    value_text = str(value_parse.get("value_text") or "").lower()
    target_terms = [span_key]
    target_terms.extend(aliases_for_lab(span_key, pretty_key))
    target_terms = sorted({term for term in target_terms if term}, key=len, reverse=True)
    target = "|".join(re.escape(term) for term in target_terms[:8])
    if not target:
        return False
    if re.search(rf"\b(?:goal|target|maintain|keep|avoid)\b[^.;\n]{{0,80}}\b(?:{target})\b", text):
        return True
    if re.search(rf"\b(?:{target})\b[^.;\n]{{0,80}}\b(?:goal|target|threshold|maintain|keep|avoid)\b", text):
        return True
    if re.search(rf"\b(?:risk|decision|vitamin\s+k|monitoring|follow\s+up)\b[^.;\n]{{0,90}}\b(?:{target})\b\s*(?:<=|>=|<|>)", text):
        return True
    if re.search(r"\b(?:goal|target)\b", value_text):
        return True
    return False


def is_blood_pressure_range_not_single_measurement(span_key: str, pretty_key: str, value_parse: dict[str, Any]) -> bool:
    if span_key not in {"bp", "blood pressure"} and "blood pressure" not in pretty_key:
        return False
    value = str(value_parse.get("numeric_value") or "")
    value_text = str(value_parse.get("value_text") or "")
    return bool("-" in value and "/" in value and re.search(r"\d{2,3}\s*-\s*\d{2,3}\s*/\s*\d{2,3}", value_text))


def is_family_history_context(sentence: str, section: str) -> bool:
    text = f"{section} {sentence}".lower()
    if re.search(r"\bsocial\s+and\s+family\s+history\b", text) and not re.search(
        r"\b(mother|father|sister|brother|daughter|son|parent|maternal|paternal|aunt|uncle)\b.{0,80}\b(history|hx|had|has|died|deceased|diagnosed|cancer|diabetes|cad|mi|stroke)\b",
        text,
    ):
        return False
    if re.search(r"\b(family history|fhx|family hx)\b", text):
        return True
    if re.search(r"\b(mother|father|sister|brother|daughter|son|parent|maternal|paternal|aunt|uncle)\b.{0,80}\b(history|hx|had|has|died|deceased|diagnosed|cancer|diabetes|cad|mi|stroke)\b", text):
        return True
    return False


def is_family_role_or_context_target(span_key: str, pretty_key: str) -> bool:
    text = f"{span_key} {pretty_key}".lower()
    return any(term in text for term in FAMILY_ROLE_TERMS)


def is_echo_anatomy_attribute(span_key: str, pretty_key: str, sentence: str) -> bool:
    text = f"{span_key} {pretty_key} {sentence}".lower()
    if span_key in {"ra", "la", "lv", "rv"} and re.search(r"\b(size|enlargement|dilat(?:ed|ion)|chamber|wall|thickness|atrium|ventricle)\b", text):
        return True
    if re.search(r"\b(?:left|right)?\s*(?:atri(?:um|al)|ventric(?:le|ular)|valve|appendage|aorta|artery|vein|carotid|conjunctiva|knee|extremity|limb|arterial)\b", text):
        return True
    if re.search(r"\b(?:normal|mild|mildly|moderate|severe|trivial|trace)\b.{0,35}\b(?:size|diameter|wall|leaflets?|chamber|atrium|ventricle|valve|aorta)\b", text):
        return True
    return False


def is_generic_or_artifact_target(span_key: str, pretty_key: str, sentence: str, section: str, category: str) -> str | None:
    text = f"{span_key} {pretty_key} {sentence} {section} {category}".lower()
    span_only = span_key
    if "[**date range" in text or span_only in {"range", "date range"}:
        return "deid_date_range_artifact"
    if span_only in {
        "affect",
        "arterial",
        "blood products",
        "cardiac",
        "fluid balance",
        "issues",
        "physical examination",
        "pulmonary",
        "range",
        "reporting",
        "respiratory support",
        "resting",
        "travels",
        "vascular",
    }:
        return "generic_or_header_span"
    if span_only in {"ue"}:
        return "ambiguous_rehab_abbreviation"
    if span_only == "bs" and re.search(r"\b(?:equal|breath|pulm|ctab)\b", text):
        return "bs_breath_sounds_not_bowel_sounds"
    if span_only == "bites" and re.search(r"\b(?:applesauce|swallow|liquids?|po trials?|bites down|does not suck|oral mucosa)\b", text):
        return "food_bites_not_wound"
    if span_only == "safe travels":
        return "education_brochure_artifact"
    if span_only in {"feeling", "talking"}:
        return "generic_behavior_or_wellness_phrase"
    if span_only == "fluid repletion":
        return "treatment_phrase_not_diagnosis"
    if span_only == "carotid" and re.search(r"\b(?:bruits?|pulses?)\b", text):
        return "anatomy_exam_location"
    if span_only in {"right upper extremity", "left upper extremity", "conjunctiva"}:
        return "anatomy_exam_location"
    if span_only == "heart rhythm":
        return "heading_prefer_specific_rhythm"
    if span_only == "respiratory support" and re.search(r"\b(?:o2 delivery|ventilator mode|peep|fio2|rr \\(set\\))\b", text):
        return "flowsheet_heading"
    if span_only == "fluid balance" and re.search(r"\b(?:24 hours|total in|total out|since 12 am)\b", text):
        return "flowsheet_heading"
    if span_only == "physical examination" and re.search(r"\b(?:vital signs|temperature|exam)\b", text):
        return "section_header_span"
    if span_only in {"pulmonary"} and re.search(r"\b(?:pulmonary toilet|pulmonary mac)\b", text):
        return "adjective_fragment"
    if span_only == "reporting" and re.search(r"\bpain\b", text):
        return "reporting_verb_not_target"
    if span_only == "cardiac" and re.search(r"\brisk factors?\b", text):
        return "adjective_fragment"
    return None


def apply_concept_overrides(
    concept_id: str,
    pretty_name: str,
    type_ids: list[str],
    span: str,
    section: str,
    sentence: str,
    context: str,
) -> tuple[str, str, list[str], list[str]]:
    reasons: list[str] = []
    span_key = norm_key(span)
    pretty_key = norm_key(pretty_name)
    if is_ef_context(span_key, pretty_key, sentence, section, context) and pretty_key != "left ventricular ejection fraction":
        reasons.append(EF_OVERRIDE_CONCEPT["reason"])
        return (
            EF_OVERRIDE_CONCEPT["concept_id"],
            EF_OVERRIDE_CONCEPT["pretty_name"],
            list(EF_OVERRIDE_CONCEPT["type_ids"]),
            reasons,
        )
    if is_coag_context(span_key, pretty_key, sentence, section, context):
        if span_key == "ptt":
            concept = COAG_OVERRIDE_CONCEPTS["ptt"]
        elif span_key == "inr":
            concept = COAG_OVERRIDE_CONCEPTS["inr"]
        elif span_key == "pt":
            concept = COAG_OVERRIDE_CONCEPTS["pt"]
        elif span_key in COAG_OVERRIDE_CONCEPTS:
            concept = COAG_OVERRIDE_CONCEPTS[span_key]
        elif "partial thromboplastin" in pretty_key:
            concept = COAG_OVERRIDE_CONCEPTS["ptt"]
        elif "international normalized ratio" in pretty_key:
            concept = COAG_OVERRIDE_CONCEPTS["inr"]
        else:
            concept = COAG_OVERRIDE_CONCEPTS["pt"]
        if concept_id != concept["concept_id"] or pretty_name != concept["pretty_name"]:
            reasons.append(concept["reason"])
            return (
                concept["concept_id"],
                concept["pretty_name"],
                list(concept["type_ids"]),
                reasons,
            )
    return concept_id, pretty_name, type_ids, reasons


def is_ecg_target(span_key: str, pretty_key: str) -> bool:
    text = f"{span_key} {pretty_key}".lower()
    return any(
        phrase in text
        for phrase in (
            "sinus rhythm",
            "atrial fibrillation",
            "t wave",
            "st ",
            "myocardial infarction",
            "bradycardia",
            "tachycardia",
            "heart block",
            "left bundle",
            "right bundle",
            "q wave",
            "r wave",
            "atrial abnormality",
        )
    )


def is_echo_specific_target(span_key: str, pretty_key: str) -> bool:
    text = f"{span_key} {pretty_key}".lower()
    return any(
        phrase in text
        for phrase in (
            "pericardial effusion",
            "stenosis",
            "regurgitation",
            "vegetation",
            "thrombus",
            "ejection fraction",
            "lvef",
            "systolic dysfunction",
            "diastolic dysfunction",
        )
    )


def filter_flags(
    span: str,
    pretty_name: str,
    sentence: str,
    section: str,
    context: str,
    category: str,
    type_ids: list[str],
    acc: float,
    policy: dict[str, Any],
) -> set[str]:
    flags: set[str] = set()
    span_key = norm_key(span)
    pretty_key = norm_key(pretty_name)
    upper = clean(span).upper()
    text = f"{span} {pretty_name} {sentence} {section}"
    type_set = set(type_ids)

    runtime = policy.get("_runtime") or {}
    reject_spans = runtime.get("reject_exact_spans") or set(policy.get("reject_exact_spans", []))
    context_spans = runtime.get("context_only_spans") or set(policy.get("context_only_spans", []))
    unit_spans = runtime.get("unit_or_route_spans") or set(policy.get("unit_or_route_spans", []))
    lab_spans = runtime.get("lab_or_vital_spans") or set(policy.get("lab_or_vital_spans", []))
    high_risk = runtime.get("high_risk_abbreviations") or set(policy.get("high_risk_abbreviations", []))
    context_type_ids = runtime.get("context_or_noise_type_ids") or set(policy.get("context_or_noise_type_ids", {}))
    priority_type_ids = runtime.get("clinical_priority_type_ids") or set(policy.get("clinical_priority_type_ids", {}))

    if not clean(span):
        flags.add("empty_span")
    if span_key in reject_spans:
        flags.add("reject_exact_span")
    if span_key in context_spans:
        flags.add("context_only_span")
    if span_key in unit_spans:
        flags.add("unit_or_route_span")
    generic_reason = is_generic_or_artifact_target(span_key, pretty_key, sentence, section, category)
    if generic_reason:
        flags.add(generic_reason)
        flags.add("generic_or_artifact_target")
    if span_key in lab_spans or pretty_key in lab_spans or lab_like_candidate(span, pretty_name, sentence, section, context):
        flags.add("lab_or_vital_span")
    if upper in high_risk or (clean(span).isupper() and 2 <= len(clean(span)) <= 5):
        flags.add("abbreviation_needs_context")
    if "[**" in context:
        flags.add("deid_context")
    if NUMERIC_CONTEXT.search(context):
        flags.add("numeric_context")
    if FAMILY_CUE.search(text):
        flags.add("family_context")
    if is_family_history_context(sentence, section):
        flags.add("family_history_context")
    if "family_context" in flags and not ("family_history_context" in flags or is_family_role_or_context_target(span_key, pretty_key)):
        flags.add("family_context_bystander")
    if SOCIAL_CUE.search(text):
        flags.add("social_context")
    if NEGATION_CUE.search(sentence):
        flags.add("negation_cue_context")
    if POSSIBLE_CUE.search(sentence):
        flags.add("possible_cue_context")
    if CONDITIONAL_CUE.search(sentence):
        flags.add("conditional_cue_context")
    if HYPOTHETICAL_CUE.search(sentence):
        flags.add("hypothetical_cue_context")
    if HISTORICAL_CUE.search(f"{section} {sentence}"):
        flags.add("historical_cue_context")
    if acc < 0.50:
        flags.add("low_acc")
    elif acc < 0.80:
        flags.add("mid_low_acc")
    elif acc < 0.90:
        flags.add("review_score_acc")
    if looks_like_header(span):
        flags.add("header_like_span")
    if len(clean(span)) <= 2:
        flags.add("short_span")
    if type_set & context_type_ids:
        flags.add("context_or_noise_type_id")
    if type_set & priority_type_ids:
        flags.add("clinical_priority_type_id")
    lowered_section = norm_key(section)
    admin_sections = runtime.get("admin_sections")
    if admin_sections is None:
        admin_sections = {norm_key(s) for s in policy.get("section_policy", {}).get("admin_sections", [])}
    if lowered_section in admin_sections:
        flags.add("admin_section")
    if is_pt_therapy_context(span_key, sentence, category):
        flags.add("pt_therapy_context")
    if is_coag_context(span_key, pretty_key, sentence, section, context):
        flags.add("coag_lab_context")
    if category.lower().strip() == "echo" and is_echo_anatomy_attribute(span_key, pretty_key, sentence):
        flags.add("echo_anatomy_attribute")
    if is_social_target(span_key, pretty_key, sentence):
        flags.add("social_target_context")
    return flags


def classify_action(
    span: str,
    pretty_name: str,
    category: str,
    section: str,
    sentence: str,
    context: str,
    type_ids: list[str],
    acc: float,
    flags: set[str],
    value_parse: dict[str, Any],
    policy: dict[str, Any],
) -> tuple[str, list[str]]:
    reasons: list[str] = []
    span_key = norm_key(span)
    pretty_key = norm_key(pretty_name)
    category_key = norm_key(category)
    type_set = set(type_ids)
    runtime = policy.get("_runtime") or {}
    context_type_ids = runtime.get("context_or_noise_type_ids") or set(policy.get("context_or_noise_type_ids", {}))

    def decide(action: str, reason: str) -> tuple[str, list[str]]:
        reasons.append(reason)
        return action, reasons

    if "empty_span" in flags:
        return decide("reject_candidate", "empty_span")
    if "reject_exact_span" in flags:
        return decide("reject_candidate", "policy_reject_exact_span")
    if "admin_section" in flags:
        return decide("reject_candidate", "administrative_section")
    if "unit_or_route_span" in flags:
        return decide("context_only", "standalone_unit_or_route")
    if "generic_or_artifact_target" in flags:
        if flags & {"deid_date_range_artifact", "education_brochure_artifact", "section_header_span"}:
            return decide("reject_candidate", "generic_or_artifact_target")
        return decide("context_only", "generic_or_artifact_target")
    if "header_like_span" in flags and "clinical_priority_type_id" not in flags:
        return decide("reject_candidate", "header_like_nonclinical_span")

    if span_key == "pt" and "pt_therapy_context" in flags:
        return decide("context_only", "pt_means_physical_therapy_context")
    if span_key in {"iv", "po", "ng", "prn", "tf", "tpn"}:
        return decide("context_only", "route_or_feeding_context")

    if "lab_or_vital_span" in flags:
        if span_key == "pt" and not is_lab_pt_context(span_key, pretty_key, sentence, section):
            return decide("context_only", "pt_without_lab_context")
        if is_medication_dose_not_lab_value(span_key, pretty_key, section, context, value_parse):
            return decide("context_only", "medication_dose_not_lab_value")
        if is_non_observed_value_context(span_key, pretty_key, sentence, section, context, value_parse):
            return decide("review_candidate", "lab_or_vital_target_or_threshold_not_observed")
        if is_blood_pressure_range_not_single_measurement(span_key, pretty_key, value_parse):
            return decide("review_candidate", "blood_pressure_range_not_single_measurement")
        if value_parse.get("numeric_value") is None:
            if is_lab_monitoring_without_concrete_value(span_key, pretty_key, sentence, section):
                return decide("context_only", "lab_or_vital_monitoring_without_concrete_value")
            reasons.append("lab_or_vital_value_not_parsed")
            return "review_candidate", sorted(set(reasons))
        else:
            reasons.append("lab_or_vital_value_parsed")
        return "value_parse", reasons

    if "context_only_span" in flags:
        return decide("context_only", "policy_context_only_span")
    if "31601201" in type_set:
        return decide("context_only", "person_role_type_context_only")
    if type_set and type_set.issubset(context_type_ids):
        return decide("context_only", "context_or_noise_type_only")

    if "low_acc" in flags:
        return decide("reject_candidate", "low_linker_score")
    if "mid_low_acc" in flags:
        reasons.append("mid_low_linker_score")
        action = "review_candidate"
    elif "review_score_acc" in flags:
        reasons.append("review_score_linker_score")
        action = "review_candidate"
    else:
        action = default_action_for_score(acc)
        reasons.append(f"default_score_policy_{score_bin(acc)}")

    if "abbreviation_needs_context" in flags:
        known_safe_abbrev = False
        if span_key in {"peg", "icd"} and any(term in pretty_key for term in ("gastrostomy", "implant", "defibrillator")):
            known_safe_abbrev = True
        if span_key in {"cad", "chf", "copd"} and acc >= 0.90:
            known_safe_abbrev = True
        if span_key == "mi" and "myocardial infarction" in pretty_key and acc >= 0.90:
            known_safe_abbrev = True
        if not known_safe_abbrev:
            action = "review_candidate" if acc >= 0.80 else "reject_candidate"
            reasons.append("high_risk_abbreviation_requires_context")

    if category_key in {"case management", "social work"}:
        if "social_target_context" in flags:
            action = "keep_candidate" if acc >= 0.90 else "review_candidate"
            reasons.append("case_social_category_explicit_social_target")
        elif "family_history_context" in flags and "clinical_priority_type_id" in flags:
            action = "review_candidate"
            reasons.append("family_history_context_clinical_candidate")
        elif action == "keep_candidate":
            action = "review_candidate"
            reasons.append("case_social_category_downshift")

    if category_key == "ecg":
        if is_ecg_target(span_key, pretty_key):
            action = "keep_candidate" if acc >= 0.80 else "review_candidate"
            reasons.append("ecg_specific_target")
        elif action == "keep_candidate":
            action = "review_candidate"
            reasons.append("ecg_category_downshift_non_specific")

    if category_key == "echo":
        if span_key in {"normal", "mild", "mildly", "size", "wall", "function", "ventricular", "aortic valve", "left", "right"}:
            return decide("context_only", "echo_modifier_or_anatomy_attribute")
        if "echo_anatomy_attribute" in flags and not is_echo_specific_target(span_key, pretty_key):
            return decide("context_only", "echo_modifier_or_anatomy_attribute")
        if span_key in {"ef", "lvef", "ejection fraction"} or "ejection fraction" in pretty_key:
            if value_parse.get("numeric_value") is None:
                return decide("review_candidate", "echo_fraction_without_parsed_value")
            return decide("value_parse", "echo_fraction_requires_value_parse")
        if is_echo_specific_target(span_key, pretty_key):
            action = "keep_candidate" if acc >= 0.80 else "review_candidate"
            reasons.append("echo_specific_target")
        elif action == "keep_candidate":
            action = "review_candidate"
            reasons.append("echo_category_downshift")

    if category_key == "radiology":
        if span_key in {"reason", "reason for", "final report", "examination", "study", "year", "clip", "contrast"}:
            return decide("reject_candidate", "radiology_report_header_or_artifact")
        if "37552161" in type_set and action == "keep_candidate":
            action = "review_candidate"
            reasons.append("radiology_anatomy_downshift")

    if category_key == "pharmacy":
        if span_key in {"dose", "numeric", "identifier", "mcg/ml", "mg/hr", "recommendation", "pharmacy"}:
            return decide("context_only", "pharmacy_admin_or_dose_context")
        if "91187746" in type_set and span_key not in {"medication", "medications"} and acc >= 0.80:
            action = "keep_candidate"
            reasons.append("pharmacy_medication_or_substance")

    return action, sorted(set(reasons))


def chmod_private(path: Path) -> None:
    try:
        path.chmod(0o600)
    except FileNotFoundError:
        pass


def add_reservoir(records: list[dict[str, Any]], record: dict[str, Any], seen: int, limit: int, rng: random.Random) -> None:
    if limit <= 0:
        return
    if len(records) < limit:
        records.append(record)
        return
    replacement_index = rng.randrange(seen)
    if replacement_index < limit:
        records[replacement_index] = record


def strict_pre_umls_decision(record: dict[str, Any], min_keep_score: float) -> tuple[bool, list[str]]:
    """Apply the final strict gate before real UMLS annotation is available.

    The final gate requires a single UMLS CUI. During MedCAT extraction we only
    have the linker concept ID, so this function injects a temporary single-CUI
    placeholder and reuses the same strict logic for all non-UMLS-dependent
    checks. Real CUI mapping is still enforced later by the crosswalk step.
    """

    try:
        from build_strict_sft_dataset import strict_decision
    except Exception as exc:  # pragma: no cover - defensive for unusual envs
        return False, [f"strict_pre_umls_import_failed:{exc}"]

    candidate = dict(record)
    if UMLS_CUI.match(str(candidate.get("concept_id") or "")):
        candidate["umls_cui"] = candidate.get("concept_id")
        candidate["umls_mapping_status"] = "already_umls_cui"
    else:
        candidate["umls_cui"] = "C0000000"
        candidate["umls_mapping_status"] = "snomed_single_cui"
    candidate["umls_mapping_needs_review"] = False
    ok, reasons = strict_decision(candidate, min_keep_score)
    return ok, reasons


def iter_note_entity_results(
    cat,
    selected_notes: list[dict[str, str]],
    max_note_chars: int,
    n_process: int,
    batch_size: int,
    batch_size_chars: int,
    use_multi_texts: bool = False,
):
    """Yield (row, snippet, result) while preserving the old single-process path by default."""

    if n_process <= 1 and not use_multi_texts:
        for row in selected_notes:
            snippet = row["TEXT"][:max_note_chars]
            yield row, snippet, cat.get_entities(snippet)
        return

    indexed_rows: dict[str, tuple[dict[str, str], str]] = {}

    def text_iter() -> Iterable[tuple[str, str]]:
        for idx, row in enumerate(selected_notes):
            snippet = row["TEXT"][:max_note_chars]
            key = str(idx)
            indexed_rows[key] = (row, snippet)
            yield key, snippet

    for key, result in cat.get_entities_multi_texts(
        text_iter(),
        only_cui=False,
        n_process=n_process,
        batch_size=batch_size,
        batch_size_chars=batch_size_chars,
    ):
        row, snippet = indexed_rows.pop(str(key))
        yield row, snippet, result


def main() -> int:
    args = parse_args()
    if not os.environ.get("SLURM_JOB_ID") and not args.allow_no_slurm:
        print("ERROR: This script must run inside a SLURM job for MIMIC/MedCAT processing.", file=sys.stderr)
        return 2

    zip_path = Path(args.zip) if args.zip else None
    notes_path = Path(args.notes_jsonl_gz) if args.notes_jsonl_gz else None
    modelpack_path = Path(args.modelpack)
    policy_path = Path(args.filter_policy)
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_dir.chmod(0o700)

    if (zip_path is None) == (notes_path is None):
        print("ERROR: provide exactly one of --zip or --notes-jsonl-gz", file=sys.stderr)
        return 2

    required_paths = [modelpack_path, policy_path]
    required_paths.append(notes_path if notes_path is not None else zip_path)
    for required in required_paths:
        if not required.exists():
            print(f"ERROR missing input: {required}", file=sys.stderr)
            return 2

    policy = prepare_policy_runtime(load_policy(policy_path))
    rng = random.Random(args.seed)

    if args.medcat_n_process < 1:
        print("ERROR: --medcat-n-process must be >= 1", file=sys.stderr)
        return 2

    gpu_setup_status = "not_requested"
    gpu_setup_result: bool | None = None
    if args.medcat_require_gpu or args.medcat_prefer_gpu:
        try:
            import spacy

            if args.medcat_require_gpu:
                gpu_setup_result = bool(spacy.require_gpu())
                gpu_setup_status = "require_gpu_succeeded"
            else:
                gpu_setup_result = bool(spacy.prefer_gpu())
                gpu_setup_status = "prefer_gpu_succeeded" if gpu_setup_result else "prefer_gpu_returned_false"
        except Exception as exc:
            print(f"ERROR: GPU request failed before MedCAT load: {exc}", file=sys.stderr)
            return 2

    from medcat.cat import CAT

    if args.selection_mode == "sample":
        if notes_path is not None:
            selected_notes, category_population, rows_read, nonempty_rows = sample_notes_from_shard(
                notes_path,
                per_category=args.max_notes_per_category,
                max_total=args.max_total_notes,
                seed=args.seed,
            )
        else:
            assert zip_path is not None
            selected_notes, category_population, rows_read, nonempty_rows = sample_notes(
                zip_path,
                per_category=args.max_notes_per_category,
                max_total=args.max_total_notes,
                seed=args.seed,
            )
        selection_stats = {
            "strategy": "per-category reservoir sample over NOTEEVENTS",
            "seed": args.seed,
            "source_kind": "notes_jsonl_gz" if notes_path is not None else "mimic_zip",
            "source_path": str(notes_path if notes_path is not None else zip_path),
            "rows_read": rows_read,
            "nonempty_note_rows": nonempty_rows,
            "max_total_notes": args.max_total_notes,
            "max_notes_per_category": args.max_notes_per_category,
            "max_note_chars": args.max_note_chars,
            "min_acc": args.min_acc,
        }
    else:
        if notes_path is not None:
            selected_notes, category_population, rows_read, nonempty_rows = range_notes_from_shard(
                notes_path,
                start_note_index=args.start_note_index,
                end_note_index=args.end_note_index,
                max_total=args.max_total_notes,
            )
        else:
            assert zip_path is not None
            selected_notes, category_population, rows_read, nonempty_rows = range_notes(
                zip_path,
                start_note_index=args.start_note_index,
                end_note_index=args.end_note_index,
                max_total=args.max_total_notes,
            )
        selection_stats = {
            "strategy": "contiguous nonempty-note index range over NOTEEVENTS",
            "source_kind": "notes_jsonl_gz" if notes_path is not None else "mimic_zip",
            "source_path": str(notes_path if notes_path is not None else zip_path),
            "start_note_index": args.start_note_index,
            "end_note_index": args.end_note_index,
            "rows_read": rows_read,
            "nonempty_note_rows_seen": nonempty_rows,
            "max_total_notes": args.max_total_notes,
            "max_note_chars": args.max_note_chars,
            "min_acc": args.min_acc,
        }
    note_text_filter_stats = apply_note_text_filters(
        selected_notes,
        args.note_section_keep_regex,
        args.note_section_drop_regex,
        args.note_section_include_unknown,
    )
    cat = CAT.load_model_pack(str(modelpack_path))

    candidates_path = out_dir / ("medcat_candidates.jsonl.gz" if args.compress_output else "medcat_candidates.jsonl")
    rejected_path = out_dir / "medcat_rejected_sample.jsonl"
    stats_path = out_dir / "medcat_candidate_stats.json"

    action_counts = Counter()
    category_counts = Counter()
    selected_category_counts = Counter(row["CATEGORY"] for row in selected_notes)
    flag_counts = Counter()
    reason_counts = Counter()
    score_counts = Counter()
    type_counts = Counter()
    category_action_counts: dict[str, Counter] = defaultdict(Counter)
    value_parse_counts = Counter()
    concept_category_counts = Counter()
    anatomy_cue_counts = Counter()
    modifier_cue_counts = Counter()
    trend_cue_counts = Counter()
    assertion_post_judge_counts = Counter()
    output_decision_counts = Counter()
    pre_umls_reject_reason_counts = Counter()

    notes_processed = 0
    notes_with_entities = 0
    entities_seen = 0
    candidate_records_written = 0
    rejected_seen = 0
    rejected_records: list[dict[str, Any]] = []

    with open_output_text(candidates_path, args.gzip_compresslevel) as cand_fh:
        for row, snippet, result in iter_note_entity_results(
            cat,
            selected_notes,
            args.max_note_chars,
            args.medcat_n_process,
            args.medcat_batch_size,
            args.medcat_batch_size_chars,
        ):
            notes_processed += 1
            if args.progress_every > 0 and notes_processed % args.progress_every == 0:
                print(f"[progress] processed_notes={notes_processed} entities_seen={entities_seen}", flush=True)

            category = row["CATEGORY"]
            ents = result.get("entities", result) if isinstance(result, dict) else {}
            iterable = enumerate(ents) if isinstance(ents, list) else ents.items()
            note_entity_count = 0

            for _, ent in iterable:
                if not isinstance(ent, dict):
                    continue
                acc = float(entity_field(ent, "acc", "context_similarity", default=1.0) or 0.0)
                if acc < args.min_acc:
                    continue
                start = int(entity_field(ent, "start", default=0) or 0)
                end = int(entity_field(ent, "end", default=start) or start)
                if start < 0 or end < start or start >= len(snippet):
                    continue
                raw_id = str(entity_field(ent, "cui", "id", default=""))
                pretty_name = str(entity_field(ent, "pretty_name", "source_value", "detected_name", default=""))
                raw_source_value = str(entity_field(ent, "source_value", "detected_name", default=snippet[start:end]))
                source_value = raw_source_value
                source_value, start, end, normalized_lab_alias = normalize_lab_value_span(source_value, start, end)
                types = entity_field(ent, "type_ids", "types", default=[]) or []
                if isinstance(types, str):
                    types = [types]
                type_ids = sorted(map(str, types))
                type_key = "|".join(type_ids) if type_ids else "unknown"
                sentence = sentence_for_span(snippet, start, end, args.sentence_chars)
                evidence_context_raw, context_span_start, context_span_end = context_with_marker(snippet, start, end)
                evidence_context, context_span_start, context_span_end = clean_context_with_span(
                    evidence_context_raw,
                    context_span_start,
                    context_span_end,
                )
                section = section_for_span(snippet, start)
                raw_id, pretty_name, type_ids, override_reasons = apply_concept_overrides(
                    raw_id,
                    pretty_name,
                    type_ids,
                    source_value,
                    section,
                    sentence,
                    evidence_context,
                )
                type_key = "|".join(type_ids) if type_ids else "unknown"

                flags = filter_flags(
                    source_value,
                    pretty_name,
                    sentence,
                    section,
                    evidence_context,
                    category,
                    type_ids,
                    acc,
                    policy,
                )
                if override_reasons:
                    flags.add("concept_override")
                if normalized_lab_alias:
                    flags.add("lab_value_span_normalized")
                value_parse = parse_lab_value(
                    source_value,
                    pretty_name,
                    evidence_context,
                    context_span_start,
                    context_span_end,
                ) if "lab_or_vital_span" in flags else {
                    "numeric_value": None,
                    "unit": None,
                    "comparator": None,
                    "value_text": None,
                    "value_status": "not_applicable",
                    "value_parse_method": "not_applicable",
                    "measurement_context": "not_applicable",
                }
                action, reasons = classify_action(
                    source_value,
                    pretty_name,
                    category,
                    section,
                    sentence,
                    evidence_context,
                    type_ids,
                    acc,
                    flags,
                    value_parse,
                    policy,
                )
                if action == "value_parse" and value_parse.get("numeric_value") is None:
                    value_parse["value_status"] = "unknown"
                if action != "value_parse":
                    value_parse = {
                        "numeric_value": None,
                        "unit": None,
                        "comparator": None,
                        "value_text": None,
                        "value_status": "not_applicable",
                        "value_parse_method": "not_applicable",
                        "measurement_context": "not_applicable",
                    }
                if override_reasons:
                    reasons = sorted(set([*reasons, *override_reasons]))

                concept_category = infer_concept_category(source_value, pretty_name, type_ids, flags)
                assertion_preview_raw = infer_assertion_preview(
                    sentence,
                    section,
                    action,
                    source_value,
                    evidence_context,
                    concept_category,
                )
                assertion_preview, assertion_post_judge_applied, assertion_post_judge_reasons = refine_assertion_preview(
                    sentence,
                    section,
                    action,
                    source_value,
                    evidence_context,
                    concept_category,
                    assertion_preview_raw,
                )
                assertion_evaluable, assertion_evaluable_reason = assertion_evaluable_preview(
                    source_value,
                    concept_category,
                )
                temporality_preview = infer_temporality_preview(sentence, section, source_value)
                experiencer_preview = infer_experiencer_preview(sentence, section, flags)
                anatomy_cue = infer_context_cue(evidence_context, ANATOMY_CUE_PATTERNS)
                modifier_cue = infer_context_cue(evidence_context, MODIFIER_CUE_PATTERNS)
                trend_cue = infer_context_cue(evidence_context, TREND_CUE_PATTERNS)

                record = {
                    "note_row_id": row["ROW_ID"],
                    "subject_id": row["SUBJECT_ID"],
                    "hadm_id": row["HADM_ID"],
                    "chartdate": row["CHARTDATE"],
                    "category": category,
                    "section": section,
                    "span_start": start,
                    "span_end": end,
                    "span": clean(source_value),
                    "concept_id": raw_id,
                    "concept_id_system": concept_id_system(raw_id),
                    "pretty_name": pretty_name,
                    "type_ids": type_ids,
                    "raw_linker_concept_id": str(entity_field(ent, "cui", "id", default="")),
                    "raw_linker_pretty_name": str(entity_field(ent, "pretty_name", "source_value", "detected_name", default="")),
                    "raw_linker_span": clean(raw_source_value),
                    "concept_override_applied": bool(override_reasons),
                    "span_normalization_applied": bool(normalized_lab_alias),
                    "linker_name": "medcat_umls_snomed_v2",
                    "linker_score": acc,
                    "score_bin": score_bin(acc),
                    "filter_action": action,
                    "reason_codes": reasons,
                    "filter_flags": sorted(flags),
                    "concept_category_preview": concept_category,
                    "assertion_status_preview_raw": assertion_preview_raw,
                    "assertion_status_preview": assertion_preview,
                    "assertion_post_judge_applied": assertion_post_judge_applied,
                    "assertion_post_judge_reasons": assertion_post_judge_reasons,
                    "assertion_evaluable_preview": assertion_evaluable,
                    "assertion_evaluable_reason": assertion_evaluable_reason,
                    "experiencer_preview": experiencer_preview,
                    "temporality_preview": temporality_preview,
                    "anatomical_location_cue": anatomy_cue.get("label"),
                    "anatomical_location_text": anatomy_cue.get("text"),
                    "modifier_cue": modifier_cue.get("label"),
                    "modifier_text": modifier_cue.get("text"),
                    "trend_cue": trend_cue.get("label"),
                    "trend_text": trend_cue.get("text"),
                    "value_status": value_parse.get("value_status"),
                    "numeric_value": value_parse.get("numeric_value"),
                    "unit": value_parse.get("unit"),
                    "comparator": value_parse.get("comparator"),
                    "value_text": value_parse.get("value_text"),
                    "value_parse_method": value_parse.get("value_parse_method"),
                    "measurement_context": value_parse.get("measurement_context"),
                    "needs_review": action in {"review_candidate", "value_parse"} or value_parse.get("value_parse_method") == "not_parsed",
                    "evidence": sentence,
                    "evidence_context": clip(evidence_context, args.context_chars),
                    "value_context": clip(evidence_context if action == "value_parse" else "", args.context_chars),
                }

                entities_seen += 1
                note_entity_count += 1
                category_counts[category] += 1
                action_counts[action] += 1
                category_action_counts[category][action] += 1
                score_counts[record["score_bin"]] += 1
                type_counts[type_key] += 1
                concept_category_counts[concept_category] += 1
                anatomy_cue_counts[anatomy_cue.get("label") or "none"] += 1
                modifier_cue_counts[modifier_cue.get("label") or "none"] += 1
                trend_cue_counts[trend_cue.get("label") or "none"] += 1
                for flag in flags or {"no_flags"}:
                    flag_counts[flag] += 1
                for reason in reasons:
                    reason_counts[reason] += 1
                if assertion_post_judge_applied:
                    assertion_post_judge_counts[assertion_preview] += 1
                if action == "value_parse":
                    value_parse_counts[value_parse.get("value_parse_method") or "unknown"] += 1

                if action == "reject_candidate":
                    rejected_seen += 1
                    add_reservoir(rejected_records, record, rejected_seen, args.max_rejected_records, rng)
                else:
                    if args.output_mode == "strict_pre_umls":
                        output_ok, output_reasons = strict_pre_umls_decision(record, args.pre_umls_min_keep_score)
                        if not output_ok:
                            output_decision_counts["pre_umls_rejected"] += 1
                            for reason in output_reasons:
                                pre_umls_reject_reason_counts[reason] += 1
                            continue
                        record["pre_umls_strict_gate"] = "passed"
                        record["pre_umls_strict_reasons"] = output_reasons
                        output_decision_counts["pre_umls_written"] += 1
                    else:
                        output_decision_counts["non_rejected_written"] += 1
                    cand_fh.write(json.dumps(record, ensure_ascii=False) + "\n")
                    candidate_records_written += 1

            if note_entity_count:
                notes_with_entities += 1

    with rejected_path.open("w", encoding="utf-8") as rej_fh:
        for record in rejected_records:
            rej_fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    stats = {
        "zip": str(zip_path) if zip_path is not None else None,
        "notes_jsonl_gz": str(notes_path) if notes_path is not None else None,
        "modelpack": str(modelpack_path),
        "filter_policy": str(policy_path),
        "filter_policy_version": policy.get("version"),
        "runtime_config": {
            "medcat_n_process": args.medcat_n_process,
            "medcat_batch_size": args.medcat_batch_size,
            "medcat_batch_size_chars": args.medcat_batch_size_chars,
            "medcat_prefer_gpu": bool(args.medcat_prefer_gpu),
            "medcat_require_gpu": bool(args.medcat_require_gpu),
            "gpu_setup_status": gpu_setup_status,
            "gpu_setup_result": gpu_setup_result,
            "note_section_keep_regex": args.note_section_keep_regex,
            "note_section_drop_regex": args.note_section_drop_regex,
            "note_section_include_unknown": bool(args.note_section_include_unknown),
            "slurm_job_id": os.environ.get("SLURM_JOB_ID"),
            "slurm_array_task_id": os.environ.get("SLURM_ARRAY_TASK_ID"),
            "slurm_cpus_per_task": os.environ.get("SLURM_CPUS_PER_TASK"),
            "cuda_visible_devices": os.environ.get("CUDA_VISIBLE_DEVICES"),
        },
        "sampling": selection_stats,
        "note_text_filter_stats": note_text_filter_stats,
        "output_mode": args.output_mode,
        "pre_umls_min_keep_score": args.pre_umls_min_keep_score,
        "notes_processed": notes_processed,
        "notes_with_entities": notes_with_entities,
        "entities_seen": entities_seen,
        "candidate_records_written": candidate_records_written,
        "rejected_records_sampled": len(rejected_records),
        "rejected_records_seen": rejected_seen,
        "sampled_note_category_counts": selected_category_counts,
        "selected_note_category_counts": selected_category_counts,
        "note_population_category_counts_top": category_population.most_common(100),
        "entity_category_counts": category_counts,
        "filter_action_counts": action_counts,
        "filter_action_counts_by_category": {k: dict(v) for k, v in sorted(category_action_counts.items())},
        "score_bin_counts": score_counts,
        "filter_flag_counts": flag_counts,
        "reason_code_counts": reason_counts,
        "type_counts": type_counts,
        "concept_category_preview_counts": concept_category_counts,
        "anatomical_location_cue_counts": anatomy_cue_counts,
        "modifier_cue_counts": modifier_cue_counts,
        "trend_cue_counts": trend_cue_counts,
        "assertion_post_judge_counts": assertion_post_judge_counts,
        "value_parse_method_counts": value_parse_counts,
        "output_decision_counts": output_decision_counts,
        "pre_umls_reject_reason_counts": pre_umls_reject_reason_counts,
        "outputs": {
            "candidates": str(candidates_path),
            "rejected_sample": str(rejected_path),
            "stats": str(stats_path),
        },
    }
    stats_path.write_text(json.dumps(stats, indent=2, ensure_ascii=False), encoding="utf-8")

    chmod_private(candidates_path)
    chmod_private(rejected_path)
    chmod_private(stats_path)

    stdout_summary = {
        "output_dir": str(out_dir),
        "notes_processed": notes_processed,
        "entities_seen": entities_seen,
        "candidate_records_written": candidate_records_written,
        "rejected_records_seen": rejected_seen,
        "filter_action_counts": dict(action_counts),
        "stats": str(stats_path),
    }
    print(json.dumps(stdout_summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
