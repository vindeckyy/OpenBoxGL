//! OBS WebSocket 5.x client for Record That (`pkg/parity/parity_obs_bridge.py`).

use std::time::Duration;

use base64::Engine;
use base64::engine::general_purpose::STANDARD as B64;
use futures_util::{SinkExt, StreamExt};
use openbox_metadata::http::block_on_io;
use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};
use tokio::time::timeout;
use tokio_tungstenite::tungstenite::Message;
use url::Url;
use uuid::Uuid;

pub const DEFAULT_HOST: &str = "127.0.0.1";
pub const DEFAULT_PORT: u16 = 4455;
pub const DEFAULT_TIMEOUT_SECS: f64 = 5.0;
pub const MAX_TIMEOUT_SECS: f64 = 30.0;
pub const RPC_VERSION: u32 = 1;

#[derive(Debug, Clone, thiserror::Error)]
pub enum ObsError {
    #[error("{0}")]
    InvalidConfig(String),
    #[error("{0}")]
    Unavailable(String),
    #[error("{0}")]
    Timeout(String),
    #[error("{0}")]
    Protocol(String),
    #[error("{0}")]
    Authentication(String),
    #[error("{0}")]
    Request(String),
}

pub fn parse_endpoint(
    endpoint: Option<&str>,
    default_host: &str,
    default_port: u16,
) -> Result<(String, u16, String), ObsError> {
    let raw = endpoint.unwrap_or("").trim();
    if raw.is_empty() {
        return Ok((default_host.to_owned(), default_port, "/".to_owned()));
    }
    let normalized = if raw.contains("://") {
        raw.to_owned()
    } else {
        format!("ws://{raw}")
    };
    let parsed = Url::parse(&normalized)
        .map_err(|_| ObsError::InvalidConfig("OBS WebSocket endpoint is invalid.".to_owned()))?;
    if !parsed.scheme().eq_ignore_ascii_case("ws") {
        return Err(ObsError::InvalidConfig(
            "OBS WebSocket endpoint must use ws://.".to_owned(),
        ));
    }
    if !parsed.username().is_empty() || parsed.password().is_some() {
        return Err(ObsError::InvalidConfig(
            "OBS WebSocket credentials belong in settings, not the URL.".to_owned(),
        ));
    }
    if parsed.fragment().is_some() {
        return Err(ObsError::InvalidConfig(
            "OBS WebSocket endpoint must not contain a fragment.".to_owned(),
        ));
    }
    let host = parsed
        .host_str()
        .ok_or_else(|| ObsError::InvalidConfig("OBS WebSocket endpoint has no host.".to_owned()))?
        .to_owned();
    let port = parsed.port().unwrap_or(default_port);
    let mut target = parsed.path().to_owned();
    if target.is_empty() {
        target = "/".to_owned();
    }
    if let Some(q) = parsed.query() {
        target.push('?');
        target.push_str(q);
    }
    Ok((host, port, target))
}

pub fn build_authentication(
    password: Option<&str>,
    salt: &str,
    challenge: &str,
) -> Result<String, ObsError> {
    let Some(pw) = password else {
        return Err(ObsError::Authentication(
            "OBS requires a WebSocket password.".to_owned(),
        ));
    };
    let mut h1 = Sha256::new();
    h1.update(pw.as_bytes());
    h1.update(salt.as_bytes());
    let secret = B64.encode(h1.finalize());

    let mut h2 = Sha256::new();
    h2.update(secret.as_bytes());
    h2.update(challenge.as_bytes());
    Ok(B64.encode(h2.finalize()))
}

pub fn replay_enabled(settings: Option<&Map<String, Value>>) -> bool {
    let Some(map) = settings else {
        return false;
    };
    match map.get("obs_replay_enabled") {
        Some(Value::Bool(b)) => *b,
        Some(Value::String(s)) => {
            matches!(
                s.trim().to_lowercase().as_str(),
                "1" | "true" | "yes" | "on"
            )
        }
        Some(Value::Number(n)) => n.as_f64().is_some_and(|x| x != 0.0),
        _ => false,
    }
}

fn first_str<'a>(settings: &'a Map<String, Value>, keys: &[&str]) -> Option<&'a str> {
    for k in keys {
        if let Some(s) = settings.get(*k).and_then(Value::as_str)
            && !s.is_empty()
        {
            return Some(s);
        }
    }
    None
}

#[derive(Debug, Clone)]
pub struct ObsConfig {
    pub host: String,
    pub port: u16,
    pub target: String,
    pub password: Option<String>,
    pub timeout_secs: f64,
}

impl ObsConfig {
    pub fn from_settings(settings: &Map<String, Value>) -> Result<Self, ObsError> {
        let endpoint = first_str(
            settings,
            &["obs_websocket_url", "obs_ws_url", "obs_url", "obs_endpoint"],
        );
        let host = first_str(settings, &["obs_websocket_host", "obs_ws_host", "obs_host"])
            .unwrap_or(DEFAULT_HOST);
        let port = ["obs_websocket_port", "obs_ws_port", "obs_port"]
            .iter()
            .find_map(|k| settings.get(*k).and_then(Value::as_u64))
            .and_then(|p| u16::try_from(p).ok())
            .filter(|p| *p >= 1)
            .unwrap_or(DEFAULT_PORT);
        let password = first_str(
            settings,
            &["obs_websocket_password", "obs_ws_password", "obs_password"],
        )
        .map(str::to_owned);
        let timeout_secs = ["obs_websocket_timeout", "obs_timeout"]
            .iter()
            .find_map(|k| settings.get(*k).and_then(Value::as_f64))
            .filter(|t| t.is_finite() && *t > 0.0)
            .unwrap_or(DEFAULT_TIMEOUT_SECS)
            .min(MAX_TIMEOUT_SECS);
        let (h, p, t) = parse_endpoint(endpoint, host, port)?;
        Ok(Self {
            host: h,
            port: p,
            target: t,
            password,
            timeout_secs,
        })
    }

    pub fn ws_url(&self) -> String {
        let host_part = if self.host.contains(':') && !self.host.starts_with('[') {
            format!("[{}]", self.host)
        } else {
            self.host.clone()
        };
        format!("ws://{}:{}{}", host_part, self.port, self.target)
    }
}

fn saved_replay_path(val: &Value) -> String {
    if let Some(s) = val.as_str() {
        return s.trim().to_owned();
    }
    let Some(obj) = val.as_object() else {
        return String::new();
    };
    for key in [
        "savedFilename",
        "saved_filename",
        "savedReplayPath",
        "saved_replay_path",
        "filename",
        "path",
    ] {
        if let Some(s) = obj.get(key).and_then(Value::as_str)
            && !s.trim().is_empty()
        {
            return s.trim().to_owned();
        }
    }
    String::new()
}

fn saved_replay_event(msg: &Value) -> Option<Value> {
    if msg.get("op").and_then(Value::as_u64) != Some(5) {
        return None;
    }
    let d = msg.get("d")?.as_object()?;
    if d.get("eventType").and_then(Value::as_str) != Some("ReplayBufferSaved") {
        return None;
    }
    let path = saved_replay_path(d.get("eventData")?);
    if path.is_empty() {
        None
    } else {
        Some(json!({"savedFilename": path}))
    }
}

async fn read_obs_json(
    ws: &mut tokio_tungstenite::WebSocketStream<
        tokio_tungstenite::MaybeTlsStream<tokio::net::TcpStream>,
    >,
    dur: Duration,
) -> Result<Value, ObsError> {
    loop {
        let next = timeout(dur, ws.next())
            .await
            .map_err(|_| ObsError::Timeout("OBS WebSocket operation timed out.".to_owned()))?;
        match next {
            Some(Ok(Message::Text(txt))) => {
                return serde_json::from_str::<Value>(&txt).map_err(|_| {
                    ObsError::Protocol("OBS WebSocket message is not valid JSON.".to_owned())
                });
            }
            Some(Ok(Message::Ping(data))) => {
                let _ = ws.send(Message::Pong(data)).await;
            }
            Some(Ok(Message::Pong(_))) => continue,
            Some(Ok(Message::Close(_))) | None => {
                return Err(ObsError::Unavailable(
                    "OBS WebSocket peer closed the connection.".to_owned(),
                ));
            }
            Some(Ok(_)) => {
                return Err(ObsError::Protocol(
                    "OBS WebSocket message is not JSON text.".to_owned(),
                ));
            }
            Some(Err(e)) => {
                return Err(ObsError::Unavailable(format!("OBS WebSocket error: {e}")));
            }
        }
    }
}

async fn obs_request_async(
    cfg: &ObsConfig,
    request_type: &str,
    request_data: Option<Value>,
    wait_for_replay_event: bool,
) -> Result<Value, ObsError> {
    let dur = Duration::from_secs_f64(cfg.timeout_secs);
    let url = cfg.ws_url();
    let (mut ws, _) = timeout(dur, tokio_tungstenite::connect_async(&url))
        .await
        .map_err(|_| ObsError::Timeout("OBS WebSocket connection timed out.".to_owned()))?
        .map_err(|e| ObsError::Unavailable(format!("OBS WebSocket is unavailable: {e}")))?;

    // 1. Hello (op 0)
    let hello = read_obs_json(&mut ws, dur).await?;
    if hello.get("op").and_then(Value::as_u64) != Some(0) {
        return Err(ObsError::Protocol(
            "OBS WebSocket Hello message is invalid.".to_owned(),
        ));
    }
    let hello_d = hello
        .get("d")
        .and_then(Value::as_object)
        .ok_or_else(|| ObsError::Protocol("OBS WebSocket Hello message is invalid.".to_owned()))?;

    let mut identify = Map::new();
    identify.insert("rpcVersion".into(), json!(RPC_VERSION));
    if let Some(auth) = hello_d.get("authentication").and_then(Value::as_object) {
        let salt = auth.get("salt").and_then(Value::as_str).unwrap_or("");
        let challenge = auth.get("challenge").and_then(Value::as_str).unwrap_or("");
        if salt.is_empty() || challenge.is_empty() {
            return Err(ObsError::Protocol(
                "OBS WebSocket authentication challenge is incomplete.".to_owned(),
            ));
        }
        let resp = build_authentication(cfg.password.as_deref(), salt, challenge)?;
        identify.insert("authentication".into(), Value::String(resp));
    }

    // 2. Identify (op 1) -> Identified (op 2)
    let id_msg = json!({"op": 1, "d": identify});
    ws.send(Message::Text(id_msg.to_string().into()))
        .await
        .map_err(|_| ObsError::Unavailable("OBS WebSocket send failed.".to_owned()))?;
    loop {
        let msg = read_obs_json(&mut ws, dur).await?;
        match msg.get("op").and_then(Value::as_u64) {
            Some(2) => break,
            Some(5) => continue,
            _ => {
                return Err(ObsError::Protocol(
                    "OBS WebSocket Identify was not accepted.".to_owned(),
                ));
            }
        }
    }

    // 3. Request (op 6) -> RequestResponse (op 7)
    let req_id = Uuid::new_v4().to_string();
    let mut d = Map::new();
    d.insert("requestType".into(), json!(request_type));
    d.insert("requestId".into(), json!(req_id));
    if let Some(rd) = request_data {
        d.insert("requestData".into(), rd);
    }
    ws.send(Message::Text(json!({"op": 6, "d": d}).to_string().into()))
        .await
        .map_err(|_| ObsError::Unavailable("OBS WebSocket send failed.".to_owned()))?;

    let mut events = Vec::new();
    let response_data = loop {
        let msg = read_obs_json(&mut ws, dur).await?;
        match msg.get("op").and_then(Value::as_u64) {
            Some(5) => {
                events.push(msg);
                continue;
            }
            Some(7) => {
                let rd = msg.get("d").and_then(Value::as_object).ok_or_else(|| {
                    ObsError::Protocol("OBS request response opcode is invalid.".to_owned())
                })?;
                if rd.get("requestId").and_then(Value::as_str) != Some(req_id.as_str()) {
                    continue;
                }
                let ok = rd
                    .get("requestStatus")
                    .and_then(|s| s.get("result"))
                    .and_then(Value::as_bool)
                    .unwrap_or(false);
                if !ok {
                    let code = rd
                        .get("requestStatus")
                        .and_then(|s| s.get("code"))
                        .map(Value::to_string)
                        .unwrap_or_else(|| "unknown".to_owned());
                    return Err(ObsError::Request(format!(
                        "OBS request {request_type} failed ({code})"
                    )));
                }
                break rd.get("responseData").cloned().unwrap_or_else(|| json!({}));
            }
            _ => {
                return Err(ObsError::Protocol(
                    "OBS request response opcode is invalid.".to_owned(),
                ));
            }
        }
    };

    if wait_for_replay_event {
        if !saved_replay_path(&response_data).is_empty() {
            let _ = ws.close(None).await;
            return Ok(response_data);
        }
        for ev in &events {
            if let Some(found) = saved_replay_event(ev) {
                let _ = ws.close(None).await;
                return Ok(found);
            }
        }
        loop {
            let msg = read_obs_json(&mut ws, dur).await?;
            if let Some(found) = saved_replay_event(&msg) {
                let _ = ws.close(None).await;
                return Ok(found);
            }
        }
    }

    let _ = ws.close(None).await;
    Ok(response_data)
}

pub fn save_replay_buffer(settings: Option<&Map<String, Value>>) -> Option<Value> {
    let map = settings?;
    if !replay_enabled(Some(map)) {
        return None;
    }
    let cfg = ObsConfig::from_settings(map).ok()?;
    block_on_io(async { obs_request_async(&cfg, "SaveReplayBuffer", None, true).await }).ok()
}

pub fn arm_replay_buffer(settings: Option<&Map<String, Value>>) -> Option<Value> {
    let map = settings?;
    if !replay_enabled(Some(map)) {
        return None;
    }
    let cfg = ObsConfig::from_settings(map).ok()?;
    let status =
        block_on_io(async { obs_request_async(&cfg, "GetReplayBufferStatus", None, false).await })
            .ok()?;
    let active = [
        "outputActive",
        "replayBufferActive",
        "replay_buffer_active",
        "active",
    ]
    .iter()
    .any(|k| status.get(*k).and_then(Value::as_bool) == Some(true));
    if active {
        return Some(json!({"armed": true, "owned": false, "status": status}));
    }
    let started =
        block_on_io(async { obs_request_async(&cfg, "StartReplayBuffer", None, false).await })
            .ok()?;
    Some(json!({"armed": true, "owned": true, "status": started}))
}

pub fn disarm_replay_buffer(settings: Option<&Map<String, Value>>) -> Option<Value> {
    let map = settings?;
    if !replay_enabled(Some(map)) {
        return None;
    }
    let cfg = ObsConfig::from_settings(map).ok()?;
    block_on_io(async { obs_request_async(&cfg, "StopReplayBuffer", None, false).await }).ok()
}
