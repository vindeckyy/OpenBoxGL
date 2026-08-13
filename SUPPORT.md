# Support

## Supported platforms

OpenBox targets Linux. The maintainers test these environments per release:

| Environment | Status |
|---|---|
| Ubuntu LTS (two most recent) | Tested |
| Fedora (latest stable) | Tested |
| Arch Linux | Tested |
| SteamOS / Steam Deck | Tested (gamescope guest mode) |
| Other glibc distributions | Best effort |

## Supported runtimes

- Python 3.10 or newer (CI runs 3.10 and 3.12)
- Chromium-family browsers get the chrome-less app window; Firefox opens a separate window; no compatible browser falls back to the default browser

## Reporting problems

Open **Settings -> Library Audit** and use the diagnostic log, or paste the error banner's "Copy details" output (it includes a request id maintainers can correlate with the log).

Bug reports should include: OpenBox version, distro and desktop, Python version when running from source, the diagnostic report from `/api/diagnostic` (Settings), and steps to reproduce.

## Known behavior

See `docs/reliability.md` for the full edge case catalog. Highlights:

- Games you launch keep running after OpenBox exits unless you close them from the Running panel.
- OpenBox data lives in `~/.local/share/openbox-game-launcher/`; deleting `library.json` resets the library while media files stay.
- The web UI is local-only. Sharing the token in the URL with another machine is equivalent to handing over control of the instance.
