# OpenBox release notes

OpenBox 1.17.0 imports installed GOG, EA app, and Ubisoft Connect games on Windows, remembers Game Night decks, records what each game cost, and translates server notices. It also fixes ScreenScraper lookups. Upgrade from any 1.16.x release. The full list is in [CHANGELOG.md](https://github.com/vindeckyy/OpenBoxGL/blob/master/docs/CHANGELOG.md), and the code changes are in [v1.16.1...v1.17.0](https://github.com/vindeckyy/OpenBoxGL/compare/v1.16.1...v1.17.0).

## Your other launchers

Windows now imports games installed by GOG Galaxy, the EA app, and Ubisoft Connect. Open **Tools > Import GOG**, **Import EA app**, or **Import Ubisoft Connect**. The same buttons sit in the Storefront Manager. Importing again matches the launcher's store id, so a second import does not add duplicates.

GOG reads the install entries and the game's own `goggame-*.info` file; its executable runs directly and must stay inside the install folder. The EA app reads its `installerdata.xml` and launches through the EA app. Ubisoft Connect reads its install entries and launches through Ubisoft Connect. A malformed entry is skipped with an error, not a crash.

The import is read-only and offline. Uninstalling the EA app or Ubisoft Connect breaks those launches until the launcher returns.

## Game Night decks

Save the rules for a couch session as a named deck: players, session length, and a filter such as `platform:SNES`. Choosing the deck next time restores those settings. **Save deck** and **Delete deck** sit under the setup controls.

Every draw returns an eight-character seed. On another device with the same library, the same seed and filter produce the same queue.

## What your library cost

Open a game's edit dialog and enter **Price paid** and its **Currency**. Play Insights adds a **What it cost** card with totals by currency, hours played, average cost per hour, and the best- and worst-value games. A game's details show its price and its own cost per hour.

Set the default currency in **Settings > Library**. Bulk Edit can set or clear a price for several games at once. A cost per hour appears only for a priced game with at least a minute of playtime. Totals and prices stay on this device and are not carried by library sync, and nothing is converted between currencies.

## Notices in your language

Cloud sync, launch-check changes, rejected emulator-definition packs, and webhook failures now carry translated titles and bodies in English, Spanish, German, French, and Portuguese. Notifications saved by older versions keep their English text and still render.

## Fixed

- **ScreenScraper lookups returned HTTP 400.** Requests now use the parameter names the API reads (`ssid`, `sspassword`, `softname`, `md5`, `sha1`, `crc`, `romtaille`, and `rom`/`iso`), the Test button calls `ssuserInfos.php`, and refusals sent as plain text are no longer retried. A ScreenScraper developer ID and password are required and are now documented that way.

The [changelog](https://github.com/vindeckyy/OpenBoxGL/blob/master/docs/CHANGELOG.md) lists the main areas, not every change.
