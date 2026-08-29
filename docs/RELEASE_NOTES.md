# OpenBox v1.7.1

1.7.1 is a small polish release on top of 1.7.0. It keeps everything from 1.7.0 and just makes it feel finished, especially if you have a large library.

## Play Insights

There is a new insights view that is local only. It reads the history and games you already have and shows a 366 day heatmap, your current and longest streak, your top platforms and genres, and how the last 30 days compares to the previous 30. It is two new endpoints, `GET /api/v2/insights/summary` and `/heatmap`, and it runs entirely on your machine. Even with 20,000 history entries the heatmap builds in about 15 ms.

## Performance

The library grid now virtualizes with spacers and observes when to expand, and search can run in a worker with a fallback to the main thread. The facet cache is bounded and the write path is coalesced so 20,000 games stays smooth. The existing 10k and 20k gates still run in CI.

## Setup polish

Setup now shows a human message in the preview like "Found 342 games - 12 need your pick" so you know where you are. The same preview guards as before are still there, just with clearer copy.

## Launch Doctor polish

Every blocking check now includes a `fix_action` so the UI can show a real button. That can be `flatpak_install` to install the emulator, `reveal_bios_path` to show where the BIOS should go, `pick_core` to choose a core or adapter, or `explain_token` to explain a bad launch token. The detail pane renders these with platform chips for ambiguous cases.

## Frontend

Nine new tokens were added for the insights cards and heatmap, and all five stock themes were updated. The insights panel lazy loads and includes a table fallback for screen readers.

Thanks for testing 1.7.0. The full changelog is at https://github.com/vindeckyy/OpenBoxGL/compare/v1.7.0...v1.7.1.
