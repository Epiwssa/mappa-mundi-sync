"""Smoke test: load the plugin with a stub iface, build the dock, start/stop the server."""

import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
# The test saves a port and pairing code through QgsSettings; keep them out of the real QGIS profile.
os.environ["QGIS_CUSTOM_CONFIG_PATH"] = tempfile.mkdtemp(prefix="mapper_ui_cfg_")

from qgis.core import QgsApplication, QgsProject, QgsRasterLayer, QgsVectorLayer  # noqa: E402
from qgis.gui import QgsMapCanvas, QgsMessageBar  # noqa: E402
from qgis.PyQt.QtWidgets import QMainWindow  # noqa: E402

app = QgsApplication([], True)
app.initQgis()


class StubIface:
    def __init__(self):
        self.window = QMainWindow()
        self.canvas = QgsMapCanvas()
        self.active = None
        self.bar = QgsMessageBar()

    def activeLayer(self):
        return self.active

    def messageBar(self):
        return self.bar

    def mainWindow(self):
        return self.window

    def mapCanvas(self):
        return self.canvas

    def addDockWidget(self, area, dock):
        self.window.addDockWidget(area, dock)

    def removeDockWidget(self, dock):
        self.window.removeDockWidget(dock)

    def addToolBarIcon(self, action):
        pass

    def removeToolBarIcon(self, action):
        pass

    def addPluginToMenu(self, name, action):
        pass

    def removePluginMenu(self, name, action):
        pass


import mappa_mundi_sync  # noqa: E402

folder = tempfile.mkdtemp(prefix="mapper_ui_")
QgsProject.instance().write(os.path.join(folder, "ui.qgz"))
iface = StubIface()
plugin = mappa_mundi_sync.classFactory(iface)
plugin.initGui()

pts = QgsVectorLayer("Point?crs=EPSG:4326&field=name:string", "Pts", "memory")
osm = QgsRasterLayer("type=xyz&url=https://tile.openstreetmap.org/{z}/{x}/{y}.png", "OSM", "wms")
QgsProject.instance().addMapLayers([pts, osm])
plugin.action.setChecked(True)  # shows dock and refreshes

assert plugin.layer_table.rowCount() == 2, plugin.layer_table.rowCount()
modes = [plugin.layer_table.cellWidget(r, 1).currentData() for r in range(2)]
assert sorted(modes) == ["basemap", "edit"], modes
assert plugin.raster_combo.count() == 1

plugin.port_spin.setValue(18766)
plugin._toggle_server()
assert plugin.server and plugin.server.running and plugin.server.code in plugin.status_label.text()
pin = plugin.server.code
assert "&c=" + pin in plugin.pairing, plugin.pairing
assert plugin.qr_label.isVisibleTo(plugin.dock) and not plugin.qr_label.pixmap().isNull()
assert pin in plugin.code_label.text()

# The panel scrolls rather than forcing QGIS taller than the screen.
assert plugin.dock.minimumSizeHint().height() < 300, plugin.dock.minimumSizeHint()
# QR size: each choice gives a different size, and the choice is remembered.
widths = {}
for key in ("small", "medium", "large"):
    plugin.qr_size_combo.setCurrentIndex(plugin.qr_size_combo.findData(key))
    widths[key] = plugin.qr_label.pixmap().width()
assert widths["small"] < widths["medium"] < widths["large"] <= 360, widths
from qgis.core import QgsSettings  # noqa: E402
assert QgsSettings().value("mapper_sync/qr_size") == "large"
plugin.qr_size_combo.setCurrentIndex(plugin.qr_size_combo.findData("medium"))
assert plugin.qr_size_row.isVisibleTo(plugin.dock)

shot = os.environ.get("MAPPER_UI_SHOT")
if shot:
    plugin.dock.resize(380, 900)
    plugin.dock.widget().resize(380, 900)
    plugin.dock.widget().grab().save(shot)

# The pairing code survives a server restart (phones stay paired)...
plugin._toggle_server()
assert plugin.server is None and plugin.start_btn.text() == "Start sync server"
assert not plugin.qr_label.isVisibleTo(plugin.dock) and not plugin.qr_size_row.isVisibleTo(plugin.dock)
plugin._toggle_server()
assert plugin.server.code == pin, "the pairing code should persist between server runs"
# ...until the user asks for a new one, which applies to the running server immediately.
plugin._new_code()
assert plugin.server.code != pin and plugin.server.code in plugin.status_label.text()
plugin._toggle_server()

# Phones appear in the "Phones seen" list.
plugin.backend.touch_device("d1", "Survey Phone", "192.168.1.50", "Sent edits")
plugin._refresh_devices()
assert plugin.device_list.count() == 1 and "Survey Phone" in plugin.device_list.item(0).text()

# changing a mode through the combo is stored on the layer
row = modes.index("edit")
combo = plugin.layer_table.cellWidget(row, 1)
combo.setCurrentIndex(combo.findData("view"))
from mappa_mundi_sync.backend import layer_mode  # noqa: E402
assert layer_mode(pts) == "view"

# Field area: layers with data in it stay shared, the rest are excluded; tiles follow it.
from qgis.core import QgsCoordinateReferenceSystem, QgsFeature, QgsGeometry, QgsPointXY, QgsRectangle  # noqa: E402

wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")
far = QgsVectorLayer("Point?crs=EPSG:4326", "Far", "memory")
for layer, (x, y) in ((pts, (28.0, -26.0)), (far, (10.0, 50.0))):
    f = QgsFeature()
    f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(x, y)))
    layer.dataProvider().addFeatures([f])
    layer.updateExtents()
QgsProject.instance().addMapLayer(far)
iface.canvas.setDestinationCrs(wgs84)
iface.canvas.setLayers([pts, far, osm])
iface.canvas.setExtent(QgsRectangle(27.9, -26.1, 28.1, -25.9))

assert "whole project" in plugin.area_label.text() and not plugin.share_area_btn.isEnabled()
plugin._area_from_view()
assert plugin.area and "map view" in plugin.area_label.text() and plugin.share_area_btn.isEnabled()
assert plugin.extent_combo.currentData() == "area"
assert plugin.backend._manifest()["project"]["bbox"][0] > 27.8, "phones open on the field area"
plugin._share_in_area()
assert layer_mode(pts) == "view" and layer_mode(osm) == "basemap" and layer_mode(far) == "exclude"

# Sharing what's on screen brings back an excluded layer once it's in view.
iface.canvas.setExtent(QgsRectangle(9.9, 49.9, 10.1, 50.1))
plugin._share_on_screen()
assert layer_mode(far) == "edit" and layer_mode(pts) == "exclude" and layer_mode(osm) == "basemap"

# Selected features, and the area is kept in the project.
import mappa_mundi_sync.plugin as plugin_module  # noqa: E402

messages = []
plugin_module.QMessageBox.information = lambda _parent, _title, text: messages.append(text)
plugin._area_from_selection()  # nothing selected: only a message, area unchanged
assert messages and "Select one or more features" in messages[-1] and "map view" in plugin.area.label
far2 = QgsVectorLayer("Point?crs=EPSG:4326", "Sel", "memory")
feats = []
for x, y in ((10.0, 50.0), (10.5, 50.4)):
    f = QgsFeature()
    f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(x, y)))
    feats.append(f)
far2.dataProvider().addFeatures(feats)
far2.selectAll()
iface.active = far2
plugin._area_from_selection()
assert "2 selected features of Sel" in plugin.area.label
from mappa_mundi_sync.area import load_area  # noqa: E402
assert load_area(QgsProject.instance()).label == plugin.area.label

# Drawing switches to the rectangle tool and restores the previous one afterwards.
plugin._draw_area()
assert iface.canvas.mapTool() is plugin.area_tool
plugin._area_drawn(QgsRectangle(27.95, -26.05, 28.05, -25.95))
assert plugin.area.label == "drawn rectangle" and plugin.area_tool is None and iface.canvas.mapTool() is None

# Raster tiles: the online basemap is cut to the area instead of the whole world.
plugin.raster_combo.setCurrentIndex(plugin.raster_combo.findData(osm.id()))
plugin.zmin_spin.setValue(10)
plugin.zmax_spin.setValue(12)
in_area = plugin.estimate_label.text()
plugin.extent_combo.setCurrentIndex(plugin.extent_combo.findData("full"))
whole = plugin.estimate_label.text()
assert in_area != whole and "About" in in_area, (in_area, whole)

plugin._clear_area()
assert plugin.area is None and load_area(QgsProject.instance()) is None and "whole project" in plugin.area_label.text()

plugin.unload()
print("RESULT: PASS (pairing code was", pin + ")")
app.exitQgis()
