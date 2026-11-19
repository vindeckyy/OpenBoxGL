//! Cross-platform OS shims for Linux, macOS, and Windows (sole crate permitted `unsafe` FFI).
//! Mirrors `pkg/platform_compat.py`.

pub mod battery;
pub mod features;
pub mod lock;
pub mod paths;
pub mod process;
pub mod shell;
pub mod storefronts;

pub use battery::{BatteryStatus, battery_status, parse_pmset_battery, parse_windows_power_status};
pub use features::{
    IS_LINUX, IS_MACOS, IS_POSIX, IS_WINDOWS, PlatformFeatures, platform_features, platform_key,
    platform_label, python_command,
};
pub use lock::{DEFAULT_LOCK_TIMEOUT, FileLockGuard, file_lock, try_file_lock};
pub use paths::{
    MAX_FILENAME_COMPONENT, UnsafeIdentifier, contained_path, default_data_dir,
    display_name_for_file, ensure_macos_tool_path, env_file_roots, expand_user, file_is_private,
    home_dir, is_group_or_world_writable, libretro_core_filename, libretro_system_core_dir,
    macos_app_bundle_for, macos_app_executable, macos_screenshot_command, owned_by_current_user,
    path_identity_fold, resolve_non_strict, resolved_data_dir, safe_filename_component,
    safe_identifier, start_menu_programs_dir, user_symlink_component, windows_install_dir,
};
pub use process::{
    child_pids, find_pids_by_name, find_pids_in_folder, process_alive, process_command_line,
    process_cwd, process_group_id, process_name, process_start_token, resume_process_tree,
    suspend_process_tree, terminate_process, terminate_process_tree,
};
pub use shell::{
    EXTERNAL_OPEN_DISABLED_ENV, browser_application_commands, executable_paths,
    external_open_disabled, flatpak_app_installed, is_executable, join_command, open_path,
    open_url, resolve_browser_application, resolve_native_executable, reveal_path, split_command,
    split_posix_command, split_windows_command, which,
};
pub use storefronts::{
    epic_manifest_paths, steam_candidate_roots, steam_install_roots, windows_registry_tree,
};

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn safe_identifier_and_filename_rules() {
        assert_eq!(
            safe_identifier("abc_123-x.json", "id").unwrap(),
            "abc_123-x.json"
        );
        assert!(safe_identifier("../escape", "id").is_err());
        assert!(safe_identifier(".hidden", "id").is_err());
        assert!(safe_identifier("a/b", "id").is_err());

        assert_eq!(
            safe_filename_component("Sonic & Knuckles (Disc 1).bin", "file").unwrap(),
            "Sonic & Knuckles (Disc 1).bin"
        );
        assert!(safe_filename_component("CON.txt", "file").is_err());
        assert!(safe_filename_component("nul", "file").is_err());
        assert!(safe_filename_component("game.", "file").is_err());
        assert!(safe_filename_component("a\\b", "file").is_err());
    }

    #[test]
    fn windows_and_posix_command_splitting() {
        assert_eq!(
            split_windows_command(r#""C:\Program Files\Emu\emu.exe" --rom "C:\My Games\a.iso""#),
            vec![
                r"C:\Program Files\Emu\emu.exe",
                "--rom",
                r"C:\My Games\a.iso"
            ]
        );
        assert_eq!(
            split_posix_command("retroarch -L '/opt/cores/snes9x.so' \"My Game.sfc\"").unwrap(),
            vec!["retroarch", "-L", "/opt/cores/snes9x.so", "My Game.sfc"]
        );
    }

    #[test]
    fn battery_parsers_match_python() {
        let pm = "Now drawing from 'AC Power'\n -InternalBattery-0 (id=123) 87%; charging; 0:42 remaining";
        let st = parse_pmset_battery(pm);
        assert_eq!(
            st,
            BatteryStatus {
                present: true,
                percent: 87,
                charging: true
            }
        );
        assert_eq!(
            parse_windows_power_status(0, 50, 128),
            BatteryStatus::default()
        );
        assert_eq!(
            parse_windows_power_status(1, 255, 8),
            BatteryStatus {
                present: true,
                percent: 0,
                charging: true
            }
        );
    }

    #[test]
    fn advisory_file_lock_and_try_lock() {
        let dir = std::env::temp_dir().join(format!("openbox-lock-test-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let lock_path = dir.join(".test.lock");
        let guard = file_lock(&lock_path, true, DEFAULT_LOCK_TIMEOUT).unwrap();
        // Contending via separate fd on same file fails non-blocking try_file_lock
        let contended = try_file_lock(&lock_path).unwrap();
        assert!(contended.is_none());
        drop(guard);
        let acquired = try_file_lock(&lock_path).unwrap();
        assert!(acquired.is_some());
        drop(acquired);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn current_process_introspection_works() {
        let pid = std::process::id() as i32;
        assert!(process_alive(pid));
        assert!(process_start_token(pid).is_some());
        assert!(!process_command_line(pid).is_empty());
        assert!(!process_name(pid).is_empty());
        assert!(!terminate_process(1, true));
    }
}
