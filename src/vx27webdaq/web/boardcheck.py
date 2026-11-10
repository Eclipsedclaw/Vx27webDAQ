"""Check boards: what each board of the configuration says it is, through the DAQ program's check command
(python -m vx27webdaq.daq check, a process of its own, so a problem of the CAEN library never touches the
server). For every board: model, serial, firmware, channels, ADC, temperatures, and its whole parameter tree,
kept in the state folder for the Boards tool (as WaveDump2's Device Manager shows it). Not during a run: a
second connection must not disturb the boards that take data.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time

from vx27webdaq.core import config as C
from vx27webdaq.web.settings import PROJECT, write_atomic

CHECK_TIMEOUT_S = 20        # per board
PARAM_PATH = re.compile(r"^/(par|ch/\d{1,2}(\.\.\d{1,2})?/par|endpoint/par)/[A-Za-z0-9_]{1,64}$")
COMMAND = re.compile(r"^[A-Za-z0-9_]{1,64}$")


class CheckError(RuntimeError):
    """The check cannot run now (the message says why)."""


def flatten(tree, prefix: str = "") -> list[dict]:
    """The parameters of a device tree as rows {path, value, access, ...}: a node that has a value (or the
    attributes of a parameter) is one; every other node is a folder of more nodes."""
    rows = []
    if isinstance(tree, dict):
        keys = {k.lower() for k in tree}
        if "value" in keys or ("accessmode" in keys and prefix.split("/")[-2:-1] == ["par"]):
            row = {"path": prefix or "/"}
            for k, v in tree.items():
                if isinstance(v, (str, int, float, bool)) or v is None:
                    row[k.lower()] = v
                elif isinstance(v, list) and all(isinstance(x, (str, int, float)) for x in v):
                    row[k.lower()] = ", ".join(str(x) for x in v)
            rows.append(row)
            children = {k: v for k, v in tree.items() if isinstance(v, (dict, list))}
        else:
            children = tree
        for k, v in children.items():
            if isinstance(v, (dict, list)):
                rows += flatten(v, f"{prefix}/{k}")
    elif isinstance(tree, list):
        for i, v in enumerate(tree):
            rows += flatten(v, f"{prefix}/{i}")
    return rows


class BoardCheck:
    def __init__(self, station):
        self.station = station
        self.results: dict[str, dict] = {}
        self.running = False
        self.checked_at: float | None = None
        self.by = ""
        self.simulator = False
        self.trees_dir = station.state_dir / "trees"
        self.param_busy = False
        self._load()

    def _file(self):
        return self.station.state_dir / "board_check.json"

    def _load(self) -> None:
        try:
            with open(self._file(), encoding="utf-8") as f:
                data = json.load(f)
            self.results = data.get("results", {})
            self.checked_at, self.by = data.get("checked_at"), data.get("by", "")
            self.simulator = bool(data.get("simulator"))
        except (OSError, ValueError, AttributeError):
            pass

    def _save(self) -> None:
        try:
            write_atomic(self._file(), json.dumps({"results": self.results, "checked_at": self.checked_at,
                                                   "by": self.by, "simulator": self.simulator}))
        except OSError:
            pass

    def facts(self) -> dict:
        """name -> what the board said, for the configuration check (simulated answers do not count)."""
        if self.simulator:
            return {}
        return {name: r.get("facts", {}) for name, r in self.results.items() if r.get("ok")}

    def view(self) -> dict:
        rows = []
        for b in self.station.cfg.boards:
            r = self.results.get(b.name)
            rows.append({"name": b.name, "uri": b.uri, **(r or {"ok": None}),
                         "stale": bool(r and r.get("uri") != b.uri)})
        return {"running": self.running, "checked_at": self.checked_at, "by": self.by, "simulator": self.simulator,
                "results": rows}

    # ---- one user of the boards at a time ----
    def busy(self) -> str:
        """What has the boards now (a board check, Send to boards or the manual controller), or ""."""
        if self.running:
            return "a board check"
        if self.param_busy:
            return "Send to boards or the manual controller"
        return ""

    def refuse(self, during_run: str) -> None:
        """The board lock for a check, Send to boards and the manual controller: refused (CheckError) during a run, a
        run sequence (also in the pause between its runs), and while another of them has the boards."""
        st = self.station
        if st.runs.active is not None:
            raise CheckError(f"not during a run: {during_run}")
        seq = getattr(st, "sequence", None)
        if seq is not None and seq.running():
            raise CheckError("not during a run sequence (also not in the pause between its runs): stop the sequence "
                             "first, or wait until it ends")
        waiting = getattr(st.runs, "waiting_for", None)
        if waiting is not None and waiting():
            raise CheckError("the run of the server before this one may still be going (its folder cannot be read "
                             "yet): wait until the server follows it again")
        if self.busy():
            raise CheckError(f"the boards are busy with {self.busy()}: try again in a moment")

    @staticmethod
    def check_names(names: list[str]) -> None:
        """Board names go into file names (the parameter trees, the data files): only names of the configuration's
        rule, each once."""
        seen = set()
        for k, name in enumerate(names):
            if not isinstance(name, str) or not C.NAME_RE.match(name):
                raise CheckError(f"board {k + 1} has no usable name ({str(name)[:40]!r}): a name has letters, digits, "
                                 "_ . - (at most 32, a letter or digit first); fix it on the Configuration page")
            if name in seen:
                raise CheckError(f"two boards are called {name}: give each board its own name on the Configuration page")
            seen.add(name)

    @staticmethod
    async def _end_process(proc) -> None:
        """A program of the boards still running when its task ends (a time-out, or the server stops): killed and
        waited for, so it never goes on talking to the boards behind the next run."""
        if proc is None or proc.returncode is not None:
            return
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(proc.wait(), 5)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass

    # ---- Check boards ----
    def begin_check(self, user: str) -> dict:
        """Start a check now or refuse it (CheckError): what it needs is taken from the configuration at once (a board
        removed or renamed meanwhile changes nothing), and the check is marked as going."""
        st = self.station
        self.refuse("the run's own table shows the boards")
        boards = [(b.name, b.uri) for b in st.cfg.boards]
        if not boards:
            raise CheckError("the configuration has no board")
        self.check_names([n for n, _ in boards])
        plan = {"boards": boards, "cfg": st.cfg.copy(), "simulator": st.settings.use_simulator,
                "options": st.settings.simulator_options, "env": st.settings.lib_env()}
        self.running, self.by, self.simulator = True, user or "guest", plan["simulator"]
        for name, uri in boards:
            self.results[name] = {"ok": None, "checking": True, "uri": uri}
        self.publish()
        return plan

    async def check(self, user: str) -> None:
        await self.run_check(user, self.begin_check(user))

    async def run_check(self, user: str, plan: dict) -> None:
        st = self.station
        names, uris = [n for n, _ in plan["boards"]], dict(plan["boards"])
        proc = None
        try:                                # from here on everything ends in the finally: the check is never left going
            path = str(st.state_dir / "check_config.json")
            C.save_config(plan["cfg"], path)
            cmd = [sys.executable, "-m", "vx27webdaq.daq", "check", path, "--trees", str(self.trees_dir)]
            if plan["simulator"]:
                cmd += ["--simulate", plan["options"]]
            env = dict(os.environ, PYTHONPATH=str(PROJECT / "src"), PYTHONUNBUFFERED="1", OMP_NUM_THREADS="1", **plan["env"])
            st.activity(user, "checked the boards", ", ".join(names))
            proc = await asyncio.create_subprocess_exec(*cmd, stdin=asyncio.subprocess.DEVNULL, env=env,
                                                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            deadline = time.monotonic() + CHECK_TIMEOUT_S * len(names) + 5
            extra = []
            while True:
                left = deadline - time.monotonic()
                if left <= 0:
                    proc.kill()
                    break
                try:
                    line = await asyncio.wait_for(proc.stdout.readline(), left)
                except asyncio.TimeoutError:
                    proc.kill()
                    break
                if not line:
                    break
                text = line.decode("utf-8", "replace").strip()
                try:
                    r = json.loads(text)
                except ValueError:
                    extra.append(text)          # what the CAEN library printed, kept for a failed board
                    continue
                if not isinstance(r, dict):
                    continue
                if r.get("name") in uris:
                    if not r.get("ok") and extra:
                        r["error"] = (str(r.get("error", "")) + " · " + " ".join(extra[-3:])).strip(" ·")
                    self.results[r["name"]] = r
                    self.publish()
                elif r.get("error"):
                    for name in names:
                        self.results[name] = {"ok": False, "error": r["error"], "uri": uris[name]}
            await proc.wait()
            for name in names:
                if self.results.get(name, {}).get("checking"):
                    self.results[name] = {"ok": False, "uri": uris[name],
                                          "error": "no answer within the time limit" + (
                                              f" ({' '.join(extra[-2:])})" if extra else "")}
        except OSError as err:
            for name in names:
                self.results[name] = {"ok": False, "error": f"cannot start the check: {err}", "uri": uris[name]}
        finally:
            await self._end_process(proc)
            self.running = False
            for name in names:                  # cancelled (the server stops) in the middle: not "checking" for ever
                if self.results.get(name, {}).get("checking"):
                    self.results[name] = {"ok": False, "uri": uris[name], "error": "the check was stopped"}
            self.checked_at = time.time()
            self._save()
            self.publish()
            st.config_changed()             # the configuration check compares with what the boards said

    # ---- Send to boards ----
    async def apply(self, user: str) -> dict:
        """Send to boards: the configuration being edited, to every board switched on, now (python -m vx27webdaq.daq
        apply): reset, then every parameter, as at the start of a run, without taking data. Not during a run."""
        st = self.station
        self.refuse("the run sent the configuration when it started")
        bad = [p for p in C.check_config(st.cfg, check_paths=False, facts=self.facts()) if p.level == "error"]
        if bad:
            raise C.ConfigError("the configuration has errors: " + "; ".join(f"{p.where}: {p.message}" for p in bad))
        names = [b.name for _, b in st.cfg.active()]
        self.check_names(names)
        cfg = st.cfg.copy()
        simulator, options, lib = st.settings.use_simulator, st.settings.sim_options(), st.settings.lib_env()
        self.param_busy = True
        proc = None
        try:
            cfg_path = str(st.state_dir / "apply_config.json")
            C.save_config(cfg, cfg_path)
            cmd = [sys.executable, "-m", "vx27webdaq.daq", "apply", cfg_path]
            if simulator:
                cmd += ["--simulate", options]
            env = dict(os.environ, PYTHONPATH=str(PROJECT / "src"), PYTHONUNBUFFERED="1", OMP_NUM_THREADS="1", **lib)
            proc = await asyncio.create_subprocess_exec(*cmd, stdin=asyncio.subprocess.DEVNULL, env=env,
                                                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            try:
                out, _ = await asyncio.wait_for(proc.communicate(), CHECK_TIMEOUT_S * max(1, len(names)) + 10)
            except asyncio.TimeoutError:
                raise CheckError("the boards did not answer in time") from None
        finally:
            await self._end_process(proc)
            self.param_busy = False
        results, extra = [], []
        for line in out.decode("utf-8", "replace").splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                if line.strip():
                    extra.append(line.strip())       # what the CAEN library printed
                continue
            if not isinstance(r, dict):
                continue
            if "name" in r:
                results.append(r)
            elif r.get("error"):
                raise CheckError(r["error"])
        for name in names:
            if not any(r["name"] == name for r in results):
                results.append({"name": name, "ok": False, "sent": 0, "warnings": [],
                                "error": "no answer" + (f" ({' '.join(extra[-2:])})" if extra else "")})
        good = sum(1 for r in results if r["ok"])
        st.activity(user, "sent the configuration to the boards", f"{good} of {len(results)} took it")
        return {"results": results, "simulator": simulator}

    # ---- the manual controller ----
    async def param(self, user: str, board: str, action: str, path: str, value: str = "") -> dict:
        """The manual controller (WaveDump2's): get or set one parameter of an idle board, or send it a command,
        through python -m vx27webdaq.daq param. The next run resets the board and sets it from the configuration."""
        st = self.station
        self.refuse("the DAQ program has the boards")
        if not C.NAME_RE.match(board or "") or st.cfg.board(board) is None:
            raise CheckError(f"there is no board {board[:40]} in the configuration")
        if action in ("get", "set") and not PARAM_PATH.match(path):
            raise ValueError(f"{path}: a parameter is /par/NAME or /ch/N/par/NAME")
        if action == "cmd" and not COMMAND.match(path):
            raise ValueError(f"{path}: a command is a name, e.g. Reset (sent as /cmd/NAME)")
        if action not in ("get", "set", "cmd"):
            raise ValueError("the action is get, set or cmd")
        value = str(value).strip()
        if action == "set" and (not value or len(value) > 200 or "\n" in value):
            raise ValueError("give the value to set (one line)")
        cfg = st.cfg.copy()
        simulator, options, lib = st.settings.use_simulator, st.settings.simulator_options, st.settings.lib_env()
        self.param_busy = True
        proc = None
        try:
            cfg_path = str(st.state_dir / "param_config.json")
            C.save_config(cfg, cfg_path)
            cmd = [sys.executable, "-m", "vx27webdaq.daq", "param", cfg_path, "--board", board]
            cmd += {"get": ["--get", path], "set": ["--set", path, value], "cmd": ["--cmd", path]}[action]
            if simulator:
                cmd += ["--simulate", options]
            env = dict(os.environ, PYTHONPATH=str(PROJECT / "src"), PYTHONUNBUFFERED="1", OMP_NUM_THREADS="1", **lib)
            proc = await asyncio.create_subprocess_exec(*cmd, stdin=asyncio.subprocess.DEVNULL, env=env,
                                                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            try:
                out, _ = await asyncio.wait_for(proc.communicate(), CHECK_TIMEOUT_S)
            except asyncio.TimeoutError:
                raise CheckError(f"{board} did not answer within {CHECK_TIMEOUT_S} s") from None
        finally:
            await self._end_process(proc)
            self.param_busy = False
        lines = out.decode("utf-8", "replace").strip().splitlines()
        res = None
        for line in reversed(lines):
            try:
                res = json.loads(line)
                break
            except ValueError:
                continue
        if not isinstance(res, dict):
            res = {"ok": False, "error": " ".join(lines[-3:]) or "no answer"}
        if action != "get":
            what = f"{path} = {value}" if action == "set" else f"command {path}"
            st.activity(user, "manual controller", f"{board}: {what}: {'ok' if res.get('ok') else res.get('error', 'refused')}")
        res["simulator"] = simulator
        return res

    def tree(self, name: str) -> dict:
        if not C.NAME_RE.match(name or ""):             # it names a file: trees/NAME.json
            raise ValueError("a board's name has letters, digits, _ . - (at most 32)")
        if self.station.cfg.board(name) is None and name not in self.results:
            raise FileNotFoundError(f"there is no board {name}")
        path = self.trees_dir / f"{name}.json"
        try:
            with open(path, encoding="utf-8") as f:
                tree = json.load(f)
        except FileNotFoundError:
            raise FileNotFoundError(f"no parameter tree of {name} yet: press Check boards") from None
        except ValueError as err:
            raise ValueError(f"the saved tree of {name} cannot be read: {err}") from None
        rows = flatten(tree)
        return {"name": name, "rows": rows, "saved": os.path.getmtime(path),
                "simulator": self.simulator, "facts": self.results.get(name, {}).get("facts", {})}

    def publish(self) -> None:
        self.station.publish({"type": "boards", "boards": self.view()})
