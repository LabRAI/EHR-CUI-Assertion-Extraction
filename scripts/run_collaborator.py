#!/usr/bin/env python3
"""One-config collaborator interface for validated EHR extraction."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from collaborator_config import (
    config_sha256,
    final_extraction_command,
    load_collaborator_config,
    template_config,
)


SCRIPT_DIR = Path(__file__).resolve().parent


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="action", required=True)

    init_parser = subparsers.add_parser("init", help="write a complete local configuration template")
    init_parser.add_argument("--output", required=True)

    for action in ("validate", "show", "run"):
        command_parser = subparsers.add_parser(action)
        command_parser.add_argument("--config", required=True)
        if action == "run":
            command_parser.add_argument("--dry-run", action="store_true")

    profile_parser = subparsers.add_parser("build-profile", help="build an aggregate site profile on calibration data")
    profile_parser.add_argument("--config", required=True)
    profile_parser.add_argument("--candidate-jsonl", action="append", required=True)
    profile_parser.add_argument("--output", required=True)
    return parser.parse_args()


def write_template(path: Path) -> None:
    if path.exists():
        raise FileExistsError(f"refusing to overwrite existing file: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = template_config()
    payload["$schema"] = "./collaborator_run.schema.json"
    path.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    path.chmod(0o600)


def build_profile_command(args: argparse.Namespace, config: dict) -> list[str]:
    builder = config["site_adaptation"]["builder"]
    command = [sys.executable, str(SCRIPT_DIR / "build_site_profile.py")]
    for path in config["inputs"]["notes_jsonl"]:
        command.extend(["--notes-jsonl-gz", path])
    for path in args.candidate_jsonl:
        command.extend(["--candidate-jsonl", str(Path(path).expanduser().resolve(strict=True))])
    command.extend([
        "--output", str(Path(args.output).expanduser().resolve(strict=False)),
        "--quantile", str(builder["quantile"]),
        "--minimum-observations", str(builder["minimum_observations"]),
        "--max-notes", str(builder["max_notes"]),
    ])
    if config["execution"]["allow_no_slurm"]:
        command.append("--allow-no-slurm")
    return command


def main() -> int:
    args = parse_args()
    if args.action == "init":
        output = Path(args.output).expanduser().resolve(strict=False)
        write_template(output)
        print(json.dumps({"created": str(output), "next": f"edit paths, then validate --config {output}"}, indent=2))
        return 0

    check_paths = args.action in {"validate", "run", "build-profile"}
    config = load_collaborator_config(args.config, check_paths=check_paths)
    if args.action == "validate":
        print(json.dumps({
            "valid": True,
            "preset": config["preset"],
            "config_sha256": config_sha256(config["config_path"]),
            "notes_files": len(config["inputs"]["notes_jsonl"]),
            "workers": config["execution"]["workers"],
            "site_adaptation": config["site_adaptation"]["mode"],
        }, indent=2, sort_keys=True))
        return 0

    if args.action == "build-profile":
        command = build_profile_command(args, config)
        print("[collaborator] " + " ".join(command), flush=True)
        return subprocess.run(command, check=False).returncode

    command = final_extraction_command(config, SCRIPT_DIR, dry_run=getattr(args, "dry_run", False))
    if args.action == "show":
        printable = dict(config)
        printable["command"] = command
        print(json.dumps(printable, indent=2, sort_keys=True))
        return 0

    print("[collaborator] preset=" + config["preset"], flush=True)
    print("[collaborator] config_sha256=" + config_sha256(config["config_path"]), flush=True)
    print("[collaborator] " + " ".join(command), flush=True)
    return subprocess.run(command, check=False, env=os.environ.copy()).returncode


if __name__ == "__main__":
    raise SystemExit(main())
