"""Builds offline tiles for a small synthetic orthophoto and checks the tile API.

  "C:\\Program Files\\QGIS 3.44.13\\bin\\python-qgis-ltr.bat" qgis-plugin\\tests\\test_packager.py
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from osgeo import gdal, osr  # noqa: E402
from qgis.analysis import QgsNativeAlgorithms  # noqa: E402
from qgis.core import QgsApplication, QgsProject, QgsRasterLayer  # noqa: E402

app = QgsApplication([], False)
app.initQgis()
QgsApplication.processingRegistry().addProvider(QgsNativeAlgorithms())

from mapper_sync.backend import ProjectBackend, layer_mode  # noqa: E402
from mapper_sync.packager import RasterPackager, estimate_tiles, suggested_max_zoom  # noqa: E402
from mapper_sync.server import MainThreadInvoker  # noqa: E402


def make_ortho(path):
    size = 400
    ds = gdal.GetDriverByName("GTiff").Create(path, size, size, 3, gdal.GDT_Byte)
    ds.SetGeoTransform([600000, 0.1, 0, 7100000, 0, -0.1])  # 10 cm pixels, 40 m square
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(32735)
    ds.SetProjection(srs.ExportToWkt())
    for b in range(1, 4):
        band = ds.GetRasterBand(b)
        band.Fill(60 * b)
    ds = None


folder = tempfile.mkdtemp(prefix="mapper_tiles_")
tif = os.path.join(folder, "ortho.tif")
make_ortho(tif)
layer = QgsRasterLayer(tif, "Ortho")
assert layer.isValid()
QgsProject.instance().addMapLayer(layer)
QgsProject.instance().write(os.path.join(folder, "p.qgz"))

assert layer_mode(layer) == "offline", layer_mode(layer)
zmax = suggested_max_zoom(layer)
print("suggested max zoom", zmax, "estimate z17-19:", estimate_tiles(layer, 17, 19))
assert 20 <= zmax <= 22, zmax

backend = ProjectBackend(MainThreadInvoker())
result = {}


def done(ok, error):
    result["ok"] = ok
    result["error"] = error
    app.quit()


from qgis.PyQt.QtCore import QTimer  # noqa: E402

packager = RasterPackager(layer, backend.tile_dir(layer.id()), 17, 19, "jpg", done,
                          on_progress=lambda p: print(f"progress {p:.0f}%", flush=True))
packager.start()
print("task status after start:", packager.task.status(), flush=True)
QTimer.singleShot(90000, lambda: (print("WATCHDOG: timed out", flush=True), app.quit()))
app.exec_()
assert result.get("ok"), f"packaging failed: {result.get('error')}"

man = backend.manifest()
raster = man["rasters"][0]
pkg = raster["package"]
print("package:", pkg)
assert pkg and pkg["minzoom"] == 17 and pkg["maxzoom"] == 19 and pkg["format"] == "jpg"
idx = backend.tile_index(layer.id())
assert idx["stamp"] == pkg["stamp"] and idx["tiles"], idx
assert all(t.endswith(".jpg") for t in idx["tiles"]), idx["tiles"][:5]
assert {int(t.split("/")[0]) for t in idx["tiles"]} == {17, 18, 19}
z, x, name = idx["tiles"][-1].split("/")
data = backend.tile(layer.id(), int(z), int(x), int(name.split(".")[0]), "jpg")
assert data and data[:2] == b"\xff\xd8", "not a JPEG"
assert backend.tile(layer.id(), 3, 0, 0, "jpg") is None
print(f"RESULT: PASS ({len(idx['tiles'])} tiles, sample {idx['tiles'][-1]} {len(data)} bytes)")
app.exitQgis()
