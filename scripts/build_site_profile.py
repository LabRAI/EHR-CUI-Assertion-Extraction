#!/usr/bin/env python3
"""Build a privacy-preserving CPU-only extraction profile from local notes.

The output contains aggregate counts and bounded scope parameters only. It does
not contain note text, entity strings, CUIs, section names, or source paths.
"""

from __future__ import annotations

import argparse
import gzip
import json
import math
import os
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

from site_profile import (
    DEFAULT_ASSERTION_SCOPES,
    DEFAULT_MEASUREMENT_SCOPE,
    SCHEMA_NAME,
    SCHEMA_VERSION,
    resolve_site_profile,
)


CUE_PATTERNS = {
    "negation": re.compile(
        r"\b(?:no|none|denies?|denied|without|w/o|negative for|free of|no evidence of|"
        r"no signs? of|absence of|lack of|absent|not present|ruled out|excluded)\b",
        re.I,
    ),
    "conditional": re.compile(
        r"\b(?:if|unless|when|whenever|provided that|in case of|pending|depending on|"
        r"prn|as needed|if needed|if necessary|use as needed|take as needed)\b",
        re.I,
    ),
    "possible": re.compile(
        r"\b(?:possible|possibly|probable|presumed|suspected|suspicious(?: for)?|"
        r"concern(?:ing)? for|r/o|rule out|cannot rule out|cannot exclude|may have|"
        r"may represent|might represent|could represent|suggestive of|compatible with)\b",
        re.I,
    ),
    "hypothetical": re.compile(
        r"\b(?:plan(?:ned|s|ning)?(?: for| to)?|schedule(?:d)?(?: for| to)?|evaluate for|"
        r"workup for|screen(?:ing)? for|monitor(?:ing)? for|risk of|risk for|"
        r"prophylaxis for|to prevent|would require|may require)\b",
        re.I,
    ),
    "historical": re.compile(
        r"\b(?:remote|prior|previous|previously|former|resolved|status post|s/p|"
        r"history of|h/o|hx of|underwent|completed|post[- ]?op)\b",
        re.I,
    ),
    "current": re.compile(
        r"\b(?:current|currently|active|present(?:s|ed|ing)? with|admitted with|"
        r"found to have|noted to have|shows?|revealed|demonstrated|confirmed|"
        r"reports?|continue|receiving|treated with|started on)\b",
        re.I,
    ),
    "uncertain": re.compile(
        r"\b(?:cannot\s+(?:be\s+)?(?:excluded?|rule out)|not\s+(?:fully\s+)?excluded|"
        r"not\s+ruled\s+out|to\s+be\s+ruled\s+out)\b",
        re.I,
    ),
}

NUMERIC = re.compile(r"(?<![A-Za-z])(?:<=|>=|<|>)?\s*(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?:/\d+(?:\.\d+)?)?")
TARGET_ACTIONS = {"keep_candidate", "review_candidate", "value_parse", "context_only"}
BOUNDARY = re.compile(r"[.;\n]")
HEADER = re.compile(r"^\s*[A-Za-z][A-Za-z0-9 /,_()#&+\-]{1,80}:\s*$")
LIST_LINE = re.compile(r"^\s*(?:[-*]|\d+[.)])\s+\S")
TABLE_LINE = re.compile(r"\t|\||\S\s{2,}\S")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--notes-jsonl-gz", action="append", required=True)
    parser.add_argument("--candidate-jsonl", action="append", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--quantile", type=float, default=0.95)
    parser.add_argument("--minimum-observations", type=int, default=25)
    parser.add_argument("--max-notes", type=int, default=0)
    parser.add_argument("--allow-no-slurm", action="store_true", help="Synthetic/local smoke tests only")
    return parser.parse_args()


def open_text(path: Path):
    if str(path).endswith(".gz"):
        return gzip.open(path, "rt", encoding="utf-8", errors="replace")
    return path.open("r", encoding="utf-8", errors="replace")


def iter_files(paths: Iterable[str]) -> Iterable[Path]:
    for value in paths:
        path = Path(value)
        if path.is_file():
            yield path
        else:
            yield from sorted(path.glob("**/medcat_candidates.jsonl"))
            yield from sorted(path.glob("**/medcat_candidates.jsonl.gz"))


def clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def target_occurrences(text: str, span: str) -> list[re.Match[str]]:
    pieces = [re.escape(part) for part in clean(span).split()]
    if not pieces:
        return []
    return list(re.finditer(r"(?<!\w)" + r"\s+".join(pieces) + r"(?!\w)", text, re.I))


def clause_bounds(text: str, start: int, end: int) -> tuple[int, int]:
    left = max((match.end() for match in BOUNDARY.finditer(text, 0, start)), default=0)
    right_match = BOUNDARY.search(text, end)
    right = right_match.start() if right_match else len(text)
    return left, right


def nearest_extents(text: str, target: re.Match[str], pattern: re.Pattern[str]) -> tuple[int | None, int | None, int | None, int | None]:
    left, right = clause_bounds(text, target.start(), target.end())
    before_matches = [match for match in pattern.finditer(text, left, target.start()) if match.end() <= target.start()]
    after_matches = [match for match in pattern.finditer(text, target.end(), right) if match.start() >= target.end()]
    before = max(before_matches, key=lambda match: match.end(), default=None)
    after = min(after_matches, key=lambda match: match.start(), default=None)
    return (
        target.start() - before.start() if before else None,
        target.start() - before.end() if before else None,
        after.end() - target.end() if after else None,
        after.start() - target.end() if after else None,
    )


def add_histogram(histogram: Counter[int], value: int | None) -> None:
    if value is not None and value >= 0:
        histogram[min(value, 1000)] += 1


def histogram_count(histogram: Counter[int]) -> int:
    return sum(histogram.values())


def histogram_quantile(histogram: Counter[int], quantile: float) -> int | None:
    total = histogram_count(histogram)
    if not total:
        return None
    threshold = max(1, math.ceil(total * quantile))
    cumulative = 0
    for value, count in sorted(histogram.items()):
        cumulative += count
        if cumulative >= threshold:
            return value
    return max(histogram)


def bounded_selection(observed: int | None, default: int, minimum_fraction: float) -> int:
    if observed is None:
        return default
    floor = math.ceil(default * minimum_fraction)
    candidate = max(floor, observed + 8)
    return min(default, candidate)


def profile_notes(paths: list[str], max_notes: int) -> dict[str, Any]:
    notes = lines = headers = lists = tables = blank_separated_blocks = 0
    for path_value in paths:
        with open_text(Path(path_value)) as handle:
            for line in handle:
                if not line.strip():
                    continue
                row = json.loads(line)
                text = str(row.get("text") or row.get("TEXT") or row.get("note_text") or "")
                if not text.strip():
                    continue
                notes += 1
                note_lines = text.splitlines() or [text]
                lines += len(note_lines)
                headers += sum(bool(HEADER.match(item)) for item in note_lines)
                lists += sum(bool(LIST_LINE.match(item)) for item in note_lines)
                tables += sum(bool(TABLE_LINE.search(item)) for item in note_lines)
                blank_separated_blocks += len(re.findall(r"\n\s*\n", text))
                if max_notes and notes >= max_notes:
                    break
        if max_notes and notes >= max_notes:
            break
    denominator = max(1, lines)
    return {
        "notes": notes,
        "lines": lines,
        "header_line_fraction": headers / denominator,
        "list_line_fraction": lists / denominator,
        "table_like_line_fraction": tables / denominator,
        "blank_separated_blocks_per_note": blank_separated_blocks / max(1, notes),
    }


def profile_candidates(paths: list[str]) -> tuple[dict[str, dict[str, Counter[int]]], dict[str, Counter[int]], int, int]:
    assertion_histograms = {
        family: {key: Counter() for key in ("before_extent", "before_gap", "after_extent", "after_gap")}
        for family in CUE_PATTERNS
    }
    measurement_histograms = {"before_extent": Counter(), "after_extent": Counter()}
    rows = measurement_rows = 0
    for path in iter_files(paths):
        with open_text(path) as handle:
            for line in handle:
                if not line.strip():
                    continue
                row = json.loads(line)
                if row.get("filter_action") not in TARGET_ACTIONS:
                    continue
                span = clean(row.get("span"))
                text = clean(row.get("evidence_context")) or clean(row.get("evidence"))
                occurrences = target_occurrences(text, span)
                if not occurrences:
                    continue
                rows += 1
                target = occurrences[0]
                for family, pattern in CUE_PATTERNS.items():
                    before_extent, before_gap, after_extent, after_gap = nearest_extents(text, target, pattern)
                    add_histogram(assertion_histograms[family]["before_extent"], before_extent)
                    add_histogram(assertion_histograms[family]["before_gap"], before_gap)
                    add_histogram(assertion_histograms[family]["after_extent"], after_extent)
                    add_histogram(assertion_histograms[family]["after_gap"], after_gap)

                if row.get("filter_action") == "value_parse" or row.get("concept_category_preview") == "lab_result_or_value":
                    before_extent, _before_gap, after_extent, _after_gap = nearest_extents(text, target, NUMERIC)
                    add_histogram(measurement_histograms["before_extent"], before_extent)
                    add_histogram(measurement_histograms["after_extent"], after_extent)
                    measurement_rows += 1
    return assertion_histograms, measurement_histograms, rows, measurement_rows


def build_profile(args: argparse.Namespace) -> dict[str, Any]:
    if not 0.5 <= args.quantile <= 0.99:
        raise ValueError("--quantile must be between 0.5 and 0.99")
    if args.minimum_observations < 1:
        raise ValueError("--minimum-observations must be positive")

    layout = profile_notes(args.notes_jsonl_gz, args.max_notes)
    assertion_histograms, measurement_histograms, candidate_rows, measurement_rows = profile_candidates(args.candidate_jsonl)
    assertion_scopes: dict[str, dict[str, int]] = {}
    diagnostics: dict[str, Any] = {}
    for family, default in DEFAULT_ASSERTION_SCOPES.items():
        family_histograms = assertion_histograms[family]
        before_n = histogram_count(family_histograms["before_extent"])
        after_n = histogram_count(family_histograms["after_extent"])
        before_extent = histogram_quantile(family_histograms["before_extent"], args.quantile) if before_n >= args.minimum_observations else None
        after_extent = histogram_quantile(family_histograms["after_extent"], args.quantile) if after_n >= args.minimum_observations else None
        before_gap = histogram_quantile(family_histograms["before_gap"], args.quantile) if before_n >= args.minimum_observations else None
        after_gap = histogram_quantile(family_histograms["after_gap"], args.quantile) if after_n >= args.minimum_observations else None
        assertion_scopes[family] = {
            "before_chars": bounded_selection(before_extent, default["before_chars"], 0.60),
            "after_chars": bounded_selection(after_extent, default["after_chars"], 0.60),
            "before_gap_chars": bounded_selection(before_gap, default["before_gap_chars"], 0.60),
            "after_gap_chars": bounded_selection(after_gap, default["after_gap_chars"], 0.60),
        }
        diagnostics[family] = {
            "before_observations": before_n,
            "after_observations": after_n,
            "before_extent_quantile": before_extent,
            "after_extent_quantile": after_extent,
            "before_gap_quantile": before_gap,
            "after_gap_quantile": after_gap,
            "used_frozen_default": before_extent is None and after_extent is None,
        }

    measurement_before_n = histogram_count(measurement_histograms["before_extent"])
    measurement_after_n = histogram_count(measurement_histograms["after_extent"])
    measurement_before = histogram_quantile(measurement_histograms["before_extent"], args.quantile) if measurement_before_n >= args.minimum_observations else None
    measurement_after = histogram_quantile(measurement_histograms["after_extent"], args.quantile) if measurement_after_n >= args.minimum_observations else None
    profile = {
        "schema_name": SCHEMA_NAME,
        "schema_version": SCHEMA_VERSION,
        "adaptation_mode": "unlabeled_structural_bounded",
        "assertion_scopes": assertion_scopes,
        "measurement_scope": {
            "before_chars": bounded_selection(measurement_before, DEFAULT_MEASUREMENT_SCOPE["before_chars"], 0.70),
            "after_chars": bounded_selection(measurement_after, DEFAULT_MEASUREMENT_SCOPE["after_chars"], 0.70),
        },
        "safeguards": {
            "scope_boundaries_before_distance": True,
            "unlabeled_profile_never_expands_frozen_windows": True,
            "minimum_observations": args.minimum_observations,
            "quantile": args.quantile,
            "requires_held_out_regression_check": True,
        },
        "aggregate_diagnostics": {
            "layout": layout,
            "candidate_rows_profiled": candidate_rows,
            "measurement_rows_profiled": measurement_rows,
            "assertion_distance": diagnostics,
            "measurement_distance": {
                "before_observations": measurement_before_n,
                "after_observations": measurement_after_n,
                "before_extent_quantile": measurement_before,
                "after_extent_quantile": measurement_after,
            },
        },
    }
    resolve_site_profile(profile)
    return profile


def main() -> int:
    args = parse_args()
    if not os.environ.get("SLURM_JOB_ID") and not args.allow_no_slurm:
        print("ERROR: build profiles from clinical data through SLURM.", file=sys.stderr)
        return 2
    profile = build_profile(args)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(profile, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    output.chmod(0o600)
    print(json.dumps({
        "output": str(output),
        "notes_profiled": profile["aggregate_diagnostics"]["layout"]["notes"],
        "candidate_rows_profiled": profile["aggregate_diagnostics"]["candidate_rows_profiled"],
        "adaptation_mode": profile["adaptation_mode"],
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
