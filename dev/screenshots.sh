#!/usr/bin/env bash
# Render the Glowcast window offscreen in every mode, into dev/screenshots/.
#
#   dev/screenshots.sh
#
# Uses GTK's headless Broadway backend inside the Flatpak runtime, so nothing
# appears on screen and your lights and settings stay untouched.

set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/flatpak-env.sh"

out="${1:-$repo/dev/screenshots}"
mkdir -p "$out"
flatpak run --command=sh \
    --filesystem="$repo" \
    --env=PYTHONPATH="$repo/src" \
    "${host_args[@]}" \
    "$app" -c "
        gtk4-broadwayd :7 >/dev/null 2>&1 &
        sleep 1
        GDK_BACKEND=broadway BROADWAY_DISPLAY=:7 python3 '$repo/dev/screenshots.py' '$out'
        kill %1
    "
echo "Screenshots in $out"
