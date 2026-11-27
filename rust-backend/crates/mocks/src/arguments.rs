//! Arguments of an untyped call, as `unittest.mock` records them: positional
//! and keyword arguments are distinct.

use std::collections::BTreeMap;

use serde_json::Value;

#[derive(Clone, Debug, Default, PartialEq)]
pub struct Arguments {
    pub positional: Vec<Value>,
    pub keywords: BTreeMap<String, Value>,
}

impl Arguments {
    pub fn new<'a>(
        positional: impl IntoIterator<Item = Value>,
        keywords: impl IntoIterator<Item = (&'a str, Value)>,
    ) -> Self {
        Self {
            positional: positional.into_iter().collect(),
            keywords: keywords
                .into_iter()
                .map(|(name, value)| (name.to_owned(), value))
                .collect(),
        }
    }
}
