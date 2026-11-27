//! Real operating-system resources for integration tests: `socket`,
//! `threading.Thread` and child processes from `subprocess` and `asyncio`.

use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
use std::os::unix::net::{UnixListener, UnixStream};
use std::process::{Child, Command, Stdio};
use std::sync::{Arc, Mutex, mpsc};
use std::time::Duration;

use crate::object::{
    Args, Native, Object, Raised, bytes, call, coroutine, func, items, iter, lock, native,
    raise_new, require_value, str_, text, tuple,
};

pub const AF_INET: i64 = 2;
pub const AF_UNIX: i64 = 1;

fn os_error(e: std::io::Error) -> ! {
    raise_new("OSError", &e.to_string())
}

enum Endpoint {
    Fresh,
    Tcp(TcpListener),
    Unix(UnixListener),
    TcpStream(TcpStream),
    UnixStream(UnixStream),
    Closed,
}

struct Socket {
    family: i64,
    endpoint: Mutex<Endpoint>,
}

fn socket_object(family: i64, endpoint: Endpoint) -> Object {
    native(SocketHandle(Arc::new(Socket {
        family,
        endpoint: Mutex::new(endpoint),
    })))
}

/// `socket.socket(family=AF_INET, type=SOCK_STREAM)`.
pub fn socket(args: Args) -> Object {
    let family = match args.arg(0, "family") {
        Some(Object::Int(f)) => f,
        _ => AF_INET,
    };
    socket_object(family, Endpoint::Fresh)
}

fn address(value: &Object) -> String {
    match value {
        Object::Tuple(_) | Object::List(_) => {
            let parts = iter(value);
            let host = text(&parts[0]);
            let host = if host.is_empty() {
                "0.0.0.0".into()
            } else {
                host
            };
            format!("{host}:{}", text(&parts[1]))
        }
        other => text(other),
    }
}

impl Socket {
    fn bind(&self, target: &Object) {
        let endpoint = if self.family == AF_UNIX {
            Endpoint::Unix(UnixListener::bind(text(target)).unwrap_or_else(|e| os_error(e)))
        } else {
            Endpoint::Tcp(TcpListener::bind(address(target)).unwrap_or_else(|e| os_error(e)))
        };
        *lock(&self.endpoint) = endpoint;
    }

    fn connect(&self, target: &Object) {
        let endpoint = if self.family == AF_UNIX {
            Endpoint::UnixStream(UnixStream::connect(text(target)).unwrap_or_else(|e| os_error(e)))
        } else {
            Endpoint::TcpStream(TcpStream::connect(address(target)).unwrap_or_else(|e| os_error(e)))
        };
        *lock(&self.endpoint) = endpoint;
    }

    fn name(&self) -> Object {
        match &*lock(&self.endpoint) {
            Endpoint::Tcp(l) => {
                let a = l.local_addr().unwrap_or_else(|e| os_error(e));
                tuple(vec![
                    str_(&a.ip().to_string()),
                    Object::Int(i64::from(a.port())),
                ])
            }
            Endpoint::TcpStream(s) => {
                let a = s.local_addr().unwrap_or_else(|e| os_error(e));
                tuple(vec![
                    str_(&a.ip().to_string()),
                    Object::Int(i64::from(a.port())),
                ])
            }
            _ => str_(""),
        }
    }

    /// Accept without holding the lock, so `close` from another thread works.
    fn accept(&self) -> Object {
        enum Listener {
            Tcp(TcpListener),
            Unix(UnixListener),
        }
        let listener = match &*lock(&self.endpoint) {
            Endpoint::Tcp(l) => Listener::Tcp(l.try_clone().unwrap_or_else(|e| os_error(e))),
            Endpoint::Unix(l) => Listener::Unix(l.try_clone().unwrap_or_else(|e| os_error(e))),
            _ => raise_new("OSError", "socket is not listening"),
        };
        match listener {
            Listener::Tcp(l) => {
                let (stream, peer) = l.accept().unwrap_or_else(|e| os_error(e));
                tuple(vec![
                    socket_object(AF_INET, Endpoint::TcpStream(stream)),
                    tuple(vec![
                        str_(&peer.ip().to_string()),
                        Object::Int(i64::from(peer.port())),
                    ]),
                ])
            }
            Listener::Unix(l) => {
                let (stream, _) = l.accept().unwrap_or_else(|e| os_error(e));
                tuple(vec![
                    socket_object(AF_UNIX, Endpoint::UnixStream(stream)),
                    str_(""),
                ])
            }
        }
    }

    fn recv(&self, size: usize) -> Object {
        let mut buffer = vec![0u8; size];
        let read = match &mut *lock(&self.endpoint) {
            Endpoint::TcpStream(s) => s.read(&mut buffer),
            Endpoint::UnixStream(s) => s.read(&mut buffer),
            _ => raise_new("OSError", "socket is not connected"),
        }
        .unwrap_or_else(|e| os_error(e));
        bytes(&buffer[..read])
    }

    fn send(&self, data: &[u8]) {
        match &mut *lock(&self.endpoint) {
            Endpoint::TcpStream(s) => s.write_all(data),
            Endpoint::UnixStream(s) => s.write_all(data),
            _ => raise_new("OSError", "socket is not connected"),
        }
        .unwrap_or_else(|e| os_error(e));
    }
}

fn data(value: &Object) -> Vec<u8> {
    match value {
        Object::Bytes(b) => b.to_vec(),
        other => text(other).into_bytes(),
    }
}

struct SocketHandle(Arc<Socket>);

impl Native for SocketHandle {
    fn class(&self) -> String {
        "socket".into()
    }
    fn enter(&self, this: &Object) -> Option<Object> {
        Some(this.clone())
    }
    fn exit(&self, _error: Option<&Raised>) -> bool {
        *lock(&self.0.endpoint) = Endpoint::Closed;
        false
    }
    fn getattr(&self, _this: &Object, attribute: &str) -> Option<Object> {
        let socket = self.0.clone();
        Some(match attribute {
            "bind" => func("bind", move |args| {
                socket.bind(&args.bind(0, "address", None));
                Object::None
            }),
            "connect" => func("connect", move |args| {
                socket.connect(&args.bind(0, "address", None));
                Object::None
            }),
            "listen" | "setsockopt" | "settimeout" | "setblocking" | "shutdown" => {
                func(attribute, |_| Object::None)
            }
            "getsockname" => func("getsockname", move |_| socket.name()),
            "accept" => func("accept", move |_| socket.accept()),
            "recv" => func("recv", move |args| {
                let size = match args.bind(0, "bufsize", None) {
                    Object::Int(n) => n.max(0) as usize,
                    _ => 4096,
                };
                socket.recv(size)
            }),
            "send" | "sendall" => func(attribute, move |args| {
                let payload = data(&args.bind(0, "data", None));
                socket.send(&payload);
                Object::None
            }),
            "close" => func("close", move |_| {
                *lock(&socket.endpoint) = Endpoint::Closed;
                Object::None
            }),
            "family" => Object::Int(self.0.family),
            _ => return None,
        })
    }
}

// ---------------------------------------------------------------- threads

struct Thread {
    target: Object,
    args: Vec<Object>,
    done: Mutex<Option<mpsc::Receiver<()>>>,
    finished: Mutex<bool>,
}

/// `threading.Thread(target=..., args=(...), daemon=...)`. A failure in the
/// thread ends it, as an uncaught exception ends a Python thread.
pub fn thread(args: Args) -> Object {
    let target = args.kwarg("target").unwrap_or(Object::None);
    let arguments = args.kwarg("args").map(|a| iter(&a)).unwrap_or_default();
    native(ThreadHandle(Arc::new(Thread {
        target,
        args: arguments,
        done: Mutex::new(None),
        finished: Mutex::new(false),
    })))
}

struct ThreadHandle(Arc<Thread>);

impl Native for ThreadHandle {
    fn class(&self) -> String {
        "Thread".into()
    }
    fn getattr(&self, _this: &Object, attribute: &str) -> Option<Object> {
        let thread = self.0.clone();
        Some(match attribute {
            "start" => func("start", move |_| {
                let (tx, rx) = mpsc::channel();
                *lock(&thread.done) = Some(rx);
                let target = thread.target.clone();
                let args = thread.args.clone();
                std::thread::spawn(move || {
                    let _ = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                        call(&target, Args::new(args, vec![]))
                    }));
                    let _ = tx.send(());
                });
                Object::None
            }),
            "join" => func("join", move |args| {
                let timeout = match args.arg(0, "timeout") {
                    Some(Object::Int(n)) => Some(Duration::from_secs(n.max(0) as u64)),
                    Some(Object::Float(f)) => Some(Duration::from_secs_f64(f.max(0.0))),
                    _ => None,
                };
                if let Some(rx) = lock(&thread.done).as_ref() {
                    let finished = match timeout {
                        Some(t) => rx.recv_timeout(t).is_ok(),
                        None => rx.recv().is_ok(),
                    };
                    if finished {
                        *lock(&thread.finished) = true;
                    }
                }
                Object::None
            }),
            "is_alive" => func("is_alive", move |_| {
                Object::Bool(lock(&thread.done).is_some() && !*lock(&thread.finished))
            }),
            _ => return None,
        })
    }
}

// -------------------------------------------------------------- processes

/// `subprocess.DEVNULL` and `subprocess.PIPE`, as markers.
pub const DEVNULL: i64 = -3;
pub const PIPE: i64 = -1;

fn command(args: &Args, program_and_args: Vec<Object>) -> Command {
    let first = program_and_args
        .first()
        .cloned()
        .unwrap_or_else(|| raise_new("ValueError", "empty command"));
    for part in &program_and_args {
        require_value(part);
    }
    let mut command = Command::new(text(&first));
    command.args(program_and_args[1..].iter().map(text));
    if let Some(env) = args.kwarg("env").filter(|e| !matches!(e, Object::None)) {
        command.env_clear();
        for (key, value) in items(&env) {
            command.env(text(&key), text(&value));
        }
    }
    if let Some(cwd) = args.kwarg("cwd").filter(|c| !matches!(c, Object::None)) {
        command.current_dir(text(&cwd));
    }
    let stream = |name: &str| match args.kwarg(name) {
        Some(Object::Int(PIPE)) => Stdio::piped(),
        Some(Object::Int(DEVNULL)) => Stdio::null(),
        _ if args
            .kwarg("capture_output")
            .is_some_and(|c| matches!(c, Object::Bool(true))) =>
        {
            Stdio::piped()
        }
        _ => Stdio::null(),
    };
    command
        .stdout(stream("stdout"))
        .stderr(stream("stderr"))
        .stdin(Stdio::null());
    command
}

/// `subprocess.run(args, env=, cwd=, check=, timeout=, capture_output=)`.
pub fn run(args: Args) -> Object {
    let argv = iter(&args.bind(0, "args", None));
    let output = command(&args, argv)
        .output()
        .unwrap_or_else(|e| os_error(e));
    let code = i64::from(output.status.code().unwrap_or(-1));
    if code != 0
        && args
            .kwarg("check")
            .is_some_and(|c| matches!(c, Object::Bool(true)))
    {
        raise_new(
            "subprocess.CalledProcessError",
            &format!("Command returned non-zero exit status {code}."),
        );
    }
    crate::stdlib::simple_namespace(Args::new(
        vec![],
        vec![
            ("returncode", Object::Int(code)),
            ("stdout", bytes(&output.stdout)),
            ("stderr", bytes(&output.stderr)),
        ],
    ))
}

struct Process {
    child: Mutex<Child>,
    pid: i64,
    returncode: Mutex<Option<i64>>,
}

impl Process {
    fn poll(&self) -> Object {
        let mut code = lock(&self.returncode);
        if code.is_none()
            && let Ok(Some(status)) = lock(&self.child).try_wait()
        {
            *code = Some(i64::from(status.code().unwrap_or(-1)));
        }
        code.map_or(Object::None, Object::Int)
    }

    fn wait(&self) -> Object {
        let status = lock(&self.child).wait().unwrap_or_else(|e| os_error(e));
        let code = i64::from(status.code().unwrap_or(-1));
        *lock(&self.returncode) = Some(code);
        Object::Int(code)
    }

    fn signal(&self) {
        let _ = lock(&self.child).kill();
    }
}

fn spawn(args: &Args, argv: Vec<Object>, awaitable: bool) -> Object {
    let child = command(args, argv).spawn().unwrap_or_else(|e| os_error(e));
    let pid = i64::from(child.id());
    native(ProcessHandle {
        process: Arc::new(Process {
            child: Mutex::new(child),
            pid,
            returncode: Mutex::new(None),
        }),
        awaitable,
    })
}

/// `subprocess.Popen(args, ...)`.
pub fn popen(args: Args) -> Object {
    let argv = iter(&args.bind(0, "args", None));
    spawn(&args, argv, false)
}

/// `asyncio.create_subprocess_exec(program, *args, ...)`.
pub fn create_subprocess_exec(args: Args) -> Object {
    let argv = args.pos.clone();
    let process = spawn(&args, argv, true);
    coroutine(move || process)
}

struct ProcessHandle {
    process: Arc<Process>,
    awaitable: bool,
}

impl Native for ProcessHandle {
    fn class(&self) -> String {
        if self.awaitable { "Process" } else { "Popen" }.into()
    }
    fn enter(&self, this: &Object) -> Option<Object> {
        Some(this.clone())
    }
    fn exit(&self, _error: Option<&Raised>) -> bool {
        self.process.wait();
        false
    }
    fn getattr(&self, _this: &Object, attribute: &str) -> Option<Object> {
        let process = self.process.clone();
        let awaitable = self.awaitable;
        let wrap = move |value: Object| {
            if awaitable {
                coroutine(move || value)
            } else {
                value
            }
        };
        Some(match attribute {
            "pid" => Object::Int(self.process.pid),
            "returncode" => self.process.poll(),
            "poll" => func("poll", move |_| process.poll()),
            "wait" => func("wait", move |_| wrap(process.wait())),
            "kill" | "terminate" | "send_signal" => func(attribute, move |_| {
                process.signal();
                Object::None
            }),
            _ => return None,
        })
    }
}

// ------------------------------------------------------------------ http

/// `httpx.get(url, timeout=)` over plain HTTP. Without TLS in this crate, an
/// `https` URL fails as a connection error, like an unreachable host.
pub fn http_get(args: Args) -> Object {
    let url = text(&args.bind(0, "url", None));
    let timeout = match args.kwarg("timeout") {
        Some(Object::Int(n)) => Duration::from_secs(n.max(1) as u64),
        Some(Object::Float(f)) => Duration::from_secs_f64(f.max(0.1)),
        _ => Duration::from_secs(5),
    };
    let Some(rest) = url.strip_prefix("http://") else {
        raise_new(
            "httpx.ConnectError",
            &format!("TLS is not available for {url}"),
        )
    };
    let (authority, path) = rest
        .split_once('/')
        .map_or((rest, "/".to_owned()), |(a, p)| (a, format!("/{p}")));
    let authority = if authority.contains(':') {
        authority.to_owned()
    } else {
        format!("{authority}:80")
    };
    let connect = |e: std::io::Error| -> ! { raise_new("httpx.ConnectError", &e.to_string()) };
    let address = std::net::ToSocketAddrs::to_socket_addrs(&authority)
        .unwrap_or_else(|e| connect(e))
        .next()
        .unwrap_or_else(|| raise_new("httpx.ConnectError", "no address"));
    let mut stream = TcpStream::connect_timeout(&address, timeout).unwrap_or_else(|e| connect(e));
    let _ = stream.set_read_timeout(Some(timeout));
    let host = authority.split(':').next().unwrap_or("");
    write!(
        stream,
        "GET {path} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n"
    )
    .unwrap_or_else(|e| connect(e));
    let mut response = Vec::new();
    stream
        .read_to_end(&mut response)
        .unwrap_or_else(|e| raise_new("httpx.ReadError", &e.to_string()));
    let response = String::from_utf8_lossy(&response);
    let (head, body) = response.split_once("\r\n\r\n").unwrap_or((&response, ""));
    let status = head
        .split_whitespace()
        .nth(1)
        .and_then(|s| s.parse::<i64>().ok())
        .unwrap_or_else(|| raise_new("httpx.RemoteProtocolError", "malformed status line"));
    crate::stdlib::simple_namespace(Args::new(
        vec![],
        vec![("status_code", Object::Int(status)), ("text", str_(body))],
    ))
}
