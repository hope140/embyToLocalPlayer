"""Request a graceful shutdown of the local ETLP HTTP service."""

import json
import sys
import urllib.error
import urllib.request


SHUTDOWN_URL = 'http://127.0.0.1:58000/shutdown/'
PROTOCOL_HEADER = 'X-ETLP-Protocol'
PROTOCOL_VERSION = '1'
TIMEOUT_SECONDS = 3
MAX_RESPONSE_BYTES = 64 * 1024


def _error(message):
    print(f'ETLP shutdown failed: {message}', file=sys.stderr)
    return 1


def _response_status(response):
    status = getattr(response, 'status', None)
    if status is not None:
        return status
    getcode = getattr(response, 'getcode', None)
    return getcode() if getcode is not None else None


def _read_response(response):
    body = response.read(MAX_RESPONSE_BYTES + 1)
    if len(body) > MAX_RESPONSE_BYTES:
        raise ValueError('response body is too large')
    return body


def stop_instance(url=None, timeout=TIMEOUT_SECONDS):
    """Send the fixed local shutdown request and return a process exit code."""

    url = SHUTDOWN_URL if url is None else url
    request = urllib.request.Request(
        url,
        data=json.dumps({}).encode('utf-8'),
        method='POST',
    )
    request.add_header('Content-Type', 'application/json')
    request.add_header('Accept', 'application/json')
    request.add_header(PROTOCOL_HEADER, PROTOCOL_VERSION)

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = _response_status(response)
            body = _read_response(response)
    except urllib.error.HTTPError as exc:
        try:
            exc.close()
        except Exception:
            pass
        if exc.code == 403:
            return _error(
                'HTTP 403: shutdown was refused; verify that the request is sent '
                'from the local machine and that the ETLP protocol is supported.'
            )
        if exc.code == 409:
            return _error(
                'HTTP 409: port 58000 is busy or the shutdown request was rejected; '
                'check which service owns the port.'
            )
        return _error(
            f'HTTP {exc.code}: the local service did not accept the ETLP shutdown '
            'request; check the service using port 58000.'
        )
    except (urllib.error.URLError, OSError, TimeoutError):
        return _error(
            'ETLP is not running or port 58000 is unavailable; start ETLP and retry.'
        )
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
        return _error(
            'unexpected response from port 58000; check which service owns the '
            'port and verify that it is ETLP.'
        )

    if status != 200:
        return _error(
            f'HTTP {status}: the local service did not return the ETLP shutdown '
            'response; check which service owns port 58000.'
        )

    try:
        payload = json.loads(body.decode('utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _error(
            'unexpected response from port 58000; check which service owns the '
            'port and verify that it is ETLP.'
        )
    if (not isinstance(payload, dict) or set(payload) != {'shutdown'}
            or payload.get('shutdown') is not True):
        return _error(
            'unexpected response from port 58000; check which service owns the '
            'port and verify that it is ETLP.'
        )

    print('ETLP shutdown requested.')
    return 0


def main():
    return stop_instance()


if __name__ == '__main__':
    raise SystemExit(main())
