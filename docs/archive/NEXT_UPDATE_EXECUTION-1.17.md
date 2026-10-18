# OpenBox 1.17.0 — execution record

Status: released as `v1.17.0` on 2026-10-18. PR #57 merged; tag pushed from `66fb68d`.

This is the execution record for the approved 1.17.0 plan. It records what was verified, on which platform, and how the release was cut.

## Shipped

- Windows native storefront imports for GOG, EA app, and Ubisoft Connect: registry/manifest discovery, digit-only IDs, GOG executable containment, provider-ID merge, and `/api/v2/import/{gog,ea,ubisoft}`.
- Game Night decks: named `{name, query, players, minutes}` decks, 20-deck bound, filtered draws, 8-hex seeds, and reproducible order.
- Price paid/cost per hour: integer cents, per-game currency, bulk set/clear, currency setting, spend card, detail facts, export, and restore diff.
- Translated server notices: `title_key`/`body_key`/`params` with English fallback for cloud sync, launch audit, definition rejection, and webhook failure.
- Documentation and gates: version 1.17.0, changelog, release notes, What's New, PARITY, reliability rows 87-97, ADRs 0073-0076, regenerated contracts/API docs, and the 1.16.1 correction notes.

## Verified

| Check | Result |
|---|---|
| Linux full gate (`make check`) | 188 test files, 0 failed; coverage 87.0%, `web_app.py` 74.0%, new module 99% |
| Changed-line coverage vs `origin/master` | 548/549 = 99.8% (floor 95%) |
| UI smoke (`scripts/ui_smoke.sh`, headless Chrome) | Desktop and Big Box passed; deck saved, filter restored, seed shown |
| Performance gate (`perf_bench.py --sizes 10000,20000 --runs 2`) | Passed |
| Windows Python 3.12 under Wine | New importer, platform-compat, and import-route tests pass; real `winreg` GOG/Ubisoft end-to-end tests pass |
| AppImage (`build_appimage.sh`) | Built locally (unsigned); new module and locales included; booted headless and `GET /api/v2/party/decks` returned `{"decks": []}` |
| GitHub CI, draft PR #57 | All checks green, including `windows-latest` (188/188 test files), Linux gates, UI smoke, perf, Flatpak validate, CodeQL |
| Contract ratchets | 316 routes, 111 settings, 42 definitions, 42 frontend modules, 97 reliability rows |
| Version sync / i18n | Pass; 100% key coverage in five locales |
| Release workflow | [v1.17.0](https://github.com/vindeckyy/OpenBoxGL/releases/tag/v1.17.0) published with 20 assets: both AppImages, Windows zip, native host, Flatpak, SBOMs, installers, and signatures |
| Release smoke | [Run 39161082614](https://github.com/vindeckyy/OpenBoxGL/actions/runs/39161082614): Linux x86_64, Linux aarch64, and Windows all passed; desktop screenshots inspected |
| Signed definition pack | `community-defs.tar.gz` verified against `community-defs.tar.gz.sig` with `openbox-release.pub` |

Wine ran 177/188 test files. The 11 failures are Wine environment gaps, not new-code failures: Unix symlinks are invisible to Windows `is_symlink()`, `git`/PowerShell executables are absent, the Linux `native_host` fixture is absent, and Wine cannot exercise the Windows updater test. The real Windows CI job passed all 188 files.

The UI smoke's 20k-game search bound was raised from 20 ms to 50 ms after a shared CI runner measured 21.3 ms (local: 5.1 ms). The bound still catches an algorithmic regression.

## Release record

- `release/1.17.0` merged to `master` as PR #57 (`79ce8a3`); tag `v1.17.0` points at `66fb68d`.
- The owner authorized the tag and publish without the real-launcher desktop pass. The importer is verified by fixtures, the real `windows-latest` suite, and real-`winreg` end-to-end tests; rows 87-92 in `docs/reliability.md` remain the manual follow-up for a desktop with the three launchers installed.
- One release-workflow bug was fixed during the cut: `ldd --version | head -1` tripped SIGPIPE under `pipefail`; it now uses `sed -n '1p'`.
