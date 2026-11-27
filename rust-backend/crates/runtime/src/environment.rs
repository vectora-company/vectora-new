//! Per-instance environment overrides without changing process state.

use std::collections::BTreeMap;
use std::ffi::{OsStr, OsString};

#[derive(Clone, Debug, Default)]
pub struct Environment {
    values: BTreeMap<OsString, OsString>,
}

impl Environment {
    pub fn new(values: impl IntoIterator<Item = (OsString, OsString)>) -> Self {
        Self {
            values: values.into_iter().collect(),
        }
    }

    pub fn set(&mut self, name: impl Into<OsString>, value: impl Into<OsString>) {
        self.values.insert(name.into(), value.into());
    }

    pub fn remove(&mut self, name: &OsStr) -> Option<OsString> {
        self.values.remove(name)
    }

    pub fn get(&self, name: &OsStr) -> Option<&OsStr> {
        self.values.get(name).map(OsString::as_os_str)
    }

    pub fn names(&self) -> impl Iterator<Item = &OsStr> {
        self.values.keys().map(OsString::as_os_str)
    }
}
