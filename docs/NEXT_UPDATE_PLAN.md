# OpenBox next-update plan — the "Time" release

Planning baseline: September 11, 2026; application version `1.10.0` in `updates.py`; 1.10.0 shipped
2026-09-08. A repository-wide bug-fix sweep (27 inventoried defects) is in flight under `.agents/`;
Milestone 1 fixes are implemented and under review at planning time.

> **Planning record.** This document proposes the next update; it does not start implementation.
> 1.9.0 was "look and discover," 1.10.0 was "trust." This release is about **time**: OpenBox learns
> to hold your place, remember your play, rewind your library, and turn sessions into keepsakes —
> all local, all dependency-free, all opt-in where it touches anything new.

## Continuous execution contract

Execute included work in one looping run; milestones are checkpoints, not pauses. Every change
follows `AGENTS.md`: zero new runtime dependencies; new modules under `pkg/parity/` or `handlers/`;
`runtime_modules.txt` + a `test_*.py` per new module; visual values tokenized in `static/app.css
:root` **and** all five themes; strings across `en/es/de/fr/pt`; v1 routes frozen — new surface is
additive `/api/v2/*`; ≥95% changed-line coverage; `make check` green before each milestone closes;
progress checkpointed in `docs/archive/NEXT_UPDATE_EXECUTION-1.11.md` after execution. Publishing and external submissions need
explicit authorization.

## 1. Recommended direction

1. **1.10.1 patch** — sweep-verified fixes only (media flush/disconnects, RA cache path, backup
   preflight, Faugus names, native auth, route whitelist, document ranges, M2–M4 findings). No
   features. Does not block 1.11.0 workstreams.
2. **1.11.0 — "Every Second Counts"** — six flagship capabilities no launcher ships today, plus a
   tight bridge track.

Release promise:

> Close a game mid-fight and come back to that exact frame — tomorrow, or on your other machine.
> Your library remembers everything, and can show its own history. Clip the moment that just
> happened. And when you ask "what should I play?", OpenBox answers from how *you* actually play.

Priority order:

1. **Moments & Quick Resume** — the flagship. Console-grade suspend/resume for every emulator that
   can support it, plus bookmarkable moments.
2. **Library Time Machine** — the 1.10.0 event log becomes a browsable, revertible history.
3. **Record That** — replay-buffer clips and highlight reels from moments.
4. **Backlog Radio** — honest habit-aware recommendations; a "just say it" search bar.
5. **The Arcade Room** — a walkable virtual room of your library; the release's showpiece.
6. **Household** — local-first leaderboards and challenges over the sync folder; zero servers.
7. Supporting track — Steam Bridge, SteamGridDB, undo/trash, recap card — the connective tissue the
   flagships need.

Explicit non-goals: no cloud service, no telemetry, no AI/LLM dependency, no game streaming, no
framework rewrite, no new v1 routes.

## 2. What exists — verified at baseline (build on it, don't rebuild it)

| Capability | Where | Reused by |
|---|---|---|
| Atomic launch reservations, session lifecycle, SSE session events, PID/start-time reconciliation | `pkg/state/launch.py`, `pkg/state/registry.py`, `pkg/state/sse.py` | Quick Resume, recap, clips |
| Causal, content-addressed event log with tombstones, device identity, conflict review, recovery snapshots | `pkg/parity/parity_library_sync.py` (ADRs 0038–0039) | Time Machine, Household, save/state sync |
| Save discovery roots + versioned backups + restore | `saves.py`, `pkg/parity/parity_saves.py` | Resume-state storage, save sync |
| Screenshot capture (gnome-screenshot/spectacle/scrot/import), OBS auto-attach | `pkg/parity/parity_integrations.py`, `handlers/media.py` | Moments, Record That |
| Optional-tool convention: degrade silently when a binary isn't on `PATH` (ryzenadj, ludusavi, spectacle, OBS) | throughout | ffmpeg reels, obs-websocket, emulator state flags |
| `openbox://` deep links + `dispatch_uri` + `--launcher` rofi/wofi | `pkg/parity/parity_deeplinks.py` | `--play` for Steam Bridge, `openbox://moment`, `openbox://clip` |
| Search worker `{id, type:'search'|'expand'|'warm'}` trigram protocol + acronym index | `static/worker.search.js` | Palette, smart query bar |
| Constellation edge computation (series/dev/publisher/genre/co-play) | `pkg/parity/parity_constellation.py` | Backlog Radio similarity |
| Picker scoring (time/mood/familiarity/players) | `pkg/parity/parity_picker.py` | Smart query target semantics |
| Durable operations + Activity drawer | `pkg/state/operations.py`, `static/activity.js` | all long jobs |
| History/heatmap/streak/momentum aggregates | `pkg/parity/parity_insights.py`, `handlers/insights.py` | Radio, Time Machine, trophies, On-this-day |
| Per-adapter YAML definitions (24) with launch tokens | `emulator_defs/`, `pkg/parity/parity_emulator_defs.py`, `launch_tokens.py` | state-capability table |
| Rolling state snapshots (5) + `.bak` + recovery | `state_store.py` | Time Machine coarse restore points |
| Game Night queue/wheel, bigbox layouts/gamepad, attract/screensaver | `static/party.js`, `static/bigbox.js` | Arcade Room, Museum |
| Confirmed absent at baseline | savestate launch/resume, event-journal UI, OBS websocket, habit analytics, room/kiosk mode, household records, launcher trophies, NL-ish query, `shortcuts.vdf` writer, SteamGridDB | the work below |

## 3. The flagships

### Workstream T1 — Quick Resume & Moments ("Never lose your place")

**Priority:** flagship. **Risk:** high on adapter variance — mitigate with an M0 capability spike.
**Implementation:** `emulator_defs/*.yaml` gain a `state:` capability block; new
`pkg/parity/parity_resume.py`; `pkg/state/launch.py` suspend/resume hooks; `handlers/launch.py`
resume action; `static/library.js` card affordances; new `static/moments.js`.

**Product behavior**

- *Quick Resume:* when a session on a supported adapter ends (or is closed via OpenBox), the last
  save state is captured into `saves/<game>/openbox-resume.*` with a screenshot thumbnail and a
  timestamp. The game card and detail pane show **Resume — "World 4, 2h ago"**; `Play` splits into
  Start / Resume where supported.
- *Moments:* during a session (or from the recap card after), press `M` / hit `openbox://moment/<id>`
  / click the pause-overlay button → capture screenshot + optional state snapshot + one-line note →
  a per-game **Moments timeline** (new detail tab): every moment with art, timestamp, note; clicking
  a moment offers *Resume from this moment* where the adapter supports it.
- *Auto-moments* (opt-in): first boot of a game ("First steps"), RA unlock, beaten/mastered
  transition, milestone thresholds.
- *Share a moment:* render a share card PNG (canvas → cover art + stat + note) to clipboard/file.
- *Adapter truth:* resume state validity is bound to adapter+version; on emulator/core upgrade,
  stale states are marked (same posture as BIOS drift, ADR 0018) rather than failing weirdly.

**M0 spike (decides the ceiling):** build the per-adapter capability table —
RetroArch (`--appendconfig` with `savestate_auto_load`/`savestate_auto_save`/`state_slot` scoped to
OpenBox sessions — the known-good path that never mutates the user's RA config), Dolphin, PCSX2,
PPSSPP, RPCS3, ScummVM, mGBA-class cores, Vita3K/Eden/Xemu/Xenia (likely none — document). Where a
native flag doesn't exist, try adapter config override; where impossible, that adapter is
`state: none` and Moments still get screenshots+notes (no resume). Honest matrix in docs; no fake
support claims.

**Implementation tasks**

1. `emulator_defs` schema: `state: {kind: retroarch|adapter-cli|config-override|none, template: …}`
   + `launch_tokens.py` additions only if needed (`{state_path}`).
2. `parity_resume.py`: state capture path conventions, freshness/version stamping, stale detection,
   bounded retention per game.
3. Launch pipeline: `--uri openbox://resume/<id>` and detail-pane Resume both flow through the
   atomic reservation path; launching Start does not destroy a saved resume state until the new
   session itself suspends.
4. `static/moments.js`: timeline tab (thumbnails, notes, jump-in), share-card canvas renderer,
   capture affordances (hotkey + pause overlay + recap button).
5. Settings keys: `quick_resume_enabled` (default on where supported), `moments_autocapture`,
   `state_retention` — registered in `settings_schema.py`, exposed in Settings → Sessions & Saves.

**Acceptance tests**

- RetroArch fixture: suspend → state file + thumbnail exist; Resume relaunches with the slot
  config; the game process observes the injected slot only (no user-config mutation).
- Version drift: bump adapter version → state flagged stale, Resume offers clean Start.
- Moment capture: keyboard, deeplink, and recap paths all produce a timeline entry; share card
  renders offscreen deterministically (test the payload, not pixels).
- Unsupported adapter ⇒ no Resume affordance anywhere, no error spam; shelf/missing-path games
  never offer it.
- `make check` + new `test_*.py` for `parity_resume` at the new-module coverage floor.

**Done means:** a user can leave mid-level on SNES/GBA/PS1-class emulation tonight and land on the
same spot in three weeks — with a photo album of how they got there.

### Workstream T2 — Library Time Machine

**Priority:** flagship. **Risk:** medium (event replay correctness; honest retention story).
**Implementation:** `pkg/parity/parity_library_sync.py` (journal mode), new
`pkg/parity/parity_time_machine.py`, `handlers/data.py`/`library.py` v2 routes,
`static/timemachine.js`.

**Product behavior**

- Tools → Time Machine: a vertical, filterable timeline of everything that ever happened to the
  library — adds, edits (with field-level diffs), deletes, restores, imports, sessions, moments —
  grouped by day, searchable.
- *Browse as of <date>:* read-only library view reconstructed at that instant (nearest snapshot
  base + deterministic event replay; bounded horizon).
- *Per-field revert:* any event offers "restore this field" — the revert is itself a new event
  (event-sourced all the way down; never rewrites history).
- Works **without** sync enabled: `library_journal_enabled` (default on) records the same validated
  events locally; sync remains opt-in on top.
- Honest scope: catalog metadata/history are journaled; media binaries are not (docs say so).

**Implementation tasks**

1. Reuse the 1.10.0 event machinery in "journal-only" mode (no remote folder): event writer,
   validation, and tombstones are already transaction-bound via `openbox.update_state`.
2. `parity_time_machine.py`: snapshot+replay materializer (bounded event window, LRU over dates),
   field-level revert planner producing a new event, retention/compaction policy.
3. Routes (v2): `/api/v2/library/time-machine/events?days=&kind=`, `/as-of?date=`,
   `/revert` (preview→apply discipline).
4. `static/timemachine.js`: timeline UI, day grouping, diff rendering, as-of grid (reuse the
   virtualized grid in read-only mode).

**Acceptance tests**

- Replay correctness: random-mutation fuzz run — materialized as-of state equals a saved fixture
  state at each checkpoint.
- Revert is additive, previewed, and appears on the timeline; concurrent edits during revert
  preview are stale-rejected.
- Journal-off ⇒ zero overhead, zero events; journal survives crash mid-write (atomic event files).
- 20k-game library: timeline paginates; as-of browse doesn't OOM or hang (perf gate fixture).

**Done means:** "what did my library look like in June?" and "undo that edit from Tuesday" are
real answers, not recovery folklore.

### Workstream T3 — Record That: clips & highlight reels

**Priority:** flagship. **Risk:** medium-high (obs-websocket is a new protocol surface; ffmpeg is
optional-if-present). **Implementation:** new `pkg/parity/parity_obs_bridge.py`,
`pkg/parity/parity_reels.py`, `handlers/media.py`, `static/bigbox.js` pause overlay,
`static/recap.js`/moments UI.

**Product behavior**

- *Replay buffer:* when `obs_replay_enabled` and OBS (with obs-websocket) is running, OpenBox arms
  the replay buffer at session start. **"Clip it"** (pause overlay, recap card, `openbox://clip`)
  saves the last N seconds → auto-attached to the game/session as a clip (existing video-media
  plumbing). No OBS ⇒ the button degrades to "capture screenshot" and docs are honest.
- *Highlight reels:* if `ffmpeg` is on `PATH`, generate a montage (clips + moments + cover art,
  crossfades) per game ("this game's story") and a **Wrapped video** — "watch your year" export
  alongside the existing printable Wrapped. No ffmpeg ⇒ export a scrollable HTML reel instead —
  never a hard dependency.
- *Clip gallery:* moments/clips live in one per-game gallery (the Moments tab becomes Moments &
  Clips).

**Implementation tasks**

1. `parity_obs_bridge.py`: minimal stdlib obs-websocket v5 client — HTTP→WS upgrade handshake,
   masked client frames, JSON op-codes (`ToggleReplayBuffer`, `SaveReplayBuffer`,
   `SetRecordDirectory`, identify/hello), socket discovery (`127.0.0.1:4455` default + settings
   override + optional password), bounded timeouts, fail-closed errors. ~200 lines, no deps.
2. Arm/disarm on session lifecycle via `pkg/state/launch.py` hooks; buffer state surfaced in the
   pause overlay ("replay buffer armed").
3. `parity_reels.py`: ffmpeg probe → concat/crossfade pipeline into `reels/<game|year>.mp4`;
   deterministic input manifest; Activity-job execution; ffmpeg-absent ⇒ HTML reel fallback.
4. Clip naming/retention bounded; clips attach via the existing video media field + a `memories`
   sidecar list if S5 lands (else a `clips` list — decide in M0).

**Acceptance tests**

- Mocked websocket server fixture: handshake, identify, save-replay round-trip; OBS-absent,
  auth-required, and dropped-socket paths all degrade cleanly.
- Clip lands in the game's gallery with correct metadata; works from pause overlay and deeplink.
- Reel: fixture clips+images → valid mp4 (probe exit 0) when ffmpeg present; HTML reel otherwise.
- Zero behavior change when the setting is off (no sockets opened, ever).

**Done means:** "did you see that?!" finally has a yes-button — and Wrapped becomes a movie.

### Workstream T4 — Backlog Radio & the "just say it" bar

**Priority:** flagship. **Risk:** low-medium (pure analytics; the risk is taste, not tech).
**Implementation:** new `pkg/parity/parity_radio.py`, `pkg/parity/parity_query.py`,
`handlers/insights.py`, `static/insights.js`, palette work in `static/palette.js` (new),
`static/navigation.js` keymap table refactor.

**Product behavior**

- *Backlog Radio:* a regenerating playlist — "5 games you'll actually finish this week" — scored
  from **your real habits**: session-length distribution per genre/platform, completion vs
  abandonment patterns, time-of-day tendencies, constellation similarity to recently-loved games,
  observed-vs-estimated playtime. Every pick shows its reason ("short · you finish platformers ·
  similar to Celeste"). Refresh weekly or on demand; never two of the same pick in a row.
- *Abandonment radar:* an Insights card — started-then-stalled games that are statistically still
  winnable for you (and gentle "park it" suggestion for the ones that aren't — honesty as a
  feature).
- *"Just say it" bar:* the search field gains a deterministic semi-natural grammar —
  `short unplayed rpg`, `couch co-op for 4`, `90s platformers rated 4+`, `haven't played in a year` —
  compiled to the existing filter/preset rules, with the parsed interpretation shown as editable
  chips (same chip vocabulary as the visual preset builder, ADR 0020). No LLM; output is
  explainable by construction.
- *Command palette* (`Ctrl+K`): ships inside this workstream because it shares the query surface —
  fuzzy games (worker.search.js) + `>`-prefixed actions + settings navigation + generated `?`
  keyboard map.

**Implementation tasks**

1. `parity_radio.py`: habit model from `history` (windowed, decayed), candidate scoring =
   similarity edges × habit-fit × freshness; playlist materialization as a managed filter preset;
   reason-string generation (testable templates, i18n keys).
2. `parity_query.py`: tokenizer → rule AST → existing `game_matches_rules`; grammar documented in
   docs; parse-preview API `POST /api/v2/library/query/parse` returns chips+rules (no mutation).
3. `static/palette.js` + `SHORTCUTS` table refactor; chips-under-search UI.

**Acceptance tests**

- Synthetic histories: the habitual-platformer player gets platformers with true reasons; a
  two-week-old library falls back to rating/edge picks honestly ("not enough history yet").
- Query corpus: ~60 phrase fixtures → expected rules; unparsable input → plain substring search +
  hint, never a wrong filter.
- Palette: identical game results to sidebar search; every action executes its real path; focus
  restore; 20k fixture within the worker budget.

**Done means:** OpenBox gives advice it can defend, in plain words, using nothing but your own
history.

### Workstream T5 — The Arcade Room & Museum mode

**Priority:** flagship showpiece. **Risk:** medium (pure-frontend scope; art/asset discipline).
**Implementation:** new `static/arcaderoom.js` (canvas), `static/bigbox.js` integration, theme
tokens, `handlers/` none (read-only over existing library+media endpoints).

**Product behavior**

- Tools → Arcade Room (also a Big Box layout): a side-scrolling canvas room — one zone per
  platform — where each cabinet/shelf unit wears its games' clear logos/box art as marquees and
  cycles screenshots/video snaps on its screen. Walk with arrows/gamepad; `Enter` opens the
  cabinet's game list; `Play` launches. Themes restyle the room automatically via tokens.
- *Museum mode:* an attract/kiosk evolution — OpenBox idles into a self-guided exhibit: it walks
  the room, stops at cabinets, and shows fact cards (year, dev, genre, "you've played 12h", "3
  achievements left"). A party/cabinet showpiece; any input returns to the room.
- Optional *kiosk lock* (PIN) folds the old "guest mode" idea into this: museum visitors can browse
  and launch, never edit — documented as a convenience boundary, not security.

**Implementation tasks**

1. Canvas renderer with device-pixel-ratio handling, sprite atlas from existing media endpoints,
   bounded texture cache, virtualized cabinet list per zone, `prefers-reduced-motion` ⇒ static
   variant.
2. Gamepad+keyboard+touch walk controls via `navigation.js` edge detection; cabinet → existing
   detail/launch dispatch (`app:show-game`, launch routes).
3. Museum scheduler: idle timer (reuse screensaver timing), fact-card templates (i18n), kiosk PIN
   (salted hash, settings key, adult toggle never cached).
4. New `--arcade-*`/`--museum-*` tokens in `:root` + all five themes — the room is *the* theme
   showcase, so stock themes should visibly differ.

**Acceptance tests**

- Renders at 720p/1080p/4K and inside native WebKitGTK without page errors (UI smoke extension).
- 20k games: texture cache stays bounded; walk stays smooth (measure long tasks, don't promise FPS).
- Empty platform ⇒ tasteful empty zone; missing art ⇒ generated-marquee fallback.
- Museum: facts are real (never fabricated for missing metadata), idle timing honored, input exits.

**Done means:** the release-notes screenshot everyone shares — and the first thing guests see at a
cabinet.

### Workstream T6 — Household: local-first social

**Priority:** conditional flagship (needs only the shipped 1.10.0 catalog transport — not blob
sync). **Risk:** medium (new record semantics). **Implementation:**
`pkg/parity/parity_library_sync.py` record types, new `pkg/parity/parity_household.py`,
`handlers/health.py` sync UI + a Leaderboard panel, `static/household.js`.

**Product behavior**

- Opt-in per device: display name + avatar color; rides the existing device-identity/events
  machinery — **zero servers, zero accounts**.
- *Leaderboard:* members' playtime, completions, and RA counts (shared voluntarily) ranked
  weekly/all-time in a new Insights tab.
- *Challenges:* "Beat Celeste before Sunday", "First to finish a PS1 game this month" — challenge
  cards as sync events → notification center on the other devices → accept/decline → live
  progress → a winner banner + a small trophy history.
- *Hand-me-downs:* recommend a game to the household ("you'd like this because…" — Radio reasons
  reused).

**Implementation tasks**

1. Record kinds: `member`, `challenge`, `challenge_result`, `share` — validated, bounded, tombstoned
   like catalog events; play stats stay opt-in per device.
2. Leaderboard computation purely local from synced records; conflicts = latest valid record.
3. UI: Household section in Settings → Integrations + Leaderboard/Challenges panel; notifications
   for arrivals/results.

**Acceptance tests**

- Two simulated devices: member records converge; a challenge issued on A appears on B, completes,
  and the result converges on A.
- Stats sharing off ⇒ the device emits catalog events only; leaderboard degrades honestly.
- Malformed/oversized records rejected without touching local state.

**Done means:** a Steam Deck and a desktop sharing a Syncthing folder get a friendly rivalry with
literally no infrastructure.

## 4. Supporting track — the connective tissue

Smaller, still user-visible; each exists partly because the flagships need it.

| # | Item | Why it's here | Key files |
|---|---|---|---|
| S1 | **Steam Bridge** (`shortcuts.vdf` preview/apply/remove + `openbox --play <id>` boot-and-launch) | Resume/moments must be reachable from Game Mode; still the #1 handheld ask | `pkg/parity/parity_steam_bridge.py` (new vdf codec: `\x00`-record/int32/string tags, `appid=crc32(exe+name)\|0x80000000`), `handlers/steambridge.py`, `parity_deeplinks.py` |
| S2 | **Session recap card** | the capture surface for moments/clips; progress quick-actions; RA delta | `pkg/state/launch.py` finish payload + SSE, `static/recap.js`, `GET /api/v2/sessions/recap` |
| S3 | **Undo/trash bin** (bounded `trash` state list, toast undo, restore with identity/playlists) | Time Machine UX expects deletions to be visible/revertible | `handlers/library.py` v2 `trash*` routes; v1 remove semantics decided in M0 (default: v1 keeps hard delete, v2 moves to trash) |
| S4 | **SteamGridDB provider** | moments/reels/room all get better when art coverage is better; the provider gap users notice most | `pkg/parity/parity_steamgrid.py` mirroring `parity_screenscraper.py` discipline (env creds, https-only, cache, 429 backoff), v2 routes, metadata+bulk UI |
| S5 | **Memory roots auto-import** (Steam/RetroArch/Dolphin screenshot dirs → per-game gallery, sha256 dedupe, caps) | feeds Moments without manual capture | `pkg/parity/parity_memories.py`, Activity job |
| S6 | **Trophies** (launcher-level achievements: decade tourist, deep diver, curator…) — deterministic rules over library+history, toast + case | cheap delight; the meta-layer Radio/Time Machine already compute | `pkg/parity/parity_trophies.py`, `static/dialogs.js` case dialog |
| S7 | **ES-DE/gamelist.xml import** (conditional) | the other half of Deck migration; preview/apply reuses LaunchBox machinery | `pkg/parity/parity_esde_import.py`, `handlers/imports.py` |
| S8 | Polish essentials: detail-pane tabs (Moments & Clips lives there), quick-filter chips, cover zoom, What's New panel + tips engine | the new features need a home users can find | `static/library.js`, `dialogs.js`, `whatsnew.js` |

Each supporting item inherits the standard bar: v2-only routes, i18n ×5, tokens ×6 surfaces,
module registration + tests, Activity-job for anything long.

## 5. Deliberately deferred / out

- LAN phone companion/remote — needs a real threat model (bind surface, token-in-URL on LAN, rate
  limits); design doc only this release.
- Game streaming, netplay lobbies — revisit after Quick Resume proves the adapter layer.
- Animated-cover downloads, marquee second display, jukebox mode, ROM patcher, No-Intro set
  completion, Game Night tournaments, per-user accounts.
- Flathub submission — maintainer decision; checklist stands ready.

## 6. Milestones

Focused-engineering-day ranges, one developer; re-estimate after the M0 spike.

| M | Deliverable | Depends | Effort |
|---|---|---|---|
| M0 | Baseline green; sweep reconciled; **adapter state-capability spike** (T1 ceiling); obs-websocket spike; decisions resolved | — | 3–5 d |
| M1 | 1.10.1 patch from verified sweep fixes | sweep M1–M4 | 2–4 d |
| M2 | T1 Quick Resume core (RetroArch-path adapters first) + Moments capture/timeline | M0 | 8–14 d |
| M3 | S2 recap + S5 memory roots + S3 trash/undo | M0 | 4–7 d |
| M4 | T2 Time Machine (journal mode → timeline → as-of → revert) | M0 | 7–12 d |
| M5 | T3 Record That (obs bridge → clips → reels) | M2/M3 | 6–10 d |
| M6 | T4 Radio + query bar + palette | M0 | 6–9 d |
| M7 | T5 Arcade Room + Museum + kiosk PIN | M0 | 7–11 d |
| M8 | S1 Steam Bridge + S4 SteamGridDB | M0 | 7–11 d |
| M9 | T6 Household + S6 trophies (conditional) | M4 transport reuse | 5–8 d |
| M10 | S7 ES-DE + S8 polish pack (conditional/remainder) | M0 | 5–8 d |
| M11 | Integration, docs, packaging, RC | all included | 5–8 d |

Core flagships (M0–M8, M11): ~**55–90 days**. With conditionals: ~**65–105 d**. Ship M2–M8 + S1–S6
even if T6/S7 slip — that's still a landmark release.

## 7. Test & release matrix

Everything from the standing gate (`make check`: ruff, runtime-modules, v1-contract, version-sync,
eslint/tsc, i18n 100%, py_compile, coverage floors, ≥95% changed-line, tokens=0) plus:

- **Resume matrix:** each `state:`-capable adapter × (suspend, resume, stale-version, missing file,
  shelf entry, concurrent launch) — real process launches where CI-able, recorded harness elsewhere.
- **Event-sourcing battery:** replay-correctness fuzz; revert-as-event; journal-off zero-overhead;
  crash-mid-write.
- **Protocol battery:** obs-websocket mock (auth, drop, absence); `shortcuts.vdf` fixtures (empty,
  foreign-only, corrupt, giant; Steam running/Flatpak/multi-account); SGDB mock (401/429/https-only).
- **Frontend:** palette result-parity corpus; ~60 smart-query fixtures; room smoke under WebKitGTK;
  reduced-motion + UI-scale sweep over every new surface.
- **Hardware truth:** real handheld session for resume-from-Game-Mode and the room — browser
  automation is not proof.
- Perf gates 10k/20k unchanged + new fixtures: palette search, time-machine pagination, room
  texture bounds.

## 8. Decisions to settle during M0

| Decision | Default | Evidence to change |
|---|---|---|
| Resume adapter ceiling | spike in M0; RetroArch-path first | native flags prove wider support |
| Clips storage field | `clips` list on game record | memories schema subsumes it |
| v1 `remove` vs trash | v1 hard-deletes; v2 moves to trash | contract review shows safe move |
| obs-websocket auth | support password, default off | — |
| ffmpeg reels | optional-if-present, HTML fallback | — |
| Journal default | on (local-only events) | measurable overhead says otherwise |
| Household stats sharing | per-device opt-in | — |
| Kiosk PIN | convenience boundary, documented | — |
| `--play` vs deeplink boot-launch | dedicated flag | deeplink proves sufficient |
| ES-DE media | reference paths, opt-in copy | — |

## 9. Definition of success

A user closes a game mid-fight and returns to that exact frame next week — on the same machine or
their other one. Their library can answer "what did this look like in June?" and undo Tuesday.
"Clip it" exists and works. Their backlog gets advice it can defend, in words, from their own
history. And the release screenshot is a room they can walk through. Everything local, everything
tested, nothing phoned home.

## 10. Primary references

- `AGENTS.md` — module/token/i18n/gate rules this plan is written against.
- `docs/archive/NEXT_UPDATE_PLAN.md` — conventions this document follows.
- `pkg/parity/parity_library_sync.py` + ADRs 0038–0039 — event machinery T2/T6 ride on.
- `pkg/state/launch.py`, `pkg/state/registry.py`, `pkg/state/sse.py` — session lifecycle T1/T3 hook.
- `emulator_defs/`, `pkg/parity/parity_emulator_defs.py`, `launch_tokens.py` — adapter capability table.
- `saves.py`, `pkg/parity/parity_saves.py` — save/state roots and backup discipline.
- `pkg/parity/parity_integrations.py` — capture/OBS conventions T3 extends.
- `pkg/parity/parity_deeplinks.py` — `--play`, `openbox://moment|clip|resume`.
- `pkg/parity/parity_insights.py`, `parity_constellation.py`, `parity_picker.py` — Radio inputs.
- `static/worker.search.js`, `static/navigation.js`, `static/dialogs.js` — palette/query substrate.
- `static/bigbox.js`, `static/party.js`, `pkg/parity/parity_gamescope.py` — room/museum surface.
- `pkg/parity/parity_screenscraper.py` — provider discipline S4 mirrors.
- `pkg/parity/parity_launchbox_import.py`, `pkg/state/imports.py` — merge policy S7 reuses.
- `settings_schema.py` — every new key registers; `v1_contracts.json` — frozen, v2 only.
- `scripts/check_tests.py`, `tests/`, `.github/workflows/` — the gate; `docs/flathub-checklist.md` — boundary.
