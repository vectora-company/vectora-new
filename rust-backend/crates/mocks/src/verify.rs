//! `unittest.mock` style assertions over the recorded history of a fake.
//!
//! Each method panics on mismatch, like the Python assertion it replaces.

use std::fmt::Debug;

use crate::function::{AsyncFunctionMock, FunctionMock};
use crate::{AsyncScript, Script};

pub trait CallAssertions<A: Clone + PartialEq + Debug> {
    fn recorded_calls(&self) -> Vec<A>;

    #[track_caller]
    fn assert_called(&self) {
        assert!(!self.recorded_calls().is_empty(), "expected a call");
    }

    #[track_caller]
    fn assert_not_called(&self) {
        let calls = self.recorded_calls();
        assert!(calls.is_empty(), "expected no calls, got {calls:?}");
    }

    #[track_caller]
    fn assert_called_once(&self) {
        let calls = self.recorded_calls();
        assert!(calls.len() == 1, "expected one call, got {calls:?}");
    }

    #[track_caller]
    fn assert_called_with(&self, expected: &A) {
        let calls = self.recorded_calls();
        assert_eq!(calls.last(), Some(expected), "last call differs");
    }

    #[track_caller]
    fn assert_called_once_with(&self, expected: &A) {
        let calls = self.recorded_calls();
        assert_eq!(
            calls,
            std::slice::from_ref(expected),
            "expected one matching call"
        );
    }

    #[track_caller]
    fn assert_any_call(&self, expected: &A) {
        let calls = self.recorded_calls();
        assert!(calls.contains(expected), "{expected:?} not in {calls:?}");
    }
}

pub trait AwaitAssertions<A: Clone + PartialEq + Debug> {
    fn recorded_awaits(&self) -> Vec<A>;

    #[track_caller]
    fn assert_awaited(&self) {
        assert!(!self.recorded_awaits().is_empty(), "expected an await");
    }

    #[track_caller]
    fn assert_not_awaited(&self) {
        let awaits = self.recorded_awaits();
        assert!(awaits.is_empty(), "expected no awaits, got {awaits:?}");
    }

    #[track_caller]
    fn assert_awaited_once(&self) {
        let awaits = self.recorded_awaits();
        assert!(awaits.len() == 1, "expected one await, got {awaits:?}");
    }

    #[track_caller]
    fn assert_awaited_with(&self, expected: &A) {
        let awaits = self.recorded_awaits();
        assert_eq!(awaits.last(), Some(expected), "last await differs");
    }

    #[track_caller]
    fn assert_awaited_once_with(&self, expected: &A) {
        let awaits = self.recorded_awaits();
        assert_eq!(
            awaits,
            std::slice::from_ref(expected),
            "expected one matching await"
        );
    }

    #[track_caller]
    fn assert_any_await(&self, expected: &A) {
        let awaits = self.recorded_awaits();
        assert!(awaits.contains(expected), "{expected:?} not in {awaits:?}");
    }
}

impl<A: Clone + PartialEq + Debug, R: Clone> CallAssertions<A> for Script<A, R> {
    fn recorded_calls(&self) -> Vec<A> {
        self.calls()
    }
}

impl<A: Clone + PartialEq + Debug, R: Clone> CallAssertions<A> for AsyncScript<A, R> {
    fn recorded_calls(&self) -> Vec<A> {
        self.calls()
    }
}

impl<A: Clone + PartialEq + Debug, R: Clone> AwaitAssertions<A> for AsyncScript<A, R> {
    fn recorded_awaits(&self) -> Vec<A> {
        self.awaits()
    }
}

impl<A: Clone + PartialEq + Debug, F> CallAssertions<A> for FunctionMock<A, F> {
    fn recorded_calls(&self) -> Vec<A> {
        self.calls()
    }
}

impl<A: Clone + PartialEq + Debug, F> CallAssertions<A> for AsyncFunctionMock<A, F> {
    fn recorded_calls(&self) -> Vec<A> {
        self.calls()
    }
}

impl<A: Clone + PartialEq + Debug, F> AwaitAssertions<A> for AsyncFunctionMock<A, F> {
    fn recorded_awaits(&self) -> Vec<A> {
        self.awaits()
    }
}
