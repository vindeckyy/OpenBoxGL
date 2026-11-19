//! Platform feature capability contract (ADR 0086). Mirrors `pkg/platform_compat.py::platform_features`.

use serde::Serialize;

pub const IS_WINDOWS: bool = cfg!(target_os = "windows");
pub const IS_MACOS: bool = cfg!(target_os = "macos");
pub const IS_LINUX: bool = cfg!(target_os = "linux");
pub const IS_POSIX: bool = cfg!(unix);

#[derive(Debug, Clone, Copy, Serialize, PartialEq, Eq)]
pub struct PlatformFeatures {
    pub gamescope: bool,
    pub mangohud: bool,
    pub handheld_perf: bool,
    pub flatpak: bool,
    pub wine: bool,
    pub faugus: bool,
    pub lutris: bool,
    pub heroic: bool,
    pub epic: bool,
    pub native_storefronts: bool,
    pub desktop_entry: bool,
}

pub fn platform_features() -> PlatformFeatures {
    PlatformFeatures {
        gamescope: IS_LINUX,
        mangohud: IS_LINUX,
        handheld_perf: IS_LINUX,
        flatpak: IS_LINUX,
        wine: IS_LINUX,
        faugus: IS_LINUX,
        lutris: IS_LINUX,
        heroic: IS_LINUX || IS_WINDOWS,
        epic: IS_LINUX || IS_WINDOWS,
        native_storefronts: IS_WINDOWS,
        desktop_entry: IS_LINUX,
    }
}

pub fn platform_label() -> &'static str {
    if IS_WINDOWS {
        "Windows"
    } else if IS_MACOS {
        "macOS"
    } else {
        "Linux"
    }
}

pub fn platform_key() -> &'static str {
    if IS_WINDOWS {
        "windows"
    } else if IS_MACOS {
        "macos"
    } else {
        "linux"
    }
}

pub fn python_command() -> &'static str {
    if IS_WINDOWS { "python" } else { "python3" }
}
