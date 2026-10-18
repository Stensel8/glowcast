#!/usr/bin/env bash
# Run Glowcast from this checkout, inside the installed Flatpak.
#
#   dev/run.sh
#
# Stops a running Glowcast first, then starts the code in src/. Ctrl+C quits.

set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/flatpak-env.sh"

# A running Glowcast would just raise its old window. Stop it cleanly, so it
# ends its entertainment stream on the bridge.
if pkill -TERM -f "python3? .*-m glowcast|/app/bin/glowcast"; then
    echo "Stopping the running Glowcast..."
    for _ in $(seq 20); do
        pgrep -f "python3? .*-m glowcast|/app/bin/glowcast" >/dev/null || break
        sleep 0.5
    done
fi

# The same permissions as flatpak/io.github.stensel8.glowcast.yml
exec flatpak run \
    --command=python3 \
    --socket=pulseaudio \
    --nofilesystem=xdg-run/pipewire-0 \
    --filesystem="$repo" \
    --env=PYTHONPATH="$repo/src" \
    --env=PYTHONUNBUFFERED=1 \
    "${host_args[@]}" \
    "$app" -m glowcast "$@"
