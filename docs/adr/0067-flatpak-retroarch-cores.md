# ADR 0067: RetroArch's Flatpak loads cores from the user's app config

**Date:** 2026-10-08
**Status:** Accepted (1.16.1)

## Context

The eight RetroArch definitions name their cores under `/usr/lib/libretro/` (the system
package layout). OpenBox launches RetroArch through `flatpak run org.libretro.RetroArch`
whenever the native binary is absent, and that is the install path the app itself offers.

Checked on this machine with the Flathub build (RetroArch 1.22.2), not inferred:

- The sandbox has no `/app/lib/libretro` and no `/usr/lib/libretro`. Its `share/libretro`
  holds assets, databases and overlays, but no cores.
- `flatpak run org.libretro.RetroArch -L /usr/lib/libretro/snes9x_libretro.so game` fails:
  `--libretro argument ... is not a file ... Frontend is built for dynamic libretro cores, but path is not set.`
- A core placed in `~/.var/app/org.libretro.RetroArch/config/retroarch/cores/` loads:
  `[Core] Loading dynamic libretro core from: ".../cores/snes9x_libretro.so"`. The sandbox sees
  the app's config at the same path as the host.

So a Flatpak launch with the system path could not start any RetroArch system. The Launch
Doctor checked the host path, so it reported the core as present on a machine where the
launch would fail.

## Decision

- A launch through a Flatpak prefix rewrites a `-L /usr/lib/libretro/<core>` argument to
  `~/.var/app/org.libretro.RetroArch/config/retroarch/cores/<core>`. The rewrite is
  `pkg/parity/parity_emulator_defs.py::flatpak_core_path`. Native launches keep the system path.
- The Launch Doctor checks the same resolved path when the install mode is Flatpak, and names
  the core it expects when it is missing.
- The definitions do not change. The mapping lives in one place, so a future definition
  inherits it.

## Consequences

- A Flatpak user still has to download the core, through RetroArch's Online Updater
  (or by placing the file). OpenBox names the missing core; it does not download it.
- A core installed by the Flatpak's own updater is in the directory this ADR names. A core
  that lives elsewhere (for example a native install of the same version) is not used by the
  Flatpak launch; that is intended, because the sandbox cannot read it.
- Tests: `tests/test_emulators.py::FlatpakRetroArchCoreTests`, `tests/test_launch_doctor.py::FlatpakCoreMissingTests`.
