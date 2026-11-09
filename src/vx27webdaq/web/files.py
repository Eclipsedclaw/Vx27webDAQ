"""Data files for the page: browsing the data folders, and the waveforms of .raw and .root files.

Only files inside the data folders can be opened: the data folder of the configuration, the save folder of
the runs, and the folders added in the settings. The server reads them as the service account, so this keeps
users to the data and away from everything else that account can read.

A data file carries its own settings: the header of NAME.raw (and the "settings" string of NAME.root) holds the
board, every parameter as it was set, and the run's configuration; the note may have been edited during the
run, so the note of NAME.json next to the file counts when it is there.
"""

from __future__ import annotations

import json
import math
import os
import stat
import threading
from collections import OrderedDict

import numpy as np

from vx27webdaq.core import config as C
from vx27webdaq.core import models, rawdata, rootconv

DATA_SUFFIXES = (".raw", ".root")
MAX_BINS = 4000
MAX_FULL = 1 << 16             # full=1 (FFT, histogram, averaging): at most this many samples of a channel
BASELINE_SAMPLES = 16           # the baseline of a waveform: the mean of its first 16 samples (as CAEN's demo)
BROWSE_LIMIT = 5000
SUMMARY_EVENTS = 200
MAX_TRACES = 16                 # the combined view: at most this many channels in one plot
Y_UNITS = ("mv", "adc", "raw")    # mV from the baseline, ADC counts from the baseline, raw ADC counts
ALIGNS = ("trigger", "timestamp")  # time 0: each board's own trigger, or the first board's (timestamps)


class FileAccessError(PermissionError):
    """A path outside the data folders, or one that is not a data file."""


def browse(path: str, hidden: bool = False) -> dict:
    """Any folder the service account can read, for operators choosing a configuration file or a folder (not the
    data folders' chooser, which keeps everyone to the data): its folders and files, the names starting with "."
    only with hidden, at most BROWSE_LIMIT. The path of a file gives its folder, with that file in "file"."""
    folder = os.path.abspath(os.path.expanduser(path or "~"))
    picked = None
    if os.path.isfile(folder):
        folder, picked = os.path.dirname(folder), os.path.basename(folder)
    if not os.path.exists(folder):
        raise FileNotFoundError(f"{folder} does not exist")
    if not os.path.isdir(folder):
        raise ValueError(f"{folder} is not a folder")
    entries = []
    try:
        with os.scandir(folder) as it:
            for e in it:
                if e.name.startswith(".") and not hidden:
                    continue
                try:
                    st = e.stat()
                except OSError:
                    continue
                if stat.S_ISDIR(st.st_mode):
                    entries.append({"name": e.name, "kind": "folder", "mtime": st.st_mtime})
                else:
                    entries.append({"name": e.name, "kind": "file", "size": st.st_size, "mtime": st.st_mtime,
                                    "exec": bool(st.st_mode & 0o111) and os.access(e.path, os.X_OK)})
    except PermissionError:
        raise PermissionError(f"the server may not read the folder {folder}") from None
    entries.sort(key=lambda x: (x["kind"] != "folder", x["name"].lower()))
    return {"folder": folder, "parent": os.path.dirname(folder) if folder != "/" else None, "file": picked,
            "entries": entries[:BROWSE_LIMIT], "more": max(0, len(entries) - BROWSE_LIMIT)}


def envelope(samples: np.ndarray, x0: int = 0, x1: int | None = None, bins: int = 800) -> dict:
    """The samples from x0 to x1 for a plot `bins` points wide: every sample when there are few ("raw"), else
    per bin of `step` samples its lowest and highest, in the order they come ("envelope"), so a spike shows."""
    n_all = len(samples)
    x0 = max(0, min(int(x0), n_all))
    x1 = n_all if x1 is None else max(x0, min(int(x1), n_all))
    part = np.asarray(samples[x0:x1])
    bins = max(16, min(int(bins), MAX_BINS))
    if len(part) <= 2 * bins:
        return {"mode": "raw", "x0": x0, "step": 1, "y": part.astype(int).tolist()}
    step = math.ceil(len(part) / bins)
    full = len(part) // step
    rows = part[:full * step].reshape(full, step)
    ys = [_ordered_extremes(rows)]
    if len(part) > full * step:
        ys.append(_ordered_extremes(part[full * step:].reshape(1, -1)))
    return {"mode": "envelope", "x0": x0, "step": step, "y": np.concatenate(ys).astype(int).tolist()}


def _ordered_extremes(rows: np.ndarray) -> np.ndarray:
    lo_i, hi_i = rows.argmin(axis=1), rows.argmax(axis=1)
    idx = np.arange(len(rows))
    lo, hi = rows[idx, lo_i], rows[idx, hi_i]
    first = np.where(lo_i <= hi_i, lo, hi)
    second = np.where(lo_i <= hi_i, hi, lo)
    return np.stack([first, second], axis=1).ravel()


def measure(samples: np.ndarray, x0: int, x1: int, baseline: float, sample_ns: float) -> dict:
    """The samples x0 <= k < x1 of one waveform: lowest, highest, mean, σ, the sample farthest from the
    baseline (peak, signed, and where), and the area against the baseline in ADC·ns."""
    a, b = max(0, int(x0)), min(len(samples), int(x1))
    if b <= a:
        return {"x0": a, "x1": b, "n": 0}
    part = np.asarray(samples[a:b], dtype=float)
    d = part - baseline
    k = int(np.argmax(np.abs(d)))
    return {"x0": a, "x1": b, "n": b - a, "min": int(part.min()), "max": int(part.max()), "mean": float(part.mean()),
            "std": float(part.std()), "peak": float(d[k]), "peak_at": a + k, "area": float(d.sum() * sample_ns),
            "first": int(part[0]), "last": int(part[-1])}       # the values at the two edges (WaveDump2's cursors)


def channel_view(samples, enabled: bool, ch: int, trigger, full_length: int, x0: int, x1: int | None, bins: int,
                 lines: dict | None = None, span: tuple[int, int] | None = None, sample_ns: float = 8.0,
                 full: bool = False) -> dict:
    """What a waveform plot shows for one channel; lines: where the trigger and the threshold sit."""
    out = {"ch": ch, "enabled": enabled, "present": samples is not None, "trigger": trigger, **(lines or {})}
    if samples is None:
        return out
    s = np.asarray(samples)
    out.update(nsamps=int(len(s)), short=bool(full_length and len(s) < full_length), full_length=full_length)
    if len(s):
        baseline = float(s[:BASELINE_SAMPLES].mean())
        data = ({"mode": "raw", "x0": 0, "step": 1, "y": s[:MAX_FULL].astype(int).tolist(), "cut": len(s) > MAX_FULL}
                if full else envelope(s, x0, x1, bins))
        out.update(mean=float(s.mean()), std=float(s.std()), baseline=baseline, minimum=int(s.min()),
                   maximum=int(s.max()), data=data)
        if span is not None:
            out["measure"] = measure(s, span[0], span[1], baseline, sample_ns)
    return out


def mv_per_adc(info: dict) -> float:
    """One ADC count in mV: the model's input range (2 Vpp on the V2730 and V2740) over its ADC scale."""
    m = models.model(info.get("model") or "")
    return (m.input_vpp if m else 2.0) * 1000.0 / ((info.get("adc_max") or 65535) + 1)


def trace_view(samples, info: dict, shift_ns: float, t_lo: float | None, t_hi: float | None, bins: int,
               yunit: str) -> dict:
    """One channel for the combined plot: x in ns from the trigger (+ shift_ns), y in yunit, the samples of
    the time window [t_lo, t_hi] thinned to `bins` points like envelope()."""
    if samples is None:
        return {"present": False}
    s = np.asarray(samples)
    if not len(s):
        return {"present": False}
    ns, pre = float(info["sample_ns"]), int(info.get("pre_trigger") or 0)
    first = -pre * ns + shift_ns                       # the time of sample 0
    i0 = 0 if t_lo is None else max(0, min(len(s), math.floor((t_lo - first) / ns)))
    i1 = len(s) if t_hi is None else max(i0, min(len(s), math.ceil((t_hi - first) / ns) + 1))
    env = envelope(s, i0, i1, bins)
    base = float(s[:BASELINE_SAMPLES].mean())
    y = np.asarray(env["y"], dtype=float)
    if yunit == "mv":
        y = np.round((y - base) * mv_per_adc(info), 3)
    elif yunit == "adc":
        y = np.round(y - base, 2)
    return {"present": True, "mode": env["mode"], "x0_ns": first + env["x0"] * ns, "step_ns": env["step"] * ns,
            "y": y.tolist(), "baseline": base, "nsamps": int(len(s)), "std": float(s[:BASELINE_SAMPLES].std())}


def combined_view(entries: list[dict], traces: list[tuple[str, int]], t_lo: float | None, t_hi: float | None,
                  bins: int, yunit: str, align: str) -> dict:
    """Channels of several boards in one plot. entries: per board {name, info (board_lines), ev, enabled}; the
    time axis covers every board's window: each from its own trigger, or (align \"timestamp\") shifted by its
    timestamp against the first board's, so the same moment lines up on all boards."""
    ref = next((e["ev"].timestamp for e in entries if e["ev"] is not None), None)
    boards, lo, hi = [], None, None
    for e in entries:
        info, ev = e["info"], e["ev"]
        ns, pre = float(info["sample_ns"]), int(info.get("pre_trigger") or 0)
        rec = int(info.get("record_length") or (ev.samples if ev is not None else 0))
        shift = (ev.timestamp - ref) * rawdata.TIMESTAMP_NS if align == "timestamp" and ev is not None and ref is not None else 0
        win = [-pre * ns + shift, (rec - pre) * ns + shift]
        lo, hi = (win[0] if lo is None else min(lo, win[0])), (win[1] if hi is None else max(hi, win[1]))
        e["shift"] = shift
        boards.append({"name": e["name"], "model": info.get("model"), "sample_ns": ns, "pre_trigger": pre,
                       "record_length": rec, "adc_max": info.get("adc_max"), "mv_per_adc": mv_per_adc(info),
                       "shift_ns": shift, "window_ns": win, "present": ev is not None,
                       "trigger_id": ev.trigger_id if ev is not None else None,
                       "timestamp": ev.timestamp if ev is not None else None})
    by_name = {e["name"]: e for e in entries}
    out = []
    for name, c in traces:
        e = by_name.get(name)
        if e is None:
            out.append({"board": name, "ch": c, "present": False, "why": "no such board"})
            continue
        enabled = e["enabled"][c] if c < len(e["enabled"]) else False
        samples = e["ev"].waveform(c) if e["ev"] is not None else None
        v = trace_view(samples, e["info"], e["shift"], t_lo, t_hi, bins, yunit)
        why = "" if v["present"] else ("not read out" if not enabled else "no event" if e["ev"] is None else "not in this event")
        out.append({"board": name, "ch": c, "enabled": enabled, "why": why, **v})
    ids = {b["trigger_id"] for b in boards if b["present"]}
    stamps = [b["timestamp"] for b in boards if b["present"]]
    return {"boards": boards, "traces": out, "range_ns": [lo or 0.0, hi or 0.0], "yunit": yunit, "align": align,
            "same_ids": len(ids) == 1, "spread_ns": (max(stamps) - min(stamps)) * rawdata.TIMESTAMP_NS if stamps else None}


HK_ROWS = 2000                  # the most readings a housekeeping table sends (every n-th when there are more)


def hk_table(path: str, max_rows: int = HK_ROWS) -> dict:
    """A board's housekeeping file for the page: its columns, thinned to max_rows readings (the last kept)."""
    cols = rootconv.read_hk(path)
    n = len(next(iter(cols.values()))) if cols else 0
    keep = np.unique(np.concatenate([np.arange(0, n, max(1, math.ceil(n / max_rows))), [n - 1]])) if n else np.zeros(0, int)
    out = {}
    for k, v in cols.items():
        x = v[keep]
        out[k] = [None if not np.isfinite(e) else float(e) for e in x]
    return {"path": path, "rows": n, "shown": int(len(keep)), "columns": out}


def board_lines(cfg: C.DaqConfig | None, board: C.Board | None, status: dict | None = None,
                header_board: dict | None = None) -> dict:
    """The facts of a board's records and, per channel, where the trigger (sample pre_trigger) and the threshold
    (baseline ± threshold, in the pulse's direction; used only when the board triggers on its channels) sit."""
    src = status or header_board or {}
    m = models.model((src.get("model") or (board.model if board else "")) or "") or models.MODELS["2740"]
    rec = src.get("record_length") or (board.record_length if board else 0)
    pre = src.get("pre_trigger") if src.get("pre_trigger") is not None else (board.pre_trigger if board else None)
    sns = src.get("sample_ns") or (C.sample_ns(board, m) if board else m.sample_ns)
    bits = src.get("adc_bits") or m.adc_bits
    k = next((j for j, (_, x) in enumerate(cfg.active()) if board is not None and x.name == board.name), None) if cfg else None
    used = bool(cfg and C.triggers_on_channels(cfg, k))      # only a board that triggers on its own channels
    lines = []
    n = len(board.channels) if board else int(src.get("channels") or m.channels)
    for c in range(n):
        ch = board.channels[c] if board and c < len(board.channels) else None
        thr = None if ch is None else (ch.threshold if ch.positive else -ch.threshold)
        lines.append({"trigger_at": pre, "threshold": thr, "threshold_used": used and bool(ch and ch.self_trigger)})
    return {"record_length": int(rec or 0), "pre_trigger": pre, "sample_ns": float(sns), "adc_max": (1 << int(bits)) - 1,
            "model": src.get("model") or (board.model if board else m.text), "lines": lines}


def event_summary(events: list, n_channels: int) -> dict:
    """Per channel, over the events given: the baseline (mean of the first 16 samples), the noise (σ of those
    samples) and the peak (the largest distance from the baseline), averaged; None for a channel not read out."""
    base = [[] for _ in range(n_channels)]
    noise = [[] for _ in range(n_channels)]
    peak = [[] for _ in range(n_channels)]
    for ev in events:
        if ev is None:
            continue
        for c, w in zip(ev.channels, ev.waveforms):
            if c >= n_channels or not len(w):
                continue
            head = w[:BASELINE_SAMPLES].astype(float)
            b = float(head.mean())
            base[c].append(b)
            noise[c].append(float(head.std()))
            d = w.astype(float) - b
            peak[c].append(float(d[np.argmax(np.abs(d))]))

    def avg(x):
        return [float(np.mean(v)) if v else None for v in x]
    return {"baseline": avg(base), "noise": avg(noise), "peak": avg(peak), "events": len([e for e in events if e])}


def side_json(data_path: str) -> str:
    """NAME.json, the configuration kept next to NAME.raw and NAME.root."""
    return os.path.splitext(data_path)[0] + ".json"


def _side_note(path: str) -> str | None:
    try:
        with open(side_json(path), encoding="utf-8") as f:
            doc = json.load(f)
        return doc.get("note") if isinstance(doc.get("note"), str) else None
    except (OSError, ValueError):
        return None


class OpenFile:
    """A data file opened for the page: .raw (core.rawdata) or .root (uproot), with its header."""

    def __init__(self, path: str):
        st = os.stat(path)
        self.path, self.stamp = path, (st.st_mtime_ns, st.st_size)
        self.kind = path.rsplit(".", 1)[1].lower()
        self.lock = threading.Lock()
        if self.kind == "raw":
            self.raw = rawdata.RawFile(path)
            self.header = self.raw.header
            self.n = len(self.raw.index())
            self.truncated = self.raw.truncated
        else:
            import uproot
            self.root = uproot.open(path)
            try:
                self.tree = self.root["events"]
                self.header = json.loads(str(self.root["settings"])) if "settings" in self.root else {}
            except Exception as err:            # uproot raises many kinds for files of other programs
                self.root.close()
                raise rawdata.RawFormatError(f"{os.path.basename(path)} is not a ROOT file of Vx27webDAQ ({err})") from None
            if self.header.get("format") == "alab-daq-built":
                self.root.close()
                raise rawdata.RawFormatError(f"{os.path.basename(path)} holds every board of a run per trigger: open it in "
                                             "ROOT (TTree events); the page shows one board's files")
            self.n = int(self.tree.num_entries)
            self.truncated = bool(self.header.get("truncated"))
            self.channels = sorted(int(k[2:]) for k in self.tree.keys() if k.startswith("ch") and k[2:].isdigit())
        try:
            self.cfg = C.from_dict(self.header["config"]) if isinstance(self.header.get("config"), dict) else None
        except C.ConfigError:
            self.cfg = None
        self.board_header = self.header.get("board", {}) or {}
        name = self.board_header.get("name")
        self.board = self.cfg.board(name) if self.cfg and name else None
        note = _side_note(path) if self.kind == "raw" else _side_note(path[:-5] + ".raw")
        self.note = note if note is not None else str(self.header.get("note") or "")
        self._ids = None

    def find_trigger(self, trigger_id: int) -> int | None:
        """The number of the event with this trigger ID (the first, if the 24-bit IDs went round), or None."""
        with self.lock:
            if self._ids is None:
                if self.kind == "raw":
                    ids = self.raw.headers()["trigger_id"].astype(np.int64)
                else:
                    ids = np.asarray(self.tree["trigger_id"].array(library="np"), dtype=np.int64)
                order = np.argsort(ids, kind="stable")
                self._ids = (ids[order], order)
        s, order = self._ids
        i = int(np.searchsorted(s, trigger_id))
        return int(order[i]) if i < len(s) and s[i] == trigger_id else None

    def enabled(self) -> list[bool]:
        """Per channel: read out in the run (from the configuration in the header, else from the first event)."""
        n = len(self.lines()["lines"])
        if self.board is not None:
            return [c < len(self.board.channels) and self.board.channels[c].enabled for c in range(n)]
        ev = self.event(0) if self.n else None
        return [bool(ev is not None and c in ev.channels) for c in range(n)]

    def event(self, k: int) -> rawdata.ScopeEvent:
        if not 0 <= k < self.n:
            raise IndexError(f"event {k} of {self.n}")
        with self.lock:
            if self.kind == "raw":
                return self.raw.event(k)
            names = ["trigger_id", "timestamp", "ch_mask"] + [f"ch{c}" for c in self.channels]
            arr = self.tree.arrays(names, entry_start=k, entry_stop=k + 1, library="np")
            mask = int(arr["ch_mask"][0])
            chans = [c for c in self.channels if mask >> c & 1]
            waves = np.stack([np.asarray(arr[f"ch{c}"][0], dtype=np.uint16) for c in chans]) if chans \
                else np.zeros((0, 0), np.uint16)
            return rawdata.ScopeEvent(int(arr["trigger_id"][0]), int(arr["timestamp"][0]), 0, 0, False, mask, chans, waves)

    def lines(self) -> dict:
        return board_lines(self.cfg, self.board, header_board=self.board_header)

    def settings(self) -> dict:
        b = self.board_header
        return {"source": "file", "run": self.header.get("run"), "run_dir": self.header.get("run_folder"),
                "board": b.get("name"), "model": b.get("model"), "serial": b.get("serial"), "note": self.note,
                "setup": self.header.get("trigger_setup"), "sync": self.header.get("sync"),
                "simulator": bool(self.header.get("simulator")), "created": self.header.get("created")}

    def close(self) -> None:
        if self.kind == "raw":
            self.raw.close()
        else:
            self.root.close()


class DataFiles:
    def __init__(self, roots, cache_size: int = 16):
        """roots() returns the data folders [(label, path)]."""
        self.roots = roots
        self._open: OrderedDict[str, OpenFile] = OrderedDict()
        self._cache_size = cache_size
        self._lock = threading.Lock()

    def check(self, path: str, want_file: bool = True) -> str:
        """The real path, if it lies inside a data folder; FileAccessError otherwise."""
        if not path:
            raise FileAccessError("no path given")
        real = os.path.realpath(path)
        for _, root in self.roots():
            root = os.path.realpath(root)
            if real == root or real.startswith(root.rstrip("/") + "/"):
                break
        else:
            raise FileAccessError(f"{path} is not inside a data folder")
        if want_file and not real.lower().endswith(DATA_SUFFIXES):
            raise FileAccessError(f"{path} is not a .raw or .root file")
        return real

    def listing(self, folder: str) -> dict:
        folder = self.check(folder, want_file=False)
        if not os.path.isdir(folder):
            raise FileAccessError(f"{folder} is not a folder")
        entries = []
        with os.scandir(folder) as it:
            for e in it:
                if e.name.startswith("."):
                    continue
                try:
                    st = e.stat()
                except OSError:
                    continue
                if e.is_dir():
                    entries.append({"name": e.name, "kind": "folder", "mtime": st.st_mtime})
                elif e.name.lower().endswith(DATA_SUFFIXES):
                    entries.append({"name": e.name, "kind": e.name.rsplit(".", 1)[1].lower(), "size": st.st_size,
                                    "mtime": st.st_mtime})
        entries.sort(key=lambda x: (x["kind"] != "folder", -x["mtime"] if x["kind"] != "folder" else 0, x["name"]))
        parent = os.path.dirname(folder)
        try:
            self.check(parent, want_file=False)
        except FileAccessError:
            parent = None
        return {"folder": folder, "parent": parent, "entries": entries}

    def open(self, path: str) -> OpenFile:
        """The file, kept open for the next requests (the 4 newest files); read again when it changed."""
        path = self.check(path)
        st = os.stat(path)
        with self._lock:
            f = self._open.get(path)
            if f is not None and f.stamp == (st.st_mtime_ns, st.st_size):
                self._open.move_to_end(path)
                return f
        f = OpenFile(path)
        with self._lock:
            old = self._open.pop(path, None)
            if old is not None:
                old.close()
            self._open[path] = f
            while len(self._open) > self._cache_size:
                self._open.popitem(last=False)[1].close()
        return f

    def info(self, path: str) -> dict:
        f = self.open(path)
        lines = f.lines()
        return {"path": f.path, "kind": f.kind, "events": f.n, "size": f.stamp[1], "truncated": f.truncated,
                "channels": len(lines["lines"]), "settings": f.settings(), "enabled": f.enabled(),
                **{k: lines[k] for k in ("record_length", "pre_trigger", "sample_ns", "adc_max", "model")}}

    def waveforms(self, path: str, k: int, channels: list[int], x0: int, x1: int | None, bins: int,
                  span: tuple[int, int] | None = None, full: bool = False) -> dict:
        f = self.open(path)
        ev = f.event(k) if f.n else None
        info = f.lines()
        out = []
        for c in channels:
            if not 0 <= c < len(info["lines"]):
                continue
            enabled = f.board.channels[c].enabled if f.board and c < len(f.board.channels) else \
                bool(ev is not None and c in ev.channels)
            samples = ev.waveform(c) if ev is not None else None
            out.append(channel_view(samples, enabled, c, k, info["record_length"], x0, x1, bins, info["lines"][c],
                                    span, info["sample_ns"], full))
        return {"source": "file", "path": f.path, "event": k, "events": f.n, "board": f.board_header.get("name"),
                "trigger_id": ev.trigger_id if ev is not None else None,
                "timestamp": ev.timestamp if ev is not None else None,
                "settings": {**f.settings(), **{x: info[x] for x in ("record_length", "pre_trigger", "sample_ns",
                                                                     "adc_max", "model")}, "mv_per_adc": mv_per_adc(info)},
                "channels": out, "n_channels": len(info["lines"])}

    def hk(self, path: str) -> dict:
        """The housekeeping of the board of this data file (its RUN_BOARD_hk.csv beside it)."""
        f = self.open(path)
        hk = rootconv.hk_path(f.path, f.header)
        out = {"board": f.board_header.get("name"), "model": f.board_header.get("model"), "run": f.header.get("run")}
        if not os.path.isfile(hk):
            return {**out, "path": hk, "rows": 0, "columns": {}, "missing": True}
        return {**out, **hk_table(hk)}

    def export_text(self, path: str, first: int, count: int, channels: list[int]) -> tuple[str, str]:
        """Events first … first+count-1 of a data file as text (WaveDump2's ASCII file): per event the header
        lines, then one row per sample with a column per channel; (text, file name)."""
        f = self.open(path)
        info = f.lines()
        last = min(f.n, first + count)
        if first >= f.n:
            raise IndexError(f"event {first} of {f.n}")
        out = [f"# Vx27webDAQ text export of {os.path.basename(f.path)}: board {f.board_header.get('name')}"
               f" ({f.board_header.get('model')}), run {f.header.get('run')}, events {first} to {last - 1}",
               "# per event: Event number, Timestamp (8 ns ticks), Samples, Sampling period (us), Channels;"
               " then one row per sample, one column per channel (ADC counts)"]
        total = 0
        for k in range(first, last):
            ev = f.event(k)
            chans = [c for c in (channels or ev.channels) if c in ev.channels]
            waves = [ev.waveform(c) for c in chans]
            n = len(waves[0]) if waves else 0
            total += n * len(chans)
            if total > 20_000_000:
                raise ValueError("too many samples for one text file: ask for fewer events or channels")
            out += [f"Event number: {k}", f"Timestamp: {ev.timestamp}", f"Samples: {n}",
                    f"Sampling period: {info['sample_ns'] / 1000:g}", "Channels: " + " ".join(str(c) for c in chans)]
            if waves:
                block = np.stack(waves, axis=1)
                out += ["\t".join(str(int(v)) for v in row) for row in block]
        base = os.path.splitext(os.path.basename(f.path))[0]
        return "\n".join(out) + "\n", f"{base}_events{first}-{last - 1}.txt"

    def run_files(self, f: OpenFile) -> dict[str, list[OpenFile]]:
        """The files of the same run as f in its folder, one board each (in parts: _001, ...): {board: [files]}."""
        run, folder, suffix = f.header.get("run"), os.path.dirname(f.path), "." + f.kind
        out: dict[str, list] = {}
        if not run:
            return {f.board_header.get("name") or "?": [f]}
        for name in sorted(os.listdir(folder)):
            if not (name.startswith(f"{run}_") and name.lower().endswith(suffix)):
                continue
            try:
                g = f if os.path.join(folder, name) == f.path else self.open(os.path.join(folder, name))
            except (OSError, rawdata.RawFormatError, FileAccessError):
                continue
            if g.header.get("run") != run:
                continue
            out.setdefault(g.board_header.get("name") or "?", []).append(g)
        for files in out.values():
            files.sort(key=lambda g: int(g.header.get("file_index") or 0))
        return out

    def combined(self, path: str, k: int, traces: list[tuple[str, int]], t_lo: float | None, t_hi: float | None,
                 bins: int, yunit: str, align: str) -> dict:
        """The combined view of a run's files: event k of the file given, and the event with its trigger ID in
        the files of the run's other boards."""
        f = self.open(path)
        ev = f.event(k) if f.n else None
        own = f.board_header.get("name") or "?"
        by_board = self.run_files(f)
        entries, missing = [], []
        for name in dict.fromkeys(b for b, _ in traces):
            files = by_board.get(name)
            if not files:
                continue
            if name == own:
                e = ev
            else:
                e = None
                for g in files if ev is not None else []:
                    j = g.find_trigger(ev.trigger_id)
                    if j is not None:
                        e = g.event(j)
                        break
                if e is None:
                    missing.append(name)
            g = files[0]
            entries.append({"name": name, "info": g.lines(), "ev": e, "enabled": g.enabled()})
        out = combined_view(entries, traces, t_lo, t_hi, bins, yunit, align)
        out.update(source="file", path=f.path, event=k, events=f.n, board=own, missing=missing, run=f.header.get("run"),
                   matched=not missing, available=[{"name": n, "channels": len(fs[0].lines()["lines"]),
                                                    "enabled": fs[0].enabled(), "model": fs[0].board_header.get("model")}
                                                   for n, fs in by_board.items()])
        return out

    def overview(self, path: str) -> dict:
        """Per channel baseline, noise and peak, from up to SUMMARY_EVENTS events spread over the file."""
        f = self.open(path)
        picks = sorted(set(np.linspace(0, f.n - 1, min(f.n, SUMMARY_EVENTS)).astype(int).tolist())) if f.n else []
        events = [f.event(k) for k in picks]
        n_ch = len(f.lines()["lines"])
        return {"source": "file", "path": f.path, "boards": [{"name": f.board_header.get("name") or "?",
                                                             "model": f.board_header.get("model"), "channels": n_ch,
                                                             "summary": event_summary(events, n_ch)}],
                "events": f.n, "settings": f.settings()}

    def close_all(self) -> None:
        with self._lock:
            for f in self._open.values():
                f.close()
            self._open.clear()
