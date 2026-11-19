//! Game DNA search — classical-IR semantic-ish search over the library (`pkg/parity/parity_dna.py`).
//!
//! BM25 + curated concept lexicon (lexicon v3, 4 locale overlays) + 1024-dim MD5 hashing-trick
//! cosine similarity. Sidecar persisted at `<data_dir>/dna_index.json` (format 1).

use std::collections::{BTreeMap, HashMap, HashSet};
use std::path::{Path, PathBuf};
use std::sync::LazyLock;

use openbox_io::atomic_write_text;
use regex::Regex;
use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};
use unicode_normalization::UnicodeNormalization;

use crate::query::{filter_games_by_query, parse_query};

pub const INDEX_FORMAT: i64 = 1;
pub const LEXICON_VERSION: i64 = 3;
pub const INDEX_FILENAME: &str = "dna_index.json";
pub const TERMS_PER_GAME_CAP: usize = 400;
pub const HASHED_DIM: usize = 1024;
pub const CONCEPT_WEIGHT: f64 = 0.5;

const BM25_K1: f64 = 1.2;
const BM25_B: f64 = 0.75;

const FIELD_WEIGHTS: &[(&str, f64)] = &[
    ("name", 3.0),
    ("alternate_names", 2.0),
    ("genre", 2.0),
    ("tags", 2.0),
    ("series", 1.5),
    ("description", 1.0),
    ("notes", 1.0),
    ("developer", 0.5),
    ("publisher", 0.5),
    ("platform", 0.5),
    ("play_mode", 0.5),
];

static WORD_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"[^\W_]+").expect("valid unicode word regex"));

static LIKE_PATTERNS: LazyLock<Vec<Regex>> = LazyLock::new(|| {
    vec![
        Regex::new(r"(?i)^\s*like\s+(.+?)\s*$").unwrap(),
        Regex::new(r"(?i)^\s*games?\s+like\s+(.+?)\s*$").unwrap(),
        Regex::new(r"(?i)^\s*similar\s+to\s+(.+?)\s*$").unwrap(),
        Regex::new(r"(?i)^\s*more\s+like\s+(.+?)\s*$").unwrap(),
    ]
});

static STOPWORDS_EN: LazyLock<HashSet<&'static str>> = LazyLock::new(|| {
    "a an and are as at be but by for from has have in is it its of on or that \
     the their then there these they this to was with will you your not no nor \
     can could should would may might must shall do does did doing done we our \
     they them his her she he his him were been being about into over after \
     before between during under again once here when where which while who \
     whom whose what how all any both each few more most other some such only \
     own same than too very game games play played playing player players"
        .split_whitespace()
        .collect()
});

static STOPWORDS_DE: LazyLock<HashSet<&'static str>> = LazyLock::new(|| {
    "der die das und oder aber als bei von für mit nach zu zum zur im in ist \
     sind war waren hat haben nicht kein keine einer einen einem eines auch \
     schon noch nur sehr aus auf an es sie er wir ihr ich du den dem des \
     dass wie was wo wer wenn dann doch man mein meine deiner seine ihre \
     spiel spiele spielen spieler"
        .split_whitespace()
        .collect()
});

static STOPWORDS_ES: LazyLock<HashSet<&'static str>> = LazyLock::new(|| {
    "el la los las un una unos unas y o pero de del en con por para que se \
     su sus es son está están fue fueron ha han hay no sí también muy más \
     menos como cuando donde cual cuales este esta estos estas ese esa eso \
     aquel aquella aquello juego juegos jugar jugador"
        .split_whitespace()
        .collect()
});

static STOPWORDS_FR: LazyLock<HashSet<&'static str>> = LazyLock::new(|| {
    "le la les un une des du de en et ou mais donc or ni car avec pour par \
     sur dans est sont était étaient été a ont pas plus moins très aussi \
     comme quand où quel quelle quels quelles ce cette ces celui celle \
     jeu jeux jouer joueur"
        .split_whitespace()
        .collect()
});

static STOPWORDS_PT: LazyLock<HashSet<&'static str>> = LazyLock::new(|| {
    "o a os as um uma uns umas e ou mas de do da dos das no na nos nas em \
     com por para que se seu sua seus suas é são está estão foi foram há \
     não sim também muito mais menos como quando onde qual quais este esta \
     estes estas esse essa isso jogo jogos jogar jogador"
        .split_whitespace()
        .collect()
});

pub fn stopwords_for(locale: &str) -> &'static HashSet<&'static str> {
    let prefix: String = locale.chars().take(2).collect::<String>().to_lowercase();
    match prefix.as_str() {
        "de" => &STOPWORDS_DE,
        "es" => &STOPWORDS_ES,
        "fr" => &STOPWORDS_FR,
        "pt" => &STOPWORDS_PT,
        _ => &STOPWORDS_EN,
    }
}

pub fn tokenize(text: &str, locale: &str) -> Vec<String> {
    if text.is_empty() {
        return Vec::new();
    }
    let normalized: String = text.nfkc().collect::<String>().to_lowercase();
    let stop = stopwords_for(locale);
    WORD_RE
        .find_iter(&normalized)
        .map(|m| m.as_str())
        .filter(|w| w.chars().count() >= 2 && !stop.contains(*w))
        .map(str::to_owned)
        .collect()
}

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

fn field_texts(game: &Value, field: &str) -> Vec<String> {
    match game.get(field) {
        Some(Value::Array(arr)) => arr
            .iter()
            .map(|it| val_str(Some(it)))
            .filter(|s| !s.trim().is_empty())
            .collect(),
        other => {
            let text = val_str(other).trim().to_owned();
            if text.is_empty() {
                Vec::new()
            } else {
                vec![text]
            }
        }
    }
}

pub fn corpus_hash(game: &Value) -> String {
    let mut parts = Vec::with_capacity(FIELD_WEIGHTS.len());
    for &(field, _) in FIELD_WEIGHTS {
        let mut texts: Vec<String> = field_texts(game, field)
            .into_iter()
            .map(|t| t.to_lowercase())
            .collect();
        texts.sort();
        parts.push(format!("{field}:{}", texts.join("\x00")));
    }
    let joined = parts.join("\x01");
    let digest = Sha256::digest(joined.as_bytes());
    hex::encode(digest)[..24].to_owned()
}

pub fn build_vector(game: &Value, locale: &str) -> HashMap<String, f64> {
    let mut vector: HashMap<String, f64> = HashMap::new();
    for &(field, weight) in FIELD_WEIGHTS {
        for text in field_texts(game, field) {
            for term in tokenize(&text, locale) {
                *vector.entry(term).or_insert(0.0) += weight;
            }
        }
    }
    if vector.len() > TERMS_PER_GAME_CAP {
        let mut items: Vec<(String, f64)> = vector.into_iter().collect();
        items.sort_by(|a, b| {
            b.1.partial_cmp(&a.1)
                .unwrap_or(std::cmp::Ordering::Equal)
                .then_with(|| a.0.cmp(&b.0))
        });
        items.truncate(TERMS_PER_GAME_CAP);
        return items.into_iter().collect();
    }
    vector
}

pub fn doc_id_for(game: &Value) -> String {
    if let Some(gid) = game
        .get("game_id")
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|s| !s.is_empty())
    {
        return gid.to_owned();
    }
    let mut obj = BTreeMap::new();
    obj.insert("name", game.get("name").cloned().unwrap_or(Value::Null));
    obj.insert("path", game.get("path").cloned().unwrap_or(Value::Null));
    obj.insert(
        "platform",
        game.get("platform").cloned().unwrap_or(Value::Null),
    );
    let raw = serde_json::to_string(&obj).unwrap_or_default();
    let digest = Sha256::digest(raw.as_bytes());
    format!("game-{}", &hex::encode(digest)[..24])
}

// ── Concept lexicon (v3) ───────────────────────────────────────────────────

static CONCEPTS: LazyLock<HashMap<&'static str, &'static [&'static str]>> = LazyLock::new(|| {
    let entries: &[(&str, &[&str])] = &[
        // moods
        (
            "cozy",
            &[
                "cozy",
                "relaxing",
                "wholesome",
                "farming",
                "slow-paced",
                "gentle",
                "charming",
                "peaceful",
            ],
        ),
        (
            "relaxing",
            &[
                "relaxing",
                "cozy",
                "slow-paced",
                "peaceful",
                "gentle",
                "zen",
                "calm",
            ],
        ),
        (
            "wholesome",
            &[
                "wholesome",
                "cozy",
                "charming",
                "cute",
                "heartwarming",
                "uplifting",
            ],
        ),
        (
            "dark",
            &["dark", "grim", "bleak", "dystopian", "macabre", "brooding"],
        ),
        (
            "horror",
            &[
                "horror",
                "scary",
                "terrifying",
                "frightening",
                "survival-horror",
                "jumpscare",
            ],
        ),
        (
            "spooky",
            &["spooky", "creepy", "eerie", "haunted", "ghost", "halloween"],
        ),
        (
            "funny",
            &[
                "funny",
                "humorous",
                "comedy",
                "silly",
                "witty",
                "hilarious",
                "parody",
            ],
        ),
        (
            "comedy",
            &["comedy", "funny", "humorous", "silly", "satire", "parody"],
        ),
        (
            "sad",
            &["sad", "tragic", "melancholic", "sorrowful", "heartbreaking"],
        ),
        (
            "emotional",
            &["emotional", "moving", "touching", "heartfelt", "poignant"],
        ),
        (
            "epic",
            &["epic", "grandiose", "sweeping", "monumental", "legendary"],
        ),
        (
            "chill",
            &["chill", "relaxing", "laid-back", "casual", "low-pressure"],
        ),
        (
            "intense",
            &[
                "intense",
                "adrenaline",
                "frantic",
                "fast-paced",
                "high-stakes",
            ],
        ),
        (
            "tense",
            &[
                "tense",
                "suspenseful",
                "nerve-wracking",
                "thrilling",
                "gripping",
            ],
        ),
        (
            "atmospheric",
            &["atmospheric", "moody", "immersive", "ambient", "evocative"],
        ),
        (
            "melancholic",
            &[
                "melancholic",
                "bittersweet",
                "wistful",
                "somber",
                "nostalgic",
            ],
        ),
        (
            "hopeful",
            &["hopeful", "uplifting", "optimistic", "inspiring"],
        ),
        (
            "cute",
            &["cute", "adorable", "kawaii", "charming", "wholesome"],
        ),
        ("grim", &["grim", "bleak", "harsh", "unforgiving", "brutal"]),
        (
            "creepy",
            &["creepy", "unsettling", "disturbing", "eerie", "uncanny"],
        ),
        (
            "mysterious",
            &[
                "mysterious",
                "enigmatic",
                "cryptic",
                "secretive",
                "puzzling",
            ],
        ),
        (
            "mystery",
            &[
                "mystery",
                "detective",
                "investigation",
                "clues",
                "whodunit",
                "noir",
            ],
        ),
        (
            "noir",
            &["noir", "detective", "crime", "hardboiled", "shadowy"],
        ),
        (
            "whimsical",
            &["whimsical", "playful", "quirky", "fanciful", "magical"],
        ),
        ("silly", &["silly", "goofy", "absurd", "zany", "wacky"]),
        (
            "serious",
            &[
                "serious",
                "mature",
                "dramatic",
                "weighty",
                "thought-provoking",
            ],
        ),
        (
            "dramatic",
            &["dramatic", "theatrical", "cinematic", "sweeping"],
        ),
        ("romantic", &["romantic", "romance", "love", "relationship"]),
        (
            "lonely",
            &["lonely", "solitary", "isolated", "desolate", "alone"],
        ),
        (
            "peaceful",
            &["peaceful", "tranquil", "serene", "calm", "quiet"],
        ),
        (
            "scary",
            &["scary", "frightening", "terrifying", "horror", "spooky"],
        ),
        (
            "weird",
            &[
                "weird",
                "strange",
                "bizarre",
                "surreal",
                "trippy",
                "experimental",
            ],
        ),
        // mechanics
        (
            "soulslike",
            &[
                "soulslike",
                "difficult",
                "punishing",
                "stamina",
                "bonfire",
                "boss",
                "methodical",
                "deliberate",
            ],
        ),
        (
            "souls-like",
            &[
                "soulslike",
                "difficult",
                "punishing",
                "stamina",
                "bonfire",
                "boss",
            ],
        ),
        (
            "roguelike",
            &[
                "roguelike",
                "permadeath",
                "procedural",
                "run-based",
                "dungeon",
                "turn-based",
            ],
        ),
        (
            "roguelite",
            &[
                "roguelite",
                "run-based",
                "procedural",
                "meta-progression",
                "permadeath",
            ],
        ),
        (
            "metroidvania",
            &[
                "metroidvania",
                "interconnected",
                "backtracking",
                "abilities",
                "exploration",
                "2d",
            ],
        ),
        (
            "permadeath",
            &["permadeath", "roguelike", "run-based", "high-stakes"],
        ),
        (
            "procedural",
            &[
                "procedural",
                "procedurally-generated",
                "randomized",
                "roguelike",
            ],
        ),
        (
            "crafting",
            &["crafting", "recipes", "gathering", "resources", "survival"],
        ),
        (
            "building",
            &[
                "building",
                "construction",
                "base-building",
                "sandbox",
                "creative",
            ],
        ),
        (
            "base-building",
            &[
                "base-building",
                "building",
                "management",
                "defense",
                "colony",
            ],
        ),
        (
            "farming",
            &["farming", "crops", "harvest", "cozy", "life-sim", "seasons"],
        ),
        ("fishing", &["fishing", "angling", "catch", "relaxing"]),
        ("cooking", &["cooking", "recipes", "restaurant", "food"]),
        (
            "deck-building",
            &[
                "deck-building",
                "deckbuilder",
                "cards",
                "card-battler",
                "roguelike",
            ],
        ),
        (
            "deckbuilder",
            &["deckbuilder", "deck-building", "cards", "card-battler"],
        ),
        (
            "tower-defense",
            &["tower-defense", "tower", "defense", "waves", "strategy"],
        ),
        (
            "stealth",
            &["stealth", "sneaking", "infiltration", "shadows", "assassin"],
        ),
        (
            "parkour",
            &[
                "parkour",
                "traversal",
                "climbing",
                "movement",
                "free-running",
            ],
        ),
        (
            "platformer",
            &["platformer", "jumping", "precision", "2d", "side-scroller"],
        ),
        ("shooter", &["shooter", "guns", "fps", "combat", "action"]),
        ("fps", &["fps", "first-person", "shooter", "guns"]),
        (
            "bullet-hell",
            &["bullet-hell", "shmup", "danmaku", "dodge", "patterns"],
        ),
        (
            "hack-and-slash",
            &["hack-and-slash", "melee", "combo", "action", "spectacle"],
        ),
        (
            "turn-based",
            &["turn-based", "tactical", "strategy", "grid"],
        ),
        ("real-time", &["real-time", "rts", "fast-paced", "strategy"]),
        (
            "rts",
            &["rts", "real-time", "strategy", "base-building", "army"],
        ),
        (
            "4x",
            &["4x", "strategy", "empire", "civilization", "grand-strategy"],
        ),
        (
            "grand-strategy",
            &["grand-strategy", "strategy", "diplomacy", "map", "empire"],
        ),
        (
            "auto-battler",
            &["auto-battler", "autobattler", "team", "synergy", "draft"],
        ),
        ("rhythm", &["rhythm", "music", "beat", "timing", "dance"]),
        (
            "puzzle",
            &["puzzle", "logic", "brain-teaser", "sokoban", "match-3"],
        ),
        (
            "point-and-click",
            &[
                "point-and-click",
                "adventure",
                "inventory",
                "dialogue",
                "story",
            ],
        ),
        (
            "visual-novel",
            &["visual-novel", "story", "choices", "romance", "dialogue"],
        ),
        (
            "dating-sim",
            &["dating-sim", "romance", "visual-novel", "relationships"],
        ),
        (
            "idle",
            &[
                "idle",
                "incremental",
                "clicker",
                "automation",
                "numbers-go-up",
            ],
        ),
        ("clicker", &["clicker", "idle", "incremental", "tapping"]),
        (
            "survival",
            &["survival", "hunger", "shelter", "crafting", "permadeath"],
        ),
        (
            "battle-royale",
            &[
                "battle-royale",
                "last-man-standing",
                "shooter",
                "multiplayer",
            ],
        ),
        ("moba", &["moba", "lanes", "heroes", "team", "competitive"]),
        (
            "racing",
            &["racing", "cars", "driving", "speed", "arcade-racer", "kart"],
        ),
        (
            "sports",
            &["sports", "football", "basketball", "soccer", "competition"],
        ),
        (
            "fighting",
            &["fighting", "versus", "combo", "arcade", "1v1"],
        ),
        (
            "beat-em-up",
            &["beat-em-up", "brawler", "arcade", "co-op", "side-scroller"],
        ),
        ("golf", &["golf", "sports", "precision", "relaxing"]),
        (
            "exploration",
            &[
                "exploration",
                "discovery",
                "open-world",
                "wandering",
                "secrets",
            ],
        ),
        (
            "dungeon-crawler",
            &["dungeon-crawler", "dungeon", "loot", "crawl", "rpg"],
        ),
        (
            "boss-fights",
            &["boss-fights", "boss", "challenging", "patterns", "epic"],
        ),
        ("loot", &["loot", "drops", "gear", "grinding", "arpg"]),
        (
            "grinding",
            &["grinding", "loot", "repetition", "progression", "mmo"],
        ),
        (
            "management",
            &["management", "tycoon", "economy", "simulation", "strategy"],
        ),
        (
            "tycoon",
            &["tycoon", "management", "business", "economy", "building"],
        ),
        (
            "simulation",
            &["simulation", "sim", "realistic", "systems", "sandbox"],
        ),
        (
            "life-sim",
            &["life-sim", "farming", "relationships", "cozy", "daily-life"],
        ),
        (
            "colony-sim",
            &[
                "colony-sim",
                "colony",
                "management",
                "dwarves",
                "base-building",
            ],
        ),
        (
            "city-builder",
            &["city-builder", "city", "building", "management", "planning"],
        ),
        (
            "factory",
            &[
                "factory",
                "automation",
                "logistics",
                "satisfactory-like",
                "optimization",
            ],
        ),
        (
            "automation",
            &[
                "automation",
                "factory",
                "programming",
                "idle",
                "optimization",
            ],
        ),
        (
            "programming",
            &["programming", "coding", "hacking", "puzzle", "logic"],
        ),
        (
            "hacking",
            &["hacking", "cyberpunk", "terminal", "puzzle", "stealth"],
        ),
        (
            "detective",
            &[
                "detective",
                "investigation",
                "mystery",
                "clues",
                "deduction",
            ],
        ),
        (
            "investigation",
            &[
                "investigation",
                "detective",
                "mystery",
                "evidence",
                "interrogation",
            ],
        ),
        (
            "time-travel",
            &["time-travel", "time", "paradox", "loops", "sci-fi"],
        ),
        (
            "space",
            &["space", "sci-fi", "spaceship", "planets", "exploration"],
        ),
        (
            "sci-fi",
            &[
                "sci-fi",
                "science-fiction",
                "futuristic",
                "space",
                "technology",
            ],
        ),
        (
            "fantasy",
            &["fantasy", "magic", "dragons", "medieval", "epic"],
        ),
        (
            "medieval",
            &["medieval", "knights", "castles", "fantasy", "history"],
        ),
        (
            "western",
            &["western", "cowboy", "frontier", "desert", "outlaw"],
        ),
        (
            "pirate",
            &["pirate", "ships", "ocean", "adventure", "treasure"],
        ),
        (
            "zombie",
            &["zombie", "undead", "apocalypse", "horror", "survival"],
        ),
        ("vampire", &["vampire", "gothic", "blood", "dark", "noir"]),
        (
            "underwater",
            &["underwater", "ocean", "diving", "submarine", "exploration"],
        ),
        // structure
        (
            "short",
            &["short", "bite-sized", "2-hours", "evening", "compact"],
        ),
        (
            "long",
            &["long", "epic", "100-hours", "sprawling", "massive"],
        ),
        (
            "open-world",
            &["open-world", "sandbox", "exploration", "freedom", "vast"],
        ),
        (
            "linear",
            &["linear", "guided", "cinematic", "story-driven", "focused"],
        ),
        (
            "sandbox",
            &["sandbox", "open-ended", "creative", "freedom", "emergent"],
        ),
        (
            "story-rich",
            &["story-rich", "narrative", "plot", "characters", "writing"],
        ),
        (
            "narrative",
            &["narrative", "story", "plot", "dialogue", "story-driven"],
        ),
        (
            "replayable",
            &[
                "replayable",
                "roguelike",
                "endless",
                "new-game-plus",
                "variety",
            ],
        ),
        (
            "endless",
            &["endless", "infinite", "sandbox", "roguelike", "arcade"],
        ),
        ("episodic", &["episodic", "chapters", "seasons", "story"]),
        (
            "co-op",
            &["co-op", "coop", "multiplayer", "friends", "team"],
        ),
        ("coop", &["coop", "co-op", "multiplayer", "friends"]),
        (
            "multiplayer",
            &["multiplayer", "online", "pvp", "co-op", "competitive"],
        ),
        (
            "singleplayer",
            &["singleplayer", "solo", "story", "campaign"],
        ),
        (
            "local-multiplayer",
            &[
                "local-multiplayer",
                "couch",
                "split-screen",
                "party",
                "friends",
            ],
        ),
        (
            "couch",
            &[
                "couch",
                "local-multiplayer",
                "split-screen",
                "party",
                "friends",
            ],
        ),
        (
            "party",
            &[
                "party",
                "local-multiplayer",
                "minigames",
                "friends",
                "casual",
            ],
        ),
        (
            "online",
            &["online", "multiplayer", "servers", "pvp", "mmo"],
        ),
        (
            "competitive",
            &["competitive", "ranked", "esports", "pvp", "ladder"],
        ),
        (
            "casual",
            &[
                "casual",
                "easy",
                "pick-up-and-play",
                "relaxing",
                "accessible",
            ],
        ),
        (
            "hardcore",
            &[
                "hardcore",
                "difficult",
                "punishing",
                "permadeath",
                "challenging",
            ],
        ),
        (
            "difficult",
            &["difficult", "challenging", "hard", "punishing", "soulslike"],
        ),
        (
            "easy",
            &[
                "easy",
                "casual",
                "accessible",
                "relaxing",
                "beginner-friendly",
            ],
        ),
        (
            "beginner-friendly",
            &["beginner-friendly", "easy", "tutorial", "accessible"],
        ),
        (
            "completionist",
            &[
                "completionist",
                "achievements",
                "collectibles",
                "100-percent",
            ],
        ),
        (
            "speedrun",
            &["speedrun", "timer", "fast", "leaderboard", "precision"],
        ),
        (
            "replay",
            &["replay", "replayable", "nostalgia", "favorites"],
        ),
        // aesthetics
        (
            "pixel-art",
            &["pixel-art", "pixel", "retro", "16-bit", "8-bit", "sprite"],
        ),
        (
            "low-poly",
            &["low-poly", "stylized", "ps1", "retro-3d", "geometric"],
        ),
        (
            "hand-drawn",
            &["hand-drawn", "painted", "artistic", "watercolor", "2d"],
        ),
        (
            "retro",
            &["retro", "nostalgic", "8-bit", "16-bit", "arcade", "crt"],
        ),
        (
            "voxel",
            &["voxel", "cubes", "minecraft-like", "3d", "blocky"],
        ),
        (
            "anime",
            &["anime", "japanese", "manga", "jrpg", "visual-novel"],
        ),
        (
            "cartoon",
            &["cartoon", "cel-shaded", "colorful", "stylized", "family"],
        ),
        (
            "realistic",
            &["realistic", "photorealistic", "simulation", "graphics"],
        ),
        (
            "stylized",
            &["stylized", "artistic", "cel-shaded", "unique"],
        ),
        (
            "minimalist",
            &["minimalist", "simple", "clean", "abstract", "elegant"],
        ),
        ("neon", &["neon", "synthwave", "cyberpunk", "glow", "80s"]),
        (
            "cyberpunk",
            &["cyberpunk", "neon", "dystopian", "futuristic", "hacking"],
        ),
        (
            "gothic",
            &["gothic", "dark", "victorian", "vampire", "cathedral"],
        ),
        (
            "watercolor",
            &["watercolor", "painted", "artistic", "soft", "hand-drawn"],
        ),
        ("8-bit", &["8-bit", "retro", "chiptune", "nes", "pixel-art"]),
        (
            "16-bit",
            &["16-bit", "retro", "snes", "pixel-art", "sprite"],
        ),
        (
            "isometric",
            &["isometric", "top-down", "diablo-like", "tactical"],
        ),
        ("top-down", &["top-down", "twin-stick", "zelda-like", "2d"]),
        ("first-person", &["first-person", "fps", "immersive", "vr"]),
        (
            "third-person",
            &["third-person", "over-shoulder", "action", "adventure"],
        ),
        (
            "side-scroller",
            &["side-scroller", "2d", "platformer", "beat-em-up"],
        ),
    ];
    entries.iter().copied().collect()
});

fn overlay_concept(locale: &str, term: &str) -> Option<&'static [&'static str]> {
    let lang: String = locale.chars().take(2).collect::<String>().to_lowercase();
    let target = match lang.as_str() {
        "de" => match term {
            "gemütlich" => "cozy",
            "entspannend" => "relaxing",
            "gruselig" | "grusel" => "spooky",
            "düster" | "dunkel" => "dark",
            "lustig" => "funny",
            "komödie" => "comedy",
            "traurig" => "sad",
            "episch" => "epic",
            "intensiv" => "intense",
            "atmosphärisch" => "atmospheric",
            "geheimnisvoll" => "mysterious",
            "krimi" => "mystery",
            "süß" => "cute",
            "gruslig" => "creepy",
            "schwierig" | "schwer" => "difficult",
            "leicht" | "einfach" => "easy",
            "kurz" => "short",
            "lang" => "long",
            "offene-welt" | "welt" => "open-world",
            "story" | "geschichte" => "story-rich",
            "mehrspieler" => "multiplayer",
            "ko-op" => "co-op",
            "einzelspieler" => "singleplayer",
            "strategie" => "rts",
            "taktik" => "turn-based",
            "rennen" => "racing",
            "kampf" => "fighting",
            "rätsel" | "puzzles" => "puzzle",
            "überleben" => "survival",
            "bauen" => "building",
            "farmen" | "landwirtschaft" => "farming",
            "pixel" => "pixel-art",
            "retro" => "retro",
            "weltraum" => "space",
            "fantasie" => "fantasy",
            "zombie" => "zombie",
            "vampir" => "vampire",
            _ => return None,
        },
        "es" => match term {
            "acogedor" => "cozy",
            "relajante" => "relaxing",
            "espeluznante" => "spooky",
            "terror" => "horror",
            "oscuro" => "dark",
            "divertido" => "funny",
            "triste" => "sad",
            "épico" | "epico" => "epic",
            "intenso" => "intense",
            "misterioso" => "mysterious",
            "misterio" => "mystery",
            "lindo" => "cute",
            "difícil" | "dificil" => "difficult",
            "fácil" | "facil" => "easy",
            "corto" => "short",
            "largo" => "long",
            "mundo-abierto" => "open-world",
            "historia" => "story-rich",
            "multijugador" => "multiplayer",
            "cooperativo" => "co-op",
            "un-jugador" => "singleplayer",
            "estrategia" => "rts",
            "carreras" => "racing",
            "lucha" => "fighting",
            "puzle" | "rompecabezas" => "puzzle",
            "supervivencia" => "survival",
            "construcción" | "construccion" => "building",
            "granja" => "farming",
            "píxeles" => "pixel-art",
            "retro" => "retro",
            "espacio" => "space",
            "fantasía" | "fantasia" => "fantasy",
            "zombi" => "zombie",
            "vampiro" => "vampire",
            _ => return None,
        },
        "fr" => match term {
            "cosy" | "douillet" => "cozy",
            "relaxant" => "relaxing",
            "effrayant" => "spooky",
            "horreur" => "horror",
            "sombre" => "dark",
            "drôle" | "drole" => "funny",
            "triste" => "sad",
            "épique" | "epique" => "epic",
            "intense" => "intense",
            "mystérieux" | "mysterieux" => "mysterious",
            "mystère" | "mystere" => "mystery",
            "mignon" => "cute",
            "difficile" => "difficult",
            "facile" => "easy",
            "court" => "short",
            "long" => "long",
            "monde-ouvert" => "open-world",
            "histoire" => "story-rich",
            "multijoueur" => "multiplayer",
            "coopératif" | "cooperatif" => "co-op",
            "solo" => "singleplayer",
            "stratégie" | "strategie" => "rts",
            "course" => "racing",
            "combat" => "fighting",
            "énigme" | "enigme" => "puzzle",
            "survie" => "survival",
            "construction" => "building",
            "ferme" => "farming",
            "pixel" => "pixel-art",
            "rétro" | "retro" => "retro",
            "espace" => "space",
            "fantasy" => "fantasy",
            "zombie" => "zombie",
            "vampire" => "vampire",
            _ => return None,
        },
        "pt" => match term {
            "aconchegante" => "cozy",
            "relaxante" => "relaxing",
            "assustador" => "spooky",
            "terror" => "horror",
            "sombrio" | "escuro" => "dark",
            "engraçado" | "engracado" => "funny",
            "triste" => "sad",
            "épico" | "epico" => "epic",
            "intenso" => "intense",
            "misterioso" => "mysterious",
            "mistério" | "misterio" => "mystery",
            "fofo" => "cute",
            "difícil" | "dificil" => "difficult",
            "fácil" | "facil" => "easy",
            "curto" => "short",
            "longo" => "long",
            "mundo-aberto" => "open-world",
            "história" | "historia" => "story-rich",
            "multijogador" => "multiplayer",
            "cooperativo" => "co-op",
            "um-jogador" => "singleplayer",
            "estratégia" | "estrategia" => "rts",
            "corrida" => "racing",
            "luta" => "fighting",
            "quebra-cabeça" => "puzzle",
            "sobrevivência" | "sobrevivencia" => "survival",
            "construção" | "construcao" => "building",
            "fazenda" => "farming",
            "pixel" => "pixel-art",
            "retrô" | "retro" => "retro",
            "espaço" | "espaco" => "space",
            "fantasia" => "fantasy",
            "zumbi" => "zombie",
            "vampiro" => "vampire",
            _ => return None,
        },
        _ => return None,
    };
    CONCEPTS.get(target).copied()
}

pub fn expand_concepts(terms: &[String], locale: &str) -> (HashMap<String, f64>, Vec<String>) {
    let mut extra: HashMap<String, f64> = HashMap::new();
    let mut chips: Vec<String> = Vec::new();
    for term in terms {
        let Some(expansions) = CONCEPTS
            .get(term.as_str())
            .copied()
            .or_else(|| overlay_concept(locale, term))
        else {
            continue;
        };
        for &exp in expansions {
            if exp == term.as_str() {
                continue;
            }
            let cur = extra.entry(exp.to_owned()).or_insert(0.0);
            if CONCEPT_WEIGHT > *cur {
                *cur = CONCEPT_WEIGHT;
            }
        }
        let target = if expansions.len() > 1 {
            expansions[1]
        } else {
            expansions[0]
        };
        chips.push(format!("{term} \u{2192} {target}"));
    }
    (extra, chips)
}

// ── Index I/O ──────────────────────────────────────────────────────────────

pub fn index_path(data_dir: &Path) -> PathBuf {
    data_dir.join(INDEX_FILENAME)
}

pub fn load_index(data_dir: &Path) -> Option<Value> {
    let raw = std::fs::read_to_string(index_path(data_dir)).ok()?;
    let payload: Value = serde_json::from_str(&raw).ok()?;
    if payload.get("format").and_then(Value::as_i64) != Some(INDEX_FORMAT) {
        return None;
    }
    Some(payload)
}

pub fn save_index_atomic(index: &Value, data_dir: &Path) -> Result<(), String> {
    let Some(obj) = index.as_object() else {
        return Err("Index must be an object.".to_owned());
    };
    let mut clean = Map::new();
    for (k, v) in obj {
        if !k.starts_with('_') {
            clean.insert(k.clone(), v.clone());
        }
    }
    let text = serde_json::to_string(&Value::Object(clean)).map_err(|e| e.to_string())?;
    atomic_write_text(&index_path(data_dir), &text, 0o600)
        .map(|_| ())
        .map_err(|e| e.to_string())
}

pub fn empty_index(state_signature: Option<&Value>) -> Value {
    json!({
        "format": INDEX_FORMAT,
        "lexicon_version": LEXICON_VERSION,
        "state_signature": state_signature.cloned().unwrap_or(Value::Null),
        "doc_count": 0,
        "df": {},
        "games": {},
    })
}

pub fn rebuild_index(games: &[Value], locale: &str, state_signature: Option<&Value>) -> Value {
    let mut df: Map<String, Value> = Map::new();
    let mut df_counts: HashMap<String, i64> = HashMap::new();
    let mut entries: Map<String, Value> = Map::new();
    let mut total_len = 0.0f64;

    for game in games {
        if !game.is_object() {
            continue;
        }
        let vector = build_vector(game, locale);
        let doc_id = doc_id_for(game);
        let doc_len: f64 = vector.values().sum();
        for term in vector.keys() {
            *df_counts.entry(term.clone()).or_insert(0) += 1;
        }
        let v_map: Map<String, Value> = vector.into_iter().map(|(k, w)| (k, json!(w))).collect();
        entries.insert(
            doc_id,
            json!({"v": Value::Object(v_map), "h": corpus_hash(game), "l": doc_len}),
        );
        total_len += doc_len;
    }
    for (term, cnt) in df_counts {
        df.insert(term, json!(cnt));
    }
    let count = entries.len();
    let avg_len = if count > 0 {
        total_len / count as f64
    } else {
        1.0
    };
    json!({
        "format": INDEX_FORMAT,
        "lexicon_version": LEXICON_VERSION,
        "state_signature": state_signature.cloned().unwrap_or(Value::Null),
        "doc_count": count,
        "avg_len": avg_len,
        "df": Value::Object(df),
        "games": Value::Object(entries),
    })
}

pub fn note_game_upserted(index: &mut Value, game: &Value, locale: &str) {
    let doc_id = doc_id_for(game);
    let old_terms: HashSet<String> = index
        .get("games")
        .and_then(|g| g.get(&doc_id))
        .and_then(|e| e.get("v"))
        .and_then(Value::as_object)
        .map(|m| m.keys().cloned().collect())
        .unwrap_or_default();
    let old_len = index
        .get("games")
        .and_then(|g| g.get(&doc_id))
        .and_then(|e| e.get("l"))
        .and_then(Value::as_f64)
        .unwrap_or(0.0);
    let existed = index
        .get("games")
        .and_then(Value::as_object)
        .is_some_and(|m| m.contains_key(&doc_id));
    let prev_avg = index.get("avg_len").and_then(Value::as_f64).unwrap_or(0.0);
    let prev_count = index
        .get("doc_count")
        .and_then(Value::as_u64)
        .unwrap_or(0)
        .max(1) as f64;

    let new_vec = build_vector(game, locale);
    let new_len: f64 = new_vec.values().sum();
    let Some(root) = index.as_object_mut() else {
        return;
    };
    let df = root
        .entry("df".to_owned())
        .or_insert_with(|| Value::Object(Map::new()));
    if let Some(df_map) = df.as_object_mut() {
        for term in &old_terms {
            if !new_vec.contains_key(term) {
                let next = df_map
                    .get(term)
                    .and_then(Value::as_i64)
                    .unwrap_or(0)
                    .saturating_sub(1);
                df_map.insert(term.clone(), json!(next));
            }
        }
        for term in new_vec.keys() {
            if !old_terms.contains(term) {
                let next = df_map.get(term).and_then(Value::as_i64).unwrap_or(0) + 1;
                df_map.insert(term.clone(), json!(next));
            }
        }
    }

    let total = if existed {
        prev_avg * prev_count + (new_len - old_len)
    } else {
        prev_avg * prev_count + new_len
    };
    let v_map: Map<String, Value> = new_vec.into_iter().map(|(k, w)| (k, json!(w))).collect();
    let games_val = root
        .entry("games".to_owned())
        .or_insert_with(|| Value::Object(Map::new()));
    let new_count = if let Some(gm) = games_val.as_object_mut() {
        gm.insert(
            doc_id,
            json!({"v": Value::Object(v_map), "h": corpus_hash(game), "l": new_len}),
        );
        gm.len()
    } else {
        0
    };
    root.insert("doc_count".into(), json!(new_count));
    root.insert("avg_len".into(), json!(total / (new_count.max(1) as f64)));
}

pub fn note_game_removed(index: &mut Value, game_id: &str) {
    let Some(root) = index.as_object_mut() else {
        return;
    };
    let prev_avg = root.get("avg_len").and_then(Value::as_f64).unwrap_or(0.0);
    let old = root
        .get_mut("games")
        .and_then(Value::as_object_mut)
        .and_then(|gm| gm.remove(game_id));
    let new_count = root
        .get("games")
        .and_then(Value::as_object)
        .map(Map::len)
        .unwrap_or(0);
    if let Some(old_entry) = old {
        let old_len = old_entry.get("l").and_then(Value::as_f64).unwrap_or(0.0);
        if let Some(old_v) = old_entry.get("v").and_then(Value::as_object) {
            let df = root
                .entry("df".to_owned())
                .or_insert_with(|| Value::Object(Map::new()));
            if let Some(df_map) = df.as_object_mut() {
                for term in old_v.keys() {
                    let next = df_map
                        .get(term)
                        .and_then(Value::as_i64)
                        .unwrap_or(0)
                        .saturating_sub(1);
                    df_map.insert(term.clone(), json!(next));
                }
            }
        }
        let total = (prev_avg * ((new_count + 1) as f64) - old_len).max(0.0);
        root.insert("avg_len".into(), json!(total / (new_count.max(1) as f64)));
    }
    root.insert("doc_count".into(), json!(new_count));
}

pub fn signature_matches(index: &Value, state_signature: Option<&Value>) -> bool {
    let expected = state_signature.cloned().unwrap_or(Value::Null);
    index.get("state_signature").cloned().unwrap_or(Value::Null) == expected
}

// ── BM25 ───────────────────────────────────────────────────────────────────

fn avg_doc_len(index: &Value) -> f64 {
    if let Some(cached) = index.get("avg_len").and_then(Value::as_f64)
        && cached > 0.0
    {
        return cached;
    }
    let Some(games) = index.get("games").and_then(Value::as_object) else {
        return 1.0;
    };
    if games.is_empty() {
        return 1.0;
    }
    let total: f64 = games
        .values()
        .map(|entry| {
            let l = entry.get("l").and_then(Value::as_f64).unwrap_or(0.0);
            if l > 0.0 {
                l
            } else {
                entry
                    .get("v")
                    .and_then(Value::as_object)
                    .map(|m| m.values().filter_map(Value::as_f64).sum())
                    .unwrap_or(0.0)
            }
        })
        .sum();
    (total / games.len() as f64).max(1.0)
}

pub fn bm25_search(
    index: &Value,
    query_weights: &[(String, f64)],
    candidates: Option<&HashSet<String>>,
) -> Vec<(String, f64)> {
    let Some(games) = index.get("games").and_then(Value::as_object) else {
        return Vec::new();
    };
    if games.is_empty() || query_weights.is_empty() {
        return Vec::new();
    }
    let df = index.get("df").and_then(Value::as_object);
    let doc_count = index
        .get("doc_count")
        .and_then(Value::as_u64)
        .unwrap_or(games.len() as u64)
        .max(1) as f64;
    let avg_len = avg_doc_len(index);
    let idf: Vec<(&str, f64, f64)> = query_weights
        .iter()
        .map(|(term, weight)| {
            let df_t = df
                .and_then(|m| m.get(term))
                .and_then(Value::as_f64)
                .unwrap_or(0.0);
            let val = (1.0 + (doc_count - df_t + 0.5) / (df_t + 0.5)).ln();
            (term.as_str(), *weight, val)
        })
        .collect();

    let mut scored: Vec<(String, f64)> = Vec::new();
    for (doc_id, entry) in games {
        if let Some(wanted) = candidates
            && !wanted.contains(doc_id)
        {
            continue;
        }
        let Some(vec_map) = entry.get("v").and_then(Value::as_object) else {
            continue;
        };
        let mut score = 0.0f64;
        let mut doc_len_opt: Option<f64> = None;
        for &(term, weight, term_idf) in &idf {
            let Some(tf) = vec_map.get(term).and_then(Value::as_f64) else {
                continue;
            };
            let doc_len = *doc_len_opt.get_or_insert_with(|| {
                let l = entry.get("l").and_then(Value::as_f64).unwrap_or(0.0);
                if l > 0.0 {
                    l
                } else {
                    let s: f64 = vec_map.values().filter_map(Value::as_f64).sum();
                    if s > 0.0 { s } else { 1.0 }
                }
            });
            let denom = tf + BM25_K1 * (1.0 - BM25_B + BM25_B * doc_len / avg_len);
            score += weight * term_idf * (tf * (BM25_K1 + 1.0) / denom);
        }
        if score > 0.0 {
            scored.push((doc_id.clone(), score));
        }
    }
    scored.sort_by(|a, b| {
        b.1.partial_cmp(&a.1)
            .unwrap_or(std::cmp::Ordering::Equal)
            .then_with(|| a.0.cmp(&b.0))
    });
    scored
}

// ── MD5 (RFC 1321) for exact Python hashlib.md5 hashing-trick parity ───────

#[allow(clippy::many_single_char_names)]
fn md5_digest(input: &[u8]) -> [u8; 16] {
    const S: [u32; 64] = [
        7, 12, 17, 22, 7, 12, 17, 22, 7, 12, 17, 22, 7, 12, 17, 22, 5, 9, 14, 20, 5, 9, 14, 20, 5,
        9, 14, 20, 5, 9, 14, 20, 4, 11, 16, 23, 4, 11, 16, 23, 4, 11, 16, 23, 4, 11, 16, 23, 6, 10,
        15, 21, 6, 10, 15, 21, 6, 10, 15, 21, 6, 10, 15, 21,
    ];
    const K: [u32; 64] = [
        0xd76aa478, 0xe8c7b756, 0x242070db, 0xc1bdceee, 0xf57c0faf, 0x4787c62a, 0xa8304613,
        0xfd469501, 0x698098d8, 0x8b44f7af, 0xffff5bb1, 0x895cd7be, 0x6b901122, 0xfd987193,
        0xa679438e, 0x49b40821, 0xf61e2562, 0xc040b340, 0x265e5a51, 0xe9b6c7aa, 0xd62f105d,
        0x02441453, 0xd8a1e681, 0xe7d3fbc8, 0x21e1cde6, 0xc33707d6, 0xf4d50d87, 0x455a14ed,
        0xa9e3e905, 0xfcefa3f8, 0x676f02d9, 0x8d2a4c8a, 0xfffa3942, 0x8771f681, 0x6d9d6122,
        0xfde5380c, 0xa4beea44, 0x4bdecfa9, 0xf6bb4b60, 0xbebfbc70, 0x289b7ec6, 0xeaa127fa,
        0xd4ef3085, 0x04881d05, 0xd9d4d039, 0xe6db99e5, 0x1fa27cf8, 0xc4ac5665, 0xf4292244,
        0x432aff97, 0xab9423a7, 0xfc93a039, 0x655b59c3, 0x8f0ccc92, 0xffeff47d, 0x85845dd1,
        0x6fa87e4f, 0xfe2ce6e0, 0xa3014314, 0x4e0811a1, 0xf7537e82, 0xbd3af235, 0x2ad7d2bb,
        0xeb86d391,
    ];
    let mut msg = input.to_vec();
    let bit_len = (msg.len() as u64).wrapping_mul(8);
    msg.push(0x80);
    while msg.len() % 64 != 56 {
        msg.push(0);
    }
    msg.extend_from_slice(&bit_len.to_le_bytes());

    let mut a0: u32 = 0x6745_2301;
    let mut b0: u32 = 0xefcd_ab89;
    let mut c0: u32 = 0x98ba_dcfe;
    let mut d0: u32 = 0x1032_5476;

    for chunk in msg.as_chunks::<64>().0 {
        let mut m = [0u32; 16];
        for (i, word) in m.iter_mut().enumerate() {
            *word = u32::from_le_bytes([
                chunk[i * 4],
                chunk[i * 4 + 1],
                chunk[i * 4 + 2],
                chunk[i * 4 + 3],
            ]);
        }
        let (mut a, mut b, mut c, mut d) = (a0, b0, c0, d0);
        for i in 0..64 {
            let (f, g) = match i {
                0..=15 => ((b & c) | ((!b) & d), i),
                16..=31 => ((d & b) | ((!d) & c), (5 * i + 1) % 16),
                32..=47 => (b ^ c ^ d, (3 * i + 5) % 16),
                _ => (c ^ (b | (!d)), (7 * i) % 16),
            };
            let f = f.wrapping_add(a).wrapping_add(K[i]).wrapping_add(m[g]);
            a = d;
            d = c;
            c = b;
            b = b.wrapping_add(f.rotate_left(S[i]));
        }
        a0 = a0.wrapping_add(a);
        b0 = b0.wrapping_add(b);
        c0 = c0.wrapping_add(c);
        d0 = d0.wrapping_add(d);
    }

    let mut out = [0u8; 16];
    out[0..4].copy_from_slice(&a0.to_le_bytes());
    out[4..8].copy_from_slice(&b0.to_le_bytes());
    out[8..12].copy_from_slice(&c0.to_le_bytes());
    out[12..16].copy_from_slice(&d0.to_le_bytes());
    out
}

fn hash_slot(term: &str) -> (usize, f64) {
    let digest = md5_digest(term.as_bytes());
    let raw = u32::from_le_bytes([digest[0], digest[1], digest[2], digest[3]]) as usize;
    let slot = raw % HASHED_DIM;
    let sign = if digest[4] & 1 != 0 { 1.0 } else { -1.0 };
    (slot, sign)
}

pub fn hashed_vector(vector: &HashMap<String, f64>) -> Vec<f64> {
    let mut dense = vec![0.0f64; HASHED_DIM];
    for (term, &weight) in vector {
        let (slot, sign) = hash_slot(term);
        dense[slot] += sign * weight;
    }
    let norm: f64 = dense.iter().map(|v| v * v).sum::<f64>().sqrt();
    if norm > 0.0 {
        for v in &mut dense {
            *v /= norm;
        }
    }
    dense
}

fn hashed_vector_from_map(map: &Map<String, Value>) -> Vec<f64> {
    let mut dense = vec![0.0f64; HASHED_DIM];
    for (term, val) in map {
        if let Some(weight) = val.as_f64() {
            let (slot, sign) = hash_slot(term);
            dense[slot] += sign * weight;
        }
    }
    let norm: f64 = dense.iter().map(|v| v * v).sum::<f64>().sqrt();
    if norm > 0.0 {
        for v in &mut dense {
            *v /= norm;
        }
    }
    dense
}

pub fn similarity_search(
    index: &Value,
    anchor_vector: &HashMap<String, f64>,
    exclude_id: Option<&str>,
) -> Vec<(String, f64)> {
    let Some(games) = index.get("games").and_then(Value::as_object) else {
        return Vec::new();
    };
    if games.is_empty() || anchor_vector.is_empty() {
        return Vec::new();
    }
    let mut top: Vec<(String, f64)> = anchor_vector.iter().map(|(k, &v)| (k.clone(), v)).collect();
    top.sort_by(|a, b| {
        b.1.partial_cmp(&a.1)
            .unwrap_or(std::cmp::Ordering::Equal)
            .then_with(|| a.0.cmp(&b.0))
    });
    top.truncate(25);
    let bm25_pairs = bm25_search(index, &top, None);
    let bm25: HashMap<String, f64> = bm25_pairs.into_iter().collect();
    let mut candidates: Vec<String> = bm25
        .keys()
        .filter(|id| exclude_id != Some(id.as_str()))
        .cloned()
        .collect();
    candidates.sort();
    if candidates.is_empty() {
        return Vec::new();
    }
    let anchor_hashed = hashed_vector(anchor_vector);
    let anchor_nonzero: Vec<(usize, f64)> = anchor_hashed
        .iter()
        .enumerate()
        .filter(|(_, v)| **v != 0.0)
        .map(|(i, &v)| (i, v))
        .collect();
    let mut cosine: HashMap<String, f64> = HashMap::new();
    let mut max_cosine = 0.0f64;
    let mut max_bm25 = 0.0f64;
    for doc_id in &candidates {
        let doc_hashed = games
            .get(doc_id)
            .and_then(|e| e.get("v"))
            .and_then(Value::as_object)
            .map(hashed_vector_from_map)
            .unwrap_or_else(|| vec![0.0; HASHED_DIM]);
        let dot: f64 = anchor_nonzero
            .iter()
            .map(|&(slot, val)| val * doc_hashed[slot])
            .sum::<f64>()
            .max(0.0);
        if dot > max_cosine {
            max_cosine = dot;
        }
        let b = bm25[doc_id];
        if b > max_bm25 {
            max_bm25 = b;
        }
        cosine.insert(doc_id.clone(), dot);
    }
    let mut blended: Vec<(String, f64)> = candidates
        .into_iter()
        .filter_map(|doc_id| {
            let c_norm = if max_cosine > 0.0 {
                cosine[&doc_id] / max_cosine
            } else {
                0.0
            };
            let b_norm = if max_bm25 > 0.0 {
                bm25[&doc_id] / max_bm25
            } else {
                0.0
            };
            let score = 0.5 * c_norm + 0.5 * b_norm;
            if score > 0.0 {
                Some((doc_id, score))
            } else {
                None
            }
        })
        .collect();
    blended.sort_by(|a, b| {
        b.1.partial_cmp(&a.1)
            .unwrap_or(std::cmp::Ordering::Equal)
            .then_with(|| a.0.cmp(&b.0))
    });
    blended
}

// ── Anchor resolution ──────────────────────────────────────────────────────

fn trigrams(value: &str) -> HashSet<String> {
    let chars: Vec<char> = format!("  {value} ").chars().collect();
    if chars.len() < 3 {
        return HashSet::new();
    }
    (0..=chars.len() - 3)
        .map(|i| chars[i..i + 3].iter().collect())
        .collect()
}

pub fn resolve_anchor<'a>(title: &str, games: &'a [Value]) -> Option<&'a Value> {
    let query: String = title
        .nfkc()
        .collect::<String>()
        .to_lowercase()
        .trim()
        .to_owned();
    if query.chars().count() < 2 {
        return None;
    }
    let q_tris = trigrams(&query);
    if q_tris.is_empty() {
        return None;
    }
    let mut best: Option<&'a Value> = None;
    let mut best_key: Option<(f64, usize, String)> = None;
    for game in games {
        if !game.is_object() {
            continue;
        }
        let name: String = game
            .get("name")
            .and_then(Value::as_str)
            .unwrap_or("")
            .nfkc()
            .collect::<String>()
            .to_lowercase();
        if name.is_empty() {
            continue;
        }
        let h_tris = trigrams(&name);
        let overlap = q_tris.intersection(&h_tris).count() as f64 / q_tris.len() as f64;
        if overlap < 0.5 {
            continue;
        }
        let key = (-overlap, name.chars().count(), name);
        let is_better = match &best_key {
            None => true,
            Some(cur) => key
                .0
                .partial_cmp(&cur.0)
                .unwrap_or(std::cmp::Ordering::Equal)
                .then_with(|| key.1.cmp(&cur.1))
                .then_with(|| key.2.cmp(&cur.2))
                .is_lt(),
        };
        if is_better {
            best_key = Some(key);
            best = Some(game);
        }
    }
    best
}

pub fn extract_anchor_title(query: &str) -> Option<String> {
    let quotes = ['"', '\'', '\u{201c}', '\u{201d}', '\u{2018}', '\u{2019}'];
    let text = query.trim().trim_matches(quotes);
    for pat in LIKE_PATTERNS.iter() {
        if let Some(cap) = pat.captures(text)
            && let Some(m) = cap.get(1)
        {
            let title = m.as_str().trim().trim_matches(quotes);
            if !title.is_empty() {
                return Some(title.to_owned());
            }
        }
    }
    None
}

// ── Taste boost & intent pipeline ──────────────────────────────────────────

pub fn taste_boost(game: &Value, score: f64, replay: bool) -> (f64, Vec<String>) {
    let mut chips = Vec::new();
    let user_rating = game
        .get("user_rating")
        .and_then(|v| {
            v.as_i64()
                .or_else(|| v.as_f64().map(|f| f as i64))
                .or_else(|| v.as_str().and_then(|s| s.trim().parse::<i64>().ok()))
        })
        .unwrap_or(0)
        .clamp(0, 5);
    let rating_factor = 1.0 + 0.15 * user_rating as f64;
    if user_rating >= 3 {
        chips.push(format!("{user_rating}\u{2605} rated"));
    }
    let progress = game
        .get("progress")
        .and_then(Value::as_str)
        .map(str::trim)
        .unwrap_or("");
    let novelty_factor = if !replay && (progress.is_empty() || progress == "Unplayed") {
        1.25
    } else {
        1.0
    };
    (score * rating_factor * novelty_factor, chips)
}

fn ttb_chip(game: &Value) -> Option<String> {
    let hours = game
        .get("time_to_beat_hours")
        .and_then(|v| {
            v.as_f64()
                .or_else(|| v.as_str().and_then(|s| s.trim().parse::<f64>().ok()))
        })
        .unwrap_or(0.0);
    if hours <= 0.0 {
        return None;
    }
    if hours < 1.0 {
        Some(format!("~{}m to beat", (hours * 60.0) as i64))
    } else if hours.fract() == 0.0 {
        Some(format!("~{}h to beat", hours as i64))
    } else {
        Some(format!("~{hours}h to beat"))
    }
}

fn soft_genre_terms(parsed: &Value, locale: &str) -> Vec<String> {
    let mut terms = Vec::new();
    if let Some(g) = parsed
        .get("rules")
        .and_then(|r| r.get("genre"))
        .and_then(Value::as_str)
    {
        terms.extend(tokenize(g, locale));
    }
    if let Some(clauses) = parsed.get("clauses").and_then(Value::as_array) {
        for clause in clauses {
            if clause.get("kind").and_then(Value::as_str) == Some("genre_any")
                && let Some(arr) = clause.get("terms").and_then(Value::as_array)
            {
                for t in arr {
                    if let Some(s) = t.as_str() {
                        terms.extend(tokenize(s, locale));
                    }
                }
            }
        }
    }
    let mut seen = HashSet::new();
    terms.retain(|t| seen.insert(t.clone()));
    terms
}

fn hard_predicates(parsed: &Value) -> Option<Value> {
    if !parsed
        .get("parsed")
        .and_then(Value::as_bool)
        .unwrap_or(false)
    {
        return None;
    }
    let mut rules = Map::new();
    if let Some(rmap) = parsed.get("rules").and_then(Value::as_object) {
        for (k, v) in rmap {
            if k == "genre" || k == "query" {
                continue;
            }
            let empty = match v {
                Value::Null => true,
                Value::String(s) => s.is_empty(),
                Value::Array(a) => a.is_empty(),
                Value::Object(m) => m.is_empty(),
                _ => false,
            };
            if !empty {
                rules.insert(k.clone(), v.clone());
            }
        }
    }
    let clauses: Vec<Value> = parsed
        .get("clauses")
        .and_then(Value::as_array)
        .map(|a| {
            a.iter()
                .filter(|c| {
                    c.is_object() && c.get("kind").and_then(Value::as_str) != Some("genre_any")
                })
                .cloned()
                .collect()
        })
        .unwrap_or_default();
    if rules.is_empty() && clauses.is_empty() {
        return None;
    }
    Some(json!({"parsed": true, "rules": Value::Object(rules), "clauses": clauses}))
}

fn build_ordered_weights(terms: &[String], extra: &HashMap<String, f64>) -> Vec<(String, f64)> {
    let mut weights: Vec<(String, f64)> = Vec::new();
    let mut idx_by_term: HashMap<String, usize> = HashMap::new();
    for t in terms {
        if !idx_by_term.contains_key(t) {
            idx_by_term.insert(t.clone(), weights.len());
            weights.push((t.clone(), 1.0));
        }
    }
    let mut extra_sorted: Vec<(&String, &f64)> = extra.iter().collect();
    extra_sorted.sort_by_key(|(k, _)| *k);
    for (term, &w) in extra_sorted {
        if let Some(&idx) = idx_by_term.get(term) {
            if w > weights[idx].1 {
                weights[idx].1 = w;
            }
        } else {
            idx_by_term.insert(term.clone(), weights.len());
            weights.push((term.clone(), w));
        }
    }
    weights
}

pub fn parse_dna_query(
    query: &str,
    games: &[Value],
    index: &Value,
    locale: &str,
    limit: usize,
) -> Value {
    let text = query.trim();
    let mut why: HashMap<String, Vec<String>> = HashMap::new();
    let anchor_title = extract_anchor_title(text);
    let mut anchor: Option<&Value> = None;
    let branch: &str;
    let mut scored: Vec<(String, f64)>;
    let mut parsed = json!({"parsed": false, "rules": {}, "clauses": [], "unparsed": text});
    let mut genre_terms: Vec<String> = Vec::new();

    if let Some(ref atitle) = anchor_title {
        anchor = resolve_anchor(atitle, games);
        if let Some(anc) = anchor {
            branch = "similarity";
            let anchor_doc_id = doc_id_for(anc);
            let mut anchor_vec: HashMap<String, f64> = index
                .get("games")
                .and_then(|g| g.get(&anchor_doc_id))
                .and_then(|e| e.get("v"))
                .and_then(Value::as_object)
                .map(|m| {
                    m.iter()
                        .filter_map(|(k, v)| Some((k.clone(), v.as_f64()?)))
                        .collect()
                })
                .unwrap_or_default();
            if anchor_vec.is_empty() {
                anchor_vec = build_vector(anc, locale);
            }
            scored = similarity_search(index, &anchor_vec, Some(&anchor_doc_id));
            let like_chip = format!(
                "like {}",
                anc.get("name").and_then(Value::as_str).unwrap_or("")
            );
            for (doc_id, _) in &scored {
                why.entry(doc_id.clone())
                    .or_default()
                    .push(like_chip.clone());
            }
        } else {
            branch = "bm25-unresolved-anchor";
            parsed = parse_query(text, None);
            let terms = tokenize(text, locale);
            let (extra, _) = expand_concepts(&terms, locale);
            let weights = build_ordered_weights(&terms, &extra);
            scored = bm25_search(index, &weights, None);
            let chip = format!("no match for '{atitle}'");
            for (doc_id, _) in &scored {
                why.entry(doc_id.clone()).or_default().push(chip.clone());
            }
        }
    } else {
        branch = "bm25";
        parsed = parse_query(text, None);
        let unparsed_text = match parsed.get("unparsed") {
            Some(Value::Array(arr)) if !arr.is_empty() => arr
                .iter()
                .filter_map(Value::as_str)
                .collect::<Vec<_>>()
                .join(" "),
            Some(Value::String(s)) if !s.trim().is_empty() => s.trim().to_owned(),
            _ => text.to_owned(),
        };
        let leftover = if unparsed_text.trim().is_empty() {
            text.to_owned()
        } else {
            unparsed_text
        };
        let mut terms = tokenize(&leftover, locale);
        genre_terms = soft_genre_terms(&parsed, locale);
        for gt in &genre_terms {
            if !terms.contains(gt) {
                terms.push(gt.clone());
            }
        }
        let (extra, expansion_chips) = expand_concepts(&terms, locale);
        let weights = build_ordered_weights(&terms, &extra);
        scored = bm25_search(index, &weights, None);
        if !expansion_chips.is_empty() {
            for (doc_id, _) in &scored {
                why.entry(doc_id.clone())
                    .or_default()
                    .extend(expansion_chips.iter().cloned());
            }
        }
    }

    if let Some(hard) = hard_predicates(&parsed) {
        let filtered = filter_games_by_query(games, &hard, None);
        let allowed: HashSet<String> = filtered.iter().map(doc_id_for).collect();
        scored.retain(|(doc_id, _)| allowed.contains(doc_id));
    }
    if !genre_terms.is_empty() {
        let chip = format!("genre: {}", genre_terms.join(", "));
        for (doc_id, _) in &scored {
            why.entry(doc_id.clone()).or_default().push(chip.clone());
        }
    }
    if branch == "similarity"
        && let Some(anc) = anchor
    {
        let anchor_doc_id = doc_id_for(anc);
        scored.retain(|(doc_id, _)| doc_id != &anchor_doc_id);
    }

    let by_id: HashMap<String, &Value> = games
        .iter()
        .filter(|g| g.is_object())
        .map(|g| (doc_id_for(g), g))
        .collect();
    let replay = tokenize(text, locale).iter().any(|t| t == "replay");
    let mut results = Vec::new();
    for (doc_id, score) in scored.into_iter().take(limit.max(1)) {
        let Some(&game) = by_id.get(&doc_id) else {
            continue;
        };
        let (boosted, taste_chips) = taste_boost(game, score, replay);
        let mut chips = why.remove(&doc_id).unwrap_or_default();
        chips.extend(taste_chips);
        if let Some(tc) = ttb_chip(game) {
            chips.push(tc);
        }
        let mut seen = HashSet::new();
        let unique_chips: Vec<String> = chips
            .into_iter()
            .filter(|c| seen.insert(c.clone()))
            .collect();
        results.push(json!({
            "game_id": doc_id,
            "score": (boosted * 10_000.0).round() / 10_000.0,
            "why": unique_chips,
            "name": game.get("name").and_then(Value::as_str).unwrap_or(""),
        }));
    }

    let anchor_json = anchor.map(|a| {
        json!({
            "game_id": doc_id_for(a),
            "name": a.get("name").cloned().unwrap_or(Value::Null),
        })
    });
    json!({
        "branch": branch,
        "anchor": anchor_json,
        "parse": parsed,
        "results": results,
    })
}
