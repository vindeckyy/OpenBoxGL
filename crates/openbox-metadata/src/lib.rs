//! Metadata providers, media engine, Artwork Doctor, Memories import,
//! RetroAchievements, Steam achievements, Achievement Trail, and Deal Radar.

pub mod artwork_hygiene;
pub mod deals;
pub mod emumovies;
pub mod http;
pub mod igdb;
pub mod lbdb;
pub mod media;
pub mod memories;
pub mod providers;
pub mod retroachievements;
pub mod screenscraper;
pub mod steam_achievements;
pub mod steamgrid;
pub mod trail;

#[cfg(test)]
mod tests {
    use super::*;
    use md5::{Digest as _, Md5};
    use serde_json::json;
    use std::io::Write;
    use tempfile::TempDir;
    use zip::write::SimpleFileOptions;

    #[test]
    fn sequence_matcher_matches_python_difflib() {
        assert!(
            (http::sequence_matcher_ratio("chrono trigger", "chrono trigger") - 1.0).abs() < 1e-9
        );
        let r = http::sequence_matcher_ratio("super mario world", "super mario bros");
        // Python: SequenceMatcher(None, 'super mario world', 'super mario bros').ratio() == 26/33 = 0.787878...
        assert!((r - (26.0 / 33.0)).abs() < 1e-6, "got {r}");
        let r2 = http::sequence_matcher_ratio("abcd", "efgh");
        assert_eq!(r2, 0.0);
    }

    #[test]
    fn lbdb_streaming_xml_build_and_search() {
        let dir = TempDir::new().unwrap();
        let zip_path = dir.path().join("Metadata.zip");
        let db_path = dir.path().join("launchbox.db");
        {
            let file = std::fs::File::create(&zip_path).unwrap();
            let mut zw = zip::ZipWriter::new(file);
            zw.start_file("Metadata.xml", SimpleFileOptions::default())
                .unwrap();
            zw.write_all(
                br#"<?xml version="1.0" encoding="utf-8"?>
<LaunchBox>
  <Game>
    <DatabaseID>42</DatabaseID>
    <Name>Chrono Trigger</Name>
    <Platform>Super Nintendo Entertainment System</Platform>
    <ReleaseDate>1995-03-11T00:00:00-05:00</ReleaseDate>
    <Developer>Square</Developer>
    <Publisher>Square</Publisher>
    <Genres>Role-Playing</Genres>
    <Overview>Time travel RPG.</Overview>
    <ESRB>E</ESRB>
  </Game>
  <GameImage>
    <DatabaseID>42</DatabaseID>
    <FileName>chrono-box.jpg</FileName>
    <Type>Box - Front</Type>
    <Region>North America</Region>
  </GameImage>
</LaunchBox>"#,
            )
            .unwrap();
            zw.finish().unwrap();
        }
        lbdb::build_database(&zip_path, &db_path).unwrap();
        let hits = lbdb::search_games(&db_path, "Chrono Trigger (USA)", "SNES", 10).unwrap();
        assert_eq!(hits.len(), 1);
        assert_eq!(hits[0]["database_id"], 42);
        assert_eq!(hits[0]["developer"], "Square");
        let imgs = lbdb::lbdb_media_candidates(&db_path, 42).unwrap();
        assert_eq!(imgs.len(), 1);
        assert_eq!(imgs[0]["kind"], "cover");
        assert!(imgs[0]["url"].as_str().unwrap().ends_with("chrono-box.jpg"));
    }

    #[test]
    fn retroachievements_rom_hashes_match_python_rules() {
        let dir = TempDir::new().unwrap();
        // iNES header (16 bytes starting with "NES\x1a") stripped before MD5
        let nes_path = dir.path().join("mario.nes");
        let mut nes_bytes = b"NES\x1a0123456789AB".to_vec();
        nes_bytes.extend_from_slice(b"PRGROMDATA");
        std::fs::write(&nes_path, &nes_bytes).unwrap();
        let got = retroachievements::game_hash(&json!({
            "path": nes_path.to_string_lossy(),
            "platform": "NES",
        }))
        .unwrap();
        let expected = hex::encode(Md5::digest(b"PRGROMDATA"));
        assert_eq!(got, expected);

        // N64 .v64 byte-swapped pairs
        let n64_path = dir.path().join("oot.v64");
        std::fs::write(&n64_path, b"\x37\x80\x40\x12").unwrap();
        let got_n64 = retroachievements::game_hash(&json!({
            "path": n64_path.to_string_lossy(),
            "platform": "Nintendo 64",
        }))
        .unwrap();
        let expected_n64 = hex::encode(Md5::digest(b"\x80\x37\x12\x40"));
        assert_eq!(got_n64, expected_n64);

        // Arcade hashes stem
        let arc_path = dir.path().join("sf2.zip");
        std::fs::write(&arc_path, b"dummy").unwrap();
        let got_arc = retroachievements::game_hash(&json!({
            "path": arc_path.to_string_lossy(),
            "platform": "Arcade",
        }))
        .unwrap();
        assert_eq!(got_arc, hex::encode(Md5::digest(b"sf2")));
    }

    #[test]
    fn artwork_hygiene_png_dimensions_and_report() {
        let dir = TempDir::new().unwrap();
        let png_path = dir.path().join("small.png");
        let mut hdr = Vec::new();
        hdr.extend_from_slice(b"\x89PNG\r\n\x1a\n");
        hdr.extend_from_slice(&13u32.to_be_bytes());
        hdr.extend_from_slice(b"IHDR");
        hdr.extend_from_slice(&100u32.to_be_bytes());
        hdr.extend_from_slice(&100u32.to_be_bytes());
        std::fs::write(&png_path, &hdr).unwrap();
        assert_eq!(
            artwork_hygiene::image_dimensions(&png_path),
            Some((100, 100))
        );

        let report = artwork_hygiene::build_report(
            &[json!({"game_id": "g1", "name": "Test", "cover": png_path.to_string_lossy()})],
            None,
            200,
            true,
        );
        assert_eq!(report["counts"]["low_res"], 1);
        assert_eq!(report["counts"]["wrong_aspect"], 1);
    }

    #[test]
    fn deals_and_trail_pure_logic() {
        assert!(
            deals::validate_allowlisted_url("https://store.steampowered.com/api/appdetails")
                .is_ok()
        );
        assert!(deals::validate_allowlisted_url("https://evil.example.com/api").is_err());

        let mut state =
            json!({"games": [], "deals": {"watchlist": [], "prices": {}, "last_poll_at": ""}});
        let entry = deals::watch_game(
            &mut state,
            json!({"appid": "620", "name": "Portal 2", "target_price_cents": 499})
                .as_object()
                .unwrap(),
        )
        .unwrap();
        let did = entry["id"].as_str().unwrap().to_owned();
        let listed = deals::list_deals(&state);
        assert_eq!(listed["watchlist"].as_array().unwrap().len(), 1);
        deals::unwatch_game(&mut state, &did).unwrap();
        assert!(
            deals::list_deals(&state)["watchlist"]
                .as_array()
                .unwrap()
                .is_empty()
        );

        let trail_res = trail::compute_game_trail(
            &json!({"game_id": "g1", "name": "Super Metroid", "platform": "SNES"}),
            &json!({
                "earned": 1,
                "total": 2,
                "achievements": [
                    {"id": 1, "title": "First", "points": 10, "earned": true, "date_earned": "2026-01-01 12:00:00"},
                    {"id": 2, "title": "Second", "points": 25, "earned": false}
                ]
            }),
            None,
        );
        assert_eq!(trail_res["earned"], 1);
        assert_eq!(trail_res["remaining"], 1);
        assert_eq!(trail_res["progress_pct"], 50.0);
        assert_eq!(trail_res["next_up"].as_array().unwrap().len(), 1);
    }
}
