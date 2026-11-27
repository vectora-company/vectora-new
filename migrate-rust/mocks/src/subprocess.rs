//! `asyncio.create_subprocess_exec` and the process object the suite mocks.
//!
//! Mirrors `MagicMock` processes: signals are only recorded and never change
//! the return code; tests set it explicitly.

use std::ffi::OsString;
use std::path::PathBuf;
use std::sync::{Arc, Mutex};

use crate::{AsyncScript, Script};

/// `(stdout, stderr)`, as returned by `communicate`.
pub type Output = (Vec<u8>, Vec<u8>);

pub struct Process<E> {
    returncode: Mutex<Option<i32>>,
    /// Argument is the optional input bytes.
    pub communicate: AsyncScript<Option<Vec<u8>>, Result<Output, E>>,
    pub wait: AsyncScript<(), Option<i32>>,
    pub kill: Script<(), ()>,
    pub terminate: Script<(), ()>,
    pub stdin_write: Script<Vec<u8>, ()>,
    pub stdin_drain: AsyncScript<(), ()>,
    /// An empty line is end of file.
    pub stdout_readline: AsyncScript<(), Result<Vec<u8>, E>>,
}

impl<E: Clone> Process<E> {
    pub fn exited(stdout: &[u8], stderr: &[u8], returncode: Option<i32>) -> Self {
        let output = (stdout.to_vec(), stderr.to_vec());
        Self {
            returncode: Mutex::new(returncode),
            communicate: AsyncScript::new([], Some(Ok(output))),
            wait: AsyncScript::new([], Some(returncode)),
            kill: Script::new([], Some(())),
            terminate: Script::new([], Some(())),
            stdin_write: Script::new([], Some(())),
            stdin_drain: AsyncScript::new([], Some(())),
            stdout_readline: AsyncScript::new([], Some(Ok(Vec::new()))),
        }
    }

    pub fn returncode(&self) -> Option<i32> {
        *self.returncode.lock().expect("process state")
    }

    pub fn set_returncode(&self, value: Option<i32>) {
        *self.returncode.lock().expect("process state") = value;
    }
}

#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Spawn {
    pub argv: Vec<OsString>,
    pub cwd: Option<PathBuf>,
    /// `None` inherits the parent environment.
    pub env: Option<Vec<(OsString, OsString)>>,
}

impl Spawn {
    pub fn new(argv: impl IntoIterator<Item = impl Into<OsString>>) -> Self {
        Self {
            argv: argv.into_iter().map(Into::into).collect(),
            ..Self::default()
        }
    }

    pub fn cwd(mut self, cwd: impl Into<PathBuf>) -> Self {
        self.cwd = Some(cwd.into());
        self
    }

    pub fn env(mut self, name: impl Into<OsString>, value: impl Into<OsString>) -> Self {
        self.env
            .get_or_insert_default()
            .push((name.into(), value.into()));
        self
    }
}

pub struct Spawner<E> {
    pub exec: AsyncScript<Spawn, Result<Arc<Process<E>>, E>>,
}

impl<E> Spawner<E> {
    fn with(outcome: Result<Arc<Process<E>>, E>) -> Self
    where
        E: Clone,
    {
        // `Arc` is cloneable whatever the process holds.
        Self {
            exec: AsyncScript::new([], Some(outcome)),
        }
    }

    pub fn returning(process: Arc<Process<E>>) -> Self
    where
        E: Clone,
    {
        Self::with(Ok(process))
    }

    pub fn failing(error: E) -> Self
    where
        E: Clone,
    {
        Self::with(Err(error))
    }
}
