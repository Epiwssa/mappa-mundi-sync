"""Build the zip to upload to plugins.qgis.org: dist/mappa_mundi_sync-<version>.zip

Run with any Python 3:  python package.py
"""
import configparser
import pathlib
import zipfile

HERE = pathlib.Path(__file__).resolve().parent
NAME = "mappa_mundi_sync"
SKIP_DIRS = {"__pycache__", ".git"}
SKIP_SUFFIXES = {".pyc", ".pyo"}

meta = configparser.ConfigParser()
meta.read(HERE / NAME / "metadata.txt", encoding="utf-8")
version = meta["general"]["version"]
for key in ("name", "qgisMinimumVersion", "description", "about", "version", "author", "email", "repository"):
    if not meta["general"].get(key, "").strip():
        raise SystemExit(f"metadata.txt is missing {key}")
if not (HERE / NAME / "LICENSE").exists():
    raise SystemExit("LICENSE is missing from the plugin folder")

out = HERE / "dist" / f"{NAME}-{version}.zip"
out.parent.mkdir(exist_ok=True)
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
    for f in sorted((HERE / NAME).rglob("*")):
        rel = f.relative_to(HERE)
        if f.is_dir() or SKIP_DIRS & set(rel.parts) or f.suffix in SKIP_SUFFIXES:
            continue
        z.write(f, rel.as_posix())
        print(" ", rel.as_posix())
print(f"Built {out.relative_to(HERE)} ({out.stat().st_size // 1024} KB)")
