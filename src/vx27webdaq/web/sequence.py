"""Run sequences (like WaveDump2's settings scripts): runs taken one after the other without anyone at the page.

    repeat   N runs of the configuration, each of T seconds
    scan     one setting over a list of values, each value for R runs of T seconds:
             threshold   every channel's threshold (of one board, or of every board), ADC counts
             dc_offset   every channel's DC offset, %
             param       any board parameter (an expert parameter of the configuration: /par/NAME or
                         /ch/N/par/NAME), as text
Each run is an ordinary run of a copy of the configuration with the step's change (the configuration being
edited stays as it is); its note gets "sequence ID: run k of N · the change" after the note (an edit of the note
during the sequence keeps that part), and its config.json and run.json a "sequence" entry. The simulator setting
is the one the sequence started with. The next run starts a few seconds after one ends; when the boards are busy
then (a board check, Send to boards, the manual controller, another DAQ program) it waits and tries again for up
to BUSY_WAIT_S. A run that fails stops the sequence (unless asked not to); Stop on the Run page stops the run and
the sequence, Stop sequence lets the run going finish. No other run starts while a sequence goes. The sequence is
kept in the state folder; after a server restart it is marked interrupted, not resumed.
"""

from __future__ import annotations

import json
import math
import time

from vx27webdaq.core import config as C
from vx27webdaq.web.runs import BoardsBusy, RunError
from vx27webdaq.web.settings import write_atomic

PAUSE_S = 3.0               # between two runs
BUSY_RETRY_S = 5.0          # a run that finds the boards busy is tried again this often ...
BUSY_WAIT_S = 120.0         # ... for this long, then the sequence stops
MAX_STEPS = 1000
MAX_VALUES = 200
TARGETS = ("threshold", "dc_offset", "param")


def _values(raw, as_number: bool) -> list:
    if isinstance(raw, str):
        raw = [x for x in raw.replace(";", ",").split(",")]
    if not isinstance(raw, list):
        raise ValueError("values: a list, or text with commas")
    out = []
    for x in raw:
        if isinstance(x, bool) or not isinstance(x, (str, int, float)):
            raise ValueError("values: numbers or text, one per step")
        x = str(x).strip()
        if not x:
            continue
        if as_number:
            try:
                v = float(x)
            except ValueError:
                raise ValueError(f"values: {x[:20]} is not a number") from None
            if not math.isfinite(v):
                raise ValueError(f"values: {x[:20]} is not a finite number")
            out.append(v)
        else:
            out.append(x)
    if not out:
        raise ValueError("give at least one value")
    if len(out) > MAX_VALUES:
        raise ValueError(f"at most {MAX_VALUES} values")
    return out


def _whole(spec: dict, key: str, default: int = 0) -> int:
    """spec[key] as a whole number (absent: default); any other type is refused."""
    v = spec.get(key)
    if v is None or v == "":
        return default
    if isinstance(v, bool) or not isinstance(v, (int, float, str)):
        raise ValueError(f"{key} must be a whole number")
    try:
        x = float(v)
    except ValueError:
        raise ValueError(f"{key} must be a whole number") from None
    if not math.isfinite(x) or x != int(x):
        raise ValueError(f"{key} must be a whole number")
    return int(x)


class SequenceManager:
    def __init__(self, station):
        self.station = station
        self.seq: dict | None = None
        self._cfgs: list[C.DaqConfig] = []
        self._next_at: float | None = None
        self._load()

    # ---- the steps ----
    def plan(self, spec: dict) -> tuple[list[str], list[C.DaqConfig]]:
        """(label, configuration) of every run of a sequence: copies of the configuration being edited."""
        st = self.station
        if not isinstance(spec, dict):
            raise ValueError("a sequence is a JSON object")
        kind = spec.get("kind")
        duration = _whole(spec, "duration_s")
        if not 1 <= duration <= 7 * 86400:
            raise ValueError("each run of a sequence needs a run time from 1 s to 7 days")
        base = st.cfg.copy()
        base.run.duration_s = duration
        steps: list[tuple[C.DaqConfig, list[str]]] = []     # each distinct configuration, and the labels of its runs
        if kind == "repeat":
            n = _whole(spec, "count")
            if not 1 <= n <= MAX_STEPS:
                raise ValueError(f"from 1 to {MAX_STEPS} runs")
            steps = [(base, [f"repeat {k + 1} of {n}" for k in range(n)])]
        elif kind == "scan":
            target = spec.get("target")
            if target not in TARGETS:
                raise ValueError(f"the scan changes one of {', '.join(TARGETS)}")
            per = _whole(spec, "runs_per_value", 1)
            board = spec.get("board") or ""
            path = spec.get("path") or ""
            if not isinstance(board, str) or not isinstance(path, str):
                raise ValueError("board and path must be text")
            path = path.strip()
            boards = [b for b in base.boards if b.enabled and (not board or b.name == board)]
            if not boards:
                raise ValueError(f"there is no board {board} switched on in the configuration")
            if target == "param" and not C.EXTRA_RE.match(path):
                raise ValueError("the parameter is /par/NAME or /ch/N/par/NAME")
            values = _values(spec.get("values"), as_number=target != "param")
            if not 1 <= per <= 100 or len(values) * per > MAX_STEPS:
                raise ValueError(f"from 1 to 100 runs per value, at most {MAX_STEPS} runs in all")
            where = board or "every board"
            for v in values:
                cfg = base.copy()
                for b in [x for x in cfg.boards if x.enabled and (not board or x.name == board)]:
                    if target == "threshold":
                        for ch in b.channels:
                            ch.threshold = int(v)
                    elif target == "dc_offset":
                        for ch in b.channels:
                            ch.dc_offset = float(v)
                    else:
                        b.extra = [x for x in b.extra if x[0] != path] + [[path, str(v)]]
                shown = f"{v:g}" if isinstance(v, float) else v
                name = path if target == "param" else target
                steps.append((cfg, [f"{name} = {shown} on {where}" + (f" ({r + 1} of {per})" if per > 1 else "")
                                    for r in range(per)]))
        else:
            raise ValueError("the kind of sequence is repeat or scan")
        labels, cfgs = [], []
        for cfg, names in steps:            # each distinct configuration is checked once, not once per run
            bad = [p for p in C.check_config(cfg, check_paths=True) if p.level == "error"]
            if bad:
                raise C.ConfigError(f"run {len(labels) + 1} ({names[0]}): {bad[0].where}: {bad[0].message}")
            labels += names
            cfgs += [cfg.copy() for _ in names]
        return labels, cfgs

    # ---- going ----
    def start(self, user: str, spec: dict) -> dict:
        st = self.station
        if self.running():
            raise RunError("a sequence is going already")
        if st.runs.active is not None:
            raise RunError("a run is going: let it end first")
        if st.runs.waiting_for():
            raise RunError("the run of the server before this one may still be going: wait until the server follows it again")
        if st.boards.busy():
            raise RunError(f"the boards are busy ({st.boards.busy()}): try again in a moment")
        labels, cfgs = self.plan(spec)
        s = st.settings
        self.seq = {"id": time.strftime("%Y%m%d_%H%M%S"), "by": user or "?", "started": time.time(), "spec": spec,
                    "labels": labels, "index": 0, "state": "running", "runs": [], "note": st.run_note,
                    "message": "", "simulator": s.use_simulator, "sim_options": s.sim_options()}
        self._cfgs = cfgs
        st.activity(user, "started a run sequence", f"{len(labels)} runs: {labels[0]} …")
        self._start_step()
        if not self.running():              # its first run did not start: say why
            raise RunError(self.seq["state"])
        return self.view()

    def _start_step(self) -> None:
        s = self.seq
        k, n = s["index"], len(s["labels"])
        suffix = f"sequence {s['id']}: run {k + 1} of {n} · {s['labels'][k]}"
        info = {"id": s["id"], "run": k + 1, "of": n, "step": s["labels"][k]}
        try:
            run_dir = self.station.runs.start(s["by"], cfg=self._cfgs[k], note_suffix=suffix, extra={"sequence": info},
                                              simulator=s.get("simulator"), sim_options=s.get("sim_options"),
                                              from_sequence=True)
        except BoardsBusy as err:           # a board check, Send to boards, another DAQ program: wait and try again
            since = s.setdefault("waiting_since", time.time())
            if s["runs"] and time.time() - since < BUSY_WAIT_S:
                s["message"] = f"waiting: {err}"
                self._next_at = time.time() + BUSY_RETRY_S
                self._save()
                self.publish()
                return
            self._end(f"stopped: run {k + 1} could not start ({err})")
            return
        except (RunError, C.ConfigError, OSError) as err:
            self._end(f"stopped: run {k + 1} could not start ({err})")
            return
        s.pop("waiting_since", None)
        s["message"] = ""
        a = self.station.runs.active
        s["runs"].append({"index": k, "label": s["labels"][k], "run_dir": run_dir, "run_name": a.run_name if a else None,
                          "phase": "running"})
        self._save()
        self.publish()

    def run_ended(self, run_dir: str, phase: str) -> None:
        """RunManager calls this when a run ends; only the sequence's own run counts."""
        s = self.seq
        if not self.running() or not s["runs"] or s["runs"][-1]["run_dir"] != run_dir \
                or s["runs"][-1]["phase"] != "running":
            return
        s["runs"][-1]["phase"] = phase
        if phase != "finished" and s["spec"].get("stop_on_failure", True):
            self._end(f"stopped: run {s['index'] + 1} failed")
            return
        s["index"] += 1
        if s["index"] >= len(s["labels"]):
            self._end("finished")
            return
        self._next_at = time.time() + PAUSE_S
        self._save()
        self.publish()

    async def tick(self) -> None:
        if self._next_at is not None and time.time() >= self._next_at and self.station.runs.active is None:
            self._next_at = None
            if self.running():
                self._start_step()

    def stop(self, user: str, now: bool = False) -> None:
        if not self.running():
            raise RunError("no sequence is going")
        self._end(f"stopped by {user or '?'}")
        if now and self.station.runs.active is not None:
            self.station.runs.stop(user)

    def _end(self, state: str) -> None:
        self.seq["state"] = state
        self.seq["ended"] = time.time()
        self.seq["message"] = ""
        self.seq.pop("waiting_since", None)
        self._next_at = None
        self.station.activity(self.seq["by"], "run sequence", state)
        self._save()
        self.publish()

    def running(self) -> bool:
        return bool(self.seq and self.seq.get("state") == "running")

    # ---- for the page, and over a restart ----
    def view(self) -> dict | None:
        if not self.seq:
            return None
        s = self.seq
        return {**{k: v for k, v in s.items() if k != "spec"}, "spec": s.get("spec"), "count": len(s["labels"]),
                "next_at": self._next_at, "now": time.time()}

    def publish(self) -> None:
        self.station.publish({"type": "sequence", "sequence": self.view()})

    def _file(self):
        return self.station.state_dir / "sequence.json"

    def _save(self) -> None:
        try:
            write_atomic(self._file(), json.dumps(self.seq))
        except OSError:
            pass

    def _load(self) -> None:
        try:
            with open(self._file(), encoding="utf-8") as f:
                self.seq = json.load(f)
        except (OSError, ValueError):
            self.seq = None
            return
        if self.seq and self.seq.get("state") == "running":     # the server stopped during the sequence
            self.seq["state"] = "interrupted: the server restarted"
            self._save()
