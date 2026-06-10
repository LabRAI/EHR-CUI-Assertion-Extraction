#!/usr/bin/env python3
"""Summarize extraction scaling benchmark results and draw runtime curves."""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
from pathlib import Path
from statistics import mean


def parse_gnu_time(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text(errors="ignore").splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        out[key.strip()] = value.strip()
    return out


def seconds_to_hms(seconds: float | int | None) -> str:
    if seconds is None or math.isnan(float(seconds)):
        return "NA"
    seconds = int(round(float(seconds)))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    if days:
        return f"{days}-{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def summarize_variant(bench_dir: Path) -> dict:
    config_files = sorted((bench_dir.parent / "configs").glob(f"{bench_dir.name}.json"))
    config = load_json(config_files[0]) if config_files else {}
    extraction_dir = bench_dir / f"variant_{bench_dir.name}" / "extraction"
    summary = load_json(extraction_dir / "prefork4_summary.json")
    results = summary.get("results") or []
    stats_files = sorted(extraction_dir.glob("*/medcat_candidate_stats.json"))
    stat_rows = [load_json(path) for path in stats_files]
    notes_processed = sum(int(row.get("notes_processed") or 0) for row in stat_rows)
    notes_with_entities = sum(int(row.get("notes_with_entities") or 0) for row in stat_rows)
    candidates = sum(int(row.get("candidate_records_written") or 0) for row in stat_rows)
    entities_seen = sum(int(row.get("entities_seen") or 0) for row in stat_rows)
    rejected = sum(int(row.get("rejected_records_seen") or 0) for row in stat_rows)
    worker_elapsed_max = max((int(item.get("elapsed_seconds") or 0) for item in results), default=0)
    total_elapsed = int(summary.get("total_elapsed_seconds") or worker_elapsed_max or 0)
    gnu = parse_gnu_time(bench_dir / "extract_gnu_time.txt")
    max_rss_kb = None
    if gnu.get("Maximum resident set size (kbytes)"):
        try:
            max_rss_kb = int(gnu["Maximum resident set size (kbytes)"])
        except ValueError:
            pass
    m = re.search(r"_n(\d+)_cpu(\d+)_", bench_dir.name)
    name_notes = int(m.group(1)) if m else None
    name_cpus = int(m.group(2)) if m else None
    status = "complete" if summary and notes_processed else "missing"
    return {
        "variant": bench_dir.name,
        "status": status,
        "requested_notes": config.get("requested_notes", name_notes),
        "planned_notes": config.get("actual_planned_notes", name_notes),
        "notes_processed": notes_processed,
        "notes_with_entities": notes_with_entities,
        "cpus": config.get("workers", name_cpus),
        "task_count": config.get("task_count") or len(results),
        "elapsed_seconds": total_elapsed,
        "elapsed_hms": seconds_to_hms(total_elapsed) if total_elapsed else "NA",
        "notes_per_second": notes_processed / total_elapsed if total_elapsed else 0.0,
        "candidate_records_written": candidates,
        "candidates_per_note": candidates / notes_processed if notes_processed else 0.0,
        "entities_seen": entities_seen,
        "rejected_records_seen": rejected,
        "model_load_seconds": summary.get("model_load_seconds"),
        "worker_wall_seconds": summary.get("worker_wall_seconds"),
        "max_rss_gb": max_rss_kb / 1024 / 1024 if max_rss_kb else None,
        "benchmark_dir": str(bench_dir),
    }


def fit_power_law(rows: list[dict]) -> dict[int, dict[str, float]]:
    by_cpu: dict[int, list[dict]] = {}
    for row in rows:
        if row["status"] != "complete" or row["elapsed_seconds"] <= 0 or row["notes_processed"] <= 0:
            continue
        by_cpu.setdefault(int(row["cpus"]), []).append(row)
    fits: dict[int, dict[str, float]] = {}
    for cpu, items in by_cpu.items():
        if len(items) < 2:
            continue
        xs = [math.log(float(item["notes_processed"])) for item in items]
        ys = [math.log(float(item["elapsed_seconds"])) for item in items]
        xbar, ybar = mean(xs), mean(ys)
        denom = sum((x - xbar) ** 2 for x in xs)
        if denom == 0:
            continue
        slope = sum((x - xbar) * (y - ybar) for x, y in zip(xs, ys)) / denom
        intercept = ybar - slope * xbar
        fits[cpu] = {"intercept": intercept, "slope": slope, "scale": math.exp(intercept)}
    return fits


def write_plot(rows: list[dict], fits: dict[int, dict[str, float]], out_path: Path) -> None:
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import numpy as np
    except Exception as exc:
        out_path.with_suffix(".plot_error.txt").write_text(str(exc), encoding="utf-8")
        return

    complete = [r for r in rows if r["status"] == "complete" and r["elapsed_seconds"] > 0]
    if not complete:
        return
    fig, ax = plt.subplots(figsize=(9, 5.2))
    cpus = sorted({int(r["cpus"]) for r in complete})
    for cpu in cpus:
        items = sorted([r for r in complete if int(r["cpus"]) == cpu], key=lambda r: r["notes_processed"])
        x = [r["notes_processed"] for r in items]
        y = [r["elapsed_seconds"] / 60 for r in items]
        ax.plot(x, y, marker="o", linewidth=2, label=f"{cpu} CPU")
        for r in items:
            ax.annotate(
                seconds_to_hms(r["elapsed_seconds"]),
                (r["notes_processed"], r["elapsed_seconds"] / 60),
                textcoords="offset points",
                xytext=(4, 4),
                fontsize=7,
            )
        if cpu in fits:
            xmin, xmax = min(x), max(x)
            if xmin < xmax:
                grid = np.geomspace(xmin, xmax, 100)
                fit = fits[cpu]
                pred = fit["scale"] * (grid ** fit["slope"]) / 60
                ax.plot(grid, pred, linestyle="--", alpha=0.45)
    ax.set_xscale("log")
    ax.set_yscale("log")
    ax.set_xlabel("Notes processed")
    ax.set_ylabel("Extraction wall time (minutes)")
    ax.set_title("MIMIC-III CUI/assertion extraction scaling")
    ax.grid(True, which="both", alpha=0.25)
    ax.legend(title="Resource")
    fig.tight_layout()
    fig.savefig(out_path, dpi=200)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--benchmark-root", required=True)
    args = parser.parse_args()
    root = Path(args.benchmark_root)
    config_dir = root / "configs"
    report_dir = root / "report"
    report_dir.mkdir(parents=True, exist_ok=True)
    report_dir.chmod(0o700)

    rows = []
    for cfg_path in sorted(config_dir.glob("scaling_*.json")):
        variant = json.loads(cfg_path.read_text(encoding="utf-8"))["variant_name"]
        rows.append(summarize_variant(root / variant))
    rows.sort(key=lambda r: (int(r.get("cpus") or 0), int(r.get("planned_notes") or 0)))
    fits = fit_power_law(rows)

    csv_path = report_dir / "extraction_scaling_results.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as fh:
        fieldnames = [
            "variant",
            "status",
            "planned_notes",
            "notes_processed",
            "cpus",
            "task_count",
            "elapsed_hms",
            "elapsed_seconds",
            "notes_per_second",
            "candidate_records_written",
            "candidates_per_note",
            "entities_seen",
            "model_load_seconds",
            "worker_wall_seconds",
            "max_rss_gb",
            "benchmark_dir",
        ]
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key) for key in fieldnames})

    json_path = report_dir / "extraction_scaling_results.json"
    json_path.write_text(json.dumps({"rows": rows, "fits": fits}, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    plot_path = report_dir / "extraction_scaling_runtime_curves.png"
    write_plot(rows, fits, plot_path)

    lines = [
        "# Extraction Scaling Benchmark",
        "",
        "Scope: MedCAT SNOMED/UMLS linker + unchanged schema-aware strict pre-UMLS candidate extraction.",
        "",
        "| CPU | Notes | Time | Notes/sec | Candidates | Candidates/note | Status |",
        "|---:|---:|---:|---:|---:|---:|---|",
    ]
    for row in rows:
        lines.append(
            f"| {row.get('cpus')} | {row.get('notes_processed') or row.get('planned_notes')} | "
            f"{row.get('elapsed_hms')} | {float(row.get('notes_per_second') or 0):.3f} | "
            f"{row.get('candidate_records_written')} | {float(row.get('candidates_per_note') or 0):.2f} | "
            f"{row.get('status')} |"
        )
    lines.extend(["", "## Power-Law Fits", ""])
    if fits:
        lines.append("| CPU | Fit form | Slope |")
        lines.append("|---:|---|---:|")
        for cpu, fit in sorted(fits.items()):
            lines.append(f"| {cpu} | time = {fit['scale']:.4g} * notes^{fit['slope']:.3f} | {fit['slope']:.3f} |")
    else:
        lines.append("Not enough completed points yet.")
    lines.extend(
        [
            "",
            f"CSV: `{csv_path}`",
            f"JSON: `{json_path}`",
            f"Plot: `{plot_path}`",
        ]
    )
    md_path = report_dir / "extraction_scaling_report.md"
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    for path in (csv_path, json_path, plot_path, md_path):
        if path.exists():
            path.chmod(0o600)
    print(json.dumps({"csv": str(csv_path), "json": str(json_path), "plot": str(plot_path), "md": str(md_path)}, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
