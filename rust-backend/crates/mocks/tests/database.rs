use std::sync::Arc;

use vectora_test_mocks::arguments::Arguments;
use vectora_test_mocks::backend::{Backend, json, truthy};
use vectora_test_mocks::database::SqliteDb;
use vectora_test_mocks::runtime::block_on;

const SCHEMA: &str = "CREATE TABLE t (id TEXT, n INTEGER, ratio REAL, note TEXT, flag INTEGER);";

#[test]
fn parameters_bind_like_the_python_driver() {
    let db = SqliteDb::new("p.db", SCHEMA);
    db.execute(
        "INSERT INTO t VALUES (?, ?, ?, ?, ?)",
        &[
            json(r#""a""#),
            json("2"),
            json("1.5"),
            json("null"),
            json("true"),
        ],
    );
    let rows = db.query("SELECT * FROM t WHERE id = ?", &[json(r#""a""#)]);
    assert_eq!(
        rows,
        json(r#"[{"id":"a","n":2,"ratio":1.5,"note":null,"flag":1}]"#)
    );
}

#[test]
fn a_capability_reaches_the_database_of_the_test() {
    let db = Arc::new(SqliteDb::new("c.db", SCHEMA));
    let backend = Backend::new()
        .with_database(db.clone())
        .implement("count", |backend, _| {
            let rows = backend
                .database()
                .unwrap()
                .rows("SELECT count(*) AS n FROM t");
            rows[0]["n"].clone()
        });
    assert_eq!(
        block_on(backend.call("count", Arguments::default())),
        json("0")
    );
}

#[test]
fn truthiness_follows_python() {
    for falsy in ["null", "false", "0", "0.0", r#""""#, "[]", "{}"] {
        assert!(!truthy(&json(falsy)), "{falsy}");
    }
    for value in ["true", "1", "-0.5", r#""a""#, "[0]", r#"{"a":null}"#] {
        assert!(truthy(&json(value)), "{value}");
    }
}
