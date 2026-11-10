"""The field area: the part of the project that goes into the field.

It picks which layers are shared with phones and how far offline raster tiles reach. It is
stored in the project file, so it survives closing QGIS.
"""

from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsFeatureRequest,
    QgsGeometry,
    QgsProject,
    QgsRasterLayer,
    QgsRectangle,
    QgsVectorLayer,
    QgsWkbTypes,
)

SCOPE = "mappa_mundi_sync"
WGS84 = QgsCoordinateReferenceSystem("EPSG:4326")


def _transform(geometry, src, dst):
    g = QgsGeometry(geometry)
    if src.isValid() and dst.isValid() and src != dst:
        g.transform(QgsCoordinateTransform(src, dst, QgsProject.instance()))
    return g


class FieldArea:
    def __init__(self, geometry, crs, label):
        self.geometry = geometry
        self.crs = crs
        self.label = label

    @classmethod
    def from_rect(cls, rect, crs, label):
        return cls(QgsGeometry.fromRect(rect), crs, label)

    @classmethod
    def from_features(cls, layer, features):
        """Polygons keep their shape; points and lines use the box around them."""
        geoms = [f.geometry() for f in features if f.hasGeometry()]
        if not geoms:
            return None
        union = QgsGeometry.unaryUnion(geoms)
        if "Polygon" not in QgsWkbTypes.displayString(union.wkbType()):
            box = union.boundingBox()
            if box.width() <= 0 or box.height() <= 0:
                return None
            union = QgsGeometry.fromRect(box)
        n = len(geoms)
        return cls(union, layer.crs(), f"{n} selected feature{'s' if n != 1 else ''} of {layer.name()}")

    def geometry_in(self, crs):
        return _transform(self.geometry, self.crs, crs)

    def bbox_wgs84(self):
        return self.geometry_in(WGS84).boundingBox()


def save_area(project, area):
    if area is None:
        project.removeEntry(SCOPE, "/area")
        return
    project.writeEntry(SCOPE, "/area/wkt", area.geometry.asWkt())
    project.writeEntry(SCOPE, "/area/crs", area.crs.toWkt())
    project.writeEntry(SCOPE, "/area/label", area.label)


def load_area(project):
    wkt, ok = project.readEntry(SCOPE, "/area/wkt", "")
    if not ok or not wkt:
        return None
    geometry = QgsGeometry.fromWkt(wkt)
    if geometry.isNull():
        return None
    crs = QgsCoordinateReferenceSystem.fromWkt(project.readEntry(SCOPE, "/area/crs", "")[0])
    return FieldArea(geometry, crs, project.readEntry(SCOPE, "/area/label", "Field area")[0])


def _is_online(layer):
    return isinstance(layer, QgsRasterLayer) and layer.providerType() == "wms"


def layer_in_area(layer, area):
    """True if the layer has data inside the area. Online basemaps and plain tables always count."""
    if isinstance(layer, QgsVectorLayer):
        if not layer.isSpatial():
            return True
        geom = area.geometry_in(layer.crs())
        request = QgsFeatureRequest().setFilterRect(geom.boundingBox()).setNoAttributes()
        engine = QgsGeometry.createGeometryEngine(geom.constGet())
        engine.prepareGeometry()
        return any(f.hasGeometry() and engine.intersects(f.geometry().constGet()) for f in layer.getFeatures(request))
    if _is_online(layer):
        return True
    if isinstance(layer, QgsRasterLayer):
        return area.geometry_in(layer.crs()).intersects(QgsGeometry.fromRect(layer.extent()))
    return False


def layer_bbox_wgs84(layer):
    if not layer.crs().isValid() or layer.crs() == WGS84:
        return QgsRectangle(layer.extent())
    return QgsCoordinateTransform(layer.crs(), WGS84, QgsProject.instance()).transformBoundingBox(layer.extent())


def clipped_bbox_wgs84(layer, clip):
    """The layer's extent in WGS84, cut to the clip box. None if they don't overlap."""
    bbox = layer_bbox_wgs84(layer)
    if clip is None:
        return bbox
    if _is_online(layer):
        return QgsRectangle(clip)  # world-wide layer: the clip is the whole job
    out = bbox.intersect(clip)
    return None if out.isEmpty() else out
