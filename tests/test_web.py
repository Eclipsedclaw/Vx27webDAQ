"""The web server, without a browser: accounts and roles, whole runs with simulated boards, Stop and Kill, a
server restart during a run, the board check, ROOT conversion, data files, the configuration, and the page.

Every test has its own settings, accounts and folders in .test_tmp/web/<test>.
"""

import asyncio
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time
import unittest

import numpy as np
from aiohttp.test_utils import TestClient, TestServer

from alab_daq.core import config as C
from alab_daq.core import rawdata as R
from alab_daq.web import runs as runs_module
from alab_daq.web.accounts import AccountError, Accounts, check_password, hash_password
from alab_daq.web.boardcheck import flatten
from alab_daq.web.files import board_lines, envelope, event_summary, measure
from alab_daq.web.settings import PROJECT, ServerSettings, save_settings
from alab_daq.web.station import Station

TMP = os.environ.get("ALAB_DAQ_TEST_TMP", os.path.join(os.path.dirname(__file__), "..", ".test_tmp"))
STATIC = PROJECT / "src" / "alab_daq" / "web" / "static"
CHANGE = {"X-ALAB": "1"}
NOTE = "Cs-137 源 at 10 cm\nHV 1450 V"
PASSWORD = "correct horse 42"


class ViewTests(unittest.TestCase):
    def test_envelope(self):
        s = np.full(32042, 8500, dtype=np.uint16)
        s[12345] = 9000
        e = envelope(s, 0, None, 400)
        self.assertEqual(e["mode"], "envelope")
        self.assertIn(9000, e["y"])
        r = envelope(s, 12300, 12400, 400)
        self.assertEqual((r["mode"], r["x0"], len(r["y"]), r["y"][45]), ("raw", 12300, 100, 9000))

    def test_measure_uses_the_sample_time(self):
        s = np.full(100, 100, dtype=np.uint16)
        s[30], s[31] = 50, 75
        m = measure(s, 25, 40, 100.0, 8.0)
        self.assertEqual((m["n"], m["min"], m["peak"], m["peak_at"]), (15, 50, -50.0, 30))
        self.assertAlmostEqual(m["area"], (-50 - 25) * 8.0)             # ADC x 8 ns per sample (V2740)

    def test_lines(self):
        cfg = C.default_config()
        cfg.trigger.setup = "self"
        b = cfg.boards[1]                                              # a V2730
        b.channels[2].positive, b.channels[2].threshold = True, 40
        info = board_lines(cfg, b)
        self.assertEqual((info["sample_ns"], info["adc_max"], info["record_length"]), (2.0, 16383, b.record_length))
        self.assertEqual(info["lines"][2], {"trigger_at": b.pre_trigger, "threshold": 40, "threshold_used": True})
        self.assertEqual(info["lines"][0]["threshold"], -100)
        cfg.trigger.setup = "external"
        self.assertFalse(board_lines(cfg, b)["lines"][0]["threshold_used"])

    def test_summary(self):
        w = np.full((2, 64), 1000, dtype=np.uint16)
        w[1, 30] = 600
        ev = R.ScopeEvent(0, 0, 0, 0, False, 0b101, [0, 2], w)
        s = event_summary([ev], 4)
        self.assertEqual((s["baseline"][0], s["noise"][0], s["peak"][2], s["baseline"][1]), (1000.0, 0.0, -400.0, None))

    def test_flatten(self):
        tree = {"par": {"modelname": {"value": "V2740", "accessmode": "READ_ONLY"}},
                "ch": {"0": {"par": {"chenable": {"value": "True", "allowedvalues": ["True", "False"]}}}}}
        rows = flatten(tree)
        self.assertEqual([r["path"] for r in rows], ["/par/modelname", "/ch/0/par/chenable"])
        self.assertEqual(rows[1]["allowedvalues"], "True, False")


class AccountTests(unittest.TestCase):
    def test_accounts(self):
        d = os.path.join(TMP, "web", "accounts")
        shutil.rmtree(d, ignore_errors=True)
        h = hash_password("secret-pass")
        self.assertTrue(check_password("secret-pass", h))
        self.assertFalse(check_password("secret-pas", h))
        acc = Accounts(os.path.join(d, "users.json"))
        acc.add("Alice", "long enough 1", "operator")
        self.assertEqual(acc.role("alice"), "operator")
        with self.assertRaises(AccountError):
            acc.add("alice", "long enough 2", "viewer")
        with self.assertRaises(AccountError):
            acc.add("bob", "short", "viewer")


class WebTestCase(unittest.IsolatedAsyncioTestCase):
    """A server of its own for every test, with simulated boards."""

    convert_to_root = False
    duration = 3
    options = "--rate 30 --seed 3"

    async def asyncSetUp(self):
        self.root = os.path.abspath(os.path.join(TMP, "web", self.id().rsplit(".", 1)[-1]))
        shutil.rmtree(self.root, ignore_errors=True)
        os.makedirs(os.path.join(self.root, "data"))
        self._env = {k: os.environ.get(k) for k in ("XDG_CONFIG_HOME", "XDG_STATE_HOME")}
        os.environ["XDG_CONFIG_HOME"] = os.path.join(self.root, "config")
        os.environ["XDG_STATE_HOME"] = os.path.join(self.root, "state")
        cfg = C.default_config()
        cfg.run.duration_s = self.duration
        cfg.run.data_dir = os.path.join(self.root, "data")
        cfg.trigger.setup = "self"
        for b in cfg.boards:
            b.record_length, b.pre_trigger = (64, 16) if "2740" in b.model else (256, 64)
            for c, ch in enumerate(b.channels):
                ch.enabled = c < 4
        self.conf = os.path.join(self.root, "test.json")
        C.save_config(cfg, self.conf, note="")
        save_settings(ServerSettings(config_file=self.conf, use_simulator=True, simulator_options=self.options,
                                     runs_dir=os.path.join(self.root, "runs"), refresh_ms=200,
                                     convert_to_root=self.convert_to_root, caen_lib_dir=os.path.join(self.root, "nolib")))
        self.client = await self.start_server()
        acc = self.station.accounts
        acc.add("admin1", PASSWORD, "admin")
        acc.add("oper1", PASSWORD, "operator")
        acc.add("view1", PASSWORD, "viewer")

    async def start_server(self):
        from alab_daq.web.server import create_app
        self.station = Station()
        client = TestClient(TestServer(create_app(self.station)))
        await client.start_server()
        return client

    async def asyncTearDown(self):
        await self.client.close()
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    async def login(self, name, password=PASSWORD, client=None):
        r = await (client or self.client).post("/api/login", json={"name": name, "password": password}, headers=CHANGE)
        self.assertEqual(r.status, 200, await r.text())

    async def wait(self, condition, timeout_s, what="it"):
        end = time.time() + timeout_s
        while time.time() < end:
            if await condition():
                return
            await asyncio.sleep(0.1)
        self.fail(f"{what} did not happen within {timeout_s} s")

    async def run_state(self):
        return (await (await self.client.get("/api/state")).json())["run"]

    async def start_run(self, note=None):
        r = await self.client.post("/api/run/start", json={"note": note} if note is not None else {}, headers=CHANGE)
        self.assertEqual(r.status, 200, await r.text())
        return (await r.json())["run_dir"]

    async def ended(self):
        return not (await self.run_state())["active"]


class RoleTests(WebTestCase):
    async def test_roles(self):
        r = await self.client.get("/api/state")
        self.assertEqual(r.status, 200)                                 # guests may look
        r = await self.client.post("/api/run/start", json={}, headers=CHANGE)
        self.assertEqual(r.status, 401)
        await self.login("view1")
        self.assertEqual((await self.client.post("/api/run/start", json={}, headers=CHANGE)).status, 403)
        self.assertEqual((await self.client.post("/api/boards/check", headers=CHANGE)).status, 403)
        self.assertEqual((await self.client.get("/api/users")).status, 403)
        await self.login("oper1")
        self.assertEqual((await self.client.get("/api/run/preview")).status, 200)
        self.assertEqual((await self.client.get("/api/users")).status, 403)
        await self.login("admin1")
        self.assertEqual((await self.client.get("/api/users")).status, 200)

    async def test_page_header_and_host(self):
        await self.login("oper1")
        r = await self.client.post("/api/run/start", json={})            # no X-ALAB header: not from the page
        self.assertEqual(r.status, 403)
        r = await self.client.get("/api/state", headers={"Host": "evil.example:80"})
        self.assertEqual(r.status, 403)


class RunTests(WebTestCase):
    convert_to_root = True

    async def test_whole_run(self):
        await self.login("oper1")
        p = await (await self.client.get("/api/run/preview")).json()
        self.assertEqual((p["errors"], p["boards"], p["simulator"]), ([], 2, True))
        run_dir = await self.start_run(NOTE)
        self.assertTrue(os.path.isfile(os.path.join(run_dir, "config.json")))
        r = await self.client.post("/api/run/start", json={}, headers=CHANGE)
        self.assertEqual(r.status, 409)                                  # one run at a time

        async def live():
            v = await (await self.client.get("/api/live/waveforms", params={"board": "b1", "channels": "0-3"})).json()
            return v.get("channels") and v["channels"][0].get("present")
        await self.wait(live, 15, "live waveforms of b1")
        v = await (await self.client.get("/api/live/waveforms", params={"board": "b1", "channels": "0-5", "m0": "10", "m1": "40"})).json()
        self.assertEqual((v["settings"]["sample_ns"], v["settings"]["adc_max"], v["n_channels"]), (2.0, 16383, 32))
        self.assertEqual(len(v["channels"]), 6)
        self.assertEqual(v["channels"][1]["nsamps"], 256)
        self.assertEqual(v["channels"][1]["measure"]["n"], 30)
        self.assertFalse(v["channels"][5]["present"])                    # not read out
        same = await (await self.client.get("/api/live/waveforms", params={"board": "b1", "since": v["seq"]})).json()
        self.assertTrue(same.get("same") or same.get("seq") != v["seq"])
        ov = await (await self.client.get("/api/live/overview")).json()
        self.assertEqual([b["name"] for b in ov["boards"]], ["b0", "b1"])
        await self.wait(self.ended, 30, "the end of the run")
        run = await self.run_state()
        last = run["last"]
        self.assertEqual(last["phase"], "finished", last)
        with open(os.path.join(run_dir, "run.json"), encoding="utf-8") as f:
            summary = json.load(f)
        self.assertEqual((summary["phase"], summary["note"], summary["started_by"]), ("finished", NOTE, "oper1"))
        self.assertEqual([b["ended"] for b in summary["boards"]], ["stop record", "stop record"])
        files = summary["data_files"]
        self.assertEqual(len(files), 2)
        for path in files:
            self.assertTrue(path.startswith(os.path.join(run_dir, "raw")))     # simulated data stay in the run folder
            with open(os.path.splitext(path)[0] + ".json", encoding="utf-8") as f:
                side = json.load(f)
            self.assertEqual(side["note"], NOTE)
            self.assertIn("ended", side)
        # the ROOT conversion after the run
        async def converted():
            return all(os.path.exists(p[:-4] + ".root") for p in files) and not self.station.converter.busy()
        await self.wait(converted, 60, "the ROOT files")
        with open(os.path.join(run_dir, "run.json"), encoding="utf-8") as f:
            self.assertEqual({v["status"] for v in json.load(f)["root_files"].values()}, {"ok"})
        # the data files on the page
        info = await (await self.client.get("/api/files/info", params={"path": files[0]})).json()
        self.assertEqual((info["kind"], info["settings"]["board"], info["settings"]["note"]), ("raw", "b0", NOTE))
        w = await (await self.client.get("/api/files/waveforms", params={"path": files[0], "event": 2, "channels": "0,3"})).json()
        self.assertEqual([c["ch"] for c in w["channels"]], [0, 3])
        self.assertEqual(w["channels"][0]["nsamps"], 64)
        root = files[1][:-4] + ".root"
        wr = await (await self.client.get("/api/files/waveforms", params={"path": root, "event": 2, "channels": "1"})).json()
        wraw = await (await self.client.get("/api/files/waveforms", params={"path": files[1], "event": 2, "channels": "1"})).json()
        self.assertEqual(wr["channels"][0]["data"]["y"], wraw["channels"][0]["data"]["y"])     # ROOT = raw
        ov = await (await self.client.get("/api/files/overview", params={"path": root})).json()
        self.assertEqual(ov["boards"][0]["channels"], 32)
        r = await self.client.get("/api/files/info", params={"path": "/etc/passwd"})
        self.assertEqual(r.status, 403)                                  # only the data folders
        r = await self.client.post("/api/root/convert", json={"path": files[0]}, headers=CHANGE)
        self.assertEqual(r.status, 409)                                  # never over an existing ROOT file

    async def test_refused_configuration(self):
        await self.login("oper1")
        cfg = (await (await self.client.get("/api/config")).json())["config"]
        cfg["boards"][0]["uri"] = "nonsense"
        r = await self.client.put("/api/config", json={"config": cfg}, headers=CHANGE)
        payload = await r.json()
        self.assertIn("boards[0].uri", {p["where"] for p in payload["problems"] if p["level"] == "error"})
        r = await self.client.post("/api/run/start", json={}, headers=CHANGE)
        self.assertEqual(r.status, 409)
        r = await self.client.put("/api/config", json={"config": {"boards": "x"}}, headers=CHANGE)
        self.assertEqual(r.status, 400)


class StopTests(WebTestCase):
    duration = 0

    async def test_stop(self):
        await self.login("oper1")
        run_dir = await self.start_run()

        async def running():
            return (await self.run_state()).get("phase") == "running"
        await self.wait(running, 15, "the run to go")
        await asyncio.sleep(1)
        r = await self.client.post("/api/run/stop", headers=CHANGE)
        self.assertEqual(r.status, 200, await r.text())
        await self.wait(self.ended, 30, "the end after Stop")
        last = (await self.run_state())["last"]
        self.assertEqual((last["phase"], last["stop_reason"]), ("finished", "Stop (SIGTERM)"))
        with open(os.path.join(run_dir, "run.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["stopped_by"], "oper1")


class KillTests(WebTestCase):
    duration = 0
    options = "--rate 30 --fail nostop:b1"

    async def test_kill(self):
        old = runs_module.KILL_AFTER_S
        runs_module.KILL_AFTER_S = 0.5
        try:
            await self.login("oper1")
            await self.start_run()

            async def running():
                return (await self.run_state()).get("phase") == "running"
            await self.wait(running, 15, "the run to go")
            r = await self.client.post("/api/run/kill", headers=CHANGE)
            self.assertEqual(r.status, 409)                              # only after Stop
            await self.client.post("/api/run/stop", headers=CHANGE)
            await asyncio.sleep(1)                                       # b1 sends no stop record: the DAQ waits
            r = await self.client.post("/api/run/kill", headers=CHANGE)
            self.assertEqual(r.status, 200, await r.text())
            await self.wait(self.ended, 20, "the end after Kill")
            last = (await self.run_state())["last"]
            self.assertEqual(last["phase"], "failed")
            self.assertEqual(last["killed_by"], "oper1")
        finally:
            runs_module.KILL_AFTER_S = old


class RestartTests(WebTestCase):
    duration = 6

    async def test_restart_during_a_run(self):
        """The server stops during a run: the run goes on, and the next server follows it to its end."""
        await self.login("oper1")
        run_dir = await self.start_run("before the restart")

        async def running():
            return (await self.run_state()).get("phase") == "running"
        await self.wait(running, 15, "the run to go")
        await self.client.close()
        self.client = await self.start_server()
        self.assertTrue(self.station.runs.active is not None and self.station.runs.active.reattached)
        await self.wait(self.ended, 40, "the end of the run under the new server")
        with open(os.path.join(run_dir, "run.json"), encoding="utf-8") as f:
            summary = json.load(f)
        self.assertEqual((summary["phase"], summary["note"]), ("finished", "before the restart"))


class BoardCheckTests(WebTestCase):
    async def test_check_boards(self):
        await self.login("oper1")
        r = await self.client.post("/api/boards/check", headers=CHANGE)
        self.assertEqual(r.status, 200, await r.text())

        async def checked():
            b = (await (await self.client.get("/api/state")).json())["boards"]
            return not b["running"] and b["checked_at"]
        await self.wait(checked, 30, "the board check")
        b = (await (await self.client.get("/api/state")).json())["boards"]
        self.assertTrue(b["simulator"])
        facts = {x["name"]: x["facts"] for x in b["results"]}
        self.assertEqual((facts["b0"]["model"], facts["b0"]["channels"], facts["b1"]["adc_bits"]), ("V2740", 64, 14))
        tree = await (await self.client.get("/api/boards/tree", params={"name": "b1"})).json()
        paths = {row["path"] for row in tree["rows"]}
        self.assertIn("/par/recordlengths", paths)
        self.assertIn("/ch/31/par/dcoffset", paths)
        self.assertEqual((await self.client.get("/api/boards/tree", params={"name": "zz"})).status, 404)
        # not during a run
        await self.start_run()
        r = await self.client.post("/api/boards/check", headers=CHANGE)
        self.assertEqual(r.status, 409)
        await self.wait(self.ended, 30, "the end of the run")

    async def test_real_boards_without_the_library(self):
        """Without the simulator and without CAEN FELib: each board says why, the server stays up."""
        await self.login("admin1")
        r = await self.client.put("/api/settings", json={"changes": {"use_simulator": False}}, headers=CHANGE)
        self.assertEqual(r.status, 200, await r.text())
        await self.client.post("/api/boards/check", headers=CHANGE)

        async def checked():
            b = (await (await self.client.get("/api/state")).json())["boards"]
            return not b["running"] and b["checked_at"]
        await self.wait(checked, 60, "the board check")
        b = (await (await self.client.get("/api/state")).json())["boards"]
        self.assertTrue(all(x["ok"] is False and "CAEN FELib" in x["error"] for x in b["results"]), b["results"])
        settings = await (await self.client.get("/api/settings")).json()
        self.assertEqual(settings["caen_libs"], {"libCAEN_FELib.so": False, "libCAEN_Dig2.so": False})


class ConfigTests(WebTestCase):
    async def test_note_save_open_new(self):
        await self.login("oper1")
        r = await self.client.put("/api/note", json={"note": NOTE}, headers=CHANGE)
        self.assertEqual((await r.json())["note"], NOTE)
        path = os.path.join(self.root, "saved.json")
        r = await self.client.post("/api/config/save", json={"path": path}, headers=CHANGE)
        self.assertEqual(r.status, 200, await r.text())
        cfg, note = C.load_config(path)
        self.assertEqual((note, len(cfg.boards)), (NOTE, 2))
        r = await self.client.post("/api/config/save", json={"path": os.path.join(self.root, "x.conf")}, headers=CHANGE)
        self.assertEqual(r.status, 400)                                  # a configuration is a .json file
        r = await self.client.post("/api/config/new", headers=CHANGE)
        self.assertTrue((await r.json())["dirty"])
        r = await self.client.post("/api/config/open", json={"path": path}, headers=CHANGE)
        payload = await r.json()
        self.assertEqual((payload["note"], payload["dirty"], payload["path"]), (NOTE, False, path))
        self.assertEqual(len(payload["plans"]), 2)
        self.assertEqual(payload["plans"][0]["role"], "master")
        # edits survive a server restart (they are kept in the state folder)
        cfg = payload["config"]
        cfg["boards"][0]["record_length"] = 2048
        await self.client.put("/api/config", json={"config": cfg}, headers=CHANGE)
        await self.client.close()
        self.client = await self.start_server()
        self.assertEqual(self.station.cfg.boards[0].record_length, 2048)


class BrowseTests(WebTestCase):
    async def test_choosers(self):
        await self.login("oper1")
        r = await self.client.get("/api/browse", params={"dir": self.root})
        data = await r.json()
        self.assertIn("test.json", [e["name"] for e in data["entries"]])
        self.assertIn("computer", [p["label"] for p in data["places"]])
        await self.login("view1")
        self.assertEqual((await self.client.get("/api/browse", params={"dir": "/"})).status, 403)
        r = await self.client.get("/api/files/list", params={"dir": os.path.join(self.root, "data")})
        self.assertEqual(r.status, 200)
        self.assertEqual((await self.client.get("/api/files/list", params={"dir": "/"})).status, 403)


class CommandLineTests(unittest.TestCase):
    def test_user_commands(self):
        state = os.path.abspath(os.path.join(TMP, "web", "cli_state"))
        shutil.rmtree(state, ignore_errors=True)
        env = dict(os.environ, XDG_STATE_HOME=state, PYTHONPATH=str(PROJECT / "src"))
        p = subprocess.run([sys.executable, "-m", "alab_daq.web", "user", "add", "carol", "--role", "operator",
                            "--password-stdin"], input="a long password\n", capture_output=True, text=True, env=env)
        self.assertEqual(p.returncode, 0, p.stderr)
        p = subprocess.run([sys.executable, "-m", "alab_daq.web", "user", "list"], capture_output=True, text=True, env=env)
        self.assertIn("carol", p.stdout)
        p = subprocess.run([sys.executable, "-m", "alab_daq.web", "--version"], capture_output=True, text=True, env=env)
        self.assertIn("ALAB DAQ web v", p.stdout)


class PageTests(WebTestCase):
    async def test_page_files(self):
        r = await self.client.get("/")
        html = await r.text()
        self.assertEqual(r.status, 200)
        self.assertIn("ALAB DAQ", html)
        self.assertNotIn("{{version}}", html)
        self.assertIn("default-src 'self'", r.headers["Content-Security-Policy"])
        for src in re.findall(r'(?:src|href)="(/static/[^"?]+)', html):
            self.assertEqual((await self.client.get(src)).status, 200, src)
        js = glob.glob(str(STATIC / "js" / "**" / "*.js"), recursive=True)
        imports = set()
        for path in js:
            for m in re.findall(r'from "(\.{1,2}/[^"]+)"', open(path, encoding="utf-8").read()):
                imports.add(os.path.normpath(os.path.join(os.path.dirname(path), m)))
        self.assertEqual(sorted(p for p in imports if not os.path.isfile(p)), [])      # every module exists

    def test_chinese_texts(self):
        """Every text of the page that goes through t() or is marked T() has a Chinese translation, and so has
        every word the server sends for the page to show (states, statuses, roles, the parameters' reasons)."""
        js = "".join(open(p, encoding="utf-8").read() for p in glob.glob(str(STATIC / "js" / "**" / "*.js"), recursive=True))
        with open(STATIC / "i18n" / "zh.json", encoding="utf-8") as f:
            zh = json.load(f)
        used = {text.replace("\\'", "'").replace('\\"', '"')
                for _, text in re.findall(r"""\b[tT]\(\s*(["'])((?:\\.|(?!\1).)*)\1""", js)}
        from alab_daq.web.accounts import ROLES
        used |= set(ROLES) | {"data", "runs", "home", "configuration", "CAEN libraries", "computer"}     # chooser places
        used |= {"ok", "failed", "mismatch", "cancelled", "exists"}                                     # conversions
        used |= {"stop record", "time-out", "error"}                                                    # how a board ended
        cfg = C.default_config()
        for setup in C.SETUPS:                                          # the reasons in "What this board gets"
            for target in C.SOFTWARE_TARGETS:
                for source in C.PERIODIC_SOURCES:
                    for logic in C.LOGICS:
                        cfg.trigger.setup, cfg.trigger.software_target, cfg.trigger.periodic_source = setup, target, source
                        cfg.trigger.logic, cfg.trigger.busy_veto = logic, True
                        for sync in C.SYNC_MODES:
                            cfg.run.sync = sync
                            for k in range(len(cfg.boards)):
                                used |= {s.why for s in C.plan(cfg, k) if s.why}
        three = C.default_config()
        three.boards.append(C.Board("b2", "dig2://x", "V2740", channels=C.channels_for("V2740")))
        used |= {s.why for s in C.plan(three, 1) if s.why}
        missing = sorted(text for text in used if text not in zh)
        self.assertEqual(missing, [])
        unused = sorted(k for k in zh if k not in used)
        self.assertLess(len(unused), 40, unused)                         # old texts do not pile up


if __name__ == "__main__":
    unittest.main()
