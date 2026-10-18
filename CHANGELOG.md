# Changelog

All notable changes to Glowcast, which started as a fork of
[Lumux](https://github.com/enginkirmaci/lumux) by Engin Kırmacı. Numbers refer to
issues and pull requests in this repository.

## 0.1.1

### Bridge setup

- **Setting up a bridge for the first time works.** After you pressed the
  bridge's button, the setup wizard kept using the connection it had made before
  pairing, which had no app key yet. Reading the entertainment areas then failed
  ("The bridge is not set up" in the log) and the wizard showed *No
  entertainment zones found*, so Finish stayed greyed out. Only a fresh install
  ran into it: a bridge paired before was never affected. The wizard now
  connects with the new key once pairing is done. (#2)
- **The setup says why it could not read the entertainment areas**, for example
  *Could not read the entertainment zones: Bridge answered 403 …*, instead of
  *No entertainment zones found*, which looked like a bridge without any.

### Look

- **An app icon of Glowcast's own**: a pear-shaped E27 bulb on an
  orange-to-blue gradient, instead of Lumux's icon. The About window says
  Glowcast started as a fork of Lumux, credits it under *Forked from* and shows
  both copyright lines, as in the licence.
- **Scenes are colour swatches**, the same tiles as the music palettes, instead
  of round pictures, so every mode previews its colours the same way.

### Settings

- **Where Glowcast keeps its settings, and how to start over**, is in the
  README now: one `settings.json`, what the Reset button keeps, and which folder
  to remove for a completely fresh start.
- Settings files from development versions before 0.1.0 are no longer
  converted; their old values fall back to the defaults.

### Platform

- **GNOME 51**: the Flatpak runs on the GNOME 51 platform, with Python 3.14
  and numpy 2.5.4.
- **Releases come with the Flatpak**: publishing a GitHub release attaches
  `glowcast-<version>-x86_64.flatpak` to it, built from the release's tag. See
  *Install* in the README.

### Development

- Every dependency is pinned: GitHub Actions by commit, container images by
  digest and the development tools by hash. Dependabot and Renovate keep them
  current, Renovate also the GNOME platform, and a weekly
  flatpak-external-data-checker run updates the numpy wheels in the Flatpak.
- CI lints on Python 3.15 and runs the tests on Fedora 44 (Python 3.14, as in
  the Flatpak) and Fedora 45 (Python 3.15).
- `dev/run.sh` runs the code in `src/` inside Glowcast's own Flatpak and no
  longer borrows Lumux's. The offscreen screenshot scripts are gone: GTK's
  panel crossfade never finished there, so the scenes and video screenshots
  showed the previous mode's panel.

## 0.1.0

First version of Glowcast, based on Lumux by Engin Kırmacı.

- Scenes, Games, Music and Video, switchable while syncing
- Several monitors at once, with a capture area you drag on the live preview
- Music palettes and scenes of your own, plus the scenes on your bridge
- The window glows with your lights
- Keyboard shortcuts, sync delay, background lighting and a vocal filter
