"""Administrator API and two-user watch-together coordinator.

The coordinator talks to Emby's Sessions REST API only.  Playback clients
remain ordinary ``embyToLocalPlayer`` sessions created by S1; no PartyService
or client-to-client listener is involved.
"""

from __future__ import annotations

import copy
import datetime as _datetime
import secrets
import threading
import time
import urllib.parse

from utils.configs import configs, MyLogger
from utils.net_tools import requests_urllib
from utils.watch_together_store import (
    WatchTogetherStore,
    WatchTogetherStoreError,
    normalize_server_url,
)


logger = MyLogger()
TICKS_PER_SECOND = 10 ** 7
MAX_RUNTIME_DIFFERENCE_TICKS = 3 * TICKS_PER_SECOND
CONTROL_CLIENT = "embyToLocalPlayer"
CONTROL_DEVICE_NAME = "watch-together"
CONTROL_DEVICE_PREFIX = "etlp-wt-"


class WatchTogetherApiError(RuntimeError):
    """Raised for a failed administrator API request."""


class EmbyAdminApi:
    """Small, explicit Emby REST client used by the local administrator.

    ``request_func`` has the same shape as ``utils.net_tools.requests_urllib``
    and is injectable for deterministic tests.
    """

    def __init__(self, server_url=None, admin_api_key="", *, api_key=None,
                 request_func=None, client_name="embyToLocalPlayer-admin",
                 device_name="watch-together-admin", device_id="etlp-wt-admin",
                 timeout=10):
        self.server_url = normalize_server_url(server_url) if server_url else ""
        self.admin_api_key = str(api_key if api_key is not None else admin_api_key or "")
        self.request_func = request_func or requests_urllib
        self.client_name = client_name
        self.device_name = device_name
        self.device_id = device_id
        self.timeout = timeout
        self.server_id = None

    @property
    def base_url(self):
        return self.server_url

    @property
    def http_base_url(self):
        return f"{self.server_url}/emby" if self.server_url else ""

    def _headers(self, token=None):
        # Do not put credentials in logs or response objects.  The temporary
        # user token supplied to verify_admin_user is only represented here.
        token = self.admin_api_key if token is None else str(token)
        authorization = (
            f'MediaBrowser Client="{self.client_name}", '
            f'Device="{self.device_name}", DeviceId="{self.device_id}", '
            f'Version="1.0", Token="{token}"'
        )
        return {
            "Accept": "application/json",
            "Content-Type": "application/json; charset=utf-8",
            "X-Emby-Token": token,
            "X-Emby-Client": self.client_name,
            "X-Emby-Device-Name": self.device_name,
            "X-Emby-Device-Id": self.device_id,
            "X-Emby-Authorization": authorization,
        }

    def _request(self, path, *, method="GET", params=None, payload=None,
                 token=None, timeout=None):
        if not self.http_base_url:
            raise WatchTogetherApiError("server_url is not configured")
        url = f"{self.http_base_url}/{str(path).lstrip('/')}"
        kwargs = {
            "headers": self._headers(token),
            "method": method,
            "timeout": self.timeout if timeout is None else timeout,
            "retry": 1,
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
            raise WatchTogetherApiError(f"Emby request failed: {type(exc).__name__}") from exc

    @staticmethod
    def _items(response):
        if isinstance(response, dict):
            if isinstance(response.get("Items"), list):
                return response["Items"]
            return [response] if response.get("Id") else []
        return response if isinstance(response, list) else []

    def get_system_info(self):
        result = self._request("System/Info")
        if isinstance(result, dict):
            self.server_id = result.get("Id") or result.get("ServerId")
        return result

    def get_users(self):
        return self._items(self._request("Users"))

    def get_users_for_ui(self):
        users = []
        for user in self.get_users():
            if not isinstance(user, dict) or not user.get("Id"):
                continue
            users.append({"id": str(user["Id"]), "name": str(user.get("Name") or "")})
        return users

    def get_sessions(self):
        return self._items(self._request("Sessions"))

    def get_user(self, user_id, user_token):
        value = self._request(
            f"Users/{urllib.parse.quote(str(user_id), safe='')}", token=user_token,
        )
        return value if isinstance(value, dict) else {}

    def verify_admin_user(self, user_id, user_token):
        """Verify a browser token's user and administrator policy.

        The supplied token is used only for this request and is not copied to
        ``self.admin_api_key`` or any coordinator/store object.
        """

        if not str(user_id or "").strip() or not str(user_token or "").strip():
            return False
        try:
            user = self.get_user(user_id, user_token)
        except Exception:
            return False
        policy = user.get("Policy") if isinstance(user, dict) else None
        return bool(
            isinstance(user, dict)
            and str(user.get("Id", "")) == str(user_id)
            and isinstance(policy, dict)
            and policy.get("IsAdministrator") is True
        )

    def send_command(self, session_id, command, *, position_ticks=None):
        command = str(command)
        if command not in ("Pause", "Unpause", "Seek"):
            raise ValueError(f"unsupported session command: {command}")
        params = {}
        if command == "Seek":
            try:
                position_ticks = max(0, int(position_ticks))
            except (TypeError, ValueError):
                raise ValueError("Seek requires PositionTicks") from None
            params["SeekPositionTicks"] = position_ticks
        return self._request(
            f"Sessions/{urllib.parse.quote(str(session_id), safe='')}/Playing/{command}",
            method="POST", params=params or None,
        )

    command_session = send_command


EmbyAdminAPI = EmbyAdminApi


def _as_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _ticks_to_sec(value):
    return _as_float(value) / TICKS_PER_SECOND


def _sec_to_ticks(value):
    return max(0, int(round(_as_float(value) * TICKS_PER_SECOND)))


def _timestamp(value):
    if not value:
        return 0.0
    try:
        parsed = _datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=_datetime.timezone.utc)
        return parsed.timestamp()
    except (TypeError, ValueError, OverflowError):
        return 0.0


class WatchTogetherCoordinator:
    """Two-participant room state machine driven by Emby Sessions."""

    def __init__(self, store=None, api=None, *, server_url=None,
                 admin_api_key=None, config=None, enabled=None,
                 poll_interval=1.0, clock=None, sleeper=None, poll=None,
                 poll_func=None):
        self.store = store or WatchTogetherStore()
        self.config = config or configs
        self._enabled_override = enabled
        self._server_url_override = server_url
        self._admin_api_key_override = admin_api_key
        self.server_url = ""
        self.admin_api_key = ""
        self._refresh_config()
        self.api = api
        if self.api is None and self.server_url and self.admin_api_key:
            try:
                self.api = EmbyAdminApi(self.server_url, self.admin_api_key)
            except ValueError:
                self.api = None
        elif self.api is not None and not self.server_url:
            self.server_url = normalize_server_url(getattr(self.api, "server_url", "")) \
                if getattr(self.api, "server_url", "") else ""
        self.poll_interval = max(0.05, float(poll_interval))
        self.clock = clock or time.monotonic
        self.sleeper = sleeper or time.sleep
        self.poll = poll or poll_func
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread = None
        self._runtime = {}

    def _refresh_config(self):
        raw = getattr(self.config, "raw", self.config)
        getter = getattr(raw, "get", None)
        getbool = getattr(raw, "getboolean", None)
        if getter:
            try:
                self.server_url = str(getter("watch_together", "server_url", fallback="") or "").strip()
                self.admin_api_key = str(getter("watch_together", "admin_api_key", fallback="") or "").strip()
            except (TypeError, ValueError, AttributeError):
                self.server_url = ""
                self.admin_api_key = ""
        if not self.server_url:
            self.server_url = str(getattr(self.config, "server_url", "") or "").strip()
        if not self.admin_api_key:
            self.admin_api_key = str(getattr(self.config, "admin_api_key", "") or "").strip()
        if self._server_url_override is not None:
            self.server_url = str(self._server_url_override or "").strip()
        if self._admin_api_key_override is not None:
            self.admin_api_key = str(self._admin_api_key_override or "").strip()
        try:
            self._config_enable = bool(getbool("watch_together", "enable", fallback=False)) if getbool else False
            self._config_admin_enable = bool(getbool("watch_together", "admin_enable", fallback=False)) if getbool else False
        except (TypeError, ValueError, AttributeError):
            self._config_enable = False
            self._config_admin_enable = False

    def feature_status(self):
        self._refresh_config()
        if self._enabled_override is not None:
            enabled = bool(self._enabled_override)
        else:
            enabled = self._config_enable and self._config_admin_enable
        if not enabled:
            return False, "watch-together administrator service is disabled"
        if self._enabled_override is True and self.api is not None:
            # Explicitly enabled injected APIs are useful for offline tests;
            # the production HTTP service never sets this override and still
            # requires configured URL/key values below.
            return True, ""
        if not self.server_url or not self.admin_api_key:
            return False, "watch-together server_url/admin_api_key is missing"
        if self.api is None:
            return False, "watch-together administrator API is unavailable"
        return True, ""

    def is_configured(self):
        return self.feature_status()[0]

    @property
    def thread(self):
        return self._thread

    @property
    def runtime(self):
        with self._lock:
            return copy.deepcopy(self._runtime)

    def start(self):
        enabled, _ = self.feature_status()
        if not enabled:
            return False
        with self._lock:
            if self._thread and self._thread.is_alive():
                return True
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._run, name="watch-together-coordinator", daemon=True
            )
            self._thread.start()
        return True

    def stop(self, timeout=5.0):
        self._stop_event.set()
        thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(max(0.0, float(timeout)))
        self._thread = None

    def _run(self):
        while not self._stop_event.is_set():
            try:
                self.poll_once()
            except Exception as exc:
                logger.error(f"watch-together coordinator error: {type(exc).__name__}")
            self._stop_event.wait(self.poll_interval)

    def _api_call(self, name, *args, **kwargs):
        method = getattr(self.api, name, None)
        if not method:
            raise WatchTogetherApiError(f"administrator API lacks {name}")
        return method(*args, **kwargs)

    def _room_runtime(self, room):
        room_id = room["id"]
        runtime = self._runtime.get(room_id)
        if runtime is None:
            runtime = {
                "state": "waiting",
                "error": None,
                "pending": {},
                "suppressed": {},
                "previous": {},
                "previous_at": None,
                "drift_rounds": 0,
                "barrier": None,
            }
            self._runtime[room_id] = runtime
        return runtime

    def list_rooms(self):
        rooms = self.store.list_rooms()
        with self._lock:
            summaries = []
            for room in rooms:
                runtime = self._runtime.get(room["id"], {})
                summaries.append({
                    "room_id": room["id"],
                    "state": runtime.get("state", "waiting"),
                    "error": runtime.get("error"),
                })
        return rooms, summaries

    def users_for_ui(self):
        if hasattr(self.api, "get_users_for_ui"):
            return self.api.get_users_for_ui()
        users = self._api_call("get_users")
        if isinstance(users, dict):
            users = users.get("Items", [])
        return [
            {"id": str(user["Id"]), "name": str(user.get("Name") or "")}
            for user in users if isinstance(user, dict) and user.get("Id")
        ]

    def current_server_id(self):
        value = getattr(self.api, "server_id", None)
        if value:
            return str(value)
        info = self._api_call("get_system_info")
        if isinstance(info, dict):
            value = info.get("Id") or info.get("ServerId")
        if not value:
            raise WatchTogetherApiError("Emby System/Info did not return a server id")
        return str(value)

    def create_room(self, name, participant_user_ids, primary_user_id):
        users = {str(user["id"]): user for user in self.users_for_ui()}
        members = [str(value) for value in participant_user_ids]
        if len(members) != 2 or len(set(members)) != 2:
            raise ValueError("participant_user_ids must contain exactly two distinct users")
        if any(member not in users for member in members):
            raise ValueError("participant_user_ids contains an unknown user")
        primary = str(primary_user_id)
        if primary not in members:
            raise ValueError("primary_user_id must be a participant")
        server_url = normalize_server_url(self.server_url or getattr(self.api, "server_url", ""))
        room = self.store.create_room(
            server_id=self.current_server_id(), server_url=server_url,
            name=name, participant_user_ids=members, primary_user_id=primary,
        )
        with self._lock:
            self._room_runtime(room)
        return room

    def delete_room(self, room_id):
        deleted = self.store.delete_room(room_id)
        with self._lock:
            self._runtime.pop(str(room_id), None)
        return deleted

    def action(self, room_id, action):
        room = self.store.get_room(room_id)
        if not room:
            raise KeyError("room not found")
        action = str(action).lower()
        if action not in ("pause", "resume", "resync"):
            raise ValueError("unknown room action")
        with self._lock:
            runtime = self._room_runtime(room)
            if action == "resync":
                runtime["state"] = "waiting"
                runtime["error"] = None
                runtime["barrier"] = None
                runtime["previous"] = {}
                runtime["pending"] = {}
                runtime["suppressed"] = {}
                runtime["drift_rounds"] = 0
                return {"room_id": room["id"], "state": "waiting"}
            command = "Pause" if action == "pause" else "Unpause"
            selected = self._select_sessions(self._api_call("get_sessions"), room["participant_user_ids"])
            sessions = {
                user_id: self._snapshot(session, user_id)
                for user_id, session in selected.items()
            }
            issued = []
            now = self.clock()
            for user_id, snapshot in sessions.items():
                if snapshot is not None and snapshot.get("online"):
                    self._issue(runtime, user_id, snapshot, command, now=now)
                    issued.append(user_id)
            return {"room_id": room["id"], "state": runtime["state"], "command": command,
                    "users": issued}

    @staticmethod
    def _is_control_session(session):
        if not isinstance(session, dict):
            return False
        return (
            str(session.get("Client", "")) == CONTROL_CLIENT
            and (
                str(session.get("DeviceName", "")) == CONTROL_DEVICE_NAME
                or str(session.get("DeviceId", "")).startswith(CONTROL_DEVICE_PREFIX)
            )
        )

    @classmethod
    def _select_sessions(cls, sessions, user_ids):
        if isinstance(sessions, dict):
            sessions = sessions.get("Items", sessions.get("Sessions", []))
        by_user = {str(user_id): None for user_id in user_ids}
        candidates = {user_id: [] for user_id in by_user}
        for session in sessions or []:
            if not cls._is_control_session(session):
                continue
            user_id = session.get("UserId")
            if not user_id and isinstance(session.get("User"), dict):
                user_id = session["User"].get("Id")
            user_id = str(user_id or "")
            if user_id in candidates:
                candidates[user_id].append(session)
        for user_id, values in candidates.items():
            if values:
                by_user[user_id] = max(
                    values, key=lambda value: _timestamp(value.get("LastActivityDate"))
                )
        return by_user

    @classmethod
    def select_control_sessions(cls, sessions, user_ids):
        """Public filtering helper used by integrations and tests."""

        return cls._select_sessions(sessions, user_ids)

    @classmethod
    def _snapshot(cls, session, user_id):
        if session is None:
            return None
        play_state = session.get("PlayState") or {}
        item = session.get("NowPlayingItem") or {}
        state_name = str(play_state.get("PlaybackState") or play_state.get("State") or "").lower()
        stopped = bool(play_state.get("IsStopped")) or state_name == "stopped" or not item.get("Id")
        rate = _as_float(play_state.get("PlaybackRate", session.get("PlaybackRate", 1.0)), 1.0)
        if rate <= 0:
            rate = 1.0
        position_ticks = max(0, int(_as_float(
            play_state.get("PositionTicks", session.get("PositionTicks", 0)), 0
        )))
        runtime_ticks = max(0, int(_as_float(
            play_state.get("RunTimeTicks", session.get("RunTimeTicks", item.get("RunTimeTicks", 0))), 0
        )))
        return {
            "user_id": str(user_id),
            "session_id": str(session.get("Id") or session.get("SessionId") or ""),
            "item_id": str(item.get("Id") or session.get("ItemId") or ""),
            "media_source_id": str(play_state.get("MediaSourceId") or session.get("MediaSourceId") or ""),
            "position_ticks": position_ticks,
            "position_sec": _ticks_to_sec(position_ticks),
            "runtime_ticks": runtime_ticks,
            "is_paused": bool(play_state.get("IsPaused", session.get("IsPaused", False))),
            "playback_rate": rate,
            "stopped": stopped,
            "online": bool(session.get("Id") or session.get("SessionId")) and not stopped,
            "raw": session,
        }

    @classmethod
    def _pair_is_eligible(cls, snapshots):
        if len(snapshots) != 2 or any(value is None for value in snapshots.values()):
            return False
        values = list(snapshots.values())
        if any(not value["online"] or value["stopped"] for value in values):
            return False
        if not values[0]["item_id"] or values[0]["item_id"] != values[1]["item_id"]:
            return False
        runtimes = [value["runtime_ticks"] for value in values]
        if all(runtimes) and abs(runtimes[0] - runtimes[1]) > MAX_RUNTIME_DIFFERENCE_TICKS:
            return False
        return all(abs(value["playback_rate"] - 1.0) <= 0.01 for value in values)

    def _issue(self, runtime, user_id, snapshot, command, *, now, position_ticks=None):
        user_id = str(user_id)
        pending = runtime["pending"].get(user_id)
        if pending and pending["command"] == command:
            if command != "Seek" or abs(int(position_ticks or 0) - snapshot.get("position_ticks", 0)) <= 2 * TICKS_PER_SECOND:
                return True
        try:
            method = getattr(self.api, "send_command", None) or getattr(self.api, "command_session", None)
            if method is None:
                method = getattr(self.api, "command", None)
            if method is None:
                raise WatchTogetherApiError("administrator API lacks send_command")
            result = method(snapshot.get("session_id"), command, position_ticks=position_ticks)
            if result is False:
                raise WatchTogetherApiError("command rejected")
        except TypeError:
            # Small fakes commonly use ``command(session, command, ticks)``.
            try:
                result = method(snapshot.get("session_id"), command, position_ticks)
                if result is False:
                    raise WatchTogetherApiError("command rejected")
            except Exception as exc:
                runtime["error"] = f"{command} command failed"
                runtime["state"] = "waiting"
                return False
        except Exception:
            runtime["error"] = f"{command} command failed"
            runtime["state"] = "waiting"
            return False
        runtime["pending"][user_id] = {
            "command": command,
            "position_ticks": int(position_ticks) if position_ticks is not None else None,
            "issued_at": now,
            "retries": 0,
        }
        return True

    @classmethod
    def _pending_matches(cls, pending, snapshot):
        if not pending or snapshot is None:
            return False
        command = pending["command"]
        if command == "Pause":
            return bool(snapshot.get("is_paused"))
        if command == "Unpause":
            return not bool(snapshot.get("is_paused"))
        if command == "Seek":
            return abs(snapshot.get("position_ticks", 0) - int(pending.get("position_ticks") or 0)) <= 2 * TICKS_PER_SECOND
        return False

    def _observe_pending(self, runtime, snapshots, now):
        failed = False
        for user_id, pending in list(runtime["pending"].items()):
            snapshot = snapshots.get(user_id)
            if self._pending_matches(pending, snapshot):
                del runtime["pending"][user_id]
                runtime.setdefault("suppressed", {})[user_id] = {
                    "command": pending["command"],
                    "position_ticks": pending.get("position_ticks"),
                    "until": now + 3.0,
                }
                continue
            if now - pending["issued_at"] < 3.0:
                continue
            if pending["retries"] == 0 and snapshot is not None:
                # Remove the old entry before issuing so _issue cannot treat
                # the retry as a duplicate and silently suppress it.
                position_ticks = pending.get("position_ticks")
                del runtime["pending"][user_id]
                self._issue(
                    runtime, user_id, snapshot, pending["command"], now=now,
                    position_ticks=position_ticks,
                )
                if user_id in runtime["pending"]:
                    runtime["pending"][user_id]["retries"] = 1
            else:
                del runtime["pending"][user_id]
                failed = True
        if failed:
            runtime["state"] = "waiting"
            runtime["error"] = "playback command was not acknowledged"
        return failed

    def _start_barrier(self, runtime, room, snapshots, now):
        primary_id = room["primary_user_id"]
        primary = snapshots.get(primary_id)
        runtime["state"] = "barrier"
        runtime["error"] = None
        runtime["barrier"] = {
            "stage": "pause",
            "started_at": now,
            "primary_position_ticks": primary["position_ticks"],
            "primary_paused": bool(primary["is_paused"]),
            "item_id": primary["item_id"],
            "pause_sent": False,
            "seek_sent": False,
            "restore_sent": False,
        }
        runtime["pending"] = {}
        runtime["suppressed"] = {}

    def _barrier_tick(self, runtime, room, snapshots, now):
        barrier = runtime["barrier"]
        members = room["participant_user_ids"]
        if barrier is None:
            self._start_barrier(runtime, room, snapshots, now)
            barrier = runtime["barrier"]
        stage = barrier["stage"]
        if stage == "pause":
            if not barrier["pause_sent"]:
                for user_id in members:
                    self._issue(runtime, user_id, snapshots[user_id], "Pause", now=now)
                barrier["pause_sent"] = True
                return
            if all(snapshots[user_id]["is_paused"] for user_id in members):
                barrier["stage"] = "seek"
                barrier["started_at"] = now
                return
            if now - barrier["started_at"] >= 3.0:
                runtime["state"] = "waiting"
                runtime["error"] = "barrier pause timed out"
                runtime["barrier"] = None
            return
        if stage == "seek":
            secondary = next(user for user in members if user != room["primary_user_id"])
            target = barrier["primary_position_ticks"]
            if not barrier["seek_sent"]:
                self._issue(runtime, secondary, snapshots[secondary], "Seek", now=now, position_ticks=target)
                barrier["seek_sent"] = True
                return
            if abs(snapshots[secondary]["position_ticks"] - target) <= 2 * TICKS_PER_SECOND:
                barrier["stage"] = "restore"
                barrier["started_at"] = now
                return
            if now - barrier["started_at"] >= 3.0:
                runtime["state"] = "waiting"
                runtime["error"] = "barrier seek timed out"
                runtime["barrier"] = None
            return
        if stage == "restore":
            if not barrier["restore_sent"]:
                command = "Pause" if barrier["primary_paused"] else "Unpause"
                for user_id in members:
                    self._issue(runtime, user_id, snapshots[user_id], command, now=now)
                barrier["restore_sent"] = True
                return
            desired = barrier["primary_paused"]
            if all(bool(snapshots[user_id]["is_paused"]) == desired for user_id in members):
                runtime["state"] = "watching"
                runtime["barrier"] = None
                runtime["pending"] = {}
                runtime["suppressed"] = {}
                runtime["previous"] = {key: copy.deepcopy(value) for key, value in snapshots.items()}
                runtime["previous_at"] = now
                runtime["drift_rounds"] = 0
            elif now - barrier["started_at"] >= 3.0:
                runtime["state"] = "waiting"
                runtime["error"] = "barrier restore timed out"
                runtime["barrier"] = None

    def _pause_other_when_waiting(self, runtime, snapshots, room, now):
        online_playing = [
            (user_id, value) for user_id, value in snapshots.items()
            if value is not None and value.get("online") and not value.get("is_paused")
        ]
        if not online_playing:
            return
        if len(online_playing) == 1:
            user_id, snapshot = online_playing[0]
        else:
            # When both sessions are live but their items diverge, preserve
            # the participant whose previous item still matches and pause the
            # other.  On the first mismatched round there is no history, so
            # the secondary is the deterministic fallback.
            previous = runtime.get("previous") or {}
            primary_id = room["primary_user_id"]
            previous_item = (previous.get(primary_id) or {}).get("item_id")
            if previous_item:
                target = next(
                    ((uid, snap) for uid, snap in online_playing
                     if uid != primary_id and snap.get("item_id") != previous_item),
                    None,
                )
                if target:
                    user_id, snapshot = target
                else:
                    user_id, snapshot = online_playing[0]
            else:
                user_id, snapshot = next(
                    ((uid, snap) for uid, snap in online_playing if uid != primary_id),
                    online_playing[0],
                )
        self._issue(runtime, user_id, snapshot, "Pause", now=now)

    def _watching_tick(self, runtime, room, snapshots, now):
        members = room["participant_user_ids"]
        previous = runtime.get("previous") or {}
        previous_at = runtime.get("previous_at")
        if previous_at is None:
            runtime["previous"] = {key: copy.deepcopy(value) for key, value in snapshots.items()}
            runtime["previous_at"] = now
            return
        elapsed = max(0.0, now - previous_at)
        primary = room["primary_user_id"]
        pause_changes = []
        seek_changes = []
        for user_id in members:
            current = snapshots.get(user_id)
            old = previous.get(user_id)
            if current is None or old is None:
                continue
            pending = runtime["pending"].get(user_id)
            suppressed = runtime.get("suppressed", {}).get(user_id)
            suppress_pause = False
            suppress_seek = False
            if suppressed:
                if suppressed.get("until", 0) <= now:
                    runtime["suppressed"].pop(user_id, None)
                elif self._pending_matches(suppressed, current):
                    suppress_pause = suppressed["command"] in ("Pause", "Unpause")
                    suppress_seek = suppressed["command"] == "Seek"
                    runtime["suppressed"].pop(user_id, None)
            if bool(current["is_paused"]) != bool(old["is_paused"]):
                if not suppress_pause and not (pending and pending["command"] in ("Pause", "Unpause") and self._pending_matches(pending, current)):
                    pause_changes.append((user_id, bool(current["is_paused"])))
            expected = old["position_ticks"]
            if not old["is_paused"]:
                expected += _sec_to_ticks(elapsed * old.get("playback_rate", 1.0))
            if abs(current["position_ticks"] - expected) >= 5 * TICKS_PER_SECOND:
                if not suppress_seek and not (pending and pending["command"] == "Seek" and self._pending_matches(pending, current)):
                    seek_changes.append((user_id, current["position_ticks"]))
        if pause_changes:
            winner = next((change for change in pause_changes if change[0] == primary), pause_changes[0])
            command = "Pause" if winner[1] else "Unpause"
            for user_id in members:
                if user_id != winner[0] and snapshots.get(user_id):
                    self._issue(runtime, user_id, snapshots[user_id], command, now=now)
        if seek_changes:
            winner = next((change for change in seek_changes if change[0] == primary), seek_changes[0])
            for user_id in members:
                if user_id != winner[0] and snapshots.get(user_id):
                    self._issue(runtime, user_id, snapshots[user_id], "Seek", now=now,
                                position_ticks=winner[1])
        positions = [snapshots[user]["position_ticks"] for user in members if snapshots.get(user)]
        if len(positions) == 2 and abs(positions[0] - positions[1]) > 2 * TICKS_PER_SECOND:
            runtime["drift_rounds"] += 1
        else:
            runtime["drift_rounds"] = 0
        if runtime["drift_rounds"] >= 2 and not seek_changes:
            secondary = next(user for user in members if user != primary)
            if snapshots.get(primary) and snapshots.get(secondary):
                self._issue(runtime, secondary, snapshots[secondary], "Seek", now=now,
                            position_ticks=snapshots[primary]["position_ticks"])
            runtime["drift_rounds"] = 0
        runtime["previous"] = {key: copy.deepcopy(value) for key, value in snapshots.items()}
        runtime["previous_at"] = now

    def poll_once(self, now=None):
        enabled, _ = self.feature_status()
        if not enabled:
            return []
        now = self.clock() if now is None else float(now)
        try:
            sessions = self.poll() if self.poll is not None else self._api_call("get_sessions")
        except Exception:
            return []
        results = []
        rooms = self.store.list_rooms()
        with self._lock:
            for room in rooms:
                runtime = self._room_runtime(room)
                selected = self._select_sessions(sessions, room["participant_user_ids"])
                snapshots = {
                    user_id: self._snapshot(session, user_id)
                    for user_id, session in selected.items()
                }
                pending_failed = self._observe_pending(runtime, snapshots, now)
                eligible = self._pair_is_eligible(snapshots)
                if pending_failed:
                    runtime["state"] = "waiting"
                    runtime["barrier"] = None
                    runtime["previous"] = {}
                    runtime["drift_rounds"] = 0
                    results.append({"room_id": room["id"], "state": runtime["state"], "eligible": eligible})
                    continue
                if runtime["state"] == "watching":
                    if eligible:
                        self._watching_tick(runtime, room, snapshots, now)
                    else:
                        self._pause_other_when_waiting(runtime, snapshots, room, now)
                        runtime["state"] = "waiting"
                        runtime["barrier"] = None
                        runtime["previous"] = {}
                        runtime["drift_rounds"] = 0
                elif eligible:
                    if runtime["state"] == "waiting" and runtime.get("error"):
                        # A command failure requires an explicit resync (or a
                        # fresh room action) before another barrier can issue
                        # commands; this prevents one-second command storms.
                        results.append({"room_id": room["id"], "state": runtime["state"], "eligible": eligible})
                        continue
                    if runtime["state"] != "barrier":
                        self._start_barrier(runtime, room, snapshots, now)
                    self._barrier_tick(runtime, room, snapshots, now)
                else:
                    self._pause_other_when_waiting(runtime, snapshots, room, now)
                    runtime["state"] = "waiting"
                    runtime["barrier"] = None
                    runtime["previous"] = {}
                results.append({"room_id": room["id"], "state": runtime["state"], "eligible": eligible})
        return results

    tick = poll_once
    step = poll_once


class WatchTogetherHttpService:
    """Local authenticated facade consumed by ``utils.http_server``.

    The service returns ``(status_code, json_object)`` and performs no socket
    operations itself, which keeps HTTP tests independent from a real server.
    """

    TOKEN_HEADER = "X-ETLP-Watch-Token"
    TOKEN_TTL = 30 * 60

    def __init__(self, coordinator=None, *, config=None, clock=None,
                 token_ttl=TOKEN_TTL):
        self.coordinator = coordinator or WatchTogetherCoordinator(config=config)
        self.config = config or getattr(self.coordinator, "config", configs)
        self.clock = clock or time.time
        self.token_ttl = max(1, int(token_ttl))
        self._tokens = {}
        self._lock = threading.RLock()

    def _config_values(self):
        refresh = getattr(self.coordinator, "_refresh_config", None)
        if refresh:
            refresh()
        raw = getattr(self.config, "raw", self.config)
        get = getattr(raw, "get", None)
        getbool = getattr(raw, "getboolean", None)
        if get:
            try:
                enable = bool(getbool("watch_together", "enable", fallback=False)) if getbool else False
                admin_enable = bool(getbool("watch_together", "admin_enable", fallback=False)) if getbool else False
                server_url = str(get("watch_together", "server_url", fallback="") or "").strip()
                admin_key = str(get("watch_together", "admin_api_key", fallback="") or "").strip()
            except (TypeError, ValueError, AttributeError):
                enable = admin_enable = False
                server_url = admin_key = ""
        else:
            enable = bool(getattr(self.config, "enable", False))
            admin_enable = bool(getattr(self.config, "admin_enable", False))
            server_url = str(getattr(self.config, "server_url", "") or "").strip()
            admin_key = str(getattr(self.config, "admin_api_key", "") or "").strip()
        return enable, admin_enable, server_url, admin_key

    def _available(self):
        enable, admin_enable, server_url, admin_key = self._config_values()
        # A coordinator supplied by a test/integration may intentionally use a
        # fake API; production configuration still requires both switches and
        # both credentials before starting the worker.
        if not (enable and admin_enable):
            return False, 503, "watch-together administrator service is disabled"
        if not server_url or not admin_key:
            return False, 503, "watch-together server_url/admin_api_key is missing"
        return True, 0, ""

    def start(self):
        available, _, _ = self._available()
        if not available:
            return False
        return bool(self.coordinator.start())

    def is_enabled(self):
        return bool(self._available()[0])

    def stop(self, timeout=5.0):
        return self.coordinator.stop(timeout=timeout)

    @staticmethod
    def _error(status, code, message):
        return status, {"error": {"code": code, "message": message}}

    @staticmethod
    def _loopback(client_address):
        if isinstance(client_address, (tuple, list)):
            client_address = client_address[0] if client_address else ""
        try:
            import ipaddress

            return ipaddress.ip_address(str(client_address or "")).is_loopback
        except ValueError:
            return False

    @staticmethod
    def _body_object(body):
        if body is None:
            return {}
        if isinstance(body, (bytes, bytearray)):
            body = body.decode("utf-8")
        if isinstance(body, str):
            import json

            try:
                body = json.loads(body)
            except (TypeError, ValueError) as exc:
                raise ValueError("request body must be valid JSON") from exc
        if not isinstance(body, dict):
            raise ValueError("request body must be a JSON object")
        return body

    def _token_context(self, headers):
        headers = headers or {}
        token = next((value for key, value in headers.items()
                      if str(key).lower() == self.TOKEN_HEADER.lower()), "")
        token = str(token or "")
        if not token:
            return None
        with self._lock:
            context = self._tokens.get(token)
            if not context:
                return None
            if context["expires_at"] <= self.clock():
                del self._tokens[token]
                return None
            return dict(context)

    def _auth(self, body):
        available, status, message = self._available()
        if not available:
            return self._error(status, "watch_together_unavailable", message)
        required = ("server_url", "user_id", "api_key")
        if any(not str(body.get(key) or "").strip() for key in required):
            return self._error(400, "invalid_request", "server_url, user_id and api_key are required")
        try:
            requested_url = normalize_server_url(body["server_url"])
            configured_url = normalize_server_url(self._config_values()[2])
        except ValueError:
            return self._error(400, "invalid_server_url", "server_url must be an absolute http(s) URL")
        if requested_url != configured_url:
            return self._error(403, "server_mismatch", "server_url does not match administrator configuration")
        user_id = str(body["user_id"])
        user_token = str(body["api_key"])
        try:
            verified = bool(self.coordinator.api.verify_admin_user(user_id, user_token))
        except Exception:
            verified = False
        if not verified:
            return self._error(403, "administrator_required", "current Emby user is not an administrator")
        token = secrets.token_urlsafe(32)
        expires_at = self.clock() + self.token_ttl
        with self._lock:
            self._tokens[token] = {
                "user_id": user_id,
                "server_url": configured_url,
                "expires_at": expires_at,
            }
        return 200, {"token": token, "expires_in": self.token_ttl}

    def handle(self, path, body=None, *, headers=None, client_address=None, method="POST"):
        path = urllib.parse.urlsplit(str(path or "")).path.rstrip("/") or "/"
        if not path.startswith("/watch-together"):
            return self._error(404, "not_found", "watch-together endpoint not found")
        if not self._loopback(client_address):
            return self._error(403, "loopback_required", "watch-together is available on loopback only")
        method = str(method or "POST").upper()
        if method == "OPTIONS":
            return 204, {}
        if method != "POST":
            return self._error(405, "method_not_allowed", "only POST and OPTIONS are supported")
        try:
            body = self._body_object(body)
        except ValueError as exc:
            return self._error(400, "invalid_json", str(exc))
        if path == "/watch-together/auth":
            return self._auth(body)
        available, status, message = self._available()
        if not available:
            return self._error(status, "watch_together_unavailable", message)
        context = self._token_context(headers)
        if context is None:
            return self._error(401, "invalid_token", "watch-together token is missing or expired")
        try:
            if path == "/watch-together/rooms/list":
                rooms, runtime = self.coordinator.list_rooms()
                return 200, {"rooms": rooms, "users": self.coordinator.users_for_ui(), "runtime": runtime}
            if path == "/watch-together/rooms/create":
                members = body.get("participant_user_ids")
                if (not isinstance(body.get("name"), str) or not body.get("name").strip()
                        or not isinstance(members, list)
                        or not isinstance(body.get("primary_user_id"), str)):
                    return self._error(400, "invalid_request",
                                       "name, participant_user_ids and primary_user_id are required")
                room = self.coordinator.create_room(
                    name=body.get("name"),
                    participant_user_ids=members,
                    primary_user_id=body.get("primary_user_id"),
                )
                return 200, {"room": room}
            if path == "/watch-together/rooms/delete":
                room_id = str(body.get("room_id") or "")
                if not room_id:
                    return self._error(400, "invalid_request", "room_id is required")
                if not self.coordinator.delete_room(room_id):
                    return self._error(404, "room_not_found", "room not found")
                return 200, {"deleted": True, "room_id": room_id}
            if path == "/watch-together/rooms/action":
                room_id = str(body.get("room_id") or "")
                action = str(body.get("action") or "").lower()
                if not room_id or not action:
                    return self._error(400, "invalid_request", "room_id and action are required")
                if action not in ("pause", "resume", "resync"):
                    return self._error(400, "unknown_action", "action must be pause, resume or resync")
                return 200, {"result": self.coordinator.action(room_id, action)}
        except KeyError:
            return self._error(404, "room_not_found", "room not found")
        except ValueError as exc:
            return self._error(400, "invalid_request", str(exc))
        except WatchTogetherStoreError as exc:
            return self._error(409, "room_invalid", str(exc))
        except Exception:
            return self._error(503, "watch_together_error", "watch-together service is temporarily unavailable")
        return self._error(404, "not_found", "watch-together endpoint not found")


WatchTogetherService = WatchTogetherHttpService


__all__ = [
    "EmbyAdminApi", "EmbyAdminAPI", "WatchTogetherCoordinator", "WatchTogetherApiError",
    "WatchTogetherHttpService", "WatchTogetherService",
    "WatchTogetherStore", "WatchTogetherStoreError", "normalize_server_url",
    "TICKS_PER_SECOND",
]
