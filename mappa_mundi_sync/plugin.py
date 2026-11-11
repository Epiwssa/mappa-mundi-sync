import contextlib
import os
import time
from urllib.parse import quote

from qgis.core import Qgis, QgsMessageLog, QgsProject, QgsRasterLayer, QgsSettings, QgsVectorLayer
from qgis.gui import QgsMapToolExtent, QgsRubberBand
from qgis.PyQt.QtCore import QRectF, Qt, QTime, QTimer, pyqtSignal
from qgis.PyQt.QtGui import QColor, QIcon, QImage, QPainter, QPixmap
from qgis.PyQt.QtWidgets import (
    QAction,
    QComboBox,
    QDialog,
    QDockWidget,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QListWidget,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .area import FieldArea, layer_in_area, load_area, save_area
from .backend import MAX_PHONE_FEATURES, MODE_LABELS, ProjectBackend, allowed_modes, default_mode, layer_mode, safe_name, set_layer_mode
from .packager import RasterPackager, estimate_tiles, suggested_max_zoom
from .qrcode import encode as qr_encode
from .secure import code_to_key, key_to_code, new_key
from .server import DEFAULT_PORT, MainThreadInvoker, SyncServer, local_ip_addresses, pairing_url

HERE = os.path.dirname(__file__)

# While the app is in Google Play testing, people join a Google Group that is the tester list.
# The page walks them through it; the email is a fallback if they get stuck.
APP_TESTING_URL = "https://github.com/Epiwssa/mappa-mundi-sync/blob/main/TESTING.md"
ACCESS_EMAIL = "mrepiwssa@gmail.com"
ACCESS_MAILTO = "mailto:{}?subject={}&body={}".format(
    ACCESS_EMAIL,
    quote("Mappa Mundi tester request"),
    quote("Hi, please add me as a tester for the Mappa Mundi app.\n\n"
          "The Google account (Gmail) I use on my Android phone is: \n"),
)


# Pairing QR code sizes in pixels; the user picks one, and it is remembered.
QR_SIZES = {"small": 150, "medium": 230, "large": 340}


def qr_pixmap(text, module_px=5, quiet=4, size=None):
    """QR code image. With size, the modules are scaled (in whole pixels, so it stays sharp) to about size px."""
    matrix = qr_encode(text)
    n = len(matrix) + quiet * 2
    if size:
        module_px = max(2, size // n)
    image = QImage(n * module_px, n * module_px, QImage.Format.Format_RGB32)
    image.fill(QColor("white"))
    painter = QPainter(image)
    for y, row in enumerate(matrix):
        for x, dark in enumerate(row):
            if dark:
                painter.fillRect(QRectF((x + quiet) * module_px, (y + quiet) * module_px, module_px, module_px), QColor("black"))
    painter.end()
    return QPixmap.fromImage(image)


class ClickableLabel(QLabel):
    clicked = pyqtSignal()

    def mousePressEvent(self, event):
        self.clicked.emit()
        super().mousePressEvent(event)


def saved_key():
    """The pairing key stays the same between QGIS sessions so paired phones keep working."""
    settings = QgsSettings()
    key = code_to_key(settings.value("mapper_sync/pairing_code", ""))
    if key is None:
        key = new_key()
        settings.setValue("mapper_sync/pairing_code", key_to_code(key))
    return key


class MapperSyncPlugin:
    def __init__(self, iface):
        self.iface = iface
        self.dock = None
        self.action = None
        self.server = None
        self.invoker = None
        self.backend = None
        self.packager = None
        self.area = None  # FieldArea, or None for the whole project
        self.area_band = None
        self.area_tool = None
        self.previous_tool = None

    # --- QGIS plugin lifecycle -------------------------------------------

    def initGui(self):
        self.action = QAction(QIcon(os.path.join(HERE, "icon.png")), "Mappa Mundi Sync", self.iface.mainWindow())
        self.action.setCheckable(True)
        self.action.toggled.connect(self._toggle_dock)
        self.iface.addToolBarIcon(self.action)
        self.iface.addPluginToMenu("Mappa Mundi Sync", self.action)

        self.invoker = MainThreadInvoker()
        self.backend = ProjectBackend(self.invoker, canvas_extent=self._canvas_extent, field_area=lambda: self.area)
        self.backend.watch_project()
        self._build_dock()
        self.device_timer = QTimer()
        self.device_timer.timeout.connect(self._refresh_devices)
        self.device_timer.start(3000)

        project = QgsProject.instance()
        project.layersAdded.connect(self._refresh_layers)
        project.layersRemoved.connect(self._refresh_layers)
        project.readProject.connect(self._refresh_layers)
        project.readProject.connect(self._load_area)
        project.cleared.connect(self._load_area)
        self.iface.mapCanvas().extentsChanged.connect(self._canvas_moved)
        self._load_area()

    def unload(self):
        self._stop_server()
        self.device_timer.stop()
        self.backend.unwatch_project()
        project = QgsProject.instance()
        for sig in (project.layersAdded, project.layersRemoved, project.readProject):
            with contextlib.suppress(TypeError):  # already disconnected
                sig.disconnect(self._refresh_layers)
        for sig in (project.readProject, project.cleared):
            with contextlib.suppress(TypeError):
                sig.disconnect(self._load_area)
        with contextlib.suppress(TypeError):
            self.iface.mapCanvas().extentsChanged.disconnect(self._canvas_moved)
        self._stop_drawing()
        if self.area_band:
            self.area_band.reset()
            self.iface.mapCanvas().scene().removeItem(self.area_band)
            self.area_band = None
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
        """Where phones open the map: the field area if there is one, else the current view."""
        if self.area:
            return self.area.geometry.boundingBox(), self.area.crs
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
        app_label.setToolTip("Joining takes two steps on the phone: join the tester group, then opt in on "
                             "Google Play. The page explains both. Stuck? Ask for access by email.")
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
        self.qr_label = ClickableLabel()
        self.qr_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.qr_label.setVisible(False)
        self.qr_label.setCursor(Qt.CursorShape.PointingHandCursor)
        self.qr_label.setToolTip("Scan with the Mappa Mundi app: Connect > Scan QR code. Click to enlarge.")
        self.qr_label.clicked.connect(self._show_big_qr)
        bl.addWidget(self.qr_label)
        self.qr_size_row = QWidget()
        qr_row = QHBoxLayout(self.qr_size_row)
        qr_row.setContentsMargins(0, 0, 0, 0)
        qr_row.addStretch(1)
        qr_row.addWidget(QLabel("QR size"))
        self.qr_size_combo = QComboBox()
        for key in QR_SIZES:
            self.qr_size_combo.addItem(key.capitalize(), key)
        saved = QgsSettings().value("mapper_sync/qr_size", "medium")
        self.qr_size_combo.setCurrentIndex(max(0, self.qr_size_combo.findData(saved)))
        self.qr_size_combo.currentIndexChanged.connect(self._qr_size_changed)
        qr_row.addWidget(self.qr_size_combo)
        enlarge_btn = QPushButton("Enlarge")
        enlarge_btn.setToolTip("Show the QR code big, in its own window")
        enlarge_btn.clicked.connect(self._show_big_qr)
        qr_row.addWidget(enlarge_btn)
        qr_row.addStretch(1)
        self.qr_size_row.setVisible(False)
        bl.addWidget(self.qr_size_row)
        self.pairing = None  # pairing URL in the QR code while the server runs
        pin_row = QHBoxLayout()
        self.code_label = QLabel()
        self.code_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.code_label.setToolTip("Encrypts everything between phones and this computer. "
                                   "Type it in the app under Enter address manually, or scan the QR code.")
        self.new_code_btn = QPushButton("New code")
        self.new_code_btn.setToolTip("Phones paired with the old code will need to scan the new QR code")
        self.new_code_btn.clicked.connect(self._new_code)
        pin_row.addWidget(self.code_label, 1)
        pin_row.addWidget(self.new_code_btn)
        bl.addLayout(pin_row)
        self.device_list = QListWidget()
        self.device_list.setMaximumHeight(90)
        bl.addWidget(QLabel("Phones seen"))
        bl.addWidget(self.device_list)
        layout.addWidget(box)
        self._show_code()

        # Layers
        box = QGroupBox("Layers shared with the phone")
        bl = QVBoxLayout(box)
        area_row = QHBoxLayout()
        self.area_label = QLabel()
        self.area_label.setWordWrap(True)
        area_btn = QPushButton("Set field area")
        area_btn.setToolTip("The part of the project to take into the field")
        menu = QMenu(area_btn)
        menu.addAction("Use the current map view", self._area_from_view)
        menu.addAction("Draw a rectangle on the map", self._draw_area)
        menu.addAction("Use the selected features", self._area_from_selection)
        menu.addSeparator()
        self.clear_area_action = menu.addAction("Clear (whole project)", self._clear_area)
        area_btn.setMenu(menu)
        area_row.addWidget(self.area_label, 1)
        area_row.addWidget(area_btn)
        bl.addLayout(area_row)
        pick_row = QHBoxLayout()
        self.share_area_btn = QPushButton("Share layers in area")
        self.share_area_btn.setToolTip("Share the layers that have data in the field area and exclude the rest")
        self.share_area_btn.clicked.connect(self._share_in_area)
        share_screen_btn = QPushButton("Share layers on screen")
        share_screen_btn.setToolTip("Share the layers that are switched on and have data in the current "
                                    "map view, and exclude the rest")
        share_screen_btn.clicked.connect(self._share_on_screen)
        pick_row.addWidget(self.share_area_btn)
        pick_row.addWidget(share_screen_btn)
        bl.addLayout(pick_row)
        self.layer_table = QTableWidget(0, 2)
        self.layer_table.setMinimumHeight(180)
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
        self.extent_combo = QComboBox()
        self.extent_combo.addItem("Field area", "area")
        self.extent_combo.addItem("Current map view", "view")
        self.extent_combo.addItem("Whole layer", "full")
        self.extent_combo.setToolTip("Tiles are only made inside this extent")
        self.extent_combo.currentIndexChanged.connect(self._update_estimate)
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
        form.addRow("Extent", self.extent_combo)
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

        # Scrolls instead of growing: a tall panel would stretch the QGIS window off the screen.
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(root)
        self.dock.setWidget(scroll)
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
            if layer_mode(layer) != "exclude" and self.backend.too_big(layer):
                where = "in the field area" if self.area else "and no field area is set"
                item.setText(f"⚠ {layer.name()}")
                item.setForeground(QColor(180, 60, 0))
                item.setToolTip(f"More than {MAX_PHONE_FEATURES:,} features {where}, too many for a phone. "
                                "It won't be sent. Set a smaller field area, or exclude it.")
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

    # --- field area -------------------------------------------------------

    def _load_area(self, *_):
        self.area = load_area(QgsProject.instance())
        self._show_area()
        self._refresh_layers()

    def _set_area(self, area):
        self.area = area
        save_area(QgsProject.instance(), area)
        QgsProject.instance().setDirty(True)
        self.backend.area_changed()  # phones open on the new area and refetch its features
        self._show_area()
        self._refresh_layers()
        if area:
            self.extent_combo.setCurrentIndex(self.extent_combo.findData("area"))
            self._log(f"Field area set: {area.label}")
        else:
            self._log("Field area cleared")

    def _show_area(self):
        if not self.dock:
            return
        if self.area:
            self.area_label.setText(f"Field area: <b>{self.area.label}</b>")
        else:
            self.area_label.setText("Field area: <b>whole project</b>")
        self.share_area_btn.setEnabled(self.area is not None)
        self.clear_area_action.setEnabled(self.area is not None)
        canvas = self.iface.mapCanvas()
        if self.area_band is None:
            self.area_band = QgsRubberBand(canvas)
            self.area_band.setStrokeColor(QColor(214, 51, 132))
            self.area_band.setFillColor(QColor(214, 51, 132, 25))
            self.area_band.setWidth(2)
        if self.area:
            self.area_band.setToGeometry(self.area.geometry, self.area.crs)
        else:
            self.area_band.reset()
        self._update_estimate()

    def _canvas_area(self, label):
        """The current map view as an area, or None if the map has nothing to show yet."""
        canvas = self.iface.mapCanvas()
        if canvas.extent().isEmpty():
            return None
        return FieldArea.from_rect(canvas.extent(), canvas.mapSettings().destinationCrs(), label)

    def _area_from_view(self):
        area = self._canvas_area("map view")
        if area is None:
            QMessageBox.information(self.dock, "Mappa Mundi Sync", "Zoom the map to the field area first.")
            return
        self._set_area(area)

    def _draw_area(self):
        canvas = self.iface.mapCanvas()
        self._stop_drawing()
        self.previous_tool = canvas.mapTool()
        self.area_tool = QgsMapToolExtent(canvas)
        self.area_tool.extentChanged.connect(self._area_drawn)
        canvas.setMapTool(self.area_tool)
        self.iface.messageBar().pushInfo("Mappa Mundi Sync", "Drag a rectangle on the map to set the field area.")

    def _area_drawn(self, rect):
        if rect.isEmpty():
            return
        crs = self.iface.mapCanvas().mapSettings().destinationCrs()
        self._stop_drawing()
        self._set_area(FieldArea.from_rect(rect, crs, "drawn rectangle"))

    def _stop_drawing(self):
        if self.area_tool is None:
            return
        canvas = self.iface.mapCanvas()
        if canvas.mapTool() is self.area_tool:
            if self.previous_tool:
                canvas.setMapTool(self.previous_tool)
            else:
                canvas.unsetMapTool(self.area_tool)
        self.area_tool.deleteLater()
        self.area_tool = None
        self.previous_tool = None

    def _area_from_selection(self):
        layer = self.iface.activeLayer()
        if not isinstance(layer, QgsVectorLayer) or layer.selectedFeatureCount() == 0:
            QMessageBox.information(self.dock, "Mappa Mundi Sync",
                                    "Select one or more features in the active layer first.")
            return
        area = FieldArea.from_features(layer, layer.selectedFeatures())
        if area is None:
            QMessageBox.information(self.dock, "Mappa Mundi Sync",
                                    "The selection has no area. Select polygons, or several points or lines "
                                    "spread over the area.")
            return
        self._set_area(area)

    def _clear_area(self):
        self._set_area(None)

    def _share_in_area(self):
        if self.area:
            self._share_only(self.area, "field area")

    def _share_on_screen(self):
        area = self._canvas_area("map view")
        if area is not None:
            self._share_only(area, "map view", candidates=set(self.iface.mapCanvas().layers()))

    def _share_only(self, area, where, candidates=None):
        """Share the layers with data in the area (keeping their chosen mode); exclude the rest."""
        layers = [n.layer() for n in QgsProject.instance().layerTreeRoot().findLayers() if n.layer() is not None]
        shared = 0
        for layer in layers:
            keep = (candidates is None or layer in candidates) and layer_in_area(layer, area)
            if keep:
                if layer_mode(layer) == "exclude":
                    set_layer_mode(layer, default_mode(layer))
                shared += 1
            else:
                set_layer_mode(layer, "exclude")
        QgsProject.instance().setDirty(True)
        self.backend.bump_structure()
        self._refresh_layers()
        self._log(f"Sharing {shared} layer{'s' if shared != 1 else ''} with data in the {where}; "
                  f"{len(layers) - shared} excluded")

    def _canvas_moved(self):
        if self.dock and self.dock.isVisible() and self.extent_combo.currentData() == "view":
            self._update_estimate()

    def _raster_clip(self):
        """The area offline tiles are cut to, or None for the whole layer."""
        choice = self.extent_combo.currentData()
        if choice == "view":
            return self._canvas_area("map view")
        if choice == "area":
            return self.area
        return None

    def _show_code(self):
        self.code_label.setText(f"Pairing code: <b>{key_to_code(saved_key())}</b>")

    def _new_code(self):
        key = new_key()
        QgsSettings().setValue("mapper_sync/pairing_code", key_to_code(key))
        if self.server:
            self.server.key = key
            self._show_connection()
        self._show_code()
        self._log("New pairing code created; phones must scan the QR code again")

    def _show_app_qr(self):
        box = QMessageBox(self.dock)
        box.setWindowTitle("Get the Mappa Mundi app")
        box.setIconPixmap(qr_pixmap(APP_TESTING_URL, module_px=6))
        box.setText(
            "Scan with the Android phone's camera to open the instructions for joining the test: "
            "join the tester group, then become a tester on Google Play.<br><br>"
            "Stuck? "
            f'<a href="{ACCESS_MAILTO}">Ask for access</a> at {ACCESS_EMAIL} '
            "and include the Gmail address you use on the phone."
        )
        box.setTextFormat(Qt.TextFormat.RichText)
        box.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        box.exec()

    def _render_qr(self):
        if self.pairing:
            self.qr_label.setPixmap(qr_pixmap(self.pairing, size=QR_SIZES[self.qr_size_combo.currentData()]))

    def _qr_size_changed(self, *_):
        QgsSettings().setValue("mapper_sync/qr_size", self.qr_size_combo.currentData())
        self._render_qr()

    def _show_big_qr(self):
        if not self.pairing:
            return
        screen = self.dock.screen().availableGeometry() if hasattr(self.dock, "screen") else None
        size = int(min(screen.width(), screen.height()) * 0.7) if screen else 600
        dialog = QDialog(self.dock)
        dialog.setWindowTitle("Mappa Mundi Sync: scan to connect")
        layout = QVBoxLayout(dialog)
        image = QLabel()
        image.setPixmap(qr_pixmap(self.pairing, size=size))
        image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(image)
        text = QLabel(f"In the Mappa Mundi app tap <b>Scan QR code</b>.<br>Pairing code <b>{self.server.code}</b>")
        text.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(text)
        dialog.exec()

    def _show_connection(self):
        port = self.server.port
        ips = local_ip_addresses()
        self.pairing = pairing_url(ips, port, self.server.code, QgsProject.instance().baseName())
        self._render_qr()
        self.qr_label.setVisible(True)
        self.qr_size_row.setVisible(True)
        addresses = ", ".join(f"<b>{ip}:{port}</b>" for ip in ips)
        self.status_label.setText(
            f"Running. In the Mappa Mundi app tap <b>Scan QR code</b>, or enter {addresses} "
            f"and pairing code <b>{self.server.code}</b>.<br>"
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
        n = estimate_tiles(layer, self.zmin_spin.value(), self.zmax_spin.value(), self._raster_clip())
        if n is None:
            self.estimate_label.setText("This layer is outside the chosen extent.")
            self.package_btn.setEnabled(False)
            return
        self.package_btn.setEnabled(self.packager is None)
        note = " Field area not set: whole layer." if self.extent_combo.currentData() == "area" and not self.area else ""
        self.estimate_label.setText(f"About {n:,} tiles (~{n * 25 / 1024:,.0f} MB as JPEG).{note}")

    # --- actions ----------------------------------------------------------

    def _toggle_server(self):
        if self.server and self.server.running:
            self._stop_server()
        else:
            self._start_server()

    def _start_server(self):
        port = self.port_spin.value()
        QgsSettings().setValue("mapper_sync/port", port)
        self.server = SyncServer(self.backend, port=port, key=saved_key(), log=self._log)
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
            self.qr_size_row.setVisible(False)
            self.pairing = None
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
        clip = self._raster_clip()

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

        try:
            self.packager = RasterPackager(
                layer, out_dir, zmin, zmax, self.format_combo.currentData(), done,
                on_progress=lambda p: self.progress.setValue(int(p)), clip=clip,
            )
        except ValueError as e:
            QMessageBox.information(self.dock, "Mappa Mundi Sync", str(e))
            return
        self.package_btn.setEnabled(False)
        self.progress.setValue(0)
        self.progress.setVisible(True)
        self._log(f"Building tiles for {layer.name()} (z{zmin}-{zmax}, {self.extent_combo.currentText().lower()})")
        self.packager.start()
