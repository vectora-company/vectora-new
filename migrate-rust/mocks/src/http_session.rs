//! `httpx.AsyncClient` used as an async context manager, and its responses.

use std::sync::Mutex;

use serde_json::Value;

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Method {
    Get,
    Post,
    /// `client.request(method, ...)`, which tests patch separately.
    Request(String),
}

#[derive(Clone, Debug, PartialEq)]
pub struct Sent {
    pub method: Method,
    pub url: String,
    pub headers: Vec<(String, String)>,
    pub json: Option<Value>,
}

impl Sent {
    pub fn new(method: Method, url: impl Into<String>) -> Self {
        Self {
            method,
            url: url.into(),
            headers: Vec::new(),
            json: None,
        }
    }

    /// Attach a JSON body. Panics on invalid JSON, which is a broken test.
    pub fn json(mut self, body: &str) -> Self {
        self.json = Some(serde_json::from_str(body).expect("valid JSON body"));
        self
    }

    pub fn header(mut self, name: impl Into<String>, value: impl Into<String>) -> Self {
        self.headers.push((name.into(), value.into()));
        self
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Response {
    pub status_code: u16,
    pub headers: Vec<(String, String)>,
    pub text: String,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct StatusError {
    pub status_code: u16,
}

impl Response {
    pub fn json_body(status_code: u16, text: impl Into<String>) -> Self {
        Self {
            status_code,
            headers: Vec::new(),
            text: text.into(),
        }
    }

    pub fn json(&self) -> Value {
        serde_json::from_str(&self.text).expect("response body is not JSON")
    }

    /// The suite's fake responses raise from status 400 upwards. Real `httpx`
    /// also raises for 1xx and 3xx; consumers relying on that need their own.
    pub fn raise_for_status(&self) -> Result<(), StatusError> {
        if self.status_code >= 400 {
            return Err(StatusError {
                status_code: self.status_code,
            });
        }
        Ok(())
    }
}

type Outcome<E> = Result<Response, E>;

pub struct Client<E> {
    routes: Vec<(String, Outcome<E>)>,
    fallback: Outcome<E>,
    sent: Mutex<Vec<Sent>>,
    scope: Mutex<(usize, usize)>,
}

/// Guard for `async with`; dropping it records the exit.
pub struct Session<'a, E> {
    client: &'a Client<E>,
}

impl<E> Drop for Session<'_, E> {
    fn drop(&mut self) {
        if let Ok(mut scope) = self.client.scope.lock() {
            scope.1 += 1;
        }
    }
}

impl<E: Clone> Client<E> {
    /// The first route whose fragment occurs in the URL wins.
    pub fn routed<'a>(
        routes: impl IntoIterator<Item = (&'a str, Outcome<E>)>,
        fallback: Outcome<E>,
    ) -> Self {
        Self {
            routes: routes
                .into_iter()
                .map(|(fragment, outcome)| (fragment.to_owned(), outcome))
                .collect(),
            fallback,
            sent: Mutex::new(Vec::new()),
            scope: Mutex::new((0, 0)),
        }
    }

    pub fn failing(error: E) -> Self {
        Self::routed([], Err(error))
    }

    pub async fn enter(&self) -> Session<'_, E> {
        self.scope.lock().expect("client scope").0 += 1;
        Session { client: self }
    }

    pub async fn send(&self, request: Sent) -> Outcome<E> {
        let outcome = self
            .routes
            .iter()
            .find(|(fragment, _)| request.url.contains(fragment))
            .map_or(&self.fallback, |(_, outcome)| outcome)
            .clone();
        self.sent.lock().expect("sent requests").push(request);
        outcome
    }

    pub fn sent(&self) -> Vec<Sent> {
        self.sent.lock().expect("sent requests").clone()
    }

    pub fn entered(&self) -> usize {
        self.scope.lock().expect("client scope").0
    }

    pub fn exited(&self) -> usize {
        self.scope.lock().expect("client scope").1
    }
}
