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
import socket
import sys
import threading
import time

from utils.configs import MyLogger
from utils.emby_session_api import EmbySessionApi, watch_together_enabled
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
        logger.info('watch-together disabled: bundled websocket-client wheel is missing')
        return None

    digest = hashlib.sha256()
    try:
        with wheel_path.open('rb') as wheel_file:
            for chunk in iter(lambda: wheel_file.read(1024 * 1024), b''):
                digest.update(chunk)
    except OSError:
        logger.info('watch-together disabled: bundled websocket-client wheel is unreadable')
        return None
    if digest.hexdigest().lower() != _BUILTIN_WEBSOCKET_SHA256:
        logger.info('watch-together disabled: bundled websocket-client wheel checksum mismatch')
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
        logger.info('watch-together disabled: bundled websocket-client wheel import failed')
        return None


class WatchTogetherClient:
    """Control and state bridge for one Emby playback session."""

    def __init__(self, data=None, player=None, *, mpv=None, session_api=None,
                 enabled=None, ws_factory=None, report_interval=10.0,
                 heartbeat_interval=20.0, ws_timeout=0.5, reconnect_min=1.0,
                 reconnect_max=30.0, seek_threshold=3.0, clock=None):
        self.data = data or {}
        self.player = player or mpv
        self.enabled = watch_together_enabled(self.data, override=enabled)
        self.session_api = session_api or EmbySessionApi(self.data)
        self.ws_factory = ws_factory
        self.report_interval = max(0.1, float(report_interval))
        self.heartbeat_interval = max(1.0, float(heartbeat_interval))
        self.ws_timeout = max(0.05, float(ws_timeout))
        self.reconnect_min = max(0.05, float(reconnect_min))
        self.reconnect_max = max(self.reconnect_min, float(reconnect_max))
        self.seek_threshold = max(0.5, float(seek_threshold))
        self._clock = clock or time.monotonic
        self._stop_event = threading.Event()
        self._thread = None
        self._ws = None
        self._ws_lock = threading.RLock()
        self._report_lock = threading.RLock()
        self._started = False
        self._stopped_reported = False
        self._session_capabilities_declared = False
        self._initial_playing_reported = False
        self._next_capabilities_attempt_at = 0.0
        self._last_snapshot = None
        self._last_report_at = None
        self._last_snapshot_at = None
        self._last_position = None
        self._last_pause = None
        self._last_playback_rate = 1.0
        self._last_heartbeat_at = None
        self._next_connect_at = 0.0
        self._backoff = self.reconnect_min

    @property
    def thread(self):
        return self._thread

    @property
    def control_device_id(self):
        return self.session_api.control_device_id

    @property
    def device_id(self):
        """Alias used by callers that treat the control id as a device id."""

        return self.control_device_id

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
            logger.info('watch-together disabled: websocket-client create_connection unavailable')
            return None
        return factory

    def start(self):
        """Start the state/WebSocket worker, returning whether it started."""

        if not self.is_enabled() or self._started:
            return bool(self._started)
        self.ws_factory = self._load_websocket_factory()
        if self.ws_factory is None:
            return False
        self._stop_event.clear()
        self._stopped_reported = False
        self._started = True
        self._thread = threading.Thread(
            target=self._run, name='emby-watch-together', daemon=True,
        )
        self._thread.start()
        return True

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
        thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=max(0.0, float(timeout)))
        if thread and thread.is_alive():
            # A custom test socket or a broken third-party socket may ignore a
            # timeout.  Closing it unblocks recv and lets the daemon exit.
            self._close_ws()
            thread.join(timeout=max(0.0, float(timeout)))
        else:
            self._close_ws()
        self._started = False
        self._thread = None

    def _close_ws(self):
        with self._ws_lock:
            ws, self._ws = self._ws, None
        if ws is None:
            return
        try:
            ws.close()
        except Exception:
            pass

    def _connect_ws(self):
        factory = self.ws_factory
        if factory is None:
            return None
        # websocket-client uses ``header``.  The fallback signatures keep the
        # same client easy to fake in tests and support wrappers using
        # ``headers`` or positional-only URL arguments.
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
        with self._ws_lock:
            self._ws = ws
        self._send_identity(ws)
        # A new WebSocket is a fresh opportunity to resolve the server-side
        # session immediately, even if the previous connection was throttled.
        self._next_capabilities_attempt_at = 0.0
        self._declare_capabilities()
        self._backoff = self.reconnect_min
        self._next_connect_at = 0.0
        self._last_heartbeat_at = self._clock()
        return ws

    def _send_identity(self, ws):
        identity = {
            'Client': self.session_api.client_name,
            'Device': self.session_api.device_name,
            'DeviceId': self.session_api.control_device_id,
            'Version': self.session_api.client_version,
        }
        self._send_ws(ws, {
            'MessageType': 'Identity',
            'Data': json.dumps(identity, separators=(',', ':')),
        })

    def _send_ws(self, ws, payload):
        try:
            ws.send(json.dumps(payload, separators=(',', ':')))
            return True
        except Exception:
            return False

    def _declare_capabilities(self):
        now = self._clock()
        if (
            self._session_capabilities_declared
            or not self._initial_playing_reported
            or now < self._next_capabilities_attempt_at
        ):
            return False
        try:
            # The first Playing report creates the server-side session.  Look
            # it up explicitly before advertising capabilities so stale
            # control sessions cannot receive the declaration.
            session = self.session_api.find_session(self.play_session_id)
            if not isinstance(session, dict):
                logger.info('watch-together capabilities unavailable: session not found')
                self._next_capabilities_attempt_at = now + _CAPABILITIES_RETRY_INTERVAL
                return False
            session_id = session.get('Id') or session.get('SessionId')
            if session_id is None or not str(session_id).strip():
                logger.info('watch-together capabilities unavailable: session id missing')
                self._next_capabilities_attempt_at = now + _CAPABILITIES_RETRY_INTERVAL
                return False
            self.session_api.declare_capabilities(
                session_id=str(session_id).strip(), full=True,
            )
            self._session_capabilities_declared = True
            self._next_capabilities_attempt_at = 0.0
            return True
        except Exception:
            # Keep this retryable on reconnect while avoiding IDs, URLs and
            # credentials that an exception message might contain.
            logger.info('watch-together capabilities unavailable: declaration failed')
            self._next_capabilities_attempt_at = now + _CAPABILITIES_RETRY_INTERVAL
            return False

    def _schedule_reconnect(self):
        self._next_connect_at = self._clock() + self._backoff
        self._backoff = min(self.reconnect_max, self._backoff * 2)
        self._close_ws()

    @staticmethod
    def _is_timeout_error(exc):
        if isinstance(exc, (socket.timeout, TimeoutError)):
            return True
        name = type(exc).__name__.lower()
        return 'timeout' in name or 'timedout' in name

    def _recv_ws(self, ws):
        try:
            return ws.recv()
        except Exception as exc:
            if self._is_timeout_error(exc):
                return None
            raise

    def _heartbeat(self, ws, now):
        if now - (self._last_heartbeat_at or 0) < self.heartbeat_interval:
            return
        sent = False
        try:
            ping = getattr(ws, 'ping', None)
            if ping:
                ping()
                sent = True
        except Exception:
            pass
        if not sent:
            sent = self._send_ws(ws, {'MessageType': 'KeepAlive', 'Data': ''})
        if not sent:
            raise ConnectionError('watch-together heartbeat failed')
        self._last_heartbeat_at = now

    def _run(self):
        ws = None
        try:
            # Send Playing even when the first WebSocket connection is
            # temporarily unavailable; HTTP and WS failures are independent.
            self._report_snapshot(force=True)
            while not self._stop_event.is_set():
                now = self._clock()
                if ws is None and now >= self._next_connect_at:
                    try:
                        ws = self._connect_ws()
                    except Exception as exc:
                        logger.info(f'watch-together websocket reconnect: {str(exc)[:120]}')
                        self._schedule_reconnect()
                        ws = None
                self._report_snapshot()
                if ws is not None:
                    self._declare_capabilities()
                if ws is not None:
                    try:
                        message = self._recv_ws(ws)
                        if message:
                            self.handle_message(message)
                        self._heartbeat(ws, now)
                    except Exception as exc:
                        logger.info(f'watch-together websocket disconnected: {str(exc)[:120]}')
                        self._schedule_reconnect()
                        ws = None
                self._stop_event.wait(0.05)
        finally:
            # stop() normally sends this first; this branch covers worker
            # failures and callers that only set the event in a test.
            self._report_stopped()
            self._close_ws()

    def _snapshot(self):
        return get_mpv_snapshot(self.player)

    def publish_snapshot(self, snapshot, *, force=False, now=None):
        """Publish one externally supplied snapshot (handy for tests)."""

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
            self._last_snapshot = dict(snapshot)
            self._last_position = position
            self._last_pause = paused
            self._last_playback_rate = playback_rate
            self._last_report_at = now
            self._last_snapshot_at = now
            result = self._report(
                'report_playing', position, paused, 'TimeUpdate', playback_rate,
            )
            if result:
                self._initial_playing_reported = True
            return result

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
            self._last_report_at = now
        else:
            result = False
        self._last_snapshot = dict(snapshot)
        self._last_position = position
        self._last_pause = paused
        self._last_playback_rate = playback_rate
        self._last_snapshot_at = now
        return result

    def _report_snapshot(self, force=False):
        return self.publish_snapshot(self._snapshot(), force=force)

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
                    return True
                except Exception as exc:
                    logger.info(f'watch-together {method_name} failed: {str(exc)[:120]}')
                    return False
            except Exception as exc:
                logger.info(f'watch-together {method_name} failed: {str(exc)[:120]}')
                return False

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
                return True
            except TypeError:
                try:
                    self.session_api.report_stopped(
                        position_sec=position,
                        is_paused=paused,
                    )
                    self._stopped_reported = True
                    return True
                except Exception as exc:
                    logger.info(f'watch-together stopped report failed: {str(exc)[:120]}')
                    return False
            except Exception as exc:
                logger.info(f'watch-together stopped report failed: {str(exc)[:120]}')
                return False

    def _finish_playstate_command(self, command, handled):
        """Report a successfully applied remote command immediately."""

        handled = bool(handled)
        if handled:
            try:
                reported = bool(self._report_snapshot(force=True))
            except Exception:
                reported = False
            if command == 'Stop' and not reported:
                # A stopped player may no longer expose a snapshot.  Preserve
                # the existing Stopped reporting path in that case.
                try:
                    if self._snapshot() is None:
                        self._report_stopped()
                except Exception:
                    pass
        logger.info(
            f'watch-together command={command} handled={str(handled).lower()}'
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
        message_type = message.get('MessageType') or message.get('messageType')
        payload = message.get('Data', message.get('data', {}))
        if isinstance(payload, str):
            try:
                payload = json.loads(payload) if payload else {}
            except (TypeError, ValueError):
                payload = {'Text': payload}
        if not isinstance(payload, dict):
            payload = {}
        return message_type, payload

    def _belongs_to_playback(self, payload):
        message_session = payload.get('PlaySessionId') or payload.get('playSessionId')
        return not message_session or str(message_session) == str(self.play_session_id)

    def _handle_playstate(self, payload):
        if not self._belongs_to_playback(payload):
            return False
        command = (
            payload.get('Command') or payload.get('command')
            or payload.get('PlaystateCommand') or payload.get('Name')
        )
        command = str(command or '').lower()
        if command in ('pause', 'unpause', 'play'):
            try:
                handled = mpv_set_pause(self.player, command == 'pause')
            except Exception:
                handled = False
            return self._finish_playstate_command(
                'Pause' if command == 'pause' else 'Unpause', handled,
            )
        if command == 'seek':
            ticks = payload.get('SeekPositionTicks')
            if ticks is None:
                ticks = payload.get('PositionTicks')
            position = payload.get('SeekPosition')
            if position is None:
                position = payload.get('SeekPositionSeconds')
            if ticks is not None:
                try:
                    position = float(ticks) / 10 ** 7
                except (TypeError, ValueError):
                    position = None
            if position is None:
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
        return False

    def _handle_general_command(self, payload):
        if not self._belongs_to_playback(payload):
            return False
        name = payload.get('Name') or payload.get('Command') or payload.get('name')
        if str(name or '').lower() != 'displaymessage':
            return False
        args = payload.get('Arguments') or payload.get('arguments') or payload
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except (TypeError, ValueError):
                args = {'Text': args}
        if not isinstance(args, dict):
            args = {}
        text = args.get('Text') or args.get('Message') or args.get('Header')
        if text is None:
            return False
        duration = args.get('TimeoutMs') or args.get('Timeout') or args.get('Duration') or 5000
        try:
            duration = int(duration)
        except (TypeError, ValueError):
            duration = 5000
        return mpv_display_message(self.player, text, duration)

    def handle_message(self, raw_message):
        """Parse one Emby WebSocket message and apply supported commands."""

        message_type, payload = self._decode_message(raw_message)
        message_type = str(message_type or '').lower()
        if message_type == 'playstate':
            return self._handle_playstate(payload)
        if message_type == 'generalcommand':
            return self._handle_general_command(payload)
        if message_type == 'forcekeepalive':
            # The next loop iteration sends a ping/KeepAlive promptly.
            self._last_heartbeat_at = 0
            return True
        return False

    # Keep a couple of descriptive aliases for small integrations and test
    # doubles without exposing their implementation details.
    handle_ws_message = handle_message
    process_message = handle_message
    shutdown = stop


EmbyWatchTogetherClient = WatchTogetherClient
