"""The DAQ program of ALAB DAQ: one run of every board of a configuration (it takes the place testDAQ has in
XIAOLAB DAQ). The web server starts it apart from itself (web/wrapper.py), so a run goes on when the server
restarts:

    python -m alab_daq.daq run RUN_FOLDER [--simulate "OPTIONS"]

RUN_FOLDER/config.json is the run's configuration (written by the server: the configuration, its note, the
run's name). The program prints its log (the server keeps it as daq.log) and writes into RUN_FOLDER:
    status.json      twice a second: the phase, and each board's state, counters and files
    live/NAME.evt    the newest event of each board, as the board sent it (the page's live waveforms)
and the data, one stream per board, as the board sent it: DATA_FOLDER/RUN_NAME_BOARD.raw (and _001.raw ...
when a file reaches file_mb). See core/rawdata.py.

A run, in this order (CAEN's sync demo, caen-dig2 demo/caen-felib-demo-sync): connect to every board, one after
the other, and check its firmware (Scope) and model; reset it and set its parameters (core/config.py: plan);
activate the raw endpoint; arm every board, the last of the chain first; start one reader per board; send
SwStartAcquisition to the master (to every board when they are independent). The run ends at its run time,
when the first board has max_events events, on SIGTERM or SIGINT (Stop on the page), or when a board fails:
SwStopAcquisition to the master (or every board); each reader goes on until its board's stop record, at most
STOP_WAIT_S; then every board is disarmed and closed. Exit code 0: every board ended with its stop record;
1: anything else.
"""

from __future__ import annotations

import json
import os
import signal
import struct
import sys
import threading
import time
from collections import deque

from alab_daq import __version__
from alab_daq.core import config as C
from alab_daq.core import models, rawdata
from alab_daq.daq.boards import BoardError, monitors, open_board

STOP_WAIT_S = 10.0          # after the stop command: how long a board may take to send its stop record
STATUS_S = 0.5
LIVE_S = 0.25               # the newest event of a board is written at most this often
MONITOR_S = 10.0            # temperatures and the acquisition status
SILENT_WARN_S = 5.0         # a board without any data this long after the start gets a warning
RATE_WINDOW_S = 10.0


def stamp() -> str:
    return time.strftime("%H:%M:%S")


def write_json(path: str, data) -> None:
    tmp = f"{path}.part"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, path)


class BoardRun:
    """One board during the run: its connection, facts, file and counters (the reader thread updates these)."""

    def __init__(self, k: int, cfg_board: C.Board, n: int, chained: bool):
        self.k, self.cfg, self.name, self.uri = k, cfg_board, cfg_board.name, cfg_board.uri
        self.role = C.role(k, n) if chained else "single"
        self.board = None
        self.facts: dict = {}
        self.model: models.Model | None = None
        self.state = "waiting"
        self.message = ""
        self.writer: rawdata.RawWriter | None = None
        self.events = 0
        self.bytes = 0
        self.buffers = 0
        self.first_trigger = self.last_trigger = None
        self.last_timestamp = None
        self.start_record = self.stop_record = None
        self.ended = ""             # "stop record", "time-out", "error"
        self.error = ""
        self.done = False
        self.last_data = None
        self.live_at = 0.0
        self.history: deque = deque()       # (time, events, bytes), for the recent rates
        self.monitors: dict = {}
        self.wanted_monitors = None
        self.applied: list = []     # [path, value] as set
        self.record_length = cfg_board.record_length
        self.pre_trigger = cfg_board.pre_trigger
        self.warned_silent = False
        self.format_warned = False
        self.thread: threading.Thread | None = None
        self.lock = threading.Lock()

    def view(self, now: float) -> dict:
        with self.lock:
            hist = list(self.history)
        rate = mbps = None
        if len(hist) >= 2 and hist[-1][0] > hist[0][0]:
            dt = hist[-1][0] - hist[0][0]
            rate = (hist[-1][1] - hist[0][1]) / dt
            mbps = (hist[-1][2] - hist[0][2]) / dt / 1e6
        files = []
        if self.writer is not None:
            for p in self.writer.files:
                try:
                    files.append({"path": p, "size": os.path.getsize(p)})
                except OSError:
                    files.append({"path": p, "size": None})
        f = self.facts
        return {"index": self.k, "name": self.name, "uri": self.uri, "role": self.role, "state": self.state,
                "message": self.message, "model": f.get("model") or self.cfg.model, "serial": f.get("serial"),
                "fw_type": f.get("fw_type"), "fw_version": f.get("fw_version"), "channels": f.get("channels"),
                "adc_bits": f.get("adc_bits"), "adc_msps": f.get("adc_msps"),
                "sample_ns": C.sample_ns(self.cfg, self.model) if self.model else None,
                "record_length": self.record_length, "pre_trigger": self.pre_trigger,
                "events": self.events, "bytes": self.bytes, "rate": rate, "mbps": mbps,
                "first_trigger": self.first_trigger, "last_trigger": self.last_trigger,
                "last_timestamp": self.last_timestamp, "start_record": self.start_record,
                "stop_record": self.stop_record, "ended": self.ended, "files": files, "monitors": self.monitors,
                "age": None if self.last_data is None else now - self.last_data}


class Run:
    def __init__(self, run_dir: str, simulate: str | None = None):
        self.run_dir = os.path.abspath(run_dir)
        with open(os.path.join(self.run_dir, "config.json"), encoding="utf-8") as f:
            doc = json.load(f)
        self.doc = doc
        self.cfg = C.from_dict(doc["config"])
        self.note = doc.get("note", "")
        self.run_name = doc.get("run_name") or os.path.basename(self.run_dir)
        self.simulate = simulate
        active = [b for _, b in self.cfg.active()]
        n = len(active)
        self.chained = self.cfg.run.sync == "chain" and n > 1
        self.boards = [BoardRun(k, b, n, self.chained) for k, b in enumerate(active)]
        self.crate = None
        self.phase = "starting"
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.stop_reason = ""
        self.stop_requested = ""
        self.kill_requested = False
        self.t_start = None             # time.time() of the start command
        self.mono_start = None
        self.stopping_since = None
        self.live_dir = os.path.join(self.run_dir, "live")
        self.status_path = os.path.join(self.run_dir, "status.json")
        self._last_status = 0.0

    # ---- output ----
    def log(self, text: str, level: str = "") -> None:
        prefix = {"error": "ERROR: ", "warning": "WARNING: "}.get(level, "")
        print(f"{stamp()} {prefix}{text}", flush=True)
        if level == "error":
            self.errors.append(text)
        elif level == "warning":
            self.warnings.append(text)

    def status(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_status < STATUS_S:
            return
        self._last_status = now
        boards = [b.view(now) for b in self.boards]
        t = 0.0 if self.mono_start is None else (now - self.mono_start)
        data = {"phase": self.phase, "run_name": self.run_name, "pid": os.getpid(), "version": __version__,
                "simulator": self.simulate is not None, "started": self.t_start, "t": t,
                "duration": self.cfg.run.duration_s, "max_events": self.cfg.run.max_events,
                "sync": self.cfg.run.sync, "setup": self.cfg.trigger.setup, "boards": boards,
                "events": sum(b["events"] for b in boards), "bytes": sum(b["bytes"] for b in boards),
                "stop_reason": self.stop_reason, "stop_requested": self.stop_requested,
                "errors": self.errors[-20:], "warnings": self.warnings[-20:], "time": time.time()}
        try:
            write_json(self.status_path, data)
        except OSError as err:
            print(f"{stamp()} WARNING: cannot write {self.status_path}: {err}", flush=True)

    def set_phase(self, phase: str) -> None:
        self.phase = phase
        self.status(force=True)

    # ---- signals ----
    def on_signal(self, number, _frame) -> None:
        if self.stop_requested:
            self.kill_requested = True          # a second one: do not wait for the stop records
        self.stop_requested = signal.Signals(number).name

    # ---- the run ----
    def main(self) -> int:
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, self.on_signal)
        os.makedirs(self.live_dir, exist_ok=True)
        cfg = self.cfg
        self.log(f"ALAB DAQ v{__version__}: run {self.run_name}, {len(self.boards)} board(s), "
                 f"{'clock chain' if self.chained else 'independent boards' if len(self.boards) > 1 else 'one board'}, "
                 f"trigger setup {cfg.trigger.setup}" + (", SIMULATOR (no hardware)" if self.simulate is not None else ""))
        if not self.boards:
            self.log("no board is switched on in the configuration", "error")
            self.set_phase("failed")
            return 1
        code = 1
        try:
            if self.connect() and self.configure() and self.arm_and_start():
                self.take_data()
                self.stop_all()
            code = 0 if not self.errors and all(b.ended == "stop record" for b in self.boards) else 1
        except Exception as err:                    # anything unexpected: still close the boards
            self.log(f"internal error: {err!r}", "error")
            code = 1
        finally:
            self.close_all()
        self.summary()
        self.set_phase("finished" if code == 0 else "failed")
        return code

    def connect(self) -> bool:
        self.set_phase("connecting")
        if self.simulate is not None:
            from alab_daq.daq.sim import Crate
            cabling = {"external": "chain", "logic_or": "or_unit"}.get(self.cfg.trigger.setup, "none")
            if self.cfg.trigger.setup == "periodic" and self.cfg.trigger.software_target == "chain":
                cabling = "chain"
            try:
                self.crate = Crate(self.simulate, cabling, {b.name: b.cfg.model for b in self.boards})
            except ValueError as err:
                self.log(str(err), "error")
                return False
        for br in self.boards:                      # CAEN_FELib_Open is not thread safe: one after the other
            br.state = "connecting"
            self.status()
            try:
                br.board = open_board(br.uri, self.crate, br.name)
                br.facts = br.board.identify()
            except BoardError as err:
                br.state, br.message, br.ended = "error", str(err), "error"
                self.log(f"{br.name}: {err}", "error")
                return False
            f = br.facts
            self.log(f"{br.name} ({br.uri}, {br.role}): {f.get('model', '?')} serial {f.get('serial', '?')}, firmware "
                     f"{f.get('fw_type', '?')} {f.get('fw_version', '')}, {f.get('channels', '?')} channels, "
                     f"{f.get('adc_bits', '?')} bit, {f.get('adc_msps', '?')} MS/s")
            problem = self.check_board(br)
            if problem:
                br.state, br.message, br.ended = "error", problem, "error"
                self.log(f"{br.name}: {problem}", "error")
                return False
            br.state = "connected"
        return True

    def check_board(self, br: BoardRun) -> str:
        f = br.facts
        if f.get("fw_type", "").lower() != "scope":
            return (f"it runs the {f.get('fw_type', 'unknown')} firmware; ALAB DAQ needs the Scope firmware (the one "
                    "WaveDump2 uses): load it with CAEN's tools first")
        fam = models.family(f.get("model", ""))
        want = models.family(br.cfg.model)
        if fam and want and fam != want:
            return f"it is a {f['model']}; the configuration says {br.cfg.model}: correct the model there"
        br.model = models.from_board(f) if f.get("channels") else models.model(br.cfg.model)
        if f.get("channels") and f["channels"] != len(br.cfg.channels):
            return f"it has {f['channels']} channels; the configuration has {len(br.cfg.channels)}"
        return ""

    def apply(self, br: BoardRun, s: C.Setting, refused: set) -> bool:
        name = s.path.rsplit("/", 1)[-1].lower()
        if name in refused:
            return True
        try:
            br.board.set(s.path, s.value)
            br.applied.append([s.path, s.value])
            self.log(f"  {br.name}: {s.path} = {s.value}" + (f"   ({s.why})" if s.why else ""))
            return True
        except BoardError as err:
            if not s.alt:
                br.state, br.message, br.ended = "error", str(err), "error"
                self.log(f"{br.name}: the board refused {s.path} = {s.value}: {err}", "error")
                return False
            refused.add(name)
            self.log(f"{br.name}: {s.path} was refused ({err}); using the alternative", "warning")
            return all(self.apply(br, a, refused) for a in s.alt if a.path)

    def configure(self) -> bool:
        self.set_phase("configuring")
        for br in self.boards:
            br.state = "configuring"
            self.status()
            try:
                br.board.cmd("Reset")
            except BoardError as err:
                br.state, br.message, br.ended = "error", str(err), "error"
                self.log(f"{br.name}: {err}", "error")
                return False
            refused: set = set()
            for s in C.plan(self.cfg, br.k, br.model):
                if not self.apply(br, s, refused):
                    return False
            try:
                br.record_length = int(float(br.board.get("/par/RecordLengthS")))
                br.pre_trigger = int(float(br.board.get("/par/PreTriggerS")))
            except (BoardError, ValueError) as err:
                self.log(f"{br.name}: cannot read the record length back: {err}", "warning")
            ns = C.sample_ns(br.cfg, br.model)
            self.log(f"{br.name}: record {br.record_length} samples ({br.record_length * ns / 1000:g} µs), "
                     f"{br.pre_trigger} before the trigger, {ns:g} ns per sample")
            if br.record_length != br.cfg.record_length or br.pre_trigger != br.cfg.pre_trigger:
                self.log(f"{br.name}: the board made the record {br.record_length} samples and the pre-trigger "
                         f"{br.pre_trigger} (asked: {br.cfg.record_length}, {br.cfg.pre_trigger})", "warning")
            try:
                br.board.open_raw()
            except BoardError as err:
                br.state, br.message, br.ended = "error", str(err), "error"
                self.log(f"{br.name}: {err}", "error")
                return False
            if self.cfg.run.write_raw:
                try:
                    data_dir = os.path.expanduser(self.cfg.run.data_dir)
                    os.makedirs(data_dir, exist_ok=True)
                    br.writer = rawdata.RawWriter(os.path.join(data_dir, f"{self.run_name}_{br.name}"),
                                                  self.file_header(br), self.cfg.run.file_mb << 20,
                                                  on_file=lambda p, b=br: self.log(f"{b.name}: data file {p}"))
                except OSError as err:
                    self.log(f"{br.name}: cannot prepare the data file: {err}", "error")
                    return False
            br.state = "configured"
        return True

    def file_header(self, br: BoardRun) -> dict:
        f = br.facts
        return {"format": "alab-daq-raw", "version": 1, "daq_version": __version__, "run": self.run_name,
                "run_folder": self.run_dir, "note": self.note, "simulator": self.simulate is not None,
                "created": time.strftime("%Y-%m-%d %H:%M:%S"), "sync": self.cfg.run.sync,
                "trigger_setup": self.cfg.trigger.setup,
                "board": {"name": br.name, "uri": br.uri, "index": br.k, "role": br.role, "model": f.get("model"),
                          "serial": f.get("serial"), "fw_type": f.get("fw_type"), "fw_version": f.get("fw_version"),
                          "cup": f.get("cup"), "channels": f.get("channels"), "adc_bits": f.get("adc_bits"),
                          "adc_msps": f.get("adc_msps"), "sample_ns": C.sample_ns(br.cfg, br.model),
                          "record_length": br.record_length, "pre_trigger": br.pre_trigger,
                          "decimation": br.cfg.decimation},
                "applied": br.applied, "config": C.to_dict(self.cfg)}

    def arm_and_start(self) -> bool:
        self.set_phase("arming")
        for br in reversed(self.boards):            # the slaves first: they must be armed when the run arrives
            try:
                br.board.cmd("ArmAcquisition")
            except BoardError as err:
                br.state, br.message, br.ended = "error", str(err), "error"
                self.log(f"{br.name}: {err}", "error")
                return False
            br.state = "armed"
            self.log(f"{br.name}: armed")
        for br in self.boards:
            br.thread = threading.Thread(target=self.reader, args=(br,), name=f"read-{br.name}", daemon=True)
            br.thread.start()
        starters = C.start_order(self.cfg)
        self.t_start, self.mono_start = time.time(), time.monotonic()
        for k in starters:
            br = self.boards[k]
            try:
                br.board.cmd("SwStartAcquisition")
            except BoardError as err:
                br.state, br.message = "error", str(err)
                self.log(f"{br.name}: {err}", "error")
                self.stop_reason = "a board could not start"
                return True                     # the boards are armed: stop_all() stops them cleanly
        for br in self.boards:
            br.state = "running"
        who = "the master" if self.chained else "every board"
        self.log(f"run started: SwStartAcquisition sent to {who}")
        self.set_phase("running")
        return True

    def reader(self, br: BoardRun) -> None:
        """Reads one board until its stop record (or the time-out after the stop command)."""
        b, last_flush, last_hist = br.board, time.monotonic(), 0.0
        while True:
            try:
                data, n = b.read_raw(100)
            except BoardError as err:
                br.error, br.ended, br.state = str(err), "error", "error"
                self.log(f"{br.name}: {err}", "error")
                break
            now = time.monotonic()
            if now - last_hist >= 1.0:
                with br.lock:
                    br.history.append((now, br.events, br.bytes))
                    while br.history and now - br.history[0][0] > RATE_WINDOW_S:
                        br.history.popleft()
                last_hist = now
            if data is None:                        # nothing for 100 ms
                if self.kill_requested or (self.stopping_since is not None and now - self.stopping_since > STOP_WAIT_S):
                    br.ended = "time-out"
                    break
                continue
            if len(data) == 0:
                br.ended = "stop record"
                break
            try:
                sc = rawdata.scan(data)
            except rawdata.RawFormatError as err:
                sc = None
                if not br.format_warned:
                    br.format_warned = True
                    self.log(f"{br.name}: data that are not Scope events: {err}", "warning")
            if br.writer is not None:
                try:
                    br.writer.write(data)
                except OSError as err:
                    br.error, br.ended, br.state = str(err), "error", "error"
                    self.log(f"{br.name}: cannot write the data: {err}", "error")
                    break
            br.buffers += 1
            br.bytes += len(data)
            br.events += sc.events if sc else n
            br.last_data = now
            if sc is not None:
                if sc.first_scope is not None and br.first_trigger is None:
                    br.first_trigger = (struct.unpack_from(">Q", data, sc.first_scope)[0] >> 32) & 0xFFFFFF
                if sc.last_scope is not None:
                    w1, w2 = struct.unpack_from(">QQ", data, sc.last_scope)
                    br.last_trigger, br.last_timestamp = (w1 >> 32) & 0xFFFFFF, w2 & 0xFFFFFFFFFFFF
                    if now - br.live_at >= LIVE_S:
                        br.live_at = now
                        size = (w1 & 0xFFFFFFFF) * rawdata.WORD
                        self.write_live(br, bytes(data[sc.last_scope:sc.last_scope + size]))
                for _, sp in sc.specials:
                    if sp["kind"] == "start":
                        br.start_record = sp
                    elif sp["kind"] == "stop":
                        br.stop_record = sp
                        br.ended = "stop record"
            if br.writer is not None and now - last_flush > 1.0:
                br.writer.flush()
                last_flush = now
            if br.ended:
                break
        if br.writer is not None:
            br.writer.flush()
        if br.state != "error":
            br.state = "stopped"
        br.done = True

    def write_live(self, br: BoardRun, event: bytes) -> None:
        path = os.path.join(self.live_dir, f"{br.name}.evt")
        try:
            with open(f"{path}.part", "wb") as f:
                f.write(event)
            os.replace(f"{path}.part", path)
        except OSError:
            pass

    def take_data(self) -> None:
        cfg = self.cfg
        targets = C.software_targets(cfg)
        period = 1.0 / cfg.trigger.software_rate_hz if targets else None
        next_sw = time.monotonic() + (period or 0)
        next_monitor = 0.0
        while not self.stop_reason:
            now = time.monotonic()
            if self.stop_requested:
                self.stop_reason = f"Stop ({self.stop_requested})"
            elif cfg.run.duration_s and now - self.mono_start >= cfg.run.duration_s:
                self.stop_reason = "run time reached"
            elif cfg.run.max_events and self.boards[0].events >= cfg.run.max_events:
                self.stop_reason = f"{cfg.run.max_events} events on {self.boards[0].name}"
            elif any(b.state == "error" for b in self.boards):
                self.stop_reason = "a board failed"
            elif all(b.done for b in self.boards):
                self.stop_reason = "every board stopped by itself"
            if self.stop_reason:
                break
            if period is not None and now >= next_sw:
                for k in targets:
                    try:
                        self.boards[k].board.cmd("SendSwTrigger")
                    except BoardError as err:
                        self.log(f"{self.boards[k].name}: {err}", "warning")
                next_sw += period
                if now - next_sw > 1.0:             # far behind (a slow moment): do not send a burst
                    next_sw = now + period
            if now >= next_monitor:
                next_monitor = now + MONITOR_S
                for b in self.boards:
                    if b.wanted_monitors is None:
                        from alab_daq.daq.boards import MONITORS
                        b.wanted_monitors = dict(MONITORS)
                    b.monitors = monitors(b.board, b.wanted_monitors)
            for b in self.boards:
                if not b.warned_silent and b.last_data is None and now - self.mono_start > SILENT_WARN_S:
                    b.warned_silent = True
                    b.message = "no data since the start"
                    self.log(f"{b.name}: no data {SILENT_WARN_S:g} s after the start: is it armed, and (in a chain) is "
                             "its CLK-IN cable from the board before it in place?", "warning")
            self.status()
            wait = 0.05 if period is None else max(0.0005, min(0.05, next_sw - time.monotonic()))
            time.sleep(wait)
        self.log(f"stopping: {self.stop_reason}")

    def stop_all(self) -> None:
        self.set_phase("stopping")
        for b in self.boards:
            if b.state == "running":
                b.state = "stopping"
        for k in C.start_order(self.cfg):
            b = self.boards[k]
            try:
                b.board.cmd("SwStopAcquisition")
            except BoardError as err:
                self.log(f"{b.name}: {err}", "warning")
        self.stopping_since = time.monotonic()
        for b in self.boards:
            while b.thread is not None and b.thread.is_alive():
                b.thread.join(0.2)
                self.status()
        for b in self.boards:
            if b.ended == "time-out" and b.bytes == 0:
                self.log(f"{b.name} sent no data in this run: it did not start. Was it armed, and (in a chain) is its "
                         "CLK-IN cable from the board before it in place?", "warning")
            elif b.ended == "time-out":
                self.log(f"{b.name}: no stop record within {STOP_WAIT_S:g} s after the stop; the end of its data may "
                         "be missing", "warning")

    def close_all(self) -> None:
        for b in self.boards:
            if b.board is not None:
                try:
                    b.board.cmd("DisarmAcquisition")
                except BoardError:
                    pass                        # it disarmed by itself (EnAutoDisarmAcq), or it is gone
                b.board.close()
            if b.writer is not None:
                b.writer.close()

    def summary(self) -> None:
        for b in self.boards:
            files = ", ".join(os.path.basename(p) for p in (b.writer.files if b.writer else [])) or "none"
            ids = "" if b.first_trigger is None else f", trigger IDs {b.first_trigger}-{b.last_trigger}"
            self.log(f"{b.name}: {b.events} events, {b.bytes / 1e6:.2f} MB{ids}, ended: {b.ended or 'not started'}; "
                     f"files: {files}")
        ok = not self.errors and self.boards and all(b.ended == "stop record" for b in self.boards)
        self.log(f"run {self.run_name} {'ended' if ok else 'FAILED'}" + (f" ({self.stop_reason})" if self.stop_reason else ""))
