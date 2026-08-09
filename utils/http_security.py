"""Security helpers shared by the local HTTP server and its clients."""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import time
import urllib.parse


ETLP_PROTOCOL_HEADER = 'X-ETLP-Protocol'
ETLP_PROTOCOL_VERSION = '1'
HTTP_SERVER_PORT = 58000
MAX_MEDIA_URL_LIFETIME_SECONDS = 24 * 60 * 60


def is_loopback_address(address: str | None) -> bool:
    """Return whether an HTTP peer address is a loopback address."""

    if not address:
        return False
    try:
        return ipaddress.ip_address(address.split('%', 1)[0]).is_loopback
    except ValueError:
        return False


def is_local_http_server_url(url: str) -> bool:
    """Return whether *url* targets the local ETLP HTTP listener."""

    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port
    except ValueError:
        return False
    if parsed.scheme.lower() != 'http' or port != HTTP_SERVER_PORT:
        return False
    return parsed.hostname == '127.0.0.1'


def protocol_header_valid(headers) -> bool:
    """Validate the version marker used by local POST callers."""

    for name, value in headers.items():
        if name.lower() == ETLP_PROTOCOL_HEADER.lower():
            return value == ETLP_PROTOCOL_VERSION
    return False


def bearer_header(token: str) -> str:
    """Build the Authorization value for a configured server token."""

    return f'Bearer {token}'


def bearer_token_valid(header: str | None, token: str) -> bool:
    """Validate an exact ``Bearer <token>`` header without leaking secrets."""

    if not token or not header or not header.startswith('Bearer '):
        return False
    supplied = header[7:]
    if not supplied:
        return False
    return hmac.compare_digest(supplied, token)


def media_signature(token: str, file_path: str, expires: str) -> str:
    """Return the HMAC-SHA256 signature for a media URL."""

    payload = f'{file_path}\n{expires}'.encode('utf-8')
    return hmac.new(token.encode('utf-8'), payload, hashlib.sha256).hexdigest()


def media_url_signature_valid(token: str, file_path: str, expires: str,
                              signature: str, *, now: int | None = None) -> bool:
    """Validate token, expiry window and signature for a media URL."""

    if not token or not file_path or not expires or not signature:
        return False
    try:
        expires_at = int(expires)
    except (TypeError, ValueError):
        return False
    if str(expires_at) != str(expires):
        return False
    current = int(time.time()) if now is None else int(now)
    if expires_at < current or expires_at > current + MAX_MEDIA_URL_LIFETIME_SECONDS:
        return False
    expected = media_signature(token, file_path, expires)
    return hmac.compare_digest(signature, expected)
