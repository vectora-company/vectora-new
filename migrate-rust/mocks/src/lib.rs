//! Typed test doubles for migrated Vectora tests.

// The runtime modules, re-exported so doubles and tests share one path.
pub use vectora_runtime::{
    async_sqlite, classes, convert, environment, graph, libraries, natives, object, stdlib, system,
};

pub mod arguments;
pub mod backend;
pub mod browser;
pub mod client;
pub mod clock;
pub mod database;
pub mod doubles;
pub mod fixtures;
pub mod function;
pub mod http;
pub mod http_session;
pub mod patch;
pub mod process;
pub mod provider;
pub mod runtime;
pub mod storage;
pub mod subprocess;
pub mod verify;
pub mod workspace;

use std::collections::VecDeque;
use std::sync::Mutex;

/// Explicit outcomes. An unconfigured call fails immediately and is recorded.
pub struct Script<A, R> {
    state: Mutex<ScriptState<A, R>>,
}

struct ScriptState<A, R> {
    calls: Vec<A>,
    outcomes: VecDeque<R>,
    fallback: Option<R>,
}

impl<A, R: Clone> Script<A, R> {
    pub fn new(outcomes: impl IntoIterator<Item = R>, fallback: Option<R>) -> Self {
        Self {
            state: Mutex::new(ScriptState {
                calls: Vec::new(),
                outcomes: outcomes.into_iter().collect(),
                fallback,
            }),
        }
    }

    pub fn call(&self, arguments: A) -> R {
        let outcome = {
            let mut state = self.state.lock().expect("mock state");
            state.calls.push(arguments);
            state
                .outcomes
                .pop_front()
                .or_else(|| state.fallback.clone())
        };
        outcome.expect("unexpected mock call: scripted outcomes exhausted")
    }

    pub fn calls(&self) -> Vec<A>
    where
        A: Clone,
    {
        self.state.lock().expect("mock state").calls.clone()
    }

    pub fn remaining(&self) -> usize {
        self.state.lock().expect("mock state").outcomes.len()
    }

    /// Forget recorded calls, as `reset_mock()` does.
    pub fn reset_calls(&self) {
        self.state.lock().expect("mock state").calls.clear();
    }

    /// Drop queued outcomes and repeat this one, keeping the call history.
    pub fn set_fallback(&self, outcome: R) {
        let mut state = self.state.lock().expect("mock state");
        state.outcomes.clear();
        state.fallback = Some(outcome);
    }
}

/// Records invocation separately from the first poll of its returned future.
pub struct AsyncScript<A, R> {
    calls: Mutex<Vec<A>>,
    awaited: Script<A, R>,
}

impl<A: Clone, R: Clone> AsyncScript<A, R> {
    pub fn new(outcomes: impl IntoIterator<Item = R>, fallback: Option<R>) -> Self {
        Self {
            calls: Mutex::new(Vec::new()),
            awaited: Script::new(outcomes, fallback),
        }
    }

    pub fn call<'a>(&'a self, arguments: A) -> impl Future<Output = R> + 'a
    where
        A: 'a,
    {
        self.calls
            .lock()
            .expect("mock calls")
            .push(arguments.clone());
        async move { self.awaited.call(arguments) }
    }

    pub fn calls(&self) -> Vec<A> {
        self.calls.lock().expect("mock calls").clone()
    }

    /// Reassigning `return_value` or `side_effect`: later awaits get this
    /// outcome and the recorded history is kept.
    pub fn set_fallback(&self, outcome: R) {
        self.awaited.set_fallback(outcome);
    }

    pub fn awaits(&self) -> Vec<A> {
        self.awaited.calls()
    }

    pub fn remaining(&self) -> usize {
        self.awaited.remaining()
    }
}
