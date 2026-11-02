"""Reads the open QGIS project for the phone and writes field edits back.

Every public method is safe to call from a server worker thread: QGIS work is
pushed onto the main thread through the invoker.
"""

import contextlib
import hashlib
import json
import os
import re
import secrets
import threading
import time

from qgis.core import (
    Qgis,
    QgsCategorizedSymbolRenderer,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsFeature,
    QgsField,
    QgsFieldConstraints,
    QgsGeometry,
    QgsGraduatedSymbolRenderer,
    QgsJsonExporter,
    QgsProject,
    QgsProviderRegistry,
    QgsRasterLayer,
    QgsRectangle,
    QgsRuleBasedRenderer,
    QgsSingleSymbolRenderer,
    QgsVectorDataProvider,
    QgsVectorLayer,
    QgsVectorLayerSimpleLabeling,
)

MODE_PROP = "mapper_sync/mode"
MODES = ("edit", "view", "basemap", "offline", "exclude")
MODE_LABELS = {
    "edit": "Editable",
    "view": "View only",
    "basemap": "Online basemap",
    "offline": "Offline tiles",
    "exclude": "Exclude",
}
TILES_DIRNAME = ".mapper_tiles"
WGS84 = QgsCoordinateReferenceSystem("EPSG:4326")
MM_TO_PX = 3.0

# QVariant / QMetaType ids, stable across Qt5 and Qt6
_BOOL, _INTS, _DOUBLE, _DATE, _DATETIME = 1, (2, 3, 4, 5, 32, 33, 35, 36), 6, 14, 16


def _int(e):
    """Enum -> int for both sip int enums and Python Enum (QGIS >= 3.30 scoped enums)."""
    return e.value if hasattr(e, "value") else int(e)


ORIGIN_PROVIDER = 1


def safe_name(text):
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", text).strip("._") or "file"


# --- layer classification -------------------------------------------------


def _is_xyz_or_wms(layer):
    return isinstance(layer, QgsRasterLayer) and layer.providerType() == "wms"


def can_edit(layer):
    if not isinstance(layer, QgsVectorLayer) or layer.readOnly():
        return False
    caps = layer.dataProvider().capabilities()
    flag = getattr(getattr(Qgis, "VectorProviderCapability", None), "AddFeatures", None)
    return bool(caps & (flag if flag is not None else QgsVectorDataProvider.AddFeatures))


def default_mode(layer):
    if isinstance(layer, QgsVectorLayer):
        return "edit" if can_edit(layer) else "view"
    if _is_xyz_or_wms(layer):
        return "basemap"
    if isinstance(layer, QgsRasterLayer):
        return "offline"
    return "exclude"


def allowed_modes(layer):
    if isinstance(layer, QgsVectorLayer):
        return (["edit"] if can_edit(layer) else []) + ["view", "exclude"]
    if _is_xyz_or_wms(layer):
        return ["basemap", "offline", "exclude"]
    if isinstance(layer, QgsRasterLayer):
        return ["offline", "exclude"]
    return ["exclude"]


def layer_mode(layer):
    mode = layer.customProperty(MODE_PROP)
    return mode if mode in allowed_modes(layer) else default_mode(layer)


def set_layer_mode(layer, mode):
    layer.setCustomProperty(MODE_PROP, mode)


# --- styles ---------------------------------------------------------------


def _color(c):
    return c.name() if c and c.isValid() else None


def symbol_style(symbol, geom_type):
    if symbol is None:
        return None
    color = symbol.color()
    style = {"fill": _color(color), "opacity": round(symbol.opacity() * color.alphaF(), 3)}
    sl = symbol.symbolLayer(0) if symbol.symbolLayerCount() else None
    stroke = getattr(sl, "strokeColor", None)
    stroke_width = getattr(sl, "strokeWidth", None)
    if geom_type == "Point":
        style["radius"] = max(3.0, symbol.size() * MM_TO_PX / 2)
        style["stroke"] = _color(stroke()) if stroke else "#ffffff"
        style["strokeWidth"] = max(1.0, stroke_width() * MM_TO_PX) if stroke_width else 1.0
    elif geom_type == "LineString":
        style["stroke"] = style.pop("fill")
        style["strokeWidth"] = max(1.0, symbol.width() * MM_TO_PX)
    else:
        if sl is not None and hasattr(sl, "brushStyle") and _int(sl.brushStyle()) == 0:  # Qt.NoBrush
            style["fillOpacity"] = 0
        style["stroke"] = _color(stroke()) if stroke else style["fill"]
        style["strokeWidth"] = max(0.5, stroke_width() * MM_TO_PX) if stroke_width else 1.0
    return style


def renderer_style(layer, geom_type):
    r = layer.renderer()
    if isinstance(r, QgsCategorizedSymbolRenderer) and r.classAttribute() in layer.fields().names():
        cats = [
            {"value": c.value(), "label": c.label(), "symbol": symbol_style(c.symbol(), geom_type)}
            for c in r.categories()
            if c.renderState() and c.value() not in (None, "")
        ]
        default = next((c.symbol() for c in r.categories() if c.value() in (None, "")), None)
        return {
            "kind": "categorized",
            "field": r.classAttribute(),
            "categories": cats,
            "default": symbol_style(default, geom_type),
        }
    if isinstance(r, QgsGraduatedSymbolRenderer) and r.classAttribute() in layer.fields().names():
        return {
            "kind": "graduated",
            "field": r.classAttribute(),
            "ranges": [
                {"lower": g.lowerValue(), "upper": g.upperValue(), "label": g.label(),
                 "symbol": symbol_style(g.symbol(), geom_type)}
                for g in r.ranges()
                if g.renderState()
            ],
        }
    if isinstance(r, QgsSingleSymbolRenderer):
        return {"kind": "single", "symbol": symbol_style(r.symbol(), geom_type)}
    if isinstance(r, QgsRuleBasedRenderer):
        rules = r.rootRule().descendants()
        sym = next((rule.symbol() for rule in rules if rule.symbol()), None)
        return {"kind": "single", "symbol": symbol_style(sym, geom_type)}
    return {"kind": "single", "symbol": None}


def label_field(layer):
    if not layer.labelsEnabled():
        return None
    labeling = layer.labeling()
    if isinstance(labeling, QgsVectorLayerSimpleLabeling):
        settings = labeling.settings()
        if not settings.isExpression and settings.fieldName in layer.fields().names():
            return settings.fieldName
    return None


# --- fields / forms ---------------------------------------------------------


def _field_type(field):
    t = _int(field.type())
    if t == _BOOL:
        return "boolean"
    if t in _INTS:
        return "integer"
    if t == _DOUBLE:
        return "double"
    if t == _DATE:
        return "date"
    if t == _DATETIME:
        return "datetime"
    return "string"


def _value_map_options(config):
    raw = config.get("map") or []
    pairs = []
    if isinstance(raw, dict):
        pairs = list(raw.items())
    else:
        for item in raw:
            pairs.extend(item.items())
    return [{"label": str(k), "value": v} for k, v in pairs if not str(v).startswith("{2839923C")]


def form_fields(layer):
    pk = set(layer.primaryKeyAttributes())
    out = []
    for idx, field in enumerate(layer.fields()):
        setup = layer.editorWidgetSetup(idx)
        wtype = setup.type()
        if wtype == "Hidden":
            continue
        ftype = _field_type(field)
        spec = {
            "name": field.name(),
            "alias": field.alias() or field.name(),
            "type": ftype,
            "widget": "text",
            "readOnly": idx in pk or _int(layer.fields().fieldOrigin(idx)) != ORIGIN_PROVIDER,
            "notNull": bool(field.constraints().constraints() & QgsFieldConstraints.ConstraintNotNull),
        }
        if wtype == "ValueMap":
            spec["widget"] = "select"
            spec["options"] = _value_map_options(setup.config())
        elif wtype == "CheckBox" or ftype == "boolean":
            spec["widget"] = "checkbox"
        elif wtype == "ExternalResource":
            spec["widget"] = "photo"
        elif wtype == "DateTime" or ftype in ("date", "datetime"):
            spec["widget"] = ftype if ftype in ("date", "datetime") else "date"
        elif ftype in ("integer", "double"):
            spec["widget"] = "number"
        out.append(spec)
    return out


# --- geometry helpers -------------------------------------------------------


def _coords(c):
    return f"{float(c[0])} {float(c[1])}"


def _ring(cs):
    return "(" + ", ".join(_coords(c) for c in cs) + ")"


def _poly(rings):
    return "(" + ", ".join(_ring(r) for r in rings) + ")"


def geojson_to_wkt(g):
    t, c = g["type"], g["coordinates"]
    if t == "Point":
        return f"POINT({_coords(c)})"
    if t == "LineString":
        return "LINESTRING" + _ring(c)
    if t == "Polygon":
        return "POLYGON" + _poly(c)
    if t == "MultiPoint":
        return "MULTIPOINT(" + ", ".join(f"({_coords(p)})" for p in c) + ")"
    if t == "MultiLineString":
        return "MULTILINESTRING" + _poly(c)
    if t == "MultiPolygon":
        return "MULTIPOLYGON(" + ", ".join(_poly(p) for p in c) + ")"
    raise ValueError(f"Unsupported geometry type {t}")


def geometry_kind(layer):
    # 0 = point, 1 = line, 2 = polygon in both the old and the scoped enum
    return {0: "Point", 1: "LineString", 2: "Polygon"}.get(_int(layer.geometryType()), "NoGeometry")


def feature_hash(feature):
    """Short fingerprint of a feature's attributes and geometry, used to spot
    features changed in QGIS after a phone downloaded them."""
    # A change detector, not security: MD5 keeps hashes identical to those phones already hold.
    h = hashlib.md5(usedforsecurity=False)
    for value in feature.attributes():
        h.update(repr(value).encode("utf-8", "replace"))
        h.update(b"\x1f")
    geom = feature.geometry()
    if geom is not None and not geom.isNull():
        h.update(bytes(geom.asWkb()))
    return h.hexdigest()[:16]


class Conflict(Exception):
    pass


# --- backend ----------------------------------------------------------------


class ProjectBackend:
    def __init__(self, invoker, canvas_extent=None):
        self.invoker = invoker
        self.canvas_extent = canvas_extent  # callable -> (QgsRectangle, crs) or None
        # New on every QGIS session, so phones know old revision numbers no longer apply.
        self.server_id = secrets.token_hex(6)
        self.structure_rev = 1  # layers added/removed, styles, sharing modes, tiles rebuilt
        self.layer_revs = {}  # layer id -> data revision
        self.devices = {}  # device id -> {name, ip, lastSeen, lastAction}
        self._lock = threading.Lock()
        self._watched = set()
        self._project_slots = []

    # --- change tracking (main thread) ------------------------------------

    def watch_project(self):
        """Hook project/layer signals so phones can tell when something changed."""
        project = self._project()
        self._project_slots = [
            (project.layersAdded, self._on_layers_added),
            (project.layersRemoved, self._on_structure_signal),
            (project.readProject, self._on_structure_signal),
        ]
        for sig, slot in self._project_slots:
            sig.connect(slot)
        self._on_layers_added(project.mapLayers().values())

    def unwatch_project(self):
        for sig, slot in self._project_slots:
            with contextlib.suppress(TypeError):  # already disconnected
                sig.disconnect(slot)
        self._project_slots = []

    def _on_structure_signal(self, *_):
        self.bump_structure()

    def _on_layers_added(self, layers):
        for layer in layers:
            if layer.id() in self._watched:
                continue
            self._watched.add(layer.id())
            lid = layer.id()
            if isinstance(layer, QgsVectorLayer):
                layer.afterCommitChanges.connect(lambda lid=lid: self.bump_layer(lid))
                layer.dataChanged.connect(lambda lid=lid: self.bump_layer(lid))
            for sig in ("rendererChanged", "styleChanged", "nameChanged"):
                if hasattr(layer, sig):
                    getattr(layer, sig).connect(self._on_structure_signal)
        self.bump_structure()

    def bump_structure(self):
        with self._lock:
            self.structure_rev += 1

    def bump_layer(self, layer_id):
        with self._lock:
            self.layer_revs[layer_id] = self.layer_revs.get(layer_id, 0) + 1

    def revisions(self):
        with self._lock:
            return {"serverId": self.server_id, "structure": self.structure_rev, "layers": dict(self.layer_revs)}

    # --- devices (any thread) ---------------------------------------------

    def touch_device(self, device_id, name, ip, action=None):
        if not device_id:
            return
        with self._lock:
            d = self.devices.setdefault(device_id, {"name": name or "Phone", "ip": ip, "lastAction": None})
            d.update(name=name or d["name"], ip=ip, lastSeen=time.time())
            if action:
                d["lastAction"] = action

    def device_list(self):
        with self._lock:
            return sorted(({"id": k, **v} for k, v in self.devices.items()), key=lambda d: -d["lastSeen"])

    # helpers that must run on the main thread

    def _project(self):
        return QgsProject.instance()

    def _layer(self, layer_id):
        layer = self._project().mapLayer(layer_id)
        if layer is None or layer_mode(layer) == "exclude":
            raise LookupError(f"Layer {layer_id} is not shared")
        return layer

    def tiles_root(self):
        home = self._project().homePath() or os.path.expanduser("~")
        return os.path.join(home, TILES_DIRNAME)

    def tile_dir(self, layer_id):
        return os.path.join(self.tiles_root(), safe_name(layer_id))

    # public API

    def ping(self, api_version):
        return self.invoker.call(lambda: {
            "app": "Mappa Mundi Sync",
            "api": api_version,
            "project": self._project().baseName() or "Untitled project",
            "serverId": self.server_id,
        })

    def status(self):
        return self.revisions()

    def manifest(self):
        return self.invoker.call(self._manifest)

    def _manifest(self):
        project = self._project()
        layers, basemaps, rasters = [], [], []
        extent = QgsRectangle()
        extent.setMinimal()
        nodes = project.layerTreeRoot().findLayers()
        for order, node in enumerate(nodes):
            layer = node.layer()
            if layer is None or not layer.isValid():
                continue
            mode = layer_mode(layer)
            if mode == "exclude":
                continue
            visible = node.isVisible()
            common = {"id": layer.id(), "name": layer.name(), "visible": visible, "order": order}
            if isinstance(layer, QgsVectorLayer):
                kind = geometry_kind(layer)
                layers.append({
                    **common,
                    "geometryType": kind,
                    "editable": mode == "edit",
                    "style": renderer_style(layer, kind),
                    "labelField": label_field(layer),
                    "fields": form_fields(layer),
                    "featureCount": layer.featureCount(),
                })
                if visible and kind != "NoGeometry":
                    extent.combineExtentWith(self._to_wgs84(layer.extent(), layer.crs()))
            elif mode == "basemap":
                tiles = self._tile_url(layer)
                if tiles:
                    url, scheme = tiles
                    basemaps.append({**common, "tiles": url, "scheme": scheme, "opacity": layer.opacity()})
            elif mode == "offline":
                meta = self._tile_meta(layer.id())
                rasters.append({**common, "opacity": layer.opacity(), "package": meta})
                if visible:
                    extent.combineExtentWith(self._to_wgs84(layer.extent(), layer.crs()))

        if self.canvas_extent:
            canvas = self.canvas_extent()
            if canvas:
                extent = self._to_wgs84(*canvas)
        bbox = None
        if not extent.isNull() and extent.isFinite():
            bbox = [extent.xMinimum(), extent.yMinimum(), extent.xMaximum(), extent.yMaximum()]
        return {
            "project": {
                "name": project.baseName() or "Untitled project",
                "title": project.title() or project.baseName(),
                "bbox": bbox,
            },
            "layers": layers,
            "basemaps": basemaps,
            "rasters": rasters,
            "generated": time.time(),
            "revisions": self.revisions(),
        }

    def _to_wgs84(self, rect, crs):
        if not crs.isValid() or crs == WGS84:
            return QgsRectangle(rect)
        xform = QgsCoordinateTransform(crs, WGS84, self._project())
        return xform.transformBoundingBox(rect)

    def _tile_url(self, layer):
        uri = QgsProviderRegistry.instance().decodeUri("wms", layer.source())
        url = uri.get("url", "")
        if not url:
            return None
        if uri.get("type") == "xyz":
            scheme = "tms" if "{-y}" in url else "xyz"
            return url.replace("{-y}", "{y}").replace("{q}", "{quadkey}"), scheme
        # Plain WMS: let the phone request 256px tiles in Web Mercator.
        sep = "&" if "?" in url else "?"
        layers = uri.get("layers", "")
        layers = ",".join(layers) if isinstance(layers, list) else layers
        styles = uri.get("styles", "")
        styles = ",".join(styles) if isinstance(styles, list) else styles
        fmt = uri.get("format", "image/png")
        return (
            f"{url}{sep}SERVICE=WMS&REQUEST=GetMap&VERSION=1.3.0&LAYERS={layers}&STYLES={styles}"
            f"&FORMAT={fmt}&TRANSPARENT=TRUE&CRS=EPSG:3857&WIDTH=256&HEIGHT=256&BBOX={{bbox-epsg-3857}}",
            "xyz",
        )

    def _tile_meta(self, layer_id):
        path = os.path.join(self.tile_dir(layer_id), "mapper.json")
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def layer_features(self, layer_id):
        def run():
            layer = self._layer(layer_id)
            if not isinstance(layer, QgsVectorLayer):
                raise LookupError("Not a vector layer")
            exporter = QgsJsonExporter(layer, 7)
            exporter.setTransformGeometries(True)
            exporter.setDestinationCrs(WGS84)
            exporter.setIncludeRelated(False)
            # Raw stored values, not ValueMap labels; the phone maps values to labels itself.
            exporter.setUseFieldFormatters(False)
            features = list(layer.getFeatures())
            hashes = {f.id(): feature_hash(f) for f in features}
            fc = json.loads(exporter.exportFeatures(features))
            for f in fc.get("features", []):
                f["mapper_hash"] = hashes.get(f.get("id"))
            return json.dumps(fc)
        return self.invoker.call(run)

    # offline tiles (files only, so no main-thread hop needed after locating the dir)

    def tile_index(self, layer_id):
        root = self.invoker.call(lambda: self.tile_dir(layer_id))
        meta = self.invoker.call(lambda: self._tile_meta(layer_id))
        if not meta:
            return {"stamp": None, "tiles": []}
        tiles = []
        for z in sorted(os.listdir(root)):
            if not z.isdigit():
                continue
            for x in os.listdir(os.path.join(root, z)):
                for name in os.listdir(os.path.join(root, z, x)):
                    if name.endswith((".png", ".jpg")):
                        tiles.append(f"{z}/{x}/{name}")
        return {"stamp": meta.get("stamp"), "format": meta.get("format"), "tiles": tiles}

    def tile(self, layer_id, z, x, y, ext):
        root = self.invoker.call(lambda: self.tile_dir(layer_id))
        path = os.path.join(root, str(z), str(x), f"{y}.{ext}")
        if not os.path.isfile(path):
            return None
        with open(path, "rb") as f:
            return f.read()

    # edits

    def import_phone_layers(self, payload):
        from .phone_import import import_phone_layers  # imports backend helpers, so loaded lazily
        result = self.invoker.call(lambda: import_phone_layers(payload))
        self.bump_structure()
        return result

    def save_photo(self, name, data):
        if not data:
            raise ValueError("Empty photo upload")
        home = self.invoker.call(lambda: self._project().homePath())
        if not home:
            raise ValueError("Save the QGIS project first so photos have a folder to go in")
        folder = os.path.join(home, "DCIM")
        os.makedirs(folder, exist_ok=True)
        fname = safe_name(name)
        with open(os.path.join(folder, fname), "wb") as f:
            f.write(data)
        return f"DCIM/{fname}"

    def apply_edits(self, edits):
        def run():
            results = [self._apply_one(e) for e in edits]
            for layer_id in {e.get("layerId") for e, r in zip(edits, results) if r["ok"]}:
                self.bump_layer(layer_id)
            return results
        return self.invoker.call(run)

    def _check_base(self, layer, edit):
        """Refuses to overwrite a feature changed in QGIS since the phone downloaded it.
        Returns False when a delete has nothing left to delete."""
        current = layer.getFeature(int(edit["fid"]))
        if not current.isValid():
            if edit["op"] == "delete":
                return False
            raise Conflict("This feature was deleted in QGIS after you downloaded it.")
        base = edit.get("baseHash")
        if base and not edit.get("force") and feature_hash(current) != base:
            raise Conflict("This feature was changed in QGIS after you downloaded it.")
        return True

    def _apply_one(self, edit):
        result = {"id": edit.get("id"), "ok": False}
        try:
            layer = self._layer(edit["layerId"])
            if layer_mode(layer) != "edit":
                raise PermissionError(f"'{layer.name()}' is not editable from the field app")
            if layer.isEditable():
                raise RuntimeError(f"'{layer.name()}' is in edit mode in QGIS. Save and stop editing, then sync again.")
            provider = layer.dataProvider()
            op = edit["op"]
            self._ensure_gnss_fields(layer, edit.get("properties") or {})
            if op == "add":
                feat = QgsFeature(layer.fields())
                self._set_attributes(layer, feat, edit.get("properties") or {})
                if edit.get("geometry"):
                    feat.setGeometry(self._geometry(layer, edit["geometry"]))
                ok, added = provider.addFeatures([feat])
                if not ok:
                    raise RuntimeError("; ".join(provider.errors()) or "Provider refused the feature")
                result["fid"] = added[0].id()
            elif op == "update":
                self._check_base(layer, edit)
                fid = int(edit["fid"])
                props = edit.get("properties")
                if props is not None:
                    changes = {}
                    pk = set(layer.primaryKeyAttributes())
                    for name, value in props.items():
                        idx = layer.fields().indexOf(name)
                        if idx >= 0 and idx not in pk and _int(layer.fields().fieldOrigin(idx)) == ORIGIN_PROVIDER:
                            changes[idx] = self._convert(layer.fields().at(idx), value)
                    if changes and not provider.changeAttributeValues({fid: changes}):
                        raise RuntimeError("; ".join(provider.errors()) or "Attribute update failed")
                if edit.get("geometry"):
                    if not provider.changeGeometryValues({fid: self._geometry(layer, edit["geometry"])}):
                        raise RuntimeError("; ".join(provider.errors()) or "Geometry update failed")
                result["fid"] = fid
            elif op == "delete":
                if self._check_base(layer, edit) and not provider.deleteFeatures([int(edit["fid"])]):
                    raise RuntimeError("; ".join(provider.errors()) or "Delete failed")
            else:
                raise ValueError(f"Unknown op {op}")
            layer.updateExtents()
            layer.triggerRepaint()
            result["ok"] = True
        except Conflict as e:
            result["error"] = str(e)
            result["conflict"] = True
        except Exception as e:
            result["error"] = str(e)
        return result

    def _convert(self, field, value):
        if value is None or value == "":
            return None
        try:
            return field.convertCompatible(value)
        except Exception:
            return value

    def _ensure_gnss_fields(self, layer, props):
        """Add the phone's survey attributes (gnss_*) to the layer the first time they arrive."""
        from qgis.PyQt.QtCore import QVariant

        missing = [k for k in props if k.startswith("gnss_") and layer.fields().indexOf(k) < 0]
        if not missing:
            return
        provider = layer.dataProvider()
        flag = getattr(getattr(Qgis, "VectorProviderCapability", None), "AddAttributes", None)
        if not provider.capabilities() & (flag if flag is not None else QgsVectorDataProvider.AddAttributes):
            return  # e.g. a read-only format: the values are dropped as before
        fields = []
        for name in missing:
            value = props[name]
            if name == "gnss_time":
                kind = QVariant.DateTime
            elif isinstance(value, bool):
                kind = QVariant.Bool
            elif isinstance(value, int):
                kind = QVariant.LongLong
            elif isinstance(value, float) or value is None:
                kind = QVariant.Double
            else:
                kind = QVariant.String
            fields.append(QgsField(name, kind))
        if provider.addAttributes(fields):
            layer.updateFields()
            self.bump_structure()

    def _set_attributes(self, layer, feat, props):
        pk = set(layer.primaryKeyAttributes())
        for name, value in props.items():
            idx = layer.fields().indexOf(name)
            if idx >= 0 and idx not in pk and _int(layer.fields().fieldOrigin(idx)) == ORIGIN_PROVIDER:
                feat.setAttribute(idx, self._convert(layer.fields().at(idx), value))

    def _geometry(self, layer, geojson):
        geom = QgsGeometry.fromWkt(geojson_to_wkt(geojson))
        if geom.isNull():
            raise ValueError("Invalid geometry")
        if layer.crs().isValid() and layer.crs() != WGS84:
            geom.transform(QgsCoordinateTransform(WGS84, layer.crs(), self._project()))
        coerced = geom.coerceToType(layer.wkbType())
        return coerced[0] if coerced else geom
