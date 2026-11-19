//! Game launch supervision, process tree control (pause/resume/stop/kill),
//! persisted `active_sessions` lifecycle, crash-restart reconciliation, and shutdown reaping.
//! Mirrors `pkg/state/launch.py`.

use std::path::Path;
use std::process::{Command, Stdio};
use std::sync::Arc;
use std::time::Instant;

use openbox_core::{ApiError, SESSION_ABANDONED, SESSION_ACTIVE, SESSION_STOPPING};
use openbox_platform::{
    process_alive, process_command_line, process_group_id, process_start_token,
    resume_process_tree, suspend_process_tree, terminate_process_tree,
};
use openbox_runtime::{AppState, SessionEventParams};
use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};
use uuid::Uuid;

use crate::defs::{load_adapters, resolve_launch};
use crate::gamescope::wrap_launch_command;
use crate::perf::{apply_perf_profile, effective_profile_name, restore_perf_profile};
use crate::tracking::close_store_client;

pub fn cmd_fingerprint(args: &[String]) -> String {
    let joined = args.join("\0");
    hex::encode(&Sha256::digest(joined.as_bytes())[..12])
}

pub fn resolve_game_from_payload(
    state: &Value,
    payload: &Map<String, Value>,
) -> Result<(usize, Value), ApiError> {
    let games = state
        .get("games")
        .and_then(Value::as_array)
        .ok_or_else(|| ApiError::game_not_found("Game not found"))?;
    if let Some(gid) = payload
        .get("game_id")
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|s| !s.is_empty())
    {
        for (idx, g) in games.iter().enumerate() {
            if g.get("game_id").and_then(Value::as_str) == Some(gid) {
                return Ok((idx, g.clone()));
            }
        }
        return Err(ApiError::game_not_found("Game not found"));
    }
    let idx = payload
        .get("id")
        .or_else(|| payload.get("legacy_id"))
        .and_then(|v| {
            v.as_u64()
                .map(|n| n as usize)
                .or_else(|| v.as_str().and_then(|s| s.parse::<usize>().ok()))
        })
        .ok_or_else(|| ApiError::bad_request("missing or invalid game id"))?;
    let game = games
        .get(idx)
        .cloned()
        .ok_or_else(|| ApiError::game_not_found("Game not found"))?;
    Ok((idx, game))
}

pub fn start_game(
    app: &Arc<AppState>,
    index: usize,
    stable_game_id: &str,
) -> Result<Value, ApiError> {
    let state = app
        .load_state()
        .map_err(|e| ApiError::internal(e.to_string()))?;
    let games = state
        .get("games")
        .and_then(Value::as_array)
        .ok_or_else(|| ApiError::game_not_found("Game not found"))?;
    let game = if !stable_game_id.is_empty() {
        games
            .iter()
            .find(|g| g.get("game_id").and_then(Value::as_str) == Some(stable_game_id))
            .cloned()
            .ok_or_else(|| ApiError::game_not_found("Game not found"))?
    } else {
        games
            .get(index)
            .cloned()
            .ok_or_else(|| ApiError::game_not_found("Game not found"))?
    };

    let gid = game
        .get("game_id")
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_owned();
    let game_name = game
        .get("name")
        .and_then(Value::as_str)
        .unwrap_or("Untitled")
        .to_owned();
    let platform = game
        .get("platform")
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_owned();

    // Prevent concurrent duplicate launches of the exact same game
    {
        let reg = app.registry.lock();
        if reg
            .running
            .values()
            .any(|s| s.get("stable_game_id").and_then(Value::as_str) == Some(gid.as_str()))
        {
            return Err(ApiError::conflict("This game is already running."));
        }
    }

    let profiles = state
        .get("profiles")
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default();
    let settings = state.get("settings").cloned().unwrap_or_else(|| json!({}));
    let adapters = load_adapters(&app.root_dir, Some(&app.data_dir));
    let plan = resolve_launch(&game, &profiles, &adapters, &app.data_dir, Some(&state))
        .map_err(ApiError::bad_request)?;
    let final_args = wrap_launch_command(&plan.args, &game, &settings);
    if final_args.is_empty() {
        return Err(ApiError::bad_request("Empty launch command."));
    }

    let profile_name = effective_profile_name(&game, &profiles);
    let perf_applied = apply_perf_profile(&profile_name, &state);

    let mut cmd = Command::new(&final_args[0]);
    cmd.args(&final_args[1..])
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    if !plan.cwd.is_empty() && Path::new(&plan.cwd).is_dir() {
        cmd.current_dir(&plan.cwd);
    }
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        // Put the launched game into its own process group for clean tree stop/pause/resume
        cmd.process_group(0);
    }

    let mut child = cmd
        .spawn()
        .map_err(|e| ApiError::bad_request(format!("Could not launch {game_name}: {e}")))?;

    let pid = child.id() as i32;
    let pgid = process_group_id(pid);
    let start_tok = process_start_token(pid).unwrap_or_default();
    let fp = cmd_fingerprint(&final_args);
    let launch_id = format!("run-{}", &Uuid::new_v4().simple().to_string()[..12]);
    let started_iso = jiff::Timestamp::now().to_string();

    let session_info = json!({
        "launch_id": launch_id,
        "id": index,
        "game_id": gid,
        "stable_game_id": gid,
        "game": game_name,
        "platform": platform,
        "pid": pid,
        "pgid": pgid,
        "proc_start_time": start_tok,
        "cmd_fingerprint": fp,
        "started": started_iso,
        "status": SESSION_ACTIVE,
        "paused": false,
        "precedence": plan.precedence,
    });

    {
        let mut reg = app.registry.lock();
        reg.running.insert(launch_id.clone(), session_info.clone());
    }

    let sess_persist = session_info.clone();
    let gid_for_persist = gid.clone();
    let started_for_persist = started_iso.clone();
    let _ = app.transact_state(move |st| {
        if let Some(arr) = st.get_mut("active_sessions").and_then(Value::as_array_mut) {
            arr.push(sess_persist);
        }
        let first_play = st
            .get("settings")
            .and_then(|s| s.get("progress_on_first_play"))
            .and_then(Value::as_str)
            .unwrap_or("Playing")
            .to_owned();
        if let Some(games_arr) = st.get_mut("games").and_then(Value::as_array_mut)
            && let Some(g) = games_arr.iter_mut().find(|g| {
                g.get("game_id").and_then(Value::as_str) == Some(gid_for_persist.as_str())
            })
            && let Some(obj) = g.as_object_mut()
        {
            let prev = obj.get("play_count").and_then(Value::as_u64).unwrap_or(0);
            obj.insert("play_count".into(), Value::from(prev + 1));
            obj.insert("last_played".into(), Value::String(started_for_persist));
            let progress = obj.get("progress").and_then(Value::as_str).unwrap_or("");
            if (progress.is_empty() || progress == "Unplayed") && !first_play.is_empty() {
                obj.insert("progress".into(), Value::String(first_play));
            }
        }
        Ok(())
    });

    app.sse.session_event(
        &app.registry,
        &app.data_dir,
        SessionEventParams {
            kind: "started",
            launch_id: &launch_id,
            game_name: &game_name,
            exit_code: None,
            seconds: None,
            timed_out: None,
        },
    );
    app.publish_webhook_event(
        "session.started",
        &json!({
            "launch_id": launch_id,
            "game_id": gid,
            "name": game_name,
            "platform": platform,
            "started_at": started_iso,
        }),
    );

    // Spawn supervisor thread that waits for child exit and finalizes session bookkeeping
    let app_sup = Arc::clone(app);
    let lid_sup = launch_id.clone();
    let gid_sup = gid.clone();
    let name_sup = game_name.clone();
    let plat_sup = platform.clone();
    let started_sup = started_iso.clone();
    let started_instant = Instant::now();
    std::thread::Builder::new()
        .name(format!("openbox-session-{lid_sup}"))
        .spawn(move || {
            let exit_code = match child.wait() {
                Ok(status) => status.code().unwrap_or(0),
                Err(_) => -1,
            };
            let seconds = started_instant.elapsed().as_secs() as i64;
            finish_session(
                &app_sup,
                &lid_sup,
                &gid_sup,
                &name_sup,
                &plat_sup,
                &started_sup,
                exit_code,
                seconds,
                false,
                if perf_applied {
                    Some(profile_name.as_str())
                } else {
                    None
                },
            );
        })
        .ok();

    Ok(json!({
        "launch_id": launch_id,
        "pid": pid,
        "game_id": gid,
        "game": game_name,
        "precedence": plan.precedence,
        "warnings": plan.warnings,
    }))
}

#[allow(clippy::too_many_arguments)]
fn finish_session(
    app: &Arc<AppState>,
    launch_id: &str,
    game_id: &str,
    game_name: &str,
    platform: &str,
    started_at: &str,
    exit_code: i32,
    seconds: i64,
    timed_out: bool,
    perf_profile: Option<&str>,
) {
    {
        let mut reg = app.registry.lock();
        reg.running.remove(launch_id);
    }
    let stopped_at = jiff::Timestamp::now().to_string();
    let lid = launch_id.to_owned();
    let gid = game_id.to_owned();
    let gname = game_name.to_owned();
    let plat = platform.to_owned();
    let started = started_at.to_owned();
    let stopped = stopped_at.clone();

    let commit_res = app.transact_state(move |st| {
        if let Some(arr) = st.get_mut("active_sessions").and_then(Value::as_array_mut) {
            arr.retain(|s| s.get("launch_id").and_then(Value::as_str) != Some(lid.as_str()));
        }
        let track_history = st
            .get("settings")
            .and_then(|s| s.get("track_session_history"))
            .and_then(Value::as_bool)
            .unwrap_or(true);
        if track_history && let Some(hist) = st.get_mut("history").and_then(Value::as_array_mut) {
            hist.push(json!({
                "launch_id": lid,
                "game_id": gid,
                "game": gname,
                "platform": plat,
                "started": started,
                "stopped": stopped,
                "seconds": seconds.max(0),
                "exit_code": exit_code,
                "timed_out": timed_out,
            }));
            if hist.len() > 2000 {
                let drop_n = hist.len() - 2000;
                hist.drain(..drop_n);
            }
        }
        if let Some(games) = st.get_mut("games").and_then(Value::as_array_mut)
            && let Some(g) = games
                .iter_mut()
                .find(|g| g.get("game_id").and_then(Value::as_str) == Some(gid.as_str()))
            && let Some(obj) = g.as_object_mut()
        {
            let prev = obj
                .get("playtime_seconds")
                .and_then(Value::as_i64)
                .unwrap_or(0);
            obj.insert(
                "playtime_seconds".into(),
                Value::from(prev + seconds.max(0)),
            );
        }
        Ok(())
    });

    if let Ok((state, _)) = commit_res {
        if let Some(prof) = perf_profile {
            let _ = restore_perf_profile(prof, &state);
        }
        if let Some(game) = state
            .get("games")
            .and_then(Value::as_array)
            .and_then(|arr| {
                arr.iter()
                    .find(|g| g.get("game_id").and_then(Value::as_str) == Some(game_id))
            })
        {
            let settings = state.get("settings").cloned().unwrap_or_else(|| json!({}));
            close_store_client(game, &settings);
            let recap = json!({
                "launch_id": launch_id,
                "game_id": game_id,
                "name": game_name,
                "platform": platform,
                "started_at": started_at,
                "stopped_at": stopped_at,
                "seconds": seconds.max(0),
                "exit_code": exit_code,
                "partial": exit_code != 0,
                "game_missing": false,
                "progress": game.get("progress").and_then(Value::as_str).unwrap_or(""),
                "screenshots_taken": 0,
                "ra": {"tracked": false, "available": false, "baseline": false, "earned": 0, "earned_hardcore": 0, "total": 0, "progress_pct": 0.0, "mastered": false, "earned_delta": 0, "earned_hardcore_delta": 0},
                "resume_state_available": false,
                "capture": {"moment": true, "clip": true},
            });
            app.sse.store_session_recap(recap.clone());
            app.sse.broadcast_event("session.recap", &recap);
        }
    }

    app.sse.session_event(
        &app.registry,
        &app.data_dir,
        SessionEventParams {
            kind: "stopped",
            launch_id,
            game_name,
            exit_code: Some(exit_code),
            seconds: Some(seconds.max(0)),
            timed_out: Some(timed_out),
        },
    );
    app.publish_webhook_event(
        "session.stopped",
        &json!({
            "launch_id": launch_id,
            "game_id": game_id,
            "name": game_name,
            "seconds": seconds.max(0),
            "exit_code": exit_code,
            "timed_out": timed_out,
            "started_at": started_at,
            "stopped_at": stopped_at,
        }),
    );
}

pub fn control_game_session(
    app: &Arc<AppState>,
    launch_id: &str,
    action: &str,
) -> Result<Value, ApiError> {
    let info = {
        let reg = app.registry.lock();
        reg.running.get(launch_id).cloned()
    }
    .ok_or_else(|| ApiError::bad_request("Running session not found."))?;
    let pid = info.get("pid").and_then(Value::as_i64).unwrap_or(0) as i32;
    let pgid = info.get("pgid").and_then(Value::as_i64).map(|x| x as i32);
    let game_name = info
        .get("game")
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_owned();

    match action {
        "pause" => {
            if !suspend_process_tree(pid, pgid) {
                return Err(ApiError::bad_request("Could not pause this game session."));
            }
            if let Some(entry) = app.registry.lock().running.get_mut(launch_id) {
                entry["paused"] = Value::Bool(true);
            }
            app.sse.session_event(
                &app.registry,
                &app.data_dir,
                SessionEventParams {
                    kind: "paused",
                    launch_id,
                    game_name: &game_name,
                    exit_code: None,
                    seconds: None,
                    timed_out: None,
                },
            );
            Ok(json!({"ok": true, "launch_id": launch_id, "action": "pause", "paused": true}))
        }
        "resume" => {
            if !resume_process_tree(pid, pgid) {
                return Err(ApiError::bad_request("Could not resume this game session."));
            }
            if let Some(entry) = app.registry.lock().running.get_mut(launch_id) {
                entry["paused"] = Value::Bool(false);
            }
            app.sse.session_event(
                &app.registry,
                &app.data_dir,
                SessionEventParams {
                    kind: "resumed",
                    launch_id,
                    game_name: &game_name,
                    exit_code: None,
                    seconds: None,
                    timed_out: None,
                },
            );
            Ok(json!({"ok": true, "launch_id": launch_id, "action": "resume", "paused": false}))
        }
        "stop" | "kill" => {
            let force = action == "kill";
            let _ = terminate_process_tree(pid, force, pgid);
            Ok(json!({"ok": true, "launch_id": launch_id, "action": action}))
        }
        _ => Err(ApiError::bad_request(
            "Session action must be pause, resume, stop, or kill.",
        )),
    }
}

/// Reconcile persisted `active_sessions` on server startup: live matching PIDs are reattached,
/// dead or mismatched PIDs are marked `abandoned`.
pub fn reconcile_sessions_on_startup(state: &Value) -> (Vec<Value>, Vec<Value>) {
    let mut reattached = Vec::new();
    let mut abandoned = Vec::new();
    let Some(sessions) = state.get("active_sessions").and_then(Value::as_array) else {
        return (reattached, abandoned);
    };
    for sess in sessions {
        let Some(obj) = sess.as_object() else {
            continue;
        };
        let status = obj.get("status").and_then(Value::as_str).unwrap_or("");
        if status == SESSION_ABANDONED {
            abandoned.push(sess.clone());
            continue;
        }
        let pid = obj.get("pid").and_then(Value::as_i64).unwrap_or(0) as i32;
        let expected_start = obj
            .get("proc_start_time")
            .and_then(|v| {
                v.as_str()
                    .map(str::to_owned)
                    .or_else(|| v.as_f64().map(|f| f.to_string()))
            })
            .unwrap_or_default();
        let expected_fp = obj
            .get("cmd_fingerprint")
            .and_then(Value::as_str)
            .unwrap_or("");
        let alive = pid > 0 && process_alive(pid);
        let same_start = !alive
            || expected_start.is_empty()
            || expected_start == "0"
            || process_start_token(pid)
                .map(|actual| actual == expected_start)
                .unwrap_or(false);
        let same_cmd = !alive || expected_fp.is_empty() || {
            let cmd = process_command_line(pid);
            cmd.is_empty() || {
                let parts: Vec<String> = cmd.split_whitespace().map(str::to_owned).collect();
                cmd_fingerprint(&parts) == expected_fp
            }
        };
        if alive && same_start && same_cmd {
            reattached.push(sess.clone());
        } else {
            let mut ab = obj.clone();
            ab.insert("status".into(), Value::String(SESSION_ABANDONED.to_owned()));
            abandoned.push(Value::Object(ab));
        }
    }
    (reattached, abandoned)
}

/// Graceful shutdown session cleanup matching `web_app.py::reap_sessions_for_shutdown`.
pub fn reap_sessions_for_shutdown(app: &Arc<AppState>) {
    let Ok(state) = app.load_state() else {
        return;
    };
    let active: Vec<Value> = state
        .get("active_sessions")
        .and_then(Value::as_array)
        .map(|arr| {
            arr.iter()
                .filter(|s| s.get("status").and_then(Value::as_str) == Some(SESSION_ACTIVE))
                .cloned()
                .collect()
        })
        .unwrap_or_default();
    if active.is_empty() {
        return;
    }
    let _ = app.transact_state(|st| {
        if let Some(arr) = st.get_mut("active_sessions").and_then(Value::as_array_mut) {
            for s in arr {
                if s.get("status").and_then(Value::as_str) == Some(SESSION_ACTIVE) {
                    s["status"] = Value::String(SESSION_STOPPING.to_owned());
                }
            }
        }
        Ok(())
    });
    let deadline = Instant::now() + std::time::Duration::from_secs(2);
    while Instant::now() < deadline {
        if !active.iter().any(|s| {
            let pid = s.get("pid").and_then(Value::as_i64).unwrap_or(0) as i32;
            pid > 0 && process_alive(pid)
        }) {
            break;
        }
        std::thread::sleep(std::time::Duration::from_millis(150));
    }
    for s in &active {
        let pid = s.get("pid").and_then(Value::as_i64).unwrap_or(0) as i32;
        if pid > 0 && process_alive(pid) {
            let pgid = s.get("pgid").and_then(Value::as_i64).map(|x| x as i32);
            let _ = terminate_process_tree(pid, true, pgid);
        }
    }
    let _ = app.transact_state(|st| {
        if let Some(arr) = st.get_mut("active_sessions").and_then(Value::as_array_mut) {
            arr.retain(|s| s.get("status").and_then(Value::as_str) != Some(SESSION_STOPPING));
        }
        Ok(())
    });
}
