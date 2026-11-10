"""Data files for the page: browsing the data folders, and the waveforms of .raw and .root files.

Only files inside the data folders can be opened: the data folder of the configuration, the save folder of
the runs, and the folders added in the settings. The server reads them as the service account, so this keeps
users to the data and away from everything else that account can read.

A data file carries its own settings: the header of NAME.raw (and the "settings" string of NAME.root) holds the
board, every parameter as it was set, and the run's configuration; the note may have been edited during the
run, so the note of NAME.json next to the file counts when it is there.
"""

from __future__ import annotations

import io
import json
import math
import os
import re
import stat
import threading
from collections import OrderedDict
from contextlib import contextmanager

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
PART_NAME = re.compile(r"(?P<board>.+?)(?:_(?P<part>\d{3,}))?")     # BOARD or BOARD_001 of RUN_BOARD[_001].raw
RUN_NAME = re.compile(r"^run\d{5,}$")  # a run's name, as the DAQ program makes it (file names are made from it)
DEFAULT_SYNC_WINDOW_NS = 1000     # the combined view of files: the other boards' events within this time
EXPORT_MAX_SAMPLES = 20_000_000   # a text export holds at most this many samples


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
    return hk_columns(path, rootconv.read_hk(path), max_rows)


def hk_columns(path: str, cols: dict, max_rows: int = HK_ROWS) -> dict:
    """Housekeeping columns (from the CSV file or a ROOT file's hk tree) for the page, thinned to max_rows readings."""
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
        thr = None if ch is None else C.signed_threshold(ch)
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


def _clean_board(h) -> dict:
    """The "board" part of a data file's header with every value of the type the page expects (a file of another
    program, or a damaged one, may hold anything there)."""
    out: dict = {}
    if not isinstance(h, dict):
        return out
    for k, v in h.items():
        if k in ("record_length", "pre_trigger", "adc_bits", "channels", "index", "decimation"):
            if isinstance(v, int) and not isinstance(v, bool) and 0 <= v < 1 << 40:
                out[k] = v
        elif k in ("sample_ns", "adc_msps"):
            if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v > 0:
                out[k] = float(v)
        elif k in ("name", "model", "serial", "fw_type", "fw_version", "uri", "role", "cup"):
            out[k] = v if isinstance(v, str) else None
        elif isinstance(v, (str, int, float, bool)) or v is None:
            out[k] = v
    return out


class OpenFile:
    """A data file opened for the page: .raw (core.rawdata) or .root (uproot), with its header. users and retired
    belong to DataFiles: a file is closed only when it is out of the cache and no request uses it any more."""

    def __init__(self, path: str):
        st = os.stat(path)
        if not stat.S_ISREG(st.st_mode):
            raise FileAccessError(f"{os.path.basename(path)} is not a data file (a folder or a special file)")
        self.path, self.stamp = path, (st.st_mtime_ns, st.st_size)
        self.kind = path.rsplit(".", 1)[1].lower()
        self.lock = threading.Lock()
        self.users, self.retired, self.closed = 0, False, False
        self._ids = self._ts = None
        if self.kind == "raw":
            self.raw = rawdata.RawFile(path)
            try:
                self.header = self.raw.header
                if not isinstance(self.header, dict):
                    raise rawdata.RawFormatError(f"{os.path.basename(path)}: its header is not a JSON object")
                self.n = len(self.raw.index())
                self.truncated = self.raw.truncated
            except BaseException:
                self.raw.close()
                raise
        else:
            self._open_root(path)
        cfg = self.header.get("config")
        try:
            self.cfg = C.from_dict(cfg) if isinstance(cfg, dict) else None
        except Exception:                    # noqa: BLE001 - a configuration of another version or a damaged one
            self.cfg = None
        self.board_header = _clean_board(self.header.get("board"))
        name = self.board_header.get("name")
        self.board = self.cfg.board(name) if self.cfg and name else None
        note = _side_note(path) if self.kind == "raw" else _side_note(path[:-5] + ".raw")
        self.note = note if note is not None else str(self.header.get("note") or "")

    def _open_root(self, path: str) -> None:
        import uproot
        name = os.path.basename(path)
        try:
            self.root = uproot.open(path)
        except OSError:
            raise
        except Exception as err:            # noqa: BLE001 - uproot raises many kinds for files of other programs
            raise rawdata.RawFormatError(f"{name} is not a ROOT file of Vx27webDAQ ({err})") from None
        try:                                 # everything that reads the file in here: a failure closes it again
            self.tree = self.root["events"]
            header = json.loads(str(self.root["settings"])) if "settings" in self.root else {}
            if not isinstance(header, dict):
                raise rawdata.RawFormatError(f"{name}: its settings are not a JSON object")
            if header.get("format") == "alab-daq-built":
                raise rawdata.RawFormatError(f"{name} holds every board of a run per trigger: open it in ROOT (TTree "
                                             "events); the page shows one board's files")
            keys = set(self.tree.keys())
            lacking = [k for k in ("trigger_id", "timestamp", "ch_mask") if k not in keys]
            if lacking:
                raise rawdata.RawFormatError(f"{name} is not a ROOT file of Vx27webDAQ (no {', '.join(lacking)} branch)")
            self.header = header
            self.n = int(self.tree.num_entries)
            self.truncated = bool(header.get("truncated"))
            self.channels = sorted(int(k[2:]) for k in keys if k.startswith("ch") and k[2:].isdigit() and int(k[2:]) < 64)
        except rawdata.RawFormatError:
            self.root.close()
            raise
        except Exception as err:            # noqa: BLE001
            self.root.close()
            raise rawdata.RawFormatError(f"{name} is not a ROOT file of Vx27webDAQ ({err})") from None

    # ---- the run it belongs to ----
    def run_name(self) -> str | None:
        """The run's name from the header, only when it is a run's name (runNNNNN): it is used in file names."""
        run = self.header.get("run")
        return run if isinstance(run, str) and RUN_NAME.match(run) else None

    def file_index(self) -> int:
        k = self.header.get("file_index")
        return k if isinstance(k, int) and not isinstance(k, bool) and k >= 0 else 0

    def sync_window_ns(self) -> float:
        w = self.cfg.run.sync_window_ns if self.cfg else None
        return float(w) if isinstance(w, (int, float)) and w > 0 else float(DEFAULT_SYNC_WINDOW_NS)

    def same_clock(self) -> bool:
        """The boards of its run ran on one clock (a chain of several boards): their timestamps can be compared."""
        sync = self.cfg.run.sync if self.cfg else self.header.get("sync")
        return sync == "chain" and (self.cfg is None or len(self.cfg.active()) > 1)

    # ---- events ----
    def timestamps(self) -> tuple[np.ndarray, np.ndarray]:
        """(every event's timestamp in order, the event numbers in that order)."""
        with self.lock:
            if self._ts is None:
                try:
                    if self.kind == "raw":
                        ts = self.raw.headers()["timestamp"].astype(np.int64)
                    else:
                        ts = np.asarray(self.tree["timestamp"].array(library="np"), dtype=np.int64)
                except rawdata.RawFormatError:
                    raise
                except Exception as err:     # noqa: BLE001
                    raise rawdata.RawFormatError(f"{os.path.basename(self.path)}: the timestamps cannot be read ({err})") from None
                order = np.argsort(ts, kind="stable")
                self._ts = (ts[order], order)
        return self._ts

    def nearest(self, timestamp: int) -> tuple[int, int] | None:
        """(the number of the event nearest in time to this timestamp, how far it is in timestamp units), or None for
        a file without events."""
        ts, order = self.timestamps()
        if not len(ts):
            return None
        i = int(np.searchsorted(ts, timestamp))
        best = None
        for j in (i - 1, i):
            if 0 <= j < len(ts):
                d = abs(int(ts[j]) - int(timestamp))
                if best is None or d < best[1]:
                    best = (int(order[j]), d)
        return best

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
            if self.closed:
                raise rawdata.RawFormatError(f"{os.path.basename(self.path)} was closed: open it again")
            if self.kind == "raw":
                return self.raw.event(k)
            try:
                names = ["trigger_id", "timestamp", "ch_mask"] + [f"ch{c}" for c in self.channels]
                arr = self.tree.arrays(names, entry_start=k, entry_stop=k + 1, library="np")
                mask = int(arr["ch_mask"][0])
                chans = [c for c in self.channels if mask >> c & 1]
                waves = np.stack([np.asarray(arr[f"ch{c}"][0], dtype=np.uint16) for c in chans]) if chans \
                    else np.zeros((0, 0), np.uint16)
                return rawdata.ScopeEvent(int(arr["trigger_id"][0]), int(arr["timestamp"][0]), 0, 0, False, mask, chans, waves)
            except Exception as err:         # noqa: BLE001 - a damaged ROOT file
                raise rawdata.RawFormatError(f"{os.path.basename(self.path)}: event {k} cannot be read ({err})") from None

    def lines(self) -> dict:
        return board_lines(self.cfg, self.board, header_board=self.board_header)

    def settings(self) -> dict:
        b, h = self.board_header, self.header
        text = lambda key: h.get(key) if isinstance(h.get(key), str) else None      # noqa: E731
        return {"source": "file", "run": text("run"), "run_dir": text("run_folder"),
                "board": b.get("name"), "model": b.get("model"), "serial": b.get("serial"), "note": self.note,
                "setup": text("trigger_setup"), "sync": text("sync"),
                "simulator": bool(h.get("simulator")), "created": text("created")}

    def hk_columns(self) -> dict | None:
        """The ROOT file's hk tree (the board's housekeeping), or None."""
        if self.kind != "root":
            return None
        with self.lock:
            try:
                if "hk" not in self.root:
                    return None
                tree = self.root["hk"]
                return {k: np.asarray(tree[k].array(library="np"), dtype=np.float64) for k in tree.keys()}
            except Exception:                # noqa: BLE001
                return None

    def close(self) -> None:
        with self.lock:
            if self.closed:
                return
            self.closed = True
            if self.kind == "raw":
                self.raw.close()
            else:
                self.root.close()


class TextExport:
    """Events of a data file as text (WaveDump2's ASCII file), made one event at a time while it is sent: per event
    the header lines, then one row per sample with a column per channel."""

    def __init__(self, files: "DataFiles", f: OpenFile, first: int, last: int, channels: list[int]):
        self.files, self.f, self.first, self.last, self.channels = files, f, first, last, channels
        base = os.path.splitext(os.path.basename(f.path))[0]
        self.name = f"{base}_events{first}-{last - 1}.txt"
        self._released = False

    def chunks(self):
        f, info = self.f, self.f.lines()
        yield "\n".join([f"# Vx27webDAQ text export of {os.path.basename(f.path)}: board {f.board_header.get('name')}"
                         f" ({f.board_header.get('model')}), run {f.settings()['run']}, events {self.first} to {self.last - 1}",
                         "# per event: Event number, Timestamp (8 ns ticks), Samples, Sampling period (us), Channels;"
                         " then one row per sample, one column per channel (ADC counts)"]) + "\n"
        total = 0
        for k in range(self.first, self.last):
            ev = f.event(k)
            chans = [c for c in (self.channels or ev.channels) if c in ev.channels]
            waves = [ev.waveform(c) for c in chans]
            n = len(waves[0]) if waves else 0
            total += n * len(chans)
            if total > EXPORT_MAX_SAMPLES:
                yield "# stopped here: too many samples for one text file (ask for fewer events or channels)\n"
                return
            head = [f"Event number: {k}", f"Timestamp: {ev.timestamp}", f"Samples: {n}",
                    f"Sampling period: {info['sample_ns'] / 1000:g}", "Channels: " + " ".join(str(c) for c in chans)]
            buf = io.StringIO()
            buf.write("\n".join(head) + "\n")
            if waves:
                np.savetxt(buf, np.stack(waves, axis=1).astype(np.int64), fmt="%d", delimiter="\t")
            yield buf.getvalue()

    def close(self) -> None:
        if not self._released:
            self._released = True
            self.files.release(self.f)


class DataFiles:
    def __init__(self, roots, cache_size: int = 16):
        """roots() returns the data folders [(label, path)]."""
        self.roots = roots
        self._open: OrderedDict[str, OpenFile] = OrderedDict()
        self._cache_size = cache_size
        self._lock = threading.Lock()
        self._opening: dict[str, threading.Lock] = {}

    def check(self, path: str, want: str = "data") -> str:
        """The real path, if it lies inside a data folder and is what is wanted: "data" a .raw or .root file, "file"
        any regular file, "folder" a folder. A path outside the data folders: FileAccessError; not there:
        FileNotFoundError."""
        if not isinstance(path, str) or not path:
            raise ValueError("no path given: choose a data file (or folder)")
        real = os.path.realpath(path)
        for _, root in self.roots():
            root = os.path.realpath(root)
            if real == root or real.startswith(root.rstrip("/") + "/"):
                break
        else:
            raise FileAccessError(f"{path} is not inside a data folder")
        if want == "data" and not real.lower().endswith(DATA_SUFFIXES):
            raise FileAccessError(f"{path} is not a .raw or .root file")
        if want == "new":                   # a file about to be written (it need not be there)
            return real
        st = os.stat(real)                   # never opened when it is not a regular file: a FIFO would block for ever
        if want == "folder" and not stat.S_ISDIR(st.st_mode):
            raise FileAccessError(f"{path} is not a folder")
        if want != "folder" and not stat.S_ISREG(st.st_mode):
            raise FileAccessError(f"{path} is not a data file (a folder or a special file)")
        return real

    def listing(self, folder: str) -> dict:
        """The folders and the data files of a data folder (only folders and regular files), the newest files first,
        at most BROWSE_LIMIT entries ("more": true when there were more)."""
        folder = self.check(folder, want="folder")
        entries = []
        with os.scandir(folder) as it:
            for e in it:
                if e.name.startswith("."):
                    continue
                try:
                    st = e.stat()
                except OSError:
                    continue
                if stat.S_ISDIR(st.st_mode):
                    entries.append({"name": e.name, "kind": "folder", "mtime": st.st_mtime})
                elif stat.S_ISREG(st.st_mode) and e.name.lower().endswith(DATA_SUFFIXES):
                    entries.append({"name": e.name, "kind": e.name.rsplit(".", 1)[1].lower(), "size": st.st_size,
                                    "mtime": st.st_mtime})
        entries.sort(key=lambda x: (x["kind"] != "folder", -x["mtime"] if x["kind"] != "folder" else 0, x["name"]))
        parent = os.path.dirname(folder)
        try:
            self.check(parent, want="folder")
        except (FileAccessError, OSError):
            parent = None
        return {"folder": folder, "parent": parent, "entries": entries[:BROWSE_LIMIT],
                "more": len(entries) > BROWSE_LIMIT, "more_count": max(0, len(entries) - BROWSE_LIMIT)}

    # ---- the files kept open ----
    def _acquire(self, path: str) -> OpenFile:
        """The file, opened once and kept for the next requests (read again when it changed); release() it after use
        (use() does). One request opens (and indexes) a file at a time; the others wait for it."""
        path = self.check(path)
        st = os.stat(path)
        stamp = (st.st_mtime_ns, st.st_size)
        with self._lock:
            f = self._open.get(path)
            if f is not None and f.stamp == stamp:
                f.users += 1
                self._open.move_to_end(path)
                return f
            opening = self._opening.setdefault(path, threading.Lock())
        with opening:
            with self._lock:
                f = self._open.get(path)
                if f is not None and f.stamp == stamp:
                    f.users += 1
                    self._open.move_to_end(path)
                    return f
            new = OpenFile(path)
            with self._lock:
                old = self._open.pop(path, None)
                if old is not None:
                    self._retire(old)
                new.users = 1
                self._open[path] = new
                self._trim()
                self._opening.pop(path, None)
                return new

    def _retire(self, f: OpenFile) -> None:
        f.retired = True
        if f.users == 0:
            f.close()

    def _trim(self) -> None:
        """Close the files used longest ago while the cache holds more than cache_size; a file in use stays (open
        and in the cache) until its request is done, so one request may use more files than the cache holds."""
        unused = [p for p, f in self._open.items() if f.users == 0]
        while len(self._open) > self._cache_size and unused:
            self._retire(self._open.pop(unused.pop(0)))

    def release(self, f: OpenFile) -> None:
        with self._lock:
            f.users -= 1
            if f.users <= 0:
                f.users = 0
                if f.retired:
                    f.close()
                else:
                    self._trim()

    @contextmanager
    def use(self, path: str):
        f = self._acquire(path)
        try:
            yield f
        finally:
            self.release(f)

    # ---- for the page ----
    def info(self, path: str) -> dict:
        with self.use(path) as f:
            lines = f.lines()
            return {"path": f.path, "kind": f.kind, "events": f.n, "size": f.stamp[1], "truncated": f.truncated,
                    "channels": len(lines["lines"]), "settings": f.settings(), "enabled": f.enabled(),
                    "run_files": self.run_boards(f),
                    **{k: lines[k] for k in ("record_length", "pre_trigger", "sample_ns", "adc_max", "model")}}

    def run_boards(self, f: OpenFile) -> list[dict]:
        """Every board's data file of the same run as f, in its folder: [{"board", "path"}] in the run's order (the
        master first), each board's part of the same number as f (else its first), f itself for f's board. Found by
        the file names alone (RUN_BOARD.raw, RUN_BOARD_001.raw, ...; not RUN_built.root), so nothing is opened, and
        no memory is used, until a board is chosen."""
        run, folder, suffix, own = f.run_name(), os.path.dirname(f.path), "." + f.kind, f.board_header.get("name")
        if not run:
            return []
        try:
            names = os.listdir(folder)
        except OSError:
            names = []
        parts: dict[str, dict[int, str]] = {}
        for name in names:
            if not (name.startswith(f"{run}_") and name.lower().endswith(suffix)):
                continue
            m = PART_NAME.fullmatch(name[len(run) + 1:len(name) - len(suffix)])
            if m is None or m["board"] == "built" or not rawdata.name_part(m["board"]):
                continue
            parts.setdefault(m["board"], {})[int(m["part"] or 0)] = os.path.join(folder, name)
        k = f.file_index()
        out = {b: f.path if b == own else ps.get(k, ps[min(ps)]) for b, ps in parts.items()}
        if isinstance(own, str) and own:
            out[own] = f.path
        order = [b.name for b in f.cfg.boards] if f.cfg else []
        return [{"board": b, "path": p} for b, p in sorted(out.items(), key=lambda x: (order.index(x[0]) if x[0] in order
                                                                                    else len(order), x[0]))]

    def waveforms(self, path: str, k: int, channels: list[int], x0: int, x1: int | None, bins: int,
                  span: tuple[int, int] | None = None, full: bool = False) -> dict:
        with self.use(path) as f:
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
        """The housekeeping of the board of this data file: its RUN_BOARD_hk.csv beside it, else the hk tree of its
        ROOT file (only the ROOT files were copied). The file names come from the header, so only a run's name
        (runNNNNN) and a board's name are used, and the files must lie inside a data folder too."""
        with self.use(path) as f:
            out = {"board": f.board_header.get("name"), "model": f.board_header.get("model"), "run": f.settings()["run"]}
            run, board = f.run_name(), f.board_header.get("name")
            folder = os.path.dirname(f.path)
            csv = os.path.join(folder, f"{run}_{board}_hk.csv") if run and board and C.NAME_RE.match(board) else ""
            if csv:
                try:
                    return {**out, **hk_table(self.check(csv, want="file"))}
                except (OSError, ValueError):     # not there (or not a data folder's file): the ROOT file's copy
                    pass
            cols, source = f.hk_columns(), f.path
            roots = [p for p in (f.path[:-4] + ".root" if f.kind == "raw" else "",
                                 os.path.join(folder, f"{run}_{board}.root") if csv else "") if p and p != f.path]
            for root in roots if cols is None else []:
                try:
                    with self.use(root) as g:
                        cols, source = g.hk_columns(), g.path
                except (OSError, ValueError):
                    continue
                if cols is not None:
                    break
            if cols:
                return {**out, **hk_columns(source, cols), "from_root": True}
            return {**out, "path": csv, "rows": 0, "columns": {}, "missing": True}

    def export_open(self, path: str, first: int, count: int, channels: list[int]) -> TextExport:
        """Events first … first+count-1 of a data file as text, made while it is sent (TextExport.chunks()); close()
        the export when it is done."""
        f = self._acquire(path)
        try:
            if first >= f.n:
                raise IndexError(f"event {first} of {f.n}")
            last = min(f.n, first + count)
            ev = f.event(first)
            chans = [c for c in (channels or ev.channels) if c in ev.channels]
            n = len(ev.waveform(chans[0])) if chans else 0
            if n * len(chans) * (last - first) > EXPORT_MAX_SAMPLES:
                raise ValueError("too many samples for one text file: ask for fewer events or channels")
            return TextExport(self, f, first, last, channels)
        except BaseException:
            self.release(f)
            raise

    def _run_files(self, f: OpenFile) -> tuple[dict[str, list[OpenFile]], list[OpenFile]]:
        """The files of the same run as f in its folder, one board each (in parts: _001, ...): ({board: [files]},
        the files held for this request, to release())."""
        run, folder, suffix = f.run_name(), os.path.dirname(f.path), "." + f.kind
        out: dict[str, list] = {}
        held: list[OpenFile] = []
        if not run:
            return {f.board_header.get("name") or "?": [f]}, held
        for name in sorted(os.listdir(folder)):
            if not (name.startswith(f"{run}_") and name.lower().endswith(suffix)):
                continue
            path = os.path.join(folder, name)
            if path == f.path:
                g = f
            else:
                try:
                    g = self._acquire(path)
                except (OSError, ValueError):
                    continue
                held.append(g)
            if g.run_name() != run:
                continue
            out.setdefault(g.board_header.get("name") or "?", []).append(g)
        for files in out.values():
            files.sort(key=lambda g: g.file_index())
        return out, held

    def combined(self, path: str, k: int, traces: list[tuple[str, int]], t_lo: float | None, t_hi: float | None,
                 bins: int, yunit: str, align: str) -> dict:
        """The combined view of a run's files: event k of the file given, and for every other board of the run the
        event nearest in time (the boards share one clock in a chain), when it is within the run's sync window
        (1000 ns when the file does not say): like the event builder. A board without one is "not matched"; so is
        every other board of a run whose boards had clocks of their own."""
        with self.use(path) as f:
            ev = f.event(k) if f.n else None
            own = f.board_header.get("name") or "?"
            by_board, held = self._run_files(f)
            try:
                window_ns, same_clock = f.sync_window_ns(), f.same_clock()
                entries, missing = [], []
                for name in dict.fromkeys(b for b, _ in traces):
                    files = by_board.get(name)
                    if not files:
                        continue
                    if name == own:
                        e = ev
                    else:
                        e, best = None, None
                        for g in files if ev is not None and same_clock else []:
                            hit = g.nearest(ev.timestamp)
                            if hit is not None and (best is None or hit[1] < best[2]):
                                best = (g, hit[0], hit[1])
                        if best is not None and best[2] * rawdata.TIMESTAMP_NS <= window_ns:
                            e = best[0].event(best[1])
                        if e is None:
                            missing.append(name)
                    g = files[0]
                    entries.append({"name": name, "info": g.lines(), "ev": e, "enabled": g.enabled()})
                out = combined_view(entries, traces, t_lo, t_hi, bins, yunit, align)
                out.update(source="file", path=f.path, event=k, events=f.n, board=own, missing=missing,
                           run=f.settings()["run"], matched=not missing, window_ns=window_ns, same_clock=same_clock,
                           available=[{"name": n, "channels": len(fs[0].lines()["lines"]), "enabled": fs[0].enabled(),
                                       "model": fs[0].board_header.get("model")} for n, fs in by_board.items()])
                return out
            finally:
                for g in held:
                    self.release(g)

    def overview(self, path: str) -> dict:
        """Per channel baseline, noise and peak, from up to SUMMARY_EVENTS events spread over the file."""
        with self.use(path) as f:
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
                self._retire(f)
            self._open.clear()
