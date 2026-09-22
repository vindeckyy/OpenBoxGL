"""Game DNA search — classical-IR semantic-ish search over the library.

Dependency-free by design: BM25 + a curated concept lexicon + similarity via
the hashing trick. No neural embeddings, no model downloads, no cloud.
The UI says this openly ("Smart search runs fully on your device").

Index format (sidecar ``<data_dir>/dna_index.json``, NOT in the state store
or the SQLite read model):
    {"format": 1, "lexicon_version": 3, "state_signature": "<sig>",
     "doc_count": n, "df": {term: n}, "games": {<game_id>: {"v": {term: tf}, "h": "<corpus hash>"}}}

Freshness is keyed on ``STATE_STORE.signature()``. Writes are atomic
(tmp + rename). Per-game term vectors are capped at 400 terms.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import unicodedata
from typing import Any

# ── Versioning ─────────────────────────────────────────────────────────────
INDEX_FORMAT = 1
LEXICON_VERSION = 3
INDEX_FILENAME = "dna_index.json"
TERMS_PER_GAME_CAP = 400
HASHED_DIM = 1024

# ── Corpus field weights ───────────────────────────────────────────────────
_FIELD_WEIGHTS = (
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
)

# ── Tokenizer ──────────────────────────────────────────────────────────────
_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)


def _base_stopwords_en() -> frozenset:
    return frozenset(
        "a an and are as at be but by for from has have in is it its of on or that "
        "the their then there these they this to was with will you your not no nor "
        "can could should would may might must shall do does did doing done we our "
        "they them his her she he his him were been being about into over after "
        "before between during under again once here when where which while who "
        "whom whose what how all any both each few more most other some such only "
        "own same than too very game games play played playing player players".split()
    )


def _base_stopwords_de() -> frozenset:
    return frozenset(
        "der die das und oder aber als bei von für mit nach zu zum zur im in ist "
        "sind war waren hat haben nicht kein keine einer einen einem eines auch "
        "schon noch nur sehr aus auf an es sie er wir ihr ich du den dem des "
        "dass wie was wo wer wenn dann doch man mein meine deiner seine ihre "
        "spiel spiele spielen spieler".split()
    )


def _base_stopwords_es() -> frozenset:
    return frozenset(
        "el la los las un una unos unas y o pero de del en con por para que se "
        "su sus es son está están fue fueron ha han hay no sí también muy más "
        "menos como cuando donde cual cuales este esta estos estas ese esa eso "
        "aquel aquella aquello juego juegos jugar jugador".split()
    )


def _base_stopwords_fr() -> frozenset:
    return frozenset(
        "le la les un une des du de en et ou mais donc or ni car avec pour par "
        "sur dans est sont était étaient été a ont pas plus moins très aussi "
        "comme quand où quel quelle quels quelles ce cette ces celui celle "
        "jeu jeux jouer joueur".split()
    )


def _base_stopwords_pt() -> frozenset:
    return frozenset(
        "o a os as um uma uns umas e ou mas de do da dos das no na nos nas em "
        "com por para que se seu sua seus suas é são está estão foi foram há "
        "não sim também muito mais menos como quando onde qual quais este esta "
        "estes estas esse essa isso jogo jogos jogar jogador".split()
    )


STOPWORDS: dict[str, frozenset] = {
    "en": _base_stopwords_en(),
    "de": _base_stopwords_de(),
    "es": _base_stopwords_es(),
    "fr": _base_stopwords_fr(),
    "pt": _base_stopwords_pt(),
}


def stopwords_for(locale: str) -> frozenset:
    """Per-locale stopwords; unknown locales fall back to English."""
    return STOPWORDS.get((locale or "en")[:2].lower(), STOPWORDS["en"])


def tokenize(text: Any, locale: str = "en") -> list[str]:
    """Unicode-aware tokenizer: NFKC, lowercase, length >= 2, stopwords out."""
    if not text:
        return []
    text = unicodedata.normalize("NFKC", str(text)).lower()
    stop = stopwords_for(locale)
    return [word for word in _WORD_RE.findall(text) if len(word) >= 2 and word not in stop]


# ── Corpus ─────────────────────────────────────────────────────────────────
def _field_texts(game: dict[str, Any], field: str) -> list[str]:
    value = game.get(field)
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value if str(item).strip()]
    text = str(value or "").strip()
    return [text] if text else []


def corpus_hash(game: dict[str, Any]) -> str:
    """Hash of the raw corpus fields — detects per-game drift for increments."""
    parts = []
    for field, _weight in _FIELD_WEIGHTS:
        texts = _field_texts(game, field)
        parts.append(field + ":" + "\x00".join(sorted(t.casefold() for t in texts)))
    return hashlib.sha256("\x01".join(parts).encode("utf-8")).hexdigest()[:24]


def build_vector(game: dict[str, Any], locale: str = "en") -> dict[str, float]:
    """Weighted sparse term vector for one game; capped at 400 terms."""
    vector: dict[str, float] = {}
    for field, weight in _FIELD_WEIGHTS:
        for text in _field_texts(game, field):
            for term in tokenize(text, locale):
                vector[term] = vector.get(term, 0.0) + weight
    if len(vector) > TERMS_PER_GAME_CAP:
        # Keep the strongest signals; longest descriptions are the tail.
        top = sorted(vector.items(), key=lambda item: (-item[1], item[0]))[:TERMS_PER_GAME_CAP]
        vector = dict(top)
    return vector


def doc_id_for(game: dict[str, Any]) -> str:
    game_id = str(game.get("game_id") or "").strip()
    if game_id:
        return game_id
    raw = json.dumps(
        {"name": game.get("name"), "platform": game.get("platform"), "path": game.get("path")},
        sort_keys=True,
        separators=(",", ":"),
    )
    return "game-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


# ── Concept lexicon (data section) ─────────────────────────────────────────
# concept -> expansion terms. Query-side only: OR-ed into the query at lower
# weight, so the index never needs rebuilding when the lexicon changes.
CONCEPTS: dict[str, list[str]] = {
    # ── moods ──
    "cozy": ["cozy", "relaxing", "wholesome", "farming", "slow-paced", "gentle", "charming", "peaceful"],
    "relaxing": ["relaxing", "cozy", "slow-paced", "peaceful", "gentle", "zen", "calm"],
    "wholesome": ["wholesome", "cozy", "charming", "cute", "heartwarming", "uplifting"],
    "dark": ["dark", "grim", "bleak", "dystopian", "macabre", "brooding"],
    "horror": ["horror", "scary", "terrifying", "frightening", "survival-horror", "jumpscare"],
    "spooky": ["spooky", "creepy", "eerie", "haunted", "ghost", "halloween"],
    "funny": ["funny", "humorous", "comedy", "silly", "witty", "hilarious", "parody"],
    "comedy": ["comedy", "funny", "humorous", "silly", "satire", "parody"],
    "sad": ["sad", "tragic", "melancholic", "sorrowful", "heartbreaking"],
    "emotional": ["emotional", "moving", "touching", "heartfelt", "poignant"],
    "epic": ["epic", "grandiose", "sweeping", "monumental", "legendary"],
    "chill": ["chill", "relaxing", "laid-back", "casual", "low-pressure"],
    "intense": ["intense", "adrenaline", "frantic", "fast-paced", "high-stakes"],
    "tense": ["tense", "suspenseful", "nerve-wracking", "thrilling", "gripping"],
    "atmospheric": ["atmospheric", "moody", "immersive", "ambient", "evocative"],
    "melancholic": ["melancholic", "bittersweet", "wistful", "somber", "nostalgic"],
    "hopeful": ["hopeful", "uplifting", "optimistic", "inspiring"],
    "cute": ["cute", "adorable", "kawaii", "charming", "wholesome"],
    "grim": ["grim", "bleak", "harsh", "unforgiving", "brutal"],
    "creepy": ["creepy", "unsettling", "disturbing", "eerie", "uncanny"],
    "mysterious": ["mysterious", "enigmatic", "cryptic", "secretive", "puzzling"],
    "mystery": ["mystery", "detective", "investigation", "clues", "whodunit", "noir"],
    "noir": ["noir", "detective", "crime", "hardboiled", "shadowy"],
    "whimsical": ["whimsical", "playful", "quirky", "fanciful", "magical"],
    "silly": ["silly", "goofy", "absurd", "zany", "wacky"],
    "serious": ["serious", "mature", "dramatic", "weighty", "thought-provoking"],
    "dramatic": ["dramatic", "theatrical", "cinematic", "sweeping"],
    "romantic": ["romantic", "romance", "love", "relationship"],
    "lonely": ["lonely", "solitary", "isolated", "desolate", "alone"],
    "peaceful": ["peaceful", "tranquil", "serene", "calm", "quiet"],
    "scary": ["scary", "frightening", "terrifying", "horror", "spooky"],
    "weird": ["weird", "strange", "bizarre", "surreal", "trippy", "experimental"],
    # ── mechanics ──
    "soulslike": ["soulslike", "difficult", "punishing", "stamina", "bonfire", "boss", "methodical", "deliberate"],
    "souls-like": ["soulslike", "difficult", "punishing", "stamina", "bonfire", "boss"],
    "roguelike": ["roguelike", "permadeath", "procedural", "run-based", "dungeon", "turn-based"],
    "roguelite": ["roguelite", "run-based", "procedural", "meta-progression", "permadeath"],
    "metroidvania": ["metroidvania", "interconnected", "backtracking", "abilities", "exploration", "2d"],
    "permadeath": ["permadeath", "roguelike", "run-based", "high-stakes"],
    "procedural": ["procedural", "procedurally-generated", "randomized", "roguelike"],
    "crafting": ["crafting", "recipes", "gathering", "resources", "survival"],
    "building": ["building", "construction", "base-building", "sandbox", "creative"],
    "base-building": ["base-building", "building", "management", "defense", "colony"],
    "farming": ["farming", "crops", "harvest", "cozy", "life-sim", "seasons"],
    "fishing": ["fishing", "angling", "catch", "relaxing"],
    "cooking": ["cooking", "recipes", "restaurant", "food"],
    "deck-building": ["deck-building", "deckbuilder", "cards", "card-battler", "roguelike"],
    "deckbuilder": ["deckbuilder", "deck-building", "cards", "card-battler"],
    "tower-defense": ["tower-defense", "tower", "defense", "waves", "strategy"],
    "stealth": ["stealth", "sneaking", "infiltration", "shadows", "assassin"],
    "parkour": ["parkour", "traversal", "climbing", "movement", "free-running"],
    "platformer": ["platformer", "jumping", "precision", "2d", "side-scroller"],
    "shooter": ["shooter", "guns", "fps", "combat", "action"],
    "fps": ["fps", "first-person", "shooter", "guns"],
    "bullet-hell": ["bullet-hell", "shmup", "danmaku", "dodge", "patterns"],
    "hack-and-slash": ["hack-and-slash", "melee", "combo", "action", "spectacle"],
    "turn-based": ["turn-based", "tactical", "strategy", "grid"],
    "real-time": ["real-time", "rts", "fast-paced", "strategy"],
    "rts": ["rts", "real-time", "strategy", "base-building", "army"],
    "4x": ["4x", "strategy", "empire", "civilization", "grand-strategy"],
    "grand-strategy": ["grand-strategy", "strategy", "diplomacy", "map", "empire"],
    "auto-battler": ["auto-battler", "autobattler", "team", "synergy", "draft"],
    "rhythm": ["rhythm", "music", "beat", "timing", "dance"],
    "puzzle": ["puzzle", "logic", "brain-teaser", "sokoban", "match-3"],
    "point-and-click": ["point-and-click", "adventure", "inventory", "dialogue", "story"],
    "visual-novel": ["visual-novel", "story", "choices", "romance", "dialogue"],
    "dating-sim": ["dating-sim", "romance", "visual-novel", "relationships"],
    "idle": ["idle", "incremental", "clicker", "automation", "numbers-go-up"],
    "clicker": ["clicker", "idle", "incremental", "tapping"],
    "survival": ["survival", "hunger", "shelter", "crafting", "permadeath"],
    "battle-royale": ["battle-royale", "last-man-standing", "shooter", "multiplayer"],
    "moba": ["moba", "lanes", "heroes", "team", "competitive"],
    "racing": ["racing", "cars", "driving", "speed", "arcade-racer", "kart"],
    "sports": ["sports", "football", "basketball", "soccer", "competition"],
    "fighting": ["fighting", "versus", "combo", "arcade", "1v1"],
    "beat-em-up": ["beat-em-up", "brawler", "arcade", "co-op", "side-scroller"],
    "golf": ["golf", "sports", "precision", "relaxing"],
    "exploration": ["exploration", "discovery", "open-world", "wandering", "secrets"],
    "dungeon-crawler": ["dungeon-crawler", "dungeon", "loot", "crawl", "rpg"],
    "boss-fights": ["boss-fights", "boss", "challenging", "patterns", "epic"],
    "loot": ["loot", "drops", "gear", "grinding", "arpg"],
    "grinding": ["grinding", "loot", "repetition", "progression", "mmo"],
    "management": ["management", "tycoon", "economy", "simulation", "strategy"],
    "tycoon": ["tycoon", "management", "business", "economy", "building"],
    "simulation": ["simulation", "sim", "realistic", "systems", "sandbox"],
    "life-sim": ["life-sim", "farming", "relationships", "cozy", "daily-life"],
    "colony-sim": ["colony-sim", "colony", "management", "dwarves", "base-building"],
    "city-builder": ["city-builder", "city", "building", "management", "planning"],
    "factory": ["factory", "automation", "logistics", "satisfactory-like", "optimization"],
    "automation": ["automation", "factory", "programming", "idle", "optimization"],
    "programming": ["programming", "coding", "hacking", "puzzle", "logic"],
    "hacking": ["hacking", "cyberpunk", "terminal", "puzzle", "stealth"],
    "detective": ["detective", "investigation", "mystery", "clues", "deduction"],
    "investigation": ["investigation", "detective", "mystery", "evidence", "interrogation"],
    "time-travel": ["time-travel", "time", "paradox", "loops", "sci-fi"],
    "space": ["space", "sci-fi", "spaceship", "planets", "exploration"],
    "sci-fi": ["sci-fi", "science-fiction", "futuristic", "space", "technology"],
    "fantasy": ["fantasy", "magic", "dragons", "medieval", "epic"],
    "medieval": ["medieval", "knights", "castles", "fantasy", "history"],
    "western": ["western", "cowboy", "frontier", "desert", "outlaw"],
    "pirate": ["pirate", "ships", "ocean", "adventure", "treasure"],
    "zombie": ["zombie", "undead", "apocalypse", "horror", "survival"],
    "vampire": ["vampire", "gothic", "blood", "dark", "noir"],
    "underwater": ["underwater", "ocean", "diving", "submarine", "exploration"],
    # ── structure ──
    "short": ["short", "bite-sized", "2-hours", "evening", "compact"],
    "long": ["long", "epic", "100-hours", "sprawling", "massive"],
    "open-world": ["open-world", "sandbox", "exploration", "freedom", "vast"],
    "linear": ["linear", "guided", "cinematic", "story-driven", "focused"],
    "sandbox": ["sandbox", "open-ended", "creative", "freedom", "emergent"],
    "story-rich": ["story-rich", "narrative", "plot", "characters", "writing"],
    "narrative": ["narrative", "story", "plot", "dialogue", "story-driven"],
    "replayable": ["replayable", "roguelike", "endless", "new-game-plus", "variety"],
    "endless": ["endless", "infinite", "sandbox", "roguelike", "arcade"],
    "episodic": ["episodic", "chapters", "seasons", "story"],
    "co-op": ["co-op", "coop", "multiplayer", "friends", "team"],
    "coop": ["coop", "co-op", "multiplayer", "friends"],
    "multiplayer": ["multiplayer", "online", "pvp", "co-op", "competitive"],
    "singleplayer": ["singleplayer", "solo", "story", "campaign"],
    "local-multiplayer": ["local-multiplayer", "couch", "split-screen", "party", "friends"],
    "couch": ["couch", "local-multiplayer", "split-screen", "party", "friends"],
    "party": ["party", "local-multiplayer", "minigames", "friends", "casual"],
    "online": ["online", "multiplayer", "servers", "pvp", "mmo"],
    "competitive": ["competitive", "ranked", "esports", "pvp", "ladder"],
    "casual": ["casual", "easy", "pick-up-and-play", "relaxing", "accessible"],
    "hardcore": ["hardcore", "difficult", "punishing", "permadeath", "challenging"],
    "difficult": ["difficult", "challenging", "hard", "punishing", "soulslike"],
    "easy": ["easy", "casual", "accessible", "relaxing", "beginner-friendly"],
    "beginner-friendly": ["beginner-friendly", "easy", "tutorial", "accessible"],
    "completionist": ["completionist", "achievements", "collectibles", "100-percent"],
    "speedrun": ["speedrun", "timer", "fast", "leaderboard", "precision"],
    "replay": ["replay", "replayable", "nostalgia", "favorites"],
    # ── aesthetics ──
    "pixel-art": ["pixel-art", "pixel", "retro", "16-bit", "8-bit", "sprite"],
    "low-poly": ["low-poly", "stylized", "ps1", "retro-3d", "geometric"],
    "hand-drawn": ["hand-drawn", "painted", "artistic", "watercolor", "2d"],
    "retro": ["retro", "nostalgic", "8-bit", "16-bit", "arcade", "crt"],
    "voxel": ["voxel", "cubes", "minecraft-like", "3d", "blocky"],
    "anime": ["anime", "japanese", "manga", "jrpg", "visual-novel"],
    "cartoon": ["cartoon", "cel-shaded", "colorful", "stylized", "family"],
    "realistic": ["realistic", "photorealistic", "simulation", "graphics"],
    "stylized": ["stylized", "artistic", "cel-shaded", "unique"],
    "minimalist": ["minimalist", "simple", "clean", "abstract", "elegant"],
    "neon": ["neon", "synthwave", "cyberpunk", "glow", "80s"],
    "cyberpunk": ["cyberpunk", "neon", "dystopian", "futuristic", "hacking"],
    "gothic": ["gothic", "dark", "victorian", "vampire", "cathedral"],
    "watercolor": ["watercolor", "painted", "artistic", "soft", "hand-drawn"],
    "8-bit": ["8-bit", "retro", "chiptune", "nes", "pixel-art"],
    "16-bit": ["16-bit", "retro", "snes", "pixel-art", "sprite"],
    "isometric": ["isometric", "top-down", "diablo-like", "tactical"],
    "top-down": ["top-down", "twin-stick", "zelda-like", "2d"],
    "first-person": ["first-person", "fps", "immersive", "vr"],
    "third-person": ["third-person", "over-shoulder", "action", "adventure"],
    "side-scroller": ["side-scroller", "2d", "platformer", "beat-em-up"],
}
# Per-locale concept overlays: native-language synonyms for the top ~40
# concepts, so queries in the UI locale still expand. Expansions stay in
# English because game descriptions are overwhelmingly English.
CONCEPT_OVERLAYS: dict[str, dict[str, list[str]]] = {
    "de": {
        "gemütlich": CONCEPTS["cozy"],
        "entspannend": CONCEPTS["relaxing"],
        "gruselig": CONCEPTS["spooky"],
        "grusel": CONCEPTS["spooky"],
        "düster": CONCEPTS["dark"],
        "dunkel": CONCEPTS["dark"],
        "lustig": CONCEPTS["funny"],
        "komödie": CONCEPTS["comedy"],
        "traurig": CONCEPTS["sad"],
        "episch": CONCEPTS["epic"],
        "intensiv": CONCEPTS["intense"],
        "atmosphärisch": CONCEPTS["atmospheric"],
        "geheimnisvoll": CONCEPTS["mysterious"],
        "krimi": CONCEPTS["mystery"],
        "süß": CONCEPTS["cute"],
        "gruslig": CONCEPTS["creepy"],
        "schwierig": CONCEPTS["difficult"],
        "schwer": CONCEPTS["difficult"],
        "leicht": CONCEPTS["easy"],
        "einfach": CONCEPTS["easy"],
        "kurz": CONCEPTS["short"],
        "lang": CONCEPTS["long"],
        "offene-welt": CONCEPTS["open-world"],
        "welt": CONCEPTS["open-world"],
        "story": CONCEPTS["story-rich"],
        "geschichte": CONCEPTS["story-rich"],
        "mehrspieler": CONCEPTS["multiplayer"],
        "ko-op": CONCEPTS["co-op"],
        "einzelspieler": CONCEPTS["singleplayer"],
        "strategie": CONCEPTS["rts"],
        "taktik": CONCEPTS["turn-based"],
        "rennen": CONCEPTS["racing"],
        "kampf": CONCEPTS["fighting"],
        "rätsel": CONCEPTS["puzzle"],
        "puzzles": CONCEPTS["puzzle"],
        "überleben": CONCEPTS["survival"],
        "bauen": CONCEPTS["building"],
        "farmen": CONCEPTS["farming"],
        "landwirtschaft": CONCEPTS["farming"],
        "pixel": CONCEPTS["pixel-art"],
        "retro": CONCEPTS["retro"],
        "weltraum": CONCEPTS["space"],
        "fantasie": CONCEPTS["fantasy"],
        "zombie": CONCEPTS["zombie"],
        "vampir": CONCEPTS["vampire"],
    },
    "es": {
        "acogedor": CONCEPTS["cozy"],
        "relajante": CONCEPTS["relaxing"],
        "espeluznante": CONCEPTS["spooky"],
        "terror": CONCEPTS["horror"],
        "oscuro": CONCEPTS["dark"],
        "divertido": CONCEPTS["funny"],
        "triste": CONCEPTS["sad"],
        "épico": CONCEPTS["epic"],
        "epico": CONCEPTS["epic"],
        "intenso": CONCEPTS["intense"],
        "misterioso": CONCEPTS["mysterious"],
        "misterio": CONCEPTS["mystery"],
        "lindo": CONCEPTS["cute"],
        "difícil": CONCEPTS["difficult"],
        "dificil": CONCEPTS["difficult"],
        "fácil": CONCEPTS["easy"],
        "facil": CONCEPTS["easy"],
        "corto": CONCEPTS["short"],
        "largo": CONCEPTS["long"],
        "mundo-abierto": CONCEPTS["open-world"],
        "historia": CONCEPTS["story-rich"],
        "multijugador": CONCEPTS["multiplayer"],
        "cooperativo": CONCEPTS["co-op"],
        "un-jugador": CONCEPTS["singleplayer"],
        "estrategia": CONCEPTS["rts"],
        "carreras": CONCEPTS["racing"],
        "lucha": CONCEPTS["fighting"],
        "puzle": CONCEPTS["puzzle"],
        "rompecabezas": CONCEPTS["puzzle"],
        "supervivencia": CONCEPTS["survival"],
        "construcción": CONCEPTS["building"],
        "construccion": CONCEPTS["building"],
        "granja": CONCEPTS["farming"],
        "píxeles": CONCEPTS["pixel-art"],
        "retro": CONCEPTS["retro"],
        "espacio": CONCEPTS["space"],
        "fantasía": CONCEPTS["fantasy"],
        "fantasia": CONCEPTS["fantasy"],
        "zombi": CONCEPTS["zombie"],
        "vampiro": CONCEPTS["vampire"],
    },
    "fr": {
        "cosy": CONCEPTS["cozy"],
        "douillet": CONCEPTS["cozy"],
        "relaxant": CONCEPTS["relaxing"],
        "effrayant": CONCEPTS["spooky"],
        "horreur": CONCEPTS["horror"],
        "sombre": CONCEPTS["dark"],
        "drôle": CONCEPTS["funny"],
        "drole": CONCEPTS["funny"],
        "triste": CONCEPTS["sad"],
        "épique": CONCEPTS["epic"],
        "epique": CONCEPTS["epic"],
        "intense": CONCEPTS["intense"],
        "mystérieux": CONCEPTS["mysterious"],
        "mysterieux": CONCEPTS["mysterious"],
        "mystère": CONCEPTS["mystery"],
        "mystere": CONCEPTS["mystery"],
        "mignon": CONCEPTS["cute"],
        "difficile": CONCEPTS["difficult"],
        "facile": CONCEPTS["easy"],
        "court": CONCEPTS["short"],
        "long": CONCEPTS["long"],
        "monde-ouvert": CONCEPTS["open-world"],
        "histoire": CONCEPTS["story-rich"],
        "multijoueur": CONCEPTS["multiplayer"],
        "coopératif": CONCEPTS["co-op"],
        "cooperatif": CONCEPTS["co-op"],
        "solo": CONCEPTS["singleplayer"],
        "stratégie": CONCEPTS["rts"],
        "strategie": CONCEPTS["rts"],
        "course": CONCEPTS["racing"],
        "combat": CONCEPTS["fighting"],
        "énigme": CONCEPTS["puzzle"],
        "enigme": CONCEPTS["puzzle"],
        "survie": CONCEPTS["survival"],
        "construction": CONCEPTS["building"],
        "ferme": CONCEPTS["farming"],
        "pixel": CONCEPTS["pixel-art"],
        "rétro": CONCEPTS["retro"],
        "retro": CONCEPTS["retro"],
        "espace": CONCEPTS["space"],
        "fantasy": CONCEPTS["fantasy"],
        "zombie": CONCEPTS["zombie"],
        "vampire": CONCEPTS["vampire"],
    },
    "pt": {
        "aconchegante": CONCEPTS["cozy"],
        "relaxante": CONCEPTS["relaxing"],
        "assustador": CONCEPTS["spooky"],
        "terror": CONCEPTS["horror"],
        "sombrio": CONCEPTS["dark"],
        "escuro": CONCEPTS["dark"],
        "engraçado": CONCEPTS["funny"],
        "engracado": CONCEPTS["funny"],
        "triste": CONCEPTS["sad"],
        "épico": CONCEPTS["epic"],
        "epico": CONCEPTS["epic"],
        "intenso": CONCEPTS["intense"],
        "misterioso": CONCEPTS["mysterious"],
        "mistério": CONCEPTS["mystery"],
        "misterio": CONCEPTS["mystery"],
        "fofo": CONCEPTS["cute"],
        "difícil": CONCEPTS["difficult"],
        "dificil": CONCEPTS["difficult"],
        "fácil": CONCEPTS["easy"],
        "facil": CONCEPTS["easy"],
        "curto": CONCEPTS["short"],
        "longo": CONCEPTS["long"],
        "mundo-aberto": CONCEPTS["open-world"],
        "história": CONCEPTS["story-rich"],
        "historia": CONCEPTS["story-rich"],
        "multijogador": CONCEPTS["multiplayer"],
        "cooperativo": CONCEPTS["co-op"],
        "um-jogador": CONCEPTS["singleplayer"],
        "estratégia": CONCEPTS["rts"],
        "estrategia": CONCEPTS["rts"],
        "corrida": CONCEPTS["racing"],
        "luta": CONCEPTS["fighting"],
        "quebra-cabeça": CONCEPTS["puzzle"],
        "sobrevivência": CONCEPTS["survival"],
        "sobrevivencia": CONCEPTS["survival"],
        "construção": CONCEPTS["building"],
        "construcao": CONCEPTS["building"],
        "fazenda": CONCEPTS["farming"],
        "pixel": CONCEPTS["pixel-art"],
        "retrô": CONCEPTS["retro"],
        "retro": CONCEPTS["retro"],
        "espaço": CONCEPTS["space"],
        "espaco": CONCEPTS["space"],
        "fantasia": CONCEPTS["fantasy"],
        "zumbi": CONCEPTS["zombie"],
        "vampiro": CONCEPTS["vampire"],
    },
}

CONCEPT_WEIGHT = 0.5  # expansion terms weigh half of literal query terms

_ASSERTED_CONCEPTS = 100


def _validate_lexicon() -> None:
    assert len(CONCEPTS) >= _ASSERTED_CONCEPTS, f"lexicon shrank: {len(CONCEPTS)}"
    for _concept, terms in CONCEPTS.items():
        assert 1 <= len(terms) <= 12, f"concept {_concept} has {len(terms)} terms"
        assert all(isinstance(t, str) and t for t in terms)


_validate_lexicon()


def expand_concepts(terms: list[str], locale: str = "en") -> tuple[dict[str, float], list[str]]:
    """Expand query terms through the concept lexicon (query-side, OR-ed).

    Returns (extra_term_weights, chips). ``chips`` are transparency strings
    like "cozy → relaxing" shown in the UI.
    """
    overlay = CONCEPT_OVERLAYS.get((locale or "en")[:2].lower(), {})
    extra: dict[str, float] = {}
    chips: list[str] = []
    for term in terms:
        expansions = CONCEPTS.get(term) or overlay.get(term)
        if not expansions:
            continue
        for expansion in expansions:
            if expansion == term:
                continue
            extra[expansion] = max(extra.get(expansion, 0.0), CONCEPT_WEIGHT)
        chips.append(f"{term} → {expansions[1] if len(expansions) > 1 else expansions[0]}")
    return extra, chips


# ── Index I/O ──────────────────────────────────────────────────────────────
def index_path(data_dir: "str | None" = None) -> "str":
    if data_dir is None:
        from openbox import APP_DIR

        data_dir = str(APP_DIR)
    return os.path.join(data_dir, INDEX_FILENAME)


def load_index(data_dir: "str | None" = None) -> dict[str, Any] | None:
    """Load the sidecar index; None when missing or corrupt (never raises)."""
    path = index_path(data_dir)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("format") != INDEX_FORMAT:
        return None
    return payload


def save_index_atomic(index: dict[str, Any], data_dir: "str | None" = None) -> None:
    """Atomic write (tmp + rename): a crash mid-write keeps the old index."""
    path = index_path(data_dir)
    tmp_path = path + ".tmp"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # Memory-only caches (keys starting with "_") are never persisted.
    payload = {key: value for key, value in index.items() if not key.startswith("_")}
    with open(tmp_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, separators=(",", ":"), ensure_ascii=False)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp_path, path)


def empty_index(state_signature: Any = None) -> dict[str, Any]:
    return {
        "format": INDEX_FORMAT,
        "lexicon_version": LEXICON_VERSION,
        "state_signature": None if state_signature is None else list(state_signature),
        "doc_count": 0,
        "df": {},
        "games": {},
    }


def rebuild_index(games: list[dict[str, Any]], locale: str = "en", state_signature: Any = None) -> dict[str, Any]:
    """Full rebuild from scratch; returns the index payload."""
    df: dict[str, int] = {}
    entries: dict[str, dict[str, Any]] = {}
    total_len = 0.0
    for game in games or []:
        if not isinstance(game, dict):
            continue
        vector = build_vector(game, locale)
        doc_id = doc_id_for(game)
        doc_len = sum(vector.values())
        entries[doc_id] = {"v": vector, "h": corpus_hash(game), "l": doc_len}
        total_len += doc_len
        for term in vector:
            df[term] = df.get(term, 0) + 1
    count = len(entries)
    return {
        "format": INDEX_FORMAT,
        "lexicon_version": LEXICON_VERSION,
        "state_signature": None if state_signature is None else list(state_signature),
        "doc_count": count,
        "avg_len": (total_len / count) if count else 1.0,
        "df": df,
        "games": entries,
    }


def note_game_upserted(index: dict[str, Any], game: dict[str, Any], locale: str = "en") -> dict[str, Any]:
    """Incremental update for one added/changed game.

    DF deltas are approximate (documented): terms unique to the old vector are
    decremented, never below zero; drift is corrected by the idle
    reconciliation pass. Returns the mutated index.
    """
    doc_id = doc_id_for(game)
    old = (index.get("games") or {}).get(doc_id) or {}
    old_terms = old.get("v") or {}
    new_vector = build_vector(game, locale)
    df = index.setdefault("df", {})
    for term in old_terms:
        if term not in new_vector:
            df[term] = max(0, df.get(term, 0) - 1)
    for term in new_vector:
        if term not in old_terms:
            df[term] = df.get(term, 0) + 1
    old_len = float((old.get("l") or 0.0))
    new_len = sum(new_vector.values())
    total = float(index.get("avg_len") or 0.0) * max(1, int(index.get("doc_count") or 0))
    games_map = index.setdefault("games", {})
    if doc_id not in games_map:
        total += new_len
    else:
        total += new_len - old_len
    games_map[doc_id] = {"v": new_vector, "h": corpus_hash(game), "l": new_len}
    index["doc_count"] = len(games_map)
    index["avg_len"] = total / max(1, len(games_map))
    _evict_hashed(index, doc_id)
    index.pop("_postings", None)  # transient inverted index rebuilt lazily
    return index


def note_game_removed(index: dict[str, Any], game_id: str) -> dict[str, Any]:
    games_map = index.get("games") or {}
    old = games_map.pop(str(game_id), None)
    if old:
        df = index.setdefault("df", {})
        for term in old.get("v") or {}:
            df[term] = max(0, df.get(term, 0) - 1)
        total = float(index.get("avg_len") or 0.0) * (len(games_map) + 1)
        index["avg_len"] = max(0.0, total - float(old.get("l") or 0.0)) / max(1, len(games_map))
        _evict_hashed(index, str(game_id))
        index.pop("_postings", None)  # transient inverted index rebuilt lazily
    index["doc_count"] = len(games_map)
    return index


def signature_matches(index: dict[str, Any], state_signature: Any) -> bool:
    expected = None if state_signature is None else list(state_signature)
    return index.get("state_signature") == expected


# ── BM25 ───────────────────────────────────────────────────────────────────
_BM25_K1 = 1.2
_BM25_B = 0.75


def _avg_doc_len(index: dict[str, Any]) -> float:
    cached = index.get("avg_len")
    if cached:
        return float(cached)
    games = index.get("games") or {}
    if not games:
        return 1.0
    total = sum(float(entry.get("l") or 0.0) or sum(entry.get("v", {}).values()) for entry in games.values())
    return max(1.0, total / len(games))


def _postings_for(index: dict[str, Any]) -> dict[str, list[tuple[str, float]]]:
    """Transient inverted index: term -> [(doc_id, tf)]. Memory-only (the
    ``_`` prefix keeps it out of the persisted sidecar); rebuilt lazily and
    dropped by note_game_upserted/note_game_removed.
    """
    postings = index.get("_postings")
    if postings is None:
        postings = {}
        for doc_id, entry in (index.get("games") or {}).items():
            for term, tf in (entry.get("v") or {}).items():
                bucket = postings.get(term)
                if bucket is None:
                    bucket = postings[term] = []
                bucket.append((doc_id, tf))
        index["_postings"] = postings
    return postings


def bm25_search(
    index: dict[str, Any], query_weights: dict[str, float], candidates: "list[str] | None" = None
) -> list[tuple[str, float]]:
    """Score docs with BM25; returns [(doc_id, score)] sorted desc, ties by id.

    Scores only docs containing at least one query term (via the transient
    inverted index); docs without term overlap score 0 anyway, so results are
    identical to a full-corpus scan. Per-doc term contributions are summed in
    query_weights order, keeping scores bit-identical to the scan version.

    Deterministic: no wall-clock, no randomness, no dict-order dependence.
    """
    games = index.get("games") or {}
    if not games or not query_weights:
        return []
    df = index.get("df") or {}
    doc_count = max(1, int(index.get("doc_count") or len(games)))
    avg_len = _avg_doc_len(index)
    idf = {
        term: math.log(1.0 + (doc_count - df.get(term, 0) + 0.5) / (df.get(term, 0) + 0.5)) for term in query_weights
    }
    wanted = set(candidates) if candidates is not None else None
    postings = _postings_for(index)
    # hits[doc_id] = [(term, tf)] in query_weights order (terms iterate first).
    hits: dict[str, list[tuple[str, float]]] = {}
    for term, _weight in query_weights.items():
        bucket = postings.get(term)
        if not bucket:
            continue
        for doc_id, tf in bucket:
            if wanted is not None and doc_id not in wanted:
                continue
            pairs = hits.get(doc_id)
            if pairs is None:
                pairs = hits[doc_id] = []
            pairs.append((term, tf))
    scored: list[tuple[str, float]] = []
    for doc_id, pairs in hits.items():
        entry = games[doc_id]
        vector = entry.get("v") or {}
        doc_len = float(entry.get("l") or 0.0) or sum(vector.values()) or 1.0
        score = 0.0
        for term, tf in pairs:
            denom = tf + _BM25_K1 * (1.0 - _BM25_B + _BM25_B * doc_len / avg_len)
            score += query_weights[term] * idf[term] * (tf * (_BM25_K1 + 1.0) / denom)
        if score > 0.0:
            scored.append((doc_id, score))
    scored.sort(key=lambda item: (-item[1], item[0]))
    return scored


# ── Hashing-trick similarity ───────────────────────────────────────────────
def _hash_slot(term: str) -> tuple[int, int]:
    digest = hashlib.md5(term.encode("utf-8")).digest()
    slot = int.from_bytes(digest[:4], "little") % HASHED_DIM
    sign = 1.0 if digest[4] & 1 else -1.0
    return slot, sign


def hashed_vector(vector: dict[str, float]) -> list[float]:
    """1024-dim hashed projection of a sparse term vector (deterministic)."""
    dense = [0.0] * HASHED_DIM
    for term, weight in vector.items():
        slot, sign = _hash_slot(term)
        dense[slot] += sign * weight
    norm = math.sqrt(sum(value * value for value in dense))
    if norm > 0.0:
        dense = [value / norm for value in dense]
    return dense


def cosine_similarity(left: list[float], right: list[float]) -> float:
    return sum(a * b for a, b in zip(left, right, strict=True))


def _sparse_cosine(anchor_nonzero: list[tuple[int, float]], doc_hashed: list[float]) -> float:
    """Dot product over the anchor's nonzero slots only (≤400, not 1024)."""
    total = 0.0
    for slot, value in anchor_nonzero:
        total += value * doc_hashed[slot]
    return total


def _evict_hashed(index: dict[str, Any], doc_id: str) -> None:
    cache = index.get("_hashed")
    if isinstance(cache, dict):
        cache.pop(doc_id, None)


def _cached_hashed(index: dict[str, Any], doc_id: str, entry: dict[str, Any]) -> list[float]:
    """In-memory hashed-vector cache, keyed by corpus hash.

    Memory-only (never persisted — save_index_atomic strips "_" keys);
    evicted by note_game_upserted/note_game_removed. Docs whose corpus
    hash changed are rehashed on demand.
    """
    cache = index.get("_hashed")
    if not isinstance(cache, dict):
        cache = {}
        index["_hashed"] = cache
    corpus = entry.get("h")
    hit = cache.get(doc_id)
    if isinstance(hit, list) and len(hit) == 2 and hit[0] == corpus:
        return hit[1]
    vector = hashed_vector(entry.get("v") or {})
    cache[doc_id] = [corpus, vector]
    return vector


def similarity_search(
    index: dict[str, Any], anchor_vector: dict[str, float], exclude_id: "str | None" = None
) -> list[tuple[str, float]]:
    """'Games like X': cosine over hashed vectors, blended 50/50 with BM25 on
    the anchor's top terms. Deterministic, same ordering contract as BM25.

    Cosine is computed only over docs sharing at least one anchor top term:
    docs with no term overlap score ~0 on both halves anyway, and this keeps
    the branch inside the p95 < 150 ms budget at 20k docs.
    """
    games = index.get("games") or {}
    if not games or not anchor_vector:
        return []
    top_terms = dict(sorted(anchor_vector.items(), key=lambda i: (-i[1], i[0]))[:25])
    bm25 = dict(bm25_search(index, top_terms))
    candidates = [doc_id for doc_id in sorted(bm25) if doc_id != exclude_id]
    if not candidates:
        return []
    anchor_hashed = hashed_vector(anchor_vector)
    anchor_nonzero = [(slot, value) for slot, value in enumerate(anchor_hashed) if value]
    cosine: dict[str, float] = {}
    for doc_id in candidates:
        entry = games.get(doc_id) or {}
        cosine[doc_id] = max(0.0, _sparse_cosine(anchor_nonzero, _cached_hashed(index, doc_id, entry)))
    max_cosine = max(cosine.values())
    max_bm25 = max(bm25.values())
    blended = [(doc_id, 0.5 * (cosine[doc_id] / max_cosine) + 0.5 * (bm25[doc_id] / max_bm25)) for doc_id in candidates]
    blended = [(doc_id, score) for doc_id, score in blended if score > 0.0]
    blended.sort(key=lambda item: (-item[1], item[0]))
    return blended


# ── Anchor resolution (trigram title search) ───────────────────────────────
def _trigrams(value: str) -> set[str]:
    padded = f"  {value} "
    return {padded[i : i + 3] for i in range(len(padded) - 2)}


def resolve_anchor(title: str, games: list[dict[str, Any]]) -> "dict[str, Any] | None":
    """Resolve an anchor title via trigram overlap (mirrors worker.search.js).

    Requires at least half of the query trigrams to match; ties break by
    shorter name, then name for determinism.
    """
    query = unicodedata.normalize("NFKC", str(title or "")).lower().strip()
    if len(query) < 2:
        return None
    query_tris = _trigrams(query)
    best = None
    best_key = None
    for game in games or []:
        if not isinstance(game, dict):
            continue
        name = unicodedata.normalize("NFKC", str(game.get("name") or "")).lower()
        if not name:
            continue
        hay_tris = _trigrams(name)
        overlap = len(query_tris & hay_tris) / len(query_tris)
        if overlap < 0.5:
            continue
        key = (-overlap, len(name), name)
        if best_key is None or key < best_key:
            best_key = key
            best = game
    return best


_LIKE_PATTERNS = (
    re.compile(r"^\s*like\s+(?P<title>.+?)\s*$", re.IGNORECASE),
    re.compile(r"^\s*games?\s+like\s+(?P<title>.+?)\s*$", re.IGNORECASE),
    re.compile(r"^\s*similar\s+to\s+(?P<title>.+?)\s*$", re.IGNORECASE),
    re.compile(r"^\s*more\s+like\s+(?P<title>.+?)\s*$", re.IGNORECASE),
)


def extract_anchor_title(query: str) -> "str | None":
    """Detect 'like X' / 'similar to X' / 'more like X' intent; None otherwise."""
    text = str(query or "").strip().strip("\"'“”‘’")
    for pattern in _LIKE_PATTERNS:
        match = pattern.match(text)
        if match:
            title = match.group("title").strip().strip("\"'“”‘’")
            return title or None
    return None


# ── Taste boost (F4 integration surface) ───────────────────────────────────
# CONTRACT: F4 implements ``progress`` (+"Unplayed") and ``user_rating``
# (int 0-5) in parallel. These names are referenced EXACTLY; missing fields
# degrade to neutral (no boost, no crash).
def taste_boost(game: dict[str, Any], score: float, replay: bool = False) -> tuple[float, list[str]]:
    """score × (1 + 0.15 × user_rating) × novelty. Boosts are modest (≤1.5×
    total) and every one is surfaced as a why-chip — never silent."""
    chips: list[str] = []
    try:
        user_rating = int(game.get("user_rating") or 0)
    except (TypeError, ValueError):
        user_rating = 0
    user_rating = max(0, min(5, user_rating))
    rating_factor = 1.0 + 0.15 * user_rating
    if user_rating >= 3:
        chips.append(f"{user_rating}★ rated")
    progress = str(game.get("progress") or "").strip()
    if not replay and progress in ("", "Unplayed"):
        novelty_factor = 1.25
    else:
        novelty_factor = 1.0
    return score * rating_factor * novelty_factor, chips


def _ttb_chip(game: dict[str, Any]) -> "str | None":
    try:
        hours = float(game.get("time_to_beat_hours") or 0)
    except (TypeError, ValueError):
        return None
    if hours <= 0:
        return None
    if hours < 1:
        return f"~{int(hours * 60)}m to beat"
    return f"~{hours:g}h to beat"


# ── Query intent layer (D3) ─────────────────────────────────────────────────
def _soft_genre_terms(parsed: dict[str, Any], locale: str) -> list[str]:
    """Genre words from the structured parse, used as soft ranking signals.

    DNA search is semantic: a genre word inside a natural query ("cozy
    farming adventure") must not become a hard filter that empties the
    result set. Genre predicates from rules["genre"] and genre_any clauses
    are folded back into the ranking terms instead; only non-genre,
    non-text predicates (platform, time-to-beat, …) hard-filter.
    """
    terms: list[str] = []
    rules = parsed.get("rules") or {}
    genre_rule = rules.get("genre")
    if genre_rule:
        terms.extend(tokenize(genre_rule, locale))
    for clause in parsed.get("clauses") or []:
        if not isinstance(clause, dict) or clause.get("kind") != "genre_any":
            continue
        for term in clause.get("terms") or []:
            terms.extend(tokenize(term, locale))
    seen: set[str] = set()
    return [term for term in terms if not (term in seen or seen.add(term))]


def _hard_predicates(parsed: dict[str, Any]) -> "dict[str, Any] | None":
    """Parsed query with genre/text predicates stripped, or None when no
    hard predicate remains (then filter_games_by_query can be skipped).
    """
    if not parsed.get("parsed"):
        return None
    rules = {
        key: value
        for key, value in (parsed.get("rules") or {}).items()
        if key not in ("genre", "query") and value not in (None, "", [], {})
    }
    clauses = [
        clause
        for clause in (parsed.get("clauses") or [])
        if isinstance(clause, dict) and clause.get("kind") != "genre_any"
    ]
    if not rules and not clauses:
        return None
    return {"parsed": True, "rules": rules, "clauses": clauses}


def parse_dna_query(
    query: str, games: list[dict[str, Any]], index: dict[str, Any], locale: str = "en", limit: int = 50
) -> dict[str, Any]:
    """Intent pipeline; extends parity_query, never forks it.

    1. parse_query() → structured predicates (genre/platform/under-N-hours…).
    2. 'like <title>' → anchor → similarity branch.
    3. Free text → concept expansion → BM25 branch.
    4. Predicates filter, BM25/similarity ranks.
    """
    from parity_query import filter_games_by_query, parse_query

    text = str(query or "").strip()
    why: dict[str, list[str]] = {}
    anchor_title = extract_anchor_title(text)
    anchor = None
    branch = "bm25"
    scored: list[tuple[str, float]] = []
    parsed: dict[str, Any] = {"parsed": False, "rules": {}, "clauses": [], "unparsed": text}
    genre_terms: list[str] = []

    if anchor_title:
        anchor = resolve_anchor(anchor_title, games)
        if anchor is not None:
            branch = "similarity"
            anchor_doc_id = doc_id_for(anchor)
            anchor_vector = (index.get("games") or {}).get(anchor_doc_id, {}).get("v") or {}
            if not anchor_vector:
                anchor_vector = build_vector(anchor, locale)
            scored = similarity_search(index, anchor_vector, exclude_id=anchor_doc_id)
            like_chip = f"like {anchor.get('name')}"
            for doc_id, _score in scored:
                why.setdefault(doc_id, []).append(like_chip)
        else:
            # Unresolvable anchor: fall back to BM25 on the raw text; chip says so.
            branch = "bm25-unresolved-anchor"
            parsed = parse_query(text)
            terms = tokenize(text, locale)
            query_weights = {term: 1.0 for term in terms}
            extra, chips = expand_concepts(terms, locale)
            for term, weight in extra.items():
                query_weights[term] = max(query_weights.get(term, 0.0), weight)
            scored = bm25_search(index, query_weights)
            for _doc_id, _score in scored:
                why.setdefault(_doc_id, []).append(f"no match for '{anchor_title}'")
    else:
        parsed = parse_query(text)
        leftover = str(parsed.get("unparsed") or "").strip() or text
        terms = tokenize(leftover, locale)
        # Genre predicates are soft in DNA search (see _soft_genre_terms):
        # fold the genre words back into the ranking terms.
        genre_terms = _soft_genre_terms(parsed, locale)
        terms.extend(term for term in genre_terms if term not in terms)
        query_weights = {term: 1.0 for term in terms}
        extra, expansion_chips = expand_concepts(terms, locale)
        for term, weight in extra.items():
            query_weights[term] = max(query_weights.get(term, 0.0), weight)
        scored = bm25_search(index, query_weights)
        # Concept chips attach to every ranked result for transparency.
        for doc_id, _score in scored:
            if expansion_chips:
                why.setdefault(doc_id, []).extend(expansion_chips)

    # Predicates filter (ADR 0045 grammar stays authoritative); ranking ranks.
    # Genre and free-text predicates are soft (folded into ranking above, with
    # a why-chip); only the remaining structured predicates hard-filter, e.g.
    # "RPGs under 10 hours" can never return a 60-hour shooter. Skipping the
    # filter entirely when no hard predicate remains also keeps free-text
    # search O(query terms) instead of O(library × grammar rules).
    hard = _hard_predicates(parsed)
    if hard is not None:
        filtered = filter_games_by_query(games, hard)
        allowed = {doc_id_for(game) for game in filtered}
        scored = [(doc_id, score) for doc_id, score in scored if doc_id in allowed]
    if genre_terms:
        for doc_id, _score in scored:
            why.setdefault(doc_id, []).append("genre: " + ", ".join(genre_terms))

    # Never surface the anchor itself in "games like X".
    if branch == "similarity" and anchor is not None:
        anchor_doc_id = doc_id_for(anchor)
        scored = [(doc_id, score) for doc_id, score in scored if doc_id != anchor_doc_id]

    by_id = {doc_id_for(game): game for game in games if isinstance(game, dict)}
    replay = "replay" in tokenize(text, locale)
    results = []
    for doc_id, score in scored[: max(1, limit)]:
        game = by_id.get(doc_id)
        if game is None:
            continue
        boosted, taste_chips = taste_boost(game, score, replay=replay)
        chips = list(why.get(doc_id, [])) + taste_chips
        ttb_chip = _ttb_chip(game)
        if ttb_chip:
            chips.append(ttb_chip)
        # Deduplicate chips, keep order.
        seen: set[str] = set()
        unique_chips = [chip for chip in chips if not (chip in seen or seen.add(chip))]
        results.append({"game_id": doc_id, "score": round(boosted, 4), "why": unique_chips,
                        "name": str(game.get("name") or "")})

    return {
        "branch": branch,
        "anchor": {"game_id": doc_id_for(anchor), "name": anchor.get("name")} if anchor is not None else None,
        "parse": parsed,
        "results": results,
    }
