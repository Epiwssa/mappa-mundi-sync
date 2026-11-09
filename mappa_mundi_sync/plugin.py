import contextlib
import os
import time
from urllib.parse import quote

from qgis.core import Qgis, QgsMessageLog, QgsProject, QgsRasterLayer, QgsSettings
from qgis.PyQt.QtCore import QRectF, Qt, QTime, QTimer
from qgis.PyQt.QtGui import QColor, QIcon, QImage, QPainter, QPixmap
from qgis.PyQt.QtWidgets import (
    QAction,
    QComboBox,
    QDockWidget,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QListWidget,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .backend import MODE_LABELS, ProjectBackend, allowed_modes, layer_mode, safe_name, set_layer_mode
from .packager import RasterPackager, estimate_tiles, suggested_max_zoom
from .qrcode import encode as qr_encode
from .server import DEFAULT_PORT, MainThreadInvoker, SyncServer, local_ip_addresses, new_pin, pairing_url

HERE = os.path.dirname(__file__)

# While the app is in Google Play testing: the opt-in page works for people on the tester list,
# everyone else can ask to be added by email.
APP_TESTING_URL = "https://play.google.com/apps/testing/com.epiwssa.mappamundi"
ACCESS_EMAIL = "mrepiwssa@gmail.com"
ACCESS_MAILTO = "mailto:{}?subject={}&body={}".format(
    ACCESS_EMAIL,
    quote("Mappa Mundi tester request"),
    quote("Hi, please add me as a tester for the Mappa Mundi app.\n\n"
          "The Google account (Gmail) I use on my Android phone is: \n"),
)


def qr_pixmap(text, module_px=5, quiet=4):
    matrix = qr_encode(text)
    n = len(matrix) + quiet * 2
    image = QImage(n * module_px, n * module_px, QImage.Format.Format_RGB32)
    image.fill(QColor("white"))
    painter = QPainter(image)
    for y, row in enumerate(matrix):
        for x, dark in enumerate(row):
            if dark:
                painter.fillRect(QRectF((x + quiet) * module_px, (y + quiet) * module_px, module_px, module_px), QColor("black"))
    painter.end()
    return QPixmap.fromImage(image)


def saved_pin():
    """The PIN stays the same between QGIS sessions so paired phones keep working."""
    settings = QgsSettings()
    pin = settings.value("mapper_sync/pin", "")
    if not (isinstance(pin, str) and len(pin) == 6 and pin.isdigit()):
        pin = new_pin()
        settings.setValue("mapper_sync/pin", pin)
    return pin


class MapperSyncPlugin:
    def __init__(self, iface):
        self.iface = iface
        self.dock = None
        self.action = None
        self.server = None
        self.invoker = None
        self.backend = None
        self.packager = None

    # --- QGIS plugin lifecycle -------------------------------------------

    def initGui(self):
        self.action = QAction(QIcon(os.path.join(HERE, "icon.png")), "Mappa Mundi Sync", self.iface.mainWindow())
        self.action.setCheckable(True)
        self.action.toggled.connect(self._toggle_dock)
        self.iface.addToolBarIcon(self.action)
        self.iface.addPluginToMenu("Mappa Mundi Sync", self.action)

        self.invoker = MainThreadInvoker()
        self.backend = ProjectBackend(self.invoker, canvas_extent=self._canvas_extent)
        self.backend.watch_project()
        self._build_dock()
        self.device_timer = QTimer()
        self.device_timer.timeout.connect(self._refresh_devices)
        self.device_timer.start(3000)

        project = QgsProject.instance()
        project.layersAdded.connect(self._refresh_layers)
        project.layersRemoved.connect(self._refresh_layers)
        project.readProject.connect(self._refresh_layers)

    def unload(self):
        self._stop_server()
        self.device_timer.stop()
        self.backend.unwatch_project()
        project = QgsProject.instance()
        for sig in (project.layersAdded, project.layersRemoved, project.readProject):
            with contextlib.suppress(TypeError):  # already disconnected
                sig.disconnect(self._refresh_layers)
        self.iface.removePluginMenu("Mappa Mundi Sync", self.action)
        self.iface.removeToolBarIcon(self.action)
        if self.dock:
            self.iface.removeDockWidget(self.dock)
            self.dock.deleteLater()

    def _toggle_dock(self, on):
        self.dock.setVisible(on)
        if on:
            self._refresh_layers()

    def _canvas_extent(self):
        canvas = self.iface.mapCanvas()
        return canvas.extent(), canvas.mapSettings().destinationCrs()

    # --- UI ---------------------------------------------------------------

    def _build_dock(self):
        self.dock = QDockWidget("Mappa Mundi Sync", self.iface.mainWindow())
        self.dock.setObjectName("MapperSyncDock")
        self.dock.visibilityChanged.connect(lambda v: self.action.setChecked(v))
        root = QWidget()
        layout = QVBoxLayout(root)

        # Getting the phone app
        app_row = QHBoxLayout()
        app_label = QLabel(
            f'Need the phone app? Android, in testing: <a href="{APP_TESTING_URL}">join the test</a>'
            f' or <a href="{ACCESS_MAILTO}">ask for access</a> ({ACCESS_EMAIL}).'
        )
        app_label.setWordWrap(True)
        app_label.setOpenExternalLinks(True)
        app_label.setToolTip("The test page only works once you're on the tester list. "
                             "If it says the app isn't available, ask for access by email.")
        app_qr_btn = QPushButton("QR")
        app_qr_btn.setToolTip("Show the test link as a QR code to scan with the phone")
        app_qr_btn.clicked.connect(self._show_app_qr)
        app_row.addWidget(app_label, 1)
        app_row.addWidget(app_qr_btn)
        layout.addLayout(app_row)

        # Server
        box = QGroupBox("Phone connection")
        bl = QVBoxLayout(box)
        row = QHBoxLayout()
        self.port_spin = QSpinBox()
        self.port_spin.setRange(1024, 65535)
        self.port_spin.setValue(int(QgsSettings().value("mapper_sync/port", DEFAULT_PORT)))
        self.start_btn = QPushButton("Start sync server")
        self.start_btn.clicked.connect(self._toggle_server)
        row.addWidget(QLabel("Port"))
        row.addWidget(self.port_spin)
        row.addWidget(self.start_btn, 1)
        bl.addLayout(row)
        self.status_label = QLabel("Server stopped.")
        self.status_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.status_label.setWordWrap(True)
        bl.addWidget(self.status_label)
        self.qr_label = QLabel()
        self.qr_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.qr_label.setVisible(False)
        self.qr_label.setToolTip("Scan with the Mappa Mundi app: Connect > Scan QR code")
        bl.addWidget(self.qr_label)
        pin_row = QHBoxLayout()
        self.pin_label = QLabel()
        self.new_pin_btn = QPushButton("New PIN")
        self.new_pin_btn.setToolTip("Phones paired with the old PIN will need to scan the new QR code")
        self.new_pin_btn.clicked.connect(self._new_pin)
        pin_row.addWidget(self.pin_label, 1)
        pin_row.addWidget(self.new_pin_btn)
        bl.addLayout(pin_row)
        self.device_list = QListWidget()
        self.device_list.setMaximumHeight(90)
        bl.addWidget(QLabel("Phones seen"))
        bl.addWidget(self.device_list)
        layout.addWidget(box)
        self._show_pin()

        # Layers
        box = QGroupBox("Layers shared with the phone")
        bl = QVBoxLayout(box)
        self.layer_table = QTableWidget(0, 2)
        self.layer_table.setHorizontalHeaderLabels(["Layer", "On phone"])
        self.layer_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.layer_table.verticalHeader().setVisible(False)
        bl.addWidget(self.layer_table)
        layout.addWidget(box, 1)

        # Offline rasters
        box = QGroupBox("Offline tiles for rasters / basemaps")
        form = QFormLayout(box)
        self.raster_combo = QComboBox()
        self.raster_combo.currentIndexChanged.connect(self._raster_selected)
        self.zmin_spin = QSpinBox()
        self.zmin_spin.setRange(0, 22)
        self.zmin_spin.setValue(12)
        self.zmax_spin = QSpinBox()
        self.zmax_spin.setRange(0, 22)
        self.zmax_spin.setValue(18)
        self.zmin_spin.valueChanged.connect(self._update_estimate)
        self.zmax_spin.valueChanged.connect(self._update_estimate)
        self.format_combo = QComboBox()
        self.format_combo.addItem("JPEG (smaller, for photos/orthos)", "jpg")
        self.format_combo.addItem("PNG (keeps transparency)", "png")
        self.estimate_label = QLabel("")
        self.package_btn = QPushButton("Build offline tiles")
        self.package_btn.clicked.connect(self._package)
        self.progress = QProgressBar()
        self.progress.setVisible(False)
        form.addRow("Layer", self.raster_combo)
        zoom_row = QHBoxLayout()
        zoom_row.addWidget(self.zmin_spin)
        zoom_row.addWidget(QLabel("to"))
        zoom_row.addWidget(self.zmax_spin)
        form.addRow("Zoom", zoom_row)
        form.addRow("Format", self.format_combo)
        form.addRow("", self.estimate_label)
        form.addRow(self.package_btn)
        form.addRow(self.progress)
        layout.addWidget(box)

        # Log
        self.log_list = QListWidget()
        self.log_list.setMaximumHeight(110)
        layout.addWidget(QLabel("Activity"))
        layout.addWidget(self.log_list)

        self.dock.setWidget(root)
        self.iface.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.dock)
        self.dock.hide()

    def _log(self, msg):
        # Can be called from server threads; hop to the GUI thread.
        def add():
            self.log_list.insertItem(0, f"{QTime.currentTime().toString('HH:mm:ss')}  {msg}")
            while self.log_list.count() > 200:
                self.log_list.takeItem(self.log_list.count() - 1)
        try:
            self.invoker.call(add, timeout=5)
        except Exception as e:  # GUI busy or closing: keep the message in the QGIS log instead
            QgsMessageLog.logMessage(f"{msg} ({e})", "Mappa Mundi Sync", Qgis.MessageLevel.Info)

    def _refresh_layers(self, *_):
        if not self.dock:
            return
        nodes = QgsProject.instance().layerTreeRoot().findLayers()
        layers = [n.layer() for n in nodes if n.layer() is not None]
        self.layer_table.setRowCount(len(layers))
        for row, layer in enumerate(layers):
            item = QTableWidgetItem(layer.name())
            item.setFlags(Qt.ItemFlag.ItemIsEnabled)
            self.layer_table.setItem(row, 0, item)
            combo = QComboBox()
            for mode in allowed_modes(layer):
                combo.addItem(MODE_LABELS[mode], mode)
            combo.setCurrentIndex(max(0, combo.findData(layer_mode(layer))))
            combo.currentIndexChanged.connect(lambda _i, lyr=layer, c=combo: self._set_mode(lyr, c.currentData()))
            self.layer_table.setCellWidget(row, 1, combo)

        current = self.raster_combo.currentData()
        self.raster_combo.blockSignals(True)
        self.raster_combo.clear()
        for layer in layers:
            if isinstance(layer, QgsRasterLayer):
                self.raster_combo.addItem(layer.name(), layer.id())
        idx = self.raster_combo.findData(current)
        self.raster_combo.setCurrentIndex(max(0, idx))
        self.raster_combo.blockSignals(False)
        self._raster_selected()

    def _set_mode(self, layer, mode):
        set_layer_mode(layer, mode)
        QgsProject.instance().setDirty(True)
        self.backend.bump_structure()

    def _show_pin(self):
        self.pin_label.setText(f"PIN: <b>{saved_pin()}</b>")

    def _new_pin(self):
        pin = new_pin()
        QgsSettings().setValue("mapper_sync/pin", pin)
        if self.server:
            self.server.pin = pin
            self._show_connection()
        self._show_pin()
        self._log("New PIN created; phones must scan the QR code again")

    def _show_app_qr(self):
        box = QMessageBox(self.dock)
        box.setWindowTitle("Get the Mappa Mundi app")
        box.setIconPixmap(qr_pixmap(APP_TESTING_URL, module_px=6))
        box.setText(
            "Scan with the Android phone's camera to open the Google Play test page.<br><br>"
            "If it says the app isn't available, you're not on the tester list yet: "
            f'<a href="{ACCESS_MAILTO}">ask for access</a> at {ACCESS_EMAIL} '
            "and include the Gmail address you use on the phone."
        )
        box.setTextFormat(Qt.TextFormat.RichText)
        box.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        box.exec()

    def _show_connection(self):
        port = self.server.port
        ips = local_ip_addresses()
        url = pairing_url(ips, port, self.server.pin, QgsProject.instance().baseName())
        self.qr_label.setPixmap(qr_pixmap(url))
        self.qr_label.setVisible(True)
        addresses = ", ".join(f"<b>{ip}:{port}</b>" for ip in ips)
        self.status_label.setText(
            f"Running. In the Mappa Mundi app tap <b>Scan QR code</b>, or enter {addresses} "
            f"and PIN <b>{self.server.pin}</b>.<br>"
            "<small>Phone and PC must be on the same Wi-Fi. If the phone can't connect, "
            "allow QGIS through Windows Firewall for private networks.</small>"
        )

    def _refresh_devices(self):
        if not self.dock or self.dock.isHidden():
            return
        now = time.time()
        self.device_list.clear()
        for d in self.backend.device_list():
            ago = int(now - d["lastSeen"])
            when = "now" if ago < 45 else f"{ago // 60} min ago" if ago < 3600 else f"{ago // 3600} h ago"
            action = f" · {d['lastAction']}" if d.get("lastAction") else ""
            self.device_list.addItem(f"{d['name']} ({d['ip']}) · {when}{action}")

    def _selected_raster(self):
        return QgsProject.instance().mapLayer(self.raster_combo.currentData() or "")

    def _raster_selected(self, *_):
        layer = self._selected_raster()
        self.package_btn.setEnabled(layer is not None and self.packager is None)
        if layer is None:
            self.estimate_label.setText("Add a raster layer to build offline tiles.")
            return
        if layer.providerType() != "wms":
            self.zmax_spin.setValue(suggested_max_zoom(layer))
            self.zmin_spin.setValue(max(0, self.zmax_spin.value() - 6))
        self._update_estimate()

    def _update_estimate(self, *_):
        layer = self._selected_raster()
        if layer is None:
            return
        if self.zmin_spin.value() > self.zmax_spin.value():
            self.estimate_label.setText("Min zoom must not exceed max zoom.")
            return
        n = estimate_tiles(layer, self.zmin_spin.value(), self.zmax_spin.value())
        self.estimate_label.setText(f"About {n:,} tiles (~{n * 25 / 1024:,.0f} MB as JPEG)")

    # --- actions ----------------------------------------------------------

    def _toggle_server(self):
        if self.server and self.server.running:
            self._stop_server()
        else:
            self._start_server()

    def _start_server(self):
        port = self.port_spin.value()
        QgsSettings().setValue("mapper_sync/port", port)
        self.server = SyncServer(self.backend, port=port, pin=saved_pin(), log=self._log)
        try:
            self.server.start()
        except OSError as e:
            self.server = None
            QMessageBox.warning(self.dock, "Mappa Mundi Sync", f"Could not start on port {port}: {e}")
            return
        self._show_connection()
        self.start_btn.setText("Stop sync server")
        self.port_spin.setEnabled(False)
        self._log(f"Server started on port {port}")

    def _stop_server(self):
        if self.server:
            self.server.stop()
            self.server = None
            self._log("Server stopped")
        if self.dock:
            self.status_label.setText("Server stopped.")
            self.qr_label.setVisible(False)
            self.start_btn.setText("Start sync server")
            self.port_spin.setEnabled(True)

    def _package(self):
        layer = self._selected_raster()
        if layer is None:
            return
        if not QgsProject.instance().homePath():
            QMessageBox.information(self.dock, "Mappa Mundi Sync", "Save the project first; tiles are stored next to it.")
            return
        zmin, zmax = self.zmin_spin.value(), self.zmax_spin.value()
        if zmin > zmax:
            return
        out_dir = os.path.join(self.backend.tiles_root(), safe_name(layer.id()))
        self.package_btn.setEnabled(False)
        self.progress.setValue(0)
        self.progress.setVisible(True)
        self._log(f"Building tiles for {layer.name()} (z{zmin}-{zmax})")

        def done(ok, error):
            self.packager = None
            self.progress.setVisible(False)
            self.package_btn.setEnabled(True)
            if ok:
                self._set_mode(layer, "offline")
                self._refresh_layers()
                self._log(f"Tiles for {layer.name()} ready. Sync the phone to download them.")
            else:
                self._log(f"Tiles for {layer.name()} failed: {error}")

        self.packager = RasterPackager(
            layer, out_dir, zmin, zmax, self.format_combo.currentData(), done,
            on_progress=lambda p: self.progress.setValue(int(p)),
        )
        self.packager.start()
