//! Conversions at the boundary of typed signatures, driven by the type map.

use std::collections::BTreeMap;
use std::path::PathBuf;

use crate::object::{Object, dict, items, iter, list, raise_new, str_, text, type_name};

pub trait FromObject: Sized {
    fn from_object(value: &Object) -> Option<Self>;
}

/// Convert a returned or received value to the annotated Rust type.
pub fn to<T: FromObject>(value: &Object, location: &str) -> T {
    T::from_object(value).unwrap_or_else(|| {
        crate::object::require_value(value);
        if matches!(value, Object::List(_) | Object::Tuple(_)) {
            iter(value).iter().for_each(crate::object::require_value);
        }
        raise_new(
            "TypeError",
            &format!(
                "{location}: {} does not match {}",
                type_name(value),
                std::any::type_name::<T>()
            ),
        )
    })
}

impl FromObject for Object {
    fn from_object(value: &Object) -> Option<Self> {
        Some(value.clone())
    }
}

impl FromObject for () {
    fn from_object(value: &Object) -> Option<Self> {
        matches!(value, Object::None).then_some(())
    }
}

impl FromObject for bool {
    fn from_object(value: &Object) -> Option<Self> {
        match value {
            Object::Bool(b) => Some(*b),
            _ => None,
        }
    }
}

impl FromObject for i64 {
    fn from_object(value: &Object) -> Option<Self> {
        match value {
            Object::Int(i) => Some(*i),
            Object::Bool(b) => Some(i64::from(*b)),
            _ => None,
        }
    }
}

impl FromObject for f64 {
    fn from_object(value: &Object) -> Option<Self> {
        match value {
            Object::Float(f) => Some(*f),
            Object::Int(i) => Some(*i as f64),
            _ => None,
        }
    }
}

impl FromObject for String {
    fn from_object(value: &Object) -> Option<Self> {
        match value {
            Object::Str(s) => Some(s.to_string()),
            _ => None,
        }
    }
}

impl FromObject for Vec<u8> {
    fn from_object(value: &Object) -> Option<Self> {
        match value {
            Object::Bytes(b) => Some(b.to_vec()),
            _ => None,
        }
    }
}

impl FromObject for PathBuf {
    fn from_object(value: &Object) -> Option<Self> {
        match value {
            Object::Path(p) => Some(p.as_ref().clone()),
            Object::Str(s) => Some(PathBuf::from(s.as_ref())),
            _ => None,
        }
    }
}

impl<T: FromObject> FromObject for Option<T> {
    fn from_object(value: &Object) -> Option<Self> {
        match value {
            Object::None => Some(None),
            other => T::from_object(other).map(Some),
        }
    }
}

impl<T: FromObject> FromObject for Vec<T> {
    fn from_object(value: &Object) -> Option<Self> {
        match value {
            Object::List(_) | Object::Tuple(_) => iter(value).iter().map(T::from_object).collect(),
            _ => None,
        }
    }
}

impl<V: FromObject> FromObject for BTreeMap<String, V> {
    fn from_object(value: &Object) -> Option<Self> {
        match value {
            Object::Dict(_) => items(value)
                .iter()
                .map(|(k, v)| Some((String::from_object(k)?, V::from_object(v)?)))
                .collect(),
            _ => None,
        }
    }
}

impl From<()> for Object {
    fn from((): ()) -> Self {
        Object::None
    }
}

impl From<bool> for Object {
    fn from(value: bool) -> Self {
        Object::Bool(value)
    }
}

impl From<i64> for Object {
    fn from(value: i64) -> Self {
        Object::Int(value)
    }
}

impl From<usize> for Object {
    fn from(value: usize) -> Self {
        Object::Int(value as i64)
    }
}

impl From<f64> for Object {
    fn from(value: f64) -> Self {
        Object::Float(value)
    }
}

impl From<&str> for Object {
    fn from(value: &str) -> Self {
        str_(value)
    }
}

impl From<String> for Object {
    fn from(value: String) -> Self {
        str_(&value)
    }
}

impl From<Vec<u8>> for Object {
    fn from(value: Vec<u8>) -> Self {
        crate::object::bytes(&value)
    }
}

impl From<PathBuf> for Object {
    fn from(value: PathBuf) -> Self {
        Object::Path(std::sync::Arc::new(value))
    }
}

impl<T: Into<Object>> From<Option<T>> for Object {
    fn from(value: Option<T>) -> Self {
        value.map_or(Object::None, Into::into)
    }
}

impl<T: Into<Object>> From<Vec<T>> for Object {
    fn from(value: Vec<T>) -> Self {
        list(value.into_iter().map(Into::into).collect())
    }
}

impl<V: Into<Object>> From<BTreeMap<String, V>> for Object {
    fn from(value: BTreeMap<String, V>) -> Self {
        dict(
            value
                .into_iter()
                .map(|(k, v)| (str_(&k), v.into()))
                .collect(),
        )
    }
}

/// Used in messages only.
pub fn describe(value: &Object) -> String {
    text(value)
}
