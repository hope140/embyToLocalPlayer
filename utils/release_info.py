"""Read the release metadata embedded in the runtime module."""
from __future__ import annotations

import re
from typing import Any


RELEASE_VERSION = "source"
RELEASE_COMMIT = "unknown"
RELEASE_CHANNEL = "source"
_RELEASE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+\-]{0,63}$")
_COMMIT_RE = re.compile(r"^[0-9a-fA-F]{12}$")
_CHANNEL_RE = re.compile(r"^(?:beta|stable)$")


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


def load_release_channel() -> str:
    """Return the package channel, defaulting old/source installs to beta."""
    return _safe_value(RELEASE_CHANNEL, _CHANNEL_RE, "beta")


__all__ = [
    "RELEASE_VERSION",
    "RELEASE_COMMIT",
    "RELEASE_CHANNEL",
    "load_release_info",
    "load_release_channel",
]
