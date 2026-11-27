//! The boundary between generated tests and the Rust backend still to be built.
//!
//! A capability without an implementation panics with a fixed message, so a
//! translated test compiles and fails for the expected TDD reason. This fake
//! never produces results on its own.

use std::collections::BTreeMap;
use std::sync::{Arc, Mutex};

pub use serde_json::Value;

use crate::AsyncScript;
use crate::arguments::Arguments;
use crate::browser::Page;
use crate::database::SqliteDb;
use crate::environment::Environment;

/// An `AsyncMock` installed by the test.
pub type Mock = Arc<AsyncScript<Arguments, Value>>;

/// `AsyncMock(return_value=value)`; a bare `AsyncMock()` returns null.
pub fn mock(value: Value) -> Mock {
    Arc::new(AsyncScript::new([], Some(value)))
}

/// Parse a literal emitted by the generator.
pub fn json(text: &str) -> Value {
    serde_json::from_str(text).expect("valid JSON literal")
}

/// A value as an f-string renders it. Only strings and integers occur.
pub fn text(value: &Value) -> String {
    match value {
        Value::String(text) => text.clone(),
        Value::Number(number) if number.is_i64() => number.to_string(),
        other => panic!("unsupported f-string value: {other}"),
    }
}

/// Python `item in container`: substring, element or key.
pub fn contains(container: &Value, item: &Value) -> bool {
    match (container, item) {
        (Value::String(text), Value::String(part)) => text.contains(part.as_str()),
        (Value::Array(items), item) => items.contains(item),
        (Value::Object(fields), Value::String(key)) => fields.contains_key(key),
        (container, item) => panic!("unsupported `in`: {item} in {container}"),
    }
}

/// Python truthiness of a result, for a bare `assert value`.
pub fn truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(flag) => *flag,
        Value::Number(number) => number.as_f64() != Some(0.0),
        Value::String(text) => !text.is_empty(),
        Value::Array(items) => !items.is_empty(),
        Value::Object(fields) => !fields.is_empty(),
    }
}

/// Python `len` of a result.
pub fn length(value: &Value) -> usize {
    match value {
        Value::String(text) => text.chars().count(),
        Value::Array(items) => items.len(),
        Value::Object(fields) => fields.len(),
        other => panic!("value has no len(): {other}"),
    }
}

type Implementation = Box<dyn Fn(&Backend, Arguments) -> Value + Send + Sync>;

#[derive(Default)]
pub struct Backend {
    implementations: BTreeMap<String, Implementation>,
    patches: Mutex<BTreeMap<String, Value>>,
    mocks: Mutex<BTreeMap<String, Mock>>,
    page: Option<Arc<Page>>,
    database: Option<Arc<SqliteDb>>,
    environment: Mutex<Environment>,
}

impl Backend {
    pub fn new() -> Self {
        Self::default()
    }

    /// The page returned by the patched `get_browser_page`.
    pub fn with_page(mut self, page: Arc<Page>) -> Self {
        self.page = Some(page);
        self
    }

    /// The database behind the patched `_get_db`.
    pub fn with_database(mut self, database: Arc<SqliteDb>) -> Self {
        self.database = Some(database);
        self
    }

    pub fn database(&self) -> Option<&SqliteDb> {
        self.database.as_deref()
    }

    pub fn implement(
        mut self,
        capability: &str,
        implementation: impl Fn(&Backend, Arguments) -> Value + Send + Sync + 'static,
    ) -> Self {
        self.implementations
            .insert(capability.to_owned(), Box::new(implementation));
        self
    }

    /// An `AsyncMock` replacing `module.name`, or reachable from a patched
    /// object. The test keeps its own handle to assert on it.
    pub fn patch_mock(&self, target: &str, mock: Mock) {
        self.mocks
            .lock()
            .expect("mocks")
            .insert(target.to_owned(), mock);
    }

    /// Leaving a `with patch(...)` block.
    pub fn unpatch(&self, target: &str) {
        self.mocks.lock().expect("mocks").remove(target);
        self.patches.lock().expect("patches").remove(target);
    }

    pub fn mocked(&self, target: &str) -> Option<Mock> {
        self.mocks.lock().expect("mocks").get(target).cloned()
    }

    /// The value returned by a patched synchronous function.
    pub fn patch(&self, target: &str, value: Value) {
        self.patches
            .lock()
            .expect("patches")
            .insert(target.to_owned(), value);
    }

    pub fn patched(&self, target: &str) -> Option<Value> {
        self.patches.lock().expect("patches").get(target).cloned()
    }

    /// `os.environ[name] = value`, visible only to this test.
    pub fn set_env(&self, name: &str, value: &str) {
        self.environment
            .lock()
            .expect("environment")
            .set(name, value);
    }

    pub fn remove_env(&self, name: &str) {
        let mut environment = self.environment.lock().expect("environment");
        environment.remove(name.as_ref());
    }

    /// The variable, or null when it is absent.
    pub fn env(&self, name: &str) -> Value {
        let environment = self.environment.lock().expect("environment");
        environment
            .get(name.as_ref())
            .map_or(Value::Null, |value| value.to_string_lossy().into())
    }

    pub fn page(&self) -> Option<&Page> {
        self.page.as_deref()
    }

    pub async fn call(&self, capability: &str, arguments: Arguments) -> Value {
        let Some(implementation) = self.implementations.get(capability) else {
            panic!("TDD pending: capability `{capability}` is not implemented");
        };
        implementation(self, arguments)
    }
}
