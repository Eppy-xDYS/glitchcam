"""Glitch pipeline: decode packed RAW10 Bayer frames "wrong" on purpose.

Everything works on the raw byte buffer exactly as it comes off the CSI-2
receiver (SBGGR10_CSI2P: 4 pixels in 5 bytes, 4 MSB bytes then 1 LSB byte).
A frame is rendered by gathering 2x2 Bayer quads at a grid of quad positions,
so the viewfinder (subsampled grid) and a saved still (every quad) come out of
the very same mis-decode, just at different resolutions.

Every glitch is a pure function of (raw buffer, mode, level, seed), so the
same seed gives the same tear/smear/colour damage again.
"""

import io
import numpy as np
from PIL import Image, ImageFile

ImageFile.LOAD_TRUNCATED_IMAGES = True

MODES = ["CLEAN", "TEAR", "DEPTH", "SWAP", "BEND", "JPEG", "MOSH", "SENSOR"]
MODE_INFO = {
    "CLEAN": "raw decode, no damage",
    "TEAR": "wrong line stride per band",
    "DEPTH": "10-bit read at wrong bit depth",
    "SWAP": "wrong bayer order + channel slip",
    "BEND": "circuit-bent ADC: hold 1-9 to bridge",
    "JPEG": "bytes flipped inside the JPEG",
    "MOSH": "motion with no keyframes",
    "SENSOR": "bent IMX219 registers (experimental)",
}


class RawFrame:
    """One packed RAW10 frame plus the colour info needed to develop it."""

    def __init__(self, arr, width, height, stride, gains=(1.6, 1.6), order="BGGR"):
        self.order = order
        self.flat = np.ascontiguousarray(arr).reshape(-1)
        self.w, self.h, self.stride = width, height, stride
        self.qw, self.qh = width // 2, height // 2
        self.gains = gains


# --------------------------------------------------------------------------
# Tone curves
# --------------------------------------------------------------------------
_LUT_CACHE = {}


def tone_luts(gains, bits=8):
    """Per-channel (R, G, B) LUTs: black level, white balance, gamma."""
    key = (round(gains[0], 2), round(gains[1], 2), bits)
    lut = _LUT_CACHE.get(key)
    if lut is None:
        n = 1 << bits
        black = 64 >> (10 - bits)
        x = np.clip((np.arange(n, dtype=np.float32) - black) / (n - 1 - black), 0, None)
        out = []
        for g in (gains[0], 1.0, gains[1]):
            y = np.clip(x * g * 1.15, 0, 1) ** (1 / 2.2)
            # gentle S-curve so the raw looks less flat
            y = y * y * (3 - 2 * y) * 0.35 + y * 0.65
            out.append(y)
        lut = np.stack(out).astype(np.float32)
        if len(_LUT_CACHE) > 64:
            _LUT_CACHE.clear()
        _LUT_CACHE[key] = lut
    return lut


def lut8(gains):
    return (tone_luts(gains, 8) * 255 + 0.5).astype(np.uint8)


# --------------------------------------------------------------------------
# Byte addressing helpers
# --------------------------------------------------------------------------
def msb_byte(px):
    return (px // 4) * 5 + (px % 4)


def lsb_byte(px):
    return (px // 4) * 5 + 4


def gather(flat, rowoff, coloff):
    idx = rowoff[:, None] + coloff[None, :]
    n = flat.shape[0]
    if idx.min() < 0 or idx.max() >= n:
        idx %= n
    return flat[idx]


def grid(raw, out_w, out_h, aspect_crop=True):
    """Quad sample positions for an out_w x out_h image (centre crop)."""
    qx = np.linspace(0, raw.qw - 1, out_w).astype(np.int64)
    if aspect_crop:
        span = raw.qw * out_h / out_w
        y0 = max(0.0, (raw.qh - span) / 2)
        qy = np.linspace(y0, min(raw.qh - 1, y0 + span - 1), out_h).astype(np.int64)
    else:
        qy = np.linspace(0, raw.qh - 1, out_h).astype(np.int64)
    return qx, qy


def full_grid(raw):
    return np.arange(raw.qw, dtype=np.int64), np.arange(raw.qh, dtype=np.int64)


# --------------------------------------------------------------------------
# Decoders
# --------------------------------------------------------------------------
def assign(order, a, b, c, d):
    """Map quad planes a b / c d to R, G1, G2, B for a Bayer order."""
    if order == "BGGR":
        return d, b, c, a
    if order == "RGGB":
        return a, b, c, d
    if order == "GRBG":
        return b, a, d, c
    return c, a, d, b  # GBRG


QUAD = ((0, 0), (0, 1), (1, 0), (1, 1))


def tear_rowfn(raw, level, seed):
    """Wrong line stride per band: rows start at the wrong byte."""
    rng = np.random.default_rng(seed * 7919 + 1)
    nb = 2 + 2 * level
    cuts = np.sort(rng.integers(0, raw.h, nb - 1))
    starts = np.concatenate(([0], cuts))
    active = rng.random(nb) < 0.15 + 0.08 * level
    active[rng.integers(0, nb)] = True  # always at least one tear
    # low levels: bands just slip sideways (and out of 5-byte alignment);
    # mid levels: a wrong stride inside some bands -> shear;
    # high levels: big stride errors everywhere -> smeared diagonals.
    if level <= 3:
        errs = np.array([0])
    elif level <= 6:
        errs = np.array([0, 0, 1, 2, 5])
    else:
        errs = np.array([1, 2, 3, 5, 10, 20, 40, 80])[: level - 1]
    err = rng.choice([-1, 1], nb) * rng.choice(errs, nb)
    shift = rng.integers(-level * 80, level * 80 + 1, nb)
    err = np.where(active, err, 0)
    shift = np.where(active, shift, 0)
    drift = int(rng.choice([-1, 1]) * (level - 7)) if level > 7 else 0
    stride = raw.stride

    def rowfn(r):
        bi = np.searchsorted(cuts, r, side="right")
        return r * (stride + drift) + shift[bi] + (r - starts[bi]) * err[bi]

    return rowfn


def swap_params(raw, level, seed):
    """Wrong Bayer order, R/B planes slipped, optionally byte-misaligned."""
    rng = np.random.default_rng(seed * 1299709 + 3)
    wrong = [o for o in ("BGGR", "RGGB", "GRBG", "GBRG") if o != raw.order]
    order = wrong[int(rng.integers(0, 3))]
    k = level * 3

    def slip():
        return tuple(int(v) for v in rng.integers(-1, 2, 2) * k)

    offs = [slip(), (0, 0), (0, 0), slip()]
    if offs[0] == (0, 0):
        offs[0] = (k, 0)
    shift = [0, 0, 0, 0]
    if level >= 6:  # read across the 5-byte group boundary: LSB byte leaks in
        shift[int(rng.integers(0, 4))] = 1
    return order, offs, shift


def depth_luts(raw, level, seed):
    """10-bit tone LUTs with the wrong bit depth: values overflow and wrap."""
    rng = np.random.default_rng(seed * 104729 + 2)
    base = [1.5, 2, 2.5, 3, 4, 5, 6, 8, 12][level - 1]
    mult = np.full(3, float(base))
    if level >= 4:
        mult *= rng.uniform(0.7, 1.45, 3)
    tl = tone_luts(raw.gains, 10)
    return (((tl * 255 * mult[:, None]).astype(np.int64)) & 255).astype(np.uint8)


# --------------------------------------------------------------------------
# BEND: circuit-bending emulation. On a CCD camera you bridge the AFE's ADC
# data pins (B0..B11), clamp (CLPOB/CLPDM), sample-hold (SHP/SHD), ADC clock
# and the CCD vertical clocks (XV/XSG). The IMX219 has all of that inside the
# chip, so each "wire" is emulated on the 10-bit samples at the same stage.
# --------------------------------------------------------------------------
def bend_patches(seed):
    """Nine wires in a seed-dependent order. Level n connects wires 1..n."""
    rng = np.random.default_rng(seed * 7727 + 5)
    hi = [9, 8, 7, 6]
    a, b = rng.choice(hi, 2, replace=False)
    c, d = rng.choice([8, 7, 6, 5], 2, replace=False)
    sb = int(rng.choice([5, 6]))
    pool = [
        ("B%d~B%d" % (a, b), ("bridge", int(a), int(b))),
        ("CLPOB", ("clamp", int(rng.integers(0, 1 << 30)), int(rng.integers(50, 160)))),
        ("B%d HI" % sb, ("stuck1", sb)),
        ("SHP", ("shp", int(rng.integers(2, 9)))),
        ("ADCCK", ("adcck", int(rng.integers(3, 12)), int(rng.integers(0, 1 << 30)))),
        ("B%d<>B%d" % (c, d), ("swap", int(c), int(d))),
        ("XV2", ("xv", float(rng.uniform(0.93, 0.985)))),
        ("XSG", ("xsg", int(rng.integers(4, 30)), int(rng.integers(0, 1 << 30)))),
        ("B9 LO", ("stuck0", 9)),
    ]
    first, rest = pool[:1], pool[1:]
    order = rng.permutation(len(rest))
    return first + [rest[i] for i in order]


def _remap(axis_vals, target):
    return np.clip(np.searchsorted(axis_vals, target), 0, len(axis_vals) - 1)


def _bands(n_rows_raw, seed, count, maxh):
    rng = np.random.default_rng(seed)
    s = rng.integers(0, n_rows_raw, count)
    return s, s + rng.integers(4, maxh, count)


def apply_bend(planes, ops, qx, qy, raw):
    """planes: 4 x (h, w) uint16 10-bit quad planes -> bent planes."""
    planes = [p.astype(np.int32) for p in planes]
    for op in ops:
        kind = op[0]
        if kind in ("bridge", "stuck1", "stuck0", "swap"):
            for i, v in enumerate(planes):
                if kind == "bridge":  # two data lines shorted: wired-OR
                    ma, mb = 1 << op[1], 1 << op[2]
                    on = (v & (ma | mb)) != 0
                    v = np.where(on, v | ma | mb, v)
                elif kind == "stuck1":
                    v = v | (1 << op[1])
                elif kind == "stuck0":
                    v = v & ~(1 << op[1])
                else:  # lines crossed
                    ba = (v >> op[1]) & 1
                    bb = (v >> op[2]) & 1
                    v = (v & ~((1 << op[1]) | (1 << op[2]))) | (ba << op[2]) | (bb << op[1])
                planes[i] = v
        elif kind == "clamp":  # black clamp pulses on the wrong lines: banding
            brng = np.random.default_rng(op[1])
            edges = np.sort(brng.integers(0, raw.qh, 14))
            lvls = brng.integers(-1, 2, 15) * op[2] * brng.uniform(0.5, 1.5, 15)
            off = lvls[np.searchsorted(edges, qy)].astype(np.int32)[:, None]
            planes = [(v + off) & 1023 for v in planes]
        elif kind == "shp":  # sample-hold timing: neighbour leaks in, chroma ghost
            idx = _remap(qx, qx - op[1])
            idx2 = _remap(qx, qx + op[1] * 2)
            planes[0] = planes[0][:, idx]
            planes[3] = np.maximum(planes[3], planes[3][:, idx2])
            planes[1] = (planes[1] + planes[1][:, idx]) >> 1
        elif kind == "adcck":  # ADC clock dropouts: pixels held across columns
            n = op[1]
            starts, ends = _bands(raw.qh, op[2], 6, 120)
            rows = np.zeros(len(qy), bool)
            for s, e in zip(starts, ends):
                rows |= (qy >= s) & (qy < e)
            idx = _remap(qx, (qx // n) * n)
            for i in range(4):
                planes[i][rows] = planes[i][rows][:, idx]
        elif kind == "xsg":  # transfer gate glitch: rows repeat in bands
            n = op[1]
            starts, ends = _bands(raw.qh, op[2], 4, 200)
            ridx = _remap(qy, (qy // n) * n)
            rows = np.zeros(len(qy), bool)
            for s, e in zip(starts, ends):
                rows |= (qy >= s) & (qy < e)
            for i in range(4):
                planes[i][rows] = planes[i][ridx[rows]]
        elif kind == "xv":  # vertical clock off: charge smears down the frame
            step = max(1, int(np.median(np.diff(qy))) if len(qy) > 1 else 1)
            k = op[1] ** step
            for i in range(4):
                v = planes[i]
                for r in range(1, v.shape[0]):
                    np.maximum(v[r], (v[r - 1] * k).astype(np.int32), out=v[r])
    return [np.clip(p, 0, 1023).astype(np.uint16) for p in planes]


def bend_latched(level, toggled=(), n=9):
    """Wires that stay connected: 1..level, with each toggled wire flipped."""
    return set(range(min(level, n))) ^ {t for t in toggled if 0 <= t < n}


def bend_ops(level, held, seed, toggled=()):
    pats = bend_patches(seed)
    on = bend_latched(level, toggled, len(pats)) | {h for h in held if 0 <= h < len(pats)}
    return [pats[i] for i in sorted(on)]


def decode(raw, qx, qy, layers, seed):
    """One pass over the raw bytes with any mix of TEAR / DEPTH / SWAP.

    layers: {mode_name: level}. Unknown names are ignored here.
    BEND may also be (level, held_wires) or (level, held_wires, toggled_wires).
    """
    rowfn = tear_rowfn(raw, layers["TEAR"], seed) if "TEAR" in layers else (lambda r: r * raw.stride)
    if "SWAP" in layers:
        order, offs, shift = swap_params(raw, layers["SWAP"], seed)
    else:
        order, offs, shift = raw.order, [(0, 0)] * 4, [0, 0, 0, 0]
    depth = layers.get("DEPTH")
    bend = layers.get("BEND")
    if isinstance(bend, int):
        bend = (bend, ())
    ten = bool(depth or bend)
    planes = []
    for i, (dy, dx) in enumerate(QUAD):
        ox, oy = offs[i]
        r = 2 * np.clip(qy + oy, 0, raw.qh - 1) + dy
        c = 2 * np.clip(qx + ox, 0, raw.qw - 1) + dx
        ro = rowfn(r)
        m = gather(raw.flat, ro, msb_byte(c) + shift[i])
        if ten:
            lsb = gather(raw.flat, ro, lsb_byte(c) + shift[i])
            sh = (2 * (c % 4)).astype(np.uint8)
            m = (m.astype(np.uint16) << 2) | ((lsb >> sh[None, :]) & 3)
        planes.append(m)
    if bend:
        toggled = bend[2] if len(bend) > 2 else ()
        planes = apply_bend(planes, [op for _, op in bend_ops(bend[0], bend[1], seed, toggled)],
                            qx, qy, raw)
    R, G1, G2, B = assign(order, *planes)
    out = np.empty(R.shape + (3,), np.uint8)
    if ten and not depth:
        luts = (tone_luts(raw.gains, 10) * 255 + 0.5).astype(np.uint8)
        G = (G1 + G2) >> 1
    elif depth:
        luts = depth_luts(raw, depth, seed)
        if depth >= 7:  # bits land in the wrong place: rotate one channel's word
            rot = depth - 6
            B = ((B << rot) | (B >> (10 - rot))) & 1023
        G = (G1 + G2) >> 1
    else:
        luts = lut8(raw.gains)
        G = ((G1.astype(np.uint16) + G2) >> 1).astype(np.uint8)
    out[..., 0] = luts[0][R]
    out[..., 1] = luts[1][G]
    out[..., 2] = luts[2][B]
    return out


def render_clean(raw, qx, qy):
    return decode(raw, qx, qy, {}, 0)


def render_tear(raw, qx, qy, level, seed):
    return decode(raw, qx, qy, {"TEAR": level}, seed)


def render_depth(raw, qx, qy, level, seed):
    return decode(raw, qx, qy, {"DEPTH": level}, seed)


def render_swap(raw, qx, qy, level, seed):
    return decode(raw, qx, qy, {"SWAP": level}, seed)


def corrupt_jpeg(rgb, level, seed, quality=80):
    """Encode, flip bytes inside the entropy-coded data, decode.

    Returns (decoded_rgb, jpeg_bytes) or (None, None) if undecodable.
    """
    buf = io.BytesIO()
    Image.fromarray(rgb).save(buf, "JPEG", quality=quality)
    data = bytearray(buf.getvalue())
    sos = data.find(b"\xff\xda")
    if sos < 0:
        return None, None
    hdr = (data[sos + 2] << 8) | data[sos + 3]
    start, end = sos + 2 + hdr, len(data) - 2
    rng = np.random.default_rng(seed * 15485863 + 4)
    n = [1, 2, 3, 5, 8, 12, 18, 26, 40][level - 1]
    pos = (rng.random(n) * 0.85 * (end - start)).astype(int) + start + 8
    vals = rng.integers(0, 255, n)  # never 0xFF: that would forge a marker
    for p, v in zip(pos, vals):
        if p < end and data[p - 1] != 0xFF:
            data[p] = int(v)
    try:
        im = Image.open(io.BytesIO(bytes(data)))
        im.load()
        out = np.asarray(im.convert("RGB"))
        if out.shape != rgb.shape:
            return None, None
        return out, bytes(data)
    except Exception:
        return None, None


class Mosher:
    """Live datamosh: estimate block motion, push the *old* picture along it.

    This is what a decoder does when the keyframe is gone: motion vectors keep
    arriving, but they move stale pixels. Residuals leak in at (10-level)/10.
    """

    B = 10

    def __init__(self):
        self.prev_in = None
        self.out = None
        self.frames = 0

    def reset(self):
        self.prev_in = None

    def step(self, rgb, level, seed):
        h, w, _ = rgb.shape
        B = self.B
        gh, gw = h // B, w // B
        cur = rgb[: gh * B, : gw * B]
        if self.prev_in is None or self.prev_in.shape != cur.shape or self.out is None:
            self.prev_in = cur.copy()
            self.out = cur.copy()
            return self.out
        lum_c = cur[..., 1].astype(np.int16)
        lum_p = self.prev_in[..., 1].astype(np.int16)
        s = 2 + level // 2
        cands = [(0, 0), (0, s), (0, -s), (s, 0), (-s, 0), (s, s), (-s, -s)]
        best = None
        bestv = None
        for i, (dy, dx) in enumerate(cands):
            shifted = np.roll(lum_p, (dy, dx), (0, 1))
            sad = np.abs(lum_c - shifted).reshape(gh, B, gw, B).sum((1, 3))
            if best is None:
                best, bestv = sad, np.zeros((gh, gw), np.int8)
            else:
                better = sad < best * 0.9
                best = np.where(better, sad, best)
                bestv = np.where(better, i, bestv)
        out = self.out.astype(np.int16)
        moved = out.copy()
        for i, (dy, dx) in enumerate(cands):
            if i == 0:
                continue
            m = np.repeat(np.repeat(bestv == i, B, 0), B, 1)
            # exaggerate: carry stale pixels further than they actually moved
            rolled = np.roll(out, (dy * 2, dx * 2), (0, 1))
            moved[m] = rolled[m]
        leak = max(0.0, (10 - level) / 14.0)
        moved = moved + (cur.astype(np.int16) - moved) * leak
        # occasional intra refresh blocks keep it from going to mud
        rng = np.random.default_rng(seed + self.frames)
        refresh = rng.random((gh, gw)) < (0.04 / level)
        rm = np.repeat(np.repeat(refresh, B, 0), B, 1)
        moved[rm] = cur[rm]
        self.out = np.clip(moved, 0, 255).astype(np.uint8)
        self.prev_in = cur.copy()
        self.frames += 1
        return self.out


RAW_LAYERS = ("TEAR", "DEPTH", "SWAP", "BEND")


def render(mode, raw, qx, qy, level, seed):
    return decode(raw, qx, qy, {mode: level} if mode in RAW_LAYERS else {}, seed)


def render_stack(raw, qx, qy, layers, seed, jpeg_quality=75, fallback=None):
    """Raw-level layers in one decode, then JPEG damage on top.

    MOSH is stateful and applied by the caller afterwards.
    Returns (rgb, jpeg_bytes_or_None).
    """
    img = decode(raw, qx, qy, {k: v for k, v in layers.items() if k in RAW_LAYERS}, seed)
    data = None
    if "JPEG" in layers:
        bent, data = corrupt_jpeg(img, layers["JPEG"], seed, quality=jpeg_quality)
        if bent is not None:
            img = bent
        elif fallback is not None and fallback.shape == img.shape:
            img = fallback
    return img, data
