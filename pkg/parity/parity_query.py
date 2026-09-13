"""parity_query.py — "just say it" library query grammar (1.11.0).

A deterministic semi-natural grammar for the library search bar. The pipeline
is tokenizer -> rule AST -> compiled constraints:

- ``tokenize(text)`` splits the input into word / quoted-phrase tokens.
- ``parse_tokens(tokens)`` scans the tokens left to right, matching the
  longest grammar pattern at each position, and returns the rule AST (a list
  of nodes) plus leftover words that no pattern claimed.
- ``compile_nodes(nodes, leftover)`` folds the AST into ``rules`` (the
  filter-preset vocabulary of ``parity_filter_presets``), ``clauses`` (the
  extended predicates this module adds on top), ``chips`` (ADR-0020 chip
  descriptors covering every emitted constraint) and the ``unparsed`` hint.
- ``parse_query(text)`` runs the whole pipeline; ``game_matches_query``
  evaluates a game against a parsed query — preset rules through the shared
  ``game_matches_rules`` plus every clause through the matchers below, so the
  applied filter can never be broader than what the chips show.

Design rules (spec): an ambiguous phrase takes the safest interpretation and
shows it as chips; input nothing parses stays a plain substring search with a
hint; a clause that has no data to check excludes the game rather than
guessing — an honest empty result beats a silently wrong filter.
"""

from __future__ import annotations

import re
from datetime import datetime

from pkg.parity.parity_filter_presets import game_matches_rules, rules_to_chips

# ── Tunable thresholds (named constants, surfaced in chips + grammar doc) ────
SHORT_GAME_HOURS = 5.0        # "short" / "quick" -> time-to-beat ceiling
LONG_GAME_HOURS = 20.0        # "long" / "epic" -> time-to-beat floor
HIGH_RATING_MIN = 4.0         # "highly rated"
TOP_RATING_MIN = 4.5          # "top rated" / "best rated"
RETRO_YEAR_MAX = 2000         # "retro" / "classic" -> released <= 2000
RECENT_DAYS = 31              # "recently played" / "newly added" window
DAYS_PER = {"y": 365, "mo": 30, "w": 7, "d": 1}   # year/month/week/day units
FINISHED_PROGRESS = ("Beaten", "Completed", "Mastered")
BACKLOG_PROGRESS = ("", "Playing", "Paused")
PLAYING_PROGRESS = ("Playing", "Paused")
KIDS_ESRB = ("E", "E10+", "EC")
MATURE_ESRB = ("M", "AO")

# ── Tokenizer ────────────────────────────────────────────────────────────────
_TOKEN_RE = re.compile(r'"([^"]*)"|([^\s"]+)')
# Word-edge punctuation stripped for matching; inner '-', '+' and ':' survive
# so "co-op", "4+", "e10+" and "genre:rpg" stay intact.
_EDGE_PUNCT = "!\"$&()*,./;<=>?@[\\]^_`{|}~"
_WORD_NUMBERS = {"one": 1, "two": 2, "three": 3, "four": 4,
                 "five": 5, "six": 6, "seven": 7, "eight": 8}


def tokenize(text):
    """Split query text into ``("word", raw)`` / ``("phrase", inner)`` tokens."""
    norm = " ".join(str(text or "").casefold().replace("\u2019", "'").split())
    tokens = []
    for match in _TOKEN_RE.finditer(norm):
        if match.group(1) is not None:
            tokens.append(("phrase", match.group(1).strip()))
        else:
            tokens.append(("word", match.group(2)))
    return tokens


def _clean(word):
    """Strip edge punctuation and a stray trailing apostrophe from a word."""
    return word.strip(_EDGE_PUNCT).rstrip("'")


# ── Token specs ──────────────────────────────────────────────────────────────
WORD = "any-word"          # matches a word or quoted-phrase token
PHRASE = "quoted-phrase"   # matches quoted-phrase tokens only
_NEG = re.compile(r"haven'?t|havent|hasn'?t|hasnt|ain'?t|aint|never|not")
_UNIT = re.compile(r"years?|yrs?|months?|mos?|weeks?|wks?|days?")
_NUM = re.compile(r"\d+")
_NUMWORD = re.compile(r"\d+|one|two|three|four|five|six|seven|eight")
_HOURS = re.compile(r"h|hrs?|hours?")
_YEAR4 = re.compile(r"(?:19[7-9]\d|20\d{2})")
_DECADE2 = re.compile(r"\d{2}'?s")
_DECADE4 = re.compile(r"(?:19|20)\d{2}s")
_RATING = re.compile(r"\d(?:\.\d)?\+?")
_STARS = re.compile(r"stars?")
_PLAYERS = re.compile(r"players?|people|peeps|ppl|friends?")
_ESRB_WORD = re.compile(r"e10\+?|e|t|m|ao|rp|ec")
_TYPED = re.compile(r"([a-z_]+):(.+)")
_NEGATED = re.compile(r"-\w+")
_HASHTAG = re.compile(r"#\w+")


def _numword(word):
    """Parse a digit token or a small number word; None when not numeric."""
    if word.isdigit():
        return int(word)
    return _WORD_NUMBERS.get(word)


def _unit_days(word):
    """Map a time-unit word (year/month/week/day) to its day count."""
    for prefix, days in DAYS_PER.items():
        if word.startswith(prefix):
            return days
    return 1


# ── AST node emitters ────────────────────────────────────────────────────────
def _clause(**clause):
    return [{"kind": "clause", "clause": clause}]


def _rule(key, value):
    return [{"kind": "rule", "key": key, "value": value}]


def _genre(*terms):
    return [{"kind": "genre", "terms": list(terms)}]


def _progress(*values):
    return [{"kind": "progress", "values": list(values)}]


def _esrb(*values):
    return [{"kind": "esrb", "values": list(values)}]


def _flag(field, value=True):
    return _clause(kind="flag", field=field, value=value)


def _genre_node(terms):
    return [{"kind": "genre", "terms": list(terms)}]


# ── Stopwords / connectors: consumed silently when no pattern claims them ────
STOPWORDS = {
    "a", "an", "the", "i", "me", "my", "we", "us", "you", "your", "yours",
    "he", "she", "his", "her", "its", "it", "they", "them", "their",
    "want", "wanna", "would", "like", "feel", "let's", "lets", "let",
    "to", "play", "some", "something", "any", "game", "games", "genre",
    "series", "please", "show", "find", "list", "give", "gimme", "that",
    "this", "these", "those", "and", "or", "with", "has", "have", "having",
    "for", "on", "in", "by", "from", "at", "of", "is", "are", "was", "were",
    "be", "been", "get", "got", "do", "does", "did", "can", "could", "should",
    "shall", "will", "just", "really", "very", "much", "kind", "kinda",
    "sort", "sorta", "maybe", "now", "today", "tonight", "all", "every",
    "each", "both", "either", "neither", "more", "most", "less", "least",
    "than", "then", "when", "while", "into", "over", "under", "again",
    "still", "yet", "already", "soon", "later", "ago", "before", "after",
    "since", "until", "till", "up", "down", "out", "about", "around",
    "through", "via", "per", "also", "too", "as", "well", "even", "only",
    "quite", "rather", "pretty", "so", "such", "what", "which", "who",
    "where", "why", "how", "if", "because", "unless", "except", "but",
    "nor", "not", "no", "without", "never", "haven't", "havent", "hasn't",
    "hasnt", "i'm", "im", "i've", "ive", "stuff", "things", "thing",
}

# Words that must never be treated as a platform/store/credit/tag target even
# when they follow a connector ("for free", "on sale", ...).
_TARGET_BLOCKLIST = STOPWORDS | {
    "sale", "free", "cheap", "real", "sure", "fun", "good", "great", "best",
    "now", "later", "tonight", "kids", "me", "us",
}

_SOURCE_WORDS = (
    "steam", "epic", "gog", "lutris", "heroic", "itch", "gameyfin",
    "battlenet", "origin", "uplay", "humble", "amazon",
)

_ESRB_CANON = {
    "e": "E", "everyone": "E", "e10": "E10+", "e10+": "E10+",
    "t": "T", "teen": "T", "m": "M", "mature": "M", "ao": "AO",
    "adults": "AO", "rp": "RP", "ec": "EC",
}

# Genre aliases: trigger words -> substring terms matched against game.genre.
# Terms are stems ("shoot" covers Shooter / Shoot 'em up) or explicit lists
# for families a single substring cannot cover (fps, rts, open world...).
_GENRE_TABLE = [
    (("role", "playing"), ["rpg", "role-playing", "role playing"]),
    (("role-playing",), ["rpg", "role-playing", "role playing"]),
    (("rpg",), ["rpg", "role-playing", "role playing"]),
    (("jrpg",), ["jrpg", "j-rpg"]),
    (("platformers",), ["platform"]), (("platformer",), ["platform"]),
    (("platforming",), ["platform"]), (("platform",), ["platform"]),
    (("shooters",), ["shoot"]), (("shooter",), ["shoot"]),
    (("fps",), ["fps", "first-person shooter", "first person shooter", "shooter"]),
    (("puzzles",), ["puzzl"]), (("puzzle",), ["puzzl"]), (("puzzler",), ["puzzl"]),
    (("strategy",), ["strategy"]), (("strategic",), ["strategy"]),
    (("rts",), ["rts", "real-time strategy", "real time strategy"]),
    (("racing",), ["rac"]), (("racer",), ["rac"]),
    (("driving",), ["rac", "driv"]),
    (("sports",), ["sport"]), (("sport",), ["sport"]),
    (("fighting",), ["fight"]), (("fighter",), ["fight"]),
    (("survival", "horror"), ["survival horror"]),
    (("horror",), ["horror"]),
    (("survival",), ["survival"]),
    (("metroidvania",), ["metroidvania"]),
    (("roguelike",), ["rogue"]), (("roguelite",), ["rogue"]),
    (("rogue-like",), ["rogue"]),
    (("open", "world"), ["open world", "open-world"]),
    (("open-world",), ["open world", "open-world"]),
    (("visual", "novels"), ["visual novel"]),
    (("visual", "novel"), ["visual novel"]),
    (("point", "and", "click"), ["point and click", "point-and-click"]),
    (("point-and-click",), ["point and click", "point-and-click"]),
    (("beat", "em", "up"), ["beat em up", "beat 'em up", "beat-em-up", "brawler"]),
    (("beat-em-up",), ["beat em up", "beat 'em up", "beat-em-up", "brawler"]),
    (("brawler",), ["brawler", "beat"]),
    (("shmup",), ["shmup", "shoot 'em up", "shoot em up"]),
    (("shoot", "em", "up"), ["shoot 'em up", "shoot em up", "shmup"]),
    (("bullet", "hell"), ["bullet hell"]),
    (("soulslike",), ["soulslike", "souls-like", "souls"]),
    (("souls-like",), ["soulslike", "souls-like", "souls"]),
    (("souls",), ["soulslike", "souls-like", "souls"]),
    (("indie",), ["indie"]),
    (("action",), ["action"]),
    (("adventure",), ["adventure"]),
    (("stealth",), ["stealth"]),
    (("sandbox",), ["sandbox"]),
    (("arcade",), ["arcade"]),
    (("pinball",), ["pinball"]),
    (("card",), ["card"]), (("cards",), ["card"]),
    (("board",), ["board"]),
    (("educational",), ["educat"]),
    (("simulation",), ["sim"]), (("simulator",), ["sim"]), (("sim",), ["sim"]),
    (("rhythm",), ["rhythm"]),
    (("moba",), ["moba"]),
    (("mmorpg",), ["mmo", "mmorpg"]), (("mmo",), ["mmo", "mmorpg"]),
    (("tower", "defense"), ["tower defense"]),
    (("tower", "defence"), ["tower defence", "tower defense"]),
    (("turn", "based"), ["turn-based", "turn based"]),
    (("turn-based",), ["turn-based", "turn based"]),
    (("trivia",), ["trivia", "quiz"]), (("quiz",), ["quiz", "trivia"]),
    (("casual",), ["casual"]),
    (("walking", "simulator"), ["walking sim", "walking simulator"]),
    (("walking", "sim"), ["walking sim", "walking simulator"]),
]


def _idle_num(words, now=None):
    return _clause(kind="idle_days", days=int(words[3]) * _unit_days(words[4]))


def _idle_unit(words, now=None):
    return _clause(kind="idle_days", days=_unit_days(words[4]))


def _idle_over_num(words, now=None):
    return _clause(kind="idle_days", days=int(words[4]) * _unit_days(words[5]))


def _idle_over_unit(words, now=None):
    return _clause(kind="idle_days", days=_unit_days(words[5]))


def _idle_since(words, now=None):
    return _clause(kind="idle_before", year=int(words[3]))


def _added_year_now(words, now):
    return _clause(kind="added_year", year=(now or datetime.now()).year)


def _this_year(words, now):
    year = (now or datetime.now()).year
    return _clause(kind="year_range", min=year, max=year)


def _ttb_max_const(words, now=None):
    return _clause(kind="ttb_max", hours=SHORT_GAME_HOURS)


def _ttb_min_const(words, now=None):
    return _clause(kind="ttb_min", hours=LONG_GAME_HOURS)


def _ttb_under(words, now=None):
    return _clause(kind="ttb_max", hours=float(_numword(words[1]) or 0))


def _ttb_less_than(words, now=None):
    return _clause(kind="ttb_max", hours=float(_numword(words[2]) or 0))


def _ttb_or_less(words, now=None):
    return _clause(kind="ttb_max", hours=float(_numword(words[0]) or 0))


def _ttb_over(words, now=None):
    return _clause(kind="ttb_min", hours=float(_numword(words[1]) or 0))


def _ttb_more_than(words, now=None):
    return _clause(kind="ttb_min", hours=float(_numword(words[2]) or 0))


def _ttb_at_least(words, now=None):
    return _clause(kind="ttb_min", hours=float(_numword(words[2]) or 0))


def _ttb_no_more(words, now=None):
    return _clause(kind="ttb_max", hours=float(_numword(words[3]) or 0))


def _ttb_plus_hours(words, now=None):
    return _clause(kind="ttb_min", hours=float(words[0].rstrip("+")))


def _rating_rated(words, now=None):
    return _clause(kind="rating_min", value=float(words[1].rstrip("+")))


def _rating_stars(words, now=None):
    value = _numword(words[0].rstrip("+"))
    if value is None:
        return None
    return _clause(kind="rating_min", value=float(value))


def _decade(words, now=None):
    digits = re.sub(r"\D", "", words[0])
    if len(digits) == 4:
        start = int(digits) // 10 * 10
    else:
        pair = int(digits[:2])
        start = (1900 if pair >= 40 else 2000) + pair
    return _clause(kind="year_range", min=start, max=start + 9)


def _year_exact(words, now=None):
    return _clause(kind="year_range", min=int(words[-1]), max=int(words[-1]))


def _year_before(words, now=None):
    return _clause(kind="year_range", min=None, max=int(words[-1]) - 1)


def _year_after(words, now=None):
    return _clause(kind="year_range", min=int(words[-1]) + 1, max=None)


def _year_since(words, now=None):
    return _clause(kind="year_range", min=int(words[-1]), max=None)


def _retro(words, now=None):
    return _clause(kind="year_range", min=None, max=RETRO_YEAR_MAX)


def _esrb_rated(words, now=None):
    return _esrb(_ESRB_CANON[words[1]])


def _esrb_typed_word(words, now=None):
    return _esrb(_ESRB_CANON[words[0]])


def _kids(words, now=None):
    return _clause(kind="esrb_any", values=list(KIDS_ESRB))


def _mature(words, now=None):
    return _clause(kind="esrb_any", values=list(MATURE_ESRB))


def _players_min(words, now=None):
    for word in reversed(words):
        value = _numword(word.rstrip("+"))
        if value is None:
            digits = re.match(r"\d+", word)
            value = _numword(digits.group(0)) if digits else None
        if value is not None:
            return _clause(kind="players_min", value=value)
    return None


def _players_dash(words, now=None):
    digits = re.match(r"(\d+)", words[0])
    if not digits:
        return None
    return _clause(kind="players_min", value=int(digits.group(1)))


def _platform_any(words, now=None):
    term = _clean(words[-1])
    if not term or term in _TARGET_BLOCKLIST:
        return None
    return _clause(kind="platform_any", term=term)


def _credit(words, now=None):
    term = _clean(words[-1])
    if not term or term in _TARGET_BLOCKLIST:
        return None
    return _clause(kind="credit", term=term)


def _source(words, now=None):
    for word in words:
        if word in _SOURCE_WORDS:
            return _clause(kind="source", term=word)
    return None


def _tag(words, now=None):
    term = _clean(words[-1]).lstrip("#")
    if not term or term in _TARGET_BLOCKLIST:
        return None
    return _clause(kind="tag", term=term)


def _hashtag(words, now=None):
    return _clause(kind="tag", term=words[0].lstrip("#"))


def _not_query(words, now=None):
    term = _clean(words[-1]).lstrip("-")
    if not term or term in _TARGET_BLOCKLIST:
        return None
    return _clause(kind="not_query", term=term)


def _hundred(words, now=None):
    return _clause(kind="progress_any", values=["Completed", "Mastered"])


def _playing(words, now=None):
    return _clause(kind="progress_any", values=list(PLAYING_PROGRESS))


def _finished(words, now=None):
    return _clause(kind="progress_any", values=list(FINISHED_PROGRESS))


def _typed(words, now=None):
    """Handle ``key:value`` tokens with the same vocabulary as the grammar."""
    match = _TYPED.fullmatch(words[0])
    if not match:
        return None
    key, value = match.group(1), _clean(match.group(2))
    if not value:
        return None
    if key == "genre":
        return _clause(kind="genre_any", terms=[value])
    if key in ("platform", "plat"):
        return _clause(kind="platform_any", term=value)
    if key in ("dev", "developer"):
        return _rule("developer", value)
    if key in ("pub", "publisher"):
        return _rule("publisher", value)
    if key == "series":
        return _clause(kind="series", term=value)
    if key == "region":
        return _clause(kind="region", term=value)
    if key == "progress":
        canon = next((item for item in ("Playing", "Paused", "Beaten", "Completed", "Mastered", "Abandoned")
                      if item.casefold() == value.casefold()), None)
        if canon is None:
            return _clause(kind="progress_any", values=[value])
        return _rule("progress", canon)
    if key == "esrb":
        canon = _ESRB_CANON.get(value.casefold(), value.upper())
        return _rule("esrb", canon)
    if key == "rating":
        try:
            return _clause(kind="rating_min", value=float(value.rstrip("+")))
        except ValueError:
            return None
    if key == "year":
        digits = re.sub(r"\D", "", value)
        if len(digits) != 4:
            return None
        return _clause(kind="year_range", min=int(digits), max=int(digits))
    if key in ("player", "players"):
        num = _numword(value)
        return _clause(kind="players_min", value=num) if num else None
    if key in ("tag", "tags"):
        return _clause(kind="tag", term=value)
    if key in ("source", "store", "storefront"):
        return _clause(kind="source", term=value)
    if key in ("favorite", "fav"):
        return _rule("favorite", value in ("yes", "true", "1"))
    if key == "installed":
        return _rule("installed", "uninstalled" if value in ("no", "false", "0") else "installed")
    if key in ("hide", "hidden"):
        return _rule("hidden", value in ("yes", "true", "1"))
    if key == "unplayed":
        return _clause(kind="unplayed") if value in ("yes", "true", "1") else _clause(kind="played")
    if key == "saves":
        return _rule("has_saves", value in ("yes", "true", "1"))
    if key in ("achievements", "achievement"):
        return _rule("has_achievements", value in ("yes", "true", "1"))
    if key == "notes":
        return _flag("notes", value in ("yes", "true", "1"))
    return None


def _source_game(words, now=None):
    return _source(words)


# ── Grammar table: (token specs) -> handler ─────────────────────────────────
# At each position the longest matching pattern wins; equal-length patterns
# keep declaration order, so specific connectors are declared before generic
# catch-alls ("for 4" before "for <platform>", "on hold" before "on <plat>").
def _build_grammar():
    table = [
        # recency / played state
        ((_NEG, "played", "in", "over", _NUM, _UNIT), _idle_over_num),
        ((_NEG, "played", "in", "over", "a", _UNIT), _idle_over_unit),
        ((_NEG, "played", "in", _NUM, _UNIT), _idle_num),
        ((_NEG, "played", "in", re.compile(r"an?"), _UNIT), _idle_unit),
        ((_NEG, "played", "since", _YEAR4), _idle_since),
        ((_NEG, "played"), lambda w, now=None: _clause(kind="unplayed")),
        ((_NEG, "started"), lambda w, now=None: _clause(kind="unplayed")),
        (("unplayed",), lambda w, now=None: _clause(kind="unplayed")),
        (("untouched",), lambda w, now=None: _clause(kind="unplayed")),
        (("unstarted",), lambda w, now=None: _clause(kind="unplayed")),
        (("unopened",), lambda w, now=None: _clause(kind="unplayed")),
        (("played", "recently"), lambda w, now=None: _clause(kind="played_within", days=RECENT_DAYS)),
        (("recently", "played"), lambda w, now=None: _clause(kind="played_within", days=RECENT_DAYS)),
        (("played", "lately"), lambda w, now=None: _clause(kind="played_within", days=RECENT_DAYS)),
        (("recently", "added"), lambda w, now=None: _clause(kind="added_days", days=RECENT_DAYS)),
        (("newly", "added"), lambda w, now=None: _clause(kind="added_days", days=RECENT_DAYS)),
        (("just", "added"), lambda w, now=None: _clause(kind="added_days", days=RECENT_DAYS)),
        (("added", "recently"), lambda w, now=None: _clause(kind="added_days", days=RECENT_DAYS)),
        (("new", "to", "the", "library"), lambda w, now=None: _clause(kind="added_days", days=RECENT_DAYS)),
        (("new", "to", "library"), lambda w, now=None: _clause(kind="added_days", days=RECENT_DAYS)),
        (("added", "this", "year"), _added_year_now),
        (("this", "year"), _this_year),
        (("played",), lambda w, now=None: _clause(kind="played")),
        (("unbeaten",), lambda w, now=None: _clause(kind="not_finished")),
        (("unfinished",), lambda w, now=None: _clause(kind="not_finished")),
        ((_NEG, "finished"), lambda w, now=None: _clause(kind="not_finished")),
        ((_NEG, "beaten"), lambda w, now=None: _clause(kind="not_finished")),
        ((_NEG, "completed"), lambda w, now=None: _clause(kind="not_finished")),
        (("backlog",), lambda w, now=None: _clause(kind="backlog")),
        (("pile", "of", "shame"), lambda w, now=None: _clause(kind="backlog")),
        (("playing",), _playing),
        (("in", "progress"), _playing),
        (("currently", "playing"), _playing),
        (("still", "playing"), _playing),
        (("beaten",), lambda w, now=None: _progress("Beaten")),
        (("beat",), lambda w, now=None: _progress("Beaten")),
        (("completed",), lambda w, now=None: _progress("Completed")),
        (("complete",), lambda w, now=None: _progress("Completed")),
        (("mastered",), lambda w, now=None: _progress("Mastered")),
        (("finished",), _finished),
        (("100%",), _hundred),
        (("hundred", "percent"), _hundred),
        (("abandoned",), lambda w, now=None: _progress("Abandoned")),
        (("dropped",), lambda w, now=None: _progress("Abandoned")),
        (("gave", "up"), lambda w, now=None: _progress("Abandoned")),
        (("gave", "up", "on"), lambda w, now=None: _progress("Abandoned")),
        (("paused",), lambda w, now=None: _progress("Paused")),
        (("on", "hold"), lambda w, now=None: _progress("Paused")),
        (("parked",), lambda w, now=None: _progress("Paused")),
        # length
        (("short",), _ttb_max_const),
        (("quick",), _ttb_max_const),
        (("bite-sized",), _ttb_max_const),
        (("bite", "sized"), _ttb_max_const),
        (("tiny",), _ttb_max_const),
        (("less", "than", _NUMWORD, _HOURS), _ttb_less_than),
        (("no", "more", "than", _NUMWORD, _HOURS), _ttb_no_more),
        (("under", _NUMWORD, _HOURS), _ttb_under),
        ((_NUM, _HOURS, "or", "less"), _ttb_or_less),
        ((_NUM, _HOURS, "or", "shorter"), _ttb_or_less),
        (("long",), _ttb_min_const),
        (("epic",), _ttb_min_const),
        (("lengthy",), _ttb_min_const),
        (("more", "than", _NUMWORD, _HOURS), _ttb_more_than),
        (("at", "least", _NUMWORD, _HOURS), _ttb_at_least),
        (("over", _NUMWORD, _HOURS), _ttb_over),
        ((re.compile(r"\d+\+"), _HOURS), _ttb_plus_hours),
        # rating
        (("rated", _NUM, "or", re.compile(r"higher|better|more|up")), _rating_rated),
        (("rated", "at", "least", _NUM), _rating_rated),
        (("rated", _RATING), _rating_rated),
        ((_NUMWORD, _STARS), _rating_stars),
        ((_RATING, _STARS), _rating_stars),
        (("highly", "rated"), lambda w, now=None: _clause(kind="rating_min", value=HIGH_RATING_MIN)),
        (("top", "rated"), lambda w, now=None: _clause(kind="rating_min", value=TOP_RATING_MIN)),
        (("best", "rated"), lambda w, now=None: _clause(kind="rating_min", value=TOP_RATING_MIN)),
        (("unrated",), lambda w, now=None: _clause(kind="unrated")),
        (("no", "rating"), lambda w, now=None: _clause(kind="unrated")),
        (("not", "rated"), lambda w, now=None: _clause(kind="unrated")),
        # esrb
        (("rated", _ESRB_WORD), _esrb_rated),
        (("esrb", _ESRB_WORD), _esrb_rated),
        (("rated", "mature"), lambda w, now=None: _esrb("M")),
        (("rated", "teen"), lambda w, now=None: _esrb("T")),
        (("rated", "everyone"), lambda w, now=None: _esrb("E")),
        (("rated", "adults"), lambda w, now=None: _esrb("AO")),
        (("esrb", "mature"), lambda w, now=None: _esrb("M")),
        (("adults", "only"), lambda w, now=None: _esrb("AO")),
        (("mature",), _mature),
        (("adult",), _mature),
        (("for", "the", "kids"), _kids),
        (("for", "kids"), _kids),
        (("kids",), _kids),
        (("kid-friendly",), _kids),
        (("family", "friendly"), _kids),
        (("family",), _kids),
        # era
        ((_DECADE4,), _decade),
        ((_DECADE2,), _decade),
        (("released", "in", _YEAR4), _year_exact),
        (("made", "in", _YEAR4), _year_exact),
        (("released", _YEAR4), _year_exact),
        (("from", _YEAR4), _year_exact),
        (("in", _YEAR4), _year_exact),
        (("before", _YEAR4), _year_before),
        (("prior", "to", _YEAR4), _year_before),
        (("pre", _YEAR4), _year_before),
        (("after", _YEAR4), _year_after),
        (("since", _YEAR4), _year_since),
        (("retro",), _retro),
        (("old", "school"), _retro),
        (("old-school",), _retro),
        (("oldschool",), _retro),
        (("classic",), _retro),
        (("vintage",), _retro),
        # players / co-op
        (("single", "player"), lambda w, now=None: _clause(kind="players_max", value=1)),
        (("single-player",), lambda w, now=None: _clause(kind="players_max", value=1)),
        (("singleplayer",), lambda w, now=None: _clause(kind="players_max", value=1)),
        (("one", "player"), lambda w, now=None: _clause(kind="players_max", value=1)),
        (("solo",), lambda w, now=None: _clause(kind="players_max", value=1)),
        (("up", "to", _NUMWORD, _PLAYERS), _players_min),
        (("for", _NUMWORD, _PLAYERS), _players_min),
        ((_NUMWORD, _PLAYERS), _players_min),
        ((re.compile(r"\d+-players?"),), _players_dash),
        (("for", _NUMWORD), _players_min),
        (("local", "co-op"), lambda w, now=None: _clause(kind="coop")),
        (("local", "coop"), lambda w, now=None: _clause(kind="coop")),
        (("couch", "co-op"), lambda w, now=None: _clause(kind="coop")),
        (("couch", "coop"), lambda w, now=None: _clause(kind="coop")),
        (("co-operative",), lambda w, now=None: _clause(kind="coop")),
        (("cooperative",), lambda w, now=None: _clause(kind="coop")),
        (("co-op",), lambda w, now=None: _clause(kind="coop")),
        (("coop",), lambda w, now=None: _clause(kind="coop")),
        (("co", "op"), lambda w, now=None: _clause(kind="coop")),
        (("couch",), lambda w, now=None: _clause(kind="coop")),
        (("local", "multiplayer"), lambda w, now=None: _clause(kind="multiplayer")),
        (("local", "multi", "player"), lambda w, now=None: _clause(kind="multiplayer")),
        (("multi-player",), lambda w, now=None: _clause(kind="multiplayer")),
        (("multi", "player"), lambda w, now=None: _clause(kind="multiplayer")),
        (("multiplayer",), lambda w, now=None: _clause(kind="multiplayer")),
        (("split", "screen"), lambda w, now=None: _clause(kind="multiplayer")),
        (("split-screen",), lambda w, now=None: _clause(kind="multiplayer")),
        (("splitscreen",), lambda w, now=None: _clause(kind="multiplayer")),
        (("party", "games"), lambda w, now=None: _clause(kind="multiplayer")),
        (("party",), lambda w, now=None: _clause(kind="multiplayer")),
        # flags / preset booleans
        (("favorites",), lambda w, now=None: _rule("favorite", True)),
        (("favorite",), lambda w, now=None: _rule("favorite", True)),
        (("favourites",), lambda w, now=None: _rule("favorite", True)),
        (("favourite",), lambda w, now=None: _rule("favorite", True)),
        (("faves",), lambda w, now=None: _rule("favorite", True)),
        (("not", "favorites"), lambda w, now=None: _rule("favorite", False)),
        (("not", "favorite"), lambda w, now=None: _rule("favorite", False)),
        (("unfavorited",), lambda w, now=None: _rule("favorite", False)),
        (("unfavourited",), lambda w, now=None: _rule("favorite", False)),
        (("hidden",), lambda w, now=None: _rule("hidden", True)),
        (("unhidden",), lambda w, now=None: _rule("hidden", False)),
        (("not", "hidden"), lambda w, now=None: _rule("hidden", False)),
        (("installed",), lambda w, now=None: _rule("installed", "installed")),
        (("uninstalled",), lambda w, now=None: _rule("installed", "uninstalled")),
        (("not", "installed"), lambda w, now=None: _rule("installed", "uninstalled")),
        (("owned",), lambda w, now=None: _clause(kind="owned")),
        (("own",), lambda w, now=None: _clause(kind="owned")),
        (("with", "saves"), lambda w, now=None: _rule("has_saves", True)),
        (("has", "saves"), lambda w, now=None: _rule("has_saves", True)),
        (("have", "saves"), lambda w, now=None: _rule("has_saves", True)),
        (("save", "files"), lambda w, now=None: _rule("has_saves", True)),
        (("saves",), lambda w, now=None: _rule("has_saves", True)),
        (("no", "saves"), lambda w, now=None: _rule("has_saves", False)),
        (("without", "saves"), lambda w, now=None: _rule("has_saves", False)),
        (("no", "save", "files"), lambda w, now=None: _rule("has_saves", False)),
        (("with", "achievements"), lambda w, now=None: _rule("has_achievements", True)),
        (("has", "achievements"), lambda w, now=None: _rule("has_achievements", True)),
        (("achievements",), lambda w, now=None: _rule("has_achievements", True)),
        (("achievement", "support"), lambda w, now=None: _rule("has_achievements", True)),
        (("no", "achievements"), lambda w, now=None: _rule("has_achievements", False)),
        (("without", "achievements"), lambda w, now=None: _rule("has_achievements", False)),
        (("missing", "media"), lambda w, now=None: _rule("has_missing_media", True)),
        (("missing", "covers"), lambda w, now=None: _rule("has_missing_media", True)),
        (("missing", "cover"), lambda w, now=None: _rule("has_missing_media", True)),
        (("missing", "art"), lambda w, now=None: _rule("has_missing_media", True)),
        (("no", "cover"), lambda w, now=None: _rule("has_missing_media", True)),
        (("no", "covers"), lambda w, now=None: _rule("has_missing_media", True)),
        (("without", "covers"), lambda w, now=None: _rule("has_missing_media", True)),
        (("without", "cover"), lambda w, now=None: _rule("has_missing_media", True)),
        (("no", "art"), lambda w, now=None: _rule("has_missing_media", True)),
        (("with", "covers"), lambda w, now=None: _rule("has_missing_media", False)),
        (("with", "cover"), lambda w, now=None: _rule("has_missing_media", False)),
        (("has", "covers"), lambda w, now=None: _rule("has_missing_media", False)),
        (("high", "scores"), lambda w, now=None: _rule("has_highscores", True)),
        (("highscores",), lambda w, now=None: _rule("has_highscores", True)),
        (("with", "high", "scores"), lambda w, now=None: _rule("has_highscores", True)),
        (("leaderboards",), lambda w, now=None: _rule("has_highscores", True)),
        (("shelf", "entries"), lambda w, now=None: _flag("manual_entry")),
        (("shelf", "entry"), lambda w, now=None: _flag("manual_entry")),
        (("shelf",), lambda w, now=None: _flag("manual_entry")),
        (("manual", "entries"), lambda w, now=None: _flag("manual_entry")),
        (("manual", "entry"), lambda w, now=None: _flag("manual_entry")),
        (("physical",), lambda w, now=None: _flag("manual_entry")),
        (("broken",), lambda w, now=None: _flag("broken")),
        (("not", "working"), lambda w, now=None: _flag("broken")),
        (("working",), lambda w, now=None: _flag("broken", False)),
        (("portable",), lambda w, now=None: _flag("portable")),
        (("controller", "support"), lambda w, now=None: _flag("controller_support")),
        (("controller",), lambda w, now=None: _flag("controller_support")),
        (("gamepad", "support"), lambda w, now=None: _flag("controller_support")),
        (("gamepad",), lambda w, now=None: _flag("controller_support")),
        (("with", "notes"), lambda w, now=None: _flag("notes")),
        (("has", "notes"), lambda w, now=None: _flag("notes")),
        (("noted",), lambda w, now=None: _flag("notes")),
        (("no", "notes"), lambda w, now=None: _flag("notes", False)),
        (("without", "notes"), lambda w, now=None: _flag("notes", False)),
        # platform / store / credit / tag (generic catch-alls LAST per prefix)
        (("on", WORD), lambda w, now=None: _source(w) or _platform_any(w)),
        (("for", WORD), _platform_any),
        (("from", WORD), lambda w, now=None: _source(w) or _credit(w)),
        (("by", WORD), _credit),
        (("made", "by", WORD), _credit),
        (("developed", "by", WORD), _credit),
        (("published", "by", WORD), _credit),
        (("tagged", "with", WORD), _tag),
        (("tagged", WORD), _tag),
        (("tag", WORD), _tag),
        ((_HASHTAG,), _hashtag),
        (("on", _YEAR4), _year_exact),
        # negation catch-alls (after every specific not/no/without pattern)
        (("not", WORD), _not_query),
        (("no", WORD), _not_query),
        (("never", WORD), _not_query),
        (("without", WORD), _not_query),
        ((_NEGATED,), _not_query),
        # typed key:value tokens
        ((_TYPED,), _typed),
    ]
    for word in _SOURCE_WORDS:
        table.append(((word, "games"), _source_game))
        table.append(((word, "library"), _source_game))
    for triggers, terms in _GENRE_TABLE:
        table.append((triggers, lambda w, now=None, terms=terms: _genre_node(terms)))
    # Longest match first; equal lengths keep declaration order.
    indexed = list(enumerate(table))
    indexed.sort(key=lambda item: (-len(item[1][0]), item[0]))
    return [(pattern, handler) for _, (pattern, handler) in indexed]


GRAMMAR = _build_grammar()


def _spec_matches(spec, kind, word):
    if spec is WORD:
        return True
    if spec is PHRASE:
        return kind == "phrase"
    if kind != "word":
        return False
    if isinstance(spec, str):
        return word == spec
    return bool(spec.fullmatch(word))


def parse_tokens(tokens, now=None):
    """Scan tokens left to right; returns ``(ast_nodes, leftover_words)``.

    Each node carries ``pos`` (token index) so chips can follow phrase order.
    Leftover entries are ``(pos, cleaned_word)`` tuples.
    """
    cleaned = [_clean(text) if kind == "word" else text for kind, text in tokens]
    nodes, leftover = [], []
    i = 0
    while i < len(tokens):
        kind = tokens[i][0]
        for pattern, handler in GRAMMAR:
            size = len(pattern)
            if i + size > len(tokens):
                continue
            if not all(_spec_matches(pattern[j], tokens[i + j][0], cleaned[i + j])
                       for j in range(size)):
                continue
            produced = handler(cleaned[i:i + size], now)
            if produced is None:
                continue
            for node in produced:
                node["pos"] = i
            nodes.extend(produced)
            break
        else:
            if kind == "phrase":
                if cleaned[i]:
                    leftover.append((i, cleaned[i]))
            elif cleaned[i] and cleaned[i] not in STOPWORDS:
                leftover.append((i, cleaned[i]))
            i += 1
            continue
        i += size
    return nodes, leftover


# ── Compile: fold the AST into rules + clauses + chips ──────────────────────
# Clause kinds that merge into a single tighter bound when repeated.
_BOUND_MERGE = {
    "ttb_max": ("hours", min), "ttb_min": ("hours", max),
    "rating_min": ("value", max),
    "players_min": ("value", max), "players_max": ("value", min),
    "idle_days": ("days", max), "played_within": ("days", min),
    "added_days": ("days", min),
}


def _merge_clause(clauses, clause):
    """Fold one clause into the list, merging bounds and deduping repeats."""
    kind = clause["kind"]
    if kind == "year_range":
        for existing in clauses:
            if existing["kind"] != "year_range":
                continue
            low = max(v for v in (existing.get("min"), clause.get("min")) if v is not None) \
                if existing.get("min") is not None and clause.get("min") is not None \
                else existing.get("min") if clause.get("min") is None else clause.get("min")
            high = min(v for v in (existing.get("max"), clause.get("max")) if v is not None) \
                if existing.get("max") is not None and clause.get("max") is not None \
                else existing.get("max") if clause.get("max") is None else clause.get("max")
            existing["min"], existing["max"] = low, high
            return
        clauses.append(clause)
        return
    bound = _BOUND_MERGE.get(kind)
    if bound is not None:
        field, combine = bound
        for existing in clauses:
            if existing["kind"] == kind:
                existing[field] = combine(existing[field], clause[field])
                return
        clauses.append(clause)
        return
    for existing in clauses:
        if existing == clause:
            return
    clauses.append(clause)


def _dedupe(values):
    seen, out = set(), []
    for value in values:
        if value not in seen:
            seen.add(value)
            out.append(value)
    return out


def _clause_chip(clause):
    """ADR-0020 chip descriptor for an extended clause."""
    kind = clause["kind"]
    chip = {"key": kind, "label": kind.replace("_", " ").title(),
            "value": None, "display": "", "i18n": f"sayit.chip.{kind}"}
    labels = {
        "unplayed": "Unplayed", "played": "Played", "backlog": "Backlog",
        "not_finished": "Not finished", "unrated": "Unrated",
        "multiplayer": "Multiplayer", "coop": "Co-op", "owned": "Owned",
        "year_range": "Year", "rating_min": "Rating",
        "players_min": "Players", "players_max": "Players",
        "idle_days": "Last played", "played_within": "Last played",
        "added_days": "Added", "added_year": "Added", "idle_before": "Last played",
        "ttb_max": "Length", "ttb_min": "Length",
        "platform_any": "Platform", "genre_any": "Genre", "credit": "By",
        "series": "Series", "region": "Region", "source": "Store",
        "tag": "Tag", "esrb_any": "ESRB", "not_query": "Exclude",
        "flag": "Flag", "progress_any": "Progress",
    }
    chip["label"] = labels.get(kind, chip["label"])
    if kind == "year_range":
        low, high = clause.get("min"), clause.get("max")
        chip["value"] = [low, high]
        if low is not None and high is not None:
            chip["display"] = f"{low}\u2013{high}" if low != high else str(low)
        elif low is not None:
            chip["display"] = f"\u2265 {low}"
        else:
            chip["display"] = f"\u2264 {high}"
    elif kind in ("rating_min",):
        chip["value"] = clause["value"]
        chip["display"] = f"\u2265 {clause['value']:g}"
    elif kind == "players_min":
        chip["value"] = clause["value"]
        chip["display"] = f"{clause['value']}+"
    elif kind == "players_max":
        chip["value"] = clause["value"]
        chip["display"] = f"\u2264 {clause['value']}"
    elif kind in ("idle_days",):
        chip["value"] = clause["days"]
        chip["display"] = f"\u2265 {clause['days']} days ago"
    elif kind == "played_within":
        chip["value"] = clause["days"]
        chip["display"] = f"\u2264 {clause['days']} days ago"
    elif kind == "added_days":
        chip["value"] = clause["days"]
        chip["display"] = f"\u2264 {clause['days']} days ago"
    elif kind == "added_year":
        chip["value"] = clause["year"]
        chip["display"] = str(clause["year"])
    elif kind == "idle_before":
        chip["value"] = clause["year"]
        chip["display"] = f"before {clause['year']}"
    elif kind in ("ttb_max", "ttb_min"):
        hours = clause["hours"]
        chip["value"] = hours
        sign = "\u2264" if kind == "ttb_max" else "\u2265"
        chip["display"] = f"{sign} {hours:g} h"
    elif kind in ("platform_any", "credit", "series", "region", "source", "tag", "not_query"):
        chip["value"] = clause["term"]
        chip["display"] = str(clause["term"])
    elif kind in ("genre_any", "esrb_any", "progress_any"):
        chip["value"] = list(clause["values"] if kind != "genre_any" else clause["terms"])
        chip["display"] = " / ".join(str(v) for v in chip["value"])
    elif kind == "flag":
        field_labels = {"manual_entry": "Shelf entry", "broken": "Broken",
                        "portable": "Portable", "controller_support": "Controller",
                        "notes": "Notes"}
        chip["key"] = f"flag_{clause['field']}"
        chip["label"] = field_labels.get(clause["field"], clause["field"])
        chip["value"] = clause.get("value", True)
        chip["display"] = "" if clause.get("value", True) else "No"
    elif kind == "owned":
        chip["display"] = "not installed"
    else:
        chip["value"] = True
    return chip


def compile_nodes(nodes, leftover):
    """Fold AST nodes + leftover words into rules/clauses/chips/unparsed/hint."""
    rules, clauses = {}, []
    clause_positions = {}
    order = []                    # (pos, seq, chip) keeps chips in phrase order
    seq = 0
    genre_terms, progress_vals, esrb_vals = [], [], []
    group_pos = {}
    for node in nodes:
        pos = node.get("pos", 0)
        kind = node["kind"]
        if kind == "rule":
            rules[node["key"]] = node["value"]
            order.append((pos, seq, ("rule", node["key"])))
            seq += 1
        elif kind == "genre":
            genre_terms.extend(node["terms"])
            group_pos.setdefault("genre", pos)
        elif kind == "progress":
            progress_vals.extend(node["values"])
            group_pos.setdefault("progress", pos)
        elif kind == "esrb":
            esrb_vals.extend(node["values"])
            group_pos.setdefault("esrb", pos)
        elif kind == "clause":
            before = len(clauses)
            _merge_clause(clauses, node["clause"])
            if len(clauses) > before:
                clause_positions[len(clauses) - 1] = pos
                order.append((pos, seq, ("clause", len(clauses) - 1)))
                seq += 1
    if genre_terms:
        terms = _dedupe(genre_terms)
        if len(terms) == 1:
            rules["genre"] = terms[0]
            order.append((group_pos["genre"], seq, ("rule", "genre")))
        else:
            before = len(clauses)
            _merge_clause(clauses, {"kind": "genre_any", "terms": terms})
            if len(clauses) > before:
                clause_positions[len(clauses) - 1] = group_pos["genre"]
            order.append((group_pos["genre"], seq, ("clause", len(clauses) - 1)))
        seq += 1
    if progress_vals:
        values = _dedupe(progress_vals)
        if len(values) == 1:
            rules["progress"] = values[0]
            order.append((group_pos["progress"], seq, ("rule", "progress")))
        else:
            before = len(clauses)
            _merge_clause(clauses, {"kind": "progress_any", "values": values})
            if len(clauses) > before:
                clause_positions[len(clauses) - 1] = group_pos["progress"]
            order.append((group_pos["progress"], seq, ("clause", len(clauses) - 1)))
        seq += 1
    if esrb_vals:
        values = _dedupe(esrb_vals)
        if len(values) == 1:
            rules["esrb"] = values[0]
            order.append((group_pos["esrb"], seq, ("rule", "esrb")))
        else:
            before = len(clauses)
            _merge_clause(clauses, {"kind": "esrb_any", "values": values})
            if len(clauses) > before:
                clause_positions[len(clauses) - 1] = group_pos["esrb"]
            order.append((group_pos["esrb"], seq, ("clause", len(clauses) - 1)))
        seq += 1
    if leftover:
        rules["query"] = " ".join(word for _, word in leftover)
        order.append((min(pos for pos, _ in leftover), seq, ("rule", "query")))
        seq += 1
    if len(clauses) > 1:
        permutation = sorted(
            range(len(clauses)),
            key=lambda index: (clause_positions.get(index, 10**9), index),
        )
        if permutation != list(range(len(clauses))):
            remap = {old: new for new, old in enumerate(permutation)}
            clauses = [clauses[old] for old in permutation]
            order = [
                (pos, item_seq,
                 ("clause", remap[ref[1]]) if ref[0] == "clause" else ref)
                for pos, item_seq, ref in order
            ]
    chips = []
    for _pos, _seq, ref in sorted(order):
        if ref[0] == "rule":
            chip = rules_to_chips({ref[1]: rules[ref[1]]})[0]
            chip["i18n"] = f"sayit.chip.{ref[1]}"
        else:
            chip = _clause_chip(clauses[ref[1]])
        chips.append(chip)
    unparsed = [word for _, word in leftover]
    if unparsed and nodes:
        hint = "Some words aren't in the query grammar and were used as a plain text search."
    elif unparsed:
        hint = "No query grammar matched — this is a plain text search."
    else:
        hint = ""
    return {
        "rules": rules,
        "clauses": clauses,
        "chips": chips,
        "unparsed": unparsed,
        "hint": hint,
        "parsed": bool(nodes),
        "complete": not clauses,
    }


def parse_query(text, now=None):
    """Run the full pipeline; returns the parse-preview response dict."""
    tokens = tokenize(text)
    nodes, leftover = parse_tokens(tokens, now=now)
    result = compile_nodes(nodes, leftover)
    # Keep the user's complete phrase when the grammar did not claim any
    # tokens.  Stopwords are intentionally omitted from ``rules.query`` so
    # they do not become chips, but they are meaningful inside titles such as
    # "Life is Strange" and must remain part of the plain-text fallback.
    if not nodes and any(
        kind == "word" and word in STOPWORDS for kind, word in tokens
    ) and leftover:
        result["plain_query"] = " ".join(
            (text if kind == "phrase" else _clean(text))
            for kind, text in tokens
            if text
        )
    result["ok"] = True
    result["text"] = str(text or "")
    return result


# ── Matching ─────────────────────────────────────────────────────────────────
_COOP_WORDS = ("co-op", "coop", "co op", "co-operative", "cooperative")
_MULTI_WORDS = _COOP_WORDS + ("multiplayer", "multi-player", "multi player",
                              "versus", " vs", "vs ", "split screen",
                              "splitscreen", "party")
_HAYSTACK_FIELDS = ("name", "sort_title", "platform", "genre", "developer",
                    "publisher", "series", "notes")


def _sub(term, value):
    return str(term).casefold() in str(value or "").casefold()


def _haystack(game):
    return " ".join(str(game.get(field, "")) for field in _HAYSTACK_FIELDS).casefold()


def _int_or(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _rating(game):
    try:
        return float(game.get("rating") or 0)
    except (TypeError, ValueError):
        return 0.0


def _year(game):
    match = re.search(r"\d{4}", str(game.get("year") or ""))
    return int(match.group(0)) if match else None


def _players(game):
    return _int_or(game.get("max_players"), 1) or 1


def _ttb(game):
    try:
        hours = float(game.get("time_to_beat_hours") or 0)
    except (TypeError, ValueError):
        return None
    return hours if hours > 0 else None


def _dt(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _was_played(game):
    if _int_or(game.get("play_count"), 0) > 0:
        return True
    if str(game.get("last_played") or "").strip():
        return True
    return bool(str(game.get("progress") or "").strip())


def _is_installed(game):
    if game.get("store_installed") is False:
        return False
    return bool(game.get("path_exists") or game.get("store_installed"))


def _is_owned(game):
    flag = game.get("owned") or game.get("store_catalog") or game.get("steam_app_id") \
        or game.get("heroic_app_id") or game.get("lutris_id") or game.get("gameyfin_id")
    return bool(flag) and not _is_installed(game)


def _evidence(game, words):
    fields = [game.get("play_mode"), game.get("genre")]
    fields.extend(game.get("tags") or [])
    hay = " ".join(str(item) for item in fields).casefold()
    return any(word in hay for word in words)


def _days_between(now, then):
    try:
        return (now - then).days
    except TypeError:
        return None


def _flag_value(game, field):
    if field == "notes":
        return bool(str(game.get("notes") or "").strip())
    if field == "controller_support":
        return bool(str(game.get("controller_support") or "").strip())
    return bool(game.get(field))


def _clause_matches(game, clause, now):
    """Evaluate one extended clause; unknown kinds fail closed (never guessed)."""
    kind = clause.get("kind")
    if kind == "unplayed":
        return not _was_played(game)
    if kind == "played":
        return _was_played(game)
    if kind == "backlog":
        return str(game.get("progress") or "") in BACKLOG_PROGRESS
    if kind == "not_finished":
        return str(game.get("progress") or "") not in FINISHED_PROGRESS
    if kind == "progress_any":
        return str(game.get("progress") or "") in (clause.get("values") or [])
    if kind == "year_range":
        year = _year(game)
        if year is None:
            return False
        low, high = clause.get("min"), clause.get("max")
        return (low is None or year >= low) and (high is None or year <= high)
    if kind == "rating_min":
        return _rating(game) >= float(clause.get("value") or 0)
    if kind == "unrated":
        return _rating(game) <= 0
    if kind == "players_min":
        return _players(game) >= _int_or(clause.get("value"), 1)
    if kind == "players_max":
        return _players(game) <= _int_or(clause.get("value"), 1)
    if kind == "multiplayer":
        return _players(game) >= 2 or _evidence(game, _MULTI_WORDS)
    if kind == "coop":
        return _evidence(game, _COOP_WORDS)
    if kind == "idle_days":
        last = _dt(game.get("last_played"))
        if last is None:
            return True   # never played is also "not played in N days"
        days = _days_between(now, last)
        return days is not None and days >= _int_or(clause.get("days"), 0)
    if kind == "played_within":
        last = _dt(game.get("last_played"))
        if last is None:
            return False
        days = _days_between(now, last)
        return days is not None and days <= _int_or(clause.get("days"), 0)
    if kind == "idle_before":
        last = _dt(game.get("last_played"))
        return last is None or last < datetime(_int_or(clause.get("year"), 0), 1, 1)
    if kind == "added_days":
        added = _dt(game.get("added_at"))
        if added is None:
            return False
        days = _days_between(now, added)
        return days is not None and days <= _int_or(clause.get("days"), 0)
    if kind == "added_year":
        added = _dt(game.get("added_at"))
        return added is not None and added.year == _int_or(clause.get("year"), -1)
    if kind == "ttb_max":
        hours = _ttb(game)
        return hours is not None and hours <= float(clause.get("hours") or 0)
    if kind == "ttb_min":
        hours = _ttb(game)
        return hours is not None and hours >= float(clause.get("hours") or 0)
    if kind == "platform_any":
        return _sub(clause.get("term"), game.get("platform"))
    if kind == "genre_any":
        return any(_sub(term, game.get("genre")) for term in clause.get("terms") or [])
    if kind == "credit":
        term = str(clause.get("term") or "").casefold()
        return str(game.get("developer") or "").casefold().startswith(term) or \
            str(game.get("publisher") or "").casefold().startswith(term)
    if kind == "series":
        return _sub(clause.get("term"), game.get("series"))
    if kind == "region":
        return _sub(clause.get("term"), game.get("region"))
    if kind == "source":
        return _sub(clause.get("term"), game.get("source"))
    if kind == "tag":
        return any(_sub(clause.get("term"), tag) for tag in game.get("tags") or [])
    if kind == "esrb_any":
        return str(game.get("esrb") or "") in (clause.get("values") or [])
    if kind == "flag":
        return _flag_value(game, str(clause.get("field") or "")) == bool(clause.get("value", True))
    if kind == "owned":
        return _is_owned(game)
    if kind == "not_query":
        return not _sub(clause.get("term"), _haystack(game))
    return False


def game_matches_query(game, parsed, now=None):
    """True when *game* satisfies the preset rules AND every parsed clause."""
    if not isinstance(game, dict):
        return True
    if not isinstance(parsed, dict):
        return False
    rules = parsed.get("rules")
    plain_query = str(parsed.get("plain_query") or "").strip()
    if plain_query and isinstance(rules, dict) and "query" in rules:
        # ``rules.query`` is the stopword-stripped compatibility projection;
        # the complete phrase is the authoritative matcher for this case.
        rules = {key: value for key, value in rules.items() if key != "query"}
    if isinstance(rules, dict) and rules and not game_matches_rules(game, rules):
        return False
    if plain_query and not _sub(plain_query, _haystack(game)):
        return False
    clauses = parsed.get("clauses")
    if not isinstance(clauses, list):
        clauses = []
    now = now or datetime.now()
    return all(_clause_matches(game, clause, now)
               for clause in clauses if isinstance(clause, dict))


def filter_games_by_query(games, parsed, now=None):
    """Apply ``game_matches_query`` over a game iterable; list in, list out."""
    if not isinstance(parsed, dict):
        return list(games or [])
    now = now or datetime.now()
    return [game for game in games or [] if game_matches_query(game, parsed, now=now)]
