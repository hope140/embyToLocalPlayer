"""Optional Emby remote-control client for one mpv/IINA playback instance.

The client is deliberately self-contained.  It polls the active mpv JSON IPC
handle for playback state, reports that state through :mod:`emby_session_api`,
and listens on Emby's WebSocket for playstate/general-command messages.  It is
opt-in and all dependency/network failures are swallowed so ordinary playback
continues exactly as before.
"""

import hashlib
import importlib
import json
from pathlib import Path
import queue
import socket
import sys
import threading
import time

from utils.configs import MyLogger
from utils.emby_session_api import (
    EmbySessionApi,
    remote_control_enabled as is_remote_control_enabled,
)
from utils.players import get_mpv_snapshot, mpv_display_message, mpv_seek, mpv_set_pause


logger = MyLogger()


# The wheel is kept untouched in ``third_party`` and is only used when the
# normal import cannot find a usable websocket-client installation.  Keep this
# digest in code so a damaged or replaced archive is never imported.
_BUILTIN_WEBSOCKET_FILENAME = 'websocket_client-1.8.0-py3-none-any.whl'
_BUILTIN_WEBSOCKET_SHA256 = (
    '17b44cc997f5c498e809b22cdf2d9c7a9e71c02c8cc2b6c56e7c2d1239bfa526'
)
_CAPABILITIES_RETRY_INTERVAL = 1.0
_CAPABILITIES_MAX_ATTEMPTS = 3
_COMMAND_QUEUE_MAXSIZE = 32
_HEARTBEAT_DEFAULT_INTERVAL = 5.0
_HEARTBEAT_DEFAULT_TIMEOUT = 5.0
_PAUSE_CONFIRM_TIMEOUT = 0.5
_PAUSE_CONFIRM_INTERVAL = 0.02

# websocket-client exposes these values through ``ABNF``.  Keeping the small
# numeric table local means the optional dependency is still imported lazily
# and fake/wrapper sockets can use the same control-frame contract.
_WS_OPCODE_CONTINUATION = 0x0
_WS_OPCODE_TEXT = 0x1
_WS_OPCODE_BINARY = 0x2
_WS_OPCODE_CLOSE = 0x8
_WS_OPCODE_PING = 0x9
_WS_OPCODE_PONG = 0xA


class _HeartbeatTimeout(ConnectionError):
    """Raised when a sent WebSocket ping has no matching Pong in time."""


def _short_hash(value):
    """Return a stable short diagnostic hash without exposing an identifier."""

    if value is None or value == "":
        return "none"
    return hashlib.sha256(str(value).encode("utf-8", "replace")).hexdigest()[:8]


def _safe_label(value, default="unknown", limit=32):
    """Keep a log label bounded and free of URLs, IDs, and free-form text."""

    if value is None:
        return default
    value = str(value)
    label = "".join(
        char if char.isalnum() or char in "_.-" else "_"
        for char in value
    )[:limit]
    return label or default


def _message_type_label(value):
    """Return a known message type or a hash-only label for diagnostics."""

    normalized = str(value or "").strip().lower()
    if normalized in ("playstate", "generalcommand", "forcekeepalive"):
        return normalized
    return f"unknown_{_short_hash(normalized)}"


def _command_label(value):
    """Keep unsupported command diagnostics free of arbitrary payload text."""

    normalized = str(value or "").strip().lower()
    if normalized in {
        "pause", "unpause", "play", "playpause", "seek", "stop",
        "displaymessage",
    }:
        return normalized
    return f"unknown_{_short_hash(normalized)}"


def _field(mapping, name, default=None):
    """Read Emby JSON fields case-insensitively."""

    if not isinstance(mapping, dict):
        return default
    expected = str(name).lower()
    for key, value in mapping.items():
        if str(key).lower() == expected:
            return value
    return default


def _decode_json_object(value):
    """Decode a WebSocket Data value while rejecting unrelated shapes."""

    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    if isinstance(value, str):
        if not value:
            return {}
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return {}
    return value if isinstance(value, dict) else {}


def _clear_websocket_modules():
    """Remove a partially imported websocket package from ``sys.modules``."""

    for name in tuple(sys.modules):
        if name == 'websocket' or name.startswith('websocket.'):
            sys.modules.pop(name, None)


def _load_bundled_websocket():
    """Load the verified project wheel without installing it.

    The wheel path is added to ``sys.path`` only after its hard-coded digest is
    verified.  A successful import keeps that path so websocket submodules can
    be loaded lazily; all failure paths remove an insertion made by this call.
    """

    wheel_path = (
        Path(__file__).resolve().parents[1]
        / 'third_party'
        / _BUILTIN_WEBSOCKET_FILENAME
    )
    if not wheel_path.is_file():
        logger.info('remote-control disabled: bundled websocket-client wheel is missing')
        return None

    digest = hashlib.sha256()
    try:
        with wheel_path.open('rb') as wheel_file:
            for chunk in iter(lambda: wheel_file.read(1024 * 1024), b''):
                digest.update(chunk)
    except OSError:
        logger.info('remote-control disabled: bundled websocket-client wheel is unreadable')
        return None
    if digest.hexdigest().lower() != _BUILTIN_WEBSOCKET_SHA256:
        logger.info('remote-control disabled: bundled websocket-client wheel checksum mismatch')
        return None

    wheel_entry = str(wheel_path)
    inserted = False
    if wheel_entry not in sys.path:
        sys.path.insert(0, wheel_entry)
        inserted = True
    try:
        # A failed system import can leave ``websocket`` and one or more of its
        # submodules half initialized.  Clear those entries before zipimport.
        _clear_websocket_modules()
        importlib.invalidate_caches()
        return importlib.import_module('websocket')
    except Exception:
        _clear_websocket_modules()
        if inserted:
            try:
                sys.path.remove(wheel_entry)
            except ValueError:
                pass
        logger.info('remote-control disabled: bundled websocket-client wheel import failed')
        return None


class RemoteControlClient:
    """Control and state bridge for one Emby playback session."""

    def __init__(self, data=None, player=None, *, mpv=None, session_api=None,
                 enabled=None, remote_enabled=None,
                 remote_control_enabled=None, ws_factory=None, report_interval=10.0,
                 heartbeat_interval=_HEARTBEAT_DEFAULT_INTERVAL,
                 heartbeat_timeout=_HEARTBEAT_DEFAULT_TIMEOUT,
                 ws_timeout=0.5, reconnect_min=1.0,
                 reconnect_max=30.0, seek_threshold=3.0, snapshot_interval=0.2,
                 clock=None, episodes_by_title=None):
        self.data = data or {}
        self.player = player or mpv
        # media-title -> episode data mapping for the current playlist.  Used
        # to follow mpv into the next episode without losing the Emby session.
        self.episodes_by_title = episodes_by_title or {}
        # ``enabled`` is the historical constructor override.  Keep it for
        # callers/tests while offering descriptive remote-control spellings for
        # new integrations.  The most explicit value wins.
        configured_override = (
            remote_control_enabled
            if remote_control_enabled is not None
            else remote_enabled
            if remote_enabled is not None
            else enabled
        )
        self.enabled = is_remote_control_enabled(
            self.data, override=configured_override,
        )
        self.session_api = session_api or EmbySessionApi(self.data)
        self.ws_factory = ws_factory
        self.report_interval = max(0.1, float(report_interval))
        self.heartbeat_interval = max(0.05, float(heartbeat_interval))
        self.heartbeat_timeout = max(0.05, float(heartbeat_timeout))
        self.ws_timeout = max(0.05, float(ws_timeout))
        self.reconnect_min = max(0.05, float(reconnect_min))
        self.reconnect_max = max(self.reconnect_min, float(reconnect_max))
        self.seek_threshold = max(0.5, float(seek_threshold))
        # Keep WebSocket command handling responsive while avoiding a tight
        # 50 ms loop that performs several synchronous mpv IPC reads each time.
        self.snapshot_interval = max(0.05, float(snapshot_interval))
        self._clock = clock or time.monotonic
        self._stop_event = threading.Event()
        self._thread = None
        self._report_thread = None
        self._command_thread = None
        self._ws = None
        self._generation = 0
        self._ws_generation = None
        self._ws_lock = threading.RLock()
        self._snapshot_lock = threading.RLock()
        self._report_lock = threading.RLock()
        self._report_wakeup = threading.Event()
        self._stop_report_requested = threading.Event()
        self._command_queue = queue.Queue(maxsize=_COMMAND_QUEUE_MAXSIZE)
        self._in_command_worker = False
        self._started = False
        self._stopped_reported = False
        self._session_capabilities_declared = False
        self._initial_playing_reported = False
        self._next_capabilities_attempt_at = 0.0
        self._capabilities_generation = None
        self._capabilities_attempt = 0
        self._last_snapshot = None
        self._last_report_at = None
        self._last_snapshot_at = None
        self._last_position = None
        self._last_pause = None
        self._last_playback_rate = 1.0
        self._last_heartbeat_at = None
        self._last_receive_at = None
        self._last_ping_at = None
        self._last_pong_at = None
        self._pending_ping = None
        self._pending_ping_at = None
        self._ping_sequence = 0
        self._control_frame_supported = False
        self._last_close_code = None
        self._last_close_reason_hash = None
        self._ws_started_at = None
        self._next_connect_at = 0.0
        self._next_snapshot_at = 0.0
        self._backoff = self.reconnect_min
        self._reconnect_attempt = 0
        # Direct unit/integration callers historically invoke handle_message
        # without starting the worker.  Once start() is called, messages are
        # accepted only from the active socket generation.
        self._transport_state_required = False

    @property
    def thread(self):
        return self._thread

    @property
    def report_thread(self):
        return self._report_thread

    @property
    def command_thread(self):
        return self._command_thread

    @property
    def control_device_id(self):
        return self.session_api.control_device_id

    @property
    def device_id(self):
        """Alias used by callers that treat the control id as a device id."""

        return self.control_device_id

    @property
    def connection_generation(self):
        """Return the active WebSocket generation for diagnostics/tests."""

        with self._ws_lock:
            return self._ws_generation

    def _connection_is_current(self, ws, generation):
        """Check the ``(socket, generation)`` compare-and-clear contract."""

        with self._ws_lock:
            return (
                ws is not None
                and self._ws is ws
                and self._ws_generation == generation
            )

    def _current_connection(self):
        with self._ws_lock:
            return self._ws, self._ws_generation

    def _connection_diagnostics(self, ws=None, generation=None, now=None):
        """Build bounded, monotonic WebSocket diagnostics without secrets."""

        now = self._clock() if now is None else float(now)
        with self._ws_lock:
            if ws is None:
                ws = self._ws
            if generation is None and ws is self._ws:
                generation = self._ws_generation
            current = ws is self._ws and self._ws_generation == generation
            started_at = self._ws_started_at if current else None
            last_receive_at = self._last_receive_at if current else None
            last_ping_at = self._last_ping_at if current else None
            last_pong_at = self._last_pong_at if current else None
            close_code = self._last_close_code if current else None
            close_reason_hash = (
                self._last_close_reason_hash if current else None
            )
            control_frames = self._control_frame_supported if current else None
        def age(timestamp):
            if timestamp is None:
                return 'none'
            return f'{max(0.0, now - timestamp):.3f}'
        lifetime = 'none'
        if started_at is not None:
            lifetime = f'{max(0.0, now - started_at):.3f}'
        return {
            'generation': generation if generation is not None else 'none',
            'lifetime': lifetime,
            'since_receive': age(last_receive_at),
            'since_ping': age(last_ping_at),
            'since_pong': age(last_pong_at),
            'close_code': close_code if close_code is not None else 'none',
            'close_reason_hash': close_reason_hash or 'none',
            'pong_validation': 'enabled' if control_frames else 'unavailable',
        }

    @staticmethod
    def _error_label(exc):
        """Map exceptions to a fixed safe label; never log their message."""

        if isinstance(exc, _HeartbeatTimeout):
            return 'heartbeat_timeout'
        if isinstance(exc, ConnectionResetError):
            return 'connection_reset'
        if isinstance(exc, (ConnectionAbortedError, BrokenPipeError)):
            return 'connection_aborted'
        if isinstance(exc, (socket.timeout, TimeoutError)):
            return 'timeout'
        if isinstance(exc, ConnectionError):
            return 'connection_error'
        return 'socket_error'

    @staticmethod
    def _error_number(exc, *names):
        for name in names:
            value = getattr(exc, name, None)
            if isinstance(value, int):
                return value
        return 'none'

    @property
    def play_session_id(self):
        return self.session_api.play_session_id

    def is_enabled(self):
        """Return whether the opt-in switch and supported playback are active."""

        return bool(
            self.enabled
            and self.player is not None
            and str(self.data.get('server', 'emby')).lower() == 'emby'
        )

    def _load_websocket_factory(self):
        if self.ws_factory is not None:
            return self.ws_factory
        # websocket-client is intentionally imported only when this optional
        # feature is enabled.  Missing dependency therefore cannot affect the
        # regular player startup path.
        try:
            websocket = importlib.import_module('websocket')
        except Exception:
            websocket = _load_bundled_websocket()
        if websocket is None:
            return None
        factory = getattr(websocket, 'create_connection', None)
        if not callable(factory):
            logger.info('remote-control disabled: websocket-client create_connection unavailable')
            return None
        return factory

    def start(self):
        """Start the report, command, and WebSocket workers."""

        if not self.is_enabled() or self._started:
            return bool(self._started)
        self.ws_factory = self._load_websocket_factory()
        if self.ws_factory is None:
            return False
        self._discard_pending_commands()
        self._stop_event.clear()
        self._report_wakeup.clear()
        self._stop_report_requested.clear()
        self._stopped_reported = False
        self._transport_state_required = True
        self._started = True
        self._report_thread = threading.Thread(
            target=self._report_run, name='emby-report-state', daemon=True,
        )
        self._command_thread = threading.Thread(
            target=self._command_run, name='emby-remote-command', daemon=True,
        )
        self._thread = threading.Thread(
            target=self._run, name='emby-remote-control', daemon=True,
        )
        self._report_thread.start()
        self._command_thread.start()
        self._thread.start()
        return True

    def _discard_pending_commands(self):
        while True:
            try:
                self._command_queue.get_nowait()
            except queue.Empty:
                return
            else:
                self._command_queue.task_done()

    def stop(self, timeout=5.0):
        """Report Stopped, then close the WebSocket and join the worker."""

        if not self._started:
            if self._last_snapshot is not None:
                self._report_stopped()
            return
        self._stop_event.set()
        # Capture/send the final state before closing the socket.  The HTTP
        # call is independent of WebSocket state and is retried by the helper.
        # Setting the event first prevents a fresh progress poll from starting
        # after the final Stopped event.
        self._report_stopped()
        wait_timeout = max(0.0, float(timeout))
        ws_thread = self._thread
        report_thread = self._report_thread
        command_thread = self._command_thread
        for thread in (ws_thread, report_thread, command_thread):
            if thread and thread is not threading.current_thread():
                thread.join(timeout=wait_timeout)
        if ws_thread and ws_thread.is_alive():
            # A custom test socket or a broken third-party socket may ignore a
            # timeout.  Closing it unblocks recv and lets the daemon exit.
            self._close_ws()
            if ws_thread is not threading.current_thread():
                ws_thread.join(timeout=wait_timeout)
        self._started = False
        self._thread = None
        self._report_thread = None
        self._command_thread = None

    def _close_ws(self, ws=None, generation=None):
        """Close a socket only after an identity-checked state detach.

        Passing ``ws`` and ``generation`` is required for worker-side cleanup.
        A stale receive loop can still close its own old socket, but it cannot
        clear or close the current connection registered by a newer loop.
        """

        with self._ws_lock:
            current_ws = self._ws
            current_generation = self._ws_generation
            if ws is None:
                ws = current_ws
                generation = current_generation
            if current_ws is not ws or current_generation != generation:
                # The old socket is no longer current.  Close it below without
                # touching any state belonging to the replacement generation.
                details = self._connection_diagnostics(ws, generation)
                close_target = not (
                    current_ws is ws and current_generation != generation
                )
            else:
                details = self._connection_diagnostics(ws, generation)
                close_target = True
                self._ws = None
                self._ws_generation = None
                self._ws_started_at = None
                self._last_receive_at = None
                self._last_ping_at = None
                self._last_pong_at = None
                self._pending_ping = None
                self._pending_ping_at = None
                self._control_frame_supported = False
                self._last_close_code = None
                self._last_close_reason_hash = None
        if ws is None or not close_target:
            return False
        try:
            ws.close()
        except Exception:
            pass
        logger.info(
            'remote-control websocket status=closed '
            f"generation={details['generation']} "
            f"lifetime={details['lifetime']} "
            f"pong_validation={details['pong_validation']} "
            f"close_code={details['close_code']} "
            f"close_reason_hash={details['close_reason_hash']}"
        )
        return True

    def _connect_ws(self):
        factory = self.ws_factory
        if factory is None:
            return None
        # websocket-client uses ``header``.  The fallback signatures keep the
        # same client easy to fake in tests and support wrappers using
        # ``headers`` or positional-only URL arguments.
        # websocket-client 1.8.0's default socket options already enable
        # SO_KEEPALIVE.  Leave ``sockopt`` unspecified so wrappers remain
        # compatible; Windows TCP_KEEPIDLE/KEEPINTVL probes are intentionally
        # not treated as the WebSocket liveness mechanism here.
        call_variants = (
            {'timeout': self.ws_timeout, 'header': self.session_api.websocket_headers},
            {'timeout': self.ws_timeout, 'headers': self.session_api.websocket_headers},
            {'header': self.session_api.websocket_headers},
            {'headers': self.session_api.websocket_headers},
            {'timeout': self.ws_timeout},
            {},
        )
        ws = None
        last_error = None
        for kwargs in call_variants:
            try:
                ws = factory(self.session_api.websocket_url, **kwargs)
                break
            except TypeError as exc:
                last_error = exc
        if ws is None:
            if last_error:
                raise last_error
            raise ConnectionError('websocket factory returned no connection')
        connected_at = self._clock()
        with self._ws_lock:
            if self._ws is not None:
                # The current implementation has one worker and one socket.
                # Refuse an accidental overlapping connection instead of
                # allowing two receive loops to race over shared state.
                if ws is not self._ws:
                    try:
                        ws.close()
                    except Exception:
                        pass
                raise ConnectionError('websocket connection already active')
            self._generation += 1
            generation = self._generation
            self._ws = ws
            self._ws_generation = generation
            self._ws_started_at = connected_at
            self._last_receive_at = connected_at
            self._last_ping_at = None
            self._last_pong_at = None
            self._pending_ping = None
            self._pending_ping_at = None
            self._ping_sequence = 0
            self._control_frame_supported = bool(
                callable(getattr(ws, 'recv_data', None))
                or callable(getattr(ws, 'recv_frame', None))
            )
            self._last_close_code = None
            self._last_close_reason_hash = None
            self._capabilities_generation = generation
            self._capabilities_attempt = 0
            self._session_capabilities_declared = False
            self._next_capabilities_attempt_at = 0.0
        if self._stop_event.is_set() or not self._connection_is_current(ws, generation):
            self._close_ws(ws, generation)
            raise ConnectionError('websocket connection cancelled')
        if not self._send_identity(ws, generation):
            self._close_ws(ws, generation)
            raise ConnectionError('websocket identity send failed')
        logger.info(
            'remote-control websocket connected '
            f'generation={generation} '
            f'reconnect_attempt={self._reconnect_attempt} '
            f'device_hash={_short_hash(self.session_api.control_device_id)}'
        )
        # A new WebSocket is a fresh opportunity to resolve the server-side
        # session immediately, even if the previous connection was throttled.
        self._declare_capabilities(generation=generation)
        with self._ws_lock:
            if self._ws is not ws or self._ws_generation != generation:
                raise ConnectionError('websocket connection replaced')
            self._backoff = self.reconnect_min
            self._next_connect_at = 0.0
            self._last_heartbeat_at = self._clock()
        return ws

    def _send_identity(self, ws, generation=None):
        # Emby.ApiClient's ApiWebSocket expects a pipe-delimited identity,
        # rather than JSON in Data: ClientName|DeviceId|Version|DeviceName.
        identity = '|'.join((
            str(self.session_api.client_name),
            str(self.session_api.control_device_id),
            str(self.session_api.client_version),
            str(self.session_api.device_name),
        ))
        sent = self._send_ws(ws, {
            'MessageType': 'Identity',
            'Data': identity,
        })
        logger.info(
            'remote-control websocket identity '
            f'status={"ok" if sent else "failed"} '
            f'generation={generation if generation is not None else "none"} '
            f'device_hash={_short_hash(self.session_api.control_device_id)}'
        )
        return sent

    def _send_ws(self, ws, payload):
        try:
            ws.send(json.dumps(payload, separators=(',', ':')))
            return True
        except Exception:
            return False

    def _declare_capabilities(self, generation=None):
        now = self._clock()
        ws, current_generation = self._current_connection()
        if generation is None:
            generation = current_generation
        if generation is not None and not self._connection_is_current(ws, generation):
            return False
        with self._ws_lock:
            if generation is not None and (
                self._ws is not ws or self._ws_generation != generation
            ):
                return False
            if self._capabilities_generation != generation:
                self._capabilities_generation = generation
                self._capabilities_attempt = 0
            if (
                self._session_capabilities_declared
                or not self._initial_playing_reported
                or now < self._next_capabilities_attempt_at
            ):
                return False
            attempt = self._capabilities_attempt + 1
        if attempt > _CAPABILITIES_MAX_ATTEMPTS:
            with self._ws_lock:
                if generation is not None and (
                    self._ws is not ws or self._ws_generation != generation
                ):
                    return False
                self._next_capabilities_attempt_at = float('inf')
            logger.info(
                'remote-control capabilities status=exhausted '
                f'generation={generation if generation is not None else "none"} '
                f'attempt={self._capabilities_attempt} '
                'duration_ms=0'
            )
            return False
        with self._ws_lock:
            if generation is not None and (
                self._ws is not ws or self._ws_generation != generation
            ):
                return False
            self._capabilities_attempt = attempt
        started_at = now
        logger.info(
            'remote-control capabilities status=start '
            f'generation={generation if generation is not None else "none"} '
            f'attempt={attempt}'
        )

        def finish(status, *, reason=None, session_id=None, retry=True):
            duration_ms = int(max(0.0, self._clock() - started_at) * 1000)
            current = True
            with self._ws_lock:
                if generation is not None and (
                    self._ws is not ws or self._ws_generation != generation
                ):
                    current = False
                elif retry and attempt < _CAPABILITIES_MAX_ATTEMPTS:
                    self._next_capabilities_attempt_at = (
                        self._clock() + _CAPABILITIES_RETRY_INTERVAL
                    )
                else:
                    self._next_capabilities_attempt_at = float('inf')
            if not current:
                return
            fields = [
                'remote-control capabilities',
                f'status={status}',
                f'generation={generation if generation is not None else "none"}',
                f'attempt={attempt}',
                f'duration_ms={duration_ms}',
            ]
            if reason:
                fields.append(f'reason={_safe_label(reason)}')
            if session_id:
                fields.append(f'session_hash={_short_hash(session_id)}')
            logger.info(' '.join(fields))

        try:
            # The first Playing report creates the server-side session.  Look
            # it up explicitly before advertising capabilities so stale
            # control sessions cannot receive the declaration.
            find_session = getattr(self.session_api, 'find_session', None)
            if not callable(find_session):
                raise RuntimeError('session lookup unavailable')
            try:
                session = find_session(self.play_session_id)
            except TypeError:
                # Preserve compatibility with older small API doubles whose
                # lookup accepted only keyword/default arguments.
                session = find_session()
            session_id = None
            if isinstance(session, dict):
                session_id = (
                    _field(session, 'Id')
                    or _field(session, 'SessionId')
                )
            elif session:
                # Legacy lookup helpers may return a truthy sentinel while
                # storing the concrete id on the API object.
                session_id = getattr(self.session_api, 'session_id', None)
            if session_id is None or not str(session_id).strip():
                finish('unavailable', reason='session_not_found')
                return False
            session_id = str(session_id).strip()
            declare_capabilities = getattr(
                self.session_api, 'declare_capabilities', None
            )
            if not callable(declare_capabilities):
                raise RuntimeError('capability declaration unavailable')
            try:
                declare_capabilities(session_id=session_id, full=True)
            except TypeError:
                # Older clients bind through their ``session_id`` property and
                # do not expose the explicit argument.  Keep that binding.
                try:
                    self.session_api.session_id = session_id
                except Exception:
                    pass
                declare_capabilities(full=True)
            if generation is not None and not self._connection_is_current(ws, generation):
                # A declaration response from an old socket must not mark the
                # replacement socket as capable.
                return False
            with self._ws_lock:
                if generation is not None and (
                    self._ws is not ws or self._ws_generation != generation
                ):
                    return False
                self._session_capabilities_declared = True
                self._next_capabilities_attempt_at = 0.0
            duration_ms = int(max(0.0, self._clock() - started_at) * 1000)
            logger.info(
                'remote-control capabilities status=declared '
                f'generation={generation if generation is not None else "none"} '
                f'attempt={attempt} duration_ms={duration_ms} '
                f'session_hash={_short_hash(session_id)}'
            )
            return True
        except Exception:
            # Keep this retryable on the same generation for a bounded number
            # of attempts while avoiding IDs, URLs and credentials that an
            # exception message might contain.
            finish('failed', reason='declaration_error')
            return False

    def _schedule_reconnect(self, ws=None, generation=None):
        """Schedule one reconnect, guarded by the active socket identity."""

        now = self._clock()
        with self._ws_lock:
            current_ws, current_generation = self._ws, self._ws_generation
            if ws is None:
                ws, generation = current_ws, current_generation
            elif current_ws is not ws or current_generation != generation:
                # A stale receive loop may finish after a newer connection is
                # installed.  It must not reset the newer connection's backoff
                # or capability state.
                return False
            delay = self._backoff
            self._next_connect_at = now + delay
            self._backoff = min(self.reconnect_max, self._backoff * 2)
            self._reconnect_attempt += 1
            # Capabilities belong to the active Emby control connection.  A
            # fresh socket must advertise them again, while any failed
            # declaration stays retryable on the same socket through the
            # throttled path above.
            self._session_capabilities_declared = False
        details = self._connection_diagnostics(ws, generation, now=now)
        self._close_ws(ws, generation)
        logger.info(
            'remote-control websocket reconnect '
            f'generation={details["generation"]} '
            f'attempt={self._reconnect_attempt} '
            f'backoff={delay:.3f} '
            f'lifetime={details["lifetime"]}'
        )
        return True

    @staticmethod
    def _is_timeout_error(exc):
        if isinstance(exc, (socket.timeout, TimeoutError)):
            return True
        name = type(exc).__name__.lower()
        return 'timeout' in name or 'timedout' in name

    @staticmethod
    def _opcode(value):
        if isinstance(value, int):
            return value
        normalized = str(value or '').strip().lower()
        return {
            'continuation': _WS_OPCODE_CONTINUATION,
            'text': _WS_OPCODE_TEXT,
            'binary': _WS_OPCODE_BINARY,
            'close': _WS_OPCODE_CLOSE,
            'ping': _WS_OPCODE_PING,
            'pong': _WS_OPCODE_PONG,
        }.get(normalized)

    @staticmethod
    def _payload_bytes(value):
        if isinstance(value, bytes):
            return value
        if isinstance(value, bytearray):
            return bytes(value)
        if value is None:
            return b''
        return str(value).encode('utf-8', 'replace')

    @staticmethod
    def _close_frame_details(data):
        """Return a numeric close code and a hash-only reason diagnostic."""

        code = None
        reason = None
        if isinstance(data, dict):
            code = _field(data, 'code') or _field(data, 'Code')
            reason = _field(data, 'reason') or _field(data, 'Reason')
        else:
            raw = RemoteControlClient._payload_bytes(data)
            if len(raw) >= 2:
                code = int.from_bytes(raw[:2], 'big')
                reason = raw[2:].decode('utf-8', 'replace')
        try:
            code = int(code) if code is not None else None
        except (TypeError, ValueError):
            code = None
        return code, _short_hash(reason) if reason else None

    def _capture_close_attributes(self, ws, generation):
        """Use wrapper-provided close metadata when no close frame was read."""

        code = getattr(ws, 'close_code', None)
        if code is None:
            code = getattr(ws, 'close_status', None)
        reason = getattr(ws, 'close_reason', None)
        if reason is None:
            reason = getattr(ws, 'reason', None)
        try:
            code = int(code) if code is not None else None
        except (TypeError, ValueError):
            code = None
        reason_hash = _short_hash(reason) if reason else None
        if code is None and reason_hash is None:
            return
        with self._ws_lock:
            if self._ws is ws and self._ws_generation == generation:
                if self._last_close_code is None:
                    self._last_close_code = code
                if self._last_close_reason_hash is None:
                    self._last_close_reason_hash = reason_hash

    def _mark_received(self, ws, generation, now=None):
        now = self._clock() if now is None else float(now)
        with self._ws_lock:
            if self._ws is not ws or self._ws_generation != generation:
                return False
            self._last_receive_at = now
        return True

    def _mark_pong(self, ws, generation, data, now=None):
        now = self._clock() if now is None else float(now)
        with self._ws_lock:
            if self._ws is not ws or self._ws_generation != generation:
                return False
            self._last_pong_at = now
            pending = self._pending_ping
            if pending is not None and (
                self._payload_bytes(data) == self._payload_bytes(pending)
            ):
                self._pending_ping = None
                self._pending_ping_at = None
        return True

    def _consume_ws_frame(self, ws, generation, frame, *, respond_to_ping=False):
        """Consume one control/data frame and return application payloads."""

        if frame is None:
            return None
        opcode = None
        data = frame
        if isinstance(frame, tuple) and len(frame) >= 2:
            opcode, data = frame[0], frame[1]
        elif not isinstance(frame, (str, bytes, bytearray, dict)):
            opcode = getattr(frame, 'opcode', None)
            data = getattr(frame, 'data', frame)
        opcode = self._opcode(opcode)
        self._mark_received(ws, generation)
        if opcode == _WS_OPCODE_PONG:
            self._mark_pong(ws, generation, data)
            return None
        if opcode == _WS_OPCODE_PING:
            if respond_to_ping:
                pong = getattr(ws, 'pong', None)
                if callable(pong):
                    pong(data)
            return None
        if opcode == _WS_OPCODE_CLOSE:
            code, reason_hash = self._close_frame_details(data)
            with self._ws_lock:
                if self._ws is ws and self._ws_generation == generation:
                    self._last_close_code = code
                    self._last_close_reason_hash = reason_hash
            raise ConnectionError('remote-control close frame')
        # A data frame (or a recv()-only wrapper with no opcode) is returned to
        # the command decoder.  Continuation handling remains websocket-client
        #'s responsibility, as recv_data() already reassembles it.
        return data

    def _recv_ws(self, ws, generation=None):
        if generation is None:
            _, generation = self._current_connection()
        try:
            recv_data = getattr(ws, 'recv_data', None)
            if callable(recv_data):
                auto_pong = False
                try:
                    frame = recv_data(control_frame=True)
                    # websocket-client 1.8.0's control_frame path handles
                    # incoming PING frames itself.  Do not send a duplicate
                    # Pong here.
                    auto_pong = True
                except TypeError:
                    # A small wrapper may expose recv_data() without the
                    # websocket-client keyword while still returning frames.
                    try:
                        frame = recv_data(True)
                    except TypeError:
                        frame = recv_data()
                return self._consume_ws_frame(
                    ws, generation, frame, respond_to_ping=not auto_pong,
                )
            recv_frame = getattr(ws, 'recv_frame', None)
            if callable(recv_frame):
                return self._consume_ws_frame(
                    ws, generation, recv_frame(), respond_to_ping=True,
                )
            # Compatibility path for existing simple fakes/wrappers that only
            # expose recv().  Such a socket cannot prove Pong matching, so the
            # bounded heartbeat check is enabled only when control frames are
            # available; send/recv errors still trigger reconnects.
            return self._consume_ws_frame(
                ws, generation, ws.recv(), respond_to_ping=True,
            )
        except Exception as exc:
            if self._is_timeout_error(exc):
                return None
            raise

    def _heartbeat(self, ws, now, generation=None):
        # Emby can mark a session ineligible as soon as its server-side
        # command path is gone.  The client observes a separate event: the
        # receive call must first reach its timeout and the Pong deadline must
        # then expire.  With the defaults (5s interval, 5s deadline, 0.5s
        # receive timeout and 50ms loop wait), this is a bounded ~10.55s local
        # observation window; it cannot identify which network component sent
        # a TCP reset.
        if self._stop_event.is_set():
            return False
        if generation is None:
            _, generation = self._current_connection()
        if not self._connection_is_current(ws, generation):
            return False
        with self._ws_lock:
            pending_at = self._pending_ping_at
            pending = self._pending_ping
            last_heartbeat_at = self._last_heartbeat_at
            control_frames = self._control_frame_supported
        if pending is not None and pending_at is not None:
            if now - pending_at >= self.heartbeat_timeout:
                with self._ws_lock:
                    if (
                        self._ws is ws
                        and self._ws_generation == generation
                        and self._pending_ping == pending
                        and self._pending_ping_at == pending_at
                    ):
                        raise _HeartbeatTimeout(
                            'remote-control Pong deadline exceeded'
                        )
            return False
        if last_heartbeat_at is not None and (
            now - last_heartbeat_at < self.heartbeat_interval
        ):
            return False
        sent = False
        ping_payload = None
        ping = getattr(ws, 'ping', None)
        if callable(ping):
            with self._ws_lock:
                self._ping_sequence += 1
                ping_payload = (
                    f'etlp-{generation}-{self._ping_sequence}'
                ).encode('ascii')
            try:
                ping(ping_payload)
                sent = True
            except TypeError:
                # Existing wrappers/fakes may expose ping() without a payload.
                ping()
                sent = True
                ping_payload = None
        else:
            # Keep the historical application-level fallback for wrappers that
            # do not expose ping().  It is not treated as a Pong acknowledgement.
            sent = self._send_ws(ws, {'MessageType': 'KeepAlive', 'Data': ''})
            ping_payload = None
        if not sent:
            raise ConnectionError('remote-control heartbeat failed')
        with self._ws_lock:
            if self._ws is not ws or self._ws_generation != generation:
                return False
            self._last_heartbeat_at = now
            self._last_ping_at = now
            if control_frames and ping_payload is not None:
                self._pending_ping = ping_payload
                self._pending_ping_at = now
            else:
                self._pending_ping = None
                self._pending_ping_at = None
        return True

    def _log_disconnect(self, exc, ws, generation, now=None):
        self._capture_close_attributes(ws, generation)
        details = self._connection_diagnostics(ws, generation, now=now)
        errno = self._error_number(exc, 'errno')
        winerror = self._error_number(exc, 'winerror', 'win_errno')
        logger.info(
            'remote-control websocket status=disconnected '
            f"generation={details['generation']} "
            f"lifetime={details['lifetime']} "
            f"since_receive={details['since_receive']} "
            f"since_ping={details['since_ping']} "
            f"since_pong={details['since_pong']} "
            f"pong_validation={details['pong_validation']} "
            f'error_type={_safe_label(type(exc).__name__)} '
            f'error_label={self._error_label(exc)} '
            f'errno={errno} winerror={winerror} '
            f"close_code={details['close_code']} "
            f"close_reason_hash={details['close_reason_hash']}"
        )

    def _enqueue_ws_message(self, raw_message, ws, generation):
        if not self._connection_is_current(ws, generation):
            message_type, _ = self._decode_message(raw_message)
            logger.info(
                'remote-control command filtered '
                f'reason=stale_websocket_generation '
                f'type={_message_type_label(message_type)}'
            )
            return False
        message_type, _ = self._decode_message(raw_message)
        message_type_label = _message_type_label(message_type)
        try:
            self._command_queue.put_nowait((raw_message, ws, generation))
        except queue.Full:
            logger.info(
                'remote-control command filtered '
                f'reason=command_queue_full type={message_type_label} '
                f'generation={generation}'
            )
            return False
        return True

    def _consume_queued_command(self, item):
        raw_message, ws, generation = item
        if not self._connection_is_current(ws, generation):
            message_type, _ = self._decode_message(raw_message)
            logger.info(
                'remote-control command filtered '
                f'reason=stale_websocket_generation '
                f'type={_message_type_label(message_type)}'
            )
            return False
        self._in_command_worker = True
        try:
            return self.handle_message(
                raw_message, _ws=ws, _generation=generation,
            )
        finally:
            self._in_command_worker = False

    def _command_run(self):
        """Execute queued WebSocket commands outside the I/O worker."""

        while not self._stop_event.is_set():
            try:
                command_item = self._command_queue.get(timeout=0.05)
            except queue.Empty:
                continue
            if self._stop_event.is_set():
                self._command_queue.task_done()
                continue
            try:
                self._consume_queued_command(command_item)
            except Exception as exc:
                logger.info(
                    'remote-control command worker failed '
                    f'error_type={_safe_label(type(exc).__name__)} '
                    f'error_label={self._error_label(exc)}'
                )
            finally:
                self._command_queue.task_done()

    def _report_run(self):
        """Poll mpv and report HTTP state without blocking WebSocket liveness."""

        try:
            try:
                # Send Playing even when the WebSocket is unavailable; HTTP
                # progress and remote commands are independent transports.
                self._report_snapshot(force=True)
            except Exception:
                logger.info(
                    'remote-control report method=report_playing '
                    'status=failed retry=pending'
                )
            self._next_snapshot_at = self._clock() + self.snapshot_interval
            while not self._stop_event.is_set():
                now = self._clock()
                if self._report_wakeup.is_set():
                    self._report_wakeup.clear()
                    if self._stop_report_requested.is_set():
                        self._stop_report_requested.clear()
                        self._report_stopped()
                    else:
                        try:
                            self._report_snapshot(force=True)
                        except Exception:
                            logger.info(
                                'remote-control report method=report_progress '
                                'status=failed retry=pending'
                            )
                else:
                    try:
                        self._poll_snapshot_if_due(now)
                    except Exception:
                        logger.info(
                            'remote-control report method=report_progress '
                            'status=failed retry=pending'
                        )
                        self._next_snapshot_at = now + self.snapshot_interval
                self._stop_event.wait(0.05)
        finally:
            # stop() normally sends this first; this branch covers report
            # worker failures and callers that only set the event in a test.
            self._report_stopped()

    def _run(self):
        ws = None
        generation = None
        try:
            while not self._stop_event.is_set():
                now = self._clock()
                if ws is None and now >= self._next_connect_at:
                    try:
                        ws = self._connect_ws()
                        _, generation = self._current_connection()
                    except Exception as exc:
                        logger.info(
                            'remote-control websocket status=connect_failed '
                            f'error_type={_safe_label(type(exc).__name__)} '
                            f'error_label={self._error_label(exc)} '
                            f'errno={self._error_number(exc, "errno")} '
                            f'winerror={self._error_number(exc, "winerror", "win_errno")} '
                            f'attempt={self._reconnect_attempt + 1} '
                            f'backoff={self._backoff:.3f}'
                        )
                        current_ws, current_generation = self._current_connection()
                        if current_ws is not None:
                            # A replacement connection won a concurrent
                            # lifecycle race; leave its state untouched.
                            ws, generation = current_ws, current_generation
                        else:
                            self._schedule_reconnect()
                            ws = None
                            generation = None
                if ws is not None:
                    if not self._connection_is_current(ws, generation):
                        ws = None
                        generation = None
                        continue
                    self._declare_capabilities(generation=generation)
                if ws is not None:
                    try:
                        message = self._recv_ws(ws, generation)
                        if (
                            message is not None
                            and not self._stop_event.is_set()
                            and self._connection_is_current(ws, generation)
                        ):
                            self._enqueue_ws_message(message, ws, generation)
                        self._heartbeat(ws, self._clock(), generation)
                    except Exception as exc:
                        self._log_disconnect(exc, ws, generation)
                        self._schedule_reconnect(ws, generation)
                        ws = None
                        generation = None
                self._stop_event.wait(0.05)
        finally:
            if ws is not None:
                self._close_ws(ws, generation)

    def _snapshot(self):
        with self._snapshot_lock:
            return get_mpv_snapshot(self.player)

    def _poll_snapshot_if_due(self, now=None):
        """Poll mpv at a bounded cadence while keeping WS handling frequent."""

        now = self._clock() if now is None else float(now)
        if now < self._next_snapshot_at:
            return False
        self._report_snapshot()
        self._next_snapshot_at = now + self.snapshot_interval
        return True

    def publish_snapshot(self, snapshot, *, force=False, now=None):
        """Publish one externally supplied snapshot (handy for tests)."""

        with self._report_lock:
            return self._publish_snapshot(snapshot, force=force, now=now)

    def _publish_snapshot(self, snapshot, *, force=False, now=None):
        """Apply one snapshot while the report/state lock is held."""

        if not snapshot:
            return False
        now = self._clock() if now is None else float(now)
        try:
            position = float(snapshot.get('position_sec'))
        except (TypeError, ValueError, AttributeError):
            return False
        paused = bool(snapshot.get('is_paused', False))
        try:
            playback_rate = float(snapshot.get('playback_rate', 1.0))
        except (TypeError, ValueError):
            playback_rate = 1.0
        if not playback_rate > 0:
            playback_rate = 1.0
        if self._last_snapshot is None:
            result = self._report(
                'report_playing', position, paused, 'TimeUpdate', playback_rate,
            )
            if result:
                self._last_snapshot = dict(snapshot)
                self._last_position = position
                self._last_pause = paused
                self._last_playback_rate = playback_rate
                self._last_report_at = now
                self._last_snapshot_at = now
                self._initial_playing_reported = True
            else:
                # Keep the initial state unset so a temporary HTTP failure
                # retries Sessions/Playing instead of being misclassified as a
                # later Progress update.
                logger.info(
                    'remote-control report method=report_playing '
                    'status=failed retry=pending'
                )
            return result

        media_title = snapshot.get('media_title')
        if media_title and media_title != self._last_snapshot.get('media_title'):
            if self._switch_playback_item(media_title, position, paused, playback_rate):
                self._last_report_at = now

        previous_position = self._last_position
        previous_pause = self._last_pause
        last_snapshot_at = self._last_snapshot_at if self._last_snapshot_at is not None else now
        last_report_at = self._last_report_at if self._last_report_at is not None else now
        elapsed_since_snapshot = max(0.0, now - last_snapshot_at)
        elapsed_since_report = max(0.0, now - last_report_at)
        expected = ((previous_position or 0.0)
                    + (0.0 if previous_pause else elapsed_since_snapshot))
        seeked = abs(position - expected) >= self.seek_threshold
        pause_changed = paused != previous_pause
        rate_changed = playback_rate != self._last_playback_rate
        due = elapsed_since_report >= self.report_interval
        event_name = 'TimeUpdate'
        if pause_changed:
            event_name = 'Pause' if paused else 'Unpause'
        elif rate_changed:
            event_name = 'PlaybackRateChange'
        if pause_changed or rate_changed or seeked or due or force:
            result = self._report(
                'report_progress', position, paused, event_name, playback_rate,
            )
            if result:
                self._last_report_at = now
            else:
                logger.info(
                    'remote-control report method=report_progress '
                    'status=failed retry=pending'
                )
        else:
            result = False
        self._last_snapshot = dict(snapshot)
        self._last_position = position
        self._last_pause = paused
        self._last_playback_rate = playback_rate
        self._last_snapshot_at = now
        return result

    def _report_snapshot(self, force=False, snapshot=None):
        if snapshot is None:
            snapshot = self._snapshot()
        return self.publish_snapshot(snapshot, force=force)

    def _report(self, method_name, position, paused, event_name, playback_rate=1.0):
        with self._report_lock:
            try:
                method = getattr(self.session_api, method_name)
                method(
                    position_sec=position,
                    is_paused=paused,
                    event_name=event_name,
                    playback_rate=playback_rate,
                )
                logger.info(
                    f'remote-control report method={_safe_label(method_name)} '
                    'status=ok'
                )
                return True
            except TypeError:
                # Keep compatibility with small session fakes written before
                # PlaybackRate was added to the API.
                try:
                    method(
                        position_sec=position,
                        is_paused=paused,
                        event_name=event_name,
                    )
                    logger.info(
                        f'remote-control report method={_safe_label(method_name)} '
                        'status=ok'
                    )
                    return True
                except Exception:
                    logger.info(
                        f'remote-control report method={_safe_label(method_name)} '
                        'status=failed'
                    )
                    return False
            except Exception:
                logger.info(
                    f'remote-control report method={_safe_label(method_name)} '
                    'status=failed'
                )
                return False

    def _switch_playback_item(self, media_title, position, paused, playback_rate):
        """Point the active Emby session at the newly playing playlist episode.

        mpv advances through etlp's playlist without ending the process, so the
        reported ItemId/MediaSourceId must follow the current media-title.
        Otherwise Emby keeps showing the previous episode with a moving
        position until playback stops.
        """
        ep = self.episodes_by_title.get(media_title)
        if not ep:
            return False
        switch = getattr(self.session_api, 'switch_playback_item', None)
        if switch is None:
            return False
        try:
            switch(item_id=ep.get('item_id'), media_source_id=ep.get('media_source_id'))
        except Exception as exc:
            logger.info(f'remote-control item switch failed: {str(exc)[:120]}')
            return False
        result = self._report(
            'report_playing', position, paused, 'TimeUpdate', playback_rate,
        )
        if result:
            logger.info(
                'remote-control switched item '
                f'title_hash={_short_hash(media_title)}'
            )
        return result

    def _report_stopped(self):
        with self._report_lock:
            if self._stopped_reported:
                return False
            snapshot = self._snapshot()
            if snapshot:
                position = snapshot.get('position_sec', self._last_position or 0)
                paused = bool(snapshot.get('is_paused', self._last_pause or False))
                try:
                    playback_rate = float(snapshot.get('playback_rate', self._last_playback_rate))
                except (TypeError, ValueError):
                    playback_rate = self._last_playback_rate
            else:
                position = self._last_position or 0
                paused = bool(self._last_pause)
                playback_rate = self._last_playback_rate
            if not playback_rate > 0:
                playback_rate = 1.0
            try:
                self.session_api.report_stopped(
                    position_sec=position,
                    is_paused=paused,
                    playback_rate=playback_rate,
                )
                self._stopped_reported = True
                logger.info(
                    'remote-control report method=report_stopped status=ok'
                )
                return True
            except TypeError:
                try:
                    self.session_api.report_stopped(
                        position_sec=position,
                        is_paused=paused,
                    )
                    self._stopped_reported = True
                    logger.info(
                        'remote-control report method=report_stopped status=ok'
                    )
                    return True
                except Exception:
                    logger.info(
                        'remote-control report method=report_stopped status=failed'
                    )
                    return False
            except Exception:
                logger.info(
                    'remote-control report method=report_stopped status=failed'
                )
                return False

    def _confirm_pause_state(self, target_paused, timeout=None):
        """Poll mpv until a pause-state command is visible or times out."""

        timeout = _PAUSE_CONFIRM_TIMEOUT if timeout is None else max(0.0, float(timeout))
        deadline = time.monotonic() + timeout
        while True:
            try:
                snapshot = self._snapshot()
            except Exception:
                snapshot = None
            if (
                isinstance(snapshot, dict)
                and isinstance(snapshot.get('is_paused'), bool)
                and snapshot['is_paused'] == bool(target_paused)
            ):
                return snapshot
            if time.monotonic() >= deadline or self._stop_event.is_set():
                return None
            self._stop_event.wait(
                min(_PAUSE_CONFIRM_INTERVAL, max(0.0, deadline - time.monotonic()))
            )

    def _finish_playstate_command(self, command, handled, snapshot=None):
        """Acknowledge a command and request its report without blocking WS."""

        handled = bool(handled)
        if handled:
            if self._started and not self._in_command_worker:
                # The WebSocket worker must never block on mpv IPC or HTTP.
                # The report worker consumes this edge at its next wake-up.
                if command == 'Stop':
                    self._stop_report_requested.set()
                self._report_wakeup.set()
            else:
                try:
                    if snapshot is None:
                        reported = bool(self._report_snapshot(force=True))
                    else:
                        reported = bool(
                            self._report_snapshot(force=True, snapshot=snapshot)
                        )
                except Exception:
                    reported = False
                if command == 'Stop' and not reported:
                    # A stopped player may no longer expose a snapshot.
                    try:
                        if self._snapshot() is None:
                            self._report_stopped()
                    except Exception:
                        pass
        logger.info(
            f'remote-control command={command} handled={str(handled).lower()}'
        )
        return handled

    @staticmethod
    def _decode_message(raw_message):
        if isinstance(raw_message, bytes):
            raw_message = raw_message.decode('utf-8', 'replace')
        if isinstance(raw_message, str):
            try:
                message = json.loads(raw_message)
            except (TypeError, ValueError):
                return None, {}
        elif isinstance(raw_message, dict):
            message = raw_message
        else:
            return None, {}
        if not isinstance(message, dict):
            return None, {}
        message_type = _field(message, 'MessageType')
        payload = _decode_json_object(_field(message, 'Data', {}))
        # A few Emby-compatible WebSocket wrappers add one extra Data or
        # Playstate/GeneralCommand object.  Unwrap only those known containers;
        # arbitrary nested objects are not treated as commands.
        for nested_name in ('Data', 'Playstate', 'PlayState', 'GeneralCommand'):
            nested = _field(payload, nested_name)
            if isinstance(nested, (dict, str, bytes)):
                decoded = _decode_json_object(nested)
                if decoded:
                    payload = decoded
                    break
        return message_type, payload

    def _belongs_to_playback(self, payload):
        message_session = _field(payload, 'PlaySessionId')
        if message_session is None:
            return True
        if self.play_session_id and str(message_session) == str(self.play_session_id):
            return True
        logger.info(
            'remote-control command filtered '
            'reason=play_session_mismatch '
            f'expected_hash={_short_hash(self.play_session_id)} '
            f'received_hash={_short_hash(message_session)}'
        )
        return False

    def _handle_playstate(self, payload):
        if not self._belongs_to_playback(payload):
            return False
        command = (
            _field(payload, 'Command')
            or _field(payload, 'PlaystateCommand')
            or _field(payload, 'Name')
        )
        command = str(command or '').strip().lower()
        if command in ('pause', 'unpause', 'play'):
            target_paused = command == 'pause'
            try:
                handled = mpv_set_pause(self.player, target_paused)
            except Exception:
                handled = False
            confirmed_snapshot = (
                self._confirm_pause_state(target_paused)
                if handled else None
            )
            handled = bool(handled and confirmed_snapshot is not None)
            return self._finish_playstate_command(
                'Pause' if command == 'pause' else 'Unpause', handled,
                snapshot=confirmed_snapshot,
            )
        if command == 'playpause':
            try:
                snapshot = self._snapshot()
                paused = (
                    snapshot.get('is_paused')
                    if isinstance(snapshot, dict) else None
                )
                if not isinstance(paused, bool):
                    handled = False
                    confirmed_snapshot = None
                else:
                    target_paused = not paused
                    handled = mpv_set_pause(self.player, target_paused)
                    confirmed_snapshot = (
                        self._confirm_pause_state(target_paused)
                        if handled else None
                    )
                    handled = bool(handled and confirmed_snapshot is not None)
            except Exception:
                handled = False
                confirmed_snapshot = None
            return self._finish_playstate_command(
                'PlayPause', handled, snapshot=confirmed_snapshot,
            )
        if command == 'seek':
            ticks = _field(payload, 'SeekPositionTicks')
            if ticks is None:
                ticks = _field(payload, 'PositionTicks')
            position = _field(payload, 'SeekPosition')
            if position is None:
                position = _field(payload, 'SeekPositionSeconds')
            if ticks is not None:
                try:
                    position = float(ticks) / 10 ** 7
                except (TypeError, ValueError):
                    position = None
            if position is None:
                logger.info(
                    'remote-control command filtered '
                    'reason=seek_position_missing'
                )
                return False
            try:
                handled = mpv_seek(self.player, position)
            except Exception:
                handled = False
            return self._finish_playstate_command('Seek', handled)
        if command == 'stop':
            try:
                handled = self.player.command('stop') is not False
            except Exception:
                handled = False
            return self._finish_playstate_command('Stop', handled)
        logger.info(
            'remote-control command filtered '
            f'reason=unsupported_playstate command={_command_label(command)}'
        )
        return False

    def _handle_general_command(self, payload):
        if not self._belongs_to_playback(payload):
            return False
        name = _field(payload, 'Name') or _field(payload, 'Command')
        if str(name or '').lower() != 'displaymessage':
            logger.info(
                'remote-control command filtered '
                f'reason=unsupported_general command={_command_label(name)}'
            )
            return False
        args = _field(payload, 'Arguments') or payload
        if isinstance(args, str):
            args = _decode_json_object(args)
        if not isinstance(args, dict):
            args = {}
        text = (
            _field(args, 'Text')
            or _field(args, 'Message')
            or _field(args, 'Header')
        )
        if text is None:
            logger.info(
                'remote-control command filtered '
                'reason=displaymessage_payload_missing'
            )
            return False
        duration = (
            _field(args, 'TimeoutMs')
            or _field(args, 'Timeout')
            or _field(args, 'Duration')
            or 5000
        )
        try:
            duration = int(duration)
        except (TypeError, ValueError):
            duration = 5000
        try:
            handled = bool(mpv_display_message(self.player, text, duration))
        except Exception:
            handled = False
        logger.info(
            'remote-control command=DisplayMessage '
            f'handled={str(handled).lower()}'
        )
        return handled

    def handle_message(self, raw_message, *, _ws=None, _generation=None):
        """Parse one Emby WebSocket message and apply supported commands."""

        if _ws is not None and not self._connection_is_current(_ws, _generation):
            message_type, _ = self._decode_message(raw_message)
            logger.info(
                'remote-control command filtered '
                f'reason=stale_websocket_generation type={_message_type_label(message_type)}'
            )
            return False
        if self._transport_state_required:
            current_ws, current_generation = self._current_connection()
            if (
                current_ws is None
                or (_ws is not None and (
                    current_ws is not _ws or current_generation != _generation
                ))
            ):
                message_type, _ = self._decode_message(raw_message)
                command = _message_type_label(message_type)
                logger.info(
                    'remote-control command filtered '
                    f'reason=websocket_unavailable type={command}'
                )
                return False

        message_type, payload = self._decode_message(raw_message)
        message_type_label = _message_type_label(message_type)
        logger.info(
            f'remote-control websocket message type={message_type_label}'
        )
        if message_type_label == 'playstate':
            return self._handle_playstate(payload)
        if message_type_label == 'generalcommand':
            return self._handle_general_command(payload)
        if message_type_label == 'forcekeepalive':
            # The next loop iteration sends a ping/KeepAlive promptly.
            if _ws is None:
                self._last_heartbeat_at = 0
            else:
                with self._ws_lock:
                    if self._ws is _ws and self._ws_generation == _generation:
                        self._last_heartbeat_at = 0
            return True
        logger.info(
            f'remote-control websocket message ignored type={message_type_label}'
        )
        return False

    # Keep a couple of descriptive aliases for small integrations and test
    # doubles without exposing their implementation details.
    handle_ws_message = handle_message
    process_message = handle_message
    shutdown = stop


EmbyRemoteControlClient = RemoteControlClient
