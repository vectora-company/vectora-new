//! Third-party and standard libraries the suite uses directly: `httpx` test
//! transports, `git` over the real `git` command, and small standard modules.

use std::process::Command;
use std::sync::{Arc, Mutex};
use std::time::{SystemTime, UNIX_EPOCH};

use crate::natives::{from_json, to_json};
use crate::object::{
    Args, Native, Object, Raised, await_, bytes, call, coroutine, dict, func, instance, items,
    iter, lock, native, raise_new, str_, text,
};

// ------------------------------------------------------------------ httpx

fn content_of(value: &Object) -> Vec<u8> {
    match value {
        Object::Bytes(b) => b.to_vec(),
        Object::None => Vec::new(),
        other => text(other).into_bytes(),
    }
}

fn headers(value: Option<Object>) -> Object {
    let pairs = value
        .filter(|v| !matches!(v, Object::None))
        .map(|v| items(&v))
        .unwrap_or_default();
    dict(
        pairs
            .into_iter()
            .map(|(k, v)| (str_(&text(&k).to_lowercase()), v))
            .collect(),
    )
}

/// `httpx.Request(method, url, json=, content=, headers=, params=)`.
pub fn request(args: Args) -> Object {
    let method = text(&args.bind(0, "method", None)).to_uppercase();
    let url = text(&args.bind(1, "url", None));
    let body = match (
        args.kwarg("json"),
        args.kwarg("content"),
        args.kwarg("data"),
    ) {
        (Some(json), _, _) if !matches!(json, Object::None) => {
            to_json(&json).to_string().into_bytes()
        }
        (_, Some(content), _) => content_of(&content),
        (_, _, Some(data)) if !matches!(data, Object::None) => {
            let form: Vec<String> = items(&data)
                .into_iter()
                .map(|(k, v)| format!("{}={}", text(&k), text(&v)))
                .collect();
            form.join("&").into_bytes()
        }
        _ => Vec::new(),
    };
    let params = args.kwarg("params").filter(|p| !matches!(p, Object::None));
    let url = match params {
        Some(p) => {
            let query: Vec<String> = items(&p)
                .into_iter()
                .map(|(k, v)| format!("{}={}", text(&k), text(&v)))
                .collect();
            let separator = if url.contains('?') { '&' } else { '?' };
            format!("{url}{separator}{}", query.join("&"))
        }
        None => url,
    };
    native(Request {
        method,
        url,
        body,
        headers: headers(args.kwarg("headers")),
    })
}

struct Request {
    method: String,
    url: String,
    body: Vec<u8>,
    headers: Object,
}

impl Native for Request {
    fn class(&self) -> String {
        "Request".into()
    }
    fn getattr(&self, _this: &Object, attribute: &str) -> Option<Object> {
        let body = self.body.clone();
        Some(match attribute {
            "method" => str_(&self.method),
            "url" => native(Url(self.url.clone())),
            "headers" => self.headers.clone(),
            "content" => bytes(&self.body),
            "read" => func("read", move |_| bytes(&body)),
            _ => return None,
        })
    }
}

/// `httpx.URL`: compares and prints as its text; exposes path, host, query.
struct Url(String);

impl Url {
    fn parts(&self) -> (String, String, String, String) {
        let (scheme, rest) = self.0.split_once("://").unwrap_or(("", &self.0));
        let (authority, path) = rest
            .split_once('/')
            .map_or((rest, String::from("/")), |(a, p)| (a, format!("/{p}")));
        let (path, query) = path
            .split_once('?')
            .map_or((path.clone(), String::new()), |(p, q)| {
                (p.to_owned(), q.to_owned())
            });
        (scheme.to_owned(), authority.to_owned(), path, query)
    }
}

impl Native for Url {
    fn class(&self) -> String {
        "URL".into()
    }
    fn text(&self) -> Option<String> {
        Some(self.0.clone())
    }
    fn equals(&self, other: &Object) -> Option<bool> {
        Some(text(other) == self.0)
    }
    fn getattr(&self, _this: &Object, attribute: &str) -> Option<Object> {
        let (scheme, authority, path, query) = self.parts();
        Some(match attribute {
            "scheme" => str_(&scheme),
            "host" => str_(authority.split(':').next().unwrap_or("")),
            "path" => str_(&path),
            "query" => bytes(query.as_bytes()),
            "params" => dict(
                query
                    .split('&')
                    .filter(|p| !p.is_empty())
                    .map(|p| {
                        let (k, v) = p.split_once('=').unwrap_or((p, ""));
                        (str_(k), str_(v))
                    })
                    .collect(),
            ),
            _ => return None,
        })
    }
}

/// `httpx.Response(status_code, json=, content=, text=, headers=, request=)`.
pub fn response(args: Args) -> Object {
    let status = match args.bind(0, "status_code", None) {
        Object::Int(i) => i,
        other => raise_new("TypeError", &format!("status_code {}", text(&other))),
    };
    let body = match (
        args.kwarg("json"),
        args.kwarg("content"),
        args.kwarg("text"),
    ) {
        (Some(json), _, _) => to_json(&json).to_string().into_bytes(),
        (_, Some(content), _) => content_of(&content),
        (_, _, Some(t)) => text(&t).into_bytes(),
        _ => Vec::new(),
    };
    native(Response {
        status,
        body,
        headers: headers(args.kwarg("headers")),
        request: Mutex::new(args.kwarg("request").unwrap_or(Object::None)),
    })
}

struct Response {
    status: i64,
    body: Vec<u8>,
    headers: Object,
    request: Mutex<Object>,
}

impl Native for Response {
    fn class(&self) -> String {
        "Response".into()
    }
    fn getattr(&self, this: &Object, attribute: &str) -> Option<Object> {
        let body = self.body.clone();
        let status = self.status;
        let this = this.clone();
        Some(match attribute {
            "status_code" => Object::Int(self.status),
            "is_success" => Object::Bool((200..300).contains(&self.status)),
            "is_error" => Object::Bool(self.status >= 400),
            "content" => bytes(&self.body),
            "text" => str_(&String::from_utf8_lossy(&self.body)),
            "headers" => self.headers.clone(),
            "request" => lock(&self.request).clone(),
            "json" => func("json", move |_| {
                let value: serde_json::Value = serde_json::from_slice(&body)
                    .unwrap_or_else(|e| raise_new("json.JSONDecodeError", &e.to_string()));
                from_json(&value)
            }),
            "raise_for_status" => func("raise_for_status", move |_| {
                if status >= 400 {
                    raise_new("httpx.HTTPStatusError", &format!("status {status}"));
                }
                this.clone()
            }),
            "aread" | "read" => func(attribute, move |_| {
                let data = bytes(&body);
                coroutine(move || data)
            }),
            "aclose" | "close" => func(attribute, |_| coroutine(|| Object::None)),
            _ => return None,
        })
    }
    fn setattr(&self, attribute: &str, value: Object) -> bool {
        if attribute == "request" {
            *lock(&self.request) = value;
            return true;
        }
        false
    }
}

/// `httpx.MockTransport(handler)`.
pub fn mock_transport(args: Args) -> Object {
    let handler = args.bind(0, "handler", None);
    instance("MockTransport", vec![("handler", handler)])
}

/// `httpx.AsyncClient` and `httpx.Client`; with a `MockTransport` every
/// request goes to its handler. Without one, requests fail as unreachable.
fn client(args: Args, asynchronous: bool) -> Object {
    let transport = args.kwarg("transport").unwrap_or(Object::None);
    let base = args.kwarg("base_url").map(|b| text(&b)).unwrap_or_default();
    client_handle(Client {
        transport,
        base,
        headers: headers(args.kwarg("headers")),
        asynchronous,
        closed: Mutex::new(false),
    })
}

pub fn async_client(args: Args) -> Object {
    client(args, true)
}

pub fn sync_client(args: Args) -> Object {
    client(args, false)
}

struct Client {
    transport: Object,
    base: String,
    headers: Object,
    asynchronous: bool,
    closed: Mutex<bool>,
}

impl Client {
    fn send(&self, request_object: Object) -> Object {
        let handler = match &self.transport {
            Object::None => raise_new("httpx.ConnectError", "no transport in the test double"),
            transport => crate::object::getattr(transport, "handler"),
        };
        let response = await_(call(
            &handler,
            Args::new(vec![request_object.clone()], vec![]),
        ));
        crate::object::setattr(&response, "request", request_object);
        response
    }

    fn request(&self, method: &str, mut args: Args, shift: usize) -> Object {
        let url = text(&args.bind(shift, "url", None));
        let url = if url.contains("://") {
            url
        } else {
            format!("{}{url}", self.base)
        };
        let mut merged = items(&self.headers);
        if let Some(extra) = args.kwarg("headers").filter(|h| !matches!(h, Object::None)) {
            merged.extend(items(&extra));
        }
        args.kw
            .retain(|(k, _)| k != "headers" && k != "url" && k != "method");
        args.kw.push(("headers".into(), dict(merged)));
        args.pos = vec![str_(method), str_(&url)];
        self.send(request(args))
    }

    fn wrap(&self, value: Object) -> Object {
        if self.asynchronous {
            coroutine(move || value)
        } else {
            value
        }
    }
}

struct ClientHandle(Arc<Client>);

impl Native for ClientHandle {
    fn class(&self) -> String {
        if self.0.asynchronous {
            "AsyncClient"
        } else {
            "Client"
        }
        .into()
    }
    fn enter(&self, this: &Object) -> Option<Object> {
        Some(this.clone())
    }
    fn exit(&self, _error: Option<&Raised>) -> bool {
        *lock(&self.0.closed) = true;
        false
    }
    fn awaited(&self, this: &Object) -> Option<Object> {
        Some(this.clone())
    }
    fn getattr(&self, _this: &Object, attribute: &str) -> Option<Object> {
        let client = self.0.clone();
        let method = attribute.to_uppercase();
        Some(match attribute {
            "get" | "post" | "put" | "patch" | "delete" | "head" | "options" => {
                func(attribute, move |args| {
                    client.wrap(client.request(&method, args, 0))
                })
            }
            "request" => func("request", move |args| {
                let method = text(&args.bind(0, "method", None)).to_uppercase();
                client.wrap(client.request(&method, args, 1))
            }),
            "send" => func("send", move |args| {
                let response = client.send(args.bind(0, "request", None));
                client.wrap(response)
            }),
            "aclose" | "close" => func(attribute, move |_| {
                *lock(&client.closed) = true;
                client.wrap(Object::None)
            }),
            "is_closed" => Object::Bool(*lock(&self.0.closed)),
            "headers" => self.0.headers.clone(),
            _ => return None,
        })
    }
}

fn client_handle(client: Client) -> Object {
    native(ClientHandle(Arc::new(client)))
}

// -------------------------------------------------------------------- git

fn git(directory: &str, arguments: &[String]) -> String {
    let mut command = Command::new("git");
    command.arg("-C").arg(directory);
    // A default identity for commits; `git config` reads what the test wrote.
    if arguments.first().is_none_or(|a| a != "config") {
        let has = |key: &str| {
            Command::new("git")
                .args(["-C", directory, "config", "--get", key])
                .output()
                .is_ok_and(|o| o.status.success())
        };
        if !has("user.name") {
            command.args(["-c", "user.name=Vectora Test"]);
        }
        if !has("user.email") {
            command.args(["-c", "user.email=test@example.invalid"]);
        }
    }
    let output = command
        .args([
            "-c",
            "init.defaultBranch=master",
            "-c",
            "commit.gpgsign=false",
        ])
        .args(arguments)
        .output()
        .unwrap_or_else(|e| raise_new("git.exc.GitCommandNotFound", &e.to_string()));
    if !output.status.success() {
        raise_new(
            "git.exc.GitCommandError",
            String::from_utf8_lossy(&output.stderr).trim(),
        );
    }
    String::from_utf8_lossy(&output.stdout)
        .trim_end()
        .to_owned()
}

/// `git.Repo.init(path)` and `git.Repo(path)` over the `git` command.
pub fn repo_init(args: Args) -> Object {
    let path = text(&args.bind(0, "path", None));
    std::fs::create_dir_all(&path).unwrap_or_else(|e| raise_new("OSError", &e.to_string()));
    git(&path, &["init".into(), "-q".into()]);
    repo_object(path)
}

pub fn repo_open(args: Args) -> Object {
    let path = text(&args.bind(0, "path", None));
    let root = git(&path, &["rev-parse".into(), "--show-toplevel".into()]);
    repo_object(root)
}

fn repo_object(path: String) -> Object {
    native(Repo { path })
}

struct Repo {
    path: String,
}

fn commit_object(path: &str, reference: &str) -> Object {
    let sha = git(path, &["rev-parse".into(), reference.into()]);
    let message = git(
        path,
        &["log".into(), "-1".into(), "--format=%B".into(), sha.clone()],
    );
    let author = git(
        path,
        &[
            "log".into(),
            "-1".into(),
            "--format=%an%n%ae".into(),
            sha.clone(),
        ],
    );
    let mut lines = author.lines();
    let author = instance(
        "Actor",
        vec![
            ("name", str_(lines.next().unwrap_or_default())),
            ("email", str_(lines.next().unwrap_or_default())),
        ],
    );
    let summary = message.lines().next().unwrap_or_default().to_owned();
    instance(
        "Commit",
        vec![
            ("hexsha", str_(&sha)),
            ("message", str_(&format!("{message}\n"))),
            ("summary", str_(&summary)),
            ("author", author),
        ],
    )
}

impl Native for Repo {
    fn class(&self) -> String {
        "Repo".into()
    }
    fn getattr(&self, _this: &Object, attribute: &str) -> Option<Object> {
        let path = self.path.clone();
        Some(match attribute {
            "working_dir" | "working_tree_dir" => str_(&self.path),
            "git_dir" => str_(&format!("{}/.git", self.path)),
            "git" => native(GitCommand(path)),
            "index" => {
                let add_path = path.clone();
                let commit_path = path.clone();
                instance(
                    "IndexFile",
                    vec![
                        (
                            "add",
                            func("add", move |args| {
                                let mut arguments = vec!["add".to_owned(), "--".to_owned()];
                                arguments
                                    .extend(iter(&args.bind(0, "items", None)).iter().map(text));
                                git(&add_path, &arguments);
                                Object::None
                            }),
                        ),
                        (
                            "commit",
                            func("commit", move |args| {
                                let message = text(&args.bind(0, "message", None));
                                let mut arguments = vec![
                                    "commit".to_owned(),
                                    "-q".into(),
                                    "--allow-empty".into(),
                                    "-m".into(),
                                    message,
                                ];
                                if let Some(author) =
                                    args.kwarg("author").filter(|a| !matches!(a, Object::None))
                                {
                                    let field = |n: &str| text(&crate::object::getattr(&author, n));
                                    arguments.push(format!(
                                        "--author={} <{}>",
                                        field("name"),
                                        field("email")
                                    ));
                                }
                                git(&commit_path, &arguments);
                                commit_object(&commit_path, "HEAD")
                            }),
                        ),
                    ],
                )
            }
            "head" => {
                let commit = commit_object(&path, "HEAD");
                instance("HEAD", vec![("commit", commit)])
            }
            "active_branch" => {
                let name = git(
                    &path,
                    &["rev-parse".into(), "--abbrev-ref".into(), "HEAD".into()],
                );
                head_object(&path, &name)
            }
            "is_dirty" => func("is_dirty", move |args| {
                let untracked = args
                    .kwarg("untracked_files")
                    .is_some_and(|u| crate::object::truthy(&u));
                let mut arguments = vec!["status".to_owned(), "--porcelain".to_owned()];
                if !untracked {
                    arguments.push("--untracked-files=no".into());
                }
                Object::Bool(!git(&path, &arguments).is_empty())
            }),
            "untracked_files" => {
                let listed = git(
                    &path,
                    &[
                        "ls-files".into(),
                        "--others".into(),
                        "--exclude-standard".into(),
                    ],
                );
                crate::object::list(listed.lines().map(str_).collect())
            }
            "config_writer" | "config_reader" => {
                let config_path = path.clone();
                func(attribute, move |_| native(GitConfig(config_path.clone())))
            }
            "create_head" => func("create_head", move |args| {
                let name = text(&args.bind(0, "path", None));
                let mut arguments = vec!["branch".to_owned(), name.clone()];
                if let Some(commit) = args.arg(1, "commit").filter(|c| !matches!(c, Object::None)) {
                    arguments.push(text(&commit));
                }
                git(&path, &arguments);
                head_object(&path, &name)
            }),
            "heads" | "branches" => {
                let names = git(
                    &path,
                    &["branch".into(), "--format=%(refname:short)".into()],
                );
                crate::object::list(names.lines().map(|n| head_object(&path, n)).collect())
            }
            "iter_commits" => func("iter_commits", move |args| {
                let mut arguments = vec!["rev-list".to_owned()];
                if let Some(n) = args.kwarg("max_count") {
                    arguments.push(format!("--max-count={}", text(&n)));
                }
                arguments.push(args.arg(0, "rev").map_or("HEAD".into(), |r| text(&r)));
                let shas = git(&path, &arguments);
                crate::object::list(shas.lines().map(|sha| commit_object(&path, sha)).collect())
            }),
            "commit" => func("commit", move |args| {
                commit_object(
                    &path,
                    &args.arg(0, "rev").map_or("HEAD".into(), |r| text(&r)),
                )
            }),
            "close" => func("close", |_| Object::None),
            _ => return None,
        })
    }
}

fn head_object(path: &str, name: &str) -> Object {
    let checkout_path = path.to_owned();
    let branch = name.to_owned();
    instance(
        "Head",
        vec![
            ("name", str_(name)),
            ("path", str_(&format!("refs/heads/{name}"))),
            (
                "checkout",
                func("checkout", move |_| {
                    git(
                        &checkout_path,
                        &["checkout".into(), "-q".into(), branch.clone()],
                    );
                    Object::None
                }),
            ),
        ],
    )
}

/// `git.Actor(name, email)`.
pub fn actor(args: Args) -> Object {
    instance(
        "Actor",
        vec![
            ("name", args.bind(0, "name", None)),
            ("email", args.bind(1, "email", None)),
        ],
    )
}

/// `repo.config_writer()`: `set_value(section, option, value)` over `git config`.
struct GitConfig(String);

impl Native for GitConfig {
    fn class(&self) -> String {
        "GitConfigParser".into()
    }
    fn enter(&self, this: &Object) -> Option<Object> {
        Some(this.clone())
    }
    fn getattr(&self, this: &Object, attribute: &str) -> Option<Object> {
        let path = self.0.clone();
        let this = this.clone();
        Some(match attribute {
            "set_value" => func("set_value", move |args| {
                let key = format!(
                    "{}.{}",
                    text(&args.bind(0, "section", None)).replace(' ', "."),
                    text(&args.bind(1, "option", None))
                );
                git(
                    &path,
                    &["config".into(), key, text(&args.bind(2, "value", None))],
                );
                this.clone()
            }),
            "get_value" => func("get_value", move |args| {
                let key = format!(
                    "{}.{}",
                    text(&args.bind(0, "section", None)),
                    text(&args.bind(1, "option", None))
                );
                str_(&git(&path, &["config".into(), "--get".into(), key]))
            }),
            "release" | "write" => func(attribute, |_| Object::None),
            _ => return None,
        })
    }
}

/// `repo.git.<command>(*args)`: runs `git <command> args`.
struct GitCommand(String);

impl Native for GitCommand {
    fn class(&self) -> String {
        "Git".into()
    }
    fn getattr(&self, _this: &Object, attribute: &str) -> Option<Object> {
        let path = self.0.clone();
        if attribute == "execute" {
            return Some(func("execute", move |args| {
                let argv: Vec<String> = iter(&args.bind(0, "command", None))
                    .iter()
                    .map(text)
                    .collect();
                let rest = if argv.first().is_some_and(|a| a == "git") {
                    &argv[1..]
                } else {
                    &argv[..]
                };
                str_(&git(&path, rest))
            }));
        }
        let command = attribute.replace('_', "-");
        Some(func(attribute, move |args| {
            let mut arguments = vec![command.clone()];
            arguments.extend(args.pos.iter().map(text));
            for (key, value) in &args.kw {
                let flag = if key.len() == 1 {
                    format!("-{key}")
                } else {
                    format!("--{}", key.replace('_', "-"))
                };
                match value {
                    Object::Bool(true) => arguments.push(flag),
                    Object::Bool(false) | Object::None => {}
                    other => arguments.push(format!("{flag}={}", text(other))),
                }
            }
            str_(&git(&path, &arguments))
        }))
    }
}

// ------------------------------------------------------- standard modules

pub fn b64encode(args: Args) -> Object {
    const TABLE: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let data = content_of(&args.bind(0, "s", None));
    let mut out = Vec::new();
    for chunk in data.chunks(3) {
        let n = (u32::from(chunk[0]) << 16)
            | (u32::from(*chunk.get(1).unwrap_or(&0)) << 8)
            | u32::from(*chunk.get(2).unwrap_or(&0));
        for i in 0..4 {
            if i <= chunk.len() {
                out.push(TABLE[((n >> (18 - 6 * i)) & 63) as usize]);
            } else {
                out.push(b'=');
            }
        }
    }
    bytes(&out)
}

pub fn b64decode(args: Args) -> Object {
    let data = content_of(&args.bind(0, "s", None));
    let value = |c: u8| -> Option<u32> {
        Some(match c {
            b'A'..=b'Z' => c - b'A',
            b'a'..=b'z' => c - b'a' + 26,
            b'0'..=b'9' => c - b'0' + 52,
            b'+' | b'-' => 62,
            b'/' | b'_' => 63,
            _ => return None,
        } as u32)
    };
    let digits: Vec<u32> = data.iter().filter_map(|c| value(*c)).collect();
    let mut out = Vec::new();
    for chunk in digits.chunks(4) {
        let mut n = 0u32;
        for (i, d) in chunk.iter().enumerate() {
            n |= d << (18 - 6 * i);
        }
        for i in 0..chunk.len().saturating_sub(1) {
            out.push((n >> (16 - 8 * i)) as u8);
        }
    }
    bytes(&out)
}

/// `uuid.uuid4()`: random, printed as the canonical hyphenated text.
pub fn uuid4(_args: Args) -> Object {
    let mut seed = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |d| d.as_nanos()) as u64;
    static COUNTER: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(0);
    seed ^= COUNTER.fetch_add(0x9E37_79B9_7F4A_7C15, std::sync::atomic::Ordering::Relaxed);
    let mut next = || {
        seed ^= seed << 13;
        seed ^= seed >> 7;
        seed ^= seed << 17;
        seed
    };
    let (a, b) = (next(), next());
    let hex = format!("{a:016x}{b:016x}");
    let hex = format!(
        "{}4{}{}{}",
        &hex[..12],
        &hex[13..16],
        "89ab".as_bytes()[(b & 3) as usize] as char,
        &hex[17..]
    );
    let value = format!(
        "{}-{}-{}-{}-{}",
        &hex[0..8],
        &hex[8..12],
        &hex[12..16],
        &hex[16..20],
        &hex[20..32]
    );
    instance(
        "UUID",
        vec![
            ("hex", str_(&value.replace('-', ""))),
            ("__str__", str_(&value)),
        ],
    )
}

/// `logging.getLogger(name)`: records nothing, accepts every level.
pub fn get_logger(args: Args) -> Object {
    let name = args.arg(0, "name").unwrap_or_else(|| str_("root"));
    let ignore = || func("log", |_| Object::None);
    instance(
        "Logger",
        vec![
            ("name", name),
            ("debug", ignore()),
            ("info", ignore()),
            ("warning", ignore()),
            ("error", ignore()),
            ("exception", ignore()),
            ("critical", ignore()),
            ("setLevel", ignore()),
            ("addHandler", ignore()),
            ("removeHandler", ignore()),
            ("handlers", crate::object::list(Vec::new())),
            ("level", Object::Int(0)),
            ("propagate", Object::Bool(true)),
        ],
    )
}

/// `asyncio.Lock()`: one awaitable lock usable with `async with`.
pub fn async_lock(_args: Args) -> Object {
    native(AsyncLock(Arc::new(Mutex::new(false))))
}

struct AsyncLock(Arc<Mutex<bool>>);

impl Native for AsyncLock {
    fn class(&self) -> String {
        "Lock".into()
    }
    fn enter(&self, _this: &Object) -> Option<Object> {
        *lock(&self.0) = true;
        Some(Object::None)
    }
    fn exit(&self, _error: Option<&Raised>) -> bool {
        *lock(&self.0) = false;
        false
    }
    fn getattr(&self, _this: &Object, attribute: &str) -> Option<Object> {
        let state = self.0.clone();
        Some(match attribute {
            "locked" => func("locked", move |_| Object::Bool(*lock(&state))),
            "acquire" => func("acquire", move |_| {
                *lock(&state) = true;
                coroutine(|| Object::Bool(true))
            }),
            "release" => func("release", move |_| {
                *lock(&state) = false;
                Object::None
            }),
            _ => return None,
        })
    }
}

/// `asyncio.Event()` / `threading.Event()`: set, clear, is_set, wait.
pub fn event(_args: Args) -> Object {
    native(Event(Arc::new((
        Mutex::new(false),
        std::sync::Condvar::new(),
    ))))
}

struct Event(Arc<(Mutex<bool>, std::sync::Condvar)>);

impl Native for Event {
    fn class(&self) -> String {
        "Event".into()
    }
    fn getattr(&self, _this: &Object, attribute: &str) -> Option<Object> {
        let state = self.0.clone();
        Some(match attribute {
            "set" => func("set", move |_| {
                *lock(&state.0) = true;
                state.1.notify_all();
                Object::None
            }),
            "clear" => func("clear", move |_| {
                *lock(&state.0) = false;
                Object::None
            }),
            "is_set" => func("is_set", move |_| Object::Bool(*lock(&state.0))),
            "wait" => func("wait", move |args| {
                let timeout = match args.arg(0, "timeout") {
                    Some(Object::Int(n)) => n as f64,
                    Some(Object::Float(f)) => f,
                    _ => 5.0,
                };
                let guard = lock(&state.0);
                let (guard, _) = state
                    .1
                    .wait_timeout_while(
                        guard,
                        std::time::Duration::from_secs_f64(timeout.max(0.0)),
                        |set| !*set,
                    )
                    .unwrap_or_else(|e| e.into_inner());
                let value = Object::Bool(*guard);
                coroutine(move || value)
            }),
            _ => return None,
        })
    }
}

/// `time.perf_counter()`.
pub fn perf_counter(_args: Args) -> Object {
    static START: std::sync::OnceLock<std::time::Instant> = std::sync::OnceLock::new();
    Object::Float(
        START
            .get_or_init(std::time::Instant::now)
            .elapsed()
            .as_secs_f64(),
    )
}

/// `datetime.datetime.now(tz=None)` as an ISO-8601 aware value in UTC.
pub fn datetime_now(_args: Args) -> Object {
    let seconds = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0.0, |d| d.as_secs_f64());
    datetime_from_timestamp(seconds)
}

fn datetime_from_timestamp(seconds: f64) -> Object {
    native(DateTime(seconds))
}

/// Civil date and time of a UTC timestamp (Howard Hinnant's algorithm).
fn civil(seconds: f64) -> [i64; 7] {
    let days = (seconds / 86_400.0).floor() as i64;
    let rest = seconds - days as f64 * 86_400.0;
    let z = days + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z - era * 146_097;
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let day = doy - (153 * mp + 2) / 5 + 1;
    let month = if mp < 10 { mp + 3 } else { mp - 9 };
    let year = yoe + era * 400 + i64::from(month <= 2);
    let micro = ((rest.fract()) * 1e6).round() as i64;
    [
        year,
        month,
        day,
        (rest / 3600.0) as i64,
        ((rest % 3600.0) / 60.0) as i64,
        (rest % 60.0) as i64,
        micro,
    ]
}

/// An aware UTC `datetime`, kept as seconds since the epoch.
struct DateTime(f64);

impl DateTime {
    fn new(seconds: f64) -> Self {
        Self(micro(seconds))
    }

    fn iso(&self, separator: char) -> String {
        let [year, month, day, hour, minute, second, micro] = civil(self.0);
        let fraction = if micro == 0 {
            String::new()
        } else {
            format!(".{micro:06}")
        };
        format!(
            "{year:04}-{month:02}-{day:02}{separator}{hour:02}:{minute:02}:{second:02}{fraction}+00:00"
        )
    }
}

impl Native for DateTime {
    fn class(&self) -> String {
        "datetime".into()
    }
    fn text(&self) -> Option<String> {
        Some(self.iso(' '))
    }
    fn equals(&self, other: &Object) -> Option<bool> {
        Some(
            matches!(other, Object::Native(n) if n.class() == "datetime" && n.order_value() == Some(self.0)),
        )
    }
    fn order_value(&self) -> Option<f64> {
        Some(self.0)
    }
    fn binary(&self, op: &str, other: &Object, reflected: bool) -> Option<Object> {
        let Object::Native(other) = other else {
            return None;
        };
        let value = other.order_value()?;
        match (op, other.class().as_str(), reflected) {
            ("+", "timedelta", _) => Some(native(DateTime::new(self.0 + value))),
            ("-", "timedelta", false) => Some(native(DateTime::new(self.0 - value))),
            ("-", "datetime", false) => Some(native(TimeDelta::new(self.0 - value))),
            ("-", "datetime", true) => Some(native(TimeDelta::new(value - self.0))),
            _ => None,
        }
    }
    fn getattr(&self, _this: &Object, attribute: &str) -> Option<Object> {
        let fields = civil(self.0);
        let seconds = self.0;
        let iso = self.iso('T');
        Some(match attribute {
            "year" => Object::Int(fields[0]),
            "month" => Object::Int(fields[1]),
            "day" => Object::Int(fields[2]),
            "hour" => Object::Int(fields[3]),
            "minute" => Object::Int(fields[4]),
            "second" => Object::Int(fields[5]),
            "microsecond" => Object::Int(fields[6]),
            "tzinfo" => str_("UTC"),
            "isoformat" => func("isoformat", move |_| str_(&iso)),
            "timestamp" => func("timestamp", move |_| Object::Float(seconds)),
            "astimezone" => func("astimezone", move |_| native(DateTime(seconds))),
            "replace" => func("replace", move |args| {
                let mut value = seconds;
                if args
                    .kwarg("microsecond")
                    .is_some_and(|m| matches!(m, Object::Int(0)))
                {
                    value = value.trunc();
                }
                native(DateTime(value))
            }),
            _ => return None,
        })
    }
}

/// `datetime.timedelta`, kept as seconds.
struct TimeDelta(f64);

/// Seconds rounded to the microsecond, Python's resolution.
fn micro(seconds: f64) -> f64 {
    (seconds * 1e6).round() / 1e6
}

impl TimeDelta {
    fn new(seconds: f64) -> Self {
        Self(micro(seconds))
    }
}

impl Native for TimeDelta {
    fn class(&self) -> String {
        "timedelta".into()
    }
    fn equals(&self, other: &Object) -> Option<bool> {
        Some(
            matches!(other, Object::Native(n) if n.class() == "timedelta" && n.order_value() == Some(self.0)),
        )
    }
    fn order_value(&self) -> Option<f64> {
        Some(self.0)
    }
    fn binary(&self, op: &str, other: &Object, reflected: bool) -> Option<Object> {
        if let Object::Native(n) = other
            && n.class() == "timedelta"
        {
            let value = n.order_value()?;
            let (a, b) = if reflected {
                (value, self.0)
            } else {
                (self.0, value)
            };
            return match op {
                "+" => Some(native(TimeDelta::new(a + b))),
                "-" => Some(native(TimeDelta::new(a - b))),
                "/" => Some(Object::Float(a / b)),
                _ => None,
            };
        }
        let factor = match other {
            Object::Int(i) => *i as f64,
            Object::Float(f) => *f,
            _ => return None,
        };
        match (op, reflected) {
            ("*", _) => Some(native(TimeDelta::new(self.0 * factor))),
            ("/", false) => Some(native(TimeDelta::new(self.0 / factor))),
            _ => None,
        }
    }
    fn getattr(&self, _this: &Object, attribute: &str) -> Option<Object> {
        let seconds = self.0;
        Some(match attribute {
            "days" => Object::Int(seconds.div_euclid(86_400.0) as i64),
            "seconds" => Object::Int(seconds.rem_euclid(86_400.0) as i64),
            "microseconds" => Object::Int((seconds.fract().abs() * 1e6).round() as i64),
            "total_seconds" => func("total_seconds", move |_| Object::Float(seconds)),
            _ => return None,
        })
    }
}

/// `datetime.timedelta(days, seconds, microseconds, milliseconds, minutes, hours, weeks)`.
pub fn timedelta(args: Args) -> Object {
    let units = [
        ("days", 86_400.0),
        ("seconds", 1.0),
        ("microseconds", 1e-6),
        ("milliseconds", 1e-3),
        ("minutes", 60.0),
        ("hours", 3600.0),
        ("weeks", 604_800.0),
    ];
    let mut total = 0.0;
    for (index, (unit, scale)) in units.iter().enumerate() {
        if let Some(value) = args.arg(index, unit) {
            total += match value {
                Object::Int(i) => i as f64,
                Object::Float(f) => f,
                other => raise_new(
                    "TypeError",
                    &format!("unsupported type for timedelta {unit}: {}", text(&other)),
                ),
            } * scale;
        }
    }
    native(TimeDelta::new(total))
}

/// `datetime.datetime.fromtimestamp(ts, tz=None)`.
pub fn datetime_fromtimestamp(args: Args) -> Object {
    let seconds = match args.bind(0, "timestamp", None) {
        Object::Int(i) => i as f64,
        Object::Float(f) => f,
        other => raise_new("TypeError", &text(&other)),
    };
    datetime_from_timestamp(seconds)
}

/// `argparse.Namespace(**kw)`.
pub fn namespace(args: Args) -> Object {
    crate::stdlib::simple_namespace(args)
}
