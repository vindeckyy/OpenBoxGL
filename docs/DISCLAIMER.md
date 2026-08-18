# Legal and third-party reference policy

## 1. Non-affiliation and independent development

**OpenBox Game Launcher is an independent open-source project. It is not affiliated, associated, authorized, endorsed by, or officially connected with LaunchBox, Unbroken Software, LLC, the Openbox window manager project, or any of their subsidiaries or affiliates.**

- The official LaunchBox application and website are located at [https://www.launchbox-app.com](https://www.launchbox-app.com).

### Separate from the Openbox window manager

OpenBox Game Launcher is unrelated to [Openbox](https://openbox.org/), the open-source Linux window manager. The projects have different maintainers, codebases, and purposes. OpenBox Game Launcher is a game library manager and launcher; it does not replace or modify the Openbox window manager.

This policy describes project identity and project practice. It is not legal advice and does not determine whether a particular use is lawful. Consult qualified counsel for jurisdiction-specific questions.

---

## 2. Third-party names and references

All product names, logos, brands, service marks, trademarks, and registered trademarks cited within this project, repository, documentation, and software are the property of their respective owners.

- **LaunchBox** and **Big Box** are registered trademarks of Unbroken Software, LLC.
- **Steam** and the Steam logo are trademarks and/or registered trademarks of Valve Corporation.
- **Epic Games**, **GOG**, **EA**, **Ubisoft**, **Amazon Games**, and **Lutris** are trademarks or registered trademarks of their respective holders.
- **Nintendo** (NES, SNES, N64, Game Boy, GBA, GameCube, Wii, Wii U), **Sony** (PlayStation, PCSX2, RPCS3, PSP), **Microsoft** (Xbox, xemu), and **Sega** are registered trademarks of their respective owners.
- **RetroArch**, **Dolphin**, **PCSX2**, **RPCS3**, **PPSSPP**, **Cemu**, **MAME**, and other emulators belong to their respective open-source project maintainers and developers.

References to third-party products, platforms, services, and compatibility targets are used to identify interoperability, supported workflows, or product comparisons. They do not constitute or imply endorsement, sponsorship, affiliation, or recommendation by OpenBox or its maintainers. See [TRADEMARKS.md](TRADEMARKS.md) for the project's usage rules.

---

## 3. Content and anti-piracy policy

OpenBox does not distribute, host, or bundle:

1. **Bundled games or ROMs**: The repository and release artifacts contain no game files, ROMs, ISOs, or game binaries. No cover art or game files are vendored in the repository. Showcase and documentation images are user supplied captures of user supplied games.
2. **System firmware or BIOS**: The repository and release artifacts contain no proprietary console BIOS images, system firmware, or decryption keys. Import and save discovery merely read paths the user configured. Missing BIOS is reported as a dependency check, not silently fetched.
3. **DRM circumvention**: OpenBox does not bypass, tamper with, or circumvent digital rights management, encryption, or access controls. It launches software through user-configured launchers and emulators with tokenized commands that do not perform shell interpolation. User supplied files remain on the user's machine.

Contributions that add an import path, emulator profile, or sample must not include a copyrighted file, key, or bypass. See [TRADEMARKS.md](TRADEMARKS.md) and the Developer Certificate of Origin in [CONTRIBUTING.md](CONTRIBUTING.md).

---

## 4. Third-party data and public metadata compliance

1. **Public metadata and open endpoints**: Metadata, box art, wallpapers, and game descriptions fetched via public API integrations or database synchronization remain the property of their respective creators and service providers (including LaunchBox Games Database and RetroAchievements.org).
2. **User responsibility**: Users of OpenBox are responsible for complying with the terms of service, acceptable use policies, and API rate limits of any third-party platforms accessed through this software.

---

## 5. Formal notices and designated agent

Do not post legal notices, takedown requests, or private contact information in a public GitHub issue, discussion, or pull request. GitHub is the host and the designated agent for content stored on GitHub.

- For copyright complaints about content hosted on GitHub, use GitHub's [copyright claims form](https://github.com/contact/dmca) and follow GitHub's published [DMCA process](https://docs.github.com/en/site-policy/content-removal-policies/dmca-takedown-policy). The project has no separate DMCA agent for GitHub hosted content.
- For trademark complaints about misleading use of a name, logo, or brand on GitHub, use the [GitHub Trademark Policy](https://docs.github.com/en/site-policy/content-removal-policies/github-trademark-policy).
- For security vulnerabilities, use the repository's [private security advisory form](https://github.com/vindeckyy/OpenBoxGL/security/advisories/new) as described in [SECURITY.md](SECURITY.md).
- For documentation site concerns, use the same GitHub forms for the Pages repository, or open an issue that describes the page without pasting private data.

The project cannot determine the legal validity of a complaint. Formal notices should use the applicable GitHub process or qualified legal counsel. See also the site policy at [openboxgl.github.io/policies/dmca/](https://openboxgl.github.io/policies/dmca/) for the documentation site.

---

## 6. Limitation of liability and warranty disclaimer

This software is provided under the **GNU Affero General Public License v3.0** "AS IS", without warranty of any kind, express or implied, including but not limited to the warranties of merchantability, fitness for a particular purpose, and non-infringement. In no event shall the authors or copyright holders be liable for any claim, damages, or other liability arising from, out of, or in connection with the software or the use or other dealings in the software.
