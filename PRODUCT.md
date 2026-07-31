# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

Linux desktop, laptop, Steam Deck, and handheld PC owners who manage and launch a mixed game library.

## Product Purpose

OpenBox is a local-first game library manager and launcher for Linux. It brings games from storefronts, ROM folders, arcade sets, and emulator workflows into one searchable catalog, then helps users enrich, organize, launch, and track them. Success means a Linux user can manage a large mixed library from one place without depending on a vendor account or cloud-hosted library.

## Positioning

OpenBox is an open-source Linux launcher that unifies storefronts, ROMs, emulators, metadata, saves, and play sessions in local user-owned data, with advanced library workflows included without a subscription.

## Operating Context

Users run OpenBox on Linux desktops, laptops, Steam Decks, and handheld PCs. The web UI is the full-featured interface and includes library management, REST API access, and Big Box controller-oriented browsing. A separate lightweight Tk interface is also shipped. Users may already have Steam, Heroic, Lutris, Gameyfin, RetroArch, Flatpak emulators, local ROM folders, or standalone game executables installed.

## Capabilities and Constraints

- Library data is stored locally as JSON under the user's data directory.
- The web UI is served locally by Python and communicates with a token-authenticated REST API.
- Current import sources include Steam, Heroic, Lutris, Gameyfin, ROM folders, arcade sets, ScummVM, RPCS3, Vita3K, and local executables.
- Current workflows include advanced search, metadata and artwork galleries, filters, collections, ordered playlists, configurable badges, custom fields, ESRB data, emulator profiles and per-game overrides, game launching, session history, progress automation, save and library backups, RetroAchievements, plugins, themes, and Big Box mode.
- Linux is the primary platform. OpenBox does not provide Windows LaunchBox binary compatibility or distribute ROMs.
- The project prefers the Python standard library and existing patterns; new dependencies require justification.
- OpenBox has no Premium paywall, and it does not require an OpenBox account.
- Secrets and tokens come from environment configuration and user data must not enter the repository.
- OpenBox is not a multiplayer game server or client.

## Brand Commitments

- The product name is OpenBox.
- OpenBox is an independent open-source project and must not imply affiliation with LaunchBox or Unbroken Software, LLC.
- The project is licensed under AGPL-3.0.

## Evidence on Hand

- `README.md` documents the product purpose, supported workflows, interfaces, installation, and development commands.
- `PARITY.md` records the LaunchBox parity matrix and acceptance checks.
- `assets/openbox-screenshot.png` and `assets/openbox-game-detail.png` show the current web UI.
- The repository contains implementation modules and tests for the capabilities listed above.
- No customer testimonials, usage benchmarks, or third-party endorsements are established; future work must not fabricate them.

## Product Principles

- Keep library data local and under the user's control.
- Fit the Linux tools and paths users already have.
- Make mixed libraries searchable, organized, and practical to launch.
- Ship advanced library workflows without a subscription gate.
- Protect user data and credentials at every integration boundary.

## Accessibility & Inclusion

No product-specific accessibility requirement has been established yet.
