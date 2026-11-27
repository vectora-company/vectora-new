use std::panic::{AssertUnwindSafe, catch_unwind};

use vectora_test_mocks::object::{self, Args, Flow, Object, Table};
use vectora_test_mocks::{
    async_sqlite, classes, doubles, graph, libraries, natives, stdlib, system,
};

fn class(name: &'static str, bases: &'static [&'static str]) -> Object {
    Object::Class(object::exception_class(name, bases))
}

static TABLE: Table = Table {
    names: &[
        ("builtins.Exception", || class("builtins.Exception", &[])),
        ("builtins.ValueError", || {
            class("builtins.ValueError", &["builtins.Exception"])
        }),
        ("builtins.KeyError", || {
            class("builtins.KeyError", &["builtins.Exception"])
        }),
        ("builtins.AssertionError", || {
            class("builtins.AssertionError", &["builtins.Exception"])
        }),
        ("builtins.len", || object::func("len", natives::len)),
        ("unittest.mock.AsyncMock", || {
            object::func("AsyncMock", doubles::async_mock)
        }),
        ("unittest.mock.MagicMock", || {
            object::func("MagicMock", doubles::magic_mock)
        }),
        ("pytest.raises", || object::func("raises", doubles::raises)),
        ("os.environ", doubles::environ),
    ],
    methods: &[
        ("str.split", natives::str_split),
        ("list.append", natives::list_append),
    ],
    properties: &[("Path.name", natives::path_name)],
    capabilities: &["backend."],
};

fn run(body: impl FnOnce() -> Object) {
    object::run_test(&[&TABLE], "test", body);
}

fn fails(body: impl FnOnce() -> Object) -> String {
    let error = catch_unwind(AssertUnwindSafe(|| run(body))).unwrap_err();
    error.downcast_ref::<String>().cloned().unwrap_or_default()
}

fn s(text: &str) -> Object {
    object::str_(text)
}

#[test]
fn operators_follow_python() {
    run(|| {
        assert!(object::eq(
            &object::add(&Object::Int(1), &Object::Float(0.5)),
            &Object::Float(1.5)
        ));
        assert!(object::eq(&Object::Int(1), &Object::Bool(true)));
        assert!(!object::eq(
            &object::list(vec![Object::Int(1)]),
            &object::tuple(vec![Object::Int(1)])
        ));
        assert!(object::eq(
            &object::floordiv(&Object::Int(-7), &Object::Int(2)),
            &Object::Int(-4)
        ));
        assert!(object::eq(
            &object::mod_(&Object::Int(-7), &Object::Int(2)),
            &Object::Int(1)
        ));
        assert!(object::contains(&s("abc"), &s("bc")));
        let d = object::dict(vec![(s("a"), Object::Int(1))]);
        assert!(object::contains(&d, &s("a")) && !object::contains(&d, &Object::Int(1)));
        assert_eq!(
            object::text(&object::slice(
                &s("hello"),
                &Object::Int(1),
                &Object::Int(-1)
            )),
            "ell"
        );
        Object::None
    });
}

#[test]
fn methods_and_properties_come_from_the_table() {
    run(|| {
        let parts = object::call_method(&s("a,b"), "split", Args::new(vec![s(",")], vec![]));
        assert!(object::eq(&parts, &object::list(vec![s("a"), s("b")])));
        object::call_method(&parts, "append", Args::new(vec![s("c")], vec![]));
        assert!(object::eq(
            &object::call(
                &object::name("builtins.len"),
                Args::new(vec![parts], vec![])
            ),
            &Object::Int(3)
        ));
        let path = Object::from(std::path::PathBuf::from("/tmp/x.txt"));
        assert_eq!(object::text(&object::getattr(&path, "name")), "x.txt");
        Object::None
    });
}

#[test]
fn raised_exceptions_are_caught_by_kind() {
    run(|| {
        let result = object::catch(|| {
            object::raise(object::call(
                &object::name("builtins.ValueError"),
                Args::new(vec![s("bad")], vec![]),
            ))
        });
        let Err(raised) = result else {
            panic!("expected an exception")
        };
        assert!(object::matches(
            &raised,
            &object::name("builtins.ValueError")
        ));
        assert!(object::matches(
            &raised,
            &object::name("builtins.Exception")
        ));
        assert!(!object::matches(
            &raised,
            &object::name("builtins.KeyError")
        ));
        Object::None
    });
}

#[test]
fn pytest_raises_checks_kind_and_message() {
    run(|| {
        let manager = object::call(
            &object::name("pytest.raises"),
            Args::new(
                vec![object::name("builtins.ValueError")],
                vec![("match", s("b.d"))],
            ),
        );
        object::with(&manager, |_| {
            object::raise(object::new_exception("ValueError", vec![s("bad")]))
        });
        Object::None
    });
    let message = fails(|| {
        let manager = object::call(
            &object::name("pytest.raises"),
            Args::new(vec![object::name("builtins.ValueError")], vec![]),
        );
        object::with(&manager, |_| Flow::Normal);
        Object::None
    });
    assert!(message.contains("DID NOT RAISE"), "{message}");
}

#[test]
fn async_mock_separates_calls_from_awaits() {
    run(|| {
        let m = object::call(
            &object::name("unittest.mock.AsyncMock"),
            Args::new(vec![], vec![("return_value", Object::Int(7))]),
        );
        let pending = object::call(&m, Args::new(vec![s("x")], vec![]));
        object::call_method(
            &m,
            "assert_called_once_with",
            Args::new(vec![s("x")], vec![]),
        );
        object::call_method(&m, "assert_not_awaited", Args::none());
        assert!(object::eq(&object::await_(pending), &Object::Int(7)));
        object::call_method(&m, "assert_awaited_once", Args::none());
        assert!(object::eq(
            &object::getattr(&m, "await_count"),
            &Object::Int(1)
        ));
        Object::None
    });
}

#[test]
fn a_failed_mock_assertion_is_an_assertion_error() {
    let message = fails(|| {
        let m = object::call(&object::name("unittest.mock.MagicMock"), Args::none());
        object::call_method(&m, "assert_called_once", Args::none());
        Object::None
    });
    assert!(message.contains("AssertionError"), "{message}");
}

#[test]
fn monkeypatch_is_undone_at_teardown() {
    run(|| {
        let mp = doubles::monkeypatch_fixture();
        let module = object::name("backend.x");
        object::call_method(
            &mp,
            "setattr",
            Args::new(vec![module.clone(), s("f"), Object::Int(1)], vec![]),
        );
        assert!(object::eq(&object::getattr(&module, "f"), &Object::Int(1)));
        object::call_method(
            &mp,
            "setenv",
            Args::new(vec![s("VECTORA_X"), s("1")], vec![]),
        );
        assert!(object::eq(
            &object::getitem(&object::name("os.environ"), &s("VECTORA_X")),
            &s("1")
        ));
        Object::None
    });
    run(|| {
        assert!(matches!(
            object::getattr(&object::name("backend.x"), "f"),
            Object::Name(_)
        ));
        assert!(!object::contains(
            &object::name("os.environ"),
            &s("VECTORA_X")
        ));
        Object::None
    });
}

#[test]
fn a_backend_capability_fails_for_the_expected_reason() {
    let message = fails(|| object::call(&object::name("backend.tools.f"), Args::none()));
    assert!(
        message.contains("TDD pending: capability `backend.tools.f`"),
        "{message}"
    );
}

#[test]
fn generator_fixture_runs_teardown_after_the_test() {
    let log = std::sync::Arc::new(std::sync::Mutex::new(Vec::new()));
    let seen = log.clone();
    run(move || {
        let inner = seen.clone();
        let generator = object::generator(move |y| {
            inner.lock().unwrap().push("setup");
            y.send(Object::Int(5));
            inner.lock().unwrap().push("teardown");
            Object::None
        });
        let value = object::fixture(generator);
        assert!(object::eq(&value, &Object::Int(5)));
        seen.lock().unwrap().push("test");
        Object::None
    });
    assert_eq!(*log.lock().unwrap(), ["setup", "test", "teardown"]);
}

#[test]
fn an_untranslated_fragment_fails_with_its_location() {
    let message = fails(|| object::pending("unit/test_x.py:3", "frobnicate()"));
    assert!(
        message.contains("migration pending: unit/test_x.py:3: frobnicate()"),
        "{message}"
    );
}

#[test]
fn classes_bind_methods_inherit_and_answer_isinstance() {
    run(|| {
        let greet = object::func("greet", |a: Args| {
            object::str_(&format!(
                "hi {}",
                object::text(&object::getattr(&a.pos[0], "who"))
            ))
        });
        let init = object::func("__init__", |a: Args| {
            object::setattr(&a.pos[0], "who", a.pos[1].clone());
            Object::None
        });
        let base = vectora_test_mocks::classes::class(
            "Base",
            vec![],
            vec![("greet", greet), ("__init__", init)],
        );
        let shout = vectora_test_mocks::classes::property(object::func("loud", |a: Args| {
            object::str_(&object::text(&object::getattr(&a.pos[0], "who")).to_uppercase())
        }));
        let child =
            vectora_test_mocks::classes::class("Child", vec![base.clone()], vec![("loud", shout)]);
        let x = object::call(&child, Args::new(vec![s("ana")], vec![]));
        assert_eq!(
            object::text(&object::call_method(&x, "greet", Args::none())),
            "hi ana"
        );
        assert_eq!(object::text(&object::getattr(&x, "loud")), "ANA");
        assert!(object::truthy(&natives::isinstance(Args::new(
            vec![x.clone(), base],
            vec![]
        ))));
        let error = vectora_test_mocks::classes::class(
            "MyError",
            vec![object::name("builtins.ValueError")],
            vec![],
        );
        let raised =
            object::catch(|| object::raise(object::call(&error, Args::new(vec![s("x")], vec![]))))
                .err()
                .unwrap();
        assert!(object::matches(
            &raised,
            &object::name("builtins.ValueError")
        ));
        Object::None
    });
}

#[test]
fn a_repository_module_is_a_capability_too() {
    let message = fails(|| object::call(&object::name("backend.utils.f"), Args::none()));
    assert!(message.contains("TDD pending"), "{message}");
}

#[test]
fn async_sqlite_reads_rows_by_name_and_by_position() {
    run(|| {
        let directory = vectora_test_mocks::fixtures::TempDir::new();
        let path = s(directory.path().join("a.db").to_str().unwrap());
        let db = object::await_(vectora_test_mocks::async_sqlite::connect(Args::new(
            vec![path],
            vec![],
        )));
        object::await_(object::call_method(
            &db,
            "executescript",
            Args::new(vec![s("CREATE TABLE t (id TEXT, n INTEGER);")], vec![]),
        ));
        object::await_(object::call_method(
            &db,
            "execute",
            Args::new(
                vec![
                    s("INSERT INTO t VALUES (?, ?)"),
                    object::tuple(vec![s("a"), Object::Int(2)]),
                ],
                vec![],
            ),
        ));
        object::setattr(&db, "row_factory", Object::Name("aiosqlite.Row".into()));
        let cursor = object::await_(object::call_method(
            &db,
            "execute",
            Args::new(vec![s("SELECT * FROM t")], vec![]),
        ));
        let row = object::await_(object::call_method(&cursor, "fetchone", Args::none()));
        assert!(object::eq(&object::getitem(&row, &s("n")), &Object::Int(2)));
        assert!(object::eq(&object::getitem(&row, &Object::Int(0)), &s("a")));
        object::await_(object::call_method(&db, "close", Args::none()));
        Object::None
    });
}

fn method(target: &Object, name: &str, args: Vec<Object>) -> Object {
    object::call(&object::getattr(target, name), Args::new(args, vec![]))
}

#[test]
fn sockets_and_threads_talk_over_a_real_loopback_port() {
    run(|| {
        let server = system::socket(Args::new(vec![Object::Int(system::AF_INET)], vec![]));
        method(
            &server,
            "bind",
            vec![object::tuple(vec![s("127.0.0.1"), Object::Int(0)])],
        );
        method(&server, "listen", vec![Object::Int(1)]);
        let port = object::getitem(&method(&server, "getsockname", vec![]), &Object::Int(1));
        let serving = server.clone();
        let worker = system::thread(Args::new(
            vec![],
            vec![(
                "target",
                object::func("serve", move |_| {
                    let pair = method(&serving, "accept", vec![]);
                    let connection = object::getitem(&pair, &Object::Int(0));
                    let received = method(&connection, "recv", vec![Object::Int(16)]);
                    method(&connection, "sendall", vec![received]);
                    Object::None
                }),
            )],
        ));
        method(&worker, "start", vec![]);
        let client = system::socket(Args::none());
        method(
            &client,
            "connect",
            vec![object::tuple(vec![s("127.0.0.1"), port])],
        );
        method(&client, "sendall", vec![object::bytes(b"ping")]);
        let echoed = method(&client, "recv", vec![Object::Int(16)]);
        assert!(object::eq(&echoed, &object::bytes(b"ping")));
        method(&worker, "join", vec![Object::Int(1)]);
        Object::None
    });
}

#[test]
fn a_capability_value_used_as_a_program_is_the_expected_red() {
    let message = fails(|| {
        system::popen(Args::new(
            vec![object::list(vec![Object::Name("backend.main".into())])],
            vec![],
        ))
    });
    assert!(
        message.starts_with("TDD pending: capability `backend.main`"),
        "{message}"
    );
}

#[test]
fn a_real_child_process_reports_its_exit_code() {
    run(|| {
        let done = system::run(Args::new(
            vec![object::list(vec![s("sh"), s("-c"), s("exit 3")])],
            vec![],
        ));
        assert!(object::eq(
            &object::getattr(&done, "returncode"),
            &Object::Int(3)
        ));
        Object::None
    });
}

#[test]
fn three_argument_type_dict_view_and_data_frame() {
    run(|| {
        let attrs = object::dict(vec![(s("answer"), Object::Int(42))]);
        let class = natives::type_builtin(Args::new(
            vec![s("Store"), object::tuple(vec![]), attrs],
            vec![],
        ));
        let store = object::call(&class, Args::none());
        assert!(object::eq(
            &object::getattr(&store, "answer"),
            &Object::Int(42)
        ));

        let module = doubles::module_type(Args::new(vec![s("plugin")], vec![]));
        method(
            &object::getattr(&module, "__dict__"),
            "update",
            vec![object::dict(vec![(s("ready"), Object::Bool(true))])],
        );
        assert!(object::eq(
            &object::getattr(&module, "ready"),
            &Object::Bool(true)
        ));

        let frame = doubles::data_frame(Args::new(
            vec![object::list(vec![object::dict(vec![(s("text"), s("a"))])])],
            vec![],
        ));
        let column = object::getitem(&frame, &s("text"));
        assert!(object::eq(&column, &object::list(vec![s("a")])));
        Object::None
    });
}

#[test]
fn httpx_mock_transport_answers_through_the_handler() {
    run(|| {
        let handler = object::func("handler", |args: Args| {
            let request = args.pos[0].clone();
            let path = object::getattr(&object::getattr(&request, "url"), "path");
            libraries::response(Args::new(
                vec![Object::Int(200)],
                vec![("json", object::dict(vec![(s("path"), path)]))],
            ))
        });
        let transport = libraries::mock_transport(Args::new(vec![handler], vec![]));
        let client = libraries::async_client(Args::new(vec![], vec![("transport", transport)]));
        let response = object::await_(method(&client, "get", vec![s("https://api.test/v1/x")]));
        assert!(object::eq(
            &object::getattr(&response, "status_code"),
            &Object::Int(200)
        ));
        let body = method(&response, "json", vec![]);
        assert!(object::eq(&object::getitem(&body, &s("path")), &s("/v1/x")));
        Object::None
    });
}

#[test]
fn git_repo_commits_through_the_git_command() {
    run(|| {
        let directory = doubles::tmp_path_fixture();
        let repo = libraries::repo_init(Args::new(vec![directory.clone()], vec![]));
        std::fs::write(format!("{}/a.txt", object::text(&directory)), "a").unwrap();
        method(
            &object::getattr(&repo, "index"),
            "add",
            vec![object::list(vec![s("a.txt")])],
        );
        let commit = method(
            &object::getattr(&repo, "index"),
            "commit",
            vec![s("initial")],
        );
        let sha = object::text(&object::getattr(&commit, "hexsha"));
        assert_eq!(sha.len(), 40);
        let branch = object::getattr(&object::getattr(&repo, "active_branch"), "name");
        assert!(object::eq(&branch, &s("master")));
        Object::None
    });
}

#[test]
fn graph_keeps_nodes_edges_and_attributes() {
    run(|| {
        let g = graph::directed(Args::none());
        object::call(
            &object::getattr(&g, "add_node"),
            Args::new(vec![s("a")], vec![("kind", s("file"))]),
        );
        method(&g, "add_edge", vec![s("a"), s("b")]);
        assert!(object::eq(
            &method(&g, "number_of_nodes", vec![]),
            &Object::Int(2)
        ));
        assert!(object::truthy(&method(
            &g,
            "has_edge",
            vec![s("a"), s("b")]
        )));
        assert!(!object::truthy(&method(
            &g,
            "has_edge",
            vec![s("b"), s("a")]
        )));
        let attrs = object::getitem(&object::getattr(&g, "nodes"), &s("a"));
        assert!(object::eq(&object::getitem(&attrs, &s("kind")), &s("file")));
        Object::None
    });
}

#[test]
fn sqlite3_returns_rows_without_awaiting() {
    run(|| {
        let directory = doubles::tmp_path_fixture();
        let db = format!("{}/x.db", object::text(&directory));
        let connection = async_sqlite::connect_sync(Args::new(vec![s(&db)], vec![]));
        method(&connection, "execute", vec![s("create table t (v int)")]);
        method(&connection, "execute", vec![s("insert into t values (7)")]);
        let cursor = method(&connection, "execute", vec![s("select v from t")]);
        let row = method(&cursor, "fetchone", vec![]);
        assert!(object::eq(&row, &object::tuple(vec![Object::Int(7)])));
        Object::None
    });
}

#[test]
fn git_config_writer_sets_values() {
    run(|| {
        let directory = doubles::tmp_path_fixture();
        let repo = libraries::repo_init(Args::new(vec![directory], vec![]));
        let writer = method(&repo, "config_writer", vec![]);
        method(&writer, "set_value", vec![s("user"), s("name"), s("Ana")]);
        let reader = method(&repo, "config_reader", vec![]);
        assert!(object::eq(
            &method(&reader, "get_value", vec![s("user"), s("name")]),
            &s("Ana")
        ));
        Object::None
    });
}

#[test]
fn a_capability_in_an_operator_is_the_expected_red() {
    let message =
        fails(|| object::add(&Object::Name("backend.limits.MAX".into()), &Object::Int(1)));
    assert!(
        message.starts_with("TDD pending: capability `backend.limits.MAX`"),
        "{message}"
    );
    let message = fails(|| {
        object::setitem(
            &Object::Name("backend.cache".into()),
            &s("k"),
            Object::Int(1),
        );
        Object::None
    });
    assert!(
        message.starts_with("TDD pending: capability `backend.cache`"),
        "{message}"
    );
}

#[test]
fn datetime_and_timedelta_do_arithmetic() {
    run(|| {
        let now = libraries::datetime_now(Args::none());
        let hour = libraries::timedelta(Args::new(vec![], vec![("hours", Object::Int(1))]));
        let earlier = object::sub(&now, &hour);
        assert!(object::eq(&object::lt(&earlier, &now), &Object::Bool(true)));
        let gap = object::sub(&now, &earlier);
        assert!(object::eq(&gap, &hour));
        let total = method(&gap, "total_seconds", vec![]);
        assert!(object::eq(&total, &Object::Float(3600.0)));
        Object::None
    });
}

#[test]
fn sha256_bytes_io_and_exit_stack() {
    run(|| {
        let digest = stdlib::sha256(Args::new(vec![object::bytes(b"abc")], vec![]));
        assert!(object::eq(
            &method(&digest, "hexdigest", vec![]),
            &s("ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad")
        ));
        let buffer = stdlib::bytes_io(Args::none());
        method(&buffer, "write", vec![object::bytes(b"xy")]);
        assert!(object::eq(
            &method(&buffer, "getvalue", vec![]),
            &object::bytes(b"xy")
        ));
        let stack = stdlib::exit_stack(Args::none());
        let order = object::list(vec![]);
        for item in ["a", "b"] {
            let order = order.clone();
            let push = object::func("push", move |_| {
                natives::list_append(order.clone(), Args::new(vec![s(item)], vec![]));
                Object::None
            });
            method(&stack, "callback", vec![push]);
        }
        object::exit(&stack, None);
        assert!(object::eq(&order, &object::list(vec![s("b"), s("a")])));
        Object::None
    });
}

#[test]
fn super_reaches_the_base_method() {
    run(|| {
        let base = classes::class(
            "Base",
            vec![],
            vec![("hello", object::func("hello", |_| s("base")))],
        );
        let child = classes::class("Child", vec![base], vec![]);
        let instance = object::call(&child, Args::none());
        let proxy = classes::super_(&instance, "Child");
        assert!(object::eq(&method(&proxy, "hello", vec![]), &s("base")));
        Object::None
    });
}

#[test]
fn a_test_class_over_a_backend_class_is_the_expected_red() {
    let message = fails(|| {
        let child = classes::class(
            "Quota",
            vec![Object::Name("backend.services.Quota".into())],
            vec![],
        );
        object::call(&child, Args::none())
    });
    assert!(
        message.starts_with("TDD pending: capability `backend.services.Quota`"),
        "{message}"
    );
}

#[test]
fn source_file_is_relative_to_the_repository() {
    run(|| {
        let file = natives::source_file("/repo/rust-backend", "tests/unit/test_a.py");
        assert!(object::eq(&file, &s("/repo/tests/unit/test_a.py")));
        Object::None
    });
}
