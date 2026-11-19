//! LaunchBox Premium parity: custom fields, ESRB, media packs, localized strings,
//! platform categories, ROM ranking, and trailer/GOG downloads (`pkg/parity/parity_premium.py`).

use std::collections::{BTreeMap, HashSet};
use std::io::Read;
use std::path::Path;
use std::sync::LazyLock;

use openbox_io::{DownloadOptions, download_file};
use openbox_metadata::http::{block_on_io, http_get_raw, quote_path};
use regex::Regex;
use serde_json::{Map, Value, json};

pub const ESRB_VALUES: &[&str] = &[
    "", "E", "E10+", "T", "M", "AO", "RP", "EC", "K-A", "Unrated",
];

pub const BULK_WIZARD_FIELDS: &[&str] = &[
    "platform",
    "genre",
    "progress",
    "rating",
    "user_rating",
    "favorite",
    "hidden",
    "esrb",
    "custom_fields",
    "reset_stats",
    "play_count",
    "playtime_seconds",
    "last_played",
];

pub const MAX_ROM_ARCHIVE_BYTES: usize = 512 * 1024 * 1024;

static BAD_TAGS: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"(?i)\((?:beta|proto|demo|sample|unl|pirate|hack|translation)\)").unwrap()
});

pub fn default_platform_categories() -> BTreeMap<String, String> {
    [
        ("Arcade", "Arcade"),
        ("MAME", "Arcade"),
        ("FinalBurn Neo", "Arcade"),
        ("NES", "Nintendo"),
        ("SNES", "Nintendo"),
        ("Nintendo 64", "Nintendo"),
        ("Game Boy", "Nintendo"),
        ("Game Boy Color", "Nintendo"),
        ("Game Boy Advance", "Nintendo"),
        ("Nintendo DS", "Nintendo"),
        ("Nintendo 3DS", "Nintendo"),
        ("GameCube", "Nintendo"),
        ("Wii", "Nintendo"),
        ("WiiWare", "Nintendo"),
        ("Wii U", "Nintendo"),
        ("Nintendo Switch", "Nintendo"),
        ("PlayStation", "Sony"),
        ("PlayStation 2", "Sony"),
        ("PlayStation 3", "Sony"),
        ("PSP", "Sony"),
        ("PlayStation Vita", "Sony"),
        ("Xbox", "Microsoft"),
        ("Xbox 360", "Microsoft"),
        ("PC", "Computer"),
        ("Windows", "Computer"),
        ("Linux", "Computer"),
        ("ScummVM", "Adventure"),
        ("DOS", "Computer"),
    ]
    .into_iter()
    .map(|(k, v)| (k.to_owned(), v.to_owned()))
    .collect()
}

pub fn strings_for(locale: &str) -> Value {
    let lang = locale
        .to_lowercase()
        .split('-')
        .next()
        .unwrap_or("en")
        .to_owned();
    match lang.as_str() {
        "es" => json!({
            "library": "Biblioteca",
            "all_games": "Todos los juegos",
            "import": "Importar",
            "settings": "Ajustes",
            "big_box": "Big Box",
            "list_view": "Vista de lista",
            "grid_view": "Vista de cuadrícula",
            "drop_import": "Suelta carpetas ROM o archivos aquí para importar",
        }),
        "de" => json!({
            "library": "Bibliothek",
            "all_games": "Alle Spiele",
            "import": "Importieren",
            "settings": "Einstellungen",
            "big_box": "Big Box",
            "list_view": "Listenansicht",
            "grid_view": "Rasteransicht",
            "drop_import": "ROM-Ordner oder Spieldateien hier ablegen",
        }),
        "fr" => json!({
            "library": "Bibliothèque",
            "all_games": "Tous les jeux",
            "import": "Importer",
            "settings": "Paramètres",
            "big_box": "Big Box",
            "list_view": "Vue liste",
            "grid_view": "Vue grille",
            "drop_import": "Déposez des dossiers ROM ou des fichiers ici",
        }),
        "pt" => json!({
            "library": "Biblioteca",
            "all_games": "Todos os jogos",
            "import": "Importar",
            "settings": "Configurações",
            "big_box": "Big Box",
            "list_view": "Vista em lista",
            "grid_view": "Vista em grade",
            "drop_import": "Solte pastas ROM ou arquivos aqui",
        }),
        _ => json!({
            "library": "Library",
            "all_games": "All games",
            "import": "Import",
            "settings": "Settings",
            "big_box": "Big Box",
            "list_view": "List view",
            "grid_view": "Grid view",
            "drop_import": "Drop ROM folders or game files here to import",
        }),
    }
}

pub fn media_packs() -> Vec<Value> {
    vec![
        json!({
            "id": "clear-logos-default",
            "name": "Platform Clear Logos",
            "description": "High-contrast platform logos for sidebar and Big Box hybrid views.",
            "kinds": ["platform_logo"],
        }),
        json!({
            "id": "controller-xbox",
            "name": "Xbox Controller Prompts",
            "description": "Xbox-style button prompts for Big Box status hints.",
            "kinds": ["controller_art"],
            "mapping_hint": "A Play · B Back · X Favorite · Y Menu",
        }),
        json!({
            "id": "controller-playstation",
            "name": "PlayStation Controller Prompts",
            "description": "PlayStation-style button prompts for Big Box status hints.",
            "kinds": ["controller_art"],
            "mapping_hint": "Cross Play · Circle Back · Square Favorite · Triangle Menu",
        }),
        json!({
            "id": "badges-core",
            "name": "Core Status Badges",
            "description": "Favorite, save, document, and progress badge styling pack.",
            "kinds": ["badges"],
        }),
    ]
}

pub fn list_media_packs(settings: &Value) -> Vec<Value> {
    let active: HashSet<String> = settings
        .get("active_media_packs")
        .and_then(Value::as_array)
        .map(|a| {
            a.iter()
                .filter_map(Value::as_str)
                .map(str::to_owned)
                .collect()
        })
        .unwrap_or_default();
    media_packs()
        .into_iter()
        .map(|mut pack| {
            let is_active = pack
                .get("id")
                .and_then(Value::as_str)
                .is_some_and(|id| active.contains(id));
            if let Some(m) = pack.as_object_mut() {
                m.insert("active".into(), Value::Bool(is_active));
            }
            pack
        })
        .collect()
}

pub fn apply_media_pack(state: &mut Value, pack_id: &str) -> Result<Value, String> {
    let Some(pack) = media_packs()
        .into_iter()
        .find(|p| p.get("id").and_then(Value::as_str) == Some(pack_id))
    else {
        return Err("Unknown media pack.".to_owned());
    };
    if let Some(root) = state.as_object_mut() {
        let settings = root
            .entry("settings".to_owned())
            .or_insert_with(|| Value::Object(Map::new()));
        if !settings.is_object() {
            *settings = Value::Object(Map::new());
        }
        if let Some(sm) = settings.as_object_mut() {
            let active = sm
                .entry("active_media_packs".to_owned())
                .or_insert_with(|| Value::Array(Vec::new()));
            if let Some(arr) = active.as_array_mut()
                && !arr.iter().any(|v| v.as_str() == Some(pack_id))
            {
                arr.push(Value::String(pack_id.to_owned()));
            }
            if pack
                .get("kinds")
                .and_then(Value::as_array)
                .is_some_and(|k| k.iter().any(|x| x.as_str() == Some("controller_art")))
            {
                sm.insert(
                    "controller_prompt_pack".into(),
                    Value::String(pack_id.to_owned()),
                );
                sm.insert(
                    "controller_prompt_hint".into(),
                    Value::String(
                        pack.get("mapping_hint")
                            .and_then(Value::as_str)
                            .unwrap_or("")
                            .to_owned(),
                    ),
                );
            }
        }
    }
    Ok(pack)
}

pub fn platform_categories(settings: &Value) -> BTreeMap<String, String> {
    let mut merged = default_platform_categories();
    if let Some(mapping) = settings
        .get("platform_categories")
        .and_then(Value::as_object)
    {
        for (k, v) in mapping {
            let ks = k.trim();
            let vs = v.as_str().map(str::trim).unwrap_or("");
            if !ks.is_empty() && !vs.is_empty() {
                merged.insert(ks.to_owned(), vs.to_owned());
            }
        }
    }
    merged
}

pub fn category_for_platform(platform: &str, settings: &Value) -> String {
    let key = if platform.is_empty() {
        "Unspecified"
    } else {
        platform
    };
    platform_categories(settings)
        .get(key)
        .cloned()
        .unwrap_or_else(|| "Other".to_owned())
}

pub fn bulk_wizard_changes(step_fields: &Value) -> Result<Map<String, Value>, String> {
    let Some(obj) = step_fields.as_object() else {
        return Err("Bulk wizard payload must be an object.".to_owned());
    };
    for key in obj.keys() {
        if !BULK_WIZARD_FIELDS.contains(&key.as_str()) {
            return Err("Bulk wizard contains unsupported fields.".to_owned());
        }
    }
    Ok(obj.clone())
}

pub fn region_score(name: &str) -> i64 {
    let lowered = name.to_lowercase();
    for (token, score) in [
        ("world", 0),
        ("usa", 1),
        ("us", 1),
        ("english", 1),
        ("europe", 2),
        ("eur", 2),
        ("japan", 3),
        ("jpn", 3),
        ("asia", 4),
    ] {
        if lowered.contains(token) {
            return score;
        }
    }
    5
}

pub fn rom_quality_score(path: &Path) -> i64 {
    let name = path
        .file_name()
        .map(|s| s.to_string_lossy().into_owned())
        .unwrap_or_default();
    let mut score = 0i64;
    if BAD_TAGS.is_match(&name) {
        score += 100;
    }
    score += region_score(&name);
    let ext = path
        .extension()
        .map(|e| format!(".{}", e.to_string_lossy().to_lowercase()))
        .unwrap_or_default();
    if matches!(ext.as_str(), ".chd" | ".cue" | ".m3u") {
        score -= 2;
    } else if matches!(ext.as_str(), ".zip" | ".7z") {
        score += 1;
    }
    match std::fs::metadata(path) {
        Ok(m) => {
            let bonus = (m.len() / (64 * 1024 * 1024)).min(5) as i64;
            score -= bonus;
        }
        Err(_) => score += 1000,
    }
    score
}

pub fn pick_best_rom(paths: &[String]) -> String {
    let mut existing: Vec<&Path> = paths
        .iter()
        .map(Path::new)
        .filter(|p| p.is_file())
        .collect();
    existing.sort_by(|a, b| {
        rom_quality_score(a)
            .cmp(&rom_quality_score(b))
            .then_with(|| {
                let na = a
                    .file_name()
                    .map(|s| s.to_string_lossy().to_lowercase())
                    .unwrap_or_default();
                let nb = b
                    .file_name()
                    .map(|s| s.to_string_lossy().to_lowercase())
                    .unwrap_or_default();
                na.cmp(&nb)
            })
    });
    existing
        .first()
        .map(|p| p.to_string_lossy().into_owned())
        .unwrap_or_default()
}

pub fn archive_rom_bytes(path: &Path) -> Result<Vec<u8>, String> {
    let ext = path
        .extension()
        .map(|e| format!(".{}", e.to_string_lossy().to_lowercase()))
        .unwrap_or_default();
    if ext == ".zip" {
        let f = std::fs::File::open(path).map_err(|e| e.to_string())?;
        let mut archive = zip::ZipArchive::new(f).map_err(|e| e.to_string())?;
        let mut best: Option<(usize, u64)> = None;
        for i in 0..archive.len() {
            if let Ok(ent) = archive.by_index(i)
                && !ent.is_dir()
                && best.is_none_or(|(_, sz)| ent.size() > sz)
            {
                best = Some((i, ent.size()));
            }
        }
        let Some((idx, size)) = best else {
            return Err("Archive contains no ROM files.".to_owned());
        };
        if size as usize > MAX_ROM_ARCHIVE_BYTES {
            return Err("ROM archive member is too large.".to_owned());
        }
        let mut ent = archive.by_index(idx).map_err(|e| e.to_string())?;
        let mut buf = Vec::with_capacity(size as usize);
        ent.read_to_end(&mut buf).map_err(|e| e.to_string())?;
        return Ok(buf);
    }
    let meta = std::fs::metadata(path)
        .map_err(|_| format!("Could not stat ROM file: {}", path.display()))?;
    if meta.len() as usize > MAX_ROM_ARCHIVE_BYTES {
        return Err("ROM file is too large.".to_owned());
    }
    let data = std::fs::read(path).map_err(|e| e.to_string())?;
    if data.len() > MAX_ROM_ARCHIVE_BYTES {
        return Err("ROM file is too large.".to_owned());
    }
    Ok(data)
}

pub fn download_steam_trailer(game: &mut Value, media_root: &Path) -> Result<String, String> {
    let app_id = game
        .get("steam_app_id")
        .and_then(|v| {
            v.as_str()
                .map(str::to_owned)
                .or_else(|| v.as_i64().map(|n| n.to_string()))
        })
        .unwrap_or_default();
    if app_id.is_empty() || !app_id.bytes().all(|b| b.is_ascii_digit()) {
        return Err("Steam App ID required for trailer download.".to_owned());
    }
    let url = format!("https://store.steampowered.com/api/appdetails?appids={app_id}");
    let resp = http_get_raw(
        &url,
        &[("User-Agent", "OpenBox/1".to_owned())],
        20,
        4 * 1024 * 1024,
    )?;
    if resp.status >= 400 {
        return Err("Steam did not return app details.".to_owned());
    }
    let payload: Value = serde_json::from_slice(&resp.body)
        .map_err(|_| "Steam did not return app details.".to_owned())?;
    let record = payload.get(&app_id).cloned().unwrap_or(Value::Null);
    if !record
        .get("success")
        .and_then(Value::as_bool)
        .unwrap_or(false)
    {
        return Err("Steam did not return app details.".to_owned());
    }
    let movies = record
        .get("data")
        .and_then(|d| d.get("movies"))
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let Some(movie) = movies.first() else {
        return Err("Steam returned no trailers for this game.".to_owned());
    };
    let mp4 = movie
        .get("mp4")
        .and_then(Value::as_object)
        .and_then(|m| {
            m.values()
                .find_map(|v| v.as_str().filter(|s| !s.is_empty()))
        })
        .or_else(|| {
            movie
                .get("webm")
                .and_then(|w| w.get("max"))
                .and_then(Value::as_str)
                .filter(|s| !s.is_empty())
        })
        .unwrap_or("");
    if mp4.is_empty() {
        return Err("Steam trailer URL was missing.".to_owned());
    }
    let dest = media_root.join("steam").join(&app_id).join("trailer.mp4");
    let opts = DownloadOptions {
        max_bytes: 512 * 1024 * 1024,
        timeout_secs: 20,
        ..Default::default()
    };
    let saved = block_on_io(download_file(mp4, &dest, &opts)).map_err(|e| e.to_string())?;
    let saved_str = saved.to_string_lossy().into_owned();
    if let Some(m) = game.as_object_mut() {
        m.insert("video_trailer".into(), Value::String(saved_str.clone()));
    }
    Ok(saved_str)
}

pub fn download_gog_media(game: &mut Value, media_root: &Path) -> Result<Value, String> {
    let app_id = game
        .get("heroic_app_id")
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_owned();
    if app_id.is_empty() {
        return Err("Heroic/GOG app id required.".to_owned());
    }
    let slug = app_id.replace("gog_", "");
    let url = format!(
        "https://embed.gog.com/games/ajax/filtered?mediaType=game&search={}",
        quote_path(&slug)
    );
    let resp = http_get_raw(
        &url,
        &[("User-Agent", "OpenBox/1".to_owned())],
        20,
        4 * 1024 * 1024,
    )?;
    if resp.status >= 400 {
        return Err("GOG metadata lookup returned no product.".to_owned());
    }
    let payload: Value = serde_json::from_slice(&resp.body)
        .map_err(|_| "GOG metadata lookup returned no product.".to_owned())?;
    let products = payload
        .get("products")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let Some(product) = products.iter().find(|it| {
        let id_s = it
            .get("id")
            .and_then(|v| {
                v.as_str()
                    .map(str::to_owned)
                    .or_else(|| v.as_i64().map(|n| n.to_string()))
            })
            .unwrap_or_default();
        let slug_s = it.get("slug").and_then(Value::as_str).unwrap_or("");
        id_s == slug || slug_s.contains(&slug)
    }) else {
        return Err("GOG metadata lookup returned no product.".to_owned());
    };
    let root = media_root.join("gog").join(&slug);
    let opts = DownloadOptions {
        max_bytes: 512 * 1024 * 1024,
        timeout_secs: 20,
        ..Default::default()
    };
    if let Some(img) = product
        .get("img")
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
    {
        let url_full = if img.starts_with("//") {
            format!("https:{img}")
        } else {
            img.to_owned()
        };
        if let Ok(saved) = block_on_io(download_file(&url_full, &root.join("cover.jpg"), &opts))
            && let Some(m) = game.as_object_mut()
        {
            m.insert(
                "cover".into(),
                Value::String(saved.to_string_lossy().into_owned()),
            );
        }
    }
    if let Some(bg) = product
        .get("background")
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
    {
        let url_full = if bg.starts_with("//") {
            format!("https:{bg}")
        } else {
            bg.to_owned()
        };
        if let Ok(saved) = block_on_io(download_file(
            &url_full,
            &root.join("background.jpg"),
            &opts,
        )) && let Some(m) = game.as_object_mut()
        {
            m.insert(
                "background".into(),
                Value::String(saved.to_string_lossy().into_owned()),
            );
        }
    }
    Ok(game.clone())
}
