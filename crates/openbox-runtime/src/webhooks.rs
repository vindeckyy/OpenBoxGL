//! Signed, bounded, asynchronous webhook delivery for OpenBox automation.
//! Mirrors `automation.py`.

use std::net::{IpAddr, ToSocketAddrs};
use std::sync::Arc;
use std::time::Duration;

use hmac::{Hmac, Mac};
use openbox_io::{PublicDnsResolver, is_public_ip};
use serde_json::{Map, Value, json};
use sha2::Sha256;
use tokio::sync::mpsc;
use uuid::Uuid;

pub const MAX_URL_LENGTH: usize = 2048;
pub const MAX_ENVELOPE_BYTES: usize = 64 * 1024;
pub const DEFAULT_MAX_PENDING: usize = 128;
pub const DEFAULT_ATTEMPTS: u32 = 3;
pub const MAX_ATTEMPTS: u32 = 5;
pub const DEFAULT_TIMEOUT: u64 = 5;
pub const MAX_TIMEOUT: u64 = 15;
pub const MAX_WEBHOOKS: usize = 32;
pub const SOURCE_NAME: &str = "openbox";
pub const ENVELOPE_VERSION: u32 = 1;
pub const SIGNATURE_HEADER: &str = "X-OpenBox-Signature";

const ATTEMPT_DELAYS_SECS: &[f64] = &[1.0, 2.0, 4.0, 8.0];
const MAX_RETRY_AFTER_SECS: f64 = 30.0;

pub const EVENT_TYPES: &[&str] = &[
    "session.started",
    "session.stopped",
    "queue.advanced",
    "library.imported",
    "library.changed",
    "metadata.synced",
    "backup.created",
    "update.installed",
    "plugin.changed",
];

fn allowlist_for(event: &str) -> Option<&'static [&'static str]> {
    Some(match event {
        "session.started" => &["launch_id", "game_id", "name", "platform", "started_at"],
        "session.stopped" => &[
            "launch_id",
            "game_id",
            "name",
            "seconds",
            "exit_code",
            "timed_out",
            "started_at",
            "stopped_at",
        ],
        "queue.advanced" => &["from_game_id", "from_entry_id", "to_game_id", "to_entry_id"],
        "library.imported" => &["source", "found", "added"],
        "library.changed" => &["action", "game_ids", "count"],
        "metadata.synced" => &["status"],
        "backup.created" => &["archive_name", "items"],
        "update.installed" => &["version"],
        "plugin.changed" => &["action", "plugin"],
        _ => return None,
    })
}

pub fn utc_now() -> String {
    jiff::Timestamp::now().to_string()
}

pub fn new_event_id() -> String {
    format!("evt-{}", Uuid::new_v4().simple())
}

pub fn build_event(
    event: &str,
    data: &Value,
    event_id: Option<&str>,
    timestamp: Option<&str>,
) -> Result<Value, String> {
    let Some(allowed) = allowlist_for(event) else {
        return Err(format!("Unknown webhook event type: {event}"));
    };
    let Some(obj) = data.as_object() else {
        return Err("Webhook event data must be an object.".to_owned());
    };
    let mut clean = Map::new();
    for &key in allowed {
        if let Some(val) = obj.get(key) {
            clean.insert(key.to_owned(), val.clone());
        }
    }
    let envelope = json!({
        "id": event_id.map(str::to_owned).unwrap_or_else(new_event_id),
        "type": event,
        "version": ENVELOPE_VERSION,
        "created_at": timestamp.map(str::to_owned).unwrap_or_else(utc_now),
        "source": SOURCE_NAME,
        "data": clean,
    });
    let bytes = serde_json::to_vec(&envelope).map_err(|e| e.to_string())?;
    if bytes.len() > MAX_ENVELOPE_BYTES {
        return Err("Webhook envelope exceeds the 64 KiB size limit.".to_owned());
    }
    Ok(envelope)
}

pub fn sign_event(secret: &str, timestamp: &str, body: &[u8]) -> String {
    let mut mac =
        Hmac::<Sha256>::new_from_slice(secret.as_bytes()).expect("HMAC accepts any key size");
    mac.update(timestamp.as_bytes());
    mac.update(b".");
    mac.update(body);
    hex::encode(mac.finalize().into_bytes())
}

fn is_loopback_host(host: &str) -> bool {
    let lower = host.to_ascii_lowercase();
    if lower == "localhost" || lower == "localhost.localdomain" {
        return true;
    }
    lower
        .trim_matches(|c| c == '[' || c == ']')
        .parse::<IpAddr>()
        .map(|ip| ip.is_loopback())
        .unwrap_or(false)
}

pub fn validate_webhook(config: &Value, openbox_port: Option<u16>) -> Result<(), String> {
    let Some(obj) = config.as_object() else {
        return Err("Webhook configuration must be an object.".to_owned());
    };
    let Some(events) = obj.get("events").and_then(Value::as_array) else {
        return Err("Select at least one webhook event.".to_owned());
    };
    if events.is_empty() {
        return Err("Select at least one webhook event.".to_owned());
    }
    for ev in events {
        let name = ev.as_str().unwrap_or("");
        if !EVENT_TYPES.contains(&name) {
            return Err(format!("Unknown webhook event: {name}"));
        }
    }
    let raw_url = obj
        .get("url")
        .and_then(Value::as_str)
        .map(str::trim)
        .unwrap_or("");
    if raw_url.is_empty() {
        return Err("Webhook URL is required.".to_owned());
    }
    if raw_url.len() > MAX_URL_LENGTH {
        return Err("Webhook URL is too long.".to_owned());
    }
    let parsed = reqwest::Url::parse(raw_url).map_err(|_| "Webhook URL is not a valid URL.")?;
    let scheme = parsed.scheme();
    if scheme != "http" && scheme != "https" {
        return Err("Webhook URL must use http or https.".to_owned());
    }
    if scheme == "http" && std::env::var("OPENBOX_ALLOW_HTTP_WEBHOOKS").as_deref() != Ok("1") {
        return Err(
            "HTTP webhooks are disabled; enable them with OPENBOX_ALLOW_HTTP_WEBHOOKS=1."
                .to_owned(),
        );
    }
    if !parsed.username().is_empty() || parsed.password().is_some() {
        return Err("Webhook URLs may not embed credentials.".to_owned());
    }
    if parsed.fragment().is_some() {
        return Err("Webhook URLs may not contain a fragment.".to_owned());
    }
    let host = parsed
        .host_str()
        .map(|h| h.trim_end_matches('.'))
        .filter(|h| !h.is_empty())
        .ok_or_else(|| "Webhook URL must include a hostname.".to_owned())?;
    let port = parsed.port_or_known_default().unwrap_or(80);
    let addrs: Vec<IpAddr> = (host, port)
        .to_socket_addrs()
        .map_err(|_| "Webhook URL could not be resolved.".to_owned())?
        .map(|sa| sa.ip())
        .collect();
    if addrs.is_empty() {
        return Err("Webhook URL could not be resolved.".to_owned());
    }
    if is_loopback_host(host) {
        if addrs.iter().any(|ip| !ip.is_loopback()) {
            return Err("Webhook loopback hostname resolved outside loopback.".to_owned());
        }
        if let Some(ob_port) = openbox_port
            && ob_port == port
        {
            return Err("Webhook URL may not point at the running OpenBox server.".to_owned());
        }
    } else if addrs.iter().any(|ip| !is_public_ip(*ip)) {
        return Err("Webhook URLs may only target public addresses.".to_owned());
    }
    Ok(())
}

pub fn public_webhook_configs(state: &Value) -> Vec<Value> {
    let Some(arr) = state
        .get("settings")
        .and_then(|s| s.get("webhooks"))
        .and_then(Value::as_array)
    else {
        return Vec::new();
    };
    let mut out = Vec::new();
    for item in arr.iter().take(MAX_WEBHOOKS) {
        let Some(obj) = item.as_object() else {
            continue;
        };
        let mut pub_obj = Map::new();
        for (k, v) in obj {
            if k != "secret" {
                pub_obj.insert(k.clone(), v.clone());
            }
        }
        let secret_set = obj
            .get("secret")
            .and_then(Value::as_str)
            .is_some_and(|s| !s.is_empty());
        pub_obj.insert("secret_set".into(), Value::Bool(secret_set));
        out.push(Value::Object(pub_obj));
    }
    out
}

pub type WebhookResultCallback =
    Arc<dyn Fn(String, String, Option<u16>, String, String, bool) + Send + Sync>;

#[derive(Debug, Clone)]
struct DeliveryItem {
    webhook_id: String,
    url: String,
    secret: String,
    timeout: u64,
    attempts: u32,
    event_id: String,
    event: String,
    body: Vec<u8>,
    timestamp: String,
}

pub struct WebhookDispatcher {
    tx: mpsc::Sender<DeliveryItem>,
}

impl WebhookDispatcher {
    pub fn spawn(on_result: WebhookResultCallback) -> Self {
        let (tx, mut rx) = mpsc::channel::<DeliveryItem>(DEFAULT_MAX_PENDING);
        if let Ok(handle) = tokio::runtime::Handle::try_current() {
            handle.spawn(async move {
                while let Some(item) = rx.recv().await {
                    let cb = Arc::clone(&on_result);
                    tokio::spawn(async move {
                        deliver_item(item, cb).await;
                    });
                }
            });
        }
        Self { tx }
    }

    pub fn enqueue(&self, configs: &[Value], envelope: &Value) -> bool {
        if std::env::var("OPENBOX_SAFE_MODE").is_ok_and(|v| !v.is_empty()) {
            return true;
        }
        let event = envelope
            .get("type")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_owned();
        let event_id = envelope
            .get("id")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_owned();
        let timestamp = envelope
            .get("created_at")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_owned();
        let Ok(body) = serde_json::to_vec(envelope) else {
            return false;
        };
        let mut ok = true;
        for cfg in configs.iter().take(MAX_WEBHOOKS) {
            let Some(obj) = cfg.as_object() else {
                continue;
            };
            if !obj.get("enabled").and_then(Value::as_bool).unwrap_or(false) {
                continue;
            }
            if let Some(evs) = obj.get("events").and_then(Value::as_array)
                && !evs.iter().any(|v| v.as_str() == Some(event.as_str()))
            {
                continue;
            }
            let webhook_id = obj
                .get("id")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_owned();
            let url = obj
                .get("url")
                .and_then(Value::as_str)
                .map(str::trim)
                .unwrap_or("")
                .to_owned();
            if webhook_id.is_empty() || url.is_empty() {
                continue;
            }
            let item = DeliveryItem {
                webhook_id,
                url,
                secret: obj
                    .get("secret")
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .to_owned(),
                timeout: obj
                    .get("timeout")
                    .and_then(Value::as_u64)
                    .unwrap_or(DEFAULT_TIMEOUT)
                    .clamp(1, MAX_TIMEOUT),
                attempts: obj
                    .get("attempts")
                    .and_then(Value::as_u64)
                    .map(|n| n as u32)
                    .unwrap_or(DEFAULT_ATTEMPTS)
                    .clamp(1, MAX_ATTEMPTS),
                event_id: event_id.clone(),
                event: event.clone(),
                body: body.clone(),
                timestamp: timestamp.clone(),
            };
            if self.tx.try_send(item).is_err() {
                ok = false;
            }
        }
        ok
    }
}

fn build_webhook_client(
    allow_loopback: bool,
    timeout_secs: u64,
) -> Result<reqwest::Client, String> {
    let mut b = reqwest::Client::builder()
        .redirect(reqwest::redirect::Policy::none())
        .no_proxy()
        .timeout(Duration::from_secs(timeout_secs))
        .user_agent("OpenBox/1");
    if !allow_loopback {
        b = b.dns_resolver(Arc::new(PublicDnsResolver));
    }
    b.build().map_err(|e| e.to_string())
}

async fn deliver_item(item: DeliveryItem, on_result: WebhookResultCallback) {
    let mut final_status = None;
    let mut final_error = String::new();
    let mut sent_at = String::new();

    for attempt in 0..item.attempts {
        if let Err(err) = validate_webhook(&json!({"url": item.url, "events": [item.event]}), None)
        {
            final_error = err;
            break;
        }
        let allow_loopback = reqwest::Url::parse(&item.url)
            .ok()
            .and_then(|u| u.host_str().map(is_loopback_host))
            .unwrap_or(false);
        let Ok(client) = build_webhook_client(allow_loopback, item.timeout) else {
            final_error = "Could not build HTTP client.".to_owned();
            break;
        };
        let mut req = client
            .post(&item.url)
            .header("Content-Type", "application/json")
            .header("X-OpenBox-Event-Id", &item.event_id)
            .header("X-OpenBox-Event", &item.event)
            .header("X-OpenBox-Timestamp", &item.timestamp)
            .body(item.body.clone());
        if !item.secret.is_empty() {
            req = req.header(
                SIGNATURE_HEADER,
                format!(
                    "sha256={}",
                    sign_event(&item.secret, &item.timestamp, &item.body)
                ),
            );
        }
        match req.send().await {
            Ok(resp) => {
                sent_at = utc_now();
                let status = resp.status().as_u16();
                let retry_after = resp
                    .headers()
                    .get("Retry-After")
                    .and_then(|v| v.to_str().ok())
                    .and_then(|s| s.trim().parse::<f64>().ok())
                    .unwrap_or(0.0);
                if (300..400).contains(&status) {
                    final_status = Some(status);
                    final_error = "Webhook redirects are disabled.".to_owned();
                    break;
                }
                final_status = Some(status);
                if (200..300).contains(&status) {
                    final_error.clear();
                    break;
                }
                final_error = format!("HTTP {status}");
                let retryable = matches!(status, 408 | 425 | 429) || status >= 500;
                if !retryable || attempt + 1 >= item.attempts {
                    break;
                }
                let base_delay = ATTEMPT_DELAYS_SECS
                    .get(attempt as usize)
                    .copied()
                    .unwrap_or(8.0);
                let delay = base_delay.max(retry_after.min(MAX_RETRY_AFTER_SECS));
                tokio::time::sleep(Duration::from_secs_f64(delay)).await;
            }
            Err(err) => {
                final_error = err.to_string();
                if attempt + 1 >= item.attempts {
                    break;
                }
                let delay = ATTEMPT_DELAYS_SECS
                    .get(attempt as usize)
                    .copied()
                    .unwrap_or(8.0);
                tokio::time::sleep(Duration::from_secs_f64(delay)).await;
            }
        }
    }
    on_result(
        item.webhook_id,
        item.event_id,
        final_status,
        final_error,
        sent_at,
        true,
    );
}

pub async fn test_ping(config: &Value, openbox_port: Option<u16>) -> Value {
    if let Err(err) = validate_webhook(config, openbox_port) {
        return json!({"ok": false, "status": null, "error": err});
    }
    let url = config
        .get("url")
        .and_then(Value::as_str)
        .map(str::trim)
        .unwrap_or("");
    let secret = config.get("secret").and_then(Value::as_str).unwrap_or("");
    let timeout = config
        .get("timeout")
        .and_then(Value::as_u64)
        .unwrap_or(DEFAULT_TIMEOUT)
        .clamp(1, MAX_TIMEOUT);
    let envelope = json!({
        "id": new_event_id(),
        "type": "test.ping",
        "version": ENVELOPE_VERSION,
        "created_at": utc_now(),
        "source": SOURCE_NAME,
        "data": {"ok": true},
    });
    let body = serde_json::to_vec(&envelope).unwrap_or_default();
    let allow_loopback = reqwest::Url::parse(url)
        .ok()
        .and_then(|u| u.host_str().map(is_loopback_host))
        .unwrap_or(false);
    let Ok(client) = build_webhook_client(allow_loopback, timeout) else {
        return json!({"ok": false, "status": null, "error": "Could not build HTTP client."});
    };
    let mut req = client
        .post(url)
        .header("Content-Type", "application/json")
        .header("X-OpenBox-Event-Id", envelope["id"].as_str().unwrap_or(""))
        .header("X-OpenBox-Event", "test.ping")
        .header(
            "X-OpenBox-Timestamp",
            envelope["created_at"].as_str().unwrap_or(""),
        )
        .body(body.clone());
    if !secret.is_empty() {
        req = req.header(
            SIGNATURE_HEADER,
            format!(
                "sha256={}",
                sign_event(secret, envelope["created_at"].as_str().unwrap_or(""), &body)
            ),
        );
    }
    match req.send().await {
        Ok(resp) => {
            let status = resp.status().as_u16();
            if (300..400).contains(&status) {
                return json!({"ok": false, "status": status, "error": "Webhook redirects are disabled."});
            }
            json!({
                "ok": status < 300,
                "status": status,
                "error": if status < 300 { String::new() } else { format!("HTTP {status}") },
            })
        }
        Err(err) => json!({"ok": false, "status": null, "error": err.to_string()}),
    }
}
