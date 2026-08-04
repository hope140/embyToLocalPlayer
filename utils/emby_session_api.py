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
SUPPORTED_COMMANDS = ("Pause", "Unpause", "Seek", "DisplayMessage")


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
                 client_name="embyToLocalPlayer", device_name="watch-together",
                 client_version=CONTROL_DEVICE_VERSION, scheme="", netloc="",
                 api_key="", user_id="", item_id="", media_source_id="",
                 play_session_id="", browser_device_id="", host=""):
        data = data or {}
        host = str(data.get("host", data.get("server_address", host)) or host).strip()
        if host and "://" in host and not (data.get("scheme") or scheme):
            parsed_host = urllib.parse.urlsplit(host)
            scheme, netloc = parsed_host.scheme, parsed_host.netloc
        self.scheme = str(data.get("scheme", scheme) or scheme).strip().rstrip("/")
        self.scheme = self.scheme[:-3] if self.scheme.endswith("://") else self.scheme
        self.scheme = self.scheme.rstrip(":")
        self.netloc = str(data.get("netloc", netloc) or netloc).strip("/")
        if self.netloc.endswith("/emby"):
            self.netloc = self.netloc[:-5].rstrip("/")
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
        self.client_name = client_name
        self.device_name = device_name
        self.client_version = client_version
        self.request_func = request_func or requests_urllib
        self.session_id = None

    @property
    def base_url(self):
        if not self.scheme or not self.netloc:
            return ""
        return f"{self.scheme}://{self.netloc}".rstrip("/")

    @property
    def http_base_url(self):
        return f"{self.base_url}/emby" if self.base_url else ""

    @property
    def websocket_url(self):
        """Build the Emby WebSocket endpoint for the control device."""

        scheme = {"http": "ws", "https": "wss"}.get(self.scheme, self.scheme)
        base = f"{scheme}://{self.netloc}".rstrip("/")
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
        return {
            "Accept": "application/json",
            "Content-Type": "application/json; charset=utf-8",
            "X-Emby-Token": self.api_key,
            "X-Emby-Client": self.client_name,
            "X-Emby-Device-Name": self.device_name,
            "X-Emby-Device-Id": self.control_device_id,
            "X-Emby-Authorization": authorization,
        }

    @property
    def websocket_headers(self):
        """Headers accepted by websocket-client's ``create_connection``."""

        return [
            f"X-Emby-Token: {self.api_key}",
            f"X-Emby-Client: {self.client_name}",
            f"X-Emby-Device-Name: {self.device_name}",
            f"X-Emby-Device-Id: {self.control_device_id}",
            f"X-Emby-Authorization: {self.auth_headers['X-Emby-Authorization']}",
        ]

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
            if isinstance(response.get("Items"), list):
                return response["Items"]
            # A few test doubles and Emby-compatible servers return one object.
            if response.get("Id") or response.get("PlayState"):
                return [response]
        return response if isinstance(response, list) else []

    def get_sessions(self, timeout=10):
        return self._session_items(self._request("Sessions", timeout=timeout))

    def find_session(self, play_session_id=None, timeout=10):
        """Find this playback's session without relying on browser identity."""

        target = str(play_session_id or self.play_session_id or "")
        sessions = self.get_sessions(timeout=timeout)
        for session in sessions:
            play_state = session.get("PlayState") or {}
            ids = (
                play_state.get("PlaySessionId"),
                session.get("PlaySessionId"),
            )
            if target and target in [str(value) for value in ids if value is not None]:
                self.session_id = session.get("Id") or session.get("SessionId")
                return session
        for session in sessions:
            if str(session.get("DeviceId", "")) == self.control_device_id:
                self.session_id = session.get("Id") or session.get("SessionId")
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
        return self._request(
            "Sessions/Playing", method="POST",
            payload=self._playback_payload(
                position_sec, event_name=event_name, is_paused=is_paused,
                playback_rate=playback_rate,
            ), timeout=timeout,
        )

    def report_progress(self, position_sec=0, *, is_paused=False,
                        event_name="TimeUpdate", playback_rate=1.0, timeout=10):
        return self._request(
            "Sessions/Playing/Progress", method="POST",
            payload=self._playback_payload(
                position_sec, event_name=event_name, is_paused=is_paused,
                playback_rate=playback_rate,
            ), timeout=timeout,
        )

    def report_stopped(self, position_sec=0, *, is_paused=False,
                       playback_rate=1.0, timeout=10):
        # Emby's Stopped endpoint does not require an EventName.  Keeping the
        # payload otherwise identical makes the final position unambiguous.
        return self._request(
            "Sessions/Playing/Stopped", method="POST",
            payload=self._playback_payload(
                position_sec, is_paused=is_paused, playback_rate=playback_rate,
            ), timeout=timeout,
        )

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
