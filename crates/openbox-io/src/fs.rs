//! Atomic filesystem writes, directory fsync, and bounded downloads mirroring `backend_io.py`.

use std::fs;
use std::io::{self, Read, Write};
use std::path::{Path, PathBuf};

use futures_util::StreamExt;
use sha2::{Digest, Sha256};
use tempfile::Builder;

use crate::ssrf::{assert_public_url, build_public_http_client};

pub const DEFAULT_MAX_DOWNLOAD: usize = 64 * 1024 * 1024;
pub const CHUNK_SIZE: usize = 1024 * 1024;
pub const MAX_RESPONSE_BYTES: usize = 8 * 1024 * 1024;

pub fn fsync_directory(dir: &Path) {
    #[cfg(unix)]
    {
        if let Ok(f) = fs::File::open(dir) {
            let _ = f.sync_all();
        }
    }
    #[cfg(not(unix))]
    {
        let _ = dir;
    }
}

#[cfg(unix)]
pub fn set_mode(path: &Path, mode: u32) -> io::Result<()> {
    use std::os::unix::fs::PermissionsExt;
    fs::set_permissions(path, fs::Permissions::from_mode(mode))
}

#[cfg(not(unix))]
pub fn set_mode(_path: &Path, _mode: u32) -> io::Result<()> {
    Ok(())
}

/// Atomically write bytes to `path` (temp file in same dir, fsync, chmod `mode`, rename, fsync parent dir).
pub fn atomic_write_bytes(path: &Path, data: &[u8], mode: u32) -> io::Result<PathBuf> {
    let parent = path.parent().unwrap_or_else(|| Path::new("."));
    fs::create_dir_all(parent)?;
    let file_name = path
        .file_name()
        .map(|n| n.to_string_lossy().into_owned())
        .unwrap_or_else(|| "openbox".to_owned());
    let mut temp = Builder::new()
        .prefix(&format!(".{file_name}."))
        .suffix(".tmp")
        .tempfile_in(parent)?;
    temp.write_all(data)?;
    temp.flush()?;
    temp.as_file().sync_all()?;
    let temp_path = temp.into_temp_path();
    set_mode(&temp_path, mode)?;
    temp_path.persist(path).map_err(|e| e.error)?;
    let _ = set_mode(path, mode);
    fsync_directory(parent);
    Ok(path.to_path_buf())
}

pub fn atomic_write_text(path: &Path, value: &str, mode: u32) -> io::Result<PathBuf> {
    atomic_write_bytes(path, value.as_bytes(), mode)
}

pub fn atomic_copy_stream<R: Read>(
    mut source: R,
    path: &Path,
    mode: u32,
    max_bytes: Option<usize>,
) -> io::Result<PathBuf> {
    let parent = path.parent().unwrap_or_else(|| Path::new("."));
    fs::create_dir_all(parent)?;
    let file_name = path
        .file_name()
        .map(|n| n.to_string_lossy().into_owned())
        .unwrap_or_else(|| "openbox".to_owned());
    let mut temp = Builder::new()
        .prefix(&format!(".{file_name}."))
        .suffix(".tmp")
        .tempfile_in(parent)?;
    let mut buf = vec![0u8; CHUNK_SIZE];
    let mut total: usize = 0;
    loop {
        let n = source.read(&mut buf)?;
        if n == 0 {
            break;
        }
        total += n;
        if let Some(limit) = max_bytes
            && total > limit
        {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "The file is larger than the allowed limit.",
            ));
        }
        temp.write_all(&buf[..n])?;
    }
    temp.flush()?;
    temp.as_file().sync_all()?;
    let temp_path = temp.into_temp_path();
    set_mode(&temp_path, mode)?;
    temp_path.persist(path).map_err(|e| e.error)?;
    let _ = set_mode(path, mode);
    fsync_directory(parent);
    Ok(path.to_path_buf())
}

/// Options for `download_file`.
#[derive(Debug, Clone)]
pub struct DownloadOptions<'a> {
    pub expected_types: &'a [&'a str],
    pub max_bytes: usize,
    pub timeout_secs: u64,
    pub extra_headers: &'a [(&'a str, &'a str)],
    pub expected_sha256: Option<&'a str>,
    pub mode: u32,
}

impl Default for DownloadOptions<'_> {
    fn default() -> Self {
        Self {
            expected_types: &[],
            max_bytes: DEFAULT_MAX_DOWNLOAD,
            timeout_secs: 30,
            extra_headers: &[],
            expected_sha256: None,
            mode: 0o644,
        }
    }
}

/// Download a public URL atomically to `destination` with content-type, size-limit, and SHA-256 verification.
pub async fn download_file(
    url: &str,
    destination: &Path,
    opts: &DownloadOptions<'_>,
) -> Result<PathBuf, String> {
    let checked = assert_public_url(url).map_err(|e| e.to_string())?;
    let client = build_public_http_client(opts.timeout_secs).map_err(|e| e.to_string())?;
    let mut req = client.get(checked);
    for (k, v) in opts.extra_headers {
        req = req.header(*k, *v);
    }
    let resp = req.send().await.map_err(|e| e.to_string())?;
    let status = resp.status().as_u16();
    if status >= 400 {
        return Err(format!("The remote server returned HTTP {status}."));
    }
    if !opts.expected_types.is_empty() {
        let ct = resp
            .headers()
            .get(reqwest::header::CONTENT_TYPE)
            .and_then(|v| v.to_str().ok())
            .unwrap_or("")
            .split(';')
            .next()
            .unwrap_or("")
            .trim();
        if !opts
            .expected_types
            .iter()
            .any(|prefix| ct.starts_with(prefix))
        {
            return Err(format!(
                "The remote server returned an unsupported content type: {}",
                if ct.is_empty() { "unknown" } else { ct }
            ));
        }
    }
    if let Some(len) = resp.content_length()
        && len as usize > opts.max_bytes
    {
        return Err("The download is too large.".to_owned());
    }
    let parent = destination.parent().unwrap_or_else(|| Path::new("."));
    fs::create_dir_all(parent).map_err(|e| e.to_string())?;
    let fname = destination
        .file_name()
        .map(|n| n.to_string_lossy().into_owned())
        .unwrap_or_else(|| "download".to_owned());
    let mut temp = Builder::new()
        .prefix(&format!(".{fname}."))
        .suffix(".tmp")
        .tempfile_in(parent)
        .map_err(|e| e.to_string())?;
    let mut hasher = Sha256::new();
    let mut total: usize = 0;
    let mut stream = resp.bytes_stream();
    while let Some(chunk) = stream.next().await {
        let bytes = chunk.map_err(|e| e.to_string())?;
        total += bytes.len();
        if total > opts.max_bytes {
            return Err("The download is too large.".to_owned());
        }
        hasher.update(&bytes);
        temp.write_all(&bytes).map_err(|e| e.to_string())?;
    }
    temp.flush().map_err(|e| e.to_string())?;
    temp.as_file().sync_all().map_err(|e| e.to_string())?;
    if let Some(expected) = opts.expected_sha256.filter(|s| !s.is_empty()) {
        let actual = hex::encode(hasher.finalize());
        if !actual.eq_ignore_ascii_case(expected) {
            return Err("The downloaded file failed checksum verification.".to_owned());
        }
    }
    let temp_path = temp.into_temp_path();
    set_mode(&temp_path, opts.mode).map_err(|e| e.to_string())?;
    temp_path
        .persist(destination)
        .map_err(|e| e.error.to_string())?;
    fsync_directory(parent);
    Ok(destination.to_path_buf())
}
