"""The boards of one run in one ROOT file, trigger by trigger (WaveDump2's "multi-board single file"):

    python -m vx27webdaq.core.eventbuild ANY_RAW_FILE_OF_THE_RUN [OUT.root] [--window NS]

OUT: RUN_built.root beside the run's data files. The run's files are found by its name in the same folder
(RUN_BOARD.raw and the parts RUN_BOARD_001.raw, ..., one board each, by their headers). The first board of the
chain is the reference: one entry per event of it, and for every board the event with the same trigger ID (the
24-bit IDs made continuous) whose timestamp is within the window of the reference's (default: the run's
sync window, else 1000 ns). TTree "events":
    trigger_id, timestamp, time_ns       of the reference event (UInt_t, ULong64_t: 8 ns since the run start)
    n_boards                             how many boards have this trigger (UChar_t)
    complete                             every board has it within the window (Bool_t)
    BOARD_found, BOARD_timestamp, BOARD_dt_ns     per board: found, its timestamp, minus the reference's in ns
    BOARD_chN                            per board and channel read out: the samples (UShort_t[n], zeros when
                                         the board does not have the trigger)
(BOARD: the board's name with anything but letters, digits and _ made _.) With it a TObjString "settings": the
run, the window, the reference, and per board its files, model, sample time, record length and pre-trigger.
It prints "processed N %", then "check: N triggers of REF, M entries"; exit code 0 when they agree, 2 when the
files cannot be read.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time

import numpy as np

from vx27webdaq import __version__
from vx27webdaq.core import rawdata

BATCH = 500


def safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "_", name)


def built_name(raw_path: str) -> str:
    header, _ = rawdata.read_header(raw_path)
    return os.path.join(os.path.dirname(os.path.abspath(raw_path)), f"{header.get('run', 'run')}_built.root")


def run_files(raw_path: str) -> tuple[str, list[dict]]:
    """(run name, boards in chain order: {name, header, paths}) of the run of this file."""
    header, _ = rawdata.read_header(raw_path)
    run = header.get("run")
    folder = os.path.dirname(os.path.abspath(raw_path))
    if not run:
        raise rawdata.RawFormatError(f"{raw_path}: its header names no run")
    boards: dict[str, dict] = {}
    for name in sorted(os.listdir(folder)):
        if not (name.startswith(f"{run}_") and name.endswith(".raw")):
            continue
        path = os.path.join(folder, name)
        try:
            h, _ = rawdata.read_header(path)
        except (OSError, rawdata.RawFormatError):
            continue
        if h.get("run") != run:
            continue
        b = h.get("board") or {}
        entry = boards.setdefault(b.get("name") or "?", {"name": b.get("name") or "?", "header": h, "paths": []})
        entry["paths"].append((int(h.get("file_index") or 0), path))
        if int(h.get("file_index") or 0) == 0:
            entry["header"] = h
    out = sorted(boards.values(), key=lambda e: int((e["header"].get("board") or {}).get("index") or 0))
    for e in out:
        e["paths"] = [p for _, p in sorted(e["paths"])]
    return run, out


class BoardEvents:
    """All events of one board (over its files): continuous trigger IDs, timestamps, and each event's file."""

    def __init__(self, entry: dict):
        self.name, self.header, self.paths = entry["name"], entry["header"], entry["paths"]
        self.files = [rawdata.RawFile(p) for p in self.paths]
        ids, ts, where = [], [], []
        base, last = 0, None
        for j, f in enumerate(self.files):
            hd = f.headers()
            raw = hd["trigger_id"].astype(np.int64)
            if len(raw):
                prev = raw[0] if last is None else last
                wraps = np.cumsum(np.diff(np.concatenate(([prev], raw))) < -(1 << 23))
                ids.append(raw + ((base + wraps) << 24))
                base += int(wraps[-1])
                last = int(raw[-1])
                ts.append(hd["timestamp"].astype(np.int64))
                where.append(np.stack([np.full(len(raw), j), np.arange(len(raw))], axis=1))
        self.ids = np.concatenate(ids) if ids else np.zeros(0, np.int64)
        self.ts = np.concatenate(ts) if ts else np.zeros(0, np.int64)
        self.where = np.concatenate(where) if where else np.zeros((0, 2), np.int64)
        self.order = np.argsort(self.ids, kind="stable")
        self.sorted_ids = self.ids[self.order]
        mask = 0
        for f in self.files:
            hd = f.headers()
            if len(hd["ch_mask"]):
                mask |= int(np.bitwise_or.reduce(hd["ch_mask"]))
        self.channels = rawdata.mask_channels(mask)
        first = self.event(0) if len(self.ids) else None
        self.samples = first.samples if first is not None else 0

    def __len__(self) -> int:
        return len(self.ids)

    def find(self, ids: np.ndarray) -> np.ndarray:
        """Event numbers of these trigger IDs (-1 where the board has none)."""
        pos = np.searchsorted(self.sorted_ids, ids)
        pos = np.minimum(pos, max(0, len(self.sorted_ids) - 1))
        hit = len(self.sorted_ids) > 0
        ok = (self.sorted_ids[pos] == ids) if hit else np.zeros(len(ids), bool)
        return np.where(ok, self.order[pos] if hit else -1, -1)

    def event(self, k: int) -> rawdata.ScopeEvent:
        j, e = self.where[k]
        return self.files[int(j)].event(int(e))

    def close(self) -> None:
        for f in self.files:
            f.close()


def build(raw_path: str, out_path: str, window_ns: float | None = None, report=None) -> dict:
    import uproot
    report = report or (lambda percent: None)
    run, entries = run_files(raw_path)
    if not entries:
        raise rawdata.RawFormatError(f"no data files of run {run} beside {raw_path}")
    if window_ns is None:
        cfg = entries[0]["header"].get("config") or {}
        window_ns = float(((cfg.get("run") or {}).get("sync_window_ns")) or 1000)
    window = max(1, int(round(window_ns / rawdata.TIMESTAMP_NS)))
    boards = [BoardEvents(e) for e in entries]
    try:
        ref = boards[0]
        n = len(ref)
        types = {"trigger_id": np.uint32, "timestamp": np.uint64, "time_ns": np.uint64, "n_boards": np.uint8,
                 "complete": np.bool_}
        for b in boards:
            s = safe(b.name)
            types.update({f"{s}_found": np.bool_, f"{s}_timestamp": np.uint64, f"{s}_dt_ns": np.int64})
            for c in b.channels:
                types[f"{s}_ch{c}"] = ("u2", (b.samples,))
        settings = {"format": "alab-daq-built", "run": run, "window_ns": window * rawdata.TIMESTAMP_NS,
                    "reference": ref.name, "daq_version": __version__, "created": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "boards": [{"name": b.name, "branch": safe(b.name), "files": [os.path.basename(p) for p in b.paths],
                                "events": len(b), "channels": b.channels, "samples": b.samples,
                                "model": (b.header.get("board") or {}).get("model"),
                                "sample_ns": (b.header.get("board") or {}).get("sample_ns"),
                                "pre_trigger": (b.header.get("board") or {}).get("pre_trigger")} for b in boards]}
        found_total = {b.name: 0 for b in boards}
        complete_total = 0
        with uproot.recreate(out_path) as out:
            out["settings"] = json.dumps(settings, indent=1)
            tree = out.mktree("events", types, title=f"{run}: every board, one entry per trigger of {ref.name}")
            for a in range(0, n, BATCH):
                z = min(n, a + BATCH)
                ids, ts0 = ref.ids[a:z], ref.ts[a:z]
                m = z - a
                data = {"trigger_id": (ids & 0xFFFFFF).astype(np.uint32), "timestamp": ts0.astype(np.uint64),
                        "time_ns": (ts0 * rawdata.TIMESTAMP_NS).astype(np.uint64)}
                count = np.zeros(m, np.uint8)
                for b in boards:
                    s = safe(b.name)
                    k = np.arange(a, z) if b is ref else b.find(ids)
                    ts = np.where(k >= 0, b.ts[np.maximum(k, 0)], 0) if len(b) else np.zeros(m, np.int64)
                    dt = (ts - ts0) * rawdata.TIMESTAMP_NS
                    found = (k >= 0) & (np.abs(ts - ts0) <= window)
                    count += found.astype(np.uint8)
                    found_total[b.name] += int(found.sum())
                    data[f"{s}_found"], data[f"{s}_timestamp"] = found, np.where(found, ts, 0).astype(np.uint64)
                    data[f"{s}_dt_ns"] = np.where(k >= 0, dt, 0).astype(np.int64)
                    waves = {c: np.zeros((m, b.samples), np.uint16) for c in b.channels}
                    for i in np.nonzero(found)[0]:
                        ev = b.event(int(k[i]))
                        for c, w in zip(ev.channels, ev.waveforms):
                            if c in waves and len(w) == b.samples:
                                waves[c][i] = w
                    for c in b.channels:
                        data[f"{s}_ch{c}"] = waves[c]
                complete = count == len(boards)
                complete_total += int(complete.sum())
                data["n_boards"], data["complete"] = count, complete
                tree.extend(data)
                report(100.0 * z / max(1, n))
    finally:
        for b in boards:
            b.close()
    return {"run": run, "reference": ref.name, "triggers": n, "complete": complete_total, "found": found_total,
            "window_ns": window * rawdata.TIMESTAMP_NS, "boards": [b.name for b in boards]}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m vx27webdaq.core.eventbuild", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("raw")
    ap.add_argument("out", nargs="?")
    ap.add_argument("--window", type=float, default=None, metavar="NS")
    args = ap.parse_args(argv)
    out = args.out or built_name(args.raw)
    try:
        s = build(args.raw, out, args.window, report=lambda p: print(f"processed {p:.1f} %", flush=True))
        import uproot
        with uproot.open(out) as r:
            entries = int(r["events"].num_entries)
    except (OSError, rawdata.RawFormatError, ValueError, KeyError) as err:
        print(f"ERROR: {err}", flush=True)
        return 2
    print(f"built {os.path.basename(out)}: {s['triggers']} triggers of {s['reference']}, {s['complete']} on every board "
          f"within {s['window_ns']} ns; found: " + ", ".join(f"{k} {v}" for k, v in s["found"].items()), flush=True)
    print(f"check: {s['triggers']} triggers of {s['reference']}, {entries} entries", flush=True)
    return 0 if entries == s["triggers"] else 1


if __name__ == "__main__":
    sys.exit(main())
