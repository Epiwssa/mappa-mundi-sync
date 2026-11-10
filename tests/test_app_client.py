"""Runs the phone app's API client (mobile/src/api.ts, via Node) against a real
headless sync server: address failover, revisions, conflicts, device names.

  "C:\\Program Files\\QGIS 3.44.13\\bin\\python-qgis-ltr.bat" qgis-plugin\\tests\\test_app_client.py
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from qgis.core import QgsApplication, QgsFeature, QgsGeometry, QgsPointXY, QgsProject, QgsVectorLayer  # noqa: E402
from qgis.PyQt.QtCore import QTimer  # noqa: E402

app = QgsApplication([], False)
app.initQgis()

from mappa_mundi_sync.backend import ProjectBackend  # noqa: E402
from mappa_mundi_sync.server import MainThreadInvoker, SyncServer  # noqa: E402

PORT = 18767
MOBILE = os.path.join(os.path.dirname(__file__), "..", "..", "mobile")
# QGIS's Python environment usually lacks Node on PATH.
NODE = shutil.which("node") or os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "nodejs", "node.exe")

layer = QgsVectorLayer("Point?crs=EPSG:4326&field=name:string", "Pts", "memory")
f = QgsFeature(layer.fields())
f.setAttributes(["A"])
f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(28, -26)))
layer.dataProvider().addFeature(f)
QgsProject.instance().addMapLayer(layer)
folder = tempfile.mkdtemp(prefix="mapper_client_")
QgsProject.instance().write(os.path.join(folder, "client.qgz"))

backend = ProjectBackend(MainThreadInvoker())
backend.watch_project()
server = SyncServer(backend, port=PORT)
server.start()
outcome = {}

# One offline tile (binary, with every byte value) for the phone to download through the encryption.
TILE = bytes(range(256)) * 40
tile_dir = os.path.join(backend.tile_dir("fake-raster"), "12", "2363")
os.makedirs(tile_dir)
with open(os.path.join(tile_dir, "2280.png"), "wb") as f:
    f.write(TILE)
with open(os.path.join(backend.tile_dir("fake-raster"), "mapper.json"), "w", encoding="utf-8") as f:
    json.dump({"layerId": "fake-raster", "format": "png", "stamp": "1"}, f)
tile_path = os.path.join(tile_dir, "2280.png")


def run_client():
    try:
        proc = subprocess.run(
            [NODE, os.path.join(MOBILE, "scripts", "client-integration.js"), str(PORT), server.code, layer.id(), tile_path],
            cwd=MOBILE, capture_output=True, text=True, encoding="utf-8", timeout=60,
        )
        outcome["code"] = proc.returncode
        outcome["out"] = proc.stdout + proc.stderr
    except subprocess.TimeoutExpired as e:
        outcome["code"] = "timeout"
        outcome["out"] = (e.stdout or b"").decode("utf-8", "replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
    except Exception as e:
        outcome["code"] = "error"
        outcome["out"] = repr(e)
    finally:
        QTimer.singleShot(0, app.quit)


threading.Thread(target=run_client, daemon=True).start()
app.exec_()
server.stop()
print(outcome.get("out", "").strip())
devices = backend.device_list()
assert outcome.get("code") == 0, "client failed"
assert devices and devices[0]["name"] == "Integration Phone ✓", devices
assert next(layer.getFeatures())["name"] == "From app"
with open(os.path.join(folder, "DCIM", "app_photo.jpg"), "rb") as f:
    assert f.read() == bytes(range(255, -1, -1)) * 50, "photo arrived changed"
print("RESULT: PASS")
app.exitQgis()
