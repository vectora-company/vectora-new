//! The `db` fixtures: an isolated SQLite file with a schema applied.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use rusqlite::types::{Value as Sql, ValueRef};
use rusqlite::{Connection, params_from_iter};
use serde_json::Value;

use crate::fixtures::TempDir;

/// A row keyed by column name, like the suite's `dict` row factory.
pub type Row = BTreeMap<String, Value>;

/// Python's `sqlite3` binds booleans as integers; containers are not bindable.
fn bind(value: &Value) -> Sql {
    match value {
        Value::Null => Sql::Null,
        Value::Bool(flag) => Sql::Integer((*flag).into()),
        Value::Number(number) => number.as_i64().map_or_else(
            || Sql::Real(number.as_f64().expect("finite number")),
            Sql::Integer,
        ),
        Value::String(text) => Sql::Text(text.clone()),
        other => panic!("unsupported SQL parameter: {other}"),
    }
}

pub struct SqliteDb {
    path: PathBuf,
    _directory: TempDir,
}

impl SqliteDb {
    /// The consumer supplies the schema, so it is never duplicated here.
    pub fn new(file_name: &str, schema: &str) -> Self {
        let directory = TempDir::new();
        let db = Self {
            path: directory.path().join(file_name),
            _directory: directory,
        };
        db.connect().execute_batch(schema).expect("apply schema");
        db
    }

    pub fn path(&self) -> &Path {
        &self.path
    }

    /// A new connection per call, as the patched `_get_db` does.
    pub fn connect(&self) -> Connection {
        Connection::open(&self.path).expect("open fixture database")
    }

    /// Run and commit one statement with positional parameters.
    pub fn execute(&self, statement: &str, parameters: &[Value]) {
        self.connect()
            .execute(statement, params_from_iter(parameters.iter().map(bind)))
            .expect("execute statement");
    }

    /// Rows of a query with parameters, as a JSON array of objects.
    pub fn query(&self, query: &str, parameters: &[Value]) -> Value {
        self.select(query, parameters)
            .into_iter()
            .map(|row| Value::Object(row.into_iter().collect()))
            .collect()
    }

    /// Run a query without parameters. Blobs become arrays of bytes.
    pub fn rows(&self, query: &str) -> Vec<Row> {
        self.select(query, &[])
    }

    fn select(&self, query: &str, parameters: &[Value]) -> Vec<Row> {
        let connection = self.connect();
        let mut statement = connection.prepare(query).expect("prepare query");
        let names: Vec<String> = statement
            .column_names()
            .into_iter()
            .map(str::to_owned)
            .collect();
        statement
            .query_map(params_from_iter(parameters.iter().map(bind)), |row| {
                names
                    .iter()
                    .enumerate()
                    .map(|(index, name)| {
                        let value = match row.get_ref(index)? {
                            ValueRef::Null => Value::Null,
                            ValueRef::Integer(number) => number.into(),
                            ValueRef::Real(number) => number.into(),
                            ValueRef::Text(text) => String::from_utf8_lossy(text).into(),
                            ValueRef::Blob(bytes) => bytes.to_vec().into(),
                        };
                        Ok((name.clone(), value))
                    })
                    .collect()
            })
            .expect("run query")
            .collect::<Result<_, _>>()
            .expect("read rows")
    }
}
