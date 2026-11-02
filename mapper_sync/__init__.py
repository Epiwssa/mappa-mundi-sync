def classFactory(iface):
    from .plugin import MapperSyncPlugin
    return MapperSyncPlugin(iface)
