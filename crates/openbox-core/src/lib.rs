//! Core OpenBox domain types, errors, settings registry, state machines, IDs, and lock ordering.

pub mod errors;
pub mod identity;
pub mod locking;
pub mod machines;
pub mod redact;
pub mod settings;
pub mod state_types;
pub mod version;

pub use errors::{ApiError, problem_payload, sanitize_error_message};
pub use identity::{
    identity_payload, is_legacy_indexed_id, is_valid_library_id, new_library_id,
    python_canonical_ascii_json, stable_game_id,
};
pub use locking::{OrderedMutex, OrderedRwLock, lock_level};
pub use machines::{
    MACHINES, Machine, SESSION_ABANDONED, SESSION_ACTIVE, SESSION_STOPPING, job_result_state,
};
pub use redact::{
    EXPORT_EXCLUDED_TOP_LEVEL_KEYS, REDACTED_KEYS, merge_preserved_credentials, redact_settings,
    redact_state_for_export,
};
pub use settings::{KNOWN_SETTINGS, is_known_setting, prune_unknown_settings, sanitize_settings};
pub use state_types::default_state;
pub use version::*;

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::{Map, Value, json};
    use std::path::Path;

    #[test]
    fn golden_game_ids_match_python_byte_for_byte() {
        let fixture_path =
            Path::new(env!("CARGO_MANIFEST_DIR")).join("../../tests/golden/game_ids.json");
        let raw = std::fs::read_to_string(&fixture_path).expect("game_ids.json fixture");
        let doc: Value = serde_json::from_str(&raw).expect("valid json");
        let cases = doc["cases"].as_array().expect("cases array");
        assert!(cases.len() >= 10);

        #[cfg(not(windows))]
        for case in cases {
            let input = case["input"].as_object().expect("input obj");
            let expected_id = case["game_id"].as_str().expect("game_id str");
            let actual_id = stable_game_id(input);
            assert_eq!(actual_id, expected_id, "mismatch for input {input:?}");
        }

        // `tests/golden/game_ids.json` was generated on POSIX (`os.path.normpath`);
        // on Windows both Python `state_store._stable_game_id` and Rust apply
        // `ntpath.normcase(ntpath.normpath(...))`, so verify pathless cases against
        // the golden file AND every case live against Python `state_store._stable_game_id`.
        #[cfg(windows)]
        {
            let repo_root = Path::new(env!("CARGO_MANIFEST_DIR"))
                .parent()
                .unwrap()
                .parent()
                .unwrap();
            let python = ["python", "python3"]
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
            let script = format!(
                "import json, sys; sys.path.insert(0, {repo:?}); sys.path.insert(0, {app:?}); \
                 from state_store import _stable_game_id; \
                 cases = json.load(open({fix:?}, encoding='utf-8'))['cases']; \
                 print(json.dumps([_stable_game_id(c['input']) for c in cases]))",
                repo = repo_root.to_string_lossy(),
                app = repo_root.join("app").to_string_lossy(),
                fix = fixture_path.to_string_lossy(),
            );
            let out = std::process::Command::new(python)
                .env("PYTHONUTF8", "1")
                .args(["-B", "-c", &script])
                .output()
                .expect("python must run");
            assert!(
                out.status.success(),
                "python _stable_game_id failed: {}",
                String::from_utf8_lossy(&out.stderr)
            );
            let py_ids: Vec<String> = serde_json::from_slice(&out.stdout).expect("valid json ids");
            assert_eq!(py_ids.len(), cases.len());
            for (case, py_id) in cases.iter().zip(py_ids.iter()) {
                let input = case["input"].as_object().expect("input obj");
                let actual_id = stable_game_id(input);
                assert_eq!(&actual_id, py_id, "Windows parity mismatch for {input:?}");
                let has_path = input
                    .get("path")
                    .and_then(Value::as_str)
                    .map(|s| !s.trim().is_empty())
                    .unwrap_or(false);
                if !has_path {
                    let expected_id = case["game_id"].as_str().expect("game_id str");
                    assert_eq!(actual_id, expected_id, "pathless mismatch for {input:?}");
                }
            }
        }
    }

    #[test]
    fn golden_settings_and_errors_match_python() {
        let fixture_path = Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../../tests/golden/settings_and_errors.json");
        let raw = std::fs::read_to_string(&fixture_path).expect("settings_and_errors.json");
        let doc: Value = serde_json::from_str(&raw).expect("valid json");

        // All 161 KNOWN_SETTINGS must match Python sorted set 1:1
        let py_settings: Vec<&str> = doc["known_settings"]
            .as_array()
            .expect("known_settings")
            .iter()
            .map(|v| v.as_str().unwrap())
            .collect();
        assert_eq!(KNOWN_SETTINGS, py_settings.as_slice());
        assert_eq!(KNOWN_SETTINGS.len(), 161);

        // Sanitizer parity
        for row in doc["sanitized"].as_array().expect("sanitized") {
            let raw_msg = row["raw"].as_str().unwrap();
            let expected = row["clean"].as_str().unwrap();
            assert_eq!(sanitize_error_message(raw_msg), expected, "raw={raw_msg:?}");
        }

        // RFC 9457 problem document parity
        let errors = [
            ("BadRequest", ApiError::bad_request("Invalid field")),
            ("GameNotFound", ApiError::game_not_found("No such game")),
            ("RouteNotFound", ApiError::route_not_found("Not found")),
            (
                "V1ApiRemoved",
                ApiError::v1_api_removed("Removed")
                    .with_detail(json!({"retired": "/api/v1/x", "use": "/api/v2/x"})),
            ),
            (
                "ApiVersionRequired",
                ApiError::api_version_required("Use v2"),
            ),
            (
                "HostChannelRetired",
                ApiError::host_channel_retired("Use /api/host"),
            ),
            (
                "HostApiUnsupported",
                ApiError::host_api_unsupported("Bad host version"),
            ),
        ];
        for (row, (name, err)) in doc["problems"].as_array().unwrap().iter().zip(errors) {
            assert_eq!(row["name"].as_str().unwrap(), name);
            assert_eq!(row["status"].as_u64().unwrap(), u64::from(err.status));
            assert_eq!(
                err.to_problem("deadbeef", "/api/v2/test"),
                row["payload"],
                "problem payload mismatch for {name}"
            );
        }
    }

    #[test]
    fn ulid_and_machines_invariants() {
        let id = new_library_id();
        assert!(is_valid_library_id(&id), "invalid ULID: {id}");
        assert_eq!(MACHINES.len(), 17);
        let op = &MACHINES["operation"];
        assert!(op.allows("submit", None, Some("queued")));
        assert!(op.allows("start", Some("queued"), Some("running")));
        assert!(!op.allows("start", Some("done"), Some("running")));
    }

    #[test]
    fn redact_and_preserve_credentials_roundtrip() {
        let existing: Map<String, Value> = serde_json::from_value(json!({
            "theme": "Harbor Light",
            "gameyfin_password": "secret-pw",
            "integration_tokens": {"igdb": "tok"},
            "webhooks": [{"id": "w1", "url": "https://example.com", "secret": "whsec"}]
        }))
        .unwrap();
        let redacted = redact_settings(&existing);
        assert!(!redacted.contains_key("gameyfin_password"));
        assert!(!redacted.contains_key("integration_tokens"));
        assert_eq!(redacted["webhooks"][0]["secret_set"], json!(true));
        assert!(redacted["webhooks"][0].get("secret").is_none());

        let restored = merge_preserved_credentials(&redacted, &existing);
        assert_eq!(restored["gameyfin_password"], json!("secret-pw"));
        assert_eq!(restored["webhooks"][0]["secret"], json!("whsec"));
        assert!(restored["webhooks"][0].get("secret_set").is_none());
    }

    #[test]
    #[should_panic(expected = "ADR-0007 lock-ordering violation")]
    fn ordered_lock_rejects_nesting_process_and_state() {
        let proc_lock: OrderedMutex<(), { lock_level::PROCESS }> = OrderedMutex::new(());
        let state_lock: OrderedMutex<(), { lock_level::STATE }> = OrderedMutex::new(());
        let _g1 = proc_lock.lock();
        let _g2 = state_lock.lock();
    }
}
