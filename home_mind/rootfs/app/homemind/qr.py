"""A small QR code encoder (byte mode, versions 1-10, error correction M), so labels need no extra package (#63).
Follows the structure of Project Nayuki's reference QR generator (MIT). Returns a square list of rows of booleans;
svg() draws it.
"""

ECC_PER_BLOCK = {"L": [-1, 7, 10, 15, 20, 26, 18, 20, 24, 30, 18], "M": [-1, 10, 16, 26, 18, 24, 16, 18, 22, 22, 26]}
NUM_BLOCKS = {"L": [-1, 1, 1, 1, 1, 1, 2, 2, 2, 2, 4], "M": [-1, 1, 1, 1, 2, 2, 4, 4, 4, 5, 5]}
FORMAT_BITS = {"L": 1, "M": 0}
MAX_VERSION = 10


def _raw_modules(ver):
    r = (16 * ver + 128) * ver + 64
    if ver >= 2:
        n = ver // 7 + 2
        r -= (25 * n - 10) * n - 55
        if ver >= 7:
            r -= 36
    return r


def _data_codewords(ver, ecl):
    return _raw_modules(ver) // 8 - ECC_PER_BLOCK[ecl][ver] * NUM_BLOCKS[ecl][ver]


def _align_positions(ver):
    if ver == 1:
        return []
    n = ver // 7 + 2
    size = ver * 4 + 17
    step = (ver * 4 + n * 2 + 1) // (n * 2 - 2) * 2
    out = [6]
    for i in range(n - 1):
        out.insert(1, size - 7 - i * step)
    return out


def _gf_mul(x, y):
    z = 0
    for i in reversed(range(8)):
        z = (z << 1) ^ ((z >> 7) * 0x11D)
        z ^= ((y >> i) & 1) * x
    return z


def _rs_divisor(degree):
    r = [0] * (degree - 1) + [1]
    root = 1
    for _ in range(degree):
        for j in range(degree):
            r[j] = _gf_mul(r[j], root)
            if j + 1 < degree:
                r[j] ^= r[j + 1]
        root = _gf_mul(root, 0x02)
    return r


def _rs_remainder(data, div):
    r = [0] * len(div)
    for b in data:
        f = b ^ r.pop(0)
        r.append(0)
        for i, c in enumerate(div):
            r[i] ^= _gf_mul(c, f)
    return r


def _codewords(data, ver, ecl):
    nb, ecc = NUM_BLOCKS[ecl][ver], ECC_PER_BLOCK[ecl][ver]
    raw = _raw_modules(ver) // 8
    short_n = nb - raw % nb
    short_len = raw // nb
    div = _rs_divisor(ecc)
    blocks, k = [], 0
    for i in range(nb):
        dat = data[k:k + short_len - ecc + (0 if i < short_n else 1)]
        k += len(dat)
        e = _rs_remainder(dat, div)
        if i < short_n:
            dat = dat + [0]
        blocks.append(dat + e)
    out = []
    for i in range(len(blocks[0])):
        for j, b in enumerate(blocks):
            if i != short_len - ecc or j >= short_n:
                out.append(b[i])
    return out


def encode(text, ecl="M"):
    data = text.encode("utf-8")
    for ver in range(1, MAX_VERSION + 1):
        cap = _data_codewords(ver, ecl) * 8
        need = 4 + (8 if ver < 10 else 16) + 8 * len(data)
        if need <= cap:
            break
    else:
        raise ValueError(f"too long for a label ({len(data)} bytes)")
    bits = []

    def put(v, n):
        bits.extend((v >> i) & 1 for i in reversed(range(n)))
    put(0x4, 4)
    put(len(data), 8 if ver < 10 else 16)
    for b in data:
        put(b, 8)
    put(0, min(4, cap - len(bits)))
    put(0, -len(bits) % 8)
    pad = 0xEC
    while len(bits) < cap:
        put(pad, 8)
        pad ^= 0xEC ^ 0x11
    words = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]
    return _matrix(_codewords(words, ver, ecl), ver, ecl)


def _matrix(cw, ver, ecl):
    size = ver * 4 + 17
    m = [[False] * size for _ in range(size)]
    fn = [[False] * size for _ in range(size)]

    def setf(x, y, dark):
        m[y][x] = dark
        fn[y][x] = True
    for i in range(size):
        setf(6, i, i % 2 == 0)
        setf(i, 6, i % 2 == 0)
    for cx, cy in ((3, 3), (size - 4, 3), (3, size - 4)):
        for dy in range(-4, 5):
            for dx in range(-4, 5):
                x, y = cx + dx, cy + dy
                if 0 <= x < size and 0 <= y < size:
                    setf(x, y, max(abs(dx), abs(dy)) not in (2, 4))
    al = _align_positions(ver)
    for i, a in enumerate(al):
        for j, b in enumerate(al):
            if (i == 0 and j == 0) or (i == 0 and j == len(al) - 1) or (i == len(al) - 1 and j == 0):
                continue
            for dy in range(-2, 3):
                for dx in range(-2, 3):
                    setf(a + dx, b + dy, max(abs(dx), abs(dy)) != 1)

    def draw_format(mask):
        d = FORMAT_BITS[ecl] << 3 | mask
        rem = d
        for _ in range(10):
            rem = (rem << 1) ^ ((rem >> 9) * 0x537)
        b = (d << 10 | rem) ^ 0x5412
        bit = lambda i: (b >> i) & 1 != 0
        for i in range(6):
            setf(8, i, bit(i))
        setf(8, 7, bit(6))
        setf(8, 8, bit(7))
        setf(7, 8, bit(8))
        for i in range(9, 15):
            setf(14 - i, 8, bit(i))
        for i in range(8):
            setf(size - 1 - i, 8, bit(i))
        for i in range(8, 15):
            setf(8, size - 15 + i, bit(i))
        setf(8, size - 8, True)
    draw_format(0)
    if ver >= 7:
        rem = ver
        for _ in range(12):
            rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
        b = ver << 12 | rem
        for i in range(18):
            d = (b >> i) & 1 != 0
            a, c = size - 11 + i % 3, i // 3
            setf(a, c, d)
            setf(c, a, d)
    # data, zigzag
    i = 0
    right = size - 1
    while right >= 1:
        if right == 6:
            right = 5
        for vert in range(size):
            for j in range(2):
                x = right - j
                up = ((right + 1) & 2) == 0
                y = size - 1 - vert if up else vert
                if not fn[y][x] and i < len(cw) * 8:
                    m[y][x] = (cw[i >> 3] >> (7 - (i & 7))) & 1 != 0
                    i += 1
        right -= 2
    masks = [lambda x, y: (x + y) % 2 == 0, lambda x, y: y % 2 == 0, lambda x, y: x % 3 == 0,
             lambda x, y: (x + y) % 3 == 0, lambda x, y: (x // 3 + y // 2) % 2 == 0,
             lambda x, y: x * y % 2 + x * y % 3 == 0, lambda x, y: (x * y % 2 + x * y % 3) % 2 == 0,
             lambda x, y: ((x + y) % 2 + x * y % 3) % 2 == 0]

    def apply(k):
        f = masks[k]
        for y in range(size):
            for x in range(size):
                if not fn[y][x] and f(x, y):
                    m[y][x] = not m[y][x]
    best, best_k = None, 0
    for k in range(8):
        apply(k)
        draw_format(k)
        p = _penalty(m)
        if best is None or p < best:
            best, best_k = p, k
        apply(k)
    apply(best_k)
    draw_format(best_k)
    return m


def _penalty(m):
    n = len(m)
    p = 0
    for rows in (m, [list(c) for c in zip(*m)]):
        for row in rows:
            run, prev = 0, None
            for v in row:
                if v == prev:
                    run += 1
                else:
                    if run >= 5:
                        p += run - 2
                    run, prev = 1, v
            if run >= 5:
                p += run - 2
            s = "".join("1" if v else "0" for v in row)
            p += 40 * (s.count("1011101") and (s.count("10111010000") + s.count("00001011101")))
    for y in range(n - 1):
        for x in range(n - 1):
            if m[y][x] == m[y][x + 1] == m[y + 1][x] == m[y + 1][x + 1]:
                p += 3
    dark = sum(v for r in m for v in r)
    p += (abs(dark * 20 - n * n * 10) // (n * n)) * 10
    return p


def svg(text, scale=6, border=4, label=None):
    m = encode(text)
    n = len(m)
    w = (n + 2 * border) * scale
    h = w + (scale * 7 if label else 0)
    path = "".join(f"M{(x + border) * scale},{(y + border) * scale}h{scale}v{scale}h-{scale}z"
                   for y in range(n) for x in range(n) if m[y][x])
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}">',
           f'<rect width="{w}" height="{h}" fill="#fff"/>', f'<path d="{path}" fill="#000"/>']
    if label:
        from html import escape
        out.append(f'<text x="{w / 2}" y="{w + scale * 3}" font-family="sans-serif" font-size="{scale * 3}" '
                   f'text-anchor="middle">{escape(label[:40])}</text>')
    out.append("</svg>")
    return "".join(out)
