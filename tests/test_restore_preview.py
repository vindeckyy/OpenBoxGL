#!/usr/bin/env python3
"""F2 -- the restore preview, on the diff that feeds it.

`diff_manifests()` has shipped since 1.7.2 with three id lists and a settings
flag, and nothing in the UI has ever read it. F2's whole value rests on one
rule -- *the restore button does not exist until a diff has succeeded* -- and on
the diff being honest about what a restore would do, which the 1.7.2 vocabulary
is not: `added` is what the current library has that the backup lacks, which is
precisely what a restore would **delete**.

So these tests pin the restore-relative block against a real `create_backup`
fixture, the truncation contract on a library large enough to trip it, and the
settings line that the old three keys could not express.
"""

import sys
import tempfile
import zipfile
from pathlib import Path


def _repo_root() -> Path:
    candidate = Path(__file__).resolve().parent
    if (candidate / "runtime_modules.txt").is_file():
        return candidate
    if (candidate.parent / "runtime_modules.txt").is_file():
        return candidate.parent
    return candidate


ROOT = _repo_root()
sys.path.insert(0, str(ROOT))

from pkg.parity.parity_backup import (  # noqa: E402
    DIFF_FIELDS,
    DIFF_PREVIEW_LIMIT,
    create_backup,
    diff_manifests,
)


def _state(n, *, offset=0, settings=None):
    games = [
        {
            "game_id": f"game-{index:04d}",
            "name": f"Game {index}",
            "platform": "PC",
            "play_count": index,
        }
        for index in range(offset, offset + n)
    ]
    return {"games": games, "settings": settings if settings is not None else {"locale": "en"}}


def _archive(tmp, state, items=("library",)):
    return create_backup(Path(tmp), state, list(items), keep=0)


def test_restore_block_names_what_the_restore_does():
    """`will_remove` must be the current-only set. Getting this backwards is the
    whole failure: the user is told "12 added" while the restore deletes 12."""
    with tempfile.TemporaryDirectory() as tmp:
        # Backed up: the 10 originals plus two that were since deleted.
        backup = _state(10)
        backup["games"] += _state(2, offset=200)["games"]
        archive = _archive(tmp, backup)
        # In the library now: 7 of the originals (3 were deleted) plus 3 new ones.
        current = _state(10)
        current["games"] = current["games"][:7] + _state(3, offset=100)["games"]
        result = diff_manifests(current, archive)

        assert result["restore"]["will_remove"]["total"] == 3, result["restore"]
        assert {row["game_id"] for row in result["restore"]["will_remove"]["rows"]} == {
            "game-0100", "game-0101", "game-0102",
        }
        assert result["restore"]["will_add"]["total"] == 5, result["restore"]
        assert {row["game_id"] for row in result["restore"]["will_add"]["rows"]} == {
            "game-0007", "game-0008", "game-0009", "game-0200", "game-0201",
        }
        # The 1.7.2 set names are the same numbers under their own vocabulary.
        assert result["summary"]["added"] == 3
        assert result["summary"]["removed"] == 5


def test_rows_carry_a_display_name_not_a_bare_id():
    """`game-0100` is what a restore would take away; `Game 100` is what the user
    recognises. The old contract returned ids only, so the UI could not render a
    preview at all without a second round trip."""
    with tempfile.TemporaryDirectory() as tmp:
        archive = _archive(tmp, _state(4))
        current = _state(4)
        current["games"] = _state(1, offset=900)["games"]
        result = diff_manifests(current, archive)
        row = result["restore"]["will_remove"]["rows"][0]
        assert row["game_id"] == "game-0900"
        assert row["name"] == "Game 900", row


def test_changed_rows_report_from_and_to_per_field():
    """A count of '2 changed' is not a preview. This is the per-field detail the
    time-machine compare view already renders, and it is the only thing that
    makes an overwrite reviewable."""
    with tempfile.TemporaryDirectory() as tmp:
        archive = _archive(tmp, _state(4))
        current = _state(4)
        current["games"][0]["play_count"] = 41
        current["games"][1]["platform"] = "Switch"
        result = diff_manifests(current, archive)

        assert result["restore"]["will_change"]["total"] == 2
        by_id = {row["game_id"]: row for row in result["restore"]["will_change"]["rows"]}
        assert by_id["game-0000"]["fields"]["play_count"] == {"from": 0, "to": 41}
        assert by_id["game-0001"]["fields"]["platform"] == {"from": "PC", "to": "Switch"}
        # A field that did not move is not in the row, and neither is a field
        # outside the compared set.
        assert "platform" not in by_id["game-0000"]["fields"]
        for row in result["restore"]["will_change"]["rows"]:
            assert set(row["fields"]) <= set(DIFF_FIELDS)


def test_lists_are_capped_and_the_totals_are_not():
    """A 20,000-game restore used to send 20,000 ids and a UI that drew them all.
    The lists are capped; `total` and `truncated` are the honest numbers, and a
    capped list that does not announce itself is worse than no list."""
    with tempfile.TemporaryDirectory() as tmp:
        archive = _archive(tmp, _state(10))
        current = _state(10)
        current["games"] = _state(600, offset=1000)["games"]
        result = diff_manifests(current, archive, limit=25)

        assert len(result["restore"]["will_remove"]["rows"]) == 25
        assert result["restore"]["will_remove"]["total"] == 600
        assert result["restore"]["will_remove"]["truncated"] is True
        # The 1.7.2 keys are capped by the same limit and say so.
        assert len(result["added"]) == 25
        assert result["truncated"]["added"] is True
        assert result["truncated"]["limit"] == 25
        # The other two sets were empty, so they must not claim truncation.
        assert result["truncated"]["removed"] is False
        assert result["truncated"]["changed"] is False
        assert result["summary"]["added"] == 600


def test_under_the_cap_nothing_is_marked_truncated():
    """The truncation flag has to be false in the ordinary case, or the UI would
    show 'showing 5 of 5' forever and the signal would be noise."""
    with tempfile.TemporaryDirectory() as tmp:
        archive = _archive(tmp, _state(5))
        current = _state(2)
        result = diff_manifests(current, archive)
        assert result["restore"]["will_remove"]["total"] == 0
        assert result["restore"]["will_add"]["total"] == 3
        assert result["restore"]["will_add"]["truncated"] is False
        assert len(result["restore"]["will_add"]["rows"]) == 3
        assert result["truncated"] == {
            "added": False, "removed": False, "changed": False, "limit": DIFF_PREVIEW_LIMIT,
        }


def test_settings_line_distinguishes_manifest_from_archive():
    """Restoring `settings.json` moves save paths and emulator profiles. The old
    `settings_changed` boolean could not say whether a restore would touch
    settings at all, which is the question the user actually has."""
    with tempfile.TemporaryDirectory() as tmp:
        archive = _archive(tmp, _state(4, settings={"locale": "en"}), items=("library", "settings"))
        result = diff_manifests(_state(4, settings={"locale": "fr"}), archive)
        settings = result["settings"]
        assert settings["restored"] is True, "a manifest listing settings means a restore rewrites them"
        assert settings["present"] is True
        assert settings["would_change"] is True
        assert settings["differs"] is True
        assert result["settings_changed"] is True
        assert settings["keys_changed"] == ["locale"]
        assert settings["redacted_secrets"] is True, (
            "create_backup() redacts secrets, so the UI must be able to say the "
            "stored credentials survive the restore"
        )


def test_a_library_only_backup_does_not_claim_settings_move():
    """The preview's warning must follow the archive, not a habit.

    `library.json` is a whole-state export, so a library-only backup still carries
    a settings block and it can genuinely differ. But `restore_backup()` resolves
    items from the manifest, and `settings` is not in it -- so warning about
    settings here would describe a restore that cannot happen."""
    with tempfile.TemporaryDirectory() as tmp:
        archive = _archive(tmp, _state(4, settings={"locale": "en"}), items=("library",))
        result = diff_manifests(_state(4, settings={"locale": "fr"}), archive)
        assert result["settings"]["restored"] is False
        assert result["settings"]["present"] is False
        assert result["settings"]["would_change"] is False, (
            "a library-only restore cannot move settings, whatever the exported "
            "settings block says"
        )
        # The underlying comparison is still reported, under its own name.
        assert result["settings"]["differs"] is True
        assert result["settings_changed"] is True


def test_a_manifest_that_promises_settings_without_the_file_is_reported():
    """A truncated or half-written archive lists settings and has none. Restore
    would act on the manifest, so the preview has to say the payload is missing
    rather than reporting a clean settings diff."""
    with tempfile.TemporaryDirectory() as tmp:
        archive = _archive(tmp, _state(4, settings={"locale": "en"}), items=("library", "settings"))
        rewritten = Path(tmp) / "half.zip"
        with zipfile.ZipFile(archive) as src, zipfile.ZipFile(rewritten, "w") as dst:
            for info in src.infolist():
                if info.filename == "settings.json":
                    continue
                dst.writestr(info, src.read(info.filename))
        result = diff_manifests(_state(4, settings={"locale": "fr"}), rewritten)
        assert result["settings"]["restored"] is True
        assert result["settings"]["present"] is False
        assert result["settings"]["would_change"] is False, (
            "there is no settings payload to write, so nothing would change"
        )


def test_an_identical_library_previews_as_a_no_op():
    """The honest answer has to be reachable: if nothing differs, the UI says so
    and does not invent rows to fill the space."""
    with tempfile.TemporaryDirectory() as tmp:
        state = _state(6)
        archive = _archive(tmp, state)
        result = diff_manifests(state, archive)
        restore = result["restore"]
        assert restore["will_remove"]["total"] == 0
        assert restore["will_add"]["total"] == 0
        assert restore["will_change"]["total"] == 0
        assert restore["will_remove"]["rows"] == []
        assert result["summary"] == {"added": 0, "removed": 0, "changed": 0, "total": 6}


def test_a_broken_archive_still_raises_rather_than_previewing_nothing():
    """A diff failure has to stay a failure. If this degraded to an empty
    preview the UI would arm a restore over an archive it could not read."""
    with tempfile.TemporaryDirectory() as tmp:
        broken = Path(tmp) / "broken.zip"
        broken.write_bytes(b"not a zip")
        try:
            diff_manifests(_state(3), broken)
        except ValueError as error:
            assert "invalid" in str(error).lower()
        else:
            raise AssertionError("a corrupt archive must not produce a preview")


def test_a_game_with_no_title_falls_back_to_its_id():
    """Imports produce records with neither `name` nor `title`. A row that renders
    as empty is the same as a row that was never there."""
    with tempfile.TemporaryDirectory() as tmp:
        archive = _archive(tmp, _state(2))
        current = _state(2)
        current["games"].append({"game_id": "orphan-1"})
        result = diff_manifests(current, archive)
        row = next(r for r in result["restore"]["will_remove"]["rows"] if r["game_id"] == "orphan-1")
        assert row["name"] == "orphan-1"


def test_every_compared_field_is_reported_when_they_all_differ():
    """A guard on the comprehension itself: a field added to DIFF_FIELDS but not
    threaded through the detail rows would silently drop out of the preview."""
    with tempfile.TemporaryDirectory() as tmp:
        archive = _archive(tmp, _state(1))
        current = _state(1)
        for field in DIFF_FIELDS:
            current["games"][0][field] = f"changed-{field}"
        result = diff_manifests(current, archive)
        row = result["restore"]["will_change"]["rows"][0]
        assert set(row["fields"]) == set(DIFF_FIELDS), (
            f"only {sorted(row['fields'])} reported, expected all of {sorted(DIFF_FIELDS)}"
        )
        for field, values in row["fields"].items():
            assert values["to"] == f"changed-{field}"
            assert values["from"] != values["to"]


def run_all_tests():
    tests = sorted(
        (name, obj)
        for name, obj in list(globals().items())
        if name.startswith("test_") and callable(obj)
    )
    failures = 0
    for name, test in tests:
        try:
            test()
            print(f"PASS {name}")
        except AssertionError as error:
            print(f"FAIL {name}: {error}")
            failures += 1
        except Exception as error:  # noqa: BLE001 - the harness reports, never raises
            print(f"ERROR {name}: {error!r}")
            failures += 1
    if failures:
        print(f"\n{failures} test(s) failed")
        return 1
    print(f"\nALL PASS ({len(tests)} tests)")
    return 0


if __name__ == "__main__":
    sys.exit(run_all_tests())
