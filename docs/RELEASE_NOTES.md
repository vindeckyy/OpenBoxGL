# OpenBox release notes

OpenBox 1.17.0 brings installed GOG, EA app, and Ubisoft Connect games into the library on Windows, remembers Game Night decks, records what each game cost, and translates server notices. It also fixes ScreenScraper lookups. Upgrade from any 1.16.x release. The full list is in [CHANGELOG.md](https://github.com/vindeckyy/OpenBoxGL/blob/master/docs/CHANGELOG.md), and the code changes are in [v1.16.1...v1.17.0](https://github.com/vindeckyy/OpenBoxGL/compare/v1.16.1...v1.17.0).

## Your other launchers

Windows now imports games installed by GOG Galaxy, the EA app, and Ubisoft Connect. Open **Tools > Import GOG**, **Import EA app**, or **Import Ubisoft Connect**. The same buttons sit in the Storefront Manager. Importing again finds the same records by their store id, so no duplicates are added.

- **GOG** reads the install entries and the game's own `goggame-*.info` file. The game executable runs directly; an executable outside the install folder is refused.
- **EA app** reads the launcher's `installerdata.xml` and launches through the EA app.
- **Ubisoft Connect** reads the launcher's install entries and launches through Ubisoft Connect.

Import is read-only and offline. A malformed entry is skipped with an error, not a crash. Uninstalling the EA app or Ubisoft Connect breaks those launches until the launcher comes back.

## Game Night decks

Save the rules for your couch night as a named deck: players, session length, and a filter such as `platform:SNES`. Pick the deck next time and OpenBox fills the setup back in. **Save deck** and **Delete deck** sit under the setup controls.

Every draw returns an eight-character **seed**. On another device with the same library, entering the same seed and filter produces the same queue. The wheel, up-next strip, and launch flow are unchanged.

## What your library cost

Open a game's edit dialog and enter **Price paid** and its **Currency**. Play Insights adds a **What it cost** card: totals by currency, hours played, average cost per hour, and the best-value games. A game's details show its price and its own cost per hour.

- Set the default currency in **Settings > Library**.
- Bulk Edit can set or clear a price for many games at once.
- Cost per hour appears only for a priced game with at least a minute of playtime.
- Totals are grouped by currency. Nothing is converted, and no exchange rate is downloaded.

Prices and store ids stay on this device for now; they are not carried by library sync.

## Notices in your language

Cloud sync, launch-check changes, rejected emulator-definition packs, and webhook failures now use translated titles and bodies in English, Spanish, German, French, and Portuguese. Notifications saved by older versions keep their English text and still render.

## Fixed

- **ScreenScraper lookups returned HTTP 400.** Requests now use the parameter names the API reads (`ssid`, `sspassword`, `softname`, `md5`, `sha1`, `crc`, `romtaille`, and `rom`/`iso`), the Test button calls `ssuserInfos.php`, and refusals sent as plain text are not retried. A ScreenScraper developer ID and password are required and are now documented that way.

## Safer by default

Native storefront discovery is read-only and offline. Provider ids must be digits, install folders must exist, and a GOG executable taken from a game manifest must stay inside its install folder. Commands are argument lists, never a shell. No launcher is modified and no account is used.

## Known limits

- Xbox PC package scanning and owned-but-uninstalled GOG/EA/Ubisoft catalogs are not imported. Native storefront imports are manual; there is no startup auto-import for them.
- Provider ids, price paid, and Game Night decks are local. They do not cross library sync, and Time Machine does not journal price changes.
- Cost totals are not converted between currencies.
- GOG games launch their executable directly, so Galaxy overlay and cloud-save features are not started.
- Other server responses and some dialog labels remain English.
- Windows ARM64 and macOS remain out of scope.
