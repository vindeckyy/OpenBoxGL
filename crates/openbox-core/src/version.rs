//! Canonical version and format constants for OpenBoxGL (ADR 0091, ADR 0093, ADR 0108, ADR 0115).

pub const APP_VERSION: &str = "3.0.0";
pub const API_VERSION: u32 = 2;
pub const STATE_SCHEMA_VERSION: u32 = 7;
pub const STATE_FORMAT: &str = "openbox-library";
pub const SESSIONS_FILE_NAME: &str = "sessions.json";
pub const SESSIONS_FORMAT: u32 = 1;
pub const PLUGIN_API_VERSION: u32 = 2;
pub const BACKUP_FORMAT: u32 = 2;
pub const BACKUP_READABLE_FORMATS: &[u32] = &[1, 2];
pub const BACKUP_WRITABLE_FORMATS: &[u32] = &[2];
pub const SYNC_FORMAT: u32 = 3;
pub const PATH_MODES: &[&str] = &["absolute", "portable"];
pub const ENVELOPE_FIELDS: &[&str] = &[
    "format",
    "schema_version",
    "library_id",
    "created_with",
    "path_mode",
    "library_root",
];
