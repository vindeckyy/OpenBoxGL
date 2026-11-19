//! Steam Bridge (`shortcuts.vdf`), OBS WebSocket 5, highlight reels, Quick Resume,
//! Gameyfin client, high scores, deep links, tray recent writer, themes, and verified updates.

pub mod deeplinks;
pub mod gameyfin;
pub mod misc;
pub mod obs_bridge;
pub mod reels;
pub mod resume;
pub mod steam_bridge;
pub mod themes;
pub mod tray_recent;
pub mod updates;

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use std::fs;
    use tempfile::TempDir;

    #[test]
    fn steam_vdf_lossless_non_utf8_roundtrip_and_stale_guard() {
        // Construct raw shortcuts.vdf with non-UTF-8 bytes (0xFF 0xFE) in a foreign field
        let mut raw = Vec::new();
        raw.push(steam_bridge::TYPE_RECORD);
        raw.extend_from_slice(b"shortcuts\0");
        raw.push(steam_bridge::TYPE_RECORD);
        raw.extend_from_slice(b"0\0");
        raw.push(steam_bridge::TYPE_INT32);
        raw.extend_from_slice(b"appid\0");
        raw.extend_from_slice(&12345u32.to_le_bytes());
        raw.push(steam_bridge::TYPE_STRING);
        raw.extend_from_slice(b"ForeignField\0");
        raw.extend_from_slice(&[0x41, 0xff, 0xfe, 0x42, 0x00]);
        raw.push(steam_bridge::TYPE_END);
        raw.push(steam_bridge::TYPE_END);

        let parsed =
            steam_bridge::parse_shortcuts_bytes(&raw, steam_bridge::MAX_SHORTCUTS_BYTES).unwrap();
        let reencoded =
            steam_bridge::encode_shortcuts(&parsed, steam_bridge::MAX_SHORTCUTS_BYTES, true)
                .unwrap();
        assert_eq!(
            raw, reencoded,
            "binary VDF roundtrip must preserve non-UTF-8 bytes verbatim"
        );

        // Preview + apply + stale detection on disk
        let tmp = TempDir::new().unwrap();
        let vdf_path = tmp.path().join("shortcuts.vdf");
        fs::write(&vdf_path, &raw).unwrap();

        let game = json!({"game_id": "g1", "name": "Chrono Trigger", "path": "/roms/ct.sfc"});
        let sc =
            steam_bridge::shortcut_from_game(game.as_object().unwrap(), Some("/usr/bin/openbox"))
                .unwrap();
        let plan = steam_bridge::preview_shortcuts(&vdf_path, &[sc]).unwrap();
        assert_eq!(plan["added"], 1);
        assert_eq!(plan["foreign_count"], 1);

        let applied = steam_bridge::commit_plan(&vdf_path, &plan, false).unwrap();
        assert_eq!(applied["written"], true);

        // Re-applying stale plan must fail with Stale error
        let err = steam_bridge::commit_plan(&vdf_path, &plan, false).unwrap_err();
        assert!(err.is_stale());
    }

    #[test]
    fn ed25519_rejects_placeholder_and_small_order_keys() {
        let msg = [0u8; 32];
        let sig = [0u8; 64];
        let err = updates::verify_ed25519_strict(&updates::PLACEHOLDER_PUBLIC_KEY, &sig, &msg)
            .unwrap_err();
        assert!(err.contains("placeholder"));

        // Identity / small-order key rejected
        let mut small = [0u8; 32];
        small[0] = 1;
        assert!(updates::verify_ed25519_strict(&small, &sig, &msg).is_err());

        // Embedded openbox-release.pub is 32 bytes
        assert_eq!(updates::EMBEDDED_PUBLIC_KEY.len(), 32);
    }

    #[test]
    fn obs_auth_and_deeplinks_and_tray_recent() {
        let auth = obs_bridge::build_authentication(Some("secret"), "salt", "challenge").unwrap();
        assert!(!auth.is_empty());
        assert!(
            obs_bridge::parse_endpoint(Some("wss://127.0.0.1:4455"), "127.0.0.1", 4455).is_err()
        );
        let (h, p, t) =
            obs_bridge::parse_endpoint(Some("127.0.0.1:4455"), "127.0.0.1", 4455).unwrap();
        assert_eq!((h.as_str(), p, t.as_str()), ("127.0.0.1", 4455, "/"));

        let parsed = deeplinks::parse_uri("openbox://search/chrono%20trigger");
        assert_eq!(parsed["action"], "search");
        assert_eq!(parsed["query"], "chrono trigger");
        assert_eq!(
            deeplinks::parse_uri("openbox://evil.example/launch/1")["action"],
            "unknown"
        );

        let tmp = TempDir::new().unwrap();
        let state = json!({
            "games": [{"game_id": "g1", "name": "Super\tMario\nWorld", "last_played": "2026-01-02T00:00:00"}],
            "history": []
        });
        let out = tray_recent::write_recent_games(tmp.path(), &state, 8).unwrap();
        assert_eq!(fs::read_to_string(out).unwrap(), "Super Mario World\tg1\n");
    }

    #[test]
    fn reels_builds_html_fallback_and_themes_sanitizes_css() {
        let tmp = TempDir::new().unwrap();
        let res = reels::create_reel(
            json!({"game_id": "g1", "name": "Test Game"}).as_object(),
            tmp.path(),
            None,
        )
        .unwrap();
        assert_eq!(res["format"], "html");
        assert_eq!(res["fallback"], true);

        let bad_css = tmp.path().join("bad.css");
        fs::write(&bad_css, "@import url('https://evil.test/x.css');").unwrap();
        assert!(themes::import_theme_file(&bad_css.to_string_lossy(), tmp.path()).is_err());

        let good_css = tmp.path().join("neon.css");
        fs::write(&good_css, ":root { --bg: #112233; }").unwrap();
        let name = themes::import_theme_file(&good_css.to_string_lossy(), tmp.path()).unwrap();
        assert_eq!(name, "neon");
        let bg = themes::write_native_background("neon", tmp.path(), tmp.path());
        assert_eq!(bg, "#112233");
    }
}
