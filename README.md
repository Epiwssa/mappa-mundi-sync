# Mappa Mundi Sync

A QGIS plugin that syncs the open project to the **Mappa Mundi** field app on an Android phone over local Wi-Fi. No cloud account is needed.

- **Pairing:** phones pair by scanning a QR code shown in the plugin panel. The connection is protected by a PIN.
- **To the phone:** vector layers with their styles and forms, online basemaps, and rasters rendered as offline tiles.
- **Back to QGIS:** new, edited and deleted features and photos are written into the real layers (GeoPackage, PostGIS, shapefile and others). Conflicting edits are detected.
- **Phone-made layers:** layers and GPS tracks created on a phone are saved to a GeoPackage next to the project and added to it.
- **Survey GPS:** accuracy, averaging and receiver details are stored in `gnss_*` fields, which are created automatically.

## Install

1. In QGIS, open *Plugins → Manage and Install Plugins*.
2. On the *Settings* tab, tick *Show also experimental plugins*.
3. Search for **Mappa Mundi Sync** and install it.

To install from source, copy (or link) the `mappa_mundi_sync` folder into your QGIS profile's `python/plugins` folder. On Windows, `install.ps1` does this for you.

## Use

1. Open a project and click the phone icon on the toolbar.
2. Press **Start sync server**.
3. In the Mappa Mundi app, tap **Connect to QGIS → Scan QR code**.
4. Choose per layer whether phones can edit it, view it, or not see it at all.
5. Build offline tiles for any raster layers you want on the phone.

## Development

- **Tests** use QGIS's own Python. For example, on Windows:
  `"C:\Program Files\QGIS 3.44.13\bin\python-qgis-ltr.bat" tests\test_server.py`
- **Packaging:** `python package.py` builds `dist/mappa_mundi_sync-<version>.zip`, ready to upload to plugins.qgis.org.

## Licence

GNU General Public License v2 or later. See [LICENSE](LICENSE).
