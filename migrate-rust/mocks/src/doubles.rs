//! `unittest.mock`, `monkeypatch`, `pytest.raises` and friends over `Object`.
//!
//! Call and await assertions reuse `crate::verify`, so their semantics are the
//! same as for the typed fakes.

use std::sync::{Arc, Mutex};

use regex::Regex;

use crate::Script;
use crate::fixtures::TempDir;
use crate::object::{
    Args, Native, Object, Raised, Skipped, await_, call, coroutine, defer, dict, env_get, env_keys,
    env_set, func, getattr_opt, instance, iter, lock, matches, name, native, raise, raise_new,
    replace_attr, str_, text, tuple, type_name,
};
use crate::verify::{AwaitAssertions, CallAssertions};

pub struct Mock {
    name: String,
    asynchronous: bool,
    children: Mutex<Vec<(String, Object)>>,
    return_value: Mutex<Option<Object>>,
    side_effect: Mutex<Option<Object>>,
    /// Remaining items when `side_effect` is a sequence.
    sequence: Mutex<Option<Vec<Object>>>,
    /// Calls and awaits are recorded by the same `Script` the typed fakes use.
    calls: Script<Args, ()>,
    awaits: Script<Args, ()>,
}

impl CallAssertions<Args> for Mock {
    fn recorded_calls(&self) -> Vec<Args> {
        self.calls.calls()
    }
}

impl AwaitAssertions<Args> for Mock {
    fn recorded_awaits(&self) -> Vec<Args> {
        self.awaits.calls()
    }
}

fn mock_of(value: &Object) -> Option<Arc<Mock>> {
    match value {
        Object::Native(n) => n
            .mock_handle()
            .and_then(|handle| handle.downcast::<Mock>().ok()),
        _ => None,
    }
}

/// `MagicMock(**config)` and `AsyncMock(**config)`.
pub fn new_mock(asynchronous: bool, args: Args) -> Object {
    let mock = Arc::new(Mock {
        name: args
            .kwarg("name")
            .map(|n| text(&n))
            .unwrap_or_else(|| "mock".into()),
        asynchronous,
        children: Mutex::new(Vec::new()),
        return_value: Mutex::new(args.kwarg("return_value")),
        side_effect: Mutex::new(None),
        sequence: Mutex::new(None),
        calls: Script::new([], Some(())),
        awaits: Script::new([], Some(())),
    });
    let value = Object::Native(Arc::new(MockHandle(mock.clone())));
    if let Some(effect) = args.kwarg("side_effect") {
        set_side_effect(&mock, effect);
    }
    for (key, v) in &args.kw {
        if !matches!(
            key.as_str(),
            "return_value" | "side_effect" | "name" | "spec" | "new_callable"
        ) {
            lock(&mock.children).push((key.clone(), v.clone()));
        }
    }
    value
}

pub fn magic_mock(args: Args) -> Object {
    new_mock(false, args)
}

pub fn async_mock(args: Args) -> Object {
    new_mock(true, args)
}

fn set_side_effect(mock: &Mock, effect: Object) {
    let sequence = match &effect {
        Object::List(_) | Object::Tuple(_) => Some(iter(&effect)),
        _ => None,
    };
    *lock(&mock.sequence) = sequence;
    *lock(&mock.side_effect) = Some(effect);
}

/// What a call produces, honouring `side_effect` then `return_value`.
fn outcome(mock: &Arc<Mock>, args: Args, this: &Object) -> Object {
    if let Some(sequence) = lock(&mock.sequence).as_mut() {
        if sequence.is_empty() {
            raise_new("StopAsyncIteration", "side_effect exhausted");
        }
        let next = sequence.remove(0);
        return match next {
            Object::Exception(_) | Object::Class(_) => raise(next),
            other => other,
        };
    }
    let effect = lock(&mock.side_effect).clone();
    match effect {
        Some(Object::Exception(_) | Object::Class(_)) => raise(effect.expect("checked")),
        Some(Object::None) | None => return_value(mock, this),
        Some(f) => {
            let produced = await_(call(&f, args));
            match produced {
                Object::Name(n) if n.as_ref() == "unittest.mock.DEFAULT" => {
                    return_value(mock, this)
                }
                other => other,
            }
        }
    }
}

fn return_value(mock: &Arc<Mock>, _this: &Object) -> Object {
    let mut slot = lock(&mock.return_value);
    slot.get_or_insert_with(|| new_mock(false, Args::none()))
        .clone()
}

struct MockHandle(Arc<Mock>);

impl MockHandle {
    fn child(&self, attribute: &str) -> Object {
        let mut children = lock(&self.0.children);
        if let Some((_, v)) = children.iter().find(|(k, _)| k == attribute) {
            return v.clone();
        }
        // Children of an AsyncMock are AsyncMock, except dunder protocol names.
        let child = new_mock(
            self.0.asynchronous && !attribute.starts_with("__")
                || matches!(attribute, "__aenter__" | "__aexit__"),
            Args::none(),
        );
        children.push((attribute.to_owned(), child.clone()));
        child
    }

    fn assertion(&self, attribute: &str) -> Option<Object> {
        let mock = self.0.clone();
        let check: fn(&Mock, Args) = match attribute {
            "assert_called" => |m, _| m.assert_called(),
            "assert_not_called" => |m, _| m.assert_not_called(),
            "assert_called_once" => |m, _| m.assert_called_once(),
            "assert_called_with" => |m, a| m.assert_called_with(&a),
            "assert_called_once_with" => |m, a| m.assert_called_once_with(&a),
            "assert_any_call" => |m, a| m.assert_any_call(&a),
            "assert_awaited" => |m, _| m.assert_awaited(),
            "assert_not_awaited" => |m, _| m.assert_not_awaited(),
            "assert_awaited_once" => |m, _| m.assert_awaited_once(),
            "assert_awaited_with" => |m, a| m.assert_awaited_with(&a),
            "assert_awaited_once_with" => |m, a| m.assert_awaited_once_with(&a),
            "assert_any_await" => |m, a| m.assert_any_await(&a),
            "reset_mock" => |m, _| {
                m.calls.reset_calls();
                m.awaits.reset_calls();
            },
            _ => return None,
        };
        Some(func(attribute, move |args| {
            check(&mock, args);
            Object::None
        }))
    }
}

fn call_object(args: &Args) -> Object {
    let kwargs = dict(args.kw.iter().map(|(k, v)| (str_(k), v.clone())).collect());
    instance(
        "call",
        vec![("args", tuple(args.pos.clone())), ("kwargs", kwargs)],
    )
}

impl Native for MockHandle {
    fn class(&self) -> String {
        if self.0.asynchronous {
            "AsyncMock"
        } else {
            "MagicMock"
        }
        .into()
    }

    fn mock_handle(&self) -> Option<Arc<dyn std::any::Any + Send + Sync>> {
        Some(self.0.clone())
    }

    fn getattr(&self, _this: &Object, attribute: &str) -> Option<Object> {
        let m = &self.0;
        let last = |list: Vec<Args>| list.last().map_or(Object::None, call_object);
        let all = |list: Vec<Args>| crate::object::list(list.iter().map(call_object).collect());
        Some(match attribute {
            "return_value" => return_value(m, _this),
            "side_effect" => lock(&m.side_effect).clone().unwrap_or(Object::None),
            "called" => Object::Bool(!m.calls.calls().is_empty()),
            "call_count" => Object::from(m.calls.calls().len()),
            "call_args" => last(m.calls.calls()),
            "call_args_list" => all(m.calls.calls()),
            "await_count" => Object::from(m.awaits.calls().len()),
            "await_args" => last(m.awaits.calls()),
            "await_args_list" => all(m.awaits.calls()),
            other => return self.assertion(other).or_else(|| Some(self.child(other))),
        })
    }

    fn setattr(&self, attribute: &str, value: Object) -> bool {
        match attribute {
            "return_value" => *lock(&self.0.return_value) = Some(value),
            "side_effect" => set_side_effect(&self.0, value),
            _ => {
                let mut children = lock(&self.0.children);
                children.retain(|(k, _)| k != attribute);
                children.push((attribute.to_owned(), value));
            }
        }
        true
    }

    fn call(&self, this: &Object, args: Args) -> Option<Object> {
        self.0.calls.call(args.clone());
        let mock = self.0.clone();
        if !mock.asynchronous {
            return Some(outcome(&mock, args, this));
        }
        let this = this.clone();
        Some(coroutine(move || {
            mock.awaits.call(args.clone());
            outcome(&mock, args, &this)
        }))
    }

    fn iterate(&self) -> Option<Vec<Object>> {
        Some(Vec::new())
    }

    fn enter(&self, this: &Object) -> Option<Object> {
        let method = self.child(if self.0.asynchronous {
            "__aenter__"
        } else {
            "__enter__"
        });
        let configured = lock(&mock_of(&method)?.return_value).clone();
        Some(match configured {
            Some(_) => await_(call(&method, Args::none())),
            None => this.clone(),
        })
    }

    fn exit(&self, _error: Option<&Raised>) -> bool {
        false
    }
}

// ------------------------------------------------------------- patch

/// `patch("a.b", new=..., return_value=..., side_effect=...)` as context
/// manager. The replacement is a mock unless `new` is given.
pub fn patch(args: Args) -> Object {
    let target = text(&args.bind(0, "target", None));
    let new = args.arg(1, "new");
    let mut config = args.clone();
    config.pos.clear();
    config.kw.retain(|(k, _)| k != "target" && k != "new");
    let asynchronous = config
        .kwarg("new_callable")
        .is_some_and(|c| crate::object::type_name(&c).contains("Async"));
    native(Patch {
        target,
        attribute: None,
        replacement: new,
        config,
        asynchronous,
        undo: Mutex::new(Vec::new()),
    })
}

/// `patch.object(obj, "attr", ...)`.
pub fn patch_object(args: Args) -> Object {
    let object = args.bind(0, "target", None);
    let attribute = text(&args.bind(1, "attribute", None));
    let mut config = args.clone();
    config.pos.drain(..config.pos.len().min(2));
    let new = config.pos.first().cloned().or_else(|| config.kwarg("new"));
    config.pos.clear();
    config.kw.retain(|(k, _)| k != "new");
    native(Patch {
        target: String::new(),
        attribute: Some((object, attribute)),
        replacement: new,
        config,
        asynchronous: false,
        undo: Mutex::new(Vec::new()),
    })
}

/// `patch.dict(os.environ, {...}, clear=False)`.
pub fn patch_dict(args: Args) -> Object {
    let target = match args.bind(0, "in_dict", None) {
        Object::Str(dotted) => name(&dotted),
        other => other,
    };
    let values = args.arg(1, "values").unwrap_or_else(|| dict(Vec::new()));
    let clear = args
        .kwarg("clear")
        .is_some_and(|c| crate::object::truthy(&c));
    native(PatchDict {
        target,
        values,
        extra: args
            .kw
            .into_iter()
            .filter(|(k, _)| k != "clear" && k != "values")
            .collect(),
        clear,
        saved: Mutex::new(None),
    })
}

struct Patch {
    target: String,
    attribute: Option<(Object, String)>,
    replacement: Option<Object>,
    config: Args,
    asynchronous: bool,
    undo: Mutex<Vec<Box<dyn FnOnce() + Send>>>,
}

impl Native for Patch {
    fn class(&self) -> String {
        "_patch".into()
    }

    fn enter(&self, _this: &Object) -> Option<Object> {
        let value = self
            .replacement
            .clone()
            .unwrap_or_else(|| new_mock(self.asynchronous, self.config.clone()));
        let undo: Box<dyn FnOnce() + Send> = match &self.attribute {
            Some((object, attribute)) => replace_attr(object, attribute, value.clone()),
            None => Box::new(crate::object::patch_name(&self.target, value.clone())),
        };
        lock(&self.undo).push(undo);
        Some(value)
    }

    fn exit(&self, _error: Option<&Raised>) -> bool {
        if let Some(undo) = lock(&self.undo).pop() {
            undo();
        }
        false
    }

    fn getattr(&self, this: &Object, attribute: &str) -> Option<Object> {
        let this = this.clone();
        match attribute {
            "start" => Some(func("start", move |_| {
                let Object::Native(n) = &this else {
                    unreachable!()
                };
                n.enter(&this).unwrap_or(Object::None)
            })),
            "stop" => Some(func("stop", move |_| {
                let Object::Native(n) = &this else {
                    unreachable!()
                };
                n.exit(None);
                Object::None
            })),
            _ => None,
        }
    }
}

struct PatchDict {
    target: Object,
    values: Object,
    extra: Vec<(String, Object)>,
    clear: bool,
    saved: Mutex<Option<Vec<(Object, Object)>>>,
}

impl Native for PatchDict {
    fn class(&self) -> String {
        "_patch_dict".into()
    }

    fn enter(&self, _this: &Object) -> Option<Object> {
        let saved = crate::object::items(&self.target);
        *lock(&self.saved) = Some(saved.clone());
        if self.clear {
            for (k, _) in &saved {
                delete_key(&self.target, k);
            }
        }
        for (k, v) in crate::object::items(&self.values) {
            crate::object::setitem(&self.target, &k, v);
        }
        for (k, v) in &self.extra {
            crate::object::setitem(&self.target, &str_(k), v.clone());
        }
        Some(self.target.clone())
    }

    fn exit(&self, _error: Option<&Raised>) -> bool {
        if let Some(saved) = lock(&self.saved).take() {
            for k in iter(&self.target) {
                delete_key(&self.target, &k);
            }
            for (k, v) in saved {
                crate::object::setitem(&self.target, &k, v);
            }
        }
        false
    }
}

fn delete_key(target: &Object, key: &Object) {
    match target {
        Object::Native(n) if n.class() == "os.environ" => {
            env_set(&text(key), None);
        }
        _ => crate::object::delitem(target, key),
    }
}

// ------------------------------------------------------- monkeypatch

struct MonkeyPatch {
    undo: Mutex<Vec<Box<dyn FnOnce() + Send>>>,
}

impl MonkeyPatch {
    fn record(&self, undo: Box<dyn FnOnce() + Send>) {
        lock(&self.undo).push(undo);
    }

    fn set_env(&self, key: String, value: Option<String>) {
        let previous = env_set(&key, value);
        self.record(Box::new(move || {
            env_set(&key, previous);
        }));
    }

    fn method(self: &Arc<Self>, attribute: &str) -> Option<Object> {
        let mp = self.clone();
        let f: fn(&Arc<MonkeyPatch>, Args) -> Object = match attribute {
            "setattr" => |mp, args| {
                if args.pos.len() == 2 || args.arg(2, "value").is_none() && args.pos.len() < 3 {
                    // `setattr("pkg.mod.name", value)`
                    let target = text(&args.bind(0, "target", None));
                    let value = args.bind(1, "name", None);
                    mp.record(Box::new(crate::object::patch_name(&target, value)));
                } else {
                    let object = args.bind(0, "target", None);
                    let attribute = text(&args.bind(1, "name", None));
                    let value = args.bind(2, "value", None);
                    mp.record(replace_attr(&object, &attribute, value));
                }
                Object::None
            },
            "delattr" => |mp, args| {
                let object = args.bind(0, "target", None);
                let attribute = text(&args.bind(1, "name", None));
                mp.record(replace_attr(&object, &attribute, Object::Unbound));
                Object::None
            },
            "setenv" => |mp, args| {
                let key = text(&args.bind(0, "name", None));
                let value = text(&args.bind(1, "value", None));
                mp.set_env(key, Some(value));
                Object::None
            },
            "delenv" => |mp, args| {
                let key = text(&args.bind(0, "name", None));
                let raising = args
                    .kwarg("raising")
                    .is_none_or(|r| crate::object::truthy(&r));
                if raising && env_get(&key).is_none() {
                    raise(crate::object::new_exception("KeyError", vec![str_(&key)]));
                }
                mp.set_env(key, None);
                Object::None
            },
            "setitem" => |mp, args| {
                let target = args.bind(0, "dic", None);
                let key = args.bind(1, "name", None);
                let value = args.bind(2, "value", None);
                let previous = match &target {
                    Object::Native(n) => n.getitem(&key),
                    Object::Dict(_) if crate::object::contains(&target, &key) => {
                        Some(crate::object::getitem(&target, &key))
                    }
                    _ => None,
                };
                crate::object::setitem(&target, &key, value);
                mp.record(Box::new(move || match previous {
                    Some(v) => crate::object::setitem(&target, &key, v),
                    None => delete_key(&target, &key),
                }));
                Object::None
            },
            "delitem" => |mp, args| {
                let target = args.bind(0, "dic", None);
                let key = args.bind(1, "name", None);
                let raising = args
                    .arg(2, "raising")
                    .is_none_or(|r| crate::object::truthy(&r));
                if !raising && !crate::object::truthy(&crate::object::in_(&key, &target)) {
                    return Object::None;
                }
                let previous = crate::object::getitem(&target, &key);
                delete_key(&target, &key);
                mp.record(Box::new(move || {
                    crate::object::setitem(&target, &key, previous)
                }));
                Object::None
            },
            "chdir" => |mp, args| {
                let previous = std::env::current_dir().expect("current dir");
                std::env::set_current_dir(text(&args.bind(0, "path", None))).expect("chdir");
                mp.record(Box::new(move || {
                    let _ = std::env::set_current_dir(previous);
                }));
                Object::None
            },
            "syspath_prepend" => |_, _| Object::None,
            _ => return None,
        };
        Some(func(attribute, move |args| f(&mp, args)))
    }
}

struct MonkeyPatchHandle(Arc<MonkeyPatch>);

impl Native for MonkeyPatchHandle {
    fn class(&self) -> String {
        "MonkeyPatch".into()
    }
    fn getattr(&self, _this: &Object, attribute: &str) -> Option<Object> {
        self.0.method(attribute)
    }
}

/// The `monkeypatch` fixture; every change is undone at teardown.
pub fn monkeypatch_fixture() -> Object {
    let mp = Arc::new(MonkeyPatch {
        undo: Mutex::new(Vec::new()),
    });
    let teardown = mp.clone();
    defer(move || {
        let actions = std::mem::take(&mut *lock(&teardown.undo));
        for undo in actions.into_iter().rev() {
            undo();
        }
    });
    Object::Native(Arc::new(MonkeyPatchHandle(mp)))
}

/// The `tmp_path` fixture, removed at teardown.
pub fn tmp_path_fixture() -> Object {
    let directory = TempDir::new();
    let path = Object::from(directory.path().to_path_buf());
    defer(move || drop(directory));
    path
}

// ---------------------------------------------------------- os.environ

struct Environ;

impl Native for Environ {
    fn class(&self) -> String {
        "os.environ".into()
    }
    fn getitem(&self, key: &Object) -> Option<Object> {
        Some(match env_get(&text(key)) {
            Some(v) => str_(&v),
            None => raise(crate::object::new_exception("KeyError", vec![key.clone()])),
        })
    }
    fn setitem(&self, key: &Object, value: Object) -> bool {
        env_set(&text(key), Some(text(&value)));
        true
    }
    fn contains(&self, item: &Object) -> Option<bool> {
        Some(env_get(&text(item)).is_some())
    }
    fn iterate(&self) -> Option<Vec<Object>> {
        Some(env_keys().iter().map(|k| str_(k)).collect())
    }
    fn getattr(&self, _this: &Object, attribute: &str) -> Option<Object> {
        Some(match attribute {
            "get" => func("get", |args| {
                let key = text(&args.bind(0, "key", None));
                env_get(&key).map_or_else(
                    || args.arg(1, "default").unwrap_or(Object::None),
                    |v| str_(&v),
                )
            }),
            "pop" => func("pop", |args| {
                let key = text(&args.bind(0, "key", None));
                match env_set(&key, None) {
                    Some(v) => str_(&v),
                    None => args.arg(1, "default").unwrap_or_else(|| {
                        raise(crate::object::new_exception("KeyError", vec![str_(&key)]))
                    }),
                }
            }),
            "setdefault" => func("setdefault", |args| {
                let key = text(&args.bind(0, "key", None));
                let default = text(&args.arg(1, "default").unwrap_or_else(|| str_("")));
                let value = env_get(&key).unwrap_or_else(|| {
                    env_set(&key, Some(default.clone()));
                    default
                });
                str_(&value)
            }),
            "copy" => func("copy", |_| {
                dict(
                    env_keys()
                        .iter()
                        .map(|k| (str_(k), str_(&env_get(k).unwrap_or_default())))
                        .collect(),
                )
            }),
            "items" => func("items", |_| {
                crate::object::list(
                    env_keys()
                        .iter()
                        .map(|k| tuple(vec![str_(k), str_(&env_get(k).unwrap_or_default())]))
                        .collect(),
                )
            }),
            "keys" => func("keys", |_| {
                crate::object::list(env_keys().iter().map(|k| str_(k)).collect())
            }),
            _ => return None,
        })
    }
}

pub fn environ() -> Object {
    native(Environ)
}

// ------------------------------------------------ context managers

/// `pytest.raises(Kind, match=...)`: the block must raise a matching error.
pub fn raises(args: Args) -> Object {
    let kind = args.bind(0, "expected_exception", None);
    let pattern = args.kwarg("match").map(|m| text(&m));
    native(Raises {
        kind,
        pattern,
        caught: Arc::new(Mutex::new(None)),
    })
}

struct Raises {
    kind: Object,
    pattern: Option<String>,
    caught: Arc<Mutex<Option<Object>>>,
}

struct ExceptionInfo(Arc<Mutex<Option<Object>>>);

impl Native for ExceptionInfo {
    fn class(&self) -> String {
        "ExceptionInfo".into()
    }
    fn getattr(&self, _this: &Object, attribute: &str) -> Option<Object> {
        let value = lock(&self.0).clone().unwrap_or(Object::None);
        match attribute {
            "value" => Some(value),
            "type" => Some(crate::object::type_of(&value)),
            _ => getattr_opt(&value, attribute),
        }
    }
}

impl Native for Raises {
    fn class(&self) -> String {
        "RaisesContext".into()
    }
    fn enter(&self, _this: &Object) -> Option<Object> {
        Some(native(ExceptionInfo(self.caught.clone())))
    }
    fn exit(&self, error: Option<&Raised>) -> bool {
        let Some(error) = error else {
            raise_new(
                "Failed",
                &format!("DID NOT RAISE {}", crate::object::repr(&self.kind)),
            );
        };
        if !matches(error, &self.kind) {
            return false;
        }
        if let Some(pattern) = &self.pattern {
            let message = text(&error.0);
            let regex = Regex::new(pattern).expect("valid match pattern");
            if !regex.is_match(&message) {
                raise_new(
                    "AssertionError",
                    &format!(
                        "Regex pattern did not match.\n Regex: {pattern:?}\n Input: {message:?}"
                    ),
                );
            }
        }
        *lock(&self.caught) = Some(error.0.clone());
        true
    }
}

/// `contextlib.suppress(*kinds)`.
pub fn suppress(args: Args) -> Object {
    native(Suppress(tuple(args.pos)))
}

struct Suppress(Object);

impl Native for Suppress {
    fn class(&self) -> String {
        "suppress".into()
    }
    fn exit(&self, error: Option<&Raised>) -> bool {
        error.is_some_and(|e| matches(e, &self.0))
    }
}

/// `SimpleNamespace(**attrs)`.
pub use vectora_runtime::stdlib::simple_namespace;

pub fn skip(args: Args) -> Object {
    let reason = args.arg(0, "reason").map(|r| text(&r)).unwrap_or_default();
    std::panic::panic_any(Skipped(reason))
}

pub fn fail(args: Args) -> Object {
    let reason = args.arg(0, "reason").map(|r| text(&r)).unwrap_or_default();
    raise_new("Failed", &reason)
}

/// `pytest.approx(value, rel=..., abs=...)` compared with `==`.
pub fn approx(args: Args) -> Object {
    let expected = args.bind(0, "expected", None);
    let rel = args
        .kwarg("rel")
        .and_then(|r| f64::from_object_number(&r))
        .unwrap_or(1e-6);
    let absolute = args
        .kwarg("abs")
        .and_then(|r| f64::from_object_number(&r))
        .unwrap_or(1e-12);
    instance(
        "approx",
        vec![
            ("expected", expected),
            ("rel", Object::Float(rel)),
            ("abs", Object::Float(absolute)),
        ],
    )
}

trait Number {
    fn from_object_number(value: &Object) -> Option<f64>;
}

impl Number for f64 {
    fn from_object_number(value: &Object) -> Option<f64> {
        match value {
            Object::Int(i) => Some(*i as f64),
            Object::Float(f) => Some(*f),
            _ => None,
        }
    }
}

pub fn is_mock(value: &Object) -> bool {
    mock_of(value).is_some()
}

pub fn mock_name(value: &Object) -> Option<String> {
    mock_of(value).map(|m| m.name.clone())
}

pub fn describe(value: &Object) -> String {
    type_name(value)
}

pub fn lookup(dotted: &str) -> Object {
    name(dotted)
}

/// The `caplog` fixture. Logging of the backend is not captured yet, so the
/// records start and stay empty until the backend emits them.
pub fn caplog_fixture() -> Object {
    let records = crate::object::list(Vec::new());
    let level = func("set_level", |_| Object::None);
    instance(
        "LogCaptureFixture",
        vec![
            ("records", records.clone()),
            ("messages", crate::object::list(Vec::new())),
            ("text", str_("")),
            ("set_level", level),
            (
                "clear",
                func("clear", move |_| {
                    if let Object::List(items) = &records {
                        lock(items).clear();
                    }
                    Object::None
                }),
            ),
            ("at_level", func("at_level", |_| suppress(Args::none()))),
        ],
    )
}

/// `pytest.importorskip(module)`: Python modules are not importable in Rust.
pub fn importorskip(args: Args) -> Object {
    let module = text(&args.bind(0, "modname", None));
    std::panic::panic_any(Skipped(format!("module {module} is not available")))
}

/// `types.ModuleType(name)`: an object whose attributes the test assigns.
pub fn module_type(args: Args) -> Object {
    let name = args.bind(0, "name", None);
    instance("module", vec![("__name__", name)])
}

/// `starlette.requests.Request(scope)`: the scope fields the handlers read.
pub fn starlette_request(args: Args) -> Object {
    let scope = args.bind(0, "scope", None);
    let field = |key: &str, default: Object| {
        crate::natives::dict_get(scope.clone(), Args::new(vec![str_(key), default], vec![]))
    };
    instance(
        "Request",
        vec![
            ("scope", scope.clone()),
            ("method", field("method", str_("GET"))),
            ("headers", dict(Vec::new())),
            ("query_params", dict(Vec::new())),
            ("state", simple_namespace(Args::none())),
            (
                "url",
                simple_namespace(Args::new(vec![], vec![("path", field("path", str_("/")))])),
            ),
        ],
    )
}

/// `pandas.DataFrame(records)`: rows as dicts, for code that reads frames.
struct DataFrame {
    columns: Vec<String>,
    rows: Vec<Object>,
}

pub fn data_frame(args: Args) -> Object {
    let rows = args.arg(0, "data").map(|d| iter(&d)).unwrap_or_default();
    let mut columns: Vec<String> = Vec::new();
    for row in &rows {
        for (key, _) in crate::object::items(row) {
            let key = text(&key);
            if !columns.contains(&key) {
                columns.push(key);
            }
        }
    }
    native(DataFrame { columns, rows })
}

impl DataFrame {
    fn column(&self, name: &str) -> Object {
        let key = str_(name);
        crate::object::list(
            self.rows
                .iter()
                .map(|r| crate::natives::dict_get(r.clone(), Args::new(vec![key.clone()], vec![])))
                .collect(),
        )
    }
}

impl Native for DataFrame {
    fn class(&self) -> String {
        "DataFrame".into()
    }
    fn iterate(&self) -> Option<Vec<Object>> {
        Some(self.columns.iter().map(|c| str_(c)).collect())
    }
    fn getitem(&self, key: &Object) -> Option<Object> {
        Some(self.column(&text(key)))
    }
    fn getattr(&self, _this: &Object, attribute: &str) -> Option<Object> {
        let rows = self.rows.clone();
        Some(match attribute {
            "columns" => crate::object::list(self.columns.iter().map(|c| str_(c)).collect()),
            "empty" => Object::Bool(self.rows.is_empty()),
            "shape" => tuple(vec![
                Object::Int(self.rows.len() as i64),
                Object::Int(self.columns.len() as i64),
            ]),
            "iterrows" => func("iterrows", move |_| {
                crate::object::list(
                    rows.iter()
                        .enumerate()
                        .map(|(i, r)| tuple(vec![Object::Int(i as i64), r.clone()]))
                        .collect(),
                )
            }),
            "to_dict" => func("to_dict", move |_| crate::object::list(rows.clone())),
            "head" | "tail" => func(attribute, {
                let columns = self.columns.clone();
                let tail = attribute == "tail";
                move |args| {
                    let n = match args.arg(0, "n") {
                        Some(Object::Int(n)) => n.max(0) as usize,
                        _ => 5,
                    };
                    let picked = if tail {
                        rows[rows.len().saturating_sub(n)..].to_vec()
                    } else {
                        rows.iter().take(n).cloned().collect()
                    };
                    native(DataFrame {
                        columns: columns.clone(),
                        rows: picked,
                    })
                }
            }),
            _ => return None,
        })
    }
}

/// `sys.modules`: one table per process, so `monkeypatch.setitem` sees the
/// same object on every read and can undo its change.
pub fn modules() -> Object {
    static MODULES: std::sync::OnceLock<Object> = std::sync::OnceLock::new();
    MODULES.get_or_init(|| dict(Vec::new())).clone()
}

/// `tmp_path_factory`: fresh directories under one base for the test.
pub fn tmp_path_factory_fixture() -> Object {
    let base = TempDir::new();
    let root = base.path().to_path_buf();
    defer(move || drop(base));
    let counter = Arc::new(Mutex::new(0usize));
    let base_path = root.clone();
    instance(
        "TempPathFactory",
        vec![
            (
                "mktemp",
                func("mktemp", move |args: Args| {
                    let name = text(&args.bind(0, "basename", None));
                    let numbered = !matches!(args.arg(1, "numbered"), Some(Object::Bool(false)));
                    let mut n = lock(&counter);
                    let directory = if numbered {
                        *n += 1;
                        root.join(format!("{name}{}", *n - 1))
                    } else {
                        root.join(&name)
                    };
                    std::fs::create_dir_all(&directory)
                        .unwrap_or_else(|e| raise_new("OSError", &e.to_string()));
                    Object::from(directory)
                }),
            ),
            (
                "getbasetemp",
                func("getbasetemp", move |_| Object::from(base_path.clone())),
            ),
        ],
    )
}

/// `capsys`: captures `print` and `sys.stdout/stderr.write` during the test.
pub fn capsys_fixture() -> Object {
    *lock(&crate::natives::CAPTURED) = Some((String::new(), String::new()));
    defer(|| *lock(&crate::natives::CAPTURED) = None);
    let read = func("readouterr", |_| {
        let (out, err) = lock(&crate::natives::CAPTURED)
            .as_mut()
            .map(|(o, e)| (std::mem::take(o), std::mem::take(e)))
            .unwrap_or_default();
        native(CaptureResult(str_(&out), str_(&err)))
    });
    instance("CaptureFixture", vec![("readouterr", read)])
}

struct CaptureResult(Object, Object);

impl Native for CaptureResult {
    fn class(&self) -> String {
        "CaptureResult".into()
    }
    fn iterate(&self) -> Option<Vec<Object>> {
        Some(vec![self.0.clone(), self.1.clone()])
    }
    fn getitem(&self, key: &Object) -> Option<Object> {
        match key {
            Object::Int(0) => Some(self.0.clone()),
            Object::Int(1) => Some(self.1.clone()),
            _ => None,
        }
    }
    fn getattr(&self, _this: &Object, attribute: &str) -> Option<Object> {
        match attribute {
            "out" => Some(self.0.clone()),
            "err" => Some(self.1.clone()),
            _ => None,
        }
    }
}

/// `sys.stdout` / `sys.stderr`: `write` goes through `capsys` when active.
pub fn stream(error: bool) -> Object {
    instance(
        if error { "stderr" } else { "stdout" },
        vec![
            (
                "write",
                func("write", move |args: Args| {
                    let data = text(&args.bind(0, "s", None));
                    crate::natives::write_stream(error, &data);
                    Object::Int(data.chars().count() as i64)
                }),
            ),
            ("flush", func("flush", |_| Object::None)),
            ("isatty", func("isatty", |_| Object::Bool(false))),
        ],
    )
}

/// `pytest.mark.<name>(...)` used as a value, as in `requires_x = pytest.mark.skipif(...)`.
pub fn mark(name: &'static str) -> Object {
    func(name, move |args: Args| {
        let reason = args.kwarg("reason").unwrap_or_else(|| str_(""));
        instance(
            "MarkDecorator",
            vec![
                ("name", str_(name)),
                ("args", tuple(args.pos.clone())),
                ("reason", reason),
            ],
        )
    })
}

/// A test decorator evaluated when the test runs: `skip`/`skipif` marks skip
/// it; a context manager such as `patch.dict` is active for the whole test.
pub fn apply_decorator(value: &Object) {
    if let Object::Instance(i) = value
        && i.class == "MarkDecorator"
    {
        let field = |key: &str| getattr_opt(value, key).unwrap_or(Object::None);
        let name = text(&field("name"));
        let condition = iter(&field("args"))
            .first()
            .cloned()
            .unwrap_or(Object::Bool(true));
        if name == "skip" || (name == "skipif" && crate::object::truthy(&condition)) {
            skip(Args::new(vec![field("reason")], vec![]));
        }
        return;
    }
    if let Object::Native(_) = value {
        apply_patch(value);
    }
}

/// `@patch(...)` on a test: active until the test ends; returns the mock.
pub fn apply_patch(value: &Object) -> Object {
    let entered = crate::object::enter(value);
    let manager = value.clone();
    defer(move || {
        crate::object::exit(&manager, None);
    });
    entered
}

/// `contextlib.contextmanager` / `asynccontextmanager`: the generator runs to
/// its `yield` on enter and finishes on exit.
pub fn context_manager(args: Args) -> Object {
    let function = args.bind(0, "func", None);
    func("contextmanager", move |args: Args| {
        let generator = await_(call(&function, args));
        native(GeneratorManager(generator))
    })
}

struct GeneratorManager(Object);

impl Native for GeneratorManager {
    fn class(&self) -> String {
        "_GeneratorContextManager".into()
    }
    fn enter(&self, _this: &Object) -> Option<Object> {
        Some(crate::object::next_value(&self.0))
    }
    fn exit(&self, _error: Option<&Raised>) -> bool {
        let _ = crate::object::next_value(&self.0);
        false
    }
    fn awaited(&self, this: &Object) -> Option<Object> {
        Some(this.clone())
    }
}

/// `request`: the pytest fixture request, without markers or parameters.
pub fn request_fixture() -> Object {
    let node = instance(
        "Node",
        vec![
            ("name", str_("test")),
            (
                "get_closest_marker",
                func("get_closest_marker", |_| Object::None),
            ),
            (
                "iter_markers",
                func("iter_markers", |_| crate::object::list(Vec::new())),
            ),
            ("add_marker", func("add_marker", |_| Object::None)),
        ],
    );
    instance(
        "FixtureRequest",
        vec![
            ("node", node),
            ("param", Object::None),
            ("config", simple_namespace(Args::none())),
            (
                "addfinalizer",
                func("addfinalizer", |args: Args| {
                    let finalizer = args.bind(0, "finalizer", None);
                    defer(move || {
                        call(&finalizer, Args::none());
                    });
                    Object::None
                }),
            ),
            (
                "getfixturevalue",
                func("getfixturevalue", |args: Args| {
                    crate::object::unsupported(&format!(
                        "getfixturevalue({})",
                        text(&args.bind(0, "argname", None))
                    ))
                }),
            ),
        ],
    )
}
