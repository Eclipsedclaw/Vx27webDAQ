"""The run: the DAQ program (python -m alab_daq.daq run), started by an operator and followed by everyone.

The DAQ program runs apart from the server (web/wrapper.py, in a session of its own) and prints into the
run's daq.log; it writes status.json (every board's state and counters) and live/BOARD.evt (each board's newest
event). The server follows these files, so a run goes on when the server stops or restarts: a new server finds
it through current_run.json and the wrapper's pid and exit files, and carries on.

Every run gets its own folder in the save folder: config.json (the exact configuration, with the note and the
run's name), daq.log, status.json, live/, daq.pid / daq.exit (the wrapper's), and at the end run.json. Next to
every data file NAME.raw goes NAME.json: the same configuration with the note and what is known of the run, so
the settings stay with the data wherever they are copied (written when the file appears, when the note
changes, and at the end of the run). The data files hold the settings themselves too (their header).
"""

from __future__ import annotations

import asyncio
import codecs
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field

from alab_daq import __version__
from alab_daq.core import config as C
from alab_daq.core import rawdata
from alab_daq.core.system import disk_free, running_daq
from alab_daq.web.files import board_lines, channel_view, combined_view, hk_table, side_json
from alab_daq.web.settings import PROJECT, write_atomic

KILL_AFTER_S = 15           # Kill is offered when the DAQ has not ended this long after Stop
LOG_LINES = 20000           # the newest lines of the DAQ's output that a page gets when it opens
UNKNOWN_EXIT = -1000        # the wrapper ended without writing the DAQ's exit code
RUN_FILE = re.compile(r"^run(\d{5,})_")
DAQ_MARKER = "alab_daq.daq run"


class RunError(RuntimeError):
    """A run cannot be started, stopped or killed now (the message says why)."""


@dataclass
class Active:
    run_dir: str
    cfg: C.DaqConfig
    run_name: str
    command: list
    started: float
    started_by: str
    simulator: bool
    wrapper_pid: int
    proc: subprocess.Popen | None = None        # None after a server restart: the run is not our child
    log_file: object = None
    decoder: object = field(default_factory=lambda: codecs.getincrementaldecoder("utf-8")("replace"))
    partial: str = ""
    status: dict = field(default_factory=dict)
    status_mtime: int = 0
    latest: dict = field(default_factory=dict)  # board name -> (ScopeEvent, number of the live event)
    live_mtimes: dict = field(default_factory=dict)
    combined: dict = field(default_factory=dict)    # live/combined.set: {"info", "events"}
    combined_mtime: int = 0
    mbps: list = field(default_factory=list)    # [run time s, MB/s of all boards]
    stop_sent_at: float | None = None
    stopped_by: str = ""
    killed_by: str = ""
    reattached: bool = False
    last_poll: float = 0.0
    problems: list = field(default_factory=list)
    note: str = ""
    side_files: list = field(default_factory=list)      # data files that have their NAME.json

    def path(self, name: str) -> str:
        return os.path.join(self.run_dir, name)

    def data_files(self) -> list[str]:
        return [f["path"] for b in self.status.get("boards", []) for f in b.get("files", [])]


def pid_runs(pid: int | None, marker: str = "") -> bool:
    """True if process pid exists and its command line contains marker (a pid can be reused)."""
    if not pid:
        return False
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            cmdline = f.read().replace(b"\0", b" ").decode(errors="replace")
    except OSError:
        return False
    return marker in cmdline


def next_run_name(folders: list[str]) -> str:
    """run00001, or one more than the highest runNNNNN_ of the data files in these folders."""
    high = 0
    for d in folders:
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for n in names:
            m = RUN_FILE.match(n)
            if m:
                high = max(high, int(m.group(1)))
    return f"run{high + 1:05d}"


class RunManager:
    def __init__(self, station):
        self.station = station
        self.active: Active | None = None
        self.last: dict | None = None               # the snapshot of the run that ended last
        self.last_latest: dict = {}                 # and its newest events
        self.last_combined: dict = {}               # and its last set of matched events
        self.last_cfg: C.DaqConfig | None = None
        self.lines: list = []                       # [number, text, error]
        self.line_no = 0
        self.live_seq = 0                           # goes up with every new event
        self._control_seq = 0
        self._last_publish = 0.0

    # ---- before a run ----
    def preview(self) -> dict:
        """What the Start question shows, and why a start would be refused."""
        st = self.station
        cfg, s = st.cfg, st.settings
        problems = C.check_config(cfg, check_paths=not s.use_simulator, facts=st.boards.facts())
        r = cfg.run
        data_dir = os.path.expanduser(r.data_dir)
        free = disk_free(data_dir) if r.write_raw and not s.use_simulator else None
        need = None
        t = cfg.trigger
        if t.setup == "periodic" and r.duration_s:
            rate = t.software_rate_hz if t.periodic_source == "software" else 1e9 / max(1, t.test_pulse_period_ns)
            need = 1.1 * rate * r.duration_s * sum(C.event_bytes(b) for _, b in cfg.active())
        return {"errors": [asdict(p) for p in problems if p.level == "error"],
                "warnings": [asdict(p) for p in problems if p.level == "warning"],
                "others": [] if s.use_simulator else [{"pid": p, "command": c} for p, c in running_daq(DAQ_MARKER)],
                "busy": self.active is not None, "simulator": s.use_simulator, "duration": r.duration_s,
                "max_events": r.max_events, "boards": len(cfg.active()), "sync": r.sync, "setup": t.setup,
                "runs_folder": s.runs_folder(),
                "data_folder": None if s.use_simulator or not r.write_raw else data_dir,
                "run_name": next_run_name([data_dir]) if not s.use_simulator else None,
                "free_bytes": free, "need_bytes": need, "converting": st.converter.left()}

    def start(self, user: str, note: str | None = None) -> str:
        """Start a run; note: the note for it (and for the next runs), as edited in the Start question."""
        st, s = self.station, self.station.settings
        p = self.preview()
        if p["busy"]:
            raise RunError("a run is going already")
        if p["errors"]:
            raise RunError("the configuration has errors: " + "; ".join(f"{e['where']}: {e['message']}"
                                                                         for e in p["errors"]))
        if p["others"]:
            raise RunError("a DAQ program is running already (pid " + ", ".join(str(o["pid"]) for o in p["others"]) + ")")
        if note is not None:
            st.set_note(note, user)
        cfg = st.cfg.copy()
        base = os.path.join(s.runs_folder(), time.strftime("%Y%m%d_%H%M%S") + ("_simulator" if s.use_simulator else ""))
        run_dir, n = base, 1
        while os.path.exists(run_dir):
            run_dir, n = f"{base}_{n}", n + 1
        os.makedirs(run_dir)
        if s.use_simulator:         # simulated data stay in the run folder, never in a real data folder
            cfg.run.data_dir, cfg.run.write_raw = os.path.join(run_dir, "raw"), True
            os.makedirs(cfg.run.data_dir)
        run_name = next_run_name([os.path.expanduser(cfg.run.data_dir)])
        C.save_config(cfg, os.path.join(run_dir, "config.json"), note=st.run_note, run_name=run_name,
                      run_folder=run_dir, started_by=user, started=time.strftime("%Y-%m-%d %H:%M:%S"),
                      origin=f"ALAB DAQ v{__version__}", simulator=s.use_simulator or None)
        daq = [sys.executable, "-m", "alab_daq.daq", "run", run_dir]
        if s.use_simulator:
            daq += ["--simulate", s.simulator_options]
        cmd = [sys.executable, "-m", "alab_daq.web.wrapper", "--pid-file", os.path.join(run_dir, "daq.pid"),
               "--exit-file", os.path.join(run_dir, "daq.exit"), "--"] + daq
        env = dict(os.environ, PYTHONPATH=str(PROJECT / "src"), PYTHONUNBUFFERED="1", OMP_NUM_THREADS="1",
                   OPENBLAS_NUM_THREADS="1", MKL_NUM_THREADS="1", **s.lib_env())
        with open(os.path.join(run_dir, "daq.log"), "w", encoding="utf-8") as log:
            log.write("$ " + " ".join(shlex.quote(c) for c in daq) + "\n")
            log.flush()
            proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, cwd=run_dir,
                                    env=env, start_new_session=True)
        self._begin(Active(run_dir, cfg, run_name, daq, time.time(), user, s.use_simulator, proc.pid, proc=proc,
                           note=st.run_note))
        self._write_current(self.active)
        st.activity(user, "started a run", f"{run_name} · {run_dir}")
        return run_dir

    def _begin(self, a: Active) -> None:
        self.active = a
        self.lines.clear()
        self.line_no = 0
        self.live_seq += 1
        a.log_file = open(a.path("daq.log"), "rb")
        self.station.publish({"type": "log_reset"})
        self.publish_state()

    def _current_file(self) -> str:
        return str(self.station.state_dir / "current_run.json")

    def _write_current(self, a: Active) -> None:
        write_atomic(self._current_file(), json.dumps({"run_dir": a.run_dir, "wrapper_pid": a.wrapper_pid,
                                                       "started": a.started, "started_by": a.started_by,
                                                       "simulator": a.simulator, "command": a.command,
                                                       "note": a.note, "run_name": a.run_name}))

    # ---- the configuration next to the data ----
    def _write_side(self, a: Active, data_path: str, end: str = "") -> None:
        path = side_json(data_path)
        board = next((b for b in a.status.get("boards", []) if any(f["path"] == data_path for f in b.get("files", []))), {})
        try:
            C.save_config(a.cfg, path, note=a.note, run_name=a.run_name, run_folder=a.run_dir,
                          started_by=a.started_by, data_file=os.path.basename(data_path),
                          started=time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(a.started)),
                          board={k: board.get(k) for k in ("name", "uri", "role", "model", "serial", "fw_version",
                                                           "record_length", "pre_trigger", "sample_ns")} or None,
                          ended=end or None, origin=f"ALAB DAQ v{__version__}", simulator=a.simulator or None)
        except OSError as err:
            problem = f"could not write {path}: {err}"
            if problem not in a.problems:
                a.problems.append(problem)
            return
        if data_path not in a.side_files:
            a.side_files.append(data_path)

    def note_changed(self) -> None:
        """The note was edited: the run going takes it, and its saved copies follow."""
        a = self.active
        if a is None or a.note == self.station.run_note:
            return
        a.note = self.station.run_note
        for data_path in list(a.side_files):
            self._write_side(a, data_path)
        self._write_current(a)
        self.publish_state()

    # ---- during a run ----
    def stop(self, user: str) -> None:
        a = self._need_active()
        if not self._signal(a, signal.SIGTERM):
            raise RunError("the DAQ program has ended already")
        a.stop_sent_at, a.stopped_by = time.time(), user
        self.station.activity(user, "stopped the run", a.run_dir)
        self.publish_state()

    def kill(self, user: str) -> None:
        a = self._need_active()
        if a.stop_sent_at is None or time.time() - a.stop_sent_at < KILL_AFTER_S:
            raise RunError(f"Kill is possible {KILL_AFTER_S} s after Stop")
        child = self._read_int(a.path("daq.pid"))
        targets = [child] if child and pid_runs(child, DAQ_MARKER) else [a.wrapper_pid]
        killed = False
        for pid in targets:
            try:
                os.kill(pid, signal.SIGKILL)
                killed = True
            except (ProcessLookupError, TypeError):
                pass
        if not killed:
            raise RunError("the DAQ program has ended already")
        a.killed_by = user
        self.station.activity(user, "killed the DAQ program", a.run_dir)
        self.publish_state()

    async def control(self, user: str, data: dict) -> dict:
        """A command to the DAQ program during the run (its control folder): a Force trigger, or a channel's DC
        offset or threshold. A change also goes into the run's configuration here (the dashed lines follow) and,
        with keep, into the configuration being edited, for the next runs."""
        a = self._need_active()
        if a.status.get("phase") != "running":
            raise RunError("the boards are not taking data yet")
        cmd = data.get("cmd")
        req: dict = {"cmd": cmd, "user": user}
        if cmd == "set":
            name, what = str(data.get("board", "")), str(data.get("what", ""))
            channel, value = int(data.get("channel", -1)), float(data.get("value", "nan"))
            b = a.cfg.board(name)
            if b is None:
                raise RunError(f"there is no board {name} in this run")
            path, val = C.live_setting(b, channel, what, value)
            req.update(board=name, path=path, value=val)
        elif cmd != "swtrigger":
            raise ValueError("cmd is swtrigger or set")
        self._control_seq += 1
        name_ = f"{int(time.time() * 1000):013d}_{self._control_seq:04d}"
        write_atomic(os.path.join(a.path("control"), name_ + ".json"), json.dumps(req), private=False)
        done = os.path.join(a.path("control"), name_ + ".done")
        res = None
        for _ in range(60):
            await asyncio.sleep(0.1)
            try:
                with open(done, encoding="utf-8") as f:
                    res = json.load(f)
                os.remove(done)
                break
            except (OSError, ValueError):
                continue
        if res is None:
            raise RunError("the DAQ program did not answer within 6 s")
        if cmd == "set" and res.get("ok"):
            attr = "dc_offset" if req["path"].endswith("DCOffset") else "threshold"
            new = round(value, 3) if attr == "dc_offset" else int(value)
            setattr(a.cfg.board(name).channels[channel], attr, new)
            st, keep = self.station, bool(data.get("keep", True))
            sb = st.cfg.board(name)
            if keep and sb is not None and channel < len(sb.channels):
                setattr(sb.channels[channel], attr, new)
                st.cfg_dirty = True
                st.config_changed()
            res["kept"] = keep
            self.station.activity(user, "changed during the run", f"{name} ch{channel} {attr} = {new}")
            self.live_seq += 1
        elif cmd == "swtrigger":
            self.station.activity(user, "Force trigger", res.get("message", ""))
        return res

    def _need_active(self) -> Active:
        if self.active is None:
            raise RunError("no run is going")
        return self.active

    def _signal(self, a: Active, sig: int) -> bool:
        """Through the wrapper, which passes it on; straight to the DAQ if the wrapper has gone."""
        for pid in (a.wrapper_pid, self._read_int(a.path("daq.pid"))):
            if pid and pid_runs(pid):
                try:
                    os.kill(pid, sig)
                    return True
                except ProcessLookupError:
                    continue
        return False

    @staticmethod
    def _read_int(path: str) -> int | None:
        try:
            with open(path) as f:
                return int(f.read().strip())
        except (OSError, ValueError):
            return None

    async def tick(self) -> None:
        """Called every 0.2 s: new lines of the log, the status, new events, the end of the run."""
        a = self.active
        if a is None:
            return
        self._read_log(a)
        changed = self._read_status(a)
        now = time.time()
        if now - a.last_poll >= self.station.settings.refresh_ms / 1000:
            a.last_poll = now
            if await asyncio.to_thread(self._read_live, a):
                self.live_seq += 1
        code = self._ended(a)
        if code is not None:
            await self._finish(a, code)
        elif changed or now - self._last_publish >= 1.0:
            self.publish_state()

    def _read_log(self, a: Active, final: bool = False) -> None:
        data = a.log_file.read()
        if not data and not (final and a.partial):
            return
        text = a.partial + a.decoder.decode(data, final=final)
        lines = text.split("\n")
        a.partial = "" if final else lines.pop()
        if final and lines and lines[-1] == "":
            lines.pop()
        new = []
        for line in lines:
            line = line.rstrip("\r")
            self.line_no += 1
            entry = [self.line_no, line, "ERROR" in line or "Traceback" in line]
            self.lines.append(entry)
            new.append(entry)
        if len(self.lines) > LOG_LINES:
            del self.lines[:len(self.lines) - LOG_LINES]
        if new:
            self.station.publish({"type": "log", "lines": new})

    def _read_status(self, a: Active) -> bool:
        try:
            st = os.stat(a.path("status.json"))
            if st.st_mtime_ns == a.status_mtime:
                return False
            with open(a.path("status.json"), encoding="utf-8") as f:
                status = json.load(f)
        except (OSError, ValueError):
            return False
        a.status, a.status_mtime = status, st.st_mtime_ns
        total = sum(b.get("mbps") or 0 for b in status.get("boards", []))
        t = status.get("t") or 0
        if status.get("phase") == "running" and (not a.mbps or t > a.mbps[-1][0] + 0.9):
            a.mbps.append([round(t, 1), round(total, 3)])
        for path in a.data_files():
            if path not in a.side_files:
                self._write_side(a, path)
        return True

    def _read_live(self, a: Active) -> bool:
        """The newest event of each board, when its live file changed (in a thread)."""
        got = False
        live = a.path("live")
        try:
            names = [n for n in os.listdir(live) if n.endswith(".evt")]
        except OSError:
            return False
        for n in names:
            path = os.path.join(live, n)
            try:
                mtime = os.stat(path).st_mtime_ns
                if a.live_mtimes.get(n) == mtime:
                    continue
                with open(path, "rb") as f:
                    data = f.read()
                ev = rawdata.decode_scope(data)
            except (OSError, rawdata.RawFormatError):
                continue
            a.live_mtimes[n] = mtime
            a.latest[n[:-4]] = ev
            got = True
        path = os.path.join(live, "combined.set")
        try:
            mtime = os.stat(path).st_mtime_ns
            if a.combined_mtime != mtime:
                with open(path, "rb") as f:
                    info, events = rawdata.unpack_set(f.read())
                a.combined, a.combined_mtime = {"info": info, "events": events}, mtime
                got = True
        except (OSError, rawdata.RawFormatError, KeyError, ValueError):
            pass
        return got

    def _ended(self, a: Active) -> int | None:
        """The DAQ's exit code once the wrapper has ended, None while it runs."""
        if a.proc is not None:
            if a.proc.poll() is None:
                return None
        elif pid_runs(a.wrapper_pid, "alab_daq.web.wrapper"):
            return None
        code = self._read_int(a.path("daq.exit"))
        if code is not None:
            return code
        if pid_runs(self._read_int(a.path("daq.pid")), DAQ_MARKER):
            return None
        return UNKNOWN_EXIT

    async def _finish(self, a: Active, code: int) -> None:
        self._read_log(a, final=True)
        a.status_mtime = 0
        self._read_status(a)
        await asyncio.to_thread(self._read_live, a)
        a.log_file.close()
        s = a.status
        # finished only when the DAQ said so and ended with 0; a crash, a Kill or a missing stop record: failed
        phase = "finished" if code == 0 and s.get("phase") == "finished" else "failed"
        errors = list(s.get("errors", [])) + a.problems
        if code == UNKNOWN_EXIT:
            errors.append("the run ended while the web server was not watching; the DAQ's exit code is unknown")
        elif code < 0:
            errors.insert(0, f"the DAQ program was ended by signal {-code}" + (" (Kill)" if a.killed_by else ""))
        files = a.data_files()
        end = f"ended {time.strftime('%Y-%m-%d %H:%M:%S')}: {phase}" + (
            f" ({s.get('stop_reason')})" if s.get("stop_reason") else "")
        for path in files:
            self._write_side(a, path, end)
        boards = [{k: b.get(k) for k in ("name", "uri", "role", "model", "serial", "fw_version", "events", "bytes",
                                         "first_trigger", "last_trigger", "ended", "record_length", "pre_trigger",
                                         "sample_ns", "files", "hk_file", "stats", "monitors", "error_flags",
                                         "board_fail")} for b in s.get("boards", [])]
        summary = {"run_folder": a.run_dir, "run_name": a.run_name, "gui_version": __version__, "command": a.command,
                   "note": a.note, "started": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(a.started)),
                   "ended": time.strftime("%Y-%m-%d %H:%M:%S"), "started_by": a.started_by,
                   "stopped_by": a.stopped_by or None, "killed_by": a.killed_by or None, "simulator": a.simulator,
                   "exit_code": None if code == UNKNOWN_EXIT else code, "phase": phase,
                   "stop_reason": s.get("stop_reason"), "t": s.get("t"), "duration": a.cfg.run.duration_s,
                   "events": s.get("events", 0), "bytes": s.get("bytes", 0), "boards": boards,
                   "matching": s.get("matching"), "changes": s.get("changes", []), "data_files": files, "side_files": [side_json(p) for p in files],
                   "errors": errors, "warnings": s.get("warnings", [])}
        write_atomic(a.path("run.json"), json.dumps(summary, indent=2) + "\n", private=False)
        try:
            os.remove(self._current_file())
        except OSError:
            pass
        self.last = self._snapshot(a, final=True, phase=phase, errors=errors, code=code)
        self.last_latest, self.last_cfg, self.last_combined = a.latest, a.cfg, a.combined
        self.active = None
        self.live_seq += 1
        self.publish_state()
        self.station.activity(a.started_by, f"run ended: {phase}", f"{a.run_name} · {a.run_dir}")
        keep = [p for p in files if os.path.isfile(p) and os.path.getsize(p) > 0]
        if keep and self.station.settings.convert_to_root:
            self.station.converter.add(keep, a.run_dir, run_json=a.path("run.json"), by=a.started_by)

    # ---- after a server restart ----
    def load_last(self) -> None:
        """The result of the newest run in the save folder, so that the page shows it after a restart."""
        try:
            folders = [os.path.join(self.station.settings.runs_folder(), d)
                       for d in os.listdir(self.station.settings.runs_folder())]
            newest = max((f for f in folders if os.path.isfile(os.path.join(f, "run.json"))),
                         key=lambda f: os.path.getmtime(os.path.join(f, "run.json")))
            with open(os.path.join(newest, "run.json"), encoding="utf-8") as f:
                s = json.load(f)
            cfg, _ = C.load_config(os.path.join(newest, "config.json"))
        except (OSError, ValueError, C.ConfigError):
            return
        started = time.mktime(time.strptime(s["started"], "%Y-%m-%d %H:%M:%S")) if s.get("started") else None
        files = [{"path": p, "size": os.path.getsize(p) if os.path.exists(p) else None} for p in s.get("data_files", [])]
        self.last = {"active": False, "run_dir": newest, "run_name": s.get("run_name"), "started": started,
                     "started_by": s.get("started_by") or "?", "note": s.get("note") or "",
                     "simulator": bool(s.get("simulator")), "phase": s.get("phase", "finished"),
                     "boards": s.get("boards", []), "t": s.get("t") or 0, "duration": s.get("duration", 0),
                     "events": s.get("events", 0), "bytes": s.get("bytes", 0), "data_files": files,
                     "stop_reason": s.get("stop_reason"), "stopped_by": s.get("stopped_by"),
                     "killed_by": s.get("killed_by"), "exit_code": s.get("exit_code"), "errors": s.get("errors", []),
                     "warnings": s.get("warnings", []), "mbps_series": [], "from_record": True,
                     "matching": s.get("matching"),
                     "sync": cfg.run.sync, "setup": cfg.trigger.setup}
        self.last_cfg = cfg

    def reattach(self) -> None:
        """Follow the run a server before this one started, if there is one."""
        try:
            with open(self._current_file(), encoding="utf-8") as f:
                info = json.load(f)
            run_dir = info["run_dir"]
            cfg, _ = C.load_config(os.path.join(run_dir, "config.json"))
        except (OSError, ValueError, KeyError, C.ConfigError):
            return
        if os.path.exists(os.path.join(run_dir, "run.json")):
            os.remove(self._current_file())
            return
        a = Active(run_dir, cfg, info.get("run_name") or os.path.basename(run_dir), info.get("command", []),
                   info.get("started", time.time()), info.get("started_by", ""), bool(info.get("simulator")),
                   int(info.get("wrapper_pid") or 0), reattached=True, note=info.get("note") or "")
        self._begin(a)
        self.station.activity("server", "found a run going after a restart and follows it again", run_dir)

    # ---- for the page ----
    def _snapshot(self, a: Active, final: bool = False, phase: str | None = None, errors=None, code=None) -> dict:
        s = a.status
        files = []
        for b in s.get("boards", []):
            files += b.get("files", [])
        out = {"active": not final, "run_dir": a.run_dir, "run_name": a.run_name, "started": a.started,
               "started_by": a.started_by, "note": a.note, "simulator": a.simulator,
               "phase": phase or s.get("phase", "starting"), "boards": s.get("boards", []), "t": s.get("t", 0),
               "duration": a.cfg.run.duration_s, "max_events": a.cfg.run.max_events, "events": s.get("events", 0),
               "bytes": s.get("bytes", 0), "data_files": files, "write_raw": a.cfg.run.write_raw,
               "sync": a.cfg.run.sync, "setup": a.cfg.trigger.setup, "matching": s.get("matching"),
               "force_targets": s.get("force_targets", []), "changes": s.get("changes", [])[-20:],
               "stop_sent_at": a.stop_sent_at, "stopped_by": a.stopped_by, "kill_after_s": KILL_AFTER_S,
               "now": time.time(), "reattached": a.reattached, "errors": (errors or s.get("errors", []))[-5:],
               "warnings": s.get("warnings", [])[-5:], "stop_reason": s.get("stop_reason"),
               "mbps_point": a.mbps[-1] if a.mbps else None}
        if final:
            out.update(exit_code=None if code == UNKNOWN_EXIT else code, killed_by=a.killed_by, ended=time.time(),
                       mbps_series=a.mbps)
        return out

    def snapshot(self, full: bool = False) -> dict:
        a = self.active
        if a is None:
            return {"active": False, "last": self.last}
        out = self._snapshot(a)
        if full:
            out["mbps_series"] = a.mbps
        return out

    def publish_state(self) -> None:
        self._last_publish = time.time()
        self.station.publish({"type": "run", "run": self.snapshot()})

    def live_boards(self) -> list[dict]:
        """The boards the live view can show: of the run going, else of the last run, else the configuration's."""
        a = self.active
        cfg = a.cfg if a else (self.last_cfg or self.station.cfg)
        return [{"name": b.name, "model": b.model, "channels": len(b.channels)} for _, b in cfg.active()]

    def live_waveforms(self, board: str, channels: list[int], x0: int, x1: int | None, bins: int,
                       since: int | None, span: tuple[int, int] | None = None) -> dict:
        if since is not None and since == self.live_seq:
            return {"same": True, "seq": self.live_seq}
        a = self.active
        latest, cfg = (a.latest, a.cfg) if a else (self.last_latest, self.last_cfg or self.station.cfg)
        status_boards = {b.get("name"): b for b in (a.status.get("boards", []) if a else (self.last or {}).get("boards", []))}
        b = cfg.board(board)
        ev = latest.get(board)
        if b is None:
            raise FileNotFoundError(f"there is no board {board}")
        info = board_lines(cfg, b, status_boards.get(board))
        out = []
        for c in channels:
            if not 0 <= c < len(b.channels):
                continue
            samples = ev.waveform(c) if ev is not None else None
            out.append(channel_view(samples, b.channels[c].enabled, c, ev.trigger_id if ev is not None else None,
                                    info["record_length"], x0, x1, bins, info["lines"][c], span, info["sample_ns"]))
        run_dir = a.run_dir if a else (self.last or {}).get("run_dir")
        return {"source": "live", "board": board, "seq": self.live_seq, "running": a is not None,
                "timestamp": ev.timestamp if ev is not None else None, "trigger_id": ev.trigger_id if ev is not None else None,
                "settings": {"source": "live", "run_dir": run_dir, "run": os.path.basename(run_dir) if run_dir else None,
                             **{k: info[k] for k in ("record_length", "pre_trigger", "sample_ns", "adc_max", "model")}},
                "channels": out, "n_channels": len(b.channels)}

    def live_combined(self, traces: list[tuple[str, int]], t_lo: float | None, t_hi: float | None, bins: int,
                      yunit: str, align: str, since: int | None = None) -> dict:
        """Channels of several boards in one plot, from one moment: the DAQ's matched set (live/combined.set),
        else (no set yet) each board's newest event, marked as not matched."""
        if since is not None and since == self.live_seq:
            return {"same": True, "seq": self.live_seq}
        a = self.active
        if a is not None:
            latest, cfg, comb, status = a.latest, a.cfg, a.combined, a.status
        else:
            latest, cfg, comb, status = self.last_latest, self.last_cfg or self.station.cfg, self.last_combined, self.last or {}
        status_boards = {b.get("name"): b for b in status.get("boards", [])}
        events = comb.get("events") or {}
        names = [n for n in dict.fromkeys(b for b, _ in traces) if cfg.board(n) is not None]
        matched = bool(names) and all(n in events for n in names)
        entries = []
        for n in names:
            b = cfg.board(n)
            entries.append({"name": n, "info": board_lines(cfg, b, status_boards.get(n)),
                            "ev": events.get(n) if matched else latest.get(n),
                            "enabled": [ch.enabled for ch in b.channels]})
        out = combined_view(entries, traces, t_lo, t_hi, bins, yunit, align)
        info = comb.get("info") or {}
        chained = info.get("chained", cfg.run.sync == "chain" and len(cfg.active()) > 1)
        out.update(source="live", seq=self.live_seq, running=a is not None, matched=matched, chained=bool(chained),
                   available=[{"name": b.name, "channels": len(b.channels), "model": b.model,
                               "enabled": [ch.enabled for ch in b.channels]} for _, b in cfg.active()])
        return out

    def live_hk(self) -> dict:
        """The housekeeping of every board of the run going (else the last run), from their _hk.csv files."""
        a = self.active
        boards = (a.status.get("boards", []) if a else (self.last or {}).get("boards", [])) or []
        out = []
        for b in boards:
            path = b.get("hk_file")
            entry = {"name": b.get("name"), "model": b.get("model"), "monitors": b.get("monitors") or {},
                     "stats": b.get("stats") or {}, "error_flags": b.get("error_flags"), "path": path}
            if path and os.path.isfile(path):
                try:
                    entry.update(hk_table(path))
                except (OSError, ValueError):
                    pass
            out.append(entry)
        run = a.run_name if a else (self.last or {}).get("run_name")
        return {"source": "live", "running": a is not None, "run": run, "boards": out}

    def live_overview(self) -> dict:
        """One row per board of the newest events: per channel baseline, noise and peak."""
        from alab_daq.web.files import event_summary
        a = self.active
        latest, cfg = (a.latest, a.cfg) if a else (self.last_latest, self.last_cfg or self.station.cfg)
        boards = []
        for _, b in cfg.active():
            ev = latest.get(b.name)
            boards.append({"name": b.name, "model": b.model, "channels": len(b.channels),
                           "summary": event_summary([ev], len(b.channels)) if ev is not None else None})
        return {"source": "live", "seq": self.live_seq, "boards": boards}
