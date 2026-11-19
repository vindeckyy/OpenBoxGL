//! MAME and FinalBurn Neo full-set DAT/XML importer (`arcade.py`).

use std::collections::{HashMap, HashSet};
use std::fs::{self, File};
use std::path::{Path, PathBuf};
use std::process::Command;

use openbox_core::ApiError;
use openbox_platform::{expand_user, join_command, split_command, which};
use quick_xml::Reader;
use quick_xml::events::Event;
use serde_json::{Value, json};

#[derive(Debug, Clone)]
pub struct ArcadeRecord {
    pub name: String,
    pub description: String,
    pub cloneof: String,
    pub players: String,
    pub vertical: bool,
    pub requires_chd: bool,
    pub controls: HashMap<String, String>,
    pub merged_roms: HashSet<String>,
}

fn local_tag(raw: &str) -> &str {
    raw.rsplit(['}', ':']).next().unwrap_or(raw)
}

fn attr_map(e: &quick_xml::events::BytesStart<'_>) -> HashMap<String, String> {
    let mut map = HashMap::new();
    for attr in e.attributes().flatten() {
        let k = attr.key.as_ref().to_owned();
        let v = attr.value.as_ref().to_owned();
        map.insert(k, v);
    }
    map
}

pub fn parse_catalog_bytes(data: &[u8]) -> Result<HashMap<String, ArcadeRecord>, ApiError> {
    let text = String::from_utf8_lossy(data);
    let mut reader = Reader::from_str(&text);
    let mut buf = Vec::new();
    let mut catalog = HashMap::new();

    let mut cur: Option<ArcadeRecord> = None;
    let mut skip_machine = false;
    let mut in_desc = false;
    let mut desc_buf = String::new();

    loop {
        match reader.read_event_into(&mut buf) {
            Ok(Event::Start(e)) => {
                let tag = local_tag(e.name().as_ref()).to_owned();
                if matches!(tag.as_str(), "machine" | "game") {
                    let attrs = attr_map(&e);
                    let name = attrs.get("name").map(|s| s.trim()).unwrap_or("").to_owned();
                    let runnable = attrs.get("runnable").map(String::as_str) != Some("no");
                    let isbios = attrs.get("isbios").map(String::as_str) == Some("yes");
                    let isdevice = attrs.get("isdevice").map(String::as_str) == Some("yes");
                    if !name.is_empty() && runnable && !isbios && !isdevice {
                        skip_machine = false;
                        cur = Some(ArcadeRecord {
                            description: name.clone(),
                            name,
                            cloneof: attrs
                                .get("cloneof")
                                .map(|s| s.trim().to_owned())
                                .unwrap_or_default(),
                            players: attrs
                                .get("players")
                                .map(|s| s.trim().to_owned())
                                .unwrap_or_default(),
                            vertical: false,
                            requires_chd: false,
                            controls: HashMap::new(),
                            merged_roms: HashSet::new(),
                        });
                    } else {
                        skip_machine = true;
                        cur = None;
                    }
                } else if !skip_machine && let Some(ref mut rec) = cur {
                    match tag.as_str() {
                        "description" => {
                            in_desc = true;
                            desc_buf.clear();
                        }
                        "display" => {
                            let attrs = attr_map(&e);
                            let rot = attrs.get("rotate").map(|s| s.trim()).unwrap_or("0");
                            let orient = attrs
                                .get("orientation")
                                .map(|s| s.trim().to_lowercase())
                                .unwrap_or_default();
                            if (!rot.is_empty() && rot != "0") || orient == "vertical" {
                                rec.vertical = true;
                            }
                        }
                        "input" => {
                            let attrs = attr_map(&e);
                            if rec.players.is_empty()
                                && let Some(p) = attrs.get("players")
                            {
                                rec.players = p.trim().to_owned();
                            }
                            for k in [
                                "trackball",
                                "lightgun",
                                "paddle",
                                "spinner",
                                "keyboard",
                                "mouse",
                            ] {
                                if let Some(v) = attrs.get(k) {
                                    rec.controls.insert(k.to_owned(), v.clone());
                                }
                            }
                        }
                        "disk" => rec.requires_chd = true,
                        "rom" => {
                            let attrs = attr_map(&e);
                            if attrs.get("merge").is_some_and(|s| !s.is_empty())
                                && let Some(rn) = attrs.get("name")
                            {
                                rec.merged_roms.insert(rn.clone());
                            }
                        }
                        _ => {}
                    }
                }
            }
            Ok(Event::Empty(e)) => {
                if !skip_machine && let Some(ref mut rec) = cur {
                    let qname = e.name();
                    let tag = local_tag(qname.as_ref());
                    match tag {
                        "display" => {
                            let attrs = attr_map(&e);
                            let rot = attrs.get("rotate").map(|s| s.trim()).unwrap_or("0");
                            let orient = attrs
                                .get("orientation")
                                .map(|s| s.trim().to_lowercase())
                                .unwrap_or_default();
                            if (!rot.is_empty() && rot != "0") || orient == "vertical" {
                                rec.vertical = true;
                            }
                        }
                        "input" => {
                            let attrs = attr_map(&e);
                            if rec.players.is_empty()
                                && let Some(p) = attrs.get("players")
                            {
                                rec.players = p.trim().to_owned();
                            }
                            for k in [
                                "trackball",
                                "lightgun",
                                "paddle",
                                "spinner",
                                "keyboard",
                                "mouse",
                            ] {
                                if let Some(v) = attrs.get(k) {
                                    rec.controls.insert(k.to_owned(), v.clone());
                                }
                            }
                        }
                        "disk" => rec.requires_chd = true,
                        "rom" => {
                            let attrs = attr_map(&e);
                            if attrs.get("merge").is_some_and(|s| !s.is_empty())
                                && let Some(rn) = attrs.get("name")
                            {
                                rec.merged_roms.insert(rn.clone());
                            }
                        }
                        _ => {}
                    }
                }
            }
            Ok(Event::Text(t)) if in_desc => {
                let s = quick_xml::escape::unescape(t.as_ref())
                    .unwrap_or_else(|_| std::borrow::Cow::Borrowed(t.as_ref()));
                desc_buf.push_str(&s);
            }
            Ok(Event::End(e)) => {
                let qname = e.name();
                let tag = local_tag(qname.as_ref());
                if tag == "description" && in_desc {
                    in_desc = false;
                    if let Some(ref mut rec) = cur {
                        let d = desc_buf.trim();
                        if !d.is_empty() {
                            rec.description = d.to_owned();
                        }
                    }
                } else if matches!(tag, "machine" | "game") {
                    if let Some(rec) = cur.take() {
                        catalog.insert(rec.name.to_lowercase(), rec);
                    }
                    skip_machine = false;
                }
            }
            Ok(Event::Eof) => break,
            Err(err) => {
                return Err(ApiError::bad_request(format!(
                    "Could not parse arcade DAT/XML: {err}"
                )));
            }
            _ => {}
        }
        buf.clear();
    }
    Ok(catalog)
}

pub fn load_catalog(
    dat_path: &str,
    source: &str,
) -> Result<HashMap<String, ArcadeRecord>, ApiError> {
    if !dat_path.trim().is_empty() {
        let p = expand_user(Path::new(dat_path.trim()));
        if !p.is_file() {
            return Err(ApiError::not_found(
                "The arcade DAT/XML file does not exist.",
            ));
        }
        let data = fs::read(&p).map_err(|e| ApiError::bad_request(e.to_string()))?;
        return parse_catalog_bytes(&data);
    }
    if source != "MAME" {
        return Err(ApiError::not_found(
            "Choose a DAT/XML file, or install MAME so OpenBox can run mame -listxml.",
        ));
    }
    let Some(binary) = which("mame") else {
        return Err(ApiError::not_found(
            "Choose a DAT/XML file, or install MAME so OpenBox can run mame -listxml.",
        ));
    };
    let out = Command::new(binary)
        .arg("-listxml")
        .output()
        .map_err(|_| ApiError::bad_request("MAME could not produce its XML catalog."))?;
    if !out.status.success() {
        return Err(ApiError::bad_request(
            "MAME could not produce its XML catalog.",
        ));
    }
    if out.stdout.len() > 256 * 1024 * 1024 {
        return Err(ApiError::bad_request("MAME catalog is too large."));
    }
    parse_catalog_bytes(&out.stdout)
}

fn zip_members(path: &Path) -> HashSet<String> {
    if !path
        .extension()
        .and_then(|e| e.to_str())
        .is_some_and(|e| e.eq_ignore_ascii_case("zip"))
    {
        return HashSet::new();
    }
    let Ok(f) = File::open(path) else {
        return HashSet::new();
    };
    let Ok(archive) = zip::ZipArchive::new(f) else {
        return HashSet::new();
    };
    archive
        .file_names()
        .filter_map(|res| res.ok())
        .filter_map(|name| name.rsplit('/').next().map(ToOwned::to_owned))
        .collect()
}

pub fn import_arcade(
    folder: &str,
    dat_path: &str,
    command: &str,
    source: &str,
    players: &str,
    skip_chd: bool,
    skip_inputs: &[String],
) -> Result<Vec<Value>, ApiError> {
    let root = expand_user(Path::new(folder));
    if !root.is_dir() {
        return Err(ApiError::not_found("The arcade ROM folder does not exist."));
    }
    let source = if source == "FinalBurn Neo" {
        "FinalBurn Neo"
    } else {
        "MAME"
    };
    let players = players.trim().to_lowercase();
    let players = if players.is_empty() {
        "any".to_owned()
    } else {
        players
    };
    if !matches!(players.as_str(), "any" | "2" | "4") {
        return Err(ApiError::bad_request("Player filter must be any, 2, or 4."));
    }
    let allowed: HashSet<&str> = [
        "trackball",
        "lightgun",
        "paddle",
        "spinner",
        "keyboard",
        "mouse",
    ]
    .into_iter()
    .collect();
    let skip_set: HashSet<String> = skip_inputs
        .iter()
        .map(|s| s.trim().to_lowercase())
        .filter(|s| !s.is_empty())
        .collect();
    for s in &skip_set {
        if !allowed.contains(s.as_str()) {
            return Err(ApiError::bad_request(format!("Unknown input filter: {s}")));
        }
    }

    let mut archives: HashMap<String, PathBuf> = HashMap::new();
    if let Ok(rd) = fs::read_dir(&root) {
        for entry in rd.filter_map(Result::ok) {
            let p = entry.path();
            if p.is_file()
                && let Some(ext) = p.extension().and_then(|e| e.to_str())
                && (ext.eq_ignore_ascii_case("zip") || ext.eq_ignore_ascii_case("7z"))
                && let Some(stem) = p.file_stem().and_then(|s| s.to_str())
            {
                archives.insert(stem.to_lowercase(), p);
            }
        }
    }

    let catalog = load_catalog(dat_path, source)?;
    let cmd = if !command.trim().is_empty() {
        command.to_owned()
    } else {
        let bin_name = if source == "MAME" { "mame" } else { "fbneo" };
        let Some(binary) = which(bin_name) else {
            return Err(ApiError::not_found(format!(
                "{source} is not installed. Enter a launch command for the configured emulator."
            )));
        };
        let bstr = binary.to_string_lossy().into_owned();
        if source == "MAME" {
            join_command(&[
                bstr,
                "-rompath".into(),
                root.to_string_lossy().into_owned(),
                "{rom_name}".into(),
            ])
        } else {
            join_command(&[bstr, "{path}".into()])
        }
    };
    if split_command(&cmd).map(|v| v.is_empty()).unwrap_or(true) {
        return Err(ApiError::bad_request("The arcade launch command is empty."));
    }

    let mut games = Vec::new();
    for (key, rec) in &catalog {
        let own = archives.get(key);
        let parent = if !rec.cloneof.is_empty() {
            archives.get(&rec.cloneof.to_lowercase())
        } else {
            None
        };
        let Some(archive) = own.or(parent) else {
            continue;
        };
        if players != "any" {
            let supported: i64 = rec.players.parse().unwrap_or(0);
            let min_p: i64 = players.parse().unwrap_or(0);
            if supported < min_p {
                continue;
            }
        }
        if skip_chd && rec.requires_chd {
            continue;
        }
        if !skip_set.is_empty()
            && skip_set
                .iter()
                .any(|k| rec.controls.get(k).is_some_and(|v| !v.is_empty()))
        {
            continue;
        }
        let set_type = if !rec.cloneof.is_empty() && own.is_none() {
            "merged"
        } else if !rec.cloneof.is_empty()
            && own.is_some_and(|p| !rec.merged_roms.is_subset(&zip_members(p)))
        {
            "split"
        } else if !rec.cloneof.is_empty() {
            "non-merged"
        } else {
            "parent"
        };
        games.push(json!({
            "name": rec.description,
            "platform": "Arcade",
            "source": source,
            "collection": source,
            "path": archive.to_string_lossy(),
            "launch": cmd,
            "rom_name": rec.name,
            "clone_of": rec.cloneof,
            "set_type": set_type,
            "players": rec.players,
            "vertical": rec.vertical,
            "requires_chd": rec.requires_chd,
        }));
    }
    Ok(games)
}
