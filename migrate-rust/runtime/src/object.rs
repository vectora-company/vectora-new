//! Dynamic values and operations used by the translated tests.
//!
//! Generated code keeps dynamic values (`Object`) inside function bodies.
//! Names, methods and exception classes come from the translation maps through
//! a table installed before each test. Exceptions are panics carrying a
//! `Raised` payload, so `try`, `with` and `pytest.raises` can catch them.

use std::collections::BTreeMap;
use std::fmt;
use std::panic::{AssertUnwindSafe, catch_unwind, resume_unwind};
use std::path::PathBuf;
use std::sync::mpsc;
use std::sync::{Arc, Mutex, MutexGuard, Once};

pub use crate::convert::{FromObject, to};

/// A native function: arguments in, value out.
pub type NativeFn = Arc<dyn Fn(Args) -> Object + Send + Sync>;

/// Objects implemented in Rust: mocks, context managers, `os.environ`.
pub trait Native: Send + Sync {
    fn class(&self) -> String;
    fn getattr(&self, _this: &Object, _name: &str) -> Option<Object> {
        None
    }
    fn setattr(&self, _name: &str, _value: Object) -> bool {
        false
    }
    fn getitem(&self, _key: &Object) -> Option<Object> {
        None
    }
    fn setitem(&self, _key: &Object, _value: Object) -> bool {
        false
    }
    fn contains(&self, _item: &Object) -> Option<bool> {
        None
    }
    fn iterate(&self) -> Option<Vec<Object>> {
        None
    }
    fn call(&self, _this: &Object, _args: Args) -> Option<Object> {
        None
    }
    fn enter(&self, _this: &Object) -> Option<Object> {
        None
    }
    /// Returns whether the error is suppressed.
    fn exit(&self, _error: Option<&Raised>) -> bool {
        false
    }
    /// The recorded state, when the object is a `unittest.mock` double.
    fn mock_handle(&self) -> Option<Arc<dyn std::any::Any + Send + Sync>> {
        None
    }
    /// `next()`; `Some(None)` when exhausted, `None` when not an iterator.
    fn next_value(&self) -> Option<Option<Object>> {
        None
    }
    /// The class, when the object is a class defined by a translated test.
    fn class_object(&self) -> Option<Arc<crate::classes::ClassObject>> {
        None
    }
    /// The value of `await` on this object, when it is awaitable.
    fn awaited(&self, _this: &Object) -> Option<Object> {
        None
    }
    /// `property`, `staticmethod` or `classmethod` wrapping a function.
    /// `str(value)`, when it is not the default representation.
    fn text(&self) -> Option<String> {
        None
    }
    /// `value == other`, for natives that compare by content.
    fn equals(&self, _other: &Object) -> Option<bool> {
        None
    }
    fn descriptor(&self) -> Option<(&'static str, Object)> {
        None
    }
    /// `self <op> other` (or `other <op> self` when `reflected`).
    fn binary(&self, _op: &str, _other: &Object, _reflected: bool) -> Option<Object> {
        None
    }
    /// A number that orders values of this native among themselves.
    fn order_value(&self) -> Option<f64> {
        None
    }
}

pub struct Class {
    pub name: String,
    pub bases: Vec<Arc<Class>>,
}

impl Class {
    pub fn is(&self, name: &str) -> bool {
        self.name == name
            || self.name.rsplit('.').next() == Some(name)
            || self.bases.iter().any(|base| base.is(name))
    }
}

pub struct Exception {
    pub class: Arc<Class>,
    pub args: Vec<Object>,
    pub attrs: Mutex<Vec<(String, Object)>>,
}

pub struct Instance {
    pub class: String,
    pub attrs: Mutex<Vec<(String, Object)>>,
}

type Deferred = Box<dyn FnOnce() -> Object + Send>;

#[derive(Clone)]
pub enum Object {
    None,
    Bool(bool),
    Int(i64),
    Float(f64),
    Str(Arc<str>),
    Bytes(Arc<Vec<u8>>),
    List(Arc<Mutex<Vec<Object>>>),
    Tuple(Arc<Vec<Object>>),
    Dict(Arc<Mutex<Vec<(Object, Object)>>>),
    Set(Arc<Mutex<Vec<Object>>>),
    Path(Arc<PathBuf>),
    Func(Arc<str>, NativeFn),
    Class(Arc<Class>),
    Exception(Arc<Exception>),
    Instance(Arc<Instance>),
    Native(Arc<dyn Native>),
    /// A dotted name with no translation yet: a backend capability, a module,
    /// or a library API without a map entry.
    Name(Arc<str>),
    Coroutine(Arc<Mutex<Option<Deferred>>>),
    Unbound,
}

/// Positional and keyword arguments, as `unittest.mock` records them.
#[derive(Clone, Default)]
pub struct Args {
    pub pos: Vec<Object>,
    pub kw: Vec<(String, Object)>,
}

impl Args {
    pub fn new(pos: Vec<Object>, kw: Vec<(&str, Object)>) -> Self {
        Self {
            pos,
            kw: kw.into_iter().map(|(k, v)| (k.to_owned(), v)).collect(),
        }
    }

    pub fn none() -> Self {
        Self::default()
    }

    /// `*xs` and `**kw` spread into a call.
    pub fn spread(mut self, star: Option<Object>, double: Option<Object>) -> Self {
        if let Some(items) = star {
            self.pos.extend(iter(&items));
        }
        if let Some(Object::Dict(pairs)) = double {
            for (k, v) in lock(&pairs).iter() {
                self.kw.push((text(k), v.clone()));
            }
        }
        self
    }

    pub fn kwarg(&self, name: &str) -> Option<Object> {
        self.kw
            .iter()
            .find(|(k, _)| k == name)
            .map(|(_, v)| v.clone())
    }

    pub fn arg(&self, index: usize, name: &str) -> Option<Object> {
        self.pos.get(index).cloned().or_else(|| self.kwarg(name))
    }

    /// Bind a declared parameter like a Python call would.
    pub fn bind(&self, index: usize, name: &str, default: Option<Object>) -> Object {
        self.arg(index, name)
            .or(default)
            .unwrap_or_else(|| raise_new("TypeError", &format!("missing argument '{name}'")))
    }

    /// `*args` after `consumed` positional parameters.
    pub fn rest(&self, consumed: usize) -> Object {
        tuple(self.pos.iter().skip(consumed).cloned().collect())
    }

    /// `**kwargs` excluding declared names.
    pub fn rest_kw(&self, declared: &[&str]) -> Object {
        dict(
            self.kw
                .iter()
                .filter(|(k, _)| !declared.contains(&k.as_str()))
                .map(|(k, v)| (str_(k), v.clone()))
                .collect(),
        )
    }
}

impl PartialEq for Args {
    fn eq(&self, other: &Self) -> bool {
        self.pos.len() == other.pos.len()
            && self.pos.iter().zip(&other.pos).all(|(a, b)| eq(a, b))
            && self.kw.len() == other.kw.len()
            && self
                .kw
                .iter()
                .all(|(k, v)| other.kwarg(k).is_some_and(|o| eq(v, &o)))
    }
}

impl fmt::Debug for Args {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        let mut parts: Vec<String> = self.pos.iter().map(repr).collect();
        parts.extend(self.kw.iter().map(|(k, v)| format!("{k}={}", repr(v))));
        write!(f, "call({})", parts.join(", "))
    }
}

impl fmt::Debug for Object {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&repr(self))
    }
}

impl PartialEq for Object {
    fn eq(&self, other: &Self) -> bool {
        eq(self, other)
    }
}

/// Lock that ignores poisoning: a test that raised keeps its state readable.
pub fn lock<T>(mutex: &Mutex<T>) -> MutexGuard<'_, T> {
    mutex.lock().unwrap_or_else(|poison| poison.into_inner())
}

// ---------------------------------------------------------------- errors

/// Panic payload for a raised Python exception.
pub struct Raised(pub Object);

/// Panic payload for `pytest.skip`.
pub struct Skipped(pub String);

pub fn raise(exception: Object) -> ! {
    let exception = match exception {
        Object::Class(_) => call(&exception, Args::none()),
        other => other,
    };
    std::panic::panic_any(Raised(exception))
}

pub fn raise_new(class: &str, message: &str) -> ! {
    raise(new_exception(class, vec![str_(message)]))
}

pub fn new_exception(class: &str, args: Vec<Object>) -> Object {
    let class = match (name(class), name(&format!("builtins.{class}"))) {
        (Object::Class(class), _) | (_, Object::Class(class)) => class,
        _ => exception_class(class, &["builtins.Exception"]),
    };
    Object::Exception(Arc::new(Exception {
        class,
        args,
        attrs: Mutex::new(Vec::new()),
    }))
}

pub fn exception_class(name: &str, bases: &[&str]) -> Arc<Class> {
    let bases = bases
        .iter()
        .filter(|base| **base != name)
        .map(|base| match self::name(base) {
            Object::Class(class) => class,
            _ => Arc::new(Class {
                name: (*base).to_owned(),
                bases: Vec::new(),
            }),
        })
        .collect();
    Arc::new(Class {
        name: name.to_owned(),
        bases,
    })
}

/// A Rust panic that is not a Python exception becomes one, so assertion
/// failures inside fakes can still be caught by `pytest.raises`.
pub fn as_raised(
    payload: Box<dyn std::any::Any + Send>,
) -> Result<Raised, Box<dyn std::any::Any + Send>> {
    if payload.is::<Raised>() {
        return Ok(*payload.downcast::<Raised>().expect("checked"));
    }
    if payload.is::<Skipped>() {
        return Err(payload);
    }
    let message = payload
        .downcast_ref::<String>()
        .cloned()
        .or_else(|| payload.downcast_ref::<&str>().map(|s| (*s).to_owned()));
    match message {
        Some(message)
            if !message.starts_with("TDD pending") && !message.starts_with("migration pending") =>
        {
            Ok(Raised(new_exception(
                "AssertionError",
                vec![str_(&message)],
            )))
        }
        _ => Err(payload),
    }
}

/// Control flow leaving a block run inside a closure (`try`, `with`).
pub enum Flow {
    Normal,
    Return(Object),
    Break,
    Continue,
}

/// Run a block and capture a raised exception.
pub fn catch(block: impl FnOnce() -> Flow) -> Result<Flow, Raised> {
    match catch_unwind(AssertUnwindSafe(block)) {
        Ok(flow) => Ok(flow),
        Err(payload) => match as_raised(payload) {
            Ok(raised) => Err(raised),
            Err(other) => resume_unwind(other),
        },
    }
}

pub fn reraise(raised: Raised) -> ! {
    std::panic::panic_any(raised)
}

/// `except Kind` matching, with a tuple of kinds allowed.
pub fn matches(raised: &Raised, kind: &Object) -> bool {
    match (&raised.0, kind) {
        (Object::Exception(e), Object::Class(class)) => e.class.is(&class.name),
        (_, Object::Tuple(kinds)) => kinds.iter().any(|k| matches(raised, k)),
        (_, Object::Name(n)) if n.ends_with("Exception") || n.ends_with("Error") => match &raised.0
        {
            Object::Exception(e) => e.class.is(n.rsplit('.').next().unwrap_or(n)),
            _ => false,
        },
        _ => false,
    }
}

/// `with` statement: enter, run, exit; an unsuppressed error re-raises.
pub fn with(manager: &Object, block: impl FnOnce(Object) -> Flow) -> Flow {
    let entered = enter(manager);
    let result = catch(|| block(entered));
    match result {
        Ok(flow) => {
            exit(manager, None);
            flow
        }
        Err(raised) => {
            if exit(manager, Some(&raised)) {
                Flow::Normal
            } else {
                reraise(raised)
            }
        }
    }
}

pub fn enter(manager: &Object) -> Object {
    match manager {
        Object::Native(n) => n.enter(manager).unwrap_or_else(|| manager.clone()),
        _ => match getattr_opt(manager, "__enter__").or_else(|| getattr_opt(manager, "__aenter__"))
        {
            Some(method) => await_(call(&method, Args::none())),
            None => unsupported(&format!("{}.__enter__", type_name(manager))),
        },
    }
}

pub fn exit(manager: &Object, error: Option<&Raised>) -> bool {
    match manager {
        Object::Native(n) => n.exit(error),
        _ => match getattr_opt(manager, "__exit__").or_else(|| getattr_opt(manager, "__aexit__")) {
            Some(method) => {
                let args = match error {
                    Some(r) => vec![type_of(&r.0), r.0.clone(), Object::None],
                    None => vec![Object::None, Object::None, Object::None],
                };
                truthy(&await_(call(&method, Args::new(args, vec![]))))
            }
            None => false,
        },
    }
}

pub fn type_of(value: &Object) -> Object {
    match value {
        Object::Exception(e) => Object::Class(e.class.clone()),
        other => Object::Name(type_name(other).into()),
    }
}

// --------------------------------------------------- names and test state

#[derive(Default)]
struct State {
    names: BTreeMap<String, Object>,
    methods: BTreeMap<String, Object>,
    /// Stacks of patched values, keyed by dotted name.
    patches: BTreeMap<String, Vec<Object>>,
    deferred: Vec<Box<dyn FnOnce() + Send>>,
    environment: crate::environment::Environment,
    capabilities: Vec<&'static str>,
}

static STATE: Mutex<Option<State>> = Mutex::new(None);
static TEST: Mutex<()> = Mutex::new(());

fn state<R>(f: impl FnOnce(&mut State) -> R) -> R {
    let mut guard = lock(&STATE);
    f(guard.get_or_insert_with(State::default))
}

/// The table generated from the maps: Python names and type methods.
/// A mapped name and the function that builds its value.
pub type NameEntry = (&'static str, fn() -> Object);
/// A mapped method: receiver and arguments in, value out.
pub type MethodEntry = (&'static str, fn(Object, Args) -> Object);

pub struct Table {
    pub names: &'static [NameEntry],
    pub methods: &'static [MethodEntry],
    /// Methods read as attributes, like `Path.parent`.
    pub properties: &'static [MethodEntry],
    /// Dotted prefixes of the code under migration: calling a name under one
    /// of them is a backend capability, the expected TDD red.
    pub capabilities: &'static [&'static str],
}

fn install(tables: &[&Table]) {
    state(|s| s.names.clear());
    // Built outside the lock: exception classes look up their bases.
    let mut names = BTreeMap::new();
    for (k, f) in tables.iter().flat_map(|t| t.names.iter()) {
        let value = f();
        state(|s| s.names.insert((*k).to_owned(), value.clone()));
        names.insert((*k).to_owned(), value);
    }
    let methods: BTreeMap<String, Object> = tables
        .iter()
        .flat_map(|t| t.methods.iter())
        .map(|(k, f)| ((*k).to_owned(), method_value(k, *f)))
        .chain(
            tables
                .iter()
                .flat_map(|t| t.properties.iter())
                .map(|(k, f)| (format!("@{k}"), method_value(k, *f))),
        )
        .collect();
    state(|s| {
        s.names = names;
        s.methods = methods;
        s.patches.clear();
        s.deferred.clear();
        s.environment = crate::environment::Environment::new(std::env::vars_os());
        s.capabilities = tables
            .iter()
            .flat_map(|t| t.capabilities.iter().copied())
            .collect();
    });
}

fn method_value(name: &str, f: fn(Object, Args) -> Object) -> Object {
    // Stored unbound; `getattr` binds the receiver.
    let f = Arc::new(move |args: Args| {
        let mut args = args;
        let this = args.pos.remove(0);
        f(this, args)
    });
    Object::Func(name.into(), f)
}

/// Resolve a dotted Python name: patched value, mapped name, or the name itself.
pub fn name(dotted: &str) -> Object {
    state(|s| {
        if let Some(value) = s.patches.get(dotted).and_then(|stack| stack.last()) {
            return Some(value.clone());
        }
        s.names.get(dotted).cloned()
    })
    .unwrap_or_else(|| Object::Name(dotted.into()))
}

/// Replace a dotted name until the test ends or `undo` runs.
pub fn patch_name(dotted: &str, value: Object) -> impl FnOnce() + Send + 'static {
    state(|s| s.patches.entry(dotted.to_owned()).or_default().push(value));
    let dotted = dotted.to_owned();
    move || {
        state(|s| {
            if let Some(stack) = s.patches.get_mut(&dotted) {
                stack.pop();
            }
        })
    }
}

/// Run at the end of the test, in reverse order.
pub fn defer(action: impl FnOnce() + Send + 'static) {
    state(|s| s.deferred.push(Box::new(action)));
}

pub fn env_get(key: &str) -> Option<String> {
    state(|s| {
        s.environment
            .get(std::ffi::OsStr::new(key))
            .map(|v| v.to_string_lossy().into_owned())
    })
}

pub fn env_set(key: &str, value: Option<String>) -> Option<String> {
    let previous = env_get(key);
    state(|s| match value {
        Some(v) => s.environment.set(key, v),
        None => {
            s.environment.remove(std::ffi::OsStr::new(key));
        }
    });
    previous
}

pub fn env_keys() -> Vec<String> {
    state(|s| {
        s.environment
            .names()
            .map(|name| name.to_string_lossy().into_owned())
            .collect()
    })
}

static HOOK: Once = Once::new();

/// Run one translated test. Tests of a binary run one at a time, because the
/// runtime state is shared with fixture generators running on other threads.
pub fn run_test(tables: &[&Table], label: &str, body: impl FnOnce() -> Object) {
    HOOK.call_once(|| {
        let previous = std::panic::take_hook();
        std::panic::set_hook(Box::new(move |info| {
            let payload = info.payload();
            if !payload.is::<Raised>() && !payload.is::<Skipped>() {
                previous(info);
            }
        }));
    });
    let _serial = lock(&TEST);
    install(tables);
    let outcome = catch_unwind(AssertUnwindSafe(|| await_(body())));
    let deferred = state(|s| std::mem::take(&mut s.deferred));
    for action in deferred.into_iter().rev() {
        action();
    }
    if let Err(payload) = outcome {
        if let Some(skip) = payload.downcast_ref::<Skipped>() {
            println!("SKIPPED {label}: {}", skip.0);
            return;
        }
        match as_raised(payload) {
            Ok(Raised(exception)) => panic!("{label}: uncaught {}", repr(&exception)),
            Err(other) => resume_unwind(other),
        }
    }
}

/// A trecho without a translation rule.
pub fn pending(location: &str, source: &str) -> Object {
    panic!("migration pending: {location}: {source}")
}

/// A local shared with nested functions through `nonlocal`: one slot that
/// every clone of the closure sees.
pub fn cell(value: Object) -> Object {
    list(vec![value])
}

pub fn cell_get(cell: &Object, name: &str) -> Object {
    let value = getitem(cell, &Object::Int(0));
    local(&value, name)
}

pub fn cell_set(cell: &Object, value: Object) {
    setitem(cell, &Object::Int(0), value);
}

/// Fail as the expected TDD red when `value` names a backend capability, for
/// doubles that need the value itself (a program to run, a path to open).
pub fn require_value(value: &Object) {
    if let Object::Name(path) = value
        && state(|s| s.capabilities.iter().any(|p| path.starts_with(p)))
    {
        panic!("TDD pending: capability `{path}` is not implemented")
    }
}

/// Raises a Python error, unless an operand is a backend capability used as
/// data: that reaches the expected TDD red instead.
pub fn raise_new_with(operands: &[&Object], class: &str, message: &str) -> ! {
    for operand in operands {
        require_value(operand);
    }
    raise_new(class, message)
}

/// A Python API reached at run time that has no map entry.
pub fn unsupported(api: &str) -> Object {
    panic!("migration pending api: {api}")
}

// ------------------------------------------------------- construction

pub fn str_(text: &str) -> Object {
    Object::Str(text.into())
}

pub fn bytes(data: &[u8]) -> Object {
    Object::Bytes(Arc::new(data.to_vec()))
}

pub fn list(items: Vec<Object>) -> Object {
    Object::List(Arc::new(Mutex::new(items)))
}

pub fn tuple(items: Vec<Object>) -> Object {
    Object::Tuple(Arc::new(items))
}

pub fn set(items: Vec<Object>) -> Object {
    let mut unique: Vec<Object> = Vec::new();
    for item in items {
        if !unique.iter().any(|u| eq(u, &item)) {
            unique.push(item);
        }
    }
    Object::Set(Arc::new(Mutex::new(unique)))
}

pub fn dict(pairs: Vec<(Object, Object)>) -> Object {
    let mut out: Vec<(Object, Object)> = Vec::new();
    for (k, v) in pairs {
        match out.iter_mut().find(|(ok, _)| eq(ok, &k)) {
            Some(slot) => slot.1 = v,
            None => out.push((k, v)),
        }
    }
    Object::Dict(Arc::new(Mutex::new(out)))
}

/// `{**a, "k": v}`: entries and spreads in order.
pub fn dict_spread(parts: Vec<Result<(Object, Object), Object>>) -> Object {
    let mut pairs = Vec::new();
    for part in parts {
        match part {
            Ok(pair) => pairs.push(pair),
            Err(source) => pairs.extend(items(&source)),
        }
    }
    dict(pairs)
}

pub fn func(name: &str, f: impl Fn(Args) -> Object + Send + Sync + 'static) -> Object {
    Object::Func(name.into(), Arc::new(f))
}

pub fn instance(class: &str, attrs: Vec<(&str, Object)>) -> Object {
    Object::Instance(Arc::new(Instance {
        class: class.to_owned(),
        attrs: Mutex::new(attrs.into_iter().map(|(k, v)| (k.to_owned(), v)).collect()),
    }))
}

pub fn native(object: impl Native + 'static) -> Object {
    Object::Native(Arc::new(object))
}

/// The body of an `async def`, run when awaited.
pub fn coroutine(body: impl FnOnce() -> Object + Send + 'static) -> Object {
    Object::Coroutine(Arc::new(Mutex::new(Some(Box::new(body)))))
}

/// `await value`. Awaiting a finished coroutine twice is an error, as in Python.
pub fn await_(value: Object) -> Object {
    match value {
        Object::Coroutine(cell) => {
            let body = lock(&cell).take();
            match body {
                Some(body) => await_(body()),
                None => raise_new("RuntimeError", "cannot reuse already awaited coroutine"),
            }
        }
        Object::Native(ref n) => match n.awaited(&value) {
            Some(result) => result,
            None => value,
        },
        Object::Instance(_) => match getattr_opt(&value, "__await__") {
            Some(method) => call(&method, Args::none()),
            None => value,
        },
        other => other,
    }
}

/// A Python generator: the body runs on its own thread and pauses at each
/// `yield_`. The runtime state is shared, so patches made there are visible.
pub fn generator(body: impl FnOnce(&Yield) -> Object + Send + 'static) -> Object {
    native(Generator::start(body))
}

pub struct Yield {
    values: mpsc::SyncSender<Result<Object, Box<dyn std::any::Any + Send>>>,
    resume: Mutex<mpsc::Receiver<()>>,
}

impl Yield {
    pub fn send(&self, value: Object) -> Object {
        if self.values.send(Ok(value)).is_err() {
            std::panic::panic_any(GeneratorClosed);
        }
        if lock(&self.resume).recv().is_err() {
            std::panic::panic_any(GeneratorClosed);
        }
        Object::None
    }
}

struct GeneratorClosed;

struct Generator {
    values: Mutex<mpsc::Receiver<Result<Object, Box<dyn std::any::Any + Send>>>>,
    resume: Mutex<Option<mpsc::SyncSender<()>>>,
    started: Mutex<bool>,
    finished: Mutex<bool>,
}

impl Generator {
    fn start(body: impl FnOnce(&Yield) -> Object + Send + 'static) -> Self {
        let (values_tx, values_rx) = mpsc::sync_channel(0);
        let (resume_tx, resume_rx) = mpsc::sync_channel(0);
        let finish = values_tx.clone();
        std::thread::spawn(move || {
            // Wait for the first `next`.
            if resume_rx.recv().is_err() {
                return;
            }
            let handle = Yield {
                values: values_tx,
                resume: Mutex::new(resume_rx),
            };
            match catch_unwind(AssertUnwindSafe(|| body(&handle))) {
                Ok(_) => drop(finish.send(Err(Box::new(StopIteration)))),
                Err(payload) if payload.is::<GeneratorClosed>() => {}
                Err(payload) => drop(finish.send(Err(payload))),
            }
        });
        Self {
            values: Mutex::new(values_rx),
            resume: Mutex::new(Some(resume_tx)),
            started: Mutex::new(false),
            finished: Mutex::new(false),
        }
    }

    /// The next value, `None` once the body returns.
    fn next(&self) -> Option<Object> {
        if *lock(&self.finished) {
            return None;
        }
        *lock(&self.started) = true;
        if let Some(resume) = lock(&self.resume).as_ref()
            && resume.send(()).is_err()
        {
            {
                *lock(&self.finished) = true;
                return None;
            }
        }
        match lock(&self.values).recv() {
            Ok(Ok(value)) => Some(value),
            Ok(Err(payload)) if payload.is::<StopIteration>() => {
                *lock(&self.finished) = true;
                None
            }
            Ok(Err(payload)) => {
                *lock(&self.finished) = true;
                resume_unwind(payload)
            }
            Err(_) => {
                *lock(&self.finished) = true;
                None
            }
        }
    }
}

struct StopIteration;

impl Native for Generator {
    fn class(&self) -> String {
        "generator".into()
    }
    fn iterate(&self) -> Option<Vec<Object>> {
        let mut out = Vec::new();
        while let Some(value) = self.next() {
            out.push(value);
        }
        Some(out)
    }
    fn getattr(&self, this: &Object, name: &str) -> Option<Object> {
        let this = this.clone();
        match name {
            "__next__" | "__anext__" => Some(func("next", move |_| next_of(&this))),
            _ => None,
        }
    }
    fn next_value(&self) -> Option<Option<Object>> {
        Some(self.next())
    }
}

/// `next(it)`; `StopIteration` when exhausted.
pub fn next_of(value: &Object) -> Object {
    if let Object::Native(n) = value
        && let Some(next) = n.next_value()
    {
        return next.unwrap_or_else(|| raise_new("StopIteration", ""));
    }
    unsupported(&format!("next({})", type_name(value)))
}

/// A fixture function: the value before its `yield`, the rest at teardown.
pub fn fixture(value: Object) -> Object {
    if let Object::Native(n) = &value
        && n.class() == "generator"
    {
        let produced = next_value(&value);
        let generator = value.clone();
        defer(move || {
            let _ = next_value(&generator);
        });
        return produced;
    }
    await_(value)
}

pub fn next_value(generator: &Object) -> Object {
    match generator {
        Object::Native(n) => n.next_value().flatten().unwrap_or(Object::None),
        _ => Object::None,
    }
}

// ------------------------------------------------------- inspection

pub fn type_name(value: &Object) -> String {
    match value {
        Object::None => "NoneType".into(),
        Object::Bool(_) => "bool".into(),
        Object::Int(_) => "int".into(),
        Object::Float(_) => "float".into(),
        Object::Str(_) => "str".into(),
        Object::Bytes(_) => "bytes".into(),
        Object::List(_) => "list".into(),
        Object::Tuple(_) => "tuple".into(),
        Object::Dict(_) => "dict".into(),
        Object::Set(_) => "set".into(),
        Object::Path(_) => "Path".into(),
        Object::Func(..) => "function".into(),
        Object::Class(_) => "type".into(),
        Object::Exception(e) => e.class.name.clone(),
        Object::Instance(i) => i.class.clone(),
        Object::Native(n) => n.class(),
        Object::Name(n) => n.to_string(),
        Object::Coroutine(_) => "coroutine".into(),
        Object::Unbound => "unbound".into(),
    }
}

/// `str(value)`.
pub fn text(value: &Object) -> String {
    match value {
        Object::None => "None".into(),
        Object::Bool(true) => "True".into(),
        Object::Bool(false) => "False".into(),
        Object::Int(i) => i.to_string(),
        Object::Float(f) => float_text(*f),
        Object::Str(s) => s.to_string(),
        Object::Path(p) => p.display().to_string(),
        Object::Native(n) if n.text().is_some() => n.text().unwrap_or_default(),
        Object::Instance(i) if lock(&i.attrs).iter().any(|(k, _)| k == "__str__") => {
            let found = lock(&i.attrs)
                .iter()
                .find(|(k, _)| k == "__str__")
                .map(|(_, v)| v.clone());
            match found {
                Some(Object::Func(..)) => text(&call(&found.unwrap_or(Object::None), Args::none())),
                Some(value) => text(&value),
                None => String::new(),
            }
        }
        Object::Exception(e) => {
            if e.args.len() == 1 {
                text(&e.args[0])
            } else if e.args.is_empty() {
                lock(&e.attrs)
                    .iter()
                    .map(|(_, v)| text(v))
                    .collect::<Vec<_>>()
                    .join(": ")
            } else {
                repr(&tuple(e.args.clone()))
            }
        }
        other => repr(other),
    }
}

fn float_text(f: f64) -> String {
    if f.fract() == 0.0 && f.abs() < 1e16 {
        format!("{f:.1}")
    } else {
        format!("{f}")
    }
}

/// `repr(value)`.
pub fn repr(value: &Object) -> String {
    match value {
        Object::Str(s) => format!("'{}'", s.replace('\\', "\\\\").replace('\'', "\\'")),
        Object::Bytes(b) => format!("b{:?}", String::from_utf8_lossy(b)),
        Object::List(items) => format!("[{}]", join(&lock(items))),
        Object::Tuple(items) if items.len() == 1 => format!("({},)", repr(&items[0])),
        Object::Tuple(items) => format!("({})", join(items)),
        Object::Set(items) => format!("{{{}}}", join(&lock(items))),
        Object::Dict(pairs) => format!(
            "{{{}}}",
            lock(pairs)
                .iter()
                .map(|(k, v)| format!("{}: {}", repr(k), repr(v)))
                .collect::<Vec<_>>()
                .join(", ")
        ),
        Object::Path(p) => format!("PosixPath('{}')", p.display()),
        Object::Exception(e) => format!("{}({})", e.class.name, join(&e.args)),
        Object::Instance(i) => format!(
            "{}({})",
            i.class,
            lock(&i.attrs)
                .iter()
                .map(|(k, v)| format!("{k}={}", repr(v)))
                .collect::<Vec<_>>()
                .join(", ")
        ),
        Object::Func(n, _) => format!("<function {n}>"),
        Object::Class(c) => format!("<class '{}'>", c.name),
        Object::Native(n) => format!("<{}>", n.class()),
        Object::Name(n) => format!("<{n}>"),
        other => text(other),
    }
}

fn join(items: &[Object]) -> String {
    items.iter().map(repr).collect::<Vec<_>>().join(", ")
}

pub fn truthy(value: &Object) -> bool {
    match value {
        Object::None | Object::Unbound => false,
        Object::Bool(b) => *b,
        Object::Int(i) => *i != 0,
        Object::Float(f) => *f != 0.0,
        Object::Str(s) => !s.is_empty(),
        Object::Bytes(b) => !b.is_empty(),
        Object::List(items) | Object::Set(items) => !lock(items).is_empty(),
        Object::Tuple(items) => !items.is_empty(),
        Object::Dict(pairs) => !lock(pairs).is_empty(),
        _ => true,
    }
}

pub fn identical(a: &Object, b: &Object) -> bool {
    match (a, b) {
        (Object::None, Object::None) => true,
        (Object::Bool(x), Object::Bool(y)) => x == y,
        (Object::List(x), Object::List(y)) => Arc::ptr_eq(x, y),
        (Object::Dict(x), Object::Dict(y)) => Arc::ptr_eq(x, y),
        (Object::Set(x), Object::Set(y)) => Arc::ptr_eq(x, y),
        (Object::Instance(x), Object::Instance(y)) => Arc::ptr_eq(x, y),
        (Object::Native(x), Object::Native(y)) => {
            std::ptr::addr_eq(Arc::as_ptr(x), Arc::as_ptr(y)) || x.equals(b).unwrap_or(false)
        }
        (Object::Exception(x), Object::Exception(y)) => Arc::ptr_eq(x, y),
        (Object::Class(x), Object::Class(y)) => Arc::ptr_eq(x, y) || x.name == y.name,
        (Object::Func(_, x), Object::Func(_, y)) => Arc::ptr_eq(x, y),
        (Object::Name(x), Object::Name(y)) => x == y,
        (Object::Native(n), other) | (other, Object::Native(n)) if n.equals(other).is_some() => {
            n.equals(other).unwrap_or(false)
        }
        (Object::Int(x), Object::Int(y)) => x == y,
        (Object::Str(x), Object::Str(y)) => x == y,
        _ => false,
    }
}

fn number(value: &Object) -> Option<f64> {
    match value {
        Object::Bool(b) => Some(f64::from(u8::from(*b))),
        Object::Int(i) => Some(*i as f64),
        Object::Float(f) => Some(*f),
        _ => None,
    }
}

fn approx_eq(approx: &Instance, other: &Object) -> bool {
    let attrs = lock(&approx.attrs).clone();
    let get = |k: &str| attrs.iter().find(|(n, _)| n == k).map(|(_, v)| v.clone());
    let (Some(expected), Some(rel), Some(abs)) = (get("expected"), get("rel"), get("abs")) else {
        return false;
    };
    match (number(&expected), number(other), number(&rel), number(&abs)) {
        (Some(e), Some(o), Some(r), Some(a)) => (e - o).abs() <= a.max(r * e.abs()),
        _ => eq(&expected, other),
    }
}

pub fn eq(a: &Object, b: &Object) -> bool {
    match (a, b) {
        (Object::Instance(x), other) | (other, Object::Instance(x)) if x.class == "approx" => {
            approx_eq(x, other)
        }
        (Object::Int(x), Object::Int(y)) => x == y,
        (Object::Str(x), Object::Str(y)) => x == y,
        (Object::Bytes(x), Object::Bytes(y)) => x == y,
        (Object::None, Object::None) => true,
        (Object::Path(x), Object::Path(y)) => x == y,
        (Object::List(x), Object::List(y)) => {
            Arc::ptr_eq(x, y) || seq_eq(&lock(x).clone(), &lock(y).clone())
        }
        (Object::Tuple(x), Object::Tuple(y)) => seq_eq(x, y),
        (Object::Set(x), Object::Set(y)) => {
            let (x, y) = (lock(x).clone(), lock(y).clone());
            x.len() == y.len() && x.iter().all(|i| y.iter().any(|j| eq(i, j)))
        }
        (Object::Dict(x), Object::Dict(y)) => {
            if Arc::ptr_eq(x, y) {
                return true;
            }
            let (x, y) = (lock(x).clone(), lock(y).clone());
            x.len() == y.len()
                && x.iter()
                    .all(|(k, v)| y.iter().any(|(k2, v2)| eq(k, k2) && eq(v, v2)))
        }
        (Object::Instance(x), Object::Instance(y)) => {
            Arc::ptr_eq(x, y)
                || (x.class == y.class && x.class == "SimpleNamespace" && {
                    let (x, y) = (lock(&x.attrs).clone(), lock(&y.attrs).clone());
                    x.len() == y.len()
                        && x.iter()
                            .all(|(k, v)| y.iter().any(|(k2, v2)| k == k2 && eq(v, v2)))
                })
        }
        _ => match (number(a), number(b)) {
            (Some(x), Some(y)) => x == y,
            _ => identical(a, b),
        },
    }
}

fn seq_eq(x: &[Object], y: &[Object]) -> bool {
    x.len() == y.len() && x.iter().zip(y).all(|(a, b)| eq(a, b))
}

// ------------------------------------------------------- operators

pub fn not(a: &Object) -> Object {
    Object::Bool(!truthy(a))
}

pub fn cmp_eq(a: &Object, b: &Object) -> Object {
    Object::Bool(eq(a, b))
}

pub fn cmp_ne(a: &Object, b: &Object) -> Object {
    Object::Bool(!eq(a, b))
}

pub fn is(a: &Object, b: &Object) -> Object {
    Object::Bool(identical(a, b))
}

pub fn is_not(a: &Object, b: &Object) -> Object {
    Object::Bool(!identical(a, b))
}

fn order(a: &Object, b: &Object) -> std::cmp::Ordering {
    if let (Object::Native(x), Object::Native(y)) = (a, b)
        && let (Some(x), Some(y)) = (x.order_value(), y.order_value())
    {
        return x.partial_cmp(&y).unwrap_or(std::cmp::Ordering::Equal);
    }
    if let (Some(x), Some(y)) = (number(a), number(b)) {
        return x.partial_cmp(&y).unwrap_or(std::cmp::Ordering::Equal);
    }
    match (a, b) {
        (Object::Str(x), Object::Str(y)) => x.cmp(y),
        (Object::Bytes(x), Object::Bytes(y)) => x.cmp(y),
        (Object::Tuple(_) | Object::List(_), Object::Tuple(_) | Object::List(_)) => {
            let (x, y) = (iter(a), iter(b));
            for (i, j) in x.iter().zip(&y) {
                let o = order(i, j);
                if o != std::cmp::Ordering::Equal {
                    return o;
                }
            }
            x.len().cmp(&y.len())
        }
        _ => raise_new_with(
            &[a, b],
            "TypeError",
            &format!(
                "'<' not supported between {} and {}",
                type_name(a),
                type_name(b)
            ),
        ),
    }
}

pub fn lt(a: &Object, b: &Object) -> Object {
    Object::Bool(order(a, b).is_lt())
}
pub fn le(a: &Object, b: &Object) -> Object {
    Object::Bool(order(a, b).is_le())
}
pub fn gt(a: &Object, b: &Object) -> Object {
    Object::Bool(order(a, b).is_gt())
}
pub fn ge(a: &Object, b: &Object) -> Object {
    Object::Bool(order(a, b).is_ge())
}

pub fn contains(container: &Object, item: &Object) -> bool {
    match (container, item) {
        (Object::Str(s), Object::Str(part)) => s.contains(part.as_ref()),
        (Object::Bytes(s), Object::Bytes(part)) => {
            part.is_empty() || s.windows(part.len()).any(|w| w == part.as_slice())
        }
        (Object::Dict(pairs), _) => lock(pairs).iter().any(|(k, _)| eq(k, item)),
        (Object::Native(n), _) => n
            .contains(item)
            .unwrap_or_else(|| iter(container).iter().any(|x| eq(x, item))),
        _ => iter(container).iter().any(|x| eq(x, item)),
    }
}

pub fn in_(item: &Object, container: &Object) -> Object {
    Object::Bool(contains(container, item))
}

pub fn not_in(item: &Object, container: &Object) -> Object {
    Object::Bool(!contains(container, item))
}

fn arith(
    a: &Object,
    b: &Object,
    op: &str,
    ints: fn(i64, i64) -> Option<i64>,
    floats: fn(f64, f64) -> f64,
) -> Object {
    if let Some(value) = native_binary(a, b, op) {
        return value;
    }
    match (a, b) {
        (Object::Int(_) | Object::Bool(_), Object::Int(_) | Object::Bool(_)) => {
            let (x, y) = (
                number(a).expect("int") as i64,
                number(b).expect("int") as i64,
            );
            ints(x, y)
                .map(Object::Int)
                .unwrap_or_else(|| raise_new("OverflowError", op))
        }
        _ => match (number(a), number(b)) {
            (Some(x), Some(y)) => Object::Float(floats(x, y)),
            _ => raise_new_with(
                &[a, b],
                "TypeError",
                &format!(
                    "unsupported operand {op} for {} and {}",
                    type_name(a),
                    type_name(b)
                ),
            ),
        },
    }
}

fn native_binary(a: &Object, b: &Object, op: &str) -> Option<Object> {
    if let Object::Native(n) = a
        && let Some(value) = n.binary(op, b, false)
    {
        return Some(value);
    }
    match b {
        Object::Native(n) => n.binary(op, a, true),
        _ => None,
    }
}

pub fn add(a: &Object, b: &Object) -> Object {
    match (a, b) {
        (Object::Str(x), Object::Str(y)) => str_(&format!("{x}{y}")),
        (Object::Bytes(x), Object::Bytes(y)) => bytes(&[x.as_slice(), y.as_slice()].concat()),
        (Object::List(x), Object::List(y)) => {
            let mut items = lock(x).clone();
            items.extend(lock(y).iter().cloned());
            list(items)
        }
        (Object::Tuple(x), Object::Tuple(y)) => tuple(x.iter().chain(y.iter()).cloned().collect()),
        _ => arith(a, b, "+", i64::checked_add, |x, y| x + y),
    }
}

pub fn sub(a: &Object, b: &Object) -> Object {
    if let (Object::Set(x), Object::Set(y)) = (a, b) {
        let y = lock(y).clone();
        return set(lock(x)
            .iter()
            .filter(|i| !y.iter().any(|j| eq(i, j)))
            .cloned()
            .collect());
    }
    arith(a, b, "-", i64::checked_sub, |x, y| x - y)
}

pub fn mul(a: &Object, b: &Object) -> Object {
    match (a, b) {
        (Object::Str(s), Object::Int(n)) | (Object::Int(n), Object::Str(s)) => {
            str_(&s.repeat((*n).max(0) as usize))
        }
        (Object::Bytes(s), Object::Int(n)) | (Object::Int(n), Object::Bytes(s)) => {
            bytes(&s.repeat((*n).max(0) as usize))
        }
        (Object::List(s), Object::Int(n)) | (Object::Int(n), Object::List(s)) => {
            let items = lock(s).clone();
            list((0..(*n).max(0)).flat_map(|_| items.clone()).collect())
        }
        _ => arith(a, b, "*", i64::checked_mul, |x, y| x * y),
    }
}

pub fn div(a: &Object, b: &Object) -> Object {
    if let Some(value) = native_binary(a, b, "/") {
        return value;
    }
    match (a, b) {
        (Object::Path(p), other) => Object::Path(Arc::new(p.join(text(other)))),
        _ => match (number(a), number(b)) {
            (Some(_), Some(0.0)) => raise_new("ZeroDivisionError", "division by zero"),
            (Some(x), Some(y)) => Object::Float(x / y),
            _ => raise_new_with(&[a, b], "TypeError", "unsupported operand /"),
        },
    }
}

pub fn floordiv(a: &Object, b: &Object) -> Object {
    if number(b) == Some(0.0) {
        raise_new("ZeroDivisionError", "integer division or modulo by zero");
    }
    arith(
        a,
        b,
        "//",
        |x, y| Some(x.div_euclid(y) - i64::from(x.rem_euclid(y) != 0 && y < 0)),
        |x, y| (x / y).floor(),
    )
}

pub fn mod_(a: &Object, b: &Object) -> Object {
    if let Object::Str(template) = a {
        return crate::natives::percent_format(template, b);
    }
    if number(b) == Some(0.0) {
        raise_new("ZeroDivisionError", "integer division or modulo by zero");
    }
    arith(
        a,
        b,
        "%",
        |x, y| Some(((x % y) + y) % y),
        |x, y| ((x % y) + y) % y,
    )
}

pub fn pow(a: &Object, b: &Object) -> Object {
    arith(
        a,
        b,
        "**",
        |x, y| u32::try_from(y).ok().and_then(|y| x.checked_pow(y)),
        f64::powf,
    )
}

pub fn bitor(a: &Object, b: &Object) -> Object {
    match (a, b) {
        (Object::Dict(x), Object::Dict(y)) => {
            let mut pairs = lock(x).clone();
            pairs.extend(lock(y).iter().cloned());
            dict(pairs)
        }
        (Object::Set(x), Object::Set(y)) => {
            set(lock(x).iter().chain(lock(y).iter()).cloned().collect())
        }
        (Object::Int(x), Object::Int(y)) => Object::Int(x | y),
        (Object::Bool(x), Object::Bool(y)) => Object::Bool(*x || *y),
        // `A | B` between types is a union; `isinstance` accepts it as a tuple.
        (
            Object::Name(_)
            | Object::Class(_)
            | Object::Native(_)
            | Object::None
            | Object::Tuple(_),
            _,
        )
        | (_, Object::Name(_) | Object::Class(_) | Object::Native(_) | Object::None) => {
            let mut members = match a {
                Object::Tuple(t) => t.to_vec(),
                other => vec![other.clone()],
            };
            members.push(b.clone());
            tuple(members)
        }
        _ => raise_new_with(&[a, b], "TypeError", "unsupported operand |"),
    }
}

pub fn bitand(a: &Object, b: &Object) -> Object {
    match (a, b) {
        (Object::Set(x), Object::Set(y)) => {
            let y = lock(y).clone();
            set(lock(x)
                .iter()
                .filter(|i| y.iter().any(|j| eq(i, j)))
                .cloned()
                .collect())
        }
        (Object::Int(x), Object::Int(y)) => Object::Int(x & y),
        (Object::Bool(x), Object::Bool(y)) => Object::Bool(*x && *y),
        (Object::Dict(_), Object::Set(_) | Object::Dict(_)) | (Object::Set(_), Object::Dict(_)) => {
            let keys = |v: &Object| set(iter(v));
            bitand(&keys(a), &keys(b))
        }
        _ => raise_new_with(&[a, b], "TypeError", "unsupported operand &"),
    }
}

pub fn neg(a: &Object) -> Object {
    match a {
        Object::Int(i) => Object::Int(-i),
        Object::Float(f) => Object::Float(-f),
        Object::Bool(b) => Object::Int(-i64::from(*b)),
        _ => raise_new("TypeError", "bad operand for unary -"),
    }
}

// ---------------------------------------------- attributes and items

pub fn getattr(object: &Object, attribute: &str) -> Object {
    getattr_opt(object, attribute).unwrap_or_else(|| {
        raise_new(
            "AttributeError",
            &format!(
                "'{}' object has no attribute '{attribute}'",
                type_name(object)
            ),
        )
    })
}

fn method(object: &Object, attribute: &str) -> Option<Object> {
    let mut kinds = vec![type_name(object)];
    if matches!(object, Object::Exception(_)) {
        kinds.push("Exception".into());
    }
    kinds.push("object".into());
    for kind in kinds {
        let key = format!("{kind}.{attribute}");
        if let Some(Object::Func(_, f)) = state(|s| s.methods.get(&format!("@{key}")).cloned()) {
            return Some(f(Args::new(vec![object.clone()], vec![])));
        }
        if let Some(Object::Func(n, f)) = state(|s| s.methods.get(&key).cloned()) {
            let this = object.clone();
            return Some(Object::Func(
                n,
                Arc::new(move |mut args: Args| {
                    args.pos.insert(0, this.clone());
                    f(args)
                }),
            ));
        }
    }
    None
}

/// `instance.__dict__`: reads and writes go to the instance attributes.
struct AttributeView(Arc<Instance>);

impl Native for AttributeView {
    fn class(&self) -> String {
        "dict".into()
    }
    fn getitem(&self, key: &Object) -> Option<Object> {
        let key = text(key);
        let found = lock(&self.0.attrs)
            .iter()
            .find(|(k, _)| *k == key)
            .map(|(_, v)| v.clone());
        Some(found.unwrap_or_else(|| raise_new("KeyError", &key)))
    }
    fn setitem(&self, key: &Object, value: Object) -> bool {
        set_pair(&self.0.attrs, &text(key), value);
        true
    }
    fn iterate(&self) -> Option<Vec<Object>> {
        Some(lock(&self.0.attrs).iter().map(|(k, _)| str_(k)).collect())
    }
    fn getattr(&self, _this: &Object, attribute: &str) -> Option<Object> {
        let instance = self.0.clone();
        (attribute == "update").then(|| {
            func("update", move |args| {
                for value in &args.pos {
                    for (k, v) in items(value) {
                        set_pair(&instance.attrs, &text(&k), v);
                    }
                }
                for (k, v) in args.kw {
                    set_pair(&instance.attrs, &k, v);
                }
                Object::None
            })
        })
    }
}

pub fn getattr_opt(object: &Object, attribute: &str) -> Option<Object> {
    match object {
        Object::Name(path) => Some(name(&format!("{path}.{attribute}"))),
        Object::Instance(i) if attribute == "__dict__" => Some(native(AttributeView(i.clone()))),
        Object::Instance(i) => {
            // Release the lock before looking at the class, which reads it again.
            let own = lock(&i.attrs)
                .iter()
                .find(|(k, _)| k == attribute)
                .map(|(_, v)| v.clone());
            own.or_else(|| crate::classes::instance_attribute(object, &i.attrs, attribute))
                .or_else(|| method(object, attribute))
        }
        Object::Exception(e) => match attribute {
            "args" => Some(tuple(e.args.clone())),
            _ => lock(&e.attrs)
                .iter()
                .find(|(k, _)| k == attribute)
                .map(|(_, v)| v.clone())
                .or_else(|| method(object, attribute)),
        },
        Object::Native(n) => n
            .getattr(object, attribute)
            .or_else(|| method(object, attribute)),
        Object::Class(c) if attribute == "__name__" => {
            Some(str_(c.name.rsplit('.').next().unwrap_or("")))
        }
        Object::Func(path, _) if path.contains('.') => {
            let dotted = format!("{path}.{attribute}");
            let patched = state(|s| s.patches.get(&dotted).and_then(|v| v.last().cloned()));
            patched.or_else(|| method(object, attribute))
        }
        _ => method(object, attribute),
    }
}

pub fn setattr(object: &Object, attribute: &str, value: Object) {
    match object {
        Object::Func(path, _) if path.contains('.') => {
            let undo = patch_name(&format!("{path}.{attribute}"), value);
            defer(undo);
        }
        Object::Name(path) => {
            let undo = patch_name(&format!("{path}.{attribute}"), value);
            defer(undo);
        }
        Object::Instance(i) => set_pair(&i.attrs, attribute, value),
        Object::Exception(e) => set_pair(&e.attrs, attribute, value),
        Object::Native(n) if n.setattr(attribute, value.clone()) => {}
        _ => raise_new(
            "AttributeError",
            &format!("cannot set '{attribute}' on {}", type_name(object)),
        ),
    }
}

fn set_pair(attrs: &Mutex<Vec<(String, Object)>>, attribute: &str, value: Object) {
    let mut attrs = lock(attrs);
    match attrs.iter_mut().find(|(k, _)| k == attribute) {
        Some(slot) => slot.1 = value,
        None => attrs.push((attribute.to_owned(), value)),
    }
}

/// Undo token for an attribute replaced by `monkeypatch` or `patch`.
pub fn replace_attr(object: &Object, attribute: &str, value: Object) -> Box<dyn FnOnce() + Send> {
    match object {
        Object::Name(path) => Box::new(patch_name(&format!("{path}.{attribute}"), value)),
        Object::Func(path, _) if path.contains('.') => {
            Box::new(patch_name(&format!("{path}.{attribute}"), value))
        }
        _ => {
            let previous = getattr_opt(object, attribute);
            setattr(object, attribute, value);
            let object = object.clone();
            let attribute = attribute.to_owned();
            Box::new(move || match previous {
                Some(previous) => setattr(&object, &attribute, previous),
                None => {
                    if let Object::Instance(i) = &object {
                        lock(&i.attrs).retain(|(k, _)| *k != attribute);
                    }
                }
            })
        }
    }
}

fn index(len: usize, key: &Object) -> usize {
    let Object::Int(i) = key else {
        raise_new("TypeError", "indices must be integers");
    };
    let len = len as i64;
    let real = if *i < 0 { len + i } else { *i };
    if real < 0 || real >= len {
        raise_new("IndexError", "index out of range");
    }
    real as usize
}

pub fn getitem(object: &Object, key: &Object) -> Object {
    match object {
        Object::List(items) => {
            let items = lock(items);
            let i = index(items.len(), key);
            items[i].clone()
        }
        Object::Tuple(items) => items[index(items.len(), key)].clone(),
        Object::Str(s) => {
            let chars: Vec<char> = s.chars().collect();
            str_(&chars[index(chars.len(), key)].to_string())
        }
        Object::Bytes(b) => Object::Int(i64::from(b[index(b.len(), key)])),
        Object::Dict(pairs) => lock(pairs)
            .iter()
            .find(|(k, _)| eq(k, key))
            .map(|(_, v)| v.clone())
            .unwrap_or_else(|| raise(new_exception("KeyError", vec![key.clone()]))),
        Object::Native(n) => n
            .getitem(key)
            .unwrap_or_else(|| unsupported(&format!("{}[]", n.class()))),
        _ => match getattr_opt(object, "__getitem__") {
            Some(m) => call(&m, Args::new(vec![key.clone()], vec![])),
            None => raise_new(
                "TypeError",
                &format!("'{}' is not subscriptable", type_name(object)),
            ),
        },
    }
}

pub fn setitem(object: &Object, key: &Object, value: Object) {
    match object {
        Object::List(items) => {
            let mut items = lock(items);
            let i = index(items.len(), key);
            items[i] = value;
        }
        Object::Dict(pairs) => {
            let mut pairs = lock(pairs);
            match pairs.iter_mut().find(|(k, _)| eq(k, key)) {
                Some(slot) => slot.1 = value,
                None => pairs.push((key.clone(), value)),
            }
        }
        Object::Native(n) if n.setitem(key, value.clone()) => {}
        _ => raise_new_with(
            &[object, key],
            "TypeError",
            &format!("'{}' does not support item assignment", type_name(object)),
        ),
    }
}

pub fn delitem(object: &Object, key: &Object) {
    match object {
        Object::Dict(pairs) => lock(pairs).retain(|(k, _)| !eq(k, key)),
        Object::List(items) => {
            let mut items = lock(items);
            let i = index(items.len(), key);
            items.remove(i);
        }
        _ => raise_new("TypeError", "item deletion not supported"),
    }
}

fn bound(len: usize, value: &Object, default: i64) -> usize {
    let len = len as i64;
    let v = match value {
        Object::None => default,
        Object::Int(i) if *i < 0 => (len + i).max(0),
        Object::Int(i) => (*i).min(len),
        _ => raise_new("TypeError", "slice indices must be integers"),
    };
    v.clamp(0, len) as usize
}

pub fn slice(object: &Object, start: &Object, stop: &Object) -> Object {
    let cut = |len: usize| {
        let a = bound(len, start, 0);
        let b = bound(len, stop, len as i64);
        (a, b.max(a))
    };
    match object {
        Object::Str(s) => {
            let chars: Vec<char> = s.chars().collect();
            let (a, b) = cut(chars.len());
            str_(&chars[a..b].iter().collect::<String>())
        }
        Object::Bytes(x) => {
            let (a, b) = cut(x.len());
            bytes(&x[a..b])
        }
        Object::List(items) => {
            let items = lock(items);
            let (a, b) = cut(items.len());
            list(items[a..b].to_vec())
        }
        Object::Tuple(items) => {
            let (a, b) = cut(items.len());
            tuple(items[a..b].to_vec())
        }
        _ => unsupported(&format!("{}[:]", type_name(object))),
    }
}

/// Eager iteration.
pub fn iter(value: &Object) -> Vec<Object> {
    match value {
        Object::List(items) | Object::Set(items) => lock(items).clone(),
        Object::Tuple(items) => items.to_vec(),
        Object::Dict(pairs) => lock(pairs).iter().map(|(k, _)| k.clone()).collect(),
        Object::Str(s) => s.chars().map(|c| str_(&c.to_string())).collect(),
        Object::Bytes(b) => b.iter().map(|x| Object::Int(i64::from(*x))).collect(),
        Object::Native(n) => n.iterate().unwrap_or_else(|| {
            unsupported(&format!("iter({})", n.class()));
            Vec::new()
        }),
        _ => match getattr_opt(value, "__iter__") {
            Some(m) => iter(&call(&m, Args::none())),
            None => raise_new(
                "TypeError",
                &format!("'{}' is not iterable", type_name(value)),
            ),
        },
    }
}

/// `dict.items()` pairs.
pub fn items(value: &Object) -> Vec<(Object, Object)> {
    match value {
        Object::Dict(pairs) => lock(pairs).clone(),
        Object::Native(_) => iter(value)
            .into_iter()
            .map(|k| {
                let v = getitem(value, &k);
                (k, v)
            })
            .collect(),
        _ => raise_new(
            "TypeError",
            &format!("'{}' is not a mapping", type_name(value)),
        ),
    }
}

/// `a, b = value`.
pub fn unpack(value: &Object, count: usize) -> Vec<Object> {
    let items = iter(value);
    if items.len() != count {
        raise_new(
            "ValueError",
            &format!("expected {count} values to unpack, got {}", items.len()),
        );
    }
    items
}

// ---------------------------------------------------------- calls

pub fn call(callee: &Object, args: Args) -> Object {
    match callee {
        Object::Func(_, f) => f(args),
        Object::Native(n) => n
            .call(callee, args)
            .unwrap_or_else(|| unsupported(&format!("{}()", n.class()))),
        Object::Class(class) => {
            let mut exception = Exception {
                class: class.clone(),
                args: args.pos,
                attrs: Mutex::new(Vec::new()),
            };
            exception.attrs = Mutex::new(args.kw);
            Object::Exception(Arc::new(exception))
        }
        Object::Name(path) if state(|s| s.capabilities.iter().any(|p| path.starts_with(p))) => {
            panic!("TDD pending: capability `{path}` is not implemented")
        }
        Object::Name(path) => unsupported(path),
        Object::Instance(_) => match getattr_opt(callee, "__call__") {
            Some(m) => call(&m, args),
            None => raise_new("TypeError", "object is not callable"),
        },
        other => raise_new(
            "TypeError",
            &format!("'{}' object is not callable", type_name(other)),
        ),
    }
}

pub fn call_method(object: &Object, method: &str, args: Args) -> Object {
    let callee = getattr(object, method);
    call(&callee, args)
}

/// Read a local variable; an unassigned one raises like Python.
pub fn local(value: &Object, name: &str) -> Object {
    match value {
        Object::Unbound => raise_new(
            "UnboundLocalError",
            &format!("local variable '{name}' referenced before assignment"),
        ),
        other => other.clone(),
    }
}

/// `assert condition, message`.
#[track_caller]
pub fn assert_(condition: &Object, location: &str, message: Option<Object>) {
    if !truthy(condition) {
        let detail = message.map(|m| text(&m)).unwrap_or_default();
        raise(new_exception(
            "AssertionError",
            vec![str_(&format!("{location}: {detail}"))],
        ));
    }
}

/// `f"..."` pieces.
pub fn fstring(parts: Vec<Object>) -> Object {
    str_(&parts.iter().map(text).collect::<String>())
}

/// `{value!r}` and `{value:spec}` inside an f-string.
pub fn format_value(value: &Object, conversion: &str, spec: &str) -> Object {
    let value = match conversion {
        "r" => str_(&repr(value)),
        "s" => str_(&text(value)),
        _ => value.clone(),
    };
    if spec.is_empty() {
        return str_(&text(&value));
    }
    crate::natives::format_spec(&value, spec)
}
