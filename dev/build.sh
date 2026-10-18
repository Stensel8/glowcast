#!/usr/bin/env bash
# Build the Glowcast Flatpak and install it for your user.
#
#   dev/build.sh
#
# Needs Flathub's packaged flatpak-builder: flatpak install flathub org.flatpak.Builder
# The GNOME SDK it builds against is installed automatically from Flathub.

set -euo pipefail
repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo"
exec flatpak run org.flatpak.Builder \
    --user --install --force-clean \
    --install-deps-from=flathub \
    --state-dir=flatpak/.flatpak-builder \
    flatpak/build \
    flatpak/io.github.stensel8.glowcast.yml
