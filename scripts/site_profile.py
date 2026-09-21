#!/usr/bin/env python3
"""Validated access to the optional site-adaptive extraction profile."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any


SCHEMA_NAME = "ehr_cui_assertion_site_profile"
SCHEMA_VERSION = 1
SITE_ADAPTATION_MODES = {
    "unlabeled_structural_bounded",
    "labeled_calibration_bounded",
    "site_adaptive_bounded",
}
PROFILE_KEYS = {
    "$schema",
    "schema_name",
    "schema_version",
    "adaptation_mode",
    "assertion_scopes",
    "measurement_scope",
    "safeguards",
    "aggregate_diagnostics",
}

DEFAULT_ASSERTION_SCOPES: dict[str, dict[str, int]] = {
    "negation": {"before_chars": 150, "after_chars": 90, "before_gap_chars": 120, "after_gap_chars": 20},
    "conditional": {"before_chars": 170, "after_chars": 130, "before_gap_chars": 110, "after_gap_chars": 28},
    "possible": {"before_chars": 160, "after_chars": 120, "before_gap_chars": 90, "after_gap_chars": 10},
    "hypothetical": {"before_chars": 180, "after_chars": 120, "before_gap_chars": 90, "after_gap_chars": 14},
    "historical": {"before_chars": 170, "after_chars": 80, "before_gap_chars": 90, "after_gap_chars": 14},
    "current": {"before_chars": 150, "after_chars": 80, "before_gap_chars": 90, "after_gap_chars": 20},
    "uncertain": {"before_chars": 120, "after_chars": 120, "before_gap_chars": 90, "after_gap_chars": 60},
}

DEFAULT_MEASUREMENT_SCOPE = {"before_chars": 90, "after_chars": 130}

_ASSERTION_LIMITS = {
    "before_chars": (30, 300),
    "after_chars": (20, 260),
    "before_gap_chars": (8, 180),
    "after_gap_chars": (4, 120),
}
_MEASUREMENT_LIMITS = {"before_chars": (20, 180), "after_chars": (40, 300)}


def default_runtime_profile() -> dict[str, Any]:
    return {
        "schema_name": SCHEMA_NAME,
        "schema_version": SCHEMA_VERSION,
        "adaptation_mode": "frozen_default",
        "assertion_scopes": copy.deepcopy(DEFAULT_ASSERTION_SCOPES),
        "measurement_scope": dict(DEFAULT_MEASUREMENT_SCOPE),
    }


def _validated_int(value: Any, key: str, limits: tuple[int, int]) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{key} must be an integer")
    low, high = limits
    if not low <= value <= high:
        raise ValueError(f"{key} must be between {low} and {high}")
    return value


def resolve_site_profile(raw: dict[str, Any] | None) -> dict[str, Any]:
    """Overlay a validated profile onto the exact frozen defaults."""
    resolved = default_runtime_profile()
    if raw is None:
        return resolved
    if raw.get("schema_name") != SCHEMA_NAME:
        raise ValueError(f"site profile schema_name must be {SCHEMA_NAME!r}")
    if raw.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"unsupported site profile schema_version: {raw.get('schema_version')!r}")
    unexpected = sorted(set(raw) - PROFILE_KEYS)
    if unexpected:
        raise ValueError(f"unsupported site profile fields: {', '.join(unexpected)}")
    mode = str(raw.get("adaptation_mode") or "site_adaptive_bounded")
    if mode not in SITE_ADAPTATION_MODES:
        raise ValueError(f"unsupported site profile adaptation_mode: {mode!r}")

    assertion_scopes = raw.get("assertion_scopes") or {}
    if not isinstance(assertion_scopes, dict):
        raise ValueError("assertion_scopes must be an object")
    for family, values in assertion_scopes.items():
        if family not in DEFAULT_ASSERTION_SCOPES:
            raise ValueError(f"unsupported assertion scope family: {family}")
        if not isinstance(values, dict):
            raise ValueError(f"assertion_scopes.{family} must be an object")
        for key, value in values.items():
            if key not in _ASSERTION_LIMITS:
                raise ValueError(f"unsupported assertion scope setting: {family}.{key}")
            resolved["assertion_scopes"][family][key] = _validated_int(
                value, f"assertion_scopes.{family}.{key}", _ASSERTION_LIMITS[key]
            )
            if resolved["assertion_scopes"][family][key] > DEFAULT_ASSERTION_SCOPES[family][key]:
                raise ValueError(
                    f"site profile cannot expand frozen {family}.{key}; "
                    f"maximum is {DEFAULT_ASSERTION_SCOPES[family][key]}"
                )

    measurement_scope = raw.get("measurement_scope") or {}
    if not isinstance(measurement_scope, dict):
        raise ValueError("measurement_scope must be an object")
    for key, value in measurement_scope.items():
        if key not in _MEASUREMENT_LIMITS:
            raise ValueError(f"unsupported measurement scope setting: {key}")
        resolved["measurement_scope"][key] = _validated_int(
            value, f"measurement_scope.{key}", _MEASUREMENT_LIMITS[key]
        )
        if resolved["measurement_scope"][key] > DEFAULT_MEASUREMENT_SCOPE[key]:
            raise ValueError(
                f"site profile cannot expand frozen measurement_scope.{key}; "
                f"maximum is {DEFAULT_MEASUREMENT_SCOPE[key]}"
            )

    resolved["adaptation_mode"] = mode
    return resolved


def load_site_profile(path: str | Path | None) -> dict[str, Any]:
    if not path:
        return default_runtime_profile()
    profile_path = Path(path)
    raw = json.loads(profile_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("site profile root must be an object")
    return resolve_site_profile(raw)


def profile_sha256(path: str | Path | None) -> str | None:
    if not path:
        return None
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def assertion_scope(profile: dict[str, Any], family: str, key: str) -> int:
    return int(profile["assertion_scopes"][family][key])


def measurement_scope(profile: dict[str, Any], key: str) -> int:
    return int(profile["measurement_scope"][key])
