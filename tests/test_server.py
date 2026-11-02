"""Headless end-to-end test of the sync server.

Run with QGIS's Python:
  "C:\\Program Files\\QGIS 3.44.13\\bin\\python-qgis-ltr.bat" qgis-plugin\\tests\\test_server.py
"""

import json
import os
import sys
import tempfile
import threading
import traceback
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from qgis.core import (  # noqa: E402
    QgsApplication,
    QgsCategorizedSymbolRenderer,
    QgsEditorWidgetSetup,
    QgsFeature,
    QgsField,
    QgsGeometry,
    QgsMarkerSymbol,
    QgsPointXY,
    QgsProject,
    QgsRasterLayer,
    QgsRendererCategory,
    QgsVectorFileWriter,
    QgsVectorLayer,
)
from qgis.PyQt.QtCore import QTimer, QVariant  # noqa: E402

app = QgsApplication([], False)
app.initQgis()

from mappa_mundi_sync.backend import ProjectBackend  # noqa: E402
from mappa_mundi_sync.server import MainThreadInvoker, SyncServer, pairing_url  # noqa: E402

PORT = 18765
BASE = f"http://127.0.0.1:{PORT}"


def make_project(folder):
    mem = QgsVectorLayer("Point?crs=EPSG:32735", "Boreholes", "memory")
    mem.dataProvider().addAttributes([
        QgsField("name", QVariant.String),
        QgsField("status", QVariant.String),
        QgsField("depth", QVariant.Double),
        QgsField("photo", QVariant.String),
        QgsField("logged", QVariant.Date),
    ])
    mem.updateFields()
    for i, (name, status) in enumerate([("BH1", "drilled"), ("BH2", "planned")]):
        f = QgsFeature(mem.fields())
        f.setAttributes([name, status, 10.5 + i, None, None])
        f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(600000 + i * 100, 7100000)))
        mem.dataProvider().addFeature(f)
    gpkg = os.path.join(folder, "survey.gpkg")
    opts = QgsVectorFileWriter.SaveVectorOptions()
    opts.driverName = "GPKG"
    opts.layerName = "boreholes"
    QgsVectorFileWriter.writeAsVectorFormatV3(mem, gpkg, QgsProject.instance().transformContext(), opts)

    layer = QgsVectorLayer(f"{gpkg}|layername=boreholes", "Boreholes", "ogr")
    assert layer.isValid()
    idx = layer.fields().indexOf("status")
    layer.setEditorWidgetSetup(idx, QgsEditorWidgetSetup("ValueMap", {"map": [{"Planned": "planned"}, {"Drilled": "drilled"}]}))
    layer.setEditorWidgetSetup(layer.fields().indexOf("photo"), QgsEditorWidgetSetup("ExternalResource", {}))
    cats = [
        QgsRendererCategory("planned", QgsMarkerSymbol.createSimple({"color": "#ff0000"}), "Planned"),
        QgsRendererCategory("drilled", QgsMarkerSymbol.createSimple({"color": "#00aa00"}), "Drilled"),
    ]
    layer.setRenderer(QgsCategorizedSymbolRenderer("status", cats))
    osm = QgsRasterLayer("type=xyz&url=https://tile.openstreetmap.org/{z}/{x}/{y}.png&zmax=19&zmin=0", "OSM", "wms")
    assert osm.isValid(), "xyz layer invalid"
    QgsProject.instance().addMapLayers([layer, osm])
    QgsProject.instance().write(os.path.join(folder, "survey.qgz"))
    return layer


def req(method, path, body=None, pin=None, raw=False, headers=None):
    headers = {"Content-Type": "application/json", **(headers or {})}
    if pin:
        headers["X-Mapper-Pin"] = pin
    data = body if isinstance(body, bytes) else (json.dumps(body).encode() if body is not None else None)
    r = urllib.request.Request(BASE + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(r, timeout=30) as resp:
            content = resp.read()
            return resp.status, (content if raw else json.loads(content))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def scenario(pin, layer_id, results, ctx):
    try:
        status, ping = req("GET", "/api/ping")
        assert status == 200 and ping["project"] == "survey", ping

        status, _ = req("GET", "/api/project", pin="000000" if pin != "000000" else "111111")
        assert status == 401, status

        status, man = req("GET", "/api/project", pin=pin)
        assert status == 200
        lyr = next(l for l in man["layers"] if l["id"] == layer_id)
        assert lyr["geometryType"] == "Point" and lyr["editable"], lyr
        assert lyr["style"]["kind"] == "categorized" and len(lyr["style"]["categories"]) == 2, lyr["style"]
        widgets = {f["name"]: f["widget"] for f in lyr["fields"]}
        assert widgets["status"] == "select" and widgets["photo"] == "photo" and widgets["depth"] == "number", widgets
        assert widgets["logged"] == "date", widgets
        fid_field = next(f for f in lyr["fields"] if f["name"] == "fid")
        assert fid_field["readOnly"], fid_field
        assert man["basemaps"] and "{z}" in man["basemaps"][0]["tiles"], man["basemaps"]
        bbox = man["project"]["bbox"]
        assert 27 < bbox[0] < 29 and -27 < bbox[1] < -25, bbox  # UTM 35S, 600 km E -> lon ~28, lat ~-26
        print("manifest ok:", json.dumps(lyr["style"])[:120], "...")

        status, fc = req("GET", f"/api/layers/{layer_id}/features", pin=pin)
        assert status == 200 and len(fc["features"]) == 2
        f0 = fc["features"][0]
        assert -27 < f0["geometry"]["coordinates"][1] < -25, f0
        assert f0["properties"]["status"] == "drilled", f0  # raw value, not the ValueMap label
        print("features ok:", f0["id"], f0["properties"])

        status, photo = req("POST", "/api/photos/bh3%20photo.jpg", body=b"\xff\xd8fakejpeg", pin=pin)
        assert status == 200 and photo["path"] == "DCIM/bh3_photo.jpg", photo

        lon, lat = f0["geometry"]["coordinates"]
        edits = [
            {"id": "a1", "layerId": layer_id, "op": "add",
             "geometry": {"type": "Point", "coordinates": [lon + 0.001, lat]},
             "properties": {"name": "BH3", "status": "planned", "depth": "12.25", "photo": photo["path"],
                            "logged": "2026-10-30", "fid": 999}},
            {"id": "u1", "layerId": layer_id, "op": "update", "fid": f0["id"],
             "properties": {"status": "drilled"},
             "geometry": {"type": "Point", "coordinates": [lon, lat + 0.001]}},
            {"id": "d1", "layerId": layer_id, "op": "delete", "fid": fc["features"][1]["id"]},
            {"id": "bad", "layerId": "nope", "op": "add", "properties": {}},
        ]
        status, res = req("POST", "/api/edits", body={"edits": edits}, pin=pin)
        assert status == 200
        r = {x["id"]: x for x in res["results"]}
        assert r["a1"]["ok"] and r["u1"]["ok"] and r["d1"]["ok"], res
        assert not r["bad"]["ok"] and "not shared" in r["bad"]["error"], r["bad"]
        print("edits ok:", res["results"])

        status, fc = req("GET", f"/api/layers/{layer_id}/features", pin=pin)
        names = sorted(f["properties"]["name"] for f in fc["features"])
        assert names == ["BH1", "BH3"], names
        bh3 = next(f for f in fc["features"] if f["properties"]["name"] == "BH3")
        assert bh3["properties"]["depth"] == 12.25 and bh3["properties"]["photo"] == "DCIM/bh3_photo.jpg", bh3
        assert bh3["id"] != 999
        bh1 = next(f for f in fc["features"] if f["properties"]["name"] == "BH1")
        assert bh1["properties"]["status"] == "drilled"
        assert abs(bh1["geometry"]["coordinates"][1] - (lat + 0.001)) < 1e-6, bh1
        print("round trip ok:", bh3)

        two_way(pin, layer_id, ctx)
        results.append("PASS")
    except Exception:
        traceback.print_exc()
        results.append("FAIL")
    finally:
        QTimer.singleShot(0, app.quit)


def two_way(pin, layer_id, ctx):
    """Revisions, conflict detection and device tracking."""
    backend, invoker = ctx["backend"], ctx["invoker"]
    phone = {"X-Mapper-Device-Id": "dev1", "X-Mapper-Device-Name": "Field%20Phone%20A"}

    status, st = req("GET", "/api/status", pin=pin, headers=phone)
    assert status == 200 and st["serverId"] == backend.server_id, st
    rev0 = st["layers"].get(layer_id, 0)
    assert rev0 >= 1, st  # the earlier phone edits bumped it

    status, fc = req("GET", f"/api/layers/{layer_id}/features", pin=pin)
    bh1 = next(f for f in fc["features"] if f["properties"]["name"] == "BH1")
    assert bh1["mapper_hash"] and len(bh1["mapper_hash"]) == 16, bh1
    base = bh1["mapper_hash"]

    # Someone edits BH1 in QGIS and saves (edit buffer + commit, like the QGIS UI).
    def qgis_edit():
        layer = QgsProject.instance().mapLayer(layer_id)
        layer.startEditing()
        layer.changeAttributeValue(bh1["id"], layer.fields().indexOf("depth"), 99.0)
        assert layer.commitChanges(), layer.commitErrors()
    invoker.call(qgis_edit)
    status, st = req("GET", "/api/status", pin=pin)
    assert st["layers"][layer_id] > rev0, ("commit in QGIS did not bump the revision", st)

    # A phone that downloaded before that change must not silently overwrite it.
    stale = {"id": "c1", "layerId": layer_id, "op": "update", "fid": bh1["id"], "baseHash": base,
             "properties": {"name": "BH1-phone"}}
    status, res = req("POST", "/api/edits", body={"edits": [stale]}, pin=pin, headers=phone)
    r = res["results"][0]
    assert not r["ok"] and r.get("conflict") and "changed in QGIS" in r["error"], r
    stale_delete = {"id": "c2", "layerId": layer_id, "op": "delete", "fid": bh1["id"], "baseHash": base}
    status, res = req("POST", "/api/edits", body={"edits": [stale_delete]}, pin=pin)
    assert res["results"][0].get("conflict"), res

    # "Keep mine" sends the same edit with force.
    status, res = req("POST", "/api/edits", body={"edits": [{**stale, "force": True}]}, pin=pin, headers=phone)
    assert res["results"][0]["ok"], res
    status, fc = req("GET", f"/api/layers/{layer_id}/features", pin=pin)
    bh1 = next(f for f in fc["features"] if f["id"] == bh1["id"])
    assert bh1["properties"]["name"] == "BH1-phone" and bh1["properties"]["depth"] == 99.0, bh1

    # A matching base hash goes straight through; deleting something already gone is fine.
    ok_update = {"id": "c3", "layerId": layer_id, "op": "update", "fid": bh1["id"],
                 "baseHash": bh1["mapper_hash"], "properties": {"depth": 5}}
    gone = {"id": "c4", "layerId": layer_id, "op": "delete", "fid": 424242, "baseHash": "x"}
    upd_gone = {"id": "c5", "layerId": layer_id, "op": "update", "fid": 424242, "properties": {"depth": 1}}
    status, res = req("POST", "/api/edits", body={"edits": [ok_update, gone, upd_gone]}, pin=pin)
    r = {x["id"]: x for x in res["results"]}
    assert r["c3"]["ok"] and r["c4"]["ok"], r
    assert not r["c5"]["ok"] and r["c5"].get("conflict") and "deleted in QGIS" in r["c5"]["error"], r

    devices = backend.device_list()
    assert devices and devices[0]["name"] == "Field Phone A" and devices[0]["lastAction"] == "Sent edits", devices

    # A new layer in QGIS bumps the structure revision so phones refetch the manifest.
    s0 = backend.revisions()["structure"]
    invoker.call(lambda: QgsProject.instance().addMapLayer(QgsVectorLayer("Point?crs=EPSG:4326", "New", "memory")))
    assert backend.revisions()["structure"] > s0
    # Survey attributes from the phone create gnss_* fields on first use.
    gnss = {"gnss_accuracy": 0.012, "gnss_samples": 30, "gnss_external": True, "gnss_time": "2026-11-02T08:15:00", "gnss_alt": None}
    add = {"id": "g1", "layerId": layer_id, "op": "add", "geometry": {"type": "Point", "coordinates": [28.0, -26.2]},
           "properties": {"name": "GCP1", **gnss}}
    status, res = req("POST", "/api/edits", body={"edits": [add]}, pin=pin)
    assert res["results"][0]["ok"], res

    def check_fields():
        layer = QgsProject.instance().mapLayer(layer_id)
        f = next(x for x in layer.getFeatures() if x["name"] == "GCP1")
        types = {n: layer.fields().field(n).typeName().lower() for n in gnss}
        return f["gnss_accuracy"], f["gnss_samples"], f["gnss_external"], f["gnss_time"], types
    acc, samples, ext, when, types = invoker.call(check_fields)
    assert abs(acc - 0.012) < 1e-9 and samples == 30 and ext in (True, 1), (acc, samples, ext)
    assert "2026-11-02" in str(when.toString("yyyy-MM-dd") if hasattr(when, "toString") else when), when
    assert "date" in types["gnss_time"] and "int" in types["gnss_samples"], types
    status, man = req("GET", "/api/project", pin=pin)
    names = [f["name"] for f in next(l for l in man["layers"] if l["id"] == layer_id)["fields"]]
    assert "gnss_accuracy" in names, names
    print("two-way ok: revisions, conflicts, force, devices, gnss fields")


def main():
    folder = tempfile.mkdtemp(prefix="mapper_test_")
    layer = make_project(folder)
    invoker = MainThreadInvoker()
    backend = ProjectBackend(invoker)
    backend.watch_project()
    server = SyncServer(backend, port=PORT, log=print)
    server.start()
    results = []
    ctx = {"backend": backend, "invoker": invoker}
    threading.Thread(target=scenario, args=(server.pin, layer.id(), results, ctx), daemon=True).start()
    app.exec_()
    server.stop()
    assert os.path.isfile(os.path.join(folder, "DCIM", "bh3_photo.jpg"))
    assert pairing_url(["192.168.1.2", "10.0.0.3"], 8765, "012345", "Pit 7 / survey") == \
        "mapper://connect?h=192.168.1.2,10.0.0.3&p=8765&k=012345&n=Pit%207%20%2F%20survey"
    print("RESULT:", results[0] if results else "NO RESULT")
    app.exitQgis()
    sys.exit(0 if results == ["PASS"] else 1)


main()
