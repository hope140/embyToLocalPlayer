"""Small, failure-tolerant CloudDrive2 gRPC download URL client.

The optional gRPC/protobuf dependency is imported only when a request is made.
"""
from __future__ import annotations
from dataclasses import dataclass
import importlib
import posixpath
import re
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
                 _proto_loader: Callable[[], tuple[Any, Any, Any]] | None = None) -> None:
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
        self._logger = logger
        self._stub_factory = _stub_factory
        self._channel_factory = _channel_factory
        self._proto_loader = _proto_loader or _load_proto_modules
        self._stub = None
        self._channel = None

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

    def _resolve(self, cloud_path: str | None) -> str | None:
        if not cloud_path or not self._origin:
            return None
        loaded = self._get_stub()
        if loaded is None:
            return None
        stub, pb2 = loaded
        try:
            file_info = stub.FindFileByPath(_message(pb2, "FindFileByPathRequest", parentPath="", path=cloud_path),
                metadata=self._metadata, timeout=self.request_timeout_seconds)
            if _is_directory(file_info) or not _field(file_info, "fullPathName"):
                return None
            size = _field(file_info, "size", None)
            if size is None or int(size) < 0:
                return None
            url_info = stub.GetDownloadUrlPath(_message(pb2, "GetDownloadUrlPathRequest", path=cloud_path,
                preview=False, lazy_read=False, get_direct_url=False), metadata=self._metadata,
                timeout=self.request_timeout_seconds)
            if _field(url_info, "directUrl") or _field(url_info, "externalUrl"):
                return None
            url_path = _field(url_info, "downloadUrlPath") or _field(url_info, "placeholder")
            return self._validate_url(url_path)
        except Exception as exc:
            self._log(f"CloudDrive2 URL resolution failed ({type(exc).__name__})")
            return None

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

