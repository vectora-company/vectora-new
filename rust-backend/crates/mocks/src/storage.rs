//! A stateful key/value store fake with operation ordering.

use std::collections::BTreeMap;
use std::sync::Mutex;

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Operation<K, V> {
    Get(K),
    Put(K, V),
    Remove(K),
}

pub struct KeyValue<K, V> {
    state: Mutex<State<K, V>>,
}

struct State<K, V> {
    values: BTreeMap<K, V>,
    operations: Vec<Operation<K, V>>,
}

impl<K: Ord + Clone, V: Clone> KeyValue<K, V> {
    pub fn new(values: impl IntoIterator<Item = (K, V)>) -> Self {
        Self {
            state: Mutex::new(State {
                values: values.into_iter().collect(),
                operations: Vec::new(),
            }),
        }
    }

    pub fn get(&self, key: &K) -> Option<V> {
        let mut state = self.state.lock().expect("store state");
        state.operations.push(Operation::Get(key.clone()));
        state.values.get(key).cloned()
    }

    pub fn put(&self, key: K, value: V) -> Option<V> {
        let mut state = self.state.lock().expect("store state");
        state
            .operations
            .push(Operation::Put(key.clone(), value.clone()));
        state.values.insert(key, value)
    }

    pub fn remove(&self, key: &K) -> Option<V> {
        let mut state = self.state.lock().expect("store state");
        state.operations.push(Operation::Remove(key.clone()));
        state.values.remove(key)
    }

    pub fn operations(&self) -> Vec<Operation<K, V>> {
        self.state.lock().expect("store state").operations.clone()
    }
}
