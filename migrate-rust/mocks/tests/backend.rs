use std::panic::{AssertUnwindSafe, catch_unwind};
use std::sync::Arc;

use vectora_test_mocks::arguments::Arguments;
use vectora_test_mocks::backend::{Backend, json, mock};
use vectora_test_mocks::browser::Page;
use vectora_test_mocks::runtime::block_on;
use vectora_test_mocks::verify::AwaitAssertions;

#[test]
fn an_unimplemented_capability_fails_for_the_expected_reason() {
    let backend = Backend::new();
    let failure = catch_unwind(AssertUnwindSafe(|| {
        block_on(backend.call("backend.tools.browser.browser_click", Arguments::default()))
    }))
    .unwrap_err();
    let message = failure.downcast_ref::<String>().unwrap();
    assert_eq!(
        message,
        "TDD pending: capability `backend.tools.browser.browser_click` is not implemented"
    );
}

#[test]
fn an_implementation_sees_arguments_patches_and_the_page() {
    let page = Arc::new(Page::new());
    let backend = Backend::new()
        .with_page(page.clone())
        .implement("echo", |backend, arguments| {
            let patched = backend.patched("m.helper").unwrap();
            let url = backend.page().unwrap().url();
            let first = &arguments.keywords["a"];
            json(&format!(r#"[{first},{patched},"{url}"]"#))
        });
    backend.patch("m.helper", json("1"));
    backend.patch("m.helper", json("2"));
    let result = block_on(backend.call("echo", Arguments::new([], [("a", json("true"))])));
    assert_eq!(result, json(r#"[true,2,"about:blank"]"#));
}

#[test]
fn block_on_resumes_a_future_that_was_pending_once() {
    let mut polled = false;
    let value = block_on(std::future::poll_fn(move |context| {
        if polled {
            return std::task::Poll::Ready(5);
        }
        polled = true;
        context.waker().wake_by_ref();
        std::task::Poll::Pending
    }));
    assert_eq!(value, 5);
}

#[test]
fn installed_mocks_record_awaits_and_are_shared_with_the_test() {
    let backend = Backend::new();
    let helper = mock(json("true"));
    backend.patch_mock("m.helper", helper.clone());
    let installed = backend.mocked("m.helper").unwrap();
    let arguments = Arguments::new([json("1")], [("a", json("2"))]);
    assert_eq!(block_on(installed.call(arguments.clone())), json("true"));
    helper.assert_awaited_once_with(&arguments);
}
