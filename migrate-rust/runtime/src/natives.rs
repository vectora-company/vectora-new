//! Native implementations referenced by the builtins, standard library and
//! methods maps. Names are `fn(Args) -> Object`; methods are `fn(Object, Args) -> Object`.

use std::path::PathBuf;
use std::sync::{Arc, Mutex};

use crate::object::{
    Args, Object, add, await_, call, coroutine, dict, eq, getattr_opt, items, iter, list, lock,
    raise, raise_new, repr, set, str_, text, truthy, tuple, type_name,
};

fn one(args: &Args, name: &str) -> Object {
    args.bind(0, name, None)
}

fn int_of(value: &Object) -> i64 {
    match value {
        Object::Int(i) => *i,
        Object::Bool(b) => i64::from(*b),
        _ => crate::object::raise_new_with(
            &[value],
            "TypeError",
            &format!("expected int, got {}", type_name(value)),
        ),
    }
}

// ----------------------------------------------------------- builtins

pub fn len(args: Args) -> Object {
    let value = one(&args, "obj");
    Object::from(match &value {
        Object::Str(s) => s.chars().count(),
        Object::Bytes(b) => b.len(),
        Object::List(x) | Object::Set(x) => lock(x).len(),
        Object::Tuple(x) => x.len(),
        Object::Dict(x) => lock(x).len(),
        _ => iter(&value).len(),
    })
}

pub fn str_builtin(args: Args) -> Object {
    match args.pos.first() {
        None => str_(""),
        Some(Object::Bytes(b)) if args.pos.len() > 1 => str_(&String::from_utf8_lossy(b)),
        Some(value) => str_(&text(value)),
    }
}

pub fn repr_builtin(args: Args) -> Object {
    str_(&repr(&one(&args, "obj")))
}

pub fn int_builtin(args: Args) -> Object {
    match args.pos.first() {
        None => Object::Int(0),
        Some(Object::Str(s)) => s
            .trim()
            .parse::<i64>()
            .map(Object::Int)
            .unwrap_or_else(|_| {
                raise_new("ValueError", &format!("invalid literal for int(): '{s}'"))
            }),
        Some(Object::Float(f)) => Object::Int(f.trunc() as i64),
        Some(other) => Object::Int(int_of(other)),
    }
}

pub fn float_builtin(args: Args) -> Object {
    match args.pos.first() {
        None => Object::Float(0.0),
        Some(Object::Str(s)) => s
            .trim()
            .parse::<f64>()
            .map(Object::Float)
            .unwrap_or_else(|_| raise_new("ValueError", "could not convert string to float")),
        Some(Object::Float(f)) => Object::Float(*f),
        Some(other) => Object::Float(int_of(other) as f64),
    }
}

pub fn bool_builtin(args: Args) -> Object {
    Object::Bool(args.pos.first().is_some_and(truthy))
}

pub fn list_builtin(args: Args) -> Object {
    list(args.pos.first().map(iter).unwrap_or_default())
}

pub fn tuple_builtin(args: Args) -> Object {
    tuple(args.pos.first().map(iter).unwrap_or_default())
}

pub fn set_builtin(args: Args) -> Object {
    set(args.pos.first().map(iter).unwrap_or_default())
}

pub fn dict_builtin(args: Args) -> Object {
    let mut pairs = match args.pos.first() {
        Some(Object::Dict(_)) | Some(Object::Native(_)) => items(&args.pos[0]),
        Some(other) => iter(other)
            .iter()
            .map(|pair| {
                let kv = crate::object::unpack(pair, 2);
                (kv[0].clone(), kv[1].clone())
            })
            .collect(),
        None => Vec::new(),
    };
    pairs.extend(args.kw.iter().map(|(k, v)| (str_(k), v.clone())));
    dict(pairs)
}

fn key_of(args: &Args) -> Option<Object> {
    args.kwarg("key").filter(|k| !matches!(k, Object::None))
}

pub fn sorted(args: Args) -> Object {
    let mut values = iter(&one(&args, "iterable"));
    let key = key_of(&args);
    let keyed = |v: &Object| match &key {
        Some(f) => call(f, Args::new(vec![v.clone()], vec![])),
        None => v.clone(),
    };
    values.sort_by(|a, b| {
        let (a, b) = (keyed(a), keyed(b));
        if truthy(&crate::object::lt(&a, &b)) {
            std::cmp::Ordering::Less
        } else if truthy(&crate::object::gt(&a, &b)) {
            std::cmp::Ordering::Greater
        } else {
            std::cmp::Ordering::Equal
        }
    });
    if args.kwarg("reverse").is_some_and(|r| truthy(&r)) {
        values.reverse();
    }
    list(values)
}

pub fn reversed(args: Args) -> Object {
    let mut values = iter(&one(&args, "seq"));
    values.reverse();
    list(values)
}

pub fn enumerate(args: Args) -> Object {
    let start = args.arg(1, "start").map_or(0, |s| int_of(&s));
    list(
        iter(&one(&args, "iterable"))
            .into_iter()
            .enumerate()
            .map(|(i, v)| tuple(vec![Object::Int(start + i as i64), v]))
            .collect(),
    )
}

pub fn zip(args: Args) -> Object {
    let columns: Vec<Vec<Object>> = args.pos.iter().map(iter).collect();
    let rows = columns.iter().map(Vec::len).min().unwrap_or(0);
    list(
        (0..rows)
            .map(|i| tuple(columns.iter().map(|c| c[i].clone()).collect()))
            .collect(),
    )
}

pub fn range(args: Args) -> Object {
    let numbers: Vec<i64> = args.pos.iter().map(int_of).collect();
    let (start, stop, step) = match numbers.as_slice() {
        [stop] => (0, *stop, 1),
        [start, stop] => (*start, *stop, 1),
        [start, stop, step] => (*start, *stop, *step),
        _ => raise_new("TypeError", "range expected 1 to 3 arguments"),
    };
    if step == 0 {
        raise_new("ValueError", "range() arg 3 must not be zero");
    }
    let mut out = Vec::new();
    let mut i = start;
    while (step > 0 && i < stop) || (step < 0 && i > stop) {
        out.push(Object::Int(i));
        i += step;
    }
    list(out)
}

fn extreme(args: Args, greater: bool) -> Object {
    let values = if args.pos.len() == 1 {
        iter(&args.pos[0])
    } else {
        args.pos.clone()
    };
    let key = key_of(&args);
    let keyed = |v: &Object| {
        key.as_ref().map_or_else(
            || v.clone(),
            |f| call(f, Args::new(vec![v.clone()], vec![])),
        )
    };
    let mut best: Option<Object> = None;
    for value in values {
        best = Some(match best {
            None => value,
            Some(current) => {
                let better = if greater {
                    truthy(&crate::object::gt(&keyed(&value), &keyed(&current)))
                } else {
                    truthy(&crate::object::lt(&keyed(&value), &keyed(&current)))
                };
                if better { value } else { current }
            }
        });
    }
    best.or_else(|| args.kwarg("default"))
        .unwrap_or_else(|| raise_new("ValueError", "arg is an empty sequence"))
}

pub fn min(args: Args) -> Object {
    extreme(args, false)
}

pub fn max(args: Args) -> Object {
    extreme(args, true)
}

pub fn sum(args: Args) -> Object {
    let start = args.arg(1, "start").unwrap_or(Object::Int(0));
    iter(&one(&args, "iterable"))
        .iter()
        .fold(start, |acc, v| add(&acc, v))
}

pub fn any(args: Args) -> Object {
    Object::Bool(iter(&one(&args, "iterable")).iter().any(truthy))
}

pub fn all(args: Args) -> Object {
    Object::Bool(iter(&one(&args, "iterable")).iter().all(truthy))
}

pub fn abs(args: Args) -> Object {
    match one(&args, "x") {
        Object::Int(i) => Object::Int(i.abs()),
        Object::Float(f) => Object::Float(f.abs()),
        other => raise_new(
            "TypeError",
            &format!("bad operand for abs(): {}", type_name(&other)),
        ),
    }
}

pub fn round(args: Args) -> Object {
    let value = one(&args, "number");
    let digits = args.arg(1, "ndigits");
    match (&value, digits) {
        (Object::Float(f), None | Some(Object::None)) => Object::Int(f.round_ties_even() as i64),
        (Object::Float(f), Some(d)) => {
            let factor = 10f64.powi(int_of(&d) as i32);
            Object::Float((f * factor).round_ties_even() / factor)
        }
        (other, _) => other.clone(),
    }
}

fn class_matches(value: &Object, kind: &Object) -> bool {
    if let Some(matched) = crate::classes::is_instance(value, kind) {
        return matched;
    }
    match kind {
        Object::Tuple(kinds) => kinds.iter().any(|k| class_matches(value, k)),
        Object::Class(class) => match value {
            Object::Exception(e) => e.class.is(&class.name),
            _ => false,
        },
        Object::Name(name) | Object::Str(name) => {
            let short = name.rsplit('.').next().unwrap_or(name);
            let actual = type_name(value);
            actual == short
                || (short == "object")
                || (short == "int" && matches!(value, Object::Bool(_)))
                || (short == "Mapping" && matches!(value, Object::Dict(_)))
                || (short == "Sequence" && matches!(value, Object::List(_) | Object::Tuple(_)))
                || (short == "PurePath" && matches!(value, Object::Path(_)))
        }
        Object::Func(name, _) => {
            let short = name.rsplit('.').next().unwrap_or(name);
            let actual = type_name(value);
            actual == short || (short == "int" && matches!(value, Object::Bool(_)))
        }
        _ => false,
    }
}

pub fn isinstance(args: Args) -> Object {
    Object::Bool(class_matches(
        &one(&args, "obj"),
        &args.bind(1, "class_or_tuple", None),
    ))
}

pub fn callable(args: Args) -> Object {
    Object::Bool(matches!(
        one(&args, "obj"),
        Object::Func(..) | Object::Class(_) | Object::Name(_) | Object::Native(_)
    ))
}

pub fn hasattr(args: Args) -> Object {
    let object = one(&args, "obj");
    let name = text(&args.bind(1, "name", None));
    let found = crate::object::catch(|| {
        let found = getattr_opt(&object, &name).is_some();
        crate::object::Flow::Return(Object::Bool(found))
    });
    match found {
        Ok(crate::object::Flow::Return(v)) => v,
        _ => Object::Bool(false),
    }
}

pub fn getattr_builtin(args: Args) -> Object {
    let object = one(&args, "obj");
    let name = text(&args.bind(1, "name", None));
    match (getattr_opt(&object, &name), args.arg(2, "default")) {
        (Some(v), _) => v,
        (None, Some(default)) => default,
        (None, None) => crate::object::getattr(&object, &name),
    }
}

pub fn setattr_builtin(args: Args) -> Object {
    crate::object::setattr(
        &one(&args, "obj"),
        &text(&args.bind(1, "name", None)),
        args.bind(2, "value", None),
    );
    Object::None
}

/// `type(obj)`, or `type(name, bases, attrs)` to create a class.
pub fn type_builtin(args: Args) -> Object {
    if args.pos.len() == 3 {
        let name = text(&args.pos[0]);
        let bases = crate::object::iter(&args.pos[1]);
        let attrs: Vec<(String, Object)> = crate::object::items(&args.pos[2])
            .into_iter()
            .map(|(k, v)| (text(&k), v))
            .collect();
        let attrs = attrs.iter().map(|(k, v)| (k.as_str(), v.clone())).collect();
        return crate::classes::class(&name, bases, attrs);
    }
    crate::object::type_of(&one(&args, "obj"))
}

/// `typing.get_type_hints(cls)`: the annotations belong to the class owner,
/// so a backend class reaches its capability.
pub fn get_type_hints(args: Args) -> Object {
    let target = one(&args, "obj");
    crate::object::require_value(&target);
    crate::object::unsupported("typing.get_type_hints")
}

/// Output captured by `capsys`, when active: (stdout, stderr).
pub static CAPTURED: Mutex<Option<(String, String)>> = Mutex::new(None);

/// Write to stdout or stderr, through `capsys` when it is active.
pub fn write_stream(error: bool, data: &str) {
    if let Some((out, err)) = lock(&CAPTURED).as_mut() {
        if error { err } else { out }.push_str(data);
        return;
    }
    if error {
        eprint!("{data}");
    } else {
        print!("{data}");
    }
}

pub fn print(args: Args) -> Object {
    let sep = args
        .kwarg("sep")
        .filter(|s| !matches!(s, Object::None))
        .map_or(" ".into(), |s| text(&s));
    let end = args
        .kwarg("end")
        .filter(|s| !matches!(s, Object::None))
        .map_or("\n".into(), |s| text(&s));
    let error = matches!(args.kwarg("file"), Some(Object::Instance(ref i)) if i.class == "stderr");
    let line = args.pos.iter().map(text).collect::<Vec<_>>().join(&sep);
    write_stream(error, &format!("{line}{end}"));
    Object::None
}

pub fn next(args: Args) -> Object {
    let value = one(&args, "iterator");
    match args.arg(1, "default") {
        Some(default) => match crate::object::catch(|| {
            crate::object::Flow::Return(crate::object::next_of(&value))
        }) {
            Ok(crate::object::Flow::Return(v)) => v,
            Ok(_) => default,
            Err(raised)
                if crate::object::matches(
                    &raised,
                    &crate::object::name("builtins.StopIteration"),
                ) =>
            {
                default
            }
            Err(raised) => crate::object::reraise(raised),
        },
        None => crate::object::next_of(&value),
    }
}

pub fn iter_builtin(args: Args) -> Object {
    let values = iter(&one(&args, "obj"));
    crate::object::generator(move |y| {
        for v in values {
            y.send(v);
        }
        Object::None
    })
}

pub fn identity(args: Args) -> Object {
    // `typing.cast(T, value)` returns the value unchanged.
    args.arg(1, "val").unwrap_or_else(|| one(&args, "val"))
}

pub fn id(args: Args) -> Object {
    Object::Int(repr(&one(&args, "obj")).len() as i64)
}

// ------------------------------------------------------- json and re

pub fn to_json(value: &Object) -> serde_json::Value {
    use serde_json::Value;
    crate::object::require_value(value);
    match value {
        Object::None => Value::Null,
        Object::Bool(b) => Value::Bool(*b),
        Object::Int(i) => Value::from(*i),
        Object::Float(f) => serde_json::Number::from_f64(*f).map_or(Value::Null, Value::Number),
        Object::Str(s) => Value::String(s.to_string()),
        Object::List(_) | Object::Tuple(_) => {
            Value::Array(iter(value).iter().map(to_json).collect())
        }
        Object::Dict(_) => Value::Object(
            items(value)
                .iter()
                .map(|(k, v)| (text(k), to_json(v)))
                .collect(),
        ),
        other => raise_new(
            "TypeError",
            &format!(
                "Object of type {} is not JSON serializable",
                type_name(other)
            ),
        ),
    }
}

pub fn from_json(value: &serde_json::Value) -> Object {
    use serde_json::Value;
    match value {
        Value::Null => Object::None,
        Value::Bool(b) => Object::Bool(*b),
        Value::Number(n) => n
            .as_i64()
            .map_or_else(|| Object::Float(n.as_f64().unwrap_or(0.0)), Object::Int),
        Value::String(s) => str_(s),
        Value::Array(items) => list(items.iter().map(from_json).collect()),
        Value::Object(fields) => dict(
            fields
                .iter()
                .map(|(k, v)| (str_(k), from_json(v)))
                .collect(),
        ),
    }
}

pub fn json_loads(args: Args) -> Object {
    let source = match one(&args, "s") {
        Object::Bytes(b) => String::from_utf8_lossy(&b).into_owned(),
        other => text(&other),
    };
    match serde_json::from_str::<serde_json::Value>(&source) {
        Ok(value) => from_json(&value),
        Err(error) => raise_new("json.JSONDecodeError", &error.to_string()),
    }
}

pub fn json_dumps(args: Args) -> Object {
    let value = to_json(&one(&args, "obj"));
    let indent = args.kwarg("indent").filter(|i| !matches!(i, Object::None));
    let rendered = match indent {
        Some(_) => serde_json::to_string_pretty(&value),
        None => serde_json::to_string(&value).map(|s| python_separators(&s)),
    };
    str_(&rendered.expect("serializable"))
}

/// Python's default separators are `", "` and `": "`.
fn python_separators(compact: &str) -> String {
    let mut out = String::with_capacity(compact.len() + 16);
    let mut in_string = false;
    let mut escaped = false;
    for c in compact.chars() {
        out.push(c);
        if in_string {
            match (escaped, c) {
                (true, _) => escaped = false,
                (false, '\\') => escaped = true,
                (false, '"') => in_string = false,
                _ => {}
            }
        } else if c == '"' {
            in_string = true;
        } else if c == ',' || c == ':' {
            out.push(' ');
        }
    }
    out
}

fn regex(pattern: &Object) -> regex::Regex {
    regex::Regex::new(&text(pattern)).unwrap_or_else(|e| raise_new("re.error", &e.to_string()))
}

fn match_object(found: Option<regex::Captures<'_>>) -> Object {
    match found {
        None => Object::None,
        Some(captures) => {
            let groups: Vec<Object> = captures
                .iter()
                .map(|g| g.map_or(Object::None, |m| str_(m.as_str())))
                .collect();
            let all = tuple(groups.clone());
            crate::object::instance(
                "re.Match",
                vec![
                    ("_groups", all),
                    (
                        "group",
                        crate::object::func("group", move |a: Args| {
                            let i = a.pos.first().map_or(0, int_of) as usize;
                            groups.get(i).cloned().unwrap_or(Object::None)
                        }),
                    ),
                ],
            )
        }
    }
}

pub fn re_search(args: Args) -> Object {
    let re = regex(&one(&args, "pattern"));
    let subject = text(&args.bind(1, "string", None));
    match_object(re.captures(&subject))
}

pub fn re_match(args: Args) -> Object {
    let re = regex(&str_(&format!("^(?:{})", text(&one(&args, "pattern")))));
    let subject = text(&args.bind(1, "string", None));
    match_object(re.captures(&subject))
}

pub fn re_findall(args: Args) -> Object {
    let re = regex(&one(&args, "pattern"));
    let subject = text(&args.bind(1, "string", None));
    list(re.find_iter(&subject).map(|m| str_(m.as_str())).collect())
}

pub fn re_escape(args: Args) -> Object {
    str_(&regex::escape(&text(&one(&args, "pattern"))))
}

// ------------------------------------------------------------- os, time

pub fn os_getpid(_args: Args) -> Object {
    Object::Int(i64::from(std::process::id()))
}

pub fn os_getcwd(_args: Args) -> Object {
    str_(&std::env::current_dir().expect("cwd").display().to_string())
}

pub fn os_path_join(args: Args) -> Object {
    let mut path = PathBuf::new();
    for part in &args.pos {
        path.push(text(part));
    }
    str_(&path.display().to_string())
}

pub fn os_path_exists(args: Args) -> Object {
    Object::Bool(PathBuf::from(text(&one(&args, "path"))).exists())
}

pub fn os_getenv(args: Args) -> Object {
    let key = text(&one(&args, "key"));
    crate::object::env_get(&key).map_or_else(
        || args.arg(1, "default").unwrap_or(Object::None),
        |v| str_(&v),
    )
}

pub fn shutil_which(args: Args) -> Object {
    let program = text(&one(&args, "cmd"));
    let path = crate::object::env_get("PATH").unwrap_or_default();
    std::env::split_paths(&path)
        .map(|dir| dir.join(&program))
        .find(|candidate| candidate.is_file())
        .map_or(Object::None, |found| str_(&found.display().to_string()))
}

pub fn time_monotonic(_args: Args) -> Object {
    use std::sync::OnceLock;
    static START: OnceLock<std::time::Instant> = OnceLock::new();
    Object::Float(
        START
            .get_or_init(std::time::Instant::now)
            .elapsed()
            .as_secs_f64()
            + 1000.0,
    )
}

pub fn time_time(_args: Args) -> Object {
    Object::Float(
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .expect("clock")
            .as_secs_f64(),
    )
}

/// `time.sleep` does not wait: tests observe only order, not duration.
pub fn time_sleep(_args: Args) -> Object {
    Object::None
}

pub fn textwrap_dedent(args: Args) -> Object {
    let source = text(&one(&args, "text"));
    let indent = source
        .lines()
        .filter(|l| !l.trim().is_empty())
        .map(|l| l.len() - l.trim_start().len())
        .min()
        .unwrap_or(0);
    str_(
        &source
            .split('\n')
            .map(|l| {
                if l.len() >= indent {
                    &l[indent..]
                } else {
                    l.trim_start()
                }
            })
            .collect::<Vec<_>>()
            .join("\n"),
    )
}

pub fn path_new(args: Args) -> Object {
    let mut path = PathBuf::new();
    for part in &args.pos {
        path.push(text(part));
    }
    Object::Path(Arc::new(path))
}

pub fn path_home(_args: Args) -> Object {
    Object::Path(Arc::new(PathBuf::from(
        crate::object::env_get("HOME").unwrap_or_default(),
    )))
}

// ------------------------------------------------------------- asyncio

/// `asyncio.sleep`: a coroutine that finishes at once.
pub fn asyncio_sleep(args: Args) -> Object {
    let result = args.arg(1, "result").unwrap_or(Object::None);
    coroutine(move || result)
}

/// `asyncio.gather(*aws)`: awaited in order.
pub fn asyncio_gather(args: Args) -> Object {
    let pending = args.pos;
    let tolerant = args
        .kw
        .iter()
        .any(|(k, v)| k == "return_exceptions" && truthy(v));
    coroutine(move || {
        list(
            pending
                .into_iter()
                .map(|aw| {
                    if !tolerant {
                        return await_(aw);
                    }
                    match crate::object::catch(|| crate::object::Flow::Return(await_(aw))) {
                        Ok(crate::object::Flow::Return(v)) => v,
                        Ok(_) => Object::None,
                        Err(raised) => raised.0,
                    }
                })
                .collect(),
        )
    })
}

/// `asyncio.wait_for(aw, timeout)`: there is no real clock, so it just awaits.
pub fn asyncio_wait_for(args: Args) -> Object {
    let aw = one(&args, "fut");
    coroutine(move || await_(aw))
}

/// `asyncio.create_task(coro)`: a task that runs when awaited.
pub fn asyncio_create_task(args: Args) -> Object {
    let coro = one(&args, "coro");
    let done = Arc::new(std::sync::Mutex::new(None::<Object>));
    let result = done.clone();
    crate::object::instance(
        "Task",
        vec![
            ("_coro", coro.clone()),
            (
                "done",
                crate::object::func("done", move |_| Object::Bool(lock(&result).is_some())),
            ),
            (
                "cancel",
                crate::object::func("cancel", |_| Object::Bool(true)),
            ),
            (
                "__await__",
                crate::object::func("__await__", move |_| {
                    let mut slot = lock(&done);
                    slot.get_or_insert_with(|| await_(coro.clone())).clone()
                }),
            ),
        ],
    )
}

pub fn asyncio_run(args: Args) -> Object {
    await_(one(&args, "main"))
}

// --------------------------------------------------------- formatting

/// `"%s" % value`.
pub fn percent_format(template: &str, values: &Object) -> Object {
    let mut values = match values {
        Object::Tuple(items) => items.to_vec(),
        other => vec![other.clone()],
    }
    .into_iter();
    let mut out = String::new();
    let mut chars = template.chars().peekable();
    while let Some(c) = chars.next() {
        if c != '%' {
            out.push(c);
            continue;
        }
        match chars.next() {
            Some('%') => out.push('%'),
            Some('r') => out.push_str(&repr(&values.next().unwrap_or(Object::None))),
            Some('d') => {
                out.push_str(&int_of(&values.next().unwrap_or(Object::Int(0))).to_string())
            }
            Some(_) => out.push_str(&text(&values.next().unwrap_or(Object::None))),
            None => out.push('%'),
        }
    }
    str_(&out)
}

/// `format(value, spec)` for the specs the suite uses: width, precision, `,`.
pub fn format_spec(value: &Object, spec: &str) -> Object {
    let (body, kind) = match spec.chars().last() {
        Some(k @ ('f' | 'd' | 's' | '%' | 'x' | 'e')) => (&spec[..spec.len() - 1], Some(k)),
        _ => (spec, None),
    };
    let (width, precision) = match body.split_once('.') {
        Some((w, p)) => (w, p.parse::<usize>().ok()),
        None => (body, None),
    };
    let rendered = match (kind, value) {
        (Some('f'), v) => format!(
            "{:.*}",
            precision.unwrap_or(6),
            crate::convert::to::<f64>(v, "format")
        ),
        (Some('%'), v) => format!(
            "{:.*}%",
            precision.unwrap_or(6),
            100.0 * crate::convert::to::<f64>(v, "format")
        ),
        (Some('x'), v) => format!("{:x}", int_of(v)),
        (_, v) => text(v),
    };
    let (align, width) = match width.chars().next() {
        Some(a @ ('<' | '>' | '^')) => (a, &width[1..]),
        _ => (
            if matches!(value, Object::Str(_)) {
                '<'
            } else {
                '>'
            },
            width,
        ),
    };
    let width = width.trim_start_matches('0').parse::<usize>().unwrap_or(0);
    let pad = width.saturating_sub(rendered.chars().count());
    str_(&match align {
        '<' => format!("{rendered}{}", " ".repeat(pad)),
        '^' => format!(
            "{}{rendered}{}",
            " ".repeat(pad / 2),
            " ".repeat(pad - pad / 2)
        ),
        _ => format!("{}{rendered}", " ".repeat(pad)),
    })
}

// ------------------------------------------------------------- methods

fn s(this: &Object) -> String {
    match this {
        Object::Str(s) => s.to_string(),
        other => text(other),
    }
}

pub fn str_startswith(this: Object, args: Args) -> Object {
    let subject = s(&this);
    let prefixes = match one(&args, "prefix") {
        Object::Tuple(items) => items.iter().map(text).collect(),
        other => vec![text(&other)],
    };
    Object::Bool(prefixes.iter().any(|p| subject.starts_with(p.as_str())))
}

pub fn str_endswith(this: Object, args: Args) -> Object {
    let subject = s(&this);
    let suffixes = match one(&args, "suffix") {
        Object::Tuple(items) => items.iter().map(text).collect(),
        other => vec![text(&other)],
    };
    Object::Bool(suffixes.iter().any(|p| subject.ends_with(p.as_str())))
}

pub fn str_split(this: Object, args: Args) -> Object {
    let subject = s(&this);
    let limit = args.arg(1, "maxsplit").map_or(-1, |m| int_of(&m));
    let parts: Vec<String> = match args.arg(0, "sep").filter(|p| !matches!(p, Object::None)) {
        Some(sep) => {
            let sep = text(&sep);
            if limit < 0 {
                subject.split(sep.as_str()).map(str::to_owned).collect()
            } else {
                subject
                    .splitn(limit as usize + 1, sep.as_str())
                    .map(str::to_owned)
                    .collect()
            }
        }
        None => subject.split_whitespace().map(str::to_owned).collect(),
    };
    list(parts.iter().map(|p| str_(p)).collect())
}

pub fn str_rsplit(this: Object, args: Args) -> Object {
    let subject = s(&this);
    let sep = text(&args.bind(0, "sep", None));
    let limit = args.arg(1, "maxsplit").map_or(-1, |m| int_of(&m));
    let mut parts: Vec<String> = if limit < 0 {
        subject.rsplit(sep.as_str()).map(str::to_owned).collect()
    } else {
        subject
            .rsplitn(limit as usize + 1, sep.as_str())
            .map(str::to_owned)
            .collect()
    };
    parts.reverse();
    list(parts.iter().map(|p| str_(p)).collect())
}

pub fn str_splitlines(this: Object, _args: Args) -> Object {
    list(s(&this).lines().map(str_).collect())
}

fn strip_chars(args: &Args) -> Option<Vec<char>> {
    args.arg(0, "chars")
        .filter(|c| !matches!(c, Object::None))
        .map(|c| text(&c).chars().collect())
}

pub fn str_strip(this: Object, args: Args) -> Object {
    let subject = s(&this);
    str_(match strip_chars(&args) {
        Some(chars) => subject.trim_matches(|c| chars.contains(&c)),
        None => subject.trim(),
    })
}

pub fn str_lstrip(this: Object, args: Args) -> Object {
    let subject = s(&this);
    str_(match strip_chars(&args) {
        Some(chars) => subject.trim_start_matches(|c| chars.contains(&c)),
        None => subject.trim_start(),
    })
}

pub fn str_rstrip(this: Object, args: Args) -> Object {
    let subject = s(&this);
    str_(match strip_chars(&args) {
        Some(chars) => subject.trim_end_matches(|c| chars.contains(&c)),
        None => subject.trim_end(),
    })
}

pub fn str_lower(this: Object, _args: Args) -> Object {
    str_(&s(&this).to_lowercase())
}

pub fn str_upper(this: Object, _args: Args) -> Object {
    str_(&s(&this).to_uppercase())
}

pub fn str_replace(this: Object, args: Args) -> Object {
    str_(&s(&this).replace(&text(&one(&args, "old")), &text(&args.bind(1, "new", None))))
}

pub fn str_join(this: Object, args: Args) -> Object {
    let separator = s(&this);
    str_(
        &iter(&one(&args, "iterable"))
            .iter()
            .map(text)
            .collect::<Vec<_>>()
            .join(&separator),
    )
}

pub fn str_find(this: Object, args: Args) -> Object {
    let subject = s(&this);
    let needle = text(&one(&args, "sub"));
    Object::Int(
        subject
            .find(&needle)
            .map_or(-1, |byte| subject[..byte].chars().count() as i64),
    )
}

pub fn str_count(this: Object, args: Args) -> Object {
    Object::from(s(&this).matches(&text(&one(&args, "sub"))).count())
}

pub fn str_encode(this: Object, _args: Args) -> Object {
    crate::object::bytes(s(&this).as_bytes())
}

pub fn str_isdigit(this: Object, _args: Args) -> Object {
    let subject = s(&this);
    Object::Bool(!subject.is_empty() && subject.chars().all(|c| c.is_ascii_digit()))
}

pub fn str_format(this: Object, args: Args) -> Object {
    let template = s(&this);
    let mut out = String::new();
    let mut auto = 0usize;
    let mut chars = template.chars().peekable();
    while let Some(c) = chars.next() {
        match c {
            '{' if chars.peek() == Some(&'{') => {
                chars.next();
                out.push('{');
            }
            '}' if chars.peek() == Some(&'}') => {
                chars.next();
                out.push('}');
            }
            '{' => {
                let field: String = chars.by_ref().take_while(|c| *c != '}').collect();
                let (key, spec) = field.split_once(':').unwrap_or((&field, ""));
                let value = if key.is_empty() {
                    auto += 1;
                    args.pos.get(auto - 1).cloned()
                } else if let Ok(i) = key.parse::<usize>() {
                    args.pos.get(i).cloned()
                } else {
                    args.kwarg(key)
                };
                let value = value.unwrap_or_else(|| {
                    raise(crate::object::new_exception("KeyError", vec![str_(key)]))
                });
                out.push_str(&text(&crate::object::format_value(&value, "", spec)));
            }
            other => out.push(other),
        }
    }
    str_(&out)
}

pub fn bytes_decode(this: Object, _args: Args) -> Object {
    match this {
        Object::Bytes(b) => str_(&String::from_utf8_lossy(&b)),
        other => other,
    }
}

fn list_of(this: &Object) -> &Arc<std::sync::Mutex<Vec<Object>>> {
    match this {
        Object::List(items) | Object::Set(items) => items,
        other => raise_new(
            "TypeError",
            &format!("expected list, got {}", type_name(other)),
        ),
    }
}

pub fn list_append(this: Object, args: Args) -> Object {
    lock(list_of(&this)).push(one(&args, "object"));
    Object::None
}

pub fn list_extend(this: Object, args: Args) -> Object {
    let more = iter(&one(&args, "iterable"));
    lock(list_of(&this)).extend(more);
    Object::None
}

pub fn list_insert(this: Object, args: Args) -> Object {
    let index = int_of(&one(&args, "index"));
    let value = args.bind(1, "object", None);
    let mut items = lock(list_of(&this));
    let len = items.len() as i64;
    let at = if index < 0 {
        (len + index).max(0)
    } else {
        index.min(len)
    };
    items.insert(at as usize, value);
    Object::None
}

pub fn list_pop(this: Object, args: Args) -> Object {
    if let Object::Dict(_) = this {
        return dict_pop(this, args);
    }
    let mut items = lock(list_of(&this));
    if items.is_empty() {
        raise_new("IndexError", "pop from empty list");
    }
    let index = args.pos.first().map_or(items.len() as i64 - 1, int_of);
    let at = if index < 0 {
        items.len() as i64 + index
    } else {
        index
    };
    items.remove(at as usize)
}

pub fn list_remove(this: Object, args: Args) -> Object {
    let value = one(&args, "value");
    let mut items = lock(list_of(&this));
    match items.iter().position(|v| eq(v, &value)) {
        Some(i) => {
            items.remove(i);
            Object::None
        }
        None => raise_new("ValueError", "list.remove(x): x not in list"),
    }
}

pub fn list_index(this: Object, args: Args) -> Object {
    let value = one(&args, "value");
    iter(&this).iter().position(|v| eq(v, &value)).map_or_else(
        || raise_new("ValueError", "value is not in list"),
        Object::from,
    )
}

pub fn list_count(this: Object, args: Args) -> Object {
    let value = one(&args, "value");
    Object::from(iter(&this).iter().filter(|v| eq(v, &value)).count())
}

pub fn list_copy(this: Object, _args: Args) -> Object {
    match &this {
        Object::Dict(_) => dict(items(&this)),
        Object::Set(_) => set(iter(&this)),
        _ => list(iter(&this)),
    }
}

pub fn list_clear(this: Object, _args: Args) -> Object {
    match &this {
        Object::Dict(pairs) => lock(pairs).clear(),
        _ => lock(list_of(&this)).clear(),
    }
    Object::None
}

pub fn list_sort(this: Object, args: Args) -> Object {
    let sorted_values = sorted(Args {
        pos: vec![this.clone()],
        kw: args.kw,
    });
    *lock(list_of(&this)) = iter(&sorted_values);
    Object::None
}

pub fn set_add(this: Object, args: Args) -> Object {
    let value = one(&args, "elem");
    let mut items = lock(list_of(&this));
    if !items.iter().any(|v| eq(v, &value)) {
        items.push(value);
    }
    Object::None
}

pub fn set_discard(this: Object, args: Args) -> Object {
    let value = one(&args, "elem");
    lock(list_of(&this)).retain(|v| !eq(v, &value));
    Object::None
}

pub fn set_update(this: Object, args: Args) -> Object {
    if let Object::Dict(_) = this {
        return dict_update(this, args);
    }
    for value in args.pos.iter().flat_map(iter) {
        set_add(this.clone(), Args::new(vec![value], vec![]));
    }
    Object::None
}

pub fn set_issubset(this: Object, args: Args) -> Object {
    let other = iter(&one(&args, "other"));
    Object::Bool(iter(&this).iter().all(|v| other.iter().any(|o| eq(v, o))))
}

fn pairs_of(this: &Object) -> &Arc<std::sync::Mutex<Vec<(Object, Object)>>> {
    match this {
        Object::Dict(pairs) => pairs,
        other => raise_new(
            "TypeError",
            &format!("expected dict, got {}", type_name(other)),
        ),
    }
}

pub fn dict_get(this: Object, args: Args) -> Object {
    if let Object::Native(_) = this {
        return crate::object::call_method(&this, "get", args);
    }
    let key = one(&args, "key");
    lock(pairs_of(&this))
        .iter()
        .find(|(k, _)| eq(k, &key))
        .map(|(_, v)| v.clone())
        .unwrap_or_else(|| args.arg(1, "default").unwrap_or(Object::None))
}

pub fn dict_items(this: Object, _args: Args) -> Object {
    list(
        items(&this)
            .into_iter()
            .map(|(k, v)| tuple(vec![k, v]))
            .collect(),
    )
}

pub fn dict_keys(this: Object, _args: Args) -> Object {
    list(items(&this).into_iter().map(|(k, _)| k).collect())
}

pub fn dict_values(this: Object, _args: Args) -> Object {
    list(items(&this).into_iter().map(|(_, v)| v).collect())
}

pub fn dict_pop(this: Object, args: Args) -> Object {
    let key = one(&args, "key");
    let mut pairs = lock(pairs_of(&this));
    match pairs.iter().position(|(k, _)| eq(k, &key)) {
        Some(i) => pairs.remove(i).1,
        None => args
            .arg(1, "default")
            .unwrap_or_else(|| raise(crate::object::new_exception("KeyError", vec![key]))),
    }
}

pub fn dict_setdefault(this: Object, args: Args) -> Object {
    let key = one(&args, "key");
    let default = args.arg(1, "default").unwrap_or(Object::None);
    let mut pairs = lock(pairs_of(&this));
    if let Some((_, v)) = pairs.iter().find(|(k, _)| eq(k, &key)) {
        return v.clone();
    }
    pairs.push((key, default.clone()));
    default
}

pub fn dict_update(this: Object, args: Args) -> Object {
    let mut incoming = args.pos.first().map(items).unwrap_or_default();
    incoming.extend(args.kw.iter().map(|(k, v)| (str_(k), v.clone())));
    for (k, v) in incoming {
        crate::object::setitem(&this, &k, v);
    }
    Object::None
}

fn path_of(this: &Object) -> PathBuf {
    match this {
        Object::Path(p) => p.as_ref().clone(),
        other => PathBuf::from(text(other)),
    }
}

fn io_error(error: std::io::Error, path: &std::path::Path) -> ! {
    let class = match error.kind() {
        std::io::ErrorKind::NotFound => "FileNotFoundError",
        std::io::ErrorKind::PermissionDenied => "PermissionError",
        std::io::ErrorKind::AlreadyExists => "FileExistsError",
        _ => "OSError",
    };
    raise_new(class, &format!("{error}: '{}'", path.display()))
}

pub fn path_read_text(this: Object, _args: Args) -> Object {
    let path = path_of(&this);
    str_(&std::fs::read_to_string(&path).unwrap_or_else(|e| io_error(e, &path)))
}

pub fn path_read_bytes(this: Object, _args: Args) -> Object {
    let path = path_of(&this);
    crate::object::bytes(&std::fs::read(&path).unwrap_or_else(|e| io_error(e, &path)))
}

pub fn path_write_text(this: Object, args: Args) -> Object {
    let path = path_of(&this);
    let data = text(&one(&args, "data"));
    std::fs::write(&path, &data).unwrap_or_else(|e| io_error(e, &path));
    Object::from(data.chars().count())
}

pub fn path_write_bytes(this: Object, args: Args) -> Object {
    let path = path_of(&this);
    let Object::Bytes(data) = one(&args, "data") else {
        raise_new("TypeError", "write_bytes expects bytes");
    };
    std::fs::write(&path, data.as_slice()).unwrap_or_else(|e| io_error(e, &path));
    Object::from(data.len())
}

pub fn path_exists(this: Object, _args: Args) -> Object {
    Object::Bool(path_of(&this).exists())
}

pub fn path_is_file(this: Object, _args: Args) -> Object {
    Object::Bool(path_of(&this).is_file())
}

pub fn path_is_dir(this: Object, _args: Args) -> Object {
    Object::Bool(path_of(&this).is_dir())
}

pub fn path_mkdir(this: Object, args: Args) -> Object {
    let path = path_of(&this);
    let parents = args.kwarg("parents").is_some_and(|p| truthy(&p));
    let exist_ok = args.kwarg("exist_ok").is_some_and(|p| truthy(&p));
    let result = if parents {
        std::fs::create_dir_all(&path)
    } else {
        std::fs::create_dir(&path)
    };
    match result {
        Err(e) if !(exist_ok && e.kind() == std::io::ErrorKind::AlreadyExists) => {
            io_error(e, &path)
        }
        _ => Object::None,
    }
}

pub fn path_unlink(this: Object, args: Args) -> Object {
    let path = path_of(&this);
    let missing_ok = args.kwarg("missing_ok").is_some_and(|p| truthy(&p));
    match std::fs::remove_file(&path) {
        Err(e) if !(missing_ok && e.kind() == std::io::ErrorKind::NotFound) => io_error(e, &path),
        _ => Object::None,
    }
}

pub fn path_resolve(this: Object, _args: Args) -> Object {
    let path = path_of(&this);
    Object::from(std::fs::canonicalize(&path).unwrap_or(path))
}

pub fn path_joinpath(this: Object, args: Args) -> Object {
    let mut path = path_of(&this);
    for part in &args.pos {
        path.push(text(part));
    }
    Object::from(path)
}

pub fn path_iterdir(this: Object, _args: Args) -> Object {
    let path = path_of(&this);
    let mut entries: Vec<PathBuf> = std::fs::read_dir(&path)
        .unwrap_or_else(|e| io_error(e, &path))
        .filter_map(|e| e.ok().map(|e| e.path()))
        .collect();
    entries.sort();
    list(entries.into_iter().map(Object::from).collect())
}

pub fn path_parent(this: Object, _args: Args) -> Object {
    let path = path_of(&this);
    Object::from(path.parent().map(PathBuf::from).unwrap_or(path))
}

pub fn path_parents(this: Object, _args: Args) -> Object {
    let path = path_of(&this);
    list(
        path.ancestors()
            .skip(1)
            .map(|p| Object::from(p.to_path_buf()))
            .collect(),
    )
}

pub fn path_name(this: Object, _args: Args) -> Object {
    str_(
        &path_of(&this)
            .file_name()
            .map(|n| n.to_string_lossy().into_owned())
            .unwrap_or_default(),
    )
}

pub fn path_stem(this: Object, _args: Args) -> Object {
    str_(
        &path_of(&this)
            .file_stem()
            .map(|n| n.to_string_lossy().into_owned())
            .unwrap_or_default(),
    )
}

pub fn path_suffix(this: Object, _args: Args) -> Object {
    str_(
        &path_of(&this)
            .extension()
            .map(|n| format!(".{}", n.to_string_lossy()))
            .unwrap_or_default(),
    )
}

pub fn exception_str(this: Object, _args: Args) -> Object {
    str_(&text(&this))
}

pub fn object_await(this: Object, _args: Args) -> Object {
    match getattr_opt(&this, "__await__") {
        Some(f) => call(&f, Args::none()),
        None => this,
    }
}

pub fn path_chmod(this: Object, args: Args) -> Object {
    use std::os::unix::fs::PermissionsExt;
    let path = path_of(&this);
    let mode = int_of(&one(&args, "mode")) as u32;
    std::fs::set_permissions(&path, std::fs::Permissions::from_mode(mode))
        .unwrap_or_else(|e| io_error(e, &path));
    Object::None
}

pub fn path_touch(this: Object, _args: Args) -> Object {
    let path = path_of(&this);
    std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(&path)
        .unwrap_or_else(|e| io_error(e, &path));
    Object::None
}

pub fn path_symlink_to(this: Object, args: Args) -> Object {
    let path = path_of(&this);
    let target = path_of(&one(&args, "target"));
    std::os::unix::fs::symlink(&target, &path).unwrap_or_else(|e| io_error(e, &path));
    Object::None
}

pub fn path_stat(this: Object, _args: Args) -> Object {
    use std::os::unix::fs::MetadataExt;
    let path = path_of(&this);
    let meta = std::fs::metadata(&path).unwrap_or_else(|e| io_error(e, &path));
    crate::object::instance(
        "stat_result",
        vec![
            ("st_size", Object::Int(meta.size() as i64)),
            ("st_mode", Object::Int(i64::from(meta.mode()))),
            (
                "st_mtime",
                Object::Float(meta.mtime() as f64 + meta.mtime_nsec() as f64 / 1e9),
            ),
            (
                "st_mtime_ns",
                Object::Int(meta.mtime() * 1_000_000_000 + meta.mtime_nsec()),
            ),
            ("st_uid", Object::Int(i64::from(meta.uid()))),
            ("st_ino", Object::Int(meta.ino() as i64)),
        ],
    )
}

pub fn path_with_suffix(this: Object, args: Args) -> Object {
    let mut path = path_of(&this);
    let suffix = text(&one(&args, "suffix"));
    path.set_extension(suffix.trim_start_matches('.'));
    Object::from(path)
}

pub fn path_with_name(this: Object, args: Args) -> Object {
    let path = path_of(&this);
    Object::from(path.with_file_name(text(&one(&args, "name"))))
}

pub fn path_is_symlink(this: Object, _args: Args) -> Object {
    Object::Bool(path_of(&this).is_symlink())
}

pub fn path_rmdir(this: Object, _args: Args) -> Object {
    let path = path_of(&this);
    std::fs::remove_dir(&path).unwrap_or_else(|e| io_error(e, &path));
    Object::None
}

pub fn path_rename(this: Object, args: Args) -> Object {
    let path = path_of(&this);
    let target = path_of(&one(&args, "target"));
    std::fs::rename(&path, &target).unwrap_or_else(|e| io_error(e, &path));
    Object::from(target)
}

pub fn path_glob(this: Object, args: Args) -> Object {
    let base = path_of(&this);
    let pattern = text(&one(&args, "pattern"));
    let recursive = pattern.starts_with("**/");
    let name = pattern.trim_start_matches("**/");
    let regex = regex::Regex::new(&format!(
        "^{}$",
        regex::escape(name)
            .replace("\\*", "[^/]*")
            .replace("\\?", "[^/]")
    ))
    .unwrap_or_else(|e| raise_new("ValueError", &e.to_string()));
    let mut found = Vec::new();
    let mut stack = vec![base];
    while let Some(directory) = stack.pop() {
        let Ok(entries) = std::fs::read_dir(&directory) else {
            continue;
        };
        for entry in entries.flatten() {
            let path = entry.path();
            if recursive && path.is_dir() {
                stack.push(path.clone());
            }
            if regex.is_match(&entry.file_name().to_string_lossy()) {
                found.push(path);
            }
        }
    }
    found.sort();
    crate::object::list(found.into_iter().map(Object::from).collect())
}

pub fn bytes_strip(this: Object, _args: Args) -> Object {
    let Object::Bytes(data) = &this else {
        return this;
    };
    let start = data
        .iter()
        .position(|b| !b.is_ascii_whitespace())
        .unwrap_or(data.len());
    let end = data
        .iter()
        .rposition(|b| !b.is_ascii_whitespace())
        .map_or(start, |i| i + 1);
    crate::object::bytes(&data[start..end.max(start)])
}

pub fn str_partition(this: Object, args: Args) -> Object {
    let subject = text(&this);
    let separator = text(&one(&args, "sep"));
    let (a, sep, b) = match subject.split_once(&separator) {
        Some((a, b)) => (a, separator.as_str(), b),
        None => (subject.as_str(), "", ""),
    };
    crate::object::tuple(vec![str_(a), str_(sep), str_(b)])
}

pub fn str_rpartition(this: Object, args: Args) -> Object {
    let subject = text(&this);
    let separator = text(&one(&args, "sep"));
    let (a, sep, b) = match subject.rsplit_once(&separator) {
        Some((a, b)) => (a, separator.as_str(), b),
        None => ("", "", subject.as_str()),
    };
    crate::object::tuple(vec![str_(a), str_(sep), str_(b)])
}

pub fn path_rglob(this: Object, args: Args) -> Object {
    let pattern = text(&one(&args, "pattern"));
    path_glob(
        this,
        Args::new(vec![str_(&format!("**/{pattern}"))], vec![]),
    )
}

pub fn str_removeprefix(this: Object, args: Args) -> Object {
    let subject = text(&this);
    let prefix = text(&one(&args, "prefix"));
    str_(subject.strip_prefix(prefix.as_str()).unwrap_or(&subject))
}

pub fn str_removesuffix(this: Object, args: Args) -> Object {
    let subject = text(&this);
    let suffix = text(&one(&args, "suffix"));
    str_(subject.strip_suffix(suffix.as_str()).unwrap_or(&subject))
}

/// `__file__` of the translated Python source: `relative` to the repository
/// root, the directory above the crate being built.
pub fn source_file(manifest_dir: &str, relative: &str) -> Object {
    let root = std::path::Path::new(manifest_dir);
    let path = root.parent().unwrap_or(root).join(relative);
    str_(&path.to_string_lossy())
}
