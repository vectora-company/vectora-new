//! Scoped replacement of an injected dependency, for `monkeypatch.setattr`
//! and `patch.object`. The consumer reads the dependency through the slot.

use std::sync::{Arc, Mutex};

pub struct Slot<T> {
    state: Mutex<State<T>>,
}

struct State<T> {
    original: Arc<T>,
    patches: Vec<(u64, Arc<T>)>,
    next: u64,
}

impl<T> Slot<T> {
    pub fn new(original: T) -> Self {
        Self {
            state: Mutex::new(State {
                original: Arc::new(original),
                patches: Vec::new(),
                next: 0,
            }),
        }
    }

    /// The most recent patch still alive, or the original value.
    pub fn get(&self) -> Arc<T> {
        let state = self.state.lock().expect("slot state");
        let latest = state.patches.last().map(|(_, value)| value);
        Arc::clone(latest.unwrap_or(&state.original))
    }

    /// Replace the value until the returned guard is dropped.
    #[must_use = "the patch is undone as soon as the guard is dropped"]
    pub fn replace(&self, value: T) -> Patch<'_, T> {
        let mut state = self.state.lock().expect("slot state");
        let id = state.next;
        state.next += 1;
        state.patches.push((id, Arc::new(value)));
        Patch { slot: self, id }
    }
}

pub struct Patch<'a, T> {
    slot: &'a Slot<T>,
    id: u64,
}

impl<T> Drop for Patch<'_, T> {
    fn drop(&mut self) {
        // Remove only this entry, so out-of-order drops cannot revive a value.
        if let Ok(mut state) = self.slot.state.lock() {
            state.patches.retain(|(id, _)| *id != self.id);
        }
    }
}
