"""The boards of one run in one ROOT file, trigger by trigger (WaveDump2's "multi-board single file"):

    python -m vx27webdaq.core.eventbuild ANY_RAW_FILE_OF_THE_RUN [OUT.root] [--window NS]

OUT: RUN_built.root beside the run's data files. The run's files are found by its name in the same folder
(RUN_BOARD.raw and the parts RUN_BOARD_001.raw, ..., one board each, by their headers). The first board of the
chain is the reference: one entry per event of it, and for every other board the event whose TIMESTAMP is nearest
to the reference's, when it is within the window (default: the run's sync window, else 1000 ns). The boards of a
clock chain count one clock, so this is right for a common trigger (also when a board missed a trigger: its later
trigger IDs are off by one, its timestamps are not) and for boards that trigger by themselves (coincidences).
An event of a board goes into one entry at most (the nearest reference event's). Where a board's timestamps restart
(a timestamp reset on S-IN or GPIO) its events are matched within each stretch, in order. A run whose boards ran on
their own clocks (sync "independent") has no common time: it is not built (exit code 3).
TTree "events":
    trigger_id, timestamp, time_ns       of the reference event (UInt_t, ULong64_t: 8 ns since the run start)
    n_boards                             how many boards have this trigger (UChar_t)
    complete                             every board has it within the window (Bool_t)
    BOARD_found, BOARD_timestamp, BOARD_dt_ns, BOARD_trigger_id      per board: found, its timestamp, minus the
                                         reference's in ns, its own trigger ID (all 0 when not found)
    BOARD_chN                            per board and channel read out: the samples (UShort_t[n], zeros when
                                         the board does not have the trigger)
(BOARD: the board's name with anything but letters, digits and _ made _.) With it a TObjString "settings": the
run, the window, the reference, "match": "timestamp", per board its files, model, sample time, record length and
pre-trigger, the events found, and "ids_differ": how many found events have a trigger ID other than the
reference's (the 24-bit IDs made continuous; 0 with a common trigger where no board lost one).
It prints "processed N %", then "check: N triggers of REF, M entries"; exit code 0 when they agree, 1 when they
differ, 2 when the files cannot be read or the building fails, 3 when the run cannot be built (independent boards).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

import numpy as np

from vx27webdaq import __version__
from vx27webdaq.core import rawdata
from vx27webdaq.core.config import branch_name

# the waveforms of one batch of entries (every board's) stay below this: a record of 64 channels of 40960 samples
# is 5 MB per board, so a count of events (it was 500) could take gigabytes
BATCH_BYTES = 64 << 20
TS_BITS = 48                    # the timestamp of an event: 48 bits of 8 ns


class NotMatchable(Exception):
    """The boards of the run have no common time (independent boards): their events cannot be matched."""


def safe(name: str) -> str:
    return branch_name(name)


def built_name(raw_path: str) -> str:
    """RUN_built.root beside the run's files; the run's name comes from the header, so it must be one that can be
    part of a file name (a run named "../x" cannot put the file anywhere else)."""
    header, _ = rawdata.read_header(raw_path)
    run = rawdata.name_part(header.get("run"))
    if not run:
        raise rawdata.RawFormatError(f"{os.path.basename(raw_path)}: its header names no run (or one that cannot be "
                                     "part of a file name)")
    return os.path.join(os.path.dirname(os.path.abspath(raw_path)), f"{run}_built.root")


def run_files(raw_path: str) -> tuple[str, list[dict]]:
    """(run name, boards in chain order: {name, header, paths}) of the run of this file."""
    header, _ = rawdata.read_header(raw_path)
    run = header.get("run")
    folder = os.path.dirname(os.path.abspath(raw_path))
    if not rawdata.name_part(run):
        raise rawdata.RawFormatError(f"{raw_path}: its header names no run (or one that cannot be part of a file name)")
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


def matchable(entries: list[dict]) -> str:
    """Why the events of these boards cannot be matched ("" when they can): boards on their own clocks."""
    if len(entries) < 2:
        return ""
    h = entries[0]["header"]
    sync = h.get("sync") or ((h.get("config") or {}).get("run") or {}).get("sync") or "chain"
    if sync == "independent":
        return ("its boards ran on their own clocks (synchronization: independent boards), so their timestamps have no "
                "common time and their events cannot be matched; with the clock chain they can")
    return ""


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
        # the timestamps only increase, except where they restart (a timestamp reset, the 48-bit wrap after 26
        # days): each stretch gets a place of its own above the one before, so the key always increases and the
        # boards' events are matched stretch by stretch
        stretch = np.concatenate(([0], np.cumsum(np.diff(self.ts) < 0))) if len(self.ts) else np.zeros(0, np.int64)
        self.key = self.ts + (stretch.astype(np.int64) << TS_BITS)
        self.restarts = int(stretch[-1]) if len(stretch) else 0
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

    def event(self, k: int) -> rawdata.ScopeEvent:
        j, e = self.where[k]
        return self.files[int(j)].event(int(e))

    def close(self) -> None:
        for f in self.files:
            f.close()


def nearest(key: np.ndarray, ref: np.ndarray, window: int, chunk: int = 1 << 20) -> np.ndarray:
    """For each reference key (they only increase): the number of the board's event with the nearest key, when it
    is at most window away, else -1. Every event of the board goes to one reference event at most, the nearest
    (the first on a tie)."""
    out = np.full(len(ref), -1, np.int64)
    if not len(key) or not len(ref):
        return out
    last = len(key) - 1
    for a in range(0, len(ref), chunk):             # in chunks: the temporary arrays stay small for long runs
        q = ref[a:a + chunk]
        pos = np.searchsorted(key, q)               # the first event at or after the reference
        lo, hi = np.clip(pos - 1, 0, last), np.minimum(pos, last)
        d_lo, d_hi = np.abs(key[lo] - q), np.abs(key[hi] - q)
        k = np.where(d_hi < d_lo, hi, lo)
        out[a:a + len(q)] = np.where(np.minimum(d_lo, d_hi) <= window, k, -1)
    # two reference events that chose the same event of the board: since both keys only increase, the chosen numbers
    # only increase too, so they are neighbours among the found ones; the nearer reference event keeps it
    idx = np.flatnonzero(out >= 0)
    kk = out[idx]
    same = kk[1:] == kk[:-1]
    if same.any():
        involved = np.zeros(len(kk), bool)
        involved[1:] |= same
        involved[:-1] |= same
        sub = np.flatnonzero(involved)
        dist = np.abs(key[kk[sub]] - ref[idx[sub]])
        order = sub[np.lexsort((sub, dist, kk[sub]))]           # by the board's event, then distance, then order
        keep = np.ones(len(order), bool)
        keep[1:] = kk[order][1:] != kk[order][:-1]
        out[idx[order[~keep]]] = -1
    return out


def build(raw_path: str, out_path: str, window_ns: float | None = None, report=None) -> dict:
    import uproot
    report = report or (lambda percent: None)
    run, entries = run_files(raw_path)
    if not entries:
        raise rawdata.RawFormatError(f"no data files of run {run} beside {raw_path}")
    why = matchable(entries)
    if why:
        raise NotMatchable(f"run {run}: {why}")
    if window_ns is None:
        cfg = entries[0]["header"].get("config") or {}
        window_ns = float(((cfg.get("run") or {}).get("sync_window_ns")) or 1000)
    window = max(1, int(round(window_ns / rawdata.TIMESTAMP_NS)))
    boards = [BoardEvents(e) for e in entries]
    try:
        ref = boards[0]
        n = len(ref)
        match = {b.name: np.arange(n, dtype=np.int64) if b is ref else nearest(b.key, ref.key, window) for b in boards}
        found_total, ids_differ = {}, {}
        for b in boards:
            k = match[b.name]
            hit = k >= 0
            found_total[b.name] = int(hit.sum())
            ids_differ[b.name] = int((b.ids[k[hit]] != ref.ids[hit]).sum()) if len(b) else 0
        per_entry = sum(2 * len(b.channels) * b.samples + 64 for b in boards)        # bytes of one entry's arrays
        batch = int(max(1, BATCH_BYTES // per_entry))
        types = {"trigger_id": np.uint32, "timestamp": np.uint64, "time_ns": np.uint64, "n_boards": np.uint8,
                 "complete": np.bool_}
        for b in boards:
            s = safe(b.name)
            types.update({f"{s}_found": np.bool_, f"{s}_timestamp": np.uint64, f"{s}_dt_ns": np.int64,
                          f"{s}_trigger_id": np.uint32})
            for c in b.channels:
                types[f"{s}_ch{c}"] = ("u2", (b.samples,))
        complete_total = 0
        with uproot.recreate(out_path) as out:
            tree = out.mktree("events", types, title=f"{run}: every board, one entry per trigger of {ref.name}")
            for a in range(0, n, batch):
                z = min(n, a + batch)
                m = z - a
                ts0, key0 = ref.ts[a:z], ref.key[a:z]
                data = {"trigger_id": (ref.ids[a:z] & 0xFFFFFF).astype(np.uint32), "timestamp": ts0.astype(np.uint64),
                        "time_ns": (ts0 * rawdata.TIMESTAMP_NS).astype(np.uint64)}
                count = np.zeros(m, np.uint8)
                for b in boards:
                    s = safe(b.name)
                    k = match[b.name][a:z]
                    found = k >= 0
                    kk = np.maximum(k, 0)
                    have = len(b) > 0
                    count += found.astype(np.uint8)
                    data[f"{s}_found"] = found
                    data[f"{s}_timestamp"] = np.where(found, b.ts[kk], 0).astype(np.uint64) if have else np.zeros(m, np.uint64)
                    data[f"{s}_dt_ns"] = (np.where(found, b.key[kk] - key0, 0) * rawdata.TIMESTAMP_NS).astype(np.int64) \
                        if have else np.zeros(m, np.int64)
                    data[f"{s}_trigger_id"] = np.where(found, b.ids[kk] & 0xFFFFFF, 0).astype(np.uint32) if have \
                        else np.zeros(m, np.uint32)
                    waves = {c: np.zeros((m, b.samples), np.uint16) for c in b.channels}
                    for i in np.nonzero(found)[0]:
                        ev = b.event(int(k[i]))
                        for c, w in zip(ev.channels, ev.waveforms):
                            if c in waves and len(w) == b.samples:
                                waves[c][i] = w
                        del ev
                    for c in b.channels:
                        data[f"{s}_ch{c}"] = waves[c]
                    del waves
                complete = count == len(boards)
                complete_total += int(complete.sum())
                data["n_boards"], data["complete"] = count, complete
                tree.extend(data)
                del data
                report(100.0 * z / max(1, n))
            settings = {"format": "alab-daq-built", "run": run, "match": "timestamp",
                        "window_ns": window * rawdata.TIMESTAMP_NS, "reference": ref.name, "daq_version": __version__,
                        "created": time.strftime("%Y-%m-%d %H:%M:%S"), "triggers": n, "complete": complete_total,
                        "found": found_total, "ids_differ": ids_differ,
                        "boards": [{"name": b.name, "branch": safe(b.name), "files": [os.path.basename(p) for p in b.paths],
                                    "events": len(b), "found": found_total[b.name], "ids_differ": ids_differ[b.name],
                                    "timestamp_restarts": b.restarts, "channels": b.channels, "samples": b.samples,
                                    "model": (b.header.get("board") or {}).get("model"),
                                    "sample_ns": (b.header.get("board") or {}).get("sample_ns"),
                                    "pre_trigger": (b.header.get("board") or {}).get("pre_trigger")} for b in boards]}
            out["settings"] = json.dumps(settings, indent=1)      # last: it holds the counts of the matching
    finally:
        for b in boards:
            b.close()
    return {"run": run, "reference": ref.name, "triggers": n, "complete": complete_total, "found": found_total,
            "ids_differ": ids_differ, "match": "timestamp", "window_ns": window * rawdata.TIMESTAMP_NS,
            "boards": [b.name for b in boards]}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m vx27webdaq.core.eventbuild", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("raw")
    ap.add_argument("out", nargs="?")
    ap.add_argument("--window", type=float, default=None, metavar="NS")
    args = ap.parse_args(argv)
    try:
        out = args.out or built_name(args.raw)
        s = build(args.raw, out, args.window, report=lambda p: print(f"processed {p:.1f} %", flush=True))
        import uproot
        with uproot.open(out) as r:
            entries = int(r["events"].num_entries)
    except NotMatchable as err:
        print(f"skipped: {err}", flush=True)
        return 3
    except (OSError, rawdata.RawFormatError, ValueError, KeyError) as err:
        print(f"ERROR: {err}", flush=True)
        return 2
    except Exception as err:            # a crash (uproot, numpy, memory) is a failure too, never "built"
        print(f"ERROR: the building failed: {err!r}", flush=True)
        return 2
    differ = {k: v for k, v in s["ids_differ"].items() if v}
    print(f"built {os.path.basename(out)}: {s['triggers']} triggers of {s['reference']}, {s['complete']} on every board "
          f"within {s['window_ns']} ns (matched by timestamp); found: " + ", ".join(f"{k} {v}" for k, v in s["found"].items())
          + ("; trigger IDs differ from the reference's in " + ", ".join(f"{k} {v}" for k, v in differ.items())
             if differ else ""), flush=True)
    print(f"check: {s['triggers']} triggers of {s['reference']}, {entries} entries", flush=True)
    return 0 if entries == s["triggers"] else 1


if __name__ == "__main__":
    sys.exit(main())
