"""Runs the phone app's real sync code (mobile/src/sync.ts, via Node, with a simulated
phone file system) against a headless server: layers created on the phone in a QGIS
project must end up in QGIS and then sync both ways.

  "C:\\Program Files\\QGIS 3.44.13\\bin\\python-qgis-ltr.bat" qgis-plugin\\tests\\test_sync_handover.py
"""

import os
import shutil
import subprocess
import sys
import tempfile
import threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from qgis.core import QgsApplication, QgsProject  # noqa: E402
from qgis.PyQt.QtCore import QTimer  # noqa: E402

app = QgsApplication([], False)
app.initQgis()

from mappa_mundi_sync.backend import ProjectBackend  # noqa: E402
from mappa_mundi_sync.server import MainThreadInvoker, SyncServer  # noqa: E402

PORT = 18769
MOBILE = os.path.join(os.path.dirname(__file__), "..", "..", "mobile")
NODE = shutil.which("node") or os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "nodejs", "node.exe")

folder = tempfile.mkdtemp(prefix="mapper_handover_")
QgsProject.instance().write(os.path.join(folder, "site.qgz"))
backend = ProjectBackend(MainThreadInvoker())
backend.watch_project()
server = SyncServer(backend, port=PORT, pin="864200")
server.start()
outcome = {}


def run_client():
    try:
        proc = subprocess.run(
            [NODE, os.path.join(MOBILE, "scripts", "sync-integration.js"), str(PORT), server.pin, "site"],
            cwd=MOBILE, capture_output=True, text=True, encoding="utf-8", timeout=120,
        )
        outcome["code"] = proc.returncode
        outcome["out"] = proc.stdout + proc.stderr
    except Exception as e:
        outcome["code"] = "error"
        outcome["out"] = repr(e)
    finally:
        QTimer.singleShot(0, app.quit)


threading.Thread(target=run_client, daemon=True).start()
app.exec_()
server.stop()
print(outcome.get("out", "").strip())
assert outcome.get("code") == 0, "client failed"

names = sorted(l.name() for l in QgsProject.instance().mapLayers().values())
assert names == ["Tracks", "Trees", "Trees"], names
gpkg = os.path.join(folder, "mapper_phone", "site.gpkg")
assert os.path.isfile(gpkg)
sources = sorted(l.source().split("layername=")[-1] for l in QgsProject.instance().mapLayers().values())
assert sources == ["tracks", "trees", "trees_2"], sources
print("RESULT: PASS")
app.exitQgis()
