"""A minimal Python client for the encrypted sync API, as the phone app speaks it. Used by the tests."""

import base64
import json
import time
import urllib.error
import urllib.request

from mappa_mundi_sync.secure import Sealer
from mappa_mundi_sync.server import AUTH_HEADER, SEALED_TYPE


def make_token(sealer, method, path, device=("test-device", "Test phone"), at=None):
    claims = {"t": int((time.time() if at is None else at) * 1000), "m": method, "p": path, "d": device[0], "n": device[1]}
    return sealer.seal_text(json.dumps(claims).encode(), b"mm2-auth").decode("ascii")


def request(base, method, path, body=None, key=None, raw=False, device=("test-device", "Test phone"),
            token=None, at=None, headers=None):
    """Returns (status, parsed body). With key, the request is sealed and the reply opened."""
    headers = dict(headers or {})
    data = body if isinstance(body, bytes) else (json.dumps(body).encode() if body is not None else None)
    sealer = Sealer(key) if key else None
    if sealer:
        token = token or make_token(sealer, method, path, device, at)
        headers[AUTH_HEADER] = token
        if data is not None:
            data = sealer.seal_text(data, b"mm2-req|" + token.encode())
    r = urllib.request.Request(base + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(r, timeout=30) as resp:
            status, content, ctype = resp.status, resp.read(), resp.headers.get("Content-Type", "")
    except urllib.error.HTTPError as e:
        status, content, ctype = e.code, e.read(), e.headers.get("Content-Type", "")
    if ctype == SEALED_TYPE:
        content = sealer.open_text(content, b"mm2-res|" + token.encode())
    return status, (content if raw else json.loads(content))


def opened(key, token, text):
    return Sealer(key).open(base64.b64decode(text), b"mm2-res|" + token.encode())
