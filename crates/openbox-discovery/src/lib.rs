//! Phase 8 Discovery, Query Grammar, Game DNA, Insights, Radio, Picker,
//! Constellation, Party, Story, Trophies, Player's Log, Backlog Itinerary, & Premium.

pub mod collections;
pub mod constellation;
pub mod discovery;
pub mod dna;
pub mod filter_presets;
pub mod insights;
pub mod itinerary;
pub mod party;
pub mod picker;
pub mod playerlog;
pub mod premium;
pub mod query;
pub mod radio;
pub mod story;
pub mod trophies;

#[cfg(test)]
mod tests {
    use std::path::PathBuf;

    use jiff::civil::Date;
    use serde_json::{Map, Value, json};

    use super::*;

    #[test]
    fn query_grammar_matches_golden_vectors_exactly() {
        let manifest_dir = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
        let golden_path = manifest_dir
            .parent()
            .unwrap()
            .parent()
            .unwrap()
            .join("tests/golden/query_grammar.json");
        let raw = std::fs::read_to_string(&golden_path).expect("golden query_grammar.json");
        let golden: Value = serde_json::from_str(&raw).expect("valid golden json");
        let cases = golden["cases"].as_array().expect("cases array");
        assert!(!cases.is_empty());
        for case in cases {
            let q = case["query"].as_str().expect("query str");
            let expected = &case["parsed"];
            let actual = query::parse_query(q, None);
            assert_eq!(
                actual, *expected,
                "Query grammar mismatch for query: {q:?}\nactual:   {actual}\nexpected: {expected}"
            );
        }
    }

    #[test]
    fn dna_index_bm25_and_similarity_search() {
        let games = vec![
            json!({
                "game_id": "g1",
                "name": "Stardew Valley",
                "genre": "Farming, Simulation",
                "description": "Cozy relaxing farming life sim in a peaceful valley with crops and fishing.",
                "tags": ["cozy", "farming"],
                "user_rating": 5,
                "progress": "",
                "time_to_beat_hours": 50.0,
            }),
            json!({
                "game_id": "g2",
                "name": "Dark Souls",
                "genre": "Action RPG",
                "description": "Punishing difficult dark fantasy boss fights and stamina combat.",
                "tags": ["soulslike", "dark"],
                "user_rating": 4,
                "progress": "Beaten",
            }),
            json!({
                "game_id": "g3",
                "name": "Animal Crossing",
                "genre": "Life Sim",
                "description": "Peaceful gentle wholesome village life with fishing and charming neighbors.",
                "tags": ["cozy", "wholesome"],
            }),
        ];
        let mut idx = dna::rebuild_index(&games, "en", None);
        assert_eq!(idx["format"], json!(1));
        assert_eq!(idx["lexicon_version"], json!(3));
        assert_eq!(idx["doc_count"], json!(3));

        let res = dna::parse_dna_query("cozy farming", &games, &idx, "en", 10);
        assert_eq!(res["branch"], json!("bm25"));
        let top_id = res["results"][0]["game_id"].as_str().unwrap();
        assert_eq!(top_id, "g1");

        let sim = dna::parse_dna_query("games like Stardew Valley", &games, &idx, "en", 10);
        assert_eq!(sim["branch"], json!("similarity"));
        assert_eq!(sim["anchor"]["game_id"], json!("g1"));
        assert!(
            sim["results"]
                .as_array()
                .unwrap()
                .iter()
                .all(|r| r["game_id"] != json!("g1"))
        );

        dna::note_game_removed(&mut idx, "g2");
        assert_eq!(idx["doc_count"], json!(2));
    }

    #[test]
    fn playerlog_capsules_redaction_and_milestones() {
        let mut state = json!({
            "games": [
                {"game_id": "g1", "name": "Chrono Trigger", "platform": "SNES", "playtime_seconds": 40000, "progress": "Beaten", "added_at": "2024-01-10T10:00:00Z"},
            ],
            "history": [
                {"game_id": "g1", "started": "2024-02-01T10:00:00Z", "seconds": 40000},
            ],
        });
        let today = Date::constant(2026, 6, 1);
        let mut req = Map::new();
        req.insert("body".into(), json!("Secret future note"));
        req.insert("unlock_at".into(), json!("2026-12-25"));
        req.insert("game_id".into(), json!("g1"));
        let created = playerlog::create_capsule(&mut state, &req, Some(today), None).unwrap();
        assert_eq!(created["locked"], json!(true));
        assert!(
            created.get("body").is_none(),
            "locked capsule MUST omit body"
        );

        let cid = created["id"].as_str().unwrap().to_owned();
        let early = playerlog::open_capsule(&mut state, &cid, Some(today), None);
        assert!(early.is_err());

        let opened =
            playerlog::open_capsule(&mut state, &cid, Some(Date::constant(2026, 12, 25)), None)
                .unwrap();
        assert_eq!(opened["locked"], json!(false));
        assert_eq!(opened["body"], json!("Secret future note"));

        let ms = playerlog::compute_milestones(&state, None, Some(today), Some(50));
        assert!(ms["counts"]["unlocked"].as_u64().unwrap() >= 3);
    }

    #[test]
    fn itinerary_build_live_projection_and_replan() {
        let mut state = json!({
            "games": [
                {"game_id": "g1", "name": "Portal", "platform": "PC", "time_to_beat_hours": 3.0, "playtime_seconds": 0},
                {"game_id": "g2", "name": "Hades", "platform": "PC", "time_to_beat_hours": 20.0, "playtime_seconds": 3600},
            ],
            "history": [],
            "settings": {},
        });
        let plan = itinerary::build_itinerary(&state, 30, 60, "quick-wins", None, None, None, None)
            .unwrap();
        assert_eq!(plan["entries"][0]["game_id"], json!("g1"));
        state["itinerary"] = plan;
        let live = itinerary::project_live_itinerary(&state, None);
        assert_eq!(live["plan"]["entries"][0]["live_status"], json!("on_track"));

        // Complete g1, replan should pin g1 as done
        state["games"][0]["progress"] = json!("Beaten");
        let replanned = itinerary::replan_itinerary(&mut state, &Map::new(), None, None).unwrap();
        assert_eq!(replanned["plan"]["done_count"], json!(1));
    }

    #[test]
    fn insights_constellation_picker_radio_party_story_trophies() {
        let state = json!({
            "games": [
                {"game_id": "g1", "id": 0, "name": "Super Metroid", "platform": "SNES", "genre": "Action, Adventure", "year": "1994", "rating": 5.0, "playtime_seconds": 7200, "play_count": 3, "favorite": true, "max_players": 2, "progress": "Beaten", "has_cover": true, "added_at": "2025-01-01T00:00:00Z"},
                {"game_id": "g2", "id": 1, "name": "Chrono Trigger", "platform": "SNES", "genre": "RPG", "year": "1995", "rating": 4.8, "playtime_seconds": 1800, "play_count": 1, "max_players": 2, "added_at": "2025-01-02T00:00:00Z"},
            ],
            "history": [
                {"game_id": "g1", "started": "2026-01-01T02:00:00", "seconds": 3600},
                {"game_id": "g2", "started": "2026-01-02T14:00:00", "seconds": 1800},
            ],
        });
        let sum = insights::summarize(&state, Some(Date::constant(2026, 1, 10)), 30);
        assert_eq!(sum["totals"]["games"], json!(2));
        assert_eq!(sum["streak"]["longest"], json!(2));

        let g = constellation::build_graph(
            state["games"].as_array().unwrap(),
            state["history"].as_array().unwrap(),
            None,
            50,
        );
        assert_eq!(g["nodes"].as_array().unwrap().len(), 2);
        assert!(!g["edges"].as_array().unwrap().is_empty());

        let picks = picker::pick_games(
            state["games"].as_array().unwrap(),
            state["history"].as_array().unwrap(),
            &picker::PickerCriteria::default(),
        );
        assert!(!picks.is_empty());

        let pq = party::build_party_queue(state["games"].as_array().unwrap(), 2, 0, 10, Some(42));
        assert_eq!(pq.len(), 2);

        let st = story::game_story(&state, &state["games"][0]);
        assert!(!st["events"].as_array().unwrap().is_empty());

        let tc = trophies::trophy_case(&state);
        assert_eq!(tc["total"], json!(12));
        assert!(tc["earned"].as_u64().unwrap() >= 2); // first_launch + night_owl
    }
}
