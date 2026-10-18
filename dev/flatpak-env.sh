# Sourced by the dev scripts: which installed Flatpak to run Glowcast's code in.
#
# The Glowcast Flatpak itself once dev/build.sh has installed it. Until then the
# Lumux Flatpak (the same GNOME runtime plus numpy) can host it, with permission
# to use Glowcast's own D-Bus names.

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
app=io.github.stensel8.glowcast
host_args=()
if ! flatpak info "$app" >/dev/null 2>&1; then
    if flatpak info io.github.enginkirmaci.lumux >/dev/null 2>&1; then
        echo "Glowcast's Flatpak isn't built yet (dev/build.sh); borrowing Lumux's runtime." >&2
        app=io.github.enginkirmaci.lumux
        host_args=(--own-name=io.github.stensel8.glowcast --own-name=io.github.stensel8.glowcast.*)
    else
        echo "Build the Flatpak first: dev/build.sh" >&2
        exit 1
    fi
fi
