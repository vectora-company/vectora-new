use vectora_test_mocks::arguments::Arguments;
use vectora_test_mocks::backend::{Backend, json, text};
use vectora_test_mocks::client::request;
use vectora_test_mocks::runtime::block_on;

#[test]
fn a_route_is_a_capability_named_without_its_query() {
    let backend = Backend::new().implement("api GET /a/models", |_, arguments| {
        assert_eq!(arguments.positional, [json(r#""/a/models?q=1""#)]);
        json(r#"{"status_code":200,"json":{"ok":true},"text":""}"#)
    });
    let response = block_on(request(
        &backend,
        "GET",
        json(r#""/a/models?q=1""#),
        Arguments::default(),
    ));
    assert_eq!(response["json"]["ok"], true);
}

#[test]
#[should_panic(expected = "TDD pending: capability `api POST /x` is not implemented")]
fn an_unimplemented_route_fails_for_the_expected_reason() {
    let backend = Backend::new();
    block_on(request(
        &backend,
        "POST",
        json(r#""/x""#),
        Arguments::default(),
    ));
}

#[test]
fn the_environment_is_isolated_per_backend() {
    let backend = Backend::new();
    assert!(backend.env("PATH").is_null());
    backend.set_env("A", "1");
    assert_eq!(backend.env("A"), json(r#""1""#));
    backend.remove_env("A");
    assert!(backend.env("A").is_null());
}

#[test]
fn text_formats_values_like_an_f_string() {
    assert_eq!(text(&json(r#""a""#)), "a");
    assert_eq!(text(&json("7")), "7");
}

#[test]
fn containment_follows_python() {
    let contains = |a: &str, b: &str| vectora_test_mocks::backend::contains(&json(a), &json(b));
    assert!(contains(r#""abc""#, r#""bc""#));
    assert!(contains("[1,2]", "2"));
    assert!(contains(r#"{"k":0}"#, r#""k""#));
    assert!(!contains(r#"{"k":"v"}"#, r#""v""#));
}
