"""Smoke test: load the plugin with a stub iface, build the dock, start/stop the server."""

import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
# The test saves a port and PIN through QgsSettings; keep them out of the real QGIS profile.
os.environ["QGIS_CUSTOM_CONFIG_PATH"] = tempfile.mkdtemp(prefix="mapper_ui_cfg_")

from qgis.core import QgsApplication, QgsProject, QgsRasterLayer, QgsVectorLayer  # noqa: E402
from qgis.gui import QgsMapCanvas  # noqa: E402
from qgis.PyQt.QtWidgets import QMainWindow  # noqa: E402

app = QgsApplication([], True)
app.initQgis()


class StubIface:
    def __init__(self):
        self.window = QMainWindow()
        self.canvas = QgsMapCanvas()

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


import mapper_sync  # noqa: E402

folder = tempfile.mkdtemp(prefix="mapper_ui_")
QgsProject.instance().write(os.path.join(folder, "ui.qgz"))
iface = StubIface()
plugin = mapper_sync.classFactory(iface)
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
assert plugin.server and plugin.server.running and "PIN" in plugin.status_label.text()
pin = plugin.server.pin
assert plugin.qr_label.isVisibleTo(plugin.dock) and not plugin.qr_label.pixmap().isNull()
assert pin in plugin.pin_label.text()

shot = os.environ.get("MAPPER_UI_SHOT")
if shot:
    plugin.dock.resize(380, 900)
    plugin.dock.widget().grab().save(shot)

# The PIN survives a server restart (phones stay paired)...
plugin._toggle_server()
assert plugin.server is None and plugin.start_btn.text() == "Start sync server"
assert not plugin.qr_label.isVisibleTo(plugin.dock)
plugin._toggle_server()
assert plugin.server.pin == pin, "PIN should persist between server runs"
# ...until the user asks for a new one, which applies to the running server immediately.
plugin._new_pin()
assert plugin.server.pin != pin and plugin.server.pin in plugin.status_label.text()
plugin._toggle_server()

# Phones appear in the "Phones seen" list.
plugin.backend.touch_device("d1", "Survey Phone", "192.168.1.50", "Sent edits")
plugin._refresh_devices()
assert plugin.device_list.count() == 1 and "Survey Phone" in plugin.device_list.item(0).text()

# changing a mode through the combo is stored on the layer
row = modes.index("edit")
combo = plugin.layer_table.cellWidget(row, 1)
combo.setCurrentIndex(combo.findData("view"))
from mapper_sync.backend import layer_mode  # noqa: E402
assert layer_mode(pts) == "view"

plugin.unload()
print("RESULT: PASS (pin was", pin + ")")
app.exitQgis()
