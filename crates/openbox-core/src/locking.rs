//! ADR-0007 lock-ordering enforcement for state, process, store, and leaf cache locks.
//!
//! In debug & test builds, every acquisition checks a thread-local stack of active lock
//! levels and panics immediately if a thread tries to acquire a lock of equal or lower level
//! than one it already holds (e.g. nesting `PROCESS` and `STATE`, which share level 10).

use std::ops::{Deref, DerefMut};

use parking_lot::{Mutex, MutexGuard, RwLock, RwLockReadGuard, RwLockWriteGuard};

pub mod lock_level {
    /// Outer locks (`PROCESS_LOCK` and `STATE_LOCK`) share level 10, so they can never nest.
    pub const PROCESS: u8 = 10;
    pub const STATE: u8 = 10;
    /// Inside `JsonStateStore`: `_thread_lock -> _file_lock -> _coalesce_lock`.
    pub const STORE_THREAD: u8 = 20;
    pub const STORE_FILE: u8 = 30;
    pub const STORE_COALESCE: u8 = 40;
    /// Leaf cache locks.
    pub const LEAF_CACHE: u8 = 50;
}

#[cfg(debug_assertions)]
mod tracker {
    use std::cell::RefCell;

    thread_local! {
        static HELD: RefCell<Vec<u8>> = const { RefCell::new(Vec::new()) };
    }

    pub fn push(level: u8) {
        HELD.with(|stack| {
            let mut s = stack.borrow_mut();
            if let Some(&top) = s.last() {
                assert!(
                    level > top,
                    "ADR-0007 lock-ordering violation: attempted to acquire level {level} while holding level {top} (held: {s:?})"
                );
            }
            s.push(level);
        });
    }

    pub fn pop(level: u8) {
        HELD.with(|stack| {
            let mut s = stack.borrow_mut();
            if let Some(pos) = s.iter().rposition(|&x| x == level) {
                s.remove(pos);
            }
        });
    }
}

#[cfg(not(debug_assertions))]
mod tracker {
    #[inline(always)]
    pub fn push(_level: u8) {}
    #[inline(always)]
    pub fn pop(_level: u8) {}
}

/// Mutex that enforces ADR-0007 lock hierarchy at runtime in debug/test builds.
#[derive(Debug)]
pub struct OrderedMutex<T, const LEVEL: u8> {
    inner: Mutex<T>,
}

impl<T, const LEVEL: u8> OrderedMutex<T, LEVEL> {
    pub const fn new(value: T) -> Self {
        Self {
            inner: Mutex::new(value),
        }
    }

    pub fn lock(&self) -> OrderedMutexGuard<'_, T, LEVEL> {
        tracker::push(LEVEL);
        let guard = self.inner.lock();
        OrderedMutexGuard { guard }
    }
}

pub struct OrderedMutexGuard<'a, T, const LEVEL: u8> {
    guard: MutexGuard<'a, T>,
}

impl<T, const LEVEL: u8> Deref for OrderedMutexGuard<'_, T, LEVEL> {
    type Target = T;
    fn deref(&self) -> &Self::Target {
        &self.guard
    }
}

impl<T, const LEVEL: u8> DerefMut for OrderedMutexGuard<'_, T, LEVEL> {
    fn deref_mut(&mut self) -> &mut Self::Target {
        &mut self.guard
    }
}

impl<T, const LEVEL: u8> Drop for OrderedMutexGuard<'_, T, LEVEL> {
    fn drop(&mut self) {
        tracker::pop(LEVEL);
    }
}

/// RwLock that enforces ADR-0007 lock hierarchy at runtime in debug/test builds.
#[derive(Debug)]
pub struct OrderedRwLock<T, const LEVEL: u8> {
    inner: RwLock<T>,
}

impl<T, const LEVEL: u8> OrderedRwLock<T, LEVEL> {
    pub const fn new(value: T) -> Self {
        Self {
            inner: RwLock::new(value),
        }
    }

    pub fn read(&self) -> OrderedRwLockReadGuard<'_, T, LEVEL> {
        tracker::push(LEVEL);
        let guard = self.inner.read();
        OrderedRwLockReadGuard { guard }
    }

    pub fn write(&self) -> OrderedRwLockWriteGuard<'_, T, LEVEL> {
        tracker::push(LEVEL);
        let guard = self.inner.write();
        OrderedRwLockWriteGuard { guard }
    }
}

pub struct OrderedRwLockReadGuard<'a, T, const LEVEL: u8> {
    guard: RwLockReadGuard<'a, T>,
}

impl<T, const LEVEL: u8> Deref for OrderedRwLockReadGuard<'_, T, LEVEL> {
    type Target = T;
    fn deref(&self) -> &Self::Target {
        &self.guard
    }
}

impl<T, const LEVEL: u8> Drop for OrderedRwLockReadGuard<'_, T, LEVEL> {
    fn drop(&mut self) {
        tracker::pop(LEVEL);
    }
}

pub struct OrderedRwLockWriteGuard<'a, T, const LEVEL: u8> {
    guard: RwLockWriteGuard<'a, T>,
}

impl<T, const LEVEL: u8> Deref for OrderedRwLockWriteGuard<'_, T, LEVEL> {
    type Target = T;
    fn deref(&self) -> &Self::Target {
        &self.guard
    }
}

impl<T, const LEVEL: u8> DerefMut for OrderedRwLockWriteGuard<'_, T, LEVEL> {
    fn deref_mut(&mut self) -> &mut Self::Target {
        &mut self.guard
    }
}

impl<T, const LEVEL: u8> Drop for OrderedRwLockWriteGuard<'_, T, LEVEL> {
    fn drop(&mut self) {
        tracker::pop(LEVEL);
    }
}
