//! Saves, Save Vault, Backups (v2), Export, Cloud Sync, Causal Library Sync (v3),
//! Time Machine, Library Health / Repair / Duplicates, and Household sync.

pub mod backup;
pub mod cloud_sync;
pub mod duplicates;
pub mod export;
pub mod household;
pub mod library_health;
pub mod library_sync;
pub mod repair;
pub mod save_tools;
pub mod save_vault;
pub mod saves;
pub mod time_machine;

#[cfg(test)]
mod tests {
    use super::*;
    use openbox_core::default_state;
    use serde_json::json;
    use std::fs;
    use std::io::Write;

    #[test]
    fn backup_v2_roundtrip_v1_read_and_format_retired_410() {
        let dir = tempfile::tempdir().unwrap();
        let data_dir = dir.path();

        let mut state = default_state();
        state["games"] = json!([
            {"game_id": "g1", "name": "Chrono Trigger", "platform": "SNES", "rating": 5}
        ]);
        state["settings"]["gameyfin_password"] = json!("secret-gf-pass");
        state["settings"]["theme"] = json!("Midnight Circuit");

        // Writing format 1 MUST fail with 410 BACKUP_FORMAT_RETIRED
        let err1 = backup::create_backup(data_dir, &state, None, 0, false, 1).unwrap_err();
        assert_eq!(err1.status, 410);
        assert_eq!(err1.code, "BACKUP_FORMAT_RETIRED");

        // Writing format 99 MUST fail with 400 BACKUP_FORMAT_UNSUPPORTED
        let err99 = backup::create_backup(data_dir, &state, None, 0, false, 99).unwrap_err();
        assert_eq!(err99.status, 400);
        assert_eq!(err99.code, "BACKUP_FORMAT_UNSUPPORTED");

        // Format 2 round-trip
        let archive = backup::create_backup(data_dir, &state, None, 4, false, 2).unwrap();
        assert!(archive.is_file());

        // Diff against modified current state
        let mut modified = state.clone();
        modified["games"][0]["rating"] = json!(4);
        let diff = backup::diff_manifests(&modified, &archive, 50).unwrap();
        assert_eq!(diff["summary"]["changed"], 1);

        // Restore preserves local credentials when archive was redacted
        let dest = tempfile::tempdir().unwrap();
        fs::write(
            dest.path().join("settings.json"),
            serde_json::to_string(&json!({"gameyfin_password": "local-secret"})).unwrap(),
        )
        .unwrap();
        let report = backup::restore_backup(&archive, dest.path(), None, false, true).unwrap();
        assert!(
            report["files"]
                .as_array()
                .unwrap()
                .iter()
                .any(|f| f["verified"] == true)
        );
        let restored_settings: serde_json::Value =
            serde_json::from_str(&fs::read_to_string(dest.path().join("settings.json")).unwrap())
                .unwrap();
        assert_eq!(restored_settings["gameyfin_password"], "local-secret");

        // Format 1 archive (no manifest.format / no files list) MUST still restore cleanly
        let v1_path = dir.path().join("legacy-v1.zip");
        {
            let f = fs::File::create(&v1_path).unwrap();
            let mut zw = zip::ZipWriter::new(f);
            let opts = zip::write::SimpleFileOptions::default();
            zw.start_file("manifest.json", opts).unwrap();
            zw.write_all(br#"{"items":["library"],"created":"2026-01-01T00:00:00"}"#)
                .unwrap();
            zw.start_file("library.json", opts).unwrap();
            zw.write_all(serde_json::to_string(&state).unwrap().as_bytes())
                .unwrap();
            zw.finish().unwrap();
        }
        let v1_dest = tempfile::tempdir().unwrap();
        let v1_rep = backup::restore_backup(&v1_path, v1_dest.path(), None, false, true).unwrap();
        assert_eq!(v1_rep["items"], json!(["library"]));
    }

    #[test]
    fn save_vault_snapshot_diff_and_restore_with_safety_snapshot() {
        let dir = tempfile::tempdir().unwrap();
        let data_dir = dir.path().join("data");
        let save_dir = dir.path().join("saves");
        fs::create_dir_all(&save_dir).unwrap();
        let save_file = save_dir.join("slot1.srm");
        fs::write(&save_file, b"original-save-bytes").unwrap();

        let game = json!({
            "game_id": "game-ct",
            "name": "Chrono Trigger",
            "platform": "SNES",
            "save_paths": [save_dir.to_string_lossy()],
        });

        let snap = save_vault::create_snapshot(
            &data_dir,
            &game,
            "manual",
            Some(true),
            None,
            Some(5),
            None,
        )
        .unwrap();
        let snap_id = snap["snapshot_id"].as_str().unwrap().to_owned();
        assert_eq!(snap["files"], 1);

        // Mutate live save
        fs::write(&save_file, b"corrupted-save-bytes").unwrap();
        let diff = save_vault::diff_snapshot(&data_dir, &game, &snap_id).unwrap();
        assert_eq!(diff["counts"]["changed"], 1);

        // Restore must create a pre-restore safety snapshot and restore original bytes
        let res =
            save_vault::restore_snapshot(&data_dir, &game, &snap_id, false, false, false, None)
                .unwrap();
        assert!(res["safety_snapshot_id"].as_str().is_some());
        assert_eq!(res["counts"]["restored"], 1);
        assert_eq!(fs::read(&save_file).unwrap(), b"original-save-bytes");

        // Index now contains original + pre-restore safety snapshot
        let listed = save_vault::list_snapshots(&data_dir, "game-ct");
        assert_eq!(listed.len(), 2);
    }

    #[test]
    fn causal_sync_v3_and_time_machine_end_to_end() {
        let dir = tempfile::tempdir().unwrap();
        let sync_dir = dir.path().join("cloud");
        fs::create_dir_all(&sync_dir).unwrap();

        let mut dev_a = default_state();
        dev_a["games"] = json!([
            {"game_id": "g1", "name": "Chrono Trigger", "platform": "SNES", "genre": "RPG"}
        ]);
        let boot = library_sync::bootstrap_local_catalog(
            &mut dev_a,
            Some("2026-01-01T10:00:00+00:00"),
            Some("dev-a"),
        );
        assert_eq!(boot["changed"], 1);

        // Publish from A to shared folder
        let pub_res =
            library_sync::publish_outbox(&mut dev_a, &sync_dir.to_string_lossy()).unwrap();
        assert_eq!(pub_res["published"], 1);

        // Device B pulls from shared folder
        let mut dev_b = default_state();
        let incoming = library_sync::read_events(&sync_dir.to_string_lossy()).unwrap();
        assert_eq!(incoming.len(), 1);
        let plan = library_sync::preview_sync(&dev_b, &incoming, None).unwrap();
        assert_eq!(plan["counts"]["additions"], 1);
        let applied = library_sync::apply_sync(&mut dev_b, &plan, None, None).unwrap();
        assert_eq!(applied["applied"], 1);
        assert_eq!(dev_b["games"][0]["name"], "Chrono Trigger");

        // Time Machine: edit on A, then plan + apply revert
        let before = dev_a["games"].as_array().unwrap().clone();
        dev_a["games"][0]["genre"] = json!("JRPG");
        let after = dev_a["games"].as_array().unwrap().clone();
        let rec = library_sync::record_local_changes(
            &mut dev_a,
            &before,
            &after,
            Some("2026-01-02T10:00:00+00:00"),
            Some("dev-a"),
        )
        .unwrap();
        let edit_eid = rec["events"][0]["event_id"].as_str().unwrap().to_owned();

        let tl = time_machine::list_events(&dev_a, None, None, Some("g1"), 0, Some(50)).unwrap();
        assert_eq!(tl["total"], 2);

        let rev_plan =
            time_machine::plan_revert(&dev_a, &edit_eid, Some("g1"), None, true).unwrap();
        assert_eq!(rev_plan["noop"], false);
        let rev_res = time_machine::apply_revert(&mut dev_a, &rev_plan).unwrap();
        assert_eq!(rev_res["changed"], true);
        assert_eq!(dev_a["games"][0]["genre"], "RPG");
    }

    #[test]
    fn household_records_challenges_and_leaderboard_opt_in() {
        let dir = tempfile::tempdir().unwrap();
        let sync_dir = dir.path().join("cloud");
        fs::create_dir_all(&sync_dir).unwrap();

        let mut state = default_state();
        state["settings"]["household_stats_sharing"] = json!(true);
        household::record_member(&mut state, "alice", "Alice", "#ff0000", true).unwrap();
        household::record_challenge(
            &mut state,
            "ch1",
            "Beat 2 RPGs",
            Some("alice"),
            "completions",
            2,
            "",
            None,
            None,
            &["alice".to_owned()],
            "open",
        )
        .unwrap();
        household::record_challenge_result(&mut state, "ch1", "alice", 2, true, None, None, None)
            .unwrap();
        household::record_stats_share(
            &mut state,
            "alice",
            "all_time",
            Some(&json!({"completions": 5, "playtime_seconds": 3600})),
            None,
            None,
        )
        .unwrap();

        let recs = household::household_records(&state, false).unwrap();
        let ch_rec = recs.iter().find(|r| r["kind"] == "challenge").unwrap();
        let prog = household::challenge_progress(ch_rec, &recs, Some("alice")).unwrap();
        assert_eq!(prog["completed"], true);
        assert_eq!(prog["winner"]["member_id"], "alice");

        let lb = household::compute_leaderboard(&state, Some("all_time"), 10).unwrap();
        assert_eq!(lb["available"], true);
        assert_eq!(lb["entries"][0]["member_id"], "alice");
        assert_eq!(lb["entries"][0]["rank"], 1);

        // Publish & pull round-trip
        let pub_out =
            household::publish_household_outbox(&mut state, &sync_dir.to_string_lossy()).unwrap();
        assert_eq!(pub_out["published"], 4);
        let mut peer = default_state();
        let pull_out =
            household::pull_household_records(&mut peer, &sync_dir.to_string_lossy()).unwrap();
        assert_eq!(pull_out["applied"], 4);
    }

    #[test]
    fn health_repair_duplicates_and_export() {
        let dir = tempfile::tempdir().unwrap();
        let rom_dir = dir.path().join("roms");
        fs::create_dir_all(&rom_dir).unwrap();
        let rom_path = rom_dir.join("mario.nes");
        fs::write(&rom_path, b"nes").unwrap();

        let mut state = default_state();
        state["games"] = json!([
            {"game_id": "g1", "name": "Super Mario Bros.", "platform": "NES", "path": "/missing/mario.nes", "playtime_seconds": 600},
            {"game_id": "g2", "name": "Super Mario Bros. (USA)", "platform": "NES", "path": "/missing/other.nes", "cover": "/img/c.png"}
        ]);

        let score = library_health::score_library(state["games"].as_array().unwrap(), &state, None);
        assert!(score["score"].as_i64().unwrap() < 100);

        let missing = repair::scan_missing_paths(&state, false, 100, None);
        assert_eq!(missing["count"], 2);
        let cands = repair::scan_candidates(&rom_dir.to_string_lossy(), 100, 4).unwrap();
        let plan = repair::plan_repair(missing["items"].as_array().unwrap(), &cands, None);
        assert_eq!(plan["counts"]["matched"], 1);
        let rep =
            repair::apply_repair(&mut state, plan["matches"].as_array().unwrap(), None).unwrap();
        assert_eq!(rep["updated"], 1);

        let dups = duplicates::find_duplicates(&state, 50, true);
        assert_eq!(dups["count"], 1);
        let merged = duplicates::apply_merge(&mut state, &[0, 1], None).unwrap();
        assert_eq!(merged["merged"], 1);
        assert_eq!(state["games"].as_array().unwrap().len(), 1);
        assert_eq!(state["games"][0]["cover"], "/img/c.png");

        let rows = export::export_rows(state["games"].as_array().unwrap(), false);
        let csv_path = export::write_export(dir.path(), &rows, "csv", false).unwrap();
        assert!(csv_path.is_file());
        assert_eq!(export::list_exports(dir.path()).len(), 1);
    }
}
