"""The web layer: the page, the JSON interface, the live events, and who may do what.

The server listens on the DAQ computer's localhost only; users reach it through ssh -L port forwarding (or
VS Code's). Every request passes guard(): the address must be a local name (no DNS rebinding), the user is
found from the sign-in cookie (HttpOnly, SameSite=Strict), and a change must come from the page itself (a
custom header, and no foreign Origin). Each action then needs its role: see accounts.py.
"""

from __future__ import annotations

import asyncio
import functools
import os
import re
import time
from pathlib import Path
from urllib.parse import urlsplit

from aiohttp import WSMsgType, web

from alab_daq import __version__
from alab_daq.core.config import ConfigError
from alab_daq.core.rawdata import RawFormatError
from alab_daq.web.accounts import SESSION_MAX_S, AccountError, rank
from alab_daq.web.boardcheck import CheckError
from alab_daq.web.converter import ConvertError
from alab_daq.web.files import ALIGNS, MAX_TRACES, Y_UNITS, browse
from alab_daq.web.runs import RunError
from alab_daq.web.station import Station

STATIC = Path(__file__).parent / "static"
COOKIE = "alab_session"
LOCAL_NAMES = {"localhost", "127.0.0.1", "::1"}
OPEN_PATHS = {"/api/me", "/api/login"}          # reachable when guests are off and nobody is signed in
LOGIN_FAILS_BEFORE_PAUSE = 5
LOGIN_PAUSE_S = 30
MAX_CHANNELS_ASKED = 64
EXPORT_MAX_EVENTS = 1000
SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; "
                               "script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'",
    "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-cache"}

ERRORS = [(PermissionError, 403), (FileNotFoundError, 404), (RunError, 409), (ConvertError, 409), (CheckError, 409),
          (ConfigError, 400), (AccountError, 400), (RawFormatError, 400), (IndexError, 400), (ValueError, 400),
          (OSError, 500)]


def fail(status: int, message: str) -> web.Response:
    return web.json_response({"error": message}, status=status)


def host_name(host: str) -> str:
    if host.startswith("["):
        return host[1:].split("]", 1)[0]
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


@web.middleware
async def guard(request: web.Request, handler):
    if host_name(request.host or "") not in LOCAL_NAMES:
        return fail(403, "open the page as http://localhost:PORT (through ssh -L)")
    st: Station = request.app["station"]
    token = request.cookies.get(COOKIE)
    name = st.sessions.user(token)
    role = st.accounts.role(name)
    if role is None:
        name = None
    request["user"], request["token"] = name, token if name else None
    request["role"] = role or ("viewer" if st.settings.guests else None)
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("Origin")
        if request.headers.get("X-ALAB") != "1" or (origin and urlsplit(origin).netloc != request.host):
            return fail(403, "changes are accepted from the ALAB DAQ page only")
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
                status = next(code for e, code in ERRORS if isinstance(err, e))
                return fail(status, str(err) or type(err).__name__)
            return result if isinstance(result, web.StreamResponse) else web.json_response(result)
        return run
    return wrap


async def body(request: web.Request) -> dict:
    try:
        data = await request.json()
    except ValueError:
        raise ValueError("the request is not JSON")
    if not isinstance(data, dict):
        raise ValueError("the request is not a JSON object")
    return data


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
    """channels=0,1,5-7 -> [0, 1, 5, 6, 7] (at most 64)."""
    text = request.query.get("channels", "0-7")
    out: list[int] = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            out += list(range(int(a), int(b) + 1))
        else:
            out.append(int(part))
    if any(c < 0 or c > 63 for c in out):
        raise ValueError("channels are 0-63")
    return sorted(set(out))[:MAX_CHANNELS_ASKED]


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


async def login(request: web.Request) -> web.Response:
    st: Station = request.app["station"]
    try:
        data = await body(request)
    except ValueError as err:
        return fail(400, str(err))
    name = str(data.get("name", "")).strip().lower()
    fails = request.app["login_fails"]
    count, until = fails.get(name, (0, 0.0))
    if time.time() < until:
        return fail(429, f"too many wrong passwords: try again in {int(until - time.time()) + 1} s")
    ok = await asyncio.to_thread(st.accounts.verify, name, str(data.get("password", "")))
    if not ok:
        count += 1
        fails[name] = (count, time.time() + LOGIN_PAUSE_S if count >= LOGIN_FAILS_BEFORE_PAUSE else 0.0)
        st.activity(None, "failed sign-in", name)
        await asyncio.sleep(0.5)
        return fail(401, "wrong user name or password")
    fails.pop(name, None)
    token = st.sessions.create(name)
    st.accounts.touch_login(name)
    st.activity(name, "signed in")
    response = web.json_response({"user": name, "role": st.accounts.role(name)})
    response.set_cookie(COOKIE, token, httponly=True, samesite="Strict", path="/", max_age=SESSION_MAX_S)
    return response


async def logout(request: web.Request) -> web.Response:
    st: Station = request.app["station"]
    if request["user"]:
        st.activity(request["user"], "signed out")
    st.sessions.end(request.cookies.get(COOKIE))
    response = web.json_response({"ok": True})
    response.del_cookie(COOKIE, path="/")
    return response


@api("viewer")
async def change_password(request):
    st: Station = request.app["station"]
    if not request["user"]:
        raise PermissionError("sign in first")
    data = await body(request)
    if not await asyncio.to_thread(st.accounts.verify, request["user"], str(data.get("old", ""))):
        raise PermissionError("the present password is not right")
    await asyncio.to_thread(st.accounts.set_password, request["user"], str(data.get("new", "")))
    st.sessions.end_user(request["user"], keep=request["token"])
    st.activity(request["user"], "changed own password")
    return {"ok": True}


@api("admin")
async def list_users(request):
    return {"users": request.app["station"].accounts.listing()}


@api("admin")
async def add_user(request):
    st: Station = request.app["station"]
    data = await body(request)
    name = await asyncio.to_thread(st.accounts.add, str(data.get("name", "")), str(data.get("password", "")),
                                   str(data.get("role", "viewer")))
    st.activity(request["user"], "added an account", f"{name} ({data.get('role', 'viewer')})")
    return {"users": st.accounts.listing()}


@api("admin")
async def change_user(request):
    st: Station = request.app["station"]
    name, data = request.match_info["name"], await body(request)
    if st.accounts.role(name) is None:
        raise FileNotFoundError(f"there is no account {name}")
    if "role" in data:
        st.accounts.set_role(name, str(data["role"]))
        st.activity(request["user"], "changed a role", f"{name}: {data['role']}")
    if data.get("password"):
        await asyncio.to_thread(st.accounts.set_password, name, str(data["password"]))
        st.sessions.end_user(name, keep=request["token"] if name == request["user"] else None)
        st.activity(request["user"], "set a new password", name)
    return {"users": st.accounts.listing()}


@api("admin")
async def remove_user(request):
    st: Station = request.app["station"]
    name = request.match_info["name"]
    st.accounts.remove(name)
    st.sessions.end_user(name)
    st.activity(request["user"], "removed an account", name)
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
        raise PermissionError("the live events are for the ALAB DAQ page only")
    ws = web.WebSocketResponse(heartbeat=25, max_msg_size=1 << 16)
    await ws.prepare(request)
    client = st.add_client(request["user"], request["role"])

    async def pump():
        await ws.send_json({"type": "hello", "me": me(request), "state": st.state()})
        while True:
            message = await client.queue.get()
            if client.overflow:
                client.overflow = False
                while not client.queue.empty():
                    client.queue.get_nowait()
                await ws.send_json({"type": "hello", "me": me(request), "state": st.state()})
                continue
            await ws.send_json(message)

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
    return request.app["station"].runs.preview()


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
async def boards_param(request):
    st: Station = request.app["station"]
    data = await body(request)
    return await st.boards.param(request["user"], str(data.get("board", "")), str(data.get("action", "")),
                                 str(data.get("path", "")).strip(), str(data.get("value", "")))


@api("operator")
async def run_kill(request):
    request.app["station"].runs.kill(request["user"])
    return {"ok": True}


# ---- the boards ----
@api("operator")
async def boards_check(request):
    st: Station = request.app["station"]
    if st.boards.running:
        raise CheckError("a check is going already")
    if st.runs.active is not None:
        raise CheckError("not during a run: the run's own table shows the boards")
    request.app["background"].add(asyncio.create_task(st.boards.check(request["user"])))
    return {"ok": True}


@api("viewer")
async def boards_tree(request):
    st: Station = request.app["station"]
    return await asyncio.to_thread(st.boards.tree, request.query.get("name", ""))


# ---- configuration and settings ----
@api("viewer")
async def get_config(request):
    return request.app["station"].config_payload()


@api("operator")
async def put_note(request):
    st: Station = request.app["station"]
    data = await body(request)
    if not isinstance(data.get("note"), str):
        raise ValueError("note must be text")
    st.set_note(data["note"], request["user"])
    return st.config_payload()


@api("operator")
async def put_config(request):
    st: Station = request.app["station"]
    data = await body(request)
    st.set_config(data.get("config") or {}, request["user"])
    return st.config_payload()


@api("operator")
async def config_save(request):
    st: Station = request.app["station"]
    data = await body(request)
    return {"path": st.save_config(data.get("path") or None, request["user"]), **st.config_payload()}


@api("operator")
async def config_open(request):
    st: Station = request.app["station"]
    data = await body(request)
    st.open_config(str(data.get("path", "")), request["user"])
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
    st.update_settings(data.get("changes") or {}, request["user"], rank(request["role"]) >= 3)
    return st.settings_payload()


@api("operator")
async def browse_folder(request):
    st: Station = request.app["station"]
    places = st.browse_places()
    start = request.query.get("dir") or (os.path.dirname(os.path.abspath(st.cfg_path)) if st.cfg_path else "~")
    listing = await asyncio.to_thread(browse, start, request.query.get("hidden") == "1")
    return {"places": [{"label": a, "path": b} for a, b in places], **listing}


# ---- data files and the live view ----
@api("viewer")
async def files_list(request):
    st: Station = request.app["station"]
    folder = request.query.get("dir") or (st.data_roots()[0][1] if st.data_roots() else "")
    return {"roots": [{"label": a, "path": b} for a, b in st.data_roots()],
            **await asyncio.to_thread(st.files.listing, folder)}


@api("viewer")
async def files_info(request):
    st: Station = request.app["station"]
    return await asyncio.to_thread(st.files.info, request.query.get("path", ""))


@api("viewer")
async def files_waveforms(request):
    st: Station = request.app["station"]
    return await asyncio.to_thread(st.files.waveforms, request.query.get("path", ""), int_query(request, "event", 0, 0),
                                   channels_query(request), int_query(request, "x0", 0, 0),
                                   int_query(request, "x1", None, 0), int_query(request, "bins", 800, 16, 4000),
                                   span_query(request), request.query.get("full") == "1")


@api("viewer")
async def files_overview(request):
    st: Station = request.app["station"]
    return await asyncio.to_thread(st.files.overview, request.query.get("path", ""))


@api("viewer")
async def live_waveforms(request):
    st: Station = request.app["station"]
    return st.runs.live_waveforms(request.query.get("board", ""), channels_query(request), int_query(request, "x0", 0, 0),
                                  int_query(request, "x1", None, 0), int_query(request, "bins", 800, 16, 4000),
                                  int_query(request, "since", None), span_query(request), request.query.get("full") == "1")


@api("viewer")
async def live_combined(request):
    st: Station = request.app["station"]
    return await asyncio.to_thread(st.runs.live_combined, traces_query(request), float_query(request, "t0"),
                                   float_query(request, "t1"), int_query(request, "bins", 800, 16, 4000),
                                   choice_query(request, "y", Y_UNITS), choice_query(request, "align", ALIGNS),
                                   int_query(request, "since", None))


@api("viewer")
async def files_combined(request):
    st: Station = request.app["station"]
    return await asyncio.to_thread(st.files.combined, request.query.get("path", ""), int_query(request, "event", 0, 0),
                                   traces_query(request), float_query(request, "t0"), float_query(request, "t1"),
                                   int_query(request, "bins", 800, 16, 4000), choice_query(request, "y", Y_UNITS),
                                   choice_query(request, "align", ALIGNS))


@api("viewer")
async def live_hk(request):
    st: Station = request.app["station"]
    return await asyncio.to_thread(st.runs.live_hk)


@api("viewer")
async def files_hk(request):
    st: Station = request.app["station"]
    return await asyncio.to_thread(st.files.hk, request.query.get("path", ""))


@api("viewer")
async def live_boards(request):
    return {"boards": request.app["station"].runs.live_boards()}


@api("viewer")
async def live_overview(request):
    return request.app["station"].runs.live_overview()


# ---- ROOT ----
@api("viewer")
async def root_convert(request):
    st: Station = request.app["station"]
    data = await body(request)
    raw = st.files.check(str(data.get("path", "")))
    who = request["user"] or "guest"
    workdir = os.path.join(st.settings.runs_folder(), "users", who.replace("/", "_"), "convert")
    st.converter.request(raw, workdir, who)
    st.activity(request["user"], "asked for a ROOT conversion", raw)
    return st.converter.status()


@api("viewer")
async def root_build(request):
    st: Station = request.app["station"]
    data = await body(request)
    raw = st.files.check(str(data.get("path", "")))
    who = request["user"] or "guest"
    workdir = os.path.join(st.settings.runs_folder(), "users", who.replace("/", "_"), "convert")
    out = st.converter.request_build(raw, workdir, who)
    st.activity(request["user"], "asked for the run's events in one ROOT file", out)
    return {**st.converter.status(), "out": out}


@api("viewer")
async def files_export(request):
    st: Station = request.app["station"]
    path = request.query.get("path", "")
    first, count = int_query(request, "first", 0, 0), int_query(request, "count", 1, 1, EXPORT_MAX_EVENTS)
    text, name = await asyncio.to_thread(st.files.export_text, path, first, count, channels_query(request))
    return web.Response(text=text, content_type="text/plain", charset="utf-8",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})


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
    app["login_fails"] = {}
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
    r.add_post("/api/boards/param", boards_param)
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
