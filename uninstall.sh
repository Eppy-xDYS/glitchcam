#!/bin/sh
# Remove GlitchCam from the launcher. Your photos and clips are kept.
#   sudo /usr/share/APPLaunch/apps/glitchcam/uninstall.sh
set -eu
ROOT=/usr/share/APPLaunch

if [ "$(id -u)" -ne 0 ]; then
    echo "Run with sudo: sudo $0" >&2
    exit 1
fi

pkill -f "$ROOT/apps/glitchcam/glitchcam.py" 2>/dev/null || true
rm -f "$ROOT/applications/glitchcam.desktop"
rm -f "$ROOT/share/images/glitchcam.png"
rm -rf "$ROOT/apps/glitchcam"

echo "GlitchCam removed. Photos/clips are still in ~/Pictures/GlitchCam (delete by hand if unwanted)."
