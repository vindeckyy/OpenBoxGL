//! Natural-language "just say it" library query grammar (`pkg/parity/parity_query.py`).
//!
//! Deterministic pipeline: `tokenize` -> `parse_tokens` -> `compile_nodes` -> `game_matches_query`.
//! Matches `tests/golden/query_grammar.json` rule-for-rule.

use std::collections::{HashMap, HashSet};
use std::sync::LazyLock;

use jiff::Timestamp;
use jiff::civil::{Date, DateTime};
use regex::Regex;
use serde_json::{Map, Value, json};

use crate::filter_presets::{game_matches_rules, rules_to_chips};

pub const SHORT_GAME_HOURS: f64 = 5.0;
pub const LONG_GAME_HOURS: f64 = 20.0;
pub const HIGH_RATING_MIN: f64 = 4.0;
pub const TOP_RATING_MIN: f64 = 4.5;
pub const RETRO_YEAR_MAX: i64 = 2000;
pub const RECENT_DAYS: i64 = 31;
pub const FINISHED_PROGRESS: &[&str] = &["Beaten", "Completed", "Mastered"];
pub const BACKLOG_PROGRESS: &[&str] = &["", "Playing", "Paused"];
pub const PLAYING_PROGRESS: &[&str] = &["Playing", "Paused"];
pub const KIDS_ESRB: &[&str] = &["E", "E10+", "EC"];
pub const MATURE_ESRB: &[&str] = &["M", "AO"];

const EDGE_PUNCT: &str = "!\"$&()*,./;<=>?@[\\]^_`{|}~";

static TOKEN_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r#""([^"]*)"|([^\s"]+)"#).expect("valid token regex"));

static RE_NEG: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"^(?:haven'?t|havent|hasn'?t|hasnt|ain'?t|aint|never|not)$").unwrap()
});
static RE_UNIT: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^(?:years?|yrs?|months?|mos?|weeks?|wks?|days?)$").unwrap());
static RE_NUM: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^\d+$").unwrap());
static RE_NUMWORD: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^(?:\d+|one|two|three|four|five|six|seven|eight)$").unwrap());
static RE_HOURS: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^(?:h|hrs?|hours?)$").unwrap());
static RE_YEAR4: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^(?:19[7-9]\d|20\d{2})$").unwrap());
static RE_DECADE2: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^\d{2}'?s$").unwrap());
static RE_DECADE4: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^(?:19|20)\d{2}s$").unwrap());
static RE_RATING: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^\d(?:\.\d)?\+?$").unwrap());
static RE_STARS: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^stars?$").unwrap());
static RE_PLAYERS: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^(?:players?|people|peeps|ppl|friends?)$").unwrap());
static RE_ESRB_WORD: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^(?:e10\+?|e|t|m|ao|rp|ec)$").unwrap());
static RE_TYPED: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^([a-z_]+):(.+)$").unwrap());
static RE_NEGATED: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^-\w+$").unwrap());
static RE_HASHTAG: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^#\w+$").unwrap());
static RE_AN: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^an?$").unwrap());
static RE_DIGITS_PLUS: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^\d+\+$").unwrap());
static RE_HIGHER: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^(?:higher|better|more|up)$").unwrap());
static RE_PLAYERS_DASH: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^\d+-players?$").unwrap());
static RE_YEAR_EXTRACT: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"\d{4}").unwrap());
static RE_LEADING_DIGITS: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^\d+").unwrap());

const STOPWORDS_LIST: &[&str] = &[
    "a",
    "an",
    "the",
    "i",
    "me",
    "my",
    "we",
    "us",
    "you",
    "your",
    "yours",
    "he",
    "she",
    "his",
    "her",
    "its",
    "it",
    "they",
    "them",
    "their",
    "want",
    "wanna",
    "would",
    "like",
    "feel",
    "let's",
    "lets",
    "let",
    "to",
    "play",
    "some",
    "something",
    "any",
    "game",
    "games",
    "genre",
    "series",
    "please",
    "show",
    "find",
    "list",
    "give",
    "gimme",
    "that",
    "this",
    "these",
    "those",
    "and",
    "or",
    "with",
    "has",
    "have",
    "having",
    "for",
    "on",
    "in",
    "by",
    "from",
    "at",
    "of",
    "is",
    "are",
    "was",
    "were",
    "be",
    "been",
    "get",
    "got",
    "do",
    "does",
    "did",
    "can",
    "could",
    "should",
    "shall",
    "will",
    "just",
    "really",
    "very",
    "much",
    "kind",
    "kinda",
    "sort",
    "sorta",
    "maybe",
    "now",
    "today",
    "tonight",
    "all",
    "every",
    "each",
    "both",
    "either",
    "neither",
    "more",
    "most",
    "less",
    "least",
    "than",
    "then",
    "when",
    "while",
    "into",
    "over",
    "under",
    "again",
    "still",
    "yet",
    "already",
    "soon",
    "later",
    "ago",
    "before",
    "after",
    "since",
    "until",
    "till",
    "up",
    "down",
    "out",
    "about",
    "around",
    "through",
    "via",
    "per",
    "also",
    "too",
    "as",
    "well",
    "even",
    "only",
    "quite",
    "rather",
    "pretty",
    "so",
    "such",
    "what",
    "which",
    "who",
    "where",
    "why",
    "how",
    "if",
    "because",
    "unless",
    "except",
    "but",
    "nor",
    "not",
    "no",
    "without",
    "never",
    "haven't",
    "havent",
    "hasn't",
    "hasnt",
    "i'm",
    "im",
    "i've",
    "ive",
    "stuff",
    "things",
    "thing",
];

static STOPWORDS: LazyLock<HashSet<&'static str>> =
    LazyLock::new(|| STOPWORDS_LIST.iter().copied().collect());

static TARGET_BLOCKLIST: LazyLock<HashSet<&'static str>> = LazyLock::new(|| {
    let mut set: HashSet<&'static str> = STOPWORDS_LIST.iter().copied().collect();
    for w in [
        "sale", "free", "cheap", "real", "sure", "fun", "good", "great", "best", "now", "later",
        "tonight", "kids", "me", "us",
    ] {
        set.insert(w);
    }
    set
});

const SOURCE_WORDS: &[&str] = &[
    "steam",
    "epic",
    "gog",
    "lutris",
    "heroic",
    "itch",
    "gameyfin",
    "battlenet",
    "origin",
    "uplay",
    "humble",
    "amazon",
];

fn esrb_canon(word: &str) -> Option<&'static str> {
    match word {
        "e" | "everyone" => Some("E"),
        "e10" | "e10+" => Some("E10+"),
        "t" | "teen" => Some("T"),
        "m" | "mature" => Some("M"),
        "ao" | "adults" => Some("AO"),
        "rp" => Some("RP"),
        "ec" => Some("EC"),
        _ => None,
    }
}

const GENRE_TABLE: &[(&[&str], &[&str])] = &[
    (
        &["role", "playing"],
        &["rpg", "role-playing", "role playing"],
    ),
    (&["role-playing"], &["rpg", "role-playing", "role playing"]),
    (&["rpg"], &["rpg", "role-playing", "role playing"]),
    (&["jrpg"], &["jrpg", "j-rpg"]),
    (&["platformers"], &["platform"]),
    (&["platformer"], &["platform"]),
    (&["platforming"], &["platform"]),
    (&["platform"], &["platform"]),
    (&["shooters"], &["shoot"]),
    (&["shooter"], &["shoot"]),
    (
        &["fps"],
        &[
            "fps",
            "first-person shooter",
            "first person shooter",
            "shooter",
        ],
    ),
    (&["puzzles"], &["puzzl"]),
    (&["puzzle"], &["puzzl"]),
    (&["puzzler"], &["puzzl"]),
    (&["strategy"], &["strategy"]),
    (&["strategic"], &["strategy"]),
    (
        &["rts"],
        &["rts", "real-time strategy", "real time strategy"],
    ),
    (&["racing"], &["rac"]),
    (&["racer"], &["rac"]),
    (&["driving"], &["rac", "driv"]),
    (&["sports"], &["sport"]),
    (&["sport"], &["sport"]),
    (&["fighting"], &["fight"]),
    (&["fighter"], &["fight"]),
    (&["survival", "horror"], &["survival horror"]),
    (&["horror"], &["horror"]),
    (&["survival"], &["survival"]),
    (&["metroidvania"], &["metroidvania"]),
    (&["roguelike"], &["rogue"]),
    (&["roguelite"], &["rogue"]),
    (&["rogue-like"], &["rogue"]),
    (&["open", "world"], &["open world", "open-world"]),
    (&["open-world"], &["open world", "open-world"]),
    (&["visual", "novels"], &["visual novel"]),
    (&["visual", "novel"], &["visual novel"]),
    (
        &["point", "and", "click"],
        &["point and click", "point-and-click"],
    ),
    (
        &["point-and-click"],
        &["point and click", "point-and-click"],
    ),
    (
        &["beat", "em", "up"],
        &["beat em up", "beat 'em up", "beat-em-up", "brawler"],
    ),
    (
        &["beat-em-up"],
        &["beat em up", "beat 'em up", "beat-em-up", "brawler"],
    ),
    (&["brawler"], &["brawler", "beat"]),
    (&["shmup"], &["shmup", "shoot 'em up", "shoot em up"]),
    (
        &["shoot", "em", "up"],
        &["shoot 'em up", "shoot em up", "shmup"],
    ),
    (&["bullet", "hell"], &["bullet hell"]),
    (&["soulslike"], &["soulslike", "souls-like", "souls"]),
    (&["souls-like"], &["soulslike", "souls-like", "souls"]),
    (&["souls"], &["soulslike", "souls-like", "souls"]),
    (&["indie"], &["indie"]),
    (&["action"], &["action"]),
    (&["adventure"], &["adventure"]),
    (&["stealth"], &["stealth"]),
    (&["sandbox"], &["sandbox"]),
    (&["arcade"], &["arcade"]),
    (&["pinball"], &["pinball"]),
    (&["card"], &["card"]),
    (&["cards"], &["card"]),
    (&["board"], &["board"]),
    (&["educational"], &["educat"]),
    (&["simulation"], &["sim"]),
    (&["simulator"], &["sim"]),
    (&["sim"], &["sim"]),
    (&["rhythm"], &["rhythm"]),
    (&["moba"], &["moba"]),
    (&["mmorpg"], &["mmo", "mmorpg"]),
    (&["mmo"], &["mmo", "mmorpg"]),
    (&["tower", "defense"], &["tower defense"]),
    (&["tower", "defence"], &["tower defence", "tower defense"]),
    (&["turn", "based"], &["turn-based", "turn based"]),
    (&["turn-based"], &["turn-based", "turn based"]),
    (&["trivia"], &["trivia", "quiz"]),
    (&["quiz"], &["quiz", "trivia"]),
    (&["casual"], &["casual"]),
    (
        &["walking", "simulator"],
        &["walking sim", "walking simulator"],
    ),
    (&["walking", "sim"], &["walking sim", "walking simulator"]),
];

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TokenKind {
    Word,
    Phrase,
}

pub fn tokenize(text: &str) -> Vec<(TokenKind, String)> {
    let lowered = text.to_lowercase().replace('\u{2019}', "'");
    let norm = lowered.split_whitespace().collect::<Vec<_>>().join(" ");
    let mut tokens = Vec::new();
    for cap in TOKEN_RE.captures_iter(&norm) {
        if let Some(m) = cap.get(1) {
            tokens.push((TokenKind::Phrase, m.as_str().trim().to_owned()));
        } else if let Some(m) = cap.get(2) {
            tokens.push((TokenKind::Word, m.as_str().to_owned()));
        }
    }
    tokens
}

fn clean_word(word: &str) -> String {
    let trimmed = word.trim_matches(|c: char| EDGE_PUNCT.contains(c));
    trimmed.trim_end_matches('\'').to_owned()
}

fn numword(word: &str) -> Option<i64> {
    if !word.is_empty() && word.bytes().all(|b| b.is_ascii_digit()) {
        return word.parse::<i64>().ok();
    }
    match word {
        "one" => Some(1),
        "two" => Some(2),
        "three" => Some(3),
        "four" => Some(4),
        "five" => Some(5),
        "six" => Some(6),
        "seven" => Some(7),
        "eight" => Some(8),
        _ => None,
    }
}

fn unit_days(word: &str) -> i64 {
    if word.starts_with('y') {
        365
    } else if word.starts_with("mo") {
        30
    } else if word.starts_with('w') {
        7
    } else {
        1
    }
}

fn now_local(now: Option<DateTime>) -> DateTime {
    now.unwrap_or_else(|| jiff::tz::TimeZone::system().to_datetime(Timestamp::now()))
}

#[derive(Debug, Clone)]
pub enum AstNode {
    Rule {
        key: String,
        value: Value,
        pos: usize,
    },
    Genre {
        terms: Vec<String>,
        pos: usize,
    },
    Progress {
        values: Vec<String>,
        pos: usize,
    },
    Esrb {
        values: Vec<String>,
        pos: usize,
    },
    Clause {
        clause: Value,
        pos: usize,
    },
}

impl AstNode {
    fn set_pos(&mut self, p: usize) {
        match self {
            AstNode::Rule { pos, .. }
            | AstNode::Genre { pos, .. }
            | AstNode::Progress { pos, .. }
            | AstNode::Esrb { pos, .. }
            | AstNode::Clause { pos, .. } => *pos = p,
        }
    }
}

fn clause_node(v: Value) -> Option<Vec<AstNode>> {
    Some(vec![AstNode::Clause { clause: v, pos: 0 }])
}

fn rule_node(k: &str, v: Value) -> Option<Vec<AstNode>> {
    Some(vec![AstNode::Rule {
        key: k.to_owned(),
        value: v,
        pos: 0,
    }])
}

fn progress_node(vals: &[&str]) -> Option<Vec<AstNode>> {
    Some(vec![AstNode::Progress {
        values: vals.iter().map(|s| (*s).to_owned()).collect(),
        pos: 0,
    }])
}

fn esrb_node(vals: &[&str]) -> Option<Vec<AstNode>> {
    Some(vec![AstNode::Esrb {
        values: vals.iter().map(|s| (*s).to_owned()).collect(),
        pos: 0,
    }])
}

fn flag_node(field: &str, value: bool) -> Option<Vec<AstNode>> {
    clause_node(json!({"kind": "flag", "field": field, "value": value}))
}

#[derive(Clone, Copy)]
enum Spec {
    Exact(&'static str),
    Re(&'static LazyLock<Regex>),
    Word,
}

impl Spec {
    fn matches(self, kind: TokenKind, word: &str) -> bool {
        match self {
            Spec::Word => true,
            Spec::Exact(s) => kind == TokenKind::Word && word == s,
            Spec::Re(re) => kind == TokenKind::Word && re.is_match(word),
        }
    }
}

type HandlerFn = fn(&[String], DateTime) -> Option<Vec<AstNode>>;

struct GrammarEntry {
    pattern: Vec<Spec>,
    handler: GrammarHandler,
}

enum GrammarHandler {
    Fn(HandlerFn),
    Genre(&'static [&'static str]),
}

impl GrammarHandler {
    fn invoke(&self, words: &[String], now: DateTime) -> Option<Vec<AstNode>> {
        match self {
            GrammarHandler::Fn(f) => f(words, now),
            GrammarHandler::Genre(terms) => Some(vec![AstNode::Genre {
                terms: terms.iter().map(|s| (*s).to_owned()).collect(),
                pos: 0,
            }]),
        }
    }
}

fn h_idle_num(w: &[String], _: DateTime) -> Option<Vec<AstNode>> {
    let n = w[3].parse::<i64>().ok()?;
    clause_node(json!({"kind": "idle_days", "days": n * unit_days(&w[4])}))
}
fn h_idle_unit(w: &[String], _: DateTime) -> Option<Vec<AstNode>> {
    clause_node(json!({"kind": "idle_days", "days": unit_days(&w[4])}))
}
fn h_idle_over_num(w: &[String], _: DateTime) -> Option<Vec<AstNode>> {
    let n = w[4].parse::<i64>().ok()?;
    clause_node(json!({"kind": "idle_days", "days": n * unit_days(&w[5])}))
}
fn h_idle_over_unit(w: &[String], _: DateTime) -> Option<Vec<AstNode>> {
    clause_node(json!({"kind": "idle_days", "days": unit_days(&w[5])}))
}
fn h_idle_since(w: &[String], _: DateTime) -> Option<Vec<AstNode>> {
    let y = w[3].parse::<i64>().ok()?;
    clause_node(json!({"kind": "idle_before", "year": y}))
}
fn h_unplayed(_: &[String], _: DateTime) -> Option<Vec<AstNode>> {
    clause_node(json!({"kind": "unplayed"}))
}
fn h_played(_: &[String], _: DateTime) -> Option<Vec<AstNode>> {
    clause_node(json!({"kind": "played"}))
}
fn h_played_recent(_: &[String], _: DateTime) -> Option<Vec<AstNode>> {
    clause_node(json!({"kind": "played_within", "days": RECENT_DAYS}))
}
fn h_added_recent(_: &[String], _: DateTime) -> Option<Vec<AstNode>> {
    clause_node(json!({"kind": "added_days", "days": RECENT_DAYS}))
}
fn h_added_year_now(_: &[String], now: DateTime) -> Option<Vec<AstNode>> {
    clause_node(json!({"kind": "added_year", "year": i64::from(now.year())}))
}
fn h_this_year(_: &[String], now: DateTime) -> Option<Vec<AstNode>> {
    let y = i64::from(now.year());
    clause_node(json!({"kind": "year_range", "min": y, "max": y}))
}
fn h_not_finished(_: &[String], _: DateTime) -> Option<Vec<AstNode>> {
    clause_node(json!({"kind": "not_finished"}))
}
fn h_backlog(_: &[String], _: DateTime) -> Option<Vec<AstNode>> {
    clause_node(json!({"kind": "backlog"}))
}
fn h_playing(_: &[String], _: DateTime) -> Option<Vec<AstNode>> {
    clause_node(json!({"kind": "progress_any", "values": PLAYING_PROGRESS}))
}
fn h_beaten(_: &[String], _: DateTime) -> Option<Vec<AstNode>> {
    progress_node(&["Beaten"])
}
fn h_completed(_: &[String], _: DateTime) -> Option<Vec<AstNode>> {
    progress_node(&["Completed"])
}
fn h_mastered(_: &[String], _: DateTime) -> Option<Vec<AstNode>> {
    progress_node(&["Mastered"])
}
fn h_finished(_: &[String], _: DateTime) -> Option<Vec<AstNode>> {
    clause_node(json!({"kind": "progress_any", "values": FINISHED_PROGRESS}))
}
fn h_hundred(_: &[String], _: DateTime) -> Option<Vec<AstNode>> {
    clause_node(json!({"kind": "progress_any", "values": ["Completed", "Mastered"]}))
}
fn h_abandoned(_: &[String], _: DateTime) -> Option<Vec<AstNode>> {
    progress_node(&["Abandoned"])
}
fn h_paused(_: &[String], _: DateTime) -> Option<Vec<AstNode>> {
    progress_node(&["Paused"])
}
fn h_ttb_max_const(_: &[String], _: DateTime) -> Option<Vec<AstNode>> {
    clause_node(json!({"kind": "ttb_max", "hours": SHORT_GAME_HOURS}))
}
fn h_ttb_min_const(_: &[String], _: DateTime) -> Option<Vec<AstNode>> {
    clause_node(json!({"kind": "ttb_min", "hours": LONG_GAME_HOURS}))
}
fn h_ttb_under(w: &[String], _: DateTime) -> Option<Vec<AstNode>> {
    clause_node(json!({"kind": "ttb_max", "hours": numword(&w[1]).unwrap_or(0) as f64}))
}
fn h_ttb_less_than(w: &[String], _: DateTime) -> Option<Vec<AstNode>> {
    clause_node(json!({"kind": "ttb_max", "hours": numword(&w[2]).unwrap_or(0) as f64}))
}
fn h_ttb_or_less(w: &[String], _: DateTime) -> Option<Vec<AstNode>> {
    clause_node(json!({"kind": "ttb_max", "hours": numword(&w[0]).unwrap_or(0) as f64}))
}
fn h_ttb_over(w: &[String], _: DateTime) -> Option<Vec<AstNode>> {
    clause_node(json!({"kind": "ttb_min", "hours": numword(&w[1]).unwrap_or(0) as f64}))
}
fn h_ttb_more_than(w: &[String], _: DateTime) -> Option<Vec<AstNode>> {
    clause_node(json!({"kind": "ttb_min", "hours": numword(&w[2]).unwrap_or(0) as f64}))
}
fn h_ttb_at_least(w: &[String], _: DateTime) -> Option<Vec<AstNode>> {
    clause_node(json!({"kind": "ttb_min", "hours": numword(&w[2]).unwrap_or(0) as f64}))
}
fn h_ttb_no_more(w: &[String], _: DateTime) -> Option<Vec<AstNode>> {
    clause_node(json!({"kind": "ttb_max", "hours": numword(&w[3]).unwrap_or(0) as f64}))
}
fn h_ttb_plus_hours(w: &[String], _: DateTime) -> Option<Vec<AstNode>> {
    let val = w[0].trim_end_matches('+').parse::<f64>().ok()?;
    clause_node(json!({"kind": "ttb_min", "hours": val}))
}
fn h_rating_rated(w: &[String], _: DateTime) -> Option<Vec<AstNode>> {
    let val = w[1].trim_end_matches('+').parse::<f64>().ok()?;
    clause_node(json!({"kind": "rating_min", "value": val}))
}
fn h_rating_rated_last(w: &[String], _: DateTime) -> Option<Vec<AstNode>> {
    // Note: in Python `("rated", "at", "least", _NUM)` calls `_rating_rated(words)` which indexes `words[1]` ("at" -> raises or wait? Actually wait - let's check!)
    // Wait: in Python `_rating_rated` does `float(words[1].rstrip("+"))` — for `("rated", "at", "least", _NUM)` words[1] is "at", wait! Let's handle `words.last()` safely for at_least!
    let val = w.last()?.trim_end_matches('+').parse::<f64>().ok()?;
    clause_node(json!({"kind": "rating_min", "value": val}))
}
fn h_rating_stars(w: &[String], _: DateTime) -> Option<Vec<AstNode>> {
    let v = numword(w[0].trim_end_matches('+'))?;
    clause_node(json!({"kind": "rating_min", "value": v as f64}))
}
fn h_highly_rated(_: &[String], _: DateTime) -> Option<Vec<AstNode>> {
    clause_node(json!({"kind": "rating_min", "value": HIGH_RATING_MIN}))
}
fn h_top_rated(_: &[String], _: DateTime) -> Option<Vec<AstNode>> {
    clause_node(json!({"kind": "rating_min", "value": TOP_RATING_MIN}))
}
fn h_unrated(_: &[String], _: DateTime) -> Option<Vec<AstNode>> {
    clause_node(json!({"kind": "unrated"}))
}
fn h_user_rating_unrated(_: &[String], _: DateTime) -> Option<Vec<AstNode>> {
    clause_node(json!({"kind": "user_rating_unrated"}))
}
fn h_user_rating_at_least(w: &[String], _: DateTime) -> Option<Vec<AstNode>> {
    let v = numword(w.last()?.trim_end_matches('+'))?;
    clause_node(json!({"kind": "user_rating_min", "value": v}))
}
fn h_user_rating_stars(w: &[String], _: DateTime) -> Option<Vec<AstNode>> {
    let v = numword(w[1].trim_end_matches('+'))?;
    clause_node(json!({"kind": "user_rating_min", "value": v}))
}
fn h_esrb_rated(w: &[String], _: DateTime) -> Option<Vec<AstNode>> {
    let c = esrb_canon(&w[1])?;
    esrb_node(&[c])
}
fn h_esrb_m(_: &[String], _: DateTime) -> Option<Vec<AstNode>> {
    esrb_node(&["M"])
}
fn h_esrb_t(_: &[String], _: DateTime) -> Option<Vec<AstNode>> {
    esrb_node(&["T"])
}
fn h_esrb_e(_: &[String], _: DateTime) -> Option<Vec<AstNode>> {
    esrb_node(&["E"])
}
fn h_esrb_ao(_: &[String], _: DateTime) -> Option<Vec<AstNode>> {
    esrb_node(&["AO"])
}
fn h_mature(_: &[String], _: DateTime) -> Option<Vec<AstNode>> {
    clause_node(json!({"kind": "esrb_any", "values": MATURE_ESRB}))
}
fn h_kids(_: &[String], _: DateTime) -> Option<Vec<AstNode>> {
    clause_node(json!({"kind": "esrb_any", "values": KIDS_ESRB}))
}
fn h_decade(w: &[String], _: DateTime) -> Option<Vec<AstNode>> {
    let digits: String = w[0].chars().filter(|c| c.is_ascii_digit()).collect();
    let start = if digits.len() == 4 {
        digits.parse::<i64>().ok()? / 10 * 10
    } else {
        let pair = digits.get(..2)?.parse::<i64>().ok()?;
        (if pair >= 40 { 1900 } else { 2000 }) + pair
    };
    clause_node(json!({"kind": "year_range", "min": start, "max": start + 9}))
}
fn h_year_exact(w: &[String], _: DateTime) -> Option<Vec<AstNode>> {
    let y = w.last()?.parse::<i64>().ok()?;
    clause_node(json!({"kind": "year_range", "min": y, "max": y}))
}
fn h_year_before(w: &[String], _: DateTime) -> Option<Vec<AstNode>> {
    let y = w.last()?.parse::<i64>().ok()?;
    clause_node(json!({"kind": "year_range", "min": Value::Null, "max": y - 1}))
}
fn h_year_after(w: &[String], _: DateTime) -> Option<Vec<AstNode>> {
    let y = w.last()?.parse::<i64>().ok()?;
    clause_node(json!({"kind": "year_range", "min": y + 1, "max": Value::Null}))
}
fn h_year_since(w: &[String], _: DateTime) -> Option<Vec<AstNode>> {
    let y = w.last()?.parse::<i64>().ok()?;
    clause_node(json!({"kind": "year_range", "min": y, "max": Value::Null}))
}
fn h_retro(_: &[String], _: DateTime) -> Option<Vec<AstNode>> {
    clause_node(json!({"kind": "year_range", "min": Value::Null, "max": RETRO_YEAR_MAX}))
}
fn h_players_max1(_: &[String], _: DateTime) -> Option<Vec<AstNode>> {
    clause_node(json!({"kind": "players_max", "value": 1}))
}
fn h_players_min(w: &[String], _: DateTime) -> Option<Vec<AstNode>> {
    for word in w.iter().rev() {
        let mut value = numword(word.trim_end_matches('+'));
        if value.is_none()
            && let Some(m) = RE_LEADING_DIGITS.find(word)
        {
            value = numword(m.as_str());
        }
        if let Some(v) = value {
            return clause_node(json!({"kind": "players_min", "value": v}));
        }
    }
    None
}
fn h_players_dash(w: &[String], _: DateTime) -> Option<Vec<AstNode>> {
    let m = RE_LEADING_DIGITS.find(&w[0])?;
    let v = m.as_str().parse::<i64>().ok()?;
    clause_node(json!({"kind": "players_min", "value": v}))
}
fn h_coop(_: &[String], _: DateTime) -> Option<Vec<AstNode>> {
    clause_node(json!({"kind": "coop"}))
}
fn h_multiplayer(_: &[String], _: DateTime) -> Option<Vec<AstNode>> {
    clause_node(json!({"kind": "multiplayer"}))
}
fn h_fav_true(_: &[String], _: DateTime) -> Option<Vec<AstNode>> {
    rule_node("favorite", Value::Bool(true))
}
fn h_fav_false(_: &[String], _: DateTime) -> Option<Vec<AstNode>> {
    rule_node("favorite", Value::Bool(false))
}
fn h_hidden_true(_: &[String], _: DateTime) -> Option<Vec<AstNode>> {
    rule_node("hidden", Value::Bool(true))
}
fn h_hidden_false(_: &[String], _: DateTime) -> Option<Vec<AstNode>> {
    rule_node("hidden", Value::Bool(false))
}
fn h_installed(_: &[String], _: DateTime) -> Option<Vec<AstNode>> {
    rule_node("installed", Value::String("installed".to_owned()))
}
fn h_uninstalled(_: &[String], _: DateTime) -> Option<Vec<AstNode>> {
    rule_node("installed", Value::String("uninstalled".to_owned()))
}
fn h_owned(_: &[String], _: DateTime) -> Option<Vec<AstNode>> {
    clause_node(json!({"kind": "owned"}))
}
fn h_saves_true(_: &[String], _: DateTime) -> Option<Vec<AstNode>> {
    rule_node("has_saves", Value::Bool(true))
}
fn h_saves_false(_: &[String], _: DateTime) -> Option<Vec<AstNode>> {
    rule_node("has_saves", Value::Bool(false))
}
fn h_ach_true(_: &[String], _: DateTime) -> Option<Vec<AstNode>> {
    rule_node("has_achievements", Value::Bool(true))
}
fn h_ach_false(_: &[String], _: DateTime) -> Option<Vec<AstNode>> {
    rule_node("has_achievements", Value::Bool(false))
}
fn h_missing_media_true(_: &[String], _: DateTime) -> Option<Vec<AstNode>> {
    rule_node("has_missing_media", Value::Bool(true))
}
fn h_missing_media_false(_: &[String], _: DateTime) -> Option<Vec<AstNode>> {
    rule_node("has_missing_media", Value::Bool(false))
}
fn h_highscores_true(_: &[String], _: DateTime) -> Option<Vec<AstNode>> {
    rule_node("has_highscores", Value::Bool(true))
}
fn h_flag_manual(_: &[String], _: DateTime) -> Option<Vec<AstNode>> {
    flag_node("manual_entry", true)
}
fn h_flag_broken_true(_: &[String], _: DateTime) -> Option<Vec<AstNode>> {
    flag_node("broken", true)
}
fn h_flag_broken_false(_: &[String], _: DateTime) -> Option<Vec<AstNode>> {
    flag_node("broken", false)
}
fn h_flag_portable(_: &[String], _: DateTime) -> Option<Vec<AstNode>> {
    flag_node("portable", true)
}
fn h_flag_controller(_: &[String], _: DateTime) -> Option<Vec<AstNode>> {
    flag_node("controller_support", true)
}
fn h_flag_notes_true(_: &[String], _: DateTime) -> Option<Vec<AstNode>> {
    flag_node("notes", true)
}
fn h_flag_notes_false(_: &[String], _: DateTime) -> Option<Vec<AstNode>> {
    flag_node("notes", false)
}

fn source_from(w: &[String]) -> Option<Vec<AstNode>> {
    for word in w {
        if SOURCE_WORDS.contains(&word.as_str()) {
            return clause_node(json!({"kind": "source", "term": word}));
        }
    }
    None
}
fn platform_any_from(w: &[String]) -> Option<Vec<AstNode>> {
    let term = clean_word(w.last()?);
    if term.is_empty() || TARGET_BLOCKLIST.contains(term.as_str()) {
        return None;
    }
    clause_node(json!({"kind": "platform_any", "term": term}))
}
fn credit_from(w: &[String]) -> Option<Vec<AstNode>> {
    let term = clean_word(w.last()?);
    if term.is_empty() || TARGET_BLOCKLIST.contains(term.as_str()) {
        return None;
    }
    clause_node(json!({"kind": "credit", "term": term}))
}

fn h_on_word(w: &[String], _: DateTime) -> Option<Vec<AstNode>> {
    source_from(w).or_else(|| platform_any_from(w))
}
fn h_platform_any(w: &[String], _: DateTime) -> Option<Vec<AstNode>> {
    platform_any_from(w)
}
fn h_from_word(w: &[String], _: DateTime) -> Option<Vec<AstNode>> {
    source_from(w).or_else(|| credit_from(w))
}
fn h_credit(w: &[String], _: DateTime) -> Option<Vec<AstNode>> {
    credit_from(w)
}
fn h_source_game(w: &[String], _: DateTime) -> Option<Vec<AstNode>> {
    source_from(w)
}
fn h_tag(w: &[String], _: DateTime) -> Option<Vec<AstNode>> {
    let term = clean_word(w.last()?).trim_start_matches('#').to_owned();
    if term.is_empty() || TARGET_BLOCKLIST.contains(term.as_str()) {
        return None;
    }
    clause_node(json!({"kind": "tag", "term": term}))
}
fn h_hashtag(w: &[String], _: DateTime) -> Option<Vec<AstNode>> {
    let term = w[0].trim_start_matches('#');
    clause_node(json!({"kind": "tag", "term": term}))
}
fn h_not_query(w: &[String], _: DateTime) -> Option<Vec<AstNode>> {
    let term = clean_word(w.last()?).trim_start_matches('-').to_owned();
    if term.is_empty() || TARGET_BLOCKLIST.contains(term.as_str()) {
        return None;
    }
    clause_node(json!({"kind": "not_query", "term": term}))
}

fn h_typed(w: &[String], _: DateTime) -> Option<Vec<AstNode>> {
    let cap = RE_TYPED.captures(&w[0])?;
    let key = cap.get(1)?.as_str();
    let value = clean_word(cap.get(2)?.as_str());
    if value.is_empty() {
        return None;
    }
    match key {
        "genre" => clause_node(json!({"kind": "genre_any", "terms": [value]})),
        "platform" | "plat" => clause_node(json!({"kind": "platform_any", "term": value})),
        "dev" | "developer" => rule_node("developer", Value::String(value)),
        "pub" | "publisher" => rule_node("publisher", Value::String(value)),
        "series" => clause_node(json!({"kind": "series", "term": value})),
        "region" => clause_node(json!({"kind": "region", "term": value})),
        "progress" => {
            let v_low = value.to_lowercase();
            let v_eff = if v_low == "dropped" {
                "Abandoned".to_owned()
            } else {
                value
            };
            let canon = [
                "Unplayed",
                "Playing",
                "Paused",
                "Beaten",
                "Completed",
                "Mastered",
                "Abandoned",
            ]
            .into_iter()
            .find(|it| it.to_lowercase() == v_eff.to_lowercase());
            match canon {
                Some(c) => rule_node("progress", Value::String(c.to_owned())),
                None => clause_node(json!({"kind": "progress_any", "values": [v_eff]})),
            }
        }
        "myrating" | "my_rating" | "my-rating" => {
            let v = value.trim_end_matches('+').parse::<f64>().ok()?;
            clause_node(json!({"kind": "user_rating_min", "value": v as i64}))
        }
        "esrb" => {
            let c = esrb_canon(&value.to_lowercase())
                .map(str::to_owned)
                .unwrap_or_else(|| value.to_uppercase());
            rule_node("esrb", Value::String(c))
        }
        "rating" => {
            let v = value.trim_end_matches('+').parse::<f64>().ok()?;
            clause_node(json!({"kind": "rating_min", "value": v}))
        }
        "year" => {
            let digits: String = value.chars().filter(|c| c.is_ascii_digit()).collect();
            if digits.len() != 4 {
                return None;
            }
            let y = digits.parse::<i64>().ok()?;
            clause_node(json!({"kind": "year_range", "min": y, "max": y}))
        }
        "player" | "players" => {
            let num = numword(&value)?;
            if num == 0 {
                return None;
            }
            clause_node(json!({"kind": "players_min", "value": num}))
        }
        "tag" | "tags" => clause_node(json!({"kind": "tag", "term": value})),
        "source" | "store" | "storefront" => clause_node(json!({"kind": "source", "term": value})),
        "favorite" | "fav" => rule_node(
            "favorite",
            Value::Bool(matches!(value.as_str(), "yes" | "true" | "1")),
        ),
        "installed" => rule_node(
            "installed",
            Value::String(
                if matches!(value.as_str(), "no" | "false" | "0") {
                    "uninstalled"
                } else {
                    "installed"
                }
                .to_owned(),
            ),
        ),
        "hide" | "hidden" => rule_node(
            "hidden",
            Value::Bool(matches!(value.as_str(), "yes" | "true" | "1")),
        ),
        "unplayed" => {
            if matches!(value.as_str(), "yes" | "true" | "1") {
                clause_node(json!({"kind": "unplayed"}))
            } else {
                clause_node(json!({"kind": "played"}))
            }
        }
        "saves" => rule_node(
            "has_saves",
            Value::Bool(matches!(value.as_str(), "yes" | "true" | "1")),
        ),
        "achievements" | "achievement" => rule_node(
            "has_achievements",
            Value::Bool(matches!(value.as_str(), "yes" | "true" | "1")),
        ),
        "notes" => flag_node("notes", matches!(value.as_str(), "yes" | "true" | "1")),
        _ => None,
    }
}

static GRAMMAR: LazyLock<Vec<GrammarEntry>> = LazyLock::new(|| {
    use Spec::{Exact as E, Re as R, Word as W};
    let mut table: Vec<GrammarEntry> = Vec::new();
    let mut push = |pattern: &[Spec], handler: HandlerFn| {
        table.push(GrammarEntry {
            pattern: pattern.to_vec(),
            handler: GrammarHandler::Fn(handler),
        });
    };

    // recency / played state
    push(
        &[
            R(&RE_NEG),
            E("played"),
            E("in"),
            E("over"),
            R(&RE_NUM),
            R(&RE_UNIT),
        ],
        h_idle_over_num,
    );
    push(
        &[
            R(&RE_NEG),
            E("played"),
            E("in"),
            E("over"),
            E("a"),
            R(&RE_UNIT),
        ],
        h_idle_over_unit,
    );
    push(
        &[R(&RE_NEG), E("played"), E("in"), R(&RE_NUM), R(&RE_UNIT)],
        h_idle_num,
    );
    push(
        &[R(&RE_NEG), E("played"), E("in"), R(&RE_AN), R(&RE_UNIT)],
        h_idle_unit,
    );
    push(
        &[R(&RE_NEG), E("played"), E("since"), R(&RE_YEAR4)],
        h_idle_since,
    );
    push(&[R(&RE_NEG), E("played")], h_unplayed);
    push(&[R(&RE_NEG), E("started")], h_unplayed);
    push(&[E("unplayed")], h_unplayed);
    push(&[E("untouched")], h_unplayed);
    push(&[E("unstarted")], h_unplayed);
    push(&[E("unopened")], h_unplayed);
    push(&[E("played"), E("recently")], h_played_recent);
    push(&[E("recently"), E("played")], h_played_recent);
    push(&[E("played"), E("lately")], h_played_recent);
    push(&[E("recently"), E("added")], h_added_recent);
    push(&[E("newly"), E("added")], h_added_recent);
    push(&[E("just"), E("added")], h_added_recent);
    push(&[E("added"), E("recently")], h_added_recent);
    push(&[E("new"), E("to"), E("the"), E("library")], h_added_recent);
    push(&[E("new"), E("to"), E("library")], h_added_recent);
    push(&[E("added"), E("this"), E("year")], h_added_year_now);
    push(&[E("this"), E("year")], h_this_year);
    push(&[E("played")], h_played);
    push(&[E("unbeaten")], h_not_finished);
    push(&[E("unfinished")], h_not_finished);
    push(&[R(&RE_NEG), E("finished")], h_not_finished);
    push(&[R(&RE_NEG), E("beaten")], h_not_finished);
    push(&[R(&RE_NEG), E("completed")], h_not_finished);
    push(&[E("backlog")], h_backlog);
    push(&[E("pile"), E("of"), E("shame")], h_backlog);
    push(&[E("playing")], h_playing);
    push(&[E("in"), E("progress")], h_playing);
    push(&[E("currently"), E("playing")], h_playing);
    push(&[E("still"), E("playing")], h_playing);
    push(&[E("beaten")], h_beaten);
    push(&[E("beat")], h_beaten);
    push(&[E("completed")], h_completed);
    push(&[E("complete")], h_completed);
    push(&[E("mastered")], h_mastered);
    push(&[E("finished")], h_finished);
    push(&[E("100%")], h_hundred);
    push(&[E("hundred"), E("percent")], h_hundred);
    push(&[E("abandoned")], h_abandoned);
    push(&[E("dropped")], h_abandoned);
    push(&[E("gave"), E("up")], h_abandoned);
    push(&[E("gave"), E("up"), E("on")], h_abandoned);
    push(&[E("paused")], h_paused);
    push(&[E("on"), E("hold")], h_paused);
    push(&[E("parked")], h_paused);

    // length
    push(&[E("short")], h_ttb_max_const);
    push(&[E("quick")], h_ttb_max_const);
    push(&[E("bite-sized")], h_ttb_max_const);
    push(&[E("bite"), E("sized")], h_ttb_max_const);
    push(&[E("tiny")], h_ttb_max_const);
    push(
        &[E("less"), E("than"), R(&RE_NUMWORD), R(&RE_HOURS)],
        h_ttb_less_than,
    );
    push(
        &[E("no"), E("more"), E("than"), R(&RE_NUMWORD), R(&RE_HOURS)],
        h_ttb_no_more,
    );
    push(&[E("under"), R(&RE_NUMWORD), R(&RE_HOURS)], h_ttb_under);
    push(
        &[R(&RE_NUM), R(&RE_HOURS), E("or"), E("less")],
        h_ttb_or_less,
    );
    push(
        &[R(&RE_NUM), R(&RE_HOURS), E("or"), E("shorter")],
        h_ttb_or_less,
    );
    push(&[E("long")], h_ttb_min_const);
    push(&[E("epic")], h_ttb_min_const);
    push(&[E("lengthy")], h_ttb_min_const);
    push(
        &[E("more"), E("than"), R(&RE_NUMWORD), R(&RE_HOURS)],
        h_ttb_more_than,
    );
    push(
        &[E("at"), E("least"), R(&RE_NUMWORD), R(&RE_HOURS)],
        h_ttb_at_least,
    );
    push(&[E("over"), R(&RE_NUMWORD), R(&RE_HOURS)], h_ttb_over);
    push(&[R(&RE_DIGITS_PLUS), R(&RE_HOURS)], h_ttb_plus_hours);

    // rating
    push(
        &[E("rated"), R(&RE_NUM), E("or"), R(&RE_HIGHER)],
        h_rating_rated,
    );
    push(
        &[E("rated"), E("at"), E("least"), R(&RE_NUM)],
        h_rating_rated_last,
    );
    push(&[E("rated"), R(&RE_RATING)], h_rating_rated);
    push(&[R(&RE_NUMWORD), R(&RE_STARS)], h_rating_stars);
    push(&[R(&RE_RATING), R(&RE_STARS)], h_rating_stars);
    push(&[E("highly"), E("rated")], h_highly_rated);
    push(&[E("top"), E("rated")], h_top_rated);
    push(&[E("best"), E("rated")], h_top_rated);
    push(&[E("unrated")], h_unrated);
    push(&[E("unrated"), E("by"), E("me")], h_user_rating_unrated);
    push(
        &[E("not"), E("rated"), E("by"), E("me")],
        h_user_rating_unrated,
    );
    push(
        &[E("my"), E("rating"), E("at"), E("least"), R(&RE_NUMWORD)],
        h_user_rating_at_least,
    );
    push(&[E("my"), R(&RE_RATING), R(&RE_STARS)], h_user_rating_stars);
    push(&[E("no"), E("rating")], h_unrated);
    push(&[E("not"), E("rated")], h_unrated);

    // esrb
    push(&[E("rated"), R(&RE_ESRB_WORD)], h_esrb_rated);
    push(&[E("esrb"), R(&RE_ESRB_WORD)], h_esrb_rated);
    push(&[E("rated"), E("mature")], h_esrb_m);
    push(&[E("rated"), E("teen")], h_esrb_t);
    push(&[E("rated"), E("everyone")], h_esrb_e);
    push(&[E("rated"), E("adults")], h_esrb_ao);
    push(&[E("esrb"), E("mature")], h_esrb_m);
    push(&[E("adults"), E("only")], h_esrb_ao);
    push(&[E("mature")], h_mature);
    push(&[E("adult")], h_mature);
    push(&[E("for"), E("the"), E("kids")], h_kids);
    push(&[E("for"), E("kids")], h_kids);
    push(&[E("kids")], h_kids);
    push(&[E("kid-friendly")], h_kids);
    push(&[E("family"), E("friendly")], h_kids);
    push(&[E("family")], h_kids);

    // era
    push(&[R(&RE_DECADE4)], h_decade);
    push(&[R(&RE_DECADE2)], h_decade);
    push(&[E("released"), E("in"), R(&RE_YEAR4)], h_year_exact);
    push(&[E("made"), E("in"), R(&RE_YEAR4)], h_year_exact);
    push(&[E("released"), R(&RE_YEAR4)], h_year_exact);
    push(&[E("from"), R(&RE_YEAR4)], h_year_exact);
    push(&[E("in"), R(&RE_YEAR4)], h_year_exact);
    push(&[E("before"), R(&RE_YEAR4)], h_year_before);
    push(&[E("prior"), E("to"), R(&RE_YEAR4)], h_year_before);
    push(&[E("pre"), R(&RE_YEAR4)], h_year_before);
    push(&[E("after"), R(&RE_YEAR4)], h_year_after);
    push(&[E("since"), R(&RE_YEAR4)], h_year_since);
    push(&[E("retro")], h_retro);
    push(&[E("old"), E("school")], h_retro);
    push(&[E("old-school")], h_retro);
    push(&[E("oldschool")], h_retro);
    push(&[E("classic")], h_retro);
    push(&[E("vintage")], h_retro);

    // players / co-op
    push(&[E("single"), E("player")], h_players_max1);
    push(&[E("single-player")], h_players_max1);
    push(&[E("singleplayer")], h_players_max1);
    push(&[E("one"), E("player")], h_players_max1);
    push(&[E("solo")], h_players_max1);
    push(
        &[E("up"), E("to"), R(&RE_NUMWORD), R(&RE_PLAYERS)],
        h_players_min,
    );
    push(&[E("for"), R(&RE_NUMWORD), R(&RE_PLAYERS)], h_players_min);
    push(&[R(&RE_NUMWORD), R(&RE_PLAYERS)], h_players_min);
    push(&[R(&RE_PLAYERS_DASH)], h_players_dash);
    push(&[E("for"), R(&RE_NUMWORD)], h_players_min);
    push(&[E("local"), E("co-op")], h_coop);
    push(&[E("local"), E("coop")], h_coop);
    push(&[E("couch"), E("co-op")], h_coop);
    push(&[E("couch"), E("coop")], h_coop);
    push(&[E("co-operative")], h_coop);
    push(&[E("cooperative")], h_coop);
    push(&[E("co-op")], h_coop);
    push(&[E("coop")], h_coop);
    push(&[E("co"), E("op")], h_coop);
    push(&[E("couch")], h_coop);
    push(&[E("local"), E("multiplayer")], h_multiplayer);
    push(&[E("local"), E("multi"), E("player")], h_multiplayer);
    push(&[E("multi-player")], h_multiplayer);
    push(&[E("multi"), E("player")], h_multiplayer);
    push(&[E("multiplayer")], h_multiplayer);
    push(&[E("split"), E("screen")], h_multiplayer);
    push(&[E("split-screen")], h_multiplayer);
    push(&[E("splitscreen")], h_multiplayer);
    push(&[E("party"), E("games")], h_multiplayer);
    push(&[E("party")], h_multiplayer);

    // flags / preset booleans
    push(&[E("favorites")], h_fav_true);
    push(&[E("favorite")], h_fav_true);
    push(&[E("favourites")], h_fav_true);
    push(&[E("favourite")], h_fav_true);
    push(&[E("faves")], h_fav_true);
    push(&[E("not"), E("favorites")], h_fav_false);
    push(&[E("not"), E("favorite")], h_fav_false);
    push(&[E("unfavorited")], h_fav_false);
    push(&[E("unfavourited")], h_fav_false);
    push(&[E("hidden")], h_hidden_true);
    push(&[E("unhidden")], h_hidden_false);
    push(&[E("not"), E("hidden")], h_hidden_false);
    push(&[E("installed")], h_installed);
    push(&[E("uninstalled")], h_uninstalled);
    push(&[E("not"), E("installed")], h_uninstalled);
    push(&[E("owned")], h_owned);
    push(&[E("own")], h_owned);
    push(&[E("with"), E("saves")], h_saves_true);
    push(&[E("has"), E("saves")], h_saves_true);
    push(&[E("have"), E("saves")], h_saves_true);
    push(&[E("save"), E("files")], h_saves_true);
    push(&[E("saves")], h_saves_true);
    push(&[E("no"), E("saves")], h_saves_false);
    push(&[E("without"), E("saves")], h_saves_false);
    push(&[E("no"), E("save"), E("files")], h_saves_false);
    push(&[E("with"), E("achievements")], h_ach_true);
    push(&[E("has"), E("achievements")], h_ach_true);
    push(&[E("achievements")], h_ach_true);
    push(&[E("achievement"), E("support")], h_ach_true);
    push(&[E("no"), E("achievements")], h_ach_false);
    push(&[E("without"), E("achievements")], h_ach_false);
    push(&[E("missing"), E("media")], h_missing_media_true);
    push(&[E("missing"), E("covers")], h_missing_media_true);
    push(&[E("missing"), E("cover")], h_missing_media_true);
    push(&[E("missing"), E("art")], h_missing_media_true);
    push(&[E("no"), E("cover")], h_missing_media_true);
    push(&[E("no"), E("covers")], h_missing_media_true);
    push(&[E("without"), E("covers")], h_missing_media_true);
    push(&[E("without"), E("cover")], h_missing_media_true);
    push(&[E("no"), E("art")], h_missing_media_true);
    push(&[E("with"), E("covers")], h_missing_media_false);
    push(&[E("with"), E("cover")], h_missing_media_false);
    push(&[E("has"), E("covers")], h_missing_media_false);
    push(&[E("high"), E("scores")], h_highscores_true);
    push(&[E("highscores")], h_highscores_true);
    push(&[E("with"), E("high"), E("scores")], h_highscores_true);
    push(&[E("leaderboards")], h_highscores_true);
    push(&[E("shelf"), E("entries")], h_flag_manual);
    push(&[E("shelf"), E("entry")], h_flag_manual);
    push(&[E("shelf")], h_flag_manual);
    push(&[E("manual"), E("entries")], h_flag_manual);
    push(&[E("manual"), E("entry")], h_flag_manual);
    push(&[E("physical")], h_flag_manual);
    push(&[E("broken")], h_flag_broken_true);
    push(&[E("not"), E("working")], h_flag_broken_true);
    push(&[E("working")], h_flag_broken_false);
    push(&[E("portable")], h_flag_portable);
    push(&[E("controller"), E("support")], h_flag_controller);
    push(&[E("controller")], h_flag_controller);
    push(&[E("gamepad"), E("support")], h_flag_controller);
    push(&[E("gamepad")], h_flag_controller);
    push(&[E("with"), E("notes")], h_flag_notes_true);
    push(&[E("has"), E("notes")], h_flag_notes_true);
    push(&[E("noted")], h_flag_notes_true);
    push(&[E("no"), E("notes")], h_flag_notes_false);
    push(&[E("without"), E("notes")], h_flag_notes_false);

    // platform / store / credit / tag
    push(&[E("on"), W], h_on_word);
    push(&[E("for"), W], h_platform_any);
    push(&[E("from"), W], h_from_word);
    push(&[E("by"), W], h_credit);
    push(&[E("made"), E("by"), W], h_credit);
    push(&[E("developed"), E("by"), W], h_credit);
    push(&[E("published"), E("by"), W], h_credit);
    push(&[E("tagged"), E("with"), W], h_tag);
    push(&[E("tagged"), W], h_tag);
    push(&[E("tag"), W], h_tag);
    push(&[R(&RE_HASHTAG)], h_hashtag);
    push(&[E("on"), R(&RE_YEAR4)], h_year_exact);

    // negation catch-alls
    push(&[E("not"), W], h_not_query);
    push(&[E("no"), W], h_not_query);
    push(&[E("never"), W], h_not_query);
    push(&[E("without"), W], h_not_query);
    push(&[R(&RE_NEGATED)], h_not_query);

    // typed key:value tokens
    push(&[R(&RE_TYPED)], h_typed);

    for &word in SOURCE_WORDS {
        push(&[E(word), E("games")], h_source_game);
        push(&[E(word), E("library")], h_source_game);
    }

    for &(triggers, terms) in GENRE_TABLE {
        table.push(GrammarEntry {
            pattern: triggers.iter().map(|t| E(t)).collect(),
            handler: GrammarHandler::Genre(terms),
        });
    }

    // Longest match first; equal lengths preserve declaration order (Rust sort_by is stable).
    table.sort_by_key(|a| std::cmp::Reverse(a.pattern.len()));
    table
});

pub fn parse_tokens(
    tokens: &[(TokenKind, String)],
    now: Option<DateTime>,
) -> (Vec<AstNode>, Vec<(usize, String)>) {
    let now_dt = now_local(now);
    let cleaned: Vec<String> = tokens
        .iter()
        .map(|(k, t)| {
            if *k == TokenKind::Word {
                clean_word(t)
            } else {
                t.clone()
            }
        })
        .collect();
    let mut nodes: Vec<AstNode> = Vec::new();
    let mut leftover: Vec<(usize, String)> = Vec::new();
    let mut i = 0usize;
    while i < tokens.len() {
        let kind = tokens[i].0;
        let mut matched_len: Option<usize> = None;
        for entry in GRAMMAR.iter() {
            let size = entry.pattern.len();
            if i + size > tokens.len() {
                continue;
            }
            if !entry
                .pattern
                .iter()
                .enumerate()
                .all(|(j, spec)| spec.matches(tokens[i + j].0, &cleaned[i + j]))
            {
                continue;
            }
            if let Some(mut produced) = entry.handler.invoke(&cleaned[i..i + size], now_dt) {
                for n in &mut produced {
                    n.set_pos(i);
                }
                nodes.extend(produced);
                matched_len = Some(size);
                break;
            }
        }
        if let Some(sz) = matched_len {
            i += sz;
        } else {
            if kind == TokenKind::Phrase {
                if !cleaned[i].is_empty() {
                    leftover.push((i, cleaned[i].clone()));
                }
            } else if !cleaned[i].is_empty() && !STOPWORDS.contains(cleaned[i].as_str()) {
                leftover.push((i, cleaned[i].clone()));
            }
            i += 1;
        }
    }
    (nodes, leftover)
}

fn merge_clause(clauses: &mut Vec<Value>, clause: Value) {
    let kind = clause.get("kind").and_then(Value::as_str).unwrap_or("");
    if kind == "year_range" {
        for existing in clauses.iter_mut() {
            if existing.get("kind").and_then(Value::as_str) != Some("year_range") {
                continue;
            }
            let e_min = existing.get("min").and_then(Value::as_i64);
            let c_min = clause.get("min").and_then(Value::as_i64);
            let low = match (e_min, c_min) {
                (Some(a), Some(b)) => Some(a.max(b)),
                (a, None) => a,
                (None, b) => b,
            };
            let e_max = existing.get("max").and_then(Value::as_i64);
            let c_max = clause.get("max").and_then(Value::as_i64);
            let high = match (e_max, c_max) {
                (Some(a), Some(b)) => Some(a.min(b)),
                (a, None) => a,
                (None, b) => b,
            };
            if let Some(obj) = existing.as_object_mut() {
                obj.insert("min".into(), low.map_or(Value::Null, Value::from));
                obj.insert("max".into(), high.map_or(Value::Null, Value::from));
            }
            return;
        }
        clauses.push(clause);
        return;
    }

    let bound_spec: Option<(&str, bool, bool)> = match kind {
        // (field, is_float, take_min)
        "ttb_max" => Some(("hours", true, true)),
        "ttb_min" => Some(("hours", true, false)),
        "rating_min" => Some(("value", true, false)),
        "players_min" => Some(("value", false, false)),
        "players_max" => Some(("value", false, true)),
        "idle_days" => Some(("days", false, false)),
        "played_within" => Some(("days", false, true)),
        "added_days" => Some(("days", false, true)),
        _ => None,
    };
    if let Some((field, is_float, take_min)) = bound_spec {
        for existing in clauses.iter_mut() {
            if existing.get("kind").and_then(Value::as_str) == Some(kind) {
                if let Some(obj) = existing.as_object_mut() {
                    if is_float {
                        let a = obj.get(field).and_then(Value::as_f64).unwrap_or(0.0);
                        let b = clause.get(field).and_then(Value::as_f64).unwrap_or(0.0);
                        let combined = if take_min { a.min(b) } else { a.max(b) };
                        obj.insert(field.to_owned(), json!(combined));
                    } else {
                        let a = obj.get(field).and_then(Value::as_i64).unwrap_or(0);
                        let b = clause.get(field).and_then(Value::as_i64).unwrap_or(0);
                        let combined = if take_min { a.min(b) } else { a.max(b) };
                        obj.insert(field.to_owned(), json!(combined));
                    }
                }
                return;
            }
        }
        clauses.push(clause);
        return;
    }

    if !clauses.iter().any(|e| e == &clause) {
        clauses.push(clause);
    }
}

fn dedupe_strings(values: Vec<String>) -> Vec<String> {
    let mut seen = HashSet::new();
    let mut out = Vec::new();
    for v in values {
        if seen.insert(v.clone()) {
            out.push(v);
        }
    }
    out
}

fn fmt_g(val: f64) -> String {
    if val.fract() == 0.0 && val.abs() < 1e9 {
        format!("{}", val as i64)
    } else {
        format!("{val}")
    }
}

fn clause_chip(clause: &Value) -> Value {
    let kind = clause.get("kind").and_then(Value::as_str).unwrap_or("");
    let label = match kind {
        "unplayed" => "Unplayed",
        "played" => "Played",
        "backlog" => "Backlog",
        "not_finished" => "Not finished",
        "unrated" => "Unrated",
        "user_rating_min" => "My rating",
        "user_rating_unrated" => "Unrated by me",
        "multiplayer" => "Multiplayer",
        "coop" => "Co-op",
        "owned" => "Owned",
        "year_range" => "Year",
        "rating_min" => "Rating",
        "players_min" | "players_max" => "Players",
        "idle_days" | "played_within" | "idle_before" => "Last played",
        "added_days" | "added_year" => "Added",
        "ttb_max" | "ttb_min" => "Length",
        "platform_any" => "Platform",
        "genre_any" => "Genre",
        "credit" => "By",
        "series" => "Series",
        "region" => "Region",
        "source" => "Store",
        "tag" => "Tag",
        "esrb_any" => "ESRB",
        "not_query" => "Exclude",
        "flag" => "Flag",
        "progress_any" => "Progress",
        _ => kind,
    };
    let mut key = kind.to_owned();
    let mut final_label = label.to_owned();
    let i18n = format!("sayit.chip.{kind}");
    let mut value = Value::Null;
    let mut display = String::new();

    match kind {
        "year_range" => {
            let low = clause.get("min").and_then(Value::as_i64);
            let high = clause.get("max").and_then(Value::as_i64);
            value = json!([low, high]);
            display = match (low, high) {
                (Some(l), Some(h)) if l != h => format!("{l}\u{2013}{h}"),
                (Some(l), Some(_)) => l.to_string(),
                (Some(l), None) => format!("\u{2265} {l}"),
                (None, Some(h)) => format!("\u{2264} {h}"),
                (None, None) => "\u{2264} None".to_owned(),
            };
        }
        "rating_min" => {
            let v = clause.get("value").and_then(Value::as_f64).unwrap_or(0.0);
            value = json!(v);
            display = format!("\u{2265} {}", fmt_g(v));
        }
        "user_rating_min" => {
            let v = clause.get("value").and_then(Value::as_i64).unwrap_or(0);
            value = json!(v);
            display = format!("\u{2265} {v}\u{2605}");
        }
        "players_min" => {
            let v = clause.get("value").and_then(Value::as_i64).unwrap_or(1);
            value = json!(v);
            display = format!("{v}+");
        }
        "players_max" => {
            let v = clause.get("value").and_then(Value::as_i64).unwrap_or(1);
            value = json!(v);
            display = format!("\u{2264} {v}");
        }
        "idle_days" => {
            let d = clause.get("days").and_then(Value::as_i64).unwrap_or(0);
            value = json!(d);
            display = format!("\u{2265} {d} days ago");
        }
        "played_within" | "added_days" => {
            let d = clause.get("days").and_then(Value::as_i64).unwrap_or(0);
            value = json!(d);
            display = format!("\u{2264} {d} days ago");
        }
        "added_year" => {
            let y = clause.get("year").and_then(Value::as_i64).unwrap_or(0);
            value = json!(y);
            display = y.to_string();
        }
        "idle_before" => {
            let y = clause.get("year").and_then(Value::as_i64).unwrap_or(0);
            value = json!(y);
            display = format!("before {y}");
        }
        "ttb_max" | "ttb_min" => {
            let h = clause.get("hours").and_then(Value::as_f64).unwrap_or(0.0);
            value = json!(h);
            let sign = if kind == "ttb_max" {
                "\u{2264}"
            } else {
                "\u{2265}"
            };
            display = format!("{sign} {} h", fmt_g(h));
        }
        "platform_any" | "credit" | "series" | "region" | "source" | "tag" | "not_query" => {
            let t = clause.get("term").and_then(Value::as_str).unwrap_or("");
            value = Value::String(t.to_owned());
            display = t.to_owned();
        }
        "genre_any" | "esrb_any" | "progress_any" => {
            let arr_key = if kind == "genre_any" {
                "terms"
            } else {
                "values"
            };
            let vals: Vec<String> = clause
                .get(arr_key)
                .and_then(Value::as_array)
                .map(|a| {
                    a.iter()
                        .map(|v| {
                            v.as_str()
                                .map(str::to_owned)
                                .unwrap_or_else(|| v.to_string())
                        })
                        .collect()
                })
                .unwrap_or_default();
            display = vals.join(" / ");
            value = Value::Array(vals.into_iter().map(Value::String).collect());
        }
        "flag" => {
            let field = clause.get("field").and_then(Value::as_str).unwrap_or("");
            key = format!("flag_{field}");
            final_label = match field {
                "manual_entry" => "Shelf entry",
                "broken" => "Broken",
                "portable" => "Portable",
                "controller_support" => "Controller",
                "notes" => "Notes",
                other => other,
            }
            .to_owned();
            let flag_val = clause.get("value").and_then(Value::as_bool).unwrap_or(true);
            value = Value::Bool(flag_val);
            display = if flag_val {
                String::new()
            } else {
                "No".to_owned()
            };
        }
        "owned" => {
            display = "not installed".to_owned();
        }
        _ => {
            value = Value::Bool(true);
        }
    }

    json!({
        "key": key,
        "label": final_label,
        "value": value,
        "display": display,
        "i18n": i18n,
    })
}

#[derive(Clone, PartialEq, Eq, PartialOrd, Ord)]
enum OrderRef {
    Rule(String),
    Clause(usize),
}

pub fn compile_nodes(nodes: Vec<AstNode>, leftover: Vec<(usize, String)>) -> Map<String, Value> {
    let has_nodes = !nodes.is_empty();
    let mut rules = Map::new();
    let mut clauses: Vec<Value> = Vec::new();
    let mut clause_positions: HashMap<usize, usize> = HashMap::new();
    let mut order: Vec<(usize, usize, OrderRef)> = Vec::new();
    let mut seq = 0usize;
    let mut genre_terms: Vec<String> = Vec::new();
    let mut progress_vals: Vec<String> = Vec::new();
    let mut esrb_vals: Vec<String> = Vec::new();
    let mut group_pos: HashMap<&'static str, usize> = HashMap::new();

    for node in nodes {
        match node {
            AstNode::Rule { key, value, pos } => {
                rules.insert(key.clone(), value);
                order.push((pos, seq, OrderRef::Rule(key)));
                seq += 1;
            }
            AstNode::Genre { terms, pos } => {
                genre_terms.extend(terms);
                group_pos.entry("genre").or_insert(pos);
            }
            AstNode::Progress { values, pos } => {
                progress_vals.extend(values);
                group_pos.entry("progress").or_insert(pos);
            }
            AstNode::Esrb { values, pos } => {
                esrb_vals.extend(values);
                group_pos.entry("esrb").or_insert(pos);
            }
            AstNode::Clause { clause, pos } => {
                let before = clauses.len();
                merge_clause(&mut clauses, clause);
                if clauses.len() > before {
                    let idx = clauses.len() - 1;
                    clause_positions.insert(idx, pos);
                    order.push((pos, seq, OrderRef::Clause(idx)));
                    seq += 1;
                }
            }
        }
    }

    if !genre_terms.is_empty() {
        let terms = dedupe_strings(genre_terms);
        let pos = group_pos["genre"];
        if terms.len() == 1 {
            rules.insert("genre".into(), Value::String(terms[0].clone()));
            order.push((pos, seq, OrderRef::Rule("genre".to_owned())));
        } else {
            let before = clauses.len();
            merge_clause(&mut clauses, json!({"kind": "genre_any", "terms": terms}));
            let idx = clauses.len() - 1;
            if clauses.len() > before {
                clause_positions.insert(idx, pos);
            }
            order.push((pos, seq, OrderRef::Clause(idx)));
        }
        seq += 1;
    }

    if !progress_vals.is_empty() {
        let values = dedupe_strings(progress_vals);
        let pos = group_pos["progress"];
        if values.len() == 1 {
            rules.insert("progress".into(), Value::String(values[0].clone()));
            order.push((pos, seq, OrderRef::Rule("progress".to_owned())));
        } else {
            let before = clauses.len();
            merge_clause(
                &mut clauses,
                json!({"kind": "progress_any", "values": values}),
            );
            let idx = clauses.len() - 1;
            if clauses.len() > before {
                clause_positions.insert(idx, pos);
            }
            order.push((pos, seq, OrderRef::Clause(idx)));
        }
        seq += 1;
    }

    if !esrb_vals.is_empty() {
        let values = dedupe_strings(esrb_vals);
        let pos = group_pos["esrb"];
        if values.len() == 1 {
            rules.insert("esrb".into(), Value::String(values[0].clone()));
            order.push((pos, seq, OrderRef::Rule("esrb".to_owned())));
        } else {
            let before = clauses.len();
            merge_clause(&mut clauses, json!({"kind": "esrb_any", "values": values}));
            let idx = clauses.len() - 1;
            if clauses.len() > before {
                clause_positions.insert(idx, pos);
            }
            order.push((pos, seq, OrderRef::Clause(idx)));
        }
        seq += 1;
    }

    if !leftover.is_empty() {
        let q = leftover
            .iter()
            .map(|(_, w)| w.as_str())
            .collect::<Vec<_>>()
            .join(" ");
        let min_pos = leftover.iter().map(|(p, _)| *p).min().unwrap_or(0);
        rules.insert("query".into(), Value::String(q));
        order.push((min_pos, seq, OrderRef::Rule("query".to_owned())));
    }

    if clauses.len() > 1 {
        let mut permutation: Vec<usize> = (0..clauses.len()).collect();
        permutation.sort_by_key(|&idx| {
            (
                clause_positions.get(&idx).copied().unwrap_or(usize::MAX),
                idx,
            )
        });
        let is_identity = permutation.iter().enumerate().all(|(i, &p)| i == p);
        if !is_identity {
            let mut remap = HashMap::new();
            for (new_idx, &old_idx) in permutation.iter().enumerate() {
                remap.insert(old_idx, new_idx);
            }
            let old_clauses = std::mem::take(&mut clauses);
            clauses = permutation
                .iter()
                .map(|&old| old_clauses[old].clone())
                .collect();
            for item in &mut order {
                if let OrderRef::Clause(old) = item.2 {
                    item.2 = OrderRef::Clause(remap[&old]);
                }
            }
        }
    }

    order.sort_by(|a, b| a.0.cmp(&b.0).then_with(|| a.1.cmp(&b.1)));
    let mut chips: Vec<Value> = Vec::with_capacity(order.len());
    for (_, _, r) in order {
        match r {
            OrderRef::Rule(key) => {
                if let Some(val) = rules.get(&key) {
                    let mut single = Map::new();
                    single.insert(key.clone(), val.clone());
                    if let Some(mut chip) = rules_to_chips(&single).into_iter().next() {
                        if let Some(obj) = chip.as_object_mut() {
                            obj.insert("i18n".into(), Value::String(format!("sayit.chip.{key}")));
                        }
                        chips.push(chip);
                    }
                }
            }
            OrderRef::Clause(idx) => {
                chips.push(clause_chip(&clauses[idx]));
            }
        }
    }

    let unparsed: Vec<String> = leftover.into_iter().map(|(_, w)| w).collect();
    let hint = if !unparsed.is_empty() && has_nodes {
        "Some words aren't in the query grammar and were used as a plain text search."
    } else if !unparsed.is_empty() {
        "No query grammar matched — this is a plain text search."
    } else {
        ""
    };
    let complete = clauses.is_empty();

    let mut out = Map::new();
    out.insert("rules".into(), Value::Object(rules));
    out.insert("clauses".into(), Value::Array(clauses));
    out.insert("chips".into(), Value::Array(chips));
    out.insert(
        "unparsed".into(),
        Value::Array(unparsed.into_iter().map(Value::String).collect()),
    );
    out.insert("hint".into(), Value::String(hint.to_owned()));
    out.insert("parsed".into(), Value::Bool(has_nodes));
    out.insert("complete".into(), Value::Bool(complete));
    out
}

pub fn parse_query(text: &str, now: Option<DateTime>) -> Value {
    let tokens = tokenize(text);
    let (nodes, leftover) = parse_tokens(&tokens, now);
    let no_nodes = nodes.is_empty();
    let has_leftover = !leftover.is_empty();
    let mut result = compile_nodes(nodes, leftover);
    if no_nodes
        && has_leftover
        && tokens
            .iter()
            .any(|(k, w)| *k == TokenKind::Word && STOPWORDS.contains(w.as_str()))
    {
        let plain: String = tokens
            .iter()
            .filter(|(_, t)| !t.is_empty())
            .map(|(k, t)| {
                if *k == TokenKind::Phrase {
                    t.clone()
                } else {
                    clean_word(t)
                }
            })
            .collect::<Vec<_>>()
            .join(" ");
        result.insert("plain_query".into(), Value::String(plain));
    }
    result.insert("ok".into(), Value::Bool(true));
    result.insert("text".into(), Value::String(text.to_owned()));
    Value::Object(result)
}

// ── Matching ─────────────────────────────────────────────────────────────────

const COOP_WORDS: &[&str] = &["co-op", "coop", "co op", "co-operative", "cooperative"];
const MULTI_WORDS: &[&str] = &[
    "co-op",
    "coop",
    "co op",
    "co-operative",
    "cooperative",
    "multiplayer",
    "multi-player",
    "multi player",
    "versus",
    " vs",
    "vs ",
    "split screen",
    "splitscreen",
    "party",
];
const HAYSTACK_FIELDS: &[&str] = &[
    "name",
    "sort_title",
    "platform",
    "genre",
    "developer",
    "publisher",
    "series",
];

fn val_str(v: Option<&Value>) -> String {
    match v {
        None | Some(Value::Null) => String::new(),
        Some(Value::String(s)) => s.clone(),
        Some(Value::Number(n)) => n.to_string(),
        Some(Value::Bool(b)) => {
            if *b {
                "True".to_owned()
            } else {
                "False".to_owned()
            }
        }
        Some(other) => other.to_string(),
    }
}

fn sub_match(term: &str, value: &str) -> bool {
    value.to_lowercase().contains(&term.to_lowercase())
}

fn progress_matches(stored: &str, wanted: &str) -> bool {
    if wanted.to_lowercase() == "unplayed" {
        stored.is_empty() || stored.to_lowercase() == "unplayed"
    } else {
        stored == wanted
    }
}

fn notes_text(game: &Value) -> String {
    match game.get("notes") {
        Some(Value::Array(arr)) => arr
            .iter()
            .map(|item| {
                let raw = item
                    .as_object()
                    .and_then(|o| o.get("text"))
                    .map(|v| val_str(Some(v)))
                    .unwrap_or_else(|| val_str(Some(item)));
                raw.trim().to_owned()
            })
            .collect::<Vec<_>>()
            .join(" "),
        other => val_str(other),
    }
}

fn haystack(game: &Value) -> String {
    let mut parts: Vec<String> = HAYSTACK_FIELDS
        .iter()
        .map(|f| val_str(game.get(*f)))
        .collect();
    parts.push(notes_text(game));
    parts.join(" ").to_lowercase()
}

fn int_or(v: Option<&Value>, default: i64) -> i64 {
    match v {
        Some(Value::Number(n)) => n
            .as_i64()
            .or_else(|| n.as_f64().map(|f| f as i64))
            .unwrap_or(default),
        Some(Value::String(s)) => s.trim().parse::<i64>().unwrap_or(default),
        Some(Value::Bool(b)) => i64::from(*b),
        _ => default,
    }
}

fn float_or(v: Option<&Value>, default: f64) -> f64 {
    match v {
        Some(Value::Number(n)) => n.as_f64().unwrap_or(default),
        Some(Value::String(s)) => s.trim().parse::<f64>().unwrap_or(default),
        _ => default,
    }
}

fn game_year(game: &Value) -> Option<i64> {
    let s = val_str(game.get("year"));
    RE_YEAR_EXTRACT
        .find(&s)
        .and_then(|m| m.as_str().parse::<i64>().ok())
}

fn game_players(game: &Value) -> i64 {
    let n = int_or(game.get("max_players"), 1);
    if n == 0 { 1 } else { n }
}

fn game_ttb(game: &Value) -> Option<f64> {
    let h = float_or(game.get("time_to_beat_hours"), 0.0);
    if h > 0.0 { Some(h) } else { None }
}

fn parse_naive_dt(v: Option<&Value>) -> Option<DateTime> {
    let text = val_str(v);
    let trimmed = text.trim();
    if trimmed.is_empty() {
        return None;
    }
    if let Ok(ts) = trimmed.parse::<Timestamp>() {
        return Some(jiff::tz::TimeZone::system().to_datetime(ts));
    }
    let normalized = trimmed.replace(' ', "T");
    if let Ok(dt) = normalized.parse::<DateTime>() {
        return Some(dt);
    }
    if trimmed.len() >= 10
        && let Ok(d) = trimmed[..10].parse::<Date>()
    {
        return Some(d.at(0, 0, 0, 0));
    }
    None
}

fn days_between(now: DateTime, then: DateTime) -> Option<i64> {
    let now_ts = jiff::tz::TimeZone::UTC.to_timestamp(now).ok()?;
    let then_ts = jiff::tz::TimeZone::UTC.to_timestamp(then).ok()?;
    let diff = now_ts.as_second() - then_ts.as_second();
    Some(diff.div_euclid(86_400))
}

fn was_played(game: &Value) -> bool {
    if int_or(game.get("play_count"), 0) > 0 {
        return true;
    }
    if !val_str(game.get("last_played")).trim().is_empty() {
        return true;
    }
    !val_str(game.get("progress")).trim().is_empty()
}

fn truthy(v: Option<&Value>) -> bool {
    match v {
        None | Some(Value::Null) => false,
        Some(Value::Bool(b)) => *b,
        Some(Value::Number(n)) => n.as_f64().is_some_and(|x| x != 0.0),
        Some(Value::String(s)) => !s.is_empty(),
        Some(Value::Array(a)) => !a.is_empty(),
        Some(Value::Object(m)) => !m.is_empty(),
    }
}

fn is_installed(game: &Value) -> bool {
    if game.get("store_installed") == Some(&Value::Bool(false)) {
        return false;
    }
    truthy(game.get("path_exists")) || truthy(game.get("store_installed"))
}

fn is_owned(game: &Value) -> bool {
    let flag = truthy(game.get("owned"))
        || truthy(game.get("store_catalog"))
        || truthy(game.get("steam_app_id"))
        || truthy(game.get("heroic_app_id"))
        || truthy(game.get("lutris_id"))
        || truthy(game.get("gameyfin_id"))
        || truthy(game.get("gog_id"))
        || truthy(game.get("ea_id"))
        || truthy(game.get("ubisoft_id"));
    flag && !is_installed(game)
}

fn evidence(game: &Value, words: &[&str]) -> bool {
    let mut parts = vec![val_str(game.get("play_mode")), val_str(game.get("genre"))];
    if let Some(tags) = game.get("tags").and_then(Value::as_array) {
        for t in tags {
            parts.push(val_str(Some(t)));
        }
    }
    let hay = parts.join(" ").to_lowercase();
    words.iter().any(|w| hay.contains(w))
}

fn flag_value(game: &Value, field: &str) -> bool {
    match field {
        "notes" => !notes_text(game).trim().is_empty(),
        "controller_support" => !val_str(game.get("controller_support")).trim().is_empty(),
        other => truthy(game.get(other)),
    }
}

fn clause_matches(game: &Value, clause: &Value, now: DateTime) -> bool {
    let kind = clause.get("kind").and_then(Value::as_str).unwrap_or("");
    match kind {
        "unplayed" => !was_played(game),
        "played" => was_played(game),
        "backlog" => BACKLOG_PROGRESS.contains(&val_str(game.get("progress")).as_str()),
        "not_finished" => !FINISHED_PROGRESS.contains(&val_str(game.get("progress")).as_str()),
        "progress_any" => {
            let stored = val_str(game.get("progress"));
            clause
                .get("values")
                .and_then(Value::as_array)
                .is_some_and(|vals| {
                    vals.iter()
                        .any(|v| progress_matches(&stored, &val_str(Some(v))))
                })
        }
        "year_range" => {
            let Some(y) = game_year(game) else {
                return false;
            };
            let low = clause.get("min").and_then(Value::as_i64);
            let high = clause.get("max").and_then(Value::as_i64);
            low.is_none_or(|l| y >= l) && high.is_none_or(|h| y <= h)
        }
        "rating_min" => float_or(game.get("rating"), 0.0) >= float_or(clause.get("value"), 0.0),
        "unrated" => float_or(game.get("rating"), 0.0) <= 0.0,
        "user_rating_min" => int_or(game.get("user_rating"), 0) >= int_or(clause.get("value"), 0),
        "user_rating_unrated" => int_or(game.get("user_rating"), 0) <= 0,
        "players_min" => game_players(game) >= int_or(clause.get("value"), 1),
        "players_max" => game_players(game) <= int_or(clause.get("value"), 1),
        "multiplayer" => game_players(game) >= 2 || evidence(game, MULTI_WORDS),
        "coop" => evidence(game, COOP_WORDS),
        "idle_days" => {
            let Some(last) = parse_naive_dt(game.get("last_played")) else {
                return true;
            };
            days_between(now, last).is_some_and(|d| d >= int_or(clause.get("days"), 0))
        }
        "played_within" => {
            let Some(last) = parse_naive_dt(game.get("last_played")) else {
                return false;
            };
            days_between(now, last).is_some_and(|d| d <= int_or(clause.get("days"), 0))
        }
        "idle_before" => {
            let Some(last) = parse_naive_dt(game.get("last_played")) else {
                return true;
            };
            let y = int_or(clause.get("year"), 0) as i16;
            last.year() < y
        }
        "added_days" => {
            let Some(added) = parse_naive_dt(game.get("added_at")) else {
                return false;
            };
            days_between(now, added).is_some_and(|d| d <= int_or(clause.get("days"), 0))
        }
        "added_year" => {
            let Some(added) = parse_naive_dt(game.get("added_at")) else {
                return false;
            };
            i64::from(added.year()) == int_or(clause.get("year"), -1)
        }
        "ttb_max" => game_ttb(game).is_some_and(|h| h <= float_or(clause.get("hours"), 0.0)),
        "ttb_min" => game_ttb(game).is_some_and(|h| h >= float_or(clause.get("hours"), 0.0)),
        "platform_any" => sub_match(&val_str(clause.get("term")), &val_str(game.get("platform"))),
        "genre_any" => {
            let g = val_str(game.get("genre"));
            clause
                .get("terms")
                .and_then(Value::as_array)
                .is_some_and(|terms| terms.iter().any(|t| sub_match(&val_str(Some(t)), &g)))
        }
        "credit" => {
            let term = val_str(clause.get("term")).to_lowercase();
            val_str(game.get("developer"))
                .to_lowercase()
                .starts_with(&term)
                || val_str(game.get("publisher"))
                    .to_lowercase()
                    .starts_with(&term)
        }
        "series" => sub_match(&val_str(clause.get("term")), &val_str(game.get("series"))),
        "region" => sub_match(&val_str(clause.get("term")), &val_str(game.get("region"))),
        "source" => sub_match(&val_str(clause.get("term")), &val_str(game.get("source"))),
        "tag" => {
            let term = val_str(clause.get("term"));
            game.get("tags")
                .and_then(Value::as_array)
                .is_some_and(|tags| tags.iter().any(|t| sub_match(&term, &val_str(Some(t)))))
        }
        "esrb_any" => {
            let esrb = val_str(game.get("esrb"));
            clause
                .get("values")
                .and_then(Value::as_array)
                .is_some_and(|vals| vals.iter().any(|v| val_str(Some(v)) == esrb))
        }
        "flag" => {
            let field = clause.get("field").and_then(Value::as_str).unwrap_or("");
            let want = clause.get("value").and_then(Value::as_bool).unwrap_or(true);
            flag_value(game, field) == want
        }
        "owned" => is_owned(game),
        "not_query" => !sub_match(&val_str(clause.get("term")), &haystack(game)),
        _ => false,
    }
}

pub fn game_matches_query(game: &Value, parsed: &Value, now: Option<DateTime>) -> bool {
    if !game.is_object() {
        return true;
    }
    let Some(pobj) = parsed.as_object() else {
        return false;
    };
    let plain_query = pobj
        .get("plain_query")
        .and_then(Value::as_str)
        .map(str::trim)
        .unwrap_or("");
    if let Some(rules_obj) = pobj.get("rules").and_then(Value::as_object)
        && !rules_obj.is_empty()
    {
        let effective = if !plain_query.is_empty() && rules_obj.contains_key("query") {
            let mut copy = rules_obj.clone();
            copy.remove("query");
            Value::Object(copy)
        } else {
            Value::Object(rules_obj.clone())
        };
        if effective.as_object().is_some_and(|m| !m.is_empty())
            && !game_matches_rules(game, &effective)
        {
            return false;
        }
    }
    if !plain_query.is_empty() && !sub_match(plain_query, &haystack(game)) {
        return false;
    }
    let now_dt = now_local(now);
    if let Some(clauses) = pobj.get("clauses").and_then(Value::as_array) {
        for clause in clauses {
            if clause.is_object() && !clause_matches(game, clause, now_dt) {
                return false;
            }
        }
    }
    true
}

pub fn filter_games_by_query(games: &[Value], parsed: &Value, now: Option<DateTime>) -> Vec<Value> {
    if !parsed.is_object() {
        return games.to_vec();
    }
    let now_dt = now_local(now);
    games
        .iter()
        .filter(|g| game_matches_query(g, parsed, Some(now_dt)))
        .cloned()
        .collect()
}
