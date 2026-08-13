# Contributing to OpenBox

Thank you for your interest in contributing to OpenBox. This document describes the workflow we expect for issues, pull requests, and local development.

## Code of conduct

Participation in this project is governed by [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md). By contributing, you agree to uphold those standards.

## Before you start

1. Search [existing issues](https://github.com/vindeckyy/OpenBoxGL/issues) to avoid duplicate work.
2. For substantial changes, open an issue first so maintainers can confirm direction.
3. Keep pull requests focused. One logical change per PR is easier to review.

## Development setup

### Requirements

- Linux environment
- Python 3.10 or newer
- Git

### Clone and run

```bash
git clone https://github.com/vindeckyy/OpenBoxGL.git
cd OpenBoxGL
python3 web_app.py
```

Optional local configuration can be loaded from `~/.env` or a project `.env` file. See `.env.example`. Never commit secrets, tokens, or personal credentials.

### Native UI

```bash
python3 openbox.py
```

## Testing

Run the full verification gate before submitting a pull request. This covers lint, compile checks, the full test suite, and coverage floors:

```bash
make check
```

The gate needs dev-only tooling (ruff and coverage) in `.venv-dev`, which `make check` creates on first run. The runtime app itself stays dependency-free.

Run the plain test suite without coverage when iterating:

```bash
./run_all_tests.sh
```

Run an individual module when iterating:

```bash
make test-one TEST=test_catalog.py
```

Check that the version in `updates.py` matches every published spot:

```bash
make version-check
```

Packaging checks:

```bash
./build_appimage.sh
python3 test_packaging.py
```

All tests must pass on CI before a PR can be merged.

## Coding guidelines

- Match the style of surrounding code in each module.
- Prefer focused changes over broad refactors unless the refactor is the purpose of the PR.
- Keep user-facing strings clear and neutral.
- Do not commit ROMs, BIOS images, API tokens, or personal library data.
- Document non-obvious behavior with brief comments only where needed.

### Python

- Target Python 3.10+ syntax and standard library usage already present in the codebase.
- Avoid adding dependencies unless they are required and approved in the PR discussion.
- Use explicit error messages for user-facing validation failures.
- Keep the lint gate green: `.venv-dev/bin/ruff check .` must pass. The gate rule set lives in `pyproject.toml`.
- Any PR that changes `web_app.py` must add or update a test that exercises the touched route.
- Do not lower the coverage floors in `scripts/check_tests.py`; raise them when coverage improves.

### Web UI (`index.html`)

- Preserve existing UI patterns and accessibility conventions.
- Test dialog flows, Big Box navigation, and settings persistence manually when touching frontend logic.

## Pull request process

1. Fork the repository and create a feature branch from `master`.
2. Make your changes with clear commits.
3. Run `make check` (or `./run_all_tests.sh` when coverage is not available locally).
4. Open a pull request using the provided template.
5. Describe the problem, the solution, and the test plan.
6. Address review feedback promptly.

### Commit messages

Use concise, descriptive commit messages in the imperative mood:

```
Add storefront startup auto-import setting
Fix emulator dependency check for missing flatpak
docs: update parity matrix for OBS attach workflow
```

## Plugins

Plugins are optional Python packages installed in the user plugins directory. Each plugin requires:

- `plugin.json` with `id`, `name`, `version`, `entry`, and `hooks`
- An entry Python module that reads JSON from stdin and writes JSON to stdout

Supported hooks:

| Hook | Purpose |
| --- | --- |
| `library` | Observe or transform library state |
| `before_launch` | Adjust launch payload before execution |
| `after_session` | React to completed or stopped sessions |

Example manifest:

```json
{
  "id": "example-plugin",
  "name": "Example Plugin",
  "version": "1.0.0",
  "entry": "main.py",
  "hooks": ["library"]
}
```

Bundled catalog entries live in `plugins/catalog.json` and `plugin_catalog.py`.

## Documentation

Update relevant docs when behavior changes:

- [README.md](README.md) for user-facing overview changes
- [PARITY.md](PARITY.md) for capability status changes
- [CHANGELOG.md](CHANGELOG.md) for release-visible changes
- [openbox.metainfo.xml](openbox.metainfo.xml) and [SECURITY.md](SECURITY.md) when releases or support policy change

Parity-related modules include `parity_import.py`, `parity_premium.py`, `parity_storefront.py`, `parity_discovery.py`, `parity_media.py`, `parity_saves.py`, `parity_integrations.py`, `parity_gamescope.py`, and the other `parity_*.py` helpers.

## Documentation site

The public documentation is published at [openboxgl.github.io](https://openboxgl.github.io/). Site source and content changes belong in the [OpenBoxGL Pages repository](https://github.com/OpenBoxGL/openboxgl.github.io); run `bun install --frozen-lockfile` and `bun run test` there before pushing.

## Security issues

Do not open public issues for security vulnerabilities. Follow [SECURITY.md](SECURITY.md).

## Licensing

By contributing, you agree that your contributions will be licensed under the [GNU Affero General Public License v3.0](LICENSE).
