//! Lossless binary Steam `shortcuts.vdf` parser, writer, and safe preview/apply/remove
//! bridge (`pkg/parity/parity_steam_bridge.py`).

use std::collections::{HashMap, HashSet};
use std::fs;
use std::path::{Path, PathBuf};

use crc32fast::Hasher as Crc32Hasher;
use openbox_io::{atomic_write_bytes, fsync_directory};
use openbox_platform::{expand_user, home_dir};
use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};

pub const TYPE_RECORD: u8 = 0x00;
pub const TYPE_STRING: u8 = 0x01;
pub const TYPE_INT32: u8 = 0x02;
pub const TYPE_END: u8 = 0x08;

pub const ROOT_KEY: &str = "shortcuts";
pub const OPENBOX_APPID_MASK: u32 = 0x8000_0000;

pub const MAX_SHORTCUTS_BYTES: usize = 16 * 1024 * 1024;
pub const MAX_STRING_BYTES: usize = 1024 * 1024;
pub const MAX_RECORDS: usize = 100_000;
pub const MAX_DEPTH: usize = 32;

#[derive(Debug, Clone, thiserror::Error)]
pub enum SteamBridgeError {
    #[error("{0}")]
    Corrupt(String),
    #[error("{0}")]
    TooLarge(String),
    #[error("{0}")]
    Stale(String),
    #[error("{0}")]
    Invalid(String),
    #[error("{0}")]
    Io(String),
}

impl SteamBridgeError {
    pub fn is_stale(&self) -> bool {
        matches!(self, Self::Stale(_))
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum VdfValue {
    Record(Vec<VdfRecord>),
    Int32(u32),
    String { text: String, raw: Vec<u8> },
}

#[derive(Debug, Clone)]
pub struct VdfRecord {
    pub key: String,
    pub raw_key: Vec<u8>,
    pub value: VdfValue,
}

impl PartialEq for VdfRecord {
    fn eq(&self, other: &Self) -> bool {
        self.key == other.key && self.value == other.value
    }
}

impl Eq for VdfRecord {}

impl VdfRecord {
    pub fn record(key: impl Into<String>, children: Vec<VdfRecord>) -> Self {
        let k = key.into();
        let raw_key = k.as_bytes().to_vec();
        Self {
            key: k,
            raw_key,
            value: VdfValue::Record(children),
        }
    }

    pub fn int32(key: impl Into<String>, value: u32) -> Self {
        let k = key.into();
        let raw_key = k.as_bytes().to_vec();
        Self {
            key: k,
            raw_key,
            value: VdfValue::Int32(value),
        }
    }

    pub fn string(key: impl Into<String>, value: impl Into<String>) -> Self {
        let k = key.into();
        let raw_key = k.as_bytes().to_vec();
        let s = value.into();
        let raw = s.as_bytes().to_vec();
        Self {
            key: k,
            raw_key,
            value: VdfValue::String { text: s, raw },
        }
    }

    pub fn tag(&self) -> u8 {
        match self.value {
            VdfValue::Record(_) => TYPE_RECORD,
            VdfValue::Int32(_) => TYPE_INT32,
            VdfValue::String { .. } => TYPE_STRING,
        }
    }

    pub fn children(&self) -> &[VdfRecord] {
        match &self.value {
            VdfValue::Record(c) => c,
            _ => &[],
        }
    }

    pub fn with_key(&self, new_key: impl Into<String>) -> Self {
        let mut cloned = self.clone();
        let k = new_key.into();
        cloned.raw_key = k.as_bytes().to_vec();
        cloned.key = k;
        cloned
    }
}

#[derive(Debug, Clone)]
pub struct ShortcutsVdf {
    pub records: Vec<VdfRecord>,
    pub root_key: String,
    pub raw_root_key: Vec<u8>,
    pub source_empty: bool,
}

impl Default for ShortcutsVdf {
    fn default() -> Self {
        Self {
            records: Vec::new(),
            root_key: ROOT_KEY.to_owned(),
            raw_root_key: ROOT_KEY.as_bytes().to_vec(),
            source_empty: false,
        }
    }
}

fn decode_lossy(raw: &[u8]) -> String {
    String::from_utf8_lossy(raw).into_owned()
}

struct Parser<'a> {
    data: &'a [u8],
    pos: usize,
    record_count: usize,
}

impl<'a> Parser<'a> {
    fn new(data: &'a [u8]) -> Self {
        Self {
            data,
            pos: 0,
            record_count: 0,
        }
    }

    fn require(&self, len: usize) -> Result<(), SteamBridgeError> {
        if self
            .pos
            .checked_add(len)
            .is_none_or(|e| e > self.data.len())
        {
            return Err(SteamBridgeError::Corrupt(
                "Truncated shortcuts.vdf record.".to_owned(),
            ));
        }
        Ok(())
    }

    fn byte(&mut self) -> Result<u8, SteamBridgeError> {
        self.require(1)?;
        let b = self.data[self.pos];
        self.pos += 1;
        Ok(b)
    }

    fn cstring(&mut self, what: &str) -> Result<(String, Vec<u8>), SteamBridgeError> {
        let start = self.pos;
        let rel_end = self.data[start..]
            .iter()
            .position(|b| *b == 0)
            .ok_or_else(|| SteamBridgeError::Corrupt(format!("Unterminated VDF {what}.")))?;
        if rel_end > MAX_STRING_BYTES {
            return Err(SteamBridgeError::TooLarge(format!(
                "VDF {what} is too large."
            )));
        }
        let raw = self.data[start..start + rel_end].to_vec();
        self.pos = start + rel_end + 1;
        if what == "key" && raw.is_empty() {
            return Err(SteamBridgeError::Corrupt(
                "VDF keys cannot be empty.".to_owned(),
            ));
        }
        Ok((decode_lossy(&raw), raw))
    }

    fn count_record(&mut self) -> Result<(), SteamBridgeError> {
        self.record_count += 1;
        if self.record_count > MAX_RECORDS {
            return Err(SteamBridgeError::TooLarge(
                "shortcuts.vdf contains too many records.".to_owned(),
            ));
        }
        Ok(())
    }

    fn object_children(&mut self, depth: usize) -> Result<Vec<VdfRecord>, SteamBridgeError> {
        if depth > MAX_DEPTH {
            return Err(SteamBridgeError::TooLarge(
                "shortcuts.vdf nesting is too deep.".to_owned(),
            ));
        }
        let mut children = Vec::new();
        loop {
            let tag = self.byte()?;
            if tag == TYPE_END {
                return Ok(children);
            }
            if tag != TYPE_RECORD && tag != TYPE_INT32 && tag != TYPE_STRING {
                return Err(SteamBridgeError::Corrupt(format!(
                    "Unknown VDF record tag 0x{tag:02x}."
                )));
            }
            self.count_record()?;
            let (key, raw_key) = self.cstring("key")?;
            let value = match tag {
                TYPE_RECORD => VdfValue::Record(self.object_children(depth + 1)?),
                TYPE_INT32 => {
                    self.require(4)?;
                    let bytes: [u8; 4] = self.data[self.pos..self.pos + 4].try_into().unwrap();
                    self.pos += 4;
                    VdfValue::Int32(u32::from_le_bytes(bytes))
                }
                TYPE_STRING => {
                    let (text, raw) = self.cstring("string")?;
                    VdfValue::String { text, raw }
                }
                _ => unreachable!(),
            };
            children.push(VdfRecord {
                key,
                raw_key,
                value,
            });
        }
    }
}

pub fn parse_shortcuts_bytes(
    data: &[u8],
    max_bytes: usize,
) -> Result<ShortcutsVdf, SteamBridgeError> {
    if data.len() > max_bytes {
        return Err(SteamBridgeError::TooLarge(
            "shortcuts.vdf is too large.".to_owned(),
        ));
    }
    if data.is_empty() {
        return Ok(ShortcutsVdf {
            source_empty: true,
            ..ShortcutsVdf::default()
        });
    }
    let mut parser = Parser::new(data);
    let tag = parser.byte()?;
    if tag != TYPE_RECORD {
        return Err(SteamBridgeError::Corrupt(
            "shortcuts.vdf must begin with a record tag.".to_owned(),
        ));
    }
    let (root_key, raw_root_key) = parser.cstring("root key")?;
    if !root_key.eq_ignore_ascii_case(ROOT_KEY) {
        return Err(SteamBridgeError::Corrupt(
            "shortcuts.vdf has an unexpected root key.".to_owned(),
        ));
    }
    let records = parser.object_children(0)?;
    if parser.pos != data.len() {
        return Err(SteamBridgeError::Corrupt(
            "Trailing bytes after the shortcuts.vdf root.".to_owned(),
        ));
    }
    Ok(ShortcutsVdf {
        records,
        root_key,
        raw_root_key,
        source_empty: false,
    })
}

pub fn parse_shortcuts_file(path: &Path) -> Result<ShortcutsVdf, SteamBridgeError> {
    let meta = fs::metadata(path).map_err(|e| SteamBridgeError::Io(e.to_string()))?;
    if meta.len() as usize > MAX_SHORTCUTS_BYTES {
        return Err(SteamBridgeError::TooLarge(
            "shortcuts.vdf is too large.".to_owned(),
        ));
    }
    let data = fs::read(path).map_err(|e| SteamBridgeError::Io(e.to_string()))?;
    parse_shortcuts_bytes(&data, MAX_SHORTCUTS_BYTES)
}

fn push_checked(out: &mut Vec<u8>, slice: &[u8], max_bytes: usize) -> Result<(), SteamBridgeError> {
    if out.len().saturating_add(slice.len()) > max_bytes {
        return Err(SteamBridgeError::TooLarge(
            "Encoded shortcuts.vdf is too large.".to_owned(),
        ));
    }
    out.extend_from_slice(slice);
    Ok(())
}

fn encode_record(
    record: &VdfRecord,
    out: &mut Vec<u8>,
    max_bytes: usize,
    depth: usize,
    count: &mut usize,
) -> Result<(), SteamBridgeError> {
    if depth > MAX_DEPTH {
        return Err(SteamBridgeError::TooLarge(
            "Encoded shortcuts.vdf nesting is too deep.".to_owned(),
        ));
    }
    *count += 1;
    if *count > MAX_RECORDS {
        return Err(SteamBridgeError::TooLarge(
            "Encoded shortcuts.vdf contains too many records.".to_owned(),
        ));
    }
    if record.raw_key.is_empty() {
        return Err(SteamBridgeError::Invalid(
            "VDF keys cannot be empty.".to_owned(),
        ));
    }
    if record.raw_key.contains(&0) {
        return Err(SteamBridgeError::Invalid(
            "VDF key cannot contain NUL.".to_owned(),
        ));
    }
    push_checked(out, &[record.tag()], max_bytes)?;
    push_checked(out, &record.raw_key, max_bytes)?;
    push_checked(out, &[0], max_bytes)?;
    match &record.value {
        VdfValue::Record(children) => {
            for child in children {
                encode_record(child, out, max_bytes, depth + 1, count)?;
            }
            push_checked(out, &[TYPE_END], max_bytes)?;
        }
        VdfValue::Int32(n) => {
            push_checked(out, &n.to_le_bytes(), max_bytes)?;
        }
        VdfValue::String { raw, .. } => {
            if raw.contains(&0) {
                return Err(SteamBridgeError::Invalid(
                    "VDF string cannot contain NUL.".to_owned(),
                ));
            }
            if raw.len() > MAX_STRING_BYTES {
                return Err(SteamBridgeError::TooLarge(
                    "VDF string is too large.".to_owned(),
                ));
            }
            push_checked(out, raw, max_bytes)?;
            push_checked(out, &[0], max_bytes)?;
        }
    }
    Ok(())
}

pub fn encode_shortcuts(
    doc: &ShortcutsVdf,
    max_bytes: usize,
    preserve_empty: bool,
) -> Result<Vec<u8>, SteamBridgeError> {
    if preserve_empty && doc.source_empty && doc.records.is_empty() {
        return Ok(Vec::new());
    }
    let root = VdfRecord {
        key: doc.root_key.clone(),
        raw_key: doc.raw_root_key.clone(),
        value: VdfValue::Record(doc.records.clone()),
    };
    let mut out = Vec::new();
    let mut count = 0usize;
    encode_record(&root, &mut out, max_bytes, 0, &mut count)?;
    Ok(out)
}

pub fn write_shortcuts_bytes(path: &Path, data: &[u8]) -> Result<PathBuf, SteamBridgeError> {
    if data.len() > MAX_SHORTCUTS_BYTES {
        return Err(SteamBridgeError::TooLarge(
            "Encoded shortcuts.vdf is too large.".to_owned(),
        ));
    }
    parse_shortcuts_bytes(data, MAX_SHORTCUTS_BYTES)?;
    if let Ok(meta) = fs::symlink_metadata(path) {
        if meta.file_type().is_symlink() {
            return Err(SteamBridgeError::Invalid(
                "Refusing to replace a symlink shortcuts.vdf.".to_owned(),
            ));
        }
        if !meta.is_file() {
            return Err(SteamBridgeError::Invalid(
                "Refusing to replace a non-file shortcuts.vdf.".to_owned(),
            ));
        }
    }
    #[cfg(unix)]
    let mode = {
        use std::os::unix::fs::PermissionsExt;
        fs::metadata(path)
            .map(|m| m.permissions().mode() & 0o777)
            .unwrap_or(0o600)
    };
    #[cfg(not(unix))]
    let mode = 0o600;
    let written =
        atomic_write_bytes(path, data, mode).map_err(|e| SteamBridgeError::Io(e.to_string()))?;
    if let Some(parent) = path.parent() {
        fsync_directory(parent);
    }
    Ok(written)
}

pub fn appid_for(exe: &str, name: &str) -> Result<u32, SteamBridgeError> {
    if exe.as_bytes().contains(&0) || name.as_bytes().contains(&0) {
        return Err(SteamBridgeError::Invalid(
            "executable or shortcut name cannot contain NUL.".to_owned(),
        ));
    }
    let mut hasher = Crc32Hasher::new();
    hasher.update(exe.as_bytes());
    hasher.update(name.as_bytes());
    Ok(hasher.finalize() | OPENBOX_APPID_MASK)
}

fn coerce_u32(val: &Value) -> Result<u32, SteamBridgeError> {
    match val {
        Value::Bool(b) => Ok(u32::from(*b)),
        Value::Number(n) => {
            if let Some(u) = n.as_u64() {
                u32::try_from(u).map_err(|_| {
                    SteamBridgeError::Invalid(
                        "VDF int32 value is outside the unsigned 32-bit range.".to_owned(),
                    )
                })
            } else if let Some(i) = n.as_i64() {
                let adj = if i < 0 { i + (1i64 << 32) } else { i };
                u32::try_from(adj).map_err(|_| {
                    SteamBridgeError::Invalid(
                        "VDF int32 value is outside the unsigned 32-bit range.".to_owned(),
                    )
                })
            } else {
                Err(SteamBridgeError::Invalid(
                    "VDF int32 values must be integers.".to_owned(),
                ))
            }
        }
        Value::String(s) => {
            let i: i64 = s.trim().parse().map_err(|_| {
                SteamBridgeError::Invalid("VDF int32 values must be integers.".to_owned())
            })?;
            let adj = if i < 0 { i + (1i64 << 32) } else { i };
            u32::try_from(adj).map_err(|_| {
                SteamBridgeError::Invalid(
                    "VDF int32 value is outside the unsigned 32-bit range.".to_owned(),
                )
            })
        }
        _ => Err(SteamBridgeError::Invalid(
            "VDF int32 values must be integers.".to_owned(),
        )),
    }
}

fn field_ci<'a>(map: &'a Map<String, Value>, names: &[&str]) -> Option<&'a Value> {
    for name in names {
        if let Some(v) = map.get(*name) {
            return Some(v);
        }
    }
    for name in names {
        for (k, v) in map {
            if k.eq_ignore_ascii_case(name) {
                return Some(v);
            }
        }
    }
    None
}

fn str_or_empty(val: Option<&Value>) -> String {
    match val {
        Some(Value::String(s)) => s.clone(),
        Some(Value::Number(n)) => n.to_string(),
        _ => String::new(),
    }
}

pub fn shortcut_from_game(
    game: &Map<String, Value>,
    launcher_exe: Option<&str>,
) -> Result<VdfRecord, SteamBridgeError> {
    let name = str_or_empty(field_ci(game, &["AppName", "name"]));
    let exe = launcher_exe
        .filter(|s| !s.is_empty())
        .map(str::to_owned)
        .unwrap_or_else(|| {
            let e = str_or_empty(field_ci(game, &["Exe", "exe"]));
            if !e.is_empty() {
                e
            } else {
                str_or_empty(field_ci(game, &["path"]))
            }
        });
    if name.trim().is_empty() || exe.trim().is_empty() {
        return Err(SteamBridgeError::Invalid(
            "A bridge shortcut requires a name and executable.".to_owned(),
        ));
    }
    let game_id = str_or_empty(field_ci(game, &["game_id", "id"]));
    let mut options = str_or_empty(field_ci(game, &["LaunchOptions", "launch_options"]));
    if options.is_empty() && launcher_exe.is_some() && !game_id.is_empty() {
        options = format!("--play {game_id}");
    }
    let appid = appid_for(&exe, &name)?;
    let mut children = vec![
        VdfRecord::int32("appid", appid),
        VdfRecord::string("AppName", name),
        VdfRecord::string("Exe", exe),
        VdfRecord::string(
            "StartDir",
            str_or_empty(field_ci(game, &["StartDir", "start_dir"])),
        ),
        VdfRecord::string("icon", str_or_empty(field_ci(game, &["icon", "Icon"]))),
        VdfRecord::string(
            "ShortcutPath",
            str_or_empty(field_ci(game, &["ShortcutPath", "shortcut_path"])),
        ),
        VdfRecord::string("LaunchOptions", options),
        VdfRecord::int32(
            "IsHidden",
            u32::from(
                field_ci(game, &["IsHidden", "is_hidden"])
                    .and_then(Value::as_bool)
                    .unwrap_or(false),
            ),
        ),
        VdfRecord::int32(
            "AllowDesktopConfig",
            u32::from(
                field_ci(game, &["AllowDesktopConfig", "allow_desktop_config"])
                    .and_then(Value::as_bool)
                    .unwrap_or(true),
            ),
        ),
        VdfRecord::int32(
            "OpenVR",
            u32::from(
                field_ci(game, &["OpenVR", "openvr"])
                    .and_then(Value::as_bool)
                    .unwrap_or(false),
            ),
        ),
        VdfRecord::int32(
            "Devkit",
            u32::from(
                field_ci(game, &["Devkit", "devkit"])
                    .and_then(Value::as_bool)
                    .unwrap_or(false),
            ),
        ),
        VdfRecord::string(
            "DevkitGameID",
            str_or_empty(field_ci(game, &["DevkitGameID", "devkit_game_id"])),
        ),
        VdfRecord::int32(
            "LastPlayTime",
            field_ci(game, &["LastPlayTime", "last_play_time"])
                .and_then(|v| coerce_u32(v).ok())
                .unwrap_or(0),
        ),
    ];
    let mut tag_children = Vec::new();
    if let Some(tags) = field_ci(game, &["tags", "Tags"]) {
        if let Some(arr) = tags.as_array() {
            for (idx, item) in arr.iter().enumerate() {
                tag_children.push(VdfRecord::string(idx.to_string(), str_or_empty(Some(item))));
            }
        } else if let Some(obj) = tags.as_object() {
            for (k, v) in obj {
                tag_children.push(VdfRecord::string(k.clone(), str_or_empty(Some(v))));
            }
        }
    }
    children.push(VdfRecord::record("tags", tag_children));
    if !game_id.trim().is_empty() {
        children.push(VdfRecord::string(
            "OpenBoxGameID",
            game_id.trim().to_owned(),
        ));
    }
    Ok(VdfRecord::record("0", children))
}

pub fn record_appid(record: &VdfRecord) -> Option<u32> {
    for child in record.children() {
        if child.key.eq_ignore_ascii_case("appid") {
            return match &child.value {
                VdfValue::Int32(n) => Some(*n),
                VdfValue::String { text, .. } => coerce_u32(&Value::String(text.clone())).ok(),
                _ => None,
            };
        }
    }
    None
}

fn record_text(record: &VdfRecord, keys: &[&str]) -> String {
    for key in keys {
        for child in record.children() {
            if child.key == *key
                && let VdfValue::String { text, .. } = &child.value
            {
                return text.clone();
            }
        }
    }
    String::new()
}

fn record_openbox_game_id(record: &VdfRecord) -> String {
    for child in record.children() {
        if matches!(
            child.key.to_lowercase().as_str(),
            "openboxgameid" | "openbox_game_id" | "game_id" | "id"
        ) {
            match &child.value {
                VdfValue::String { text, .. } if !text.trim().is_empty() => {
                    return text.trim().to_owned();
                }
                VdfValue::Int32(n) => return n.to_string(),
                _ => {}
            }
        }
    }
    String::new()
}

pub fn is_bridge_shortcut(record: &VdfRecord) -> bool {
    for child in record.children() {
        if matches!(
            child.key.to_lowercase().as_str(),
            "openboxgameid" | "openbox_game_id"
        ) {
            return match &child.value {
                VdfValue::String { text, .. } => !text.trim().is_empty(),
                VdfValue::Int32(_) => true,
                _ => false,
            };
        }
    }
    false
}

fn record_with_appid(record: &VdfRecord, appid: u32) -> VdfRecord {
    let mut children = Vec::with_capacity(record.children().len() + 1);
    let mut replaced = false;
    for child in record.children() {
        if !child.key.eq_ignore_ascii_case("appid") {
            children.push(child.clone());
            continue;
        }
        replaced = true;
        match &child.value {
            VdfValue::String { .. } => children.push(VdfRecord {
                key: child.key.clone(),
                raw_key: child.raw_key.clone(),
                value: VdfValue::String {
                    text: appid.to_string(),
                    raw: appid.to_string().into_bytes(),
                },
            }),
            _ => children.push(VdfRecord {
                key: child.key.clone(),
                raw_key: child.raw_key.clone(),
                value: VdfValue::Int32(appid),
            }),
        }
    }
    if !replaced {
        children.insert(0, VdfRecord::int32("appid", appid));
    }
    VdfRecord {
        key: record.key.clone(),
        raw_key: record.raw_key.clone(),
        value: VdfValue::Record(children),
    }
}

fn collision_appid(record: &VdfRecord, occupied: &HashSet<u32>) -> u32 {
    let exe = record_text(record, &["Exe", "exe"]);
    let name = record_text(record, &["AppName", "name"]);
    let gid = record_openbox_game_id(record);
    let mut seed = Vec::new();
    seed.extend_from_slice(exe.as_bytes());
    seed.push(0);
    seed.extend_from_slice(name.as_bytes());
    seed.push(0);
    seed.extend_from_slice(gid.as_bytes());
    let mut salt = 0u64;
    loop {
        let mut hasher = Crc32Hasher::new();
        hasher.update(&seed);
        if salt > 0 {
            hasher.update(&[0]);
            hasher.update(salt.to_string().as_bytes());
        }
        let cand = hasher.finalize() | OPENBOX_APPID_MASK;
        if !occupied.contains(&cand) {
            return cand;
        }
        salt += 1;
    }
}

fn merge_record(current: &VdfRecord, proposed: &VdfRecord) -> VdfRecord {
    let (VdfValue::Record(cur_children), VdfValue::Record(prop_children)) =
        (&current.value, &proposed.value)
    else {
        return proposed.with_key(current.key.clone());
    };
    let mut children = cur_children.clone();
    let mut positions: HashMap<String, usize> = HashMap::new();
    for (idx, child) in children.iter().enumerate() {
        positions.entry(child.key.clone()).or_insert(idx);
    }
    for child in prop_children {
        if let Some(&idx) = positions.get(&child.key) {
            children[idx] = child.clone();
        } else {
            positions.insert(child.key.clone(), children.len());
            children.push(child.clone());
        }
    }
    VdfRecord {
        key: current.key.clone(),
        raw_key: current.raw_key.clone(),
        value: VdfValue::Record(children),
    }
}

fn next_record_key(records: &[VdfRecord]) -> String {
    let used: HashSet<&str> = records.iter().map(|r| r.key.as_str()).collect();
    let mut cand = records
        .iter()
        .filter_map(|r| r.key.parse::<i64>().ok())
        .max()
        .unwrap_or(-1)
        + 1;
    while used.contains(cand.to_string().as_str()) {
        cand += 1;
    }
    cand.to_string()
}

pub fn public_record(record: &VdfRecord) -> Value {
    let val = match &record.value {
        VdfValue::Record(children) => Value::Array(children.iter().map(public_record).collect()),
        VdfValue::Int32(n) => json!(*n),
        VdfValue::String { text, .. } => Value::String(text.clone()),
    };
    json!({
        "key": record.key,
        "tag": record.tag(),
        "value": val,
    })
}

fn record_from_public(val: &Value) -> Result<VdfRecord, SteamBridgeError> {
    let Some(obj) = val.as_object() else {
        return Err(SteamBridgeError::Invalid(
            "Invalid public VDF record descriptor.".to_owned(),
        ));
    };
    let key = obj
        .get("key")
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_owned();
    let tag_val = obj
        .get("tag")
        .or_else(|| obj.get("type"))
        .ok_or_else(|| SteamBridgeError::Invalid("Invalid VDF record tag.".to_owned()))?;
    let tag = match tag_val {
        Value::Number(n) => n
            .as_u64()
            .and_then(|u| u8::try_from(u).ok())
            .ok_or_else(|| SteamBridgeError::Invalid("Invalid VDF record tag.".to_owned()))?,
        Value::String(s) => match s.to_lowercase().as_str() {
            "record" | "object" => TYPE_RECORD,
            "int32" | "int" => TYPE_INT32,
            "string" => TYPE_STRING,
            other => other
                .parse::<u8>()
                .map_err(|_| SteamBridgeError::Invalid("Invalid VDF record tag.".to_owned()))?,
        },
        _ => {
            return Err(SteamBridgeError::Invalid(
                "Invalid VDF record tag.".to_owned(),
            ));
        }
    };
    let raw_val = obj.get("value");
    match tag {
        TYPE_RECORD => {
            let mut children = Vec::new();
            if let Some(arr) = raw_val.and_then(Value::as_array) {
                for item in arr {
                    children.push(record_from_public(item)?);
                }
            }
            Ok(VdfRecord::record(key, children))
        }
        TYPE_INT32 => {
            let n = coerce_u32(raw_val.unwrap_or(&json!(0)))?;
            Ok(VdfRecord::int32(key, n))
        }
        TYPE_STRING => Ok(VdfRecord::string(key, str_or_empty(raw_val))),
        _ => Err(SteamBridgeError::Invalid(format!(
            "Unsupported VDF record tag 0x{tag:02x}."
        ))),
    }
}

fn file_state(path: &Path) -> Result<(PathBuf, Vec<u8>, bool), SteamBridgeError> {
    if let Ok(meta) = fs::symlink_metadata(path) {
        if meta.file_type().is_symlink() {
            return Err(SteamBridgeError::Invalid(
                "Refusing to use a symlink shortcuts.vdf.".to_owned(),
            ));
        }
        if !meta.is_file() {
            return Err(SteamBridgeError::Invalid(
                "shortcuts.vdf path is not a regular file.".to_owned(),
            ));
        }
        if meta.len() as usize > MAX_SHORTCUTS_BYTES {
            return Err(SteamBridgeError::TooLarge(
                "shortcuts.vdf is too large.".to_owned(),
            ));
        }
        let data = fs::read(path).map_err(|e| SteamBridgeError::Io(e.to_string()))?;
        return Ok((path.to_path_buf(), data, true));
    }
    Ok((path.to_path_buf(), Vec::new(), false))
}

fn digest_opt(data: &[u8], exists: bool) -> Value {
    if exists {
        Value::String(hex::encode(Sha256::digest(data)))
    } else {
        Value::Null
    }
}

fn plan_base(
    doc: &ShortcutsVdf,
    data: &[u8],
    exists: bool,
    path: &Path,
    op: &str,
) -> Map<String, Value> {
    let foreign = doc
        .records
        .iter()
        .filter(|r| !is_bridge_shortcut(r))
        .count();
    let mut m = Map::new();
    m.insert("format".into(), json!("openbox-steam-bridge-preview"));
    m.insert("version".into(), json!(1));
    m.insert("preview".into(), json!(true));
    m.insert("operation".into(), json!(op));
    m.insert("path".into(), json!(path.to_string_lossy()));
    m.insert("base_exists".into(), json!(exists));
    m.insert("base_sha256".into(), digest_opt(data, exists));
    m.insert("current_count".into(), json!(doc.records.len()));
    m.insert("foreign_count".into(), json!(foreign));
    m
}

fn apply_plan_for_records(
    doc: &ShortcutsVdf,
    data: &[u8],
    exists: bool,
    path: &Path,
    proposed: &[VdfRecord],
) -> Result<(Value, Vec<VdfRecord>), SteamBridgeError> {
    let current = doc.records.clone();
    let mut output = current.clone();
    let mut added: Vec<VdfRecord> = Vec::new();
    let mut updated: Vec<Value> = Vec::new();
    let mut unchanged = 0usize;

    let mut by_appid: HashMap<u32, usize> = HashMap::new();
    let mut by_game_id: HashMap<String, usize> = HashMap::new();
    for (idx, rec) in output.iter().enumerate() {
        if let Some(id) = record_appid(rec) {
            by_appid.entry(id).or_insert(idx);
        }
        if is_bridge_shortcut(rec) {
            let gid = record_openbox_game_id(rec);
            if !gid.is_empty() {
                by_game_id.entry(gid).or_insert(idx);
            }
        }
    }

    let mut seen: HashSet<u32> = HashSet::new();
    let mut seen_gids: HashSet<String> = HashSet::new();
    let mut normalized: Vec<VdfRecord> = Vec::new();

    for rec in proposed {
        let mut item = rec.clone();
        let mut rec_id = record_appid(&item).ok_or_else(|| {
            SteamBridgeError::Invalid("Every bridge shortcut must have an appid.".to_owned())
        })?;
        let gid = record_openbox_game_id(&item);
        if !gid.is_empty() && seen_gids.contains(&gid) {
            return Err(SteamBridgeError::Invalid(format!(
                "Duplicate bridge game id: {gid}."
            )));
        }
        let mut idx_opt = if !gid.is_empty() {
            by_game_id.get(&gid).copied()
        } else {
            None
        };
        if let Some(idx) = idx_opt
            && let Some(existing_appid) = record_appid(&output[idx])
        {
            rec_id = existing_appid;
            item = record_with_appid(&item, rec_id);
        }
        let appid_owner = by_appid.get(&rec_id).copied();
        if let (Some(idx), Some(owner)) = (idx_opt, appid_owner) {
            if owner != idx && is_bridge_shortcut(&output[owner]) {
                let occupied: HashSet<u32> =
                    by_appid.keys().copied().filter(|k| *k != rec_id).collect();
                rec_id = collision_appid(&item, &occupied);
                item = record_with_appid(&item, rec_id);
            }
        } else if idx_opt.is_none() {
            idx_opt = appid_owner;
            if let Some(idx) = idx_opt {
                if is_bridge_shortcut(&output[idx]) {
                    let existing_gid = record_openbox_game_id(&output[idx]);
                    if gid.is_empty() || existing_gid != gid {
                        let occupied: HashSet<u32> = by_appid.keys().copied().collect();
                        rec_id = collision_appid(&item, &occupied);
                        item = record_with_appid(&item, rec_id);
                        idx_opt = None;
                    }
                } else if output[idx] != item {
                    idx_opt = None;
                }
            }
        }
        if !seen.insert(rec_id) {
            return Err(SteamBridgeError::Invalid(format!(
                "Duplicate bridge appid: {rec_id}."
            )));
        }
        if !gid.is_empty() {
            seen_gids.insert(gid);
        }
        normalized.push(item.clone());
        if let Some(idx) = idx_opt
            && !is_bridge_shortcut(&output[idx])
            && output[idx] != item
        {
            idx_opt = None;
        }
        match idx_opt {
            None => {
                let new_key = next_record_key(&output);
                let keyed = item.with_key(new_key);
                by_appid.insert(rec_id, output.len());
                added.push(keyed.clone());
                output.push(keyed);
            }
            Some(idx) => {
                if output[idx] == item {
                    unchanged += 1;
                } else {
                    let before = output[idx].clone();
                    let merged = merge_record(&before, &item);
                    output[idx] = merged.clone();
                    if before == merged {
                        unchanged += 1;
                    } else {
                        updated.push(json!({
                            "before": public_record(&before),
                            "after": public_record(&merged),
                        }));
                    }
                }
            }
        }
    }

    let changed = output != current;
    let mut plan = plan_base(doc, data, exists, path, "apply");
    plan.insert("desired_count".into(), json!(normalized.len()));
    plan.insert("added".into(), json!(added.len()));
    plan.insert("updated".into(), json!(updated.len()));
    plan.insert("removed".into(), json!(0));
    plan.insert("unchanged".into(), json!(unchanged));
    plan.insert("changed".into(), json!(changed));
    plan.insert(
        "additions".into(),
        Value::Array(added.iter().map(public_record).collect()),
    );
    plan.insert("updates".into(), Value::Array(updated));
    plan.insert("removals".into(), Value::Array(Vec::new()));
    plan.insert(
        "desired_records".into(),
        Value::Array(normalized.iter().map(public_record).collect()),
    );
    plan.insert(
        "result_records".into(),
        Value::Array(output.iter().map(public_record).collect()),
    );
    Ok((Value::Object(plan), output))
}

pub fn preview_shortcuts(path: &Path, shortcuts: &[VdfRecord]) -> Result<Value, SteamBridgeError> {
    let (target, data, exists) = file_state(path)?;
    let doc = parse_shortcuts_bytes(&data, MAX_SHORTCUTS_BYTES)?;
    let (plan, _) = apply_plan_for_records(&doc, &data, exists, &target, shortcuts)?;
    Ok(plan)
}

fn record_matches_target(record: &VdfRecord, target: &Value) -> bool {
    if !is_bridge_shortcut(record) {
        return false;
    }
    let rec_id = record_appid(record);
    let name = record_text(record, &["AppName", "name"]);
    match target {
        Value::Object(obj) => {
            if let Some(explicit) = field_ci(obj, &["appid", "app_id", "steam_app_id"])
                && let Ok(id) = coerce_u32(explicit)
                && rec_id == Some(id)
            {
                return true;
            }
            if let (Some(tn), Some(te)) = (
                field_ci(obj, &["AppName", "name"]).and_then(Value::as_str),
                field_ci(obj, &["Exe", "exe"]).and_then(Value::as_str),
            ) {
                return appid_for(te, tn).ok() == rec_id;
            }
            if let Some(gid_val) = field_ci(
                obj,
                &[
                    "OpenBoxGameId",
                    "OpenBoxGameID",
                    "openbox_game_id",
                    "game_id",
                    "id",
                ],
            ) {
                let target_gid = str_or_empty(Some(gid_val));
                if !target_gid.is_empty() && record_openbox_game_id(record) == target_gid {
                    return true;
                }
            }
            false
        }
        Value::Number(_) | Value::Bool(_) => coerce_u32(target).ok() == rec_id,
        Value::String(s) => {
            if s.chars().all(|c| c.is_ascii_digit())
                && !s.is_empty()
                && coerce_u32(target).ok() == rec_id
            {
                return true;
            }
            s == &name
        }
        _ => false,
    }
}

fn remove_plan(
    doc: &ShortcutsVdf,
    data: &[u8],
    exists: bool,
    path: &Path,
    targets: &[Value],
) -> Result<(Value, Vec<VdfRecord>), SteamBridgeError> {
    if targets.is_empty() {
        return Err(SteamBridgeError::Invalid(
            "At least one shortcut target is required.".to_owned(),
        ));
    }
    let mut removed = Vec::new();
    let mut kept = Vec::new();
    for rec in &doc.records {
        if targets.iter().any(|t| record_matches_target(rec, t)) {
            removed.push(rec.clone());
        } else {
            kept.push(rec.clone());
        }
    }
    let mut plan = plan_base(doc, data, exists, path, "remove");
    plan.insert("desired_count".into(), json!(kept.len()));
    plan.insert("added".into(), json!(0));
    plan.insert("updated".into(), json!(0));
    plan.insert("removed".into(), json!(removed.len()));
    plan.insert("unchanged".into(), json!(kept.len()));
    plan.insert("changed".into(), json!(!removed.is_empty()));
    plan.insert("additions".into(), Value::Array(Vec::new()));
    plan.insert("updates".into(), Value::Array(Vec::new()));
    plan.insert(
        "removals".into(),
        Value::Array(removed.iter().map(public_record).collect()),
    );
    plan.insert(
        "desired_records".into(),
        Value::Array(kept.iter().map(public_record).collect()),
    );
    plan.insert(
        "result_records".into(),
        Value::Array(kept.iter().map(public_record).collect()),
    );
    plan.insert("targets".into(), Value::Array(targets.to_vec()));
    Ok((Value::Object(plan), kept))
}

pub fn preview_remove(path: &Path, targets: &[Value]) -> Result<Value, SteamBridgeError> {
    let (target, data, exists) = file_state(path)?;
    let doc = parse_shortcuts_bytes(&data, MAX_SHORTCUTS_BYTES)?;
    let (plan, _) = remove_plan(&doc, &data, exists, &target, targets)?;
    Ok(plan)
}

pub fn commit_plan(path: &Path, plan: &Value, dry_run: bool) -> Result<Value, SteamBridgeError> {
    let Some(plan_obj) = plan.as_object() else {
        return Err(SteamBridgeError::Invalid(
            "Invalid Steam Bridge preview plan.".to_owned(),
        ));
    };
    if plan_obj.get("format").and_then(Value::as_str) != Some("openbox-steam-bridge-preview") {
        return Err(SteamBridgeError::Invalid(
            "Invalid Steam Bridge preview plan.".to_owned(),
        ));
    }
    if !plan_obj.contains_key("base_sha256") || !plan_obj.contains_key("base_exists") {
        return Err(SteamBridgeError::Invalid(
            "Steam Bridge preview plan has no base fingerprint.".to_owned(),
        ));
    }
    let (target, data, exists) = file_state(path)?;
    if let Some(plan_path) = plan_obj
        .get("path")
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
    {
        let p_norm = fs::canonicalize(expand_user(Path::new(plan_path)))
            .unwrap_or_else(|_| expand_user(Path::new(plan_path)));
        let t_norm =
            fs::canonicalize(expand_user(&target)).unwrap_or_else(|_| expand_user(&target));
        if p_norm != t_norm {
            return Err(SteamBridgeError::Invalid(
                "Steam Bridge plan targets a different shortcuts.vdf path.".to_owned(),
            ));
        }
    }
    let actual_digest = digest_opt(&data, exists);
    let expected_digest = plan_obj.get("base_sha256").cloned().unwrap_or(Value::Null);
    let expected_exists = plan_obj
        .get("base_exists")
        .and_then(Value::as_bool)
        .unwrap_or(false);
    if actual_digest != expected_digest || expected_exists != exists {
        return Err(SteamBridgeError::Stale(
            "shortcuts.vdf changed after the preview.".to_owned(),
        ));
    }
    let op = plan_obj
        .get("operation")
        .and_then(Value::as_str)
        .unwrap_or("apply");
    let doc = parse_shortcuts_bytes(&data, MAX_SHORTCUTS_BYTES)?;
    let (mut result_plan, result_records) = match op {
        "remove" => {
            let targets = plan_obj
                .get("targets")
                .and_then(Value::as_array)
                .cloned()
                .unwrap_or_default();
            remove_plan(&doc, &data, exists, &target, &targets)?
        }
        "apply" => {
            let desired_raw = plan_obj
                .get("desired_records")
                .and_then(Value::as_array)
                .ok_or_else(|| {
                    SteamBridgeError::Invalid("Invalid public VDF record list.".to_owned())
                })?;
            let desired: Result<Vec<VdfRecord>, _> =
                desired_raw.iter().map(record_from_public).collect();
            apply_plan_for_records(&doc, &data, exists, &target, &desired?)?
        }
        _ => {
            return Err(SteamBridgeError::Invalid(
                "Invalid Steam Bridge plan operation.".to_owned(),
            ));
        }
    };

    let changed = result_plan
        .get("changed")
        .and_then(Value::as_bool)
        .unwrap_or(false);
    if !changed || dry_run {
        if let Some(obj) = result_plan.as_object_mut() {
            obj.insert("applied".into(), json!(false));
            obj.insert("written".into(), json!(false));
        }
        return Ok(result_plan);
    }
    let out_doc = ShortcutsVdf {
        records: result_records,
        root_key: doc.root_key,
        raw_root_key: doc.raw_root_key,
        source_empty: false,
    };
    let encoded = encode_shortcuts(&out_doc, MAX_SHORTCUTS_BYTES, false)?;
    write_shortcuts_bytes(&target, &encoded)?;
    if let Some(obj) = result_plan.as_object_mut() {
        obj.insert("applied".into(), json!(true));
        obj.insert("written".into(), json!(true));
    }
    Ok(result_plan)
}

pub fn steam_roots() -> Vec<PathBuf> {
    let home = home_dir().unwrap_or_else(|| PathBuf::from("."));
    vec![
        home.join(".local/share/Steam"),
        home.join(".steam/steam"),
        home.join(".var/app/com.valvesoftware.Steam/.local/share/Steam"),
    ]
}

pub fn candidate_shortcuts_paths() -> Vec<PathBuf> {
    let mut out = Vec::new();
    for root in steam_roots() {
        let userdata = root.join("userdata");
        let Ok(rd) = fs::read_dir(&userdata) else {
            continue;
        };
        let mut accounts: Vec<PathBuf> = rd
            .flatten()
            .map(|e| e.path())
            .filter(|p| p.is_dir())
            .collect();
        accounts.sort();
        for acc in accounts {
            out.push(acc.join("config").join("shortcuts.vdf"));
        }
    }
    out
}

pub fn steam_grid_dirs() -> Vec<PathBuf> {
    candidate_shortcuts_paths()
        .into_iter()
        .filter_map(|p| p.parent().map(|cfg| cfg.join("grid")))
        .collect()
}

pub fn copy_grid_art(grid_dir: &Path, appid: u32, game: &Map<String, Value>) -> usize {
    const FIELDS: &[(&str, &str)] = &[
        ("cover", "p"),
        ("background", "_hero"),
        ("clear_logo", "_logo"),
    ];
    let mut written = 0usize;
    for (field, suffix) in FIELDS {
        let Some(src_str) = game
            .get(*field)
            .and_then(Value::as_str)
            .filter(|s| !s.is_empty())
        else {
            continue;
        };
        let src = Path::new(src_str);
        if !src.is_file() {
            continue;
        }
        let dst = grid_dir.join(format!("{appid}{suffix}.png"));
        if fs::copy(src, &dst).is_ok() {
            written += 1;
        }
    }
    written
}
