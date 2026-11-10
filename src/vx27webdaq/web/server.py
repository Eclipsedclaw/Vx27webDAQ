"""The web layer: the page, the JSON interface, the live events, and who may do what.

The server listens on the DAQ computer's localhost only; users reach it through ssh -L port forwarding (or
VS Code's). Every request passes guard(): the address must be a local name (no DNS rebinding), the user is
found from the sign-in cookie (HttpOnly, SameSite=Strict), and a change must come from the page itself (a
custom header, and no foreign Origin). Each action then needs its role: see accounts.py.
"""

from __future__ import annotations

import asyncio
import errno
import functools
import ipaddress
import json
import math
import os
import re
import time
from pathlib import Path
from urllib.parse import urlsplit

from aiohttp import WSMsgType, web

from vx27webdaq import __version__
from vx27webdaq.core.config import ConfigError
from vx27webdaq.core.rawdata import RawFormatError
from vx27webdaq.web.accounts import NAME as ACCOUNT_NAME
from vx27webdaq.web.accounts import RESERVED, SESSION_MAX_S, AccountError, rank
from vx27webdaq.web.boardcheck import CheckError
from vx27webdaq.web.converter import ConvertError
from vx27webdaq.web.files import ALIGNS, MAX_TRACES, Y_UNITS, browse
from vx27webdaq.web.runs import RunError
from vx27webdaq.web.station import Station

STATIC = Path(__file__).parent / "static"
COOKIE = "vx27_session"
LOCAL_NAMES = {"localhost", "127.0.0.1", "::1"}
OPEN_PATHS = {"/api/me", "/api/login"}          # reachable when guests are off and nobody is signed in
LOGIN_FAILS_BEFORE_PAUSE = 5
LOGIN_PAUSE_S = 30
LOGIN_FAIL_MEMORY_S = 3600      # wrong passwords of a name are forgotten after an hour without one
LOGIN_FAIL_NAMES = 1000         # at most this many names are remembered (the oldest go first)
VERIFY_AT_ONCE = 2              # password checks at the same time (each takes about 16 MB and 50 ms)
MAX_CHANNELS_ASKED = 64
MAX_CHANNEL_PARTS = 64          # channels=...: at most this many comma-separated parts
EXPORT_MAX_EVENTS = 1000
MAX_INT_DIGITS = 30             # a number in a request has at most this many digits
SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; "
                               "script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'",
    "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-cache"}

ERRORS = [(PermissionError, 403), (FileNotFoundError, 404), (RunError, 409), (ConvertError, 409), (CheckError, 409),
          (ConfigError, 400), (AccountError, 400), (RawFormatError, 400), (IndexError, 400), (ValueError, 400),
          (IsADirectoryError, 400), (NotADirectoryError, 400), (OSError, 500)]
BAD_PATH = {errno.ENAMETOOLONG, errno.ELOOP}    # a path given that no file can have: the request's fault, not the server's


def error_status(err: Exception) -> int:
    """The HTTP status of an error a handler raised (one of ERRORS)."""
    if isinstance(err, OSError) and err.errno in BAD_PATH:
        return 400
    return next(code for e, code in ERRORS if isinstance(err, e))


# ---- JSON both ways: the browser takes no NaN or Infinity (JSON has no such numbers) ----
def _finite(obj):
    """obj with every NaN and infinite float made None."""
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: _finite(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_finite(v) for v in obj]
    return obj


def dumps(obj) -> str:
    """JSON for the page: a value that is not a finite number (e.g. NaN in a board's statistics, an infinite
    value in an old draft) becomes null, so one bad number can never make the whole answer unreadable."""
    try:
        return json.dumps(obj, allow_nan=False)
    except ValueError:
        return json.dumps(_finite(obj), allow_nan=False)


def json_response(data, status: int = 200) -> web.Response:
    return web.json_response(data, status=status, dumps=dumps)


class BodyError(ValueError):
    """A request body the server does not take (the message says why)."""


def _no_constant(name: str):
    raise BodyError(f"the request has {name}, which is not a number: send numbers only")


def _finite_float(text: str) -> float:
    x = float(text)
    if not math.isfinite(x):
        raise BodyError(f"the request has the number {text[:20]}, which is too large")
    return x


def _small_int(text: str) -> int:
    if len(text.lstrip("-")) > MAX_INT_DIGITS:
        raise BodyError(f"the request has a number with more than {MAX_INT_DIGITS} digits")
    return int(text)


def loads(text: str):
    """JSON of a request, refusing NaN, Infinity, 1e999 and numbers of thousands of digits."""
    return json.loads(text, parse_constant=_no_constant, parse_float=_finite_float, parse_int=_small_int)


def fail(status: int, message: str) -> web.Response:
    return web.json_response({"error": message}, status=status)


def host_name(host: str) -> str:
    if host.startswith("["):
        return host[1:].split("]", 1)[0]
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


def is_loopback(remote: str | None) -> bool:
    """True for an address of this computer itself (127.0.0.0/8, ::1, ::ffff:127.x)."""
    try:
        ip = ipaddress.ip_address((remote or "").split("%", 1)[0])
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_loopback


def identify(st: Station, token: str | None) -> tuple[str | None, str | None]:
    """(account, role) of the browser with this sign-in token: (None, "viewer") for a guest while guests are
    allowed, (None, None) for nobody. A sign-in older than the account's last password change counts for nothing."""
    name = st.sessions.user(token, st.accounts.valid_since)
    role = st.accounts.role(name)
    if role is None:
        name = None
    return name, role or ("viewer" if st.settings.guests else None)


@web.middleware
async def guard(request: web.Request, handler):
    if host_name(request.host or "") not in LOCAL_NAMES:
        return fail(403, "open the page as http://localhost:PORT (through ssh -L)")
    if not is_loopback(request.remote):          # e.g. --host 0.0.0.0: another computer, with "Host: localhost"
        return fail(403, "this server answers only to this computer: open it through ssh -L PORT:localhost:PORT")
    st: Station = request.app["station"]
    token = request.cookies.get(COOKIE)
    name, role = identify(st, token)
    request["user"], request["token"] = name, token if name else None
    request["role"] = role
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("Origin")
        if request.headers.get("X-VX27") != "1" or (origin and urlsplit(origin).netloc != request.host):
            return fail(403, "changes are accepted from the Vx27webDAQ page only")
    if request["role"] is None and request.path.startswith("/api/") and request.path not in OPEN_PATHS:
        return fail(401, "sign in first: this server lets no guests in")
    return await handler(request)


async def security_headers(request, response) -> None:
    for key, value in SECURITY_HEADERS.items():
        response.headers.setdefault(key, value)


def api(level: str = "viewer"):
    """A JSON handler that needs a role (viewer, operator or admin); station errors become JSON errors."""
    def wrap(handler):
        @functools.wraps(handler)
        async def run(request: web.Request):
            if rank(request["role"]) < rank(level):
                if request["user"] is None:
                    return fail(401, f"sign in as {'an' if level[0] in 'aeio' else 'a'} {level} to do this")
                return fail(403, f"this needs the role {level}; you are {request['role']}")
            try:
                result = await handler(request)
            except web.HTTPException:
                raise
            except tuple(e for e, _ in ERRORS) as err:
                return fail(error_status(err), str(err) or type(err).__name__)
            return result if isinstance(result, web.StreamResponse) else json_response(result)
        return run
    return wrap


async def body(request: web.Request) -> dict:
    try:
        data = await request.json(loads=loads)
    except BodyError:
        raise
    except RecursionError:
        raise ValueError("the request is nested too deeply") from None
    except ValueError:
        raise ValueError("the request is not JSON") from None
    if not isinstance(data, dict):
        raise ValueError("the request is not a JSON object")
    return data


def text_field(data: dict, key: str, default: str = "") -> str:
    """data[key] as text: absent or null gives default; any other type is refused."""
    value = data.get(key)
    if value is None:
        return default
    if not isinstance(value, str):
        raise ValueError(f"{key} must be text")
    return value


async def threaded(func, *args) -> web.Response:
    """func(*args) and its JSON in a worker thread: a large answer (every sample of 16 channels, a run's
    combined view) must not hold up the event loop and every other page."""
    def work():
        return dumps(func(*args))
    return web.Response(text=await asyncio.to_thread(work), content_type="application/json")


def me(request: web.Request) -> dict:
    st: Station = request.app["station"]
    r = rank(request["role"])
    return {"user": request["user"], "role": request["role"], "guests": st.settings.guests, "version": __version__,
            "can": {"view": r >= 1, "control": r >= 2, "admin": r >= 3}, "no_accounts": not st.accounts.names()}


def int_query(request: web.Request, key: str, default=None, lo=None, hi=None):
    raw = request.query.get(key)
    if raw in (None, ""):
        return default
    value = int(raw)
    if lo is not None and value < lo or hi is not None and value > hi:
        raise ValueError(f"{key} must be from {lo} to {hi}")
    return value


def span_query(request: web.Request) -> tuple[int, int] | None:
    m0, m1 = int_query(request, "m0", None, 0), int_query(request, "m1", None, 0)
    if m0 is None or m1 is None:
        return None
    if m1 <= m0:
        raise ValueError("m1 must be larger than m0")
    return m0, m1


def channels_query(request: web.Request) -> list[int]:
    """channels=0,1,5-7 -> [0, 1, 5, 6, 7] (at most 64). Every number is checked before a range is filled in,
    so "0-999999999" is refused at once instead of making a list of a billion numbers."""
    text = request.query.get("channels", "0-7")
    parts = [p.strip() for p in text.split(",") if p.strip()]
    if len(parts) > MAX_CHANNEL_PARTS:
        raise ValueError(f"channels: at most {MAX_CHANNEL_PARTS} parts, e.g. 0-7,12")
    out: set[int] = set()
    for part in parts:
        a, dash, b = part.partition("-")
        try:
            lo = int(a)
            hi = int(b) if dash else lo
        except ValueError:
            raise ValueError(f"channels: {part[:20]} is not a channel or a range such as 5-7") from None
        if not (0 <= lo <= 63 and 0 <= hi <= 63):
            raise ValueError("channels are 0-63")
        out.update(range(lo, hi + 1))
    return sorted(out)[:MAX_CHANNELS_ASKED]


TRACE_BOARD = re.compile(r"^[A-Za-z0-9_.-]{1,40}$")


def traces_query(request: web.Request) -> list[tuple[str, int]]:
    """traces=b0:3,vx2740a:12 -> [(\"b0\", 3), (\"vx2740a\", 12)] (at most MAX_TRACES)."""
    out: list[tuple[str, int]] = []
    for part in request.query.get("traces", "").split(","):
        part = part.strip()
        if not part:
            continue
        name, _, ch = part.rpartition(":")
        if not TRACE_BOARD.match(name) or not ch.isdigit() or int(ch) > 63:
            raise ValueError(f"a trace is BOARD:CHANNEL, not {part}")
        if (name, int(ch)) not in out:
            out.append((name, int(ch)))
    if len(out) > MAX_TRACES:
        raise ValueError(f"at most {MAX_TRACES} channels in one plot")
    return out


def float_query(request: web.Request, key: str) -> float | None:
    raw = request.query.get(key)
    if raw in (None, ""):
        return None
    value = float(raw)
    if value != value or abs(value) > 1e12:
        raise ValueError(f"{key} is not a time")
    return value


def choice_query(request: web.Request, key: str, choices: tuple) -> str:
    value = request.query.get(key) or choices[0]
    if value not in choices:
        raise ValueError(f"{key} is one of {', '.join(choices)}")
    return value


# ---- the page ----
async def page(request: web.Request) -> web.Response:
    return web.Response(text=request.app["index"], content_type="text/html")


# ---- accounts ----
async def get_me(request):
    return web.json_response(me(request))


def _forget_old_fails(fails: dict, now: float) -> None:
    """Names whose pause is over and whose last wrong password is an hour old are forgotten; at most
    LOGIN_FAIL_NAMES names are kept (the ones tried longest ago go first)."""
    for key in [k for k, (_, until, last) in fails.items() if now >= until and now - last > LOGIN_FAIL_MEMORY_S]:
        del fails[key]
    if len(fails) > LOGIN_FAIL_NAMES:
        for key in sorted(fails, key=lambda k: fails[k][2])[:len(fails) - LOGIN_FAIL_NAMES]:
            del fails[key]


def _count_try(fails: dict, name: str) -> web.Response | None:
    """Count a password try of this name before it is checked, so tries sent at the same time are all counted:
    the 5th wrong one in a row pauses the name for 30 s. A 429 answer while the pause lasts, else None."""
    now = time.time()
    _forget_old_fails(fails, now)
    count, until, _ = fails.get(name, (0, 0.0, 0.0))
    if now < until:
        return fail(429, f"too many wrong passwords: try again in {int(until - now) + 1} s")
    count += 1
    fails[name] = (count, now + LOGIN_PAUSE_S if count >= LOGIN_FAILS_BEFORE_PAUSE else 0.0, now)
    return None


def set_session_cookie(response: web.Response, token: str) -> web.Response:
    response.set_cookie(COOKIE, token, httponly=True, samesite="Strict", path="/", max_age=SESSION_MAX_S)
    return response


async def login(request: web.Request) -> web.Response:
    st: Station = request.app["station"]
    try:
        data = await body(request)
    except ValueError as err:
        return fail(400, str(err))
    name, password = data.get("name"), data.get("password")
    name = name.strip().lower() if isinstance(name, str) else ""
    password = password if isinstance(password, str) else ""
    if not ACCOUNT_NAME.match(name) or name in RESERVED:     # never an account: no check, no entry in the log
        return fail(401, "wrong user name or password")
    fails = request.app["login_fails"]
    paused = _count_try(fails, name)
    if paused is not None:
        return paused
    async with request.app["verify_slots"]:
        ok = await asyncio.to_thread(st.accounts.verify, name, password)
    if not ok:
        st.activity(None, "failed sign-in", name)
        await asyncio.sleep(0.5)
        return fail(401, "wrong user name or password")
    fails.pop(name, None)
    token = st.sessions.create(name)
    st.accounts.touch_login(name)
    st.activity(name, "signed in")
    return set_session_cookie(json_response({"user": name, "role": st.accounts.role(name)}), token)


async def logout(request: web.Request) -> web.Response:
    st: Station = request.app["station"]
    if request["user"]:
        st.activity(request["user"], "signed out")
    st.sessions.end(request.cookies.get(COOKIE))
    st.recheck_clients()                # this browser's live connection goes on as a guest, or is closed
    response = web.json_response({"ok": True})
    response.del_cookie(COOKIE, path="/")
    return response


def _new_password_signin(st: Station, name: str, old_token: str | None, response: web.Response) -> web.Response:
    """After the password of the account signed in here changed: every other browser of it is signed out, and
    this one gets a new sign-in (the old one is older than the new password, so it no longer counts); this
    browser's open live connections go on with the new one."""
    st.sessions.end_user(name)
    token = st.sessions.create(name)
    st.move_clients(old_token, token)
    return set_session_cookie(response, token)


@api("viewer")
async def change_password(request):
    st: Station = request.app["station"]
    if not request["user"]:
        raise PermissionError("sign in first")
    data = await body(request)
    old, new = text_field(data, "old"), text_field(data, "new")
    name = request["user"]
    paused = _count_try(request.app["password_fails"], name)
    if paused is not None:
        return paused
    async with request.app["verify_slots"]:
        ok = await asyncio.to_thread(st.accounts.verify, name, old)
    if not ok:
        await asyncio.sleep(0.5)
        raise PermissionError("the present password is not right")
    request.app["password_fails"].pop(name, None)
    await asyncio.to_thread(st.accounts.set_password, name, new)
    response = _new_password_signin(st, name, request["token"], json_response({"ok": True}))
    st.activity(name, "changed own password")
    st.recheck_clients()
    return response


@api("admin")
async def list_users(request):
    return {"users": request.app["station"].accounts.listing()}


@api("admin")
async def add_user(request):
    st: Station = request.app["station"]
    data = await body(request)
    role = text_field(data, "role", "viewer")
    name = await asyncio.to_thread(st.accounts.add, text_field(data, "name"), text_field(data, "password"), role)
    st.activity(request["user"], "added an account", f"{name} ({role})")
    st.recheck_clients()
    return {"users": st.accounts.listing()}


@api("admin")
async def change_user(request):
    st: Station = request.app["station"]
    name, data = request.match_info["name"], await body(request)
    if st.accounts.role(name) is None:
        raise FileNotFoundError(f"there is no account {name}")
    role, password = data.get("role"), data.get("password")
    if role is not None and not isinstance(role, str) or password is not None and not isinstance(password, str):
        raise ValueError("role and password must be text")
    response = None
    try:
        if role is not None:
            st.accounts.set_role(name, role)
            st.activity(request["user"], "changed a role", f"{name}: {role}")
        if password:
            await asyncio.to_thread(st.accounts.set_password, name, password)
            st.sessions.end_user(name)
            if name == request["user"]:             # an admin's own: this browser stays signed in
                response = _new_password_signin(st, name, request["token"],
                                                json_response({"users": st.accounts.listing()}))
            st.activity(request["user"], "set a new password", name)
    finally:
        st.recheck_clients()                        # the pages open with this account follow the change
    return response or {"users": st.accounts.listing()}


@api("admin")
async def remove_user(request):
    st: Station = request.app["station"]
    name = request.match_info["name"]
    st.accounts.remove(name)
    st.sessions.end_user(name)
    st.activity(request["user"], "removed an account", name)
    st.recheck_clients()
    return {"users": st.accounts.listing()}


@api("admin")
async def activity(request):
    st: Station = request.app["station"]
    limit = int_query(request, "limit", 200, 1, 500)
    return {"activity": list(st.activity_log)[-limit:][::-1]}


# ---- state and live events ----
@api("viewer")
async def state(request):
    return {"me": me(request), **request.app["station"].state()}


@api("viewer")
async def events(request: web.Request):
    st: Station = request.app["station"]
    origin = request.headers.get("Origin")
    if origin and urlsplit(origin).netloc != request.host:
        raise PermissionError("the live events are for the Vx27webDAQ page only")
    ws = web.WebSocketResponse(heartbeat=25, max_msg_size=1 << 16)
    await ws.prepare(request)
    client = st.add_client(request["user"], request["role"], request.cookies.get(COOKIE))

    async def hello() -> None:
        await ws.send_str(dumps({"type": "hello", "me": me(request), "state": st.state()}))

    async def pump():
        try:
            await hello()
            while True:
                message = await client.queue.get()
                if client.overflow or message.get("type") == st.RECHECK:
                    overflow, client.overflow = client.overflow, False
                    if overflow:                    # too far behind: the whole state anew
                        while not client.queue.empty():
                            client.queue.get_nowait()
                    # who is this browser now (an account removed, its role or password changed, signed out,
                    # guests turned off)? Nobody: the connection ends (4401: the page asks to sign in)
                    user, role = identify(st, client.token)
                    if role is None:
                        await ws.close(code=4401, message=b"sign in")
                        return
                    if overflow or (user, role) != (client.user, client.role):
                        client.user, client.role = user, role
                        request["user"], request["role"] = user, role
                        await hello()
                    continue
                await ws.send_str(dumps(message))
        except (ConnectionResetError, RuntimeError):     # the page went away while a message was sent
            return

    sender = asyncio.create_task(pump())
    try:
        async for msg in ws:
            if msg.type == WSMsgType.ERROR:
                break
    finally:
        sender.cancel()
        st.remove_client(client)
    return ws


# ---- the run ----
@api("operator")
async def run_preview(request):
    return await threaded(request.app["station"].runs.preview)      # it looks at the data folder (the NAS)


@api("operator")
async def run_start(request):
    st: Station = request.app["station"]
    data = await body(request) if request.can_read_body else {}
    note = data.get("note")
    return {"run_dir": st.runs.start(request["user"], note if isinstance(note, str) else None)}


@api("operator")
async def run_stop(request):
    request.app["station"].runs.stop(request["user"])
    return {"ok": True}


@api("operator")
async def run_control(request):
    return await request.app["station"].runs.control(request["user"], await body(request))


@api("operator")
async def boards_apply(request):
    return await request.app["station"].boards.apply(request["user"])


@api("operator")
async def boards_param(request):
    st: Station = request.app["station"]
    data = await body(request)
    return await st.boards.param(request["user"], str(data.get("board", "")), str(data.get("action", "")),
                                 str(data.get("path", "")).strip(), str(data.get("value", "")))


@api("operator")
async def sequence_plan(request):
    labels, _ = request.app["station"].sequence.plan(await body(request))
    return {"labels": labels}


@api("operator")
async def sequence_start(request):
    return request.app["station"].sequence.start(request["user"], await body(request))


@api("operator")
async def sequence_stop(request):
    data = await body(request) if request.can_read_body else {}
    request.app["station"].sequence.stop(request["user"], now=bool(data.get("now")))
    return {"ok": True}


@api("operator")
async def run_kill(request):
    request.app["station"].runs.kill(request["user"])
    return {"ok": True}


# ---- the boards ----
@api("operator")
async def boards_check(request):
    st: Station = request.app["station"]
    plan = st.boards.begin_check(request["user"])        # refused at once when the boards are not free
    task = asyncio.create_task(st.boards.run_check(request["user"], plan))
    request.app["background"].add(task)
    task.add_done_callback(request.app["background"].discard)
    return {"ok": True}


@api("viewer")
async def boards_tree(request):
    st: Station = request.app["station"]
    return await asyncio.to_thread(st.boards.tree, request.query.get("name", ""))


# ---- configuration and settings ----
@api("viewer")
async def get_config(request):
    return request.app["station"].config_payload()


def conflict(st: Station, data: dict) -> web.Response | None:
    """An edit that started from an older version of the configuration than the one the server has now (someone
    else saved an edit meanwhile): 409 with the configuration as it is now, so the page can show it. Without
    "base" an edit is taken as it comes."""
    base = data.get("base")
    if base is None:
        return None
    if isinstance(base, bool) or not isinstance(base, int):
        raise ValueError("base is the version number of the configuration the edit started from")
    if base != st.cfg_version:
        return json_response({"error": "the configuration was changed by someone else meanwhile", "conflict": True,
                              "current": st.config_payload()}, status=409)
    return None


@api("operator")
async def put_note(request):
    st: Station = request.app["station"]
    data = await body(request)
    if not isinstance(data.get("note"), str):
        raise ValueError("note must be text")
    refused = conflict(st, data)
    if refused is not None:
        return refused
    st.set_note(data["note"], request["user"])
    return st.config_payload()


@api("operator")
async def put_config(request):
    st: Station = request.app["station"]
    data = await body(request)
    refused = conflict(st, data)
    if refused is not None:
        return refused
    st.set_config(data.get("config") or {}, request["user"])
    return st.config_payload()


@api("operator")
async def config_save(request):
    st: Station = request.app["station"]
    data = await body(request)
    path = text_field(data, "path").strip()
    return {"path": st.save_config(path or None, request["user"]), **st.config_payload()}


@api("operator")
async def config_open(request):
    st: Station = request.app["station"]
    data = await body(request)
    path = text_field(data, "path").strip()
    if not path:
        raise ValueError("give the path of the configuration file to open")
    st.open_config(path, request["user"])
    return st.config_payload()


@api("operator")
async def config_reload(request):
    st: Station = request.app["station"]
    if not st.cfg_path:
        raise ConfigError("the configuration was not read from a file")
    st.open_config(st.cfg_path, request["user"])
    return st.config_payload()


@api("operator")
async def config_new(request):
    st: Station = request.app["station"]
    st.new_config(request["user"])
    return st.config_payload()


@api("viewer")
async def get_settings(request):
    return request.app["station"].settings_payload()


@api("operator")
async def put_settings(request):
    st: Station = request.app["station"]
    data = await body(request)
    changes = data.get("changes")
    if changes is None:
        changes = {}
    if not isinstance(changes, dict):
        raise ValueError("changes must be an object: {setting: new value}")
    st.update_settings(changes, request["user"], rank(request["role"]) >= 3)
    return st.settings_payload()


@api("operator")
async def browse_folder(request):
    st: Station = request.app["station"]
    places = st.browse_places()
    start = request.query.get("dir") or st.default_folder() or (os.path.dirname(os.path.abspath(st.cfg_path)) if st.cfg_path else "~")
    listing = await asyncio.to_thread(browse, start, request.query.get("hidden") == "1")
    return {"places": [{"label": a, "path": b} for a, b in places], **listing}


# ---- data files and the live view ----
@api("viewer")
async def files_list(request):
    st: Station = request.app["station"]
    roots = st.data_roots()
    folder = request.query.get("dir") or st.default_folder() or (roots[0][1] if roots else "")
    if not folder:
        raise FileNotFoundError("there is no data folder yet: an operator adds one in the Settings (data folders)")
    return {"roots": [{"label": a, "path": b} for a, b in roots], **await asyncio.to_thread(st.files.listing, folder)}


@api("viewer")
async def files_info(request):
    st: Station = request.app["station"]
    return await threaded(st.files.info, request.query.get("path", ""))


@api("viewer")
async def files_waveforms(request):
    st: Station = request.app["station"]
    return await threaded(st.files.waveforms, request.query.get("path", ""), int_query(request, "event", 0, 0),
                          channels_query(request), int_query(request, "x0", 0, 0),
                          int_query(request, "x1", None, 0), int_query(request, "bins", 800, 16, 4000),
                          span_query(request), request.query.get("full") == "1")


@api("viewer")
async def files_close(request):
    """Close file (the Run page): the server closes the data files the page opened (at most 64 paths)."""
    st: Station = request.app["station"]
    paths = (await body(request)).get("paths")
    if not isinstance(paths, list) or len(paths) > 64 or not all(isinstance(p, str) and p for p in paths):
        raise ValueError("paths must be a list of at most 64 file paths")
    return {"closed": await asyncio.to_thread(st.files.forget, paths)}     # realpath may wait for the NAS


@api("viewer")
async def files_overview(request):
    st: Station = request.app["station"]
    return await threaded(st.files.overview, request.query.get("path", ""))


@api("viewer")
async def live_waveforms(request):
    st: Station = request.app["station"]
    # in a worker thread, with its JSON: with full=1 sixteen channels of 64 k samples are some 25 MB of JSON
    return await threaded(st.runs.live_waveforms, request.query.get("board", ""), channels_query(request),
                          int_query(request, "x0", 0, 0), int_query(request, "x1", None, 0),
                          int_query(request, "bins", 800, 16, 4000), int_query(request, "since", None),
                          span_query(request), request.query.get("full") == "1")


@api("viewer")
async def live_combined(request):
    st: Station = request.app["station"]
    return await threaded(st.runs.live_combined, traces_query(request), float_query(request, "t0"),
                          float_query(request, "t1"), int_query(request, "bins", 800, 16, 4000),
                          choice_query(request, "y", Y_UNITS), choice_query(request, "align", ALIGNS),
                          int_query(request, "since", None))


@api("viewer")
async def files_combined(request):
    st: Station = request.app["station"]
    return await threaded(st.files.combined, request.query.get("path", ""), int_query(request, "event", 0, 0),
                          traces_query(request), float_query(request, "t0"), float_query(request, "t1"),
                          int_query(request, "bins", 800, 16, 4000), choice_query(request, "y", Y_UNITS),
                          choice_query(request, "align", ALIGNS))


@api("viewer")
async def live_hk(request):
    st: Station = request.app["station"]
    return await threaded(st.runs.live_hk)


@api("viewer")
async def files_hk(request):
    st: Station = request.app["station"]
    return await threaded(st.files.hk, request.query.get("path", ""))


@api("viewer")
async def live_boards(request):
    return request.app["station"].runs.live_boards()


@api("viewer")
async def live_overview(request):
    return await threaded(request.app["station"].runs.live_overview)


# ---- ROOT ----
def user_folder(st: Station, who: str) -> str:
    return os.path.join(st.settings.runs_folder(), "users", who.replace("/", "_"), "convert")


@api("viewer")
async def root_convert(request):
    st: Station = request.app["station"]
    data = await body(request)
    path, who = text_field(data, "path"), request["user"] or "guest"
    workdir = user_folder(st, who)

    def files():                        # in a worker thread: the files may be on the NAS
        raw = st.files.check(path)
        st.converter.check_request(raw)
        os.makedirs(workdir, exist_ok=True)
        return raw
    raw = await asyncio.to_thread(files)
    st.converter.request(raw, workdir, who)
    st.activity(request["user"], "asked for a ROOT conversion", raw)
    return st.converter.status()


@api("viewer")
async def root_build(request):
    st: Station = request.app["station"]
    data = await body(request)
    path, who = text_field(data, "path"), request["user"] or "guest"
    workdir = user_folder(st, who)

    def files():
        raw = st.files.check(path)
        out = st.converter.check_build(raw)         # RUN_built.root: only a run's name from the header ...
        st.files.check(out, want="new")             # ... and inside a data folder
        os.makedirs(workdir, exist_ok=True)
        return raw, out
    raw, out = await asyncio.to_thread(files)
    st.converter.request_build(raw, workdir, who)
    st.activity(request["user"], "asked for the run's events in one ROOT file", out)
    return {**st.converter.status(), "out": out}


@api("viewer")
async def files_export(request):
    """Events of a data file as a text file, sent while it is made (one event at a time, in a worker thread), one
    export at a time on the server."""
    st: Station = request.app["station"]
    path = request.query.get("path", "")
    first, count = int_query(request, "first", 0, 0), int_query(request, "count", 1, 1, EXPORT_MAX_EVENTS)
    channels = channels_query(request)
    lock: asyncio.Lock = request.app["export_lock"]
    if lock.locked():
        return fail(409, "another text export is being made: try again when it is done")
    async with lock:
        export = await asyncio.to_thread(st.files.export_open, path, first, count, channels)
        try:
            response = web.StreamResponse(headers={"Content-Type": "text/plain; charset=utf-8",
                                                   "Content-Disposition": f'attachment; filename="{export.name}"'})
            await response.prepare(request)
            chunks = export.chunks()
            try:
                while True:
                    text = await asyncio.to_thread(next, chunks, None)
                    if text is None:
                        break
                    await response.write(text.encode("utf-8"))
                await response.write_eof()
            except (ConnectionResetError, RawFormatError, IndexError, OSError) as err:   # sent in part: end it there
                try:
                    await response.write(f"\n# the export ended here: {err}\n".encode("utf-8"))
                except (ConnectionResetError, RuntimeError, OSError):
                    pass
            return response
        finally:
            export.close()


@api("operator")
async def root_cancel(request):
    st: Station = request.app["station"]
    st.converter.cancel()
    st.activity(request["user"], "stopped the ROOT conversion")
    return st.converter.status()


# ---- the application ----
def create_app(station: Station | None = None) -> web.Application:
    app = web.Application(middlewares=[guard], client_max_size=4 << 20)
    app["station"] = station or Station()
    app["login_fails"] = {}             # name -> (wrong tries in a row, paused until, last try)
    app["password_fails"] = {}          # the same for "change your password" (the present one asked)
    app["verify_slots"] = asyncio.Semaphore(VERIFY_AT_ONCE)
    app["export_lock"] = asyncio.Lock()         # one text export at a time
    app["background"] = set()
    app["index"] = (STATIC / "index.html").read_text(encoding="utf-8").replace("{{version}}", __version__)
    app.on_response_prepare.append(security_headers)
    r = app.router
    r.add_get("/", page)
    r.add_static("/static/", STATIC, append_version=False)
    r.add_get("/api/me", get_me)
    r.add_post("/api/login", login)
    r.add_post("/api/logout", logout)
    r.add_post("/api/password", change_password)
    r.add_get("/api/users", list_users)
    r.add_post("/api/users", add_user)
    r.add_put("/api/users/{name}", change_user)
    r.add_delete("/api/users/{name}", remove_user)
    r.add_get("/api/activity", activity)
    r.add_get("/api/state", state)
    r.add_get("/api/events", events)
    r.add_get("/api/run/preview", run_preview)
    r.add_post("/api/run/start", run_start)
    r.add_post("/api/run/stop", run_stop)
    r.add_post("/api/run/kill", run_kill)
    r.add_post("/api/run/control", run_control)
    r.add_post("/api/sequence/plan", sequence_plan)
    r.add_post("/api/sequence/start", sequence_start)
    r.add_post("/api/sequence/stop", sequence_stop)
    r.add_post("/api/boards/param", boards_param)
    r.add_post("/api/boards/apply", boards_apply)
    r.add_post("/api/boards/check", boards_check)
    r.add_get("/api/boards/tree", boards_tree)
    r.add_get("/api/config", get_config)
    r.add_put("/api/config", put_config)
    r.add_put("/api/note", put_note)
    r.add_post("/api/config/save", config_save)
    r.add_post("/api/config/open", config_open)
    r.add_post("/api/config/reload", config_reload)
    r.add_post("/api/config/new", config_new)
    r.add_get("/api/settings", get_settings)
    r.add_put("/api/settings", put_settings)
    r.add_get("/api/browse", browse_folder)
    r.add_get("/api/files/list", files_list)
    r.add_get("/api/files/info", files_info)
    r.add_get("/api/files/waveforms", files_waveforms)
    r.add_get("/api/files/overview", files_overview)
    r.add_post("/api/files/close", files_close)
    r.add_get("/api/live/waveforms", live_waveforms)
    r.add_get("/api/live/boards", live_boards)
    r.add_get("/api/live/overview", live_overview)
    r.add_get("/api/live/combined", live_combined)
    r.add_get("/api/files/combined", files_combined)
    r.add_get("/api/live/hk", live_hk)
    r.add_get("/api/files/hk", files_hk)
    r.add_post("/api/root/convert", root_convert)
    r.add_post("/api/root/cancel", root_cancel)
    r.add_post("/api/root/build", root_build)
    r.add_get("/api/files/export", files_export)

    async def startup(app):
        await app["station"].start()

    async def cleanup(app):
        for task in app["background"]:
            task.cancel()
        await app["station"].stop()

    app.on_startup.append(startup)
    app.on_cleanup.append(cleanup)
    return app
