//! Runtime of the translated Vectora backend: the dynamic `Object` value,
//! operators, builtins, classes and the standard and third-party modules the
//! translation maps point at. Written by hand; generation only reads it.

pub mod async_sqlite;
pub mod classes;
pub mod convert;
pub mod environment;
pub mod graph;
pub mod libraries;
pub mod natives;
pub mod object;
pub mod stdlib;
pub mod system;
