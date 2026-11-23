//! Emulator registry (all 44 YAML definitions), launch tokens, signed Ed25519 definition pack
//! updates, emulator settings, Launch Doctor, Launch Audit, Flatpak grants, Gamescope, Perf,
//! Wine, Faugus, Tracking, Netplay, Highscores, Arcade kiosk PIN, and Launch Supervision engine.

pub mod arcade;
pub mod audit;
pub mod defs;
pub mod defs_update;
pub mod doctor;
pub mod emulators;
pub mod faugus;
pub mod flatpak_grant;
pub mod gamescope;
pub mod highscores;
pub mod launch_engine;
pub mod netplay;
pub mod perf;
pub mod settings;
pub mod tokens;
pub mod tracking;
pub mod wine;

pub use defs::{
    AMBIGUOUS_DISC_EXTENSIONS, Adapter, LaunchPlan, RETROARCH_CORE_CHOICE_KEY,
    RETROARCH_CORE_TOKEN, RETROARCH_FLATPAK_APP_ID, StateConfig, adapter_executable,
    apply_core_override, build_adapter_argv, build_emulators_map, build_platform_by_extension,
    build_platform_emulators, definition_search_dirs, detect_adapter_for_platform,
    detect_adapter_prefix, find_adapter, flatpak_core_path, installed_retroarch_cores,
    list_scan_configs, load_adapters, load_adapters_from_dirs, load_definitions, load_registry,
    merge_profiles_from_definitions, normalize_adapter, parse_yaml_def,
    platform_options_for_extension, resolve_launch, resolved_startup_args, save_scan_config,
    scan_folder, valid_core_name,
};
pub use launch_engine::{
    cmd_fingerprint, control_game_session, reap_sessions_for_shutdown,
    reconcile_sessions_on_startup, resolve_game_from_payload, start_game,
    start_game_with_extra_args,
};

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use std::path::Path;

    #[test]
    fn loads_all_44_bundled_emulator_defs_cleanly() {
        let repo_root = Path::new(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .unwrap()
            .parent()
            .unwrap();
        let adapters = load_adapters(repo_root, None);
        assert_eq!(adapters.len(), 44, "all 44 YAML defs must parse cleanly");
        let reg = load_registry(repo_root, None, false);
        assert_eq!(reg["adapters"].as_array().unwrap().len(), 44);

        // Verify MAME -rompath rewriting on a .zip ROM
        let mame = adapters
            .iter()
            .find(|a| a.adapter_id.to_ascii_lowercase().contains("mame"))
            .expect("mame adapter present");
        let argv = build_adapter_argv(
            mame,
            &json!({"name": "Pac-Man", "platform": "Arcade"}),
            "/roms/arcade/pacman.zip",
            Some(vec!["/usr/bin/mame".to_owned()]),
            "/tmp",
        )
        .unwrap();
        assert!(argv.contains(&"-rompath".to_owned()));
        assert!(argv.contains(&"/roms/arcade".to_owned()));
        assert_eq!(argv.last().unwrap(), "pacman");
    }

    #[test]
    fn single_pass_launch_token_substitution_does_not_reexpand() {
        let game = json!({
            "name": "{path}",
            "platform": "SNES",
            "path": "/roms/{name}.sfc"
        });
        let out = tokens::apply_tokens("{path} --title {name}", &game, None, "", "");
        assert_eq!(out, "/roms/{name}.sfc --title {path}");
        assert_eq!(
            tokens::find_invalid_tokens("{path} {unknown_tok}"),
            vec!["{unknown_tok}".to_owned()]
        );
    }

    #[test]
    fn kiosk_pin_pbkdf2_matches_python_byte_for_byte() {
        let salt = b"0123456789abcdef";
        let rust_hash = arcade::hash_kiosk_pin("4242", Some(salt), 120_000).unwrap();
        assert!(arcade::verify_kiosk_pin(&rust_hash, "4242"));
        assert!(!arcade::verify_kiosk_pin(&rust_hash, "1234"));

        // Verify exact compatibility with Python handlers/arcade.py!
        let repo_root = Path::new(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .unwrap()
            .parent()
            .unwrap();
        let py = format!(
            "import sys; sys.path.insert(0, {repo:?}); sys.path.insert(0, {app:?}); \
             from handlers.arcade import hash_kiosk_pin, verify_kiosk_pin; \
             h = hash_kiosk_pin('4242', salt=b'0123456789abcdef', iterations=120000); \
             assert h == {rh:?}, (h, {rh:?}); \
             assert verify_kiosk_pin({rh:?}, '4242'); \
             print('OK')",
            repo = repo_root.to_string_lossy(),
            app = repo_root.join("app").to_string_lossy(),
            rh = rust_hash,
        );
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
        let out = std::process::Command::new(python)
            .env("PYTHONUTF8", "1")
            .args(["-B", "-c", &py])
            .output()
            .unwrap();
        assert!(
            out.status.success(),
            "Python kiosk PIN mismatch: {}",
            String::from_utf8_lossy(&out.stderr)
        );
    }

    #[test]
    fn launch_doctor_and_audit_group_missing_paths() {
        let repo_root = Path::new(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .unwrap()
            .parent()
            .unwrap();
        let dir = tempfile::tempdir().unwrap();
        let adapters = load_adapters(repo_root, None);
        let state = json!({
            "games": [
                {"game_id": "g1", "name": "Missing ROM", "platform": "SNES", "path": "/nonexistent/missing.sfc"},
            ],
            "profiles": {},
        });
        let report = audit::audit_library(
            state["games"].as_array().unwrap(),
            state["profiles"].as_object().unwrap(),
            &adapters,
            dir.path(),
            false,
        );
        assert_eq!(report["totals"]["blocked"], json!(1));
        audit::store_audit(dir.path(), &report).unwrap();
        let loaded = audit::load_audit(dir.path()).unwrap();
        assert_eq!(loaded["game_count"], json!(1));
    }

    #[test]
    fn launch_engine_starts_tracks_and_stops_real_process() {
        let dir = tempfile::tempdir().unwrap();
        #[cfg(windows)]
        let script = {
            let p = dir.path().join("fake_game.cmd");
            std::fs::write(&p, "@echo off\r\nping -n 10 127.0.0.1 >nul\r\n").unwrap();
            p
        };
        #[cfg(not(windows))]
        let script = {
            let p = dir.path().join("fake_game.sh");
            std::fs::write(&p, "#!/bin/sh\nsleep 2\n").unwrap();
            #[cfg(unix)]
            {
                use std::os::unix::fs::PermissionsExt;
                std::fs::set_permissions(&p, std::fs::Permissions::from_mode(0o755)).unwrap();
            }
            p
        };
        let app = openbox_runtime::AppState::new(
            Some(dir.path().join("data")),
            dir.path().to_path_buf(),
            Some("tok".to_owned()),
        );
        let mut st = openbox_core::default_state();
        st["games"].as_array_mut().unwrap().push(json!({
            "name": "Fake Game",
            "platform": "PC",
            "path": script.to_string_lossy(),
        }));
        app.store.save(&st).unwrap();
        let loaded = app.load_state().unwrap();
        let gid = loaded["games"][0]["game_id"].as_str().unwrap().to_owned();

        let launched = start_game(&app, 0, &gid).unwrap();
        let lid = launched["launch_id"].as_str().unwrap().to_owned();
        assert!(!app.registry.is_empty());

        let ctrl = control_game_session(&app, &lid, "kill").unwrap();
        assert_eq!(ctrl["ok"], json!(true));
        for _ in 0..200 {
            if app.registry.is_empty() {
                break;
            }
            std::thread::sleep(std::time::Duration::from_millis(50));
        }
        assert!(app.registry.is_empty());
        let after = app.load_state().unwrap();
        assert_eq!(after["history"].as_array().unwrap().len(), 1);
    }
}
