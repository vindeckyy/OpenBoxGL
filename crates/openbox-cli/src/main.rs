//! `openbox-server` entrypoint: headless library CLI, contract dumper, and loopback HTTP server.

mod library_cmd;

use std::path::PathBuf;
use std::sync::Arc;

use openbox_http::{StranglerProxy, bind_loopback, dump_contracts_json, serve};
use openbox_io::{append_diagnostic_line, bootstrap_env};
use openbox_platform::{IS_LINUX, resolved_data_dir};
use openbox_runtime::AppState;

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

    if argv.iter().any(|a| a == "--game-mode") && !IS_LINUX {
        eprintln!("Game Mode needs gamescope and is only available on Linux.");
        std::process::exit(2);
    }

    let data_dir = resolved_data_dir();
    let root_dir = resolve_root_dir();
    let _ = std::fs::create_dir_all(&data_dir);
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
        app.set_sync_hook(Arc::new(
            openbox_sync::time_machine::record_transaction_hook,
        ));

        // Ensure valid state or capture startup errors (matching web_app.py)
        if let Err(err) = app.store.load() {
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
                        app.startup_errors.lock().push(
                            "Library data was restored from the last-known-good backup.".to_owned(),
                        );
                    }
                }
            }
        }

        // Reconcile persisted active_sessions from previous run
        if let Ok(state) = app.load_state() {
            let (reattached, abandoned) = openbox_emulators::reconcile_sessions_on_startup(&state);
            if !reattached.is_empty() || !abandoned.is_empty() {
                let mut combined = reattached.clone();
                combined.extend(abandoned);
                let _ = app.transact_state(move |st| {
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
        }

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
        if let Err(err) = serve_res {
            tracing::error!("{err}");
            std::process::exit(1);
        }
    });
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
