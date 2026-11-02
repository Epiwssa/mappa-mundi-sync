"""Sends a phone project's layers (as the app does) to a headless server and checks
the GeoPackage that QGIS writes.

  "C:\\Program Files\\QGIS 3.44.13\\bin\\python-qgis-ltr.bat" qgis-plugin\\tests\\test_phone_import.py
"""

import json
import os
import sys
import tempfile
import threading
import traceback
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from qgis.core import QgsApplication, QgsProject  # noqa: E402
from qgis.PyQt.QtCore import QTimer  # noqa: E402

app = QgsApplication([], False)
app.initQgis()

from mappa_mundi_sync.backend import ProjectBackend, layer_mode  # noqa: E402
from mappa_mundi_sync.server import MainThreadInvoker, SyncServer  # noqa: E402

PORT = 18768
folder = tempfile.mkdtemp(prefix="mapper_phone_")
QgsProject.instance().write(os.path.join(folder, "farm.qgz"))
backend = ProjectBackend(MainThreadInvoker())
backend.watch_project()
server = SyncServer(backend, port=PORT, pin="135790")
server.start()
outcome = {}

trees = {
    "name": "Trees",
    "geometryType": "Point",
    "fields": [
        {"name": "species", "type": "string", "widget": "select", "options": [{"label": "Marula", "value": "marula"}, {"label": "Baobab", "value": "baobab"}]},
        {"name": "height", "type": "double", "widget": "number"},
        {"name": "healthy", "type": "boolean", "widget": "checkbox"},
        {"name": "seen", "type": "date", "widget": "date"},
        {"name": "photo", "type": "string", "widget": "photo"},
    ],
    "features": [
        {"type": "Feature", "geometry": {"type": "Point", "coordinates": [28.1, -26.1]},
         "properties": {"species": "marula", "height": 7.5, "healthy": True, "seen": "2026-10-30", "photo": "DCIM/t1.jpg", "mapper_id": "p-1"}},
        {"type": "Feature", "geometry": {"type": "Point", "coordinates": [28.2, -26.2]},
         "properties": {"species": "baobab", "height": "12", "healthy": False, "mapper_id": "p-2"}},
        {"type": "Feature", "geometry": None, "properties": {"mapper_id": "p-3"}},
    ],
}
tracks = {
    "name": "Tracks",
    "geometryType": "LineString",
    "fields": [{"name": "name", "type": "string", "widget": "text"}, {"name": "length_m", "type": "double", "widget": "number"}],
    "features": [
        {"type": "Feature", "geometry": {"type": "LineString", "coordinates": [[28, -26], [28.001, -26]]}, "properties": {"name": "walk", "length_m": 99.9, "mapper_id": "p-t"}},
        {"type": "Feature", "geometry": {"type": "MultiLineString", "coordinates": [[[28, -26], [28.1, -26]], [[28.2, -26], [28.3, -26]]]}, "properties": {"name": "paused walk"}},
    ],
}


def post(payload):
    r = urllib.request.Request(f"http://127.0.0.1:{PORT}/api/import", data=json.dumps(payload).encode(), method="POST",
                               headers={"Content-Type": "application/json", "X-Mapper-Pin": server.pin})
    with urllib.request.urlopen(r, timeout=60) as resp:
        return json.loads(resp.read())


def scenario():
    try:
        s0 = backend.revisions()["structure"]
        res = post({"project": "Farm survey", "layers": [trees, tracks]})
        assert res["gpkg"] == "mapper_phone/Farm_survey.gpkg", res
        assert [(l["name"], l["features"]) for l in res["layers"]] == [("Trees", 2), ("Tracks", 2)], res
        assert backend.revisions()["structure"] > s0
        outcome["first"] = res
        # Sending again replaces the layers instead of duplicating them.
        trees2 = {**trees, "features": trees["features"][:1]}
        res2 = post({"project": "Farm survey", "layers": [trees2]})
        assert res2["layers"][0]["features"] == 1
        outcome["second"] = res2
    except Exception:
        outcome["error"] = traceback.format_exc()
    finally:
        QTimer.singleShot(0, app.quit)


threading.Thread(target=scenario, daemon=True).start()
app.exec_()
server.stop()
assert "error" not in outcome, outcome["error"]

project = QgsProject.instance()
by_name = {}
for lyr in project.mapLayers().values():
    by_name.setdefault(lyr.name(), []).append(lyr)
assert len(by_name["Trees"]) == 1, "re-sending must replace, not duplicate"
assert len(by_name["Tracks"]) == 1
t = by_name["Trees"][0]
assert os.path.isfile(os.path.join(folder, "mapper_phone", "Farm_survey.gpkg"))
assert t.featureCount() == 1 and layer_mode(t) == "edit"
f = next(t.getFeatures())
assert f["species"] == "marula" and abs(f["height"] - 7.5) < 1e-9 and f["photo"] == "DCIM/t1.jpg" and f["mapper_id"] == "p-1", f.attributes()
assert t.editorWidgetSetup(t.fields().indexOf("species")).type() == "ValueMap"
assert t.editorWidgetSetup(t.fields().indexOf("photo")).type() == "ExternalResource"
assert t.editorWidgetSetup(t.fields().indexOf("mapper_id")).type() == "Hidden"
geom = f.geometry().asMultiPoint()[0]
assert abs(geom.x() - 28.1) < 1e-9 and abs(geom.y() + 26.1) < 1e-9

tr = by_name["Tracks"][0]
lines = {g["name"]: g.geometry() for g in tr.getFeatures()}
assert lines["walk"].isMultipart() and len(lines["paused walk"].asMultiPolyline()) == 2
print("RESULT: PASS", json.dumps(outcome["first"]))
app.exitQgis()
