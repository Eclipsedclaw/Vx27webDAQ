"""A simulated crate of Digitizer 2.0 boards with the Scope firmware, to run Vx27webDAQ without hardware (the
simulator of the Settings page, and the tests).

Each simulated board knows the parameters Vx27webDAQ sets, with their allowed values, and refuses anything
else, as a real board would; it sends its data in the raw format of the real boards (core/rawdata.py), with
the run start and stop records. The boards of a crate share what the cables of a real setup share:
  - the clock and the run signal of a CLK-OUT -> CLK-IN chain: a board whose StartSource is EncodedClkIn starts
    when the run signal reaches it (from a master with SyncOutMode = Run, through boards with SyncIn), if it is
    armed then; the run stops the same way;
  - the trigger cables: "chain" (a pulser into the first TRG-IN, each TRG-OUT into the next TRG-IN) or
    "or_unit" (every TRG-OUT into a logic unit whose OR goes into every TRG-IN).
Physics: hits come at random (rate per second); each hit puts a pulse on some channels of every board (each
channel with probability `share`), so the boards see the same moments. The pulser of the chain fires on every
hit; a board's channel trigger (ITLA) fires on a hit when its logic (OR, AND, majority of its connected
channels whose pulse passes their threshold) is met.

Options (a string, as on the Settings page): --rate HZ (20), --share P (0.5), --amplitude COUNTS (600),
--noise SIGMA (3), --seed N, and failures for the tests: --fail open:NAME, param:NAME:PARAM, read:NAME:N,
nostop:NAME, unarmed:NAME (the board does not take the arm command), errflags:NAME:S (ErrorFlags 0x800 from S
seconds into the run on).
--generator staircase turns the random hits into a pulse generator, to see clear waveforms: hits come
regularly (--rate per second), and in every record each channel of every board has one pulse of the full
--amplitude. A V2730: every channel at the same time, at the trigger, a fast negative pulse. A V2740/V2745: a
positive pulse with a --rise-us (5 us, 10-90 %) rise and a --decay-us (1000 us) decay, channel k's coming
k x --spacing-us (5 us) after the trigger (a channel whose pulse comes after the end of the record shows none).
Housekeeping: the temperatures rise slowly while the board runs; TriggerCnt counts its triggers, the dead time is
1 µs per event, a channel's SelfTrgRate is the rate of hits on it.
"""

from __future__ import annotations

import argparse
import dataclasses
import functools
import math
import shlex
import threading
import time
import zlib

import numpy as np

from vx27webdaq.core import models, rawdata
from vx27webdaq.daq.boards import BoardError

TICK_S = rawdata.TIMESTAMP_NS * 1e-9
MAX_RAW = 16 << 20

TRG_SOURCES = ("TrgIn", "P0", "SwTrg", "LVDS", "ITLA", "ITLB", "ITLA_AND_ITLB", "ITLA_OR_ITLB", "EncodedClkIn",
               "GPIO", "TestPulse", "UserTrg")
TRGOUT_MODES = ("TrgIn", "P0", "SwTrg", "LVDS", "ITLA", "ITLB", "ITLA_AND_ITLB", "ITLA_OR_ITLB", "EncodedClkIn",
                "Run", "RefClk", "TestPulse", "Busy", "UserTrgout", "Fixed0", "Fixed1", "SyncIn", "SIN", "GPIO",
                "LBinClk", "AcceptTrg", "TrgClk", "Disabled")
GPIO_MODES = ("Disabled", "TrgIn", "P0", "SIN", "LVDS", "ITLA", "ITLB", "ITLA_AND_ITLB", "ITLA_OR_ITLB",
              "EncodedClkIn", "SwTrg", "Run", "RefClk", "TestPulse", "Busy", "UserGPIO", "Fixed0", "Fixed1")
START_SOURCES = ("EncodedClkIn", "SINlevel", "SINedge", "SWcmd", "LVDS", "FirstTrigger", "P0")
VETO_SOURCES = ("Disabled", "SIN", "LVDS", "GPIO", "P0", "EncodedClkIn")
COMMANDS = ("reset", "armacquisition", "disarmacquisition", "swstartacquisition", "swstopacquisition",
            "sendswtrigger", "cleardata", "reloadcalibration")


# ---- parameter types: each takes the text given and returns the value as the board keeps it ----
def _bool(text):
    t = str(text).strip().lower()
    if t in ("true", "1"):
        return "True"
    if t in ("false", "0"):
        return "False"
    raise ValueError("must be True or False")


_bool.meta = {"datatype": "BOOL", "allowedvalues": ["True", "False"]}


def _int(lo, hi):
    def check(text):
        x = float(text)
        if x != int(x) or not lo <= x <= hi:
            raise ValueError(f"must be a whole number from {lo} to {hi}")
        return str(int(x))
    check.meta = {"datatype": "NUMBER", "minvalue": lo, "maxvalue": hi, "increment": 1}
    return check


def _float(lo, hi, step=None):
    def check(text):
        x = float(text)
        if not lo <= x <= hi:
            raise ValueError(f"must be from {lo} to {hi}")
        if step:
            x = round(x / step) * step
        return f"{x:.3f}"
    check.meta = {"datatype": "NUMBER", "minvalue": lo, "maxvalue": hi, "increment": step or 0.001}
    return check


def _choice(options):
    low = {o.lower(): o for o in options}

    def check(text):
        t = str(text).strip().lower()
        if t not in low:
            raise ValueError(f"must be one of {', '.join(options)}")
        return low[t]
    check.meta = {"datatype": "STRING", "allowedvalues": list(options)}
    return check


def _combo(options):
    one = _choice(options)

    def check(text):
        parts = [p.strip() for p in str(text).split("|") if p.strip()]
        if not parts:
            raise ValueError("no value")
        return "|".join(one(p) for p in parts)
    check.meta = {"datatype": "STRING", "allowedvalues": list(options), "multiple": True}
    return check


def _mask(text):
    x = int(str(text), 0)
    if not 0 <= x < 1 << 64:
        raise ValueError("must be a 64-bit mask")
    return f"0x{x:x}"


_mask.meta = {"datatype": "NUMBER", "minvalue": 0, "maxvalue": "0xFFFFFFFFFFFFFFFF"}
DESCRIPTIONS = {"acqtriggersource": "the sources of the acquisition trigger (several with |)",
                "trgoutmode": "the signal on the TRG-OUT connector", "gpiomode": "the signal on the GPIO connector",
                "startsource": "what starts the acquisition", "syncoutmode": "the signal sent on CLK-OUT with the clock",
                "clocksource": "the board's clock: its own or CLK-IN", "vetosource": "what vetoes the acquisition",
                "testpulseperiod": "period of the internal test pulse (ns)", "testpulsewidth": "its width (ns)",
                "dacoutmode": "what the DAC OUT connector puts out", "recordlengths": "record length in samples",
                "pretriggers": "samples before the trigger", "chenable": "the channel is read out",
                "dcoffset": "baseline position in % of the input range", "triggerthr": "self-trigger threshold",
                "selftriggeredge": "the edge that triggers", "itlconnect": "which trigger logic the channel joins",
                "tempsenscore": "temperature of the FPGA core (°C)", "errorflags": "the board's error flags",
                "triggercnt": "triggers counted in the run", "losttriggercnt": "triggers lost in the run",
                "selftrgrate": "self-trigger rate of the channel (Hz)"}


def board_params(m: models.Model) -> dict:
    """name (lower case) -> (check, default); read-only ones have check None."""
    adc_max = m.adc_max
    p = {
        "recordlengths": (_int(4, rawdata.MAX_EVENT_WORDS), "1024"), "pretriggers": (_int(0, 1 << 20), "128"),
        "recordlengtht": (_int(8, 1 << 31), None), "pretriggert": (_int(0, 1 << 31), None),
        "iolevel": (_choice(("NIM", "TTL")), "NIM"), "enautodisarmacq": (_bool, "True"),
        "clocksource": (_choice(("Internal", "FPClkIn")), "Internal"),
        "syncoutmode": (_choice(("Disabled", "SyncIn", "TestPulse", "IntClk", "Run")), "Disabled"),
        "startsource": (_combo(START_SOURCES), "SWcmd"), "enclockoutfp": (_bool, "True"),
        "rundelay": (_int(0, 1 << 20), "0"), "volatileclockoutdelay": (_float(-20000, 20000, 74.074), "0.000"),
        "permanentclockoutdelay": (_float(-20000, 20000, 74.074), "0.000"),
        "acqtriggersource": (_combo(TRG_SOURCES), "SwTrg"), "trgoutmode": (_choice(TRGOUT_MODES), "Disabled"),
        "gpiomode": (_choice(GPIO_MODES), "Disabled"), "vetosource": (_combo(VETO_SOURCES), "Disabled"),
        "testpulseperiod": (_int(16, 10 ** 10), "100000"), "testpulsewidth": (_int(8, 10 ** 9), "128"),
        "testpulselowlevel": (_int(0, adc_max), "0"), "testpulsehighlevel": (_int(0, adc_max), str(adc_max // 2)),
        "itlamainlogic": (_choice(("OR", "AND", "Majority")), "OR"), "itlamajoritylev": (_int(1, 64), "2"),
        "itlapairlogic": (_choice(("None", "AND", "OR")), "None"),
        "itlapolarity": (_choice(("Direct", "Inverted")), "Direct"), "itlagatewidth": (_int(8, 10 ** 6), "16"),
        "itlamask": (_mask, "0x0"), "tstampresetsource": (_combo(("Start", "SIN", "GPIO", "EncodedClkIn")), "Start"),
        "dacoutmode": (_choice(("Static", "Ramp", "Sin5MHz", "Square", "IPE", "ChInput", "MemOccupancy", "ChSum",
                                "OverThrSum")), "Static"),
        "dacoutstaticlevel": (_int(0, adc_max), "0"), "dacoutchselect": (_int(0, m.channels - 1), "0"),
        "itlbmainlogic": (_choice(("OR", "AND", "Majority")), "OR"), "itlbmajoritylev": (_int(1, 64), "2"),
        "itlbpairlogic": (_choice(("None", "AND", "OR")), "None"),
        "itlbpolarity": (_choice(("Direct", "Inverted")), "Direct"), "itlbgatewidth": (_int(8, 10 ** 6), "16"),
        "itlbmask": (_mask, "0x0"), "boardvetosource": (_combo(VETO_SOURCES), "Disabled"),
        "boardvetowidth": (_int(0, 10 ** 9), "0"), "boardvetopolarity": (_choice(("ActiveHigh", "ActiveLow")), "ActiveHigh"),
        "maxrawdatasize": (None, str(MAX_RAW)), "fwtype": (None, "Scope"), "fpga_fwver": (None, "2024100301"),
        "cupver": (None, "2024100301"), "numch": (None, str(m.channels)), "adc_nbit": (None, str(m.adc_bits)),
        "adc_samplrate": (None, f"{m.msps:g}"), "tempsensairin": (None, "31.5"), "tempsensairout": (None, "36.0"),
        "tempsenscore": (None, "52.3"), "tempsensfirstadc": (None, "44.1"), "tempsenslastadc": (None, "46.8"),
        "tempsensdcdc": (None, "39.0"), "acquisitionstatus": (None, "0"), "tempsenshottestadc": (None, "47.5"),
        "speedsensfan1": (None, "3600"), "speedsensfan2": (None, "3540"), "vinsensdcdc": (None, "12.08"),
        "voutsensdcdc": (None, "1.00"), "ioutsensdcdc": (None, "8.20"), "dutycyclesensdcdc": (None, "41.0"),
        "freqsenscore": (None, "300"), "errorflags": (None, "0"), "triggercnt": (None, "0"),
        "losttriggercnt": (None, "0"), "realtimemonitor": (None, "0"), "deadtimemonitor": (None, "0"),
        "livetimemonitor": (None, "0"),
        "modelname": (None, ""), "serialnum": (None, ""), "formfactor": (None, "VME64X"),
    }
    if m.decimation:
        p["decimationfactor"] = (_choice(tuple(str(d) for d in models.DECIMATIONS)), "1")
    return p


def channel_params(m: models.Model) -> dict:
    adc_max = m.adc_max
    p = {"chenable": (_bool, "True"), "dcoffset": (_float(0, 100), "50.000"),
         "triggerthr": (_int(-adc_max, adc_max), "100"),
         "triggerthrmode": (_choice(("Relative", "Absolute")), "Relative"),
         "selftriggeredge": (_choice(("Rise", "Fall")), "Fall"), "samplesoverthreshold": (_int(0, 1 << 16), "0"),
         "itlconnect": (_choice(("Disabled", "ITLA", "ITLB")), "Disabled"), "selftrgrate": (None, "0"),
         "wavedatasource": (_choice(("ADC_DATA", "ADC_TEST_TOGGLE", "ADC_TEST_RAMP", "ADC_TEST_SIN",
                                     "ADC_TEST_PRBS", "IPE", "Ramp", "SquareWave")), "ADC_DATA")}
    if m.gain:
        p["chgain"] = (_float(0, 40), "0.000")
    return p


@functools.lru_cache(maxsize=32)
def rise_constant(rise_ns: float, decay_ns: float) -> float:
    """The time constant r of (1 - exp(-t/r)) exp(-t/decay) whose 10-90 % rise (of its own peak) is rise_ns."""
    def rise(r):
        top = math.log1p(decay_ns / r) * r
        t = np.linspace(0.0, top, 40001)
        s = (1 - np.exp(-t / r)) * np.exp(-t / decay_ns)
        peak = s[-1]
        return t[np.argmax(s >= 0.9 * peak)] - t[np.argmax(s >= 0.1 * peak)]
    lo, hi = rise_ns / 20, rise_ns
    for _ in range(50):
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if rise(mid) < rise_ns else (lo, mid)
    return (lo + hi) / 2


def parse_options(text: str) -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog="simulator", add_help=False, exit_on_error=False)
    ap.add_argument("--rate", type=float, default=20.0)
    ap.add_argument("--share", type=float, default=0.5)
    ap.add_argument("--amplitude", type=float, default=600.0)
    ap.add_argument("--noise", type=float, default=3.0)
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--fail", action="append", default=[])
    ap.add_argument("--generator", choices=("staircase",), default=None)
    ap.add_argument("--spacing-us", type=float, default=5.0)
    ap.add_argument("--rise-us", type=float, default=5.0)
    ap.add_argument("--decay-us", type=float, default=1000.0)
    try:
        args, rest = ap.parse_known_args(shlex.split(text or ""))
    except (argparse.ArgumentError, SystemExit) as err:
        raise ValueError(f"simulator options: {err}") from None
    if rest:
        raise ValueError(f"simulator options: not known: {' '.join(rest)}")
    return args


class Crate:
    """The simulated boards of one run, and what they share."""

    def __init__(self, options: str = "", cabling: str = "chain", models_by_name: dict | None = None,
                 channels_by_name: dict | None = None):
        self.opt = parse_options(options)
        self.cabling = cabling                  # "chain", "or_unit" or "none"
        self.models = models_by_name or {}      # board name -> model name (from the configuration)
        self.channels = channels_by_name or {}  # board name -> its number of inputs (0: the model's usual)
        self.boards: list[SimBoard] = []
        self.lock = threading.RLock()
        self.epoch = time.monotonic()
        seed = self.opt.seed if self.opt.seed is not None else int(time.time())
        self.rng = np.random.default_rng(seed)
        self.seed = seed
        self.hit_times = np.zeros(0)            # seconds since epoch, sorted
        self.hit_seeds = np.zeros(0, dtype=np.int64)
        self._hits_until = 0.0
        self.fails = [f.split(":") for f in self.opt.fail]

    def failing(self, kind: str, name: str):
        for f in self.fails:
            if f[0] == kind and len(f) > 1 and f[1] == name:
                return f
        return None

    def open(self, uri: str, name: str = "") -> "SimBoard":
        if self.failing("open", name):
            raise BoardError(f"cannot connect to {uri}: no reply (simulated)")
        b = SimBoard(self, uri, name, self.models.get(name, "V2740"), len(self.boards), self.channels.get(name, 0))
        self.boards.append(b)
        return b

    # ---- the physics: random hits, the same for every board ----
    def hits(self, t0: float, t1: float) -> list[tuple[float, int]]:
        with self.lock:
            if self._hits_until < t1:
                blocks_t, blocks_s = [self.hit_times], [self.hit_seeds]
                while self._hits_until < t1:
                    end = self._hits_until + 1.0
                    if self.opt.generator and self.opt.rate > 0:       # the generator: one pulse every 1/rate s
                        k = np.arange(math.ceil(self._hits_until * self.opt.rate), math.ceil(end * self.opt.rate))
                        blocks_t.append(k / self.opt.rate)
                        blocks_s.append(k.astype(np.int64))
                    else:
                        n = int(self.rng.poisson(self.opt.rate)) if self.opt.rate > 0 else 0
                        blocks_t.append(np.sort(self.rng.uniform(self._hits_until, end, n)))
                        blocks_s.append(self.rng.integers(0, 2 ** 31, n))
                    self._hits_until = end
                keep = min((b.produced_until for b in self.boards if b.produced_until is not None), default=0.0) - 30
                t_all, s_all = np.concatenate(blocks_t), np.concatenate(blocks_s)
                first = np.searchsorted(t_all, keep)            # hits long past for every board: forgotten
                self.hit_times, self.hit_seeds = t_all[first:], s_all[first:]
            lo = np.searchsorted(self.hit_times, t0, side="right")
            hi = np.searchsorted(self.hit_times, t1, side="right")
            return list(zip(self.hit_times[lo:hi].tolist(), self.hit_seeds[lo:hi].tolist()))

    def now(self) -> float:
        return time.monotonic() - self.epoch

    # ---- the run signal along the chain ----
    def run_signal(self, master: "SimBoard", on: bool, t: float) -> None:
        """The master's SyncOutMode = Run sends the run on CLK-OUT; boards with SyncIn pass it on."""
        k = master.index
        passes = master.p("syncoutmode") == "Run"
        while passes and k + 1 < len(self.boards):
            nxt = self.boards[k + 1]
            if nxt.p("clocksource") != "FPClkIn" or "EncodedClkIn" not in nxt.p("startsource"):
                break
            nxt.run_edge(on, t)
            passes = nxt.p("syncoutmode") == "SyncIn"
            k += 1

    # ---- trigger cables ----
    def trgin(self, b: "SimBoard", t0: float, t1: float, seen=None) -> list[tuple[float, int | None]]:
        seen = set() if seen is None else seen
        if b.index in seen:
            return []
        seen = seen | {b.index}
        if self.cabling == "chain":
            if b.index == 0:
                return list(self.hits(t0, t1))          # the pulser fires on every hit
            return self.trgout(self.boards[b.index - 1], t0, t1, seen)
        if self.cabling == "or_unit":
            out = []
            for other in self.boards:
                out += self.trgout(other, t0, t1, seen)
            return sorted(set(out), key=lambda x: x[0])
        return []

    def trgout(self, b: "SimBoard", t0: float, t1: float, seen) -> list[tuple[float, int | None]]:
        mode = b.p("trgoutmode")
        if not b.running_between(t0, t1):
            return []
        if mode == "TrgIn":
            return self.trgin(b, t0, t1, seen)
        if mode == "SwTrg":
            return [(t, None) for t in b.sw_between(t0, t1)]
        if mode in ("ITLA", "ITLB", "ITLA_AND_ITLB", "ITLA_OR_ITLB"):
            return b.itl_sources(t0, t1, mode)
        if mode == "TestPulse":
            return [(t, None) for t in b.test_pulses(t0, t1)]
        if mode == "AcceptTrg":                 # the triggers the board took
            return b.triggers(t0, t1, seen)
        return []


class SimBoard:
    def __init__(self, crate: Crate, uri: str, name: str, model_name: str, index: int, n_channels: int = 0):
        self.crate, self.uri, self.name, self.index = crate, uri, name, index
        self.m = models.model(model_name) or models.MODELS["2740"]
        if n_channels and n_channels != self.m.channels:     # a smaller variant (e.g. a 16-input VX2730)
            self.m = dataclasses.replace(self.m, channels=n_channels)
        self.model_name = model_name if models.model(model_name) else "V2740"
        self.serial = str(10000 + zlib.crc32(f"{name}{uri}".encode()) % 50000)
        self.bp, self.cp = board_params(self.m), channel_params(self.m)
        self.refused = {f[2].lower() for f in crate.fails if f[0] == "param" and f[1] == name and len(f) > 2}
        self.read_fail = next((int(f[2]) for f in crate.fails if f[0] == "read" and f[1] == name and len(f) > 2), None)
        self.no_stop = crate.failing("nostop", name) is not None
        self.no_arm = crate.failing("unarmed", name) is not None
        self.err_after = next((float(f[2]) for f in crate.fails if f[0] == "errflags" and f[1] == name and len(f) > 2), None)
        self.reads = 0
        self.reset()

    # ---- parameters ----
    def reset(self) -> None:
        self.values = {k: v for k, (_, v) in self.bp.items() if v is not None}
        self.ch = [{k: v for k, (_, v) in self.cp.items()} for _ in range(self.m.channels)]
        self.endpoint = "scope"
        self.armed = self.running = False
        self.t_start = self.t_stop = None
        self.sw_times: list[float] = []
        self.produced_until = None
        self.sent_start = self.sent_stop = False
        self.trigger_id = 0
        self.pending = bytearray()

    def p(self, name: str) -> str:
        return self.values.get(name, "")

    def _parse(self, path: str):
        parts = path.strip().split("/")
        if len(parts) == 3 and parts[1].lower() == "par":
            return "board", None, parts[2].lower()
        if len(parts) == 5 and parts[1].lower() == "ch" and parts[3].lower() == "par":
            spec = parts[2]
            if ".." in spec:
                a, b = spec.split("..", 1)
                chans = list(range(int(a), int(b) + 1))
            else:
                chans = [int(spec)]
            if not chans or min(chans) < 0 or max(chans) >= self.m.channels:
                raise BoardError(f"{path}: no such channel on a {self.model_name}")
            return "ch", chans, parts[4].lower()
        if len(parts) == 4 and parts[1].lower() == "endpoint" and parts[2].lower() == "par":
            return "endpoint", None, parts[3].lower()
        raise BoardError(f"{path}: not a parameter path")

    def get(self, path: str) -> str:
        kind, chans, name = self._parse(path)
        if kind == "endpoint":
            if name != "activeendpoint":
                raise BoardError(f"{path}: not found")
            return self.endpoint
        if kind == "ch":
            if name not in self.cp:
                raise BoardError(f"{path}: not found")
            if name == "selftrgrate":
                on = self.ch[chans[0]]["chenable"] == "True" and self.running
                return f"{self.crate.opt.rate * self.crate.opt.share:.1f}" if on else "0"
            return self.ch[chans[0]][name]
        if name not in self.bp:
            raise BoardError(f"{path}: not found")
        if name == "modelname":
            return self.model_name
        if name == "serialnum":
            return self.serial
        if name == "acquisitionstatus":
            return str((1 if self.armed else 0) | (2 if self.running else 0))
        if name == "recordlengtht":
            return str(int(int(self.p("recordlengths")) * self.sample_ns()))
        hk = self.housekeeping(name)
        if hk is not None:
            return hk
        if name == "pretriggert":
            return str(int(int(self.p("pretriggers")) * self.sample_ns()))
        return self.values.get(name, "")

    def set(self, path: str, value: str) -> None:
        kind, chans, name = self._parse(path)
        if kind == "endpoint":
            if name != "activeendpoint" or str(value).lower() not in ("raw", "scope"):
                raise BoardError(f"set {path} = {value}: not allowed")
            self.endpoint = str(value).lower()
            return
        table = self.cp if kind == "ch" else self.bp
        if name in self.refused or name not in table:
            raise BoardError(f"set {path} = {value}: no such parameter on this firmware (simulated)")
        check = table[name][0]
        if check is None:
            raise BoardError(f"set {path}: read-only")
        if self.armed and name not in ("dcoffset", "triggerthr"):
            raise BoardError(f"set {path}: not while the acquisition is armed")
        try:
            v = check(value)
        except ValueError as err:
            raise BoardError(f"set {path} = {value}: {err}") from None
        if kind == "ch":
            for c in chans:
                self.ch[c][name] = v
        elif name in ("recordlengtht", "pretriggert"):
            self.values[name[:-1] + "s"] = str(max(4, int(round(int(v) / self.sample_ns()))))
        else:
            self.values[name] = v

    def running_s(self) -> float:
        if self.t_start is None:
            return 0.0
        return (self.t_stop if self.t_stop is not None else self.crate.now()) - self.t_start

    def housekeeping(self, name: str) -> str | None:
        """The changing housekeeping values (None: the parameter keeps its table value)."""
        t = self.running_s()
        if name.startswith("tempsens"):
            return f"{float(self.bp[name][1]) + 3.0 * (1 - math.exp(-t / 120.0)) + 0.1 * self.index:.1f}"
        if name == "triggercnt":
            return str(self.trigger_id)
        if name == "realtimemonitor":
            return str(int(t * 1e9))
        if name == "deadtimemonitor":
            return str(self.trigger_id * 1000)
        if name == "livetimemonitor":
            return str(max(0, int(t * 1e9) - self.trigger_id * 1000))
        if name == "errorflags":
            return "0x800" if self.err_after is not None and t >= self.err_after else "0"
        return None

    def model(self) -> models.Model:
        return self.m

    def sample_ns(self) -> float:
        d = int(self.p("decimationfactor") or 1) if self.m.decimation else 1
        return self.m.sample_ns * d

    # ---- commands ----
    def cmd(self, name: str) -> None:
        c = name.lower()
        if c not in COMMANDS:
            raise BoardError(f"command {name}: not known")
        with self.crate.lock:
            now = self.crate.now()          # under the lock: no reader produces past the moment of a stop
            if c == "reset":
                self.reset()
            elif c == "armacquisition":
                if self.no_arm:
                    return
                if self.endpoint != "raw":
                    raise BoardError("command ArmAcquisition: Vx27webDAQ reads the raw endpoint; activate it first")
                self.armed = True
                self.produced_until = None
                self.sent_start = self.sent_stop = False
                self.t_start = self.t_stop = None
                self.trigger_id = 0
                self.pending = bytearray()
            elif c == "disarmacquisition":
                self.armed = False
            elif c == "swstartacquisition":
                if "SWcmd" not in self.p("startsource"):
                    raise BoardError("command SwStartAcquisition: StartSource is not SWcmd")
                if not self.armed:
                    raise BoardError("command SwStartAcquisition: the board is not armed")
                self.run_edge(True, now)
                self.crate.run_signal(self, True, now)
            elif c == "swstopacquisition":
                if self.running:
                    self.run_edge(False, now)
                    self.crate.run_signal(self, False, now)
            elif c == "sendswtrigger":
                if self.running:
                    self.sw_times.append(now)
            elif c == "cleardata":
                self.pending = bytearray()

    def run_edge(self, on: bool, t: float) -> None:
        if on and self.armed and not self.running and self.t_start is None:
            self.running, self.t_start, self.produced_until = True, t, t
        elif not on and self.running:
            self.running, self.t_stop = False, t
            if self.p("enautodisarmacq") == "True":
                self.armed = False

    def running_between(self, t0: float, t1: float) -> bool:
        return self.t_start is not None and t1 >= self.t_start and (self.t_stop is None or t0 <= self.t_stop)

    def window(self, t0: float, t1: float) -> tuple[float, float]:
        lo = max(t0, self.t_start)
        hi = t1 if self.t_stop is None else min(t1, self.t_stop)
        return lo, hi

    # ---- this board's trigger sources ----
    def sw_between(self, t0, t1):
        return [t for t in self.sw_times if t0 < t <= t1]

    def test_pulses(self, t0, t1):
        period = int(self.p("testpulseperiod")) * 1e-9
        if self.t_start is None or period <= 0:
            return []
        k0 = math.floor((t0 - self.t_start) / period) + 1
        k1 = math.floor((t1 - self.t_start) / period)
        return [self.t_start + k * period for k in range(max(1, k0), k1 + 1)]

    def hit_amplitudes(self, seed: int) -> np.ndarray:
        """Per channel of this board: the pulse amplitude of a hit (0: no pulse there)."""
        if self.crate.opt.generator:                       # the generator: every channel, the same height
            return np.full(self.m.channels, float(self.crate.opt.amplitude))
        rng = np.random.default_rng([seed, self.index])
        on = rng.random(self.m.channels) < self.crate.opt.share
        return np.where(on, self.crate.opt.amplitude * rng.uniform(0.5, 1.5, self.m.channels), 0.0)

    def itla(self, t0, t1):
        return self.itl(t0, t1, "ITLA")

    def itl_sources(self, t0, t1, name: str):
        """The triggers of a channel-trigger source: ITLA, ITLB, ITLA_AND_ITLB, ITLA_OR_ITLB."""
        if name in ("ITLA", "ITLB"):
            return self.itl(t0, t1, name)
        a, b = self.itl(t0, t1, "ITLA"), self.itl(t0, t1, "ITLB")
        if name == "ITLA_AND_ITLB":
            tb = {t for t, _ in b}
            return [x for x in a if x[0] in tb]
        merged = {x[0]: x for x in a + b}
        return [merged[t] for t in sorted(merged)]

    def itl(self, t0, t1, block: str):
        connected = [c for c in range(self.m.channels)
                     if self.ch[c]["itlconnect"] == block and self.ch[c]["chenable"] == "True"]
        pre = block.lower()
        mask = int(self.p(f"{pre}mask"), 16)
        if not connected and mask:
            connected = [c for c in range(self.m.channels) if mask >> c & 1 and self.ch[c]["chenable"] == "True"]
        if not connected:
            return []
        logic, lev = self.p(f"{pre}mainlogic"), int(self.p(f"{pre}majoritylev"))
        out = []
        for t, seed in self.crate.hits(t0, t1):
            amp = self.hit_amplitudes(seed)
            passed = sum(1 for c in connected if amp[c] > 0 and amp[c] >= abs(int(self.ch[c]["triggerthr"])))
            if (logic == "OR" and passed >= 1) or (logic == "AND" and passed == len(connected)) or \
                    (logic == "Majority" and passed >= lev):
                out.append((t, seed))
        return out

    def triggers(self, t0: float, t1: float, seen=None) -> list[tuple[float, int | None]]:
        sources = self.p("acqtriggersource").split("|")
        out: list[tuple[float, int | None]] = []
        if "SwTrg" in sources:
            out += [(t, None) for t in self.sw_between(t0, t1)]
        if "TestPulse" in sources:
            out += [(t, None) for t in self.test_pulses(t0, t1)]
        for name in ("ITLA", "ITLB", "ITLA_AND_ITLB", "ITLA_OR_ITLB"):
            if name in sources:
                out += self.itl_sources(t0, t1, name)
        if "TrgIn" in sources:
            out += self.crate.trgin(self, t0, t1, seen)
        out.sort(key=lambda x: x[0])
        merged = []                         # one trigger for the same moment from two sources
        for t, s in out:
            if merged and abs(t - merged[-1][0]) < 1e-9:
                continue
            merged.append((t, s))
        return merged

    # ---- the data ----
    def waveforms(self, channels: list[int], seed: int | None, n: int) -> np.ndarray:
        rng = np.random.default_rng() if seed is None else np.random.default_rng([seed, self.index, 7])
        adc_max = self.m.adc_max
        base = np.array([float(self.ch[c]["dcoffset"]) / 100 * adc_max + ((c * 37) % 41 - 20) for c in channels])
        w = base[:, None] + rng.normal(0, self.crate.opt.noise, (len(channels), n))
        if seed is not None:
            amp = self.hit_amplitudes(seed)
            pre = int(self.p("pretriggers"))
            tau = max(2.0, 120.0 / self.sample_ns())         # 120 ns decay
            rise = max(1.0, 8.0 / self.sample_ns())
            def pulse(start, r=rise, d=tau):               # one pulse beginning at sample `start`, peak 1
                k = np.maximum(np.arange(n) - start, 0)
                s = np.where(np.arange(n) < start, 0.0, (1 - np.exp(-k / r)) * np.exp(-k / d))
                top = math.log1p(d / r) * r                                  # where the shape peaks
                return s / max((1 - math.exp(-top / r)) * math.exp(-top / d), 1e-12)
            gen = self.crate.opt.generator
            if gen:
                if self.m.family == "2730":             # every channel at once, a fast negative pulse
                    shape = pulse(pre - 2)
                    for i, c in enumerate(channels):
                        w[i] -= amp[c] * shape
                else:                                     # slow positive pulses, channel k k x spacing later
                    ns, o = self.sample_ns(), self.crate.opt
                    r = max(0.5, rise_constant(o.rise_us * 1000.0, o.decay_us * 1000.0) / ns)
                    d = max(1.0, o.decay_us * 1000.0 / ns)
                    for i, c in enumerate(channels):
                        w[i] += amp[c] * pulse(pre + c * o.spacing_us * 1000.0 / ns, r, d)
            else:
                shape = pulse(pre - 2)
                for i, c in enumerate(channels):
                    if amp[c] > 0:
                        sign = 1.0 if self.ch[c]["selftriggeredge"] == "Rise" else -1.0
                        w[i] += sign * amp[c] * shape
        return np.clip(np.rint(w), 0, adc_max).astype(np.uint16)

    def _produce(self, until: float) -> None:
        """Append the events up to `until` (and the stop record after the run) to the pending bytes."""
        if self.t_start is None:
            return
        if not self.sent_start:
            ch_mask = rawdata.channels_mask(c for c in range(self.m.channels) if self.ch[c]["chenable"] == "True")
            dec = int(self.p("decimationfactor") or 1) if self.m.decimation else 1
            self.pending += rawdata.encode_start(int(self.p("recordlengths")), ch_mask, dec)
            self.sent_start = True
        lo, hi = self.window(self.produced_until, until)
        if hi > lo:
            channels = [c for c in range(self.m.channels) if self.ch[c]["chenable"] == "True"]
            n = int(self.p("recordlengths"))
            n -= n % 4
            for t, seed in self.triggers(lo, hi):
                ticks = int(round((t - self.t_start) / TICK_S))
                waves = self.waveforms(channels, seed, n)
                self.pending += rawdata.encode_scope(self.trigger_id & 0xFFFFFF, ticks, channels, waves)
                self.trigger_id += 1
                if len(self.pending) > MAX_RAW:
                    hi = t
                    break
            self.produced_until = hi
        if self.t_stop is not None and self.produced_until >= self.t_stop and not self.sent_stop and not self.no_stop:
            self.pending += rawdata.encode_stop(int(round((self.t_stop - self.t_start) / TICK_S)), 0)
            self.sent_stop = True

    def open_raw(self) -> None:
        self.set("/endpoint/par/ActiveEndpoint", "raw")

    def read_raw(self, timeout_ms: int):
        self.reads += 1
        if self.read_fail is not None and self.reads > self.read_fail:
            raise BoardError("reading data: connection lost (simulated)")
        deadline = time.monotonic() + timeout_ms / 1000
        while True:
            with self.crate.lock:
                if self.running or (self.t_stop is not None and not self.sent_stop):
                    self._produce(self.crate.now())
                if self.pending:
                    cut = rawdata.scan(self.pending, 0, min(len(self.pending), MAX_RAW)).end
                    if cut == 0:                    # one event larger than a buffer: it goes alone
                        cut = rawdata.header_word(self.pending)[1] * rawdata.WORD
                    data, self.pending = bytes(self.pending[:cut]), self.pending[cut:]
                    return memoryview(data), rawdata.scan(data).events
            left = deadline - time.monotonic()
            if left <= 0:
                return None, 0
            time.sleep(min(left, 0.02))

    def identify(self) -> dict:
        from vx27webdaq.daq.boards import identify
        return identify(self)

    def device_tree(self) -> dict:
        """Like FELib's device tree: par, ch/N/par, cmd, endpoint (values and allowed values)."""
        def node(table, values):
            out = {}
            for k, (check, _) in table.items():
                out[k] = {"value": values.get(k, ""), "accessmode": "READ_ONLY" if check is None else "READ_WRITE",
                          **getattr(check, "meta", {}), "description": DESCRIPTIONS.get(k, "")}
            return out
        vals = dict(self.values, modelname=self.model_name, serialnum=self.serial)
        tree = {"par": node(self.bp, vals),
                "ch": {str(c): {"par": node(self.cp, self.ch[c])} for c in range(self.m.channels)},
                "cmd": {c: {} for c in COMMANDS}, "endpoint": {"par": {"activeendpoint": {"value": self.endpoint}},
                                                               "raw": {}, "scope": {}}}
        tree["par"]["modelname"] = {"value": self.model_name, "accessmode": "READ_ONLY"}
        tree["par"]["serialnum"] = {"value": self.serial, "accessmode": "READ_ONLY"}
        return tree

    def close(self) -> None:
        self.armed = self.running = False
