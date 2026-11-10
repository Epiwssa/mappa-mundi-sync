"""Field area: which layers fall inside it, saving it in the project, and clipping raster tiles to it.

  "C:\\Program Files\\QGIS 3.44.13\\bin\\python-qgis-ltr.bat" qgis-plugin\\tests\\test_area.py
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from osgeo import gdal, osr  # noqa: E402
from qgis.core import (  # noqa: E402
    QgsApplication,
    QgsCoordinateReferenceSystem,
    QgsFeature,
    QgsGeometry,
    QgsPointXY,
    QgsProject,
    QgsRasterLayer,
    QgsRectangle,
    QgsVectorLayer,
)

app = QgsApplication([], False)
app.initQgis()

from mappa_mundi_sync.area import FieldArea, clipped_bbox_wgs84, layer_in_area, load_area, save_area  # noqa: E402
from mappa_mundi_sync.packager import RasterPackager, estimate_tiles  # noqa: E402

UTM35S = QgsCoordinateReferenceSystem("EPSG:32735")
WGS84 = QgsCoordinateReferenceSystem("EPSG:4326")
project = QgsProject.instance()


def points(name, xy, crs="EPSG:32735"):
    layer = QgsVectorLayer(f"Point?crs={crs}", name, "memory")
    feats = []
    for x, y in xy:
        f = QgsFeature()
        f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(x, y)))
        feats.append(f)
    layer.dataProvider().addFeatures(feats)
    layer.updateExtents()
    return layer


# A 1 km field area in UTM 35S.
area = FieldArea.from_rect(QgsRectangle(600000, 7100000, 601000, 7101000), UTM35S, "test area")

inside = points("Inside", [(600500, 7100500)])
outside = points("Outside", [(650000, 7150000)])
# Big extent, but no feature in the area: must count as outside.
around = points("Around", [(590000, 7090000), (610000, 7110000)])
# Same place in another CRS: must count as inside.
lon_lat = points("LonLat", [(28.0, -26.0)], "EPSG:4326")
lon_lat_area = FieldArea.from_rect(QgsRectangle(27.99, -26.01, 28.01, -25.99), WGS84, "wgs area")
table = QgsVectorLayer("None?field=code:string", "Lookup", "memory")
osm = QgsRasterLayer("type=xyz&url=https://tile.openstreetmap.org/{z}/{x}/{y}.png", "OSM", "wms")

assert layer_in_area(inside, area)
assert not layer_in_area(outside, area)
assert not layer_in_area(around, area), "only features count, not the layer's extent"
assert layer_in_area(lon_lat, lon_lat_area)
assert layer_in_area(points("LonLat2", [(28.0, -26.0)], "EPSG:4326"), FieldArea(lon_lat_area.geometry_in(UTM35S), UTM35S, "x"))
assert layer_in_area(table, area), "tables without geometry are always shared"
assert osm.isValid() and layer_in_area(osm, area), "online basemaps are always shared"

# Selected points make a box; a single point has no area.
spread = points("Spread", [(600000, 7100000), (600400, 7100300)])
from_sel = FieldArea.from_features(spread, list(spread.getFeatures()))
assert from_sel and from_sel.geometry.boundingBox().width() == 400 and "2 selected features" in from_sel.label
assert FieldArea.from_features(inside, list(inside.getFeatures())) is None

# Saved in the project and read back.
save_area(project, area)
back = load_area(project)
assert back and back.label == "test area" and back.crs == UTM35S
assert back.geometry.boundingBox() == area.geometry.boundingBox()
save_area(project, None)
assert load_area(project) is None

# Raster tiles: a 4 km ortho, clipped to the 1 km area in its corner.
folder = tempfile.mkdtemp(prefix="mapper_area_")
tif = os.path.join(folder, "ortho.tif")
ds = gdal.GetDriverByName("GTiff").Create(tif, 400, 400, 3, gdal.GDT_Byte)
ds.SetGeoTransform([600000, 10, 0, 7104000, 0, -10])  # 10 m pixels, 4 km square
srs = osr.SpatialReference()
srs.ImportFromEPSG(32735)
ds.SetProjection(srs.ExportToWkt())
for b in range(1, 4):
    ds.GetRasterBand(b).Fill(50 * b)
ds = None
ortho = QgsRasterLayer(tif, "Ortho")
assert ortho.isValid() and layer_in_area(ortho, area)
assert not layer_in_area(ortho, FieldArea.from_rect(QgsRectangle(700000, 7100000, 701000, 7101000), UTM35S, "far"))

full = estimate_tiles(ortho, 14, 16)
clipped = estimate_tiles(ortho, 14, 16, area.bbox_wgs84())
print("tiles: whole layer", full, "field area", clipped)
assert 0 < clipped < full / 4, (clipped, full)
far = FieldArea.from_rect(QgsRectangle(700000, 7100000, 701000, 7101000), UTM35S, "far").bbox_wgs84()
assert estimate_tiles(ortho, 14, 16, far) is None
assert clipped_bbox_wgs84(osm, area.bbox_wgs84()) == area.bbox_wgs84(), "online layers are cut to the area"

try:
    RasterPackager(ortho, folder, 14, 16, "jpg", lambda ok, err: None, clip=far)
    raise AssertionError("should refuse a raster outside the area")
except ValueError:
    pass

result = {}
out_dir = os.path.join(folder, "tiles")
packager = RasterPackager(ortho, out_dir, 14, 16, "jpg",
                          lambda ok, err: (result.update(ok=ok, err=err), app.quit()), clip=area.bbox_wgs84())
packager.start()
app.exec()
assert result.get("ok"), result
written = sum(len(files) for _, _, files in os.walk(out_dir)) - 1  # minus mapper.json
import json  # noqa: E402

with open(os.path.join(out_dir, "mapper.json"), encoding="utf-8") as f:
    meta = json.load(f)
bbox = area.bbox_wgs84()
assert written == clipped == meta["tileCount"], (written, clipped, meta["tileCount"])
assert abs(meta["bounds"][0] - bbox.xMinimum()) < 1e-9 and abs(meta["bounds"][3] - bbox.yMaximum()) < 1e-9, meta["bounds"]
# Phones only get the features in the field area, and oversized layers are left out, not sent.
import mappa_mundi_sync.backend as backend_module  # noqa: E402
from mappa_mundi_sync.area import count_in_area, features_in_area  # noqa: E402
from mappa_mundi_sync.backend import ProjectBackend  # noqa: E402
from mappa_mundi_sync.server import MainThreadInvoker  # noqa: E402

big = points("Big", [(600100 + i * 10, 7100100) for i in range(30)] + [(650000 + i, 7150000) for i in range(70)])
assert len(list(features_in_area(big, area))) == 30 and len(list(features_in_area(big, None))) == 100
assert count_in_area(big, None, 1000) == 100 and count_in_area(big, area, 10) == 11, "stops counting past the limit"
project.addMapLayers([big, inside, outside])

state = {"area": None}
backend = ProjectBackend(MainThreadInvoker(), field_area=lambda: state["area"])
backend_module.MAX_PHONE_FEATURES = 50
manifest = backend.manifest()
names = [layer["name"] for layer in manifest["layers"]]
assert "Big" not in names and manifest["skipped"] == ["Big"] and "Inside" in names, manifest["skipped"]
try:
    backend.layer_features(big.id())
    raise AssertionError("an oversized layer must not be built")
except ValueError as e:
    assert "field area" in str(e)

revs = backend.revisions()["layers"].get(big.id(), 0)
state["area"] = area
backend.area_changed()
assert backend.revisions()["layers"][big.id()] == revs + 1, "phones refetch after the area changes"
manifest = backend.manifest()
counts = {layer["name"]: layer["featureCount"] for layer in manifest["layers"]}
assert manifest["skipped"] == [] and counts["Big"] == 30 and counts["Outside"] == 0, counts
fc = json.loads(backend.layer_features(big.id()))
assert len(fc["features"]) == 30
assert len(json.loads(backend.layer_features(outside.id()))["features"]) == 0
# Categorized styles with an "all other values" class: its symbol must be read while still alive.
from qgis.core import QgsCategorizedSymbolRenderer, QgsRendererCategory, QgsSymbol  # noqa: E402
from qgis.PyQt.QtGui import QColor  # noqa: E402
from mappa_mundi_sync.backend import renderer_style  # noqa: E402

landuse = QgsVectorLayer("Polygon?crs=EPSG:4326&field=fclass:string", "Landuse", "memory")
cats = []
for value, color in (("farmland", "#33aa33"), ("forest", "#116611"), ("", "#abcdef")):
    sym = QgsSymbol.defaultSymbol(landuse.geometryType())
    sym.setColor(QColor(color))
    cats.append(QgsRendererCategory(value, sym, value or "all other values"))
landuse.setRenderer(QgsCategorizedSymbolRenderer("fclass", cats))
for _ in range(200):
    style = renderer_style(landuse, "Polygon")
    assert style["default"]["fill"] == "#abcdef" and len(style["categories"]) == 2, style
print("RESULT: PASS")
app.exitQgis()
