"""Shared checks for the mb publish payload."""

import math
from pathlib import PurePosixPath


def validate_payload(payload):
    if not isinstance(payload, dict):
        raise ValueError("payload must be a JSON object")
    plans = payload.get("plans")
    if not isinstance(plans, list) or not plans or not all(isinstance(p, str) for p in plans):
        raise ValueError("plans must be a nonempty array of strings")
    for field in ("scenario", "change"):
        if not isinstance(payload.get(field), str):
            raise ValueError(f"{field} must be a string")
    result = payload.get("result")
    if not isinstance(result, dict) or not isinstance(result.get("summary"), str):
        raise ValueError("result.summary must be a string")
    score = result.get("score")
    if type(score) not in (float, int) or not math.isfinite(score):
        raise ValueError("result.score must be a finite number")
    paths = payload.get("paths", [])
    if not isinstance(paths, list) or not all(
        isinstance(p, str) and not PurePosixPath(p).is_absolute()
        and ".." not in PurePosixPath(p).parts for p in paths
    ):
        raise ValueError("paths must be workspace-relative paths")
