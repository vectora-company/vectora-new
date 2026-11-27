//! The `client` fixture: `TestClient` requests become backend capabilities.
//!
//! A response is `{"status_code": n, "json": body, "text": body_text}`.

use crate::arguments::Arguments;
use crate::backend::{Backend, Value};

/// `client.<verb>(url, **keywords)`. The capability is the verb and the path;
/// the full URL stays as the first argument.
pub async fn request(backend: &Backend, verb: &str, url: Value, mut arguments: Arguments) -> Value {
    let path = url.as_str().expect("request URL is a string");
    let path = path.split('?').next().unwrap_or_default();
    let capability = format!("api {verb} {path}");
    arguments.positional.insert(0, url);
    backend.call(&capability, arguments).await
}
