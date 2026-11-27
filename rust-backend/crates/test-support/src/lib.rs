//! Generated from maps/translation by vectora-test-migrator.
#![allow(warnings)]

pub use vectora_runtime;
use vectora_runtime::object::{self, Object, Table};
use vectora_runtime::{async_sqlite, graph, libraries, natives, stdlib, system};
pub use vectora_test_mocks;
use vectora_test_mocks::doubles;

pub static TABLE: Table = Table {
    names: &[
        ("builtins.BaseException", || {
            Object::Class(object::exception_class("builtins.BaseException", &[]))
        }),
        ("builtins.Exception", || {
            Object::Class(object::exception_class(
                "builtins.Exception",
                &["builtins.BaseException"],
            ))
        }),
        ("builtins.ArithmeticError", || {
            Object::Class(object::exception_class(
                "builtins.ArithmeticError",
                &["builtins.Exception", "builtins.BaseException"],
            ))
        }),
        ("builtins.AssertionError", || {
            Object::Class(object::exception_class(
                "builtins.AssertionError",
                &["builtins.Exception", "builtins.BaseException"],
            ))
        }),
        ("builtins.AttributeError", || {
            Object::Class(object::exception_class(
                "builtins.AttributeError",
                &["builtins.Exception", "builtins.BaseException"],
            ))
        }),
        ("builtins.ImportError", || {
            Object::Class(object::exception_class(
                "builtins.ImportError",
                &["builtins.Exception", "builtins.BaseException"],
            ))
        }),
        ("builtins.LookupError", || {
            Object::Class(object::exception_class(
                "builtins.LookupError",
                &["builtins.Exception", "builtins.BaseException"],
            ))
        }),
        ("builtins.NameError", || {
            Object::Class(object::exception_class(
                "builtins.NameError",
                &["builtins.Exception", "builtins.BaseException"],
            ))
        }),
        ("builtins.OSError", || {
            Object::Class(object::exception_class(
                "builtins.OSError",
                &["builtins.Exception", "builtins.BaseException"],
            ))
        }),
        ("builtins.RuntimeError", || {
            Object::Class(object::exception_class(
                "builtins.RuntimeError",
                &["builtins.Exception", "builtins.BaseException"],
            ))
        }),
        ("builtins.StopIteration", || {
            Object::Class(object::exception_class(
                "builtins.StopIteration",
                &["builtins.Exception", "builtins.BaseException"],
            ))
        }),
        ("builtins.StopAsyncIteration", || {
            Object::Class(object::exception_class(
                "builtins.StopAsyncIteration",
                &["builtins.Exception", "builtins.BaseException"],
            ))
        }),
        ("builtins.TypeError", || {
            Object::Class(object::exception_class(
                "builtins.TypeError",
                &["builtins.Exception", "builtins.BaseException"],
            ))
        }),
        ("builtins.ValueError", || {
            Object::Class(object::exception_class(
                "builtins.ValueError",
                &["builtins.Exception", "builtins.BaseException"],
            ))
        }),
        ("builtins.ZeroDivisionError", || {
            Object::Class(object::exception_class(
                "builtins.ZeroDivisionError",
                &[
                    "builtins.ArithmeticError",
                    "builtins.Exception",
                    "builtins.BaseException",
                ],
            ))
        }),
        ("builtins.OverflowError", || {
            Object::Class(object::exception_class(
                "builtins.OverflowError",
                &[
                    "builtins.ArithmeticError",
                    "builtins.Exception",
                    "builtins.BaseException",
                ],
            ))
        }),
        ("builtins.KeyError", || {
            Object::Class(object::exception_class(
                "builtins.KeyError",
                &[
                    "builtins.LookupError",
                    "builtins.Exception",
                    "builtins.BaseException",
                ],
            ))
        }),
        ("builtins.IndexError", || {
            Object::Class(object::exception_class(
                "builtins.IndexError",
                &[
                    "builtins.LookupError",
                    "builtins.Exception",
                    "builtins.BaseException",
                ],
            ))
        }),
        ("builtins.UnboundLocalError", || {
            Object::Class(object::exception_class(
                "builtins.UnboundLocalError",
                &[
                    "builtins.NameError",
                    "builtins.Exception",
                    "builtins.BaseException",
                ],
            ))
        }),
        ("builtins.NotImplementedError", || {
            Object::Class(object::exception_class(
                "builtins.NotImplementedError",
                &[
                    "builtins.RuntimeError",
                    "builtins.Exception",
                    "builtins.BaseException",
                ],
            ))
        }),
        ("builtins.ModuleNotFoundError", || {
            Object::Class(object::exception_class(
                "builtins.ModuleNotFoundError",
                &[
                    "builtins.ImportError",
                    "builtins.Exception",
                    "builtins.BaseException",
                ],
            ))
        }),
        ("builtins.TimeoutError", || {
            Object::Class(object::exception_class(
                "builtins.TimeoutError",
                &[
                    "builtins.OSError",
                    "builtins.Exception",
                    "builtins.BaseException",
                ],
            ))
        }),
        ("builtins.FileNotFoundError", || {
            Object::Class(object::exception_class(
                "builtins.FileNotFoundError",
                &[
                    "builtins.OSError",
                    "builtins.Exception",
                    "builtins.BaseException",
                ],
            ))
        }),
        ("builtins.FileExistsError", || {
            Object::Class(object::exception_class(
                "builtins.FileExistsError",
                &[
                    "builtins.OSError",
                    "builtins.Exception",
                    "builtins.BaseException",
                ],
            ))
        }),
        ("builtins.PermissionError", || {
            Object::Class(object::exception_class(
                "builtins.PermissionError",
                &[
                    "builtins.OSError",
                    "builtins.Exception",
                    "builtins.BaseException",
                ],
            ))
        }),
        ("builtins.ConnectionError", || {
            Object::Class(object::exception_class(
                "builtins.ConnectionError",
                &[
                    "builtins.OSError",
                    "builtins.Exception",
                    "builtins.BaseException",
                ],
            ))
        }),
        ("builtins.ProcessLookupError", || {
            Object::Class(object::exception_class(
                "builtins.ProcessLookupError",
                &[
                    "builtins.OSError",
                    "builtins.Exception",
                    "builtins.BaseException",
                ],
            ))
        }),
        ("builtins.UnicodeDecodeError", || {
            Object::Class(object::exception_class(
                "builtins.UnicodeDecodeError",
                &[
                    "builtins.ValueError",
                    "builtins.Exception",
                    "builtins.BaseException",
                ],
            ))
        }),
        ("builtins.ConnectionAbortedError", || {
            Object::Class(object::exception_class(
                "builtins.ConnectionAbortedError",
                &[
                    "builtins.ConnectionError",
                    "builtins.OSError",
                    "builtins.Exception",
                    "builtins.BaseException",
                ],
            ))
        }),
        ("builtins.ConnectionResetError", || {
            Object::Class(object::exception_class(
                "builtins.ConnectionResetError",
                &[
                    "builtins.ConnectionError",
                    "builtins.OSError",
                    "builtins.Exception",
                    "builtins.BaseException",
                ],
            ))
        }),
        ("builtins.ConnectionRefusedError", || {
            Object::Class(object::exception_class(
                "builtins.ConnectionRefusedError",
                &[
                    "builtins.ConnectionError",
                    "builtins.OSError",
                    "builtins.Exception",
                    "builtins.BaseException",
                ],
            ))
        }),
        ("builtins.SystemExit", || {
            Object::Class(object::exception_class(
                "builtins.SystemExit",
                &["builtins.BaseException"],
            ))
        }),
        ("builtins.KeyboardInterrupt", || {
            Object::Class(object::exception_class(
                "builtins.KeyboardInterrupt",
                &["builtins.BaseException"],
            ))
        }),
        ("asyncio.CancelledError", || {
            Object::Class(object::exception_class(
                "asyncio.CancelledError",
                &["builtins.BaseException"],
            ))
        }),
        ("asyncio.TimeoutError", || {
            Object::Class(object::exception_class(
                "asyncio.TimeoutError",
                &[
                    "builtins.TimeoutError",
                    "builtins.OSError",
                    "builtins.Exception",
                    "builtins.BaseException",
                ],
            ))
        }),
        ("json.JSONDecodeError", || {
            Object::Class(object::exception_class(
                "json.JSONDecodeError",
                &[
                    "builtins.ValueError",
                    "builtins.Exception",
                    "builtins.BaseException",
                ],
            ))
        }),
        ("subprocess.TimeoutExpired", || {
            Object::Class(object::exception_class(
                "subprocess.TimeoutExpired",
                &["builtins.Exception", "builtins.BaseException"],
            ))
        }),
        ("subprocess.CalledProcessError", || {
            Object::Class(object::exception_class(
                "subprocess.CalledProcessError",
                &["builtins.Exception", "builtins.BaseException"],
            ))
        }),
        ("fastapi.HTTPException", || {
            Object::Class(object::exception_class(
                "fastapi.HTTPException",
                &["builtins.Exception", "builtins.BaseException"],
            ))
        }),
        ("starlette.exceptions.HTTPException", || {
            Object::Class(object::exception_class(
                "starlette.exceptions.HTTPException",
                &["builtins.Exception", "builtins.BaseException"],
            ))
        }),
        ("pydantic.ValidationError", || {
            Object::Class(object::exception_class(
                "pydantic.ValidationError",
                &[
                    "builtins.ValueError",
                    "builtins.Exception",
                    "builtins.BaseException",
                ],
            ))
        }),
        ("Failed", || {
            Object::Class(object::exception_class(
                "Failed",
                &["builtins.Exception", "builtins.BaseException"],
            ))
        }),
        ("httpx.HTTPError", || {
            Object::Class(object::exception_class(
                "httpx.HTTPError",
                &["builtins.Exception", "builtins.BaseException"],
            ))
        }),
        ("httpx.TransportError", || {
            Object::Class(object::exception_class(
                "httpx.TransportError",
                &[
                    "httpx.HTTPError",
                    "builtins.Exception",
                    "builtins.BaseException",
                ],
            ))
        }),
        ("httpx.ConnectError", || {
            Object::Class(object::exception_class(
                "httpx.ConnectError",
                &[
                    "httpx.TransportError",
                    "httpx.HTTPError",
                    "builtins.Exception",
                    "builtins.BaseException",
                ],
            ))
        }),
        ("httpx.ReadError", || {
            Object::Class(object::exception_class(
                "httpx.ReadError",
                &[
                    "httpx.TransportError",
                    "httpx.HTTPError",
                    "builtins.Exception",
                    "builtins.BaseException",
                ],
            ))
        }),
        ("httpx.RemoteProtocolError", || {
            Object::Class(object::exception_class(
                "httpx.RemoteProtocolError",
                &[
                    "httpx.TransportError",
                    "httpx.HTTPError",
                    "builtins.Exception",
                    "builtins.BaseException",
                ],
            ))
        }),
        ("httpx.HTTPStatusError", || {
            Object::Class(object::exception_class(
                "httpx.HTTPStatusError",
                &[
                    "httpx.HTTPError",
                    "builtins.Exception",
                    "builtins.BaseException",
                ],
            ))
        }),
        ("git.GitCommandError", || {
            Object::Class(object::exception_class(
                "git.GitCommandError",
                &["builtins.Exception", "builtins.BaseException"],
            ))
        }),
        ("git.exc.GitCommandError", || {
            Object::Class(object::exception_class(
                "git.exc.GitCommandError",
                &[
                    "git.GitCommandError",
                    "builtins.Exception",
                    "builtins.BaseException",
                ],
            ))
        }),
        ("git.GitCommandNotFound", || {
            Object::Class(object::exception_class(
                "git.GitCommandNotFound",
                &["builtins.Exception", "builtins.BaseException"],
            ))
        }),
        ("git.exc.GitCommandNotFound", || {
            Object::Class(object::exception_class(
                "git.exc.GitCommandNotFound",
                &[
                    "git.GitCommandNotFound",
                    "builtins.Exception",
                    "builtins.BaseException",
                ],
            ))
        }),
        ("sqlite3.Error", || {
            Object::Class(object::exception_class(
                "sqlite3.Error",
                &["builtins.Exception", "builtins.BaseException"],
            ))
        }),
        ("sqlite3.DatabaseError", || {
            Object::Class(object::exception_class(
                "sqlite3.DatabaseError",
                &[
                    "sqlite3.Error",
                    "builtins.Exception",
                    "builtins.BaseException",
                ],
            ))
        }),
        ("sqlite3.OperationalError", || {
            Object::Class(object::exception_class(
                "sqlite3.OperationalError",
                &[
                    "sqlite3.DatabaseError",
                    "sqlite3.Error",
                    "builtins.Exception",
                    "builtins.BaseException",
                ],
            ))
        }),
        ("sqlite3.IntegrityError", || {
            Object::Class(object::exception_class(
                "sqlite3.IntegrityError",
                &[
                    "sqlite3.DatabaseError",
                    "sqlite3.Error",
                    "builtins.Exception",
                    "builtins.BaseException",
                ],
            ))
        }),
        ("aiosqlite.Row", || Object::Name("aiosqlite.Row".into())),
        ("aiosqlite.connect", || {
            object::func("aiosqlite.connect", async_sqlite::connect)
        }),
        ("argparse.Namespace", || {
            object::func("argparse.Namespace", libraries::namespace)
        }),
        ("asyncio.Event", || {
            object::func("asyncio.Event", libraries::event)
        }),
        ("asyncio.Lock", || {
            object::func("asyncio.Lock", libraries::async_lock)
        }),
        ("asyncio.create_subprocess_exec", || {
            object::func(
                "asyncio.create_subprocess_exec",
                system::create_subprocess_exec,
            )
        }),
        ("asyncio.create_task", || {
            object::func("asyncio.create_task", natives::asyncio_create_task)
        }),
        ("asyncio.gather", || {
            object::func("asyncio.gather", natives::asyncio_gather)
        }),
        ("asyncio.run", || {
            object::func("asyncio.run", natives::asyncio_run)
        }),
        ("asyncio.sleep", || {
            object::func("asyncio.sleep", natives::asyncio_sleep)
        }),
        ("asyncio.subprocess.DEVNULL", || {
            Object::Int(system::DEVNULL)
        }),
        ("asyncio.subprocess.PIPE", || Object::Int(system::PIPE)),
        ("asyncio.wait_for", || {
            object::func("asyncio.wait_for", natives::asyncio_wait_for)
        }),
        ("base64.b64decode", || {
            object::func("base64.b64decode", libraries::b64decode)
        }),
        ("base64.b64encode", || {
            object::func("base64.b64encode", libraries::b64encode)
        }),
        ("base64.urlsafe_b64decode", || {
            object::func("base64.urlsafe_b64decode", libraries::b64decode)
        }),
        ("builtins.abs", || {
            object::func("builtins.abs", natives::abs)
        }),
        ("builtins.all", || {
            object::func("builtins.all", natives::all)
        }),
        ("builtins.any", || {
            object::func("builtins.any", natives::any)
        }),
        ("builtins.bool", || {
            object::func("builtins.bool", natives::bool_builtin)
        }),
        ("builtins.callable", || {
            object::func("builtins.callable", natives::callable)
        }),
        ("builtins.chr", || object::func("builtins.chr", stdlib::chr)),
        ("builtins.dict", || {
            object::func("builtins.dict", natives::dict_builtin)
        }),
        ("builtins.enumerate", || {
            object::func("builtins.enumerate", natives::enumerate)
        }),
        ("builtins.filter", || {
            object::func("builtins.filter", stdlib::filter)
        }),
        ("builtins.float", || {
            object::func("builtins.float", natives::float_builtin)
        }),
        ("builtins.frozenset", || {
            object::func("builtins.frozenset", natives::set_builtin)
        }),
        ("builtins.getattr", || {
            object::func("builtins.getattr", natives::getattr_builtin)
        }),
        ("builtins.hasattr", || {
            object::func("builtins.hasattr", natives::hasattr)
        }),
        ("builtins.id", || object::func("builtins.id", natives::id)),
        ("builtins.int", || {
            object::func("builtins.int", natives::int_builtin)
        }),
        ("builtins.isinstance", || {
            object::func("builtins.isinstance", natives::isinstance)
        }),
        ("builtins.iter", || {
            object::func("builtins.iter", natives::iter_builtin)
        }),
        ("builtins.len", || {
            object::func("builtins.len", natives::len)
        }),
        ("builtins.list", || {
            object::func("builtins.list", natives::list_builtin)
        }),
        ("builtins.max", || {
            object::func("builtins.max", natives::max)
        }),
        ("builtins.min", || {
            object::func("builtins.min", natives::min)
        }),
        ("builtins.next", || {
            object::func("builtins.next", natives::next)
        }),
        ("builtins.object", || stdlib::object_class()),
        ("builtins.print", || {
            object::func("builtins.print", natives::print)
        }),
        ("builtins.property", || {
            object::func("builtins.property", stdlib::property)
        }),
        ("builtins.range", || {
            object::func("builtins.range", natives::range)
        }),
        ("builtins.repr", || {
            object::func("builtins.repr", natives::repr_builtin)
        }),
        ("builtins.reversed", || {
            object::func("builtins.reversed", natives::reversed)
        }),
        ("builtins.round", || {
            object::func("builtins.round", natives::round)
        }),
        ("builtins.set", || {
            object::func("builtins.set", natives::set_builtin)
        }),
        ("builtins.setattr", || {
            object::func("builtins.setattr", natives::setattr_builtin)
        }),
        ("builtins.sorted", || {
            object::func("builtins.sorted", natives::sorted)
        }),
        ("builtins.str", || {
            object::func("builtins.str", natives::str_builtin)
        }),
        ("builtins.sum", || {
            object::func("builtins.sum", natives::sum)
        }),
        ("builtins.tuple", || {
            object::func("builtins.tuple", natives::tuple_builtin)
        }),
        ("builtins.type", || {
            object::func("builtins.type", natives::type_builtin)
        }),
        ("builtins.zip", || {
            object::func("builtins.zip", natives::zip)
        }),
        ("contextlib.AsyncExitStack", || {
            object::func("contextlib.AsyncExitStack", stdlib::exit_stack)
        }),
        ("contextlib.ExitStack", || {
            object::func("contextlib.ExitStack", stdlib::exit_stack)
        }),
        ("contextlib.asynccontextmanager", || {
            object::func("contextlib.asynccontextmanager", doubles::context_manager)
        }),
        ("contextlib.contextmanager", || {
            object::func("contextlib.contextmanager", doubles::context_manager)
        }),
        ("contextlib.suppress", || {
            object::func("contextlib.suppress", doubles::suppress)
        }),
        ("datetime.datetime.fromtimestamp", || {
            object::func(
                "datetime.datetime.fromtimestamp",
                libraries::datetime_fromtimestamp,
            )
        }),
        ("datetime.datetime.now", || {
            object::func("datetime.datetime.now", libraries::datetime_now)
        }),
        ("datetime.datetime.utcnow", || {
            object::func("datetime.datetime.utcnow", libraries::datetime_now)
        }),
        ("datetime.timedelta", || {
            object::func("datetime.timedelta", libraries::timedelta)
        }),
        ("fastapi.Request", || {
            object::func("fastapi.Request", doubles::starlette_request)
        }),
        ("git.Actor", || object::func("git.Actor", libraries::actor)),
        ("git.Repo", || {
            object::func("git.Repo", libraries::repo_open)
        }),
        ("git.Repo.init", || {
            object::func("git.Repo.init", libraries::repo_init)
        }),
        ("hashlib.sha256", || {
            object::func("hashlib.sha256", stdlib::sha256)
        }),
        ("httpx.AsyncClient", || {
            object::func("httpx.AsyncClient", libraries::async_client)
        }),
        ("httpx.Client", || {
            object::func("httpx.Client", libraries::sync_client)
        }),
        ("httpx.MockTransport", || {
            object::func("httpx.MockTransport", libraries::mock_transport)
        }),
        ("httpx.Request", || {
            object::func("httpx.Request", libraries::request)
        }),
        ("httpx.Response", || {
            object::func("httpx.Response", libraries::response)
        }),
        ("httpx.get", || object::func("httpx.get", system::http_get)),
        ("io.BytesIO", || {
            object::func("io.BytesIO", stdlib::bytes_io)
        }),
        ("json.dumps", || {
            object::func("json.dumps", natives::json_dumps)
        }),
        ("json.loads", || {
            object::func("json.loads", natives::json_loads)
        }),
        ("logging.getLogger", || {
            object::func("logging.getLogger", libraries::get_logger)
        }),
        ("networkx.DiGraph", || {
            object::func("networkx.DiGraph", graph::directed)
        }),
        ("networkx.Graph", || {
            object::func("networkx.Graph", graph::undirected)
        }),
        ("networkx.MultiDiGraph", || {
            object::func("networkx.MultiDiGraph", graph::directed)
        }),
        ("os.environ", || doubles::environ()),
        ("os.getcwd", || {
            object::func("os.getcwd", natives::os_getcwd)
        }),
        ("os.getenv", || {
            object::func("os.getenv", natives::os_getenv)
        }),
        ("os.getpid", || {
            object::func("os.getpid", natives::os_getpid)
        }),
        ("os.name", || object::str_("posix")),
        ("os.path.exists", || {
            object::func("os.path.exists", natives::os_path_exists)
        }),
        ("os.path.join", || {
            object::func("os.path.join", natives::os_path_join)
        }),
        ("os.pathsep", || object::str_(":")),
        ("os.sep", || object::str_("/")),
        ("pandas.DataFrame", || {
            object::func("pandas.DataFrame", doubles::data_frame)
        }),
        ("pathlib.Path", || {
            object::func("pathlib.Path", natives::path_new)
        }),
        ("pathlib.Path.home", || {
            object::func("pathlib.Path.home", natives::path_home)
        }),
        ("pytest.approx", || {
            object::func("pytest.approx", doubles::approx)
        }),
        ("pytest.fail", || object::func("pytest.fail", doubles::fail)),
        ("pytest.importorskip", || {
            object::func("pytest.importorskip", doubles::importorskip)
        }),
        ("pytest.mark.asyncio", || doubles::mark("asyncio")),
        ("pytest.mark.live", || doubles::mark("live")),
        ("pytest.mark.parametrize", || doubles::mark("parametrize")),
        ("pytest.mark.skip", || doubles::mark("skip")),
        ("pytest.mark.skipif", || doubles::mark("skipif")),
        ("pytest.mark.slow", || doubles::mark("slow")),
        ("pytest.mark.timeout", || doubles::mark("timeout")),
        ("pytest.mark.usefixtures", || doubles::mark("usefixtures")),
        ("pytest.mark.xfail", || doubles::mark("xfail")),
        ("pytest.raises", || {
            object::func("pytest.raises", doubles::raises)
        }),
        ("pytest.skip", || object::func("pytest.skip", doubles::skip)),
        ("re.escape", || {
            object::func("re.escape", natives::re_escape)
        }),
        ("re.findall", || {
            object::func("re.findall", natives::re_findall)
        }),
        ("re.match", || object::func("re.match", natives::re_match)),
        ("re.search", || {
            object::func("re.search", natives::re_search)
        }),
        ("shutil.which", || {
            object::func("shutil.which", natives::shutil_which)
        }),
        ("socket.AF_INET", || Object::Int(system::AF_INET)),
        ("socket.AF_UNIX", || Object::Int(system::AF_UNIX)),
        ("socket.SOCK_STREAM", || Object::Int(1)),
        ("socket.SOL_SOCKET", || Object::Int(1)),
        ("socket.SO_REUSEADDR", || Object::Int(2)),
        ("socket.socket", || {
            object::func("socket.socket", system::socket)
        }),
        ("sqlite3.Row", || Object::Name("sqlite3.Row".into())),
        ("sqlite3.connect", || {
            object::func("sqlite3.connect", async_sqlite::connect_sync)
        }),
        ("starlette.requests.Request", || {
            object::func("starlette.requests.Request", doubles::starlette_request)
        }),
        ("stat.S_IEXEC", || Object::Int(0o100)),
        ("stat.S_IREAD", || Object::Int(0o400)),
        ("stat.S_IRGRP", || Object::Int(0o40)),
        ("stat.S_IROTH", || Object::Int(0o4)),
        ("stat.S_IRUSR", || Object::Int(0o400)),
        ("stat.S_IRWXU", || Object::Int(0o700)),
        ("stat.S_IWRITE", || Object::Int(0o200)),
        ("stat.S_IWUSR", || Object::Int(0o200)),
        ("stat.S_IXGRP", || Object::Int(0o10)),
        ("stat.S_IXOTH", || Object::Int(0o1)),
        ("stat.S_IXUSR", || Object::Int(0o100)),
        ("subprocess.DEVNULL", || Object::Int(system::DEVNULL)),
        ("subprocess.PIPE", || Object::Int(system::PIPE)),
        ("subprocess.Popen", || {
            object::func("subprocess.Popen", system::popen)
        }),
        ("subprocess.run", || {
            object::func("subprocess.run", system::run)
        }),
        ("sys.executable", || Object::Name("sys.executable".into())),
        ("sys.modules", || doubles::modules()),
        ("sys.platform", || object::str_("linux")),
        ("sys.stderr", || doubles::stream(true)),
        ("sys.stdout", || doubles::stream(false)),
        ("textwrap.dedent", || {
            object::func("textwrap.dedent", natives::textwrap_dedent)
        }),
        ("threading.Event", || {
            object::func("threading.Event", libraries::event)
        }),
        ("threading.Lock", || {
            object::func("threading.Lock", libraries::async_lock)
        }),
        ("threading.RLock", || {
            object::func("threading.RLock", libraries::async_lock)
        }),
        ("threading.Thread", || {
            object::func("threading.Thread", system::thread)
        }),
        ("time.monotonic", || {
            object::func("time.monotonic", natives::time_monotonic)
        }),
        ("time.perf_counter", || {
            object::func("time.perf_counter", libraries::perf_counter)
        }),
        ("time.sleep", || {
            object::func("time.sleep", natives::time_sleep)
        }),
        ("time.time", || {
            object::func("time.time", natives::time_time)
        }),
        ("types.ModuleType", || {
            object::func("types.ModuleType", doubles::module_type)
        }),
        ("types.SimpleNamespace", || {
            object::func("types.SimpleNamespace", doubles::simple_namespace)
        }),
        ("typing.Any", || Object::Name("typing.Any".into())),
        ("typing.cast", || {
            object::func("typing.cast", natives::identity)
        }),
        ("typing.get_type_hints", || {
            object::func("typing.get_type_hints", natives::get_type_hints)
        }),
        ("unittest.mock.AsyncMock", || {
            object::func("unittest.mock.AsyncMock", doubles::async_mock)
        }),
        ("unittest.mock.MagicMock", || {
            object::func("unittest.mock.MagicMock", doubles::magic_mock)
        }),
        ("unittest.mock.Mock", || {
            object::func("unittest.mock.Mock", doubles::magic_mock)
        }),
        ("unittest.mock.patch", || {
            object::func("unittest.mock.patch", doubles::patch)
        }),
        ("unittest.mock.patch.dict", || {
            object::func("unittest.mock.patch.dict", doubles::patch_dict)
        }),
        ("unittest.mock.patch.object", || {
            object::func("unittest.mock.patch.object", doubles::patch_object)
        }),
        ("uuid.uuid4", || {
            object::func("uuid.uuid4", libraries::uuid4)
        }),
    ],
    methods: &[
        ("Exception.__str__", natives::exception_str),
        ("Path.chmod", natives::path_chmod),
        ("Path.exists", natives::path_exists),
        ("Path.glob", natives::path_glob),
        ("Path.is_dir", natives::path_is_dir),
        ("Path.is_file", natives::path_is_file),
        ("Path.is_symlink", natives::path_is_symlink),
        ("Path.iterdir", natives::path_iterdir),
        ("Path.joinpath", natives::path_joinpath),
        ("Path.mkdir", natives::path_mkdir),
        ("Path.read_bytes", natives::path_read_bytes),
        ("Path.read_text", natives::path_read_text),
        ("Path.rename", natives::path_rename),
        ("Path.resolve", natives::path_resolve),
        ("Path.rglob", natives::path_rglob),
        ("Path.rmdir", natives::path_rmdir),
        ("Path.stat", natives::path_stat),
        ("Path.symlink_to", natives::path_symlink_to),
        ("Path.touch", natives::path_touch),
        ("Path.unlink", natives::path_unlink),
        ("Path.with_name", natives::path_with_name),
        ("Path.with_suffix", natives::path_with_suffix),
        ("Path.write_bytes", natives::path_write_bytes),
        ("Path.write_text", natives::path_write_text),
        ("bytes.decode", natives::bytes_decode),
        ("bytes.startswith", natives::str_startswith),
        ("bytes.strip", natives::bytes_strip),
        ("dict.clear", natives::list_clear),
        ("dict.copy", natives::list_copy),
        ("dict.get", natives::dict_get),
        ("dict.items", natives::dict_items),
        ("dict.keys", natives::dict_keys),
        ("dict.pop", natives::dict_pop),
        ("dict.setdefault", natives::dict_setdefault),
        ("dict.update", natives::dict_update),
        ("dict.values", natives::dict_values),
        ("list.append", natives::list_append),
        ("list.clear", natives::list_clear),
        ("list.copy", natives::list_copy),
        ("list.count", natives::list_count),
        ("list.extend", natives::list_extend),
        ("list.index", natives::list_index),
        ("list.insert", natives::list_insert),
        ("list.pop", natives::list_pop),
        ("list.remove", natives::list_remove),
        ("list.sort", natives::list_sort),
        ("set.add", natives::set_add),
        ("set.copy", natives::list_copy),
        ("set.discard", natives::set_discard),
        ("set.issubset", natives::set_issubset),
        ("set.update", natives::set_update),
        ("str.count", natives::str_count),
        ("str.encode", natives::str_encode),
        ("str.endswith", natives::str_endswith),
        ("str.find", natives::str_find),
        ("str.format", natives::str_format),
        ("str.isdigit", natives::str_isdigit),
        ("str.join", natives::str_join),
        ("str.lower", natives::str_lower),
        ("str.lstrip", natives::str_lstrip),
        ("str.partition", natives::str_partition),
        ("str.removeprefix", natives::str_removeprefix),
        ("str.removesuffix", natives::str_removesuffix),
        ("str.replace", natives::str_replace),
        ("str.rpartition", natives::str_rpartition),
        ("str.rsplit", natives::str_rsplit),
        ("str.rstrip", natives::str_rstrip),
        ("str.split", natives::str_split),
        ("str.splitlines", natives::str_splitlines),
        ("str.startswith", natives::str_startswith),
        ("str.strip", natives::str_strip),
        ("str.upper", natives::str_upper),
        ("tuple.count", natives::list_count),
        ("tuple.index", natives::list_index),
    ],
    properties: &[
        ("Path.name", natives::path_name),
        ("Path.parent", natives::path_parent),
        ("Path.parents", natives::path_parents),
        ("Path.stem", natives::path_stem),
        ("Path.suffix", natives::path_suffix),
    ],
    capabilities: &["backend.", "utils.", "tests.", "sys.executable"],
};
