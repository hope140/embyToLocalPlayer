"""Load the optional Windows 32-bit Python 3.9 dependencies bundled with etlp."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import struct
import sys
import zipfile


_WHEELS = (
    "certifi-2025.7.14-py3-none-any.whl",
    "charset_normalizer-3.4.2-cp39-cp39-win32.whl",
    "grpcio-1.80.0-cp39-cp39-win32.whl",
    "idna-3.10-py3-none-any.whl",
    "protobuf-6.33.6-cp39-cp39-win32.whl",
    "requests-2.32.5-py3-none-any.whl",
    "typing_extensions-4.15.0-py3-none-any.whl",
    "urllib3-2.5.0-py3-none-any.whl",
)
_MARKER = ".etlp-runtime-deps"


def _safe_extract(archive: Path, destination: Path) -> None:
    destination_root = destination.resolve()
    with zipfile.ZipFile(archive) as package:
        for member in package.infolist():
            target = (destination / member.filename).resolve()
            if os.path.commonpath((str(destination_root), str(target))) != str(destination_root):
                raise RuntimeError(f"unsafe dependency archive member: {member.filename}")
        package.extractall(destination)


def ensure_bundled_dependencies() -> None:
    """Make bundled wheels importable for the supported embedded runtime.

    The repository's portable Windows build uses 32-bit CPython 3.9 without
    pip.  Binary wheels cannot be imported directly from a zip file, so they
    are extracted once into a private runtime directory beside the package.
    Other Python/platform combinations remain unchanged and use normal
    site-packages/requirements installation.
    """

    if sys.platform != "win32" or sys.version_info[:2] != (3, 9) or struct.calcsize("P") != 4:
        return

    package_root = Path(__file__).resolve().parents[1]
    wheel_root = package_root / "third_party"
    runtime_root = wheel_root / "_runtime_deps"
    required = [wheel_root / name for name in _WHEELS]
    if not all(path.is_file() for path in required):
        return

    marker = runtime_root / _MARKER
    if not marker.is_file():
        temporary_root = wheel_root / "_runtime_deps.tmp"
        if temporary_root.exists():
            shutil.rmtree(temporary_root)
        temporary_root.mkdir(parents=True)
        try:
            for archive in required:
                _safe_extract(archive, temporary_root)
            marker_path = temporary_root / _MARKER
            marker_path.write_text("etlp bundled runtime dependencies\n", encoding="utf-8")
            if runtime_root.exists():
                shutil.rmtree(runtime_root)
            temporary_root.rename(runtime_root)
        except Exception:
            if temporary_root.exists():
                shutil.rmtree(temporary_root)
            raise

    runtime_text = str(runtime_root)
    if runtime_text not in sys.path:
        sys.path.insert(0, runtime_text)


__all__ = ["ensure_bundled_dependencies"]
