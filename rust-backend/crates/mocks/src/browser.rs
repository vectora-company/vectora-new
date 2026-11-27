//! The Playwright page fake (`_FakePage`) shared by the browser tool tests.

use std::sync::{Arc, Mutex};

use crate::AsyncScript;
use crate::arguments::Arguments;

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct BrowserError(pub String);

pub type Action<R = ()> = AsyncScript<Arguments, Result<R, BrowserError>>;

fn action<R: Clone>(value: R) -> Action<R> {
    AsyncScript::new([], Some(Ok(value)))
}

/// Replace a public field to script other results or errors.
pub struct Page {
    url: Arc<Mutex<String>>,
    pub goto_calls: Action,
    pub title: Action<String>,
    pub screenshot: Action<Vec<u8>>,
    pub click: Action,
    pub fill: Action,
    pub inner_text: Action<String>,
    pub mouse_wheel: Action,
    pub wait_for_selector: Action,
    pub drag_and_drop: Action,
    pub set_input_files: Action,
    pub route: Action,
    pub unroute: Action,
}

impl Default for Page {
    fn default() -> Self {
        Self::new()
    }
}

impl Page {
    pub fn new() -> Self {
        Self {
            url: Arc::new(Mutex::new("about:blank".into())),
            goto_calls: action(()),
            title: action("Example Domain".into()),
            screenshot: action(b"fake-png-bytes".to_vec()),
            click: action(()),
            fill: action(()),
            inner_text: action("Olá mundo".into()),
            mouse_wheel: action(()),
            wait_for_selector: action(()),
            drag_and_drop: action(()),
            set_input_files: action(()),
            route: action(()),
            unroute: action(()),
        }
    }

    pub fn failing_navigation(mut self, error: BrowserError) -> Self {
        self.goto_calls = AsyncScript::new([], Some(Err(error)));
        self
    }

    pub fn url(&self) -> String {
        self.url.lock().expect("page url").clone()
    }

    /// The URL, taken from the first positional argument, changes only when a
    /// successful navigation is awaited.
    pub fn goto(
        &self,
        arguments: Arguments,
    ) -> impl Future<Output = Result<(), BrowserError>> + '_ {
        let target = arguments.positional.first().and_then(|url| url.as_str());
        let target = target.expect("goto needs a URL").to_owned();
        let pending = self.goto_calls.call(arguments);
        async move {
            pending.await?;
            *self.url.lock().expect("page url") = target;
            Ok(())
        }
    }
}
