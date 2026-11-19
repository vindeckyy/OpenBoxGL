//! Shared synchronous HTTP client bridge over `openbox-io`'s SSRF-safe async client,
//! URL query encoding, and exact Python `difflib.SequenceMatcher::ratio()` port.

use std::collections::HashMap;
use std::fs;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::LazyLock;

use futures_util::StreamExt;
use openbox_io::{
    DownloadOptions, assert_public_url, build_public_http_client, download_file, fsync_directory,
    set_mode,
};
use percent_encoding::{NON_ALPHANUMERIC, utf8_percent_encode};
use serde_json::Value;
use sha2::{Digest, Sha256};
use tempfile::Builder;

/// Dedicated background Tokio runtime for sync worker/domain code so calling from either
/// an async Axum task (`spawn_blocking`) or a plain `std::thread` never panics.
static IO_RUNTIME: LazyLock<tokio::runtime::Runtime> = LazyLock::new(|| {
    tokio::runtime::Builder::new_multi_thread()
        .worker_threads(2)
        .thread_name("openbox-meta-io")
        .enable_all()
        .build()
        .expect("openbox-metadata tokio runtime")
});

pub fn block_on_io<F, T>(fut: F) -> T
where
    F: std::future::Future<Output = T> + Send,
    T: Send,
{
    if tokio::runtime::Handle::try_current().is_ok() {
        std::thread::scope(|s| {
            s.spawn(|| IO_RUNTIME.block_on(fut))
                .join()
                .expect("io helper thread panicked")
        })
    } else {
        IO_RUNTIME.block_on(fut)
    }
}

/// Form-encode query params matching Python's `urllib.parse.urlencode`.
pub fn urlencode(params: &[(&str, String)]) -> String {
    params
        .iter()
        .map(|(k, v)| format!("{}={}", quote_plus(k), quote_plus(v),))
        .collect::<Vec<_>>()
        .join("&")
}

pub fn quote_plus(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    for b in s.bytes() {
        match b {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'_' | b'.' => {
                out.push(b as char);
            }
            b' ' => out.push('+'),
            other => {
                out.push('%');
                out.push_str(&format!("{other:02X}"));
            }
        }
    }
    out
}

pub fn quote_path(s: &str) -> String {
    utf8_percent_encode(s, NON_ALPHANUMERIC).to_string()
}

/// Perform a bounded GET returning `(status, content_type, retry_after, body_bytes)`.
pub struct HttpResponse {
    pub status: u16,
    pub content_type: String,
    pub retry_after: String,
    pub body: Vec<u8>,
}

pub fn http_get_raw(
    url: &str,
    headers: &[(&str, String)],
    timeout_secs: u64,
    max_bytes: usize,
) -> Result<HttpResponse, String> {
    let url_s = url.to_owned();
    let hdrs: Vec<(String, String)> = headers
        .iter()
        .map(|(k, v)| ((*k).to_owned(), v.clone()))
        .collect();
    block_on_io(async move {
        let checked = assert_public_url(&url_s).map_err(|e| e.to_string())?;
        let client = build_public_http_client(timeout_secs).map_err(|e| e.to_string())?;
        let mut req = client.get(checked);
        for (k, v) in &hdrs {
            req = req.header(k.as_str(), v.as_str());
        }
        let resp = req.send().await.map_err(|e| e.to_string())?;
        let status = resp.status().as_u16();
        let content_type = resp
            .headers()
            .get(reqwest::header::CONTENT_TYPE)
            .and_then(|v| v.to_str().ok())
            .unwrap_or("")
            .split(';')
            .next()
            .unwrap_or("")
            .trim()
            .to_ascii_lowercase();
        let retry_after = resp
            .headers()
            .get(reqwest::header::RETRY_AFTER)
            .and_then(|v| v.to_str().ok())
            .unwrap_or("")
            .trim()
            .to_owned();
        let mut buf = Vec::new();
        let mut stream = resp.bytes_stream();
        while let Some(chunk) = stream.next().await {
            let bytes = chunk.map_err(|e| e.to_string())?;
            if buf.len().saturating_add(bytes.len()) > max_bytes {
                return Err("Response exceeded maximum allowed size.".to_owned());
            }
            buf.extend_from_slice(&bytes);
        }
        Ok(HttpResponse {
            status,
            content_type,
            retry_after,
            body: buf,
        })
    })
}

pub fn http_post_raw(
    url: &str,
    body: Vec<u8>,
    headers: &[(&str, String)],
    timeout_secs: u64,
    max_bytes: usize,
) -> Result<HttpResponse, String> {
    let url_s = url.to_owned();
    let hdrs: Vec<(String, String)> = headers
        .iter()
        .map(|(k, v)| ((*k).to_owned(), v.clone()))
        .collect();
    block_on_io(async move {
        let checked = assert_public_url(&url_s).map_err(|e| e.to_string())?;
        let client = build_public_http_client(timeout_secs).map_err(|e| e.to_string())?;
        let mut req = client.post(checked).body(body);
        for (k, v) in &hdrs {
            req = req.header(k.as_str(), v.as_str());
        }
        let resp = req.send().await.map_err(|e| e.to_string())?;
        let status = resp.status().as_u16();
        let content_type = resp
            .headers()
            .get(reqwest::header::CONTENT_TYPE)
            .and_then(|v| v.to_str().ok())
            .unwrap_or("")
            .split(';')
            .next()
            .unwrap_or("")
            .trim()
            .to_ascii_lowercase();
        let retry_after = resp
            .headers()
            .get(reqwest::header::RETRY_AFTER)
            .and_then(|v| v.to_str().ok())
            .unwrap_or("")
            .trim()
            .to_owned();
        let mut buf = Vec::new();
        let mut stream = resp.bytes_stream();
        while let Some(chunk) = stream.next().await {
            let bytes = chunk.map_err(|e| e.to_string())?;
            if buf.len().saturating_add(bytes.len()) > max_bytes {
                return Err("Response exceeded maximum allowed size.".to_owned());
            }
            buf.extend_from_slice(&bytes);
        }
        Ok(HttpResponse {
            status,
            content_type,
            retry_after,
            body: buf,
        })
    })
}

pub fn http_get_json(
    url: &str,
    headers: &[(&str, String)],
    timeout_secs: u64,
    max_bytes: usize,
) -> Result<Value, String> {
    let resp = http_get_raw(url, headers, timeout_secs, max_bytes)?;
    if resp.status >= 400 {
        return Err(format!("HTTP {}", resp.status));
    }
    serde_json::from_slice(&resp.body).map_err(|e| format!("Invalid JSON response: {e}"))
}

pub fn download_file_sync(
    url: &str,
    destination: &Path,
    expected_types: &[&str],
    max_bytes: usize,
    timeout_secs: u64,
    extra_headers: &[(&str, String)],
) -> Result<PathBuf, String> {
    let url_s = url.to_owned();
    let dest = destination.to_path_buf();
    let types: Vec<String> = expected_types.iter().map(|s| (*s).to_owned()).collect();
    let hdrs: Vec<(String, String)> = extra_headers
        .iter()
        .map(|(k, v)| ((*k).to_owned(), v.clone()))
        .collect();
    block_on_io(async move {
        let type_refs: Vec<&str> = types.iter().map(String::as_str).collect();
        let hdr_refs: Vec<(&str, &str)> =
            hdrs.iter().map(|(k, v)| (k.as_str(), v.as_str())).collect();
        let opts = DownloadOptions {
            expected_types: &type_refs,
            max_bytes,
            timeout_secs,
            extra_headers: &hdr_refs,
            expected_sha256: None,
            mode: 0o644,
        };
        download_file(&url_s, &dest, &opts).await
    })
}

/// Mirror `pkg/parity/parity_premium.py::download_bytes`.
pub fn download_bytes_sync(url: &str, destination: &Path) -> Result<String, String> {
    download_file_sync(url, destination, &[], 512 * 1024 * 1024, 20, &[])
        .map(|p| p.to_string_lossy().into_owned())
}

/// Stream a large public download to disk while computing SHA-256 and emitting progress callbacks.
pub fn download_streaming_sha256<F>(
    url: &str,
    destination: &Path,
    max_bytes: usize,
    timeout_secs: u64,
    mut on_progress: F,
) -> Result<(PathBuf, String, u64), String>
where
    F: FnMut(u64, u64) + Send,
{
    let url_s = url.to_owned();
    let dest = destination.to_path_buf();
    block_on_io(async move {
        let checked = assert_public_url(&url_s).map_err(|e| e.to_string())?;
        let client = build_public_http_client(timeout_secs).map_err(|e| e.to_string())?;
        let resp = client
            .get(checked)
            .send()
            .await
            .map_err(|e| e.to_string())?;
        let status = resp.status().as_u16();
        if status >= 400 {
            return Err(format!("The remote server returned HTTP {status}."));
        }
        let total_expected = resp.content_length().unwrap_or(0);
        if total_expected as usize > max_bytes {
            return Err("The download is too large.".to_owned());
        }
        let parent = dest.parent().unwrap_or_else(|| Path::new("."));
        fs::create_dir_all(parent).map_err(|e| e.to_string())?;
        let fname = dest
            .file_name()
            .map(|n| n.to_string_lossy().into_owned())
            .unwrap_or_else(|| "download".to_owned());
        let mut temp = Builder::new()
            .prefix(&format!(".{fname}."))
            .suffix(".tmp")
            .tempfile_in(parent)
            .map_err(|e| e.to_string())?;
        let mut hasher = Sha256::new();
        let mut received: u64 = 0;
        let mut stream = resp.bytes_stream();
        while let Some(chunk) = stream.next().await {
            let bytes = chunk.map_err(|e| e.to_string())?;
            received = received.saturating_add(bytes.len() as u64);
            if received as usize > max_bytes {
                return Err("The download is too large.".to_owned());
            }
            hasher.update(&bytes);
            temp.write_all(&bytes).map_err(|e| e.to_string())?;
            on_progress(received, total_expected);
        }
        temp.flush().map_err(|e| e.to_string())?;
        temp.as_file().sync_all().map_err(|e| e.to_string())?;
        let digest = hex::encode(hasher.finalize());
        let temp_path = temp.into_temp_path();
        set_mode(&temp_path, 0o600).map_err(|e| e.to_string())?;
        temp_path.persist(&dest).map_err(|e| e.error.to_string())?;
        fsync_directory(parent);
        Ok((dest, digest, received))
    })
}

/// Exact port of Python's `difflib.SequenceMatcher(None, a, b).ratio()`.
///
/// Uses Ratcliff/Obershelp recursive longest-contiguous-matching-block decomposition
/// (without autojunk, matching Python for strings < 200 chars and faithfully matching
/// Python's `b2j` + `find_longest_match` semantics).
pub fn sequence_matcher_ratio(a_str: &str, b_str: &str) -> f64 {
    let a: Vec<char> = a_str.chars().collect();
    let b: Vec<char> = b_str.chars().collect();
    let total = a.len() + b.len();
    if total == 0 {
        return 1.0;
    }
    let mut b2j: HashMap<char, Vec<usize>> = HashMap::new();
    for (i, &ch) in b.iter().enumerate() {
        b2j.entry(ch).or_default().push(i);
    }
    let mut popular: HashMap<char, bool> = HashMap::new();
    let n = b.len();
    if n >= 200 {
        let ntest = n / 100 + 1;
        b2j.retain(|&ch, idxs| {
            if idxs.len() > ntest {
                popular.insert(ch, true);
                false
            } else {
                true
            }
        });
    }

    fn find_longest_match(
        a: &[char],
        b: &[char],
        b2j: &HashMap<char, Vec<usize>>,
        alo: usize,
        ahi: usize,
        blo: usize,
        bhi: usize,
    ) -> (usize, usize, usize) {
        let mut besti = alo;
        let mut bestj = blo;
        let mut bestsize = 0usize;
        let mut j2len: HashMap<usize, usize> = HashMap::new();
        for (i, &ch) in a.iter().enumerate().take(ahi).skip(alo) {
            let mut newj2len: HashMap<usize, usize> = HashMap::new();
            if let Some(positions) = b2j.get(&ch) {
                for &j in positions {
                    if j < blo {
                        continue;
                    }
                    if j >= bhi {
                        break;
                    }
                    let k = if j > 0 {
                        j2len.get(&(j - 1)).copied().unwrap_or(0) + 1
                    } else {
                        1
                    };
                    newj2len.insert(j, k);
                    if k > bestsize {
                        besti = i + 1 - k;
                        bestj = j + 1 - k;
                        bestsize = k;
                    }
                }
            }
            j2len = newj2len;
        }
        while besti > alo && bestj > blo && a[besti - 1] == b[bestj - 1] {
            besti -= 1;
            bestj -= 1;
            bestsize += 1;
        }
        while besti + bestsize < ahi
            && bestj + bestsize < bhi
            && a[besti + bestsize] == b[bestj + bestsize]
        {
            bestsize += 1;
        }
        (besti, bestj, bestsize)
    }

    let mut queue = vec![(0usize, a.len(), 0usize, b.len())];
    let mut matches = 0usize;
    while let Some((alo, ahi, blo, bhi)) = queue.pop() {
        let (i, j, k) = find_longest_match(&a, &b, &b2j, alo, ahi, blo, bhi);
        if k > 0 {
            matches += k;
            if alo < i && blo < j {
                queue.push((alo, i, blo, j));
            }
            if i + k < ahi && j + k < bhi {
                queue.push((i + k, ahi, j + k, bhi));
            }
        }
    }
    (2.0 * matches as f64) / (total as f64)
}

/// Round half-to-even (matching Python's `round(val, ndigits)`).
pub fn py_round(val: f64, ndigits: u32) -> f64 {
    let factor = 10f64.powi(ndigits as i32);
    (val * factor).round_ties_even() / factor
}

/// Extract clean file extension from a URL (stripping `?query`).
pub fn ext_from_url(url: &str, default_ext: &str) -> String {
    let path_part = url.split('?').next().unwrap_or(url);
    let ext = Path::new(path_part)
        .extension()
        .and_then(|e| e.to_str())
        .map(|s| format!(".{}", s.to_ascii_lowercase()))
        .unwrap_or_default();
    if ext.is_empty() || ext.len() > 10 || !ext[1..].chars().all(|c| c.is_ascii_alphanumeric()) {
        default_ext.to_owned()
    } else {
        ext
    }
}
