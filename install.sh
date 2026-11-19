#!/bin/sh
# Install GlitchCam into the CardputerZero launcher (APPLaunch).
#   sudo ./install.sh
set -eu

SRC="$(dirname "$(readlink -f "$0")")"
ROOT=/usr/share/APPLaunch
DEST=$ROOT/apps/glitchcam

if [ "$(id -u)" -ne 0 ]; then
    echo "Run with sudo: sudo $0" >&2
    exit 1
fi

for dep in python3 ffmpeg; do
    command -v "$dep" >/dev/null || { echo "missing: $dep" >&2; exit 1; }
done
python3 -c "import picamera2, numpy, PIL, evdev" || {
    echo "missing python modules (picamera2, numpy, pillow, evdev)" >&2; exit 1; }

mkdir -p "$DEST/fonts"
install -m 0755 "$SRC/glitchcam.py" "$SRC/run-glitchcam.sh" "$SRC/h264mosh.py" "$SRC/clipenc.py" "$DEST/"
install -m 0644 "$SRC/glitch.py" "$SRC/sensorbend.py" "$SRC/icon.png" "$SRC/README.md" "$DEST/"
install -m 0644 "$SRC/fonts/"* "$DEST/fonts/"
install -m 0755 "$SRC/uninstall.sh" "$DEST/"

# pre-rendered splash so something is on screen while python loads (~5-10 s)
python3 "$DEST/glitchcam.py" --render-splash "$DEST/splash.rgb565"

# precompile: the app runs as the normal user and can't write __pycache__ here
python3 -m compileall -q "$DEST" >/dev/null

install -m 0644 "$SRC/icon.png" "$ROOT/share/images/glitchcam.png"
install -m 0644 "$SRC/glitchcam.desktop" "$ROOT/applications/glitchcam.desktop"

echo "GlitchCam installed in $DEST"
echo "It should appear in the launcher now; if not: systemctl --user restart APPLaunch"
