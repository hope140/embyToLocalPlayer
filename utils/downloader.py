import math
import os
import platform
import re
import threading
import time
import typing
import urllib.parse

from utils.configs import configs, MyLogger
from utils.emby_api_thin import EmbyApiThin
from utils.http_security import bearer_header, is_local_http_server_url
from utils.net_tools import requests_urllib, tg_notify
from utils.tools import (load_json_file, dump_json_file, scan_cache_dir, safe_deleter, version_prefer_emby,
                         load_dict_jsons_in_folder, create_sparse_file)

logger = MyLogger()

_RANGE_MAX_ATTEMPTS = 4
_RANGE_RETRY_DELAY = 0.1
_RANGE_RETRY_DELAY_MAX = 4.0


class _RangeRequestError(Exception):
    def __init__(self, message, retryable=False):
        super().__init__(message)
        self.retryable = retryable


def _response_status(response):
    status = getattr(response, 'status', None)
    return status if status is not None else getattr(response, 'code', None)


def _response_header(response, name):
    try:
        value = response.getheader(name)
    except (AttributeError, KeyError, TypeError):
        value = None
    if value is not None:
        return value
    headers = getattr(response, 'headers', None)
    if headers is None:
        return None
    try:
        return headers.get(name) or headers.get(name.lower())
    except (AttributeError, KeyError, TypeError):
        return None


def _close_response(response):
    if response is None:
        return
    try:
        response.close()
    except Exception:
        pass


def _parse_content_range(value):
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r'\s*bytes\s+(\d+)-(\d+)/(\d+)\s*', value, re.IGNORECASE)
    if not match:
        return None
    start, end, total = (int(group) for group in match.groups())
    if end < start or total <= end:
        return None
    return start, end, total


def _is_retryable_download_error(exc):
    return isinstance(exc, (ConnectionError, TimeoutError, OSError)) or getattr(exc, 'retryable', False)


def _wait_download(delay, downloader, maximum=None):
    remaining = float(delay)
    if maximum is not None:
        remaining = min(remaining, maximum)
    while remaining > 0:
        if downloader.cancel or downloader.pause:
            return False
        sleep_for = min(_RANGE_RETRY_DELAY, remaining)
        time.sleep(sleep_for)
        remaining -= sleep_for
    return not downloader.cancel and not downloader.pause


def _wait_download_retry(delay, downloader):
    return _wait_download(delay, downloader, maximum=_RANGE_RETRY_DELAY_MAX)

if platform.system() == 'Windows':
    import msvcrt
else:
    import fcntl


class TaskFileManager:
    def __init__(self, task_path):
        self.task_path = task_path
        self.lock_path = task_path + '.lock'
        self.lock_fd = None
        self.has_lock = False

    def acquire_lock(self, blocking=True):
        self.lock_fd = open(self.lock_path, 'a+')
        try:
            if platform.system() == 'Windows':
                mode = msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK
                msvcrt.locking(self.lock_fd.fileno(), mode, 1)
            else:
                flags = fcntl.LOCK_EX
                if not blocking:
                    flags |= fcntl.LOCK_NB
                fcntl.flock(self.lock_fd, flags)
            self.has_lock = True
            return True
        except (OSError, BlockingIOError):
            return False

    def release_lock(self):
        if self.lock_fd:
            try:
                if platform.system() == 'Windows':
                    msvcrt.locking(self.lock_fd.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(self.lock_fd, fcntl.LOCK_UN)
            finally:
                self.lock_fd.close()
                self.lock_fd = None
                self.has_lock = False

    # def __del__(self):
    #     self.release_lock()


class Downloader:
    def __init__(self, url, _id, size=None, cache_path=None, save_path=None):
        self.id = _id
        self.url = url
        self.file = save_path or os.path.join(cache_path, _id)
        self.file_is_busy = False
        self.download_only = False
        self.cancel = False
        self.pause = False
        self.size = size
        self.chunk_size = 1024 * 1024
        self.progress = 0
        self.is_done = False
        self._range_error = None
        self._range_retryable = False
        self._last_success_position = 0
        if not save_path:
            os.path.exists(cache_path) or os.mkdir(cache_path)

        self.task_file = os.path.join(cache_path, _id + '.json')
        self.file_lock = TaskFileManager(self.task_file)
        if os.path.exists(self.file) and not os.path.exists(self.file_lock.lock_path):
            self.is_done = True
            self.restore_state()
        elif configs.raw.getboolean('gui', 'read_only', fallback=False):
            self.restore_state()
        else:
            lock_acquired = self.file_lock.acquire_lock(blocking=False)
            if lock_acquired:
                logger.info(f'dl: lock success: {_id}')
                if self.restore_state():
                    pass
                else:
                    self.save_state()
            else:
                logger.info(f'dl: lock failed: already locked by another process. \n{_id}')
                self.restore_state()

    def save_state(self):
        state = dict(_id=self.id, stream_url=self.url, size=self.size,
                     download_only=self.download_only, pause=self.pause,
                     progress=self.progress)
        dump_json_file(state, self.task_file)

    def restore_state(self, ):
        state = load_json_file(self.task_file, error_return='dict', silence=True)
        self.progress = state.get('progress', self.progress)
        self.download_only = state.get('download_only', self.download_only)
        self.pause = state.get('pause', self.pause)
        self.size = state.get('size', self.size)
        return state

    def mark_done(self):
        self.is_done = True
        self.file_lock.release_lock()
        os.path.exists(self.file_lock.lock_path) and os.remove(self.file_lock.lock_path)

    def get_size(self):
        if self.size:
            return self.size
        last_error = None
        for attempt in range(_RANGE_MAX_ATTEMPTS):
            resp = None
            try:
                resp = requests_urllib(
                    self.url, http_proxy=configs.dl_proxy, method='HEAD',
                    headers={'Accept-Encoding': 'identity'}, res_only=True, timeout=10,
                    retry=1, silence=True, return_http_error=True)
                status = _response_status(resp)
                if status is not None and not 200 <= status < 300:
                    raise _RangeRequestError(f'HEAD failed with HTTP {status}', retryable=status >= 500)
                content_encoding = _response_header(resp, 'Content-Encoding')
                if content_encoding and content_encoding.strip().lower() != 'identity':
                    raise _RangeRequestError('HEAD response has unsupported Content-Encoding')
                length = _response_header(resp, 'Content-Length')
                if length is None:
                    raise _RangeRequestError('HEAD response has no Content-Length')
                try:
                    size = int(length)
                except (TypeError, ValueError, OverflowError):
                    raise _RangeRequestError('HEAD response has invalid Content-Length') from None
                if size <= 0:
                    raise _RangeRequestError('HEAD response has non-positive Content-Length')
                self.size = size
                return self.size
            except Exception as exc:
                last_error = exc
                if (not _is_retryable_download_error(exc)
                        or attempt + 1 >= _RANGE_MAX_ATTEMPTS
                        or not _wait_download_retry(_RANGE_RETRY_DELAY * (2 ** attempt), self)):
                    raise
            finally:
                _close_response(resp)
        raise last_error

    def range_download(self, start: int, end: int, speed=0, update=False) -> int:
        """Download the half-open byte interval ``[start, end)``.

        The HTTP request uses the inclusive end byte ``end - 1``.  The return
        value is the next byte that still needs to be written, so callers can
        resume a short or interrupted response without rewriting the prefix.
        """
        self._range_error = None
        self._range_retryable = False
        try:
            try:
                read_only = configs.raw.getboolean('gui', 'read_only', fallback=False)
            except Exception as exc:
                self._range_error = _RangeRequestError('read_only configuration unavailable')
                logger.error(f'dl: write mode check failed {type(exc).__name__}')
                return start
            try:
                has_lock = bool(self.file_lock.has_lock)
            except Exception as exc:
                self._range_error = _RangeRequestError('task lock unavailable')
                logger.error(f'dl: lock check failed {type(exc).__name__}')
                return start
            if read_only or not has_lock:
                self._range_error = _RangeRequestError('download requires the task lock and write mode')
                return start
            if self.cancel or self.pause:
                self._range_error = _RangeRequestError('download paused or cancelled')
                return start
            self.get_size()
            if (isinstance(start, bool) or isinstance(end, bool) or start < 0 or end < start
                    or end > self.size):
                self._range_error = _RangeRequestError(f'invalid range {start=}, {end=}')
                return start
            if start == end:
                self._last_success_position = end
                return end

            request_end = end - 1
            headers = {
                'Range': f'bytes={start}-{request_end}',
                'Accept-Encoding': 'identity',
            }
            response = None
            try:
                response = requests_urllib(
                    self.url, headers=headers, http_proxy=configs.dl_proxy, res_only=True,
                    timeout=10, retry=1, silence=True, return_http_error=True)
                status = _response_status(response)
                if status != 206:
                    retryable = status is not None and 500 <= status <= 599
                    self._range_error = _RangeRequestError(
                        f'range request returned HTTP {status}', retryable=retryable)
                    self._range_retryable = retryable
                    return start

                content_range = _parse_content_range(_response_header(response, 'Content-Range'))
                if content_range != (start, request_end, self.size):
                    self._range_error = _RangeRequestError('range response has invalid Content-Range')
                    return start
                content_length = _response_header(response, 'Content-Length')
                if content_length is not None:
                    try:
                        if int(content_length) != end - start:
                            raise ValueError
                    except (TypeError, ValueError, OverflowError):
                        self._range_error = _RangeRequestError('range response has invalid Content-Length')
                        return start
                content_encoding = _response_header(response, 'Content-Encoding')
                if content_encoding and content_encoding.strip().lower() != 'identity':
                    self._range_error = _RangeRequestError('range response has unsupported Content-Encoding')
                    return start

                if start == 0:
                    safe_deleter(self.file)
                open_mode = 'r+b' if os.path.exists(self.file) else 'wb'
                if open_mode == 'wb' and configs.raw.getboolean('gui', 'sparse_file_by_server', fallback=False):
                    if server_href := configs.raw.get('dev', 'server_side_href', fallback=''):
                        sparse_headers = {}
                        if not is_local_http_server_url(server_href):
                            token = configs.raw.get('dev', 'http_server_token', fallback='').strip()
                            if token:
                                sparse_headers['Authorization'] = bearer_header(token)
                        _res = requests_urllib(
                            f'{server_href}/action/sparse_file',
                            _json={'name': self.id, 'size': self.size}, headers=sparse_headers,
                            get_json=True)
                        if not _res.get('sparse_file'):
                            raise _RangeRequestError('server sparse_file fail, check it.')
                        for _ in range(10):
                            if os.path.exists(self.file):
                                open_mode = 'r+b'
                                break
                            print('.', end='')
                            time.sleep(0.3)
                if open_mode == 'wb':
                    create_sparse_file(self.file, size=self.size)
                    open_mode = 'r+b'

                sleep = 1 / speed if speed else 0
                next_position = start
                remaining = end - start
                with open(self.file, open_mode) as file_obj:
                    file_obj.seek(start)
                    logger.trace(f'seek {start=}')
                    try:
                        while remaining > 0:
                            if self.cancel or self.pause:
                                return next_position
                            chunk = response.read(min(self.chunk_size, remaining))
                            if not chunk:
                                self._range_error = _RangeRequestError(
                                    'range response ended before Content-Range')
                                self._range_retryable = True
                                return next_position
                            if len(chunk) > remaining:
                                chunk = chunk[:remaining]
                            written = file_obj.write(chunk)
                            if written != len(chunk):
                                self._range_error = _RangeRequestError('short local file write', retryable=True)
                                self._range_retryable = True
                                next_position += max(0, written or 0)
                                return next_position
                            file_obj.flush()
                            next_position += written
                            remaining -= written
                            if update and self.size:
                                self.progress = min(end / self.size, next_position / self.size)
                            if sleep and not _wait_download(sleep, self):
                                return next_position
                    except Exception as exc:
                        self._range_error = exc
                        self._range_retryable = _is_retryable_download_error(exc)
                        logger.error(f'dl: retry: {self.id} {str(exc)[:50]}')
                        return next_position
                    self._last_success_position = next_position
                    return next_position
            except Exception as exc:
                if self._range_error is None:
                    self._range_error = exc
                self._range_retryable = _is_retryable_download_error(exc)
                logger.error(f'dl: range_download error {self.id} {str(exc)[:50]}')
                return start
            finally:
                _close_response(response)
        except Exception as exc:
            self._range_error = exc
            self._range_retryable = _is_retryable_download_error(exc)
            logger.error(f'dl: range_download error {self.id} {str(exc)[:50]}')
            return start

    def percent_download(self, start, end, speed=0, update=True):
        self.file_is_busy = True
        try:
            if self.cancel or self.pause:
                return False
            try:
                read_only = configs.raw.getboolean('gui', 'read_only', fallback=False)
            except Exception as exc:
                logger.error(f'dl: write mode check failed {type(exc).__name__}')
                return False
            try:
                has_lock = bool(self.file_lock.has_lock)
            except Exception as exc:
                logger.error(f'dl: lock check failed {type(exc).__name__}')
                return False
            if read_only or not has_lock:
                return False
            if (isinstance(start, bool) or isinstance(end, bool)
                    or not isinstance(start, (int, float)) or not isinstance(end, (int, float))
                    or not math.isfinite(start) or not math.isfinite(end)
                    or start < 0 or end > 1 or end < start):
                return False
            self.get_size()
            logger.info(f'dl: start {int(start * 100)}% end {int(end * 100)}% \n{self.id}')
            _start = math.floor(float(self.size * start))
            _end = math.floor(float(self.size * end))
            _start = max(0, min(self.size, _start))
            _end = max(0, min(self.size, _end))
            if _end < _start:
                return False
            if _start == _end:
                self._last_success_position = _end
                return True

            next_position = _start
            for attempt in range(_RANGE_MAX_ATTEMPTS):
                if self.cancel or self.pause:
                    return False
                next_position = self.range_download(next_position, _end, speed=speed, update=update)
                if (next_position >= _end and self._range_error is None
                        and not self.cancel and not self.pause):
                    if update:
                        self.progress = end
                        logger.trace(self.id, end, 'done')
                    return True
                if not self._range_retryable or attempt + 1 >= _RANGE_MAX_ATTEMPTS:
                    return False
                delay = min(_RANGE_RETRY_DELAY_MAX, _RANGE_RETRY_DELAY * (2 ** attempt))
                logger.info(f'dl: percent download error found, sleep {delay}')
                if not _wait_download_retry(delay, self):
                    return False
            return False
        except Exception as exc:
            logger.error(f'dl: percent_download error {self.id} {str(exc)[:50]}')
            return False
        finally:
            self.file_is_busy = False

    def download_fist_last(self):
        previous_progress = self.progress
        prefix_ready = self.percent_download(0, 0.01, update=False)
        if not prefix_ready:
            return False
        tail_ready = self.percent_download(0.99, 1, update=False)
        if not tail_ready:
            self.progress = previous_progress
            return False
        prefix_end = math.floor(self.size * 0.01)
        self.progress = max(previous_progress, prefix_end / self.size if self.size else 0)
        return True

    def cancel_download(self, silence=False):
        self.cancel = True
        while self.file_is_busy:
            time.sleep(1)
        self.file_lock.release_lock()
        done = False
        for f in self.file_lock.lock_path, self.task_file, self.file:
            if os.path.exists(f):
                try:
                    os.remove(f)
                except Exception as e:
                    logger.info(f'dl: file is lock, can not delete on is machine, {str(e)[:30]}\n{self.id}')
                done = True
        done and not silence and logger.info(f'dl: delete done {self.id}')
        return done


_PREFETCH_TIMEOUT = 10
_PREFETCH_RETRY = 1
_PREFETCH_READ_SIZE = 64 * 1024


def _prefetch_header(response, name):
    """Return a response header for urllib responses and test doubles."""
    try:
        value = response.getheader(name)
    except (AttributeError, KeyError, TypeError):
        value = None
    if value is not None:
        return value
    headers = getattr(response, 'headers', None)
    if headers is None:
        return None
    try:
        return headers.get(name) or headers.get(name.lower())
    except (AttributeError, KeyError, TypeError):
        return None


def _prefetch_status(response):
    status = getattr(response, 'status', None)
    return status if status is not None else getattr(response, 'code', None)


def _prefetch_size(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed <= 0:
        return None
    if isinstance(value, float) and (not math.isfinite(value) or parsed != value):
        return None
    return parsed


def _prefetch_content_range(value):
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r'\s*bytes\s+(\d+)-(\d+)/(\d+)\s*', value, re.IGNORECASE)
    if not match:
        return None
    start, end, total = (int(group) for group in match.groups())
    if end < start or total <= end:
        return None
    return start, end, total


def _prefetch_read(response, byte_count):
    read_count = 0
    while read_count < byte_count:
        remaining = byte_count - read_count
        chunk = response.read(min(_PREFETCH_READ_SIZE, remaining))
        if not chunk:
            break
        read_count += min(len(chunk), remaining)
    return read_count == byte_count


def prefetch_http_range(url, start_percent, end_percent, size=None):
    """Best-effort HTTP range prefetch that never creates local state.

    This is intentionally separate from :class:`Downloader`: discard prefetches
    must not create cache files, task files, locks, or sparse files.  A failure
    only skips this hint and must not affect playback.
    """
    try:
        start_percent = float(start_percent)
        end_percent = float(end_percent)
    except (TypeError, ValueError, OverflowError):
        logger.info('discard prefetch skipped: invalid range')
        return False
    if (not math.isfinite(start_percent) or not math.isfinite(end_percent)
            or start_percent < 0 or end_percent > 1 or end_percent <= start_percent):
        logger.info('discard prefetch skipped: invalid range')
        return False

    media_size = _prefetch_size(size)
    if media_size is None and size is not None:
        logger.info('discard prefetch skipped: invalid size')
        return False
    if media_size is None:
        response = None
        try:
            response = requests_urllib(
                url, method='HEAD', http_proxy=configs.dl_proxy,
                res_only=True, timeout=_PREFETCH_TIMEOUT, retry=_PREFETCH_RETRY,
                silence=True)
            status = _prefetch_status(response)
            if status is None or not 200 <= status < 300:
                logger.info('discard prefetch skipped: size request failed')
                return False
            media_size = _prefetch_size(_prefetch_header(response, 'Content-Length'))
        except Exception as exc:
            logger.info(f'discard prefetch skipped: size request {type(exc).__name__}')
            return False
        finally:
            if response is not None:
                try:
                    response.close()
                except Exception:
                    pass
        if media_size is None:
            logger.info('discard prefetch skipped: missing size')
            return False

    start = int(media_size * start_percent)
    end = int(media_size * end_percent) - 1
    end = min(end, media_size - 1)
    if start >= media_size or end < start:
        logger.info('discard prefetch skipped: empty range')
        return False
    byte_count = end - start + 1
    if byte_count >= media_size:
        logger.info('discard prefetch skipped: full range')
        return False

    response = None
    try:
        response = requests_urllib(
            url, headers={'Range': f'bytes={start}-{end}'},
            http_proxy=configs.dl_proxy, res_only=True,
            timeout=_PREFETCH_TIMEOUT, retry=_PREFETCH_RETRY, silence=True)
        status = _prefetch_status(response)
        if status == 206:
            content_range = _prefetch_content_range(_prefetch_header(response, 'Content-Range'))
            if not content_range or content_range != (start, end, media_size):
                logger.info('discard prefetch skipped: invalid content range')
                return False
        elif status == 200 and start == 0:
            # A server may ignore a first-byte Range.  Only consume the requested
            # prefix; a non-zero request must never be treated as a tail hit.
            pass
        else:
            logger.info('discard prefetch skipped: unsupported response')
            return False
        return _prefetch_read(response, byte_count)
    except Exception as exc:
        logger.info(f'discard prefetch skipped: {type(exc).__name__}')
        return False
    finally:
        if response is not None:
            try:
                response.close()
            except Exception:
                pass


class DownloadManager:
    def __init__(self, cache_path, speed_limit=0, max_concurrent=3, per_domain_limit=2):
        self.cache_path = cache_path
        self.tasks = {}
        self.db = {}
        self.db_path = configs.cache_db
        self.update_loop_lock = False
        self.speed_limit = speed_limit
        self.download_semaphore = threading.Semaphore(max_concurrent)
        self.per_domain_limit = per_domain_limit
        self.domain_semaphores = {}
        if configs.gui_is_enable:
            os.path.exists(cache_path) or os.mkdir(cache_path)
            threading.Thread(target=self.update_db_loop, daemon=True).start()
            if configs.raw.getboolean('gui', 'auto_resume', fallback=False):
                threading.Thread(target=self.resume_or_pause, kwargs={'resume_from_db': True}).start()

    def get_domain_semaphore(self, domain: str):
        if domain not in self.domain_semaphores:
            self.domain_semaphores[domain] = threading.BoundedSemaphore(self.per_domain_limit)
        return self.domain_semaphores[domain]

    def _get_fake_init_dl(self, _id, url=None, position=0.1234, get_fake_data=False):
        data = {'stream_url': url,
                'fake_name': _id,
                'position': position}
        if get_fake_data:
            return data
        return self._init_dl(data)

    def _init_dl(self, data, check_only=False):
        url, _id, pos = data['stream_url'], data['fake_name'], data['position']
        dl = self.tasks.get(_id) or Downloader(url, _id, cache_path=self.cache_path, size=data.get('size'))
        download_only = True if dl.download_only or data.get('download_only') else False
        dl.download_only = download_only
        if not check_only and dl.file_lock.has_lock and not self.tasks.get(_id) and not dl.is_done:
            self.tasks[_id] = dl
        logger.trace(f'init_dl {dl.download_only=}')
        return url, _id, pos, dl

    def _percent_download_with_limit(self, dl: Downloader, start, end, update=True):
        domain = urllib.parse.urlparse(dl.url).netloc
        domain_semaphore = self.get_domain_semaphore(domain)
        with self.download_semaphore, domain_semaphore:
            done = dl.percent_download(start=start, end=end, speed=self.speed_limit, update=update)
            return done

    def download_only(self, data):
        url, _id, pos, dl = self._init_dl(data)
        if not dl.file_lock.has_lock:
            logger.info(f'dlm: skip, already locked. {_id}')
            return
        if dl.progress == 1:
            logger.info(f'dlm: skip, already done {_id}')
            return
        dl.download_only = True
        if dl.file_is_busy:
            logger.info(f'dlm: skip, already downloading {_id}')
            return
        if self._percent_download_with_limit(dl, dl.progress, 1):
            dl.download_only = False

    def play_check(self, data):
        url, _id, pos, dl = self._init_dl(data, check_only=True)
        if not dl.download_only and dl.progress > pos:
            data['media_path'] = dl.file
        if not os.path.exists(dl.file):
            dl.cancel_download(silence=True)
        logger.info(f'dlm: play_check {dl.download_only=} {dl.progress=}')
        data['gui_cmd'] = 'play'
        requests_urllib('http://127.0.0.1:58000/gui', _json=data)

    def download_play(self, data, play=True):
        url, _id, pos, dl = self._init_dl(data)
        if dl.download_only:
            logger.info('download only detected, refuse play')
            return
        read_only = configs.raw.getboolean('gui', 'read_only', fallback=False)
        if dl.progress >= pos:
            if not read_only and dl.file_lock.has_lock and dl.progress == 0 and not dl.file_is_busy:
                dl.download_fist_last()
            if play:
                data['media_path'] = dl.file
                data['gui_cmd'] = 'play'
                if configs.raw.getboolean('gui', 'without_confirm', fallback=False):
                    data['gui_without_confirm'] = True
                requests_urllib('http://127.0.0.1:58000/dl', _json=data)
        else:
            if play:
                data['gui_cmd'] = 'play'
                requests_urllib('http://127.0.0.1:58000/dl', _json=data)
                logger.info(f'dlm: fallback to url, cuz: {pos=} > {dl.progress}')
        if not dl.file_is_busy and dl.progress != 1:
            if not dl.file_lock.has_lock:
                logger.info(f'dlm: already locked, skip dl. {_id}')
                return
            if read_only:
                return
            logger.info(f'dlm: start download {dl.id}')
            self._percent_download_with_limit(dl, dl.progress, 1)

    def delete(self, data=None, _id: typing.Union[str, list] = None):
        self.update_loop_lock = True
        dl = None
        if data:  # delete current
            url, _id, pos, dl = self._init_dl(data)
        _ids = _id if isinstance(_id, list) else [_id]
        logger.info(f'dlm: delete ids: {_ids=}')
        for _id in _ids:
            if _id in self.tasks:
                _dl = self.tasks[_id]
                _dl.cancel_download()
                del self.tasks[_id]
            else:
                if dl:  # delete current but not in tasks
                    dl.cancel_download()
                    break
                *_, _dl = self._get_fake_init_dl(_id=_id)
                _dl.cancel_download()
        self.update_loop_lock = False

    def get_all_json_task(self):
        return load_dict_jsons_in_folder(self.cache_path, required_key='_id')

    def resume_or_pause(self, data=None, resume_from_db=False):
        read_only = configs.raw.getboolean('gui', 'read_only', fallback=False)
        if read_only:
            logger.info('dlm: read_only mode, skip resume')
            return

        def fake_tasks_info():
            r = []
            for js in self.get_all_json_task():
                _d = self._get_fake_init_dl(_id=js['_id'], url=js['stream_url'], get_fake_data=True)
                r.append(_d)
            return r

        operate = data['operate'] if not resume_from_db else 'resume'
        data_list = data['data_list'] if not resume_from_db else fake_tasks_info()
        logger.trace(f'{operate=}\n{data_list=}')
        for data in data_list:
            url, _id, pos, dl = self._init_dl(data)
            if dl.progress == 1:
                continue
            if not dl.file_lock.has_lock:
                continue
            if operate == 'pause':
                dl.pause = True
            elif operate == 'resume':
                dl.pause = False
                if dl.progress == 0:
                    dl.download_fist_last()
                threading.Thread(target=self._percent_download_with_limit,
                                 kwargs=dict(dl=dl, start=dl.progress, end=1)).start()

    def cache_size_limit(self):
        limit = int(configs.raw.getint('gui', 'cache_size_limit') * 1024 ** 3)
        dir_info = scan_cache_dir()
        dir_info = [i for i in dir_info if i['stat'].st_size > 10 * 1024 ** 2]
        dir_size = sum([i['stat'].st_size for i in dir_info])
        if dir_size > limit:
            logger.info('out of cache limit')
            dir_info.sort(key=lambda i: i['stat'].st_mtime)
            _id = dir_info[0]['_id']
            self.delete(_id=_id)

    def update_db_loop(self):
        times = 0
        while True:
            if self.update_loop_lock:
                logger.info('update lock')
                time.sleep(1)
                continue
            times += 1
            if times > 10:
                self.cache_size_limit()
                times = 0
            for _id, dl in list(self.tasks.items()):
                dl: Downloader
                if dl.file_lock.has_lock:
                    dl.save_state()
                    if dl.progress == 1:
                        logger.info(f'{_id} completed, delete lock')
                        dl.mark_done()
                        del self.tasks[_id]
            time.sleep(3)


def prefetch_resume_tv():
    settings_all = configs.ini_str_split('dev', 'prefetch_conf', split_by=';', re_split_by=',')
    api_dict = configs.get_server_api_by_ini()
    for setting_single in settings_all:
        name, *startswith = setting_single
        fetch_type = ''
        if startswith[0] == 'first_last':
            fetch_type = 'first_last'
        if name not in api_dict:
            logger.info(f'ini incorrect: {name} not set in [dev] > server_data_group, see FAQ')
            continue
        logger.info(f'prefetch conf: {name=} {startswith=}')
        emby_thin = api_dict[name]
        threading.Thread(target=_prefetch_resume_tv, args=(emby_thin, startswith, fetch_type), daemon=True).start()


def _prefetch_resume_tv(emby_thin: EmbyApiThin, startswith, fetch_type=''):
    startswith = tuple(startswith)

    if configs.raw.getboolean('tg_notify', 'get_chat_id', fallback=False):
        tg_notify('_get_chat_id')

    item_done_stat = {}  # {item_id:[source_id,]}
    strm_done_list = []
    sleep_again = False
    while True:
        try:
            items_all = emby_thin.get_resume_items()
        except Exception:
            time.sleep(600)
            continue
        # dump_json_file(items, 'z_resume_emby.json')
        items_all = items_all['Items']
        items_fresh = [i for i in items_all if i.get('SeriesName') and i.get('PremiereDate')
                       and time.mktime(time.strptime(i['PremiereDate'][:10], '%Y-%m-%d')) > time.time() - 86400 * 7]
        resume_ids = [i['Id'] for i in items_all]
        item_done_stat = {k: v for k, v in item_done_stat.items() if k in resume_ids}
        notify_item_list = []
        for ep_index, ep in enumerate(items_all):
            item_id = ep['Id']
            ep_file_path = ep['Path']
            ep_basename = os.path.basename(ep_file_path)
            source_info = ep['MediaSources'][0] if 'MediaSources' in ep else ep
            source_path = source_info['Path']
            is_strm = source_path.startswith('http') or ep_file_path.endswith('.strm')
            if not ep_file_path.startswith(startswith) and '/' not in startswith and not is_strm:
                continue
            if item_id in strm_done_list:
                continue
            if item_id in item_done_stat.keys():
                if sleep_again:
                    sleep_again = False
                    continue
                else:
                    sleep_again = True
            else:
                item_done_stat[item_id] = []
                if ep in items_fresh:
                    notify_item_list.append(item_id)
            # if ep['UserData'].get('LastPlayedDate'):
            #     continue
            try:
                playback_info = emby_thin.get_playback_info(item_id)
                play_session_id = playback_info['PlaySessionId']
                host = emby_thin.host
                image = f'[ ]({host}/emby/Items/{item_id}/Images/Primary?maxHeight=282&maxWidth=500)'
                item_url = f"[emby]({host}/web/index.html#!/item?id={item_id}&serverId={ep['ServerId']})"
                notify_msg = f"{image}{ep['SeriesName']} \| `{time.ctime()}` \| {item_url}"

                media_sources = playback_info['MediaSources']
                ep_source_name = [m['Name'] for m in media_sources if m['Name'] in ep_basename][0]
                if fetch_type == 'fetch_type':
                    playback_info['MediaSources'] = [version_prefer_emby(media_sources)]

                for source_info in playback_info['MediaSources']:
                    source_path = source_info['Path']
                    file_path = ep_file_path
                    is_http_source = source_path.startswith('http')
                    if is_strm and is_http_source and source_info['Name'] not in ep_file_path:
                        file_path = ep_file_path.replace(ep_source_name, source_info['Name'])
                    fake_name = os.path.splitdrive(file_path)[1].replace('/', '__').replace('\\', '__')
                    container = os.path.splitext(file_path)[-1]
                    source_id = source_info['Id']
                    if source_id in item_done_stat[item_id]:
                        continue
                    else:
                        item_done_stat[item_id].append(source_id)
                    # stream_url = f'{host}/videos/{ep["Id"]}/stream{container}' \
                    #              f'?MediaSourceId={source_info["Id"]}&Static=true&api_key={api_key}'
                    stream_url = f'{host}/emby/videos/{item_id}/stream{container}' \
                                 f'?DeviceId=embyToLocalPlayer&MediaSourceId={source_id}&Static=true' \
                                 f'&PlaySessionId={play_session_id}&api_key={emby_thin.api_key}'
                    strm_direct = configs.check_str_match(host, 'dev', 'strm_direct_host', log=False)
                    is_http_direct_strm = is_strm and strm_direct and is_http_source
                    if is_http_direct_strm:
                        stream_url = source_path
                    if stream_redirect := configs.ini_str_split('dev', 'stream_redirect'):
                        stream_redirect = zip(stream_redirect[0::2], stream_redirect[1::2])
                        for (_raw, _jump) in stream_redirect:
                            if _raw in stream_url:
                                stream_url = stream_url.replace(_raw, _jump)
                                break
                    root_dir = os.path.dirname(os.path.dirname((os.path.dirname(file_path))))
                    relative_path = file_path.replace(root_dir, '')[1:]
                    notify_msg += f'\n`{relative_path}`'
                    if configs.raw.getboolean('tg_notify', 'disable_prefetch', fallback=False):
                        logger.info(f'tg_notify, {relative_path}')
                        continue
                    if configs.check_str_match(host, 'dev', 'stream_prefix', log=False):
                        stream_prefix = configs.ini_str_split('dev', 'stream_prefix')[0].strip('/')
                        stream_url = f'{stream_prefix}{stream_url}'
                    if fetch_type == 'first_last' and ep_index < 2:
                        if not configs.gui_is_enable:
                            continue
                        ep['stream_url'], ep['fake_name'], ep['position'] = stream_url, fake_name, 0.1
                        ep['gui_cmd'] = 'download_not_play'
                        requests_urllib('http://127.0.0.1:58000/gui', _json=ep)
                        continue
                    if is_strm:
                        strm_done_list.append(item_id)
                        logger.info(f'get playback info only, cuz is strm [{ep["Name"]}]{relative_path}')
                        continue
                    try:
                        logger.info(f'prefetch {relative_path} \n{stream_url[:100]}')
                        prefetch_http_range(stream_url, 0, 0.05, size=ep.get('size'))
                        prefetch_http_range(stream_url, 0.98, 1, size=ep.get('size'))
                    except Exception as exc:
                        logger.error(f'prefetch error on download connection, skip ({type(exc).__name__})')
                    print()
                if item_id in notify_item_list:
                    tg_notify(notify_msg)
            except Exception as e:
                logger.error(f'_prefetch_resume_tv error found {str(e)[:100]}')
                break
        time.sleep(600)
