#!/usr/bin/env python3
"""Detached worker: raw RGB frames on disk -> MP4.

clipenc.py IN.rgb OUT.mp4 WIDTH HEIGHT FPS   (deletes IN when done)

Frames are recorded raw because this Pi has ~350 MB of RAM: running an encoder
next to the camera pipeline pushes it into swap and the viewfinder stalls.
Encoding afterwards, niced, keeps recording smooth.
"""

import os
import subprocess
import sys


def encode(src, dst, w, h, fps):
    part = dst + ".part.mp4"
    cmd = [
        "nice", "-n", "10",
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{w}x{h}", "-r", f"{fps:.3f}", "-i", src,
        "-vf", f"scale={w * 2}:{h * 2}:flags=neighbor",
        "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency", "-threads", "1",
        "-crf", "18", "-pix_fmt", "yuv420p", "-movflags", "+faststart", part,
    ]
    try:
        subprocess.run(cmd, check=True, timeout=3600,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        os.replace(part, dst)
    finally:
        for f in (src, part):
            try:
                os.unlink(f)
            except OSError:
                pass


if __name__ == "__main__":
    a = sys.argv
    encode(a[1], a[2], int(a[3]), int(a[4]), float(a[5]))
