"""Renders one raster layer (orthomosaic, hillshade, WMS...) to XYZ tiles so the
phone can show it with no network.

Rendering happens in a background QgsTask using metatiles (4x4 tiles per
render) for speed and seamless labels/edges.

With a field area, the raster is drawn only inside the area's shape: tiles wholly
inside it are saved as usual, tiles cut by its edge as transparent PNG (the phone
reads both), and tiles outside it are skipped.
"""

import json
import math
import os
import shutil
import time

from qgis.core import (
    Qgis,
    QgsApplication,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsGeometry,
    QgsMapClippingRegion,
    QgsMapRendererCustomPainterJob,
    QgsMapSettings,
    QgsProject,
    QgsRasterMinMaxOrigin,
    QgsRectangle,
    QgsTask,
)
from qgis.PyQt.QtCore import QRect, QSize, Qt
from qgis.PyQt.QtGui import QColor, QImage, QPainter

from .area import clipped_bbox_wgs84

WEB_MERCATOR = QgsCoordinateReferenceSystem("EPSG:3857")
TILE = 256
META = 4
ORIGIN = math.pi * 6378137


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


def frozen_copy(layer):
    """A copy of the layer whose colour stretch stays as QGIS shows it now.

    Styles set to recalculate min/max for the visible extent would otherwise stretch
    each block of tiles to its own values, leaving visible seams between blocks.
    """
    copy = layer.clone()
    renderer = copy.renderer()
    if renderer is not None and hasattr(renderer, "minMaxOrigin"):
        limit = getattr(Qgis, "RasterRangeLimit", None)
        not_set = limit.NotSet if limit is not None else QgsRasterMinMaxOrigin.Limits.None_
        origin = QgsRasterMinMaxOrigin(renderer.minMaxOrigin())
        origin.setLimits(not_set)
        renderer.setMinMaxOrigin(origin)
    return copy


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


def estimate_tiles(layer, zmin, zmax, area=None):
    """Tile count for the layer, cut to the area's box if given. None if they don't overlap."""
    bbox = clipped_bbox_wgs84(layer, area.bbox_wgs84() if area else None)
    if bbox is None:
        return None
    return sum((x1 - x0 + 1) * (y1 - y0 + 1) for x0, y0, x1, y1 in tile_ranges(bbox, zmin, zmax).values())


def tile_bounds(z, x, y):
    size = 2 * ORIGIN / 2 ** z
    return QgsRectangle(x * size - ORIGIN, ORIGIN - (y + 1) * size, (x + 1) * size - ORIGIN, ORIGIN - y * size)


class TileRenderTask(QgsTask):
    def __init__(self, layer, out_dir, zmin, zmax, fmt, bbox, clip=None):
        super().__init__(f"Mappa Mundi: offline tiles for {layer.name()}", QgsTask.Flag.CanCancel)
        self.out_dir = out_dir
        self.fmt = fmt
        self.ranges = tile_ranges(bbox, zmin, zmax)
        self.total = sum((r[2] - r[0] + 1) * (r[3] - r[1] + 1) for r in self.ranges.values())
        self.written = 0
        self.done = 0  # tiles dealt with, written or skipped
        self.error = None
        # Map settings are prepared on the main thread; only rendering runs in the task.
        self.layer = frozen_copy(layer)  # kept alive for the task's lifetime
        s = QgsMapSettings()
        s.setLayers([self.layer])
        s.setDestinationCrs(WEB_MERCATOR)
        s.setTransformContext(QgsProject.instance().transformContext())
        s.setOutputDpi(96)
        s.setBackgroundColor(QColor(255, 255, 255) if fmt == "jpg" and clip is None else QColor(0, 0, 0, 0))
        s.setFlag(QgsMapSettings.Flag.Antialiasing, True)
        self.clip = clip  # area outline in Web Mercator, or None
        if clip is not None:
            s.addClippingRegion(QgsMapClippingRegion(clip))
            self.clip_engine = QgsGeometry.createGeometryEngine(clip.constGet())
            self.clip_engine.prepareGeometry()
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

    def _coverage(self, z, x, y):
        """'full', 'part' or 'none': how much of the tile the field area covers."""
        if self.clip is None:
            return "full"
        tile = QgsGeometry.fromRect(tile_bounds(z, x, y))
        if self.clip_engine.contains(tile.constGet()):
            return "full"
        return "part" if self.clip_engine.intersects(tile.constGet()) else "none"

    def _render_meta(self, z, mx, my, rng):
        x0, y0, x1, y1 = rng
        tiles = [(mx + i, my + j) for i in range(META) for j in range(META)
                 if x0 <= mx + i <= x1 and y0 <= my + j <= y1]
        coverage = {xy: self._coverage(z, *xy) for xy in tiles}
        if all(c == "none" for c in coverage.values()):
            self.done += len(tiles)
            self.setProgress(100.0 * self.done / max(1, self.total))
            return
        tl, br = tile_bounds(z, mx, my), tile_bounds(z, mx + META - 1, my + META - 1)
        settings = QgsMapSettings(self.settings)
        settings.setExtent(QgsRectangle(tl.xMinimum(), br.yMinimum(), br.xMaximum(), tl.yMaximum()))
        settings.setOutputSize(QSize(TILE * META, TILE * META))

        image = QImage(TILE * META, TILE * META, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(self.settings.backgroundColor())
        painter = QPainter(image)
        job = QgsMapRendererCustomPainterJob(settings, painter)
        job.renderSynchronously()
        painter.end()

        for (x, y), cover in coverage.items():
            self.done += 1
            if cover == "none":
                continue
            tile = image.copy(QRect((x - mx) * TILE, (y - my) * TILE, TILE, TILE))
            folder = os.path.join(self.out_dir, str(z), str(x))
            os.makedirs(folder, exist_ok=True)
            if self.fmt == "jpg" and cover == "full":
                flat = QImage(TILE, TILE, QImage.Format.Format_RGB32)
                flat.fill(QColor(255, 255, 255))
                p = QPainter(flat)
                p.drawImage(0, 0, tile)
                p.end()
                flat.save(os.path.join(folder, f"{y}.jpg"), "JPG", 82)
            else:
                tile.save(os.path.join(folder, f"{y}.png"), "PNG")
            self.written += 1
        self.setProgress(100.0 * self.done / max(1, self.total))


class RasterPackager:
    """Builds the tile folder for one layer and writes mapper.json when done.

    With a field area (clip), the raster is drawn only inside the area.
    """

    def __init__(self, layer, out_dir, zmin, zmax, fmt, on_done, on_progress=None, clip=None):
        self.layer = layer
        self.bbox = clipped_bbox_wgs84(layer, clip.bbox_wgs84() if clip else None)
        self.clip = clip.geometry_in(WEB_MERCATOR) if clip else None
        if self.bbox is None:
            raise ValueError(f"{layer.name()} doesn't overlap the chosen area")
        self.out_dir = out_dir
        self.zmin, self.zmax, self.fmt = zmin, zmax, fmt
        self.on_done = on_done
        self.on_progress = on_progress
        self.task = None

    def start(self):
        if os.path.isdir(self.out_dir):
            shutil.rmtree(self.out_dir)
        os.makedirs(self.out_dir)
        self.task = TileRenderTask(self.layer, self.out_dir, self.zmin, self.zmax, self.fmt, self.bbox, self.clip)
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
            ext = self.bbox
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
