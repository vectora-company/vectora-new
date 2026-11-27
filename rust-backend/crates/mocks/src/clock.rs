//! Deterministic clock and sleep recorder.

use std::sync::Mutex;
use std::time::Duration;

#[derive(Default)]
pub struct Clock {
    state: Mutex<State>,
}

#[derive(Default)]
struct State {
    now: Duration,
    sleeps: Vec<Duration>,
}

impl Clock {
    pub fn new(now: Duration) -> Self {
        Self {
            state: Mutex::new(State {
                now,
                sleeps: Vec::new(),
            }),
        }
    }

    pub fn now(&self) -> Duration {
        self.state.lock().expect("clock state").now
    }

    /// Advance virtual time immediately when awaited.
    pub async fn sleep(&self, duration: Duration) {
        let mut state = self.state.lock().expect("clock state");
        state.now = state
            .now
            .checked_add(duration)
            .expect("mock clock overflow");
        state.sleeps.push(duration);
    }

    pub fn sleeps(&self) -> Vec<Duration> {
        self.state.lock().expect("clock state").sleeps.clone()
    }
}
