//! Scripted process operations without spawning child processes.

use crate::AsyncScript;
use std::ffi::OsString;
use std::path::PathBuf;

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Command {
    pub program: OsString,
    pub args: Vec<OsString>,
    pub cwd: Option<PathBuf>,
    pub stdin: Vec<u8>,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Output {
    pub status: Option<i32>,
    pub stdout: Vec<u8>,
    pub stderr: Vec<u8>,
}

pub type CommandRunner<E> = AsyncScript<Command, Result<Output, E>>;
