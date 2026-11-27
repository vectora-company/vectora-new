//! Recorded fakes with callable side effects.

use std::sync::Mutex;

pub struct FunctionMock<A, F> {
    calls: Mutex<Vec<A>>,
    effect: F,
}

impl<A: Clone, F> FunctionMock<A, F> {
    pub fn new(effect: F) -> Self {
        Self {
            calls: Mutex::new(Vec::new()),
            effect,
        }
    }

    pub fn call<R>(&self, arguments: A) -> R
    where
        F: Fn(A) -> R,
    {
        self.calls
            .lock()
            .expect("mock calls")
            .push(arguments.clone());
        (self.effect)(arguments)
    }

    pub fn calls(&self) -> Vec<A> {
        self.calls.lock().expect("mock calls").clone()
    }
}

pub struct AsyncFunctionMock<A, F> {
    calls: Mutex<Vec<A>>,
    awaits: FunctionMock<A, F>,
}

impl<A: Clone, F> AsyncFunctionMock<A, F> {
    pub fn new(effect: F) -> Self {
        Self {
            calls: Mutex::new(Vec::new()),
            awaits: FunctionMock::new(effect),
        }
    }

    pub fn call<'a, R, Pending>(&'a self, arguments: A) -> impl Future<Output = R> + 'a
    where
        A: 'a,
        F: Fn(A) -> Pending,
        Pending: Future<Output = R> + 'a,
    {
        self.calls
            .lock()
            .expect("mock calls")
            .push(arguments.clone());
        async move { self.awaits.call(arguments).await }
    }

    pub fn calls(&self) -> Vec<A> {
        self.calls.lock().expect("mock calls").clone()
    }

    pub fn awaits(&self) -> Vec<A> {
        self.awaits.calls()
    }
}
