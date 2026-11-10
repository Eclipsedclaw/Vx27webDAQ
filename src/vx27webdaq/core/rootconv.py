"""A board's data file NAME.raw as a ROOT file: one TTree "events", one entry per trigger.

    python -m vx27webdaq.core.rootconv NAME.raw [OUT.root]        (OUT: NAME.root next to it)

Branches of "events":
    trigger_id          UInt_t      the board's trigger counter (24 bits)
    timestamp           ULong64_t   in 8 ns since the start of the run (the same clock on every board of a chain)
    time_ns             ULong64_t   timestamp * 8
    flags               UShort_t    the event flags of the firmware
    board_fail          Bool_t
    samples_overlapped  UChar_t
    ch_mask             ULong64_t   the channels the event has
    n_samples           UInt_t      samples per channel
    ch0, ch1, ...       UShort_t[n_samples]   the samples of each channel read out (raw ADC counts)
Two TObjString go with the tree: "settings" (the file's header: the board, its model and serial, every
parameter as it was set, the run's whole configuration) and "note" (the run's note).
A second TTree "hk" holds the board's housekeeping when its RUN_BOARD_hk.csv is next to the file: one entry
per reading, a Double_t branch per column (time in Unix seconds, run_s, the temperatures in °C, the counters,
error_flags, ...; an empty cell is NaN).
Read in PyROOT: f = ROOT.TFile("NAME.root"); t = f.events; t.GetEntry(0); list(t.ch0)

The conversion prints "processed N %" lines, then reads the ROOT file back and prints
"check: N events in the raw file, M in the ROOT file"; exit code 0 when they agree (trigger IDs and
samples), 1 when they differ, 2 when a file cannot be read or the conversion fails in any other way.
"""

from __future__ import annotations

import json
import os
import sys
import time

import numpy as np

from vx27webdaq.core import rawdata


def side_note(raw_path: str, header: dict) -> str:
    """The run's note: from NAME.json next to the file (it follows edits during the run), else the header's."""
    try:
        with open(os.path.splitext(raw_path)[0] + ".json", encoding="utf-8") as f:
            note = json.load(f).get("note")
        if isinstance(note, str):
            return note
    except (OSError, ValueError, AttributeError):
        pass
    return str(header.get("note") or "")


def hk_path(raw_path: str, header: dict) -> str:
    """The board's housekeeping file of this data file (one per board and run, beside the first data file); "" when
    the header's run or board name cannot be part of a file name (a run named "../x" must not lead elsewhere)."""
    board = header.get("board") if isinstance(header.get("board"), dict) else {}
    run, name = rawdata.name_part(header.get("run")), rawdata.name_part(board.get("name"))
    return os.path.join(os.path.dirname(raw_path), f"{run}_{name}_hk.csv") if run and name else ""


def read_hk(path: str) -> dict[str, np.ndarray]:
    """A housekeeping file as columns of float64 (an empty or non-numeric cell: NaN)."""
    with open(path, encoding="utf-8") as f:
        names = f.readline().strip().split(",")
        rows = [line.rstrip("\n").split(",") for line in f if line.strip()]
    cols = {}
    for j, name in enumerate(names):
        vals = []
        for r in rows:
            try:
                vals.append(float(int(r[j], 0)) if r[j].startswith("0x") else float(r[j]))
            except (IndexError, ValueError):
                vals.append(float("nan"))
        cols[name] = np.asarray(vals, dtype=np.float64)
    return cols


def _popcount(x: np.ndarray) -> np.ndarray:
    b = x.astype(">u8").view(np.uint8).reshape(-1, 8)
    return np.unpackbits(b, axis=1).sum(axis=1)


def convert(raw_path: str, out_path: str, report=None) -> int:
    """Write the ROOT file; returns the number of events written."""
    import awkward as ak
    import uproot

    report = report or (lambda percent: None)
    with rawdata.RawFile(raw_path) as f:
        offs = f.index()
        hdr = f.headers()
        n = len(offs)
        union = int(np.bitwise_or.reduce(hdr["ch_mask"])) if n else 0
        chans = rawdata.mask_channels(union)
        per = np.maximum(_popcount(hdr["ch_mask"]), 1) if n else np.zeros(0)
        samples = ((hdr["n_words"].astype(np.int64) - rawdata.SCOPE_HEADER_WORDS) * rawdata.SAMPLES_PER_WORD // per
                   if n else np.zeros(0, dtype=np.int64))
        uniform = len(np.unique(samples)) <= 1
        s0 = int(samples[0]) if n else 0
        board = f.header.get("board", {})
        title = f"{f.header.get('run', '')} board {board.get('name', '')} ({board.get('model', '')} " \
                f"serial {board.get('serial', '')})"
        types = {"trigger_id": np.uint32, "timestamp": np.uint64, "time_ns": np.uint64, "flags": np.uint16,
                 "board_fail": np.bool_, "samples_overlapped": np.uint8, "ch_mask": np.uint64, "n_samples": np.uint32}
        for c in chans:
            types[f"ch{c}"] = ("u2", (s0,)) if uniform else "var * uint16"
        out = uproot.recreate(out_path)
        try:
            out["settings"] = json.dumps(dict(f.header, truncated=f.truncated, stop_record=f.stop_record(),
                                              start_record=f.start_record()), indent=1)
            out["note"] = side_note(raw_path, f.header)
            tree = out.mktree("events", types, title=title)
            done = 0
            for k0, h, channels, waves in f.batches(32 << 20):
                m = len(waves)
                data = {"trigger_id": h["trigger_id"], "timestamp": h["timestamp"],
                        "time_ns": h["timestamp"] * np.uint64(rawdata.TIMESTAMP_NS), "flags": h["flags"],
                        "board_fail": h["board_fail"], "samples_overlapped": h["samples_overlapped"],
                        "ch_mask": h["ch_mask"], "n_samples": np.full(m, waves.shape[2], dtype=np.uint32)}
                for c in chans:
                    arr = waves[:, channels.index(c), :] if c in channels else np.zeros((m, waves.shape[2]), np.uint16)
                    data[f"ch{c}"] = arr if uniform else ak.from_regular(ak.from_numpy(arr), axis=1)
                tree.extend(data)
                done += m
                report(100.0 * done / n)
            hk = hk_path(raw_path, f.header)
            if int(f.header.get("file_index") or 0) == 0 and os.path.isfile(hk):
                cols = read_hk(hk)
                if cols and len(next(iter(cols.values()))):
                    out.mktree("hk", {k: np.float64 for k in cols}, title=f"{title}: housekeeping").extend(cols)
        finally:
            out.close()
    return n


def check(raw_path: str, root_path: str) -> tuple[int, int, bool, str]:
    """(events in the raw file, entries in the ROOT file, same, what differs)."""
    import uproot
    with rawdata.RawFile(raw_path) as f:
        n_raw = len(f.index())
        ids = f.headers()["trigger_id"]
        picks = sorted({0, n_raw // 2, n_raw - 1}) if n_raw else []
        events = {k: f.event(k) for k in picks}
    with uproot.open(root_path) as r:
        t = r["events"]
        n_root = t.num_entries
        if n_root != n_raw:
            return n_raw, n_root, False, "the numbers of events differ"
        if n_raw == 0:
            return n_raw, n_root, True, ""
        if not np.array_equal(t["trigger_id"].array(library="np"), ids):
            return n_raw, n_root, False, "the trigger IDs differ"
        for k, ev in events.items():
            for c, w in zip(ev.channels, ev.waveforms):
                got = t[f"ch{c}"].array(library="np", entry_start=k, entry_stop=k + 1)[0]
                if not np.array_equal(np.asarray(got)[:len(w)], w):
                    return n_raw, n_root, False, f"the samples of event {k}, channel {c} differ"
    return n_raw, n_root, True, ""


def root_name(raw_path: str) -> str:
    base = raw_path[:-4] if raw_path.lower().endswith(".raw") else raw_path
    return base + ".root"


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if not args or len(args) > 2:
        print(__doc__)
        return 2
    raw = args[0]
    out = args[1] if len(args) > 1 else root_name(raw)
    last = [0.0]

    def report(p):
        now = time.monotonic()
        if now - last[0] >= 1.0 or p >= 100:
            last[0] = now
            print(f"processed {p:.1f} %", flush=True)

    t0 = time.monotonic()
    try:
        n = convert(raw, out, report)
        size = os.path.getsize(out)
    except (OSError, rawdata.RawFormatError) as err:
        print(f"ERROR: {err}", flush=True)
        return 2
    except Exception as err:            # a crash (uproot, awkward, numpy, memory) is a failure, never exit code 1,
        print(f"ERROR: the conversion failed: {err!r}", flush=True)     # which means "written, but it differs"
        return 2
    print(f"wrote {out}: {n} events in {time.monotonic() - t0:.1f} s ({size / 1e6:.1f} MB)", flush=True)
    try:
        n_raw, n_root, same, why = check(raw, out)
    except Exception as err:            # uproot raises many kinds for a damaged file
        print(f"ERROR: cannot read {out} back: {err}", flush=True)
        return 2
    print(f"check: {n_raw} events in the raw file, {n_root} in the ROOT file" + ("" if same else f"; {why}"), flush=True)
    return 0 if same else 1


if __name__ == "__main__":
    raise SystemExit(main())
