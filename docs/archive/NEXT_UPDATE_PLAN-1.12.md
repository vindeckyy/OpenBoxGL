# OpenBox 1.12 — Megaplan

Status: shipped as 1.12.0 (2026-09-14). Archived per the docs-archive
convention (ADR 0041); the execution log below is the historical record.

## Execution log

- [x] §1.2 Smart Collections — `pkg/parity/parity_collections.py`,
  `handlers/collections.py`, sidebar + save chip (ADR 0047).
- [x] §1.3 Story Mode — `pkg/parity/parity_story.py`, `GET /api/v2/story`,
  detail-pane Story tab (projection only, no PNG export yet).
- [x] §1.5 Backup & Restore UI — already existed; added the missing
  scheduled weekly auto-backup (`auto_backup_due()` + daemon tick) and
  last-run display.
- [x] §1.6 Per-game launch sheet — `launch_env` merge + `launch_confirm`;
  profile/gamescope overrides already existed; gamescope projection bug
  fixed as found.
- [x] §3.3 Focus/Escape — `closeDialog()` owns the focus stack (one guard
  in the shared function).
- [x] §3.4 CSP regression gate — Stage 2.8 `scripts/check_csp.py`.
- [x] §4 SQLite default-on — self-enables at 5,000 games, env opt-out
  honored (facets stay on JSON; search-worker facet move deferred).
- [x] §2.3 palette learns usage — localStorage recent-count ranking, no telemetry.
- [x] §3.1 sendfile audit — single sendfile site (already fixed); export
  download reuses `send_bytes` for the same write discipline.
- [x] §3.2/3.5/3.8/3.10 audits — no other virtualized grids, SSE reap on
  write failure exists, no module-level `t()` maps, party.js reuses
  bigbox.js edge detection (arcade's copy is isolated to its canvas).
- [x] §6 webhooks — verified already wired (`publish_event` → dispatcher).
- [x] Story uses History→Timeline markup; zero new CSS/tokens.
- [ ] §1.1 Presence, §1.4 Deck Builder, household weekly auto-challenge,
  Time Machine compare, artwork fix-all — deferred to 1.12.1/1.13.
- [ ] §3 remaining sweep (sendfile audit, SSE reap, TZ, sandbox, gamepad
  dedupe), §5 packaging/Flathub, §6 plugin API + webhooks + api-v2.md,
  §7 a11y pass, story PNG export — open.

1.11 shipped Quick Resume, Moments/Clips, Time Machine, Backlog Radio, the
command palette, Arcade Room/Museum kiosk mode, Household, Steam Bridge, ES-DE
import, SteamGridDB, and local trophies. 1.12 should consolidate: make the
1.9–1.11 feature wave feel finished, fix the sharp edges it left, and land a
small number of high-drama features users will actually notice.

Guiding theme: **"Your library, alive and connected."**

---

## 0. Release shape

- Version: `1.12.0`, SemVer, v1 route contract stays frozen
  (`scripts/check_v1_contract.py`); all new surface goes under `/api/v2/`.
- No new runtime dependencies. Everything below is stdlib + existing frontend.
- Every new runtime module → `runtime_modules.txt` + `tests/test_<module>.py`.
- Every new visual value → token in `static/app.css :root` + all five stock
  themes (`scripts/check_tokens.py` is ratcheted to 0).
- Coverage floors only go up.
- ADRs for: any new route family, any state-boundary change, any new sync
  transport, any new gate.

---

## 1. Headline features (pick 3–4, do not ship all)

### 1.1 Game Sessions Live — "Now Playing" presence (Household 2.0)
Household already projects members/challenges/leaderboards over the sync
folder. Next step: presence.

- `POST /api/v2/household/presence` — heartbeat: `{game_id, started_at,
  state: playing|idle}` written as a signed event into the sync folder, TTL
  ~10 min, expired heartbeats ignored on read.
- `GET /api/v2/household/activity` — "who's playing what right now" feed:
  member card + cover + elapsed time + "Join via Game Night" CTA for couch
  titles.
- Frontend: `static/household.js` gets a live strip at the top; optional
  toast "Alex started Stardew Valley" (rate-limited, opt-in per member).
- Zero server, zero accounts — same local-first folder transport as 1.11.
- New module: `pkg/parity/parity_presence.py`; reuse
  `parity_household` event signing + `launch_tokens` for session identity.
- Privacy: off by default, per-member opt-in, no history retained (presence
  is a projection of unexpired events only).

### 1.2 Smart Collections 2.0 — auto shelves from Backlog Radio queries
ADR 0020 added smart-collection chips. Close the loop: let users pin a Radio
query as a living shelf.

- "Save as collection" button in the query bar → stores the query AST (not
  the result list), named, editable.
- Collections evaluate at render time against the canonical search path —
  "Games under 5h I haven't beaten on Steam Deck" stays correct forever.
- `GET /api/v2/collections/smart` + `POST/PUT/DELETE`; stored in settings,
  exported with library export.
- Detail-pane "In collections" section; constellation can color by
  collection.
- Frontend: shelf chips row, collection editor dialog (reuse
  `static/dialogs.js` + query chips UI).

### 1.3 Story Mode — narrative recap of a single game's journey
Wrapped proved the format. Apply it per-game.

- `GET /api/v2/games/<id>/story` — deterministic timeline: added → first
  played → longest session → beaten → mastered, with Moments/Clips/trophies
  interleaved. Pure projection over session journal + time-machine journal +
  trophy case. No new writes.
- `static/story.js` — printable/shareable vertical story card, same
  visual language as Wrapped; "Export PNG" via canvas.
- Entry points: detail pane tab, context menu "Story", palette command.

### 1.4 Deck Builder for Game Night — themed party playlists
Extend `parity_party`: save/load named queues, theme presets
("90s racers", "co-op only", "8+ players"), and share a queue into Household
so a remote member can spin the same wheel deterministically (seeded
shuffle — the seed travels, not the order).

### 1.5 Backup & Restore — first-class, user-facing
`parity_backup` exists; surface it.

- Settings → Data: "Create backup" (library + settings + saves metadata +
  trophies, excludes media binaries unless checked) and "Restore from
  backup" with the existing diff/preview API (ADR 0019).
- Scheduled weekly auto-backup to a user-chosen folder, keep last N
  (default 4), surface "last backup: 3 days ago" in Settings.
- This is the single most-requested safety feature for a launcher that
  manages real files.

### 1.6 Universal launch options — per-game override sheet
Today: global emulator/env config. Users want right-click → "Launch
options…" on a card.

- Per-game sheet: emulator override, env vars, gamescope preset (ADR 0016),
  wine/faugus profile, pre/post-launch hook commands, "always confirm before
  launch".
- Stored on the game record at the canonical state boundary; merge order
  documented: game > platform > global.
- Launch Doctor (already exists) consumes the merged config — same
  diagnostics, one code path.

---

## 2. Deepen existing features (medium effort, high perceived polish)

### 2.1 Moments & Clips
- Auto-Moment: on session end, if a trophy unlocked or progress state
  advanced, offer "Capture this moment?" — one click bookmarks the recap
  timestamp.
- Clip stitching: select up to 5 Moments → "Make reel" (parity_reels already
  exists — wire it to the Moments UI with an actual button).
- Clip/GIF export for clips that are screenshot-backed: animated WebP via
  pure-Python encoder is too heavy — instead export a contact-sheet PNG
  strip (cheap, deterministic, no deps). Video files export as-is.

### 2.2 Time Machine
- "Compare two dates" diff view: what was added/removed/re-edited between
  two points. Journal already has the data; this is a read-side diff.
- Revert preview already exists; add revert *apply* for a bounded whitelist
  (metadata fields only — never file paths or launch config, that's where
  damage happens).

### 2.3 Command palette
- Learn from usage: rank recent commands higher (local counter, no
  telemetry).
- `>` prefix: run Launch Doctor; `?` prefix: search help/docs (bundle a
  small static help index from `docs/` at build time).
- Plugin commands: `plugins.py` can register palette entries — one
  `register_command()` hook, huge extensibility win for ~50 lines.

### 2.4 Constellation
- Save viewpoints (pan/zoom + filters) per user.
- "Path between two games" — highlight shortest edge chain; party trick
  feature, cheap BFS.
- Export graph as PNG (canvas already exists — `toDataURL`).

### 2.5 Arcade Room / Museum
- Attract mode: idle >5 min in kiosk → slow auto-tour of random games with
  video snaps (reuse Big Box video path + reduced-motion guard).
- Kiosk PIN: add lockout backoff (exponential, in-memory only) — current
  PIN is a convenience boundary; make the docs honest about it.
- QR code on screen pairing a phone as a remote (same-origin URL + token) —
  no native app needed, it's just the web UI in a narrow layout.

### 2.6 Household
- Challenges v2: recurring weekly challenge auto-generated deterministically
  ("most new games beaten this week"), seeded by week number so all members
  agree without coordination.
- Member "shelf share": publish a filtered game list (not files) to the sync
  folder; others can import entries they don't have as *wishlist* entries
  (new entry state, converts to real on import of the actual game).

### 2.7 Insights
- "Finish a game" nudge: games at 80–99% progress get a subtle shelf;
  high conversion feature, one query.
- Cost-per-hour card in Wrapped/Insights if a price field exists
  (add optional `price_paid` to game record; store users asked for this).

### 2.8 Saves
- Save versioning UI: `parity_save_tools` + saves.py already snapshot; add a
  per-game "Save history" tab with restore + size + age, using the same
  bounded-diff pattern as backup.
- Cloud-sync saves opt-in via the causal catalog transport (ADR 0039) —
  saves are small, high-value, and the transport already handles conflicts.

### 2.9 Media / artwork
- Bulk artwork hygiene report: missing covers / low-res / mismatched aspect,
  one "fix all with SteamGridDB" button (provider exists — this is a batch
  UX over it).
- Animated cover hover in grid (video snap as `poster`-swapped `<video>`,
  reduced-motion honored, only when card is focused >400ms to avoid decode
  storms).

### 2.10 Storefront / emulation onboarding
- Per-platform "setup checklist" card: BIOS present (SHA1 drift detection,
  ADR 0018), emulator resolved, one game launches, artwork fetched. Turns
  "why doesn't PS2 work" into a green checklist.
- Emulator defs: ship a community-def update channel — YAML defs versioned
  and fetched like plugin_catalog, signature-checked with the existing
  release pubkey (`openbox-release.pub`).

---

## 3. Bug fixes & correctness debt (the honest list)

Sweep candidates — verify each is still live before scheduling:

1. **sendfile resume** (fixed in Unreleased) — audit *every* other
   `os.sendfile`/`wfile.write` path for the same partial-write bug pattern.
2. **Virtualized grid + panes**: the scroll-offset bug (Unreleased) suggests
   the virtualizer assumes grid-at-top. Audit `timemachine`, `mastery`,
   `household` panes for the same assumption; extract a shared
   `virtualGrid.js` helper if 3+ copies exist.
3. **Focus/Escape ordering** (Unreleased fix): audit all context menus and
   dialogs for the same "who restores focus" race — centralize an
   `openDialog()/closeDialog()` that owns a focus stack.
4. **CSP `frame-ancestors` regression class**: add a check in
   `scripts/` asserting reader/document endpoints keep `frame-ancestors
   'self'` while everything else stays `'none'` — a gate, so 1.11's bug
   can't return.
5. **SSE/presence leaks**: verify SSE connections close on pagehide and
   server-side clients are reaped on write failure (audit `pkg/state/sse.py`
   deps).
6. **Large-library cold start**: profile state load for 20k games; if the
   warm cache is bypassed on first write after boot, fix that ordering.
7. **Time zones in sessions**: verify session journal timestamps render in
   local TZ everywhere (timeline, wrapped, story); mixed-UTC strings are the
   classic bug here.
8. **i18n lazy-label pattern**: the 1.9 "raw i18n keys" fix — grep for any
   remaining module-level `t()` maps that froze before locale load.
9. **Plugin sandbox**: confirm `plugin_runner` can't write outside its
   declared dir and has a timeout; add a test if missing.
10. **Gamepad polling**: `pollGamepads` edge detection across party.js /
    bigbox.js — dedupe into one module if drifted.

---

## 4. Performance & scale

- SQLite read model: graduate FTS5 search to default-on above N games
  (threshold e.g. 5k, env override stays). ADR 0032 made it opt-in; 1.12
  makes the fast path the default where it helps, parity check retained.
- Media serving: HTTP range requests + `If-None-Match` on covers/snaps —
  Big Box re-decodes full files today; ETag by mtime+size is ~30 lines.
- Search worker: move facet counting into `worker.search.js` for >10k
  libraries so the UI thread never janks while typing.
- Perf gate: add a 50k tier to perf-20k benchmarks once, keep budgets
  p95-based per ADR 0042.

---

## 5. Platform & packaging

- Wayland native host: verify WebKitGTK host under Wayland fractional
  scaling; document X11 fallback.
- Steam Deck Game Mode smoke test script (gamescope preset launch → exit
  cleanly → back to OpenBox) as a manual checklist in docs.
- Flatpak: finish `docs/flathub-checklist.md` items; 1.12 targets Flathub
  submission.
- aarch64 AppImage from emulated gate → promote to release artifact if the
  1.11 emulated path stayed green.
- Update checker (`updates.py`): add "download in background, apply on
  restart" for AppImage via existing `.zsync` files.

---

## 6. Extensibility & API

- Public plugin API v1 freeze: document what `plugins.py` exposes
  (library read, palette commands, detail-pane tabs, notification post).
  SemVer it. Third-party plugins are a growth lever.
- Webhook out: optional `POST` to a user URL on events (game launched,
  session ended, trophy unlocked) — one urllib call in `notifications.py`,
  enables Home Assistant / Discord-bot tinkerers. Opt-in, localhost default
  off.
- OpenAPI-ish: generate a `docs/api-v2.md` route table from the route
  registry at check time — keeps docs honest for free.

---

## 7. Accessibility & UX debt

- Full keyboard pass on detail pane (focus trap audit), screen-reader
  labels on card grid (aria-rowcount etc. for virtualized grid).
- High-contrast theme variant: sixth stock theme, proves the token contract
  works, serves real users.
- Reduced-motion: audit `mood.js`, `bigbox.js`, wheel spin, constellation
  intro — every animation gated.
- First-run flow: after setup, show a 3-card "try these" (pick a game, run
  Radio query, open Arcade Room) — onboarding that teaches the features
  nobody finds.

---

## 8. i18n

- All new strings through `static/i18n.js`; new keys in all five stock
  locales (gate already checks).
- Add community locale contribution docs (`docs/CONTRIBUTING.md` section);
  accept a 6th locale if a PR lands — infra already supports it.

---

## 9. Suggested milestone split

**1.12.0 must-haves**
- Backup & Restore UI (§1.5)
- Smart Collections from queries (§1.2)
- Per-game launch options sheet (§1.6)
- Now-Playing presence (§1.1) *or* Story mode (§1.3) — pick one headline
- Bug sweep §3 items 2, 3, 4, 8 (regression-class fixes + gates)
- ETag/range media serving, SQLite-FTS default above threshold (§4)

**Stretch / 1.12.1**
- Story mode (if not headline), Deck Builder, Time Machine compare,
  Attract mode + phone remote, saves sync, webhooks, auto-update apply.

**Explicitly out of scope (say so in release notes)**
- No cloud hosting, no accounts, no mobile app — local-first is the brand.
- No scraping new storefronts without legal review.
- No telemetry of any kind.

---

## 10. Gate / process work

- ADRs: presence transport, smart-collection storage, per-game launch merge
  order, webhook egress boundary, SQLite-default-on threshold, kiosk PIN
  honesty doc.
- New gates: CSP-regression check (§3.4), locale-key parity (if missing),
  generated api-v2.md freshness check.
- `CHANGELOG.md` entries per user-visible item; release notes drafted from
  day one, not at the end.
- Perf: warm-up + worst-run-drop convention (ADR 0042) applied to any new
  benchmarked endpoint.

---

## 11. Kill list (things to *remove* — ponytail pass)

Before adding, delete:
- Any handler/route that exists only for a removed feature (grep v1
  contract for dead v2 leftovers).
- `parity_premium.py` — if premium gating isn't a real product surface, it's
  speculative abstraction; either ship it or cut it.
- Duplicate scroll/virtualization and gamepad code once deduped (§3.2, §3.10).
- Stock art/zsync leftovers in repo root (`OpenBox-x86_64-final*.zsync`) —
  move to `output/` or delete; root is not a download dir.
