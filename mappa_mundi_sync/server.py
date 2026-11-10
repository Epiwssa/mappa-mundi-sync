"""HTTP sync server.

Requests are handled on worker threads, but every QGIS API call is marshalled
onto the main (GUI) thread through MainThreadInvoker, because QGIS layers and
the project are not thread-safe.

Everything is encrypted with the pairing key from the QR code (see secure.py):

- Each request carries X-Mapper-Auth: a sealed {time, method, path, device} token. It
  proves the phone has the key, and the time and a replay check stop recorded requests
  being sent again.
- Request and response bodies are sealed and sent as base64 text, bound to that token, so
  a response can't be swapped for another one.
- Only GET /api/ping without a token is open, and it says no more than "API 2, encrypted".
"""

import json
import os
import re
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote, unquote, urlparse

from qgis.PyQt.QtCore import QObject, pyqtSignal, pyqtSlot

from .secure import Sealer, key_to_code, new_key

API_VERSION = 2
DEFAULT_PORT = 8765
MAX_UPLOAD = 70 * 1024 * 1024  # 50 MB photo, plus base64 overhead
AUTH_HEADER = "X-Mapper-Auth"
SEALED_TYPE = "application/x-mapper-sealed"
CLOCK_WINDOW = 15 * 60  # seconds a request's time may differ from this computer's clock


class MainThreadInvoker(QObject):
    """Run callables on the thread this object lives in (the GUI thread)."""

    _call = pyqtSignal(object)

    def __init__(self):
        super().__init__()
        self._call.connect(self._run)

    @pyqtSlot(object)
    def _run(self, job):
        job()

    def call(self, fn, timeout=300):
        if threading.current_thread() is threading.main_thread():
            return fn()
        done = threading.Event()
        box = {}

        def job():
            try:
                box["result"] = fn()
            except Exception as e:  # re-raised on the worker thread
                box["error"] = e
            finally:
                done.set()

        self._call.emit(job)
        if not done.wait(timeout):
            raise TimeoutError("QGIS did not respond in time")
        if "error" in box:
            raise box["error"]
        return box["result"]


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


class AuthError(Exception):
    """The request isn't from a phone with this pairing key. Answered in plain text: there's no key to seal with."""

    def __init__(self, reason, message):
        super().__init__(message)
        self.reason = reason


def local_ip_addresses():
    ips = []
    try:
        # Not actually sent anywhere; picks the interface used for the default route.
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ips.append(s.getsockname()[0])
        s.close()
    except OSError:
        pass  # no default route (offline): fall back to the host name lookup below
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127.") and ip not in ips:
                ips.append(ip)
    except OSError:
        pass  # host name does not resolve: use the default-route address (or 127.0.0.1)
    return ips or ["127.0.0.1"]


QR_MAX_BYTES = 210  # what the built-in QR encoder fits (version 10, level M), with a little room


def pairing_url(ips, port, code, project_name):
    """Text for the pairing QR code. Lists every address; the phone uses whichever answers.

    The project name is only a label for the phone, so it is shortened (or left out) to keep
    the link small enough for a QR code.
    """
    base = f"mapper://connect?h={','.join(ips)}&p={port}&c={code}"
    name = (project_name or "QGIS")[:40]
    while name and len(base) + 3 + len(quote(name, safe="")) > QR_MAX_BYTES:
        name = name[:-1]
    return f"{base}&n={quote(name, safe='')}" if name else base


class _Server(ThreadingHTTPServer):
    # Phones open several connections at once (tile downloads, several phones); the default
    # backlog of 5 makes Windows reset the rest.
    request_queue_size = 64


class SyncServer:
    def __init__(self, backend, port=DEFAULT_PORT, key=None, log=None):
        self.backend = backend
        self.port = port
        self.key = key or new_key()
        self.log = log or (lambda msg: None)
        self.httpd = None
        self.thread = None
        self._seen = {}  # token nonce -> time, to refuse replayed requests
        self._seen_lock = threading.Lock()
        self._warned = set()

    @property
    def key(self):
        return self._key

    @key.setter
    def key(self, key):
        self._key = key
        self.sealer = Sealer(key)

    @property
    def code(self):
        return key_to_code(self._key)

    def first_use(self, nonce, now):
        """False if this token was seen before (a replayed request)."""
        with self._seen_lock:
            if len(self._seen) > 5000:
                self._seen = {n: t for n, t in self._seen.items() if now - t < 2 * CLOCK_WINDOW}
            if nonce in self._seen:
                return False
            self._seen[nonce] = now
            return True

    def warn_once(self, key, msg):
        if key not in self._warned:
            self._warned.add(key)
            self.log(msg)

    @property
    def running(self):
        return self.httpd is not None

    def start(self):
        server = self

        class Handler(_Handler):
            sync = server

        # Phones connect over the local network, so the server must listen on the LAN interfaces.
        # It only runs while the user has pressed Start, and every request except /api/ping needs the pairing key.
        self.httpd = _Server(("0.0.0.0", self.port), Handler)  # nosec B104
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever, name="mapper-sync", daemon=True)
        self.thread.start()

    def stop(self):
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.httpd = None
            self.thread = None


_ACTIONS = {
    ("GET", "/api/project"): "Downloaded the project",
    ("POST", "/api/edits"): "Sent edits",
    ("POST", "/api/import"): "Sent a phone project",
}

_TILE_RE = re.compile(r"^/api/tiles/([^/]+)/(\d+)/(\d+)/(\d+)\.(png|jpg)$")


class _Handler(BaseHTTPRequestHandler):
    sync = None  # set by SyncServer.start
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    # --- plumbing -------------------------------------------------------

    token = None  # this request's X-Mapper-Auth, once checked

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", f"Content-Type, {AUTH_HEADER}")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

    def _send(self, status, body, content_type="application/json"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        if self.token:
            body = self.sync.sealer.seal_text(body, b"mm2-res|" + self.token)
            content_type = SEALED_TYPE
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status, obj):
        self._send(status, json.dumps(obj, default=str))

    def _authenticate(self, method, path):
        """Checks X-Mapper-Auth and returns its claims: t (ms), m, p, d (device id), n (device name)."""
        ip = self.client_address[0]
        auth = self.headers.get(AUTH_HEADER, "")
        if not auth:
            if self.headers.get("X-Mapper-Pin") is not None:
                self.sync.warn_once(("old", ip), f"{ip}: an older Mappa Mundi app tried to sync. Update the app on that phone.")
                raise AuthError("old-app", "Update Mappa Mundi on this phone, then scan the QR code in QGIS again.")
            raise AuthError("missing", "Not paired. Scan the QR code in QGIS.")
        try:
            claims = json.loads(self.sync.sealer.open_text(auth.encode("ascii"), b"mm2-auth"))
            if claims.get("m") != method or claims.get("p") != path:
                raise ValueError("token is for another request")
        except (ValueError, UnicodeError):
            self.sync.warn_once(("key", ip), f"{ip}: a phone with the wrong pairing code tried to sync.")
            raise AuthError("key", "Wrong pairing code. Scan the QR code in QGIS again.") from None
        now = time.time()
        skew = now - float(claims.get("t", 0)) / 1000
        if abs(skew) > CLOCK_WINDOW:
            minutes = round(abs(skew) / 60)
            raise AuthError("clock", f"This phone's clock is {minutes} minutes off the computer's. "
                                     "Set both to automatic time, then try again.")
        if not self.sync.first_use(auth[:16], now):
            raise AuthError("replay", "This request was already received.")
        self.token = auth.encode("ascii")
        return claims

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_UPLOAD:
            raise ApiError(413, "Upload too large")
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return b""
        try:
            return self.sync.sealer.open_text(raw, b"mm2-req|" + self.token)
        except ValueError:
            raise ApiError(400, "Request body failed decryption") from None

    def _dispatch(self, method):
        path = urlparse(self.path).path
        self.token = None
        try:
            if method == "GET" and path == "/api/ping" and not self.headers.get(AUTH_HEADER):
                return self._json(200, {"app": "Mappa Mundi Sync", "api": API_VERSION, "encrypted": True})
            claims = self._authenticate(method, path)
            self.sync.backend.touch_device(
                str(claims.get("d", ""))[:64], str(claims.get("n", ""))[:80], self.client_address[0], _ACTIONS.get((method, path)),
            )
            self._route(method, path)
        except AuthError as e:
            self._json(401, {"error": str(e), "reason": e.reason, "api": API_VERSION})
        except ApiError as e:
            self._json(e.status, {"error": str(e)})
        except Exception as e:
            self.sync.log(f"Error handling {method} {path}: {e}")
            self._json(500, {"error": str(e)})

    def _route(self, method, path):
        backend = self.sync.backend
        if method == "GET" and path == "/api/ping":
            return self._json(200, backend.ping(API_VERSION))

        if method == "GET" and path == "/api/status":
            return self._json(200, backend.status())

        if method == "GET" and path == "/api/project":
            manifest = backend.manifest()
            self.sync.log(f"{self.client_address[0]} downloaded the project manifest")
            if manifest["skipped"]:
                self.sync.log(f"Not sent, too many features for a phone: {', '.join(manifest['skipped'])}. "
                              "Set a field area to send only the part you need.")
            return self._json(200, manifest)

        m = re.match(r"^/api/layers/([^/]+)/features$", path)
        if method == "GET" and m:
            return self._send(200, backend.layer_features(unquote(m.group(1))), "application/geo+json")

        m = re.match(r"^/api/tiles/([^/]+)/index$", path)
        if method == "GET" and m:
            return self._json(200, backend.tile_index(unquote(m.group(1))))

        m = _TILE_RE.match(path)
        if method == "GET" and m:
            source, z, x, y, ext = m.groups()
            data = backend.tile(unquote(source), int(z), int(x), int(y), ext)
            if data is None:
                raise ApiError(404, "No such tile")
            return self._send(200, data, "image/png" if ext == "png" else "image/jpeg")

        if method == "POST" and path == "/api/edits":
            try:
                payload = json.loads(self._body() or b"{}")
            except ValueError:
                raise ApiError(400, "Body is not valid JSON")
            results = backend.apply_edits(payload.get("edits", []))
            ok = sum(1 for r in results if r.get("ok"))
            self.sync.log(f"{self.client_address[0]} pushed {ok}/{len(results)} edits")
            return self._json(200, {"results": results})

        if method == "POST" and path == "/api/import":
            try:
                payload = json.loads(self._body() or b"{}")
            except ValueError:
                raise ApiError(400, "Body is not valid JSON")
            result = backend.import_phone_layers(payload)
            self.sync.log(f"{self.client_address[0]} sent {len(result['layers'])} phone layer(s) to {result['gpkg']}")
            return self._json(200, result)

        m = re.match(r"^/api/photos/([^/]+)$", path)
        if method == "POST" and m:
            rel = backend.save_photo(unquote(m.group(1)), self._body())
            self.sync.log(f"Received photo {rel}")
            return self._json(200, {"path": rel})

        raise ApiError(404, "Not found")

    # --- verbs ----------------------------------------------------------

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")
