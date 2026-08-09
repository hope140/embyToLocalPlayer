"""Load the optional Windows 32-bit Python 3.9 dependencies bundled with etlp."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import shutil
import struct
import sys
import zipfile


# Keep this mapping ordered.  The order is part of the cache marker so a
# different wheel set (or a reordered set) cannot accidentally reuse a cache
# created for another release.
_WHEELS = {
    "certifi-2025.7.14-py3-none-any.whl": "6b31f564a415d79ee77df69d757bb49a5bb53bd9f756cbbe24394ffd6fc1f4b2",
    "charset_normalizer-3.4.2-cp39-cp39-win32.whl": "43e0933a0eff183ee85833f341ec567c0980dae57c464d8a508e1b2ceb336471",
    "grpcio-1.80.0-cp39-cp39-win32.whl": "627fb7312171cdc52828bd6fac8d7028ff2a64b89f1957b6f3416caa2218d141",
    "idna-3.15-py3-none-any.whl": "048adeaf8c2d788c40fee287673ccaa74c24ffd8dcf09ffa555a2fbb59f10ac8",
    "protobuf-6.33.6-cp39-cp39-win32.whl": "bd56799fb262994b2c2faa1799693c95cc2e22c62f56fb43af311cae45d26f0e",
    "requests-2.32.5-py3-none-any.whl": "2462f94637a34fd532264295e186976db0f5d453d1cdd31473c85a6a161affb6",
    "typing_extensions-4.15.0-py3-none-any.whl": "f0fa19c6845758ab08074a0cfa8b7aecb71c999ca73d62883bc25cc018c4e548",
    "urllib3-2.6.3-py3-none-any.whl": "bf272323e553dfb2e87d9bfd225ca7b0f467b919d7bbd355436d3fd37cb0acd4",
}
_MARKER = ".etlp-runtime-deps"
_MARKER_FORMAT_VERSION = 2
_HASH_CHUNK_SIZE = 1024 * 1024


def _manifest_fingerprint() -> str:
    """Return the deterministic digest for the ordered wheel manifest."""

    digest = hashlib.sha256()
    digest.update(f"format={_MARKER_FORMAT_VERSION}\n".encode("ascii"))
    for filename, expected_hash in _WHEELS.items():
        digest.update(filename.encode("utf-8"))
        digest.update(b"\0")
        digest.update(expected_hash.encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _marker_contents() -> str:
    """Return the marker payload for the current wheel manifest."""

    return (
        "etlp bundled runtime dependencies\n"
        f"format={_MARKER_FORMAT_VERSION}\n"
        f"fingerprint={_manifest_fingerprint()}\n"
    )


def _verify_wheel_hash(archive: Path, expected_hash: str) -> None:
    """Stream a wheel through SHA256 and reject missing or corrupt files."""

    if not archive.is_file():
        raise RuntimeError(f"bundled dependency wheel is missing: {archive.name}")

    digest = hashlib.sha256()
    try:
        with archive.open("rb") as stream:
            for chunk in iter(lambda: stream.read(_HASH_CHUNK_SIZE), b""):
                digest.update(chunk)
    except OSError as exc:
        raise RuntimeError(f"unable to read bundled dependency wheel: {archive.name}") from exc

    actual_hash = digest.hexdigest()
    if actual_hash != expected_hash:
        raise RuntimeError(
            f"bundled dependency wheel hash mismatch: {archive.name} "
            f"(expected {expected_hash}, got {actual_hash})"
        )


def _safe_extract(archive: Path, destination: Path) -> None:
    destination_root = destination.resolve()
    with zipfile.ZipFile(archive) as package:
        for member in package.infolist():
            target = (destination / member.filename).resolve()
            if os.path.commonpath((str(destination_root), str(target))) != str(destination_root):
                raise RuntimeError(f"unsafe dependency archive member: {member.filename}")
        package.extractall(destination)


def _marker_matches(runtime_root: Path) -> bool:
    marker = runtime_root / _MARKER
    if not marker.is_file():
        return False
    try:
        return marker.read_text(encoding="utf-8") == _marker_contents()
    except (OSError, UnicodeError):
        return False


def _swap_runtime_cache(temporary_root: Path, runtime_root: Path) -> None:
    """Replace the cache only after extraction succeeds, restoring on failure."""

    backup_root = runtime_root.with_name(f"{runtime_root.name}.old")
    if backup_root.exists():
        shutil.rmtree(backup_root)

    had_runtime = runtime_root.exists()
    moved_runtime = False
    installed_new = False
    try:
        if had_runtime:
            runtime_root.rename(backup_root)
            moved_runtime = True
        temporary_root.rename(runtime_root)
        installed_new = True
    except Exception:
        if installed_new and runtime_root.exists():
            shutil.rmtree(runtime_root)
        if moved_runtime and backup_root.exists():
            backup_root.rename(runtime_root)
        raise
    else:
        if backup_root.exists():
            shutil.rmtree(backup_root)


def _rebuild_runtime_cache(
    required: list[Path], wheel_root: Path, runtime_root: Path
) -> None:
    """Extract verified wheels into a temporary tree and swap it into place."""

    temporary_root = wheel_root / "_runtime_deps.tmp"
    if temporary_root.exists():
        shutil.rmtree(temporary_root)
    temporary_root.mkdir(parents=True)
    try:
        for archive in required:
            _verify_wheel_hash(archive, _WHEELS[archive.name])
            _safe_extract(archive, temporary_root)
        (temporary_root / _MARKER).write_text(_marker_contents(), encoding="utf-8")
        _swap_runtime_cache(temporary_root, runtime_root)
    except Exception:
        if temporary_root.exists():
            shutil.rmtree(temporary_root)
        raise


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

    if _marker_matches(runtime_root):
        for archive in required:
            _verify_wheel_hash(archive, _WHEELS[archive.name])
    else:
        _rebuild_runtime_cache(required, wheel_root, runtime_root)

    runtime_text = str(runtime_root)
    if runtime_text not in sys.path:
        sys.path.insert(0, runtime_text)


__all__ = ["ensure_bundled_dependencies"]
