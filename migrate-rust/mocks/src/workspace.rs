//! The `trusted_ws` fixture: one trusted workspace behind a patched registry.

use std::path::PathBuf;

use crate::function::FunctionMock;

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Workspace {
    pub id: String,
    pub name: String,
    pub cwd: PathBuf,
    pub created_at: String,
    pub trusted: bool,
}

pub type Lookup = Box<dyn Fn(String) -> Option<Workspace> + Send + Sync>;

/// `workspace_registry.get`
pub struct Registry {
    pub get: FunctionMock<String, Lookup>,
}

impl Registry {
    pub fn trusted(id: impl Into<String>, cwd: impl Into<PathBuf>) -> Self {
        let id = id.into();
        let workspace = Workspace {
            id: id.clone(),
            name: id,
            cwd: cwd.into(),
            created_at: "2024-01-01T00:00:00+00:00".into(),
            trusted: true,
        };
        Self {
            get: FunctionMock::new(Box::new(move |requested| {
                (requested == workspace.id).then(|| workspace.clone())
            })),
        }
    }
}
