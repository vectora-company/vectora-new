//! Shared pytest fixtures.

use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};

/// `tmp_path`: a new empty directory per test. Unlike pytest, which retains
/// recent directories for inspection, it is removed when dropped.
#[derive(Debug)]
pub struct TempDir {
    path: PathBuf,
}

impl TempDir {
    #[allow(clippy::new_without_default)]
    pub fn new() -> Self {
        static NEXT: AtomicU64 = AtomicU64::new(0);
        let root = std::env::temp_dir()
            .canonicalize()
            .expect("system temporary directory");
        loop {
            let sequence = NEXT.fetch_add(1, Ordering::Relaxed);
            let path = root.join(format!("vectora-test-{}-{sequence}", std::process::id()));
            // `create_dir` refuses existing entries, including symlinks.
            match std::fs::create_dir(&path) {
                Ok(()) => return Self { path },
                Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => {}
                Err(error) => panic!("cannot create {}: {error}", path.display()),
            }
        }
    }

    pub fn path(&self) -> &Path {
        &self.path
    }

    /// Owned path, for consumers that take `&PathBuf`.
    pub fn path_buf(&self) -> &PathBuf {
        &self.path
    }

    /// UTF-8 path, for consumers that take `&str`.
    pub fn path_str(&self) -> &str {
        self.path.to_str().expect("temporary path is not UTF-8")
    }
}

impl Drop for TempDir {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.path);
    }
}
