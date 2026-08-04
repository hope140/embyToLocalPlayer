"""Durable storage for the small watch-together room registry.

Only room metadata is persisted here.  Authentication credentials, Emby
tokens, session ids and coordinator state deliberately live outside this
module and are never accepted as room fields.
"""

from __future__ import annotations

import copy
import json
import os
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


SCHEMA_VERSION = 1
ROOM_REQUIRED_FIELDS = {
    "id",
    "server_id",
    "server_url",
    "name",
    "participant_user_ids",
    "primary_user_id",
    "created_at",
}
ROOM_ALLOWED_FIELDS = ROOM_REQUIRED_FIELDS


class WatchTogetherStoreError(RuntimeError):
    """Raised when the room file is unreadable or violates its schema."""


def normalize_server_url(value: str) -> str:
    """Return one canonical base URL without an ``/emby`` suffix.

    Emby links are often copied from ``/web`` or ``/emby`` pages.  Room
    records identify the server, rather than a particular UI route, so those
    suffixes are removed while preserving any genuine reverse-proxy prefix.
    """

    if value is None:
        raise ValueError("server_url is required")
    value = str(value).strip()
    if not value:
        raise ValueError("server_url is required")
    parsed = urlsplit(value)
    if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc:
        raise ValueError("server_url must be an absolute http(s) URL")
    path = parsed.path.rstrip("/")
    lower_path = path.lower()
    for suffix in ("/web/index.html", "/web", "/emby"):
        if lower_path.endswith(suffix):
            path = path[: -len(suffix)].rstrip("/")
            break
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), path, "", "")).rstrip("/")


def _iso_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _validate_string(value, field):
    if not isinstance(value, str) or not value.strip():
        raise WatchTogetherStoreError(f"room {field} must be a non-empty string")
    return value.strip()


def validate_room(room):
    """Validate and return a detached, normalized room dictionary."""

    if not isinstance(room, dict):
        raise WatchTogetherStoreError("room must be an object")
    missing = ROOM_REQUIRED_FIELDS.difference(room)
    if missing:
        raise WatchTogetherStoreError("room missing fields: " + ", ".join(sorted(missing)))
    unknown = set(room).difference(ROOM_ALLOWED_FIELDS)
    if unknown:
        raise WatchTogetherStoreError("room contains unsupported fields: " + ", ".join(sorted(unknown)))
    try:
        room_id = str(uuid.UUID(str(room["id"])))
    except (ValueError, TypeError, AttributeError) as exc:
        raise WatchTogetherStoreError("room id must be a UUID") from exc
    server_id = _validate_string(room["server_id"], "server_id")
    try:
        server_url = normalize_server_url(room["server_url"])
    except ValueError as exc:
        raise WatchTogetherStoreError(str(exc)) from exc
    name = _validate_string(room["name"], "name")
    members = room["participant_user_ids"]
    if not isinstance(members, list) or len(members) != 2:
        raise WatchTogetherStoreError("participant_user_ids must contain exactly two users")
    members = [_validate_string(value, "participant_user_ids") for value in members]
    if len(set(members)) != 2:
        raise WatchTogetherStoreError("participant_user_ids must contain two distinct users")
    primary = _validate_string(room["primary_user_id"], "primary_user_id")
    if primary not in members:
        raise WatchTogetherStoreError("primary_user_id must be a participant")
    created_at = _validate_string(room["created_at"], "created_at")
    # Ensure malformed timestamps do not become permanently opaque metadata.
    try:
        datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise WatchTogetherStoreError("created_at must be an ISO-8601 timestamp") from exc
    return {
        "id": room_id,
        "server_id": server_id,
        "server_url": server_url,
        "name": name,
        "participant_user_ids": list(members),
        "primary_user_id": primary,
        "created_at": created_at,
    }


class WatchTogetherStore:
    """Thread-safe JSON room registry with atomic replacement writes."""

    def __init__(self, path=None, *, cwd=None):
        if path is None:
            if cwd is None:
                try:
                    from utils.configs import configs

                    cwd = configs.cwd
                except Exception:
                    cwd = os.getcwd()
            path = os.path.join(cwd, "watch_together_rooms.json")
        self.path = Path(path)
        self.store_path = self.path
        self._lock = threading.RLock()
        self._rooms = {}
        self.load()

    def _read(self):
        if not self.path.exists():
            return {}
        try:
            with self.path.open("r", encoding="utf-8") as stream:
                payload = json.load(stream)
        except Exception as exc:
            raise WatchTogetherStoreError(f"cannot read room store {self.path}: {exc}") from exc
        if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
            raise WatchTogetherStoreError(
                f"unsupported room store schema in {self.path}; expected schema_version={SCHEMA_VERSION}"
            )
        unknown_top_level = set(payload).difference({"schema_version", "rooms"})
        if unknown_top_level:
            raise WatchTogetherStoreError(
                "room store contains unsupported fields: " + ", ".join(sorted(unknown_top_level))
            )
        rooms = payload.get("rooms")
        if not isinstance(rooms, list):
            raise WatchTogetherStoreError("room store rooms must be a list")
        result = {}
        for room in rooms:
            clean = validate_room(room)
            if clean["id"] in result:
                raise WatchTogetherStoreError(f"duplicate room id: {clean['id']}")
            result[clean["id"]] = clean
        return result

    def load(self):
        """Load from disk, preserving a damaged file for manual recovery."""

        with self._lock:
            self._rooms = self._read()
            return self.list_rooms()

    reload = load

    def _write(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": SCHEMA_VERSION,
            "rooms": [self._rooms[key] for key in sorted(self._rooms)],
        }
        # A same-directory temporary file makes os.replace atomic on Windows
        # and POSIX.  It is intentionally a predictable sibling for .gitignore.
        fd, temp_name = tempfile.mkstemp(
            prefix=self.path.name + ".", suffix=".tmp", dir=str(self.path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                json.dump(payload, stream, ensure_ascii=False, sort_keys=True, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_name, self.path)
        except Exception as exc:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
            raise WatchTogetherStoreError(f"cannot atomically write room store {self.path}: {exc}") from exc

    def list_rooms(self):
        with self._lock:
            return [copy.deepcopy(self._rooms[key]) for key in sorted(self._rooms)]

    list = list_rooms

    @property
    def rooms(self):
        return self.list_rooms()

    def get_room(self, room_id):
        with self._lock:
            room = self._rooms.get(str(room_id))
            return copy.deepcopy(room) if room is not None else None

    get = get_room

    def create_room(self, server_id, server_url, name, participant_user_ids,
                    primary_user_id, room_id=None, created_at=None):
        room = validate_room({
            "id": str(room_id or uuid.uuid4()),
            "server_id": server_id,
            "server_url": server_url,
            "name": name,
            "participant_user_ids": list(participant_user_ids),
            "primary_user_id": primary_user_id,
            "created_at": created_at or _iso_now(),
        })
        with self._lock:
            if room["id"] in self._rooms:
                raise WatchTogetherStoreError(f"room id already exists: {room['id']}")
            self._rooms[room["id"]] = room
            self._write()
            return copy.deepcopy(room)

    create = create_room

    def delete_room(self, room_id):
        room_id = str(room_id)
        with self._lock:
            if room_id not in self._rooms:
                return False
            del self._rooms[room_id]
            self._write()
            return True

    delete = delete_room
