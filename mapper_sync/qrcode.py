"""Minimal QR code encoder (byte mode, error correction level M, versions 1-10).

QGIS ships no QR library, and the pairing text is short, so a small encoder
avoids asking users to pip-install anything. Follows ISO/IEC 18004 and the
structure of Project Nayuki's reference implementation.
"""

# Level M tables, indexed by version (index 0 unused).
_ECC_PER_BLOCK = (None, 10, 16, 26, 18, 24, 16, 18, 22, 22, 26)
_NUM_BLOCKS = (None, 1, 1, 1, 2, 2, 4, 4, 4, 5, 5)
_FORMAT_BITS_M = 0
MAX_VERSION = 10

_MASKS = (
    lambda x, y: (x + y) % 2,
    lambda x, y: y % 2,
    lambda x, y: x % 3,
    lambda x, y: (x + y) % 3,
    lambda x, y: (x // 3 + y // 2) % 2,
    lambda x, y: x * y % 2 + x * y % 3,
    lambda x, y: (x * y % 2 + x * y % 3) % 2,
    lambda x, y: ((x + y) % 2 + x * y % 3) % 2,
)


def _bit(x, i):
    return (x >> i) & 1 != 0


def _raw_modules(ver):
    result = (16 * ver + 128) * ver + 64
    if ver >= 2:
        numalign = ver // 7 + 2
        result -= (25 * numalign - 10) * numalign - 55
        if ver >= 7:
            result -= 36
    return result


def _data_codewords(ver):
    return _raw_modules(ver) // 8 - _ECC_PER_BLOCK[ver] * _NUM_BLOCKS[ver]


def _gf_mul(x, y):
    z = 0
    for i in reversed(range(8)):
        z = (z << 1) ^ ((z >> 7) * 0x11D)
        z ^= ((y >> i) & 1) * x
    return z


def _rs_divisor(degree):
    result = [0] * (degree - 1) + [1]
    root = 1
    for _ in range(degree):
        for j in range(degree):
            result[j] = _gf_mul(result[j], root)
            if j + 1 < degree:
                result[j] ^= result[j + 1]
        root = _gf_mul(root, 0x02)
    return result


def _rs_remainder(data, divisor):
    result = [0] * len(divisor)
    for b in data:
        factor = b ^ result.pop(0)
        result.append(0)
        for i, coef in enumerate(divisor):
            result[i] ^= _gf_mul(coef, factor)
    return result


def _encode_data(payload, ver):
    bits = []

    def put(val, n):
        bits.extend((val >> i) & 1 for i in reversed(range(n)))

    put(0b0100, 4)  # byte mode
    put(len(payload), 8 if ver < 10 else 16)
    for b in payload:
        put(b, 8)
    capacity = _data_codewords(ver) * 8
    put(0, min(4, capacity - len(bits)))
    put(0, (-len(bits)) % 8)
    codewords = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]
    pad = 0xEC
    while len(codewords) < _data_codewords(ver):
        codewords.append(pad)
        pad ^= 0xEC ^ 0x11
    return codewords


def _add_ecc_and_interleave(data, ver):
    numblocks = _NUM_BLOCKS[ver]
    ecclen = _ECC_PER_BLOCK[ver]
    rawcodewords = _raw_modules(ver) // 8
    numshort = numblocks - rawcodewords % numblocks
    shortlen = rawcodewords // numblocks
    divisor = _rs_divisor(ecclen)
    blocks = []
    k = 0
    for i in range(numblocks):
        dat = data[k:k + shortlen - ecclen + (0 if i < numshort else 1)]
        k += len(dat)
        ecc = _rs_remainder(dat, divisor)
        if i < numshort:
            dat = dat + [0]
        blocks.append(dat + ecc)
    result = []
    for i in range(len(blocks[0])):
        for j, blk in enumerate(blocks):
            if i != shortlen - ecclen or j >= numshort:
                result.append(blk[i])
    return result


class _Matrix:
    def __init__(self, ver):
        self.ver = ver
        self.size = ver * 4 + 17
        self.mod = [[False] * self.size for _ in range(self.size)]
        self.func = [[False] * self.size for _ in range(self.size)]

    def set_func(self, x, y, dark):
        self.mod[y][x] = dark
        self.func[y][x] = True

    def alignment_positions(self):
        if self.ver == 1:
            return []
        numalign = self.ver // 7 + 2
        step = (self.ver * 8 + numalign * 3 + 5) // (numalign * 4 - 4) * 2
        result = [self.size - 7 - i * step for i in range(numalign - 1)] + [6]
        return list(reversed(result))

    def draw_function_patterns(self):
        n = self.size
        for i in range(n):
            self.set_func(6, i, i % 2 == 0)
            self.set_func(i, 6, i % 2 == 0)
        for cx, cy in ((3, 3), (n - 4, 3), (3, n - 4)):
            for dy in range(-4, 5):
                for dx in range(-4, 5):
                    x, y = cx + dx, cy + dy
                    if 0 <= x < n and 0 <= y < n:
                        self.set_func(x, y, max(abs(dx), abs(dy)) not in (2, 4))
        pos = self.alignment_positions()
        last = len(pos) - 1
        for i, ax in enumerate(pos):
            for j, ay in enumerate(pos):
                if (i, j) in ((0, 0), (0, last), (last, 0)):
                    continue
                for dy in range(-2, 3):
                    for dx in range(-2, 3):
                        self.set_func(ax + dx, ay + dy, max(abs(dx), abs(dy)) != 1)
        self.draw_format(0)
        if self.ver >= 7:
            rem = self.ver
            for _ in range(12):
                rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
            bits = self.ver << 12 | rem
            for i in range(18):
                a, b = n - 11 + i % 3, i // 3
                self.set_func(a, b, _bit(bits, i))
                self.set_func(b, a, _bit(bits, i))

    def draw_format(self, mask):
        n = self.size
        data = _FORMAT_BITS_M << 3 | mask
        rem = data
        for _ in range(10):
            rem = (rem << 1) ^ ((rem >> 9) * 0x537)
        bits = (data << 10 | rem) ^ 0x5412
        for i in range(6):
            self.set_func(8, i, _bit(bits, i))
        self.set_func(8, 7, _bit(bits, 6))
        self.set_func(8, 8, _bit(bits, 7))
        self.set_func(7, 8, _bit(bits, 8))
        for i in range(9, 15):
            self.set_func(14 - i, 8, _bit(bits, i))
        for i in range(8):
            self.set_func(n - 1 - i, 8, _bit(bits, i))
        for i in range(8, 15):
            self.set_func(8, n - 15 + i, _bit(bits, i))
        self.set_func(8, n - 8, True)

    def draw_codewords(self, data):
        n = self.size
        i = 0
        right = n - 1
        while right >= 1:
            if right == 6:
                right = 5
            for vert in range(n):
                for j in range(2):
                    x = right - j
                    upward = (right + 1) & 2 == 0
                    y = n - 1 - vert if upward else vert
                    if not self.func[y][x] and i < len(data) * 8:
                        self.mod[y][x] = _bit(data[i >> 3], 7 - (i & 7))
                        i += 1
            right -= 2

    def apply_mask(self, mask):
        fn = _MASKS[mask]
        for y in range(self.size):
            row, frow = self.mod[y], self.func[y]
            for x in range(self.size):
                if not frow[x] and fn(x, y) == 0:
                    row[x] = not row[x]

    def penalty(self):
        """Rules 1, 2 and 4 of the spec's mask scoring (rule 3 is rarely decisive)."""
        n, m = self.size, self.mod
        score = 0
        for lines in (m, [list(col) for col in zip(*m)]):
            for line in lines:
                run, prev = 0, None
                for v in line:
                    if v == prev:
                        run += 1
                    else:
                        if run >= 5:
                            score += run - 2
                        run, prev = 1, v
                if run >= 5:
                    score += run - 2
        for y in range(n - 1):
            for x in range(n - 1):
                c = m[y][x]
                if c == m[y][x + 1] == m[y + 1][x] == m[y + 1][x + 1]:
                    score += 3
        dark = sum(sum(row) for row in m)
        total = n * n
        score += abs(dark * 20 - total * 10) // total * 10  # 10 points per 5% away from 50% dark
        return score


def encode(text):
    """Returns the QR code as a square list of rows of booleans (True = dark)."""
    payload = text.encode("utf-8")
    ver = next((v for v in range(1, MAX_VERSION + 1)
                if 4 + (8 if v < 10 else 16) + len(payload) * 8 <= _data_codewords(v) * 8), None)
    if ver is None:
        raise ValueError("Text too long for QR code")
    codewords = _add_ecc_and_interleave(_encode_data(payload, ver), ver)

    best, best_score = None, None
    for mask in range(8):
        qr = _Matrix(ver)
        qr.draw_function_patterns()
        qr.draw_codewords(codewords)
        qr.apply_mask(mask)
        qr.draw_format(mask)
        score = qr.penalty()
        if best_score is None or score < best_score:
            best, best_score = qr, score
    return best.mod
