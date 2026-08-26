# ADR 0012: Emulator registry schema and ambiguity policy

Date: 2026-08-25
Status: Accepted

## Context

Emulator detection, platform mapping, and Launch Doctor checks currently draw from duplicated constants and legacy strings. v1.7 makes versioned YAML in `emulator_defs/` the source of truth while keeping a dependency-free fallback parser and preserving custom user profiles.

## Decision

### Flat adapter-per-emulator-platform YAML

- One **flat adapter definition per emulator/platform combination** in `emulator_defs/`.
- Each adapter defines: schema, adapter ID, emulator ID, display name, and platform; supported extensions; native executable and Flatpak application ID; `startup_args` as an argument list; optional core, BIOS, firmware, and filesystem requirements; detection patterns and priority hints.
- Group adapters by emulator ID at load time.
- Generate compatibility views for existing `EMULATORS`, recommendation, profile-discovery, and YAML APIs from the registry instead of contradictory constants.
- Read legacy `startup` strings for one release; validate and compile them into argument arrays.
- **`emulator_def`** remains a compatibility alias.

### Fallback parser

- Registry loading stays dependency-free and compatible with the existing fallback parser.
- Invalid definitions fail validation at load time with actionable errors.

### Ambiguity policy

- Extensions shared by multiple systems—especially **`.iso`**—return **every valid candidate** instead of silently selecting the first definition.
- An explicit scan configuration’s emulator/platform always wins.
- Auto-import must pass its saved `emulator_id` into the scanner.
- RetroArch adapters must specify platform-appropriate cores; no SNES9x command may be reused across every RetroArch platform.
- Missing cores, BIOS, or firmware are actionable Launch Doctor checks, never silent fallback.

### Custom profiles

- **Custom user profiles always win** and must never be overwritten by registry updates or imports.

### Launch precedence

Resolved launch command order:

1. Per-game explicit launch command
2. Per-game selected launch profile/adapter
3. User-configured platform profile
4. Detected registry adapter
5. Direct executable fallback when valid

### Registry and preflight API

- **`GET /api/v2/emulators/registry`** exposes the registry-derived compatibility view.
- **`POST /api/v2/launch/preflight`** and **`POST /api/v2/launch/preflight/batch`** run Launch Doctor checks without launching.
- Blocking preflight returns **`LAUNCH_PREFLIGHT_BLOCKED`**; missing emulator selection returns **`EMULATOR_REQUIRED`**; unresolved platform choice returns **`AMBIGUOUS_PLATFORM`** (see ADR 0010).

## Consequences

- Import preview, emulator install, and Launch Doctor share one authoritative registry.
- Shared extensions no longer mis-launch due to silent first-match selection.
- User customization survives registry refreshes unchanged.
