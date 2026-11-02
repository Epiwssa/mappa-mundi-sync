# Mappa Mundi Sync - sync a QGIS project to the Mappa Mundi field app.
# Copyright (C) 2026 Epiwssa
#
# This program is free software; you can redistribute it and/or modify it under the terms of the
# GNU General Public License as published by the Free Software Foundation; either version 2 of the
# License, or (at your option) any later version. See the LICENSE file for details.


def classFactory(iface):
    from .plugin import MapperSyncPlugin
    return MapperSyncPlugin(iface)
