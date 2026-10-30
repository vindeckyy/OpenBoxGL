# Reproducible-build report (plan W5-06)

Date: 2026-10-29. Command: `python3 -B scripts/check_reproducible.py --out-dir /tmp/openbox-repro`.
The script builds the macOS bundle twice from a clean output, then compares the two archives:
their SHA-256, their member lists, and the SHA-256 of every member.

## Result

| Property | Value |
|---|---|
| Machine | Apple M4, macOS 27.2 (arm64) |
| Builds | 2, both from `scripts/build_macos_app.sh` on the same tree |
| First archive SHA-256 | `020244d717893c8ec996a651d549d302e1a20a3eae09bd76605f6ca8dd7f99ae` |
| Second archive SHA-256 | `f5d37f4afd486a2b4e3a3f8eaaa8bdce53dea428928f1c8c4a0f647a1c6422b8` |
| Archive size (both) | 44,329,584 bytes |
| Archive bit-identical | No |
| Members compared | 1,968 |
| Members with different content | 0 |
| Members only in one archive | 0 |
| Payload identical | **Yes** |

## What this means

The installed content is reproducible: every file in the bundle, and the file the bundle was
built from, is byte-identical between the two builds. The archive hash still moves, because the
zip records file modification times and the build stamps the bundle. A consumer who verifies the
published `.sha256` still gets the value the release published for that exact file; a consumer
who rebuilds from the same commit will not reproduce that hash.

Nothing in this report is a gate. The release publishes the checksum of the artifact it
published, and `scripts/verify_release.py` checks the download against it.

## Reproducing it

```
python3 -B scripts/check_reproducible.py --out-dir /tmp/openbox-repro --report /tmp/openbox-repro/report.json
```

`--require-identical` fails when the payload differs, which is the property that matters. It is
not used in the release, because the release does not rebuild the bundle from scratch.
