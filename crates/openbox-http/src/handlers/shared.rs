//! Shared query parsing, JSON body helpers, and request context for HTTP handlers.
//! Mirrors `handlers/_shared.py`.

use std::collections::HashMap;
use std::sync::Arc;

use axum::http::HeaderMap;
use openbox_core::ApiError;
use openbox_runtime::AppState;
use serde_json::{Map, Value};

pub struct RequestCtx<'a> {
    pub app: Arc<AppState>,
    pub path: &'a str,
    pub query: &'a str,
    pub instance: &'a str,
    pub request_id: &'a str,
    pub headers: &'a HeaderMap,
}

impl<'a> RequestCtx<'a> {
    pub fn query_params(&self) -> HashMap<String, Vec<String>> {
        let mut map: HashMap<String, Vec<String>> = HashMap::new();
        if self.query.is_empty() {
            return map;
        }
        for (k, v) in url::form_urlencoded::parse(self.query.as_bytes()) {
            map.entry(k.into_owned()).or_default().push(v.into_owned());
        }
        map
    }

    pub fn query_first(&self, key: &str) -> Option<String> {
        self.query_params()
            .get(key)
            .and_then(|v| v.first())
            .cloned()
    }
}

pub fn parse_json_object(body: &[u8]) -> Result<Map<String, Value>, ApiError> {
    if body.is_empty() {
        return Ok(Map::new());
    }
    let val: Value = serde_json::from_slice(body)
        .map_err(|_| ApiError::bad_request("Request body must be valid JSON."))?;
    match val {
        Value::Object(obj) => Ok(obj),
        _ => Err(ApiError::bad_request("Request body must be a JSON object.")),
    }
}

pub fn parse_limit(raw: Option<&str>, default: usize, maximum: usize) -> Result<usize, ApiError> {
    let Some(s) = raw.filter(|s| !s.is_empty()) else {
        return Ok(default);
    };
    let n: isize = s
        .parse()
        .map_err(|_| ApiError::bad_request("limit must be an integer."))?;
    Ok(n.clamp(1, maximum as isize) as usize)
}

pub fn str_field<'a>(obj: &'a Map<String, Value>, key: &str) -> &'a str {
    obj.get(key)
        .and_then(Value::as_str)
        .map(str::trim)
        .unwrap_or("")
}

pub fn bool_field(obj: &Map<String, Value>, key: &str) -> bool {
    obj.get(key).and_then(Value::as_bool).unwrap_or(false)
}
