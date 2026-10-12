#!/usr/bin/env python3
"""Optimized prefork MedCAT candidate extraction.

This script keeps the same candidate-record logic as extract_medcat_candidates.py
while loading the MedCAT modelpack once in the parent process and forking CPU
workers over explicit note ranges. The final wrapper uses all_non_rejected mode;
standalone strict-linker benchmarks can use strict_pre_umls mode.
"""

from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import os
import random
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import extract_medcat_candidates as base  # noqa: E402


GLOBAL_CAT = None
GLOBAL_POLICY: dict[str, Any] | None = None
GLOBAL_ARGS: SimpleNamespace | None = None
GLOBAL_TASKS: dict[str, dict[str, Any]] = {}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--modelpack", required=True)
    parser.add_argument("--filter-policy", required=True)
    parser.add_argument("--output-root", required=True)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--max-note-chars", type=int, default=7000)
    parser.add_argument("--min-acc", type=float, default=0.0)
    parser.add_argument(
        "--output-mode",
        choices=["all_non_rejected", "strict_pre_umls"],
        default="strict_pre_umls",
        help="Candidate output contract; use all_non_rejected when downstream overlays perform final refinement.",
    )
    parser.add_argument("--pre-umls-min-keep-score", type=float, default=0.99)
    parser.add_argument("--sentence-chars", type=int, default=420)
    parser.add_argument("--context-chars", type=int, default=650)
    parser.add_argument("--max-rejected-records", type=int, default=0)
    parser.add_argument("--progress-every", type=int, default=500)
    parser.add_argument("--gzip-compresslevel", type=int, default=1)
    parser.add_argument("--medcat-use-multi-texts", action="store_true")
    parser.add_argument("--medcat-batch-size", type=int, default=-1)
    parser.add_argument("--medcat-batch-size-chars", type=int, default=1000000)
    parser.add_argument("--seed", type=int, default=20260604)
    parser.add_argument("--ontology-lexicon-output", default="")
    parser.add_argument("--allow-no-slurm", action="store_true")
    return parser.parse_args()


def chmod_private(path: Path) -> None:
    try:
        path.chmod(0o600)
    except FileNotFoundError:
        pass


def counter_to_dict(counter: Counter) -> dict[str, int]:
    return dict(counter)


def process_task(task_id: str) -> dict[str, Any]:
    if GLOBAL_CAT is None or GLOBAL_POLICY is None or GLOBAL_ARGS is None:
        raise RuntimeError("prefork worker was not initialized")
    task = GLOBAL_TASKS[task_id]
    args = GLOBAL_ARGS
    policy = GLOBAL_POLICY
    cat = GLOBAL_CAT
    if "selected_notes" in task:
        selected_notes = task["selected_notes"]
        selection_stats = task["selection_stats"]
        note_text_filter_stats = task["note_text_filter_stats"]
    else:
        notes_path = Path(task["notes_jsonl_gz"])
        selected_notes, category_population, rows_read, nonempty_rows = base.range_notes_from_shard(
            notes_path,
            start_note_index=int(task["start_note_index"]),
            end_note_index=int(task["end_note_index"]),
            max_total=int(task["end_note_index"]),
        )
        selection_stats = {
            "strategy": "lazy explicit contiguous nonempty-note index range over NOTEEVENTS shard",
            "source_kind": "notes_jsonl_gz",
            "source_path": str(notes_path),
            "start_note_index": int(task["start_note_index"]),
            "end_note_index": int(task["end_note_index"]),
            "rows_read": rows_read,
            "nonempty_note_rows_seen": nonempty_rows,
            "max_total_notes": int(task["end_note_index"]),
            "max_note_chars": int(args.max_note_chars),
            "min_acc": float(args.min_acc),
            "note_population_category_counts_top": category_population.most_common(100),
        }
        note_text_filter_stats = base.apply_note_text_filters(selected_notes, "", "", False)
    out_dir = Path(task["out_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    out_dir.chmod(0o700)

    rng = random.Random(int(args.seed) + int(task["task_index"]))
    candidates_path = out_dir / "medcat_candidates.jsonl.gz"
    rejected_path = out_dir / "medcat_rejected_sample.jsonl"
    stats_path = out_dir / "medcat_candidate_stats.json"

    action_counts: Counter[str] = Counter()
    category_counts: Counter[str] = Counter()
    selected_category_counts: Counter[str] = Counter(row["CATEGORY"] for row in selected_notes)
    flag_counts: Counter[str] = Counter()
    reason_counts: Counter[str] = Counter()
    score_counts: Counter[str] = Counter()
    type_counts: Counter[str] = Counter()
    category_action_counts: dict[str, Counter[str]] = defaultdict(Counter)
    value_parse_counts: Counter[str] = Counter()
    concept_category_counts: Counter[str] = Counter()
    anatomy_cue_counts: Counter[str] = Counter()
    modifier_cue_counts: Counter[str] = Counter()
    trend_cue_counts: Counter[str] = Counter()
    assertion_post_judge_counts: Counter[str] = Counter()
    output_decision_counts: Counter[str] = Counter()
    pre_umls_reject_reason_counts: Counter[str] = Counter()

    notes_processed = 0
    notes_with_entities = 0
    entities_seen = 0
    candidate_records_written = 0
    rejected_seen = 0
    rejected_records: list[dict[str, Any]] = []

    start_epoch = int(time.time())
    print(
        f"[prefork progress] task={task_id} notes={len(selected_notes)} "
        f"range={task['start_note_index']}-{task['end_note_index']} host={os.uname().nodename}",
        flush=True,
    )
    with base.open_output_text(candidates_path, int(args.gzip_compresslevel)) as cand_fh:
        for row, snippet, result in base.iter_note_entity_results(
            cat,
            selected_notes,
            int(args.max_note_chars),
            1,
            int(args.medcat_batch_size),
            int(args.medcat_batch_size_chars),
            bool(args.medcat_use_multi_texts),
        ):
            notes_processed += 1
            if int(args.progress_every) > 0 and notes_processed % int(args.progress_every) == 0:
                print(
                    f"[prefork progress] task={task_id} processed_notes={notes_processed} "
                    f"entities_seen={entities_seen}",
                    flush=True,
                )

            category = row["CATEGORY"]
            ents = result.get("entities", result) if isinstance(result, dict) else {}
            iterable = enumerate(ents) if isinstance(ents, list) else ents.items()
            note_entity_count = 0

            for _, ent in iterable:
                if not isinstance(ent, dict):
                    continue
                acc = float(base.entity_field(ent, "acc", "context_similarity", default=1.0) or 0.0)
                if acc < float(args.min_acc):
                    continue
                start = int(base.entity_field(ent, "start", default=0) or 0)
                end = int(base.entity_field(ent, "end", default=start) or start)
                if start < 0 or end < start or start >= len(snippet):
                    continue
                raw_id = str(base.entity_field(ent, "cui", "id", default=""))
                pretty_name = str(base.entity_field(ent, "pretty_name", "source_value", "detected_name", default=""))
                raw_source_value = str(base.entity_field(ent, "source_value", "detected_name", default=snippet[start:end]))
                source_value = raw_source_value
                source_value, start, end, normalized_lab_alias = base.normalize_lab_value_span(source_value, start, end)
                types = base.entity_field(ent, "type_ids", "types", default=[]) or []
                if isinstance(types, str):
                    types = [types]
                type_ids = sorted(map(str, types))
                sentence = base.sentence_for_span(snippet, start, end, int(args.sentence_chars))
                evidence_context_raw, context_span_start, context_span_end = base.context_with_marker(snippet, start, end)
                evidence_context, context_span_start, context_span_end = base.clean_context_with_span(
                    evidence_context_raw,
                    context_span_start,
                    context_span_end,
                )
                section = base.section_for_span(snippet, start)
                raw_id, pretty_name, type_ids, override_reasons = base.apply_concept_overrides(
                    raw_id,
                    pretty_name,
                    type_ids,
                    source_value,
                    section,
                    sentence,
                    evidence_context,
                )
                type_key = "|".join(type_ids) if type_ids else "unknown"

                flags = base.filter_flags(
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
                value_parse = (
                    base.parse_lab_value(source_value, pretty_name, evidence_context, context_span_start, context_span_end,
                                         note_text=snippet, note_start=start, note_end=end)
                    if "lab_or_vital_span" in flags
                    else {
                        "numeric_value": None,
                        "unit": None,
                        "comparator": None,
                        "value_text": None,
                        "value_status": "not_applicable",
                        "value_parse_method": "not_applicable",
                        "measurement_context": "not_applicable",
                    }
                )
                action, reasons = base.classify_action(
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

                concept_category = base.infer_concept_category(source_value, pretty_name, type_ids, flags)
                assertion_preview_raw = base.infer_assertion_preview(
                    sentence,
                    section,
                    action,
                    source_value,
                    evidence_context,
                    concept_category,
                )
                assertion_preview, assertion_post_judge_applied, assertion_post_judge_reasons = base.refine_assertion_preview(
                    sentence,
                    section,
                    action,
                    source_value,
                    evidence_context,
                    concept_category,
                    assertion_preview_raw,
                )
                assertion_evaluable, assertion_evaluable_reason = base.assertion_evaluable_preview(
                    source_value,
                    concept_category,
                )
                temporality_preview = base.infer_temporality_preview(sentence, section, source_value)
                experiencer_preview = base.infer_experiencer_preview(sentence, section, flags)
                anatomy_cue = base.infer_context_cue(evidence_context, base.ANATOMY_CUE_PATTERNS)
                modifier_cue = base.infer_context_cue(evidence_context, base.MODIFIER_CUE_PATTERNS)
                trend_cue = base.infer_context_cue(evidence_context, base.TREND_CUE_PATTERNS)

                record = {
                    "note_row_id": row["ROW_ID"],
                    "subject_id": row["SUBJECT_ID"],
                    "hadm_id": row["HADM_ID"],
                    "chartdate": row["CHARTDATE"],
                    "category": category,
                    "section": section,
                    "span_start": start,
                    "span_end": end,
                    "span": base.clean(source_value),
                    "concept_id": raw_id,
                    "concept_id_system": base.concept_id_system(raw_id),
                    "pretty_name": pretty_name,
                    "type_ids": type_ids,
                    "raw_linker_concept_id": str(base.entity_field(ent, "cui", "id", default="")),
                    "raw_linker_pretty_name": str(
                        base.entity_field(ent, "pretty_name", "source_value", "detected_name", default="")
                    ),
                    "raw_linker_span": base.clean(raw_source_value),
                    "concept_override_applied": bool(override_reasons),
                    "span_normalization_applied": bool(normalized_lab_alias),
                    "linker_name": "medcat_umls_snomed_v2",
                    "linker_score": acc,
                    "score_bin": base.score_bin(acc),
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
                    "needs_review": action in {"review_candidate", "value_parse"}
                    or value_parse.get("value_parse_method") == "not_parsed",
                    "evidence": sentence,
                    "evidence_context": base.clip(evidence_context, int(args.context_chars)),
                    "value_context": base.clip(evidence_context if action == "value_parse" else "", int(args.context_chars)),
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
                    base.add_reservoir(rejected_records, record, rejected_seen, int(args.max_rejected_records), rng)
                else:
                    if args.output_mode == "strict_pre_umls":
                        output_ok, output_reasons = base.strict_pre_umls_decision(
                            record,
                            float(args.pre_umls_min_keep_score),
                        )
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

    end_epoch = int(time.time())
    stats = {
        "zip": None,
        "notes_jsonl_gz": task["notes_jsonl_gz"],
        "modelpack": str(args.modelpack),
        "filter_policy": str(args.filter_policy),
        "filter_policy_version": policy.get("version"),
        "runtime_config": {
            "execution_strategy": (
                "prefork4_parent_loaded_medcat_once_worker_batch_get_entities_multi_texts"
                if bool(args.medcat_use_multi_texts)
                else "prefork4_parent_loaded_medcat_once"
            ),
            "medcat_n_process": 1,
            "medcat_use_multi_texts": bool(args.medcat_use_multi_texts),
            "medcat_batch_size": int(args.medcat_batch_size),
            "medcat_batch_size_chars": int(args.medcat_batch_size_chars),
            "medcat_prefer_gpu": False,
            "medcat_require_gpu": False,
            "gpu_setup_status": "not_requested",
            "gpu_setup_result": None,
            "note_section_keep_regex": "",
            "note_section_drop_regex": "",
            "note_section_include_unknown": False,
            "slurm_job_id": os.environ.get("SLURM_JOB_ID"),
            "slurm_array_task_id": os.environ.get("SLURM_ARRAY_TASK_ID"),
            "slurm_cpus_per_task": os.environ.get("SLURM_CPUS_PER_TASK"),
            "cuda_visible_devices": os.environ.get("CUDA_VISIBLE_DEVICES"),
        },
        "sampling": selection_stats,
        "note_text_filter_stats": note_text_filter_stats,
        "output_mode": args.output_mode,
        "pre_umls_min_keep_score": float(args.pre_umls_min_keep_score),
        "notes_processed": notes_processed,
        "notes_with_entities": notes_with_entities,
        "entities_seen": entities_seen,
        "candidate_records_written": candidate_records_written,
        "rejected_records_sampled": len(rejected_records),
        "rejected_records_seen": rejected_seen,
        "sampled_note_category_counts": counter_to_dict(selected_category_counts),
        "selected_note_category_counts": counter_to_dict(selected_category_counts),
        "note_population_category_counts_top": category_counts.most_common(100),
        "entity_category_counts": counter_to_dict(category_counts),
        "filter_action_counts": counter_to_dict(action_counts),
        "filter_action_counts_by_category": {k: counter_to_dict(v) for k, v in sorted(category_action_counts.items())},
        "score_bin_counts": counter_to_dict(score_counts),
        "filter_flag_counts": counter_to_dict(flag_counts),
        "reason_code_counts": counter_to_dict(reason_counts),
        "type_counts": counter_to_dict(type_counts),
        "concept_category_preview_counts": counter_to_dict(concept_category_counts),
        "anatomical_location_cue_counts": counter_to_dict(anatomy_cue_counts),
        "modifier_cue_counts": counter_to_dict(modifier_cue_counts),
        "trend_cue_counts": counter_to_dict(trend_cue_counts),
        "assertion_post_judge_counts": counter_to_dict(assertion_post_judge_counts),
        "value_parse_method_counts": counter_to_dict(value_parse_counts),
        "output_decision_counts": counter_to_dict(output_decision_counts),
        "pre_umls_reject_reason_counts": counter_to_dict(pre_umls_reject_reason_counts),
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

    timing_path = out_dir / "stage_timing.json"
    timing = {
        "stage": "prefork4_medcat_span_linking_schema_filter",
        "variant": task["variant"],
        "task_id": task_id,
        "shard_idx": task["shard_idx"],
        "chunk_idx": task["chunk_idx"],
        "start_note_index": task["start_note_index"],
        "end_note_index": task["end_note_index"],
        "notes_jsonl_gz": task["notes_jsonl_gz"],
        "slurm_job_id": os.environ.get("SLURM_JOB_ID"),
        "host": os.uname().nodename,
        "cpus_per_task": int(os.environ.get("SLURM_CPUS_PER_TASK", "0") or 0),
        "prefork_worker_count": int(args.workers),
        "medcat_use_multi_texts": bool(args.medcat_use_multi_texts),
        "medcat_batch_size": int(args.medcat_batch_size),
        "medcat_batch_size_chars": int(args.medcat_batch_size_chars),
        "start_epoch": start_epoch,
        "end_epoch": end_epoch,
        "elapsed_seconds": end_epoch - start_epoch,
    }
    timing_path.write_text(json.dumps(timing, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    chmod_private(timing_path)
    return {
        "task_id": task_id,
        "out_dir": str(out_dir),
        "notes_processed": notes_processed,
        "candidate_records_written": candidate_records_written,
        "elapsed_seconds": end_epoch - start_epoch,
    }


def prepare_tasks(args: argparse.Namespace, cfg: dict[str, Any], out_root: Path) -> dict[str, dict[str, Any]]:
    tasks: dict[str, dict[str, Any]] = {}
    variant = cfg["variant_name"]
    lazy_load_notes = bool(cfg.get("lazy_load_notes", False))
    explicit_tasks = cfg.get("tasks") or cfg.get("explicit_tasks")
    if explicit_tasks:
        for task_index, task_cfg in enumerate(explicit_tasks):
            notes_path = Path(task_cfg["notes_jsonl_gz"])
            start = int(task_cfg["start_note_index"])
            end = int(task_cfg["end_note_index"])
            task_id = str(task_cfg.get("task_id") or f"task_{task_index:04d}")
            task = {
                "task_index": task_index,
                "task_id": task_id,
                "variant": variant,
                "notes_jsonl_gz": str(notes_path),
                "shard_idx": int(task_cfg.get("shard_idx", task_index)),
                "chunk_idx": int(task_cfg.get("chunk_idx", 0)),
                "start_note_index": start,
                "end_note_index": end,
                "out_dir": str(out_root / f"variant_{variant}" / "extraction" / task_id),
            }
            if not lazy_load_notes:
                selected_notes, category_population, rows_read, nonempty_rows = base.range_notes_from_shard(
                    notes_path,
                    start_note_index=start,
                    end_note_index=end,
                    max_total=end,
                )
                selection_stats = {
                    "strategy": "explicit contiguous nonempty-note index range over NOTEEVENTS shard",
                    "source_kind": "notes_jsonl_gz",
                    "source_path": str(notes_path),
                    "start_note_index": start,
                    "end_note_index": end,
                    "rows_read": rows_read,
                    "nonempty_note_rows_seen": nonempty_rows,
                    "max_total_notes": end,
                    "max_note_chars": int(args.max_note_chars),
                    "min_acc": float(args.min_acc),
                }
                note_text_filter_stats = base.apply_note_text_filters(selected_notes, "", "", False)
                task["selected_notes"] = selected_notes
                task["selection_stats"] = selection_stats | {
                    "note_population_category_counts_top": category_population.most_common(100),
                }
                task["note_text_filter_stats"] = note_text_filter_stats
            tasks[task_id] = task
        return tasks

    chunks_per_shard = int(cfg["chunks_per_shard"])
    notes_per_chunk = int(cfg["notes_per_chunk"])
    for task_index in range(int(cfg["task_count"])):
        shard_idx = task_index // chunks_per_shard
        chunk_idx = task_index % chunks_per_shard
        start = chunk_idx * notes_per_chunk + 1
        end = (chunk_idx + 1) * notes_per_chunk
        notes_path = Path(cfg["note_shards"][shard_idx])
        selected_notes, category_population, rows_read, nonempty_rows = base.range_notes_from_shard(
            notes_path,
            start_note_index=start,
            end_note_index=end,
            max_total=end,
        )
        selection_stats = {
            "strategy": "contiguous nonempty-note index range over NOTEEVENTS",
            "source_kind": "notes_jsonl_gz",
            "source_path": str(notes_path),
            "start_note_index": start,
            "end_note_index": end,
            "rows_read": rows_read,
            "nonempty_note_rows_seen": nonempty_rows,
            "max_total_notes": end,
            "max_note_chars": int(args.max_note_chars),
            "min_acc": float(args.min_acc),
        }
        note_text_filter_stats = base.apply_note_text_filters(selected_notes, "", "", False)
        task_id = f"shard_{shard_idx}_chunk_{chunk_idx}"
        tasks[task_id] = {
            "task_index": task_index,
            "task_id": task_id,
            "variant": variant,
            "notes_jsonl_gz": str(notes_path),
            "shard_idx": shard_idx,
            "chunk_idx": chunk_idx,
            "start_note_index": start,
            "end_note_index": end,
            "selected_notes": selected_notes,
            "selection_stats": selection_stats | {
                "note_population_category_counts_top": category_population.most_common(100),
            },
            "note_text_filter_stats": note_text_filter_stats,
            "out_dir": str(out_root / f"variant_{variant}" / "extraction" / task_id),
        }
    return tasks


def main() -> int:
    args = parse_args()
    if not os.environ.get("SLURM_JOB_ID") and not args.allow_no_slurm:
        print("ERROR: run prefork MedCAT benchmarking through SLURM.", file=sys.stderr)
        return 2
    if int(args.workers) < 1:
        print("ERROR: --workers must be >= 1", file=sys.stderr)
        return 2

    config_path = Path(args.config)
    modelpack_path = Path(args.modelpack)
    policy_path = Path(args.filter_policy)
    out_root = Path(args.output_root)
    for path in (config_path, modelpack_path, policy_path):
        if not path.exists():
            print(f"ERROR missing input: {path}", file=sys.stderr)
            return 2
    out_root.mkdir(parents=True, exist_ok=True)
    out_root.chmod(0o700)

    cfg = json.loads(config_path.read_text(encoding="utf-8"))
    if "task_count" not in cfg:
        explicit_tasks = cfg.get("tasks") or cfg.get("explicit_tasks")
        if explicit_tasks:
            cfg["task_count"] = len(explicit_tasks)
        else:
            cfg["task_count"] = int(cfg["chunks_per_shard"]) * len(cfg["note_shards"])
    policy = base.prepare_policy_runtime(base.load_policy(policy_path))
    tasks = prepare_tasks(args, cfg, out_root)

    from medcat.cat import CAT

    print(
        f"[prefork] loading MedCAT once modelpack={modelpack_path} workers={args.workers} "
        f"tasks={len(tasks)}",
        flush=True,
    )
    load_start = int(time.time())
    cat = CAT.load_model_pack(str(modelpack_path))
    load_end = int(time.time())
    print(f"[prefork] model_loaded elapsed_seconds={load_end - load_start}", flush=True)
    # MedCAT lazily rebuilds this read-only index on the first child lookup.
    # Build it once before fork so every worker inherits the same index.
    if hasattr(cat, "cdb") and hasattr(cat.cdb, "_reset_subnames"):
        cat.cdb._reset_subnames()

    global GLOBAL_CAT, GLOBAL_POLICY, GLOBAL_ARGS, GLOBAL_TASKS
    GLOBAL_CAT = cat
    GLOBAL_POLICY = policy
    GLOBAL_ARGS = SimpleNamespace(**vars(args))
    GLOBAL_TASKS = tasks

    task_ids = sorted(tasks)
    run_start = int(time.time())
    ctx = mp.get_context("fork")
    # A killed worker must fail the run, not leave Pool waiting for a lost task.
    with ProcessPoolExecutor(max_workers=int(args.workers), mp_context=ctx) as pool:
        results = list(pool.map(process_task, task_ids))
    run_end = int(time.time())

    if args.ontology_lexicon_output:
        from add_medcat_cdb_ontology_overlay import write_run_lexicon

        model_dir = modelpack_path.with_suffix("") if modelpack_path.suffix == ".zip" else modelpack_path
        write_run_lexicon(Path(args.ontology_lexicon_output), cat.cdb, model_dir / "cdb")

    # Make the shard-level timing files compatible with the existing fair-4CPU
    # reporter: batch wall time must include the single parent-side MedCAT load.
    for result in results:
        timing_path = Path(result["out_dir"]) / "stage_timing.json"
        timing = json.loads(timing_path.read_text(encoding="utf-8"))
        timing["prefork_parent_model_load_start_epoch"] = load_start
        timing["prefork_model_loaded_epoch"] = load_end
        timing["prefork_worker_start_epoch"] = timing.get("start_epoch")
        timing["start_epoch"] = load_start
        timing["elapsed_seconds"] = int(timing["end_epoch"]) - load_start
        timing_path.write_text(json.dumps(timing, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        chmod_private(timing_path)

    summary_path = out_root / f"variant_{cfg['variant_name']}" / "extraction" / "prefork4_summary.json"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary = {
        "stage": "prefork4_medcat_parent_load_once_summary",
        "variant": cfg["variant_name"],
        "workers": int(args.workers),
        "task_count": len(task_ids),
        "slurm_job_id": os.environ.get("SLURM_JOB_ID"),
        "host": os.uname().nodename,
        "cpus_per_task": int(os.environ.get("SLURM_CPUS_PER_TASK", "0") or 0),
        "model_load_seconds": load_end - load_start,
        "worker_wall_seconds": run_end - run_start,
        "total_elapsed_seconds": run_end - load_start,
        "results": results,
    }
    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    chmod_private(summary_path)
    print(json.dumps(summary, indent=2, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
