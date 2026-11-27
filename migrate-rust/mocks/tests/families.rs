use std::pin::pin;
use std::task::{Context, Poll, Waker};

use vectora_test_mocks::arguments::Arguments;
use vectora_test_mocks::browser::{BrowserError, Page};
use vectora_test_mocks::database::SqliteDb;
use vectora_test_mocks::http_session::{Client, Method, Response, Sent};
use vectora_test_mocks::subprocess::{Process, Spawn, Spawner};
use vectora_test_mocks::verify::{AwaitAssertions, CallAssertions};
use vectora_test_mocks::workspace::{Registry, Workspace};

fn ready<F: Future>(future: F) -> F::Output {
    match pin!(future).poll(&mut Context::from_waker(Waker::noop())) {
        Poll::Ready(value) => value,
        Poll::Pending => panic!("fake futures complete on first poll"),
    }
}

#[test]
fn process_records_lifecycle_without_inventing_side_effects() {
    let process: Process<String> = Process::exited(b"ok\n", b"", Some(0));
    let output = ready(process.communicate.call(None)).unwrap();
    assert_eq!(output, (b"ok\n".to_vec(), Vec::new()));
    process.communicate.assert_awaited_once();
    process.kill.assert_not_called();
    process.kill.call(());
    process.terminate.call(());
    process.kill.assert_called_once();
    // Like a MagicMock, signalling does not change the return code.
    assert_eq!(process.returncode(), Some(0));
    process.set_returncode(None);
    assert_eq!(ready(process.wait.call(())), Some(0));
    process.stdin_write.call(b"ping\n".to_vec());
    process
        .stdin_write
        .assert_called_once_with(&b"ping\n".to_vec());
    ready(process.stdin_drain.call(()));
    // Unscripted stdout is end of file.
    assert_eq!(ready(process.stdout_readline.call(())), Ok(Vec::new()));
}

#[test]
fn spawner_records_command_and_returns_the_same_process() {
    let process = std::sync::Arc::new(Process::<String>::exited(b"", b"", None));
    let spawner = Spawner::returning(process.clone());
    spawner.exec.assert_not_awaited();
    let request = Spawn::new(["echo", "hello"]).cwd("/w").env("A", "1");
    let spawned = ready(spawner.exec.call(request.clone())).unwrap();
    assert!(std::sync::Arc::ptr_eq(&spawned, &process));
    spawner.exec.assert_awaited_once_with(&request);
    assert_eq!(spawner.exec.calls()[0].argv[0], "echo");

    let failing: Spawner<String> = Spawner::failing("missing".into());
    assert_eq!(
        ready(failing.exec.call(request)).err(),
        Some("missing".into())
    );
}

#[test]
fn http_session_records_scope_and_requests_and_routes_by_url() {
    let client: Client<String> = Client::routed(
        [("/ok", Ok(Response::json_body(200, r#"{"valid":true}"#)))],
        Ok(Response::json_body(500, "{}")),
    );
    {
        let _session = ready(client.enter());
        let sent = Sent::new(Method::Post, "https://x/ok").json(r#"{"a":1}"#);
        let response = ready(client.send(sent.clone())).unwrap();
        assert_eq!(response.json()["valid"], true);
        assert!(response.raise_for_status().is_ok());
        let other = ready(client.send(Sent::new(Method::Get, "https://x/other"))).unwrap();
        assert_eq!(other.status_code, 500);
        assert_eq!(other.raise_for_status().unwrap_err().status_code, 500);
        assert_eq!(client.sent()[0], sent);
        assert_eq!((client.entered(), client.exited()), (1, 0));
    }
    assert_eq!(client.exited(), 1);
}

#[test]
fn http_transport_errors_are_preserved() {
    let client: Client<String> = Client::failing("connection refused".into());
    let result = ready(client.send(Sent::new(Method::Request("GET".into()), "https://bad")));
    assert_eq!(result.err(), Some("connection refused".into()));
}

#[test]
fn browser_page_updates_url_only_when_navigation_is_awaited() {
    let page = Page::new();
    assert_eq!(page.url(), "about:blank");
    let navigation = |url: &str| Arguments::new([url.into()], [("timeout", 15000.into())]);
    let pending = page.goto(navigation("https://example.com"));
    assert_eq!(page.url(), "about:blank");
    ready(pending).unwrap();
    assert_eq!(page.url(), "https://example.com");
    page.goto_calls
        .assert_awaited_once_with(&navigation("https://example.com"));
    assert_eq!(
        ready(page.title.call(Arguments::default())).unwrap(),
        "Example Domain"
    );
    assert_eq!(
        ready(page.screenshot.call(Arguments::default())).unwrap(),
        b"fake-png-bytes"
    );
    assert_eq!(
        ready(page.inner_text.call(Arguments::default())).unwrap(),
        "Olá mundo"
    );
    page.click.assert_not_awaited();
    ready(page.click.call(Arguments::default())).unwrap();
    page.click.set_fallback(Err(BrowserError("gone".into())));
    assert!(ready(page.click.call(Arguments::default())).is_err());
    assert_eq!(page.click.awaits().len(), 2);
    let broken = Page::new().failing_navigation(BrowserError("net".into()));
    assert!(ready(broken.goto(navigation("https://x"))).is_err());
    assert_eq!(broken.url(), "about:blank");
}

#[test]
fn sqlite_fixture_persists_between_connections_and_is_removed() {
    let schema = "CREATE TABLE t (id TEXT PRIMARY KEY, n INTEGER, note TEXT);";
    let db = SqliteDb::new("bg.db", schema);
    db.connect()
        .execute("INSERT INTO t VALUES ('a', 2, NULL)", [])
        .unwrap();
    let rows = db.rows("SELECT * FROM t");
    assert_eq!(rows.len(), 1);
    assert_eq!(rows[0]["id"], "a");
    assert_eq!(rows[0]["n"], 2);
    assert!(rows[0]["note"].is_null());
    let path = db.path().to_path_buf();
    assert!(path.ends_with("bg.db"));
    drop(db);
    assert!(!path.exists());
}

#[test]
fn trusted_workspace_registry_resolves_only_its_id() {
    let registry = Registry::trusted("vws", "/tmp/w");
    let workspace: Workspace = registry.get.call("vws".into()).unwrap();
    assert!(workspace.trusted);
    assert_eq!(workspace.name, "vws");
    assert_eq!(workspace.cwd, std::path::PathBuf::from("/tmp/w"));
    assert_eq!(workspace.created_at, "2024-01-01T00:00:00+00:00");
    assert!(registry.get.call("other".into()).is_none());
    registry.get.assert_any_call(&"other".into());
}
