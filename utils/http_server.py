import json
import multiprocessing
import os
import re
import socket
import subprocess
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler
from http.server import HTTPServer
from socketserver import ThreadingMixIn

from utils.data_parser import parse_received_data_emby, parse_received_data_plex, list_episodes
from utils.clouddrive2_gateway import configure_gateway, gateway
from utils.downloader import DownloadManager
from utils.http_security import (ETLP_PROTOCOL_HEADER, bearer_token_valid,
                                 is_loopback_address, media_url_signature_valid,
                                 protocol_header_valid)
from utils.net_tools import (realtime_playing_request_sender, update_server_playback_progress)
from utils.player_manager import PlayerManager
from utils.players import start_player_func_dict, stop_sec_func_dict
from utils.tools import (configs, MyLogger, open_local_folder, play_media_file,
                         activate_window_by_pid, get_player_cmd, ThreadWithReturnValue,
                         create_sparse_file)

player_is_running = False
logger = MyLogger()
dl_manager = DownloadManager(configs.cache_path, speed_limit=configs.speed_limit)
miss_runtime_start_sec = {}


def get_machine_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('223.5.5.5', 80))
        local_ip = s.getsockname()[0]
        s.close()
    except Exception:
        local_ip = '127.0.0.1'
    return local_ip


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    """Handle requests in a separate thread."""


def run_server(ip='127.0.0.1', port=58000):
    listen_on_localhost = configs.raw.getboolean('dev', 'listen_on_localhost', fallback=True)
    if not listen_on_localhost:
        token = configs.raw.get('dev', 'http_server_token', fallback='').strip()
        if len(token) < 32:
            raise ValueError(
                '[dev] http_server_token is required (at least 32 characters) when '
                'listen_on_localhost = no; generate one with secrets.token_urlsafe(32)'
            )
        ip = get_machine_ip()
    server_address = (ip, port)
    httpd = ThreadingHTTPServer(server_address, UserScriptRequestHandler)
    actual_ip, actual_port = httpd.server_address[:2]
    configure_gateway(f'http://{actual_ip}:{actual_port}')
    logger.info('serving at http://%s:%d' % server_address)
    httpd.serve_forever()


class UserScriptRequestHandler(BaseHTTPRequestHandler):

    MAX_POST_BODY_BYTES = 1024 * 1024
    POST_ROUTES = {
        '/gui', '/gui/', '/dl', '/dl/', '/pl', '/pl/',
        '/embyToLocalPlayer', '/embyToLocalPlayer/',
        '/plexToLocalPlayer', '/plexToLocalPlayer/',
        '/openFolder', '/openFolder/',
        '/playMediaFile', '/playMediaFile/',
        '/action/sparse_file',
    }
    _MEDIA_PATH_RE = re.compile(r'^/send_media_file(?:\.[A-Za-z0-9]+)?$')

    @staticmethod
    def _is_client_disconnect_error(exc):
        """Return whether *exc* is the normal peer-closed HTTP race.

        mpv commonly cancels an in-flight Range request while closing or
        seeking.  On Windows the buffered socket flush performed by
        `BaseHTTPRequestHandler.finish` can then surface WSAECONNRESET
        (10054), even though the request has already been abandoned by the
        client.  Keep this check narrow so unrelated I/O failures still
        reach the normal error reporting path.
        """

        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
            return True
        return isinstance(exc, OSError) and getattr(exc, 'winerror', None) in (10053, 10054, 10058)

    def handle(self):
        try:
            super().handle()
        except OSError as exc:
            if not self._is_client_disconnect_error(exc):
                raise
            logger.debug(f'http client disconnected during request: {exc}')

    def finish(self):
        try:
            super().finish()
        except OSError as exc:
            if not self._is_client_disconnect_error(exc):
                raise
            logger.debug(f'http client disconnected while closing request: {exc}')

    def _request_path(self):
        return urllib.parse.urlparse(getattr(self, 'path', '')).path

    def _client_ip(self):
        address = getattr(self, 'client_address', ('', 0))
        return address[0] if address else ''

    def _server_token(self):
        return configs.raw.get('dev', 'http_server_token', fallback='').strip()

    def log_message(self, _format, *args):
        # BaseHTTPRequestHandler's default request-line log includes the query
        # string.  Keep only the route and status so tokens/signatures cannot
        # accidentally enter the regular request log.
        status = args[1] if len(args) > 1 else ''
        logger.info('http request', self.command, self._request_path(), status, self._client_ip())

    def _send_json_response(self, msg=None, status=200):
        if getattr(self, '_response_sent', False):
            return False
        self._response_sent = True
        payload = {'msg': 'default'} if msg is None else msg
        body = json.dumps(payload).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)
        return True

    def _post_response(self, msg=None, status=200):
        return self._send_json_response(msg, status)

    def _post_resopne(self, msg=None, status=200):
        """Backward-compatible typo alias for the single-response helper."""

        return self._post_response(msg, status)

    def _send_error(self, status, message):
        self._send_json_response({'error': message}, status)

    def _read_json_body(self):
        content_type = self.headers.get('Content-Type', '')
        media_type = content_type.split(';', 1)[0].strip().lower()
        if media_type != 'application/json':
            self._send_error(400, 'Content-Type must be application/json')
            return None

        length_header = self.headers.get('Content-Length')
        if length_header is None:
            self._send_error(400, 'Content-Length is required')
            return None
        try:
            length = int(length_header)
        except (TypeError, ValueError):
            self._send_error(400, 'Content-Length must be an integer')
            return None
        if length < 0:
            self._send_error(400, 'Content-Length must not be negative')
            return None
        if length > self.MAX_POST_BODY_BYTES:
            self._send_error(413, 'request body is too large')
            return None
        body = self.rfile.read(length)
        if len(body) != length:
            self._send_error(400, 'request body is incomplete')
            return None
        try:
            data = json.loads(body.decode('utf-8'))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_error(400, 'request body must be valid JSON')
            return None
        if not isinstance(data, dict):
            self._send_error(400, 'request JSON must be an object')
            return None
        return data

    def _authorize_post(self, path):
        loopback = is_loopback_address(self._client_ip())
        if path == '/action/sparse_file' and not loopback:
            token = self._server_token()
            if not token:
                self._send_error(401, 'Authorization required')
            elif not bearer_token_valid(self.headers.get('Authorization'), token):
                self._send_error(401, 'Authorization invalid')
            else:
                return True
            return False
        if not loopback:
            self._send_error(403, 'local action requires a loopback client')
            return False
        if not protocol_header_valid(self.headers):
            self._send_error(403, f'{ETLP_PROTOCOL_HEADER}: 1 is required')
            return False
        return True

    def _dispatch_post(self, path, data):
        canonical_path = path.rstrip('/') or '/'
        if canonical_path == '/action/sparse_file':
            cache_dir = configs.raw.get('gui', 'server_cache_path', fallback='')
            if not cache_dir:
                logger.error('gui[server_cache_path] missing, check it')
                raise ValueError('server cache path is not configured')
            create_sparse_file(os.path.join(cache_dir, data['name']), data['size'])
            return {'sparse_file': True}

        thread_dict = {
            'play': threading.Thread(target=start_play, args=(data,)),
            'play_check': threading.Thread(target=dl_manager.play_check, args=(data,)),
            'download_play': threading.Thread(target=dl_manager.download_play, args=(data,)),
            'download_not_play': threading.Thread(target=dl_manager.download_play, args=(data, False)),
            'download_only': threading.Thread(target=dl_manager.download_only, args=(data,)),
            'delete_by_id': threading.Thread(target=dl_manager.delete, args=({}, data.get('_id'))),
            'delete': threading.Thread(target=dl_manager.delete, args=(data,)),
            'resume_or_pause': threading.Thread(target=dl_manager.resume_or_pause, args=(data,)),
        }
        [setattr(t, 'daemon', True) for t in thread_dict.values()]

        if canonical_path in ('/gui', '/dl', '/pl'):
            gui_cmd = data.get('gui_cmd')
            if gui_cmd not in thread_dict:
                raise ValueError('unknown gui command')
            logger.info('http action', canonical_path, gui_cmd)
            thread_dict[gui_cmd].start()
            return None

        if canonical_path in ('/embyToLocalPlayer', '/plexToLocalPlayer'):
            if data.get('showTaskManager'):
                from utils.gui import show_task_manager
                # multiprocessing.Process would copy dl_manager and duplicate
                # an active download task; keep this on a daemon thread.
                threading.Thread(target=show_task_manager, daemon=True).start()
                return None
            data = parse_received_data_emby(data) if canonical_path == '/embyToLocalPlayer' \
                else parse_received_data_plex(data)
            logger.info(f"server={data['server']}/{data.get('server_version')} {data['mount_disk_mode']=}")
            if configs.check_str_match(_str=data['netloc'], section='gui', option='except_host'):
                threading.Thread(target=start_play, args=(data,), daemon=True).start()
                return None
            if configs.gui_is_enable:
                if configs.raw.get('gui', 'enable_path'):
                    if not configs.check_str_match(data['file_path'], 'gui', 'enable_path', log_by=False):
                        thread_dict['play'].start()
                        return None
                if configs.raw.getboolean('gui', 'without_confirm', fallback=False):
                    thread_dict['download_play'].start()
                    return None
                from utils.gui import show_ask_button
                logger.info('show ask button')
                if configs.platform != 'Darwin':
                    threading.Thread(target=show_ask_button, args=(data,), daemon=True).start()
                else:
                    multiprocessing.Process(target=show_ask_button, args=(data,), daemon=True).start()
            else:
                thread_dict['play'].start()
            return None

        if canonical_path == '/openFolder':
            open_local_folder(data)
            return None
        if canonical_path == '/playMediaFile':
            play_media_file(data)
            return None
        raise ValueError('route not allowed')

    def do_POST(self):
        path = self._request_path()
        if path not in self.POST_ROUTES:
            self._send_error(404, 'route not found')
            return
        if not self._authorize_post(path):
            return
        data = self._read_json_body()
        if data is None:
            return
        configs.update()
        try:
            response = self._dispatch_post(path, data)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            logger.error('http POST request rejected', path)
            self._send_error(400, 'invalid request data')
            return
        except Exception as exc:
            logger.error('http POST action failed', path, type(exc).__name__)
            self._send_error(400, 'request action failed')
            return
        self._send_json_response(response)

    def do_OPTIONS(self):
        self._send_error(403, 'OPTIONS is not supported')

    @classmethod
    def _is_media_path(cls, path):
        return bool(cls._MEDIA_PATH_RE.fullmatch(path))

    @staticmethod
    def _is_cd2_path(path):
        parts = path.split('/')
        return len(parts) == 3 and parts[1] == 'cd2' and bool(parts[2])

    def _authorize_runtime_get(self):
        if is_loopback_address(self._client_ip()):
            return True
        token = self._server_token()
        if not token:
            self._send_error(401, 'Authorization required')
            return False
        if not bearer_token_valid(self.headers.get('Authorization'), token):
            self._send_error(401, 'Authorization invalid')
            return False
        return True

    def do_GET(self):
        path = self._request_path()
        if path in ('/', '/favicon.ico'):
            self.send_response(200)
            self.send_header('Content-Length', str(len(b'Server is running')))
            self.end_headers()
            self.wfile.write(b'Server is running')
            return
        if self._is_media_path(path):
            self.send_media_file()
            return
        if self._is_cd2_path(path):
            self.send_cd2_file()
            return
        if path == '/miss_runtime_start_sec':
            if self._authorize_runtime_get():
                self.check_miss_runtime_start_sec()
            return
        self._send_error(404, 'route not found')

    def do_HEAD(self):
        path = self._request_path()
        if self._is_media_path(path):
            self.send_media_file()
            return
        if self._is_cd2_path(path):
            self.send_cd2_file()
            return
        self.send_response(404)
        self.end_headers()

    def return_json(self, data):
        self.wfile.write(json.dumps(data).encode('utf8'))

    def parse_get_query(self):
        parsed_path = urllib.parse.urlparse(self.path)
        query = dict(urllib.parse.parse_qsl(parsed_path.query, keep_blank_values=True))
        return parsed_path, query

    def check_miss_runtime_start_sec(self):
        _, query = self.parse_get_query()
        stop_sec = query.get('stop_sec')
        netloc, item_id = query.get('netloc'), query.get('item_id')
        if not netloc or not item_id:
            self._send_error(400, 'netloc and item_id are required')
            return
        key = f'{netloc}-{item_id}'
        if stop_sec is not None and stop_sec != '':
            try:
                value = float(stop_sec)
                if value < 0 or value > 10 * 60 * 60:
                    raise ValueError
                miss_runtime_start_sec[key] = int(value)
            except (TypeError, ValueError):
                self._send_error(400, 'stop_sec must be a valid non-negative number')
                return
        start_sec = miss_runtime_start_sec.get(key, 0)
        self._send_json_response({'start_sec': start_sec})

    def send_media_file(self):
        parsed_path = urllib.parse.urlparse(self.path)
        pairs = urllib.parse.parse_qsl(parsed_path.query, keep_blank_values=True)
        query = dict(pairs)
        required = {'file_path', 'expires', 'sig'}
        if len(pairs) != len(required) or set(query) != required:
            self._send_error(400, 'file_path, expires and sig are required')
            return
        video_path = query['file_path']
        if not media_url_signature_valid(self._server_token(), video_path,
                                         query['expires'], query['sig']):
            self._send_error(403, 'media URL signature invalid or expired')
            return

        video_ext = ['webm', 'mkv', 'flv', 'vob', 'ogv', 'ogg', 'rrc', 'gifv', 'mng', 'mov', 'avi', 'qt', 'wmv', 'yuv',
                     'rm', 'asf', 'amv', 'mp4', 'm4p', 'm4v', 'mpg', 'mp2', 'mpeg', 'mpe', 'mpv', 'm4v', 'svi', '3gp',
                     '3g2', 'mxf', 'roq', 'nsv', 'flv', 'f4v', 'f4p', 'f4a', 'f4b', 'mod']
        sub_ext = ['srt', 'sub', 'ass', 'ssa', 'vtt', 'sbv', 'smi', 'sami', 'mpl', 'txt', 'dks', 'pjs', 'stl', 'usf',
                   'cdg', 'idx', 'ttml']
        valid_ext = tuple(video_ext + sub_ext)

        if not video_path.lower().endswith(valid_ext):
            self._send_error(404, 'media extension not allowed')
            return

        if not os.path.exists(video_path):
            self.send_response(404)
            self.end_headers()
            if self.command != 'HEAD':
                self.wfile.write(b'File not found')
            return

        self._send_local_file(video_path)

    def send_cd2_file(self):
        parsed_path = urllib.parse.urlparse(self.path)
        parts = parsed_path.path.split('/')
        nonce = urllib.parse.unquote(parts[2]) if len(parts) == 3 and parts[1] == 'cd2' else ''
        entry = gateway.pop_entry(nonce) if nonce else None
        if entry is None:
            self.send_response(404)
            self.end_headers()
            return
        cd2_url = gateway.resolve_entry(entry)
        if cd2_url:
            self.send_response(307)
            self.send_header('Location', cd2_url)
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            return
        # CD2 is optional.  Keep the old mounted-file path as a transparent
        # fallback when the proxy is offline, unconfigured, or out of scope.
        self._send_local_file(entry.local_path)

    def _send_local_file(self, video_path):
        if not os.path.isfile(video_path):
            self.send_response(404)
            self.end_headers()
            if self.command != 'HEAD':
                self.wfile.write(b'File not found')
            return

        file_size = os.path.getsize(video_path)
        chunk_size = 8 * 1024 * 1024
        range_header = self.headers.get('Range', None)

        if range_header:
            start, end = self.parse_range_header(range_header, file_size)
            logger.info(f'range={start}-{end} | {video_path}')
            if start is None or end is None:
                self.send_response(416)
                self.send_header('Content-Range', f'bytes */{file_size}')
                self.end_headers()
                return

            self.send_response(206)
            self.send_header('Content-type', 'octet-stream')
            self.send_header('Content-Range', f'bytes {start}-{end}/{file_size}')
            self.send_header('Content-Length', str(end - start + 1))
            self.end_headers()

            if self.command == 'HEAD':
                return
            with open(video_path, 'rb') as file:
                file.seek(start)
                bytes_to_read = end - start + 1
                while bytes_to_read > 0:
                    chunk = file.read(min(chunk_size, bytes_to_read))
                    if not chunk:
                        break
                    try:
                        self.wfile.write(chunk)
                    except ConnectionError:
                        break
                    bytes_to_read -= len(chunk)

        else:
            logger.info(f'range: 0- | {video_path}')
            self.send_response(200)
            self.send_header('Content-type', 'octet-stream')
            self.send_header('Content-Length', str(file_size))
            self.end_headers()

            if self.command == 'HEAD':
                return
            with open(video_path, 'rb') as file:
                while chunk := file.read(chunk_size):
                    try:
                        self.wfile.write(chunk)
                    except ConnectionError:
                        break

    @staticmethod
    def parse_range_header(range_header, file_size):
        """Parse one RFC 7233 byte range, returning an inclusive span.

        A suffix range (``bytes=-N``) addresses the final ``N`` bytes.  The
        caller treats malformed or unsatisfiable ranges as HTTP 416; returning
        ``(None, None)`` keeps that decision in one place.  End offsets beyond
        EOF are clipped, as required for a satisfiable explicit range.
        """
        try:
            file_size = int(file_size)
        except (TypeError, ValueError):
            return None, None
        if file_size < 0 or not isinstance(range_header, str):
            return None, None

        match = re.fullmatch(r'bytes=(\d*)-(\d*)', range_header.strip())
        if not match:
            return None, None
        start_text, end_text = match.groups()
        if not start_text and not end_text:
            return None, None

        if not start_text:
            # A zero-length suffix is not satisfiable.  For a non-empty file,
            # a suffix at least as large as the file simply covers the file.
            suffix_length = int(end_text)
            if suffix_length <= 0 or file_size == 0:
                return None, None
            return max(file_size - suffix_length, 0), file_size - 1

        start = int(start_text)
        if start >= file_size:
            return None, None
        end = int(end_text) if end_text else file_size - 1
        if end < start:
            return None, None
        return start, min(end, file_size - 1)


def start_play(data):
    global player_is_running
    if player_is_running:
        logger.error('player_is_running, skip. You may want to disable one_instance_mode, see detail in config file')
        return
    file_path = data['file_path']
    start_sec = data['start_sec']
    sub_file = data['sub_file']
    media_title = data['media_title']
    mount_disk_mode = data['mount_disk_mode']
    eps_data_thread = ThreadWithReturnValue(target=list_episodes, args=(data,))
    eps_data_thread.start()

    # Keep the original disk-mode decision for subtitle/cache/progress logic,
    # but make the actual player transport HTTP when CD2 supplied the URL.
    player_data = dict(data)
    if data.get('use_strm_cd2_url'):
        player_data['mount_disk_mode'] = False
    cmd = get_player_cmd(media_path=data['media_path'], file_path=file_path, data=player_data)
    player_path = cmd[0]
    player_path_lower = player_path.lower()
    # 播放器特殊处理
    player_is_running = True if configs.raw.getboolean('dev', 'one_instance_mode', fallback=True) else False
    player_alias_dict = {'ddplay': 'dandanplay'}
    legal_player_name = list(start_player_func_dict) + list(player_alias_dict)
    player_name = [i for i in legal_player_name if i in player_path_lower]
    if player_name:
        player_name = player_name[0]
        player_name = player_alias_dict.get(player_name, player_name)
        if configs.check_str_match(_str=data['netloc'], section='playlist', option='enable_host', fallback=True) \
                and player_name in ('mpv', 'vlc', 'mpc', 'potplayer', 'iina') \
                or (player_name == 'dandanplay' and mount_disk_mode):
            player_manager = PlayerManager(data=player_data, player_name=player_name, player_path=player_path)
            player_manager.start_player(cmd=cmd, start_sec=start_sec, sub_file=sub_file, media_title=media_title,
                                        mount_disk_mode=player_data['mount_disk_mode'], data=player_data)
            eps_data = eps_data_thread.join()
            player_manager.playlist_add(eps_data=eps_data)
            player_manager.update_playlist_time_loop()
            player_manager.update_playback_for_eps()
            player_is_running = False
            return

        player_function = start_player_func_dict[player_name]
        stop_sec_kwargs = player_function(cmd=cmd, start_sec=start_sec, sub_file=sub_file, media_title=media_title,
                                          mount_disk_mode=player_data['mount_disk_mode'], data=player_data)
        if 'mpv' in stop_sec_kwargs:
            feedback_manager = PlayerManager(data=data, player_name=player_name, player_path=player_path)
            feedback_manager.player_kwargs = stop_sec_kwargs
            feedback_manager.start_realtime_playing_feedback()
        stop_sec = stop_sec_func_dict[player_name](**stop_sec_kwargs)
        feedback_started = False
        if 'mpv' in stop_sec_kwargs:
            feedback_manager.stop_realtime_playing_feedback()
            feedback_started = data.pop('_playing_feedback_started', False)
        logger.info('stop_sec', stop_sec)
        if stop_sec is None:
            player_is_running = False
            return
        if feedback_started:
            realtime_playing_request_sender(
                data=data, cur_sec=stop_sec, method='end', is_paused=False)
            data['update_success'] = True
        total_sec = data['total_sec']
        progress_percent = stop_sec / total_sec
        if total_sec != 86400 or progress_percent > 0.9:
            update_server_playback_progress(stop_sec=stop_sec, data=data)
        if total_sec == 86400:
            logger.info('skip update progress, cuz miss runtime data, may need to enable playlist')
        eps_data = eps_data_thread.join()
        current_ep = [i for i in eps_data if i['file_path'] == data['file_path']][0]
        current_ep['_stop_sec'] = stop_sec
        if configs.gui_is_enable \
                and progress_percent * 100 > configs.raw.getfloat('gui', 'delete_at', fallback=99.9):
            logger.info('watched, delete cache')
            threading.Thread(target=dl_manager.delete, args=(data,), daemon=True).start()
    else:
        logger.info('run as not support player mod')
        logger.info(cmd)
        player = subprocess.Popen(cmd)
        activate_window_by_pid(player.pid)
    player_is_running = False
