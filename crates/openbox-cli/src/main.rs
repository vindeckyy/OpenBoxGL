//! `openbox-server` entrypoint: headless library CLI, contract dumper, and loopback HTTP server.

mod library_cmd;

use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::Duration;

use openbox_http::{StranglerProxy, bind_loopback, dump_contracts_json, serve};
use openbox_integrations::deeplinks::{dispatch_uri, run_keyboard_launcher};
use openbox_integrations::themes::{ensure_stock_themes, write_native_background};
use openbox_integrations::tray_recent::{RECENT_LIMIT, write_recent_games};
use openbox_io::{append_diagnostic_line, bootstrap_env};
use openbox_metadata::http::quote_path;
use openbox_platform::{IS_LINUX, resolved_data_dir};
use openbox_plugins::host::emit_plugin_event;
use openbox_runtime::AppState;
use openbox_sync::backup::{create_backup, restore_backup};

fn resolve_root_dir() -> PathBuf {
    if let Ok(env_root) = std::env::var("OPENBOX_ROOT")
        && !env_root.trim().is_empty()
    {
        return PathBuf::from(env_root);
    }
    if let Ok(cwd) = std::env::current_dir()
        && cwd.join("index.html").is_file()
    {
        return cwd;
    }
    if let Ok(exe) = std::env::current_exe() {
        for ancestor in exe.ancestors().skip(1) {
            if ancestor.join("index.html").is_file() {
                return ancestor.to_path_buf();
            }
        }
    }
    std::env::current_dir().unwrap_or_else(|_| PathBuf::from("."))
}

fn main() {
    let argv: Vec<String> = std::env::args().skip(1).collect();

    if argv.iter().any(|a| a == "--dump-contracts") {
        let payload = dump_contracts_json();
        println!(
            "{}",
            serde_json::to_string_pretty(&payload).unwrap_or_else(|_| "{}".to_owned())
        );
        return;
    }

    if library_cmd::is_library_command(&argv) {
        std::process::exit(library_cmd::run(&argv, None));
    }

    let data_dir = resolved_data_dir();
    let root_dir = resolve_root_dir();

    if let Some(code) = handle_cli_flags(&argv, &data_dir, &root_dir) {
        std::process::exit(code);
    }

    if argv.iter().any(|a| a == "--game-mode") && !IS_LINUX {
        eprintln!("Game Mode needs gamescope and is only available on Linux.");
        std::process::exit(2);
    }

    let _ = std::fs::create_dir_all(&data_dir);
    ensure_stock_themes(&data_dir.join("themes"), &root_dir);
    bootstrap_env(Some(&data_dir));
    append_diagnostic_line(&data_dir, "INFO", "OpenBox Rust server starting");

    for rel in [
        "plugins/data",
        "plugins/credentials",
        "plugin-stats",
        "emulator-configs",
        "cache/steam",
    ] {
        let dir = data_dir.join(rel);
        let _ = std::fs::create_dir_all(&dir);
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let _ = std::fs::set_permissions(&dir, std::fs::Permissions::from_mode(0o700));
        }
    }

    let requested_port: u16 = std::env::var("OPENBOX_PORT")
        .ok()
        .and_then(|s| s.trim().parse().ok())
        .unwrap_or(0);

    // Optional Strangler-Fig reverse proxy starts BEFORE Rust writes server.port/server.token
    // so Rust owns the published discovery files while sharing the exact same data_dir.
    let strangler = match StranglerProxy::spawn_if_enabled(&root_dir, &data_dir) {
        Ok(opt) => opt.map(Arc::new),
        Err(err) => {
            eprintln!("{err}");
            std::process::exit(1);
        }
    };

    let rt = tokio::runtime::Builder::new_multi_thread()
        .enable_all()
        .build()
        .expect("failed to start tokio runtime");

    rt.block_on(async move {
        let app = AppState::new(Some(data_dir.clone()), root_dir, None);
        app.set_sync_hooks(
            Arc::new(openbox_sync::time_machine::capture_transaction_snapshot),
            Arc::new(openbox_sync::time_machine::record_transaction_hook),
        );

        // Bootstrap canonical state (purge dev fixtures + merge emulator profiles from defs)
        let defs_root = app.root_dir.clone();
        let defs_data = app.data_dir.clone();
        let bootstrap = |st: &mut serde_json::Value| -> Result<(), openbox_store::StateError> {
            if let Some(games) = st
                .get_mut("games")
                .and_then(serde_json::Value::as_array_mut)
            {
                games.retain(|g| {
                    let demo = g
                        .get("demo")
                        .and_then(serde_json::Value::as_bool)
                        .unwrap_or(false);
                    let path = g
                        .get("path")
                        .and_then(serde_json::Value::as_str)
                        .unwrap_or("");
                    !demo && !path.contains("/tmp/openbox-screenshots/")
                });
            }
            let adapters = openbox_emulators::load_adapters(&defs_root, Some(&defs_data));
            let existing = st
                .get("profiles")
                .and_then(serde_json::Value::as_object)
                .cloned()
                .unwrap_or_default();
            let merged = openbox_emulators::merge_profiles_from_definitions(&existing, &adapters);
            if merged != existing
                && let Some(obj) = st.as_object_mut()
            {
                obj.insert("profiles".into(), serde_json::Value::Object(merged));
            }
            Ok(())
        };

        if let Err(err) = app.transact_local_state(bootstrap) {
            match &err {
                openbox_store::StateError::FutureVersion(_)
                | openbox_store::StateError::SchemaTooOld(_) => {
                    app.startup_errors.lock().push(err.to_string());
                }
                _ => {
                    app.startup_errors
                        .lock()
                        .push(format!("Library data could not be read: {err}"));
                    if app.store.recover().is_ok() {
                        let _ = app.transact_local_state(bootstrap);
                        app.startup_errors.lock().push(
                            "Library data was restored from the last-known-good backup.".to_owned(),
                        );
                    }
                }
            }
        }

        // Reconcile persisted active_sessions from previous run and refresh tray recent + theme bg
        let reconciled = if let Ok(state) = app.load_state_view() {
            let theme = state
                .get("settings")
                .and_then(|s| s.get("theme"))
                .and_then(serde_json::Value::as_str)
                .unwrap_or("");
            write_native_background(theme, &app.data_dir, &app.root_dir);
            let _ = write_recent_games(&app.data_dir, &state, RECENT_LIMIT);
            Some(openbox_emulators::reconcile_sessions_on_startup(&state))
        } else {
            None
        };
        if let Some((reattached, abandoned)) = reconciled
            && (!reattached.is_empty() || !abandoned.is_empty())
        {
            let mut combined = reattached.clone();
            combined.extend(abandoned);
            let _ = app.transact_local_state(move |st| {
                st["active_sessions"] = serde_json::Value::Array(combined);
                Ok(())
            });
            let mut reg = app.registry.lock();
            for sess in reattached {
                if let Some(lid) = sess.get("launch_id").and_then(serde_json::Value::as_str) {
                    reg.running.insert(lid.to_owned(), sess);
                }
            }
        }

        // Background maintenance scheduler (unlock due time capsules, refresh tray recent)
        let sched_app = Arc::clone(&app);
        tokio::spawn(async move {
            let mut ticker = tokio::time::interval(Duration::from_secs(3600));
            ticker.tick().await; // skip immediate tick; startup just refreshed tray recent
            loop {
                ticker.tick().await;
                let app_ref = Arc::clone(&sched_app);
                let _ = tokio::task::spawn_blocking(move || {
                    if let Ok(state) = app_ref.load_state_view() {
                        let _ = write_recent_games(&app_ref.data_dir, &state, RECENT_LIMIT);
                        let has_locked = state
                            .get("time_capsules")
                            .and_then(serde_json::Value::as_array)
                            .is_some_and(|arr| {
                                arr.iter().any(|c| {
                                    c.get("opened_at")
                                        .and_then(serde_json::Value::as_str)
                                        .is_none_or(str::is_empty)
                                })
                            });
                        if has_locked {
                            let _ = app_ref.transact_state(|st| {
                                let _ = openbox_discovery::playerlog::unlock_due_capsules(
                                    st, None, None,
                                );
                                Ok(())
                            });
                        }
                    }
                })
                .await;
            }
        });

        // Background auto-import job (matching web_app.py JOB_MANAGER.submit("auto-import", ...))
        let auto_app = Arc::clone(&app);
        let _ = app
            .jobs
            .submit("auto-import", false, None, None, move |ctx| {
                let mut delay_secs: u64 = 10;
                let mut idle_delay: u64 = 10;
                while !ctx.is_cancelled() {
                    let steps = (delay_secs * 5).max(1);
                    for _ in 0..steps {
                        if ctx.is_cancelled() {
                            return Ok(serde_json::json!({}));
                        }
                        std::thread::sleep(Duration::from_millis(200));
                    }
                    if ctx.is_cancelled() {
                        break;
                    }
                    let Ok(state) = auto_app.load_state_view() else {
                        delay_secs = (delay_secs * 2).min(300);
                        continue;
                    };
                    let folders: Vec<String> = state
                        .get("settings")
                        .and_then(|s| s.get("watch_folders"))
                        .and_then(serde_json::Value::as_array)
                        .map(|arr| {
                            arr.iter()
                                .filter_map(|v| v.as_str().map(str::to_owned))
                                .collect()
                        })
                        .unwrap_or_default();
                    let mut found = 0usize;
                    for folder in folders {
                        if ctx.is_cancelled() {
                            return Ok(serde_json::json!({}));
                        }
                        if let Ok((added, _, _)) =
                            openbox_imports::import_folder_path(&auto_app, &folder, false, None)
                        {
                            found += added;
                        }
                    }
                    if found > 0 {
                        idle_delay = 10;
                        delay_secs = 10;
                    } else {
                        idle_delay = (idle_delay * 2).min(300);
                        delay_secs = idle_delay;
                    }
                }
                Ok(serde_json::json!({}))
            });

        emit_plugin_event(
            &app.data_dir.join("plugins"),
            "app_startup",
            None,
            None,
            Some(&app.root_dir),
        );

        let running = match bind_loopback(Arc::clone(&app), requested_port, strangler).await {
            Ok(srv) => srv,
            Err(err) => {
                eprintln!("{err}");
                std::process::exit(1);
            }
        };

        // Print tokenless loopback URL to stdout (flushed) for launchers & test harnesses
        let safe_url = format!("http://127.0.0.1:{}/", running.port);
        println!("{safe_url}");
        use std::io::Write;
        let _ = std::io::stdout().flush();
        tracing::info!("OpenBox web UI URL: {safe_url}");

        // Install SIGINT / SIGTERM graceful shutdown listener
        let shutdown_app = Arc::clone(&app);
        tokio::spawn(async move {
            wait_for_signal().await;
            shutdown_app.request_shutdown();
        });

        let serve_res = serve(running).await;
        openbox_emulators::reap_sessions_for_shutdown(&app);
        emit_plugin_event(
            &app.data_dir.join("plugins"),
            "app_shutdown",
            None,
            None,
            Some(&app.root_dir),
        );
        if let Err(err) = serve_res {
            tracing::error!("{err}");
            std::process::exit(1);
        }
    });
}

fn handle_cli_flags(argv: &[String], data_dir: &Path, root_dir: &Path) -> Option<i32> {
    if argv.iter().any(|a| a == "--help" || a == "-h") {
        println!("Usage: openbox-server [OPTIONS]");
        println!("Options:");
        println!("  -h, --help                 Show this help message and exit");
        println!("  --no-browser               Start server without opening a browser window");
        println!("  --game-mode                Force Steam Game Mode / Gamescope guest mode");
        println!("  --play <id>                Launch a game by stable id or numeric id");
        println!("  --uri <uri>                Dispatch an openbox:// deep link URI");
        println!("  --launcher                 Run the keyboard quick launcher (rofi/wofi)");
        println!("  --backup [--items <list>]  Create a backup archive");
        println!("  --restore-backup <file>    Restore data from a backup archive");
        println!(
            "  --dump-contracts           Emit the canonical API/settings/catalog contract JSON"
        );
        return Some(0);
    }
    if let Some(pos) = argv.iter().position(|a| a == "--uri") {
        let Some(uri) = argv.get(pos + 1) else {
            eprintln!("Usage: openbox --uri openbox://search/quake");
            return Some(2);
        };
        return dispatch_uri(uri, data_dir, false);
    }
    if let Some(pos) = argv.iter().position(|a| a == "--play") {
        let Some(gid) = argv
            .get(pos + 1)
            .map(|s| s.trim())
            .filter(|s| !s.is_empty())
        else {
            eprintln!("Usage: openbox --play <game-id>");
            return Some(2);
        };
        let uri = format!("openbox://launch/{}", quote_path(gid));
        return dispatch_uri(&uri, data_dir, false);
    }
    for arg in argv {
        if arg.starts_with("openbox:") {
            return dispatch_uri(arg, data_dir, true);
        }
    }
    if argv.iter().any(|a| a == "--launcher") {
        return Some(run_keyboard_launcher(root_dir));
    }
    if argv.iter().any(|a| a == "--backup") {
        let items: Vec<String> = argv
            .iter()
            .position(|a| a == "--items")
            .and_then(|pos| argv.get(pos + 1))
            .map(|s| {
                s.split(',')
                    .map(|p| p.trim().to_owned())
                    .filter(|p| !p.is_empty())
                    .collect()
            })
            .unwrap_or_else(|| vec!["library".to_owned(), "settings".to_owned()]);
        let app = AppState::new(Some(data_dir.to_path_buf()), root_dir.to_path_buf(), None);
        let state = match app.load_state() {
            Ok(s) => s,
            Err(e) => {
                eprintln!("{e}");
                return Some(1);
            }
        };
        return match create_backup(data_dir, &state, Some(&items), 0, false, 2) {
            Ok(archive) => {
                println!("{}", archive.display());
                Some(0)
            }
            Err(e) => {
                eprintln!("{}", e.message);
                Some(1)
            }
        };
    }
    if let Some(pos) = argv.iter().position(|a| a == "--restore-backup") {
        let Some(archive) = argv.get(pos + 1) else {
            eprintln!("Usage: openbox --restore-backup <file>");
            return Some(2);
        };
        return match restore_backup(Path::new(archive), data_dir, None, false, true) {
            Ok(rep) => {
                println!("{}", serde_json::to_string_pretty(&rep).unwrap_or_default());
                Some(0)
            }
            Err(e) => {
                eprintln!("{}", e.message);
                Some(1)
            }
        };
    }
    None
}

async fn wait_for_signal() {
    #[cfg(unix)]
    {
        use tokio::signal::unix::{SignalKind, signal};
        let mut sigterm = signal(SignalKind::terminate()).ok();
        let mut sigint = signal(SignalKind::interrupt()).ok();
        tokio::select! {
            _ = async {
                if let Some(ref mut s) = sigterm {
                    s.recv().await;
                } else {
                    std::future::pending::<()>().await;
                }
            } => {}
            _ = async {
                if let Some(ref mut s) = sigint {
                    s.recv().await;
                } else {
                    std::future::pending::<()>().await;
                }
            } => {}
        }
    }
    #[cfg(not(unix))]
    {
        let _ = tokio::signal::ctrl_c().await;
    }
}
