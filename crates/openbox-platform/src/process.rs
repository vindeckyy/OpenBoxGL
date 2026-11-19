//! Cross-platform process liveness, signaling, process-group trees, and session identity inspection.
//! Mirrors `pkg/platform_compat.py` lines 425-1015 & 1770-1969.

use std::path::Path;

#[cfg(unix)]
fn group_target(pid: i32, pgid: Option<i32>) -> Option<i32> {
    if let Some(explicit) = pgid
        && explicit > 1
    {
        return Some(explicit);
    }
    let derived = process_group_id(pid);
    if derived > 1 {
        Some(derived)
    } else if pid > 1 {
        Some(pid)
    } else {
        None
    }
}

pub fn process_alive(pid: i32) -> bool {
    if pid <= 0 {
        return false;
    }
    #[cfg(unix)]
    {
        // SAFETY: `kill(pid, 0)` only checks existence and permissions.
        let rc = unsafe { libc::kill(pid, 0) };
        if rc == 0 {
            return true;
        }
        std::io::Error::last_os_error().raw_os_error() == Some(libc::EPERM)
    }
    #[cfg(windows)]
    {
        windows_impl::process_alive(pid as u32)
    }
}

pub fn terminate_process(pid: i32, force: bool) -> bool {
    if pid <= 1 {
        return false;
    }
    #[cfg(unix)]
    {
        let sig = if force { libc::SIGKILL } else { libc::SIGTERM };
        // SAFETY: pid > 1 verified above.
        unsafe { libc::kill(pid, sig) == 0 }
    }
    #[cfg(windows)]
    {
        windows_impl::terminate_process(pid as u32, force)
    }
}

pub fn terminate_process_tree(pid: i32, force: bool, pgid: Option<i32>) -> bool {
    if pid <= 1 {
        return false;
    }
    #[cfg(unix)]
    {
        let sig = if force { libc::SIGKILL } else { libc::SIGTERM };
        if let Some(target) = group_target(pid, pgid) {
            // SAFETY: target > 1 verified by `group_target`.
            if unsafe { libc::killpg(target, sig) == 0 } {
                return true;
            }
        }
        // SAFETY: pid > 1 verified above.
        unsafe { libc::kill(pid, sig) == 0 }
    }
    #[cfg(windows)]
    {
        let _ = pgid;
        windows_impl::terminate_tree(pid as u32, force)
    }
}

pub fn suspend_process_tree(pid: i32, pgid: Option<i32>) -> bool {
    if pid <= 1 {
        return false;
    }
    #[cfg(unix)]
    {
        if let Some(target) = group_target(pid, pgid)
            && unsafe { libc::killpg(target, libc::SIGSTOP) == 0 }
        {
            return true;
        }
        unsafe { libc::kill(pid, libc::SIGSTOP) == 0 }
    }
    #[cfg(windows)]
    {
        let _ = pgid;
        windows_impl::suspend_resume_tree(pid as u32, true)
    }
}

pub fn resume_process_tree(pid: i32, pgid: Option<i32>) -> bool {
    if pid <= 1 {
        return false;
    }
    #[cfg(unix)]
    {
        if let Some(target) = group_target(pid, pgid)
            && unsafe { libc::killpg(target, libc::SIGCONT) == 0 }
        {
            return true;
        }
        unsafe { libc::kill(pid, libc::SIGCONT) == 0 }
    }
    #[cfg(windows)]
    {
        let _ = pgid;
        windows_impl::suspend_resume_tree(pid as u32, false)
    }
}

pub fn process_group_id(pid: i32) -> i32 {
    if pid <= 0 {
        return 0;
    }
    #[cfg(unix)]
    {
        // SAFETY: `getpgid` is read-only.
        let res = unsafe { libc::getpgid(pid) };
        if res < 0 { pid } else { res }
    }
    #[cfg(windows)]
    {
        pid
    }
}

pub fn process_start_token(pid: i32) -> Option<String> {
    if pid <= 0 {
        return None;
    }
    #[cfg(target_os = "linux")]
    {
        let stat = std::fs::read_to_string(format!("/proc/{pid}/stat")).ok()?;
        let after_comm = stat.rsplit_once(')')?.1;
        after_comm.split_whitespace().nth(19).map(ToOwned::to_owned)
    }
    #[cfg(target_os = "macos")]
    {
        macos_impl::start_token(pid)
    }
    #[cfg(windows)]
    {
        windows_impl::start_token(pid as u32)
    }
}

pub fn process_command_line(pid: i32) -> String {
    if pid <= 0 {
        return String::new();
    }
    #[cfg(target_os = "linux")]
    {
        std::fs::read(format!("/proc/{pid}/cmdline"))
            .ok()
            .map(|bytes| {
                let text = String::from_utf8_lossy(&bytes).replace('\0', " ");
                text.chars().take(100).collect()
            })
            .unwrap_or_default()
    }
    #[cfg(target_os = "macos")]
    {
        macos_impl::pid_path(pid)
    }
    #[cfg(windows)]
    {
        windows_impl::image_path(pid as u32)
            .chars()
            .take(100)
            .collect()
    }
}

pub fn process_name(pid: i32) -> String {
    if pid <= 0 {
        return String::new();
    }
    #[cfg(target_os = "linux")]
    {
        std::fs::read_to_string(format!("/proc/{pid}/comm"))
            .ok()
            .map(|s| s.trim().to_owned())
            .unwrap_or_default()
    }
    #[cfg(target_os = "macos")]
    {
        let p = macos_impl::pid_path(pid);
        Path::new(&p)
            .file_name()
            .map(|n| n.to_string_lossy().into_owned())
            .unwrap_or_default()
    }
    #[cfg(windows)]
    {
        let p = windows_impl::image_path(pid as u32);
        Path::new(&p)
            .file_name()
            .map(|n| n.to_string_lossy().into_owned())
            .unwrap_or_default()
    }
}

pub fn process_cwd(pid: i32) -> String {
    if pid <= 0 {
        return String::new();
    }
    #[cfg(target_os = "linux")]
    {
        std::fs::read_link(format!("/proc/{pid}/cwd"))
            .ok()
            .map(|p| p.to_string_lossy().into_owned())
            .unwrap_or_default()
    }
    #[cfg(target_os = "macos")]
    {
        macos_impl::process_cwd(pid)
    }
    #[cfg(windows)]
    {
        String::new()
    }
}

pub fn child_pids(pid: i32) -> Vec<i32> {
    if pid <= 0 {
        return Vec::new();
    }
    #[cfg(target_os = "linux")]
    {
        let Ok(entries) = std::fs::read_dir("/proc") else {
            return Vec::new();
        };
        let mut out = Vec::new();
        for entry in entries.flatten() {
            let name = entry.file_name();
            let Some(name_str) = name.to_str() else {
                continue;
            };
            let Ok(cand) = name_str.parse::<i32>() else {
                continue;
            };
            let Ok(stat) = std::fs::read_to_string(format!("/proc/{cand}/stat")) else {
                continue;
            };
            if let Some((_, after)) = stat.rsplit_once(')')
                && let Some(ppid_str) = after.split_whitespace().nth(1)
                && ppid_str.parse::<i32>().ok() == Some(pid)
            {
                out.push(cand);
            }
        }
        out
    }
    #[cfg(target_os = "macos")]
    {
        macos_impl::pid_list()
            .into_iter()
            .filter(|&c| c != pid && macos_impl::parent_pid(c) == pid)
            .collect()
    }
    #[cfg(windows)]
    {
        windows_impl::process_table()
            .into_iter()
            .filter(|e| e.parent == pid as u32)
            .map(|e| e.pid as i32)
            .collect()
    }
}

pub fn find_pids_by_name(pattern: &str) -> Vec<i32> {
    let needle = pattern.to_lowercase();
    if needle.is_empty() {
        return Vec::new();
    }
    #[cfg(target_os = "linux")]
    {
        let Ok(entries) = std::fs::read_dir("/proc") else {
            return Vec::new();
        };
        let mut out = Vec::new();
        for entry in entries.flatten() {
            if let Some(Ok(pid)) = entry.file_name().to_str().map(str::parse::<i32>)
                && process_name(pid).to_lowercase().contains(&needle)
            {
                out.push(pid);
            }
        }
        out
    }
    #[cfg(target_os = "macos")]
    {
        macos_impl::pid_list()
            .into_iter()
            .filter(|&c| process_name(c).to_lowercase().contains(&needle))
            .collect()
    }
    #[cfg(windows)]
    {
        windows_impl::process_table()
            .into_iter()
            .filter(|e| e.name.to_lowercase().contains(&needle))
            .map(|e| e.pid as i32)
            .collect()
    }
}

pub fn find_pids_in_folder(folder: &Path) -> Vec<i32> {
    let resolved = crate::paths::resolve_non_strict(&crate::paths::expand_user(folder));
    #[cfg(target_os = "linux")]
    {
        let Ok(entries) = std::fs::read_dir("/proc") else {
            return Vec::new();
        };
        let folder_str = resolved.to_string_lossy();
        let prefix = format!("{}/", folder_str.trim_end_matches('/'));
        let mut out = Vec::new();
        for entry in entries.flatten() {
            if let Some(Ok(pid)) = entry.file_name().to_str().map(str::parse::<i32>) {
                let cwd = process_cwd(pid);
                if cwd == folder_str.as_ref() || cwd.starts_with(&prefix) {
                    out.push(pid);
                }
            }
        }
        out
    }
    #[cfg(target_os = "macos")]
    {
        let folder_fold = crate::paths::path_identity_fold(&resolved.to_string_lossy());
        let prefix = format!("{}/", folder_fold.trim_end_matches('/'));
        macos_impl::pid_list()
            .into_iter()
            .filter(|&c| {
                let cwd = macos_impl::process_cwd(c);
                if !cwd.is_empty() {
                    let f = crate::paths::path_identity_fold(&cwd);
                    if f == folder_fold || f.starts_with(&prefix) {
                        return true;
                    }
                }
                let img = macos_impl::pid_path(c);
                if !img.is_empty() {
                    let f = crate::paths::path_identity_fold(
                        &crate::paths::resolve_non_strict(Path::new(&img)).to_string_lossy(),
                    );
                    if f.starts_with(&prefix) {
                        return true;
                    }
                }
                false
            })
            .collect()
    }
    #[cfg(windows)]
    {
        windows_impl::process_table()
            .into_iter()
            .filter(|e| {
                !e.image.is_empty()
                    && crate::paths::resolve_non_strict(Path::new(&e.image)).starts_with(&resolved)
            })
            .map(|e| e.pid as i32)
            .collect()
    }
}

#[cfg(target_os = "macos")]
mod macos_impl {
    use std::ffi::c_void;

    const PROC_PIDTBSDINFO: libc::c_int = 3;
    const PROC_PIDVNODEPATHINFO: libc::c_int = 9;
    const PROC_BSDINFO_SIZE: usize = 136;
    const PROC_VNODEPATHINFO_SIZE: usize = 2352;
    const PROC_PIDPATHINFO_MAXSIZE: usize = 4096;

    #[link(name = "proc")]
    unsafe extern "C" {
        fn proc_listallpids(buffer: *mut c_void, buffersize: libc::c_int) -> libc::c_int;
        fn proc_pidpath(pid: libc::c_int, buffer: *mut c_void, buffersize: u32) -> libc::c_int;
        fn proc_pidinfo(
            pid: libc::c_int,
            flavor: libc::c_int,
            arg: u64,
            buffer: *mut c_void,
            buffersize: libc::c_int,
        ) -> libc::c_int;
    }

    fn bsd_info(pid: i32) -> Option<[u8; PROC_BSDINFO_SIZE]> {
        let mut buf = [0u8; PROC_BSDINFO_SIZE];
        let written = unsafe {
            proc_pidinfo(
                pid,
                PROC_PIDTBSDINFO,
                0,
                buf.as_mut_ptr().cast(),
                PROC_BSDINFO_SIZE as libc::c_int,
            )
        };
        if written as usize == PROC_BSDINFO_SIZE {
            Some(buf)
        } else {
            None
        }
    }

    pub fn parent_pid(pid: i32) -> i32 {
        bsd_info(pid)
            .map(|b| u32::from_le_bytes([b[16], b[17], b[18], b[19]]) as i32)
            .unwrap_or(0)
    }

    pub fn start_token(pid: i32) -> Option<String> {
        let b = bsd_info(pid)?;
        let secs = u64::from_le_bytes(b[120..128].try_into().ok()?);
        let usecs = u64::from_le_bytes(b[128..136].try_into().ok()?);
        Some(format!("{secs}.{usecs:06}"))
    }

    pub fn pid_path(pid: i32) -> String {
        let mut buf = vec![0u8; PROC_PIDPATHINFO_MAXSIZE];
        let written = unsafe {
            proc_pidpath(
                pid,
                buf.as_mut_ptr().cast(),
                PROC_PIDPATHINFO_MAXSIZE as u32,
            )
        };
        if written <= 0 {
            return String::new();
        }
        let end = buf.iter().position(|&b| b == 0).unwrap_or(written as usize);
        String::from_utf8_lossy(&buf[..end]).into_owned()
    }

    pub fn pid_list() -> Vec<i32> {
        let mut cap: usize = 65536;
        for _ in 0..4 {
            let mut buf = vec![0i32; cap];
            let bytes = (cap * std::mem::size_of::<i32>()) as libc::c_int;
            let count = unsafe { proc_listallpids(buf.as_mut_ptr().cast(), bytes) };
            if count <= 0 {
                return Vec::new();
            }
            if (count as usize) < cap {
                buf.truncate(count as usize);
                return buf;
            }
            cap *= 4;
        }
        Vec::new()
    }

    pub fn process_cwd(pid: i32) -> String {
        let mut buf = vec![0u8; PROC_VNODEPATHINFO_SIZE];
        let written = unsafe {
            proc_pidinfo(
                pid,
                PROC_PIDVNODEPATHINFO,
                0,
                buf.as_mut_ptr().cast(),
                PROC_VNODEPATHINFO_SIZE as libc::c_int,
            )
        };
        if written as usize != PROC_VNODEPATHINFO_SIZE {
            return String::new();
        }
        let slice = &buf[152..152 + 1024];
        let end = slice.iter().position(|&b| b == 0).unwrap_or(slice.len());
        let text = String::from_utf8_lossy(&slice[..end]).into_owned();
        if text.starts_with('/') && text.chars().all(|c| !c.is_control()) {
            text
        } else {
            String::new()
        }
    }
}

#[cfg(windows)]
mod windows_impl {
    use std::process::Command;
    use windows_sys::Win32::Foundation::{
        CloseHandle, FILETIME, INVALID_HANDLE_VALUE, WAIT_TIMEOUT,
    };
    use windows_sys::Win32::System::Diagnostics::ToolHelp::{
        CreateToolhelp32Snapshot, PROCESSENTRY32W, Process32FirstW, Process32NextW,
        TH32CS_SNAPPROCESS,
    };
    use windows_sys::Win32::System::Threading::{
        GetProcessTimes, OpenProcess, PROCESS_QUERY_LIMITED_INFORMATION, PROCESS_TERMINATE,
        QueryFullProcessImageNameW, SYNCHRONIZE, TerminateProcess, WaitForSingleObject,
    };

    pub struct ProcRow {
        pub pid: u32,
        pub parent: u32,
        pub name: String,
        pub image: String,
    }

    pub fn process_alive(pid: u32) -> bool {
        let handle =
            unsafe { OpenProcess(SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION, 0, pid) };
        if handle.is_null() {
            return false;
        }
        let alive = unsafe { WaitForSingleObject(handle, 0) == WAIT_TIMEOUT };
        unsafe { CloseHandle(handle) };
        alive
    }

    pub fn terminate_process(pid: u32, force: bool) -> bool {
        if !force && graceful_close(pid) {
            return true;
        }
        let handle = unsafe { OpenProcess(PROCESS_TERMINATE, 0, pid) };
        if handle.is_null() {
            return false;
        }
        let ok = unsafe { TerminateProcess(handle, 1) != 0 };
        unsafe { CloseHandle(handle) };
        ok
    }

    fn graceful_close(pid: u32) -> bool {
        let status = Command::new("taskkill")
            .args(["/PID", &pid.to_string(), "/T"])
            .status();
        matches!(status, Ok(s) if s.success())
    }

    pub fn terminate_tree(pid: u32, force: bool) -> bool {
        if !force && graceful_close(pid) {
            return true;
        }
        let status = Command::new("taskkill")
            .args(["/PID", &pid.to_string(), "/T", "/F"])
            .status();
        if matches!(status, Ok(s) if s.success()) {
            return true;
        }
        terminate_process(pid, true)
    }

    pub fn suspend_resume_tree(pid: u32, suspend: bool) -> bool {
        let _ = (pid, suspend);
        false
    }

    pub fn start_token(pid: u32) -> Option<String> {
        let handle = unsafe { OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid) };
        if handle.is_null() {
            return None;
        }
        let mut creation: FILETIME = unsafe { std::mem::zeroed() };
        let mut exit: FILETIME = unsafe { std::mem::zeroed() };
        let mut kernel: FILETIME = unsafe { std::mem::zeroed() };
        let mut user: FILETIME = unsafe { std::mem::zeroed() };
        let ok = unsafe {
            GetProcessTimes(handle, &mut creation, &mut exit, &mut kernel, &mut user) != 0
        };
        unsafe { CloseHandle(handle) };
        if ok {
            Some(format!(
                "{:08x}{:08x}",
                creation.dwHighDateTime, creation.dwLowDateTime
            ))
        } else {
            None
        }
    }

    pub fn image_path(pid: u32) -> String {
        let handle = unsafe { OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid) };
        if handle.is_null() {
            return String::new();
        }
        let mut buf = vec![0u16; 32768];
        let mut len = buf.len() as u32;
        let ok = unsafe { QueryFullProcessImageNameW(handle, 0, buf.as_mut_ptr(), &mut len) != 0 };
        unsafe { CloseHandle(handle) };
        if ok {
            String::from_utf16_lossy(&buf[..len as usize])
        } else {
            String::new()
        }
    }

    pub fn process_table() -> Vec<ProcRow> {
        let snap = unsafe { CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0) };
        if snap == INVALID_HANDLE_VALUE || snap.is_null() {
            return Vec::new();
        }
        let mut out = Vec::new();
        let mut entry: PROCESSENTRY32W = unsafe { std::mem::zeroed() };
        entry.dwSize = std::mem::size_of::<PROCESSENTRY32W>() as u32;
        if unsafe { Process32FirstW(snap, &mut entry) != 0 } {
            loop {
                let end = entry
                    .szExeFile
                    .iter()
                    .position(|&c| c == 0)
                    .unwrap_or(entry.szExeFile.len());
                let name = String::from_utf16_lossy(&entry.szExeFile[..end]);
                let pid = entry.th32ProcessID;
                out.push(ProcRow {
                    pid,
                    parent: entry.th32ParentProcessID,
                    name,
                    image: if pid > 0 {
                        image_path(pid)
                    } else {
                        String::new()
                    },
                });
                if unsafe { Process32NextW(snap, &mut entry) == 0 } {
                    break;
                }
            }
        }
        unsafe { CloseHandle(snap) };
        out
    }
}
