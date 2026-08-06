"""Short-lived local HTTP gateway for CloudDrive2-backed STRM playback.

The gateway intentionally keeps the URL opaque.  A player receives only a
random nonce; the local path and CD2 credentials remain in process memory.
When a request arrives we resolve the CD2 URL just-in-time.  Any resolution
failure falls back to the original mounted file so enabling this feature never
breaks the existing disk mode.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
import secrets
import threading
import time
from typing import Callable, Optional
from urllib.parse import quote

from utils.clouddrive2_client import CloudDrive2Client
from utils.configs import configs


@dataclass(frozen=True)
class GatewayEntry:
    local_path: str
    expires_at: float


class CloudDrive2Gateway:
    def __init__(self, *, clock: Callable[[], float] = time.time,
                 token_urlsafe: Callable[[int], str] = secrets.token_urlsafe,
                 max_entries: int = 500, ttl_seconds: int = 24 * 60 * 60) -> None:
        self._clock = clock
        self._token_urlsafe = token_urlsafe
        self.max_entries = max(1, int(max_entries))
        self.ttl_seconds = max(60, int(ttl_seconds))
        self._lock = threading.RLock()
        self._entries: dict[str, GatewayEntry] = {}
        self._base_url = ''
        self._client_key = None
        self._client: Optional[CloudDrive2Client] = None

    def configure(self, base_url: str) -> None:
        self._base_url = str(base_url or '').rstrip('/')

    def _client_from_config(self) -> Optional[CloudDrive2Client]:
        try:
            enabled = configs.raw.getboolean('clouddrive2', 'enable', fallback=False)
        except (ValueError, TypeError):
            enabled = False
        token = os.environ.get('ETLP_CLOUDDRIVE2_TOKEN', '').strip()
        try:
            token = token or configs.raw.get('clouddrive2', 'api_token', fallback='').strip()
            origin = configs.raw.get('clouddrive2', 'origin', fallback='http://127.0.0.1:19798').strip()
            path_map = configs.raw.get('clouddrive2', 'path_map', fallback='').strip()
            timeout = configs.raw.getfloat('clouddrive2', 'request_timeout_seconds', fallback=2)
        except (ValueError, TypeError):
            return None
        # The gateway receives a local mounted path.  Requiring an explicit
        # mapping prevents a Windows drive path from being sent to CD2 as if
        # it were already a cloud path when the user forgot to configure it.
        if not enabled or not token or not path_map:
            return None
        key = (origin, token, path_map, timeout)
        with self._lock:
            if key != self._client_key:
                self._client = CloudDrive2Client(
                    origin, token, path_map=path_map, request_timeout_seconds=timeout,
                    logger=getattr(configs, 'logger', None))
                self._client_key = key
            return self._client

    def register(self, local_path: str) -> Optional[str]:
        if not self._base_url or not local_path:
            return None
        now = self._clock()
        with self._lock:
            self._prune(now)
            if len(self._entries) >= self.max_entries:
                oldest = min(self._entries, key=lambda key: self._entries[key].expires_at)
                self._entries.pop(oldest, None)
            for _ in range(3):
                nonce = self._token_urlsafe(24)
                if nonce not in self._entries:
                    self._entries[nonce] = GatewayEntry(str(local_path), now + self.ttl_seconds)
                    return f'{self._base_url}/cd2/{quote(nonce, safe="")}'
        return None

    def maybe_register(self, local_path: str) -> Optional[str]:
        """Return an opaque gateway URL when CD2 is configured and resolvable."""
        if not self._base_url or not local_path:
            return None
        client = self._client_from_config()
        if client is None:
            return None
        # A Windows drive/UNC path is local-only unless an explicit path map
        # translates it to a CloudDrive2 path.  Without this guard the client
        # intentionally preserves unmapped POSIX paths, which is unsafe for
        # drive-letter paths.
        local_text = str(local_path)
        is_windows_path = (len(local_text) >= 2 and local_text[1] == ':') or local_text.startswith(('\\\\', '//'))
        if is_windows_path and not getattr(client, '_path_map', None):
            return None
        # Do not probe the mounted file here: CD2 lookup is the feature's
        # fast path, while the actual request still has a local fallback.
        if not client.map_local_path_to_cloud_path(local_path):
            return None
        return self.register(local_path)

    def pop_entry(self, nonce: str) -> Optional[GatewayEntry]:
        now = self._clock()
        with self._lock:
            self._prune(now)
            entry = self._entries.get(nonce)
            if entry is None or entry.expires_at <= now:
                return None
            return entry

    def resolve_entry(self, entry: GatewayEntry) -> Optional[str]:
        client = self._client_from_config()
        return client.resolve_download_url(entry.local_path) if client else None

    def _prune(self, now: float) -> None:
        expired = [key for key, entry in self._entries.items() if entry.expires_at <= now]
        for key in expired:
            self._entries.pop(key, None)


gateway = CloudDrive2Gateway()


def configure_gateway(base_url: str) -> None:
    gateway.configure(base_url)


def maybe_register_strm_cd2_url(local_path: str) -> Optional[str]:
    return gateway.maybe_register(local_path)


__all__ = [
    'CloudDrive2Gateway', 'GatewayEntry', 'configure_gateway', 'gateway',
    'maybe_register_strm_cd2_url',
]
