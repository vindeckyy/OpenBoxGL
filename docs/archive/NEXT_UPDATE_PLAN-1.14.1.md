# OpenBox 1.14.1 — Two Platforms, One Library

Status: **folded into 1.15** (2026-09-29). 1.14.1 was never cut as a patch release; it
was archived per the docs-archive convention (ADR 0041) and its remaining items were
re-planned and shipped as 1.15.0. See the execution ledger at the end of this file and
`docs/NEXT_UPDATE_PLAN-1.15.md` (a local-only, untracked plan) for the shipped result. Authored
2026-09-26, one week after 1.13.0 shipped; originally baselined at `71d75ec` and rebased
onto post-1.14.0 `master` (`1b9fcf6`) when 1.14.0 shipped.

1.11 was *Every Second Counts* (features). 1.12 was *Living Library*
(consolidation). 1.13 was the *Windows port* (a new platform). 1.14.1 does
what a first patch on a new platform always needs: **make the new platform
feel finished, and pay down the promises the last two plans deferred.**

Guiding theme: *Two Platforms, One Library.*

This document is a plan, not a commitment. Every item is marked:

- **[verified-live]** — confirmed present in the tree as of `71d75ec`.
- **[verify-first]** — plausible debt inherited from the 1.12 plan; confirm it
  is real before scheduling it, and drop it if it is not.

---

## 0. Release shape

- Version: `1.14.1`, SemVer. **The v1 route surface stays frozen** — all new
  surface goes under `/api/v2/`. `v1_contracts.json` currently pins 61 routes
  and `scripts/check_v1_contract.py` fails the gate on any drift.
- **No new runtime dependencies.** stdlib only, `ctypes` included.
- Every new runtime module → `runtime_modules.txt` (126 entries today) **and**
  a `tests/test_<module>.py`. `scripts/check_runtime_modules.py` is the gate;
  the test file is convention (AGENTS.md).
- Every new visual value → a token in `static/app.css` `:root` **plus** an
  entry in all five stock themes. `scripts/check_tokens.py` is ratcheted to 0.
- Coverage floors go **up**, never down. Current: `COVERAGE_FLOOR = 83.0`,
  `WEB_APP_FLOOR = 58.0`, `CHANGED_LINE_FLOOR = 95.0`, `NEW_MODULE_FLOOR = 85.0`.
- ADRs required for: any new route family, any state-boundary change, any new
  gate, any new sync transport. Next free number is **ADR 0061**.
- Ship one patch. Cut `1.14.1` clean — no scope creep past this plan.

### Current surface (measured at `71d75ec`)

| Thing | Count |
|---|---|
| Runtime modules | 126 |
| Test files | 135 |
| `@route` decorators | 274 |
| Frozen v1 routes | 61 |
| `/api/v2/` routes | ~110 |
| Stock themes | 5 |
| Shipped locales | 5 (de, en, es, fr, pt) |
| Emulator definition YAMLs | 24 |

---

## 1. Headline features (pick 3, do not ship all six)

The rule from the last two plans: three headlines, chosen before work starts.
Everything else is section 2+ polish. If a headline slips, cut it — do not
absorb it into a later patch silently.

### 1.1 Windows reliability rows + a "what's different here" surface

**[verified-live]** `docs/reliability.md` has 23 rows and **not one mentions
Windows, WebView2, or the native host** — verified by grep, zero hits. 1.13
shipped an entire platform and the reliability catalog, which is the document
that claims to catalog failure modes a real user can hit, never learned about
it. Meanwhile ADR 0048 §9 justifies `# pragma: no cover` on Windows branches
*because* "the `windows-latest` CI job is what exercises them" — so the
platform's only safety net is a coverage mechanism, not a documented contract.

This is the highest-value item in the release and it is cheap.

- **Extend `docs/reliability.md` with a Windows section, rows 24+.** Each row
  must be a failure a real Windows user hits, and each must name its test or
  the manual procedure. Candidates, all **[verify-first]** until reproduced:
  - WebView2 runtime absent → launchers fall back to the browser app window,
    and the UI says why instead of silently opening a tab.
  - `native_host.exe` present but blocked (SmartScreen / Defender / Mark of
    the Web) → actionable message, not a silent browser fallback.
  - A game path with spaces *and* non-ASCII → MSVCRT quoting survives. ADR
    0048 already documents that a command written on one platform may not
    round-trip byte-for-byte, so the honest test is "launches correctly", not
    "argv is identical".
  - Long paths (>260 chars) → `\\?\` handling or a clear error.
  - Second instance, same data dir → named-pipe `focus` forwarding works.
  - `openbox://` deeplink from a browser with the app closed → cold start.
  - Update while running → the detached PowerShell applier completes, or
    reports precisely why it did not.
- **Add a `platform` column**, or split into "Both platforms" and
  "Windows-specific". The 23 existing rows are POSIX-flavored; do not
  retro-label them, add a section below.
- **Status discipline:** `Tested` only when a test in
  `tests/test_platform_compat.py` or `scripts/run_windows_tests.py` exercises
  it. `Manual` needs the procedure written **in this release**, not "verify
  before shipping". 1.12.1 already closed the old manual rows; do not reopen.
- **Frontend:** one line in Settings → About stating active platform, resolved
  data directory, and whether the native host or the browser fallback is in
  play. Most new-platform confusion is "where is my stuff" and "why is this a
  browser tab". `handlers/native.py` already reports capabilities — render them.
- **ADR 0060:** the Windows reliability contract, including the rule that the
  native-host-missing path is a supported configuration, not a failure.

Effort: small. Mostly writing, and the writing is the deliverable.

### 1.2 Emulator definition update channel

**[verified-live]** 24 emulator YAMLs ship in `emulator_defs/`, each carrying
a `version:` key, and `pkg/parity/parity_emulator_defs.py` loads them from
disk (`_load_raw_adapters`, `load_definitions`, `load_registry`) with no
channel to receive a *newer* one. A user on a new emulator version has no path
forward but editing YAML by hand.

Highest-leverage feature in the plan: it keeps paying out after 1.14 ships.

- Fetch a community definition pack the way `plugin_catalog.py` already does,
  verify it with the **existing** `openbox-release.pub` Ed25519 key, and apply
  it transactionally.
- **Hard rules**, taken straight from the 1.12.1 hardening:
  - Small-order key rejection (ZIP-215 / libsodium blacklist) already lives in
    `updates.py` — reuse it. Do not write a second verifier.
  - Non-object GitHub payloads fail closed.
  - The digest is normalized before signature comparison.
  - **Never** partially apply: parse, validate, and stage the whole pack, then
    swap under the canonical state boundary, keeping the previous definitions.
  - A pack that fails to verify leaves the bundled definitions untouched and

### 1.3 Story PNG export + Time Machine compare

Two small read-side features that finish a 1.12 promise. Both are projections
over data that already exists, which is the cheapest kind of work.

**[verified-live]** ADR 0047 shipped Game Story as a *projection only, no PNG
export* — the 1.12 execution log says so explicitly.

- **Story PNG export.** `pkg/parity/parity_story.py` already produces the
  timeline (`game_story`); the client renders the same data to canvas and
  `toDataURL()`s it. No new endpoint, no new server code, no new token beyond
  what the dialog already uses. The 1.12 plan correctly rejected a pure-Python
  WebP encoder; canvas PNG is the right answer.
- **Time Machine compare.** `parity_time_machine.py` already has `_diff` and
  `apply_revert` with a validated field whitelist. Add a read-side two-date
  compare: `GET /api/v2/library/time-machine/compare?a=…&b=…` returning added /
  removed / changed per field, rendered beside the existing timeline. The
  journal already holds the data.
- Revert *apply* stays exactly as bounded as ADR 0044 made it (metadata fields
  only, never paths or launch config).
- Tests: extend `tests/test_parity_time_machine.py` and
  `tests/test_parity_story.py`; the compare endpoint gets an HTTP contract test.
- ADR 0060 (small — one read-side route).

Effort: small. Good filler for a week where a headline slipped.

### 1.4 Deck Builder for Game Night

**[verified-live]** `parity_party.py` has `build_party_queue` and the ADR 0031
wheel, but only a live queue — no saved, named, themed decks. Deferred by the
1.12 plan.

- Named deck presets ("90s racers", "co-op only", "8+ players") built on the
  existing deterministic `build_party_queue`, not a second queue engine.
- **The seed travels, not the order.** Share a deck into Household as
  `{seed, players, minutes, filter_predicate}` so a remote member spins the
  same wheel deterministically. Persisting a materialized id list would make
  two devices disagree the moment the library changed — the same reasoning
  that made smart collections store the question and not the answer
  (ADR 0047).
- Reuse `pollGamepads` edge detection; do **not** add a fourth copy (see §3.6).
- Tests: `tests/test_parity_party.py` additions — same seed + same predicate
  produces the same order on two "devices"; a changed library changes the
  result, which is the point.
- ADR 0060.

Effort: medium. Deferred twice; ship it or explicitly cancel it in 1.14.1's
notes. Do not defer a third time.

### 1.5 Presence — "who's playing what right now"

**[verified-live]** Deferred from 1.12 §1.1. Verified absent: no `presence`
string in `static/library.js` or `static/moments.js`, no
`/api/v2/household/presence` route. `parity_household.py` has challenges,
leaderboards, shares, and merge — but nothing that says a person is mid-game.

- `POST /api/v2/household/presence` — heartbeat `{game_id, started_at,
  state}` written as a signed event into the sync folder, ~10 min TTL,
  expired heartbeats ignored on read.
- `GET /api/v2/household/activity` — who is playing what, with elapsed time.
- Must obey every ADR 0046 rule: opt-in, bounded, validated, no account, no
  server. Presence is the most "social" surface in the app and therefore the
  one most likely to leak; it carries a game id and a timestamp and nothing
  else.
- **Cancellation matters here.** A heartbeat that never expires is a privacy
  bug. Ship the TTL and the delete, or do not ship the feature.

Effort: medium-high. This is the one item here I would genuinely consider
cutting from 1.14.1 in favor of §1.2 — a new social surface landing on a new
platform is exactly where local-first guarantees get eroded.


---

### 1.6 Saves sync over the causal transport

**[verify-first]** The 1.12 plan proposed syncing saves through the ADR 0039
transport. `parity_library_sync.py` already references saves. **Confirm whether
the existing transport already carries them** before writing any of this — if
ADR 0039's field allowlist already includes save metadata, this item is a UI
button, not a feature.

If it is real: saves are small, high-value, and the transport already handles
conflicts and tombstones. Add a per-game "Save history" tab (restore + size +
age) using the same bounded-diff pattern as the backup engine.

Effort: unknown until verified. Do not commit to it.

    says so.
- Local definitions win over remote ones for any adapter the user deliberately
  edited. The updater must never clobber a local edit.
- Routes: `GET/POST /api/v2/emulators/defs/update` (+ `.../status`). Do not
  touch `/api/v1/emulators/install`.
- Settings → Emulators: "Check for definition updates", last-checked stamp,
  per-adapter "locally modified" marker.
- **Signature failure is a security event**: write a notification through
  `notifications.py`, never a generic "update failed".
- Tests: `tests/test_parity_emulator_defs_update.py` — valid pack, tampered
  payload, small-order key, non-object response, partial-pack rejection, local
  edit preservation, rollback leaves the old pack in place.
- ADR 0060.

Effort: medium. It is a network path with a signature, and the 1.12.1 bugs
were all in exactly this area — budget for the adversarial tests.

| Frontend modules (`static/*.js`) | 35 |
| ADRs | 48 |
| Reliability rows | 23, **0 of them Windows** |
| Open upstream issues | 0 |

Those last two rows are the thesis of this release.

## 2. Deepen existing features (medium effort, high perceived polish)

### 2.1 What's New has been lying since 1.12

**[verified-live] — a real user-visible bug, fix this first.**

`locales/*.json` hardcodes the version in the What's New title, and it still
says **1.11 in all five locales**:

- `en.json:38` — "What's new in OpenBox 1.11"
- `de.json:38` — "Neu in OpenBox 1.11"
- `es.json:38` — "Novedades de OpenBox 1.11"
- `fr.json:38` — "Nouveautés d'OpenBox 1.11"
- `pt.json:38` — "Novidades de OpenBox 1.11"

Two releases have shipped and the first thing a user opens still advertises
1.11. It also cannot self-correct: the version is duplicated into five
translated strings, and `scripts/check_version_sync.py` checks the README
badge, metainfo, PARITY, SUPPORT, SECURITY, flathub-checklist, and the docs
index — but **not** the locale files. That is why it drifted.

- Move the version out of the translated string: `whats_new.title` becomes a
  format template, and the client composes
  `t('whats_new.title').replace('{version}', version)` using the version the
  server already sends in its payload.
- **Add `locales/*.json` to `scripts/check_version_sync.py`** so this class of
  drift fails the gate next time. This is the real fix; the string edit alone
  just resets the clock.
- While in `whatsnew.js`: the four highlight cards (`say_it`, `record`,
  `arcade`, `import`) and the four rotating tips (`tip_search`, `tip_clip`,
  `tip_time_machine`, `tip_big_box`) are all still 1.11 features. 1.12 and
  1.13 shipped Smart Collections, Game Story, scheduled backups, per-game
  launch options, and Windows — none are mentioned. Refresh both lists.
- The i18n gate scans `t('key')` call sites; a key built dynamically
  (`t(tipKey)` at `whatsnew.js:41`) can drift from `en.json` without the gate
  noticing. See §7.
- Tests: extend the i18n test to assert no locale contains a hardcoded
  `OpenBox 1.x` string.

Effort: very small. Highest ratio of user-visible trust per line in the plan.

### 2.2 Palette prefixes and plugin commands

**[verify-first]** §2.3 of the 1.12 plan asked for a `>` prefix and a `?`
prefix. Verified: `static/palette.js` already handles `>` and already ranks by
recent local counts (`RECENT_KEY`, `rankRecent`) — so **§2.3 of the 1.12 plan
is done**. Remaining:

- The `?` help prefix and the bundled help index.
- **[verify-first]** Plugin palette commands: `plugins.py` has a
  `_plugin_command` function, so the hook may already exist. Confirm before
  building; if present, the remaining work is documenting it.

### 2.3 Constellation: viewpoints, path-finding, PNG export

**[verified-live]** `static/constellation.js` is 11.8 KB and contains no
viewpoint persistence, no path-finding, and no `toDataURL` export. All three
were proposed in 1.12 §2.4 and none landed.

- Save viewpoints (pan/zoom + filters) per user, persisted in `ui_state`
  (which ADR 0005 defines as UI prefs, not state).
- "Path between two games" — highlight the shortest edge chain. Cheap BFS over
  the existing graph payload.
- Export as PNG via the canvas that already exists.
- No new tokens. The graph already uses the theme surface tokens.

### 2.4 Arcade Room: attract mode honesty and kiosk PIN backoff

**[verified-live]** `static/arcaderoom.js` already renders
`'arcade.museum_attract'` / "MUSEUM · ATTRACT MODE", so attract mode partially
exists. **[verify-first]** on the rest.

- Attract mode: idle >5 min in kiosk → slow auto-tour, reusing the Big Box
  video path with the reduced-motion guard.
- Kiosk PIN: add lockout backoff. ADR 0046 is explicit that the Museum PIN is
  "a convenience boundary, not authentication or a security boundary" — an
  exponential in-memory backoff makes the *honest* claim more true, and the
  docs should keep saying the same words.
- QR pairing for a phone remote was proposed in 1.12 §2.5. **Recommend

---

### 2.5 Insights: the "finish it" nudge and cost-per-hour

**[verify-first]** Both from 1.12 §2.7, neither confirmed in the tree.

- "Finish a game" nudge: titles at 80–99% progress get a subtle shelf. High
  conversion, one query over the existing progress data.
- Cost-per-hour card **only if** an optional `price_paid` field exists.
  Verified: `price_paid` appears **nowhere** in `settings_schema.py`,
  `saves.py`, `catalog.py`, or `importers.py`. Adding it means an import path,
  an editor, a migration, and a currency story. **Recommend cutting this from
  1.14.1** — it is a spreadsheet feature wearing a launcher's clothes.

### 2.6 Media hygiene report and a per-platform setup checklist

**[verify-first]** Both from 1.12 §2.9/§2.10, unconfirmed.

- Bulk artwork hygiene: missing covers / low-res / mismatched aspect, with one
  "fix all with SteamGridDB" button over the provider that already exists.
- Per-platform setup checklist: BIOS present (ADR 0018 SHA1 drift detection),
  emulator resolved, one game launches, artwork fetched. Turns "why doesn't
  PS2 work" into a green checklist. High value on a *new platform*, where the
  answer to "why did this fail" is least obvious.

### 2.7 Windows-specific polish

- **Uninstaller.** `install.ps1` installs to
  `%LOCALAPPDATA%\OpenBox\share\openbox` and keeps `share\openbox.previous`,
  but there is no removal path — the uninstall half of ADR 0048 was never
  written, and Windows users will ask. Ship `scripts/uninstall.ps1` (removing
  the tree, the PATH entry, the Start Menu shortcut, and the
  `openbox://` registration) and document it in SUPPORT.md.
- **First-run on Windows:** the Setup Center is a JS surface and should already
  work, but the WebView2-absent path and the `%LOCALAPPDATA%` data dir deserve
  an explicit mention in the wizard.
- **`openbox://` cold start** deserves a real test (see §1.1).

## 3. Correctness & reliability debt

The 1.12 plan's §3 listed ten sweep candidates and closed several with "already
fixed". Below is what is **still open**, re-verified against `71d75ec`.

### 3.1 Calendar-coupled tests will fail again — and the class is wider than one file

**[verified-live] — this already bit the release gate once.**

`71d75ec` (the most recent commit) fixed `test_parity_radio.py`, where a
fixture materialized a playlist at a hardcoded `NOW = 2026-09-12` and then
asserted the handler *reused* it — but the handler judges staleness with
`datetime.now()` against `RADIO_STALE_DAYS = 7`. The test passed for a week
and then would have failed the release gate forever.

That is a bug class, not a bug. The tree is full of window constants that make
the same trap possible:

| Constant | Value | Module |
|---|---|---|
| `RADIO_STALE_DAYS` | 7 | `parity_radio.py` |
| `RADIO_WINDOW_DAYS` | 90 | `parity_radio.py` |
| `RADIO_HALF_LIFE_DAYS` | 30.0 | `parity_radio.py` |
| `AUTO_BACKUP_DAYS` | 7 | `parity_backup.py` |
| `RADAR_STALL_DAYS` | 14 | `parity_insights.py` |
| `RADAR_PARK_DAYS` | 30 | `parity_insights.py` |
| `FRESH_UNPLAYED_DAYS` | 14 | `parity_radio.py` |
| `RECENTLY_LOVED_DAYS` | 60 | `parity_radio.py` |
| `RECENT_DAYS` | 31 | `parity_insights.py` |
| `CO_PLAY_WINDOW_DAYS` | 7 | `parity_party.py` |

`RADIO_STALE_DAYS` was the shortest, so it failed first. The others are time
bombs with longer fuses.

- Audit every test that mixes a **fixed** timestamp with a handler that reads
  **`datetime.now()`**. Either freeze the clock at both ends (inject `now`) or
  materialize the fixture relative to the current time, as `71d75ec` did.
- Thirteen test files call `datetime.now()` / `utcnow()` or pin a literal
  `NOW = "20…"`. Review each against the constant list above.
- **Add a gate** (see §9). Cheapest honest form: fail when a test module pins
  a literal `NOW` *and* the module under test exposes a `_DAYS` constant.
- The deeper fix, if it is cheap: give the window functions an injectable `now`
  parameter defaulting to `datetime.now(timezone.utc)`. Most already accept one.
  Prefer the parameter over the test-side workaround.

This is the highest-leverage item in §3: it stops the release gate from
breaking on a date nobody chose.

### 3.2 A11y on the virtualized grid

**[verified-live]** Grep for `aria-rowcount` / `aria-rowindex` across
`static/*.js` returns **nothing**. A virtualized grid using spacer-window
virtualization (PERF.md §1.7.1) that exposes no row-count or row-index
metadata is opaque to a screen reader: the user hears a stale count that does
not match the rendered window, or none at all.

- `role="grid"` on `#grid`, `aria-rowcount` = the true total (not the rendered
  window), `aria-rowindex` per rendered card, `aria-colcount` / `aria-colindex`
  for card internals — or `aria-setsize` / `aria-posinset` if the list role is
  kept instead.
- Full keyboard pass on the detail pane: focus-trap audit across every dialog.
  1.12 shipped a `closeDialog()` focus stack — verify every dialog uses it
  rather than re-implementing it.
- 18 `aria-label` / `role=` occurrences already exist across `app.js` and
  `library.js`, so the pattern is established; this extends it.
- Gate: a `scripts/ui_smoke.cjs` case asserting the ARIA contract on a 20k
  library. That is the only place it can be checked cheaply.

### 3.3 High-contrast theme

**[verified-live]** Five themes ship; grep for "contrast" across
`themes/*.css` returns nothing. A sixth **High Contrast** theme is the cheapest
possible proof that the token contract (ADR 0004) holds — if a component needs

### 3.4 Reduced-motion audit

**[verified-live]** `static/app.css` contains only **3**
`prefers-reduced-motion` blocks for a 92 KB stylesheet and 35 JS modules. ADR
0046 requires reduced-motion to select a static presentation for the Arcade
Room specifically, but animations live well beyond it.

- Audit every animation: `mood.js`, `bigbox.js` video snaps, the party wheel
  spin (2.4 s `cubic-bezier` per ADR 0031), the constellation intro, the
  Insights heatmap transitions, the What's New card entry.
- Each must be gated by the media query, not by a class toggled once at boot.
- Add `ui_smoke` coverage for the reduced-motion path on at least the wheel
  and the constellation.

  dropping it**: a new remote-control surface over the loopback token, where
  the token lives in the URL. The security review cost is disproportionate.

### 3.5 Sendfile / partial-write audit

**[verified-live]** 1.12 fixed the one real `os.sendfile` truncation bug and
the execution log marked the audit "single sendfile site". Re-verify before
closing: grep for `os.sendfile`, `wfile.write`, and `send_bytes` across the
handlers and confirm every large-media path either resumes or does not use
sendfile. Cheap, and the 1.12 fix means the codebase already has the resume
pattern to copy.

### 3.6 Gamepad polling duplication

**[verified-live]** There are at least two independent `pollGamepads`
implementations: `static/bigbox.js:323` and `static/arcaderoom.js:853` (the
latter a class method). ADR 0031 notes party.js reuses the bigbox edge
detection, so a Deck Builder (§1.4) must reuse it too rather than add a fourth.

- Extract one edge detector into a shared module (`static/input.js` or an
  existing util) consumed by Big Box, Arcade Room, and party.
- Verify the Arcade Room copy is genuinely separate (its canvas may have
  different needs) before deleting it. If it is, document why in a comment
  rather than deleting a working implementation.

### 3.7 SSE reaping and timezone honesty

**[verify-first]** From 1.12 §3.5 and §3.7.

- `pkg/state/sse.py`: confirm clients are reaped on write failure and
  connections close on `pagehide`. A leaked SSE client is a slow resource leak
  in a long-running desktop app.
- Session timestamps must render in local time everywhere (timeline, wrapped,
  story). Mixed-UTC strings are the classic failure. Grep for any `isoformat()`
  reaching the client without TZ normalization.

### 3.8 Plugin sandbox containment

**[verify-first]** From 1.12 §3.9. `plugins.py` has `_sandbox_available` and
`_sandboxed_command`. Confirm `plugin_runner` cannot write outside its declared
directory, has a hard timeout, and that a test proves it. If a test is
missing, write it — this is a security boundary, and "convention, not
gate-enforced" is not good enough for a sandbox.

### 3.9 Cold-start ordering for large libraries

**[verify-first]** From 1.12 §3.6: profile state load for 20k games and confirm
the warm cache is not bypassed on the first write after boot. The 1.11 numbers

---

### 3.10 Docs that are now wrong

**[verified-live] — cheap, and the kind of thing a release should never ship.**

- `docs/PARITY.md:11` carries a Windows platform note, but the capability
  table has no Windows channel rows at all. 1.13 shipped a platform and the
  parity matrix barely mentions it. Add the Windows channel rows.
- `docs/flathub-checklist.md:20` pins screenshots to the **1.11.0** tag and
  carries a literal `TODO: refresh to a 1.13.x tag when artwork changes`. The
  artwork did change. Refresh the URLs to 1.14.1 or fix the tag.
- `docs/development/PERF.md:7` states plainly: "No dedicated measurement run
  was recorded for the 1.12.0 release." The newest tables are 1.11.0. A 1.14.1
  release should record a real measurement run (see §4).
- `docs/reliability.md` is covered by §1.1.

a raw value to stay legible, the token set is wrong, and the theme exposes it.

- `themes/High Contrast.css` overriding `:root` tokens only, per ADR 0004.
- `scripts/check_tokens.py` must stay at 0.
- Real users benefit, and it is a visible, demonstrable release artifact.

## 4. Performance & scale

The perf story is currently **honest but stale**, and that is the main risk:
`docs/development/PERF.md` publishes 1.11.0 numbers while 1.12 turned on the
SQLite read model at 5,000+ games (ADR 0047), so the document's own warning
says the tables no longer describe the default large-library path.

- **Record a real 1.14.1 measurement run** on the release tree:
  `python3 -B scripts/perf_bench.py --sizes 10000,20000 --runs 5`. Replace the
  stale tables. This is the single most valuable performance action available:
  the current document's own caveat undermines every number in it.
- **Re-baseline under SQLite.** 1.12 flipped the default at 5k games but never
  re-measured. The JSON-path tables are now the *non-default* path for large
  libraries and should be labelled as such. Add a SQLite-enabled row.
- **Search-worker facets.** **[verified-live]** `static/worker.search.js`
  contains no `facet` logic — facet counting is still on the UI thread, as the
  1.12 execution log deferred ("facets stay on JSON; search-worker facet move
  deferred"). Move facet counting into the worker for >10k libraries. This is
  the concrete perf item still open from 1.12.
- **Media caching headers.** **[verified-live]** `handlers/media.py` implements
  `Range` and a correct 416 path but has **no ETag / `If-None-Match`**. 1.12
  §4 proposed ETag-by-(mtime,size), ~30 lines. Big Box re-decodes full covers
  today. Add `ETag` + `If-None-Match` → 304 alongside the existing range logic
  and re-run the media tests — range and conditional GET interact.
- **50k tier.** **[verify-first]** `scripts/perf_bench.py` defaults to
  `1000,5000,10000,20000` and gates 10k/20k. ADR 0032 and PARITY both say
  above 20k is exploratory, not release-gated. **Recommend against** adding a
  50k gate: it would formalize an area the project deliberately left ungated.
  Measure it, publish it, do not gate it.
- **Constellation** is the tightest budget: `constellation_build_ms_p95` is
  1500 ms at 20k and the 1.11 sample measured 611 ms. If §2.3 adds path-finding
  to the client, compute it in the worker or accept a jank regression.

---

## 5. Platform & packaging

- **Windows uninstaller** — see §2.7. The install half shipped in 1.13; the
  removal half never existed.
- **Flathub submission** remains a maintainer decision (ADR 0013) and the
  remaining steps are genuinely manual (create the repo, open the flathubbot
  PR, agree a cadence). Do **not** re-litigate this inside the release. Two
  items are ours: re-verify `org.gnome.Platform//49` still ships
  `webkit2gtk-4.1`, and refresh the screenshot URLs (§3.10).
- **Wayland**: verify the WebKitGTK host under fractional scaling and document
  the X11 fallback. Deferred since 1.12; a real Linux papercut.
- **Steam Deck Game Mode smoke test** as a documented manual checklist:
  gamescope preset launch → exit cleanly → back to OpenBox. ADR 0031 still
  carries "Steam Deck manual pass: pending maintainer verification".
- **macOS is out of scope** and SUPPORT.md says so. Keep it out.

---

## 6. Extensibility & API

- **Public plugin API v1 freeze** — document what `plugins.py` exposes (library
  read, palette commands, detail-pane tabs, notification post) and SemVer it.
  **[verify-first]**: `_plugin_command` already exists, so the hook may be
  half-built. Third-party plugins are the main growth lever for a project that
  cannot take runtime dependencies.
- **`docs/api-v2.md` generated from the route registry.** **[verified-live]**

---

## 7. i18n

- Five shipped locales (de, en, es, fr, pt). `scripts/check_i18n.py` enforces
  three rules: every referenced key exists in `en.json`, every `en.json` key
  exists in each locale, and **no locale carries extra keys**.
- Every new string goes through `static/i18n.js`. §2.1 adds a `{version}`
  placeholder to `whats_new.title` in all five locales — a placeholder, not a
  translated version number.
- **Add a gate for dynamically-composed keys.** `whatsnew.js:41` calls
  `t(tipKey)` with a variable. The checker regex matches literal `t('key')`
  call sites, so a key that only ever appears inside the `TIPS` array is
  invisible to it. Extend the checker to also scan string arrays used as i18n
  key sources, or assert `TIPS` membership in `en.json` from a test.
- A 6th locale is welcome if a PR lands; the infra already supports it. Add
  the community-locale contribution section to `docs/CONTRIBUTING.md` that
  1.12 §8 asked for.

---

## 8. Suggested milestone split

### 1.14.1 must-haves

1. **§2.1 What's New version drift** — a real user-visible bug, tiny fix.
   Week one, before anything else.
2. **§1.1 Windows reliability rows** — the platform's missing contract.
3. **§3.1 Calendar-coupling audit + gate** — the class that already broke
   the gate once.
4. **§1.2 Emulator definition update channel** — the feature with the longest
   tail.
5. **§4 Perf re-measurement** — no 1.14 release ships stale perf tables.
6. **§3.10 Doc corrections** — PARITY Windows rows, Flathub screenshots.
7. **§3.3 High-contrast theme** — small, visible, proves the token contract.

### Pick two more from

- §1.3 Story PNG export + Time Machine compare (small, finishes a promise)
- §3.2 A11y grid ARIA contract (real accessibility debt, ungated until now)
- §2.7 Windows uninstaller + polish (closes the install/uninstall asymmetry)
- §1.4 Deck Builder (deferred twice — ship it or cancel it explicitly)

### Stretch, only if everything above is green and committed

- §1.5 Presence
- §2.3 Constellation viewpoints / path / export
- §2.6 Media hygiene report + setup checklist
- §3.4 Reduced-motion audit (**should not be stretch** — see Risks)

### Explicitly cut from 1.14.1

- §1.6 Saves sync — unverified whether the transport already does it.
- §2.5 Cost-per-hour — needs a price field, an import path, a migration, and
  a currency story. Wrong release.
- §2.4 QR phone remote — a new remote-control surface over the loopback
  token. Security review cost exceeds the feature.
- A 50k perf **gate** — measure and publish, do not formalize.
- Flathub **submission** — a maintainer decision, not a code change.

---

## 9. Gates, tests, and the ratchet

**Nothing ships without `make check` green.** Per AGENTS.md, the full gate is
ruff → runtime_modules → v1_contract → version_sync → frontend → i18n →
py_compile → tests under coverage → coverage floors → changed-line →
new-module → tokens.

New gates proposed by this plan (each needs an ADR):

| Gate | Script | Fails when |
|---|---|---|
| Locale version drift | extend `check_version_sync.py` | a locale hardcodes `OpenBox 1.x` |
| Clock coupling | new `check_clock_coupling.py` | a test pins `NOW` against a window constant |
| Emulator def integrity | new `check_emulator_defs.py` | a def lacks `version:` or `native_exe_windows` |

That third one is a nice catch from 1.13: ADR 0048 §7 added
`native_exe_windows` to every definition because adapter detection needs it,
but nothing **enforces** that a newly added YAML carries it. A 25th def without

---

## 10. Risks

| Risk | Likelihood | Mitigation |
|---|---|---|
| The def-update channel repeats a 1.12.1 verifier bug (small-order key, digest normalization) | **High** — that class shipped four bugs in one release | Reuse `updates.py` verification, never re-implement. Adversarial tests are mandatory. |
| Clock-coupled tests break the gate on a future date | **High** — already happened once | §3.1 audit + gate, in week one |
| A new Windows-only bug ships because the reliability catalog is empty | **High** today | §1.1 |
| Perf tables ship stale, contradicting the SQLite default | **High** today | §4 re-measurement is a must-have, not a stretch |
| Scope creep from six headline candidates | Medium | Pick three in week one; the rest are explicitly cut or stretch above |
| Def update clobbers a hand-edited YAML | Medium | Local-wins rule + "locally modified" marker, tested |
| Presence ships without TTL/expiry | Medium | Ship the delete or don't ship the feature |
| Reduced-motion audit treated as stretch | Medium | It is an ADR 0046 obligation; do not defer |

---

## 11. Release-day checklist

Per AGENTS.md and `scripts/check_version_sync.py`, `1.14.1` must be consistent
across **every** one of these surfaces. The version is declared once, in
`updates.py` (`VERSION`), and the gate checks:

1. `updates.py` — `VERSION = "1.14.1"` (the single source)
2. `README.md` — release badge, "Latest stable: v1.14.1", installer `VERSION=`
3. `openbox.metainfo.xml` — latest `<release version="1.14.1">` + description
4. `docs/PARITY.md` — the lead paragraph's `**v1.14.1**`
5. `docs/SUPPORT.md` — `OpenBox 1.14.1`
6. `docs/SECURITY.md` — the `1.14.x` supported line
7. `docs/flathub-checklist.md` — `OpenBox 1.14.1`
8. `docs/README.md` — the shipped-release line
9. `.github/ISSUE_TEMPLATE/bug_report.yml` — the version dropdown
10. `docs/CHANGELOG.md` — a dated `## [1.14.1]` section, **not** stranded in
    `[Unreleased]`
11. `docs/RELEASE_NOTES.md` — the new release at the top, with the download
    table covering both AppImages, the Flatpak bundle, the Windows zip, and
    the standalone native-host exe
12. `locales/*.json` — **new in 1.14**: no hardcoded `OpenBox 1.x` (§2.1)

Then: new runtime modules in `runtime_modules.txt`, a `tests/test_*.py` per
new module, new tokens in `static/app.css :root` **and** all themes, ADRs in
`docs/adr/` for every new route family / gate / contract, and this plan moved
to `docs/archive/` as `NEXT_UPDATE_PLAN-1.14.1.md` with an execution ledger
(per ADR 0041) — the 1.12 and 1.11 plans are archived that way.

---

## 12. What "done" means

- `make check` passes locally and in CI, on Linux and on `windows-latest`.
- Every §3 **[verified-live]** item is closed or explicitly re-deferred with a
  reason in the changelog.
- `docs/reliability.md` describes both platforms, and every row is `Tested` or
  has its manual procedure written down.
- `docs/development/PERF.md` has 1.14.1 numbers measured on the release tree.
- The What's New dialog does not lie about the version — and the gate that
  guarantees it exists.
- Coverage floors went **up**.
- Every deferred item from the 1.12 and 1.13 plans is either shipped or
  explicitly cancelled in the release notes. Nothing is deferred a third time
  in silence.

it would silently break Windows launch for that emulator — the exact regression
class 1.13 was written to prevent.

**Ratchet, do not hold.** Coverage floors go up. Propose
`COVERAGE_FLOOR 83.0 → 84.0` and `WEB_APP_FLOOR 58.0 → 60.0` at the end of the
release, if the tree supports it. Measure before committing to a number.

**Windows coverage.** ADR 0048 §9 justifies `# pragma: no cover` on Windows
branches because the `windows-latest` job exercises them. §1.1's reliability
rows are the human-readable counterpart: a reader should be able to see, per
scenario, what CI proves on Windows.

  `scripts/gen_api_docs.py` exists and defaults to writing `api-v1.md`, but
  **no `api-v2.md` exists in `docs/`**. There are ~110 `/api/v2/` routes and
  no generated index of them. Add a v2 mode and commit the output, so the docs
  stay honest for free. Highest value-per-effort item in this section.
- **Webhook out is already done** — `automation.py` has HMAC signing, SSRF
  guards (`_validate_delivery_url`, `_reject_unsafe_address`, loopback
  handling), retry with backoff, and a drainable queue. The 1.12 plan listed it
  as a maybe; close it as delivered.

show `20k_write_ms_p95` at 537 ms against a 1000 ms budget, so the headroom
exists — this is a measurement task, not necessarily a code task.

---

## Execution ledger

Status: **folded into 1.15** (2026-09-29). 1.14.1 was never cut; everything landed under `[Unreleased]` ships as 1.15.0. Remaining items were re-planned in `docs/NEXT_UPDATE_PLAN-1.15.md`, which lists what was shipped, closed, or explicitly not carried forward.


