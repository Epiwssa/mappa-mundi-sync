"""Receives layers made on a phone (phone projects, GPS tracks, imported files) and
writes them into a GeoPackage next to the QGIS project. Runs on the main thread."""

import contextlib
import os

from qgis.core import (
    QgsCoordinateTransformContext,
    QgsEditorWidgetSetup,
    QgsFeature,
    QgsField,
    QgsFields,
    QgsGeometry,
    QgsProject,
    QgsVectorFileWriter,
    QgsVectorLayer,
    QgsWkbTypes,
)
from qgis.PyQt.QtCore import QVariant

from .backend import geojson_to_wkt, safe_name, set_layer_mode

FOLDER = "mapper_phone"
_TYPES = {
    "integer": QVariant.LongLong,
    "double": QVariant.Double,
    "boolean": QVariant.Bool,
    "date": QVariant.Date,
    "datetime": QVariant.DateTime,
}
_GEOMS = {
    "Point": QgsWkbTypes.MultiPoint,
    "LineString": QgsWkbTypes.MultiLineString,
    "Polygon": QgsWkbTypes.MultiPolygon,
}


def _fields(specs):
    fields = QgsFields()
    names = set()
    for spec in specs:
        name = spec.get("name")
        if not name or name.lower() in names or name.lower() in ("fid", "mapper_id"):
            continue
        names.add(name.lower())
        fields.append(QgsField(name, _TYPES.get(spec.get("type"), QVariant.String)))
    fields.append(QgsField("mapper_id", QVariant.String))
    return fields


def _apply_widgets(layer, specs):
    for spec in specs:
        idx = layer.fields().indexOf(spec.get("name", ""))
        if idx < 0:
            continue
        widget = spec.get("widget")
        if widget == "photo":
            layer.setEditorWidgetSetup(idx, QgsEditorWidgetSetup("ExternalResource", {"RelativeStorage": 1, "DocumentViewer": 1}))
        elif widget == "select" and spec.get("options"):
            values = [{str(o.get("label")): o.get("value")} for o in spec["options"]]
            layer.setEditorWidgetSetup(idx, QgsEditorWidgetSetup("ValueMap", {"map": values}))
        elif widget == "checkbox":
            layer.setEditorWidgetSetup(idx, QgsEditorWidgetSetup("CheckBox", {}))
        elif widget in ("date", "datetime"):
            layer.setEditorWidgetSetup(idx, QgsEditorWidgetSetup("DateTime", {}))
    mid = layer.fields().indexOf("mapper_id")
    if mid >= 0:
        layer.setEditorWidgetSetup(mid, QgsEditorWidgetSetup("Hidden", {}))


def _table_exists(gpkg, table):
    return os.path.exists(gpkg) and QgsVectorLayer(f"{gpkg}|layername={table}", "probe", "ogr").isValid()


def import_phone_layers(payload):
    """payload: {project, mode, layers: [{id?, name, geometryType, fields, features}]}

    mode "replace" (default): a layer sent again overwrites its earlier copy.
    mode "new": always adds a new table (name_2, name_3...) so nothing in QGIS is overwritten;
    used when a QGIS project's phone layer is handed over to QGIS for good.
    """
    project = QgsProject.instance()
    home = project.homePath()
    if not home:
        raise ValueError("Save the QGIS project first; the phone's layers are stored next to it.")
    layers = payload.get("layers") or []
    if not layers:
        raise ValueError("No layers were sent.")
    folder = os.path.join(home, FOLDER)
    os.makedirs(folder, exist_ok=True)
    gpkg = os.path.join(folder, f"{safe_name(payload.get('project') or 'phone')}.gpkg")
    results = []

    for spec in layers:
        gtype = _GEOMS.get(spec.get("geometryType"))
        if gtype is None:
            continue
        table = safe_name(spec.get("name") or "layer").lower()
        if payload.get("mode") == "new":
            base, n = table, 2
            while _table_exists(gpkg, table):
                table, n = f"{base}_{n}", n + 1
        fields = _fields(spec.get("fields") or [])
        mem = QgsVectorLayer(f"{QgsWkbTypes.displayString(gtype)}?crs=EPSG:4326", table, "memory")
        mem.dataProvider().addAttributes(fields.toList())
        mem.updateFields()
        feats = []
        for f in spec.get("features") or []:
            if not f.get("geometry"):
                continue
            geom = QgsGeometry.fromWkt(geojson_to_wkt(f["geometry"]))
            if geom.isNull():
                continue
            geom.convertToMultiType()
            feat = QgsFeature(mem.fields())
            feat.setGeometry(geom)
            props = f.get("properties") or {}
            for i, field in enumerate(mem.fields()):
                value = props.get(field.name())
                if value is not None and value != "":
                    # If it can't be converted, keep the phone's value and let the provider decide.
                    with contextlib.suppress(Exception):
                        value = field.convertCompatible(value)
                    feat.setAttribute(i, value)
            feats.append(feat)
        mem.dataProvider().addFeatures(feats)

        # Replace any earlier copy of this layer from the phone.
        source = f"{gpkg}|layername={table}"
        for existing in list(project.mapLayers().values()):
            if os.path.normcase(existing.source()) == os.path.normcase(source):
                project.removeMapLayer(existing.id())
        opts = QgsVectorFileWriter.SaveVectorOptions()
        opts.driverName = "GPKG"
        opts.layerName = table
        opts.actionOnExistingFile = (
            QgsVectorFileWriter.CreateOrOverwriteLayer if os.path.exists(gpkg) else QgsVectorFileWriter.CreateOrOverwriteFile
        )
        err, msg, *_ = QgsVectorFileWriter.writeAsVectorFormatV3(mem, gpkg, QgsCoordinateTransformContext(), opts)
        if err != QgsVectorFileWriter.NoError:
            raise RuntimeError(f"Couldn't write {table}: {msg}")

        layer = QgsVectorLayer(source, spec.get("name") or table, "ogr")
        if not layer.isValid():
            raise RuntimeError(f"Couldn't open {table} after writing it")
        _apply_widgets(layer, spec.get("fields") or [])
        set_layer_mode(layer, "edit")
        project.addMapLayer(layer)
        results.append({"name": layer.name(), "layerId": layer.id(), "features": layer.featureCount(), "phoneId": spec.get("id")})

    return {"gpkg": os.path.relpath(gpkg, home).replace("\\", "/"), "layers": results}
