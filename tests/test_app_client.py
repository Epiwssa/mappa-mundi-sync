"""Runs the phone app's API client (mobile/src/api.ts, via Node) against a real
headless sync server: address failover, revisions, conflicts, device names.

  "C:\\Program Files\\QGIS 3.44.13\\bin\\python-qgis-ltr.bat" qgis-plugin\\tests\\test_app_client.py
"""

import os
import shutil
import subprocess
import sys
import threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from qgis.core import QgsApplication, QgsFeature, QgsGeometry, QgsPointXY, QgsProject, QgsVectorLayer  # noqa: E402
from qgis.PyQt.QtCore import QTimer  # noqa: E402

app = QgsApplication([], False)
app.initQgis()

from mapper_sync.backend import ProjectBackend  # noqa: E402
from mapper_sync.server import MainThreadInvoker, SyncServer  # noqa: E402

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

backend = ProjectBackend(MainThreadInvoker())
backend.watch_project()
server = SyncServer(backend, port=PORT, pin="246810")
server.start()
outcome = {}


def run_client():
    try:
        proc = subprocess.run(
            [NODE, os.path.join(MOBILE, "scripts", "client-integration.js"), str(PORT), server.pin, layer.id()],
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
print("RESULT: PASS")
app.exitQgis()
