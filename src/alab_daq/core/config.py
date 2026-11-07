"""The configuration of an ALAB DAQ run, kept as a JSON file: the boards in the order of their chain (the first
is the master), how they are synchronized and triggered, each board's window and channels, and the run.

plan(cfg, i) turns it into the exact CAEN FELib parameters for board i, in the order they are set. The
synchronization is CAEN's recipe for a CLK-OUT -> CLK-IN daisy chain of Digitizer 2.0 boards
(caen-dig2 v1.9, demo/caen-felib-demo-sync/main.c, configure_sync):

    board          ClockSource  SyncOutMode  StartSource   EnClockOutFP  RunDelay (8 ns ticks)   clock-out delay
    first (master) Internal     Run          SWcmd         True          2 (n - 1) + 4           -2148 ps
    middle         FPClkIn      SyncIn       EncodedClkIn  True          2 (n - 1 - i)            -3111 ps
    last           FPClkIn      Disabled     EncodedClkIn  False         0                         0
    all: EnAutoDisarmAcq = True; every board is armed, then only the master gets SwStartAcquisition (and
    SwStopAcquisition): the run signal travels on the clock cable, RunDelay lines the starts up.

The trigger setups: the lab's usual one (master), the three multi-board examples of the WaveDump2 manual
(UM7934, Appendix A), and a periodic trigger for pedestal and test runs:
    master     the first board (e.g. the VX2730) triggers on its own channels and sends its trigger on TRG-OUT;
               every other board takes it on TRG-IN and passes it on TRG-OUT -> TRG-IN (master_out: ITLA, or
               AcceptTrg = only the triggers the master took)
    external   a trigger into the first board's TRG-IN, passed on TRG-OUT -> TRG-IN; every board takes it
    self       every board triggers on its own channels (the ITLA logic: OR, AND or majority of them)
    logic_or   every board sends its channel triggers out on TRG-OUT into a logic unit (e.g. a V976 OR), whose
               output comes back into every TRG-IN; every board takes the common trigger
    periodic   software triggers from the DAQ (each board, or the master passing them on TRG-OUT -> TRG-IN), or
               each board's internal test pulse
Thresholds count from the channel's baseline (TriggerThrMode = Relative), in the pulse's direction: a
negative pulse gets TriggerThr = -N and SelfTriggerEdge = Fall. ALAB DAQ sends that convention; it is to be
confirmed on the real boards (WaveDump2 shows relative thresholds below the baseline as negative numbers).
"""

from __future__ import annotations

import copy
import json
import os
import re
from dataclasses import asdict, dataclass, field, fields

from alab_daq.core import models

FORMAT = "alab-daq-config"
VERSION = 1
SETUPS = ("master", "external", "self", "logic_or", "periodic")
MASTER_OUTS = ("ITLA", "AcceptTrg")
SYNC_MODES = ("chain", "independent")
LOGICS = ("OR", "AND", "Majority")
IO_LEVELS = ("NIM", "TTL")
PERIODIC_SOURCES = ("software", "test_pulse")
SOFTWARE_TARGETS = ("each", "chain")
URI_SCHEMES = ("dig2://",)
MAX_BOARDS = 32
MAX_RECORD = 6_250_000          # WaveDump2's limit on the record length (samples)
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,31}$")
EXTRA_RE = re.compile(r"^/(par|ch/[0-9]+(\.\.[0-9]+)?/par|endpoint/par)/[A-Za-z0-9_]+$")


class ConfigError(ValueError):
    """Not a configuration of ALAB DAQ (the message says why)."""


@dataclass
class Channel:
    enabled: bool = True
    dc_offset: float = 50.0     # where the baseline sits, in % of the input range (0-100)
    threshold: int = 100        # ADC counts from the baseline, in the direction of the pulse
    positive: bool = False      # positive pulses (rising edge, above the baseline); else negative
    self_trigger: bool = True   # takes part in the board's channel trigger (ITLA)
    gain: float = 0.0           # dB, 2745 only


@dataclass
class Board:
    name: str = "b0"
    uri: str = "dig2://192.168.0.254"
    model: str = "V2740"        # what the board is meant to be (the simulator makes one; a real one is checked)
    n_channels: int = 0         # its inputs; 0: the model's usual number (a VX2730 may have 16 instead of 32)
    enabled: bool = True
    record_length: int = 1024   # samples
    pre_trigger: int = 128      # samples before the trigger
    decimation: int = 1         # 2740 and 2745: 1, 2, 4 ... 1024
    run_delay_ns: int | None = None             # None: CAEN's value for the board's place in the chain
    clock_out_delay_ps: float | None = None     # None: CAEN's value
    channels: list = field(default_factory=list)    # Channel, one per channel of the model
    extra: list = field(default_factory=list)       # [path, value]: set last, as given (expert)


@dataclass
class Trigger:
    setup: str = "periodic"
    logic: str = "OR"           # how a board's self-triggering channels make its trigger (ITLA)
    majority: int = 2           # channels needed for "Majority"
    gate_ns: int = 16           # ITLAGateWidth: how long a channel's trigger stays open for AND and Majority
    periodic_source: str = "software"
    software_rate_hz: float = 10.0
    software_target: str = "each"   # each board gets the software trigger, or the master passes it on
    test_pulse_period_ns: int = 10_000_000
    test_pulse_width_ns: int = 128
    io_level: str = "NIM"
    busy_veto: bool = False     # GPIO of all boards wired together: a busy board vetoes all (NIM only)
    master_out: str = "ITLA"    # setup "master": what the master sends on TRG-OUT


@dataclass
class RunSettings:
    duration_s: int = 60        # 0: until Stop
    max_events: int = 0         # 0: no limit; else stop when the first board has this many events
    sync: str = "chain"
    data_dir: str = "~/ALAB_DAQ_data"
    write_raw: bool = True
    file_mb: int = 2048         # a new file when one reaches this size


@dataclass
class DaqConfig:
    run: RunSettings = field(default_factory=RunSettings)
    trigger: Trigger = field(default_factory=Trigger)
    boards: list = field(default_factory=list)

    def copy(self) -> "DaqConfig":
        return copy.deepcopy(self)

    def active(self) -> list[tuple[int, Board]]:
        """(index in boards, board) of the boards the run uses, in chain order."""
        return [(i, b) for i, b in enumerate(self.boards) if b.enabled]

    def board(self, name: str) -> Board | None:
        return next((b for b in self.boards if b.name == name), None)


@dataclass
class Problem:
    level: str      # "error" (the run would fail or be wrong) or "warning"
    where: str
    message: str


@dataclass
class Setting:
    path: str
    value: str
    why: str = ""
    alt: list = field(default_factory=list)     # tried instead when the board does not know this parameter;
                                                # once a name is refused, later settings of it are skipped


# ---- defaults ----
def channels_for(model_name: str, old: list | None = None, n: int = 0) -> list[Channel]:
    """n channels (0: the model's usual number), the old ones kept."""
    m = models.model(model_name) or models.MODELS["2740"]
    old = old or []
    base = old[-1] if old else Channel()
    return [copy.deepcopy(old[c]) if c < len(old) else copy.deepcopy(base) for c in range(n or m.channels)]


def default_config() -> DaqConfig:
    """Two boards in a chain, for a first look: change the addresses and models on the Configuration page."""
    boards = [Board("b0", "dig2://192.168.0.254", "V2740"), Board("b1", "dig2://192.168.0.253", "V2730")]
    for b in boards:
        b.channels = channels_for(b.model)
        if models.family(b.model) == "2730":
            b.record_length, b.pre_trigger = 4096, 512          # the same 8.192 µs window at 2 ns
    return DaqConfig(boards=boards)


def lab_setup() -> DaqConfig:
    """The lab's setup (2026-11-07): the VX2730 (16 inputs, 500 MS/s) at 192.168.0.72 is the master: it
    self-triggers and its CLK and TRG go along the chain to the VX2740s (64 inputs, 125 MS/s) at .75 and .85.
    Every board records the same 4.096 µs, 1.024 µs before the trigger."""
    boards = [Board("vx2730", "dig2://192.168.0.72", "VX2730", n_channels=16, record_length=2048, pre_trigger=512),
              Board("vx2740a", "dig2://192.168.0.75", "VX2740", record_length=512, pre_trigger=128),
              Board("vx2740b", "dig2://192.168.0.85", "VX2740", record_length=512, pre_trigger=128)]
    for b in boards:
        b.channels = channels_for(b.model, n=b.n_channels)
    cfg = DaqConfig(boards=boards)
    cfg.trigger.setup = "master"
    return cfg


# ---- from and to JSON ----
def _bool(v, where):
    if isinstance(v, bool):
        return v
    if v in (0, 1):
        return bool(v)
    raise ConfigError(f"{where} must be true or false")


def _num(v, where, kind=int, allow_none=False):
    if v is None and allow_none:
        return None
    if isinstance(v, bool):
        raise ConfigError(f"{where} must be a number")
    try:
        x = kind(v)
    except (TypeError, ValueError):
        raise ConfigError(f"{where} must be a number") from None
    if kind is int and isinstance(v, float) and v != int(v):
        raise ConfigError(f"{where} must be a whole number")
    if kind is float and x != x:
        raise ConfigError(f"{where} must be a number")
    return x


def _fill(cls, data, where):
    """An instance of the dataclass cls from a dict: known keys only, each of the default's type."""
    if not isinstance(data, dict):
        raise ConfigError(f"{where} must be an object")
    obj = cls()
    for f in fields(cls):
        if f.name not in data or f.name in ("channels", "extra", "boards"):
            continue
        v, default, w = data[f.name], getattr(obj, f.name), f"{where}.{f.name}"
        if isinstance(default, bool):
            setattr(obj, f.name, _bool(v, w))
        elif isinstance(default, int) or f.name == "run_delay_ns":
            setattr(obj, f.name, _num(v, w, int, allow_none=f.name == "run_delay_ns"))
        elif isinstance(default, float) or f.name == "clock_out_delay_ps":
            setattr(obj, f.name, _num(v, w, float, allow_none=f.name == "clock_out_delay_ps"))
        else:
            if not isinstance(v, str):
                raise ConfigError(f"{w} must be text")
            setattr(obj, f.name, v.strip())
    return obj


def from_dict(data: dict) -> DaqConfig:
    if not isinstance(data, dict):
        raise ConfigError("a configuration is a JSON object")
    cfg = DaqConfig(run=_fill(RunSettings, data.get("run", {}), "run"),
                    trigger=_fill(Trigger, data.get("trigger", {}), "trigger"))
    boards = data.get("boards", [])
    if not isinstance(boards, list):
        raise ConfigError("boards must be a list")
    for k, b in enumerate(boards[:MAX_BOARDS]):
        board = _fill(Board, b, f"boards[{k}]")
        chans = b.get("channels", [])
        if not isinstance(chans, list):
            raise ConfigError(f"boards[{k}].channels must be a list")
        if not 0 <= board.n_channels <= 64:
            raise ConfigError(f"boards[{k}].n_channels must be from 0 to 64")
        board.channels = channels_for(board.model, [_fill(Channel, c, f"boards[{k}].channels[{j}]")
                                                    for j, c in enumerate(chans)], board.n_channels)
        extra = b.get("extra", [])
        if not isinstance(extra, list) or not all(isinstance(x, (list, tuple)) and len(x) == 2 for x in extra):
            raise ConfigError(f"boards[{k}].extra must be a list of [path, value]")
        board.extra = [[str(p).strip(), str(v).strip()] for p, v in extra]
        cfg.boards.append(board)
    return cfg


def to_dict(cfg: DaqConfig) -> dict:
    return {"run": asdict(cfg.run), "trigger": asdict(cfg.trigger), "boards": [asdict(b) for b in cfg.boards]}


def load_config(path: str) -> tuple[DaqConfig, str]:
    """(configuration, note) from a JSON file."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except ValueError as err:
        raise ConfigError(f"{os.path.basename(path)} is not JSON: {err}") from None
    if isinstance(data, dict) and data.get("format") not in (None, FORMAT):
        raise ConfigError(f"{os.path.basename(path)} is not an ALAB DAQ configuration")
    cfg = from_dict(data.get("config", data) if isinstance(data, dict) else data)
    note = data.get("note", "") if isinstance(data, dict) else ""
    return cfg, note if isinstance(note, str) else ""


def config_document(cfg: DaqConfig, note: str = "", **facts) -> dict:
    """What a configuration file holds: the configuration, its note, and facts (who, when, which run...)."""
    doc = {"format": FORMAT, "version": VERSION, "note": clean_note(note)}
    doc.update({k: v for k, v in facts.items() if v is not None})
    doc["config"] = to_dict(cfg)
    return doc


def save_config(cfg: DaqConfig, path: str, note: str = "", **facts) -> None:
    text = json.dumps(config_document(cfg, note, **facts), indent=1, ensure_ascii=False) + "\n"
    tmp = f"{path}.part"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def clean_note(note: str) -> str:
    note = (note or "").replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(line.rstrip() for line in note.split("\n")).strip()[:4000]


# ---- the chain ----
def role(i: int, n: int) -> str:
    """The place of the i-th board (of n in the run) in the chain."""
    if n <= 1:
        return "single"
    return "master" if i == 0 else "last" if i == n - 1 else "middle"


def caen_run_delay_ns(i: int, n: int) -> int:
    if n <= 1:
        return 0
    return 8 * (2 * (n - 1 - i) + (4 if i == 0 else 0))


def caen_clock_out_delay_ps(i: int, n: int) -> float:
    if n <= 1 or i == n - 1:
        return 0.0
    return -2148.0 if i == 0 else -3111.0


def sample_ns(board: Board, facts: models.Model | None = None) -> float:
    m = facts or models.model(board.model) or models.MODELS["2740"]
    return m.sample_ns * (board.decimation if m.decimation else 1)


def event_bytes(board: Board) -> int:
    """Bytes of one event of the board: 3 header words and 2 bytes per sample of each enabled channel."""
    n = sum(1 for c in board.channels if c.enabled)
    return 24 + 2 * n * board.record_length


def uses_self_trigger(cfg: DaqConfig) -> bool:
    """Some board triggers on its own channels."""
    return cfg.trigger.setup in ("self", "logic_or", "master")


def triggers_on_channels(cfg: DaqConfig, k: int | None) -> bool:
    """The k-th board of the run triggers on its own channels (thresholds, ITLA): all in self and logic_or, only
    the master in setup master."""
    if k is None:
        return False
    return cfg.trigger.setup in ("self", "logic_or") or (cfg.trigger.setup == "master" and k == 0)


# ---- the parameters of one board ----
def _ranges(values: list) -> list[tuple[int, int, object]]:
    """Consecutive channels with the same value: [(first, last, value)]."""
    out = []
    for c, v in enumerate(values):
        if out and out[-1][2] == v and out[-1][1] == c - 1:
            out[-1] = (out[-1][0], c, v)
        else:
            out.append((c, c, v))
    return out


def _ch(a: int, b: int, name: str) -> str:
    return f"/ch/{a}/par/{name}" if a == b else f"/ch/{a}..{b}/par/{name}"


def _fmt(x) -> str:
    if isinstance(x, bool):
        return "True" if x else "False"
    if isinstance(x, float):
        return f"{x:.6g}" if x != int(x) else str(int(x))
    return str(x)


def plan(cfg: DaqConfig, k: int, facts: models.Model | None = None) -> list[Setting]:
    """The FELib parameters of the k-th board of the run (the k-th enabled board), in the order they are set."""
    active = [b for _, b in cfg.active()]
    b, n = active[k], len(active)
    m = facts or models.model(b.model) or models.MODELS["2740"]
    chained = cfg.run.sync == "chain" and n > 1
    place = role(k, n) if chained else "single"
    trg, out = cfg.trigger, []

    def put(path, value, why="", alt=None):
        out.append(Setting(path, _fmt(value), why, alt or []))

    # the window
    put("/par/RecordLengthS", b.record_length, "record length (samples)")
    put("/par/PreTriggerS", b.pre_trigger, "samples before the trigger")
    if m.decimation:
        put("/par/DecimationFactor", b.decimation, "decimation")
    put("/par/IOlevel", trg.io_level, "front-panel signal level")
    put("/par/EnAutoDisarmAcq", True, "disarm by itself when the run stops")
    # synchronization: CAEN's daisy-chain recipe
    if place == "single":
        put("/par/ClockSource", "Internal", "own clock")
        put("/par/StartSource", "SWcmd", "started by the DAQ")
        put("/par/SyncOutMode", "Disabled", "no chain")
        put("/par/EnClockOutFP", False, "no chain")
        put("/par/RunDelay", 0, "no chain")
    else:
        rd = b.run_delay_ns if b.run_delay_ns is not None else caen_run_delay_ns(k, n)
        cd = b.clock_out_delay_ps if b.clock_out_delay_ps is not None else caen_clock_out_delay_ps(k, n)
        put("/par/ClockSource", "Internal" if place == "master" else "FPClkIn", f"chain {place}")
        put("/par/SyncOutMode", {"master": "Run", "middle": "SyncIn", "last": "Disabled"}[place],
            "passes the run signal on" if place != "last" else "end of the chain")
        put("/par/StartSource", "SWcmd" if place == "master" else "EncodedClkIn",
            "started by the DAQ" if place == "master" else "starts with the run signal on CLK-IN")
        put("/par/EnClockOutFP", place != "last", "clock out to the next board" if place != "last" else "end of the chain")
        put("/par/RunDelay", rd, "lines up the start with the other boards (ns)")
        if place != "last" or b.clock_out_delay_ps is not None:
            put("/par/VolatileClockOutDelay", cd, "clock-out delay (ps)")
    # triggers
    last = k == n - 1
    pass_on = n > 1                         # TRG-OUT -> TRG-IN of the next board, with or without the clock chain
    own = triggers_on_channels(cfg, k)
    if trg.setup == "master":
        if k == 0:
            put("/par/AcqTriggerSource", "ITLA", "own channels (ITLA)")
            put("/par/TrgOutMode", trg.master_out if pass_on else "Disabled",
                "TRG-OUT sends the master's trigger to the next board" if pass_on else "end of the trigger chain")
        else:
            put("/par/AcqTriggerSource", "TrgIn", "the master's trigger on TRG-IN")
            put("/par/TrgOutMode", "Disabled" if last else "TrgIn",
                "end of the trigger chain" if last else "TRG-OUT passes TRG-IN to the next board")
    elif trg.setup == "external":
        put("/par/AcqTriggerSource", "TrgIn", "the external trigger on TRG-IN")
        put("/par/TrgOutMode", "Disabled" if last or not pass_on else "TrgIn",
            "end of the trigger chain" if last or not pass_on else "TRG-OUT passes TRG-IN to the next board")
    elif trg.setup == "self":
        put("/par/AcqTriggerSource", "ITLA", "own channels (ITLA)")
        put("/par/TrgOutMode", "Disabled", "")
    elif trg.setup == "logic_or":
        put("/par/AcqTriggerSource", "TrgIn", "the common trigger from the logic unit on TRG-IN")
        put("/par/TrgOutMode", "ITLA", "TRG-OUT sends the channel triggers to the logic unit")
    else:                                   # periodic
        if trg.periodic_source == "test_pulse":
            put("/par/AcqTriggerSource", "TestPulse", "internal test pulse")
            put("/par/TestPulsePeriod", trg.test_pulse_period_ns, "test pulse period (ns)")
            put("/par/TestPulseWidth", trg.test_pulse_width_ns, "test pulse width (ns)")
            put("/par/TrgOutMode", "Disabled", "")
        elif trg.software_target == "chain" and n > 1:
            put("/par/AcqTriggerSource", "SwTrg" if k == 0 else "TrgIn",
                "software trigger from the DAQ" if k == 0 else "the master's software trigger on TRG-IN")
            put("/par/TrgOutMode", "Disabled" if last else ("SwTrg" if k == 0 else "TrgIn"),
                "end of the trigger chain" if last else "TRG-OUT passes the trigger on")
        else:
            put("/par/AcqTriggerSource", "SwTrg", "software trigger from the DAQ")
            put("/par/TrgOutMode", "Disabled", "")
    if own:
        put("/par/ITLAMainLogic", trg.logic, "how the channel triggers combine")
        if trg.logic == "Majority":
            put("/par/ITLAMajorityLev", trg.majority, "channels needed")
        put("/par/ITLAPairLogic", "None", "")
        put("/par/ITLAPolarity", "Direct", "")
        put("/par/ITLAGateWidth", trg.gate_ns, "channel trigger gate (ns)")
    if trg.busy_veto:
        put("/par/GPIOMode", "Busy", "GPIO out: this board is busy")
        put("/par/VetoSource", "GPIO", "GPIO in: any busy board vetoes",
            alt=[Setting("/par/BoardVetoSource", "GPIO", "GPIO in: any busy board vetoes (other name)")])
    # channels, grouped where neighbours are the same
    ch = b.channels
    for a, z, v in _ranges([c.enabled for c in ch]):
        put(_ch(a, z, "ChEnable"), v, "read out" if v else "not read out")
    for a, z, v in _ranges([round(c.dc_offset, 3) for c in ch]):
        put(_ch(a, z, "DCOffset"), v, "baseline position (%)")
    if m.gain:
        for a, z, v in _ranges([c.gain for c in ch]):
            put(_ch(a, z, "ChGain"), v, "gain (dB)")
    if own:
        put(_ch(0, len(ch) - 1, "TriggerThrMode"), "Relative", "thresholds from the baseline")
        for a, z, v in _ranges([(-1 if not c.positive else 1) * c.threshold for c in ch]):
            put(_ch(a, z, "TriggerThr"), v, "threshold (counts from the baseline)")
        for a, z, v in _ranges(["Rise" if c.positive else "Fall" for c in ch]):
            put(_ch(a, z, "SelfTriggerEdge"), v, "positive pulses" if v == "Rise" else "negative pulses")
        mask = sum(1 << c for c, x in enumerate(ch) if x.self_trigger and x.enabled)
        connect = [("ITLA" if x.self_trigger and x.enabled else "Disabled") for x in ch]
        groups = _ranges(connect)
        alt = [Setting("/par/ITLAMask", f"0x{mask:x}", "channels in the board trigger (older firmware)")]
        for j, (a, z, v) in enumerate(groups):
            put(_ch(a, z, "ITLConnect"), v, "in the board trigger" if v == "ITLA" else "not in the board trigger",
                alt=alt if j == 0 else [])
    for path, value in b.extra:
        put(path, value, "expert setting of the configuration")
    return out


def start_order(cfg: DaqConfig) -> list[int]:
    """The boards that get SwStartAcquisition (and SwStopAcquisition), as numbers in the run: the master of a
    chain, else every board."""
    n = len(cfg.active())
    return [0] if cfg.run.sync == "chain" and n > 1 else list(range(n))


def software_targets(cfg: DaqConfig) -> list[int]:
    """The boards the DAQ sends software triggers to."""
    if cfg.trigger.setup != "periodic" or cfg.trigger.periodic_source != "software":
        return []
    n = len(cfg.active())
    return [0] if cfg.trigger.software_target == "chain" and n > 1 else list(range(n))


# ---- the check ----
def check_config(cfg: DaqConfig, check_paths: bool = True, facts: dict | None = None) -> list[Problem]:
    """What would make the run fail (errors) or look like a mistake (warnings). facts: name -> what each board
    said when it was last checked (model, channels...)."""
    out: list[Problem] = []
    err = lambda where, msg: out.append(Problem("error", where, msg))          # noqa: E731
    warn = lambda where, msg: out.append(Problem("warning", where, msg))       # noqa: E731
    r, trg = cfg.run, cfg.trigger
    active = cfg.active()
    if not cfg.boards:
        err("boards", "there is no board")
    elif not active:
        err("boards", "every board is switched off")
    if r.sync not in SYNC_MODES:
        err("run.sync", f"must be one of {', '.join(SYNC_MODES)}")
    if r.duration_s < 0:
        err("run.duration_s", "must be 0 (until Stop) or more")
    if r.max_events < 0:
        err("run.max_events", "must be 0 (no limit) or more")
    if r.duration_s == 0 and r.max_events == 0:
        warn("run.duration_s", "the run goes on until someone presses Stop")
    if not 16 <= r.file_mb <= 1 << 20:
        err("run.file_mb", "must be from 16 to 1048576 MB")
    if r.write_raw and check_paths:
        d = os.path.expanduser(r.data_dir)
        if not r.data_dir:
            err("run.data_dir", "no data folder given")
        elif not os.path.isdir(d):
            err("run.data_dir", f"{d} does not exist (make it first)")
        elif not os.access(d, os.W_OK | os.X_OK):
            err("run.data_dir", f"the server cannot write in {d}")
    if not r.write_raw:
        warn("run.write_raw", "the data are not kept: only the live view")
    if trg.setup not in SETUPS:
        err("trigger.setup", f"must be one of {', '.join(SETUPS)}")
    if trg.master_out not in MASTER_OUTS:
        err("trigger.master_out", f"must be one of {', '.join(MASTER_OUTS)}")
    if trg.logic not in LOGICS:
        err("trigger.logic", f"must be one of {', '.join(LOGICS)}")
    if trg.io_level not in IO_LEVELS:
        err("trigger.io_level", "must be NIM or TTL")
    if trg.busy_veto and trg.io_level != "NIM":
        err("trigger.busy_veto", "the wired-OR of the GPIO lines works with NIM signals only")
    if trg.setup == "periodic":
        if trg.periodic_source not in PERIODIC_SOURCES:
            err("trigger.periodic_source", "must be software or test_pulse")
        elif trg.periodic_source == "software":
            if not 0 < trg.software_rate_hz <= 1000:
                err("trigger.software_rate_hz", "must be more than 0 and at most 1000 Hz")
            if trg.software_target not in SOFTWARE_TARGETS:
                err("trigger.software_target", "must be each or chain")
            elif trg.software_target == "each" and len(active) > 1:
                warn("trigger.software_target", "each board gets its own software trigger: the boards record "
                     "different moments (fine for pedestals; for the same moment choose 'chain' and the TRG cables)")
        elif not 16 <= trg.test_pulse_period_ns <= 10 ** 10:
            err("trigger.test_pulse_period_ns", "must be from 16 ns to 10 s")
        elif not 8 <= trg.test_pulse_width_ns < trg.test_pulse_period_ns:
            err("trigger.test_pulse_width_ns", "must be at least 8 ns and shorter than the period")
    if uses_self_trigger(cfg) and trg.logic == "Majority" and trg.majority < 1:
        err("trigger.majority", "must be 1 or more")
    if not 8 <= trg.gate_ns <= 10 ** 6:
        err("trigger.gate_ns", "must be from 8 ns to 1 ms")
    if len(active) > 1 and r.sync == "independent":
        warn("run.sync", "independent boards run on their own clocks: their timestamps do not line up")
    if len(active) == 1 and r.sync == "chain":
        warn("run.sync", "one board: no chain is needed (it runs on its own)")
    names, uris = set(), set()
    windows = []
    place = {id(b): k for k, (_, b) in enumerate(active)}
    for i, b in enumerate(cfg.boards):
        w = f"board {b.name or i + 1}"
        if not NAME_RE.match(b.name or ""):
            err(f"boards[{i}].name", "a name of letters, digits, _ . - (at most 32), starting with a letter or digit")
        elif b.name in names:
            err(f"boards[{i}].name", f"{b.name} is used twice")
        names.add(b.name)
        if not b.uri.startswith(URI_SCHEMES) or len(b.uri) <= len("dig2://"):
            err(f"boards[{i}].uri", "the address is dig2://IP-ADDRESS or dig2://usb:PID")
        elif b.uri in uris:
            err(f"boards[{i}].uri", f"{b.uri} is used twice")
        uris.add(b.uri)
        m = models.model(b.model)
        if m is None:
            err(f"boards[{i}].model", f"{b.model} is not a known model ({', '.join(models.CHOICES)})")
            continue
        fact = (facts or {}).get(b.name)
        if fact and fact.get("model") and models.family(fact["model"]) != m.family:
            err(f"boards[{i}].model", f"the board at {b.uri} said it is a {fact['model']}, not a {b.model}")
        if fact and fact.get("channels") and fact["channels"] != len(b.channels):
            err(f"boards[{i}].n_channels", f"the board at {b.uri} said it has {fact['channels']} channels, not "
                                           f"{len(b.channels)} (Use what the boards said)")
        own = triggers_on_channels(cfg, place.get(id(b)))
        if not b.enabled:
            continue
        if not 4 <= b.record_length <= MAX_RECORD:
            err(f"boards[{i}].record_length", f"must be from 4 to {MAX_RECORD:,} samples")
        elif b.record_length % 4:
            warn(f"boards[{i}].record_length", "not a multiple of 4: the board rounds it")
        if not 0 <= b.pre_trigger < b.record_length:
            err(f"boards[{i}].pre_trigger", "must be at least 0 and less than the record length")
        if m.decimation:
            if b.decimation not in models.DECIMATIONS:
                err(f"boards[{i}].decimation", "must be 1, 2, 4, ... 1024")
        elif b.decimation != 1:
            err(f"boards[{i}].decimation", f"a {b.model} has no decimation: use 1")
        if not 0 <= b.n_channels <= 64:
            err(f"boards[{i}].n_channels", "must be from 1 to 64 (0: the model's usual number)")
        if len(b.channels) != (b.n_channels or m.channels):
            err(f"boards[{i}].channels", f"{w} has {b.n_channels or m.channels} channels, not {len(b.channels)}")
        en = [c for c in b.channels if c.enabled]
        if not en:
            err(f"boards[{i}].channels", f"{w}: no channel is read out")
        for c, ch in enumerate(b.channels):
            cw = f"boards[{i}].channels[{c}]"
            if not 0 <= ch.dc_offset <= 100:
                err(f"{cw}.dc_offset", "must be from 0 to 100 %")
            if ch.threshold < 0:
                err(f"{cw}.threshold", "counts from the baseline: use 0 or more; the pulse direction is set apart")
            elif ch.threshold >= m.adc_max // 2 and own and ch.self_trigger and ch.enabled:
                warn(f"{cw}.threshold", f"{ch.threshold} counts from the baseline is more than half the ADC range "
                                        f"(0-{m.adc_max}): is it meant as an ADC level?")
            if ch.gain and not m.gain:
                err(f"{cw}.gain", f"a {b.model} has no variable gain: use 0")
        if own:
            trig = [c for c in b.channels if c.enabled and c.self_trigger]
            if not trig:
                err(f"boards[{i}].channels", f"{w}: no channel takes part in the board's trigger")
            elif trg.logic == "Majority" and trg.majority > len(trig):
                err("trigger.majority", f"{w} has only {len(trig)} triggering channels")
        if b.run_delay_ns is not None and not 0 <= b.run_delay_ns <= 100_000:
            err(f"boards[{i}].run_delay_ns", "must be from 0 to 100000 ns")
        for j, (p, v) in enumerate(b.extra):
            if not EXTRA_RE.match(p):
                err(f"boards[{i}].extra[{j}]", f"'{p}' is not a parameter path such as /par/NAME or /ch/0..63/par/NAME")
            if not v:
                err(f"boards[{i}].extra[{j}]", "no value")
        windows.append((b.name, b.record_length * sample_ns(b, m), b.pre_trigger * sample_ns(b, m)))
    common = trg.setup in ("master", "external", "logic_or") or (trg.setup == "periodic" and trg.software_target == "chain")
    if len(windows) > 1 and common:
        lengths = {round(x[1]) for x in windows}
        pres = {round(x[2]) for x in windows}
        if len(lengths) > 1 or len(pres) > 1:
            warn("boards", "the boards take the same trigger but record different time windows: "
                 + ", ".join(f"{n} {ln / 1000:g} µs ({p / 1000:g} before)" for n, ln, p in windows))
    return out
