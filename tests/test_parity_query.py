"""Tests for the "just say it" library query grammar (T4-query, 1.11.0).

parity_query.tokenize -> parse_tokens (rule AST) -> compile (preset rules +
clauses + ADR-0020 chips). The parse route is a read-only preview: it returns
chips+rules and never mutates state. ``game_matches_query`` evaluates the
preset-rule subset through ``parity_filter_presets.game_matches_rules`` and
the clause list through the dedicated matchers, so a phrase can never produce
a filter that does less than its chips claim.
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import pkg.parity  # noqa: F401,E402  # register flat-import finder

from pkg.parity import parity_query  # noqa: E402

NOW = datetime(2026, 9, 12, 12, 0, 0)


def parse(text):
    return parity_query.parse_query(text, now=NOW)


def clauses_of(result, kind):
    return [c for c in result["clauses"] if c["kind"] == kind]


# ── Fixture corpus: ~60 phrases -> expected rules + clauses ─────────────────
# Each entry: (phrase, expected rules dict, expected clauses list, leftover)
FIXTURES = [
    # Spec examples
    ("short unplayed rpg",
     {},
     [{"kind": "ttb_max", "hours": 5.0}, {"kind": "unplayed"},
      {"kind": "genre_any", "terms": ["rpg", "role-playing", "role playing"]}],
     []),
    ("couch co-op for 4",
     {},
     [{"kind": "coop"}, {"kind": "players_min", "value": 4}],
     []),
    ("90s platformers rated 4+",
     {"genre": "platform"},
     [{"kind": "year_range", "min": 1990, "max": 1999}, {"kind": "rating_min", "value": 4.0}],
     []),
    ("haven't played in a year",
     {},
     [{"kind": "idle_days", "days": 365}],
     []),
    # played / recency
    ("unplayed", {}, [{"kind": "unplayed"}], []),
    ("never played", {}, [{"kind": "unplayed"}], []),
    ("haven't played", {}, [{"kind": "unplayed"}], []),
    ("not played", {}, [{"kind": "unplayed"}], []),
    ("haven't started", {}, [{"kind": "unplayed"}], []),
    ("untouched", {}, [{"kind": "unplayed"}], []),
    ("played", {}, [{"kind": "played"}], []),
    ("played recently", {}, [{"kind": "played_within", "days": 31}], []),
    ("recently played", {}, [{"kind": "played_within", "days": 31}], []),
    ("newly added", {}, [{"kind": "added_days", "days": 31}], []),
    ("recently added", {}, [{"kind": "added_days", "days": 31}], []),
    ("added this year", {}, [{"kind": "added_year", "year": 2026}], []),
    ("haven't played in 6 months", {}, [{"kind": "idle_days", "days": 180}], []),
    ("not played in 2 weeks", {}, [{"kind": "idle_days", "days": 14}], []),
    ("haven't played in over a year", {}, [{"kind": "idle_days", "days": 365}], []),
    ("haven't played since 2020", {}, [{"kind": "idle_before", "year": 2020}], []),
    # length
    ("short", {}, [{"kind": "ttb_max", "hours": 5.0}], []),
    ("quick games", {}, [{"kind": "ttb_max", "hours": 5.0}], []),
    ("under 3 hours", {}, [{"kind": "ttb_max", "hours": 3.0}], []),
    ("less than 2 hours", {}, [{"kind": "ttb_max", "hours": 2.0}], []),
    ("3 hours or less", {}, [{"kind": "ttb_max", "hours": 3.0}], []),
    ("long", {}, [{"kind": "ttb_min", "hours": 20.0}], []),
    ("epic", {}, [{"kind": "ttb_min", "hours": 20.0}], []),
    ("over 40 hours", {}, [{"kind": "ttb_min", "hours": 40.0}], []),
    ("20+ hours", {}, [{"kind": "ttb_min", "hours": 20.0}], []),
    # rating
    ("rated 4+", {}, [{"kind": "rating_min", "value": 4.0}], []),
    ("rated 4.5", {}, [{"kind": "rating_min", "value": 4.5}], []),
    ("4+ stars", {}, [{"kind": "rating_min", "value": 4.0}], []),
    ("five stars", {}, [{"kind": "rating_min", "value": 5.0}], []),
    ("highly rated", {}, [{"kind": "rating_min", "value": 4.0}], []),
    ("top rated", {}, [{"kind": "rating_min", "value": 4.5}], []),
    ("unrated", {}, [{"kind": "unrated"}], []),
    ("not rated", {}, [{"kind": "unrated"}], []),
    # esrb
    ("rated m", {"esrb": "M"}, [], []),
    ("rated teen", {"esrb": "T"}, [], []),
    ("esrb e10+", {"esrb": "E10+"}, [], []),
    ("adults only", {"esrb": "AO"}, [], []),
    ("mature", {}, [{"kind": "esrb_any", "values": ["M", "AO"]}], []),
    ("kids games", {}, [{"kind": "esrb_any", "values": ["E", "E10+", "EC"]}], []),
    ("family friendly", {}, [{"kind": "esrb_any", "values": ["E", "E10+", "EC"]}], []),
    # era
    ("80s", {}, [{"kind": "year_range", "min": 1980, "max": 1989}], []),
    ("2000s", {}, [{"kind": "year_range", "min": 2000, "max": 2009}], []),
    ("1990s", {}, [{"kind": "year_range", "min": 1990, "max": 1999}], []),
    ("from 1995", {}, [{"kind": "year_range", "min": 1995, "max": 1995}], []),
    ("released in 2001", {}, [{"kind": "year_range", "min": 2001, "max": 2001}], []),
    ("before 2000", {}, [{"kind": "year_range", "min": None, "max": 1999}], []),
    ("after 2010", {}, [{"kind": "year_range", "min": 2011, "max": None}], []),
    ("since 2015", {}, [{"kind": "year_range", "min": 2015, "max": None}], []),
    ("retro", {}, [{"kind": "year_range", "min": None, "max": 2000}], []),
    ("classic", {}, [{"kind": "year_range", "min": None, "max": 2000}], []),
    ("this year", {}, [{"kind": "year_range", "min": 2026, "max": 2026}], []),
    # players / coop
    ("co-op", {}, [{"kind": "coop"}], []),
    ("local co-op", {}, [{"kind": "coop"}], []),
    ("couch", {}, [{"kind": "coop"}], []),
    ("multiplayer", {}, [{"kind": "multiplayer"}], []),
    ("split screen", {}, [{"kind": "multiplayer"}], []),
    ("party", {}, [{"kind": "multiplayer"}], []),
    ("for 4", {}, [{"kind": "players_min", "value": 4}], []),
    ("4 players", {}, [{"kind": "players_min", "value": 4}], []),
    ("2-player", {}, [{"kind": "players_min", "value": 2}], []),
    ("for two", {}, [{"kind": "players_min", "value": 2}], []),
    ("up to 3 players", {}, [{"kind": "players_min", "value": 3}], []),
    ("single player", {}, [{"kind": "players_max", "value": 1}], []),
    ("solo", {}, [{"kind": "players_max", "value": 1}], []),
    # progress
    ("playing", {}, [{"kind": "progress_any", "values": ["Playing", "Paused"]}], []),
    ("in progress", {}, [{"kind": "progress_any", "values": ["Playing", "Paused"]}], []),
    ("beaten", {"progress": "Beaten"}, [], []),
    ("completed", {"progress": "Completed"}, [], []),
    ("mastered", {"progress": "Mastered"}, [], []),
    ("finished", {}, [{"kind": "progress_any", "values": ["Beaten", "Completed", "Mastered"]}], []),
    ("100%", {}, [{"kind": "progress_any", "values": ["Completed", "Mastered"]}], []),
    ("unfinished", {}, [{"kind": "not_finished"}], []),
    ("not finished", {}, [{"kind": "not_finished"}], []),
    ("never beaten", {}, [{"kind": "not_finished"}], []),
    ("backlog", {}, [{"kind": "backlog"}], []),
    ("abandoned", {"progress": "Abandoned"}, [], []),
    ("dropped", {"progress": "Abandoned"}, [], []),
    ("paused", {"progress": "Paused"}, [], []),
    ("on hold", {"progress": "Paused"}, [], []),
    # flags / booleans
    ("favorites", {"favorite": True}, [], []),
    ("not favorites", {"favorite": False}, [], []),
    ("hidden", {"hidden": True}, [], []),
    ("installed", {"installed": "installed"}, [], []),
    ("not installed", {"installed": "uninstalled"}, [], []),
    ("uninstalled", {"installed": "uninstalled"}, [], []),
    ("owned", {}, [{"kind": "owned"}], []),
    ("with saves", {"has_saves": True}, [], []),
    ("no saves", {"has_saves": False}, [], []),
    ("with achievements", {"has_achievements": True}, [], []),
    ("achievements", {"has_achievements": True}, [], []),
    ("no achievements", {"has_achievements": False}, [], []),
    ("missing covers", {"has_missing_media": True}, [], []),
    ("high scores", {"has_highscores": True}, [], []),
    ("shelf entries", {}, [{"kind": "flag", "field": "manual_entry", "value": True}], []),
    ("broken", {}, [{"kind": "flag", "field": "broken", "value": True}], []),
    ("portable", {}, [{"kind": "flag", "field": "portable", "value": True}], []),
    ("controller support", {}, [{"kind": "flag", "field": "controller_support", "value": True}], []),
    ("with notes", {}, [{"kind": "flag", "field": "notes", "value": True}], []),
    # platform / store / credit / tag / series / region
    ("on snes", {}, [{"kind": "platform_any", "term": "snes"}], []),
    ("for snes", {}, [{"kind": "platform_any", "term": "snes"}], []),
    ("steam games", {}, [{"kind": "source", "term": "steam"}], []),
    ("from steam", {}, [{"kind": "source", "term": "steam"}], []),
    ("on gog", {}, [{"kind": "source", "term": "gog"}], []),
    ("by nintendo", {}, [{"kind": "credit", "term": "nintendo"}], []),
    ("developed by valve", {}, [{"kind": "credit", "term": "valve"}], []),
    ("tagged roguelike", {}, [{"kind": "tag", "term": "roguelike"}], []),
    ("#horror", {}, [{"kind": "tag", "term": "horror"}], []),
    # genres
    ("rpg", {}, [{"kind": "genre_any", "terms": ["rpg", "role-playing", "role playing"]}], []),
    ("jrpg", {}, [{"kind": "genre_any", "terms": ["jrpg", "j-rpg"]}], []),
    ("platformer", {"genre": "platform"}, [], []),
    ("shooter", {"genre": "shoot"}, [], []),
    ("puzzle", {"genre": "puzzl"}, [], []),
    ("strategy", {"genre": "strategy"}, [], []),
    ("rts", {}, [{"kind": "genre_any", "terms": ["rts", "real-time strategy", "real time strategy"]}], []),
    ("racing", {"genre": "rac"}, [], []),
    ("horror", {"genre": "horror"}, [], []),
    ("metroidvania", {"genre": "metroidvania"}, [], []),
    ("roguelike", {"genre": "rogue"}, [], []),
    ("open world", {}, [{"kind": "genre_any", "terms": ["open world", "open-world"]}], []),
    ("visual novel", {"genre": "visual novel"}, [], []),
    ("point and click", {}, [{"kind": "genre_any", "terms": ["point and click", "point-and-click"]}], []),
    ("fighting", {"genre": "fight"}, [], []),
    ("turn based", {}, [{"kind": "genre_any", "terms": ["turn-based", "turn based"]}], []),
    ("shmup", {}, [{"kind": "genre_any", "terms": ["shmup", "shoot 'em up", "shoot em up"]}], []),
    ("soulslike", {}, [{"kind": "genre_any", "terms": ["soulslike", "souls-like", "souls"]}], []),
    ("indie", {"genre": "indie"}, [], []),
    # negations / typed tokens / free text
    ("not mario", {}, [{"kind": "not_query", "term": "mario"}], []),
    ("-mario", {}, [{"kind": "not_query", "term": "mario"}], []),
    ("no mario", {}, [{"kind": "not_query", "term": "mario"}], []),
    ("genre:rpg", {}, [{"kind": "genre_any", "terms": ["rpg"]}], []),
    ("developer:nintendo", {"developer": "nintendo"}, [], []),
    ("platform:snes", {}, [{"kind": "platform_any", "term": "snes"}], []),
    ("progress:beaten", {"progress": "Beaten"}, [], []),
    ("favorite:yes", {"favorite": True}, [], []),
    ("favorite:no", {"favorite": False}, [], []),
    ("installed:no", {"installed": "uninstalled"}, [], []),
    ("year:1999", {}, [{"kind": "year_range", "min": 1999, "max": 1999}], []),
    ("players:4", {}, [{"kind": "players_min", "value": 4}], []),
    ("rating:3", {}, [{"kind": "rating_min", "value": 3.0}], []),
    ("esrb:t", {"esrb": "T"}, [], []),
    ("tag:cozy", {}, [{"kind": "tag", "term": "cozy"}], []),
    ("series:zelda", {}, [{"kind": "series", "term": "zelda"}], []),
    ("region:pal", {}, [{"kind": "region", "term": "pal"}], []),
    ("source:steam", {}, [{"kind": "source", "term": "steam"}], []),
    ("unplayed:yes", {}, [{"kind": "unplayed"}], []),
    # compositions + leftovers
    ("unplayed 90s rpg",
     {},
     [{"kind": "unplayed"},
      {"kind": "year_range", "min": 1990, "max": 1999},
      {"kind": "genre_any", "terms": ["rpg", "role-playing", "role playing"]}],
     []),
    ("mario kart unplayed",
     {"query": "mario kart"},
     [{"kind": "unplayed"}],
     ["mario", "kart"]),
    ("short zelda",
     {"query": "zelda"},
     [{"kind": "ttb_max", "hours": 5.0}],
     ["zelda"]),
    ('"super metroid"',
     {"query": "super metroid"},
     [],
     ["super metroid"]),
    ("i want to play something short",
     {},
     [{"kind": "ttb_max", "hours": 5.0}],
     []),
    ("mario series",
     {"query": "mario"},
     [],
     ["mario"]),
    ("unplayed rpg from the 90s",
     {},
     [{"kind": "unplayed"},
      {"kind": "genre_any", "terms": ["rpg", "role-playing", "role playing"]},
      {"kind": "year_range", "min": 1990, "max": 1999}],
     []),
    ("haven't played in a year and rated 4+",
     {},
     [{"kind": "idle_days", "days": 365}, {"kind": "rating_min", "value": 4.0}],
     []),
]


class FixtureCorpusTest(unittest.TestCase):
    def test_fixture_corpus(self):
        self.assertGreaterEqual(len(FIXTURES), 60)
        failures = []
        for phrase, want_rules, want_clauses, want_leftover in FIXTURES:
            result = parse(phrase)
            got = (result["rules"], result["clauses"], result["unparsed"])
            want = (want_rules, want_clauses, want_leftover)
            if got != want:
                failures.append(f"{phrase!r}:\n  want {want}\n  got  {got}")
        self.assertEqual(failures, [], "\n".join(failures))

    def test_parse_is_deterministic(self):
        for phrase, _, _, _ in FIXTURES[:20]:
            self.assertEqual(parse(phrase), parse(phrase))

    def test_every_constraint_has_a_chip(self):
        for phrase, want_rules, want_clauses, _ in FIXTURES:
            result = parse(phrase)
            if set(want_rules) == {"query"} and not want_clauses:
                continue
            if not want_rules and not want_clauses:
                continue
            self.assertTrue(result["parsed"], phrase)
            self.assertEqual(
                len(result["chips"]),
                len(want_rules) + len(want_clauses) + (0 if not want_rules.get("query") else 0),
                phrase,
            )
            for chip in result["chips"]:
                self.assertIn("key", chip)
                self.assertIn("label", chip)
                self.assertIn("value", chip)
                self.assertIn("display", chip)

    def test_unparsable_falls_back_to_substring_with_hint(self):
        result = parse("xyzzy blorp")
        self.assertFalse(result["parsed"])
        self.assertEqual(result["rules"], {"query": "xyzzy blorp"})
        self.assertEqual(result["clauses"], [])
        self.assertEqual(result["unparsed"], ["xyzzy", "blorp"])
        self.assertTrue(result["hint"])

    def test_empty_and_stopword_only_inputs(self):
        for text in ("", "   ", None, "games", "i want to play"):
            result = parse(text)
            self.assertFalse(result["parsed"], text)
            self.assertEqual(result["clauses"], [])
            self.assertEqual(result["chips"], [])

    def test_complete_flag(self):
        self.assertTrue(parse("rpg")["complete"] is False)  # genre_any clause
        self.assertTrue(parse("beaten")["complete"])        # pure preset rules
        self.assertFalse(parse("unplayed")["complete"])     # clause needed

    def test_localized_search_examples_keep_executable_grammar(self):
        for locale in ("en", "de", "es", "fr", "pt"):
            with self.subTest(locale=locale):
                path = ROOT / "locales" / f"{locale}.json"
                body = json.loads(path.read_text(encoding="utf-8"))["whats_new"]["say_it_body"].lower()
                examples = re.findall(r"(?:short unplayed rpg|co-op for 4)", body)
                self.assertEqual(examples, ["short unplayed rpg", "co-op for 4"])
                self.assertTrue(parse(examples[0])["parsed"])
                self.assertTrue(parse(examples[1])["parsed"])


class ClauseMatchTest(unittest.TestCase):
    """game_matches_query: each clause kind matches only what it claims."""

    def _match(self, phrase, game):
        return parity_query.game_matches_query(game, parse(phrase), now=NOW)

    def setUp(self):
        self.days_ago = lambda n: (NOW - timedelta(days=n)).isoformat(timespec="seconds")
        self.base = {"name": "Fixture", "platform": "SNES", "genre": "RPG",
                     "developer": "Square", "publisher": "Square Enix",
                     "series": "Final", "region": "NTSC", "year": "1994",
                     "rating": 4.5, "progress": "", "play_count": 0,
                     "last_played": "", "max_players": 1,
                     "time_to_beat_hours": 3, "play_mode": "Single Player",
                     "tags": ["classic"], "notes": "", "esrb": "E",
                     "source": "cart", "added_at": self.days_ago(400)}

    def game(self, **kw):
        merged = dict(self.base)
        merged.update(kw)
        return merged

    def test_unplayed(self):
        self.assertTrue(self._match("unplayed", self.game()))
        self.assertFalse(self._match("unplayed", self.game(play_count=2)))
        self.assertFalse(self._match("unplayed", self.game(last_played=self.days_ago(3))))
        self.assertFalse(self._match("unplayed", self.game(progress="Playing")))
        self.assertTrue(self._match("played", self.game(play_count=1)))
        self.assertTrue(self._match("played", self.game(progress="Beaten")))

    def test_idle_days(self):
        self.assertTrue(self._match("haven't played in a year", self.game(last_played=self.days_ago(400))))
        self.assertFalse(self._match("haven't played in a year", self.game(last_played=self.days_ago(100))))
        # Never played counts as not played in a year (honest reading).
        self.assertTrue(self._match("haven't played in a year", self.game()))
        # But "recently played" requires an actual recent play.
        self.assertFalse(self._match("recently played", self.game()))
        self.assertTrue(self._match("recently played", self.game(last_played=self.days_ago(3))))
        self.assertFalse(self._match("recently played", self.game(last_played=self.days_ago(90))))

    def test_idle_before_year(self):
        self.assertTrue(self._match("haven't played since 2020", self.game(last_played="2019-05-01")))
        self.assertTrue(self._match("haven't played since 2020", self.game()))
        self.assertFalse(self._match("haven't played since 2020", self.game(last_played="2021-01-01")))

    def test_added(self):
        self.assertTrue(self._match("newly added", self.game(added_at=self.days_ago(3))))
        self.assertFalse(self._match("newly added", self.game(added_at=self.days_ago(400))))
        self.assertFalse(self._match("newly added", self.game(added_at="")))
        self.assertTrue(self._match("added this year", self.game(added_at="2026-02-01")))
        self.assertFalse(self._match("added this year", self.game(added_at="2025-12-31")))

    def test_ttb(self):
        self.assertTrue(self._match("short", self.game(time_to_beat_hours=2)))
        self.assertFalse(self._match("short", self.game(time_to_beat_hours=8)))
        # No metadata -> honestly excluded rather than guessed.
        self.assertFalse(self._match("short", self.game(time_to_beat_hours=None)))
        self.assertFalse(self._match("short", self.game(time_to_beat_hours="")))
        self.assertTrue(self._match("long", self.game(time_to_beat_hours=30)))
        self.assertFalse(self._match("long", self.game(time_to_beat_hours=10)))

    def test_rating(self):
        self.assertTrue(self._match("rated 4+", self.game(rating=4.2)))
        self.assertFalse(self._match("rated 4+", self.game(rating=3.9)))
        self.assertFalse(self._match("rated 4+", self.game(rating="")))
        self.assertTrue(self._match("unrated", self.game(rating="")))
        self.assertTrue(self._match("unrated", self.game(rating=0)))
        self.assertFalse(self._match("unrated", self.game(rating=1)))

    def test_year_range(self):
        self.assertTrue(self._match("90s", self.game(year="1994")))
        self.assertFalse(self._match("90s", self.game(year="2001")))
        self.assertFalse(self._match("90s", self.game(year="")))
        self.assertTrue(self._match("before 2000", self.game(year="1985")))
        self.assertTrue(self._match("after 2010", self.game(year="2012")))
        self.assertFalse(self._match("after 2010", self.game(year="2010")))

    def test_players_and_coop(self):
        self.assertTrue(self._match("for 4", self.game(max_players=4)))
        self.assertFalse(self._match("for 4", self.game(max_players=2)))
        self.assertFalse(self._match("for 4", self.game(max_players="")))
        self.assertTrue(self._match("single player", self.game(max_players=1)))
        self.assertFalse(self._match("single player", self.game(max_players=4)))
        self.assertTrue(self._match("multiplayer", self.game(max_players=2)))
        self.assertTrue(self._match("multiplayer", self.game(play_mode="Multiplayer")))
        self.assertFalse(self._match("multiplayer", self.game()))
        self.assertTrue(self._match("couch co-op", self.game(play_mode="Co-operative", max_players=4)))
        self.assertTrue(self._match("couch co-op", self.game(tags=["co-op"])))
        # No co-op evidence anywhere -> honestly excluded, never guessed.
        self.assertFalse(self._match("couch co-op", self.game(max_players=8)))

    def test_progress(self):
        self.assertTrue(self._match("beaten", self.game(progress="Beaten")))
        self.assertFalse(self._match("beaten", self.game(progress="Playing")))
        self.assertTrue(self._match("playing", self.game(progress="Paused")))
        self.assertFalse(self._match("playing", self.game(progress="Beaten")))
        self.assertTrue(self._match("finished", self.game(progress="Mastered")))
        self.assertTrue(self._match("unfinished", self.game(progress="Abandoned")))
        self.assertFalse(self._match("unfinished", self.game(progress="Completed")))
        self.assertTrue(self._match("backlog", self.game(progress="")))
        self.assertTrue(self._match("backlog", self.game(progress="Playing")))
        self.assertFalse(self._match("backlog", self.game(progress="Beaten")))
        self.assertFalse(self._match("backlog", self.game(progress="Abandoned")))

    def test_text_fields(self):
        self.assertTrue(self._match("on snes", self.game(platform="Super NES / SNES")))
        self.assertFalse(self._match("on snes", self.game(platform="Genesis")))
        self.assertTrue(self._match("by square", self.game(developer="Squaresoft")))
        self.assertTrue(self._match("by square", self.game(publisher="Square EA", developer="Other")))
        self.assertFalse(self._match("by enix", self.game()))
        self.assertTrue(self._match("tagged classic", self.game(tags=["Classic RPG"])))
        self.assertFalse(self._match("tagged spooky", self.game()))
        self.assertTrue(self._match("series:final", self.game(series="Final Fantasy")))
        self.assertTrue(self._match("region:ntsc", self.game()))
        self.assertTrue(self._match("steam games", self.game(source="steam")))
        self.assertFalse(self._match("steam games", self.game(source="gog")))
        self.assertTrue(self._match("zelda", self.game(name="Zelda II")))
        self.assertTrue(self._match("zelda", self.game(name="Other", series="Zelda")))
        self.assertFalse(self._match("zelda", self.game(name="Mario")))
        self.assertFalse(self._match("not mario", self.game(name="Super Mario")))
        self.assertTrue(self._match("not mario", self.game(name="Sonic")))
        self.assertFalse(self._match("not mario", self.game(notes="a mario romhack")))

    def test_plain_title_keeps_interstitial_stopwords(self):
        parsed = parse("Life is Strange")
        self.assertEqual(parsed.get("plain_query"), "life is strange")
        self.assertTrue(self._match("Life is Strange", self.game(name="Life is Strange")))
        self.assertFalse(self._match("Life is Strange", self.game(name="Life Strange")))

    def test_flags(self):
        self.assertTrue(self._match("shelf entries", self.game(manual_entry=True)))
        self.assertFalse(self._match("shelf entries", self.game()))
        self.assertTrue(self._match("broken", self.game(broken=True)))
        self.assertTrue(self._match("portable", self.game(portable=True)))
        self.assertTrue(self._match("controller support", self.game(controller_support="full")))
        self.assertFalse(self._match("controller support", self.game(controller_support="")))
        self.assertTrue(self._match("with notes", self.game(notes="hi")))
        self.assertFalse(self._match("with notes", self.game()))
        self.assertTrue(self._match("owned", self.game(owned=True)))
        self.assertTrue(self._match("owned", self.game(steam_app_id="123")))
        self.assertFalse(self._match("owned", self.game()))

    def test_preset_rules(self):
        self.assertTrue(self._match("favorites", self.game(favorite=True)))
        self.assertFalse(self._match("favorites", self.game()))
        self.assertTrue(self._match("hidden", self.game(hidden=True)))
        self.assertTrue(self._match("installed", self.game(store_installed=True)))
        self.assertFalse(self._match("installed", self.game(store_installed=False)))
        self.assertTrue(self._match("uninstalled", self.game(store_installed=False)))
        self.assertTrue(self._match("with saves", self.game(save_paths=["/s1"])))
        self.assertTrue(self._match("with saves", self.game(has_saves=True)))
        self.assertFalse(self._match("with saves", self.game()))
        self.assertTrue(self._match("with achievements", self.game(ra_game_id="1")))
        self.assertTrue(self._match("no achievements", self.game()))
        self.assertTrue(self._match("missing covers", self.game(has_missing_media=True)))
        self.assertTrue(self._match("missing covers", self.game()))
        self.assertFalse(self._match("missing covers", self.game(cover="c.png", background="b.png")))
        self.assertTrue(self._match("high scores", self.game(has_highscores=True)))
        self.assertTrue(self._match("esrb:t", self.game(esrb="T")))
        self.assertFalse(self._match("esrb:t", self.game(esrb="M")))
        self.assertTrue(self._match("kids games", self.game(esrb="E10+")))
        self.assertFalse(self._match("kids games", self.game(esrb="M")))

    def test_combined_spec_fixtures(self):
        games = [
            self.game(name="Tiny Quest", genre="RPG", time_to_beat_hours=3),
            self.game(name="Long Quest", genre="RPG", time_to_beat_hours=50),
            self.game(name="Tiny Kart", genre="Racing", time_to_beat_hours=2),
        ]
        matched = [g["name"] for g in games if self._match("short unplayed rpg", g)]
        self.assertEqual(matched, ["Tiny Quest"])

        coop = self.game(name="Couch Heroes", genre="Action",
                         play_mode="Co-operative", max_players=4)
        solo = self.game(name="Solo", max_players=1, play_mode="")
        versus = self.game(name="Versus", max_players=8, play_mode="Deathmatch")
        matched = [g["name"] for g in [coop, solo, versus]
                   if self._match("couch co-op for 4", g)]
        self.assertEqual(matched, ["Couch Heroes"])

        old = self.game(name="Old Plat", genre="Platformer", year="1993", rating=4.2)
        new = self.game(name="New Plat", genre="Platformer", year="2020", rating=4.8)
        low = self.game(name="Low Plat", genre="Platformer", year="1995", rating=2.0)
        matched = [g["name"] for g in [old, new, low]
                   if self._match("90s platformers rated 4+", g)]
        self.assertEqual(matched, ["Old Plat"])

    def test_bad_inputs_never_raise(self):
        self.assertTrue(parity_query.game_matches_query(None, parse("unplayed"), now=NOW))
        self.assertFalse(parity_query.game_matches_query(self.game(), {"clauses": [{"kind": "bogus"}]}, now=NOW))
        self.assertFalse(parity_query.game_matches_query(self.game(), "junk", now=NOW))


class TokenizerTest(unittest.TestCase):
    def test_tokenize_normalizes(self):
        tokens = parity_query.tokenize("  Haven't   PLAYED in a Year! ")
        self.assertEqual([t[1] for t in tokens], ["haven't", "played", "in", "a", "year!"])

    def test_tokenize_keeps_quoted_phrase(self):
        tokens = parity_query.tokenize('co-op "super metroid" 90s')
        self.assertIn(("phrase", "super metroid"), tokens)


def _handler():
    from handlers.library import LibraryHandlers

    class MockHandler(LibraryHandlers):
        def __init__(self):
            self._sent = None
            self.headers = {}

        def send_json(self, code, body):
            self._sent = (code, body)

    return MockHandler()


class ParseRouteTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["OPENBOX_DATA_DIR"] = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()
        os.environ.pop("OPENBOX_DATA_DIR", None)

    def _save(self, games=(), settings=None, **extra):
        import openbox
        state = {"schema_version": 6, "games": list(games),
                 "settings": settings or {}, "profiles": {}}
        state.update(extra)
        openbox.STATE_STORE.save(state)

    def test_route_registered(self):
        from routes import POST_TABLE
        self.assertEqual(
            POST_TABLE["/api/v2/library/query/parse"],
            "_api_post_api_v2_library_query_parse",
        )

    def test_parse_route_returns_chips_rules_and_count(self):
        self._save(games=[
            {"name": "A", "genre": "RPG", "play_count": 0, "last_played": "",
             "time_to_beat_hours": 3},
            {"name": "B", "genre": "RPG", "play_count": 5, "last_played": "2026-01-01",
             "time_to_beat_hours": 3},
        ])
        handler = _handler()
        handler._api_post_api_v2_library_query_parse({"query": "short unplayed rpg"})
        code, body = handler._sent
        self.assertEqual(code, 200)
        self.assertTrue(body["ok"])
        self.assertTrue(body["parsed"])
        self.assertEqual(body["rules"], {})
        self.assertIn({"kind": "unplayed"}, body["clauses"])
        self.assertEqual(len(body["chips"]), 3)
        self.assertEqual(body["match_count"], 1)
        self.assertEqual(len(body["matched_game_ids"]), 1)

    def test_parse_route_never_mutates_state(self):
        self._save(games=[{"name": "A", "genre": "RPG"}])
        import openbox
        before = openbox.STATE_STORE.signature()
        handler = _handler()
        handler._api_post_api_v2_library_query_parse({"query": "unplayed rpg"})
        self.assertEqual(handler._sent[0], 200)
        self.assertEqual(openbox.STATE_STORE.signature(), before)
        from webapp_state import load_state_view
        self.assertEqual(len(load_state_view()["games"]), 1)

    def test_parse_route_rejects_non_string(self):
        from api_errors import BadRequest
        self._save()
        handler = _handler()
        with self.assertRaises(BadRequest):
            handler._api_post_api_v2_library_query_parse({"query": 42})

    def test_parse_route_empty_query(self):
        self._save()
        handler = _handler()
        handler._api_post_api_v2_library_query_parse({"query": ""})
        code, body = handler._sent
        self.assertEqual(code, 200)
        self.assertFalse(body["parsed"])
        self.assertEqual(body["match_count"], 0)

    def test_parse_route_rejects_oversized(self):
        from api_errors import BadRequest
        self._save()
        handler = _handler()
        with self.assertRaises(BadRequest):
            handler._api_post_api_v2_library_query_parse({"query": "x " * 4000})


if __name__ == "__main__":
    unittest.main()
