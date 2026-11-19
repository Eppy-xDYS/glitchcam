"""Datamosh an H.264 Annex-B elementary stream.

What a keyframe (IDR) does: it is the only kind of frame that carries a whole
picture. Every other frame (P-frame) just says "move these blocks from the
previous picture, then patch small differences". Remove the keyframes and the
decoder keeps pushing the *old* picture around with the *new* motion: one
scene melts and smears into the next. That is datamoshing.

Decoders refuse frames whose frame_num jumps, so after cutting an IDR the
remaining P-frames are renumbered in place (frame_num and pic_order_cnt_lsb
are fixed-width fields in the slice header, so the bit length never changes).

- level 1..9: share of keyframes removed (level 9 = all but the first)
- "bloom": some P-frames are repeated, re-applying the same motion so the
  smear keeps flowing in one direction.
"""

import random
import subprocess


# ---------------------------------------------------------------- bitstream
def split_nals(data):
    starts = []
    i = 0
    while True:
        j = data.find(b"\x00\x00\x01", i)
        if j < 0:
            break
        s = j - 1 if j > 0 and data[j - 1] == 0 else j
        starts.append((s, j + 3))
        i = j + 3
    out = []
    for k, (s, p) in enumerate(starts):
        e = starts[k + 1][0] if k + 1 < len(starts) else len(data)
        out.append(data[s:e])
    return out


def _payload_start(nal):
    return nal.find(b"\x00\x00\x01") + 3


def nal_type(nal):
    return nal[_payload_start(nal)] & 0x1F


def unescape(b):
    out, z = bytearray(), 0
    for x in b:
        if z >= 2 and x == 3:
            z = 0
            continue
        out.append(x)
        z = z + 1 if x == 0 else 0
    return out


def escape(b):
    out, z = bytearray(), 0
    for x in b:
        if z >= 2 and x <= 3:
            out.append(3)
            z = 0
        out.append(x)
        z = z + 1 if x == 0 else 0
    return out


class Bits:
    def __init__(self, data):
        self.d, self.p = data, 0

    def u(self, n):
        v = 0
        for _ in range(n):
            v = (v << 1) | ((self.d[self.p >> 3] >> (7 - (self.p & 7))) & 1)
            self.p += 1
        return v

    def ue(self):
        z = 0
        while self.u(1) == 0:
            z += 1
        return (1 << z) - 1 + self.u(z)

    def put(self, pos, n, v):
        for i in range(n):
            bit = (v >> (n - 1 - i)) & 1
            q = pos + i
            if bit:
                self.d[q >> 3] |= 1 << (7 - (q & 7))
            else:
                self.d[q >> 3] &= ~(1 << (7 - (q & 7))) & 0xFF


def parse_sps(nal):
    b = Bits(unescape(nal[_payload_start(nal) + 1:]))
    profile = b.u(8)
    b.u(16)
    b.ue()
    if profile in (100, 110, 122, 244, 44, 83, 86, 118, 128, 138, 139, 134, 135):
        if b.ue() == 3:
            b.u(1)
        b.ue()
        b.ue()
        b.u(1)
        if b.u(1):
            raise ValueError("scaling matrices not supported")
    fn_bits = b.ue() + 4
    poc_type = b.ue()
    poc_bits = b.ue() + 4 if poc_type == 0 else 0
    if poc_type == 1:
        raise ValueError("poc type 1 not supported")
    b.ue()
    b.u(1)
    b.ue()
    b.ue()
    frame_mbs_only = b.u(1)
    return {"fn_bits": fn_bits, "poc_type": poc_type, "poc_bits": poc_bits,
            "frame_mbs_only": frame_mbs_only}


def renumber(nal, sps, frame_num, poc):
    """Rewrite frame_num (and POC lsb) of a non-IDR slice in place."""
    ps = _payload_start(nal)
    rbsp = unescape(nal[ps + 1:])
    b = Bits(rbsp)
    b.ue()  # first_mb_in_slice
    b.ue()  # slice_type
    b.ue()  # pps id
    b.put(b.p, sps["fn_bits"], frame_num % (1 << sps["fn_bits"]))
    b.p += sps["fn_bits"]
    if not sps["frame_mbs_only"]:
        if b.u(1):
            b.u(1)
    if sps["poc_type"] == 0:
        b.put(b.p, sps["poc_bits"], poc % (1 << sps["poc_bits"]))
    return nal[:ps + 1] + bytes(escape(rbsp))


# ---------------------------------------------------------------- moshing
def mosh(data, level, seed):
    rng = random.Random(seed)
    sps = None
    out = []
    dropped = kept_idr = 0
    fn = 0
    drop_p = 1.0 if level >= 9 else (level - 1) / 8.0
    bloom_p = 0.012 * level
    since_idr = 0
    for nal in split_nals(data):
        t = nal_type(nal)
        if t == 7 and sps is None:
            sps = parse_sps(nal)
        if t == 5:
            if kept_idr and rng.random() < drop_p:
                dropped += 1
                continue  # the scene keeps whatever picture it had
            kept_idr += 1
            fn = 0
            since_idr = 0
            out.append(nal)
            continue
        if t == 1 and sps is not None and kept_idr:
            fn += 1
            since_idr += 1
            out.append(renumber(nal, sps, fn, 2 * fn))
            if since_idr > 8 and rng.random() < bloom_p:
                for _ in range(rng.randint(2, 3 + level)):
                    fn += 1
                    out.append(renumber(nal, sps, fn, 2 * fn))
            continue
        if t == 1 and not kept_idr:
            continue  # P-frames before any picture exists
        out.append(nal)
    return b"".join(out), dropped


def mosh_to_mp4(h264_path, mp4_path, level, seed, fps=30):
    with open(h264_path, "rb") as f:
        data = f.read()
    moshed, dropped = mosh(data, level, seed)
    cmd = [
        "nice", "-n", "10",
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-err_detect", "ignore_err",
        "-f", "h264", "-r", str(fps), "-i", "pipe:0",
        "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency", "-threads", "1",
        "-crf", "20",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart", mp4_path,
    ]
    subprocess.run(cmd, input=moshed, check=True, timeout=1800,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return dropped


def main(argv):
    """Detached worker: h264mosh.py IN.h264 OUT.mp4 LEVEL SEED (deletes IN)."""
    import os
    src, dst, level, seed = argv[1], argv[2], int(argv[3]), int(argv[4])
    part = dst + ".part.mp4"
    try:
        mosh_to_mp4(src, part, level, seed)
        os.replace(part, dst)
    finally:
        for f in (src, part):
            try:
                os.unlink(f)
            except OSError:
                pass


if __name__ == "__main__":
    import sys
    main(sys.argv)
