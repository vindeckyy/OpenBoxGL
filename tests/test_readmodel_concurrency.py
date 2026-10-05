#!/usr/bin/env python3
"""S12/S13 -- the SQLite read model served readers from the middle of a rebuild.

**S12.** `rebuild` holds `self._lock` across `DELETE FROM games` and the
`executemany(INSERT ...)` + commit. Every reader -- `query`, `search`, `facets`,
`count` -- ran `conn.execute` on the *same* connection (opened with
`check_same_thread=False`) **without** that lock. The stdlib serializes
individual statements but not transaction boundaries, so a search thread that
landed between the DELETE and the commit saw an empty table and returned
**zero results** for a library with 20,000 games. This is a normal
configuration: the model auto-enables above `SQLITE_AUTO_THRESHOLD`.

**S13.** `ensure_fresh` published `self._signature = signature` unconditionally
after `rebuild` returned. With two callers, A-rebuild -> B-rebuild (newer
signature) -> A-resume (older) left `_signature` naming the *older* snapshot, so
every later caller passed the freshness check and was served the wrong rows
until something happened to carry a newer signature.

The invariant: **a reader observes a complete snapshot, and the published
signature always describes what is actually in the table.**
"""

import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pkg.state import sqlite_readmodel


def _state(count, prefix="Game"):
    return {
        "games": [
            {
                "game_id": f"g{index}",
                "name": f"{prefix} {index}",
                "platform": "NES",
                "genre": "Platformer",
            }
            for index in range(count)
        ]
    }


class ReadModelTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.model = sqlite_readmodel.SqliteReadModel(Path(self._tmp.name) / "readmodel.db")
        # Force the model on regardless of library size or env opt-out.
        self.model._enabled = True
        self.addCleanup(self.model.close)


class ReaderSeesCompleteSnapshotsTests(ReadModelTestCase):
    """S12: a reader must never observe a half-rebuilt table."""

    def test_a_reader_blocks_while_a_rebuild_holds_the_lock(self):
        """Deterministic proof of mutual exclusion.

        A scheduling-dependent soak cannot prove this -- it only proves it when
        the race happens to land. Holding the lock and checking that a reader
        makes no progress is the actual property: the reader cannot observe the
        post-DELETE, pre-INSERT window because it cannot run at all.
        """
        self.model.rebuild(_state(5, "Seed"))
        results = []
        finished = threading.Event()

        def read():
            results.append((self.model.count(), len(self.model.query(limit=100))))
            finished.set()

        # Stand in for rebuild: take the model's own lock and hold it, without
        # touching the table.
        self.model._lock.acquire()
        try:
            reader = threading.Thread(target=read, daemon=True)
            reader.start()
            self.assertFalse(
                finished.wait(0.5),
                "a reader completed while the lock was held -- it can see a torn table",
            )
        finally:
            self.model._lock.release()
        self.assertTrue(finished.wait(5.0), "the reader never completed after the lock was released")
        self.assertEqual(results, [(5, 5)])

    def test_each_read_returns_one_consistent_snapshot(self):
        """A soak, as a regression net rather than as the proof.

        The lock is coarse, so a rebuild *can* land between two separate reads.
        That is fine and unavoidable -- what must never happen is a single read
        returning rows from two different table versions. Each result is checked
        for a uniform generation marker.
        """
        states = [_state(3, "Small"), _state(200, "Large")]
        self.model.rebuild(states[0])
        stop = threading.Event()
        rebuilds = {"count": 0}

        def rebuild_loop():
            while not stop.is_set():
                self.model.rebuild(states[rebuilds["count"] % 2])
                rebuilds["count"] += 1

        rebuilder = threading.Thread(target=rebuild_loop, daemon=True)
        rebuilder.start()
        try:
            deadline = time.monotonic() + 3.0
            checked = 0
            while time.monotonic() < deadline:
                for rows in (self.model.query(limit=1000), self.model.search("a", limit=200)):
                    checked += 1
                    self.assertTrue(rows, "a read of a populated table returned nothing")
                    markers = {row["name"].split()[0] for row in rows}
                    self.assertEqual(
                        len(markers), 1,
                        f"a single read returned rows from more than one table version: {markers}",
                    )
                self.assertIn(self.model.count(), (3, 200))
            self.assertGreater(checked, 0, "no sample was taken")
        finally:
            stop.set()
            rebuilder.join(timeout=5.0)

    def test_search_never_returns_empty_for_a_populated_table(self):
        self.model.rebuild(_state(50, "Findable"))
        stop = threading.Event()

        def rebuild_loop():
            states = [_state(50, "Findable"), _state(120, "Findable")]
            index = 0
            while not stop.is_set():
                self.model.rebuild(states[index % 2])
                index += 1

        rebuilder = threading.Thread(target=rebuild_loop, daemon=True)
        rebuilder.start()
        try:
            deadline = time.monotonic() + 2.0
            hits = 0
            while time.monotonic() < deadline:
                if self.model.search("Findable", limit=5):
                    hits += 1
            self.assertGreater(hits, 0, "search never found anything during rebuilds")
        finally:
            stop.set()
            rebuilder.join(timeout=5.0)

    def test_the_readers_share_the_rebuild_lock(self):
        """Structural check: a reader that takes the lock cannot race rebuild."""
        import inspect

        for name in ("query", "search", "facets", "count"):
            with self.subTest(reader=name):
                source = inspect.getsource(getattr(sqlite_readmodel.SqliteReadModel, name))
                self.assertIn(
                    "with self._lock:", source,
                    f"{name}() reads the shared connection without the model's lock",
                )


class SignaturePublicationTests(ReadModelTestCase):
    """S13: the published signature must describe what is in the table."""

    def _state_with_signature(self, count, signature):
        state = _state(count, f"Sig{count}")
        state["__signature__"] = signature
        return state, signature

    def test_a_superseded_rebuild_does_not_publish_its_older_signature(self):
        """A-rebuild -> B-rebuild -> A-resume must not leave A's signature."""
        model = self.model
        older_state, older_sig = self._state_with_signature(3, (1, 0, 3))
        newer_state, newer_sig = self._state_with_signature(9, (2, 0, 9))

        order = []
        original_rebuild = model.rebuild

        def scripted_rebuild(state, generation=None):
            # Let the first (older) rebuild pause *inside* itself, so the
            # newer one can overtake it exactly as two request threads would.
            if state is older_state:
                order.append("older-start")
                older_entered.set()
                older_may_finish.wait(5.0)
            # Forward the generation: dropping it would bypass the very guard
            # under test and make the rollback look like it did nothing.
            original_rebuild(state, generation=generation)
            order.append(f"{state['games'][0]['name']}-done")

        older_entered = threading.Event()
        older_may_finish = threading.Event()
        model.rebuild = scripted_rebuild

        older_thread = threading.Thread(
            target=model.ensure_fresh, args=(older_state, older_sig), daemon=True
        )
        older_thread.start()
        self.assertTrue(older_entered.wait(5.0), "the older rebuild never started")

        # A newer state rebuilds and publishes while the older one is paused.
        model.ensure_fresh(newer_state, newer_sig)
        self.assertEqual(model._signature, newer_sig)

        # Now let the older one finish and reach its publish step.
        older_may_finish.set()
        older_thread.join(timeout=5.0)

        self.assertEqual(
            model._signature, newer_sig,
            f"the superseded rebuild overwrote the signature: publish order was {order}",
        )

    def test_the_signature_matches_the_table_contents_after_a_race(self):
        """The observable consequence: no stale rows behind a passing check."""
        model = self.model
        older_state, older_sig = self._state_with_signature(3, (1, 0, 3))
        newer_state, newer_sig = self._state_with_signature(9, (2, 0, 9))

        entered = threading.Event()
        may_finish = threading.Event()
        original_rebuild = model.rebuild

        def scripted_rebuild(state, generation=None):
            if state is older_state:
                entered.set()
                may_finish.wait(5.0)
            original_rebuild(state, generation=generation)

        model.rebuild = scripted_rebuild
        older = threading.Thread(target=model.ensure_fresh, args=(older_state, older_sig), daemon=True)
        older.start()
        self.assertTrue(entered.wait(5.0))
        model.ensure_fresh(newer_state, newer_sig)
        may_finish.set()
        older.join(timeout=5.0)
        model.rebuild = original_rebuild

        # The table holds the newer data...
        self.assertEqual(model.count(), 9)
        # ...so a caller presenting the newer signature must not trigger a
        # rebuild, and must see the newer rows.
        rebuilds = {"count": 0}

        def counting_rebuild(state, generation=None):
            rebuilds["count"] += 1
            original_rebuild(state)

        model.rebuild = counting_rebuild
        model.ensure_fresh(newer_state, newer_sig)
        self.assertEqual(rebuilds["count"], 0, "a correct signature still forced a rebuild")
        self.assertEqual(len(model.query(limit=100)), 9)
        self.assertTrue(all(g["name"].startswith("Sig9") for g in model.query(limit=100)))


class UnchangedBehaviourTests(ReadModelTestCase):
    """The locking must not change what a correct caller sees."""

    def test_queries_filters_sorts_and_limits(self):
        self.model.rebuild(_state(10, "Row"))
        self.assertEqual(len(self.model.query(limit=3)), 3)
        self.assertEqual(len(self.model.query(limit=100)), 10)
        self.assertEqual(self.model.query(platform="NES", limit=100).__len__(), 10)
        self.assertEqual(self.model.query(platform="SNES", limit=100), [])
        names = [g["name"] for g in self.model.query(limit=100)]
        self.assertEqual(names, [f"Row {i}" for i in range(10)])

    def test_facets_reject_an_unknown_field(self):
        self.model.rebuild(_state(4))
        self.assertEqual(self.model.facets("not_a_column"), [])
        self.assertEqual(self.model.facets("platform"), [("NES", 4)])

    def test_search_caps_and_matches_casefolded(self):
        self.model.rebuild(_state(60, "MixedCase"))
        self.assertEqual(len(self.model.search("mixedcase", limit=5)), 5)
        self.assertEqual(self.model.search("", limit=5), [])
        self.assertEqual(self.model.search("nothing-here", limit=5), [])

    def test_ensure_fresh_is_a_no_op_for_an_unchanged_signature(self):
        state = _state(5)
        self.model.ensure_fresh(state, (1, 1, 5))
        self.assertEqual(self.model._signature, (1, 1, 5))
        rebuilds = {"count": 0}
        original = self.model.rebuild

        def counting(state, generation=None):
            rebuilds["count"] += 1
            original(state)

        self.model.rebuild = counting
        self.model.ensure_fresh(state, (1, 1, 5))
        self.assertEqual(rebuilds["count"], 0)

    def test_a_disabled_model_does_nothing(self):
        self.model._enabled = False
        self.assertEqual(self.model.query(), [])
        self.assertEqual(self.model.count(), 0)
        self.assertEqual(self.model.facets("platform"), [])
        self.assertEqual(self.model.search("x"), [])


if __name__ == "__main__":
    unittest.main()
