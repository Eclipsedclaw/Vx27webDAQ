"""The sync check of a run: did every board record the same triggers? (WaveDump2 builds events across boards
with a "build matching window"; this is the same check, done while the data are taken.)

With one trigger for all boards (the master's trigger on the TRG chain, an external trigger, a logic unit) every
board counts the triggers it records, so the same trigger has the same trigger ID on every board, and with the
clock chain nearly the same timestamp (a slave sees the trigger a few ns later: the cable and its TRG-IN). For
every trigger ID that every board has passed ("settled"), the check counts:
    built        every board has it, and every timestamp is within the window of the first board's
    out of sync  every board has it, but some timestamp is farther off: the boards count differently, or lost
                 their common clock
    missing      per board: the IDs the other boards have and this one does not
and, per board, the difference of its timestamps from the first board's for the same ID (mean, σ, lowest,
highest): a fixed delay of a few ns is the cabling; one that moves means the clocks are not one.
The readers hand over each buffer's trigger IDs (made continuous over the 24-bit wrap) and timestamps.
"""

from __future__ import annotations

import numpy as np

TICK_NS = 8                 # one timestamp tick
LAG_IDS = 200_000           # a board this far behind the others: its missing triggers are counted anyway


class SyncCheck:
    def __init__(self, names: list[str], window_ns: float):
        self.names = list(names)
        self.window = max(1, int(round(window_ns / TICK_NS)))      # in ticks
        n = len(self.names)
        self.ids = [np.zeros(0, np.int64) for _ in range(n)]
        self.ts = [np.zeros(0, np.int64) for _ in range(n)]
        self.new: list[list] = [[] for _ in range(n)]
        self.last = [-1] * n                    # the highest ID each board sent
        self.settled = -1
        self.checked = self.built = self.out_of_sync = 0
        self.missing = [0] * n
        self.d_n = [0] * n
        self.d_sum = [0.0] * n
        self.d_sq = [0.0] * n
        self.d_min: list = [None] * n
        self.d_max: list = [None] * n
        self.first_bad: dict | None = None      # the first trigger out of sync, for the log

    def add(self, k: int, ids: np.ndarray, ts: np.ndarray) -> None:
        if len(ids):
            self.new[k].append((ids, ts))
            self.last[k] = max(self.last[k], int(ids[-1]))

    def process(self, final: bool = False) -> None:
        """Count the triggers every board has passed (all of them when final: the run is over)."""
        for k in range(len(self.names)):
            if self.new[k]:
                self.ids[k] = np.concatenate([self.ids[k]] + [a for a, _ in self.new[k]])
                self.ts[k] = np.concatenate([self.ts[k]] + [b for _, b in self.new[k]])
                self.new[k] = []
        top = max(self.last)
        if top < 0:
            return
        upto = top if final else max(min(self.last), top - LAG_IDS)
        if upto <= self.settled:
            return
        parts = []
        for k in range(len(self.names)):
            cut = int(np.searchsorted(self.ids[k], upto, side="right"))
            parts.append((self.ids[k][:cut], self.ts[k][:cut]))
            self.ids[k], self.ts[k] = self.ids[k][cut:], self.ts[k][cut:]
        self.settled = upto
        union = np.unique(np.concatenate([p[0] for p in parts]))
        if not len(union):
            return
        self.checked += len(union)
        present = []
        for k, (ids, _) in enumerate(parts):
            have = np.isin(union, ids)
            self.missing[k] += int((~have).sum())
            present.append(have)
        every = np.logical_and.reduce(present)
        common = union[every]
        if not len(common):
            return
        stamps = []
        for ids, ts in parts:                   # an ID twice in a board (a repeated ID): its first event counts
            pos = np.searchsorted(ids, common)
            stamps.append(ts[pos])
        ok = np.ones(len(common), bool)
        for k in range(1, len(parts)):
            d = stamps[k] - stamps[0]
            ok &= np.abs(d) <= self.window
            dn = d.astype(float) * TICK_NS
            self.d_n[k] += len(dn)
            self.d_sum[k] += float(dn.sum())
            self.d_sq[k] += float((dn * dn).sum())
            lo, hi = float(dn.min()), float(dn.max())
            self.d_min[k] = lo if self.d_min[k] is None else min(self.d_min[k], lo)
            self.d_max[k] = hi if self.d_max[k] is None else max(self.d_max[k], hi)
        self.built += int(ok.sum())
        bad = int((~ok).sum())
        self.out_of_sync += bad
        if bad and self.first_bad is None:
            i = int(np.nonzero(~ok)[0][0])
            self.first_bad = {"trigger_id": int(common[i]) & 0xFFFFFF,
                              "timestamps": {n: int(s[i]) for n, s in zip(self.names, stamps)}}

    def view(self) -> dict:
        offsets = {}
        for k, name in enumerate(self.names[1:], start=1):
            n = self.d_n[k]
            if n:
                mean = self.d_sum[k] / n
                offsets[name] = {"n": n, "mean_ns": mean, "std_ns": max(0.0, self.d_sq[k] / n - mean * mean) ** 0.5,
                                 "min_ns": self.d_min[k], "max_ns": self.d_max[k]}
        return {"applies": True, "reference": self.names[0], "window_ns": self.window * TICK_NS, "checked": self.checked,
                "built": self.built, "out_of_sync": self.out_of_sync,
                "missing": {n: m for n, m in zip(self.names, self.missing)}, "offsets": offsets,
                "first_bad": self.first_bad}

    def summary(self) -> str:
        v = self.view()
        miss = ", ".join(f"{n} {m}" for n, m in v["missing"].items() if m) or "none"
        offs = ", ".join(f"{n} {o['mean_ns']:+.0f} ns ({o['min_ns']:+.0f}…{o['max_ns']:+.0f})" for n, o in v["offsets"].items())
        return (f"sync check: {v['checked']} trigger IDs, {v['built']} on every board within {v['window_ns']} ns, "
                f"{v['out_of_sync']} out of sync; missing: {miss}" + (f"; timestamps against {v['reference']}: {offs}" if offs else ""))
