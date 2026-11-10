"""The run: the DAQ program (python -m vx27webdaq.daq run), started by an operator and followed by everyone.

The DAQ program runs apart from the server (web/wrapper.py, in a session of its own) and prints into the
run's daq.log; it writes status.json (every board's state and counters) and live/BOARD.evt (each board's newest
event). The server follows these files, so a run goes on when the server stops or restarts: a new server finds
it through current_run.json and the wrapper's pid and exit files, and carries on (with the changes made during
the run, and a Stop already sent). A run folder that cannot be read yet after a restart (the NAS not mounted
yet) is tried again every few seconds; meanwhile no run starts and the boards are not touched while the run's
wrapper is still there. The run's files may be on the NAS: the server reads and writes them in worker threads,
never in its event loop.

Every run gets its own folder in the save folder: config.json (the exact configuration, with the note and the
run's name), daq.log, status.json, live/, daq.pid / daq.exit (the wrapper's), and at the end run.json (when it
cannot be written, e.g. a full NAS, the summary goes into the state folder's run_records/ instead). Next to
every data file NAME.raw goes NAME.json: the same configuration with the note and what is known of the run, so
the settings stay with the data wherever they are copied (written when the file appears, when the note
changes, and at the end of the run). The data files hold the settings themselves too (their header).
"""

from __future__ import annotations

import asyncio
import codecs
import json
import math
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass, field

from vx27webdaq import __version__
from vx27webdaq.core import config as C
from vx27webdaq.core import models, rawdata
from vx27webdaq.core.system import disk_free, running_daq
from vx27webdaq.web.files import board_lines, channel_view, combined_view, hk_table, mv_per_adc, side_json
from vx27webdaq.web.settings import PROJECT, write_atomic

KILL_AFTER_S = 15           # Kill is offered when the DAQ has not ended this long after Stop
LOG_LINES = 20000           # the newest lines of the DAQ's output that a page gets when it opens
LOG_SEND_LINES = 5000       # at most this many lines in one message to the pages (after a restart: the newest)
LOG_TAIL_BYTES = 1 << 20    # after a restart the server follows daq.log from its last MB (the rest stays in the file)
READ_BYTES = 4 << 20        # the most of daq.log read in one tick
UNKNOWN_EXIT = -1000        # the wrapper ended without writing the DAQ's exit code
RUN_FILE = re.compile(r"^run(\d{5,})_")
SIM_FOLDER = "simulated"    # simulated runs write into DATA_FOLDER/simulated/: on the NAS, apart from the real runs
DAQ_MARKER = "vx27webdaq.daq run"
WRAPPER_MARKER = "vx27webdaq.web.wrapper"
REATTACH_RETRY_S = 5.0      # a run folder that cannot be read after a restart: tried again this often
CONTROL_WAIT_S = 6.0        # how long a command to the DAQ program during the run may take
LIVE_PATH = re.compile(r"^/ch/(\d+)/par/(DCOffset|TriggerThr)$", re.IGNORECASE)


class RunError(RuntimeError):
    """A run cannot be started, stopped or killed now (the message says why)."""


class BoardsBusy(RunError):
    """A run cannot start now because the boards are busy (a board check, Send to boards, the manual controller, or
    another DAQ program): a run sequence waits and tries again."""


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
    note_suffix: str = ""       # a sequence's "sequence ID: run k of N · step", kept apart from the note itself
    side_files: list = field(default_factory=list)      # data files that have their NAME.json
    daq_pid: int | None = None  # the DAQ program's pid (daq.pid), once read

    def path(self, name: str) -> str:
        return os.path.join(self.run_dir, name)

    def data_files(self) -> list[str]:
        return [f["path"] for b in self.status.get("boards", []) for f in b.get("files", [])]


def join_note(note: str, suffix: str) -> str:
    """The note of a run: the note, and a sequence's step after it."""
    if not suffix:
        return note
    return (note + " · " if note else "") + suffix


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


def apply_changes(cfg: C.DaqConfig, changes) -> int:
    """The DC offsets and thresholds changed during a run (status.json "changes": what the boards took), into the
    run's configuration, as the server did when they were made; how many."""
    n = 0
    for c in changes if isinstance(changes, list) else []:
        if not isinstance(c, dict) or not c.get("ok"):
            continue
        m = LIVE_PATH.match(str(c.get("path", "")))
        b = cfg.board(str(c.get("board", "")))
        if m is None or b is None or int(m[1]) >= len(b.channels):
            continue
        try:
            value = float(c.get("value"))
        except (TypeError, ValueError):
            continue
        if not math.isfinite(value):
            continue
        ch = b.channels[int(m[1])]
        if m[2].lower() == "dcoffset":
            ch.dc_offset = round(value, 3)
        else:
            ch.threshold = abs(int(value))          # sent as -N for negative pulses
        n += 1
    return n


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
        self._pending: dict | None = None           # current_run.json of a run that could not be followed yet
        self._pending_next = 0.0
        self._side_lock = threading.Lock()          # one writer of the NAME.json files at a time

    # ---- before a run ----
    def preview(self, cfg: C.DaqConfig | None = None, simulator: bool | None = None) -> dict:
        """What the Start question shows, and why a start would be refused (of this configuration and simulator
        setting: a sequence's step has its own)."""
        st = self.station
        s = st.settings
        cfg = cfg or st.cfg
        sim = s.use_simulator if simulator is None else simulator
        problems = C.check_config(cfg, check_paths=True, facts=st.boards.facts())
        r = cfg.run
        data_dir = os.path.expanduser(r.data_dir)
        free = disk_free(data_dir) if (r.write_raw or sim) and os.path.isdir(data_dir) else None
        if sim:
            data_dir = os.path.join(data_dir, SIM_FOLDER)
        need = None
        t = cfg.trigger
        if t.setup == "periodic" and r.duration_s:
            rate = t.software_rate_hz if t.periodic_source == "software" else 1e9 / max(1, t.test_pulse_period_ns)
            need = 1.1 * rate * r.duration_s * sum(C.event_bytes(b) for _, b in cfg.active())
        return {"errors": [asdict(p) for p in problems if p.level == "error"],
                "warnings": [asdict(p) for p in problems if p.level == "warning"],
                "others": [] if sim else [{"pid": p, "command": c} for p, c in running_daq(DAQ_MARKER)],
                "busy": self.active is not None, "simulator": sim, "duration": r.duration_s,
                "max_events": r.max_events, "boards": len(cfg.active()), "sync": r.sync, "setup": t.setup,
                "runs_folder": s.runs_folder(),
                "data_folder": data_dir if r.write_raw or sim else None,
                "run_name": next_run_name([data_dir]),
                "free_bytes": free, "need_bytes": need, "converting": st.converter.left()}

    def waiting_for(self) -> bool:
        """A run of the server before this one may still be going: its current_run.json is there, its folder cannot
        be read yet, and its wrapper still runs."""
        pid = (self._pending or {}).get("wrapper_pid")
        return isinstance(pid, int) and not isinstance(pid, bool) and pid_runs(pid, WRAPPER_MARKER)

    def start(self, user: str, note: str | None = None, cfg: C.DaqConfig | None = None, note_suffix: str = "",
              extra: dict | None = None, simulator: bool | None = None, sim_options: str | None = None,
              from_sequence: bool = False) -> str:
        """Start a run; note: the note for it (and for the next runs), as edited in the Start question. A run
        sequence gives its own configuration (cfg), the step after the note (note_suffix), what goes into config.json
        (extra) and the simulator setting it started with (simulator, sim_options)."""
        st, s = self.station, self.station.settings
        sim = s.use_simulator if simulator is None else bool(simulator)
        options = s.sim_options() if sim_options is None else sim_options
        seq = getattr(st, "sequence", None)
        if not from_sequence and seq is not None and seq.running():
            raise RunError("a run sequence is going: its runs start by themselves; stop it first (Stop sequence) or "
                           "wait until it ends")
        if self.active is not None:
            raise RunError("a run is going already")
        if self.waiting_for():
            raise BoardsBusy("the run of the server before this one may still be going (its folder cannot be read "
                             "yet, e.g. the NAS is not mounted): wait until the server follows it again")
        if st.boards.busy():
            raise BoardsBusy(f"the boards are busy ({st.boards.busy()}): try again in a moment")
        p = self.preview(cfg, sim)
        if p["errors"]:
            raise RunError("the configuration has errors: " + "; ".join(f"{e['where']}: {e['message']}"
                                                                         for e in p["errors"]))
        if p["others"]:
            raise BoardsBusy("a DAQ program is running already (pid " + ", ".join(str(o["pid"]) for o in p["others"]) + ")")
        if note is not None:
            st.set_note(note, user)
        cfg = (cfg or st.cfg).copy()
        # the DAQ program runs in the run folder: a relative data folder means there what the check meant here
        cfg.run.data_dir = os.path.abspath(os.path.expanduser(cfg.run.data_dir))
        this_note = join_note(st.run_note, note_suffix)
        base = os.path.join(s.runs_folder(), time.strftime("%Y%m%d_%H%M%S") + ("_simulator" if sim else ""))
        run_dir, n = base, 1
        while os.path.exists(run_dir):
            run_dir, n = f"{base}_{n}", n + 1
        try:
            os.makedirs(run_dir)
        except OSError as err:
            raise RunError(f"cannot make the run folder {run_dir}: {err} (is the save folder there, e.g. the NAS "
                           "mounted, and writable by the service account?)") from None
        st.folders.forget(s.runs_folder())          # it may have been made just now
        if sim:                     # simulated data: DATA_FOLDER/simulated/, never among the real runs' files
            root = cfg.run.data_dir
            sim_dir = os.path.join(root, SIM_FOLDER)
            try:
                if not os.path.isdir(root):         # e.g. the NAS not mounted: never make its folder on the local disk
                    raise OSError(f"the data folder {root} does not exist")
                os.makedirs(sim_dir, exist_ok=True)
            except OSError as err:
                shutil.rmtree(run_dir, ignore_errors=True)
                raise RunError(f"cannot make the folder of simulated data {sim_dir}: {err}") from None
            cfg.run.data_dir, cfg.run.write_raw = sim_dir, True
        run_name = next_run_name([cfg.run.data_dir])
        C.save_config(cfg, os.path.join(run_dir, "config.json"), note=this_note, run_name=run_name,
                      run_folder=run_dir, started_by=user, started=time.strftime("%Y-%m-%d %H:%M:%S"),
                      origin=f"Vx27webDAQ v{__version__}", simulator=sim or None, **(extra or {}))
        daq = [sys.executable, "-m", "vx27webdaq.daq", "run", run_dir]
        if sim:
            daq += ["--simulate", options]
        cmd = [sys.executable, "-m", "vx27webdaq.web.wrapper", "--pid-file", os.path.join(run_dir, "daq.pid"),
               "--exit-file", os.path.join(run_dir, "daq.exit"), "--"] + daq
        env = dict(os.environ, PYTHONPATH=str(PROJECT / "src"), PYTHONUNBUFFERED="1", OMP_NUM_THREADS="1",
                   OPENBLAS_NUM_THREADS="1", MKL_NUM_THREADS="1", **s.lib_env())
        with open(os.path.join(run_dir, "daq.log"), "w", encoding="utf-8") as log:
            log.write("$ " + " ".join(shlex.quote(c) for c in daq) + "\n")
            log.flush()
            proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, cwd=run_dir,
                                    env=env, start_new_session=True)
        self._begin(Active(run_dir, cfg, run_name, daq, time.time(), user, sim, proc.pid, proc=proc,
                           note=this_note, note_suffix=note_suffix))
        self._write_current(self.active)
        st.activity(user, "started a run", f"{run_name} · {run_dir}")
        return run_dir

    def _begin(self, a: Active, log=None, skipped: int = 0) -> None:
        self.active = a
        self.lines.clear()
        self.line_no = 0
        self.live_seq += 1
        a.log_file = log if log is not None else open(a.path("daq.log"), "rb")
        self.station.publish({"type": "log_reset"})
        if skipped:
            self._add_lines([f"… (the first {skipped / 1e6:.1f} MB of the DAQ program's output are in {a.path('daq.log')})"])
        self.publish_state()

    def _current_file(self) -> str:
        return str(self.station.state_dir / "current_run.json")

    def _write_current(self, a: Active) -> None:
        write_atomic(self._current_file(), json.dumps({"run_dir": a.run_dir, "wrapper_pid": a.wrapper_pid,
                                                       "started": a.started, "started_by": a.started_by,
                                                       "simulator": a.simulator, "command": a.command,
                                                       "note": a.note, "note_suffix": a.note_suffix,
                                                       "run_name": a.run_name, "stop_sent_at": a.stop_sent_at,
                                                       "stopped_by": a.stopped_by, "killed_by": a.killed_by}))

    # ---- the configuration next to the data ----
    def _write_side(self, a: Active, data_path: str, end: str = "") -> None:
        path = side_json(data_path)
        board = next((b for b in a.status.get("boards", []) if any(f["path"] == data_path for f in b.get("files", []))), {})
        try:
            with self._side_lock:
                C.save_config(a.cfg, path, note=a.note, run_name=a.run_name, run_folder=a.run_dir,
                              started_by=a.started_by, data_file=os.path.basename(data_path),
                              started=time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(a.started)),
                              board={k: board.get(k) for k in ("name", "uri", "role", "model", "serial", "fw_version",
                                                               "record_length", "pre_trigger", "sample_ns")} or None,
                              ended=end or None, origin=f"Vx27webDAQ v{__version__}", simulator=a.simulator or None)
        except OSError as err:
            problem = f"could not write {path}: {err}"
            if problem not in a.problems:
                a.problems.append(problem)
            return
        if data_path not in a.side_files:
            a.side_files.append(data_path)

    def _write_sides(self, a: Active) -> None:
        for data_path in list(a.side_files):
            self._write_side(a, data_path)

    def note_changed(self) -> None:
        """The note was edited: the run going takes it (a sequence's step stays after it), and its saved copies
        follow (written in a worker thread: they are on the NAS)."""
        a = self.active
        if a is None:
            return
        note = join_note(self.station.run_note, a.note_suffix)
        if a.note == note:
            return
        a.note = note
        self._write_current(a)
        try:
            asyncio.get_running_loop().run_in_executor(None, self._write_sides, a)
        except RuntimeError:                # no event loop (a test, the command line): at once
            self._write_sides(a)
        self.publish_state()

    # ---- during a run ----
    def stop(self, user: str) -> None:
        seq = getattr(self.station, "sequence", None)
        if seq is not None and seq.running():          # Stop on the page: the run and its sequence
            seq.stop(user)
        a = self._need_active()
        if a.stop_sent_at is not None:      # a second SIGTERM would tell the DAQ not to wait for the stop records
            return
        if not self._signal(a, signal.SIGTERM):
            raise RunError("the DAQ program has ended already")
        a.stop_sent_at, a.stopped_by = time.time(), user
        self._write_current(a)              # a server that starts now knows the Stop too
        self.station.activity(user, "stopped the run", a.run_dir)
        self.publish_state()

    def kill(self, user: str) -> None:
        a = self._need_active()
        if a.stop_sent_at is None or time.time() - a.stop_sent_at < KILL_AFTER_S:
            raise RunError(f"Kill is possible {KILL_AFTER_S} s after Stop")
        child = self._daq_pid(a)
        targets = [(child, DAQ_MARKER)] if pid_runs(child, DAQ_MARKER) else [(a.wrapper_pid, WRAPPER_MARKER)]
        killed = False
        for pid, marker in targets:
            if not pid_runs(pid, marker):   # never a process that only has the pid now
                continue
            try:
                os.kill(pid, signal.SIGKILL)
                killed = True
            except (ProcessLookupError, PermissionError):
                pass
        if not killed:
            raise RunError("the DAQ program has ended already")
        a.killed_by = user
        self._write_current(a)
        self.station.activity(user, "killed the DAQ program", a.run_dir)
        self.publish_state()

    async def control(self, user: str, data: dict) -> dict:
        """A command to the DAQ program during the run (its control folder): a Force trigger, or a channel's DC
        offset or threshold. A change also goes into the run's configuration here (the dashed lines follow) and,
        with keep, into the configuration being edited, for the next runs. A command the DAQ program does not take
        in time is taken back, so it is never done later behind the configuration's back."""
        a = self._need_active()
        if a.status.get("phase") != "running":
            raise RunError("the boards are not taking data yet")
        cmd = data.get("cmd")
        req: dict = {"cmd": cmd, "user": user}
        if cmd == "set":
            name, what = str(data.get("board", "")), str(data.get("what", ""))
            channel, value = data.get("channel"), data.get("value")
            if isinstance(channel, bool) or not isinstance(channel, int):
                raise ValueError("channel must be a channel number")
            if isinstance(value, bool) or not isinstance(value, (int, float, str)):
                raise ValueError("value must be a number")
            try:
                value = float(value)
            except ValueError:
                raise ValueError("value must be a number") from None
            if not math.isfinite(value):
                raise ValueError("value must be a finite number")
            b = a.cfg.board(name)
            if b is None:
                raise RunError(f"there is no board {name} in this run")
            path, val = C.live_setting(b, channel, what, value)
            req.update(board=name, path=path, value=val)
        elif cmd != "swtrigger":
            raise ValueError("cmd is swtrigger or set")
        self._control_seq += 1
        name_ = f"{int(time.time() * 1000):013d}_{self._control_seq:04d}"
        request = os.path.join(a.path("control"), name_ + ".json")
        done = os.path.join(a.path("control"), name_ + ".done")
        await asyncio.to_thread(write_atomic, request, json.dumps(req), False)
        res = await self._answer(done, CONTROL_WAIT_S)
        if res is None:
            if await asyncio.to_thread(self._withdraw, request):
                raise RunError(f"the DAQ program did not answer within {CONTROL_WAIT_S:g} s: the command was taken back, "
                               "nothing was changed")
            res = await self._answer(done, 3.0)     # it took the command just now: its answer follows
            if res is None:
                raise RunError("the DAQ program took the command but did not answer: see its output (the change may "
                               "have been made)")
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

    @staticmethod
    def _take_done(path: str) -> dict | None:
        try:
            with open(path, encoding="utf-8") as f:
                res = json.load(f)
            os.remove(path)
        except (OSError, ValueError):
            return None
        return res if isinstance(res, dict) else {"ok": False, "message": "the DAQ program's answer is not readable"}

    async def _answer(self, done: str, wait_s: float) -> dict | None:
        """The DAQ program's answer control/N.done, waited for at most wait_s (read in a worker thread)."""
        end = time.monotonic() + wait_s
        while time.monotonic() < end:
            await asyncio.sleep(0.1)
            res = await asyncio.to_thread(self._take_done, done)
            if res is not None:
                return res
        return None

    @staticmethod
    def _withdraw(path: str) -> bool:
        """Take a command back that the DAQ program has not taken yet: True if it was still there."""
        try:
            os.remove(path)
            return True
        except OSError:
            return False

    def _need_active(self) -> Active:
        if self.active is None:
            raise RunError("no run is going")
        return self.active

    def _daq_pid(self, a: Active) -> int | None:
        if a.daq_pid is None:
            a.daq_pid = self._read_int(a.path("daq.pid"))
        return a.daq_pid

    def _signal(self, a: Active, sig: int) -> bool:
        """Through the wrapper, which passes it on; straight to the DAQ if the wrapper has gone. Only a process whose
        command line is the wrapper's or the DAQ program's gets it (a pid can be reused by anything)."""
        for pid, marker in ((a.wrapper_pid, WRAPPER_MARKER), (self._daq_pid(a), DAQ_MARKER)):
            if pid and pid_runs(pid, marker):
                try:
                    os.kill(pid, sig)
                    return True
                except (ProcessLookupError, PermissionError):
                    continue
        return False

    @staticmethod
    def _read_int(path: str) -> int | None:
        try:
            with open(path) as f:
                return int(f.read().strip())
        except (OSError, ValueError):
            return None

    # ---- following it ----
    async def tick(self) -> None:
        """Called every 0.2 s: new lines of the log, the status, new events, the end of the run. The files are read in
        a worker thread (they may be on a NAS that hangs: the pages, Stop and Kill must still answer)."""
        a = self.active
        if a is None:
            if self._pending is not None and time.time() >= self._pending_next:
                await self.reattach()
            return
        now = time.time()
        live = now - a.last_poll >= self.station.settings.refresh_ms / 1000
        if live:
            a.last_poll = now
        got = await asyncio.to_thread(self._poll, a, live)
        if self.active is not a:
            return
        self._take_log(a, got["log"])
        if got["live"]:
            self.live_seq += 1
        if got["code"] is not None:
            await self._finish(a, got["code"])
        elif got["status"] or now - self._last_publish >= 1.0:
            self.publish_state()

    def _poll(self, a: Active, live: bool) -> dict:
        """(in a worker thread) The tick's file work."""
        data = self._read_bytes(a)
        changed = self._read_status(a)
        got = self._read_live(a) if live else False
        return {"log": data, "status": changed, "live": got, "code": self._ended(a)}

    @staticmethod
    def _read_bytes(a: Active) -> bytes:
        f = a.log_file
        if f is None or f.closed:
            return b""
        try:
            return f.read(READ_BYTES)
        except (OSError, ValueError):
            return b""

    def _add_lines(self, lines: list[str]) -> None:
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
            self.station.publish({"type": "log", "lines": new[-LOG_SEND_LINES:]})

    def _take_log(self, a: Active, data: bytes, final: bool = False) -> None:
        if not data and not (final and a.partial):
            return
        text = a.partial + a.decoder.decode(data, final=final)
        lines = text.split("\n")
        a.partial = "" if final else lines.pop()
        if final and lines and lines[-1] == "":
            lines.pop()
        self._add_lines(lines)

    def _read_status(self, a: Active) -> bool:
        try:
            st = os.stat(a.path("status.json"))
            if st.st_mtime_ns == a.status_mtime:
                return False
            with open(a.path("status.json"), encoding="utf-8") as f:
                status = json.load(f)
        except (OSError, ValueError):
            return False
        if not isinstance(status, dict):
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
        elif pid_runs(a.wrapper_pid, WRAPPER_MARKER):
            return None
        code = self._read_int(a.path("daq.exit"))
        if code is not None:
            return code
        if pid_runs(self._daq_pid(a), DAQ_MARKER):
            return None
        return UNKNOWN_EXIT

    # ---- the end ----
    async def _finish(self, a: Active, code: int) -> None:
        """The run ended: its record (run.json, the NAME.json files) is written in a worker thread; whatever happens
        there (a full NAS, a removed run folder), the run is over for the server: no longer active, its sequence
        goes on, its files are converted."""
        record = None
        try:
            record = await asyncio.to_thread(self._finish_files, a, code)
        except Exception as err:            # noqa: BLE001 - the run must end for the server in any case
            a.problems.append(f"the end of the run could not be recorded: {err!r}")
            self.station.activity("server", "error at the end of a run", repr(err))
        finally:
            self._ended_run(a, code, record)

    def _end_time(self, a: Active) -> float:
        """When the run ended: the DAQ program's last status when it ended itself (also a run that ended while no
        server watched), else when the wrapper wrote the exit code, else now."""
        s, t = a.status, a.status.get("time")
        if s.get("phase") in ("finished", "failed") and isinstance(t, (int, float)) and not isinstance(t, bool) \
                and math.isfinite(t):
            return float(t)
        try:
            return os.path.getmtime(a.path("daq.exit"))
        except OSError:
            return time.time()

    def _finish_files(self, a: Active, code: int) -> dict:
        """(in a worker thread) The rest of the log, the last status and events, the NAME.json files and run.json."""
        chunks, size = [], 0
        while size < 16 * READ_BYTES:
            chunk = self._read_bytes(a)
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
        try:
            a.log_file.close()
        except (OSError, AttributeError):
            pass
        a.status_mtime = 0
        self._read_status(a)
        self._read_live(a)
        s = a.status
        # finished only when the DAQ said so and ended with 0; a crash, a Kill or a missing stop record: failed
        phase = "finished" if code == 0 and s.get("phase") == "finished" else "failed"
        errors = list(s.get("errors", [])) + a.problems
        if code == UNKNOWN_EXIT:
            errors.append("the run ended while the web server was not watching; the DAQ's exit code is unknown")
        elif code < 0:
            errors.insert(0, f"the DAQ program was ended by signal {-code}" + (" (Kill)" if a.killed_by else ""))
        ended = self._end_time(a)
        files = a.data_files()
        end = f"ended {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(ended))}: {phase}" + (
            f" ({s.get('stop_reason')})" if s.get("stop_reason") else "")
        for path in files:
            self._write_side(a, path, end)
        boards = [{k: b.get(k) for k in ("name", "uri", "role", "model", "serial", "fw_version", "events", "bytes",
                                         "first_trigger", "last_trigger", "ended", "record_length", "pre_trigger",
                                         "sample_ns", "files", "hk_file", "stats", "monitors", "error_flags",
                                         "board_fail")} for b in s.get("boards", [])]
        summary = {"run_folder": a.run_dir, "run_name": a.run_name, "gui_version": __version__, "command": a.command,
                   "note": a.note, "started": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(a.started)),
                   "ended": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ended)), "started_by": a.started_by,
                   "stopped_by": a.stopped_by or None, "killed_by": a.killed_by or None, "simulator": a.simulator,
                   "exit_code": None if code == UNKNOWN_EXIT else code, "phase": phase,
                   "stop_reason": s.get("stop_reason"), "t": s.get("t"), "duration": a.cfg.run.duration_s,
                   "events": s.get("events", 0), "bytes": s.get("bytes", 0), "boards": boards,
                   "matching": s.get("matching"), "changes": s.get("changes", []), "data_files": files,
                   "side_files": [side_json(p) for p in files], "errors": errors, "warnings": s.get("warnings", [])}
        run_json = a.path("run.json")
        try:
            write_atomic(run_json, json.dumps(summary, indent=2) + "\n", private=False)
        except OSError as err:              # e.g. a full NAS, a quota, the run folder removed: the summary is kept here
            errors.append(f"could not write {run_json}: {err}")
            copy = self.station.state_dir / "run_records" / f"{os.path.basename(a.run_dir)}.json"
            try:
                write_atomic(copy, json.dumps(dict(summary, errors=errors), indent=2) + "\n")
                errors.append(f"the run's summary is in {copy} instead")
                run_json = str(copy)
            except OSError:
                run_json = None
        keep = []
        for p in files:
            try:
                if os.path.isfile(p) and os.path.getsize(p) > 0:
                    keep.append(p)
            except OSError:
                pass
        return {"log": b"".join(chunks), "phase": phase, "errors": errors, "ended": ended, "keep": keep,
                "run_json": run_json, "boards": len(s.get("boards", []))}

    def _ended_run(self, a: Active, code: int, record: dict | None) -> None:
        """(in the event loop) The run is over for the server, whatever could be recorded of it."""
        phase = record["phase"] if record else "failed"
        errors = record["errors"] if record else list(a.status.get("errors", [])) + a.problems
        if record:
            try:
                self._take_log(a, record["log"], final=True)
            except Exception:               # noqa: BLE001
                pass
        try:
            self.last = self._snapshot(a, final=True, phase=phase, errors=errors, code=code,
                                       ended=record["ended"] if record else None)
        except Exception:                   # noqa: BLE001
            self.last = {"active": False, "run_dir": a.run_dir, "run_name": a.run_name, "phase": phase, "errors": errors}
        self.last_latest, self.last_cfg, self.last_combined = a.latest, a.cfg, a.combined
        self.active = None
        self.live_seq += 1
        try:
            os.remove(self._current_file())
        except OSError:
            pass
        self.publish_state()
        st = self.station
        st.activity(a.started_by, f"run ended: {phase}", f"{a.run_name} · {a.run_dir}")
        seq = getattr(st, "sequence", None)
        if seq is not None:
            try:
                seq.run_ended(a.run_dir, phase)
            except Exception as err:        # noqa: BLE001
                st.activity("server", "error in the run sequence", repr(err))
        try:
            keep = record["keep"] if record else []
            if keep and st.settings.convert_to_root:
                st.converter.add(keep, a.run_dir, run_json=record["run_json"], by=a.started_by)
            # every board per trigger: only a chain of several boards has one clock to match the boards by
            if keep and st.settings.build_events and a.cfg.run.sync == "chain" and record["boards"] > 1:
                st.converter.add_build(keep[0], a.run_dir, run_json=record["run_json"], by=a.started_by)
        except Exception as err:            # noqa: BLE001
            st.activity("server", "error queuing the ROOT conversion", repr(err))

    # ---- after a server restart ----
    def load_last(self) -> None:
        """The result of the newest run in the save folder, so that the page shows it after a restart: the run folder
        with the latest name (its date and time; run.json files of older runs change later, when they are converted)."""
        try:
            folders = [os.path.join(self.station.settings.runs_folder(), d)
                       for d in os.listdir(self.station.settings.runs_folder())]
            newest = max((f for f in folders if os.path.isfile(os.path.join(f, "run.json"))), key=os.path.basename)
            with open(os.path.join(newest, "run.json"), encoding="utf-8") as f:
                s = json.load(f)
            cfg, _ = C.load_config(os.path.join(newest, "config.json"))
            started = time.mktime(time.strptime(s["started"], "%Y-%m-%d %H:%M:%S")) if s.get("started") else None
        except (OSError, ValueError, TypeError, AttributeError, C.ConfigError):
            return
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

    def _reattach_read(self):
        """(in a worker thread) What current_run.json and the run's folder say: ("none", None), ("retry", info) while
        the folder cannot be read, ("ended", run_dir) when the run's record is written already, or ("follow", (info,
        configuration, status, the log file, bytes of it skipped))."""
        try:
            with open(self._current_file(), encoding="utf-8") as f:
                info = json.load(f)
        except FileNotFoundError:
            return "none", None
        except (OSError, ValueError) as err:
            raise RunError(f"{self._current_file()} cannot be read: {err}") from None
        if not isinstance(info, dict) or not isinstance(info.get("run_dir"), str):
            raise RunError(f"{self._current_file()} names no run folder")
        run_dir = info["run_dir"]
        try:
            cfg, _ = C.load_config(os.path.join(run_dir, "config.json"))
        except OSError:                     # e.g. the NAS not mounted yet: again in a few seconds
            return "retry", info
        if os.path.exists(os.path.join(run_dir, "run.json")):
            try:
                os.remove(self._current_file())
            except OSError:
                pass
            return "ended", run_dir
        try:
            with open(os.path.join(run_dir, "status.json"), encoding="utf-8") as f:
                status = json.load(f)
        except (OSError, ValueError):
            status = {}
        try:
            log = open(os.path.join(run_dir, "daq.log"), "rb")
        except OSError:
            return "retry", info
        skipped = 0
        size = os.fstat(log.fileno()).st_size
        if size > LOG_TAIL_BYTES:           # the pages get the end of a long log, not all of it in one message
            log.seek(size - LOG_TAIL_BYTES)
            log.readline()
            skipped = log.tell()
        return "follow", (info, cfg, status if isinstance(status, dict) else {}, log, skipped)

    async def reattach(self) -> None:
        """Follow the run a server before this one started, if there is one. Nothing in here may stop the server
        from starting: a broken record is logged; a run folder that cannot be read yet is tried again every few
        seconds while current_run.json is there."""
        try:
            kind, data = await asyncio.to_thread(self._reattach_read)
        except Exception as err:            # noqa: BLE001
            self._pending = None
            self.station.activity("server", "could not follow the run of the server before this one", repr(err))
            return
        if kind == "retry":
            if self._pending is None:
                self.station.activity("server", "the run of the server before this one cannot be read yet; trying again",
                                      data.get("run_dir", ""))
            self._pending, self._pending_next = data, time.time() + REATTACH_RETRY_S
            return
        self._pending = None
        if kind != "follow":
            return
        info, cfg, status, log, skipped = data
        try:
            self._follow(info, cfg, status, log, skipped)
        except Exception as err:            # noqa: BLE001
            log.close()
            self.station.activity("server", "could not follow the run of the server before this one", repr(err))

    def _follow(self, info: dict, cfg: C.DaqConfig, status: dict, log, skipped: int) -> None:
        run_dir = info["run_dir"]
        number = lambda v: float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None   # noqa: E731
        text = lambda v: v if isinstance(v, str) else ""                                                    # noqa: E731
        a = Active(run_dir, cfg, text(info.get("run_name")) or os.path.basename(run_dir), info.get("command") or [],
                   number(info.get("started")) or time.time(), text(info.get("started_by")), bool(info.get("simulator")),
                   int(number(info.get("wrapper_pid")) or 0), reattached=True, note=text(info.get("note")),
                   note_suffix=text(info.get("note_suffix")))
        apply_changes(a.cfg, status.get("changes"))     # the DC offsets and thresholds as the boards have them now
        a.stop_sent_at, a.stopped_by = number(info.get("stop_sent_at")), text(info.get("stopped_by"))
        a.killed_by = text(info.get("killed_by"))
        if a.stop_sent_at is None and status.get("stop_requested"):     # a Stop before the restart: never a second one
            a.stop_sent_at = number(status.get("time")) or time.time()
        self._begin(a, log=log, skipped=skipped)
        self.station.activity("server", "found a run going after a restart and follows it again", run_dir)

    # ---- for the page ----
    def _snapshot(self, a: Active, final: bool = False, phase: str | None = None, errors=None, code=None,
                  ended: float | None = None) -> dict:
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
            out.update(exit_code=None if code == UNKNOWN_EXIT else code, killed_by=a.killed_by,
                       ended=ended or time.time(), mbps_series=a.mbps)
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

    def live_boards(self) -> dict:
        """The boards the live view can show: of the run going ("run"), else of the last run ("last"), else of the
        configuration ("configuration"); per board its channels read out, the channels in its board trigger (none
        when it takes its trigger on TRG-IN) and its sample time."""
        a = self.active
        if a is not None:
            cfg, source, run_dir, status = a.cfg, "run", a.run_dir, a.status.get("boards", [])
        elif self.last_cfg is not None:
            cfg, source, run_dir, status = self.last_cfg, "last", (self.last or {}).get("run_dir"), (self.last or {}).get("boards", [])
        else:
            cfg, source, run_dir, status = self.station.cfg, "configuration", None, []
        said = {b.get("name"): b for b in status if isinstance(b, dict)}
        t = cfg.trigger
        out = []
        for k, (_, b) in enumerate(cfg.active()):
            m = models.model(b.model)
            own = C.triggers_on_channels(cfg, k)
            groups = ("A", "B") if t.combine in ("A_AND_B", "A_OR_B") else (t.combine,)
            ns = said.get(b.name, {}).get("sample_ns")
            if not isinstance(ns, (int, float)) or isinstance(ns, bool) or not ns > 0:
                ns = C.sample_ns(b, m) if m else 8.0
            out.append({"name": b.name, "model": b.model, "channels": len(b.channels),
                        "read_out": [c for c, ch in enumerate(b.channels) if ch.enabled],
                        "in_trigger": [c for c, ch in enumerate(b.channels)
                                       if own and ch.enabled and ch.self_trigger and ch.itl in groups],
                        "sample_ns": float(ns)})
        return {"boards": out, "source": source, "run_dir": run_dir}

    def live_waveforms(self, board: str, channels: list[int], x0: int, x1: int | None, bins: int,
                       since: int | None, span: tuple[int, int] | None = None, full: bool = False) -> dict:
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
                                    info["record_length"], x0, x1, bins, info["lines"][c], span, info["sample_ns"], full))
        run_dir = a.run_dir if a else (self.last or {}).get("run_dir")
        return {"source": "live", "board": board, "seq": self.live_seq, "running": a is not None,
                "timestamp": ev.timestamp if ev is not None else None, "trigger_id": ev.trigger_id if ev is not None else None,
                "settings": {"source": "live", "run_dir": run_dir, "run": os.path.basename(run_dir) if run_dir else None,
                             **{k: info[k] for k in ("record_length", "pre_trigger", "sample_ns", "adc_max", "model")},
                             "mv_per_adc": mv_per_adc(info)},
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
        from vx27webdaq.web.files import event_summary
        a = self.active
        latest, cfg = (a.latest, a.cfg) if a else (self.last_latest, self.last_cfg or self.station.cfg)
        boards = []
        for _, b in cfg.active():
            ev = latest.get(b.name)
            boards.append({"name": b.name, "model": b.model, "channels": len(b.channels),
                           "summary": event_summary([ev], len(b.channels)) if ev is not None else None})
        return {"source": "live", "seq": self.live_seq, "boards": boards}
