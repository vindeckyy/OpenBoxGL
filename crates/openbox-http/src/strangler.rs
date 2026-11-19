//! Optional Strangler-Fig reverse proxy (`OPENBOX_STRANGLER_PY=1`) for incremental
//! Phases 3–9 rollout. Forwards not-yet-migrated routes to a private `web_app.py`
//! child sharing `data_dir` while Rust owns security headers, Host/auth/rate-limit,
//! and migrated routes. Disabled (zero Python spawned) once all routes run natively.

use std::io::{BufRead, BufReader};
use std::path::Path;
use std::process::{Child, Command, Stdio};
use std::time::{Duration, Instant};

use axum::body::Body;
use axum::http::header::{CONTENT_LENGTH, HeaderName, HeaderValue};
use axum::http::{HeaderMap, Method, StatusCode};
use axum::response::Response;
use parking_lot::Mutex;

use crate::middleware::json_response;

pub struct StranglerProxy {
    base_url: String,
    py_token: String,
    client: reqwest::Client,
    child: Mutex<Option<Child>>,
}

impl StranglerProxy {
    pub fn spawn_if_enabled(root_dir: &Path, data_dir: &Path) -> Result<Option<Self>, String> {
        if std::env::var("OPENBOX_STRANGLER_PY").as_deref() != Ok("1") {
            return Ok(None);
        }
        let web_app = root_dir.join("web_app.py");
        if !web_app.is_file() {
            return Err(format!(
                "Strangler proxy requested but {} missing",
                web_app.display()
            ));
        }
        let py = openbox_platform::python_command();
        let mut child = Command::new(py)
            .args(["-B", &web_app.to_string_lossy(), "--no-browser"])
            .env("OPENBOX_DATA_DIR", data_dir)
            .env("OPENBOX_NO_EXTERNAL_OPEN", "1")
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .spawn()
            .map_err(|e| format!("Failed to spawn strangler Python backend: {e}"))?;

        let stdout = child
            .stdout
            .take()
            .ok_or_else(|| "Could not capture strangler stdout".to_owned())?;
        let mut reader = BufReader::new(stdout);
        let mut line = String::new();
        let deadline = Instant::now() + Duration::from_secs(20);
        let base_url = loop {
            if Instant::now() > deadline {
                let _ = child.kill();
                return Err("Timed out waiting for strangler Python backend URL".to_owned());
            }
            line.clear();
            match reader.read_line(&mut line) {
                Ok(0) => {
                    let _ = child.kill();
                    return Err("Strangler Python backend exited before printing URL".to_owned());
                }
                Ok(_) => {
                    let trimmed = line.trim();
                    if trimmed.starts_with("http://127.0.0.1:") {
                        break trimmed.trim_end_matches('/').to_owned();
                    }
                }
                Err(err) => {
                    let _ = child.kill();
                    return Err(format!("Failed reading strangler stdout: {err}"));
                }
            }
        };
        let py_token = std::fs::read_to_string(data_dir.join("server.token"))
            .unwrap_or_default()
            .trim()
            .to_owned();
        let client = reqwest::Client::builder()
            .no_proxy()
            .redirect(reqwest::redirect::Policy::none())
            .timeout(Duration::from_secs(30))
            .build()
            .map_err(|e| e.to_string())?;
        Ok(Some(Self {
            base_url,
            py_token,
            client,
            child: Mutex::new(Some(child)),
        }))
    }

    pub async fn forward(
        &self,
        method: &Method,
        path_and_query: &str,
        req_headers: &HeaderMap,
        body: Vec<u8>,
    ) -> Response {
        let url = format!("{}{}", self.base_url, path_and_query);
        let req_method =
            reqwest::Method::from_bytes(method.as_str().as_bytes()).unwrap_or(reqwest::Method::GET);
        let mut builder = self
            .client
            .request(req_method, &url)
            .header("X-OpenBox-Token", &self.py_token);

        for name in [
            "content-type",
            "accept",
            "accept-encoding",
            "if-none-match",
            "if-modified-since",
            "range",
            "x-openbox-api-version",
        ] {
            if let Some(val) = req_headers.get(name).and_then(|v| v.to_str().ok()) {
                builder = builder.header(name, val);
            }
        }
        if !body.is_empty() {
            builder = builder.body(body);
        }
        match builder.send().await {
            Ok(upstream) => {
                let status = StatusCode::from_u16(upstream.status().as_u16())
                    .unwrap_or(StatusCode::BAD_GATEWAY);
                let headers = upstream.headers().clone();
                let bytes = upstream.bytes().await.unwrap_or_default();
                let len = bytes.len();
                let mut resp = Response::new(Body::from(bytes));
                *resp.status_mut() = status;
                for (k, v) in &headers {
                    let lower = k.as_str().to_ascii_lowercase();
                    if matches!(
                        lower.as_str(),
                        "transfer-encoding" | "connection" | "server"
                    ) {
                        continue;
                    }
                    if let (Ok(hn), Ok(hv)) = (
                        HeaderName::from_bytes(k.as_str().as_bytes()),
                        HeaderValue::from_bytes(v.as_bytes()),
                    ) {
                        resp.headers_mut().insert(hn, hv);
                    }
                }
                if let Ok(cl) = HeaderValue::from_str(&len.to_string()) {
                    resp.headers_mut().insert(CONTENT_LENGTH, cl);
                }
                resp
            }
            Err(err) => json_response(
                StatusCode::BAD_GATEWAY,
                &serde_json::json!({
                    "error": format!("Strangler backend unreachable: {err}"),
                    "code": "UPSTREAM_UNAVAILABLE",
                }),
                path_and_query,
            ),
        }
    }

    pub fn shutdown(&self) {
        if let Some(mut child) = self.child.lock().take() {
            let _ = child.kill();
            let _ = child.wait();
        }
    }
}

impl Drop for StranglerProxy {
    fn drop(&mut self) {
        self.shutdown();
    }
}
