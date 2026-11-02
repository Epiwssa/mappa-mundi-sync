"""Renders one raster layer (orthomosaic, hillshade, WMS...) to XYZ tiles so the
phone can show it with no network.

Rendering happens in a background QgsTask using metatiles (4x4 tiles per
render) for speed and seamless labels/edges.
"""

import json
import math
import os
import shutil
import time

from qgis.core import (
    QgsApplication,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsMapRendererCustomPainterJob,
    QgsMapSettings,
    QgsProject,
    QgsRectangle,
    QgsTask,
)
from qgis.PyQt.QtCore import QRect, QSize, Qt
from qgis.PyQt.QtGui import QColor, QImage, QPainter

WEB_MERCATOR = QgsCoordinateReferenceSystem("EPSG:3857")
WGS84 = QgsCoordinateReferenceSystem("EPSG:4326")
TILE = 256
META = 4
ORIGIN = math.pi * 6378137


def _to_wgs84(layer):
    xform = QgsCoordinateTransform(layer.crs(), WGS84, QgsProject.instance())
    return xform.transformBoundingBox(layer.extent())


def suggested_max_zoom(layer):
    """Zoom level whose ground resolution roughly matches the raster's pixel size."""
    try:
        xform = QgsCoordinateTransform(layer.crs(), WEB_MERCATOR, QgsProject.instance())
        ext = xform.transformBoundingBox(layer.extent())
        width_px = layer.width()
        if width_px <= 0:
            return 18
        res = ext.width() / width_px
        z = math.log2(156543.034 / max(res, 0.01))
        return max(1, min(22, int(round(z))))
    except Exception:
        return 18


def _tile_xy(lon, lat, z):
    lat = max(-85.0511, min(85.0511, lat))
    n = 2 ** z
    x = int((lon + 180.0) / 360.0 * n)
    y = int((1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n)
    return min(max(x, 0), n - 1), min(max(y, 0), n - 1)


def tile_ranges(bbox_wgs84, zmin, zmax):
    """{z: (x0, y0, x1, y1)} inclusive tile ranges covering the bbox."""
    out = {}
    for z in range(zmin, zmax + 1):
        x0, y0 = _tile_xy(bbox_wgs84.xMinimum(), bbox_wgs84.yMaximum(), z)
        x1, y1 = _tile_xy(bbox_wgs84.xMaximum(), bbox_wgs84.yMinimum(), z)
        out[z] = (x0, y0, x1, y1)
    return out


def estimate_tiles(layer, zmin, zmax):
    return sum((x1 - x0 + 1) * (y1 - y0 + 1) for x0, y0, x1, y1 in tile_ranges(_to_wgs84(layer), zmin, zmax).values())


def tile_bounds(z, x, y):
    size = 2 * ORIGIN / 2 ** z
    return QgsRectangle(x * size - ORIGIN, ORIGIN - (y + 1) * size, (x + 1) * size - ORIGIN, ORIGIN - y * size)


class TileRenderTask(QgsTask):
    def __init__(self, layer, out_dir, zmin, zmax, fmt):
        super().__init__(f"Mappa Mundi: offline tiles for {layer.name()}", QgsTask.CanCancel)
        self.out_dir = out_dir
        self.fmt = fmt
        self.ranges = tile_ranges(_to_wgs84(layer), zmin, zmax)
        self.total = sum((r[2] - r[0] + 1) * (r[3] - r[1] + 1) for r in self.ranges.values())
        self.written = 0
        self.error = None
        # Map settings are prepared on the main thread; only rendering runs in the task.
        s = QgsMapSettings()
        s.setLayers([layer])
        s.setDestinationCrs(WEB_MERCATOR)
        s.setTransformContext(QgsProject.instance().transformContext())
        s.setOutputDpi(96)
        s.setBackgroundColor(QColor(255, 255, 255) if fmt == "jpg" else QColor(0, 0, 0, 0))
        s.setFlag(QgsMapSettings.Antialiasing, True)
        self.settings = s

    def run(self):
        try:
            for z, (x0, y0, x1, y1) in self.ranges.items():
                for mx in range(x0 - x0 % META, x1 + 1, META):
                    for my in range(y0 - y0 % META, y1 + 1, META):
                        if self.isCanceled():
                            return False
                        self._render_meta(z, mx, my, (x0, y0, x1, y1))
            return True
        except Exception as e:
            self.error = str(e)
            return False

    def _render_meta(self, z, mx, my, rng):
        x0, y0, x1, y1 = rng
        tl, br = tile_bounds(z, mx, my), tile_bounds(z, mx + META - 1, my + META - 1)
        settings = QgsMapSettings(self.settings)
        settings.setExtent(QgsRectangle(tl.xMinimum(), br.yMinimum(), br.xMaximum(), tl.yMaximum()))
        settings.setOutputSize(QSize(TILE * META, TILE * META))

        image = QImage(TILE * META, TILE * META, QImage.Format_ARGB32_Premultiplied)
        image.fill(self.settings.backgroundColor())
        painter = QPainter(image)
        job = QgsMapRendererCustomPainterJob(settings, painter)
        job.renderSynchronously()
        painter.end()

        for i in range(META):
            for j in range(META):
                x, y = mx + i, my + j
                if not (x0 <= x <= x1 and y0 <= y <= y1):
                    continue
                tile = image.copy(QRect(i * TILE, j * TILE, TILE, TILE))
                folder = os.path.join(self.out_dir, str(z), str(x))
                os.makedirs(folder, exist_ok=True)
                if self.fmt == "jpg":
                    tile.convertToFormat(QImage.Format_RGB32).save(os.path.join(folder, f"{y}.jpg"), "JPG", 82)
                else:
                    tile.save(os.path.join(folder, f"{y}.png"), "PNG")
                self.written += 1
        self.setProgress(100.0 * self.written / max(1, self.total))


class RasterPackager:
    """Builds the tile folder for one layer and writes mapper.json when done."""

    def __init__(self, layer, out_dir, zmin, zmax, fmt, on_done, on_progress=None):
        self.layer = layer
        self.out_dir = out_dir
        self.zmin, self.zmax, self.fmt = zmin, zmax, fmt
        self.on_done = on_done
        self.on_progress = on_progress
        self.task = None

    def start(self):
        if os.path.isdir(self.out_dir):
            shutil.rmtree(self.out_dir)
        os.makedirs(self.out_dir)
        self.task = TileRenderTask(self.layer, self.out_dir, self.zmin, self.zmax, self.fmt)
        if self.on_progress:
            self.task.progressChanged.connect(self.on_progress)
        self.task.taskCompleted.connect(lambda: self._finished(True))
        self.task.taskTerminated.connect(lambda: self._finished(False))
        QgsApplication.taskManager().addTask(self.task)

    def cancel(self):
        if self.task:
            self.task.cancel()

    def _finished(self, ok):
        if ok:
            ext = _to_wgs84(self.layer)
            meta = {
                "layerId": self.layer.id(),
                "name": self.layer.name(),
                "minzoom": self.zmin,
                "maxzoom": self.zmax,
                "format": self.fmt,
                "bounds": [ext.xMinimum(), ext.yMinimum(), ext.xMaximum(), ext.yMaximum()],
                "tileCount": self.task.written,
                "stamp": str(int(time.time())),
            }
            with open(os.path.join(self.out_dir, "mapper.json"), "w", encoding="utf-8") as f:
                json.dump(meta, f, indent=2)
        self.on_done(ok, None if ok else (self.task.error or "Cancelled"))
