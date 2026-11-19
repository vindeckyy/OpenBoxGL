//! Cross-process advisory whole-file locking (`flock` on POSIX, `LockFileEx` on Windows).
//! Mirrors `pkg/platform_compat.py::{lock_handle, file_lock, try_file_lock}`.

use std::fs::{File, OpenOptions};
use std::io;
use std::path::Path;
use std::time::{Duration, Instant};

pub const DEFAULT_LOCK_TIMEOUT: Duration = Duration::from_secs(30);

/// RAII guard holding an advisory cross-process lock on an open file descriptor.
pub struct FileLockGuard {
    file: File,
}

impl FileLockGuard {
    pub fn file(&self) -> &File {
        &self.file
    }
}

impl Drop for FileLockGuard {
    fn drop(&mut self) {
        let _ = unlock_file(&self.file);
    }
}

/// Acquire an advisory lock on `path` (creating parent dirs & file if needed).
pub fn file_lock(path: &Path, exclusive: bool, timeout: Duration) -> io::Result<FileLockGuard> {
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    let file = OpenOptions::new()
        .create(true)
        .read(true)
        .write(true)
        .truncate(false)
        .open(path)?;
    lock_file(&file, exclusive, timeout)?;
    Ok(FileLockGuard { file })
}

/// Attempt to acquire an exclusive lock on `path` without blocking. Returns `Ok(None)` if busy.
pub fn try_file_lock(path: &Path) -> io::Result<Option<FileLockGuard>> {
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    let file = OpenOptions::new()
        .create(true)
        .read(true)
        .write(true)
        .truncate(false)
        .open(path)?;
    if try_lock_file(&file, true)? {
        Ok(Some(FileLockGuard { file }))
    } else {
        Ok(None)
    }
}

#[cfg(unix)]
fn lock_file(file: &File, exclusive: bool, timeout: Duration) -> io::Result<()> {
    use std::os::fd::AsRawFd;
    let op = if exclusive {
        libc::LOCK_EX
    } else {
        libc::LOCK_SH
    };
    let deadline = Instant::now() + timeout;
    loop {
        // SAFETY: `file.as_raw_fd()` is a valid open descriptor.
        let rc = unsafe { libc::flock(file.as_raw_fd(), op | libc::LOCK_NB) };
        if rc == 0 {
            return Ok(());
        }
        let err = io::Error::last_os_error();
        if err.kind() == io::ErrorKind::WouldBlock || err.raw_os_error() == Some(libc::EAGAIN) {
            if Instant::now() >= deadline {
                return Err(io::Error::new(
                    io::ErrorKind::TimedOut,
                    "Timed out waiting for the OpenBox data lock.",
                ));
            }
            std::thread::sleep(Duration::from_millis(20));
            continue;
        }
        if err.kind() == io::ErrorKind::Interrupted {
            continue;
        }
        return Err(err);
    }
}

#[cfg(unix)]
fn try_lock_file(file: &File, exclusive: bool) -> io::Result<bool> {
    use std::os::fd::AsRawFd;
    let op = if exclusive {
        libc::LOCK_EX
    } else {
        libc::LOCK_SH
    };
    // SAFETY: `file.as_raw_fd()` is a valid open descriptor.
    let rc = unsafe { libc::flock(file.as_raw_fd(), op | libc::LOCK_NB) };
    if rc == 0 {
        return Ok(true);
    }
    let err = io::Error::last_os_error();
    if err.kind() == io::ErrorKind::WouldBlock || err.raw_os_error() == Some(libc::EAGAIN) {
        Ok(false)
    } else {
        Err(err)
    }
}

#[cfg(unix)]
fn unlock_file(file: &File) -> io::Result<()> {
    use std::os::fd::AsRawFd;
    // SAFETY: `file.as_raw_fd()` is a valid open descriptor.
    let rc = unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_UN) };
    if rc == 0 {
        Ok(())
    } else {
        Err(io::Error::last_os_error())
    }
}

#[cfg(windows)]
fn lock_file(file: &File, exclusive: bool, timeout: Duration) -> io::Result<()> {
    let deadline = Instant::now() + timeout;
    loop {
        if try_lock_file(file, exclusive)? {
            return Ok(());
        }
        if Instant::now() >= deadline {
            return Err(io::Error::new(
                io::ErrorKind::TimedOut,
                "Timed out waiting for the OpenBox data lock.",
            ));
        }
        std::thread::sleep(Duration::from_millis(50));
    }
}

#[cfg(windows)]
fn try_lock_file(file: &File, exclusive: bool) -> io::Result<bool> {
    use std::os::windows::io::AsRawHandle;
    use windows_sys::Win32::Foundation::ERROR_LOCK_VIOLATION;
    use windows_sys::Win32::Storage::FileSystem::{
        LOCKFILE_EXCLUSIVE_LOCK, LOCKFILE_FAIL_IMMEDIATELY, LockFileEx,
    };
    use windows_sys::Win32::System::IO::OVERLAPPED;

    let flags = LOCKFILE_FAIL_IMMEDIATELY
        | if exclusive {
            LOCKFILE_EXCLUSIVE_LOCK
        } else {
            0
        };
    let mut overlapped: OVERLAPPED = unsafe { std::mem::zeroed() };
    let ok = unsafe { LockFileEx(file.as_raw_handle(), flags, 0, 1, 0, &mut overlapped) };
    if ok != 0 {
        return Ok(true);
    }
    let err = io::Error::last_os_error();
    if err.raw_os_error() == Some(ERROR_LOCK_VIOLATION as i32) {
        Ok(false)
    } else {
        Err(err)
    }
}

#[cfg(windows)]
fn unlock_file(file: &File) -> io::Result<()> {
    use std::os::windows::io::AsRawHandle;
    use windows_sys::Win32::Storage::FileSystem::UnlockFileEx;
    use windows_sys::Win32::System::IO::OVERLAPPED;

    let mut overlapped: OVERLAPPED = unsafe { std::mem::zeroed() };
    let ok = unsafe { UnlockFileEx(file.as_raw_handle(), 0, 1, 0, &mut overlapped) };
    if ok != 0 {
        Ok(())
    } else {
        Err(io::Error::last_os_error())
    }
}
