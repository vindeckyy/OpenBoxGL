//! Core runtime services: `AppState`, coordinated caches, SSE hub, job manager,
//! process registry, webhooks, notifications, play queue, commands, and media probing.

pub mod app_state;
pub mod cache;
pub mod commands;
pub mod job_manager;
pub mod media_probe;
pub mod notifications;
pub mod play_queue;
pub mod registry;
pub mod sse;
pub mod webhooks;

pub use app_state::{AppState, PostCommitHook};
pub use cache::{CacheCoordinator, CachedPayload, FACET_DEGRADED, FacetCache, GZIP_THRESHOLD};
pub use commands::{clean_commands, run_configured_commands};
pub use job_manager::{JobContext, JobManager, operation_title_for_name, operation_type_for_name};
pub use media_probe::{
    FIELDS, MEDIA_PATH_FIELDS, MEDIA_ROOTS_ENV, MEDIA_TYPES_ALL, MUSIC_MEDIA_TYPES,
    MediaProbeService, VIDEO_MEDIA_TYPES,
};
pub use notifications::{
    NotificationSpec, SERVER_NOTICE_KEYS, add_notification, clear as clear_notifications,
    mark_read as mark_notifications_read, record_cloud_sync_outcome, unread_count,
};
pub use play_queue::{
    advance, enqueue, normalize_queue, remove as remove_from_queue, reorder, resolve_queue,
};
pub use registry::{ProcessRegistry, SessionInfo};
pub use sse::{
    SSE_BUSY_RETRY_AFTER, SSE_MAX_EVENT_BYTES, SSE_MAX_SUBSCRIBERS, SSE_QUEUE_SIZE,
    SessionEventParams, SseHub, SseMessage, SseSubscription,
};
pub use webhooks::{
    EVENT_TYPES, SIGNATURE_HEADER, WebhookDispatcher, build_event, public_webhook_configs,
    sign_event, test_ping, validate_webhook,
};

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn play_queue_skip_persistence_matches_python() {
        let mut state = json!({
            "games": [
                {"game_id": "g1", "path": ""},
                {"game_id": "g2", "path": "/bin/true"},
            ],
            "queue": [
                {"game_id": "g1", "skip": false},
                {"game_id": "g2", "skip": false},
            ]
        });
        let next = play_queue::advance(&mut state, None).unwrap();
        assert_eq!(next["game_id"], json!("g2"));
        assert_eq!(state["queue"][0]["skip"], json!(true));
        assert!(play_queue::advance(&mut state, Some("g2")).is_none());
    }

    #[test]
    fn notifications_dedupe_and_cap() {
        let mut state = json!({"notifications": []});
        let first = add_notification(
            &mut state,
            NotificationSpec {
                kind: "system",
                level: "info",
                title: "Hello",
                body: "World",
                dedupe_key: "k1",
                ..Default::default()
            },
        );
        let second = add_notification(
            &mut state,
            NotificationSpec {
                kind: "system",
                level: "info",
                title: "Hello 2",
                body: "Dup",
                dedupe_key: "k1",
                ..Default::default()
            },
        );
        assert_eq!(first["id"], second["id"]);
        assert_eq!(unread_count(&state), 1);
        mark_notifications_read(&mut state, None);
        assert_eq!(unread_count(&state), 0);
    }

    #[test]
    fn sse_hub_enforces_subscriber_cap() {
        let hub = std::sync::Arc::new(SseHub::new());
        let mut subs = Vec::new();
        for _ in 0..SSE_MAX_SUBSCRIBERS {
            subs.push(hub.subscribe().expect("within cap"));
        }
        assert!(
            hub.subscribe().is_none(),
            "17th subscriber must be rejected"
        );
        subs.pop();
        assert!(hub.subscribe().is_some(), "dropping frees slot");
    }

    #[test]
    fn facet_cache_computes_and_caches() {
        let fc = FacetCache::default();
        let games = vec![
            json!({"game_id": "1", "genre": "RPG, Action", "platform": "SNES"}),
            json!({"game_id": "2", "genre": "RPG", "platform": "SNES"}),
            json!({"game_id": "3", "genre": "Action", "platform": "PC", "hidden": true}),
        ];
        let res = fc.compute_facets(&games, "genre", 10, Some(500.0));
        assert_eq!(res["code"], json!("OK"));
        assert_eq!(res["facets"][0]["value"], json!("RPG"));
        assert_eq!(res["facets"][0]["count"], json!(2));
        assert_eq!(res["facets"][1]["value"], json!("Action"));
        assert_eq!(res["facets"][1]["count"], json!(1));
    }

    #[test]
    fn webhook_hmac_signature_and_allowlist() {
        let env = build_event(
            "session.started",
            &json!({"launch_id": "l1", "game_id": "g1", "secret_leak": "nope"}),
            Some("evt-1"),
            Some("2026-01-01T00:00:00Z"),
        )
        .unwrap();
        assert!(env["data"].get("secret_leak").is_none());
        let sig = sign_event("mysecret", "2026-01-01T00:00:00Z", b"{\"ok\":true}");
        assert_eq!(sig.len(), 64);
    }

    #[tokio::test]
    async fn app_state_job_submit_and_transact() {
        let dir = tempfile::tempdir().unwrap();
        let app = AppState::new(
            Some(dir.path().to_path_buf()),
            dir.path().to_path_buf(),
            Some("tok123".to_owned()),
        );
        app.transact_state(|st| {
            st["games"].as_array_mut().unwrap().push(json!({
                "name": "Quake",
                "platform": "PC",
                "path": "/bin/true"
            }));
            Ok(())
        })
        .unwrap();
        let job = app
            .jobs
            .submit("library-backup", false, None, None, |_ctx| {
                Ok(json!({"added": 1}))
            })
            .unwrap();
        let jid = job["job_id"].as_str().unwrap().to_owned();
        tokio::time::sleep(std::time::Duration::from_millis(80)).await;
        let op = app.operations.get(&jid).unwrap();
        assert_eq!(op["state"], json!("done"));
    }
}
