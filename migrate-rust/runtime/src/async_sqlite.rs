//! `aiosqlite` and `sqlite3` over a real SQLite file, through the same
//! `rusqlite` the `db` fixtures use. Operations run when called; with
//! `aiosqlite`, awaiting returns their result.

use std::sync::{Arc, Mutex};

use rusqlite::types::{Value as Sql, ValueRef};

use crate::object::{
    Args, Native, Object, Raised, call, coroutine, func, list, lock, native, raise_new, str_, text,
    tuple,
};

struct Connection {
    inner: Mutex<Option<rusqlite::Connection>>,
    row_factory: Mutex<Object>,
    /// `sqlite3`: results are returned directly instead of awaited.
    sync: bool,
}

/// A result, awaitable for `aiosqlite`.
fn done(sync: bool, value: Object) -> Object {
    if sync {
        value
    } else {
        coroutine(move || value)
    }
}

fn to_sql(value: &Object) -> Sql {
    match value {
        Object::None => Sql::Null,
        Object::Bool(b) => Sql::Integer(i64::from(*b)),
        Object::Int(i) => Sql::Integer(*i),
        Object::Float(f) => Sql::Real(*f),
        Object::Str(s) => Sql::Text(s.to_string()),
        Object::Bytes(b) => Sql::Blob(b.to_vec()),
        other => Sql::Text(text(other)),
    }
}

fn from_sql(value: ValueRef<'_>) -> Object {
    match value {
        ValueRef::Null => Object::None,
        ValueRef::Integer(i) => Object::Int(i),
        ValueRef::Real(f) => Object::Float(f),
        ValueRef::Text(t) => str_(&String::from_utf8_lossy(t)),
        ValueRef::Blob(b) => crate::object::bytes(b),
    }
}

fn error(e: rusqlite::Error) -> ! {
    raise_new("sqlite3.OperationalError", &e.to_string())
}

impl Connection {
    fn with<R>(&self, f: impl FnOnce(&rusqlite::Connection) -> R) -> R {
        let guard = lock(&self.inner);
        let connection = guard
            .as_ref()
            .unwrap_or_else(|| raise_new("ValueError", "no active connection"));
        f(connection)
    }

    /// Run one statement; rows are read at once so the cursor owns them.
    fn execute(self: &Arc<Self>, sql: &str, parameters: &Object) -> Object {
        let parameters: Vec<Sql> = match parameters {
            Object::None => Vec::new(),
            other => crate::object::iter(other).iter().map(to_sql).collect(),
        };
        let (names, rows, rowcount, lastrowid) = self.with(|c| {
            let mut statement = c.prepare(sql).unwrap_or_else(|e| error(e));
            let names: Vec<String> = statement
                .column_names()
                .into_iter()
                .map(str::to_owned)
                .collect();
            let mut rows = Vec::new();
            let mut cursor = statement
                .query(rusqlite::params_from_iter(parameters.iter()))
                .unwrap_or_else(|e| error(e));
            while let Some(row) = cursor.next().unwrap_or_else(|e| error(e)) {
                let values: Vec<Object> = (0..names.len())
                    .map(|i| from_sql(row.get_ref(i).unwrap_or_else(|e| error(e))))
                    .collect();
                rows.push(values);
            }
            (names, rows, c.changes() as i64, c.last_insert_rowid())
        });
        let factory = lock(&self.row_factory).clone();
        let cursor = Arc::new(Cursor {
            description: list(
                names
                    .iter()
                    .map(|n| tuple(vec![str_(n), Object::None]))
                    .collect(),
            ),
            names,
            rows: Mutex::new(rows.into()),
            factory,
            rowcount,
            lastrowid,
            sync: self.sync,
        });
        Object::Native(Arc::new(CursorHandle(cursor)))
    }
}

struct Cursor {
    names: Vec<String>,
    description: Object,
    rows: Mutex<std::collections::VecDeque<Vec<Object>>>,
    factory: Object,
    rowcount: i64,
    lastrowid: i64,
    sync: bool,
}

impl Cursor {
    fn shape(&self, this: &Object, values: Vec<Object>) -> Object {
        match &self.factory {
            Object::None => tuple(values),
            Object::Name(n) if n.ends_with(".Row") => native(Row {
                names: self.names.clone(),
                values,
            }),
            factory => call(
                factory,
                Args::new(vec![this.clone(), tuple(values)], vec![]),
            ),
        }
    }

    fn next(&self, this: &Object) -> Object {
        let next = lock(&self.rows).pop_front();
        next.map_or(Object::None, |values| self.shape(this, values))
    }

    fn all(&self, this: &Object) -> Object {
        let rows: Vec<Vec<Object>> = lock(&self.rows).drain(..).collect();
        list(rows.into_iter().map(|v| self.shape(this, v)).collect())
    }
}

struct CursorHandle(Arc<Cursor>);

impl Native for CursorHandle {
    fn class(&self) -> String {
        "Cursor".into()
    }
    fn awaited(&self, this: &Object) -> Option<Object> {
        Some(this.clone())
    }
    fn enter(&self, this: &Object) -> Option<Object> {
        Some(this.clone())
    }
    fn iterate(&self) -> Option<Vec<Object>> {
        Some(crate::object::iter(&self.0.all(&Object::None)))
    }
    fn getattr(&self, this: &Object, attribute: &str) -> Option<Object> {
        let cursor = self.0.clone();
        let this = this.clone();
        let sync = self.0.sync;
        Some(match attribute {
            "fetchone" => func("fetchone", move |_| done(sync, cursor.next(&this))),
            "fetchall" => func("fetchall", move |_| done(sync, cursor.all(&this))),
            "close" => func("close", move |_| done(sync, Object::None)),
            "description" => self.0.description.clone(),
            "rowcount" => Object::Int(self.0.rowcount),
            "lastrowid" => Object::Int(self.0.lastrowid),
            _ => return None,
        })
    }
}

/// `aiosqlite.Row`: read by column name or by position.
struct Row {
    names: Vec<String>,
    values: Vec<Object>,
}

impl Native for Row {
    fn class(&self) -> String {
        "Row".into()
    }
    fn getitem(&self, key: &Object) -> Option<Object> {
        Some(match key {
            Object::Int(i) => self
                .values
                .get(*i as usize)
                .cloned()
                .unwrap_or_else(|| raise_new("IndexError", "row index")),
            other => {
                let name = text(other);
                let position = self
                    .names
                    .iter()
                    .position(|n| *n == name)
                    .unwrap_or_else(|| raise_new("IndexError", &format!("no column {name}")));
                self.values[position].clone()
            }
        })
    }
    fn iterate(&self) -> Option<Vec<Object>> {
        Some(self.values.clone())
    }
    fn getattr(&self, _this: &Object, attribute: &str) -> Option<Object> {
        let names = self.names.clone();
        (attribute == "keys").then(|| {
            func("keys", move |_| {
                list(names.iter().map(|n| str_(n)).collect())
            })
        })
    }
}

struct ConnectionHandle(Arc<Connection>);

impl Native for ConnectionHandle {
    fn class(&self) -> String {
        "Connection".into()
    }
    fn awaited(&self, this: &Object) -> Option<Object> {
        Some(this.clone())
    }
    fn enter(&self, this: &Object) -> Option<Object> {
        Some(this.clone())
    }
    /// `aiosqlite` closes at the end of `async with`; `sqlite3` only commits.
    fn exit(&self, _error: Option<&Raised>) -> bool {
        if !self.0.sync {
            lock(&self.0.inner).take();
        }
        false
    }
    fn setattr(&self, attribute: &str, value: Object) -> bool {
        if attribute == "row_factory" {
            *lock(&self.0.row_factory) = value;
            return true;
        }
        false
    }
    fn getattr(&self, _this: &Object, attribute: &str) -> Option<Object> {
        let connection = self.0.clone();
        let sync = self.0.sync;
        Some(match attribute {
            "execute" => func("execute", move |args| {
                let sql = text(&args.bind(0, "sql", None));
                let parameters = args.arg(1, "parameters").unwrap_or(Object::None);
                connection.execute(&sql, &parameters)
            }),
            "executemany" => func("executemany", move |args| {
                let sql = text(&args.bind(0, "sql", None));
                for parameters in crate::object::iter(&args.bind(1, "parameters", None)) {
                    connection.execute(&sql, &parameters);
                }
                done(sync, Object::None)
            }),
            "executescript" => func("executescript", move |args| {
                let script = text(&args.bind(0, "sql_script", None));
                connection.with(|c| c.execute_batch(&script).unwrap_or_else(|e| error(e)));
                done(sync, Object::None)
            }),
            "commit" | "rollback" => func(attribute, move |_| done(sync, Object::None)),
            "close" => func("close", move |_| {
                lock(&connection.inner).take();
                done(sync, Object::None)
            }),
            "row_factory" => lock(&self.0.row_factory).clone(),
            _ => return None,
        })
    }
}

/// `aiosqlite.connect(path)`.
pub fn connect(args: Args) -> Object {
    let path = text(&args.bind(0, "database", None));
    let inner = rusqlite::Connection::open(&path).unwrap_or_else(|e| error(e));
    native_connection(inner, false)
}

/// `sqlite3.connect(path)`.
pub fn connect_sync(args: Args) -> Object {
    let path = text(&args.bind(0, "database", None));
    let inner = rusqlite::Connection::open(&path).unwrap_or_else(|e| error(e));
    native_connection(inner, true)
}

fn native_connection(inner: rusqlite::Connection, sync: bool) -> Object {
    Object::Native(Arc::new(ConnectionHandle(Arc::new(Connection {
        inner: Mutex::new(Some(inner)),
        row_factory: Mutex::new(Object::None),
        sync,
    }))))
}
