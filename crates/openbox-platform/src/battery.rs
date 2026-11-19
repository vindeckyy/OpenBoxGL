//! Cross-platform battery status reading and 30-second caching.
//! Mirrors `pkg/platform_compat.py` lines 1345-1468.

use std::path::Path;
use std::sync::LazyLock;
use std::time::{Duration, Instant};

use parking_lot::Mutex;
use regex::Regex;
use serde::Serialize;

use crate::features::{IS_MACOS, IS_WINDOWS};

pub const BATTERY_CACHE_SECONDS: u64 = 30;
const WINDOWS_POWER_STATUS_UNKNOWN: u8 = 255;
const WINDOWS_BATTERY_FLAG_CHARGING: u8 = 8;
const WINDOWS_BATTERY_FLAG_NO_BATTERY: u8 = 128;

#[derive(Debug, Clone, Copy, Default, Serialize, PartialEq, Eq)]
pub struct BatteryStatus {
    pub present: bool,
    pub percent: u8,
    pub charging: bool,
}

static PMSET_PCT_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"(\d{1,3})%").expect("valid pmset pct regex"));
static PMSET_CHG_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r";\s*(charging|charged)\b").expect("valid pmset chg regex"));
static CACHE: LazyLock<Mutex<Option<(Instant, BatteryStatus)>>> =
    LazyLock::new(|| Mutex::new(None));

pub fn parse_pmset_battery(text: &str) -> BatteryStatus {
    for line in text.lines() {
        if !line.contains("InternalBattery") {
            continue;
        }
        let percent = PMSET_PCT_RE
            .captures(line)
            .and_then(|c| c.get(1))
            .and_then(|m| m.as_str().parse::<u8>().ok())
            .unwrap_or(0)
            .min(100);
        let charging = PMSET_CHG_RE.is_match(line);
        return BatteryStatus {
            present: true,
            percent,
            charging,
        };
    }
    BatteryStatus::default()
}

pub fn parse_windows_power_status(ac_line: u8, percent: u8, battery_flag: u8) -> BatteryStatus {
    if (battery_flag & WINDOWS_BATTERY_FLAG_NO_BATTERY) != 0 {
        return BatteryStatus::default();
    }
    let level = if percent == WINDOWS_POWER_STATUS_UNKNOWN {
        0
    } else {
        percent.min(100)
    };
    BatteryStatus {
        present: true,
        percent: level,
        charging: ac_line == 1 || (battery_flag & WINDOWS_BATTERY_FLAG_CHARGING) != 0,
    }
}

pub fn linux_battery_status(root: Option<&Path>) -> BatteryStatus {
    let base = root.unwrap_or_else(|| Path::new("/sys/class/power_supply"));
    let Ok(entries) = std::fs::read_dir(base) else {
        return BatteryStatus::default();
    };
    let mut candidates: Vec<_> = entries
        .flatten()
        .map(|e| e.path())
        .filter(|p| {
            p.file_name()
                .and_then(|n| n.to_str())
                .is_some_and(|n| n.starts_with("BAT"))
        })
        .collect();
    candidates.sort();
    for cand in candidates {
        let Ok(cap_raw) = std::fs::read_to_string(cand.join("capacity")) else {
            continue;
        };
        let Ok(pct) = cap_raw.trim().parse::<i32>() else {
            continue;
        };
        let state = std::fs::read_to_string(cand.join("status"))
            .unwrap_or_default()
            .trim()
            .to_lowercase();
        return BatteryStatus {
            present: true,
            percent: pct.clamp(0, 100) as u8,
            charging: matches!(state.as_str(), "charging" | "full"),
        };
    }
    BatteryStatus::default()
}

#[cfg(windows)]
fn read_windows_battery() -> BatteryStatus {
    use windows_sys::Win32::System::Power::{GetSystemPowerStatus, SYSTEM_POWER_STATUS};
    let mut status: SYSTEM_POWER_STATUS = unsafe { std::mem::zeroed() };
    if unsafe { GetSystemPowerStatus(&mut status) == 0 } {
        return BatteryStatus::default();
    }
    parse_windows_power_status(
        status.ACLineStatus,
        status.BatteryLifePercent,
        status.BatteryFlag,
    )
}

#[cfg(not(windows))]
fn read_windows_battery() -> BatteryStatus {
    BatteryStatus::default()
}

fn read_macos_battery() -> BatteryStatus {
    let Ok(out) = std::process::Command::new("/usr/bin/pmset")
        .args(["-g", "batt"])
        .output()
    else {
        return BatteryStatus::default();
    };
    parse_pmset_battery(&String::from_utf8_lossy(&out.stdout))
}

fn cached_read(reader: fn() -> BatteryStatus) -> BatteryStatus {
    let mut guard = CACHE.lock();
    let now = Instant::now();
    if let Some((at, val)) = *guard
        && now.duration_since(at) < Duration::from_secs(BATTERY_CACHE_SECONDS)
    {
        return val;
    }
    let fresh = reader();
    *guard = Some((now, fresh));
    fresh
}

pub fn battery_status(root: Option<&Path>) -> BatteryStatus {
    if IS_WINDOWS {
        cached_read(read_windows_battery)
    } else if IS_MACOS {
        cached_read(read_macos_battery)
    } else {
        linux_battery_status(root)
    }
}
