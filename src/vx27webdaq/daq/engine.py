"""The DAQ program of Vx27webDAQ: one run of every board of a configuration (it takes the place testDAQ has in
XIAOLAB DAQ). The web server starts it apart from itself (web/wrapper.py), so a run goes on when the server
restarts:

    python -m vx27webdaq.daq run RUN_FOLDER [--simulate "OPTIONS"]

RUN_FOLDER/config.json is the run's configuration (written by the server: the configuration, its note, the
run's name). The program prints its log (the server keeps it as daq.log) and writes into RUN_FOLDER:
    status.json      twice a second: the phase, each board's state, counters, files, housekeeping and statistics,
                     and the sync check (sync.py)
    live/NAME.evt    the newest event of each board, as the board sent it (the page's live waveforms; of an event
                     larger than LIVE_MAX_BYTES only its first samples of every channel)
    live/combined.set  one event of every board from the same moment (the page's combined view; write_set)
    control/N.done   the answer to the server's command control/N.json during the run: a Force trigger, or a
                     channel's DC offset or threshold (poll_control); every change is logged and kept in status
and the data, one stream per board, as the board sent it: DATA_FOLDER/RUN_NAME_BOARD.raw (and _001.raw ...
when a file reaches file_mb). See core/rawdata.py. Every board's housekeeping (temperatures, fans, DC-DC, error
flags, trigger and lost-trigger counters, dead time; boards.HK) every run.hk_s seconds goes into
DATA_FOLDER/RUN_NAME_BOARD_hk.csv (RUN_FOLDER/hk/ when no data are kept).

A run, in this order (CAEN's sync demo, caen-dig2 demo/caen-felib-demo-sync): connect to every board, one after
the other, and check its firmware (Scope) and model; reset it and set its parameters (core/config.py: plan);
activate the raw endpoint; arm every board, the last of the chain first; start one reader per board; send
SwStartAcquisition to the master (to every board when they are independent; none to a master with a hardware
start, whose run time begins with its first data). A Stop (SIGTERM, SIGINT or SIGHUP) before the boards are
started ends the run there. The run ends at its run time, when the first board has max_events events, on SIGTERM,
SIGINT or SIGHUP (Stop on the page), when a board fails or a board stops by itself: SwStopAcquisition to the
master (or every board); each reader goes on until its board's stop record; a board without one STOP_WAIT_S after
the stop gets SwStopAcquisition and DisarmAcquisition itself, and its reader ends when it is quiet, at the latest
STOP_LIMIT_S after the stop (a second Stop: at once); then every board is disarmed and closed. Exit code 0: every
board ended with its stop record and nothing went wrong; 1: anything else.
"""

from __future__ import annotations

import json
import math
import os
import signal
import struct
import sys
import threading
import time
from collections import deque

import numpy as np

from vx27webdaq import __version__
from vx27webdaq.core import config as C
from vx27webdaq.core import models, rawdata
from vx27webdaq.daq.boards import HK, BoardError, channel_rates, housekeeping, open_board, send_setting
from vx27webdaq.daq.sync import SyncCheck

STOP_WAIT_S = 10.0          # after the stop command: how long a board may take to send its stop record
STOP_LIMIT_S = 60.0         # a board still sending this long after the stop (it never got the stop, or ignores it):
                            # its reader ends. Generous: a board may still be sending what its memory holds
STATUS_S = 0.5
LIVE_S = 0.25               # the newest event of a board is written at most this often
SYNC_S = 0.5                # how often the sync check counts the triggers every board has passed
LIVE_PARAMS = ("dcoffset", "triggerthr")       # what the server may change while the boards run
STAT_KEYS = ("events", "icr_hz", "rate_hz", "dead_frac")      # derived columns of the housekeeping file
SILENT_WARN_S = 5.0         # a board without any data this long after the start gets a warning
START_REMIND_S = 60.0       # a master waiting for its hardware start: a line in the log this often
RATE_WINDOW_S = 10.0
GRID_SHIFT = 25             # timestamp cells of 2**25 ticks (0.27 s): each board keeps the first event of each
GRID_KEEP = 16             # cells kept per board
SET_S = 0.3                # how often the matched set is written (when there is a newer one)
TS_MASK = (1 << 48) - 1
# The live views keep and write whole events, but check_config allows records of up to 6.25 M samples: 64 channels
# of those are 800 MB per event, so 16 kept per board, or one written four times a second, would take the memory
# and the disk. 8 MB is still 64 channels of 65536 samples (more than the page draws), so usual records stay whole.
PICK_MAX_BYTES = 8 << 20    # the combined view keeps no larger event (that board then has none: it says so once)
PICKS_MAX_BYTES = 32 << 20  # and at most this much per board (the oldest go first)
LIVE_MAX_BYTES = 8 << 20    # a live/NAME.evt holds at most this: the first samples of every channel of the event


def _cell(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float):
        return f"{v:.6g}"
    return str(v).replace(",", ";")


def stamp() -> str:
    return time.strftime("%H:%M:%S")


def finite(value):
    """value with every number that is not finite (NaN, ±Infinity: a reading a board could not make) as None. JSON
    has no such numbers: a strict reader, like the browser's JSON.parse, refuses the whole file for one of them."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {k: finite(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [finite(v) for v in value]
    return value


def write_json(path: str, data) -> None:
    tmp = f"{path}.part"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(finite(data), f, allow_nan=False)
    os.replace(tmp, path)


def cut_event(data, o: int, max_bytes: int) -> tuple[bytes, int]:
    """(the Scope event at offset o, as bytes, at most max_bytes long; samples per channel kept). A longer event
    keeps its first samples of every channel: they come 4 per word, one word per channel in turn, so the first
    words are a shorter event of the same channels (with its size set to them)."""
    w1, w2, w3 = struct.unpack_from(">QQQ", data, o)
    n_words = w1 & 0xFFFFFFFF
    n_ch = bin(w3).count("1")
    if n_words * rawdata.WORD <= max_bytes or n_ch == 0:
        return bytes(data[o:o + n_words * rawdata.WORD]), (n_words - rawdata.SCOPE_HEADER_WORDS) * rawdata.SAMPLES_PER_WORD // max(1, n_ch)
    groups = (max_bytes // rawdata.WORD - rawdata.SCOPE_HEADER_WORDS) // n_ch
    keep = rawdata.SCOPE_HEADER_WORDS + groups * n_ch
    head = struct.pack(">QQQ", (w1 & ~0xFFFFFFFF) | keep, w2, w3)
    start = o + rawdata.SCOPE_HEADER_WORDS * rawdata.WORD
    return head + bytes(data[start:o + keep * rawdata.WORD]), groups * rawdata.SAMPLES_PER_WORD


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
        self.stopped_itself = False     # its stop record came before the stop command
        self.still_sending = False      # its reader ended STOP_LIMIT_S after the stop, the board still sending
        self.write_failed = False       # a write or flush of its data file failed
        self.last_data = None
        self.live_at = 0.0
        self.cell = None            # the newest timestamp cell seen (pick_cells)
        self.picks: deque = deque(maxlen=GRID_KEEP)     # (cell, trigger ID, timestamp, event bytes)
        self.history: deque = deque()       # (time, events, bytes), for the recent rates
        self.monitors: dict = {}            # the newest housekeeping values (boards.HK)
        self.hk_wanted: dict | None = None
        self.hk_seen: set = set()           # housekeeping keys read once (boards.housekeeping)
        self.hk_prev: dict | None = None
        self.stats: dict = {}               # from the counters: icr_hz, rate_hz, dead_frac, triggers, lost
        self.ch_rates: dict = {}            # channel -> self-trigger rate (Hz)
        self.rates_state: dict = {}
        self.hk_file: str | None = None
        self.hk_out = None
        self.hk_cols: list | None = None
        self.hk_rows = 0
        self.error_flags = 0
        self.temp_warned = False
        self.board_fail = False
        self.sync_q: deque = deque()        # (trigger IDs made continuous, timestamps) per buffer, for sync.py
        self.id_last_raw = None
        self.id_base = 0
        self.applied: list = []     # [path, value] as set
        self.record_length = cfg_board.record_length
        self.pre_trigger = cfg_board.pre_trigger
        self.warned_silent = False
        self.format_warned = False
        self.live_cut_warned = False
        self.pick_warned = False
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
                "stats": self.stats, "ch_rates": {str(c): v for c, v in self.ch_rates.items()}, "hk_file": self.hk_file,
                "error_flags": self.error_flags, "board_fail": self.board_fail,
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
        self.t_start = None             # time.time() of the start command (of the master's first data: hardware start)
        self.mono_start = None          # the same in time.monotonic(): the run time counts from it
        self.stop_sent = False          # the stop command is going out: from now on a stop record is its answer
        self.stopping_since = None      # time.monotonic() once the stop command went out
        self.hw_start = self.boards[0].cfg.io_start if self.boards else ""     # the master starts on S-IN, ...
        self.level_ended = False        # the master's S-IN level (SINlevel) ended its run
        self.armed_at = None
        self.live_dir = os.path.join(self.run_dir, "live")
        self.status_path = os.path.join(self.run_dir, "status.json")
        self._last_status = 0.0
        self._log_lock = threading.Lock()
        self._state_lock = threading.Lock()     # board states: the readers and the main thread both change them
        self.set_cell = None            # (cell, timestamp) of the last live/combined.set
        self.control_dir = os.path.join(self.run_dir, "control")
        self.changes: list[dict] = []   # what was changed during the run (poll_control)
        # the sync check compares the trigger IDs and timestamps of every board: it needs one trigger for all (the
        # same IDs) and one clock (the same time): with independent boards the timestamps have no common time
        self.sync, self.sync_why = None, ""
        if len(self.boards) < 2:
            self.sync_why = "one board"
        elif not C.common_trigger(self.cfg):
            self.sync_why = "each board triggers by itself"
        elif self.cfg.run.sync != "chain":
            self.sync_why = "independent boards: no common clock"
        else:
            self.sync = SyncCheck([b.name for b in self.boards], self.cfg.run.sync_window_ns)

    # ---- output ----
    def log(self, text: str, level: str = "") -> None:
        prefix = {"error": "ERROR: ", "warning": "WARNING: "}.get(level, "")
        with self._log_lock:            # the reader threads log too: one whole line at a time (print writes the
            sys.stdout.write(f"{stamp()} {prefix}{text}\n")     # text and the newline apart, so lines could mix)
            sys.stdout.flush()
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
                "matching": self.sync.view() if self.sync else {"applies": False, "why": self.sync_why},
                "events": sum(b["events"] for b in boards), "bytes": sum(b["bytes"] for b in boards),
                "stop_reason": self.stop_reason, "stop_requested": self.stop_requested,
                "errors": self.errors[-20:], "warnings": self.warnings[-20:], "time": time.time(),
                "changes": self.changes[-200:], "force_targets": [self.boards[k].name for k in C.force_targets(self.cfg)]}
        try:
            write_json(self.status_path, data)
        except OSError as err:
            print(f"{stamp()} WARNING: cannot write {self.status_path}: {err}", flush=True)

    def set_phase(self, phase: str) -> None:
        self.phase = phase
        self.status(force=True)

    # ---- the boards' states (the readers and the main thread change them) ----
    def board_error(self, br: BoardRun, message: str) -> None:
        with self._state_lock:
            br.state, br.message, br.ended, br.error = "error", message, "error", message
        self.log(f"{br.name}: {message}", "error")

    def write_error(self, br: BoardRun, err: Exception) -> None:
        br.write_failed = True                  # (the reader's last flush would only fail the same way)
        self.board_error(br, f"cannot write the data: {err}")

    def end(self, br: BoardRun, how: str) -> None:
        """How the board's run ended ("stop record", "time-out"); an error stays the end."""
        with self._state_lock:
            if br.ended != "error":
                br.ended = how

    def promote(self, br: BoardRun) -> None:
        """armed -> running; never over an error a reader set in the meantime."""
        with self._state_lock:
            if br.state == "armed":
                br.state = "running"

    # ---- signals ----
    def on_signal(self, number, _frame) -> None:
        # a second Stop while the boards are stopping (the stop command is out): do not wait for the stop records.
        # A second one before that (Stop twice, a SIGHUP after a SIGTERM) is only a Stop: the readers must still
        # get the boards' last data and stop records after the stop command
        if self.stop_requested and self.stopping_since is not None:
            self.kill_requested = True
        if not self.stop_requested:
            self.stop_requested = signal.Signals(number).name

    def stop_before_start(self) -> bool:
        """A Stop came while the boards were connected, configured or armed: the run ends without starting them."""
        if not self.stop_requested:
            return False
        if not self.stop_reason:
            self.stop_reason = f"Stop ({self.stop_requested}) before the start"
            self.log(f"Stop ({self.stop_requested}) before the boards were started: the run ends without data")
        return True

    # ---- the run ----
    def main(self) -> int:
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):     # SIGHUP: the wrapper passes it on (a closed
            signal.signal(sig, self.on_signal)                          # session): a clean stop too, never a kill
        try:
            self.run()
        except Exception as err:                    # anything unexpected, even while closing: still the end below
            self.log(f"internal error: {err!r}", "error")
        code = 0 if self.boards and not self.errors and all(b.ended == "stop record" for b in self.boards) else 1
        try:
            self.summary()
        except Exception as err:
            self.log(f"internal error in the summary: {err!r}", "error")
            code = 1
        finally:
            self.set_phase("finished" if code == 0 else "failed")
        return code

    def run(self) -> None:
        os.makedirs(self.live_dir, exist_ok=True)
        os.makedirs(self.control_dir, exist_ok=True)
        cfg = self.cfg
        self.log(f"Vx27webDAQ v{__version__}: run {self.run_name}, {len(self.boards)} board(s), "
                 f"{'clock chain' if self.chained else 'independent boards' if len(self.boards) > 1 else 'one board'}, "
                 f"trigger setup {cfg.trigger.setup}" + (", SIMULATOR (no hardware)" if self.simulate is not None else ""))
        if not self.boards:
            self.log("no board is switched on in the configuration", "error")
            return
        try:
            if self.connect() and self.configure() and self.arm_and_start():
                self.take_data()
        except Exception as err:                    # anything unexpected: still stop and close the boards
            self.log(f"internal error: {err!r}", "error")
        finally:
            try:
                # once a reader runs, the boards may be acquiring: stop them (and end the readers) before they are
                # closed, whatever happened (a CAEN_FELib_Close during a ReadData of another thread is never safe)
                if any(b.thread is not None for b in self.boards):
                    self.stop_all()
            except Exception as err:
                self.log(f"internal error while stopping the boards: {err!r}", "error")
            finally:
                self.close_all()

    def connect(self) -> bool:
        self.set_phase("connecting")
        if self.simulate is not None:
            from vx27webdaq.daq.sim import Crate
            cabling = {"external": "chain", "logic_or": "or_unit", "master": "chain"}.get(self.cfg.trigger.setup, "none")
            if self.cfg.trigger.setup == "periodic" and self.cfg.trigger.software_target == "chain":
                cabling = "chain"
            try:
                self.crate = Crate(self.simulate, cabling, {b.name: b.cfg.model for b in self.boards},
                                   {b.name: len(b.cfg.channels) for b in self.boards})
            except ValueError as err:
                self.log(str(err), "error")
                return False
        for br in self.boards:                      # CAEN_FELib_Open is not thread safe: one after the other
            if self.stop_before_start():
                return False
            br.state = "connecting"
            self.status()
            try:
                br.board = open_board(br.uri, self.crate, br.name)
                br.facts = br.board.identify()
            except BoardError as err:
                self.board_error(br, str(err))
                return False
            f = br.facts
            self.log(f"{br.name} ({br.uri}, {br.role}): {f.get('model', '?')} serial {f.get('serial', '?')}, firmware "
                     f"{f.get('fw_type', '?')} {f.get('fw_version', '')}, {f.get('channels', '?')} channels, "
                     f"{f.get('adc_bits', '?')} bit, {f.get('adc_msps', '?')} MS/s")
            problem = self.check_board(br)
            if problem:
                self.board_error(br, problem)
                return False
            br.state = "connected"
        return True

    def check_board(self, br: BoardRun) -> str:
        f = br.facts
        if f.get("fw_type", "").lower() != "scope":
            return (f"it runs the {f.get('fw_type', 'unknown')} firmware; Vx27webDAQ needs the Scope firmware (the one "
                    "WaveDump2 uses): load it with CAEN's tools first")
        fam = models.family(f.get("model", ""))
        want = models.family(br.cfg.model)
        if fam and want and fam != want:
            return f"it is a {f['model']}; the configuration says {br.cfg.model}: correct the model there"
        br.model = models.from_board(f) if f.get("channels") else models.model(br.cfg.model)
        if f.get("channels") and f["channels"] != len(br.cfg.channels):
            return (f"it has {f['channels']} channels; the configuration has {len(br.cfg.channels)}: set its channels "
                    f"to {f['channels']} on the Configuration page (or Use what the boards said)")
        return ""

    def apply(self, br: BoardRun, s: C.Setting, refused: set) -> bool:
        """One setting of plan(), by the rules of boards.send_setting; False: the board's configuration ends."""
        def took(x: C.Setting) -> None:
            br.applied.append([x.path, x.value])
            self.log(f"  {br.name}: {x.path} = {x.value}" + (f"   ({x.why})" if x.why else ""))
        try:
            send_setting(br.board, s, refused, took, lambda text: self.log(f"{br.name}: {text}", "warning"))
            return True
        except BoardError as err:
            self.board_error(br, str(err))
            return False

    def configure(self) -> bool:
        self.set_phase("configuring")
        for br in self.boards:
            if self.stop_before_start():
                return False
            br.state = "configuring"
            self.status()
            try:
                br.board.cmd("Reset")
            except BoardError as err:
                self.board_error(br, str(err))
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
                self.board_error(br, str(err))
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
            if self.stop_before_start():
                return False
            try:
                br.board.cmd("ArmAcquisition")
            except BoardError as err:
                self.board_error(br, str(err))
                return False
            br.state = "armed"
            self.log(f"{br.name}: armed")
        if self.stop_before_start():                # armed, not started: close_all() disarms them
            return False
        starters = C.start_order(self.cfg)
        hw = self.hw_start
        if hw:                                  # the master starts on a hardware signal: no software start, and the
            starters = [k for k in starters if k != 0]      # run time counts from its first data (read_loop)
            self.armed_at = time.monotonic()
            self.log(f"the run starts when the master sees its start signal ({hw}): waiting for the start signal")
        else:
            self.t_start, self.mono_start = time.time(), time.monotonic()
        for br in self.boards:
            br.thread = threading.Thread(target=self.reader, args=(br,), name=f"read-{br.name}", daemon=True)
            br.thread.start()
        for k in starters:
            br = self.boards[k]
            try:
                br.board.cmd("SwStartAcquisition")
            except BoardError as err:
                self.board_error(br, str(err))
                self.stop_reason = "a board could not start"
                return True                     # the boards are armed: stop_all() stops them cleanly
        # the boards started now: armed -> running, never over an error a reader set meanwhile (a failed first read).
        # With a hardware start each board's reader does it at its first data
        for br in (self.boards if not hw else [self.boards[k] for k in starters]):
            self.promote(br)
        who = "the master" if self.chained else "every board"
        if not hw:
            self.log(f"run started: SwStartAcquisition sent to {who}")
        self.set_phase("running")
        return True

    def reader(self, br: BoardRun) -> None:
        """Reads one board until its stop record (or the time-out after the stop command). It never ends silently:
        any failure, also of the file, ends the board with an error that the main loop sees."""
        try:
            self.read_loop(br)
        except Exception as err:
            self.board_error(br, f"its data reader failed: {err!r}")
        finally:
            if br.writer is not None and not br.write_failed:
                try:
                    br.writer.flush()
                except Exception as err:            # a full disk, an I/O error: the data in the buffer are lost
                    self.write_error(br, err)
            with self._state_lock:
                if br.state != "error":
                    br.state = "stopped"
            br.done = True

    def read_loop(self, br: BoardRun) -> None:
        b, last_flush, last_hist = br.board, time.monotonic(), 0.0
        while True:
            now = time.monotonic()
            if self.stopping_since is not None:     # on every pass: a board that keeps sending is never quiet
                if self.kill_requested:             # a second Stop during the stop: do not wait for the stop record
                    self.end(br, "time-out")
                    break
                if now - self.stopping_since > STOP_LIMIT_S:
                    br.still_sending = True
                    self.end(br, "time-out")
                    break
            try:
                data, n = b.read_raw(100)
            except BoardError as err:
                self.board_error(br, str(err))
                break
            now = time.monotonic()
            if now - last_hist >= 1.0:
                with br.lock:
                    br.history.append((now, br.events, br.bytes))
                    while br.history and now - br.history[0][0] > RATE_WINDOW_S:
                        br.history.popleft()
                last_hist = now
            if data is None:                        # nothing for 100 ms
                if self.stopping_since is not None and now - self.stopping_since > STOP_WAIT_S:
                    self.end(br, "time-out")
                    break
                continue
            if len(data) == 0:                      # FELib's STOP: the acquisition ended, after the last data
                self.stopped(br)
                break
            if br.writer is not None:               # first into the file, exactly as it came; then the analysis
                try:
                    br.writer.write(data)
                except Exception as err:
                    self.write_error(br, err)
                    break
            br.buffers += 1
            br.bytes += len(data)
            br.last_data = now
            if self.hw_start and self.mono_start is None and br.k == 0:     # the run begins with the master's data
                self.t_start, self.mono_start = time.time(), now
                self.log(f"run started: the master saw its start signal ({self.hw_start})")
            if br.state == "armed":
                self.promote(br)
            try:
                found, got_stop = self.analyse(br, data, now)
            except Exception as err:                # data the analysis does not expect: they are in the file
                found, got_stop = None, False
                if not br.format_warned:
                    br.format_warned = True
                    self.log(f"{br.name}: data that are not Scope events: {err}", "warning")
            br.events += found if found is not None else n
            if br.writer is not None and now - last_flush > 1.0:
                try:
                    br.writer.flush()
                except Exception as err:
                    self.write_error(br, err)
                    break
                last_flush = now
            if got_stop:                            # its stop record: the board sends nothing after it
                break

    def analyse(self, br: BoardRun, data, now: float) -> tuple[int, bool]:
        """One buffer of the board (already in its file): the counters, the live views, the sync check, the start
        and stop records. Returns its number of Scope events, and whether the stop record was in it."""
        got_stop = False
        offs: list = []
        sc = rawdata.scan(data, offsets=offs)
        if sc.first_scope is not None and br.first_trigger is None:
            br.first_trigger = (struct.unpack_from(">Q", data, sc.first_scope)[0] >> 32) & 0xFFFFFF
        if sc.last_scope is not None:
            w1, w2 = struct.unpack_from(">QQ", data, sc.last_scope)
            br.last_trigger, br.last_timestamp = (w1 >> 32) & 0xFFFFFF, w2 & 0xFFFFFFFFFFFF
            if now - br.live_at >= LIVE_S:
                br.live_at = now
                self.write_live(br, data, sc.last_scope)
        if offs:
            words = np.frombuffer(data, dtype=">u8", count=len(data) // rawdata.WORD)
            idx = np.asarray(offs, dtype=np.int64) // rawdata.WORD
            w1, w2 = words[idx], words[idx + 1]
            self.pick_cells(br, data, offs, w1, w2)
            if self.sync is not None:
                self.sync_push(br, w1, w2)
            if not br.board_fail and np.any((w1 >> np.uint64(56)) & np.uint64(1)):
                br.board_fail = True
                self.log(f"{br.name}: its events carry the board-fail flag: the board reports a failure "
                         "(a lost clock, an overheated part); see its housekeeping", "warning")
        for _, sp in sc.specials:
            if sp["kind"] == "start":
                br.start_record = sp
            elif sp["kind"] == "stop":
                br.stop_record = sp
                got_stop = True
                self.stopped(br)
        return sc.events, got_stop

    def stopped(self, br: BoardRun) -> None:
        """The board's acquisition ended (its stop record, or FELib's STOP). Before the stop command that is a board
        that stopped by itself: the run must not go on without it (exit code 1), except where the master starts
        on the S-IN level, whose end stops the run (the master, and in a chain every board with it)."""
        if br.ended == "stop record":
            return
        self.end(br, "stop record")
        if self.stop_sent or br.ended != "stop record":
            return
        if self.hw_start == "SINlevel" and (br.k == 0 or self.chained):
            self.level_ended = True
            self.log(f"{br.name}: its acquisition ended with the S-IN level (the master starts on SINlevel)")
            return
        br.stopped_itself = True
        advice = ("check its CLK-IN cable from the board before it, and its error flags" if self.chained and br.k > 0
                  else "check its error flags and its network link")      # (a master runs on its own clock)
        self.log(f"{br.name} stopped by itself during the run (its stop record came before the stop command): {advice}",
                 "error")

    def write_live(self, br: BoardRun, data, o: int) -> None:
        event, samples = cut_event(data, o, LIVE_MAX_BYTES)
        if len(event) < (struct.unpack_from(">Q", data, o)[0] & 0xFFFFFFFF) * rawdata.WORD and not br.live_cut_warned:
            br.live_cut_warned = True
            self.log(f"{br.name}: its events are larger than {LIVE_MAX_BYTES >> 20} MB: the live view shows the first "
                     f"{samples} samples of each channel; the data files keep every sample")
        path = os.path.join(self.live_dir, f"{br.name}.evt")
        try:
            with open(f"{path}.part", "wb") as f:
                f.write(event)
            os.replace(f"{path}.part", path)
        except OSError:
            pass

    def pick_cells(self, br: BoardRun, data, offs: list, w1: np.ndarray, w2: np.ndarray) -> None:
        """Keep the first event of every new timestamp cell (2**GRID_SHIFT ticks). Boards that share the clock
        (a clock chain) and the trigger keep the same triggers, so write_set can show one moment on all. A cell is
        new when it differs from the one before: after a timestamp reset (S-IN, GPIO) the cells are smaller again."""
        cells = ((w2 & np.uint64(TS_MASK)) >> np.uint64(GRID_SHIFT)).astype(np.int64)
        before = np.concatenate(([-1 if br.cell is None else br.cell], cells[:-1]))
        new = np.nonzero(cells != before)[0][-GRID_KEEP:]
        br.cell = int(cells[-1])
        for i in new:
            o = offs[int(i)]
            a = int(w1[i])
            size = (a & 0xFFFFFFFF) * rawdata.WORD
            if size > PICK_MAX_BYTES:
                if not br.pick_warned:
                    br.pick_warned = True
                    self.log(f"{br.name}: its events are larger than {PICK_MAX_BYTES >> 20} MB: the combined live view "
                             "keeps no events this large, so it cannot show this run; the data files keep them all")
                continue
            pick = (int(cells[i]), (a >> 32) & 0xFFFFFF, int(w2[i]) & TS_MASK, bytes(data[o:o + size]))
            with br.lock:
                br.picks.append(pick)
                total = sum(len(p[3]) for p in br.picks)
                while total > PICKS_MAX_BYTES and len(br.picks) > 1:
                    total -= len(br.picks.popleft()[3])

    def sync_push(self, br: BoardRun, w1: np.ndarray, w2: np.ndarray) -> None:
        """Hand this buffer's trigger IDs (continuous over the 24-bit wrap) and timestamps to the sync check."""
        raw = ((w1 >> np.uint64(32)) & np.uint64(0xFFFFFF)).astype(np.int64)
        prev = raw[0] if br.id_last_raw is None else br.id_last_raw
        wraps = np.cumsum(np.diff(np.concatenate(([prev], raw))) < -(1 << 23))
        ids = raw + ((br.id_base + wraps) << 24)
        br.id_base += int(wraps[-1])
        br.id_last_raw = int(raw[-1])
        with br.lock:
            br.sync_q.append((ids, (w2 & np.uint64(TS_MASK)).astype(np.int64)))

    def check_sync(self, final: bool = False) -> None:
        if self.sync is None:
            return
        for k, b in enumerate(self.boards):
            with b.lock:
                items, b.sync_q = list(b.sync_q), deque()
            for ids, ts in items:
                self.sync.add(k, ids, ts)
        before = self.sync.out_of_sync
        self.sync.process(final)
        if before == 0 and self.sync.out_of_sync and self.sync.first_bad:
            fb = self.sync.first_bad
            self.log(f"sync check: trigger ID {fb['trigger_id']} has timestamps "
                     + ", ".join(f"{n} {ts}" for n, ts in fb["timestamps"].items())
                     + f" (8 ns ticks): more than {self.cfg.run.sync_window_ns} ns apart", "warning")

    # ---- commands of the server during the run ----
    def poll_control(self) -> None:
        """Do the server's commands (control/N.json, in order) and answer each in control/N.done."""
        try:
            names = sorted(n for n in os.listdir(self.control_dir) if n.endswith(".json"))
        except OSError:
            return
        for n in names:
            path = os.path.join(self.control_dir, n)
            try:
                with open(path, encoding="utf-8") as f:
                    req = json.load(f)
            except (OSError, ValueError):
                req = None
            res = self.do_control(req) if isinstance(req, dict) else {"ok": False, "message": "not a command"}
            try:
                write_json(path[:-5] + ".done", res)
                os.remove(path)
            except OSError:
                pass

    def do_control(self, req: dict) -> dict:
        who = str(req.get("user") or "?")[:40]
        if self.phase != "running":
            return {"ok": False, "message": f"the run is {self.phase}"}
        if req.get("cmd") == "swtrigger":
            targets = [self.boards[k] for k in C.force_targets(self.cfg)]
            if not targets:
                return {"ok": False, "message": "in this trigger setup the triggers come from outside: no software trigger"}
            sent = []
            for b in targets:
                try:
                    b.board.cmd("SendSwTrigger")
                    sent.append(b.name)
                except BoardError as err:
                    self.log(f"{b.name}: {err}", "warning")
            self.log(f"Force trigger ({who}): software trigger to {', '.join(sent) or 'no board'}")
            return {"ok": bool(sent), "message": f"software trigger to {', '.join(sent)}" if sent else "no board took it",
                    "boards": sent}
        if req.get("cmd") == "set":
            b = next((x for x in self.boards if x.name == req.get("board")), None)
            path, value = str(req.get("path", "")), str(req.get("value", ""))
            parts = path.split("/")
            if b is None:
                return {"ok": False, "message": f"no board {req.get('board')} in this run"}
            if len(parts) != 5 or parts[1] != "ch" or not parts[2].isdigit() or parts[4].lower() not in LIVE_PARAMS:
                return {"ok": False, "message": f"{path}: only a channel's DCOffset or TriggerThr changes during a run"}
            change = {"time": time.time(), "user": who, "board": b.name, "path": path, "value": value}
            try:
                b.board.set(path, value)
                change["read_back"] = b.board.get(path)
                change["ok"] = True
                self.log(f"changed during the run ({who}): {b.name} {path} = {value}")
            except BoardError as err:
                change.update(ok=False, error=str(err))
                self.log(f"{b.name}: the board refused {path} = {value} during the run: {err}", "warning")
            self.changes.append(change)
            return {"ok": change["ok"], "message": change.get("error") or f"{b.name} {path} = {change.get('read_back', value)}",
                    "value": change.get("read_back")}
        return {"ok": False, "message": f"unknown command {req.get('cmd')}"}

    # ---- housekeeping ----
    def sample_hk(self, b: BoardRun, now: float) -> None:
        """The board's housekeeping: into b.monitors and b.stats (status.json), and a row of its _hk.csv."""
        if b.board is None:
            return
        if b.hk_wanted is None:
            b.hk_wanted = dict(HK)
        vals = housekeeping(b.board, b.hk_wanted, b.hk_seen)
        b.ch_rates = channel_rates(b.board, [c for c, ch in enumerate(b.cfg.channels) if ch.enabled], b.rates_state)
        run_s = 0.0 if self.mono_start is None else now - self.mono_start
        stats = {"events": b.events}
        p = b.hk_prev
        if p is not None and run_s > p["run_s"]:
            dt = run_s - p["run_s"]
            stats["rate_hz"] = (b.events - p["events"]) / dt
            if isinstance(vals.get("trigger_cnt"), int) and isinstance(p.get("trigger_cnt"), int):
                stats["icr_hz"] = max(0, vals["trigger_cnt"] - p["trigger_cnt"]) / dt
            if all(isinstance(x, int) for x in (vals.get("realtime"), vals.get("deadtime"), p.get("realtime"), p.get("deadtime"))):
                real = vals["realtime"] - p["realtime"]
                if real > 0:
                    stats["dead_frac"] = min(1.0, max(0.0, (vals["deadtime"] - p["deadtime"]) / real))
        if isinstance(vals.get("trigger_cnt"), int):
            stats["triggers"] = vals["trigger_cnt"]
        if isinstance(vals.get("lost_trigger_cnt"), int):
            stats["lost"] = vals["lost_trigger_cnt"]
        b.hk_prev = dict(vals, run_s=run_s, events=b.events)
        b.monitors, b.stats = vals, stats
        self.write_hk(b, run_s, vals, stats)
        self.check_alarms(b, vals)

    def write_hk(self, b: BoardRun, run_s: float, vals: dict, stats: dict) -> None:
        """A row of the board's _hk.csv. A disk or NAS error there is a warning, once, and the housekeeping file is
        written no more: it must never stop the run, or skip the stop of the boards."""
        if b.hk_out is False:
            return
        if self.cfg.run.write_raw:
            folder = os.path.expanduser(self.cfg.run.data_dir)
        else:
            folder = os.path.join(self.run_dir, "hk")
        try:
            if b.hk_out is None:
                os.makedirs(folder, exist_ok=True)
                b.hk_file = os.path.join(folder, f"{self.run_name}_{b.name}_hk.csv")
                try:
                    b.hk_out = open(b.hk_file, "x", encoding="utf-8")
                except OSError:
                    b.hk_file = None
                    raise
                b.hk_cols = list(vals) + [f"ch{c}_trg_rate" for c in sorted(b.ch_rates)]
                b.hk_out.write(",".join(["time", "run_s"] + b.hk_cols + list(STAT_KEYS)) + "\n")
            row = {**vals, **{f"ch{c}_trg_rate": v for c, v in b.ch_rates.items()}}
            cells = [f"{time.time():.3f}", f"{run_s:.3f}"] + [_cell(row.get(k)) for k in b.hk_cols] + [_cell(stats.get(k)) for k in STAT_KEYS]
            b.hk_out.write(",".join(cells) + "\n")
            b.hk_out.flush()
            b.hk_rows += 1
        except (OSError, ValueError) as err:
            self.log(f"{b.name}: cannot write its housekeeping file {b.hk_file or folder}: {err}; its housekeeping is "
                     "no longer written (the run goes on)", "warning")
            f, b.hk_out = b.hk_out, False
            if f:
                try:
                    f.close()
                except (OSError, ValueError):
                    pass

    def check_alarms(self, b: BoardRun, vals: dict) -> None:
        flags = vals.get("error_flags")
        if isinstance(flags, int) and flags != b.error_flags:
            if flags:
                self.log(f"{b.name}: the board reports error flags 0x{flags:x} (ErrorFlags: e.g. an overheated part or a "
                         "PLL that lost its lock; the board's web page or CAEN's manual names each bit)", "warning")
                b.message = f"error flags 0x{flags:x}"
            else:
                self.log(f"{b.name}: its error flags are clear again")
                b.message = ""
            b.error_flags = flags
        limit = self.cfg.run.temp_warn_c
        temps = [v for k, v in vals.items() if k.startswith("temp") and isinstance(v, (int, float))]
        if limit > 0 and temps:
            hot = max(temps)
            if hot >= limit and not b.temp_warned:
                b.temp_warned = True
                self.log(f"{b.name}: {hot:.1f} °C, at or above the warning of {limit:g} °C: check its cooling", "warning")
            elif hot < limit - 2:
                b.temp_warned = False

    def write_set(self) -> None:
        """live/combined.set: the newest moment (timestamp cell) every board kept, one event of each
        (rawdata.pack_set). The newest by arrival, not by the cell's number: after a timestamp reset the numbers
        start small again."""
        picks = []
        for b in self.boards:
            with b.lock:
                picks.append(list(b.picks))
        if not picks or not all(picks):
            return
        others = [{p[0]: p for p in ps} for ps in picks[1:]]        # of a cell kept twice, the newer
        first = next((p for p in reversed(picks[0]) if all(p[0] in d for d in others)), None)
        if first is None:
            return
        key = (first[0], first[2])          # the cell and the first board's timestamp: also new when a reset repeats a cell
        if key == self.set_cell:
            return
        self.set_cell = key
        chosen = [first] + [d[first[0]] for d in others]
        info = {"cell": first[0], "chained": self.chained, "sync": self.cfg.run.sync, "time": time.time(),
                "boards": [{"name": b.name, "trigger_id": p[1], "timestamp": p[2], "size": len(p[3])}
                           for b, p in zip(self.boards, chosen)]}
        path = os.path.join(self.live_dir, "combined.set")
        try:
            with open(f"{path}.part", "wb") as f:
                f.write(rawdata.pack_set(info, [p[3] for p in chosen]))
            os.replace(f"{path}.part", path)
        except OSError:
            pass

    def take_data(self) -> None:
        cfg = self.cfg
        targets = C.software_targets(cfg)
        period = 1.0 / cfg.trigger.software_rate_hz if targets else None
        next_sw = time.monotonic() + (period or 0)
        next_hk = next_set = next_sync = 0.0
        next_remind = time.monotonic() + START_REMIND_S
        hk_every = max(1.0, float(cfg.run.hk_s))
        while not self.stop_reason:
            now = time.monotonic()
            started = self.mono_start is not None       # (a hardware start: from the master's first data)
            if self.stop_requested:
                self.stop_reason = f"Stop ({self.stop_requested})"
            elif started and cfg.run.duration_s and now - self.mono_start >= cfg.run.duration_s:
                self.stop_reason = "run time reached"
            elif cfg.run.max_events and self.boards[0].events >= cfg.run.max_events:
                self.stop_reason = f"{cfg.run.max_events} events on {self.boards[0].name}"
            elif any(b.state == "error" or b.ended == "error" for b in self.boards):
                self.stop_reason = "a board failed"
            elif any(b.stopped_itself for b in self.boards):
                self.stop_reason = "a board stopped by itself"
            elif self.level_ended:
                self.stop_reason = f"the start signal of the master ended ({self.hw_start})"
            elif all(b.done for b in self.boards):
                self.stop_reason = "every board stopped by itself"
            if self.stop_reason:
                break
            if period is not None and now >= next_sw:
                if started:
                    for k in targets:
                        try:
                            self.boards[k].board.cmd("SendSwTrigger")
                        except BoardError as err:
                            self.log(f"{self.boards[k].name}: {err}", "warning")
                next_sw += period
                if now - next_sw > 1.0:             # far behind (a slow moment): do not send a burst
                    next_sw = now + period
            if now >= next_hk:
                next_hk = now + hk_every
                for b in self.boards:
                    self.sample_hk(b, now)
            if now >= next_sync:
                next_sync = now + SYNC_S
                self.check_sync()
            self.poll_control()
            if now >= next_set:
                next_set = now + SET_S
                self.write_set()
            if not started and now >= next_remind:
                next_remind = now + START_REMIND_S
                self.log(f"still waiting for the master's start signal ({self.hw_start}), "
                         f"{now - (self.armed_at or now):.0f} s after the arming")
            for b in self.boards:
                if started and not b.warned_silent and b.last_data is None and now - self.mono_start > SILENT_WARN_S:
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
        with self._state_lock:
            for b in self.boards:
                if b.state == "running":
                    b.state = "stopping"
        self.stop_sent = True                   # a stop record from now on is the answer to this stop
        for k in C.start_order(self.cfg):
            b = self.boards[k]
            try:
                b.board.cmd("SwStopAcquisition")
            except BoardError as err:
                self.log(f"{b.name}: {err}", "warning")
        self.stopping_since = time.monotonic()
        direct = False
        while True:
            alive = [b for b in self.boards if b.thread is not None and b.thread.is_alive()]
            if not alive:
                break
            if not direct and time.monotonic() - self.stopping_since > STOP_WAIT_S:
                # the stop did not reach these (a lost master, a failed stop command), or they are still sending: the
                # stop and the disarm to each of them; its reader ends when it is quiet, at the latest STOP_LIMIT_S
                direct = True
                for b in alive:
                    self.log(f"{b.name}: no stop record {STOP_WAIT_S:g} s after the stop: SwStopAcquisition and "
                             "DisarmAcquisition sent to it directly", "warning")
                    for command in ("SwStopAcquisition", "DisarmAcquisition"):
                        try:
                            b.board.cmd(command)
                        except BoardError as err:
                            self.log(f"{b.name}: {err}", "warning")
            alive[0].thread.join(0.2)
            self.status()
        self.write_set()                        # the last moment all boards have
        for b in self.boards:                   # the counters at the end
            self.sample_hk(b, time.monotonic())
        if self.sync is not None:
            self.check_sync(final=True)
            self.log(self.sync.summary(), "warning" if self.sync.out_of_sync or any(self.sync.missing) else "")
        never = bool(self.hw_start) and self.mono_start is None
        for b in self.boards:
            if b.ended == "time-out" and b.bytes == 0 and never:
                self.log(f"{b.name} sent no data in this run: the master's start signal ({self.hw_start}) never came "
                         "before the stop", "warning")
            elif b.ended == "time-out" and b.bytes == 0:
                self.log(f"{b.name} sent no data in this run: it did not start. Was it armed, and (in a chain) is its "
                         "CLK-IN cable from the board before it in place?", "warning")
            elif b.still_sending:
                self.log(f"{b.name}: still sending data {STOP_LIMIT_S:g} s after the stop: its reader was ended, so the "
                         "end of its data is missing", "warning")
            elif b.ended == "time-out" and self.kill_requested:
                self.log(f"{b.name}: the second Stop ended its reader before its stop record: the end of its data may "
                         "be missing", "warning")
            elif b.ended == "time-out":
                self.log(f"{b.name}: no stop record within {STOP_WAIT_S:g} s after the stop; the end of its data may "
                         "be missing", "warning")

    def close_all(self) -> None:
        """Disarm and close every board and its files, each step on its own: one failure (an error at the last
        write of a file, a board that is gone) must not leave the others armed and open."""
        for b in self.boards:
            reading = b.thread is not None and b.thread.is_alive()
            if b.board is not None:
                try:
                    b.board.cmd("DisarmAcquisition")
                except BoardError:
                    pass                        # it disarmed by itself (EnAutoDisarmAcq), or it is gone
                except Exception as err:
                    self.log(f"{b.name}: cannot disarm it: {err!r}", "warning")
                if reading:                     # never close a board during its reader's ReadData: the end of the
                    self.log(f"{b.name}: its reader still runs: the connection closes when the program ends", "warning")
                else:                           # program closes it
                    try:
                        b.board.close()
                    except Exception as err:
                        self.log(f"{b.name}: cannot close the connection: {err!r}", "warning")
            if b.writer is not None:
                try:
                    b.writer.close()
                except Exception as err:        # the last data of the buffer did not reach the disk
                    self.board_error(b, f"cannot close its data file: {err}; the end of its data may be missing")
            if b.hk_out:
                try:
                    b.hk_out.close()
                except Exception as err:
                    self.log(f"{b.name}: cannot close its housekeeping file: {err}", "warning")

    def summary(self) -> None:
        for b in self.boards:
            files = ", ".join(os.path.basename(p) for p in (b.writer.files if b.writer else [])) or "none"
            ids = "" if b.first_trigger is None else f", trigger IDs {b.first_trigger}-{b.last_trigger}"
            if "lost" in b.stats:
                ids += f", {b.stats['lost']} triggers lost (the board's count)"
            self.log(f"{b.name}: {b.events} events, {b.bytes / 1e6:.2f} MB{ids}, ended: {b.ended or 'not started'}; "
                     f"files: {files}")
        ok = not self.errors and self.boards and all(b.ended == "stop record" for b in self.boards)
        self.log(f"run {self.run_name} {'ended' if ok else 'FAILED'}" + (f" ({self.stop_reason})" if self.stop_reason else ""))
