"""Small, failure-tolerant CloudDrive2 gRPC download URL client.

The optional gRPC/protobuf dependency is imported only when a request is made.
"""
from __future__ import annotations
from dataclasses import dataclass
import importlib
import posixpath
import re
import threading
import time
from types import SimpleNamespace
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urljoin, urlparse, urlunparse

try:
    from utils.dependency_bootstrap import ensure_bundled_dependencies
    ensure_bundled_dependencies()
except Exception:
    pass

_PLACEHOLDER_RE = re.compile(r"\{(SCHEME|HOST|PREVIEW)\}")
_TOKEN_PREFIX = re.compile(r"^Bearer\s+", re.I)
_HEADER_NAME_RE = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
_HOP_BY_HOP_HEADERS = frozenset({
    'connection', 'content-length', 'host', 'keep-alive',
    'proxy-authenticate', 'proxy-authorization', 'te', 'trailer',
    'transfer-encoding', 'upgrade',
})
_REFRESH_MAX_CALLS = 2
_REFRESH_COOLDOWN_SECONDS = 5.0
_DIRECT_LINK_RETRY_COOLDOWN_SECONDS = 30.0

def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        wanted = name.casefold().replace("_", "")
        for key, item in value.items():
            if str(key).casefold().replace("_", "") == wanted:
                return item
        return default
    for candidate in (name, name[:1].lower() + name[1:]):
        try:
            return getattr(value, candidate)
        except (AttributeError, TypeError):
            continue
    wanted = name.casefold().replace("_", "")
    for candidate in dir(value):
        if candidate.casefold().replace("_", "") == wanted:
            try:
                return getattr(value, candidate)
            except (AttributeError, TypeError):
                pass
    return default


def _optional_field(value: Any, name: str) -> Any:
    """Read an optional protobuf field without treating absent as zero."""
    has_field = getattr(value, 'HasField', None)
    if callable(has_field):
        candidates = (name, name[:1].lower() + name[1:])
        for candidate in candidates:
            try:
                if not has_field(candidate):
                    return None
                break
            except (AttributeError, TypeError, ValueError):
                continue
    return _field(value, name)


def _parse_expires_in(value: Any) -> tuple[bool, int | None]:
    if value is None:
        return True, None
    try:
        expires_in = int(value)
    except (TypeError, ValueError, OverflowError):
        return False, None
    return (expires_in > 0, expires_in) if expires_in > 0 else (False, None)


def _normalise_header_value(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or '\r' in text or '\n' in text or len(text) > 8192:
        return None
    return text


def _normalise_additional_headers(value: Any) -> tuple[tuple[str, str], ...]:
    if value is None:
        return ()
    try:
        items = value.items()
    except AttributeError:
        return ()
    result: list[tuple[str, str]] = []
    for raw_name, raw_value in items:
        name = str(raw_name).strip()
        header_value = _normalise_header_value(raw_value)
        if (not name or not _HEADER_NAME_RE.fullmatch(name)
                or name.casefold() in _HOP_BY_HOP_HEADERS
                or header_value is None):
            continue
        result = [item for item in result if item[0].casefold() != name.casefold()]
        result.append((name, header_value))
    return tuple(result)

def _normalise_posix(path: Any) -> str | None:
    if path is None:
        return None
    value = str(path).strip().replace("\\", "/")
    if not value:
        return "/"
    if not value.startswith("/"):
        value = "/" + value
    normal = posixpath.normpath(value)
    if normal == ".":
        normal = "/"
    if normal == ".." or normal.startswith("../"):
        return None
    return normal if normal.startswith("/") else "/" + normal

def _normalise_local(path: Any) -> str | None:
    if path is None:
        return None
    value = str(path).strip().replace("\\", "/")
    if not value:
        return None
    value = re.sub(r"/+", "/", value)
    drive = ""
    if re.match(r"^[A-Za-z]:", value):
        drive, value = value[:2], value[2:]
    value = posixpath.normpath(value or "/")
    if value == ".":
        value = "/"
    if not value.startswith("/"):
        value = "/" + value
    return (drive + value).rstrip("/") or (drive + "/")

def _prefix_boundary(path: str, prefix: str) -> bool:
    folded_path, folded_prefix = path.casefold(), prefix.casefold().rstrip("/")
    return folded_path == folded_prefix or folded_path.startswith(folded_prefix + "/")

def _parse_path_map(path_map: Any) -> list[tuple[str, str]]:
    if not path_map:
        return []
    pairs: list[tuple[Any, Any]] = []
    if isinstance(path_map, str):
        match = re.split(r"\s*(?:=>|->|=)\s*", path_map, maxsplit=1)
        pairs.append((match[0], match[1] if len(match) == 2 else "/"))
    elif isinstance(path_map, Sequence) and not isinstance(path_map, (bytes, bytearray)):
        if len(path_map) == 2 and all(isinstance(x, (str, bytes)) for x in path_map):
            pairs.append((path_map[0], path_map[1]))
        else:
            for item in path_map:
                if isinstance(item, str):
                    parts = re.split(r"\s*(?:=>|->|=)\s*", item, maxsplit=1)
                    if len(parts) == 2:
                        pairs.append((parts[0], parts[1]))
                elif isinstance(item, Sequence) and len(item) >= 2:
                    pairs.append((item[0], item[1]))
    result: list[tuple[str, str]] = []
    for local, cloud in pairs:
        local_norm, cloud_norm = _normalise_local(local), _normalise_posix(cloud)
        if local_norm and cloud_norm:
            result.append((local_norm, cloud_norm))
    return sorted(result, key=lambda pair: len(pair[0]), reverse=True)

def _origin_parts(origin: Any) -> tuple[str, str, str] | None:
    parsed = urlparse(str(origin or "").strip())
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
        return None
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        return None
    base_path = _normalise_posix(parsed.path or "/")
    if base_path is None:
        return None
    host = parsed.hostname.casefold()
    try:
        port = parsed.port
    except ValueError:
        return None
    netloc = host if port is None else f"{host}:{port}"
    return parsed.scheme.casefold(), netloc, base_path.rstrip("/")

@dataclass(frozen=True)
class CloudDrive2Config:
    origin: str
    api_token: str
    path_map: Any = None
    request_timeout_seconds: float = 2.0
    get_direct_url: bool = False


@dataclass(frozen=True)
class CloudDrive2DownloadTarget:
    """Validated URL plus metadata required by the local gateway."""

    url: str
    is_direct: bool = False
    expires_in: int | None = None
    user_agent: str | None = None
    additional_headers: tuple[tuple[str, str], ...] = ()
    fallback_url: str | None = None

class CloudDrive2Client:
    def __init__(self, origin: str, api_token: str, path_map: Any = None,
                 request_timeout_seconds: float = 2, logger: Any = None, *,
                 get_direct_url: bool = False,
                 _stub_factory: Callable[..., Any] | None = None,
                 _channel_factory: Callable[..., Any] | None = None,
                 _proto_loader: Callable[[], tuple[Any, Any, Any]] | None = None,
                 _clock: Callable[[], float] | None = None) -> None:
        self.origin = str(origin or "").strip().rstrip("/")
        self._origin = _origin_parts(self.origin)
        if self._origin:
            origin_scheme, origin_host, origin_path = self._origin
            self.origin = f"{origin_scheme}://{origin_host}{origin_path}"
        token = _TOKEN_PREFIX.sub("", str(api_token or "").strip()).strip()
        self._token = token
        self._metadata = (("authorization", f"Bearer {token}"),) if token else ()
        try:
            self.request_timeout_seconds = max(0.05, float(request_timeout_seconds))
        except (TypeError, ValueError):
            self.request_timeout_seconds = 2.0
        self._path_map = _parse_path_map(path_map)
        self.get_direct_url = bool(get_direct_url)
        self._logger = logger
        self._stub_factory = _stub_factory
        self._channel_factory = _channel_factory
        self._proto_loader = _proto_loader or _load_proto_modules
        self._clock = _clock or time.monotonic
        self._stub = None
        self._channel = None
        # Directory refresh is only a recovery path for a first lookup miss.
        # One lock serializes refreshes and coalesces concurrent probes.
        self._refresh_guard = threading.Lock()
        self._refresh_cooldowns: dict[str, float] = {}
        # Direct-link enabling is a CD2 configuration side effect.  Coalesce
        # concurrent attempts and avoid retrying a missing permission on every
        # Range request, while allowing a later retry after the user fixes the
        # token or CD2 configuration.
        self._direct_link_state_lock = threading.Lock()
        self._direct_link_state: dict[tuple[str, str], float] = {}

    def map_local_path_to_cloud_path(self, local_path: Any) -> str | None:
        local = _normalise_local(local_path)
        if not local:
            return None
        for local_prefix, cloud_prefix in self._path_map:
            if not _prefix_boundary(local, local_prefix):
                continue
            suffix = local[len(local_prefix.rstrip("/")):].replace("\\", "/").lstrip("/")
            joined = posixpath.join(cloud_prefix, suffix) if suffix else cloud_prefix
            return _normalise_posix(joined)
        return None if self._path_map else _normalise_posix(local_path)

    def resolve_cloud_path(self, cloud_path: Any) -> str | None:
        target = self.resolve_cloud_path_target(cloud_path)
        return target.url if target else None

    def resolve_cloud_path_target(self, cloud_path: Any) -> CloudDrive2DownloadTarget | None:
        return self._resolve(_normalise_posix(cloud_path))

    def resolve_download_url(self, local_path_or_cloud_path: Any) -> str | None:
        target = self.resolve_download_target(local_path_or_cloud_path)
        return target.url if target else None

    def resolve_download_target(self, local_path_or_cloud_path: Any) -> CloudDrive2DownloadTarget | None:
        return self.resolve_cloud_path_target(self.map_local_path_to_cloud_path(local_path_or_cloud_path))

    def _log(self, message: str) -> None:
        if self._logger is None:
            return
        try:
            method = getattr(self._logger, "debug", None) or getattr(self._logger, "info", None)
            if method:
                method(message)
        except Exception:
            pass

    def _get_stub(self) -> tuple[Any, Any] | None:
        if self._stub is not None:
            try:
                return self._stub, self._proto_loader()[1]
            except Exception:
                return self._stub, None
        if not self._origin or not self._token:
            return None
        try:
            if self._stub_factory:
                try:
                    _, pb2, _ = self._proto_loader()
                except Exception:
                    pb2 = None
                try:
                    stub = self._stub_factory(None, pb2)
                except TypeError:
                    try:
                        stub = self._stub_factory(None)
                    except TypeError:
                        stub = self._stub_factory()
                self._stub = stub
                return stub, pb2
            grpc, pb2, pb2_grpc = self._proto_loader()
            if self._channel_factory:
                channel = self._channel_factory(self.origin)
            elif self._origin[0] == "https":
                channel = grpc.secure_channel(self._origin[1], grpc.ssl_channel_credentials())
            else:
                channel = grpc.insecure_channel(self._origin[1])
            self._channel = channel
            self._stub = pb2_grpc.CloudDriveFileSrvStub(channel)
            return self._stub, pb2
        except Exception as exc:
            self._log(f"CloudDrive2 disabled: optional gRPC unavailable ({type(exc).__name__})")
            return None

    def _resolve(self, cloud_path: str | None) -> CloudDrive2DownloadTarget | None:
        if not cloud_path or not self._origin:
            return None
        loaded = self._get_stub()
        if loaded is None:
            return None
        stub, pb2 = loaded
        file_info, state = self._find_file(stub, pb2, cloud_path)
        if state == "error":
            return None
        if state == "missing":
            # A successful refresh is followed by one and only one recheck.
            # Concurrent callers are serialized by the refresh guard and
            # short cooldown, so they do not create a refresh storm.
            self._refresh_missing_path(stub, pb2, cloud_path)
            file_info, state = self._find_file(stub, pb2, cloud_path)
        if state != "found" or _is_directory(file_info):
            return None
        if self.get_direct_url:
            self._ensure_direct_link_enabled(stub, pb2, cloud_path, file_info)
        return self._download_url_for_file(stub, pb2, cloud_path, file_info)

    def _ensure_direct_link_enabled(self, stub: Any, pb2: Any,
                                    cloud_path: str, file_info: Any) -> bool:
        """Enable CD2's per-cloud direct-link switch when the token permits it.

        The operation is deliberately best-effort.  A missing permission,
        unsupported provider, old CD2 API, or transient RPC error must leave
        the normal ``downloadUrlPath`` path available to playback.
        """
        identity = self._cloud_api_identity(stub, pb2, cloud_path, file_info)
        if identity is None:
            return False
        cloud_name, user_name = identity
        key = (cloud_name, user_name)
        now = self._clock()
        with self._direct_link_state_lock:
            retry_at = self._direct_link_state.get(key)
            if retry_at is not None and retry_at > now:
                return retry_at == float("inf")
            self._direct_link_state[key] = now + _DIRECT_LINK_RETRY_COOLDOWN_SECONDS

        request = _message(
            pb2, "GetCloudAPIConfigRequest",
            cloudName=cloud_name, userName=user_name)
        try:
            config = stub.GetCloudAPIConfig(
                request, metadata=self._metadata, timeout=self.request_timeout_seconds)
            if not bool(_optional_field(config, "supportDirectDownloadUrl")):
                self._log("CloudDrive2 direct-link provider capability unavailable")
                return False
            if bool(_field(config, "supportDirectLink", False)):
                with self._direct_link_state_lock:
                    self._direct_link_state[key] = float("inf")
                return True

            # Mutate the response object so all other mutable per-cloud
            # settings are preserved.  CD2 documents the capability/limit
            # fields as read-only and ignores them on SetCloudAPIConfig.
            setattr(config, "supportDirectLink", True)
            stub.SetCloudAPIConfig(
                _message(pb2, "SetCloudAPIConfigRequest",
                         cloudName=cloud_name, userName=user_name, config=config),
                metadata=self._metadata, timeout=self.request_timeout_seconds)
            verified = stub.GetCloudAPIConfig(
                request, metadata=self._metadata, timeout=self.request_timeout_seconds)
            if bool(_field(verified, "supportDirectLink", False)):
                with self._direct_link_state_lock:
                    self._direct_link_state[key] = float("inf")
                self._log("CloudDrive2 direct-link enabled")
                return True
            self._log("CloudDrive2 direct-link enable was not confirmed")
        except Exception as exc:
            # Do not include the RPC text: CD2 errors can contain URLs or
            # account details.  Playback continues through the fallback path.
            self._log(f"CloudDrive2 direct-link auto-enable failed ({type(exc).__name__})")
        return False

    def _cloud_api_identity(self, stub: Any, pb2: Any, cloud_path: str,
                            file_info: Any) -> tuple[str, str] | None:
        cloud_api = _field(file_info, "CloudAPI")
        cloud_name = str(_field(cloud_api, "name", "") or "").strip()
        user_name = str(_field(cloud_api, "userName", "") or "").strip()
        if not cloud_name:
            normal = _normalise_posix(cloud_path) or ""
            parts = [part for part in normal.split("/") if part]
            cloud_name = parts[0] if parts else ""
        if cloud_name and user_name:
            return cloud_name, user_name
        if not cloud_name:
            return None
        try:
            apis = stub.GetAllCloudApis(
                _empty_message(pb2), metadata=self._metadata,
                timeout=self.request_timeout_seconds)
            for api in getattr(apis, "apis", ()):
                if str(_field(api, "name", "") or "").strip() != cloud_name:
                    continue
                user_name = str(_field(api, "userName", "") or "").strip()
                if user_name:
                    return cloud_name, user_name
        except Exception as exc:
            self._log(f"CloudDrive2 direct-link identity lookup failed ({type(exc).__name__})")
        return None

    def _find_file(self, stub: Any, pb2: Any, cloud_path: str,
                   timeout: float | None = None) -> tuple[Any, str]:
        """Return (response, state) without leaking lookup errors to playback."""
        call_timeout = self._bounded_timeout(timeout)
        if call_timeout <= 0:
            return None, "error"
        try:
            file_info = stub.FindFileByPath(
                _message(pb2, "FindFileByPathRequest", parentPath="", path=cloud_path),
                metadata=self._metadata, timeout=call_timeout)
        except Exception as exc:
            if _is_not_found_error(exc):
                return None, "missing"
            self._log(f"CloudDrive2 file lookup failed ({type(exc).__name__})")
            return None, "error"
        if file_info is None or not _field(file_info, "fullPathName"):
            return None, "missing"
        return file_info, "found"

    def _download_url_for_file(self, stub: Any, pb2: Any, cloud_path: str,
                               file_info: Any) -> CloudDrive2DownloadTarget | None:
        size = _field(file_info, "size", None)
        try:
            if size is None or int(size) < 0:
                return None
        except (TypeError, ValueError, OverflowError):
            return None
        try:
            request_values = {
                'path': cloud_path,
                'preview': False,
                'lazy_read': False,
                'get_direct_url': self.get_direct_url,
            }
            request_get_direct_url = self.get_direct_url
            try:
                request = _message(pb2, "GetDownloadUrlPathRequest", **request_values)
            except Exception as exc:
                if self.get_direct_url:
                    self._log(f"CloudDrive2 direct-url request unavailable ({type(exc).__name__})")
                request_values.pop('get_direct_url', None)
                request_get_direct_url = False
                request = _message(pb2, "GetDownloadUrlPathRequest", **request_values)
            url_info = stub.GetDownloadUrlPath(
                request, metadata=self._metadata, timeout=self.request_timeout_seconds)
            expiry_ok, expires_in = _parse_expires_in(
                _optional_field(url_info, "expiresIn"))
            direct_url = _field(url_info, "directUrl")
            if request_get_direct_url and direct_url and expiry_ok:
                direct_url = self._validate_direct_url(direct_url)
                if direct_url:
                    fallback_url = self._validate_url(
                        _field(url_info, "downloadUrlPath") or _field(url_info, "placeholder"))
                    return CloudDrive2DownloadTarget(
                        url=direct_url,
                        is_direct=True,
                        expires_in=expires_in,
                        user_agent=_normalise_header_value(_field(url_info, "userAgent")),
                        additional_headers=_normalise_additional_headers(
                            _field(url_info, "additionalHeaders")),
                        fallback_url=fallback_url,
                    )
                self._log("CloudDrive2 direct URL rejected")
            elif request_get_direct_url and direct_url and not expiry_ok:
                self._log("CloudDrive2 direct URL has invalid expiry")
            if not request_get_direct_url and (
                    _field(url_info, "directUrl") or _field(url_info, "externalUrl")):
                return None
            url_path = _field(url_info, "downloadUrlPath") or _field(url_info, "placeholder")
            url_path = self._validate_url(url_path)
            return CloudDrive2DownloadTarget(url=url_path) if url_path else None
        except Exception as exc:
            self._log(f"CloudDrive2 URL resolution failed ({type(exc).__name__})")
            return None

    @staticmethod
    def _validate_direct_url(value: Any) -> str | None:
        if not isinstance(value, str) or not value.strip():
            return None
        value = value.strip()
        if '\r' in value or '\n' in value:
            return None
        parsed = urlparse(value)
        if (parsed.scheme.casefold() not in {"http", "https"}
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.fragment):
            return None
        try:
            parsed.port
        except ValueError:
            return None
        return urlunparse(parsed)

    def _refresh_missing_path(self, stub: Any, pb2: Any, cloud_path: str) -> bool:
        """Refresh at most the direct parent and its parent after a miss."""
        total_timeout = max(self.request_timeout_seconds, 0.05) * _REFRESH_MAX_CALLS
        deadline = self._clock() + total_timeout
        if not self._refresh_guard.acquire(
                timeout=self._remaining_timeout(deadline)):
            return False
        calls = 0
        target_cooldown_started = False
        try:
            if self._refresh_cooldowns.get(cloud_path, 0.0) > self._clock():
                return False
            target_cooldown_started = True
            direct_parent = _parent_path(cloud_path)
            if not direct_parent:
                return False
            remaining = self._remaining_timeout(deadline)
            if remaining <= 0:
                return False
            parent_info, parent_state = self._find_file(
                stub, pb2, direct_parent, timeout=remaining)
            if parent_state == "error":
                return False
            if parent_state == "found" and _is_directory(parent_info):
                return self._refresh_directory(
                    stub, pb2, direct_parent, deadline, calls)

            # The direct parent is absent.  One level up is the hard limit;
            # never walk to a higher ancestor or refresh the whole drive.
            upper_parent = _parent_path(direct_parent)
            if not upper_parent or upper_parent == direct_parent:
                return False
            remaining = self._remaining_timeout(deadline)
            if remaining <= 0:
                return False
            upper_info, upper_state = self._find_file(
                stub, pb2, upper_parent, timeout=remaining)
            if upper_state != "found" or not _is_directory(upper_info):
                return False
            result = self._refresh_directory(
                stub, pb2, upper_parent, deadline, calls)
            calls += int(result)
            if not result:
                return False
            return bool(self._refresh_directory(
                stub, pb2, direct_parent, deadline, calls))
        except Exception as exc:
            self._log(f"CloudDrive2 refresh coordination failed ({type(exc).__name__})")
            return False
        finally:
            if target_cooldown_started:
                self._refresh_cooldowns[cloud_path] = (
                    self._clock() + _REFRESH_COOLDOWN_SECONDS)
            self._refresh_guard.release()

    def _refresh_directory(self, stub: Any, pb2: Any, directory: str,
                           deadline: float, calls: int) -> bool:
        if calls >= _REFRESH_MAX_CALLS:
            return False
        now = self._clock()
        if self._refresh_cooldowns.get(directory, 0.0) > now:
            return False
        remaining = self._bounded_timeout(self._remaining_timeout(deadline))
        if remaining <= 0:
            return False
        try:
            response = stub.GetSubFiles(
                _message(pb2, "ListSubFileRequest", path=directory,
                         forceRefresh=True, checkExpires=True),
                metadata=self._metadata, timeout=remaining)
            # GetSubFiles is unary-stream. Consume it to EOF without retaining
            # entries, while the RPC deadline bounds the whole refresh.
            if response is not None:
                iterator = iter(response)
                while self._remaining_timeout(deadline) > 0:
                    try:
                        next(iterator)
                    except StopIteration:
                        return True
                cancel = getattr(response, "cancel", None)
                if callable(cancel):
                    cancel()
                return False
            return True
        except Exception as exc:
            self._log(f"CloudDrive2 directory refresh failed ({type(exc).__name__})")
            return False
        finally:
            self._refresh_cooldowns[directory] = (
                self._clock() + _REFRESH_COOLDOWN_SECONDS)

    def _bounded_timeout(self, timeout: float | None) -> float:
        value = self.request_timeout_seconds if timeout is None else timeout
        try:
            return max(0.0, min(self.request_timeout_seconds, float(value)))
        except (TypeError, ValueError):
            return self.request_timeout_seconds

    def _remaining_timeout(self, deadline: float) -> float:
        return max(0.0, deadline - self._clock())

    def _validate_url(self, value: Any) -> str | None:
        if not isinstance(value, str) or not value.strip():
            return None
        scheme, host, _ = self._origin or ("", "", "")
        replaced = _PLACEHOLDER_RE.sub(lambda match: {"SCHEME": scheme, "HOST": host, "PREVIEW": "false"}[match.group(1)], value.strip())
        if "{" in replaced or "}" in replaced or ".." in urlparse(replaced).path.split("/"):
            return None
        parsed = urlparse(replaced)
        if parsed.scheme and parsed.scheme.casefold() not in {"http", "https"}:
            return None
        if parsed.scheme or parsed.netloc:
            if parsed.scheme.casefold() != scheme or not parsed.hostname or parsed.hostname.casefold() != host.split(":", 1)[0]:
                return None
            expected_port = int(host.rsplit(":", 1)[1]) if ":" in host else None
            if parsed.port != expected_port:
                return None
            final = replaced
        else:
            final = urljoin(self.origin.rstrip("/") + "/", replaced.lstrip("/"))
        check = urlparse(final)
        if check.scheme.casefold() != scheme or not check.hostname or check.hostname.casefold() != host.split(":", 1)[0]:
            return None
        return urlunparse(check)

def _is_directory(value: Any) -> bool:
    if bool(_field(value, "isDirectory", False)):
        return True
    file_type = _field(value, "fileType", None)
    if isinstance(file_type, str):
        return file_type.casefold() in {"directory", "dir"}
    return file_type == 0


def _parent_path(path: str) -> str | None:
    normal = _normalise_posix(path)
    if not normal or normal == "/":
        return None
    parent = posixpath.dirname(normal) or "/"
    return _normalise_posix(parent)


def _is_not_found_error(exc: BaseException) -> bool:
    if isinstance(exc, FileNotFoundError):
        return True
    code = getattr(exc, "code", None)
    try:
        code = code() if callable(code) else code
    except Exception:
        code = None
    code_name = getattr(code, "name", "") or str(code or "")
    if "NOT_FOUND" in str(code_name).upper().replace("-", "_"):
        return True
    message = str(exc).casefold()
    return any(token in message for token in (
        "not found", "notfound", "no such file", "不存在", "找不到",
    ))

def _message(pb2: Any, name: str, **values: Any) -> Any:
    cls = getattr(pb2, name, None) if pb2 is not None else None
    if cls is not None:
        try:
            return cls(**values)
        except (TypeError, ValueError):
            obj = cls()
            for key, value in values.items():
                setattr(obj, key, value)
            return obj
    return SimpleNamespace(**values)


def _empty_message(pb2: Any) -> Any:
    cls = getattr(pb2, "Empty", None) if pb2 is not None else None
    if cls is not None:
        return cls()
    try:
        from google.protobuf.empty_pb2 import Empty
        return Empty()
    except Exception:
        return SimpleNamespace()

def _load_proto_modules() -> tuple[Any, Any, Any]:
    grpc = importlib.import_module("grpc")
    pb2 = importlib.import_module("utils.clouddrive2_proto.clouddrive_pb2")
    pb2_grpc = importlib.import_module("utils.clouddrive2_proto.clouddrive_pb2_grpc")
    return grpc, pb2, pb2_grpc

__all__ = ["CloudDrive2Client", "CloudDrive2Config", "CloudDrive2DownloadTarget"]

