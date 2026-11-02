"""Encodes texts of many lengths (every QR version 1-10) and decodes them with jsQR.

Needs Node and `npm install` in mobile/. Runs on plain Python (no QGIS needed):
  python qgis-plugin/tests/test_qrcode.py
"""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("qr", os.path.join(HERE, "..", "mapper_sync", "qrcode.py"))
qr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(qr)

texts = [
    "mapper://connect?h=192.168.1.20&p=8765&k=123456&n=Survey",
    "mapper://connect?h=192.168.1.20,172.20.160.1,10.0.0.8&p=8765&k=004211&n=Bor%C3%A9holes%20Pit%2012",
    "x",
    "Ünïcödé ✓ text",
]
# Lengths that land on every version from 1 to 10 at level M (byte capacities 14..213).
texts += ["A" * n for n in (14, 15, 26, 27, 42, 43, 62, 63, 84, 85, 106, 107, 122, 123, 152, 153, 180, 181, 213)]

matrices = [qr.encode(t) for t in texts]
versions = sorted({(len(m) - 17) // 4 for m in matrices})
assert versions == list(range(1, 11)), versions

try:
    qr.encode("A" * 214)
    raise AssertionError("expected overflow")
except ValueError:
    pass

with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
    json.dump(matrices, f)
mobile = os.path.join(HERE, "..", "..", "mobile")
out = subprocess.run(
    ["node", os.path.join(mobile, "scripts", "decode-qr-matrix.js"), f.name],
    cwd=mobile, capture_output=True, text=True, encoding="utf-8", check=True,
)
decoded = json.loads(out.stdout)
bad = [(t, d) for t, d in zip(texts, decoded) if t != d]
assert not bad, bad
print(f"RESULT: PASS ({len(texts)} codes, versions {versions[0]}-{versions[-1]}, all decoded by jsQR)")
