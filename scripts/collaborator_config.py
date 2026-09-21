#!/usr/bin/env python3
"""Validated collaborator-facing configuration for the extraction pipeline."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

from site_profile import load_site_profile


SCHEMA_NAME = "ehr_cui_assertion_collaborator_run"
SCHEMA_VERSION = 1
PRESET_NAME = "validated_harvard_v1"

DEFAULT_CONFIG: dict[str, Any] = {
    "schema_name": SCHEMA_NAME,
    "schema_version": SCHEMA_VERSION,
    "preset": PRESET_NAME,
    "inputs": {
        "notes_jsonl": ["<PRIVATE_NOTES_JSONL_OR_JSONL_GZ>"],
        "medcat_modelpack": "<LICENSED_MEDCAT_MODELPACK_ZIP>",
        "medcat_cdb": "<LICENSED_MEDCAT_CDB_PATH>",
        "filter_policy": "linker_filter_policy.example.json",
        "output_root": "<PRIVATE_OUTPUT_DIRECTORY>",
    },
    "execution": {
        "run_name": "harvard_round2",
        "workers": 0,
        "notes_per_task": 0,
        "max_notes": 0,
        "progress_every": 1000,
        "keep_selected_notes": False,
        "allow_no_slurm": False,
    },
    "hyperparameters": {
        "linker": {
            "max_note_chars": 7000,
            "minimum_confidence": 0.0,
            "pre_umls_min_keep_score": 0.99,
            "sentence_chars": 420,
            "context_chars": 650,
            "medcat_batch_size": -1,
            "medcat_batch_size_chars": 1_000_000,
            "gzip_compresslevel": 1,
        },
        "ontology": {
            "max_words": 8,
            "overlap_mode": "add_missing",
            "allow_broad_single_word": False,
        },
        "refinement": {
            "strictness": "aggressive",
            "strict_gate": True,
            "boundary_ranker": True,
            "anchor_rescue": True,
            "assertion_judge": True,
        },
    },
    "site_adaptation": {
        "mode": "frozen_default",
        "profile_path": None,
        "builder": {
            "quantile": 0.95,
            "minimum_observations": 25,
            "max_notes": 0,
        },
    },
}

TOP_LEVEL_KEYS = set(DEFAULT_CONFIG)
SECTION_KEYS = {
    "inputs": set(DEFAULT_CONFIG["inputs"]),
    "execution": set(DEFAULT_CONFIG["execution"]),
    "hyperparameters": set(DEFAULT_CONFIG["hyperparameters"]),
    "site_adaptation": set(DEFAULT_CONFIG["site_adaptation"]),
    "linker": set(DEFAULT_CONFIG["hyperparameters"]["linker"]),
    "ontology": set(DEFAULT_CONFIG["hyperparameters"]["ontology"]),
    "refinement": set(DEFAULT_CONFIG["hyperparameters"]["refinement"]),
    "builder": set(DEFAULT_CONFIG["site_adaptation"]["builder"]),
}


def template_config() -> dict[str, Any]:
    return copy.deepcopy(DEFAULT_CONFIG)


def _object(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    return value


def _reject_unknown(value: dict[str, Any], allowed: set[str], name: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ValueError(f"unsupported {name} fields: {', '.join(unknown)}")


def _integer(value: Any, name: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValueError(f"{name} must be an integer between {low} and {high}")
    return value


def _number(value: Any, name: str, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= float(value) <= high:
        raise ValueError(f"{name} must be between {low} and {high}")
    return float(value)


def _boolean(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be true or false")
    return value


def _string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _merge(default: dict[str, Any], supplied: dict[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(default)
    for key, value in supplied.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _expand_path(value: str, base_dir: Path) -> str:
    expanded = Path(os.path.expandvars(os.path.expanduser(value)))
    if not expanded.is_absolute():
        expanded = base_dir / expanded
    return str(expanded.resolve(strict=False))


def _contains_placeholder(value: str) -> bool:
    return "<" in value or ">" in value


def load_collaborator_config(path: str | Path, *, check_paths: bool = False) -> dict[str, Any]:
    config_path = Path(path).expanduser().resolve(strict=True)
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    raw = _object(raw, "config")
    _reject_unknown(raw, TOP_LEVEL_KEYS | {"$schema"}, "top-level")
    if raw.get("schema_name") != SCHEMA_NAME:
        raise ValueError(f"schema_name must be {SCHEMA_NAME!r}")
    if raw.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"schema_version must be {SCHEMA_VERSION}")
    if raw.get("preset") != PRESET_NAME:
        raise ValueError(f"preset must be {PRESET_NAME!r}")

    supplied = {key: value for key, value in raw.items() if key != "$schema"}
    resolved = _merge(DEFAULT_CONFIG, supplied)
    base_dir = config_path.parent

    inputs = _object(resolved["inputs"], "inputs")
    _reject_unknown(inputs, SECTION_KEYS["inputs"], "inputs")
    notes = inputs.get("notes_jsonl")
    if not isinstance(notes, list) or not notes:
        raise ValueError("inputs.notes_jsonl must be a non-empty array")
    inputs["notes_jsonl"] = [_expand_path(_string(value, "inputs.notes_jsonl[]"), base_dir) for value in notes]
    for key in ("medcat_modelpack", "medcat_cdb", "filter_policy", "output_root"):
        inputs[key] = _expand_path(_string(inputs[key], f"inputs.{key}"), base_dir)

    execution = _object(resolved["execution"], "execution")
    _reject_unknown(execution, SECTION_KEYS["execution"], "execution")
    execution["run_name"] = _string(execution["run_name"], "execution.run_name")
    execution["workers"] = _integer(execution["workers"], "execution.workers", 0, 1024)
    execution["notes_per_task"] = _integer(execution["notes_per_task"], "execution.notes_per_task", 0, 10_000_000)
    execution["max_notes"] = _integer(execution["max_notes"], "execution.max_notes", 0, 100_000_000)
    execution["progress_every"] = _integer(execution["progress_every"], "execution.progress_every", 1, 10_000_000)
    for key in ("keep_selected_notes", "allow_no_slurm"):
        execution[key] = _boolean(execution[key], f"execution.{key}")

    hyperparameters = _object(resolved["hyperparameters"], "hyperparameters")
    _reject_unknown(hyperparameters, SECTION_KEYS["hyperparameters"], "hyperparameters")
    linker = _object(hyperparameters["linker"], "hyperparameters.linker")
    _reject_unknown(linker, SECTION_KEYS["linker"], "hyperparameters.linker")
    linker["max_note_chars"] = _integer(linker["max_note_chars"], "hyperparameters.linker.max_note_chars", 256, 1_000_000)
    linker["minimum_confidence"] = _number(linker["minimum_confidence"], "hyperparameters.linker.minimum_confidence", 0.0, 1.0)
    linker["pre_umls_min_keep_score"] = _number(linker["pre_umls_min_keep_score"], "hyperparameters.linker.pre_umls_min_keep_score", 0.0, 1.0)
    linker["sentence_chars"] = _integer(linker["sentence_chars"], "hyperparameters.linker.sentence_chars", 50, 5000)
    linker["context_chars"] = _integer(linker["context_chars"], "hyperparameters.linker.context_chars", 50, 10_000)
    linker["medcat_batch_size"] = _integer(linker["medcat_batch_size"], "hyperparameters.linker.medcat_batch_size", -1, 100_000)
    linker["medcat_batch_size_chars"] = _integer(linker["medcat_batch_size_chars"], "hyperparameters.linker.medcat_batch_size_chars", 1, 100_000_000)
    linker["gzip_compresslevel"] = _integer(linker["gzip_compresslevel"], "hyperparameters.linker.gzip_compresslevel", 0, 9)

    ontology = _object(hyperparameters["ontology"], "hyperparameters.ontology")
    _reject_unknown(ontology, SECTION_KEYS["ontology"], "hyperparameters.ontology")
    ontology["max_words"] = _integer(ontology["max_words"], "hyperparameters.ontology.max_words", 1, 30)
    if ontology["overlap_mode"] not in {"add_missing", "replace"}:
        raise ValueError("hyperparameters.ontology.overlap_mode must be 'add_missing' or 'replace'")
    ontology["allow_broad_single_word"] = _boolean(
        ontology["allow_broad_single_word"], "hyperparameters.ontology.allow_broad_single_word"
    )

    refinement = _object(hyperparameters["refinement"], "hyperparameters.refinement")
    _reject_unknown(refinement, SECTION_KEYS["refinement"], "hyperparameters.refinement")
    if refinement["strictness"] not in {"conservative", "aggressive"}:
        raise ValueError("hyperparameters.refinement.strictness must be 'conservative' or 'aggressive'")
    for key in ("strict_gate", "boundary_ranker", "anchor_rescue", "assertion_judge"):
        refinement[key] = _boolean(refinement[key], f"hyperparameters.refinement.{key}")

    adaptation = _object(resolved["site_adaptation"], "site_adaptation")
    _reject_unknown(adaptation, SECTION_KEYS["site_adaptation"], "site_adaptation")
    if adaptation["mode"] not in {"frozen_default", "profile_file"}:
        raise ValueError("site_adaptation.mode must be 'frozen_default' or 'profile_file'")
    profile_path = adaptation.get("profile_path")
    if adaptation["mode"] == "profile_file":
        adaptation["profile_path"] = _expand_path(_string(profile_path, "site_adaptation.profile_path"), base_dir)
    elif profile_path is not None:
        raise ValueError("site_adaptation.profile_path must be null when mode is 'frozen_default'")
    builder = _object(adaptation["builder"], "site_adaptation.builder")
    _reject_unknown(builder, SECTION_KEYS["builder"], "site_adaptation.builder")
    builder["quantile"] = _number(builder["quantile"], "site_adaptation.builder.quantile", 0.5, 0.99)
    builder["minimum_observations"] = _integer(
        builder["minimum_observations"], "site_adaptation.builder.minimum_observations", 1, 1_000_000
    )
    builder["max_notes"] = _integer(builder["max_notes"], "site_adaptation.builder.max_notes", 0, 100_000_000)

    if check_paths:
        required_paths = [*inputs["notes_jsonl"], inputs["medcat_modelpack"], inputs["medcat_cdb"], inputs["filter_policy"]]
        if adaptation["mode"] == "profile_file":
            required_paths.append(adaptation["profile_path"])
        for value in required_paths:
            if _contains_placeholder(value):
                raise ValueError(f"replace placeholder path before running: {value}")
            if not Path(value).exists():
                raise FileNotFoundError(value)
        if _contains_placeholder(inputs["output_root"]):
            raise ValueError(f"replace placeholder path before running: {inputs['output_root']}")
        if adaptation["mode"] == "profile_file":
            load_site_profile(adaptation["profile_path"])

    resolved["config_path"] = str(config_path)
    return resolved


def config_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def final_extraction_command(config: dict[str, Any], script_dir: Path, *, dry_run: bool = False) -> list[str]:
    inputs = config["inputs"]
    execution = config["execution"]
    linker = config["hyperparameters"]["linker"]
    ontology = config["hyperparameters"]["ontology"]
    refinement = config["hyperparameters"]["refinement"]
    adaptation = config["site_adaptation"]
    command = [os.fspath(Path(sys.executable)), os.fspath(script_dir / "run_final_extraction.py")]
    for path in inputs["notes_jsonl"]:
        command.extend(["--notes-jsonl-gz", path])
    command.extend([
        "--modelpack", inputs["medcat_modelpack"],
        "--cdb-path", inputs["medcat_cdb"],
        "--filter-policy", inputs["filter_policy"],
        "--output-root", inputs["output_root"],
        "--variant-name", execution["run_name"],
        "--workers", str(execution["workers"]),
        "--notes-per-task", str(execution["notes_per_task"]),
        "--max-notes", str(execution["max_notes"]),
        "--progress-every", str(execution["progress_every"]),
        "--max-note-chars", str(linker["max_note_chars"]),
        "--linker-min-acc", str(linker["minimum_confidence"]),
        "--pre-umls-min-keep-score", str(linker["pre_umls_min_keep_score"]),
        "--sentence-chars", str(linker["sentence_chars"]),
        "--context-chars", str(linker["context_chars"]),
        "--medcat-batch-size", str(linker["medcat_batch_size"]),
        "--medcat-batch-size-chars", str(linker["medcat_batch_size_chars"]),
        "--gzip-compresslevel", str(linker["gzip_compresslevel"]),
        "--ontology-max-words", str(ontology["max_words"]),
        "--ontology-overlap-mode", ontology["overlap_mode"],
        "--refinement-strictness", refinement["strictness"],
        "--deployment-config-sha256", config_sha256(config["config_path"]),
    ])
    if adaptation["mode"] == "profile_file":
        command.extend(["--site-profile", adaptation["profile_path"]])
    if execution["keep_selected_notes"]:
        command.append("--keep-selected-notes")
    if execution["allow_no_slurm"]:
        command.append("--allow-no-slurm")
    if ontology["allow_broad_single_word"]:
        command.append("--ontology-allow-broad-single-word")
    for enabled, flag in (
        (refinement["strict_gate"], "--disable-refinement-strict-gate"),
        (refinement["boundary_ranker"], "--disable-boundary-ranker"),
        (refinement["anchor_rescue"], "--disable-anchor-rescue"),
        (refinement["assertion_judge"], "--disable-assertion-judge"),
    ):
        if not enabled:
            command.append(flag)
    if dry_run:
        command.append("--dry-run")
    return command
