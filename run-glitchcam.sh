#!/bin/sh
# APPLaunch entry point for GlitchCam. Runs as the launching user (pi):
# video, input and i2c group membership covers camera, screen, keyboard and
# the experimental sensor mode - no root needed.
APP_DIR="$(dirname "$(readlink -f "$0")")"
LOG_DIR="${XDG_CACHE_HOME:-$HOME/.cache}"
mkdir -p "$LOG_DIR"
cd "$APP_DIR" || exit 1
export PYTHONUNBUFFERED=1
exec python3 "$APP_DIR/glitchcam.py" >"$LOG_DIR/glitchcam.log" 2>&1
