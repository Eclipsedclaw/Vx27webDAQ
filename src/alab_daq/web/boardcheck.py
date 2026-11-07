"""Check boards: what each board of the configuration says it is, through the DAQ program's check command
(python -m alab_daq.daq check, a process of its own, so a problem of the CAEN library never touches the
server). For every board: model, serial, firmware, channels, ADC, temperatures, and its whole parameter tree,
kept in the state folder for the Boards tool (as WaveDump2's Device Manager shows it). Not during a run: a
second connection must not disturb the boards that take data.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time

from alab_daq.core import config as C
from alab_daq.web.settings import PROJECT, write_atomic

CHECK_TIMEOUT_S = 20        # per board


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

    async def check(self, user: str) -> None:
        st = self.station
        if self.running:
            raise CheckError("a check is going already")
        if st.runs.active is not None:
            raise CheckError("not during a run: the run's own table shows the boards")
        boards = [b.name for b in st.cfg.boards]
        if not boards:
            raise CheckError("the configuration has no board")
        self.running, self.by, self.simulator = True, user or "guest", st.settings.use_simulator
        for name in boards:
            self.results[name] = {"ok": None, "checking": True, "uri": st.cfg.board(name).uri}
        self.publish()
        path = str(st.state_dir / "check_config.json")
        C.save_config(st.cfg, path)
        cmd = [sys.executable, "-m", "alab_daq.daq", "check", path, "--trees", str(self.trees_dir)]
        if st.settings.use_simulator:
            cmd += ["--simulate", st.settings.simulator_options]
        env = dict(os.environ, PYTHONPATH=str(PROJECT / "src"), PYTHONUNBUFFERED="1", OMP_NUM_THREADS="1",
                   **st.settings.lib_env())
        st.activity(user, "checked the boards", ", ".join(boards))
        try:
            proc = await asyncio.create_subprocess_exec(*cmd, stdin=asyncio.subprocess.DEVNULL, env=env,
                                                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            deadline = time.monotonic() + CHECK_TIMEOUT_S * len(boards) + 5
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
                if "name" in r:
                    if not r.get("ok") and extra:
                        r["error"] = (r.get("error", "") + " · " + " ".join(extra[-3:])).strip(" ·")
                    self.results[r["name"]] = r
                    self.publish()
                elif r.get("error"):
                    for name in boards:
                        self.results[name] = {"ok": False, "error": r["error"], "uri": st.cfg.board(name).uri}
            await proc.wait()
            for name in boards:
                if self.results.get(name, {}).get("checking"):
                    self.results[name] = {"ok": False, "uri": st.cfg.board(name).uri,
                                          "error": "no answer within the time limit" + (
                                              f" ({' '.join(extra[-2:])})" if extra else "")}
        except OSError as err:
            for name in boards:
                self.results[name] = {"ok": False, "error": f"cannot start the check: {err}"}
        finally:
            self.running = False
            self.checked_at = time.time()
            self._save()
            self.publish()
            st.config_changed()             # the configuration check compares with what the boards said

    def tree(self, name: str) -> dict:
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
