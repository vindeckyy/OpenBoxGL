//! Steam, Epic, and Windows Registry discovery helpers mirroring `pkg/platform_compat.py` lines 1665-1767.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use crate::features::{IS_MACOS, IS_WINDOWS};
use crate::paths::home_dir;

pub fn steam_candidate_roots(home: &Path) -> Vec<PathBuf> {
    if IS_MACOS {
        vec![home.join("Library/Application Support/Steam")]
    } else {
        vec![
            home.join(".local/share/Steam"),
            home.join(".steam/steam"),
            home.join(".var/app/com.valvesoftware.Steam/.local/share/Steam"),
        ]
    }
}

pub fn steam_install_roots() -> Vec<PathBuf> {
    if IS_WINDOWS {
        let mut candidates = Vec::new();
        for var in ["ProgramFiles(x86)", "ProgramFiles"] {
            if let Some(base) = std::env::var_os(var).filter(|v| !v.is_empty()) {
                candidates.push(PathBuf::from(base).join("Steam"));
            }
        }
        if let Some(home) = home_dir() {
            candidates.push(home.join("Steam"));
            candidates.push(home.join(".steam/steam"));
        }
        return candidates.into_iter().filter(|p| p.is_dir()).collect();
    }
    let Some(home) = home_dir() else {
        return Vec::new();
    };
    steam_candidate_roots(&home)
        .into_iter()
        .filter(|p| p.is_dir())
        .collect()
}

pub fn epic_manifest_paths() -> Vec<PathBuf> {
    if !IS_WINDOWS {
        return Vec::new();
    }
    let program_data = std::env::var_os("PROGRAMDATA")
        .filter(|v| !v.is_empty())
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from(r"C:\ProgramData"));
    let manifests = program_data.join("Epic/EpicGamesLauncher/Data/Manifests");
    let Ok(entries) = std::fs::read_dir(manifests) else {
        return Vec::new();
    };
    let mut items: Vec<PathBuf> = entries
        .flatten()
        .map(|e| e.path())
        .filter(|p| p.extension().and_then(|e| e.to_str()) == Some("item"))
        .collect();
    items.sort();
    items
}

/// Read subkey -> string values under a Windows registry path (empty map off Windows).
pub fn windows_registry_tree(
    _root: &str,
    _path: &str,
) -> BTreeMap<String, BTreeMap<String, String>> {
    #[cfg(not(windows))]
    {
        BTreeMap::new()
    }
    #[cfg(windows)]
    {
        windows_reg_impl::read_tree(_root, _path)
    }
}

#[cfg(windows)]
mod windows_reg_impl {
    use std::collections::BTreeMap;
    use windows_sys::Win32::System::Registry::{
        HKEY, HKEY_CURRENT_USER, HKEY_LOCAL_MACHINE, KEY_READ, KEY_WOW64_32KEY, KEY_WOW64_64KEY,
        REG_EXPAND_SZ, REG_SZ, RegCloseKey, RegEnumKeyExW, RegEnumValueW, RegOpenKeyExW,
    };

    fn to_wide(s: &str) -> Vec<u16> {
        s.encode_utf16().chain(std::iter::once(0)).collect()
    }

    pub fn read_tree(root: &str, path: &str) -> BTreeMap<String, BTreeMap<String, String>> {
        let hive: HKEY = match root.to_ascii_uppercase().as_str() {
            "HKCU" => HKEY_CURRENT_USER,
            "HKLM" => HKEY_LOCAL_MACHINE,
            _ => return BTreeMap::new(),
        };
        let path_w = to_wide(path);
        for view in [KEY_WOW64_64KEY, KEY_WOW64_32KEY] {
            let mut key: HKEY = std::ptr::null_mut();
            let rc = unsafe { RegOpenKeyExW(hive, path_w.as_ptr(), 0, KEY_READ | view, &mut key) };
            if rc != 0 {
                continue;
            }
            let mut tree = BTreeMap::new();
            let mut idx = 0u32;
            loop {
                let mut name_buf = [0u16; 256];
                let mut name_len = name_buf.len() as u32;
                let erc = unsafe {
                    RegEnumKeyExW(
                        key,
                        idx,
                        name_buf.as_mut_ptr(),
                        &mut name_len,
                        std::ptr::null_mut(),
                        std::ptr::null_mut(),
                        std::ptr::null_mut(),
                        std::ptr::null_mut(),
                    )
                };
                if erc != 0 {
                    break;
                }
                idx += 1;
                let sub_name = String::from_utf16_lossy(&name_buf[..name_len as usize]);
                let sub_w = to_wide(&sub_name);
                let mut sub_key: HKEY = std::ptr::null_mut();
                if unsafe { RegOpenKeyExW(key, sub_w.as_ptr(), 0, KEY_READ | view, &mut sub_key) }
                    != 0
                {
                    continue;
                }
                let mut vals = BTreeMap::new();
                let mut vidx = 0u32;
                loop {
                    let mut vname_buf = [0u16; 256];
                    let mut vname_len = vname_buf.len() as u32;
                    let mut vtype = 0u32;
                    let mut data_buf = [0u8; 4096];
                    let mut data_len = data_buf.len() as u32;
                    let vrc = unsafe {
                        RegEnumValueW(
                            sub_key,
                            vidx,
                            vname_buf.as_mut_ptr(),
                            &mut vname_len,
                            std::ptr::null_mut(),
                            &mut vtype,
                            data_buf.as_mut_ptr(),
                            &mut data_len,
                        )
                    };
                    if vrc != 0 {
                        break;
                    }
                    vidx += 1;
                    let vname = String::from_utf16_lossy(&vname_buf[..vname_len as usize]);
                    if vtype == REG_SZ || vtype == REG_EXPAND_SZ {
                        let u16s: Vec<u16> = data_buf[..data_len as usize]
                            .chunks_exact(2)
                            .map(|c| u16::from_le_bytes([c[0], c[1]]))
                            .take_while(|&w| w != 0)
                            .collect();
                        vals.insert(vname, String::from_utf16_lossy(&u16s));
                    }
                }
                unsafe { RegCloseKey(sub_key) };
                tree.insert(sub_name, vals);
            }
            unsafe { RegCloseKey(key) };
            return tree;
        }
        BTreeMap::new()
    }
}
