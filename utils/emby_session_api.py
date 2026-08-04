"""Small Emby session client used by the optional watch-together control path.

The regular playback code intentionally keeps using the browser device id.  A
watch-together client is a second Emby device, however, so this module builds a
new id from the browser device id and the playback session id and uses explicit
headers for every request.  In particular, request headers captured from the
browser are never forwarded here.
"""

import hashlib
import urllib.parse

from utils.configs import MyLogger, configs
from utils.net_tools import requests_urllib


logger = MyLogger()

CONTROL_DEVICE_PREFIX = "etlp-wt-"
CONTROL_DEVICE_VERSION = "1.0"
# Emby Web access tokens carry this authentication identity.  Keep the
# control DeviceId independent (it is still derived per playback), but use the
# token's app/device names so HTTP check-ins and the token-bound WebSocket
# resolve to one server-side Session.
DEFAULT_AUTH_CLIENT_NAME = "Emby Web"
DEFAULT_AUTH_DEVICE_NAME = "embyToLocalPlayer"
SUPPORTED_COMMANDS = ("Pause", "Unpause", "Seek", "DisplayMessage")


def _normalise_path(value):
    """Return a URL path without a query/fragment and with one leading slash."""

    value = str(value or "").strip()
    if not value:
        return ""
    # ``urlsplit`` also keeps paths passed as ``/proxy?query`` predictable.
    value = urllib.parse.urlsplit(value).path
    if not value or value == "/":
        return ""
    return "/" + "/".join(part for part in value.split("/") if part)


def _mapping_value(mapping, name, default=None):
    """Read a mapping field using Emby's case-insensitive JSON conventions."""

    if not isinstance(mapping, dict):
        return default
    expected = str(name).lower()
    for key, value in mapping.items():
        if str(key).lower() == expected:
            return value
    return default


def derive_control_device_id(browser_device_id, play_session_id, prefix=CONTROL_DEVICE_PREFIX):
    """Return a stable, short device id for one playback control session.

    Emby device ids are opaque strings.  A SHA-256 digest keeps the id stable
    for a given browser/session pair while making two simultaneous playback
    sessions on the same browser distinct.  The result is deliberately kept
    below the commonly used 32 character device-id limit.
    """

    browser_device_id = "" if browser_device_id is None else str(browser_device_id)
    play_session_id = "" if play_session_id is None else str(play_session_id)
    digest = hashlib.sha256(
        (browser_device_id + "\x00" + play_session_id).encode("utf-8")
    ).hexdigest()
    # 8 prefix characters + 24 hexadecimal characters = 32 characters.
    return (str(prefix) + digest[:24])[:32]


def watch_together_enabled(data=None, override=None):
    """Read the opt-in switch without making a missing section an error.

    ``override`` and the explicitly named ``watch_together_enabled`` data field
    are useful to tests and to callers that already resolved configuration.
    The user-facing switch is always ``[watch_together] enable``.
    """

    if override is None and isinstance(data, dict):
        override = data.get("watch_together_enabled")
    if override is not None:
        if isinstance(override, str):
            return override.strip().lower() in ("1", "true", "yes", "on")
        return bool(override)
    try:
        return configs.raw.getboolean("watch_together", "enable", fallback=False)
    except (ValueError, TypeError, AttributeError):
        return False


class EmbySessionError(RuntimeError):
    """Raised when an Emby session operation cannot be completed."""


class EmbySessionApi:
    """HTTP helpers for one independent Emby playback/control session."""

    def __init__(self, data=None, *, request_func=None, control_device_id=None,
                 client_name=DEFAULT_AUTH_CLIENT_NAME,
                 device_name=DEFAULT_AUTH_DEVICE_NAME,
                 client_version=CONTROL_DEVICE_VERSION, scheme="", netloc="",
                 api_key="", user_id="", item_id="", media_source_id="",
                 play_session_id="", browser_device_id="", host=""):
        data = data or {}
        host = str(data.get("host", data.get("server_address", host)) or host).strip()
        configured_scheme = data.get("scheme", scheme) or scheme
        configured_netloc = data.get("netloc", netloc) or netloc
        configured_path = data.get(
            "server_path",
            data.get("base_path", data.get("path", "")),
        ) or ""

        # Keep a genuine reverse-proxy prefix while still accepting the
        # historical ``scheme`` + ``netloc`` fields.  The latter occasionally
        # contain a path even though the name says ``netloc``.
        if host:
            parsed_host = urllib.parse.urlsplit(
                host if "://" in host else f"//{host}"
            )
            if parsed_host.scheme and not data.get("scheme") and not scheme:
                configured_scheme = parsed_host.scheme
            if parsed_host.netloc and not data.get("netloc") and not netloc:
                configured_netloc = parsed_host.netloc
            if parsed_host.path and not configured_path:
                configured_path = parsed_host.path

        parsed_netloc = urllib.parse.urlsplit(
            f"//{str(configured_netloc or '').strip()}"
        )
        if parsed_netloc.netloc:
            configured_netloc = parsed_netloc.netloc
            if parsed_netloc.path and not configured_path:
                configured_path = parsed_netloc.path

        self.scheme = str(configured_scheme).strip().rstrip("/")
        self.scheme = self.scheme[:-3] if self.scheme.endswith("://") else self.scheme
        self.scheme = self.scheme.rstrip(":").lower()
        self.netloc = str(configured_netloc or "").strip().strip("/")
        self.server_path = _normalise_path(configured_path)
        self.api_key = str(data.get("api_key", api_key) or api_key)
        self.user_id = str(data.get("user_id", user_id) or user_id)
        self.item_id = str(data.get("item_id", item_id) or item_id)
        self.media_source_id = str(
            data.get("media_source_id", media_source_id) or media_source_id
        )
        self.play_session_id = str(
            data.get("play_session_id", play_session_id) or play_session_id
        )
        self.browser_device_id = str(
            data.get("browser_device_id", data.get("device_id", browser_device_id))
            or browser_device_id
        )
        self.control_device_id = str(
            control_device_id
            or data.get("watch_together_device_id")
            or data.get("control_device_id")
            or derive_control_device_id(self.browser_device_id, self.play_session_id)
        )
        # Explicit auth identity fields are useful for Emby-compatible
        # deployments whose token was issued by a branded client.  They are
        # values only (never browser headers or credentials), and all HTTP/WS
        # paths below consume the same resolved tuple.
        self.client_name = str(
            data.get(
                "auth_client_name",
                data.get("client_name", data.get("app_name", client_name)),
            )
            or client_name
        )
        self.device_name = str(
            data.get("auth_device_name", data.get("device_name", device_name))
            or device_name
        )
        self.client_version = str(
            data.get(
                "auth_client_version",
                data.get(
                    "client_version",
                    data.get(
                        "app_version",
                        data.get("server_version", client_version),
                    ),
                ),
            )
            or client_version
        )
        self.request_func = request_func or requests_urllib
        self.session_id = None

    @property
    def base_url(self):
        if not self.scheme or not self.netloc:
            return ""
        return f"{self.scheme}://{self.netloc}{self.server_path}".rstrip("/")

    @property
    def http_base_url(self):
        if not self.base_url:
            return ""
        # ``server_path=/proxy/emby`` is already the API root.  For a normal
        # server (or a reverse-proxy prefix such as ``/proxy``), Emby REST
        # endpoints live below an ``/emby`` suffix.
        if self.server_path.lower().endswith("/emby"):
            return self.base_url
        return f"{self.base_url}/emby"

    @property
    def websocket_url(self):
        """Build the Emby WebSocket endpoint for the control device."""

        if not self.base_url:
            return ""
        scheme = {"http": "ws", "https": "wss"}.get(self.scheme, self.scheme)
        websocket_path = self.server_path
        # When callers pass an API root ending in ``/emby``, the WebSocket is a
        # sibling endpoint (``/embywebsocket``), not ``/emby/embywebsocket``.
        if websocket_path.lower().endswith("/emby"):
            websocket_path = websocket_path[:-5].rstrip("/")
        base = f"{scheme}://{self.netloc}{websocket_path}".rstrip("/")
        params = {
            "api_key": self.api_key,
            "deviceId": self.control_device_id,
        }
        return f"{base}/embywebsocket?{urllib.parse.urlencode(params)}"

    @property
    def auth_headers(self):
        """Explicit Emby credentials for this device (never browser headers)."""

        authorization = (
            f'MediaBrowser Client="{self.client_name}", '
            f'Device="{self.device_name}", '
            f'DeviceId="{self.control_device_id}", '
            f'Version="{self.client_version}", '
            f'Token="{self.api_key}"'
        )
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json; charset=utf-8",
            "X-Emby-Token": self.api_key,
            "X-Emby-Client": self.client_name,
            "X-Emby-Device-Name": self.device_name,
            "X-Emby-Device-Id": self.control_device_id,
            "X-Emby-Authorization": authorization,
        }
        if self.user_id:
            # Emby's session and capabilities endpoints resolve a user from
            # the request context.  Keep this explicit so Playing, REST
            # capabilities and the WebSocket all bind to one user/session.
            headers["X-Emby-User-Id"] = self.user_id
            headers["X-Emby-Authorization"] = (
                f'{authorization}, UserId="{self.user_id}"'
            )
        return headers

    @property
    def websocket_headers(self):
        """Headers accepted by websocket-client's ``create_connection``."""

        headers = [
            f"X-Emby-Token: {self.api_key}",
            f"X-Emby-Client: {self.client_name}",
            f"X-Emby-Device-Name: {self.device_name}",
            f"X-Emby-Device-Id: {self.control_device_id}",
            f"X-Emby-Authorization: {self.auth_headers['X-Emby-Authorization']}",
        ]
        if self.user_id:
            headers.append(f"X-Emby-User-Id: {self.user_id}")
        return headers

    def _request(self, path, *, method="GET", params=None, payload=None,
                 timeout=10):
        if not self.http_base_url:
            raise EmbySessionError("Emby host is not configured")
        url = f"{self.http_base_url}/{str(path).lstrip('/')}"
        kwargs = {
            "headers": dict(self.auth_headers),
            "timeout": timeout,
            "retry": 1,
            "method": method,
        }
        if params:
            kwargs["params"] = params
        if payload is not None:
            kwargs["_json"] = payload
        if method.upper() == "GET":
            kwargs["get_json"] = True
        try:
            return self.request_func(url, **kwargs)
        except Exception as exc:
            raise EmbySessionError(str(exc)) from exc

    @staticmethod
    def _session_items(response):
        if isinstance(response, dict):
            items = _mapping_value(response, "Items")
            if isinstance(items, list):
                return items
            # A few test doubles and Emby-compatible servers return one object.
            if _mapping_value(response, "Id") or _mapping_value(response, "PlayState"):
                return [response]
        return response if isinstance(response, list) else []

    def get_sessions(self, timeout=10):
        return self._session_items(self._request("Sessions", timeout=timeout))

    def find_session(self, play_session_id=None, timeout=10):
        """Find this playback's session without relying on browser identity."""

        target = str(play_session_id or self.play_session_id or "")
        sessions = self.get_sessions(timeout=timeout)
        target_matches = []
        exact_matches = []
        for session in sessions:
            if not isinstance(session, dict):
                continue
            play_state = _mapping_value(session, "PlayState") or {}
            ids = (
                _mapping_value(play_state, "PlaySessionId"),
                _mapping_value(session, "PlaySessionId"),
            )
            if not target or target not in [
                str(value) for value in ids if value is not None
            ]:
                continue
            session_device = _mapping_value(session, "DeviceId")
            if session_device and str(session_device) != self.control_device_id:
                # A matching PlaySessionId from another device must never win
                # capability binding for this control client.
                continue
            session_user = _mapping_value(session, "UserId")
            if self.user_id and session_user and str(session_user) != self.user_id:
                continue
            if session_device and str(session_device) == self.control_device_id:
                exact_matches.append(session)
            else:
                # Some Emby-compatible servers omit DeviceId in Sessions
                # responses.  A unique PlaySessionId remains a safe fallback.
                target_matches.append(session)
        for session in exact_matches + target_matches:
            self.session_id = (
                _mapping_value(session, "Id")
                or _mapping_value(session, "SessionId")
            )
            if self.session_id:
                return session
        for session in sessions:
            if not isinstance(session, dict):
                continue
            session_device = _mapping_value(session, "DeviceId")
            session_user = _mapping_value(session, "UserId")
            if str(session_device or "") != self.control_device_id:
                continue
            if self.user_id and session_user and str(session_user) != self.user_id:
                continue
            self.session_id = (
                _mapping_value(session, "Id")
                or _mapping_value(session, "SessionId")
            )
            if self.session_id:
                return session
        return None

    def declare_capabilities(self, session_id=None, *, full=True, timeout=10):
        """Advertise controls for the current device session.

        Emby's SessionsService expects the concrete server-side session id as
        the ``Id`` query parameter.  The capabilities endpoint is
        intentionally *not* nested below a session id.
        """

        # ``find_session`` stores the id for integrations that use the
        # convenience form without an explicit argument.  Never send an
        # unbound capability declaration: Emby may otherwise apply it to a
        # stale control session selected by request headers.
        requested_session_id = self.session_id if session_id is None else session_id
        if requested_session_id is None or not str(requested_session_id).strip():
            raise EmbySessionError("session id is required for capability declaration")
        requested_session_id = str(requested_session_id).strip()
        self.session_id = requested_session_id

        suffix = "Capabilities/Full" if full else "Capabilities"
        payload = {
            "PlayableMediaTypes": ["Video"],
            "SupportedCommands": list(SUPPORTED_COMMANDS),
            "SupportsMediaControl": True,
        }
        return self._request(
            f"Sessions/{suffix}",
            method="POST", params={"Id": requested_session_id},
            payload=payload, timeout=timeout,
        )

    def _playback_payload(self, position_sec=None, *, event_name=None,
                          is_paused=None, playback_rate=1.0):
        if position_sec is None:
            position_sec = 0
        try:
            position_sec = max(0.0, float(position_sec))
        except (TypeError, ValueError):
            position_sec = 0.0
        payload = {
            "ItemId": self.item_id,
            "MediaSourceId": self.media_source_id,
            "PlaySessionId": self.play_session_id,
            "PositionTicks": int(position_sec * 10 ** 7),
            "PlayMethod": "DirectStream",
            "RepeatMode": "RepeatNone",
        }
        if self.user_id:
            payload["UserId"] = self.user_id
        try:
            playback_rate = float(playback_rate)
        except (TypeError, ValueError):
            playback_rate = 1.0
        if not playback_rate > 0:
            playback_rate = 1.0
        payload["PlaybackRate"] = playback_rate
        if event_name:
            payload["EventName"] = event_name
        if is_paused is not None:
            payload["IsPaused"] = bool(is_paused)
        return payload

    def report_playing(self, position_sec=0, *, is_paused=False,
                       event_name="TimeUpdate", playback_rate=1.0, timeout=10):
        result = self._request(
            "Sessions/Playing", method="POST",
            payload=self._playback_payload(
                position_sec, event_name=event_name, is_paused=is_paused,
                playback_rate=playback_rate,
            ), timeout=timeout,
        )
        self._capture_session_id(result)
        return result

    def report_progress(self, position_sec=0, *, is_paused=False,
                        event_name="TimeUpdate", playback_rate=1.0, timeout=10):
        result = self._request(
            "Sessions/Playing/Progress", method="POST",
            payload=self._playback_payload(
                position_sec, event_name=event_name, is_paused=is_paused,
                playback_rate=playback_rate,
            ), timeout=timeout,
        )
        self._capture_session_id(result)
        return result

    def report_stopped(self, position_sec=0, *, is_paused=False,
                       playback_rate=1.0, timeout=10):
        # Emby's Stopped endpoint does not require an EventName.  Keeping the
        # payload otherwise identical makes the final position unambiguous.
        result = self._request(
            "Sessions/Playing/Stopped", method="POST",
            payload=self._playback_payload(
                position_sec, is_paused=is_paused, playback_rate=playback_rate,
            ), timeout=timeout,
        )
        self._capture_session_id(result)
        return result

    def _capture_session_id(self, response):
        """Remember a server session id when an Emby response provides one."""

        if not isinstance(response, dict):
            return
        candidate = (
            _mapping_value(response, "SessionId")
            or _mapping_value(response, "Id")
        )
        if candidate is None:
            nested = _mapping_value(response, "Session")
            if isinstance(nested, dict):
                candidate = (
                    _mapping_value(nested, "SessionId")
                    or _mapping_value(nested, "Id")
                )
        if candidate is not None and str(candidate).strip():
            self.session_id = str(candidate).strip()

    # Concise aliases for small integrations and test doubles.
    get_session = find_session
    set_capabilities = declare_capabilities
    playing = report_playing
    progress = report_progress
    stopped = report_stopped


# Compatibility aliases make the intent obvious to callers while preserving a
# concise class name for tests and integrations.
EmbySessionAPI = EmbySessionApi
EmbySessionClient = EmbySessionApi
get_control_device_id = derive_control_device_id
derive_device_id = derive_control_device_id
make_control_device_id = derive_control_device_id
stable_device_id = derive_control_device_id
