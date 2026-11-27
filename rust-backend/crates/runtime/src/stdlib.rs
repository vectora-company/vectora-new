//! Small standard modules: `io.BytesIO`, `contextlib.ExitStack`,
//! `hashlib.sha256`, `object`, `types.SimpleNamespace` and `stat` constants.

use std::sync::{Arc, Mutex};

use crate::object::{
    Args, Native, Object, Raised, bytes, call, func, lock, native, raise_new, str_, text,
};

fn data(value: &Object) -> Vec<u8> {
    match value {
        Object::Bytes(b) => b.to_vec(),
        Object::None => Vec::new(),
        other => text(other).into_bytes(),
    }
}

// --------------------------------------------------------------- BytesIO

struct Buffer {
    data: Vec<u8>,
    position: usize,
}

struct BytesIo(Arc<Mutex<Buffer>>);

/// `io.BytesIO(initial=b"")`.
pub fn bytes_io(args: Args) -> Object {
    let initial = args
        .arg(0, "initial_bytes")
        .map(|b| data(&b))
        .unwrap_or_default();
    native(BytesIo(Arc::new(Mutex::new(Buffer {
        data: initial,
        position: 0,
    }))))
}

impl Native for BytesIo {
    fn class(&self) -> String {
        "BytesIO".into()
    }
    fn enter(&self, this: &Object) -> Option<Object> {
        Some(this.clone())
    }
    fn getattr(&self, _this: &Object, attribute: &str) -> Option<Object> {
        let state = self.0.clone();
        Some(match attribute {
            "getvalue" | "getbuffer" => func(attribute, move |_| bytes(&lock(&state).data)),
            "write" => func("write", move |args| {
                let chunk = data(&args.bind(0, "b", None));
                let mut buffer = lock(&state);
                let at = buffer.position;
                let end = at + chunk.len();
                if buffer.data.len() < end {
                    buffer.data.resize(end, 0);
                }
                buffer.data[at..end].copy_from_slice(&chunk);
                buffer.position = end;
                Object::Int(chunk.len() as i64)
            }),
            "read" => func("read", move |args| {
                let mut buffer = lock(&state);
                let at = buffer.position.min(buffer.data.len());
                let end = match args.arg(0, "size") {
                    Some(Object::Int(n)) if n >= 0 => (at + n as usize).min(buffer.data.len()),
                    _ => buffer.data.len(),
                };
                buffer.position = end;
                bytes(&buffer.data[at..end])
            }),
            "seek" => func("seek", move |args| {
                let offset = match args.bind(0, "offset", None) {
                    Object::Int(n) => n,
                    _ => 0,
                };
                let whence = match args.arg(1, "whence") {
                    Some(Object::Int(n)) => n,
                    _ => 0,
                };
                let mut buffer = lock(&state);
                let base = match whence {
                    1 => buffer.position as i64,
                    2 => buffer.data.len() as i64,
                    _ => 0,
                };
                buffer.position = (base + offset).max(0) as usize;
                Object::Int(buffer.position as i64)
            }),
            "tell" => func("tell", move |_| Object::Int(lock(&state).position as i64)),
            "close" | "flush" => func(attribute, |_| Object::None),
            "closed" => Object::Bool(false),
            _ => return None,
        })
    }
}

// ------------------------------------------------------------- ExitStack

enum Exit {
    Manager(Object),
    Callback(Object, Args),
}

struct ExitStack(Arc<Mutex<Vec<Exit>>>);

/// `contextlib.ExitStack()` and `contextlib.AsyncExitStack()`.
pub fn exit_stack(_args: Args) -> Object {
    native(ExitStack(Arc::new(Mutex::new(Vec::new()))))
}

impl ExitStack {
    fn unwind(&self, error: Option<&Raised>) -> bool {
        let mut suppressed = false;
        loop {
            let Some(next) = lock(&self.0).pop() else {
                break;
            };
            match next {
                Exit::Manager(manager) => {
                    let current = if suppressed { None } else { error };
                    suppressed |= crate::object::exit(&manager, current);
                }
                Exit::Callback(function, args) => {
                    crate::object::await_(call(&function, args));
                }
            }
        }
        suppressed
    }
}

impl Native for ExitStack {
    fn class(&self) -> String {
        "ExitStack".into()
    }
    fn enter(&self, this: &Object) -> Option<Object> {
        Some(this.clone())
    }
    fn exit(&self, error: Option<&Raised>) -> bool {
        self.unwind(error)
    }
    fn getattr(&self, this: &Object, attribute: &str) -> Option<Object> {
        let stack = self.0.clone();
        let this = this.clone();
        Some(match attribute {
            "enter_context" | "enter_async_context" => func(attribute, move |args| {
                let manager = args.bind(0, "cm", None);
                let value = crate::object::enter(&manager);
                lock(&stack).push(Exit::Manager(manager));
                value
            }),
            "callback" | "push_async_callback" => func(attribute, move |mut args| {
                let function = args.pos.remove(0);
                lock(&stack).push(Exit::Callback(function.clone(), args));
                function
            }),
            "close" | "aclose" => func(attribute, move |_| {
                if let Object::Native(n) = &this {
                    n.exit(None);
                }
                Object::None
            }),
            _ => return None,
        })
    }
}

// ---------------------------------------------------------------- sha256

fn sha256_digest(message: &[u8]) -> [u8; 32] {
    const K: [u32; 64] = [
        0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4,
        0xab1c5ed5, 0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe,
        0x9bdc06a7, 0xc19bf174, 0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f,
        0x4a7484aa, 0x5cb0a9dc, 0x76f988da, 0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7,
        0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967, 0x27b70a85, 0x2e1b2138, 0x4d2c6dfc,
        0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85, 0xa2bfe8a1, 0xa81a664b,
        0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070, 0x19a4c116,
        0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
        0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7,
        0xc67178f2,
    ];
    let mut h: [u32; 8] = [
        0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab,
        0x5be0cd19,
    ];
    let mut padded = message.to_vec();
    padded.push(0x80);
    while padded.len() % 64 != 56 {
        padded.push(0);
    }
    padded.extend(((message.len() as u64) * 8).to_be_bytes());
    for chunk in padded.chunks(64) {
        let mut w = [0u32; 64];
        for i in 0..16 {
            w[i] = u32::from_be_bytes([
                chunk[4 * i],
                chunk[4 * i + 1],
                chunk[4 * i + 2],
                chunk[4 * i + 3],
            ]);
        }
        for i in 16..64 {
            let s0 = w[i - 15].rotate_right(7) ^ w[i - 15].rotate_right(18) ^ (w[i - 15] >> 3);
            let s1 = w[i - 2].rotate_right(17) ^ w[i - 2].rotate_right(19) ^ (w[i - 2] >> 10);
            w[i] = w[i - 16]
                .wrapping_add(s0)
                .wrapping_add(w[i - 7])
                .wrapping_add(s1);
        }
        let [mut a, mut b, mut c, mut d, mut e, mut f, mut g, mut hh] = h;
        for i in 0..64 {
            let s1 = e.rotate_right(6) ^ e.rotate_right(11) ^ e.rotate_right(25);
            let ch = (e & f) ^ (!e & g);
            let t1 = hh
                .wrapping_add(s1)
                .wrapping_add(ch)
                .wrapping_add(K[i])
                .wrapping_add(w[i]);
            let s0 = a.rotate_right(2) ^ a.rotate_right(13) ^ a.rotate_right(22);
            let maj = (a & b) ^ (a & c) ^ (b & c);
            let t2 = s0.wrapping_add(maj);
            hh = g;
            g = f;
            f = e;
            e = d.wrapping_add(t1);
            d = c;
            c = b;
            b = a;
            a = t1.wrapping_add(t2);
        }
        for (slot, value) in h.iter_mut().zip([a, b, c, d, e, f, g, hh]) {
            *slot = slot.wrapping_add(value);
        }
    }
    let mut out = [0u8; 32];
    for (i, word) in h.iter().enumerate() {
        out[4 * i..4 * i + 4].copy_from_slice(&word.to_be_bytes());
    }
    out
}

struct Sha256(Arc<Mutex<Vec<u8>>>);

/// `hashlib.sha256(data=b"")`.
pub fn sha256(args: Args) -> Object {
    let initial = args.arg(0, "data").map(|d| data(&d)).unwrap_or_default();
    native(Sha256(Arc::new(Mutex::new(initial))))
}

impl Native for Sha256 {
    fn class(&self) -> String {
        "sha256".into()
    }
    fn getattr(&self, _this: &Object, attribute: &str) -> Option<Object> {
        let state = self.0.clone();
        Some(match attribute {
            "update" => func("update", move |args| {
                lock(&state).extend(data(&args.bind(0, "data", None)));
                Object::None
            }),
            "digest" => func("digest", move |_| bytes(&sha256_digest(&lock(&state)))),
            "hexdigest" => func("hexdigest", move |_| {
                str_(
                    &sha256_digest(&lock(&state))
                        .iter()
                        .map(|b| format!("{b:02x}"))
                        .collect::<String>(),
                )
            }),
            "name" => str_("sha256"),
            "digest_size" => Object::Int(32),
            _ => return None,
        })
    }
}

// ---------------------------------------------------------------- object

/// The `object` class: a bare instance, plus `object.__setattr__`.
pub fn object_class() -> Object {
    static CLASS: std::sync::OnceLock<Object> = std::sync::OnceLock::new();
    CLASS
        .get_or_init(|| {
            crate::classes::class(
                "object",
                Vec::new(),
                vec![
                    (
                        "__setattr__",
                        func("__setattr__", |args| {
                            let target = args.bind(0, "self", None);
                            let name = text(&args.bind(1, "name", None));
                            crate::object::setattr(&target, &name, args.bind(2, "value", None));
                            Object::None
                        }),
                    ),
                    ("__init__", func("__init__", |_| Object::None)),
                ],
            )
        })
        .clone()
}

/// `chr(i)`.
pub fn chr(args: Args) -> Object {
    match args.bind(0, "i", None) {
        Object::Int(i) => match u32::try_from(i).ok().and_then(char::from_u32) {
            Some(c) => str_(&c.to_string()),
            None => raise_new("ValueError", "chr() arg not in range(0x110000)"),
        },
        other => raise_new(
            "TypeError",
            &format!("an integer is required, got {}", text(&other)),
        ),
    }
}

/// `filter(function, iterable)`.
pub fn filter(args: Args) -> Object {
    let function = args.bind(0, "function", None);
    let items = crate::object::iter(&args.bind(1, "iterable", None));
    crate::object::list(
        items
            .into_iter()
            .filter(|item| match &function {
                Object::None => crate::object::truthy(item),
                f => crate::object::truthy(&call(f, Args::new(vec![item.clone()], vec![]))),
            })
            .collect(),
    )
}

/// `property(fget)` called as a function.
pub fn property(args: Args) -> Object {
    crate::classes::property(args.bind(0, "fget", None))
}

/// `types.SimpleNamespace(**kw)`.
pub fn simple_namespace(args: Args) -> Object {
    let attrs: Vec<(&str, Object)> = args
        .kw
        .iter()
        .map(|(k, v)| (k.as_str(), v.clone()))
        .collect();
    crate::object::instance("SimpleNamespace", attrs)
}
