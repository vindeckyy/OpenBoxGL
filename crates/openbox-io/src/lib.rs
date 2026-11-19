//! Bounded SSRF-safe HTTP I/O, atomic filesystem operations, archive extraction,
//! `.env` loading, redacted diagnostic logging, and crash reports.

pub mod archives;
pub mod crash_report;
pub mod env_config;
pub mod fs;
pub mod logging;
pub mod ssrf;

pub use archives::{
    ArchiveLimits, MAX_ARCHIVE_LISTING_BYTES, MAX_ARCHIVE_MEMBER_BYTES, MAX_ARCHIVE_MEMBERS,
    MAX_ARCHIVE_TOTAL_BYTES, choose_game_file, extract_game, extraction_dir, parse_7z_records,
    safe_zip_extract, validate_7z_member_record,
};
pub use crash_report::{build_preview, build_report, tokenize_home_paths, tokenize_home_str};
pub use env_config::{
    ENV_KEYS, bootstrap_env, discover_env_files, emumovies_from_env, ensure_env_loaded, env_value,
    github_token_from_env, load_dotenv, parse_env_line, retroachievements_from_env,
};
pub use fs::{
    CHUNK_SIZE, DEFAULT_MAX_DOWNLOAD, DownloadOptions, MAX_RESPONSE_BYTES, atomic_copy_stream,
    atomic_write_bytes, atomic_write_text, download_file, fsync_directory, set_mode,
};
pub use logging::{
    LOG_FILE, append_diagnostic_line, diagnostic_log_path, read_diagnostic_log, redact_log_text,
};
pub use ssrf::{
    PublicDnsResolver, SsrfError, assert_public_url, build_public_http_client, is_public_ip,
    public_https_url, resolve_and_check_host, validate_http_url,
};

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;
    use std::net::IpAddr;
    use zip::write::SimpleFileOptions;

    #[test]
    fn ssrf_blocks_private_loopback_nat64_and_6to4() {
        assert!(!is_public_ip("127.0.0.1".parse::<IpAddr>().unwrap()));
        assert!(!is_public_ip("10.0.0.1".parse::<IpAddr>().unwrap()));
        assert!(!is_public_ip("169.254.169.254".parse::<IpAddr>().unwrap()));
        assert!(!is_public_ip("100.64.0.1".parse::<IpAddr>().unwrap()));
        assert!(!is_public_ip("::1".parse::<IpAddr>().unwrap()));
        assert!(!is_public_ip("::ffff:127.0.0.1".parse::<IpAddr>().unwrap()));
        assert!(!is_public_ip(
            "64:ff9b::127.0.0.1".parse::<IpAddr>().unwrap()
        ));
        assert!(!is_public_ip(
            "64:ff9b:1::10.0.0.1".parse::<IpAddr>().unwrap()
        ));
        assert!(!is_public_ip(
            "2002:7f00:0001::1".parse::<IpAddr>().unwrap()
        ));
        assert!(is_public_ip("1.1.1.1".parse::<IpAddr>().unwrap()));
        assert!(is_public_ip(
            "2606:4700:4700::1111".parse::<IpAddr>().unwrap()
        ));
        assert!(validate_http_url("https://user:pass@example.com").is_err());
        assert!(assert_public_url("http://localhost:8080/a").is_err());
        assert!(assert_public_url("http://sub.localhost/a").is_err());
    }

    #[test]
    fn safe_zip_rejects_traversal_and_extracts_valid_zip() {
        let dir = tempfile::tempdir().unwrap();
        let bad_zip = dir.path().join("bad.zip");
        {
            let f = std::fs::File::create(&bad_zip).unwrap();
            let mut zw = zip::ZipWriter::new(f);
            zw.start_file("../escape.txt", SimpleFileOptions::default())
                .unwrap();
            zw.write_all(b"boom").unwrap();
            zw.finish().unwrap();
        }
        let dest = dir.path().join("out");
        let f = std::fs::File::open(&bad_zip).unwrap();
        assert!(safe_zip_extract(f, &dest, ArchiveLimits::default()).is_err());

        let good_zip = dir.path().join("good.zip");
        {
            let f = std::fs::File::create(&good_zip).unwrap();
            let mut zw = zip::ZipWriter::new(f);
            zw.start_file("rom/game.sfc", SimpleFileOptions::default())
                .unwrap();
            zw.write_all(b"ROMBYTES").unwrap();
            zw.finish().unwrap();
        }
        let extracted = extract_game(&good_zip, &dir.path().join("cache"), "").unwrap();
        assert!(extracted.ends_with("game.sfc"));
        assert_eq!(std::fs::read(&extracted).unwrap(), b"ROMBYTES");
    }

    #[test]
    fn log_redaction_and_home_tokenization() {
        let raw = r#"Authorization: Bearer secret123 token=abc123 "api_key": "xyz""#;
        let clean = redact_log_text(raw);
        assert!(!clean.contains("secret123"));
        assert!(!clean.contains("abc123"));
        assert!(!clean.contains("xyz"));

        assert_eq!(
            tokenize_home_str("Opened /home/alice/games/rom.iso and C:\\Users\\Bob\\a.exe"),
            "Opened ~/games/rom.iso and ~\\a.exe"
        );
    }

    #[test]
    fn atomic_write_creates_0600_file() {
        let dir = tempfile::tempdir().unwrap();
        let target = dir.path().join("secret.txt");
        atomic_write_text(&target, "hello", 0o600).unwrap();
        assert_eq!(std::fs::read_to_string(&target).unwrap(), "hello");
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let mode = std::fs::metadata(&target).unwrap().permissions().mode() & 0o777;
            assert_eq!(mode, 0o600);
        }
    }
}
