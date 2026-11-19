#!/bin/sh
# Copy this checkout to the CardputerZero's ~/glitchcam, optionally reinstall.
#
#   ./deploy.sh            copy the files (tracked files, including uncommitted edits)
#   ./deploy.sh --install  copy, then run sudo ./install.sh on the device
#                          (asks for the pi password unless sudo remembers it)
#
# Needs the "cardputer" host in ~/.ssh/config (key login, no password).
# Override the target with DEPLOY_HOST=user@host or DEPLOY_DIR=path.
set -eu

HOST="${DEPLOY_HOST:-cardputer}"
DIR="${DEPLOY_DIR:-glitchcam}"
cd "$(dirname "$0")"

# Snapshot of the tracked files as they are now, uncommitted edits included.
# git archive keeps the executable bits from git (Windows checkouts lose
# them) and autocrlf=false keeps LF line endings, which the device's sh
# and python need.
snap=$(git stash create)
snap=${snap:-HEAD}

echo "deploying $(git rev-parse --short HEAD)$( [ "$snap" = HEAD ] || echo '+edits') to $HOST:~/$DIR"
git -c core.autocrlf=false archive --format=tar "$snap" |
    ssh "$HOST" "mkdir -p ~/$DIR && tar -xf - -C ~/$DIR && echo copied: \$(ls ~/$DIR | wc -l) entries"

if [ "${1:-}" = "--install" ]; then
    ssh -t "$HOST" "cd ~/$DIR && sudo ./install.sh"
fi
