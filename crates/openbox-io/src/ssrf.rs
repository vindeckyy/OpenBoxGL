//! SSRF URL & IP validation matching `backend_io.py` (IPv4, IPv6, IPv4-mapped, NAT64, 6to4, redirect guards).

use std::net::{IpAddr, Ipv4Addr, Ipv6Addr, ToSocketAddrs};
use std::sync::Arc;

use reqwest::dns::{Addrs, Name, Resolve, Resolving};
use thiserror::Error;
use url::Url;

#[derive(Debug, Error)]
#[error("{0}")]
pub struct SsrfError(pub String);

fn embedded_ipv4(ip: Ipv6Addr) -> Option<Ipv4Addr> {
    if let Some(mapped) = ip.to_ipv4_mapped() {
        return Some(mapped);
    }
    let segs = ip.segments();
    // NAT64: 64:ff9b::/96 or 64:ff9b:1::/48
    if segs[0] == 0x0064
        && segs[1] == 0xff9b
        && ((segs[2] == 0 && segs[3] == 0 && segs[4] == 0 && segs[5] == 0) || segs[2] == 1)
    {
        let octs = ip.octets();
        return Some(Ipv4Addr::new(octs[12], octs[13], octs[14], octs[15]));
    }
    // 6to4: 2002::/16 (IPv4 in bits 16..48 -> segments 1..3)
    if segs[0] == 0x2002 {
        let octs = ip.octets();
        return Some(Ipv4Addr::new(octs[2], octs[3], octs[4], octs[5]));
    }
    None
}

fn is_public_ipv4(ip: Ipv4Addr) -> bool {
    let o = ip.octets();
    if ip.is_private()
        || ip.is_loopback()
        || ip.is_link_local()
        || ip.is_multicast()
        || ip.is_broadcast()
        || ip.is_unspecified()
        || ip.is_documentation()
    {
        return false;
    }
    // 100.64.0.0/10 (CGNAT), 0.0.0.0/8, 192.0.0.0/24, 198.18.0.0/15 (benchmarking), 240.0.0.0/4 (reserved)
    if o[0] == 0
        || (o[0] == 100 && (o[1] & 0b1100_0000) == 64)
        || (o[0] == 192 && o[1] == 0 && o[2] == 0)
        || (o[0] == 198 && (o[1] & 0b1111_1110) == 18)
        || o[0] >= 240
    {
        return false;
    }
    true
}

/// True when `addr` is a globally routable unicast address (rejects loopback/private/link-local/NAT64/6to4/etc.).
pub fn is_public_ip(addr: IpAddr) -> bool {
    match addr {
        IpAddr::V4(v4) => is_public_ipv4(v4),
        IpAddr::V6(v6) => {
            if let Some(v4) = embedded_ipv4(v6) {
                return is_public_ipv4(v4);
            }
            let segs = v6.segments();
            if v6.is_loopback() || v6.is_multicast() || v6.is_unspecified() {
                return false;
            }
            // Unique local fc00::/7, link-local fe80::/10, documentation 2001:db8::/32, site-local fec0::/10
            if (segs[0] & 0xfe00) == 0xfc00
                || (segs[0] & 0xffc0) == 0xfe80
                || (segs[0] & 0xffc0) == 0xfec0
                || (segs[0] == 0x2001 && segs[1] == 0x0db8)
                || (segs[0] == 0x0100 && segs[1] == 0 && segs[2] == 0 && segs[3] == 0)
            {
                return false;
            }
            // Global unicast 2000::/3
            (segs[0] & 0xe000) == 0x2000
        }
    }
}

pub fn validate_http_url(url: &str) -> Result<Url, SsrfError> {
    let trimmed = url.trim();
    let parsed = Url::parse(trimmed)
        .map_err(|_| SsrfError("Only absolute HTTP(S) URLs are supported.".to_owned()))?;
    if !matches!(parsed.scheme(), "http" | "https") || parsed.host_str().is_none() {
        return Err(SsrfError(
            "Only absolute HTTP(S) URLs are supported.".to_owned(),
        ));
    }
    if !parsed.username().is_empty() || parsed.password().is_some() {
        return Err(SsrfError(
            "URLs with embedded credentials are not supported.".to_owned(),
        ));
    }
    Ok(parsed)
}

pub fn resolve_and_check_host(host: &str) -> Result<Vec<IpAddr>, SsrfError> {
    let cleaned = host.trim().trim_end_matches('.');
    if cleaned.is_empty() {
        return Err(SsrfError("The download URL needs a host.".to_owned()));
    }
    let lower = cleaned.to_ascii_lowercase();
    if lower == "localhost" || lower.ends_with(".localhost") {
        return Err(SsrfError(
            "Downloads may only target public addresses.".to_owned(),
        ));
    }
    let bare = cleaned
        .strip_prefix('[')
        .and_then(|s| s.strip_suffix(']'))
        .unwrap_or(cleaned);
    let no_zone = bare.split('%').next().unwrap_or(bare);
    if let Ok(ip) = no_zone.parse::<IpAddr>() {
        if !is_public_ip(ip) {
            return Err(SsrfError(
                "Downloads may only target public addresses.".to_owned(),
            ));
        }
        return Ok(vec![ip]);
    }
    let addrs: Vec<IpAddr> = (cleaned, 0)
        .to_socket_addrs()
        .map_err(|_| SsrfError("The download host could not be resolved.".to_owned()))?
        .map(|sa| sa.ip())
        .collect();
    if addrs.is_empty() {
        return Err(SsrfError(
            "The download host could not be resolved.".to_owned(),
        ));
    }
    for ip in &addrs {
        if !is_public_ip(*ip) {
            return Err(SsrfError(
                "Downloads may only target public addresses.".to_owned(),
            ));
        }
    }
    Ok(addrs)
}

pub fn assert_public_url(url: &str) -> Result<Url, SsrfError> {
    let parsed = validate_http_url(url)?;
    let host = parsed
        .host_str()
        .ok_or_else(|| SsrfError("The download URL needs a host.".to_owned()))?;
    resolve_and_check_host(host)?;
    Ok(parsed)
}

pub fn public_https_url(url: &str) -> Option<String> {
    let parsed = assert_public_url(url).ok()?;
    if parsed.scheme() == "https" {
        Some(parsed.to_string())
    } else {
        None
    }
}

/// Custom `reqwest` DNS resolver that enforces `is_public_ip` at connect time (eliminating TOCTOU).
#[derive(Debug, Default)]
pub struct PublicDnsResolver;

impl Resolve for PublicDnsResolver {
    fn resolve(&self, name: Name) -> Resolving {
        let host = name.as_str().to_owned();
        Box::pin(async move {
            let ips = tokio::task::spawn_blocking(move || resolve_and_check_host(&host))
                .await
                .map_err(|e| {
                    Box::new(SsrfError(e.to_string())) as Box<dyn std::error::Error + Send + Sync>
                })?
                .map_err(|e| Box::new(e) as Box<dyn std::error::Error + Send + Sync>)?;
            let addrs: Addrs = Box::new(ips.into_iter().map(|ip| std::net::SocketAddr::new(ip, 0)));
            Ok(addrs)
        })
    }
}

/// Build an SSRF-hardened `reqwest::Client` (rustls, DNS-pinned public resolver, redirect hop validation).
pub fn build_public_http_client(timeout_secs: u64) -> Result<reqwest::Client, reqwest::Error> {
    let redirect_policy = reqwest::redirect::Policy::custom(|attempt| {
        if attempt.previous().len() >= 10 {
            return attempt.error(SsrfError("Too many redirects.".to_owned()));
        }
        if let Err(err) = validate_http_url(attempt.url().as_str()) {
            return attempt.error(err);
        }
        attempt.follow()
    });
    reqwest::Client::builder()
        .dns_resolver(Arc::new(PublicDnsResolver))
        .redirect(redirect_policy)
        .timeout(std::time::Duration::from_secs(timeout_secs))
        .user_agent("OpenBox/1")
        .build()
}
