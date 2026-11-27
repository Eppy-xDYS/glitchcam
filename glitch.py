"""Glitch pipeline: decode packed RAW10 Bayer frames "wrong" on purpose.

Everything works on the raw byte buffer exactly as it comes off the CSI-2
receiver (SBGGR10_CSI2P: 4 pixels in 5 bytes, 4 MSB bytes then 1 LSB byte).
A frame is rendered by gathering 2x2 Bayer quads at a grid of quad positions,
so the viewfinder (subsampled grid) and a saved still (every quad) come out of
the very same mis-decode, just at different resolutions.

Every glitch is a pure function of (raw buffer, mode, level): the damage is
wired in, like a bent camera. The same mode and intensity always break the
picture the same way, and turning the intensity up only adds damage on top
of what was already there; it never moves it around.
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
    "BEND": "clean - press 1-0, Q-U for wires",
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


TEAR_BANDS = 20


def tear_rowfn(raw, level):
    """Wrong line stride per band: rows start at the wrong byte.

    The band edges and which band tears first are fixed; intensity only
    decides how many bands tear and how hard, so the same bands keep tearing
    as you turn it up.
    """
    rng = np.random.default_rng(7920)
    nb = TEAR_BANDS
    cuts = np.sort(rng.integers(0, raw.h, nb - 1))
    starts = np.concatenate(([0], cuts))
    priority = rng.permutation(nb)          # band priority[0] tears first
    sign_e = rng.choice([-1, 1], nb)
    pick_e = rng.random(nb)                 # where in the error table each band sits
    shift_u = rng.uniform(-1, 1, nb)        # fixed direction/size of each band's slip
    drift_sign = int(rng.choice([-1, 1]))
    n_active = max(1, int(round(nb * (0.15 + 0.08 * level))))
    active = np.zeros(nb, bool)
    active[priority[:n_active]] = True
    # low levels: bands just slip sideways (and out of 5-byte alignment);
    # mid levels: a wrong stride inside some bands -> shear;
    # high levels: big stride errors everywhere -> smeared diagonals.
    if level <= 3:
        errs = np.array([0])
    elif level <= 6:
        errs = np.array([0, 0, 1, 2, 5])
    else:
        errs = np.array([1, 2, 3, 5, 10, 20, 40, 80])[: level - 1]
    err = sign_e * errs[(pick_e * len(errs)).astype(int)]
    shift = np.round(shift_u * level * 80).astype(np.int64)
    err = np.where(active, err, 0)
    shift = np.where(active, shift, 0)
    drift = drift_sign * (level - 7) if level > 7 else 0
    stride = raw.stride

    def rowfn(r):
        bi = np.searchsorted(cuts, r, side="right")
        return r * (stride + drift) + shift[bi] + (r - starts[bi]) * err[bi]

    return rowfn


def swap_params(raw, level):
    """Wrong Bayer order, R/B planes slipped, optionally byte-misaligned."""
    rng = np.random.default_rng(1299712)
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


def depth_luts(raw, level):
    """10-bit tone LUTs with the wrong bit depth: values overflow and wrap."""
    rng = np.random.default_rng(104731)
    base = [1.5, 2, 2.5, 3, 4, 5, 6, 8, 12][level - 1]
    mult = np.full(3, float(base))
    if level >= 4:
        mult *= rng.uniform(0.7, 1.45, 3)
    tl = tone_luts(raw.gains, 10)
    return (((tl * 255 * mult[:, None]).astype(np.int64)) & 255).astype(np.uint8)


# --------------------------------------------------------------------------
# BEND: circuit-bending emulation. On a CCD camera you bridge the AFE's ADC
# data pins (B0..B11), ADC reference, sample-hold (SHP/SHD), ADC clock
# and the CCD vertical clocks (XV/XSG). The IMX219 has all of that inside the
# chip, so each "wire" is emulated on the 10-bit samples at the same stage.
# --------------------------------------------------------------------------
# The bend box. Each key is soldered to one point and always does the same
# thing. Keys 1-9 and 0 are the number row, Q W E R T U the row below.
# The values here are each wire's strength at 5; every wire has its own
# strength 1-9 that turns it down or up from there (see wire_strength).
BEND_WIRES = [
    ("B8~B7", ("bridge", 8, 7)),          # 1: two high data lines shorted
    ("B8~B4", ("bridge", 8, 4)),          # 2: high line shorted to a noisy low one: speckle
    ("VREF", ("vref", 5.0, 6.0, 7.0)),    # 3: ADC reference low: values wrap into rainbow rings
    ("SHP", ("shp", 5)),                  # 4: sample-and-hold timing off
    ("XV2", ("xv", 0.96)),                # 5: vertical clock off: smear
    ("B7<>B6", ("swap", 7, 6)),           # 6: two data lines crossed
    ("ADCCK", ("adcck", 6, 202)),         # 7: ADC clock dropouts
    ("XSG", ("xsg", 12, 303)),            # 8: transfer gate glitch
    ("B9 LO", ("stuck0", 9)),             # 9: top data line stuck off
    ("PCLK", ("pclk", 1)),                # 0: pixel clock slips one pixel
    # second row of the box, on the letter keys under the numbers
    ("HSMEAR", ("hsmear", 0.97)),         # Q: horizontal readout lag: rows smear right
    ("HALO", ("halo", 6, 2.5)),           # W: amp ringing: glowing outlines on edges
    ("DOUBLE", ("double", 2)),            # E: line read twice as fast: picture twice side by side
    ("HSYNC", ("hsync", 22)),             # R: horizontal sync wobbles: wavy, jittering rows
    ("SCAN", ("scan", 8, 3.0)),           # T: sync lost: picture breaks into wavy scan lines
    ("COLADC", ("coladc", 3, 4)),         # U: column ADCs bent: fixed stripes in bright areas
]
WIRE_LABELS = "1234567890QWERTU"  # the key for each wire, in order


def bend_patches():
    return BEND_WIRES


def _remap(axis_vals, target):
    return np.clip(np.searchsorted(axis_vals, target), 0, len(axis_vals) - 1)


def _bands(n_rows_raw, where, count, maxh):
    rng = np.random.default_rng(where)
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
        elif kind == "vref":  # ADC reference pulled low: the input overflows the range and
            # wraps back to dark; each colour wraps at its own point, so rings go rainbow
            ri, g1, g2, bi = assign(raw.order, 0, 1, 2, 3)
            gain = {ri: op[1], g1: op[2], g2: op[2], bi: op[3]}
            planes = [((np.maximum(v - 64, 0) * gain[i]).astype(np.int32) & 1023) + 64
                      for i, v in enumerate(planes)]
        elif kind == "pclk":  # pixel clock slips: every sample lands one pixel late,
            # so the Bayer phase shifts and green reads as red/blue (pink/green flip)
            nxt = _remap(qx, qx + op[1])
            a, b, c, d = planes
            planes = [b, a[:, nxt], d, c[:, nxt]]
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
        elif kind == "xv":  # vertical clock off: charge drips down from bright edges
            # each sensor column leaks by its own fixed amount, so the smear
            # hangs in thin streaks of different lengths instead of one smudge.
            # Red leaks furthest, blue less, green least, so drips run pink.
            step = max(1, int(np.median(np.diff(qy))) if len(qy) > 1 else 1)
            crng = np.random.default_rng(404)
            u = crng.random(raw.qw)
            col = np.where(u < 0.25, op[1] + (1 - op[1]) * (0.5 + 0.4 * crng.random(raw.qw)),
                           op[1] * 0.9)
            ri, g1, g2, bi = assign(raw.order, 0, 1, 2, 3)
            tint = {ri: 1.0, bi: 1.6, g1: 2.4, g2: 2.4}
            for i in range(4):
                k = col[qx] ** (step * tint[i])
                v = planes[i]
                for r in range(1, v.shape[0]):
                    np.maximum(v[r], (v[r - 1] * k).astype(np.int32), out=v[r])
        elif kind == "coladc":  # CMOS sensors digitise each column with its own ADC;
            # bend some of them and fixed stripes appear wherever the picture is bright
            band = (np.arange(raw.qw) // op[1]) % op[2]
            dark = (band == 0)[qx][None, :]
            lit = (band == 2)[qx][None, :]
            ri, g1, g2, bi = assign(raw.order, 0, 1, 2, 3)
            for i in range(4):
                v = planes[i]
                v = np.where(dark, v & ~((1 << 9) | (1 << 8)), v)
                if i in (g1, g2):
                    v = np.where(lit & (v >= 256), v | (1 << 9), v)
                planes[i] = v
        elif kind == "hsmear":  # readout lags along the line: bright runs smear right
            stepx = max(1, int(np.median(np.diff(qx))) if len(qx) > 1 else 1)
            rrng = np.random.default_rng(505)
            ru = rrng.random(raw.qh)
            row = np.where(ru < 0.3, op[1] + (1 - op[1]) * (0.6 + 0.35 * rrng.random(raw.qh)),
                           op[1] * 0.85)
            k = row[qy] ** stepx
            for i in range(4):
                v = planes[i]
                for c in range(1, v.shape[1]):
                    np.maximum(v[:, c], (v[:, c - 1] * k).astype(np.int32), out=v[:, c])
        elif kind == "halo":  # amplifier rings at every edge: glowing outlines
            r = op[1]
            near = [_remap(qx, qx + d) for d in (-r, -r // 2, r // 2, r)]
            for i in range(4):
                v = planes[i]
                blur = sum(v[:, j] for j in near) // 4
                planes[i] = v + ((v - blur) * op[2]).astype(np.int32)
        elif kind == "double":  # line clocked out twice per row: two copies side by side
            idx = _remap(qx, (qx * op[1]) % raw.qw)
            planes = [v[:, idx] for v in planes]
        elif kind == "hsync":  # each row starts late by a wobble that follows its brightness
            lum = sum(planes) // 4
            rowmean = lum.mean(axis=1)
            wob = op[1] * np.sin(qy * (2 * np.pi / 97.0)) + 0.5 * op[1] * np.sin(qy * (2 * np.pi / 23.0))
            shift = (wob + (rowmean - 300) * op[1] / 300.0).astype(np.int64)
            cols = np.clip(np.searchsorted(qx, qx[None, :] - shift[:, None]), 0, len(qx) - 1)
            planes = [np.take_along_axis(v, cols, 1) for v in planes]
        elif kind == "scan":  # sync lost: only every few lines survive, lifted by brightness
            stepy = max(1.0, float(np.median(np.diff(qy))) if len(qy) > 1 else 1.0)
            gap = max(3, int(round(op[1] * 2 / stepy)))
            lum = sum(planes) / 4.0
            h, w = lum.shape
            out = [np.zeros_like(v) for v in planes]
            rows = np.arange(0, h, gap)
            lift = (lum[rows] / 1023.0 * gap * op[2]).astype(np.int64)
            tgt = np.clip(rows[:, None] - lift, 0, h - 1)
            cc = np.broadcast_to(np.arange(w), tgt.shape)
            for i in range(4):
                src = planes[i][rows]
                np.maximum.at(out[i], (tgt, cc), src)
                np.maximum.at(out[i], (np.clip(tgt + 1, 0, h - 1), cc), src // 2)
            planes = out
    return [np.clip(p, 0, 1023).astype(np.uint16) for p in planes]


def wire_strength(op, level):
    """A wire's op at intensity 1-9. Level 5 is the op as listed in BEND_WIRES.

    Data-line wires move to lower (subtler) or higher (harsher) bits; the
    rest scale their size, gain or reach by a factor from 0.5x to 2x.
    """
    k = 2.0 ** ((level - 5) / 4.0)        # 0.5 at 1, 1.0 at 5, 2.0 at 9
    off = int(round((level - 5) / 2.0))   # -2 .. +2 bits
    kind = op[0]

    def bit(b):
        return max(1, min(9, b + off))

    if kind == "bridge":
        hi, lo = op[1], op[2]
        if hi - lo == 1:                  # two neighbouring high lines: move both
            hi = bit(hi)
            return (kind, hi, max(0, hi - 1))
        return (kind, bit(hi), lo)        # high line to the noisy low one: move the high end
    if kind == "swap":
        a = bit(op[1])
        return (kind, a, max(0, a - 1))
    if kind in ("stuck0", "stuck1"):
        return (kind, bit(op[1]))
    if kind == "vref":
        return (kind,) + tuple(1.0 + (g - 1.0) * k for g in op[1:])
    if kind in ("shp", "hsync"):
        return (kind, max(1, int(round(op[1] * k))))
    if kind in ("adcck", "xsg"):
        return (kind, max(2, int(round(op[1] * k))), op[2])
    if kind in ("xv", "hsmear"):          # leak per line: longer drips / smears when stronger
        return (kind, 1.0 - (1.0 - op[1]) / k)
    if kind == "halo":
        return (kind, max(2, int(round(op[1] * k))), op[2] * k)
    if kind == "scan":
        return (kind, op[1], op[2] * k)
    if kind == "coladc":
        return (kind, op[1], max(2, int(round(op[2] / k))))
    if kind == "pclk":                    # always an odd slip, so the colours always flip
        return (kind, {1: 1, 2: 1, 3: 1, 4: 1, 5: 1, 6: 3, 7: 3, 8: 5, 9: 5}[level])
    if kind == "double":
        return (kind, 2 if level <= 5 else 3 if level <= 7 else 4)
    return op


def bend_ops(levels, held, toggled=()):
    """Connected wires (pressed on, or held) as ops, each at its own strength.

    levels: one strength 1-9 per wire (a single int applies to every wire).
    """
    n = len(BEND_WIRES)
    if isinstance(levels, int):
        levels = (levels,) * n
    on = {t for t in toggled if 0 <= t < n} | {h for h in held if 0 <= h < n}
    return [(BEND_WIRES[i][0], wire_strength(BEND_WIRES[i][1], levels[i])) for i in sorted(on)]


def decode(raw, qx, qy, layers):
    """One pass over the raw bytes with any mix of TEAR / DEPTH / SWAP.

    layers: {mode_name: level}. Unknown names are ignored here.
    BEND is (per-wire strengths, held_wires, wires_pressed_on).
    """
    rowfn = tear_rowfn(raw, layers["TEAR"]) if "TEAR" in layers else (lambda r: r * raw.stride)
    if "SWAP" in layers:
        order, offs, shift = swap_params(raw, layers["SWAP"])
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
        planes = apply_bend(planes, [op for _, op in bend_ops(bend[0], bend[1], toggled)],
                            qx, qy, raw)
    R, G1, G2, B = assign(order, *planes)
    out = np.empty(R.shape + (3,), np.uint8)
    if ten and not depth:
        luts = (tone_luts(raw.gains, 10) * 255 + 0.5).astype(np.uint8)
        G = (G1 + G2) >> 1
    elif depth:
        luts = depth_luts(raw, depth)
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
    return decode(raw, qx, qy, {})


def render_tear(raw, qx, qy, level):
    return decode(raw, qx, qy, {"TEAR": level})


def render_depth(raw, qx, qy, level):
    return decode(raw, qx, qy, {"DEPTH": level})


def render_swap(raw, qx, qy, level):
    return decode(raw, qx, qy, {"SWAP": level})


def corrupt_jpeg(rgb, level, quality=80, salt=0):
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
    # fixed list of hits; level n uses the first n, so turning it up only adds
    rng = np.random.default_rng(15485867 + salt)
    n = [1, 2, 3, 5, 8, 12, 18, 26, 40][level - 1]
    pos = (rng.random(40)[:n] * 0.85 * (end - start)).astype(int) + start + 8
    vals = rng.integers(0, 255, 40)[:n]  # never 0xFF: that would forge a marker
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

    def step(self, rgb, level):
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
        rng = np.random.default_rng(self.frames)
        refresh = rng.random((gh, gw)) < (0.04 / level)
        rm = np.repeat(np.repeat(refresh, B, 0), B, 1)
        moved[rm] = cur[rm]
        self.out = np.clip(moved, 0, 255).astype(np.uint8)
        self.prev_in = cur.copy()
        self.frames += 1
        return self.out


RAW_LAYERS = ("TEAR", "DEPTH", "SWAP", "BEND")


def render(mode, raw, qx, qy, level):
    return decode(raw, qx, qy, {mode: level} if mode in RAW_LAYERS else {})


def render_stack(raw, qx, qy, layers, jpeg_quality=75, fallback=None):
    """Raw-level layers in one decode, then JPEG damage on top.

    MOSH is stateful and applied by the caller afterwards.
    Returns (rgb, jpeg_bytes_or_None).
    """
    img = decode(raw, qx, qy, {k: v for k, v in layers.items() if k in RAW_LAYERS})
    data = None
    if "JPEG" in layers:
        bent, data = corrupt_jpeg(img, layers["JPEG"], quality=jpeg_quality)
        if bent is not None:
            img = bent
        elif fallback is not None and fallback.shape == img.shape:
            img = fallback
    return img, data
