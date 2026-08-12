"""Read the release metadata embedded in the runtime module."""
from __future__ import annotations

import re
from typing import Any


RELEASE_VERSION = "source"
RELEASE_COMMIT = "unknown"
_RELEASE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+\-]{0,63}$")
_COMMIT_RE = re.compile(r"^[0-9a-fA-F]{12}$")


def _safe_value(value: Any, pattern: re.Pattern[str], fallback: str) -> str:
    if not isinstance(value, str):
        return fallback
    value = value.strip()
    return value if pattern.fullmatch(value) else fallback


def load_release_info() -> tuple[str, str]:
    """Return validated embedded metadata or stable source fallbacks."""
    return (
        _safe_value(RELEASE_VERSION, _RELEASE_RE, "source"),
        _safe_value(RELEASE_COMMIT, _COMMIT_RE, "unknown"),
    )


__all__ = ["RELEASE_VERSION", "RELEASE_COMMIT", "load_release_info"]
