use std::panic::{AssertUnwindSafe, catch_unwind};
use vectora_test_mocks::fixtures::TempDir;
use vectora_test_mocks::patch::Slot;
use vectora_test_mocks::verify::{AwaitAssertions, CallAssertions};
use vectora_test_mocks::{AsyncScript, Script};

fn fails(check: impl FnOnce()) -> bool {
    catch_unwind(AssertUnwindSafe(check)).is_err()
}

#[test]
fn call_assertions_follow_unittest_mock() {
    let mock: Script<u8, ()> = Script::new([], Some(()));
    mock.assert_not_called();
    assert!(fails(|| mock.assert_called_once()));
    assert!(fails(|| mock.assert_called_with(&1)));
    mock.call(1);
    mock.assert_called_once();
    mock.assert_called_once_with(&1);
    mock.call(2);
    // `assert_called_with` only looks at the most recent call.
    mock.assert_called_with(&2);
    assert!(fails(|| mock.assert_called_with(&1)));
    mock.assert_any_call(&1);
    assert!(fails(|| mock.assert_called_once_with(&2)));
    assert!(fails(|| mock.assert_not_called()));
}

#[test]
fn await_assertions_ignore_calls_that_were_never_polled() {
    let mock: AsyncScript<u8, ()> = AsyncScript::new([], Some(()));
    let pending = mock.call(7);
    mock.assert_called_once_with(&7);
    mock.assert_not_awaited();
    assert!(fails(|| mock.assert_awaited_once()));
    drop(pending);
    mock.assert_not_awaited();
    assert!(fails(|| mock.assert_awaited_with(&7)));
}

#[test]
fn patched_slot_is_restored_on_drop() {
    let slot = Slot::new("original");
    {
        let _patch = slot.replace("patched");
        assert_eq!(*slot.get(), "patched");
    }
    assert_eq!(*slot.get(), "original");
}

#[test]
fn patches_restore_correctly_when_dropped_out_of_order() {
    let slot = Slot::new(0);
    let first = slot.replace(1);
    let second = slot.replace(2);
    drop(first);
    assert_eq!(*slot.get(), 2);
    drop(second);
    assert_eq!(*slot.get(), 0);
}

#[test]
fn patch_is_undone_when_the_test_panics() {
    let slot = Slot::new(0);
    assert!(fails(|| {
        let _patch = slot.replace(1);
        panic!("boom");
    }));
    assert_eq!(*slot.get(), 0);
}

#[test]
fn temp_dirs_are_empty_unique_and_removed() {
    let first = TempDir::new();
    let second = TempDir::new();
    assert_ne!(first.path(), second.path());
    assert!(first.path().is_absolute());
    assert_eq!(std::fs::read_dir(first.path()).unwrap().count(), 0);
    std::fs::create_dir(first.path().join("nested")).unwrap();
    std::fs::write(first.path().join("nested/file"), b"x").unwrap();
    let removed = first.path().to_path_buf();
    drop(first);
    assert!(!removed.exists());
    assert!(second.path().exists());
}
