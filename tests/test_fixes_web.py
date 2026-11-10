"""Regression tests of the web server's fixes after the code review and the system test of 2026-11-09. Each test
names the finding it covers (A1, K1, R3, ...) and fails on the code before that fix.

Every test has its own settings, accounts and folders in .test_tmp/web/<test> (see test_web.WebTestCase).
"""

import asyncio
import json
import os
import threading
import time
import types
import unittest
from unittest import mock

from aiohttp import ClientSession, CookieJar, WSMsgType, web
from aiohttp.test_utils import make_mocked_request

from tests.test_web import CHANGE, PASSWORD, WebTestCase
from vx27webdaq.core import config as C
from vx27webdaq.web import runs as runs_module
from vx27webdaq.web import server as S
from vx27webdaq.web.accounts import Accounts


def content(path: str, mode: str = "r"):
    with open(path, mode) as f:
        return f.read()


def strict_json(text: str):
    """JSON as a browser reads it: NaN and Infinity are errors."""
    def refuse(name):
        raise ValueError(f"{name} in the JSON")
    return json.loads(text, parse_constant=refuse)


class FixTestCase(WebTestCase):
    """WebTestCase with more browsers (each its own cookies) and the live connection."""

    def url(self, path: str) -> str:
        return str(self.client.make_url(path))

    async def browser(self, name: str | None = None) -> ClientSession:
        s = ClientSession(cookie_jar=CookieJar(unsafe=True))
        self.addAsyncCleanup(s.close)
        if name:
            r = await s.post(self.url("/api/login"), json={"name": name, "password": PASSWORD}, headers=CHANGE)
            self.assertEqual(r.status, 200, await r.text())
        return s

    async def me(self, session=None) -> dict:
        if session is None:
            return await (await self.client.get("/api/me")).json()
        return await (await session.get(self.url("/api/me"))).json()

    async def next_of(self, ws, kind: str, timeout: float = 8.0) -> dict:
        """The next message of this type on a live connection."""
        end = time.monotonic() + timeout
        while True:
            left = end - time.monotonic()
            if left <= 0:
                self.fail(f"no {kind} message within {timeout} s")
            msg = await ws.receive(timeout=left)
            if msg.type != WSMsgType.TEXT:
                self.fail(f"the connection ended ({msg.type}, code {ws.close_code}) before a {kind} message")
            data = strict_json(msg.data)
            if data.get("type") == kind:
                return data

    async def closed(self, ws, timeout: float = 8.0) -> int:
        """Wait for the server to close the live connection; its close code."""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            msg = await ws.receive(timeout=end - time.monotonic())
            if msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.CLOSING):
                return ws.close_code
        self.fail("the live connection was not closed")

    def stop_following(self) -> None:
        """Stop the station's loops (for a test that puts a run of its own into it)."""
        for t in self.station._tasks:
            t.cancel()


# ---- A1: channels=0-999999999 ----
class ChannelsQueryTests(unittest.TestCase):
    def test_range_checked_before_it_is_filled(self):
        made = []

        def guarded(*args):
            r = range(*args)
            made.append(len(r))
            if len(r) > 64:
                raise AssertionError(f"a range of {len(r)} numbers was made before the check")
            return r

        def q(text):
            return types.SimpleNamespace(query={"channels": text})
        with mock.patch.object(S, "range", guarded, create=True):
            with self.assertRaises(ValueError):
                S.channels_query(q("0-999999999"))
            with self.assertRaises(ValueError):
                S.channels_query(q(",".join(["1"] * 65)))            # too many parts
            with self.assertRaises(ValueError):
                S.channels_query(q("x-3"))
            self.assertEqual(S.channels_query(q("0-3,7, 9")), [0, 1, 2, 3, 7, 9])
            self.assertEqual(S.channels_query(q("63")), [63])


# ---- A7, A18: what requests may hold, and what the page always gets ----
class RequestTests(FixTestCase):
    async def test_non_finite_numbers(self):
        """A7: NaN, Infinity and 1e999 in a request are refused (400); a value that is not finite in the server's
        state (an old draft) still gives JSON the browser reads, on /api/state and on the live connection."""
        await self.login("oper1")
        for text in ('{"config": {"run": {"hk_s": Infinity}}}', '{"config": {"run": {"hk_s": NaN}}}',
                     '{"config": {"run": {"hk_s": 1e999}}}', '{"changes": {"refresh_ms": 1e999}}'):
            path = "/api/settings" if "changes" in text else "/api/config"
            r = await self.client.put(path, data=text, headers={**CHANGE, "Content-Type": "application/json"})
            self.assertEqual(r.status, 400, text)
        self.assertEqual(self.station.cfg.run.hk_s, 5.0)
        self.station.cfg.trigger.software_rate_hz = float("inf")         # as a draft saved before the fix
        self.station.cfg.run.temp_warn_c = float("nan")
        for path in ("/api/state", "/api/config"):
            r = await self.client.get(path)
            self.assertEqual(r.status, 200)
            strict_json(await r.text())
        ws = await self.client.ws_connect("/api/events")
        msg = await ws.receive(timeout=10)
        hello = strict_json(msg.data)
        self.assertEqual(hello["type"], "hello")
        self.assertIsNone(hello["state"]["config"]["config"]["trigger"]["software_rate_hz"])
        await ws.close()

    async def test_deeply_nested_body(self):
        """A18: a body nested a hundred thousand deep is a 400, not a 500 (RecursionError)."""
        await self.login("oper1")
        text = "[" * 100000 + "]" * 100000
        for path in ("/api/sequence/plan", "/api/config/save"):
            r = await self.client.post(path, data=text, headers={**CHANGE, "Content-Type": "application/json"})
            self.assertEqual(r.status, 400, path)

    async def test_wrong_types(self):
        """A18 and the system test's fuzz: values of the wrong type are a 400 with a message, never a 500."""
        await self.login("admin1")
        for changes in (True, 5, "x", [1], {"data_dirs": 5}, {"refresh_ms": None}, {"refresh_ms": [1]},
                        {"runs_dir": 5}, {"use_simulator": [True]}, {"simulator_pulses": 3}):
            r = await self.client.put("/api/settings", json={"changes": changes}, headers=CHANGE)
            self.assertEqual(r.status, 400, changes)
        for path in (5, True, [self.root], {"a": 1}):
            r = await self.client.post("/api/config/save", json={"path": path}, headers=CHANGE)
            self.assertEqual(r.status, 400, path)
        r = await self.client.post("/api/config/open", json={}, headers=CHANGE)
        self.assertEqual(r.status, 400)                                  # no path (it was IsADirectoryError: 500)
        r = await self.client.post("/api/config/open", json={"path": self.root}, headers=CHANGE)
        self.assertEqual(r.status, 400)                                  # a folder
        long = os.path.join(self.root, "x" * 300)                        # the system test's fuzz (after the fixes): 500
        for path, body in (("/api/config/open", {"path": long}), ("/api/config/save", {"path": long + ".json"})):
            r = await self.client.post(path, json=body, headers=CHANGE)
            self.assertEqual(r.status, 400, (path, await r.text()))     # a name too long for any file system
        spec = {"kind": "repeat", "count": 2, "duration_s": 1}
        for bad in ({"duration_s": [1, 2]}, {"duration_s": {"a": 1}}, {"count": [3]}, {"count": "x"},
                    {"kind": "scan", "target": "threshold", "values": [{"a": 1}]},
                    {"kind": "scan", "target": "threshold", "values": "10", "runs_per_value": [1]},
                    {"kind": "scan", "target": "threshold", "values": "10", "board": 5}):
            for path in ("/api/sequence/plan", "/api/sequence/start"):
                r = await self.client.post(path, json={**spec, **bad}, headers=CHANGE)
                self.assertEqual(r.status, 400, (path, bad))
        r = await self.client.post("/api/sequence/plan", json=spec, headers=CHANGE)
        self.assertEqual(r.status, 200, await r.text())

    async def test_wrong_types_of_a_live_change(self):
        """A18: /api/run/control with a channel or value of the wrong type during a run is a 400."""
        self.stop_following()
        run_dir = os.path.join(self.root, "fake_run")
        os.makedirs(run_dir)
        cfg = self.station.cfg.copy()
        self.station.runs.active = runs_module.Active(run_dir, cfg, "run00001", [], time.time(), "oper1", True, 0,
                                                      status={"phase": "running"})
        await self.login("oper1")
        base = {"cmd": "set", "board": "b0", "channel": 1, "what": "threshold", "value": 50}
        for bad in ({"channel": None}, {"channel": "1"}, {"channel": 1.5}, {"channel": True}, {"value": None},
                    {"value": [1]}, {"value": True}, {"value": "much"}):
            r = await self.client.post("/api/run/control", json={**base, **bad}, headers=CHANGE)
            self.assertEqual(r.status, 400, bad)
        self.assertFalse(os.path.exists(os.path.join(run_dir, "control")))   # nothing was sent to the DAQ
        self.station.runs.active = None

    async def test_files_without_a_path(self):
        """M5: a files request with no path is a 400 (it was 403 "no path given")."""
        for path in ("/api/files/info", "/api/files/waveforms", "/api/files/overview", "/api/files/hk"):
            r = await self.client.get(path)
            self.assertEqual(r.status, 400, path)


# ---- A21: only this computer ----
class LoopbackTests(FixTestCase):
    async def test_other_computers_are_refused(self):
        self.assertTrue(all(S.is_loopback(a) for a in ("127.0.0.1", "127.3.4.5", "::1", "::ffff:127.0.0.1")))
        self.assertFalse(any(S.is_loopback(a) for a in ("192.168.0.9", "10.0.0.1", "::ffff:10.1.2.3", "", None, "x")))
        app = self.client.server.app
        called = []

        async def handler(request):
            called.append(request.remote)
            return web.Response(text="ok")

        def request_from(address):
            transport = mock.Mock()
            transport.get_extra_info.side_effect = lambda key, default=None: (address, 40000) if key == "peername" else default
            return make_mocked_request("GET", "/api/state", headers={"Host": "localhost:8770"}, app=app, transport=transport)
        r = await S.guard(request_from("192.168.0.9"), handler)        # another computer, through --host 0.0.0.0
        self.assertEqual((r.status, called), (403, []))
        r = await S.guard(request_from("127.0.0.1"), handler)
        self.assertEqual((r.status, called), (200, ["127.0.0.1"]))


# ---- A4, A5: sign-in ----
class LoginTests(FixTestCase):
    async def test_parallel_wrong_passwords_are_counted(self):
        """A4: wrong passwords sent at the same time are all counted: only 5 are checked, the others wait (429),
        and at most two password checks run at once."""
        st, lock = self.station, threading.Lock()
        checked, running, most = [], [0], [0]
        real = st.accounts.verify

        def verify(name, password):
            with lock:
                running[0] += 1
                most[0] = max(most[0], running[0])
            try:
                checked.append(name)
                return real(name, password)
            finally:
                with lock:
                    running[0] -= 1
        st.accounts.verify = verify

        async def wrong():
            r = await self.client.post("/api/login", json={"name": "oper1", "password": "not the password"}, headers=CHANGE)
            return r.status
        codes = await asyncio.gather(*[wrong() for _ in range(12)])
        self.assertEqual(sorted(codes), [401] * 5 + [429] * 7)
        self.assertEqual(len(checked), 5)
        self.assertLessEqual(most[0], 2)
        r = await self.client.post("/api/login", json={"name": "oper1", "password": PASSWORD}, headers=CHANGE)
        self.assertEqual(r.status, 429)                                  # paused, even with the right password

    async def test_password_change_is_throttled(self):
        """A4: the present password asked by "change your password" is throttled like the sign-in."""
        await self.login("oper1")

        async def wrong():
            r = await self.client.post("/api/password", json={"old": "not the password", "new": "a new long password"},
                                       headers=CHANGE)
            return r.status
        codes = await asyncio.gather(*[wrong() for _ in range(8)])
        self.assertEqual(sorted(codes), [403] * 5 + [429] * 3)

    async def test_names_that_cannot_be_accounts(self):
        """A5: a name that breaks the account name rule is refused without a check, a log entry or a place in the
        list of wrong tries."""
        before = list(self.station.activity_log)
        for name in ("../../etc/passwd", "x" * 5000, "", "guest", "1abc", "a", 5, None):
            r = await self.client.post("/api/login", json={"name": name, "password": "whatever it is"}, headers=CHANGE)
            self.assertEqual(r.status, 401, name)
        self.assertEqual(list(self.station.activity_log), before)
        self.assertEqual(self.client.server.app["login_fails"], {})

    def test_old_wrong_tries_are_forgotten(self):
        """A5: names whose last wrong try is an hour old are forgotten, and at most 1000 names are kept."""
        now = time.time()
        fails = {f"old{k}": (3, 0.0, now - 7200) for k in range(10)}
        fails.update({f"new{k}": (1, 0.0, now - k) for k in range(1500)})
        fails["paused"] = (5, now + 20, now - 7200)                    # its pause goes on: kept
        S._forget_old_fails(fails, now)
        self.assertEqual(len(fails), 1000)
        self.assertFalse(any(k.startswith("old") for k in fails))
        self.assertIn("new0", fails)


# ---- A8, K8/A10: sign-ins and open pages follow the accounts ----
class SessionTests(FixTestCase):
    async def test_command_line_password_and_remove_sign_out(self):
        """A8: a password set on the command line (another process writing users.json) ends the sign-ins made
        before it; an account removed and made again does not get its old sign-ins back."""
        await self.login("oper1")
        self.assertEqual((await self.me())["user"], "oper1")
        cli = Accounts(self.station.accounts.path)                     # what vx27webdaq user ... does
        cli.set_password("oper1", "another long password")
        self.assertIsNone((await self.me())["user"])
        await self.login("view1")
        cli.remove("view1")
        cli.add("view1", PASSWORD, "viewer")
        self.assertIsNone((await self.me())["user"])

    async def test_own_new_password_keeps_this_browser(self):
        """A8: after changing the own password, this browser stays signed in (a new sign-in) and every other browser
        of the account is signed out."""
        await self.login("oper1")
        other = await self.browser("oper1")
        ws = await self.client.ws_connect("/api/events")                # this browser's open page
        await self.next_of(ws, "hello")
        ws2 = await other.ws_connect(self.url("/api/events"))           # the other browser's
        await self.next_of(ws2, "hello")
        r = await self.client.post("/api/password", json={"old": PASSWORD, "new": "my new long password"}, headers=CHANGE)
        self.assertEqual(r.status, 200, await r.text())
        self.assertEqual((await self.me())["user"], "oper1")
        self.assertIsNone((await self.me(other))["user"])
        hello = await self.next_of(ws2, "hello")                         # the other page goes on as a guest
        self.assertIsNone(hello["me"]["user"])
        self.station.publish({"type": "probe"})                          # this page: still signed in, no new hello
        msg = strict_json((await ws.receive(timeout=5)).data)
        while msg["type"] != "probe":
            self.assertNotEqual(msg["type"], "hello", msg)
            msg = strict_json((await ws.receive(timeout=5)).data)
        self.assertEqual([c.user for c in self.station.clients if c.token == self.client.session.cookie_jar.filter_cookies(
            self.client.make_url("/")).get("vx27_session").value], ["oper1"])
        await ws.close()
        await ws2.close()

    async def test_open_pages_follow_role_changes(self):
        """K8/A10: an open page of an account whose role changes gets a new hello with the new role; when the
        account is removed while guests are off, its connection is closed with 4401."""
        oper = await self.browser("oper1")
        ws = await oper.ws_connect(self.url("/api/events"))
        self.assertEqual((await self.next_of(ws, "hello"))["me"]["role"], "operator")
        await self.login("admin1")
        r = await self.client.put("/api/users/oper1", json={"role": "viewer"}, headers=CHANGE)
        self.assertEqual(r.status, 200, await r.text())
        self.assertEqual((await self.next_of(ws, "hello"))["me"]["role"], "viewer")
        r = await self.client.put("/api/settings", json={"changes": {"guests": False}}, headers=CHANGE)
        self.assertEqual(r.status, 200, await r.text())
        r = await self.client.delete("/api/users/oper1", headers=CHANGE)
        self.assertEqual(r.status, 200, await r.text())
        self.assertEqual(await self.closed(ws), 4401)

    async def test_open_pages_follow_the_command_line(self):
        """K8/A10: a role set on the command line reaches the open pages within a few seconds."""
        view = await self.browser("view1")
        ws = await view.ws_connect(self.url("/api/events"))
        self.assertEqual((await self.next_of(ws, "hello"))["me"]["role"], "viewer")
        Accounts(self.station.accounts.path).set_role("view1", "operator")
        self.assertEqual((await self.next_of(ws, "hello", timeout=10))["me"]["role"], "operator")
        await ws.close()

    async def test_guests_turned_off_close_guest_pages(self):
        """K8/A10: guests turned off: a page of someone not signed in is closed with 4401; signing out with guests
        off closes the page too."""
        guest = await self.browser()
        ws = await guest.ws_connect(self.url("/api/events"))
        hello = await self.next_of(ws, "hello")
        self.assertEqual((hello["me"]["user"], hello["me"]["role"]), (None, "viewer"))
        oper = await self.browser("oper1")
        ws2 = await oper.ws_connect(self.url("/api/events"))
        await self.next_of(ws2, "hello")
        await self.login("admin1")
        r = await self.client.put("/api/settings", json={"changes": {"guests": False}}, headers=CHANGE)
        self.assertEqual(r.status, 200, await r.text())
        self.assertEqual(await self.closed(ws), 4401)
        r = await oper.post(self.url("/api/logout"), headers=CHANGE)
        self.assertEqual(r.status, 200)
        self.assertEqual(await self.closed(ws2), 4401)


# ---- K1: an edit from an older version of the configuration ----
class ConflictTests(FixTestCase):
    async def test_stale_edit_is_refused(self):
        await self.login("oper1")
        p = await (await self.client.get("/api/config")).json()
        v, cfg = p["version"], p["config"]
        cfg["run"]["hk_s"] = 7.0
        r = await self.client.put("/api/config", json={"config": cfg, "base": v}, headers=CHANGE)
        self.assertEqual(r.status, 200, await r.text())
        v2 = (await r.json())["version"]
        self.assertEqual(v2, v + 1)
        cfg["run"]["hk_s"] = 9.0
        r = await self.client.put("/api/config", json={"config": cfg, "base": v}, headers=CHANGE)
        self.assertEqual(r.status, 409)
        res = await r.json()
        self.assertEqual((res["error"], res["conflict"]), ("the configuration was changed by someone else meanwhile", True))
        self.assertEqual((res["current"]["version"], res["current"]["config"]["run"]["hk_s"]), (v2, 7.0))
        self.assertEqual(self.station.cfg.run.hk_s, 7.0)                 # not applied
        r = await self.client.put("/api/note", json={"note": "from an old page", "base": v}, headers=CHANGE)
        self.assertEqual(r.status, 409)
        self.assertEqual(self.station.run_note, "")
        r = await self.client.put("/api/note", json={"note": "from the newest", "base": v2}, headers=CHANGE)
        self.assertEqual(r.status, 200)
        r = await self.client.put("/api/config", json={"config": cfg, "base": "3"}, headers=CHANGE)
        self.assertEqual(r.status, 400)
        r = await self.client.put("/api/config", json={"config": cfg}, headers=CHANGE)   # no base: taken as it comes
        self.assertEqual(r.status, 200)
        self.assertEqual(self.station.cfg.run.hk_s, 9.0)


# ---- C4/A2/R2: where Save may write ----
class SaveTargetTests(FixTestCase):
    async def save(self, path):
        return await self.client.post("/api/config/save", json={"path": path}, headers=CHANGE)

    async def test_server_files_and_records_are_refused(self):
        from vx27webdaq.web.settings import config_dir
        await self.login("oper1")
        st = self.station
        st.sessions.save()
        st.runs._write_current(runs_module.Active(os.path.join(self.root, "x"), st.cfg, "run00001", [], 0, "oper1", True, 0))
        protected = [os.path.join(st.state_dir, n) for n in ("users.json", "sessions.json", "current_run.json", "draft.json")]
        protected.append(os.path.join(config_dir(), "web.json"))
        before = {p: content(p, "rb") for p in protected if os.path.exists(p)}
        for path in protected + [os.path.join(st.state_dir, "new.json"), os.path.join(st.state_dir, "trees", "b0.json")]:
            r = await self.save(path)
            self.assertIn(r.status, (403, 404), path)
        self.assertEqual({p: content(p, "rb") for p in before}, before)
        await self.login("admin1")                                       # the accounts are all there
        # a run folder's records, and a data file's NAME.json
        run = os.path.join(self.root, "runs", "20261109_120000")
        os.makedirs(os.path.join(run, "control"))
        C.save_config(st.cfg, os.path.join(run, "config.json"), run_name="run00001", run_folder=run)
        for name in ("daq.log", "run.json", "status.json"):
            with open(os.path.join(run, name), "w") as f:
                f.write("{}\n")
        data = os.path.join(self.root, "data")
        with open(os.path.join(data, "run00001_b0.raw"), "wb") as f:
            f.write(b"ALABRAW1")
        C.save_config(st.cfg, os.path.join(data, "run00001_b0.json"), run_name="run00001", data_file="run00001_b0.raw")
        copied = os.path.join(self.root, "copied_record.json")
        C.save_config(st.cfg, copied, run_name="run00001", run_folder=run)
        for path in [os.path.join(run, n) for n in ("config.json", "run.json", "status.json")] + \
                    [os.path.join(run, "control", "1_0001.json"), os.path.join(data, "run00001_b0.json"), copied]:
            r = await self.save(path)
            self.assertEqual(r.status, 403, path)
        # a file that is not a configuration is never written over; a configuration is
        other = os.path.join(self.root, "other.json")
        with open(other, "w") as f:
            f.write('{"some": "data"}\n')
        r = await self.save(other)
        self.assertEqual(r.status, 400)
        self.assertEqual(content(other), '{"some": "data"}\n')
        mine = os.path.join(self.root, "mine.json")
        self.assertEqual((await self.save(mine)).status, 200)
        self.assertEqual((await self.save(mine)).status, 200)

    async def test_record_opened_asks_for_a_new_name(self):
        """Opened from a run's config.json, the configuration has no file of its own: Save asks for a name."""
        await self.login("oper1")
        run = os.path.join(self.root, "runs", "20261109_130000")
        os.makedirs(run)
        C.save_config(self.station.cfg, os.path.join(run, "config.json"), note="the run's note", run_name="run00002",
                      run_folder=run)
        with open(os.path.join(run, "daq.log"), "w") as f:
            f.write("$ python -m vx27webdaq.daq run\n")
        r = await self.client.post("/api/config/open", json={"path": os.path.join(run, "config.json")}, headers=CHANGE)
        p = await r.json()
        self.assertEqual((r.status, p["path"], p["note"]), (200, "", "the run's note"))
        r = await self.client.post("/api/config/save", json={}, headers=CHANGE)
        self.assertEqual(r.status, 400)
        self.assertIn("give a file name", await r.text())

    async def test_planted_link_is_not_followed(self):
        """Save writes a temporary file of a name nobody can guess: a link planted at NAME.json.part is not followed."""
        await self.login("oper1")
        precious = os.path.join(self.root, "precious.txt")
        with open(precious, "w") as f:
            f.write("keep me\n")
        path = os.path.join(self.root, "planted.json")
        os.symlink(precious, path + ".part")
        r = await self.save(path)
        self.assertEqual(r.status, 200, await r.text())
        self.assertEqual(content(precious), "keep me\n")
        C.load_config(path)


# ---- A6: the CAEN library folder ----
class LibraryFolderTests(FixTestCase):
    async def put(self, value):
        return await self.client.put("/api/settings", json={"changes": {"caen_lib_dir": value}}, headers=CHANGE)

    async def test_only_admins_and_only_safe_folders(self):
        good = os.path.join(self.root, "libs")
        os.makedirs(good)
        os.chmod(good, 0o755)
        await self.login("oper1")
        self.assertEqual((await self.put(good)).status, 403)
        await self.login("admin1")
        r = await self.put(good)
        self.assertEqual(r.status, 200, await r.text())
        self.assertEqual(self.station.settings.lib_env()["LD_LIBRARY_PATH"].split(":")[0], good)
        self.assertIsNone((await r.json())["caen_lib_problem"])
        open_dir = os.path.join(self.root, "open_libs")
        os.makedirs(open_dir)
        os.chmod(open_dir, 0o777)
        r = await self.put(open_dir)
        self.assertEqual(r.status, 400)
        self.assertIn("not safe", await r.text())
        other = os.path.join(self.root, "libs2")
        os.makedirs(other)
        os.chmod(other, 0o755)
        with open(os.path.join(other, "libCAEN_FELib.so"), "w") as f:
            f.write("x")
        os.chmod(os.path.join(other, "libCAEN_FELib.so"), 0o666)
        self.assertEqual((await self.put(other)).status, 400)
        # a library made writable by others after it was chosen: no longer given to the DAQ, and the page says why
        planted = os.path.join(good, "libc.so.6")
        with open(planted, "w") as f:
            f.write("x")
        os.chmod(planted, 0o666)
        self.assertNotIn(good, self.station.settings.lib_env().get("LD_LIBRARY_PATH", ""))
        s = await (await self.client.get("/api/settings")).json()
        self.assertIn("libc.so.6", s["caen_lib_problem"])
        self.assertIn("caen_lib_dir", s["admin_only"])


# ---- A15, A12: the data folders ----
class DataFolderTests(FixTestCase):
    async def test_root_and_home_are_never_data_folders(self):
        await self.login("oper1")
        home = os.path.expanduser("~")
        for d in ("/", home, "~", os.path.dirname(home), str(self.station.state_dir)):
            r = await self.client.put("/api/settings", json={"changes": {"data_dirs": [d]}}, headers=CHANGE)
            self.assertEqual(r.status, 400, d)
        r = await self.client.put("/api/settings", json={"changes": {"runs_dir": "."}}, headers=CHANGE)
        self.assertEqual(r.status, 400)                                  # the save folder: the home folder
        r = await self.client.put("/api/settings", json={"changes": {"data_dirs": [os.path.join(self.root, "data")]}},
                                  headers=CHANGE)
        self.assertEqual(r.status, 200, await r.text())
        self.station.cfg.run.data_dir = home                             # the configuration's data folder: the home
        self.assertNotIn(os.path.realpath(home), [os.path.realpath(p) for _, p in self.station.data_roots()])
        problems = self.station.config_payload()["problems"]
        self.assertTrue(any(p["where"] == "run.data_dir" and "home folder" in p["message"] for p in problems), problems)

    async def test_the_last_runs_folder_stays_open(self):
        """A15: the data folder of the last run stays a data folder after the configuration's data folder changed."""
        old = os.path.join(self.root, "old_data")
        os.makedirs(old)
        path = os.path.join(old, "run00001_b0.raw")
        with open(path, "wb") as f:
            f.write(b"ALABRAW1")
        cfg = self.station.cfg.copy()
        cfg.run.data_dir = old
        self.station.runs.last_cfg = cfg
        self.station.cfg.run.data_dir = os.path.join(self.root, "data")
        self.assertEqual(self.station.files.check(path), os.path.realpath(path))

    async def test_hung_folder_does_not_stop_the_server(self):
        """A12: a data folder that does not answer (a hung NAS) costs at most a moment, once; the state comes."""
        hung = os.path.join(self.root, "hung")
        os.makedirs(hung)
        real_isdir = os.path.isdir

        def isdir(p):
            if os.path.abspath(str(p)) == hung:
                time.sleep(3)
            return real_isdir(p)
        with mock.patch("os.path.isdir", isdir):
            self.station.cfg.run.data_dir = hung
            t0 = time.monotonic()
            r = await self.client.get("/api/state")
            first = time.monotonic() - t0
            state = await r.json()
            t0 = time.monotonic()
            r = await self.client.get("/api/state")
            second = time.monotonic() - t0
        self.assertEqual(r.status, 200)
        self.assertLess(first, 1.5)
        self.assertLess(second, 0.5)
        self.assertTrue(any("does not answer" in p["message"] for p in state["config"]["problems"]), state["config"]["problems"])


# ---- data files ----
def run_config(sync: str = "chain") -> dict:
    cfg = C.default_config()
    cfg.run.sync = sync
    for b in cfg.boards:
        b.record_length, b.pre_trigger = 16, 4
    return C.to_dict(cfg)


def write_raw(path: str, run, board, events, sync: str = "chain", file_index: int = 0, model: str = "V2740",
              header: dict | None = None) -> str:
    """A small data file: events [(trigger ID, timestamp)], two channels of 16 samples."""
    from vx27webdaq.core import rawdata as R
    import numpy as np
    head = {"format": "alab-daq-raw", "version": 1, "run": run, "file_index": file_index, "sync": sync,
            "board": {"name": board, "model": model, "record_length": 16, "pre_trigger": 4, "sample_ns": 8.0, "index": 0},
            "config": run_config(sync)}
    head.update(header or {})
    data = R.file_prefix(head)
    for tid, ts in events:
        data += R.encode_scope(tid, ts, [0, 1], np.full((2, 16), 1000 + tid % 100, np.uint16))
    with open(path, "wb") as f:
        f.write(data)
    return path


class DataFileTests(FixTestCase):
    def data(self, *parts) -> str:
        return os.path.join(self.root, "data", *parts)

    async def test_combined_view_of_a_run_of_many_files(self):
        """A9: a run of more files than the cache holds (16): the files the combined view uses stay open."""
        write_raw(self.data("run00001_b0.raw"), "run00001", "b0", [(1, 100), (2, 200)])
        for k in range(1, 4):
            write_raw(self.data(f"run00001_b0_{k:03d}.raw"), "run00001", "b0", [(10 * k, 1000 * k)], file_index=k)
        write_raw(self.data("run00001_b1.raw"), "run00001", "b1", [(1, 101), (2, 201)], model="V2730")
        for k in range(1, 20):
            write_raw(self.data(f"run00001_b1_{k:03d}.raw"), "run00001", "b1", [(10 * k, 1000 * k + 1)], file_index=k,
                      model="V2730")
        for event in (0, 1):
            r = await self.client.get("/api/files/combined", params={"path": self.data("run00001_b0.raw"), "event": event,
                                                                     "traces": "b0:0,b1:1"})
            res = await r.json()
            self.assertEqual(r.status, 200, res)
            self.assertTrue(res["matched"], res)
            self.assertEqual([b["timestamp"] for b in res["boards"]], [[100, 101], [200, 201]][event])
        self.assertLessEqual(len(self.station.files._open), 16)         # the cache is back to its size

    async def test_a_file_lists_its_runs_boards(self):
        """Opening one board's file of a run (the waveform view) lists every board's file of that run beside it, the
        master first (the run's order), each board's part of the same number (else its first); found by the names
        alone, so only the file opened is open; not the event-built file, not another run's files."""
        write_raw(self.data("run00005_b1.raw"), "run00005", "b1", [(1, 100), (2, 200)], model="V2730")
        write_raw(self.data("run00005_b0.raw"), "run00005", "b0", [(1, 100)])
        write_raw(self.data("run00005_b0_001.raw"), "run00005", "b0", [(3, 300)], file_index=1)
        write_raw(self.data("run00005_b1_001.raw"), "run00005", "b1", [(3, 300)], file_index=1, model="V2730")
        write_raw(self.data("run00005_a2.raw"), "run00005", "a2", [(1, 100)])     # not in the run's configuration: last
        write_raw(self.data("run00006_b0.raw"), "run00006", "b0", [(1, 100)])     # another run
        open(self.data("run00005_built.raw"), "wb").close()                       # not a board's file
        before = len(self.station.files._open)
        res = await (await self.client.get("/api/files/info", params={"path": self.data("run00005_b0.raw")})).json()
        self.assertEqual([(r["board"], os.path.basename(r["path"])) for r in res["run_files"]],
                         [("b0", "run00005_b0.raw"), ("b1", "run00005_b1.raw"), ("a2", "run00005_a2.raw")])
        self.assertEqual(len(self.station.files._open), before + 1)              # only the file opened
        res = await (await self.client.get("/api/files/info", params={"path": self.data("run00005_b1_001.raw")})).json()
        self.assertEqual([os.path.basename(r["path"]) for r in res["run_files"]],
                         ["run00005_b0_001.raw", "run00005_b1_001.raw", "run00005_a2.raw"])

    async def test_combined_view_pairs_by_time(self):
        """C8: the other board's event is the one nearest in time within the sync window, not the one with the same
        24-bit trigger ID (which came round again, or belongs to a board that triggers by itself)."""
        write_raw(self.data("run00002_b0.raw"), "run00002", "b0", [(5, 1000)])
        write_raw(self.data("run00002_b1.raw"), "run00002", "b1", [(5, 10_000_000), (77, 1002)], model="V2730")
        r = await self.client.get("/api/files/combined", params={"path": self.data("run00002_b0.raw"), "event": 0,
                                                                 "traces": "b0:0,b1:0"})
        res = await r.json()
        self.assertTrue(res["matched"], res)
        self.assertEqual([(b["trigger_id"], b["timestamp"]) for b in res["boards"]], [(5, 1000), (77, 1002)])
        # nothing within the window: not matched
        write_raw(self.data("run00003_b0.raw"), "run00003", "b0", [(5, 1000)])
        write_raw(self.data("run00003_b1.raw"), "run00003", "b1", [(5, 1000 + 200)], model="V2730")   # 1.6 µs later
        res = await (await self.client.get("/api/files/combined", params={"path": self.data("run00003_b0.raw"),
                                                                          "event": 0, "traces": "b0:0,b1:0"})).json()
        self.assertEqual((res["matched"], res["missing"]), (False, ["b1"]))
        # boards with clocks of their own: their timestamps cannot be compared
        write_raw(self.data("run00004_b0.raw"), "run00004", "b0", [(5, 1000)], sync="independent")
        write_raw(self.data("run00004_b1.raw"), "run00004", "b1", [(5, 1000)], sync="independent", model="V2730")
        res = await (await self.client.get("/api/files/combined", params={"path": self.data("run00004_b0.raw"),
                                                                          "event": 0, "traces": "b0:0,b1:0"})).json()
        self.assertEqual((res["matched"], res["missing"]), (False, ["b1"]))

    async def test_special_files_and_folders(self):
        """A14: a FIFO or a folder named *.raw is refused at once (a FIFO used to block a worker thread for ever, a
        folder gave a 500), and the listing shows only folders and regular files."""
        fifo, folder = self.data("pipe.raw"), self.data("folder.raw")
        os.mkfifo(fifo)
        os.makedirs(folder)
        write_raw(self.data("run00001_b0.raw"), "run00001", "b0", [(1, 100)])
        try:
            for path in (fifo, folder):
                r = await asyncio.wait_for(self.client.get("/api/files/info", params={"path": path}), 5)
                self.assertEqual(r.status, 403, path)
            names = {e["name"]: e["kind"] for e in self.station.files.listing(self.data())["entries"]}
            self.assertEqual(names, {"folder.raw": "folder", "run00001_b0.raw": "raw"})
        finally:
            try:                                                         # a reader still waiting gets its end of file
                os.close(os.open(fifo, os.O_WRONLY | os.O_NONBLOCK))
            except OSError:
                pass

    async def test_headers_of_other_types(self):
        """A16: a header that is not an object, a board entry of another type, a ROOT file without ch_mask: 400 or
        a page, never a 500; the ROOT file is closed again."""
        from vx27webdaq.core import rawdata as R
        with open(self.data("list.raw"), "wb") as f:
            f.write(R.file_prefix([1, 2, 3]))
        r = await self.client.get("/api/files/info", params={"path": self.data("list.raw")})
        self.assertEqual(r.status, 400)
        write_raw(self.data("odd.raw"), 7, "b0", [(1, 100)], header={"board": [1, 2], "config": "x", "file_index": "z"})
        r = await self.client.get("/api/files/info", params={"path": self.data("odd.raw")})
        self.assertEqual(r.status, 200, await r.text())
        import numpy as np
        import uproot
        path = self.data("run00005_b0.root")
        with uproot.recreate(path) as out:
            out["settings"] = json.dumps({"run": "run00005", "board": {"name": "b0"}})
            out.mktree("events", {"trigger_id": np.uint32, "timestamp": np.uint64}).extend(
                {"trigger_id": np.array([1], np.uint32), "timestamp": np.array([5], np.uint64)})
        r = await self.client.get("/api/files/info", params={"path": path})
        self.assertEqual(r.status, 400)
        self.assertIn("ch_mask", await r.text())
        self.assertNotIn(os.path.realpath(path), self.station.files._open)

    async def test_listing_has_a_limit(self):
        """A17: the data folder listing has the same limit as the chooser: the newest files, and "more"."""
        from vx27webdaq.web import files as F
        for k in range(8):
            p = write_raw(self.data(f"run{k + 1:05d}_b0.raw"), f"run{k + 1:05d}", "b0", [(1, 100)])
            os.utime(p, (1_000_000 + k, 1_000_000 + k))
        with mock.patch.object(F, "BROWSE_LIMIT", 5):
            out = self.station.files.listing(self.data())
        self.assertEqual([e["name"] for e in out["entries"]], [f"run{k:05d}_b0.raw" for k in (8, 7, 6, 5, 4)])
        self.assertTrue(out["more"])

    async def test_paths_from_a_header_stay_in_the_data_folders(self):
        """A11: the housekeeping file and RUN_built.root are named from the header: a run named "../../secret" can
        neither read a file outside the data folders nor have one written there."""
        os.makedirs(self.data("sub"))
        with open(os.path.join(self.root, "secret_b0_hk.csv"), "w") as f:
            f.write("time,temp_core\n1,42\n")
        path = write_raw(self.data("sub", "run00001_b0.raw"), "../../secret", "b0", [(1, 100)])
        res = await (await self.client.get("/api/files/hk", params={"path": path})).json()
        self.assertEqual((res["rows"], res.get("missing")), (0, True), res)
        r = await self.client.post("/api/root/build", json={"path": path}, headers=CHANGE)
        self.assertEqual(r.status, 409, await r.text())                 # refused: the header names no run
        self.assertFalse(self.station.converter.busy())

    async def test_housekeeping_from_the_root_file(self):
        """A20: without RUN_BOARD_hk.csv beside it, the housekeeping comes from the ROOT file's hk tree."""
        import numpy as np
        import uproot
        raw = write_raw(self.data("run00006_b0.raw"), "run00006", "b0", [(1, 100)])
        with uproot.recreate(self.data("run00006_b0.root")) as out:
            out["settings"] = json.dumps({"run": "run00006", "board": {"name": "b0"}})
            out.mktree("events", {"trigger_id": np.uint32, "timestamp": np.uint64, "ch_mask": np.uint64}).extend(
                {"trigger_id": np.array([1], np.uint32), "timestamp": np.array([100], np.uint64),
                 "ch_mask": np.array([3], np.uint64)})
            out.mktree("hk", {"time": np.float64, "temp_core": np.float64}).extend(
                {"time": np.array([1.0, 2.0]), "temp_core": np.array([41.5, 42.5])})
        for path in (raw, self.data("run00006_b0.root")):
            res = await (await self.client.get("/api/files/hk", params={"path": path})).json()
            self.assertEqual((res["rows"], res["columns"]["temp_core"]), (2, [41.5, 42.5]), path)

    async def test_text_export_is_streamed_one_at_a_time(self):
        """A13: the text export is sent while it is made (chunked, not one string of GBs), one export at a time."""
        from vx27webdaq.web import files as F
        path = write_raw(self.data("run00007_b0.raw"), "run00007", "b0", [(k, 100 * k) for k in range(20)])
        real = F.TextExport.chunks

        def slow(export):
            for text in real(export):
                time.sleep(0.05)
                yield text
        with mock.patch.object(F.TextExport, "chunks", slow, create=True):
            first = asyncio.ensure_future(self.client.get("/api/files/export", params={"path": path, "count": 20}))
            await asyncio.sleep(0.3)
            second = await self.client.get("/api/files/export", params={"path": path, "count": 2})
            r = await first
            text = await r.text()
        self.assertEqual(second.status, 409)
        self.assertEqual((r.status, r.headers.get("Transfer-Encoding")), (200, "chunked"))
        self.assertEqual(text.count("Event number:"), 20)
        self.assertTrue(text.endswith("\n") and "Channels: 0 1\n" in text)

    async def test_large_live_answers_leave_the_server_free(self):
        """A13: the live waveforms (with full=1 some 25 MB of JSON) are made in a worker thread: other requests are
        answered meanwhile."""
        real = self.station.runs.live_waveforms

        def slow(*args):
            time.sleep(1.0)
            return real(*args)
        self.station.runs.live_waveforms = slow
        t0 = time.monotonic()               # the test shares the server's event loop: a stall shows here too
        big = asyncio.ensure_future(self.client.get("/api/live/waveforms", params={"board": "b0", "channels": "0", "full": "1"}))
        await asyncio.sleep(0.2)
        r = await self.client.get("/api/me")
        self.assertLess(time.monotonic() - t0, 0.9)
        self.assertEqual(r.status, 200)
        self.assertEqual((await big).status, 200)


# ---- the ROOT conversions ----
class ConverterTests(FixTestCase):
    def data(self, *parts) -> str:
        return os.path.join(self.root, "data", *parts)

    async def test_queue_per_user_and_free_space(self):
        """A19: at most 20 conversions of one user wait (guests count as one); a file is skipped, with the reason,
        when the data folder has less free space than about twice its size."""
        self.stop_following()                                            # nothing is converted here
        for k in range(22):
            write_raw(self.data(f"run{k + 1:05d}_b0.raw"), f"run{k + 1:05d}", "b0", [(1, 100)])
        codes = []
        for k in range(22):
            r = await self.client.post("/api/root/convert", json={"path": self.data(f"run{k + 1:05d}_b0.raw")}, headers=CHANGE)
            codes.append(r.status)
        self.assertEqual(codes, [200] * 20 + [409] * 2)
        conv = self.station.converter
        with mock.patch("vx27webdaq.web.converter.disk_free", lambda path: 10, create=True):
            job = conv.queue[0]
            await conv._convert(job)
        self.assertEqual((conv.history[-1]["status"], conv.history[-1]["raw"]), ("skipped", job["raw"]))
        self.assertIn("free space", conv.history[-1]["message"])
        self.assertFalse(os.path.exists(job["raw"][:-4] + ".root"))

    async def test_restart_keeps_event_buildings(self):
        """R6: an event building waiting in the queue is still one after a restart (not a conversion)."""
        from vx27webdaq.web.settings import write_atomic
        self.stop_following()                                            # the queue as restored, nothing converted
        raw = write_raw(self.data("run00001_b0.raw"), "run00001", "b0", [(1, 100)])
        write_atomic(self.station.state_dir / "root_queue.json",
                     json.dumps([{"kind": "build", "raw": raw, "workdir": self.root, "run_json": None, "by": "oper1"},
                                 {"raw": raw, "workdir": self.root, "run_json": None, "by": "oper1"}]))
        conv = self.station.converter
        conv.queue.clear()
        conv.restore()
        self.assertEqual([j.get("kind", "convert") for j in conv.queue], ["build", "convert"])

    async def test_results_of_crashes_and_unmatched_runs(self):
        """R7/C10: a converter that crashed (exit 1 without its check line) leaves no NAME.root; event building of a
        run that cannot be matched (exit 3) is "skipped", not "failed". M6: RUN_built.root goes under "built" in
        run.json, not among the data files. R19: an unreadable run.json is left as it is."""
        self.stop_following()
        conv = self.station.converter
        raw = write_raw(self.data("run00008_b0.raw"), "run00008", "b0", [(1, 100)])
        run_json = os.path.join(self.root, "run.json")
        with open(run_json, "w") as f:
            json.dump({"run_name": "run00008", "root_files": {}}, f)

        def fake(code, text, part):
            async def run(cmd, cwd, log):
                with open(part, "w") as f:
                    f.write("half a file")
                return code, text
            return run
        conv._run = fake(1, "Traceback (most recent call last):\nMemoryError\n", raw[:-4] + ".root.part")
        await conv._convert({"raw": raw, "workdir": self.root, "run_json": run_json, "by": "oper1"})
        self.assertEqual(conv.history[-1]["status"], "failed")
        self.assertFalse(os.path.exists(raw[:-4] + ".root") or os.path.exists(raw[:-4] + ".root.part"))
        conv._run = fake(3, "ERROR: the boards of run00008 ran on clocks of their own: they cannot be matched\n",
                         self.data("run00008_built.root.part"))
        await conv._convert({"kind": "build", "raw": raw, "workdir": self.root, "run_json": run_json, "by": "oper1"})
        self.assertEqual(conv.history[-1]["status"], "skipped")
        self.assertIn("clocks of their own", conv.history[-1]["message"])
        await asyncio.wrap_future(conv._records.submit(lambda: None))
        with open(run_json) as f:
            summary = json.load(f)
        self.assertEqual(summary["built"]["status"], "skipped")
        self.assertEqual(set(summary["root_files"]), {"run00008_b0.raw"})
        with open(run_json, "w") as f:
            f.write("{ not JSON")
        conv._add_to_run_json(run_json, raw, "", "ok", "")
        self.assertEqual(content(run_json), "{ not JSON")
        gone = os.path.join(self.root, "removed_run", "run.json")
        conv._add_to_run_json(gone, raw, "", "ok", "")
        self.assertFalse(os.path.exists(os.path.dirname(gone)))


# ---- the boards: names, the board lock, programs that end ----
class BoardTests(FixTestCase):
    async def checked(self):
        await self.wait(lambda: asyncio.sleep(0, not self.station.boards.running), 30, "the end of the board check")

    async def test_board_names_never_name_other_files(self):
        """A3: a board named "../users" is refused by the check (whose parameter tree would have been written over
        users.json), Send to boards and the manual controller; /api/boards/tree takes only a board's name."""
        await self.login("oper1")
        users = content(self.station.accounts.path)
        self.station.cfg.boards[0].name = "../users"       # (the configuration check refuses such a name too)
        r = await self.client.post("/api/boards/check", headers=CHANGE)
        self.assertEqual(r.status, 409, await r.text())
        await self.checked()
        r = await self.client.post("/api/boards/apply", headers=CHANGE)
        self.assertIn(r.status, (400, 409))
        r = await self.client.post("/api/boards/param", json={"board": "../users", "action": "get", "path": "/par/ModelName"},
                                   headers=CHANGE)
        self.assertEqual(r.status, 409)
        r = await self.client.get("/api/boards/tree", params={"name": "../users"})
        self.assertEqual(r.status, 400)
        self.assertEqual(content(self.station.accounts.path), users)
        self.station.cfg.boards[0].name = "b0"
        await self.login("admin1")                                       # the accounts are all there

    async def test_check_that_cannot_start_ends(self):
        """R5: a check whose configuration cannot be written (a full disk) ends at once: Check boards, Send to boards and
        the manual controller stay possible."""
        await self.login("oper1")
        from vx27webdaq.web import boardcheck
        with mock.patch.object(boardcheck.C, "save_config", side_effect=OSError(28, "No space left on device")):
            r = await self.client.post("/api/boards/check", headers=CHANGE)
            self.assertEqual(r.status, 200, await r.text())
            await self.checked()
        b = self.station.boards.view()
        self.assertFalse(b["running"])
        self.assertTrue(all(x["ok"] is False and "No space left" in x["error"] for x in b["results"]), b["results"])
        r = await self.client.post("/api/boards/check", headers=CHANGE)  # possible again
        self.assertEqual(r.status, 200, await r.text())
        await self.checked()

    async def test_cancelled_program_is_killed(self):
        """R17: a board program still running when its task is cancelled (the server stops) is killed."""
        started = []
        real = asyncio.create_subprocess_exec

        async def sleeper(*cmd, **kw):
            proc = await real("sleep", "30", stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE)
            started.append(proc)
            return proc
        try:
            with mock.patch("asyncio.create_subprocess_exec", sleeper):
                task = asyncio.ensure_future(self.station.boards.apply("oper1"))
                await self.wait(lambda: asyncio.sleep(0, bool(started)), 10, "the program to start")
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            self.assertIsNotNone(started[0].returncode)                  # killed, not left talking to the boards
            self.assertFalse(self.station.boards.param_busy)
        finally:
            for p in started:
                if p.returncode is None:
                    p.kill()
                    await p.wait()

    async def test_one_user_of_the_boards_at_a_time(self):
        """R3: a check, Send to boards and the manual controller are refused during a run sequence (also in the pause
        between its runs) and while another of them has the boards; a run does not start while a check goes."""
        await self.login("oper1")
        st = self.station
        st.sequence.seq = {"id": "x", "by": "oper1", "started": time.time(), "spec": {}, "labels": ["a", "b"],
                           "index": 1, "state": "running", "runs": [], "note": "", "message": ""}
        st.sequence._next_at = time.time() + 60                       # the pause between two runs
        param = {"board": "b0", "action": "get", "path": "/par/ModelName"}
        for path, body in (("/api/boards/check", None), ("/api/boards/apply", None), ("/api/boards/param", param)):
            r = await self.client.post(path, json=body, headers=CHANGE)
            self.assertEqual(r.status, 409, path)
            self.assertIn("sequence", await r.text())
        st.sequence.seq, st.sequence._next_at = None, None
        st.boards.param_busy = True                                    # Send to boards or the manual controller
        r = await self.client.post("/api/boards/check", headers=CHANGE)
        self.assertEqual(r.status, 409)
        st.boards.param_busy = False
        st.boards.running = True                                       # a board check
        for path, body in (("/api/boards/apply", None), ("/api/boards/param", param), ("/api/run/start", {})):
            r = await self.client.post(path, json=body, headers=CHANGE)
            self.assertEqual(r.status, 409, path)
            self.assertIn("busy", await r.text())
        st.boards.running = False
        self.assertIsNone(st.runs.active)


# ---- the run ----
class FakeRunTests(FixTestCase):
    """A run of the test's own making (no DAQ program): the station's loops are stopped first."""

    def fake_run(self, phase: str = "running", **kw) -> runs_module.Active:
        self.stop_following()
        run_dir = os.path.join(self.root, "runs", "20261109_120000")
        os.makedirs(os.path.join(run_dir, "control"), exist_ok=True)
        a = runs_module.Active(run_dir, self.station.cfg.copy(), "run00001", [], time.time(), "oper1", True,
                               kw.pop("wrapper_pid", 0), status={"phase": phase}, **kw)
        self.station.runs.active = a
        return a

    async def test_manual_start_during_a_sequence(self):
        """R8: Start run is refused while a sequence goes (also in its pause), and only the sequence's own run moves
        it on."""
        await self.login("oper1")
        st = self.station
        st.sequence.seq = {"id": "x", "by": "oper1", "started": time.time(), "spec": {}, "labels": ["a", "b"], "index": 0,
                           "state": "running", "runs": [{"index": 0, "label": "a", "run_dir": "/its/own/run",
                                                         "run_name": "run00001", "phase": "running"}],
                           "note": "", "message": ""}
        r = await self.client.post("/api/run/start", json={}, headers=CHANGE)
        self.assertEqual(r.status, 409)
        self.assertIn("sequence", await r.text())
        self.assertIsNone(st.runs.active)
        st.sequence.run_ended("/another/run", "failed")                  # not its run: nothing happens
        self.assertEqual((st.sequence.seq["state"], st.sequence.seq["index"]), ("running", 0))
        st.sequence.run_ended("/its/own/run", "finished")
        self.assertEqual(st.sequence.seq["index"], 1)
        st.sequence.seq["state"] = "stopped by test"

    async def test_stop_signals_only_the_runs_processes(self):
        """R9: Stop never signals a process that only has the run's pid now (no wrapper's or DAQ program's command
        line)."""
        await self.login("oper1")
        other = await asyncio.create_subprocess_exec("sleep", "30")
        try:
            self.fake_run(wrapper_pid=other.pid)
            r = await self.client.post("/api/run/stop", headers=CHANGE)
            self.assertEqual(r.status, 409)                              # "the DAQ program has ended already"
            await asyncio.sleep(0.2)
            self.assertIsNone(other.returncode)                          # not signalled
        finally:
            other.kill()
            await other.wait()
            self.station.runs.active = None

    async def test_live_command_not_taken_is_withdrawn(self):
        """R14: a command the DAQ program did not take in time is taken back (it would have been done later, behind
        the configuration's back); one it took late still gets its answer."""
        await self.login("oper1")
        a = self.fake_run()
        with mock.patch.object(runs_module, "CONTROL_WAIT_S", 1.0, create=True):
            r = await self.client.post("/api/run/control", json={"cmd": "swtrigger"}, headers=CHANGE)
            self.assertEqual(r.status, 409)
            self.assertIn("taken back", await r.text())
            self.assertEqual(os.listdir(a.path("control")), [])

            def late_daq():                 # takes the command at once, answers after 1.5 s
                end = time.time() + 5
                while time.time() < end:
                    names = [n for n in os.listdir(a.path("control")) if n.endswith(".json")]
                    if names:
                        os.remove(os.path.join(a.path("control"), names[0]))
                        time.sleep(1.5)
                        with open(os.path.join(a.path("control"), names[0][:-5] + ".done"), "w") as f:
                            json.dump({"ok": True, "message": "software trigger to b0", "boards": ["b0"]}, f)
                        return
                    time.sleep(0.02)
            daq = threading.Thread(target=late_daq)
            daq.start()
            r = await self.client.post("/api/run/control", json={"cmd": "swtrigger"}, headers=CHANGE)
            daq.join()
            self.assertEqual(r.status, 200, await r.text())
        self.station.runs.active = None

    async def test_note_edit_keeps_the_sequence_step(self):
        """R15: the note edited during a sequence's run keeps "sequence ID: run k of N · step" after it."""
        await self.login("oper1")
        suffix = "sequence 20261109_120000: run 1 of 2 · threshold = 40 on b1"
        a = self.fake_run(note="before · " + suffix, note_suffix=suffix)
        r = await self.client.put("/api/note", json={"note": "HV 1500 V"}, headers=CHANGE)
        self.assertEqual(r.status, 200)
        self.assertEqual(a.note, "HV 1500 V · " + suffix)
        self.station.runs.active = None

    async def test_following_does_not_hold_up_the_server(self):
        """R11: the run's files are read in a worker thread: a slow NAS does not hold up the other requests."""
        a = self.fake_run()
        with open(a.path("daq.log"), "w") as f:
            f.write("$ python -m vx27webdaq.daq run\n")
        a.log_file = open(a.path("daq.log"), "rb")
        real = runs_module.RunManager._read_status

        def slow(manager, run):
            time.sleep(1.0)
            return real(manager, run)
        with mock.patch.object(runs_module.RunManager, "_read_status", slow):
            tick = asyncio.ensure_future(self.station.runs.tick())
            t0 = time.monotonic()
            await asyncio.sleep(0.2)
            r = await self.client.get("/api/me")
            self.assertLess(time.monotonic() - t0, 0.8)
            self.assertEqual(r.status, 200)
            await tick
        self.station.runs.active = None

    async def test_live_boards(self):
        """J3: /api/live/boards: each board's channels read out and in its board trigger, its sample time, and where
        the boards come from."""
        res = await (await self.client.get("/api/live/boards")).json()
        self.assertEqual((res["source"], res["run_dir"]), ("configuration", None))
        self.assertEqual([(b["name"], b["channels"], b["read_out"], b["in_trigger"], b["sample_ns"]) for b in res["boards"]],
                         [("b0", 64, [0, 1, 2, 3], [0, 1, 2, 3], 8.0), ("b1", 32, [0, 1, 2, 3], [0, 1, 2, 3], 2.0)])
        self.station.cfg.trigger.setup = "external"                      # the boards take their trigger on TRG-IN
        res = await (await self.client.get("/api/live/boards")).json()
        self.assertEqual([b["in_trigger"] for b in res["boards"]], [[], []])
        a = self.fake_run()
        res = await (await self.client.get("/api/live/boards")).json()
        self.assertEqual((res["source"], res["run_dir"]), ("run", a.run_dir))
        # J12: Tune… starts from the values the run has (changed during it), not those of the configuration edited now
        a.cfg = C.from_dict(C.to_dict(a.cfg))
        a.cfg.board("b0").channels[2].dc_offset, a.cfg.board("b0").channels[2].threshold = 33.5, 777
        res = await (await self.client.get("/api/live/boards")).json()
        b0 = res["boards"][0]
        self.assertEqual((len(b0["dc_offset"]), len(b0["threshold"]), b0["dc_offset"][2], b0["threshold"][2]), (64, 64, 33.5, -777))
        self.assertNotEqual(self.station.cfg.board("b0").channels[2].threshold, 777)
        self.station.runs.active = None

    def test_last_run_is_the_newest_folder(self):
        """R12: the last run is the run folder of the latest date and time, not the one whose run.json changed last
        (the converter writes older runs' run.json later)."""
        runs = self.station.settings.runs_folder()
        for name, mtime in (("20261101_100000", 2_000_000_000), ("20261109_100000", 1_000_000_000)):
            d = os.path.join(runs, name)
            os.makedirs(d)
            C.save_config(self.station.cfg, os.path.join(d, "config.json"))
            with open(os.path.join(d, "run.json"), "w") as f:
                json.dump({"run_name": name, "started": "2026-11-09 10:00:00", "phase": "finished"}, f)
            os.utime(os.path.join(d, "run.json"), (mtime, mtime))
        self.station.runs.load_last()
        self.assertEqual(os.path.basename(self.station.runs.last["run_dir"]), "20261109_100000")

    async def test_signed_thresholds(self):
        """Thresholds as the board gets them (negative for negative pulses, like CAEN's TriggerThr): the live boards
        give them so, a change during the run in the other direction is refused before anything goes to the DAQ
        program, and a threshold scan takes signed values (the sign is the pulse direction)."""
        await self.login("oper1")
        a = self.fake_run()
        a.cfg = C.from_dict(C.to_dict(a.cfg))
        a.cfg.board("b0").channels[1].positive, a.cfg.board("b0").channels[1].threshold = True, 55
        b0 = (await (await self.client.get("/api/live/boards")).json())["boards"][0]
        self.assertEqual(b0["threshold"][:2], [-a.cfg.board("b0").channels[0].threshold, 55])
        for channel, value in ((0, 120), (1, -120)):
            r = await self.client.post("/api/run/control", json={"cmd": "set", "board": "b0", "channel": channel,
                                                               "what": "threshold", "value": value}, headers=CHANGE)
            self.assertEqual(r.status, 400, (channel, value))
            self.assertIn("pulse direction", await r.text())
        self.assertEqual(os.listdir(a.path("control")), [])               # nothing went to the DAQ program
        self.station.runs.active = None
        labels, cfgs = self.station.sequence.plan({"kind": "scan", "target": "threshold", "board": "b1",
                                                   "values": "-40, 90, 0", "duration_s": 1})
        self.assertEqual(labels, ["threshold = -40 on b1", "threshold = 90 on b1", "threshold = 0 on b1"])
        self.assertEqual([(c.board("b1").channels[3].threshold, c.board("b1").channels[3].positive) for c in cfgs],
                         [(40, False), (90, True), (0, self.station.cfg.board("b1").channels[3].positive)])

    def test_plan_checks_each_configuration_once(self):
        """R20: a sequence of 1000 repeats is checked once, not 1000 times."""
        with mock.patch.object(C, "check_config", wraps=C.check_config) as check:
            labels, _ = self.station.sequence.plan({"kind": "repeat", "count": 1000, "duration_s": 1})
        self.assertEqual(len(labels), 1000)
        self.assertLessEqual(check.call_count, 2)


class RestartRecordTests(FixTestCase):
    """What a new server finds of the run of the server before it."""

    def make_run(self, phase="finished", wrapper_pid=0, log_bytes=0, status_time=None) -> str:
        run_dir = os.path.join(self.root, "runs", "20261109_130000")
        os.makedirs(run_dir, exist_ok=True)
        C.save_config(self.station.cfg, os.path.join(run_dir, "config.json"), run_name="run00001", run_folder=run_dir)
        with open(os.path.join(run_dir, "daq.log"), "w") as f:
            f.write("$ python -m vx27webdaq.daq run\n")
            line = "2026-11-09 13:00:00 b0: some output of the DAQ program, long enough to fill the log quickly\n"
            for _ in range(log_bytes // len(line)):
                f.write(line)
        with open(os.path.join(run_dir, "status.json"), "w") as f:
            json.dump({"phase": phase, "time": status_time or time.time(), "boards": [], "changes": []}, f)
        if phase in ("finished", "failed"):
            with open(os.path.join(run_dir, "daq.exit"), "w") as f:
                f.write("0\n")
        return run_dir

    def write_current(self, run_dir, **extra):
        from vx27webdaq.web.settings import write_atomic
        write_atomic(self.station.state_dir / "current_run.json",
                     json.dumps({"run_dir": run_dir, "wrapper_pid": 0, "started": time.time(), "started_by": "oper1",
                                 "simulator": True, "command": [], "note": "", "run_name": "run00001", **extra}))

    async def restart(self):
        await self.client.close()
        self.client = await self.start_server()

    async def test_broken_record_does_not_stop_the_server(self):
        """R10: a current_run.json the server cannot use is logged; the server starts."""
        from vx27webdaq.web.settings import write_atomic
        write_atomic(self.station.state_dir / "current_run.json", "[1, 2]")
        await self.restart()
        self.assertEqual((await self.client.get("/api/state")).status, 200)
        self.assertTrue(any("could not follow" in e["action"] for e in self.station.activity_log))
        self.write_current(os.path.join(self.root, "runs", "x"), wrapper_pid="abc", started="yesterday")
        await self.restart()
        self.assertEqual((await self.client.get("/api/state")).status, 200)

    async def test_run_folder_readable_later_is_followed(self):
        """R10: a run folder that cannot be read at the start (the NAS not mounted yet) is tried again while
        current_run.json is there; the run that ended meanwhile gets its end time from its status."""
        run_dir = os.path.join(self.root, "runs", "20261109_130000")
        self.write_current(run_dir)
        await self.restart()
        self.assertIsNone(self.station.runs.active)
        self.assertIsNotNone(self.station.runs._pending)
        ended = time.time() - 3600                                       # it ended an hour ago, unwatched
        self.make_run(status_time=ended)
        await self.wait(lambda: asyncio.sleep(0, os.path.exists(os.path.join(run_dir, "run.json"))), 15, "run.json")
        with open(os.path.join(run_dir, "run.json")) as f:
            summary = json.load(f)
        self.assertEqual(summary["ended"], time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ended)))
        self.assertFalse(os.path.exists(self.station.state_dir / "current_run.json"))

    async def test_long_log_after_a_restart(self):
        """R20: after a restart the pages get the end of a long daq.log, not all of it in one message."""
        import sys
        fake = await asyncio.create_subprocess_exec(sys.executable, "-c", "import time; time.sleep(60)",
                                                    "vx27webdaq.web.wrapper")     # a wrapper that is still running
        try:
            run_dir = self.make_run(phase="running", log_bytes=3 << 20)
            self.write_current(run_dir, wrapper_pid=fake.pid)
            await self.restart()
            st = self.station
            await self.wait(lambda: asyncio.sleep(0, st.runs.active is not None and len(st.runs.lines) > 100), 10, "the log")
            self.assertLess(sum(len(line[1]) for line in st.runs.lines), 1_200_000)
            self.assertTrue(st.runs.lines[0][1].startswith("…"), st.runs.lines[0])
        finally:
            fake.kill()
            await fake.wait()


class RunLifecycleTests(FixTestCase):
    duration = 2

    async def test_run_ends_when_run_json_cannot_be_written(self):
        """R1: a run.json that cannot be written (a full NAS) does not keep the run active for ever: the run ends, its
        summary goes into the state folder, its files are converted."""
        from vx27webdaq.web import settings as settings_module
        real = settings_module.write_atomic

        def full_nas(path, text, private=True):
            if str(path).endswith(os.sep + "run.json") and "run_records" not in str(path):
                raise OSError(28, "No space left on device")
            return real(path, text, private)
        await self.login("oper1")
        with mock.patch.object(runs_module, "write_atomic", full_nas):
            run_dir = await self.start_run()
            await self.wait(self.ended, 40, "the end of the run")
        self.assertFalse(os.path.exists(os.path.join(run_dir, "run.json")))
        copy = self.station.state_dir / "run_records" / (os.path.basename(run_dir) + ".json")
        with open(copy) as f:
            summary = json.load(f)
        self.assertTrue(any("No space left" in e for e in summary["errors"]), summary["errors"])
        self.assertFalse(os.path.exists(self.station.state_dir / "current_run.json"))
        r = await self.client.post("/api/run/start", json={}, headers=CHANGE)    # the next run starts
        self.assertEqual(r.status, 200, await r.text())
        await self.wait(self.ended, 40, "the end of the second run")

    async def test_relative_data_folder_is_kept_absolute(self):
        """R13: a relative data folder goes into the run's configuration as the absolute folder the check meant (the
        DAQ program runs in the run folder)."""
        await self.login("oper1")
        self.station.cfg.run.data_dir = os.path.relpath(os.path.join(self.root, "data"))
        run_dir = await self.start_run()
        cfg, _ = C.load_config(os.path.join(run_dir, "config.json"))
        self.assertEqual(cfg.run.data_dir, os.path.join(self.root, "data", "simulated"))
        await self.wait(self.ended, 40, "the end of the run")

    async def test_sequence_start_failure_and_busy_boards(self):
        """R3: a sequence whose first run cannot start says so (409); a later run that finds the boards busy waits and
        starts when they are free. R16: every run of a sequence is simulated like the first, whatever the setting
        says meanwhile."""
        await self.login("oper1")
        st = self.station
        good = st.settings.runs_dir
        st.settings.runs_dir = os.path.join(self.conf, "runs")          # under a file: the run folder cannot be made
        r = await self.client.post("/api/sequence/start", json={"kind": "repeat", "count": 2, "duration_s": 1}, headers=CHANGE)
        self.assertEqual(r.status, 409)
        self.assertIn("could not start", await r.text())
        st.settings.runs_dir = good
        r = await self.client.post("/api/sequence/start", json={"kind": "repeat", "count": 2, "duration_s": 1}, headers=CHANGE)
        self.assertEqual(r.status, 200, await r.text())

        async def first_done():
            s = st.sequence.view()
            return s["runs"] and s["runs"][0]["phase"] != "running" and st.runs.active is None
        await self.wait(first_done, 40, "the end of the first run")
        st.boards.param_busy = True                                     # Send to boards, in the pause
        st.settings.use_simulator = False                               # the setting changes: the sequence keeps its own
        await self.wait(lambda: asyncio.sleep(0, st.sequence.view()["message"].startswith("waiting")), 15, "waiting")
        self.assertEqual(st.sequence.view()["state"], "running")
        st.boards.param_busy = False
        await self.wait(lambda: asyncio.sleep(0, st.sequence.view()["state"] != "running"), 60, "the end of the sequence")
        s = st.sequence.view()
        self.assertEqual((s["state"], [x["phase"] for x in s["runs"]]), ("finished", ["finished", "finished"]))
        self.assertTrue(all(x["run_dir"].endswith("_simulator") for x in s["runs"]), s["runs"])


class RestartDuringRunTests(FixTestCase):
    duration = 6

    async def test_changes_and_stop_survive_a_restart(self):
        """R10: a threshold changed during the run is still the run's after a restart (and in its NAME.json files);
        R9: a Stop sent before the restart is known after it, and a second Stop sends no second SIGTERM."""
        await self.login("oper1")
        await self.start_run()

        async def running():
            return (await self.run_state()).get("phase") == "running"
        await self.wait(running, 15, "the run to go")
        r = await self.client.post("/api/run/control", json={"cmd": "set", "board": "b1", "channel": 2, "what": "threshold",
                                                           "value": -150, "keep": False}, headers=CHANGE)
        self.assertEqual(r.status, 200, await r.text())
        await asyncio.sleep(1)                                           # into status.json
        await self.client.close()
        self.client = await self.start_server()
        a = self.station.runs.active
        self.assertEqual(a.cfg.board("b1").channels[2].threshold, 150)
        await self.login("oper1")
        kills = []
        real = os.kill

        def counting(pid, sig):
            kills.append(sig)
            return real(pid, sig)
        with mock.patch.object(runs_module.os, "kill", counting):
            for _ in range(2):
                r = await self.client.post("/api/run/stop", headers=CHANGE)
                self.assertEqual(r.status, 200, await r.text())
        self.assertEqual(kills.count(15), 1)                             # one SIGTERM
        await self.client.close()                                        # and a restart after the Stop
        self.client = await self.start_server()
        self.assertIsNotNone(self.station.runs.active.stop_sent_at)
        await self.wait(self.ended, 30, "the end of the run")
        for path in self.station.runs.last["data_files"]:
            cfg, _ = C.load_config(os.path.splitext(path["path"])[0] + ".json")
            self.assertEqual(cfg.board("b1").channels[2].threshold, 150)


# ---- the wrapper and the command line ----
class WrapperTests(unittest.TestCase):
    def test_stop_while_the_daq_program_starts(self):
        """R18: a Stop that comes while the wrapper starts the DAQ program is passed on to it (it used to end the
        wrapper, leaving the DAQ program running unwatched)."""
        import subprocess
        import sys
        import tempfile
        from vx27webdaq.web.settings import PROJECT
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as d:
            pid_file, exit_file = os.path.join(d, "daq.pid"), os.path.join(d, "daq.exit")
            script = (
                "import os, signal, subprocess, sys\n"
                "from vx27webdaq.web import wrapper\n"
                "real = subprocess.Popen\n"
                "def popen(*a, **k):\n"
                "    os.kill(os.getpid(), signal.SIGTERM)       # Stop, while the DAQ program is being started\n"
                "    return real(*a, **k)\n"
                "wrapper.subprocess.Popen = popen\n"
                f"sys.exit(wrapper.main(['--pid-file', {pid_file!r}, '--exit-file', {exit_file!r}, '--', sys.executable,"
                " '-c', 'import time; time.sleep(20)']))\n")
            t0 = time.monotonic()
            p = subprocess.run([sys.executable, "-c", script], env=dict(os.environ, PYTHONPATH=str(PROJECT / "src")),
                               capture_output=True, text=True, timeout=30)
            self.assertLess(time.monotonic() - t0, 15)                   # the DAQ program got the Stop
            self.assertEqual(p.returncode, 1, p.stderr)                  # the wrapper ended by itself ...
            with open(exit_file) as f:
                self.assertEqual(f.read().strip(), "-15")                # ... and wrote how the DAQ program ended


class CommandLineTests(FixTestCase):
    async def test_warning_for_a_network_host(self):
        """A21: the server warns at its start when it listens on a network address."""
        import argparse
        import contextlib
        import io
        from vx27webdaq.web import cli
        for host, warned in (("0.0.0.0", True), ("127.0.0.1", False)):
            out, err = io.StringIO(), io.StringIO()
            with mock.patch("aiohttp.web.run_app") as run_app, contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                self.assertEqual(cli.serve(argparse.Namespace(host=host, port=8770)), 0)
            self.assertTrue(run_app.called)
            self.assertEqual("WARNING" in err.getvalue(), warned, (host, err.getvalue()))


if __name__ == "__main__":
    unittest.main()
