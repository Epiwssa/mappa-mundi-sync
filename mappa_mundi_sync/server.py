"""HTTP sync server.

Requests are handled on worker threads, but every QGIS API call is marshalled
onto the main (GUI) thread through MainThreadInvoker, because QGIS layers and
the project are not thread-safe.
"""

import json
import os
import re
import secrets
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote, unquote, urlparse

from qgis.PyQt.QtCore import QObject, pyqtSignal, pyqtSlot

API_VERSION = 1
DEFAULT_PORT = 8765
MAX_UPLOAD = 50 * 1024 * 1024


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


def new_pin():
    return "".join(secrets.choice("0123456789") for _ in range(6))


def pairing_url(ips, port, pin, project_name):
    """Text for the pairing QR code. Lists every address; the phone uses whichever answers."""
    name = quote((project_name or "QGIS")[:40], safe="")
    return f"mapper://connect?h={','.join(ips)}&p={port}&k={pin}&n={name}"


class SyncServer:
    def __init__(self, backend, port=DEFAULT_PORT, pin=None, log=None):
        self.backend = backend
        self.port = port
        self.pin = pin or new_pin()
        self.log = log or (lambda msg: None)
        self.httpd = None
        self.thread = None

    @property
    def running(self):
        return self.httpd is not None

    def start(self):
        server = self

        class Handler(_Handler):
            sync = server

        # Phones connect over the local network, so the server must listen on the LAN interfaces.
        # It only runs while the user has pressed Start, and every request except /api/ping needs the PIN.
        self.httpd = ThreadingHTTPServer(("0.0.0.0", self.port), Handler)  # nosec B104
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

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Mapper-Pin, X-Mapper-Device-Id, X-Mapper-Device-Name")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

    def _send(self, status, body, content_type="application/json"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status, obj):
        self._send(status, json.dumps(obj, default=str))

    def _check_pin(self):
        given = self.headers.get("X-Mapper-Pin", "")
        if not secrets.compare_digest(given, self.sync.pin):
            raise ApiError(401, "Wrong or missing PIN")

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_UPLOAD:
            raise ApiError(413, "Upload too large")
        return self.rfile.read(length) if length else b""

    def _dispatch(self, method):
        path = urlparse(self.path).path
        try:
            if method == "GET" and path == "/api/ping":
                return self._json(200, self.sync.backend.ping(API_VERSION))
            self._check_pin()
            self.sync.backend.touch_device(
                self.headers.get("X-Mapper-Device-Id", ""),
                unquote(self.headers.get("X-Mapper-Device-Name", "")),
                self.client_address[0],
                _ACTIONS.get((method, path)),
            )
            self._route(method, path)
        except ApiError as e:
            self._json(e.status, {"error": str(e)})
        except Exception as e:
            self.sync.log(f"Error handling {method} {path}: {e}")
            self._json(500, {"error": str(e)})

    def _route(self, method, path):
        backend = self.sync.backend
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
