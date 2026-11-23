//! Canonical JSON state store (v7), portable paths, SQLite read model, durable operations,
//! library maintenance (`check`/`repair`/`migrate`), and media manifest.

pub mod json_store;
pub mod library_v7;
pub mod media_manifest;
pub mod operations;
pub mod portable_paths;
pub mod sqlite_readmodel;

pub use json_store::{
    FileSignature, JsonStateStore, NOTIFICATIONS_CAP, QUEUE_CAP, StateError, TRASH_CAP,
    freeze_library_writes, is_library_frozen, normalize_state, prune_trash, raw_schema_version,
    read_sessions_file, schema_too_old_detail, secure_text_write, state_digest, sync_envelope,
};
pub use library_v7::{
    MOVE_PLAN_FORMAT, PRIMARY_NAME, ROLLBACK_REMOVED, V6_BACKUP_NAME, apply_move, check_library,
    encode_games, expand_games, plan_move, repair_library, server_running,
};
pub use operations::{OperationObserver, OperationService};
pub use sqlite_readmodel::{SQLITE_AUTO_THRESHOLD, SqliteReadModel};

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use std::time::Instant;

    #[test]
    fn json_store_roundtrip_snapshots_and_schema_v6_migration() {
        let dir = tempfile::tempdir().unwrap();
        let primary = dir.path().join("library.json");

        // Write real dummy ROM files so check_library reports healthy (exit_code 0)
        let rom1 = dir.path().join("ct.sfc");
        let rom2 = dir.path().join("quake.sh");
        std::fs::write(&rom1, b"snes").unwrap();
        std::fs::write(&rom2, b"#!/bin/sh\n").unwrap();

        // Write a schema v6 library on disk
        let v6 = json!({
            "schema_version": 6,
            "games": [
                {"name": "Chrono Trigger — クロノ・トリガー", "platform": "SNES", "path": rom1.to_string_lossy()},
                {"name": "Quake", "platform": "PC", "path": rom2.to_string_lossy(), "custom_plugin_field": 42}
            ],
            "profiles": {},
            "history": [],
            "settings": {"theme": "Midnight Circuit", "unknown_garbage_key": true},
            "playlists": [],
            "queue": [],
            "notifications": [],
            "ui_state": {},
            "active_sessions": [{"launch_id": "s1", "status": "active"}]
        });
        std::fs::write(&primary, serde_json::to_string_pretty(&v6).unwrap()).unwrap();

        let store = JsonStateStore::new(&primary);
        // Normal open MUST refuse schema 6 without touching the file (ADR 0108)
        let err = store.load().unwrap_err();
        assert_eq!(err.code(), "STATE_SCHEMA_TOO_OLD");

        // Explicit --migrate-library upgrades cleanly, keeps v6 backup, strips active_sessions from primary
        let (migrated, report) = store.migrate_primary_to_v7().unwrap();
        assert!(migrated.is_some());
        assert!(report.is_some());
        assert!(dir.path().join(V6_BACKUP_NAME).is_file());
        assert!(dir.path().join("migration-report.json").is_file());

        // Reload and verify unknown game field preserved, unknown setting pruned, sessions moved to sessions.json
        let loaded = store.load_readonly().unwrap();
        assert_eq!(loaded["schema_version"], json!(7));
        assert_eq!(loaded["format"], json!("openbox-library"));
        assert_eq!(loaded["games"][1]["custom_plugin_field"], json!(42));
        assert!(loaded["settings"].get("unknown_garbage_key").is_none());
        assert_eq!(loaded["active_sessions"].as_array().unwrap().len(), 1);
        assert!(dir.path().join("sessions.json").is_file());

        // Primary on disk must NOT contain active_sessions
        let disk_raw: serde_json::Value =
            serde_json::from_str(&std::fs::read_to_string(&primary).unwrap()).unwrap();
        assert!(disk_raw.get("active_sessions").is_none());

        // Integrity check passes healthy
        let report = check_library(dir.path());
        assert_eq!(report["exit_code"], json!(0));
        assert_eq!(report["games"], json!(2));
    }

    #[test]
    fn portable_paths_encode_decode_and_reject_escape() {
        let root = "/mnt/games";
        let enc = portable_paths::encode_path("/mnt/games/SNES/ct.sfc", root, false, false);
        assert_eq!(enc, "@library/SNES/ct.sfc");
        let dec = portable_paths::decode_path(&enc, root, false, false).unwrap();
        assert_eq!(dec, "/mnt/games/SNES/ct.sfc");
        assert!(portable_paths::decode_path("@library/../etc/passwd", root, false, false).is_err());
    }

    #[test]
    fn sqlite_readmodel_fts5_rebuild_and_search() {
        let dir = tempfile::tempdir().unwrap();
        let rm = SqliteReadModel::new(dir.path().join("sqlite_readmodel.db"));
        rm.set_enabled(true);
        let state = json!({
            "games": [
                {"game_id": "g1", "name": "Chrono Trigger", "platform": "SNES"},
                {"game_id": "g2", "name": "Quake II", "platform": "PC"},
            ]
        });
        rm.rebuild(&state, None).unwrap();
        assert_eq!(rm.count(), 2);
        let hits = rm.search("chrono", 10);
        assert_eq!(hits.len(), 1);
        assert_eq!(hits[0]["game_id"], json!("g1"));
    }

    #[test]
    fn operations_service_persists_and_interrupts_on_restart() {
        let dir = tempfile::tempdir().unwrap();
        let svc = OperationService::new(dir.path());
        let job = svc.create(
            "setup.scan",
            "Scanning ROMs",
            Some(&json!({"api_key": "secret"})),
        );
        let jid = job["job_id"].as_str().unwrap().to_owned();
        assert!(job["input"].get("api_key").is_none());
        svc.mark_running(&jid, "Working").unwrap();
        drop(svc);

        // New instance simulates restart: running job becomes interrupted
        let svc2 = OperationService::new(dir.path());
        let after = svc2.get(&jid).unwrap();
        assert_eq!(after["state"], json!("interrupted"));
    }

    #[test]
    fn ten_thousand_game_write_is_fast() {
        let dir = tempfile::tempdir().unwrap();
        let store = JsonStateStore::new(dir.path().join("library.json"));
        let mut games = Vec::with_capacity(10_000);
        for i in 0..10_000 {
            games.push(json!({
                "game_id": format!("game-{i:024x}"),
                "name": format!("Synthetic Game {i}"),
                "platform": "SNES",
                "genre": "RPG",
                "path": format!("/roms/snes/game_{i}.sfc"),
                "favorite": false
            }));
        }
        let mut initial = openbox_core::default_state();
        initial["games"] = serde_json::Value::Array(games);
        store.save(&initial).unwrap();

        let t0 = Instant::now();
        store
            .update(|st| {
                st["games"][0]["favorite"] = json!(true);
                Ok(())
            })
            .unwrap();
        let elapsed_ms = t0.elapsed().as_millis();
        // Python's 10k write gate is 500ms; Rust debug build should be well under it!
        assert!(
            elapsed_ms < 2500,
            "10k debug write took unexpectedly long: {elapsed_ms}ms"
        );
    }

    #[test]
    fn python_rust_bidirectional_library_v7_interop() {
        let repo_root = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .unwrap()
            .parent()
            .unwrap();
        let dir = tempfile::tempdir().unwrap();
        let primary = dir.path().join("library.json");
        let rom1 = dir.path().join("ct.sfc");
        let rom2 = dir.path().join("quake.sh");
        std::fs::write(&rom1, b"snes").unwrap();
        std::fs::write(&rom2, b"#!/bin/sh\n").unwrap();

        let python = if cfg!(windows) {
            ["python", "python3"]
        } else {
            ["python3", "python"]
        }
        .into_iter()
        .find(|cand| {
            std::process::Command::new(cand)
                .arg("--version")
                .output()
                .map(|o| {
                    o.status.success()
                        && String::from_utf8_lossy(&o.stdout).starts_with("Python 3.")
                })
                .unwrap_or(false)
        })
        .expect("python 3 must be available on PATH");

        // 1. Python writes a v7 library with Unicode title, real ROM path, and active session
        let py_write = format!(
            "import sys; sys.path.insert(0, {repo:?}); sys.path.insert(0, {app:?}); \
             from pathlib import Path; from state_store import JsonStateStore; \
             s = JsonStateStore(Path({prim:?})); \
             st = s.load(); \
             st['games'].append({{'name': 'Chrono Trigger — クロノ・トリガー', 'platform': 'SNES', 'path': {rom1:?}, 'favorite': False}}); \
             st['active_sessions'].append({{'launch_id': 'py-sess-1', 'status': 'active'}}); \
             s.save(st); \
             print(st['library_id'])",
            repo = repo_root.to_string_lossy(),
            app = repo_root.join("app").to_string_lossy(),
            prim = primary.to_string_lossy(),
            rom1 = rom1.to_string_lossy(),
        );
        let out = std::process::Command::new(python)
            .env("PYTHONUTF8", "1")
            .args(["-B", "-c", &py_write])
            .output()
            .expect("python3 must run");
        assert!(
            out.status.success(),
            "py write failed: {}",
            String::from_utf8_lossy(&out.stderr)
        );
        let py_lib_id = String::from_utf8_lossy(&out.stdout).trim().to_owned();
        assert_eq!(py_lib_id.len(), 26);

        // 2. Rust loads, verifies identical library_id & game_id, mutates & saves
        let store = JsonStateStore::new(&primary);
        let loaded = store.load().unwrap();
        assert_eq!(loaded["library_id"].as_str().unwrap(), py_lib_id);
        let py_gid = loaded["games"][0]["game_id"].as_str().unwrap().to_owned();
        let mut fresh_game = loaded["games"][0].as_object().unwrap().clone();
        fresh_game.remove("game_id");
        let expected_gid = openbox_core::stable_game_id(&fresh_game);
        assert_eq!(py_gid, expected_gid);
        assert_eq!(loaded["active_sessions"].as_array().unwrap().len(), 1);

        store
            .update(|st| {
                st["games"][0]["favorite"] = json!(true);
                st["games"].as_array_mut().unwrap().push(json!({
                    "name": "Quake",
                    "platform": "PC",
                    "path": rom2.to_string_lossy()
                }));
                Ok(())
            })
            .unwrap();

        // Both Rust and Python check_library() must report healthy (exit_code 0)
        assert_eq!(check_library(dir.path())["exit_code"], json!(0));

        // 3. Python reads Rust-saved library and runs check_library() cleanly!
        let py_verify = format!(
            "import sys, json; sys.path.insert(0, {repo:?}); sys.path.insert(0, {app:?}); \
             from pathlib import Path; from state_store import JsonStateStore; \
             from pkg.state import library_v7; \
             s = JsonStateStore(Path({prim:?})); \
             st = s.load(); \
             assert st['library_id'] == {lib_id:?}, st['library_id']; \
             assert len(st['games']) == 2, len(st['games']); \
             assert st['games'][0]['favorite'] is True; \
             assert st['games'][0]['game_id'] == {gid:?}; \
             assert len(st['active_sessions']) == 1; \
             rep = library_v7.check_library(Path({d:?})); \
             assert rep['exit_code'] == 0, rep; \
             print('OK')",
            repo = repo_root.to_string_lossy(),
            app = repo_root.join("app").to_string_lossy(),
            prim = primary.to_string_lossy(),
            lib_id = py_lib_id,
            gid = py_gid,
            d = dir.path().to_string_lossy(),
        );
        let out2 = std::process::Command::new(python)
            .env("PYTHONUTF8", "1")
            .args(["-B", "-c", &py_verify])
            .output()
            .expect("python3 verify must run");
        assert!(
            out2.status.success(),
            "py verify failed: {}",
            String::from_utf8_lossy(&out2.stderr)
        );
        assert_eq!(String::from_utf8_lossy(&out2.stdout).trim(), "OK");
    }
}
