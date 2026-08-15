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
# The supported media layout is category -> show -> season -> file.  A cold
# lookup may therefore need these three directory refreshes, but it must not
# turn into an unbounded ancestor walk or a whole-drive refresh.
_REFRESH_MAX_CALLS = 3
_REFRESH_COOLDOWN_SECONDS = 5.0
_default_logger = None
_default_logger_ready = False


def _get_default_logger() -> Any:
    """Use the project's normal INFO logger when no caller supplied one."""
    global _default_logger, _default_logger_ready
    if not _default_logger_ready:
        _default_logger_ready = True
        try:
            from utils.configs import MyLogger
            _default_logger = MyLogger()
        except Exception:
            _default_logger = None
    return _default_logger


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

class CloudDrive2Client:
    def __init__(self, origin: str, api_token: str, path_map: Any = None,
                 request_timeout_seconds: float = 2, logger: Any = None, *,
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
        self._logger = logger if logger is not None else _get_default_logger()
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
        return self._resolve(_normalise_posix(cloud_path))

    def resolve_download_url(self, local_path_or_cloud_path: Any) -> str | None:
        return self.resolve_cloud_path(self.map_local_path_to_cloud_path(local_path_or_cloud_path))

    def _log(self, message: str) -> None:
        if self._logger is None:
            return
        try:
            # CD2 lookup/refresh diagnostics are needed at the normal runtime
            # log level.  Keep a debug-only logger usable for small test/fake
            # loggers, but prefer ``info`` when both methods exist.
            method = getattr(self._logger, "info", None) or getattr(self._logger, "debug", None)
            if callable(method):
                method(message)
        except Exception:
            pass

    def _log_refresh_status(self, stage: str, status: str,
                            reason: str | None = None) -> None:
        message = f"cd2 refresh stage={stage} status={status}"
        if reason:
            message += f" reason={reason}"
        self._log(message)

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

    def _resolve(self, cloud_path: str | None) -> str | None:
        if not cloud_path or not self._origin:
            return None
        loaded = self._get_stub()
        if loaded is None:
            self._log("cd2 lookup state=error reason=client_unavailable")
            return None
        stub, pb2 = loaded
        file_info, state = self._find_file(stub, pb2, cloud_path)
        self._log(f"cd2 lookup state={state}")
        if state == "error":
            return None
        if state == "missing":
            # A successful refresh is followed by one and only one recheck.
            # Concurrent callers are serialized by the refresh guard and
            # short cooldown, so they do not create a refresh storm.
            self._refresh_missing_path(stub, pb2, cloud_path)
            file_info, state = self._find_file(stub, pb2, cloud_path)
            self._log(f"cd2 recheck state={state}")
        if state != "found" or _is_directory(file_info):
            if state == "found":
                self._log("cd2 download_url status=invalid reason=directory")
            return None
        return self._download_url_for_file(stub, pb2, cloud_path, file_info)

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
                               file_info: Any) -> str | None:
        size = _field(file_info, "size", None)
        try:
            if size is None or int(size) < 0:
                self._log("cd2 download_url status=invalid reason=file_size")
                return None
        except (TypeError, ValueError, OverflowError):
            self._log("cd2 download_url status=invalid reason=file_size")
            return None
        try:
            url_info = stub.GetDownloadUrlPath(
                _message(pb2, "GetDownloadUrlPathRequest", path=cloud_path,
                         preview=False, lazy_read=False, get_direct_url=False),
                metadata=self._metadata, timeout=self.request_timeout_seconds)
            if _field(url_info, "directUrl") or _field(url_info, "externalUrl"):
                self._log("cd2 download_url status=invalid reason=external_url")
                return None
            url_path = _field(url_info, "downloadUrlPath") or _field(url_info, "placeholder")
            url = self._validate_url(url_path)
            if not url:
                self._log("cd2 download_url status=invalid reason=unsupported_url")
                return None
            self._log("cd2 download_url status=success")
            return url
        except Exception as exc:
            self._log(f"cd2 download_url status=error reason={type(exc).__name__}")
            return None

    def _refresh_missing_path(self, stub: Any, pb2: Any, cloud_path: str) -> bool:
        """Refresh the bounded directory chain after a missing-file lookup."""
        total_timeout = max(self.request_timeout_seconds, 0.05) * _REFRESH_MAX_CALLS
        deadline = self._clock() + total_timeout
        if not self._refresh_guard.acquire(
                timeout=self._remaining_timeout(deadline)):
            self._log_refresh_status("coordination", "failed", "lock_timeout")
            return False
        calls = 0
        target_cooldown_started = False
        try:
            if self._refresh_cooldowns.get(cloud_path, 0.0) > self._clock():
                self._log_refresh_status("target", "cooldown")
                return False
            target_cooldown_started = True
            direct_parent = _parent_path(cloud_path)
            if not direct_parent:
                self._log_refresh_status("direct", "failed", "no_parent")
                return False
            remaining = self._remaining_timeout(deadline)
            if remaining <= 0:
                self._log_refresh_status("direct", "failed", "timeout")
                return False
            parent_info, parent_state = self._find_file(
                stub, pb2, direct_parent, timeout=remaining)
            if parent_state == "error":
                self._log_refresh_status("direct", "failed", "parent_lookup_error")
                return False
            elif parent_state == "found" and _is_directory(parent_info):
                return self._refresh_directory(
                    stub, pb2, direct_parent, deadline, calls, stage="direct")
            elif parent_state == "found":
                self._log_refresh_status("direct", "failed", "parent_not_directory")
            else:
                self._log_refresh_status("direct", "failed", "parent_missing")

            # The direct parent is absent.  Probe the show directory before
            # considering the known category level.  Existing behavior for a
            # present show directory remains the two-call upper -> direct
            # fast path.
            upper_parent = _parent_path(direct_parent)
            if not upper_parent or upper_parent == direct_parent:
                self._log_refresh_status("upper", "failed", "no_parent")
                return False
            remaining = self._remaining_timeout(deadline)
            if remaining <= 0:
                self._log_refresh_status("upper", "failed", "timeout")
                return False
            upper_info, upper_state = self._find_file(
                stub, pb2, upper_parent, timeout=remaining)
            if upper_state == "error":
                self._log_refresh_status("upper", "failed", "parent_lookup_error")
                return False
            if upper_state == "found" and not _is_directory(upper_info):
                self._log_refresh_status("upper", "failed", "parent_not_directory")
                return False
            if upper_state == "found":
                result = self._refresh_directory(
                    stub, pb2, upper_parent, deadline, calls, stage="upper")
                calls += int(result)
                if not result:
                    return False
                return bool(self._refresh_directory(
                    stub, pb2, direct_parent, deadline, calls, stage="direct"))

            self._log_refresh_status("upper", "failed", "parent_missing")

            # The target's Season and show directories are both absent.  The
            # user's mapped layout has one more business directory above them:
            # category -> show -> season.  Only probe that fixed category
            # ancestor; never recurse toward the drive root.
            category_parent = _parent_path(upper_parent)
            if not category_parent or category_parent == upper_parent:
                self._log_refresh_status("category", "failed", "no_parent")
                return False
            remaining = self._remaining_timeout(deadline)
            if remaining <= 0:
                self._log_refresh_status("category", "failed", "timeout")
                return False
            category_info, category_state = self._find_file(
                stub, pb2, category_parent, timeout=remaining)
            if category_state == "error":
                self._log_refresh_status("category", "failed", "parent_lookup_error")
                return False
            if category_state != "found":
                self._log_refresh_status(
                    "category", "failed", f"parent_{category_state}")
                return False
            if not _is_directory(category_info):
                self._log_refresh_status("category", "failed", "parent_not_directory")
                return False

            # Refresh from the known category down to the missing Season.  A
            # successful parent refresh may make the next directory visible,
            # but the calls remain fixed and bounded even when it does not.
            for stage, directory in (
                    ("category", category_parent),
                    ("show", upper_parent),
                    ("season", direct_parent)):
                result = self._refresh_directory(
                    stub, pb2, directory, deadline, calls, stage=stage)
                calls += int(result)
                if not result:
                    return False
            return True
        except Exception as exc:
            self._log_refresh_status("coordination", "failed", type(exc).__name__)
            return False
        finally:
            if target_cooldown_started:
                self._refresh_cooldowns[cloud_path] = (
                    self._clock() + _REFRESH_COOLDOWN_SECONDS)
            self._refresh_guard.release()

    def _refresh_directory(self, stub: Any, pb2: Any, directory: str,
                           deadline: float, calls: int,
                           *, stage: str = "direct") -> bool:
        if calls >= _REFRESH_MAX_CALLS:
            self._log_refresh_status(stage, "failed", "limit")
            return False
        now = self._clock()
        if self._refresh_cooldowns.get(directory, 0.0) > now:
            self._log_refresh_status(stage, "cooldown")
            return False
        remaining = self._bounded_timeout(self._remaining_timeout(deadline))
        if remaining <= 0:
            self._log_refresh_status(stage, "failed", "timeout")
            return False
        self._log_refresh_status(stage, "started")
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
                        self._log_refresh_status(stage, "success")
                        return True
                cancel = getattr(response, "cancel", None)
                if callable(cancel):
                    cancel()
                self._log_refresh_status(stage, "failed", "timeout")
                return False
            self._log_refresh_status(stage, "success")
            return True
        except Exception as exc:
            self._log_refresh_status(stage, "failed", type(exc).__name__)
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

def _load_proto_modules() -> tuple[Any, Any, Any]:
    grpc = importlib.import_module("grpc")
    pb2 = importlib.import_module("utils.clouddrive2_proto.clouddrive_pb2")
    pb2_grpc = importlib.import_module("utils.clouddrive2_proto.clouddrive_pb2_grpc")
    return grpc, pb2, pb2_grpc

__all__ = ["CloudDrive2Client", "CloudDrive2Config"]

